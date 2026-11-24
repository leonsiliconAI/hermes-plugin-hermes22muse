"""Automatically route admitted Slack requests to linked Muse side chats."""

import asyncio
import functools
import logging
import os
import re
from contextlib import suppress

from ._pythonmonkey import call
from .muse import Muse
from .relay import Relay, Slack, Store

LOG = logging.getLogger(__name__)
USER_ID = re.compile(r"[UW][A-Z0-9]+")
SLACK_TS = re.compile(r"[0-9]+\.[0-9]{6}")


def register(ctx):
    owner = os.getenv("HERMES22MUSE_SLACK_USER_ID", "").strip()
    url = os.getenv("HERMES22MUSE_MCP_URL", "").strip()
    secret = os.getenv("HERMES22MUSE_MCP_SECRET", "")
    if not USER_ID.fullmatch(owner):
        LOG.warning("hermes22muse: set HERMES22MUSE_SLACK_USER_ID to enable automatic handoffs")
        return
    try:
        muse = Muse(url, secret)
    except ValueError as exc:
        LOG.warning("hermes22muse: %s", exc)
        return

    relay = None

    def consume(platform, source, message_id, text, **kwargs):
        if platform != "slack" or source.get("user_id") != owner or relay is None:
            return None
        team = source.get("scope_id") or ""
        channel = source.get("chat_id") or ""
        thread = source.get("thread_id") or ""
        if not owns_slack_request(owner, team, channel, message_id):
            return None
        if thread and not SLACK_TS.fullmatch(thread):
            return None
        body = call(__file__, "requestBody", text, channel, message_id)
        if relay.store.enqueue(team, channel, thread, message_id, owner, body):
            return {"action": "handled"}
        return None

    def owns_slack_request(user, team, channel, message_id):
        return call(__file__, "ownsRequest", user, owner, team, channel, message_id)

    def wire(_native, adapter):
        nonlocal relay
        if getattr(adapter, "_hermes22muse_worker", None):
            return
        from hermes_constants import get_hermes_home

        current = Relay(Store(get_hermes_home() / "hermes22muse"), Slack(adapter), muse, owner)
        worker = asyncio.create_task(current.run())
        relay = current
        adapter._hermes22muse_worker = worker
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
        LOG.info("hermes22muse: Slack handoff relay registered")

    ctx.register_platform_handler("slack", wire)
    ctx.register_hook("post_gateway_admission", consume)
