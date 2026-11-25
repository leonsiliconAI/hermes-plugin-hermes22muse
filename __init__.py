"""Let Hermes hand Leon's Slack requests to Muse side chats and relay them in a Slack thread."""

import asyncio
import functools
import json
import logging
import os
import re
import time
from contextlib import suppress

from ._pythonmonkey import blocking, call
from .muse import Muse
from .relay import Relay, Slack, Store, operation_id

LOG = logging.getLogger(__name__)
USER_ID = re.compile(r"[UW][A-Z0-9]+")
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
    muse_token = os.getenv("SLACK22MUSE_BOT_TOKEN", "").strip()
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

    def run_tool(name, args):
        store = relay.store if relay else None
        host = None
        if store:
            def lookup(*arguments):
                row = store.handoff(*arguments)
                return dict(row) if row is not None else None

            def find(*coordinates):
                row = store.find(*coordinates)
                return dict(row) if row is not None else None

            def answer(job_id):
                status, messages = store.answer(job_id)
                return {"status": status, "messages": messages}

            async def pause():
                await asyncio.sleep(1)

            host = {
                "lookup": blocking(lookup), "find": blocking(find),
                "handoffId": operation_id, "create": blocking(store.create),
                "followUp": blocking(store.follow_up), "answer": blocking(answer),
                "touch": blocking(store.touch), "now": lambda: time.monotonic(),
                "sleep": pause,
                "logHandoff": lambda channel, message, job: LOG.info(
                    "hermes22muse: handed %s:%s to Muse as %s", channel, message, job),
            }
        return json.dumps(call(__file__, "runTool", host, name, args, session() if store else None, owner, WAIT))

    def handoff_tool(args, **_):
        return run_tool("handoff", args)

    def reply_tool(args, **_):
        return run_tool("reply", args)

    def wait_tool(args, **_):
        return run_tool("wait", args)

    def guide(**kwargs):
        if relay is None or (kwargs.get("platform") or "slack") != "slack":
            return None
        try:
            current = call(__file__, "slackTurn", session(), owner)
        except Exception:
            return None
        if current is None:
            return None
        job = relay.store.find(current["scope_id"], current["chat_id"], thread=current["thread_id"]) if current["thread_id"] else None
        return call(__file__, "guideForTurn", current, owner, dict(job) if job else None)

    def ignore_muse_delivery(event, **_):
        if relay is not None and relay.slack.is_muse_event(event):
            return {"action": "skip", "reason": "slack22muse transcript delivery"}
        return None

    def wire(_native, adapter):
        nonlocal relay
        if getattr(adapter, "_hermes22muse_worker", None):
            return
        from hermes_constants import get_hermes_home

        current = Relay(Store(get_hermes_home() / "hermes22muse"), Slack(adapter, muse_token), muse, owner)
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
    ctx.register_hook("pre_gateway_dispatch", ignore_muse_delivery)
