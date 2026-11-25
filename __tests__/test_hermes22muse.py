import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call as mock_call, patch

path = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hermes22muse", path / "__init__.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)

from hermes22muse.muse import Message, MuseChat, MuseError, STAMP, history_messages
from hermes22muse import relay as relay_module
from hermes22muse.relay import Relay, Slack, Store, operation_id

TEAM, CHANNEL, OWNER = "T001", "C001", "U001"
BOT_TOKEN = "xoxb-muse-test"
REQUEST, LATER = "1795000000.000001", "1795000002.000001"
JOB = operation_id(TEAM, CHANNEL, REQUEST)


class FakeMuse:
    def __init__(self):
        self.chats = {}
        self.registrations = []
        self.sends = []
        self.lose_receipt = False
        self.busy = False
        # muse-mcp prefixes sends with its provenance stamp unless the server strips it.
        self.stamp = STAMP

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
        self.chats.setdefault(chat_id, []).append(Message(message_id, "user", self.stamp + text))
        if self.lose_receipt:
            self.lose_receipt = False
            raise TimeoutError
        return message_id

    async def history(self, chat_id):
        return self.chats[chat_id], False, self.busy


def fake_adapter():
    posts = iter(range(1, 1000))
    client = SimpleNamespace(chat_postMessage=AsyncMock(
        side_effect=lambda **kw: {"ok": True, "ts": f"1795000100.{next(posts):06d}", "channel": kw["channel"]},
    ))
    adapter = SimpleNamespace(
        _outbound_blocked=lambda channel, action: False,
        _get_client=lambda channel, team_id: client,
        _team_clients={TEAM: client},
        _running=False,
        disconnect=AsyncMock(),
    )
    return adapter, client


def posts(client):
    return [(call.kwargs["thread_ts"], call.kwargs["text"]) for call in client.chat_postMessage.await_args_list]


class RelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.store = Store(self.directory)
        self.adapter, self.client = fake_adapter()
        self.muse = FakeMuse()
        _, self.muse_client = fake_adapter()
        self.muse_client.auth_test = AsyncMock(return_value={
            "ok": True, "team_id": TEAM, "user_id": "UMUSE", "bot_id": "BMUSE",
        })
        self.slack = Slack(self.adapter, BOT_TOKEN)
        self.slack._new_muse_client = lambda: self.muse_client
        self.relay = Relay(self.store, self.slack, self.muse, OWNER)

    def create(self, channel=CHANNEL):
        return self.store.create(TEAM, channel, REQUEST, REQUEST, OWNER, "Task:\nFind a recipe",
                                 "Everything sent to Muse:\n\nTask:\nFind a recipe")

    async def tick(self):
        with self.store.connection() as db:
            db.execute("UPDATE handoffs SET due=0")
        await self.relay.tick(self.store.due(OWNER)[0])

    def settle(self):
        with self.store.connection() as db:
            db.execute(f"UPDATE handoffs SET changed=changed-{relay_module.QUIET}")

    async def test_thread_under_request_holds_task_answer_and_follow_up(self):
        self.assertEqual(self.create(), JOB)
        self.assertIsNone(self.create(), "a second handoff for the same message")
        self.adapter._register_mentioned_thread = register = Mock()
        await self.tick()
        self.assertEqual(posts(self.client), [(REQUEST, "Everything sent to Muse:\n\nTask:\nFind a recipe")])
        self.assertEqual(register.call_args_list, [mock_call(REQUEST, TEAM)])
        self.assertEqual(self.muse.registrations, [JOB])
        self.assertTrue(self.muse.sends[0][1].endswith("Task:\nFind a recipe"))
        self.assertEqual(self.store.answer(JOB), ("working", []))

        self.muse.chats[JOB].append(Message("answer", "assistant", "Do you want soup?"))
        self.muse.busy = True
        await self.tick()
        self.settle()
        self.assertEqual(self.store.answer(JOB)[0], "working", "Muse is still writing")
        self.muse.busy = False
        await self.tick()
        self.assertEqual(self.store.answer(JOB)[0], "working", "not quiet yet")
        self.settle()
        await self.tick()
        self.assertEqual(self.store.answer(JOB), ("answered", [{"from": "muse", "text": "Do you want soup?"}]))

        # A restarted gateway keeps the handoff; Hermes follows up and Leon also writes in Muse.
        self.store = Store(self.directory)
        self.relay = Relay(self.store, self.slack, self.muse, OWNER)
        self.store.follow_up(JOB, "Vegetarian, please", "Hermes → Muse\nVegetarian, please")
        await self.tick()
        self.assertEqual(self.muse.sends[1][1], "Vegetarian, please")
        self.assertEqual(self.store.answer(JOB), ("working", []), "the earlier answer no longer counts")
        self.muse.chats[JOB] += [Message("direct", "user", "No mushrooms"), Message("final", "assistant", "Lentil soup")]
        await self.tick()
        self.settle()
        await self.tick()
        self.assertEqual(self.store.answer(JOB), ("answered", [
            {"from": "leon_in_muse", "text": "No mushrooms"}, {"from": "muse", "text": "Lentil soup"},
        ]))
        self.assertEqual(posts(self.client)[1:], [
            (REQUEST, "Hermes → Muse\nVegetarian, please"), (REQUEST, "You in Muse\nNo mushrooms"),
        ])
        self.assertEqual(posts(self.muse_client), [
            (REQUEST, "Do you want soup?"), (REQUEST, "Lentil soup"),
        ])
        self.muse_client.auth_test.assert_awaited_once()
        event = SimpleNamespace(
            source=SimpleNamespace(platform="slack", scope_id=TEAM, user_id="UMUSE"), raw_message={},
        )
        self.assertTrue(self.slack.is_muse_event(event))
        event.source.user_id = OWNER
        self.assertFalse(self.slack.is_muse_event(event))
        self.assertIsNone(self.store.next_inbound(JOB))

    async def test_receipt_lost_after_muse_acceptance_recovers_in_same_chat(self):
        self.create()
        self.muse.lose_receipt = True
        await self.tick()
        await self.tick()
        self.assertIsNone(self.store.next_inbound(JOB))
        self.assertEqual(len(self.muse.sends), 1)

    async def test_unstamped_send_reads_as_leons_own_and_survives_a_lost_receipt(self):
        self.muse.stamp = ""
        self.create()
        self.muse.lose_receipt = True
        await self.tick()
        await self.tick()
        self.assertIsNone(self.store.next_inbound(JOB))
        self.assertEqual(self.muse.sends, [(JOB, "Task:\nFind a recipe")])
        await self.tick()
        self.assertNotIn("You in Muse", " ".join(text for _, text in posts(self.client)))

    async def test_handoffs_from_1x_are_not_relayed_again(self):
        with self.store.connection() as db:
            db.execute("INSERT INTO handoffs(id, team, channel, request, actor, root, activity) "
                       "VALUES (?, ?, ?, ?, ?, ?, 0)", (JOB, TEAM, CHANNEL, REQUEST, OWNER, LATER))
            db.execute("INSERT INTO outbound(handoff, event, part, text) VALUES (?, 'old', 0, NULL)", (JOB,))
            db.execute("INSERT INTO outbound(handoff, event, part, text) VALUES (?, 'legacy', 0, ?)",
                       (JOB, "Muse\nPreviously queued"))
        self.muse.chats[JOB] = [Message("old", "assistant", "Already in Slack"), Message("new", "assistant", "New")]
        await self.tick()
        await self.tick()
        self.assertEqual(posts(self.client), [])
        self.assertEqual(posts(self.muse_client), [(LATER, "Previously queued"), (LATER, "New")])

    async def test_muse_delivery_waits_for_its_workspace_while_hermes_can_follow_up(self):
        self.create()
        await self.tick()
        self.muse.chats[JOB].append(Message("answer", "assistant", "Soup"))
        await self.tick()
        self.store.follow_up(JOB, "Vegetarian, please", "Hermes → Muse\nVegetarian, please")
        self.muse_client.auth_test.return_value["team_id"] = "TOTHER"
        await self.tick()
        self.assertEqual(posts(self.muse_client), [])
        self.assertEqual(posts(self.client)[-1], (REQUEST, "Hermes → Muse\nVegetarian, please"))
        self.assertEqual([row["text"] for row in self.store.pending_output(JOB)], ["Muse\nSoup"])
        self.assertTrue(self.muse.sends[-1][1].endswith("Vegetarian, please"))

        self.muse_client.auth_test.return_value["team_id"] = TEAM
        self.slack = Slack(self.adapter, BOT_TOKEN)
        self.slack._new_muse_client = lambda: self.muse_client
        self.relay = Relay(self.store, self.slack, self.muse, OWNER)
        await self.tick()
        self.assertEqual(posts(self.muse_client), [(REQUEST, "Soup")])
        self.assertEqual(self.store.pending_output(JOB), [])

    async def test_muse_dm_links_to_the_handoff_and_preserves_its_thread_after_restart(self):
        self.muse_client.conversations_open = AsyncMock(return_value={"ok": True, "channel": {"id": "DMUSE"}})
        job = self.create(channel="D001")
        await self.tick()
        self.muse.chats[job].append(Message("answer", "assistant", "Soup"))
        await self.tick()
        self.settle()
        await self.tick()
        self.assertEqual(self.store.answer(job), ("answered", [{"from": "muse", "text": "Soup"}]))
        self.assertEqual(len(posts(self.client)), 1)
        opening = self.muse_client.chat_postMessage.await_args.kwargs
        self.assertEqual(opening["channel"], "DMUSE")
        self.assertIsNone(opening["thread_ts"])
        self.assertEqual(opening["text"], "Soup\n\nOriginal Slack thread: https://app.slack.com/archives/D001/p1795000000000001")

        self.store = Store(self.directory)
        self.relay = Relay(self.store, self.slack, self.muse, OWNER)
        self.muse.chats[job].append(Message("second", "assistant", "Lentil soup"))
        await self.tick()
        await self.tick()
        reply = self.muse_client.chat_postMessage.await_args.kwargs
        self.assertEqual(reply["channel"], "DMUSE")
        self.assertEqual(reply["thread_ts"], "1795000100.000001")
        self.assertEqual(reply["text"], "Lentil soup")
        self.muse_client.conversations_open.assert_awaited_once_with(users=OWNER)
        self.assertEqual(self.store.pending_output(job), [])

    async def test_idle_handoffs_back_off_then_stop(self):
        self.create()
        await self.tick()
        for idle, delay in ((10, 5), (600, 60), (7200, 600), (2 * 86400, relay_module.SLEEP)):
            with self.store.connection() as db:
                db.execute("UPDATE handoffs SET activity=?", (relay_module.time.time() - idle,))
            job = self.store.handoff(JOB)
            self.store.schedule(job)
            self.assertAlmostEqual(self.store.handoff(JOB)["due"] - relay_module.time.time(), delay, delta=2)

    async def test_history_windows_and_slack_escaping(self):
        self.create()
        message = Message("answer", "assistant", "<!channel> & " + "x" * 4000)
        self.store.collect(JOB, [message], True)
        self.store.collect(JOB, [message], True)
        parts = [item["text"] for item in self.store.pending_output(JOB)]
        self.assertEqual(len(parts), 4)
        self.assertIn("earlier messages may be missing", parts[1])
        self.assertIn("&lt;!channel&gt; &amp; ", parts[2])

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


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.tools, self.hooks = {}, {}
        ctx = SimpleNamespace(
            register_tool=lambda name, handler, **kw: self.tools.__setitem__(name, handler),
            register_platform_handler=lambda platform, factory: setattr(ctx, "wire", factory),
            register_hook=lambda name, callback: self.hooks.__setitem__(name, callback),
        )
        env = {
            "HERMES22MUSE_SLACK_USER_ID": OWNER,
            "HERMES22MUSE_MCP_URL": "http://127.0.0.1:7899/mcp",
            "HERMES22MUSE_MCP_SECRET": "x" * 32,
        }
        with patch.dict(os.environ, env):
            plugin.register(ctx)
        self.adapter, self.client = fake_adapter()
        constants = SimpleNamespace(get_hermes_home=lambda: self.directory)
        with patch.dict(sys.modules, {"hermes_constants": constants}):
            ctx.wire(None, self.adapter)
        self.addAsyncCleanup(self.adapter.disconnect)
        self.session = {"platform": "slack", "scope_id": TEAM, "chat_id": CHANNEL, "thread_id": "",
                        "message_id": REQUEST, "user_id": OWNER}
        patcher = patch.object(plugin, "session", lambda: dict(self.session))
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("WAIT", 0),):
            patcher = patch.object(plugin, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = Store(self.directory / "hermes22muse")

    def call(self, name, **args):
        return json.loads(self.tools[name](args))

    async def test_handoff_threads_under_leons_message_with_everything_sent(self):
        guide = self.hooks["pre_llm_call"](platform="slack")["context"]
        self.assertIn(f"mention <@{OWNER}>", guide)
        self.assertIn("muse_handoff", guide)
        result = self.call("muse_handoff", task="Summarize LEON-1 and its open questions")
        self.assertEqual(result["status"], "not_delivered")
        self.assertEqual(result["handoff"], JOB)
        self.assertEqual(result["side_chat"], f"https://muse.ai/thread/{JOB}")
        job = self.store.handoff(JOB)
        self.assertEqual(job["root"], REQUEST)
        self.assertIn("first person", guide)
        sent = self.store.next_inbound(JOB)["text"]
        self.assertEqual(sent, "Summarize LEON-1 and its open questions")
        [thread_post] = [row["text"] for row in self.store.pending_output(JOB)]
        self.assertIn(f"Side chat: https://muse.ai/thread/{JOB}", thread_post)
        self.assertTrue(thread_post.endswith("\n\n" + sent))
        self.assertIn("already belongs", self.call("muse_handoff", task="again")["error"])

        # Leon writes in the thread; Hermes forwards it.
        self.session.update(thread_id=REQUEST, message_id=LATER)
        self.assertIn(JOB, self.hooks["pre_llm_call"](platform="slack")["context"])
        self.assertEqual(self.call("muse_reply", text="Only the blockers")["handoff"], JOB)
        self.assertEqual(self.store.pending_output(JOB)[-1]["text"], "Sent to Muse as you\nOnly the blockers")
        self.assertEqual(self.store.next_inbound(JOB)["text"], "Summarize LEON-1 and its open questions")
        self.assertEqual(self.call("muse_wait")["status"], "not_delivered")

    async def test_tools_and_guide_ignore_other_people_and_platforms(self):
        for change in ({"user_id": "UOTHER"}, {"platform": "telegram"}, {"scope_id": ""}):
            with self.subTest(change=change):
                saved = dict(self.session)
                self.session.update(change)
                self.assertIn("error", self.call("muse_handoff", task="Do it"))
                self.assertIsNone(self.hooks["pre_llm_call"](platform=self.session["platform"]))
                self.session = saved
        self.assertIn("error", self.call("muse_handoff", task="  "))
        self.assertIn("error", self.call("muse_reply", text="hi", handoff="unknown"))
        self.assertTrue(self.adapter._hermes22muse_owns_request(OWNER, TEAM, CHANNEL, REQUEST))


class HistoryTests(unittest.TestCase):
    @staticmethod
    def event(role, identifier, text, ready=True, **payload):
        # Rows shaped like the Link's GET /chat/history, as recorded on a paired Gadget.
        return {
            "event_name": f"message.{role}", "role": role, "message_id": identifier,
            "display_text": text, "display_text_ready": ready, "seq": 1,
            "payload": {
                "session_id": JOB, "message_id": identifier, "role": role, "content": text,
                "display_text": text, "display_text_ready": ready, **payload,
            },
        }

    def response(self, *events):
        return {"ok": True, "status": 200, "scoped_to_chat": "Hermes test", "response": {
            "ok": True, "result": {"channel": "app", "has_more": False, "chat_events": list(events)}}}

    def test_only_completed_messages_from_linked_chat_are_projected(self):
        event = self.event
        self.assertEqual(history_messages(self.response(
            event("assistant", "foreign", "private", session_id="main", status="completed"),
            event("user", "input", "A direct Muse message"),
            {"event_name": "delta.presentation", "payload": {}},
            event("assistant", "final", "Done", status="completed"),
        ), JOB), ([
            Message("input", "user", "A direct Muse message"),
            Message("final", "assistant", "Done"),
        ], False, False))

    def test_unfinished_replies_are_held_back_and_reported(self):
        for row in (self.event("assistant", "draft", "Dra", ready=False, status="completed"),
                    self.event("assistant", "streaming", "Half a rep", status="streaming")):
            with self.subTest(row=row["message_id"]):
                self.assertEqual(history_messages(self.response(row), JOB), ([], False, True))

    def test_unknown_or_failed_history_is_a_retry(self):
        for response in ({}, {"events": {"items": []}}, {"ok": False, "events": []}):
            with self.subTest(response=response), self.assertRaises(MuseError):
                history_messages({"ok": True, "scoped_to_chat": "side", "response": response}, JOB)


if __name__ == "__main__":
    unittest.main()
