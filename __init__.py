"""Let Hermes hand Leon's Slack requests to Muse side chats and relay them in a Slack thread."""

import asyncio
import functools
import json
import logging
import os
import re
import time
from contextlib import suppress

from ._pythonmonkey import call
from .muse import Muse
from .relay import Relay, Slack, Store, operation_id, side_chat_url, slack_permalink

LOG = logging.getLogger(__name__)
USER_ID = re.compile(r"[UW][A-Z0-9]+")
SLACK_TS = re.compile(r"[0-9]+\.[0-9]{6}")
TOOLSET = "hermes22muse"
# Hermes stops a tool call after 420 seconds by default; return before that and let Hermes wait again.
WAIT = 330

HANDOFF_SCHEMA = {
    "name": "muse_handoff",
    "description": (
        "Hand Leon's current Slack request to Muse, his personal AI, in a new Muse side chat, and "
        "wait for Muse's answer. Posts a reply in a thread under Leon's message showing exactly what "
        "was sent; Muse's replies and your follow-ups appear in that thread. Muse sees nothing but "
        "`task`, so make it self-contained. Returns Muse's messages with status answered, or status "
        "working if Muse is still busy after about five minutes (then call muse_wait)."
    ),
    "parameters": {
        "type": "object",
        "properties": {"task": {
            "type": "string",
            "description": "The complete task for Muse: goal, all relevant context from the "
                           "conversation and channel, constraints, and what a good answer looks like.",
        }},
        "required": ["task"],
        "additionalProperties": False,
    },
}
REPLY_SCHEMA = {
    "name": "muse_reply",
    "description": (
        "Send a follow-up to Muse in a handoff, for example to correct, narrow or push back on its "
        "answer or to pass on Leon's reply, show it in the handoff's Slack thread, and wait for "
        "Muse's answer. Without `handoff`, uses the handoff of the current message or thread."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The message for Muse."},
            "handoff": {"type": "string", "description": "Handoff ID returned by muse_handoff."},
        },
        "required": ["text"],
        "additionalProperties": False,
    },
}
WAIT_SCHEMA = {
    "name": "muse_wait",
    "description": (
        "Keep waiting for Muse's answer in a handoff after muse_handoff or muse_reply returned "
        "status working or not_delivered. Without `handoff`, uses the current message or thread."
    ),
    "parameters": {
        "type": "object",
        "properties": {"handoff": {"type": "string", "description": "Handoff ID returned by muse_handoff."}},
        "additionalProperties": False,
    },
}


def session():
    from gateway.session_context import get_session_env

    return {name: get_session_env(f"HERMES_SESSION_{name.upper()}") for name in (
        "platform", "scope_id", "chat_id", "thread_id", "message_id", "user_id")}


def register(ctx):
    owner = os.getenv("HERMES22MUSE_SLACK_USER_ID", "").strip()
    url = os.getenv("HERMES22MUSE_MCP_URL", "").strip()
    secret = os.getenv("HERMES22MUSE_MCP_SECRET", "")
    if not USER_ID.fullmatch(owner):
        LOG.warning("hermes22muse: set HERMES22MUSE_SLACK_USER_ID to enable Muse handoffs")
        return
    try:
        muse = Muse(url, secret)
    except ValueError as exc:
        LOG.warning("hermes22muse: %s", exc)
        return

    relay = None

    def owns_slack_request(user, team, channel, message_id):
        return call(__file__, "ownsRequest", user, owner, team or "", channel or "", message_id)

    def turn():
        """The current Slack turn's coordinates when Leon sent it, else None."""
        current = session()
        if current["platform"] != "slack" or not owns_slack_request(
                current["user_id"], current["scope_id"], current["chat_id"], current["message_id"]):
            return None
        if current["thread_id"] and not SLACK_TS.fullmatch(current["thread_id"]):
            return None
        return current

    def resolve(args, current):
        handoff = str(args.get("handoff") or "").strip()
        if handoff:
            job = relay.store.handoff(handoff)
            return job if job and job["actor"] == owner else None
        if current is None:
            return None
        return relay.store.find(current["scope_id"], current["chat_id"],
                                request=current["message_id"], thread=current["thread_id"])

    def wait(job):
        deadline = time.monotonic() + WAIT
        polls = 0
        while True:
            status, messages = relay.store.answer(job["id"])
            if status == "answered" or time.monotonic() >= deadline:
                break
            if polls % 3 == 0:
                relay.store.touch(job["id"])
            polls += 1
            time.sleep(1)
        return json.dumps({
            "status": status, "handoff": job["id"], "side_chat": side_chat_url(job["id"]),
            "slack_thread": slack_permalink(job["channel"], job["root"]),
            "messages": messages, "next": call(__file__, "nextStep", status),
        })

    def error(message):
        return json.dumps({"error": message})

    def handoff_tool(args, **_):
        if relay is None:
            return error("The Muse relay is not running in this gateway.")
        current = turn()
        if current is None:
            return error("muse_handoff only hands over Leon's own Slack messages.")
        task = str(args.get("task") or "").strip()
        if not task:
            return error("task is empty. Write the complete task, or ask Leon what he needs.")
        team, channel = current["scope_id"], current["chat_id"]
        thread = current["thread_id"] or current["message_id"]
        existing = relay.store.find(team, channel, request=current["message_id"], thread=thread)
        if existing:
            return error(f"This message already belongs to handoff {existing['id']}. "
                         "Use muse_reply to send Muse more, or muse_wait to wait for its answer.")
        texts = call(__file__, "handoffTexts", task, channel, current["message_id"],
                     operation_id(team, channel, current["message_id"]))
        job_id = relay.store.create(
            team, channel, current["message_id"], thread, owner, texts["muse"], texts["thread"])
        if job_id is None:
            return error("This Slack thread already has a handoff. Use muse_reply instead.")
        LOG.info("hermes22muse: handed %s:%s to Muse as %s", channel, current["message_id"], job_id)
        return wait(relay.store.handoff(job_id))

    def reply_tool(args, **_):
        if relay is None:
            return error("The Muse relay is not running in this gateway.")
        text = str(args.get("text") or "").strip()
        if not text:
            return error("text is empty.")
        job = resolve(args, turn())
        if job is None:
            return error("No handoff found. Pass the handoff ID that muse_handoff returned.")
        relay.store.follow_up(job["id"], text, call(__file__, "followUpThread", text))
        return wait(job)

    def wait_tool(args, **_):
        if relay is None:
            return error("The Muse relay is not running in this gateway.")
        job = resolve(args, turn())
        if job is None:
            return error("No handoff found. Pass the handoff ID that muse_handoff returned.")
        return wait(job)

    def guide(**kwargs):
        if relay is None or (kwargs.get("platform") or "slack") != "slack":
            return None
        try:
            current = turn()
        except Exception:
            return None
        if current is None:
            return None
        if current["thread_id"]:
            job = relay.store.find(current["scope_id"], current["chat_id"], thread=current["thread_id"])
            if job and job["request"] != current["message_id"]:
                return {"context": call(__file__, "threadGuide", job["id"])}
        return {"context": call(__file__, "handoffGuide", owner)}

    def wire(_native, adapter):
        nonlocal relay
        if getattr(adapter, "_hermes22muse_worker", None):
            return
        from hermes_constants import get_hermes_home

        current = Relay(Store(get_hermes_home() / "hermes22muse"), Slack(adapter), muse, owner)
        worker = asyncio.create_task(current.run())
        relay = current
        adapter._hermes22muse_worker = worker
        # AutoLinear's plugin leaves these requests to Hermes and Muse.
        adapter._hermes22muse_owns_request = owns_slack_request
        disconnect = adapter.disconnect

        @functools.wraps(disconnect)
        async def disconnect_and_stop(*args, **kwargs):
            nonlocal relay
            worker.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await worker
            finally:
                if relay is current:
                    relay = None
                del adapter._hermes22muse_owns_request
                adapter.disconnect = disconnect
                adapter._hermes22muse_worker = None
                await disconnect(*args, **kwargs)

        adapter.disconnect = disconnect_and_stop
        LOG.info("hermes22muse: Muse handoff relay registered")

    available = lambda: relay is not None  # noqa: E731
    for schema, handler, emoji in ((HANDOFF_SCHEMA, handoff_tool, "🪄"), (REPLY_SCHEMA, reply_tool, "↩️"),
                                   (WAIT_SCHEMA, wait_tool, "⏳")):
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=handler,
                          check_fn=available, emoji=emoji)
    ctx.register_platform_handler("slack", wire)
    ctx.register_hook("pre_llm_call", guide)
