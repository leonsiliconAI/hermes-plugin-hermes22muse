Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
// \0hermes-slack.ts
function slackPermalink(channel, ts) {
	return "https://app.slack.com/archives/" + channel + "/p" + ts.replaceAll(".", "");
}
// policy.ts
const finished = /* @__PURE__ */ new Set([
	"completed",
	"failed",
	"interrupted",
	"cancelled",
	"canceled",
	"error"
]);
function history(result, chat, limit) {
	if (!result.ok || !result.scoped_to_chat) return { error: "scoped history unavailable" };
	let response = result.response;
	let events;
	for (let depth = 0; depth < 5 && response && typeof response === "object" && response.ok !== false; depth++) {
		const found = "chat_events" in response ? response.chat_events : response.events;
		if (Array.isArray(found)) {
			events = found;
			break;
		}
		response = "result" in response ? response.result : response.response;
	}
	if (!events) return { error: "unrecognized Link history response" };
	const messages = /* @__PURE__ */ new Map();
	let busy = false;
	for (const event of events) {
		if (!event || typeof event !== "object") continue;
		const { event_name: name, payload } = event;
		if (name !== "message.user" && name !== "message.assistant" || !payload || typeof payload !== "object" || Array.isArray(payload)) continue;
		const role = name.slice(8);
		const session = payload.session_id || event.session_id;
		if (session && session !== chat || payload.role != null && payload.role !== role) continue;
		if ([payload.display_text_ready, event.display_text_ready].some((ready) => ready === false || ready === 0) || role === "assistant" && !finished.has(payload.status ?? "")) {
			busy ||= role === "assistant";
			continue;
		}
		const id = payload.message_id || event.message_id || payload.id;
		const text = [
			payload.display_text,
			event.display_text,
			payload.content,
			payload.text
		].find((body) => typeof body === "string" && Boolean(body.trim()));
		if (typeof id === "string" && id && text) messages.set(id, {
			id,
			role,
			text
		});
	}
	return {
		messages: [...messages.values()],
		full: events.length >= limit,
		busy
	};
}
const timestamp = /^[0-9]+\.[0-9]{6}$/;
function ownsRequest(user, owner, team, channel, ts) {
	return user === owner && /^T[A-Z0-9]+$/.test(team) && /^[CDG][A-Z0-9]+$/.test(channel) && typeof ts === "string" && ts === ts.trim() && timestamp.test(ts);
}
function escapeSlack(text) {
	return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}
function slackChunks(text) {
	const rest = Array.from(text);
	const chunks = [];
	for (let start = 0; start < rest.length;) {
		let cut = Math.min(rest.length - start, 3400);
		while (Array.from(escapeSlack(rest.slice(start, start + cut).join(""))).length > 3400) cut = Math.floor(cut / 2);
		if (start + cut < rest.length) {
			const newline = rest.lastIndexOf("\n", start + cut - 1);
			if (newline >= start + Math.floor(cut / 2)) cut = newline + 1 - start;
		}
		chunks.push(escapeSlack(rest.slice(start, start + cut).join("")));
		start += cut;
	}
	return chunks;
}
function slackDelivery(message, echoed, stamp) {
	if (message.role === "user" && (echoed || message.text.startsWith(stamp + "[Hermes delivery "))) return [null];
	const label = message.role === "assistant" ? "Muse" : "You in Muse";
	return slackChunks(message.text).map((chunk) => label + "\n" + chunk);
}
function sideChat(handoff) {
	return "https://muse.ai/thread/" + handoff;
}
function museDmOpening(text, channel, thread) {
	return text + "\n\nOriginal Slack thread: " + slackPermalink(channel, thread);
}
/**
* What Muse receives for a new handoff, and the thread post that shows it. Muse gets the task
* exactly as written, as a message from Leon, with no relay framing.
*/
function handoffTexts(task, channel, _ts, handoff) {
	const slackReply = channel.startsWith("D") ? "\nMuse will send replies in its own Slack DM." : "";
	return {
		muse: task,
		thread: "Sent to Muse as you. Side chat: " + sideChat(handoff) + slackReply + "\n\n" + task
	};
}
function followUpThread(text) {
	return "Sent to Muse as you\n" + text;
}
function handoffGuide(owner) {
	return [
		"[hermes22muse] Leon has Muse, his personal AI, do the work on his Slack requests, with you as the one who hands it over and checks the result:",
		"1. Work out what he is asking. Short messages such as \"continue\", \"do it\" or \"try again\" refer to earlier messages: read this conversation and, when needed, the channel's history, its Linear issue and the links there.",
		"2. If you can write a clear, self-contained task, call muse_handoff with it. Muse receives it as a message from Leon himself, so write it the way he would type it to Muse: first person, short and direct, without mentioning yourself, Hermes, Slack, a relay or that you act for him. Muse sees only that text, so include the goal, every relevant detail (names, links, IDs, earlier decisions, constraints) and what a good answer looks like. Don't do the task yourself first.",
		"3. If the request is too vague for that, don't call muse_handoff. Ask Leon one specific clarifying question and mention <@" + owner + "> so he is pinged.",
		"4. muse_handoff posts the full task in a thread under his message and returns Muse's reply. Judge it against his request. If it is incomplete, wrong or unclear, follow up with muse_reply, also written as Leon; the exchange stays in that thread. If Muse is still working, call muse_wait. If it is waiting for Leon's approval, the result says how to answer it.",
		"5. When you are satisfied, give Leon a concise, checked answer in your normal reply. The Muse bot relays the transcript in the original channel thread or, for a DM with you, in its own DM with Leon. Keep your own reply focused on the outcome, any corrections, and what Leon needs to know.",
		"Messages that ask for nothing (thanks, ok) and Hermes commands don't go to Muse."
	].join("\n");
}
function threadGuide(handoff) {
	return "[hermes22muse] This thread relays handoff " + handoff + " between you and Muse (side chat " + sideChat(handoff) + "), and Leon just wrote in it. If his message is for Muse or changes the task, pass it on with muse_reply, written as Leon himself would write it to Muse (first person, no mention of you or Hermes), and judge Muse's answer before you reply to Leon. The Muse bot posts Muse's messages in channel threads or in its own Slack DM with Leon. Keep your own reply focused on the checked outcome and any added context.";
}
const nextSteps = {
	answered: "Check Muse's answer against Leon's request. If it is complete, give Leon the checked outcome in your usual reply. Otherwise follow up with muse_reply, written as Leon.",
	working: "Muse is still working. Call muse_wait to keep waiting.",
	not_delivered: "Muse has not received the message yet (the Muse link may be down). Call muse_wait to keep waiting, or tell Leon it is queued.",
	waiting_for_approval: "Muse is waiting for Leon's approval in this side chat. Answer it the way Leon would, through Beeper: find Muse's request on the muse network (list_conversations, then list_messages), the newest message like \"Muse is waiting for your approval: … Allow it once with `!muse approval-allow <code>`\". Apply Leon's approval rule: if it does what he asked for (the right action, recipient or site, and content), send exactly `!muse approval-allow <code>` in that chat with send_message and say in the Slack thread what you approved; otherwise send `!muse approval-deny <code>` or ask Leon, mentioning him. Payments, purchases, money transfers and password, login or security changes always go to Leon. If no request has reached Beeper within a minute, tell Leon what Muse is waiting for and mention him. Then call muse_wait."
};
function nextStep(status) {
	return nextSteps[status] ?? "";
}
const polls = [
	[300, 5],
	[3600, 60],
	[86400, 600]
];
function pollDelay(idleSeconds, pending, never) {
	if (pending) return 1;
	return polls.find(([age]) => idleSeconds < age)?.[1] ?? never;
}
function retryDelay(failures, retryAfter) {
	const backoff = Math.min(300, 5 * 2 ** Math.min(failures, 6));
	return /^\d+$/.test(retryAfter) ? Math.max(backoff, Number(retryAfter)) : backoff;
}
function slackTurn(current, owner) {
	if (current?.platform === "slack" && ownsRequest(current.user_id, owner, current.scope_id, current.chat_id, current.message_id) && (!current.thread_id || current.thread_id === current.thread_id.trim() && timestamp.test(current.thread_id))) return current;
}
function guideForTurn(current, owner, existing) {
	return { context: existing && existing.request !== current.message_id ? threadGuide(existing.id) : handoffGuide(owner) };
}
function answerRows(job, receipt, rows, quiet) {
	if (!job || !receipt) return {
		status: "not_delivered",
		messages: []
	};
	return {
		status: rows.at(-1)?.role === "assistant" && !job.busy && job.polled - job.changed >= quiet ? "answered" : "working",
		messages: rows.map((row) => ({
			from: row.role === "assistant" ? "muse" : "leon_in_muse",
			text: row.text
		}))
	};
}
/**
* What a waiting tool does about Muse's approvals: report a pending one, ask the relay to measure,
* finish ("clear"), or keep waiting. Muse is measured after about 20 s of work and every 30 s after,
* and once more before an answer is returned, since Muse can answer and still wait on an approval.
* A measurement that doesn't arrive within 20 s never holds back an answer.
*/
function approvalDecision(approvals, status, now, waited) {
	const measured = approvals.wanted > 0 && approvals.checked >= approvals.wanted;
	if (measured && (approvals.count ?? 0) > 0 && now - approvals.checked < 60) return "pending";
	const outstanding = approvals.wanted > approvals.checked;
	if (status === "answered") {
		if (measured && approvals.checked >= approvals.changed) return "clear";
		if (outstanding) return now - approvals.wanted > 20 ? "clear" : "wait";
		return "check";
	}
	if (status === "working" && waited >= 20 && !outstanding && now - approvals.wanted >= 30) return "check";
	return "wait";
}
async function runTool(host, tool, args, session, owner, seconds) {
	if (!host) return { error: "The Muse relay is not running in this gateway." };
	const current = slackTurn(session, owner);
	let job;
	if (tool === "handoff") {
		if (!current) return { error: "muse_handoff only hands over Leon's own Slack messages." };
		const task = String(args.task || "").trim();
		if (!task) return { error: "task is empty. Write the complete task, or ask Leon what he needs." };
		const { scope_id: team, chat_id: channel, message_id: message } = current;
		const thread = current.thread_id || message;
		const existing = await host.find(team, channel, message, thread);
		if (existing) return { error: "This message already belongs to handoff " + existing.id + ". Use muse_reply to send Muse more, or muse_wait to wait for its answer." };
		const texts = handoffTexts(task, channel, message, host.handoffId(team, channel, message));
		const id = await host.create(team, channel, message, thread, owner, texts.muse, texts.thread);
		if (!id) return { error: "This Slack thread already has a handoff. Use muse_reply instead." };
		host.logHandoff(channel, message, id);
		job = await host.lookup(id);
	} else {
		const text = String(args.text || "").trim();
		if (tool === "reply" && !text) return { error: "text is empty." };
		const handoff = String(args.handoff || "").trim();
		if (handoff) {
			const match = await host.lookup(handoff);
			if (match?.actor === owner) job = match;
		} else if (current) job = await host.find(current.scope_id, current.chat_id, current.message_id, current.thread_id);
		if (job && tool === "reply") await host.followUp(job.id, text, followUpThread(text));
	}
	if (!job) return { error: "No handoff found. Pass the handoff ID that muse_handoff returned." };
	const started = host.now();
	const deadline = started + seconds;
	for (let polls = 0;; polls++) {
		const answer = await host.answer(job.id);
		const approvals = await host.approvals(job.id);
		const decision = approvalDecision(approvals, answer.status, host.clock(), host.now() - started);
		const status = decision === "pending" ? "waiting_for_approval" : answer.status === "answered" && decision !== "clear" ? "working" : answer.status;
		if (decision === "pending" || decision === "clear" || host.now() >= deadline) return {
			status,
			handoff: job.id,
			side_chat: sideChat(job.id),
			slack_thread: slackPermalink(job.channel, job.root),
			messages: answer.messages,
			...decision === "pending" ? { approvals_pending: approvals.count } : {},
			next: nextStep(status)
		};
		if (decision === "check") await host.wantApprovals(job.id);
		else if (polls % 3 === 0) await host.touch(job.id);
		await host.sleep();
	}
}
async function sendInbound(host, job, entry, stamp) {
	const first = entry.ts === job.request;
	let text = entry.text;
	if (host.byteLength(stamp + text) > 65536) text = host.clipUtf8(text, 61440) + "\n\n[Shortened. The rest is in the Slack thread] " + slackPermalink(job.channel, job.root);
	if (first) await host.register(job.id);
	if (entry.attempted && (!first || await host.exists(job.id))) {
		const history = await host.history(job.id);
		for (let index = history.length - 1; index >= 0; index--) {
			const message = history[index];
			if (message.role === "user" && (message.text === text || message.text === stamp + text)) {
				await host.accepted(job.id, entry.ts, message.id);
				return;
			}
		}
	}
	await host.attempted(job.id, entry.ts);
	await host.accepted(job.id, entry.ts, await host.send(job.id, text));
}
function collectPlan(messages, existing, posted, echoed, full, busy, oldBusy, stamp) {
	const known = new Map(existing.map((row) => [row.id, row.text]));
	const alreadyPosted = new Set(posted);
	const inbound = new Set(echoed);
	const rows = messages.filter((message) => !known.has(message.id));
	const updates = messages.filter((message) => known.has(message.id) && known.get(message.id) !== message.text);
	const output = [];
	if (full && !known.size && !alreadyPosted.size && messages.length) output.push({
		id: "gap/" + messages[0].id,
		parts: ["Muse’s recent history filled its read window. Some earlier messages may be missing."]
	});
	for (const message of rows) {
		if (alreadyPosted.has(message.id)) continue;
		const parts = slackDelivery(message, inbound.has(message.id), stamp);
		if (parts[0] !== null) output.push({
			id: message.id,
			parts
		});
	}
	for (const message of updates) {
		const before = known.get(message.id) ?? "";
		if (message.role === "assistant" && message.text.length > before.length && message.text.startsWith(before)) output.push({
			id: "update/" + message.id + "/" + message.text.length,
			parts: slackChunks(message.text).map((chunk) => "Muse (full reply)\n" + chunk)
		});
	}
	const changes = rows.length + updates.length;
	return {
		rows,
		updates,
		output,
		activity: changes > 0,
		changed: changes > 0 || oldBusy !== void 0 && oldBusy !== busy
	};
}
function directoryRegistration(current) {
	if (current.in_directory) {
		if (current.sendable && current.entry?.kind === "side") return {};
		return { error: "registered side chat is suspended or has an unexpected kind" };
	}
	if (current.entry) return { error: "side chat ID is already present in Muse's registry" };
	const revision = current.directory_revision || current.revision;
	return revision ? { revision } : { error: "Muse directory revision unavailable" };
}
exports.answerRows = answerRows;
exports.approvalDecision = approvalDecision;
exports.collectPlan = collectPlan;
exports.directoryRegistration = directoryRegistration;
exports.followUpThread = followUpThread;
exports.guideForTurn = guideForTurn;
exports.handoffGuide = handoffGuide;
exports.handoffTexts = handoffTexts;
exports.history = history;
exports.museDmOpening = museDmOpening;
exports.nextStep = nextStep;
exports.ownsRequest = ownsRequest;
exports.pollDelay = pollDelay;
exports.retryDelay = retryDelay;
exports.runTool = runTool;
exports.sendInbound = sendInbound;
exports.sideChat = sideChat;
exports.slackChunks = slackChunks;
exports.slackDelivery = slackDelivery;
exports.slackTurn = slackTurn;
exports.threadGuide = threadGuide;
