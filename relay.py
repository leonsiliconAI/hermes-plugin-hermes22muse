"""Persist the Slack root, Muse side chat and both delivery queues."""

import asyncio
import fcntl
import hashlib
import logging
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from html import escape

from .muse import MuseError, STAMP

LOG = logging.getLogger(__name__)
ACTIVE_POLL = 5
IDLE_POLL = 60
ACTIVE_FOR = 300
MAX_WORKERS = 8


def operation_id(*parts):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "hermes22muse/" + "/".join(parts)))


def marker(message_id):
    return f"[Hermes delivery {message_id}]\n"


def slack_permalink(channel, ts):
    return f"https://app.slack.com/archives/{channel}/p{ts.replace('.', '')}"


def slack_chunks(text):
    # Slack recommends a 4,000-character limit. Reserve room for the label and escaping.
    while text:
        cut = min(len(text), 3400)
        while len(escape(text[:cut], quote=False)) > 3400:
            cut //= 2
        if cut < len(text):
            newline = text.rfind("\n", cut // 2, cut)
            if newline >= 0:
                cut = newline + 1
        yield escape(text[:cut], quote=False)
        text = text[cut:]


class Store:
    def __init__(self, directory):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory = directory
        self.path = directory / "relay.db"
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS handoffs (
                    id TEXT PRIMARY KEY, team TEXT NOT NULL, channel TEXT NOT NULL,
                    request TEXT NOT NULL, actor TEXT NOT NULL, root TEXT,
                    root_attempt REAL, due REAL NOT NULL DEFAULT 0, activity REAL NOT NULL,
                    failures INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(team, channel, root)
                );
                CREATE INDEX IF NOT EXISTS handoffs_due ON handoffs(due);
                CREATE TABLE IF NOT EXISTS inbound (
                    handoff TEXT NOT NULL, ts TEXT NOT NULL, text TEXT,
                    attempted INTEGER NOT NULL DEFAULT 0, receipt TEXT,
                    PRIMARY KEY(handoff, ts)
                );
                CREATE TABLE IF NOT EXISTS outbound (
                    seq INTEGER PRIMARY KEY, handoff TEXT NOT NULL, event TEXT NOT NULL,
                    part INTEGER NOT NULL, text TEXT,
                    UNIQUE(handoff, event, part)
                );
            """)

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def enqueue(self, team, channel, thread, ts, actor, text):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            root = db.execute(
                "SELECT id, actor FROM handoffs WHERE team=? AND channel=? AND root=?",
                (team, channel, thread),
            ).fetchone()
            if root and root["actor"] != actor:
                return False
            job_id = root["id"] if root else operation_id(team, channel, ts)
            now = time.time()
            db.execute(
                """INSERT OR IGNORE INTO handoffs(id, team, channel, request, actor, activity)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (job_id, team, channel, ts, actor, now),
            )
            if db.execute(
                "INSERT OR IGNORE INTO inbound(handoff, ts, text) VALUES (?, ?, ?)",
                (job_id, ts, text),
            ).rowcount:
                db.execute("UPDATE handoffs SET due=0, activity=? WHERE id=?", (now, job_id))
            return True

    def due(self, actor, running=()):
        with self.connection() as db:
            placeholders = ",".join("?" for _ in running)
            return db.execute(
                "SELECT * FROM handoffs WHERE actor=? AND due<=? "
                f"AND id NOT IN ({placeholders}) ORDER BY due LIMIT ?",
                (actor, time.time(), *running, MAX_WORKERS - len(running)),
            ).fetchall()

    def root_attempted(self, job_id):
        with self.connection() as db:
            db.execute(
                "UPDATE handoffs SET root_attempt=COALESCE(root_attempt, ?) WHERE id=?",
                (time.time(), job_id),
            )

    def root_sent(self, job_id, root):
        with self.connection() as db:
            db.execute("UPDATE handoffs SET root=? WHERE id=?", (root, job_id))

    def next_inbound(self, job_id):
        with self.connection() as db:
            return db.execute(
                """SELECT i.* FROM inbound i JOIN handoffs h ON h.id=i.handoff
                   WHERE i.handoff=? AND i.receipt IS NULL
                   ORDER BY i.ts != h.request, i.ts LIMIT 1""",
                (job_id,),
            ).fetchone()

    def attempted(self, job_id, ts):
        with self.connection() as db:
            db.execute("UPDATE inbound SET attempted=1 WHERE handoff=? AND ts=?", (job_id, ts))

    def accepted(self, job_id, ts, receipt):
        with self.connection() as db:
            db.execute(
                "UPDATE inbound SET receipt=?, text=NULL WHERE handoff=? AND ts=?",
                (receipt, job_id, ts),
            )

    def collect(self, job_id, messages, full):
        if not messages:
            return
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            echoed = {
                row["receipt"] for row in db.execute(
                    "SELECT receipt FROM inbound WHERE handoff=? AND receipt IS NOT NULL", (job_id,),
                )
            }
            ids = [message.id for message in messages]
            known = {row["event"] for row in db.execute(
                "SELECT DISTINCT event FROM outbound WHERE handoff=? AND event IN ("
                + ",".join("?" for _ in ids) + ")",
                (job_id, *ids),
            )}
            if full and not known:
                # A first full window can also miss earlier rows before the first poll.
                self._add_output(db, job_id, "gap/" + ids[0], [
                    "Muse’s recent history filled its read window. Some earlier messages may be missing.",
                ])
            added = False
            for message in messages:
                if message.id in known:
                    continue
                if message.role == "user" and (
                    message.id in echoed or message.text.startswith(STAMP + "[Hermes delivery ")
                ):
                    self._add_output(db, job_id, message.id, [None])
                    continue
                label = "Muse" if message.role == "assistant" else "You in Muse"
                added |= self._add_output(db, job_id, message.id, [
                    f"{label}\n{chunk}" for chunk in slack_chunks(message.text)
                ])
            if added:
                db.execute("UPDATE handoffs SET activity=? WHERE id=?", (time.time(), job_id))

    @staticmethod
    def _add_output(db, job_id, event, parts):
        changed = False
        for part, text in enumerate(parts):
            changed |= db.execute(
                "INSERT OR IGNORE INTO outbound(handoff, event, part, text) VALUES (?, ?, ?, ?)",
                (job_id, event, part, text),
            ).rowcount > 0
        return changed

    def pending_output(self, job_id):
        with self.connection() as db:
            return db.execute(
                """SELECT * FROM outbound WHERE handoff=? AND text IS NOT NULL
                   ORDER BY seq LIMIT 4""",
                (job_id,),
            ).fetchall()

    def delivered(self, seq):
        with self.connection() as db:
            db.execute("UPDATE outbound SET text=NULL WHERE seq=?", (seq,))

    def schedule(self, job, retry=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            if retry is None:
                active = db.execute("SELECT activity FROM handoffs WHERE id=?", (job["id"],)).fetchone()[0]
                pending = (self._has_pending(db, job["id"], "inbound")
                           or self._has_pending(db, job["id"], "outbound"))
                delay = 1 if pending else ACTIVE_POLL if now - active < ACTIVE_FOR else IDLE_POLL
                failures = 0
            else:
                delay = retry
                failures = job["failures"] + 1
            db.execute("UPDATE handoffs SET due=?, failures=? WHERE id=?", (now + delay, failures, job["id"]))

    @staticmethod
    def _has_pending(db, job_id, table):
        # The text is cleared only after the destination acknowledges delivery.
        return db.execute(
            f"SELECT 1 FROM {table} WHERE handoff=? AND text IS NOT NULL LIMIT 1",
            (job_id,),
        ).fetchone()


class Slack:
    def __init__(self, adapter):
        self.adapter = adapter

    def client(self, job):
        channel, team = job["channel"], job["team"]
        if self.adapter._outbound_blocked(channel, "hermes22muse send to"):
            raise RuntimeError("Slack delivery blocked")
        if team not in self.adapter._team_clients:
            raise RuntimeError("Slack workspace is disconnected")
        return self.adapter._get_client(channel, team_id=team)

    async def recover_root(self, job, key, link):
        """Find a previously accepted root when its POST receipt was lost."""
        team, channel = job["team"], job["channel"]
        bot = self.adapter._team_bot_user_ids.get(team)
        if not bot:
            return None
        client = self.client(job)
        cursor = ""
        async with asyncio.timeout(20):
            while True:
                response = await client.conversations_history(
                    channel=channel, oldest=f"{job['root_attempt'] - 10:.6f}",
                    limit=100, cursor=cursor or None,
                )
                if not response.get("ok"):
                    raise RuntimeError("Slack handoff lookup failed")
                for message in response.get("messages", []):
                    if message.get("user") == bot and (
                        message.get("client_msg_id") == key or link in (message.get("text") or "")
                    ):
                        return message["ts"]
                cursor = (response.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    if response.get("has_more"):
                        raise RuntimeError("Slack handoff lookup has no continuation cursor")
                    return None

    async def post(self, job, text, key, thread=None):
        channel, team = job["channel"], job["team"]
        client = self.client(job)
        options = {"thread_ts": thread} if thread else {}
        response = await asyncio.wait_for(client.chat_postMessage(
            channel=channel, text=text, mrkdwn=thread is None, link_names=False,
            unfurl_links=False, unfurl_media=False, client_msg_id=key, **options,
        ), 15)
        if not response.get("ok") or not response.get("ts") or response.get("channel") != channel:
            raise RuntimeError("Slack returned no matching send receipt")
        if thread is None:
            self.adapter._register_mentioned_thread(response["ts"], team)
        return response["ts"]


class Relay:
    def __init__(self, store, slack, muse, actor):
        self.store, self.slack, self.muse, self.actor = store, slack, muse, actor

    async def send_to_muse(self, muse, job, entry):
        job_id, ts = job["id"], entry["ts"]
        first = ts == job["request"]
        delivery_id = operation_id(job_id, ts)
        prefix = marker(delivery_id)
        if first:
            await muse.ensure_registered(job_id)
        if entry["attempted"] and (not first or await muse.exists(job_id)):
            messages, _ = await muse.history(job_id)
            for message in messages:
                if message.role == "user" and message.text.startswith(STAMP + prefix):
                    self.store.accepted(job_id, ts, message.id)
                    return
        self.store.attempted(job_id, ts)
        if first:
            request = slack_permalink(job["channel"], ts)
            text = ("This side chat was opened for a Hermes request. Your chat replies are relayed "
                    "to the matching Slack thread. Work on the request below and reply here.\n"
                    f"Original Slack message: {request}\n\n"
                    f"{entry['text']}")
        else:
            text = f"Follow-up from the linked Slack thread\n\n{entry['text']}"
        if len((STAMP + prefix + text).encode("utf-8")) > 64 * 1024:
            text = text.encode("utf-8")[:60 * 1024].decode("utf-8", errors="ignore")
            text += f"\n\n[More text in the Slack message] {slack_permalink(job['channel'], ts)}"
        receipt = await muse.send(job_id, prefix + text)
        self.store.accepted(job_id, ts, receipt)

    async def tick(self, job):
        job_id = job["id"]
        try:
            root = job["root"]
            if not root:
                permalink = slack_permalink(job["channel"], job["request"])
                link = f"https://muse.ai/thread/{job_id}"
                key = operation_id(job_id, "root")
                if job["root_attempt"]:
                    root = await self.slack.recover_root(job, key, link)
                mention = "" if job["channel"].startswith("D") else f"<@{job['actor']}> "
                if not root:
                    self.store.root_attempted(job_id)
                    root = await self.slack.post(
                        job, f"{mention}I’m handing <{permalink}|your request> to Muse. "
                        f"Reply in this thread to continue, or <{link}|open the Muse side chat>.",
                        key,
                    )
                self.store.root_sent(job_id, root)
            # Deliver durable replies even when the Muse connection is down.
            for item in self.store.pending_output(job_id):
                key = operation_id(job_id, hashlib.sha256(item["event"].encode()).hexdigest(), str(item["part"]))
                await self.slack.post(job, item["text"], key, thread=root)
                self.store.delivered(item["seq"])
            async with asyncio.timeout(45):
                async with self.muse.connect() as muse:
                    entry = self.store.next_inbound(job_id)
                    if entry:
                        await self.send_to_muse(muse, job, entry)
                    messages, full = await muse.history(job_id)
                    self.store.collect(job_id, messages, full)
        except Exception as exc:
            retry = min(300, 5 * 2 ** min(job["failures"], 6))
            headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
            retry_after = str(headers.get("Retry-After", headers.get("retry-after", "")))
            if retry_after.isdigit():
                retry = max(retry, int(retry_after))
            LOG.warning("hermes22muse: handoff %s retry in %ss (%s)", job_id, retry, type(exc).__name__)
            self.store.schedule(job, retry=retry)
        else:
            self.store.schedule(job)

    async def run(self):
        with (self.store.directory / "relay.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                LOG.warning("hermes22muse: another gateway owns the relay")
                return
            running = set()
            async with asyncio.TaskGroup() as tasks:
                while True:
                    if self.slack.adapter._running and len(running) < MAX_WORKERS:
                        for job in self.store.due(self.actor, running):
                            job_id = job["id"]
                            running.add(job_id)
                            task = tasks.create_task(self.tick(job))
                            task.add_done_callback(lambda _, key=job_id: running.discard(key))
                    await asyncio.sleep(1)
