"""The niclydon/muse-mcp contract, backed by Meta's paired Gadget SDK."""

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

CALLER = "hermes22muse"
STAMP = f"⟦via muse-mcp · from {CALLER}⟧ "
HISTORY_LIMIT = 50


class MuseError(Exception):
    pass


@dataclass(frozen=True)
class Message:
    id: str
    role: str
    text: str


def history_messages(result, chat_id):
    """Project completed conversation rows from the Link's response envelope."""
    if not result.get("ok") or not result.get("scoped_to_chat"):
        raise MuseError("scoped history unavailable")
    response = result.get("response")
    for _ in range(5):
        if not isinstance(response, dict) or response.get("ok") is False:
            break
        events = response.get("chat_events", response.get("events"))
        if isinstance(events, list):
            break
        response = response.get("result", response.get("response"))
    else:
        raise MuseError("unrecognized Link history response")
    if not isinstance(response, dict) or response.get("ok") is False:
        raise MuseError("unrecognized Link history response")

    messages = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        name, payload = event.get("event"), event.get("payload")
        if name not in ("message.user", "message.assistant") or not isinstance(payload, dict):
            continue
        session = payload.get("session_id") or event.get("session_id")
        role = name.split(".")[1]
        if (session and session != chat_id) or payload.get("role") not in (None, role):
            continue
        if payload.get("display_text_ready") is False:
            continue
        identifier = payload.get("message_id") or event.get("message_id") or payload.get("id")
        body = next((
            value for value in (
                payload.get("display_text"), event.get("display_text"), payload.get("content"),
                payload.get("text"),
            ) if isinstance(value, str) and value.strip()
        ), None)
        if isinstance(identifier, str) and identifier and body:
            messages[identifier] = Message(identifier, role, body)
    return list(messages.values()), len(events) >= HISTORY_LIMIT


class MuseChat:
    def __init__(self, session):
        self.session = session

    async def call(self, tool, **args):
        result = await self.session.call_tool(tool, arguments=args)
        if result.is_error:
            raise MuseError(f"{tool} returned an MCP error")
        payload = result.structured_content
        if payload is None and len(result.content) == 1 and result.content[0].type == "text":
            # The pinned server declares no output schema and sends dictionaries as JSON text.
            try:
                payload = json.loads(result.content[0].text)
            except ValueError:
                raise MuseError(f"{tool} returned invalid JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
            raise MuseError(f"{tool} returned an invalid payload")
        return payload

    async def ensure_registered(self, chat_id):
        current = await self.call("muse_directory_get", chat=chat_id)
        if current.get("in_directory"):
            if current.get("sendable") and current.get("entry", {}).get("kind") == "side":
                return
            raise MuseError("registered side chat is suspended or has an unexpected kind")
        if current.get("entry"):
            raise MuseError("side chat ID is already present in Muse's registry")
        revision = current.get("directory_revision") or current.get("revision")
        if not revision:
            raise MuseError("Muse directory revision unavailable")
        result = await self.call(
            "muse_directory_set", chat_id=chat_id, base_revision=revision,
            name=f"Hermes {chat_id}", kind="side",
            purpose="Linked to a Hermes Slack handoff", **{"from": CALLER},
        )
        if not result["ok"]:
            raise MuseError("side chat registration failed")

    async def exists(self, chat_id):
        result = await self.call("muse_chat_exists", chat=chat_id, **{"from": CALLER})
        if not result["ok"] or not isinstance(result.get("exists"), bool):
            raise MuseError("side chat existence unknown")
        return result["exists"]

    async def send(self, chat_id, text):
        result = await self.call("muse_chat_send", chat=chat_id, text=text, **{"from": CALLER})
        identifier = result.get("message_id")
        if (not result["ok"] or result.get("session_id") != chat_id
                or not isinstance(identifier, str) or not identifier):
            raise MuseError("side chat send returned no matching receipt")
        return identifier

    async def history(self, chat_id):
        result = await self.call(
            "muse_chat_history", chat=chat_id, limit=HISTORY_LIMIT, **{"from": CALLER},
        )
        return history_messages(result, chat_id)


class Muse:
    def __init__(self, url, secret):
        endpoint = urlsplit(url)
        loopback = endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
        if (not endpoint.hostname or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment
                or not (endpoint.scheme == "https" or endpoint.scheme == "http" and loopback)):
            raise ValueError("muse-mcp needs a private HTTPS URL or loopback HTTP URL")
        if len(secret) < 32:
            raise ValueError("muse-mcp needs its origin secret (at least 32 characters)")
        self.url, self.secret = url, secret

    @asynccontextmanager
    async def connect(self):
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with httpx2.AsyncClient(
            headers={"X-Origin-Secret": self.secret}, timeout=20, trust_env=False,
        ) as http:
            async with streamable_http_client(self.url, http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield MuseChat(session)
