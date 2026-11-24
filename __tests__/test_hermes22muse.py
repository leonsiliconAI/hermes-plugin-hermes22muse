import asyncio
from contextlib import asynccontextmanager
import importlib.util
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

path = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hermes22muse", path / "__init__.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)

from hermes22muse.muse import Message, MuseChat, MuseError, STAMP, history_messages
from hermes22muse.relay import Relay, Slack, Store, operation_id

TEAM, CHANNEL, OWNER = "T001", "D001", "U001"
REQUEST, ROOT, FOLLOWUP = "1795000000.000001", "1795000001.000001", "1795000002.000001"
JOB = operation_id(TEAM, CHANNEL, REQUEST)


class FakeMuse:
    def __init__(self):
        self.chats = {}
        self.registrations = []
        self.sends = []
        self.lose_receipt = False

    @asynccontextmanager
    async def connect(self):
        yield self

    async def ensure_registered(self, chat_id):
        self.registrations.append(chat_id)

    async def exists(self, chat_id):
        return chat_id in self.chats

    async def send(self, chat_id, text):
        self.sends.append((chat_id, text))
        message_id = f"muse-{len(self.sends)}"
        self.chats.setdefault(chat_id, []).append(Message(message_id, "user", STAMP + text))
        if self.lose_receipt:
            self.lose_receipt = False
            raise TimeoutError
        return message_id

    async def history(self, chat_id):
        return self.chats[chat_id], False


def fake_adapter():
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ok": True, "ts": ROOT, "channel": CHANNEL}),
        conversations_history=AsyncMock(return_value={"ok": True, "messages": []}),
    )
    adapter = SimpleNamespace(
        _outbound_blocked=lambda channel, action: False,
        _get_client=lambda channel, team_id: client,
        _team_clients={TEAM: client},
        _team_bot_user_ids={TEAM: "U001BOT"},
        _register_mentioned_thread=lambda ts, team: None,
        _running=False,
        disconnect=AsyncMock(),
    )
    return adapter, client


class RelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.store = Store(self.directory)
        self.adapter, self.client = fake_adapter()
        self.muse = FakeMuse()
        self.relay = Relay(self.store, Slack(self.adapter), self.muse, OWNER)

    def queue_request(self, thread=""):
        self.store.enqueue(TEAM, CHANNEL, thread, REQUEST, OWNER, "Find a recipe")
        self.store.enqueue(TEAM, CHANNEL, thread, REQUEST, OWNER, "duplicate event")

    async def tick(self):
        with self.store.connection() as db:
            db.execute("UPDATE handoffs SET due=0")
        await self.relay.tick(self.store.due(OWNER)[0])

    async def test_root_created_in_original_chat_and_two_way_sync_survives_restart(self):
        self.queue_request(thread="1794999999.000001")
        await self.tick()
        root_post = self.client.chat_postMessage.await_args.kwargs
        self.assertNotIn("thread_ts", root_post)
        self.assertIn(f"https://muse.ai/thread/{JOB}", root_post["text"])
        self.assertEqual(self.muse.registrations, [JOB])
        self.assertEqual(len(self.muse.sends), 1)
        self.assertIn("Find a recipe", self.muse.sends[0][1])

        self.muse.chats[JOB].extend([
            Message("answer", "assistant", "Do you want soup?"),
            Message("direct", "user", "Yes, from Muse"),
        ])
        self.store = Store(self.directory)
        self.relay = Relay(self.store, Slack(self.adapter), self.muse, OWNER)
        self.store.enqueue(TEAM, CHANNEL, ROOT, FOLLOWUP, OWNER, "Vegetarian, please")
        await self.tick()
        await self.tick()
        await self.tick()

        self.assertEqual(len(self.muse.sends), 2)
        self.assertEqual(self.muse.sends[1][0], JOB)
        self.assertIn("Vegetarian, please", self.muse.sends[1][1])
        self.assertEqual(
            [(post.kwargs.get("thread_ts"), post.kwargs["text"])
             for post in self.client.chat_postMessage.await_args_list[1:]],
            [(ROOT, "Muse\nDo you want soup?"), (ROOT, "You in Muse\nYes, from Muse")],
        )
        self.assertIsNone(self.store.next_inbound(JOB))

    async def test_receipt_lost_after_muse_acceptance_recovers_in_same_chat(self):
        self.queue_request()
        self.muse.lose_receipt = True
        await self.tick()
        await self.tick()
        self.assertIsNone(self.store.next_inbound(JOB))
        self.assertEqual(len(self.muse.sends), 1)
        self.client.chat_postMessage.assert_awaited_once()

    async def test_you_can_recover_slack_root_after_a_lost_receipt(self):
        self.queue_request()
        self.client.chat_postMessage.side_effect = TimeoutError
        await self.tick()
        self.assertEqual(self.muse.sends, [])
        self.client.conversations_history.return_value = {
            "ok": True, "messages": [
                {"ts": ROOT, "user": "U001BOT", "text": f"Open https://muse.ai/thread/{JOB}"},
            ],
        }
        await self.tick()
        self.client.chat_postMessage.assert_awaited_once()
        self.assertEqual(self.store.due(OWNER, {JOB}), [])
        self.assertEqual(len(self.muse.sends), 1)

    async def test_distinct_request_and_workspace_get_distinct_side_chats(self):
        self.queue_request()
        self.store.enqueue("T002", CHANNEL, "", REQUEST, OWNER, "Other workspace")
        self.store.enqueue(TEAM, CHANNEL, "", FOLLOWUP, OWNER, "New request")
        self.assertEqual(
            {job["id"] for job in self.store.due(OWNER)},
            {JOB, operation_id("T002", CHANNEL, REQUEST), operation_id(TEAM, CHANNEL, FOLLOWUP)},
        )

    async def test_history_windows_and_slack_escaping(self):
        self.queue_request()
        message = Message("answer", "assistant", "<!channel> & " + "x" * 4000)
        self.store.collect(JOB, [message], True)
        self.store.collect(JOB, [message], True)
        parts = [item["text"] for item in self.store.pending_output(JOB)]
        self.assertEqual(len(parts), 3)
        self.assertIn("earlier messages may be missing", parts[0])
        self.assertIn("&lt;!channel&gt; &amp; ", parts[1])

    async def test_muse_mcp_decodes_unstructured_json_tool_content(self):
        result = SimpleNamespace(
            is_error=False, structured_content=None,
            content=[SimpleNamespace(type="text", text='{"ok": true, "exists": false}')],
        )
        session = SimpleNamespace(call_tool=AsyncMock(return_value=result))
        self.assertFalse(await MuseChat(session).exists(JOB))
        session.call_tool.assert_awaited_once_with(
            "muse_chat_exists", arguments={"chat": JOB, "from": "hermes22muse"},
        )

    async def test_gateway_only_consumes_the_configured_slack_user(self):
        ctx = SimpleNamespace(
            register_platform_handler=lambda platform, factory: setattr(ctx, "wire", factory),
            register_hook=lambda name, callback: setattr(ctx, "consume", callback),
        )
        env = {
            "HERMES22MUSE_SLACK_USER_ID": OWNER,
            "HERMES22MUSE_MCP_URL": "http://127.0.0.1:7899/mcp",
            "HERMES22MUSE_MCP_SECRET": "x" * 32,
        }
        with patch.dict(os.environ, env):
            plugin.register(ctx)
        constants = SimpleNamespace(get_hermes_home=lambda: self.directory)
        with patch.dict(sys.modules, {"hermes_constants": constants}):
            ctx.wire(None, self.adapter)
        try:
            source = {"user_id": OWNER, "chat_id": CHANNEL, "scope_id": TEAM}
            self.assertIsNone(ctx.consume("telegram", source, REQUEST, "hi"))
            self.assertIsNone(ctx.consume("slack", {**source, "user_id": "UOTHER"}, REQUEST, "hi"))
            self.assertIsNone(ctx.consume("slack", {**source, "scope_id": ""}, REQUEST, "hi"))
            self.assertEqual(ctx.consume("slack", source, REQUEST, "hi"), {"action": "handled"})
            self.assertTrue(self.adapter._hermes22muse_owns_request(OWNER, TEAM, CHANNEL, REQUEST))
        finally:
            await self.adapter.disconnect()


class HistoryTests(unittest.TestCase):
    def test_only_completed_messages_from_linked_chat_are_projected(self):
        def event(role, identifier, **payload):
            return {
                "event": f"message.{role}",
                "payload": {"session_id": JOB, "message_id": identifier, **payload},
            }

        response = {
            "ok": True,
            "scoped_to_chat": "Hermes test",
            "response": {"ok": True, "result": {"chat_events": [
                event("assistant", "draft", display_text_ready=False, content="draft"),
                event("assistant", "foreign", session_id="main", content="private"),
                event("user", "input", text="A direct Muse message"),
                {"event": "delta.presentation", "payload": {}},
                event("assistant", "final", display_text="Done"),
            ]}},
        }
        self.assertEqual(history_messages(response, JOB), ([
            Message("input", "user", "A direct Muse message"),
            Message("final", "assistant", "Done"),
        ], False))

    def test_unknown_or_failed_history_is_a_retry(self):
        for response in ({}, {"events": {"items": []}}, {"ok": False, "events": []}):
            with self.subTest(response=response), self.assertRaises(MuseError):
                history_messages({"ok": True, "scoped_to_chat": "side", "response": response}, JOB)


if __name__ == "__main__":
    unittest.main()
