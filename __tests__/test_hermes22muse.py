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
from hermes22muse import museweb
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
        self.approvals = 0
        self.approval_checks = 0

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

    async def pending_approvals(self, chat_id):
        self.approval_checks += 1
        return self.approvals


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

    async def test_a_reply_stored_cut_off_is_completed_and_posted_in_full(self):
        self.create()
        await self.tick()
        self.muse.chats[JOB].append(Message("answer", "assistant", "**Bottom line:"))
        await self.tick()
        self.muse.chats[JOB][-1] = Message("answer", "assistant", "**Bottom line:** no, it doesn't count.")
        await self.tick()
        await self.tick()
        self.settle()
        await self.tick()
        self.assertEqual(self.store.answer(JOB), ("answered", [
            {"from": "muse", "text": "**Bottom line:** no, it doesn't count."},
        ]))
        self.assertEqual(posts(self.client)[-1], (REQUEST, "Muse (full reply)\n**Bottom line:** no, it doesn't count."))

    async def test_relay_measures_pending_approvals_only_when_asked(self):
        self.create()
        await self.tick()
        self.assertEqual(self.muse.approval_checks, 0)
        self.muse.approvals = 1
        self.store.want_approvals(JOB)
        await self.tick()
        approvals = self.store.approvals(JOB)
        self.assertEqual((approvals["count"], self.muse.approval_checks), (1, 1))
        self.assertGreaterEqual(approvals["checked"], approvals["wanted"])
        await self.tick()
        self.assertEqual(self.muse.approval_checks, 1, "one measurement per request")

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
        self.tools, self.hooks, self.tool_options = {}, {}, {}
        ctx = SimpleNamespace(
            register_tool=lambda name, handler, **kw: (self.tools.__setitem__(name, handler),
                                                       self.tool_options.__setitem__(name, kw)),
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
        # AutoLinear leaves Leon's replies in this thread where they are.
        self.assertTrue(self.adapter._hermes22muse_handoff_thread(TEAM, CHANNEL, REQUEST))
        self.assertFalse(self.adapter._hermes22muse_handoff_thread(TEAM, CHANNEL, LATER))

        # Leon writes in the thread; Hermes forwards it.
        self.session.update(thread_id=REQUEST, message_id=LATER)
        self.assertIn(JOB, self.hooks["pre_llm_call"](platform="slack")["context"])
        self.assertEqual(self.call("muse_reply", text="Only the blockers")["handoff"], JOB)
        self.assertEqual(self.store.pending_output(JOB)[-1]["text"], "Sent to Muse as you\nOnly the blockers")
        self.assertEqual(self.store.next_inbound(JOB)["text"], "Summarize LEON-1 and its open questions")
        self.assertEqual(self.call("muse_wait")["status"], "not_delivered")

    async def test_tools_are_always_listed(self):
        # Hermes evaluates a check_fn before the Slack adapter wires the relay and keeps the result.
        self.assertEqual(sorted(self.tools), [
            "muse_app", "muse_app_methods", "muse_approvals", "muse_approve", "muse_file",
            "muse_handoff", "muse_reply", "muse_wait"])
        self.assertTrue(all("check_fn" not in options for options in self.tool_options.values()))

    async def test_waiting_tools_report_a_pending_approval(self):
        self.assertEqual(self.call("muse_handoff", task="Book the usual table for Friday")["status"], "not_delivered")
        now = relay_module.time.time()
        with self.store.connection() as db:
            db.execute("UPDATE handoffs SET approvals=1, approvals_at=?, approvals_wanted=?", (now, now - 1))
        result = self.call("muse_wait")
        self.assertEqual((result["status"], result["approvals_pending"]), ("waiting_for_approval", 1))
        self.assertIn("!muse approval-allow <code>", result["next"])
        self.assertIn("Payments, purchases, money transfers", result["next"])

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


class ApprovalDecisionTests(unittest.TestCase):
    def decide(self, status, waited=0, now=1000, **approvals):
        state = {"count": None, "checked": 0, "wanted": 0, "changed": 0, **approvals}
        return plugin.call(plugin.__file__, "approvalDecision", state, status, now, waited)

    def test_working_handoffs_are_measured_after_20_s_and_every_30_s(self):
        self.assertEqual(self.decide("working", waited=5), "wait")
        self.assertEqual(self.decide("working", waited=25), "check")
        self.assertEqual(self.decide("working", waited=40, wanted=990, checked=995, count=0), "wait")
        self.assertEqual(self.decide("working", waited=60, wanted=960, checked=965, count=0), "check")
        self.assertEqual(self.decide("working", waited=25, wanted=995), "wait", "a measurement is outstanding")

    def test_an_answer_is_measured_once_before_it_is_returned(self):
        self.assertEqual(self.decide("answered", changed=990), "check")
        self.assertEqual(self.decide("answered", changed=990, wanted=995), "wait")
        self.assertEqual(self.decide("answered", changed=990, wanted=995, checked=998, count=0), "clear")
        self.assertEqual(self.decide("answered", changed=990, wanted=970), "clear", "a lost measurement")

    def test_a_fresh_pending_count_is_reported(self):
        self.assertEqual(self.decide("answered", changed=990, wanted=995, checked=998, count=2), "pending")
        self.assertEqual(self.decide("working", wanted=995, checked=998, count=1), "pending")
        self.assertEqual(self.decide("working", waited=10, wanted=900, checked=910, count=1), "wait", "stale")


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
                    self.event("assistant", "streaming", "Half a rep", status="streaming"),
                    # While Muse streams, the row has no status and only the text so far.
                    self.event("assistant", "statusless", "**Bottom line:")):
            with self.subTest(row=row["message_id"]):
                self.assertEqual(history_messages(self.response(row), JOB), ([], False, True))

    def test_unknown_or_failed_history_is_a_retry(self):
        for response in ({}, {"events": {"items": []}}, {"ok": False, "events": []}):
            with self.subTest(response=response), self.assertRaises(MuseError):
                history_messages({"ok": True, "scoped_to_chat": "side", "response": response}, JOB)


if __name__ == "__main__":
    unittest.main()


class FakeGateway:
    """muse.ai and the personal computer's gateway, speaking the app's own wire format."""

    def __init__(self):
        self.requests, self.site, self.sockets = [], [], 0
        self.login, self.next_login = "first", None
        self.answers = {}
        self.reject_token = False

    def http(self, verb, url, headers, body=None):
        path = url.removeprefix("https://muse.ai")
        self.site.append((verb, path, headers.get("Cookie"), json.loads(body) if body else None))
        if f"hatch_sess={self.login}" not in headers["Cookie"]:
            return 401, "{}", {}
        issued = {}
        if path == "/api/session":
            if self.next_login:
                self.login, self.next_login = self.next_login, None
                issued = {"hatch_sess": (self.login, 1900000000)}
            return 200, json.dumps({"status": "assigned", "vm_id": "vm-1", "vm_state": "RUNNING"}), issued
        if path == "/api/auth/check":
            return 200, '{"access_token": "access"}', {}
        if path == "/api/hatch/token":
            return 200, '{"token": "tok en"}', {}
        return 404, "{}", {}

    def socket(self, url):
        self.sockets += 1
        if self.reject_token:
            self.reject_token = False
            raise ConnectionError("refused")
        assert url == "wss://hatch.metaaivm.com/v1/noise?vm_id=vm-1&auth_token=tok%20en"
        return FakeSocket(self)


class FakeSocket:
    """The responder side of Noise_XX_25519_AESGCM_SHA256 and the gateway behind it."""

    def __init__(self, gateway):
        self.gateway, self.outbox, self.noise, self.closed = gateway, [], museweb.Noise(), False

    def send(self, data):
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

        noise = self.noise
        if not hasattr(noise, "remote"):
            noise.remote = X25519PublicKey.from_public_bytes(data[:32])
            noise._mix_hash(data[:32])
            noise._handshake_seal(data[32:], opening=True)
            reply = noise._public(noise.ephemeral)
            noise._mix_hash(reply)
            noise._mix_key(noise.ephemeral.exchange(noise.remote))
            reply += noise._handshake_seal(noise._public(noise.static))
            noise._mix_key(noise.static.exchange(noise.remote))
            self.outbox.append(reply + noise._handshake_seal(b""))
        elif noise.sending is None:
            client = X25519PublicKey.from_public_bytes(noise._handshake_seal(data[:48], opening=True))
            noise._mix_key(noise.ephemeral.exchange(client))
            noise._handshake_seal(data[48:], opening=True)
            noise.receiving, noise.sending = ([key, 0] for key in noise._hkdf(b""))
        else:
            self.serve(noise.decrypt(data))

    def serve(self, frame):
        fields = lambda data: dict(museweb._fields(data))
        envelope = fields(fields(frame)[4])
        service_frame = fields(envelope[2])
        request = fields(service_frame[2])
        verb, path = bytes(request[1]).decode(), bytes(request[2]).decode()
        body = json.loads(bytes(request[4])) if 4 in request else None
        self.gateway.requests.append((envelope.get(1, 0), verb, path, body))
        status, kind, data = self.gateway.answers.get((verb, path), (404, "application/json", b'{"error":"no"}'))
        header = museweb._field(1, b"content-type") + museweb._field(2, kind.encode())
        response = museweb._field(1, status) + museweb._field(2, header)
        # The head, then the body in two chunks.
        half = len(data) // 2
        for part in (museweb._field(3, response),
                     museweb._field(4, museweb._field(1, data[:half])),
                     museweb._field(4, museweb._field(1, data[half:]) + museweb._field(2, 1))):
            payload = museweb._field(1, museweb._field(1, service_frame[1]) + part)
            # Long answers span several Noise messages that share a chunk ID.
            pieces = [payload[index:index + 30000] for index in range(0, len(payload), 30000)] or [b""]
            for index, piece in enumerate(pieces):
                self.outbox.append(self.noise.encrypt(
                    museweb._field(1, 7) + museweb._field(2, index) + museweb._field(3, len(pieces))
                    + museweb._field(4, piece)))

    def receive(self, timeout):
        if not self.outbox:
            raise TimeoutError
        return self.outbox.pop(0)

    def close(self):
        self.closed = True


PENDING = {"pending_approvals": [
    {"approval_id": "ap-1", "status": "pending", "requested_at_ms": 1795000000000,
     "session_id": JOB,
     "decision_options": [{"kind": "allow_once", "label_text": "Allow"},
                          {"kind": "allow_always", "label_text": "Always allow apple.com",
                           "label": {"kind": "allow_always_scoped", "scope": "destination_domain"}},
                          {"kind": "deny", "label_text": "Deny"}],
     "display": {"display_name": "Browser", "card_title": "Allow {assistant} to log in to apple.com using Browser?",
                 "permission_question": {"kind": "sign_in", "text": "Allow {assistant} to log in?"},
                 "purpose_summary": "The agent wants to sign in to apple.com using your saved login details.",
                 "request_preview_document": {"blocks": [{"type": "fields", "rows": [
                     {"label": "Website", "value": "apple.com"}]}]}}},
    {"approval_id": "ap-2", "status": "pending", "approval_type": "purchase_preapproval",
     "session_id": "another-chat",
     "decision_options": [{"kind": "allow_once"}, {"kind": "deny"}],
     "display": {"card_title": "Approve this purchase"}},
    {"approval_id": "ap-3", "status": "allowed", "decision_options": [{"kind": "allow_once"}]},
]}


class MuseWebTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.gateway = FakeGateway()
        self.web = museweb.MuseWeb(Path(temporary.name), self.gateway)
        self.web.login.update(museweb.parse_cookies("hatch_sess=first; theme=dark"))

    def test_a_call_travels_the_apps_wire_format_and_extends_the_login(self):
        self.gateway.answers[("GET", "/approvals")] = (200, "application/json", json.dumps(PENDING).encode())
        self.gateway.answers[("POST", "/approvals/ap%2F1/decide")] = (
            200, "application/json", json.dumps({"approval": {"status": "allowed", "text": "x" * 70000}}).encode())
        self.gateway.next_login = "second"
        first, second = self.web.calls([
            ("egress.approvals", None),
            ("egress.approval.decide", {"approval_id": "ap/1", "decision": "allow_once"})])
        self.assertEqual(first["body"], PENDING)
        self.assertEqual(len(second["body"]["approval"]["text"]), 70000)
        # Sentinel is service 1; the body carries every parameter, as the app sends it.
        self.assertEqual(self.gateway.requests, [
            (1, "GET", "/approvals", None),
            (1, "POST", "/approvals/ap%2F1/decide", {"approval_id": "ap/1", "decision": "allow_once"})])
        # muse.ai replaced the login cookie while answering; the new one was saved and used at once.
        saved = self.web.login.read()
        self.assertEqual((saved["cookies"]["hatch_sess"], saved["expires"]["hatch_sess"], saved["computer"]),
                         ("second", 1900000000, "vm-1"))
        self.assertEqual([cookie for _, path, cookie, _ in self.gateway.site],
                         ["hatch_sess=first; theme=dark"] + ["hatch_sess=second; theme=dark"] * 2)
        self.assertEqual(oct(self.web.login.path.stat().st_mode & 0o777), "0o600")
        # Tokens and the computer are reused for the next call.
        self.web.call("egress.approvals")
        self.assertEqual(len(self.gateway.site), 3)

    def test_a_refused_connection_fetches_fresh_tokens_once(self):
        self.gateway.answers[("GET", "/goals")] = (200, "application/json", b'{"goals": []}')
        self.web.call("goals.list")
        self.gateway.reject_token = True
        self.assertEqual(self.web.call("goals.list")["body"], {"goals": []})
        self.assertEqual([path for _, path, _, _ in self.gateway.site].count("/api/hatch/token"), 2)

    def test_an_ended_login_is_reported_and_remembered(self):
        self.gateway.login = "revoked elsewhere"
        with self.assertRaises(museweb.MuseLoginRequired):
            self.web.call("goals.list")
        self.assertTrue(self.web.signed_in(), "one refusal may be muse.ai's mistake")
        with self.assertRaises(museweb.MuseLoginRequired):
            self.web.call("goals.list")
        self.assertFalse(self.web.signed_in())
        self.assertEqual(self.web.login.read()["cookies"]["hatch_sess"], "first")
        requests = len(self.gateway.site)
        with self.assertRaises(museweb.MuseLoginRequired):
            self.web.call("goals.list")
        self.assertEqual(len(self.gateway.site), requests, "an ended login is not sent again")

    def test_errors_streams_and_requests_follow_the_app(self):
        with self.assertRaises(museweb.MuseApiError) as refused:
            self.web.call("tasks.list")
        self.assertEqual(refused.exception.status, 404)
        self.gateway.answers[("POST", "/chat/subscribe")] = (
            200, "application/x-ndjson", b'{"a":1}\n{"b":2}\n')
        self.assertEqual(self.web.call("chat.subscribe", listen=1)["body"], [{"a": 1}, {"b": 2}])
        self.assertEqual(museweb.build_request("chat.history", {"session_id": "s", "limit": 5}),
                         (0, "GET", "/chat/history?session_id=s&limit=5", b""))
        self.assertEqual(museweb.build_request("fs.raw", {"path": "workspace/a b.txt"})[2],
                         "/fs/raw/workspace/a%20b.txt")
        self.assertEqual(museweb.build_request("tasks.delete", {"job_id": "j"}), (0, "DELETE", "/api/tasks/j", b""))
        with self.assertRaises(museweb.MuseWebError):
            museweb.build_request("tasks.run", {})
        with self.assertRaises(museweb.MuseWebError):
            museweb.build_request("no.such.method", {})

    def test_cookies_are_read_from_a_browser_export(self):
        exported = json.dumps([
            {"name": "hatch_sess", "value": "abc", "domain": ".muse.ai", "expires": 1900000000.5},
            {"name": "other", "value": "x", "domain": "gadgets.muse.ai", "expires": -1}])
        self.assertEqual(museweb.parse_cookies(exported), {"hatch_sess": ("abc", 1900000000.5)})
        self.assertEqual(museweb.parse_cookies("Cookie: a=1; hatch_sess=b=c"), {"a": ("1", None), "hatch_sess": ("b=c", None)})

    def test_the_method_table_is_read_from_the_apps_page(self):
        page = ("x=[{method:`tasks.run`,httpMethod:`POST`,path:`/tasks/{job_id}/run`},"
                "{method:`dyi.export`,httpMethod:`POST`,path:`/api/dyi/export`,service:`daemon`,"
                "requestBody:`empty`,requestTimeoutMs:39e5,maxResponseBytes:1/0}]")
        self.assertEqual(museweb.app_routes(page), [
            {"method": "dyi.export", "http": "POST", "path": "/api/dyi/export", "service": "daemon",
             "requestBody": "empty", "requestTimeoutMs": 3900000},
            {"method": "tasks.run", "http": "POST", "path": "/tasks/{job_id}/run"}])


class MuseAppPolicyTests(unittest.TestCase):
    def policy(self, name, *args):
        return plugin.call(plugin.__file__, name, *args)

    def test_approvals_are_listed_for_their_side_chat_in_plain_terms(self):
        listed = self.policy("approvalList", PENDING, JOB)
        self.assertEqual(listed["pending_in_all_chats"], 2)
        [approval] = listed["approvals"]
        self.assertEqual(approval["asks"], "Allow Muse to log in to apple.com using Browser?")
        self.assertEqual(approval["details"], ["Website: apple.com"])
        self.assertEqual([option["decision"] for option in approval["options"]],
                         ["allow_once", "allow_always", "deny"])
        self.assertNotIn("leon_decides", approval)
        everything = self.policy("approvalList", PENDING, None)["approvals"]
        self.assertEqual(everything[1]["leon_decides"], "it spends or moves money")

    def test_hermes_allows_what_was_asked_and_leaves_money_and_standing_choices_to_leon(self):
        allow = self.policy("approvalDecide", PENDING, "ap-1", "allow_once", False)
        self.assertEqual(allow["params"], {"approval_id": "ap-1", "decision": "allow_once"})
        self.assertIn("Leon decides", self.policy("approvalDecide", PENDING, "ap-1", "allow_always", False)["error"])
        always = self.policy("approvalDecide", PENDING, "ap-1", "allow_always", True)["params"]
        self.assertEqual((always["always_scope"], always["allow_always_scope"]),
                         ("destination_domain", "destination_domain"))
        self.assertIn("money", self.policy("approvalDecide", PENDING, "ap-2", "allow_once", False)["error"])
        self.assertIn("params", self.policy("approvalDecide", PENDING, "ap-2", "allow_once", True))
        self.assertIn("params", self.policy("approvalDecide", PENDING, "ap-2", "deny", False))
        self.assertIn("does not offer", self.policy("approvalDecide", PENDING, "ap-2", "deny_always", True)["error"])
        self.assertIn("No pending approval", self.policy("approvalDecide", PENDING, "ap-3", "allow_once", True)["error"])

    def test_risky_app_methods_wait_for_leon(self):
        for method in ("bootstrap.reset", "egress.permissions.hitl", "permissions.settings.update",
                       "credentials.delete", "ssh.operator.enable", "email.mailbox.delete"):
            self.assertIn("Ask Leon first", self.policy("methodGuard", method, False), method)
            self.assertIsNone(self.policy("methodGuard", method, True), method)
        for method in ("tasks.list", "egress.permissions", "permissions.settings", "session.rename",
                       "credentials.list", "egress.approval.history"):
            self.assertIsNone(self.policy("methodGuard", method, False), method)
        self.assertIn("muse_approve", self.policy("methodGuard", "egress.approval.decide", True))

    def test_methods_are_found_by_what_they_do(self):
        found = self.policy("methodSearch", list(museweb.ROUTES.values()), "scheduled tasks")
        self.assertEqual(found["methods"][0]["method"], "tasks.list")
        self.assertIn("tasks.run", found["methods"][0]["note"])
        self.assertGreater(found["total"], 400)
        clipped = self.policy("clipAnswer", [{"text": "x" * 400}] * 10, 1000)
        self.assertEqual((clipped["truncated"], clipped["shown"], clipped["of"]), (True, 2, 10))


class MuseAppToolTests(ToolTests):
    async def asyncSetUp(self):
        self.gateway = FakeGateway()
        self.gateway.answers[("GET", "/approvals")] = (200, "application/json", json.dumps(PENDING).encode())
        self.gateway.answers[("POST", "/approvals/ap-1/decide")] = (
            200, "application/json", b'{"approval": {"approval_id": "ap-1", "status": "allowed"}}')
        self.gateway.answers[("GET", "/tasks")] = (200, "application/json", b'{"schedules": []}')
        self.gateway.answers[("GET", "/media/raw/workspace/a.png")] = (200, "image/png", b"\x89PNG")

        def signed_in(directory):
            web = museweb.MuseWeb(directory, self.gateway)
            web.login.update(museweb.parse_cookies("hatch_sess=first"))
            return web

        patcher = patch.object(plugin, "MuseWeb", signed_in)
        patcher.start()
        self.addCleanup(patcher.stop)
        await super().asyncSetUp()
        constants = SimpleNamespace(get_hermes_home=lambda: self.directory)
        patcher = patch.dict(sys.modules, {"hermes_constants": constants})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_waiting_tools_report_a_pending_approval(self):
        self.assertEqual(self.call("muse_handoff", task="Check the App Store review")["status"], "not_delivered")
        now = relay_module.time.time()
        with self.store.connection() as db:
            db.execute("UPDATE handoffs SET approvals=1, approvals_at=?, approvals_wanted=?", (now, now - 1))
        result = self.call("muse_wait")
        self.assertEqual(result["status"], "waiting_for_approval")
        self.assertEqual([approval["approval"] for approval in result["approvals"]], ["ap-1"])
        self.assertIn("muse_approve", result["next"])
        self.assertNotIn("Beeper", result["next"])

    async def test_hermes_answers_an_approval_and_works_the_app_as_leon(self):
        self.assertEqual(len(self.call("muse_approvals")["approvals"]), 2)
        self.assertIn("Leon decides", self.call("muse_approve", approval="ap-2", decision="allow_once")["error"])
        result = self.call("muse_approve", approval="ap-1", decision="allow_once")
        self.assertEqual((result["decided"], result["status"]), ("allow_once", "allowed"))
        self.assertEqual(self.gateway.requests[-1],
                         (1, "POST", "/approvals/ap-1/decide", {"approval_id": "ap-1", "decision": "allow_once"}))
        self.assertEqual(self.call("muse_app", method="tasks.list"), {"schedules": []})
        self.assertIn("Ask Leon first", self.call("muse_app", method="bootstrap.reset")["error"])
        self.assertIn("no method", self.call("muse_app", method="tasks.explode")["error"])
        self.assertEqual(self.call("muse_app", method="goals.list")["error"], "Muse refused it (404).")
        self.assertEqual(self.call("muse_app_methods", query="approval history")["methods"][0]["method"],
                         "egress.approval.history")
        saved = self.call("muse_file", path="sandbox://workspace/a.png")
        self.assertEqual((Path(saved["saved"]).read_bytes(), saved["type"]), (b"\x89PNG", "image/png"))

    async def test_only_leons_own_requests_reach_the_app(self):
        requests = len(self.gateway.requests)
        for change in ({"user_id": "UOTHER"}, {"platform": "telegram"}):
            self.session.update(change)
            for name, args in (("muse_approvals", {}), ("muse_app", {"method": "tasks.list"}),
                               ("muse_approve", {"approval": "ap-1", "decision": "allow_once"}),
                               ("muse_file", {"path": "workspace/a.png"})):
                self.assertIn("only work on Leon's own requests", self.call(name, **args)["error"])
        self.assertEqual(len(self.gateway.requests), requests)
        # A job with no chat platform, such as a scheduled one, is Leon's own.
        self.session.update(platform="", user_id="")
        self.assertEqual(self.call("muse_app", method="tasks.list"), {"schedules": []})

    async def test_a_missing_login_says_how_to_sign_in(self):
        self.gateway.login = "ended"
        self.assertIn("Leon has to sign in again", self.call("muse_approvals")["error"])
