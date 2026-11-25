"""Open and update the Slack message that hosts an issue's collaboration thread."""

import asyncio
import functools
import logging
import time
import uuid
from weakref import WeakValueDictionary

from ._pythonmonkey import call

LOG = logging.getLogger(__name__)


class Progress:
    def __init__(self, connection):
        self.connection = connection
        self._opening = WeakValueDictionary()
        with connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS progress (
                    team TEXT, channel TEXT, root TEXT, version INTEGER NOT NULL DEFAULT 0,
                    published INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0,
                    card TEXT, summary TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(team, channel, root)
                );
                CREATE TABLE IF NOT EXISTS progress_reports (
                    team TEXT, channel TEXT, root TEXT, agent TEXT, ts TEXT NOT NULL,
                    revision TEXT NOT NULL, text TEXT NOT NULL,
                    PRIMARY KEY(team, channel, root, agent)
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(progress)")}
            if "answer" not in columns:
                db.execute("ALTER TABLE progress ADD COLUMN answer TEXT NOT NULL DEFAULT ''")
            if "plan" not in columns:
                db.execute("ALTER TABLE progress ADD COLUMN plan TEXT NOT NULL DEFAULT ''")
            if "request" not in columns:
                # Leon's message that opened the card: the turn that owes him a plan.
                db.execute("ALTER TABLE progress ADD COLUMN request TEXT")

    async def open_thread(self, slack, team, channel, request=None):
        """Return the issue's thread, posting the top-level progress message on first use."""
        async with self._opening.setdefault((team, channel), asyncio.Lock()):
            with self.connection() as db:
                existing = db.execute(
                    "SELECT root FROM progress WHERE team=? AND channel=? "
                    "ORDER BY COALESCE(root=card, 0) DESC, root DESC LIMIT 1", (team, channel),
                ).fetchone()
            # Existing issues may have agents already committed to the original request's thread.
            if existing is not None:
                return existing["root"]
            ts = await self._publish(slack, dict(team=team, channel=channel, root=None,
                                                card=None, summary="", reports=[], answer="", plan=""))
            with self.connection() as db:
                db.execute("INSERT INTO progress(team, channel, root, card, request) VALUES (?, ?, ?, ?, ?)",
                           (team, channel, ts, ts, request))
            return ts

    def needs_plan(self, route, request):
        """Whether ``request`` opened this card and Hermes hasn't said yet what it will do."""
        with self.connection() as db:
            return db.execute("SELECT 1 FROM progress WHERE team=? AND channel=? AND root=? AND card=root "
                              "AND request=? AND plan=''", (*route, request)).fetchone() is not None

    def set_plan(self, route, plan):
        with self.connection() as db:
            self._changed(db, route)
            db.execute("UPDATE progress SET plan=? WHERE team=? AND channel=? AND root=?", (plan, *route))

    def is_thread(self, team, channel, root):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM progress WHERE team=? AND channel=? AND root=? AND card=root",
                              (team, channel, root)).fetchone() is not None

    @staticmethod
    def _changed(db, route):
        db.execute("INSERT OR IGNORE INTO progress(team, channel, root) VALUES (?, ?, ?)", route)
        db.execute("UPDATE progress SET version=version+1 WHERE team=? AND channel=? AND root=?", route)

    def report(self, db, route, agent, ts, revision, text):
        changed = db.execute("""
            INSERT INTO progress_reports(team, channel, root, agent, ts, revision, text)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(team, channel, root, agent) DO UPDATE
            SET ts=excluded.ts, revision=excluded.revision, text=excluded.text
            WHERE excluded.ts > progress_reports.ts OR
                  (excluded.ts = progress_reports.ts AND excluded.revision > progress_reports.revision)
        """, (*route, agent, ts, revision, text[:4000])).rowcount
        if changed:
            self._changed(db, route)

    def edit_report(self, route, agent, ts, revision, text):
        with self.connection() as db:
            changed = db.execute("UPDATE progress_reports SET revision=?, text=? "
                                 "WHERE team=? AND channel=? AND root=? AND agent=? AND ts=? AND revision<?",
                                 (revision, text[:4000], *route, agent, ts, revision)).rowcount
            if changed:
                self._changed(db, route)

    def update(self, route, summary):
        with self.connection() as db:
            self._changed(db, route)
            db.execute("UPDATE progress SET summary=?, answer='' WHERE team=? AND channel=? AND root=?",
                       (summary, *route))

    def has_card(self, route):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM progress WHERE team=? AND channel=? AND root=? AND card IS NOT NULL",
                              route).fetchone() is not None

    def finish(self, route, answer):
        with self.connection() as db:
            db.execute("UPDATE progress SET version=version+1, summary='', answer=? "
                       "WHERE team=? AND channel=? AND root=? AND answer!=?", (answer, *route, answer))

    def recent(self, team, channel):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT root, card, summary FROM progress WHERE team=? AND channel=? "
                "ORDER BY root DESC LIMIT 8", (team, channel))]

    def read(self, route):
        with self.connection() as db:
            db.execute("BEGIN")
            card = db.execute("SELECT * FROM progress WHERE team=? AND channel=? AND root=?", route).fetchone()
            if card is None:
                return None
            reports = db.execute("SELECT agent, ts, text FROM progress_reports "
                                 "WHERE team=? AND channel=? AND root=? ORDER BY agent", route)
            return {**dict(card), "reports": [dict(row) for row in reports]}

    async def flush(self, slack):
        with self.connection() as db:
            routes = db.execute("SELECT team, channel, root FROM progress "
                                "WHERE version > published AND due <= ? ORDER BY due LIMIT 8",
                                (time.time(),)).fetchall()
        for route in map(tuple, routes):
            entry = self.read(route)
            try:
                ts = await self._publish(slack, entry)
            except Exception as exc:
                response = getattr(exc, "response", None)
                code = (getattr(response, "data", None) or {}).get("error")
                headers = getattr(response, "headers", {}) or {}
                delay = call(__file__, "retryDelay", 0, str(headers.get("Retry-After", "")))
                with self.connection() as db:
                    db.execute("UPDATE progress SET due=?, card=CASE WHEN ? AND card!=root THEN NULL ELSE card END "
                               "WHERE team=? AND channel=? AND root=?",
                               (time.time() + max(delay, 15), code == "message_not_found", *route))
                LOG.warning("Slack task progress delivery deferred for %s:%s (%s)",
                            entry["channel"], entry["root"], code or type(exc).__name__)
            else:
                with self.connection() as db:
                    db.execute("UPDATE progress SET card=?, published=?, due=0 "
                               "WHERE team=? AND channel=? AND root=?",
                               (ts, entry["version"], *route))

    async def _publish(self, slack, entry):
        channel, root = entry["channel"], entry["root"]
        client = slack.client(entry)
        thread_link = root if root != entry["card"] else None
        text = call(__file__, "progressMessage", channel, thread_link, entry["summary"], entry["reports"],
                    entry["answer"], entry["plan"])
        args = dict(channel=channel, text=text, link_names=False)
        if entry["card"]:
            response = await asyncio.wait_for(client.chat_update(ts=entry["card"], **args), 15)
        else:
            key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"slack-task-progress/{entry['team']}/{channel}/{root}"))
            response = await asyncio.wait_for(client.chat_postMessage(
                client_msg_id=key, unfurl_links=False, unfurl_media=False, **args), 15)
        if (not response.get("ok") or response.get("channel") != channel or not response.get("ts")
                or (entry["card"] and response["ts"] != entry["card"])):
            raise RuntimeError("Slack returned no matching progress message")
        return response["ts"]


def wire_progress_delivery(adapter, progress):
    """Use the progress message for completed gateway replies on an existing task route."""
    send = getattr(adapter, "send", None)
    draft = getattr(adapter, "send_draft", None)
    final = getattr(adapter, "send_final_ledgered", None)
    if not (callable(send) and callable(final)):
        return lambda: None

    from gateway.platforms.base import SendResult

    active = True

    def card_route(chat_id, metadata):
        if active and metadata and metadata.get("slack_team_id") and metadata.get("thread_id"):
            route = (metadata["slack_team_id"], chat_id, metadata["thread_id"])
            if not adapter._outbound_blocked(chat_id, "Slack task progress") and progress.has_card(route):
                return route
        return None

    @functools.wraps(send)
    async def send_to_progress(chat_id, content, reply_to=None, metadata=None):
        if metadata and (metadata.get("expect_edits") or
                         (metadata.get("notify") and (metadata.get("reply_to_message_id") or
                                                      metadata.get("_hermes_task_answer")))):
            route = card_route(chat_id, metadata)
            if route:
                if metadata.get("expect_edits"):
                    # The edit-stream consumer will deliver the completed text at the end of the turn.
                    return SendResult(success=False, error="Task progress uses completed replies")
                if call(__file__, "publishTaskAnswer", content):
                    progress.finish(route, adapter.format_message(content).strip())
                    # The durable queue owns delivery. The consumer must never treat the parent as a draft ID.
                    return SendResult(success=True)
        return await send(chat_id, content, reply_to=reply_to, metadata=metadata)

    @functools.wraps(final)
    async def final_to_progress(event, session_key, text_content, metadata, *, reply_to,
                                is_ephemeral_response=False):
        if not is_ephemeral_response and not event.is_command():
            metadata = {**metadata, "_hermes_task_answer": True}
        return await final(event, session_key, text_content, metadata, reply_to=reply_to,
                           is_ephemeral_response=is_ephemeral_response)

    adapter.send = send_to_progress
    adapter.send_final_ledgered = final_to_progress
    if callable(draft):
        @functools.wraps(draft)
        async def buffer_draft(chat_id, draft_id, content, metadata=None):
            if card_route(chat_id, metadata):
                return SendResult(success=True)  # A draft receipt has no message ID.
            return await draft(chat_id, draft_id, content, metadata=metadata)

        adapter.send_draft = buffer_draft

    def unwire():
        nonlocal active
        active = False
        if adapter.send is send_to_progress:
            adapter.send = send
        if adapter.send_final_ledgered is final_to_progress:
            adapter.send_final_ledgered = final
        if callable(draft) and adapter.send_draft is buffer_draft:
            adapter.send_draft = draft

    return unwire
