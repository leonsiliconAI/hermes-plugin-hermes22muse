"""Persist each handoff's Slack thread, Muse side chat, transcript and delivery queues."""

import asyncio
import fcntl
import hashlib
import logging
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from ._pythonmonkey import call
from .muse import STAMP

LOG = logging.getLogger(__name__)
MAX_WORKERS = 8
# Muse counts as done after a completed reply and this long without new rows or unfinished replies,
# as muse-mcp's own muse_chat_ask does: agentic work can continue after a first answer.
QUIET = 8
# A handoff idle for a day stops polling (policy.ts pollDelay).
SLEEP = 10 * 365 * 86400


def operation_id(*parts):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "hermes22muse/" + "/".join(parts)))


def marker(message_id):
    return f"[Hermes delivery {message_id}]\n"


def slack_permalink(channel, ts):
    return call(__file__, "permalink", channel, ts)


def side_chat_url(job_id):
    return call(__file__, "sideChat", job_id)


def slack_chunks(text):
    yield from call(__file__, "slackChunks", text)


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
                CREATE TABLE IF NOT EXISTS transcript (
                    seq INTEGER PRIMARY KEY, handoff TEXT NOT NULL, id TEXT NOT NULL,
                    role TEXT NOT NULL, text TEXT NOT NULL,
                    UNIQUE(handoff, id)
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(handoffs)")}
            for column in ("busy INTEGER NOT NULL DEFAULT 0", "polled REAL NOT NULL DEFAULT 0",
                           "changed REAL NOT NULL DEFAULT 0"):
                if column.split()[0] not in columns:
                    db.execute(f"ALTER TABLE handoffs ADD COLUMN {column}")

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def create(self, team, channel, request, thread, actor, muse_text, thread_text):
        """Start a handoff for one Slack request. Returns its ID, or None if one already exists."""
        job_id = operation_id(team, channel, request)
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM handoffs WHERE id=? OR (team=? AND channel=? AND root=?)",
                          (job_id, team, channel, thread)).fetchone():
                return None
            db.execute(
                """INSERT INTO handoffs(id, team, channel, request, actor, root, activity)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (job_id, team, channel, request, actor, thread, now),
            )
            db.execute("INSERT INTO inbound(handoff, ts, text) VALUES (?, ?, ?)",
                       (job_id, request, muse_text))
            self._add_output(db, job_id, "handoff", list(slack_chunks(thread_text)))
        return job_id

    def follow_up(self, job_id, text, thread_text):
        """Queue a message for Muse and show it in the handoff's thread. Returns its key."""
        key = f"h{time.time():.6f}"
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO inbound(handoff, ts, text) VALUES (?, ?, ?)", (job_id, key, text))
            self._add_output(db, job_id, "hermes/" + key, list(slack_chunks(thread_text)))
            db.execute("UPDATE handoffs SET due=0, activity=? WHERE id=?", (time.time(), job_id))
        return key

    def handoff(self, job_id):
        with self.connection() as db:
            return db.execute("SELECT * FROM handoffs WHERE id=?", (job_id,)).fetchone()

    def find(self, team, channel, request=None, thread=None):
        """The handoff started by a Slack message, or the one whose thread a message is in."""
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM handoffs WHERE team=? AND channel=? AND (request=? OR root=?)",
                (team, channel, request or "", thread or ""),
            ).fetchone()

    def touch(self, job_id):
        with self.connection() as db:
            db.execute("UPDATE handoffs SET activity=?, due=MIN(due, ?) WHERE id=?",
                       (time.time(), time.time() + 1, job_id))

    def answer(self, job_id):
        """Muse's messages after Hermes's latest delivery once Muse has finished, else the state."""
        with self.connection() as db:
            job = db.execute("SELECT busy, polled, changed FROM handoffs WHERE id=?", (job_id,)).fetchone()
            last = db.execute(
                "SELECT receipt FROM inbound WHERE handoff=? ORDER BY rowid DESC LIMIT 1", (job_id,),
            ).fetchone()
            if not job or not last or not last["receipt"]:
                return "not_delivered", []
            sent = db.execute("SELECT seq FROM transcript WHERE handoff=? AND id=?",
                              (job_id, last["receipt"])).fetchone()
            rows = db.execute(
                "SELECT role, text FROM transcript WHERE handoff=? AND seq>? ORDER BY seq",
                (job_id, sent["seq"] if sent else 2 ** 62),
            ).fetchall()
            messages = [{"from": "muse" if row["role"] == "assistant" else "leon_in_muse",
                         "text": row["text"]} for row in rows]
            done = (rows and rows[-1]["role"] == "assistant" and not job["busy"]
                    and job["polled"] - job["changed"] >= QUIET)
            return ("answered" if done else "working"), messages

    def due(self, actor, running=()):
        with self.connection() as db:
            placeholders = ",".join("?" for _ in running)
            return db.execute(
                "SELECT * FROM handoffs WHERE actor=? AND due<=? AND root IS NOT NULL "
                f"AND id NOT IN ({placeholders}) ORDER BY due LIMIT ?",
                (actor, time.time(), *running, MAX_WORKERS - len(running)),
            ).fetchall()

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

    def collect(self, job_id, messages, full, busy=False):
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            echoed = {
                row["receipt"] for row in db.execute(
                    "SELECT receipt FROM inbound WHERE handoff=? AND receipt IS NOT NULL", (job_id,),
                )
            }
            ids = [message.id for message in messages]
            marks = ",".join("?" for _ in ids)
            known = {row["id"] for row in db.execute(
                f"SELECT id FROM transcript WHERE handoff=? AND id IN ({marks})", (job_id, *ids),
            )}
            # Handoffs from 1.x relayed messages before the transcript existed.
            posted = {row["event"] for row in db.execute(
                f"SELECT DISTINCT event FROM outbound WHERE handoff=? AND event IN ({marks})", (job_id, *ids),
            )}
            if full and not known and not posted and messages:
                # A first full window can also miss earlier rows before the first poll.
                self._add_output(db, job_id, "gap/" + ids[0], [
                    "Muse’s recent history filled its read window. Some earlier messages may be missing.",
                ])
            added = False
            for message in messages:
                if message.id in known:
                    continue
                added = True
                db.execute("INSERT OR IGNORE INTO transcript(handoff, id, role, text) VALUES (?, ?, ?, ?)",
                           (job_id, message.id, message.role, message.text))
                if message.id in posted:
                    continue
                parts = call(__file__, "slackDelivery",
                             {"id": message.id, "role": message.role, "text": message.text},
                             message.id in echoed, STAMP)
                if parts != [None]:
                    self._add_output(db, job_id, message.id, parts)
            old = db.execute("SELECT busy FROM handoffs WHERE id=?", (job_id,)).fetchone()
            changed = added or bool(old and old["busy"] != int(busy))
            db.execute(
                "UPDATE handoffs SET busy=?, polled=?, changed=CASE WHEN ? THEN ? ELSE changed END,"
                " activity=CASE WHEN ? THEN ? ELSE activity END WHERE id=?",
                (int(busy), now, changed, now, added, now, job_id),
            )

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
                delay = call(__file__, "pollDelay", now - active, bool(pending), SLEEP)
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

    async def post(self, job, text, key, thread):
        channel = job["channel"]
        client = self.client(job)
        response = await asyncio.wait_for(client.chat_postMessage(
            channel=channel, text=text, thread_ts=thread, mrkdwn=False, link_names=False,
            unfurl_links=False, unfurl_media=False, client_msg_id=key,
        ), 15)
        if not response.get("ok") or not response.get("ts") or response.get("channel") != channel:
            raise RuntimeError("Slack returned no matching send receipt")
        # Replies in the handoff thread reach Hermes even where channels require a mention.
        register = getattr(self.adapter, "_register_mentioned_thread", None)
        if callable(register):
            register(thread, job["team"])
        return response["ts"]


class Relay:
    def __init__(self, store, slack, muse, actor):
        self.store, self.slack, self.muse, self.actor = store, slack, muse, actor

    async def send_to_muse(self, muse, job, entry):
        job_id, ts = job["id"], entry["ts"]
        first = ts == job["request"]
        prefix = marker(operation_id(job_id, ts))
        if first:
            await muse.ensure_registered(job_id)
        if entry["attempted"] and (not first or await muse.exists(job_id)):
            messages, _, _ = await muse.history(job_id)
            for message in messages:
                if message.role == "user" and message.text.startswith(STAMP + prefix):
                    self.store.accepted(job_id, ts, message.id)
                    return
        self.store.attempted(job_id, ts)
        text = entry["text"]
        if len((STAMP + prefix + text).encode("utf-8")) > 64 * 1024:
            text = text.encode("utf-8")[:60 * 1024].decode("utf-8", errors="ignore")
            text += f"\n\n[Shortened. The rest is in the Slack thread] {slack_permalink(job['channel'], job['root'])}"
        receipt = await muse.send(job_id, prefix + text)
        self.store.accepted(job_id, ts, receipt)

    async def tick(self, job):
        job_id, root = job["id"], job["root"]
        try:
            # Deliver durable thread posts even when the Muse connection is down.
            for item in self.store.pending_output(job_id):
                key = operation_id(job_id, hashlib.sha256(item["event"].encode()).hexdigest(), str(item["part"]))
                await self.slack.post(job, item["text"], key, thread=root)
                self.store.delivered(item["seq"])
            async with asyncio.timeout(45):
                async with self.muse.connect() as muse:
                    entry = self.store.next_inbound(job_id)
                    if entry:
                        await self.send_to_muse(muse, job, entry)
                    messages, full, busy = await muse.history(job_id)
                    self.store.collect(job_id, messages, full, busy)
        except Exception as exc:
            headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
            retry_after = str(headers.get("Retry-After", headers.get("retry-after", "")))
            retry = int(call(__file__, "retryDelay", job["failures"], retry_after))
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
