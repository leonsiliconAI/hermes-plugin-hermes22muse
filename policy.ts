import { slackPermalink } from "#slack";

interface Message {
  id: string;
  role: string;
  text: string;
}

interface EventMessage {
  session_id?: string;
  role?: string;
  status?: string;
  display_text_ready?: boolean | number;
  message_id?: string;
  display_text?: string;
  id?: string;
  content?: unknown;
  text?: string;
}

interface Event extends EventMessage {
  event_name?: string;
  payload?: EventMessage;
}

interface Response {
  ok?: boolean;
  chat_events?: Event[];
  events?: Event[];
  result?: Response;
  response?: Response;
}

const unfinished = new Set([
  "pending",
  "queued",
  "running",
  "in_progress",
  "started",
  "active",
  "working",
  "streaming",
  "generating",
]);

export function history(
  result: { ok?: boolean; scoped_to_chat?: string; response?: Response },
  chat: string,
  limit: number,
) {
  if (!result.ok || !result.scoped_to_chat) return { error: "scoped history unavailable" };
  let response = result.response;
  let events: Event[] | undefined;
  for (
    let depth = 0;
    depth < 5 && response && typeof response === "object" && response.ok !== false;
    depth++
  ) {
    const found = "chat_events" in response ? response.chat_events : response.events;
    if (Array.isArray(found)) {
      events = found;
      break;
    }
    response = "result" in response ? response.result : response.response;
  }
  if (!events) return { error: "unrecognized Link history response" };

  const messages = new Map<string, Message>();
  let busy = false;
  for (const event of events) {
    if (!event || typeof event !== "object") continue;
    const { event_name: name, payload } = event;
    if (
      (name !== "message.user" && name !== "message.assistant") ||
      !payload ||
      typeof payload !== "object" ||
      Array.isArray(payload)
    )
      continue;
    const role = name.slice("message.".length);
    const session = payload.session_id || event.session_id;
    if ((session && session !== chat) || (payload.role != null && payload.role !== role)) continue;
    if (
      [payload.display_text_ready, event.display_text_ready].some(
        (ready) => ready === false || ready === 0,
      ) ||
      (role === "assistant" && unfinished.has(payload.status ?? ""))
    ) {
      // A reply still being written is held back until its final text is ready.
      busy ||= role === "assistant";
      continue;
    }
    const id = payload.message_id || event.message_id || payload.id;
    const text = [payload.display_text, event.display_text, payload.content, payload.text].find(
      (body): body is string => typeof body === "string" && Boolean(body.trim()),
    );
    if (typeof id === "string" && id && text) messages.set(id, { id, role, text });
  }
  return { messages: [...messages.values()], full: events.length >= limit, busy };
}

const timestamp = /^[0-9]+\.[0-9]{6}$/;
export function ownsRequest(
  user: string,
  owner: string,
  team: string,
  channel: string,
  ts?: string,
) {
  return (
    user === owner &&
    /^T[A-Z0-9]+$/.test(team) &&
    /^[CDG][A-Z0-9]+$/.test(channel) &&
    typeof ts === "string" &&
    ts === ts.trim() &&
    timestamp.test(ts)
  );
}

function escapeSlack(text: string) {
  return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

export function slackChunks(text: string) {
  // Count Unicode code points and leave room for the relay label.
  const rest = Array.from(text);
  const chunks = [];
  for (let start = 0; start < rest.length;) {
    let cut = Math.min(rest.length - start, 3400);
    while (Array.from(escapeSlack(rest.slice(start, start + cut).join(""))).length > 3400)
      cut = Math.floor(cut / 2);
    if (start + cut < rest.length) {
      const newline = rest.lastIndexOf("\n", start + cut - 1);
      if (newline >= start + Math.floor(cut / 2)) cut = newline + 1 - start;
    }
    chunks.push(escapeSlack(rest.slice(start, start + cut).join("")));
    start += cut;
  }
  return chunks;
}

export function slackDelivery(message: Message, echoed: boolean, stamp: string) {
  if (message.role === "user" && (echoed || message.text.startsWith(stamp + "[Hermes delivery ")))
    return [null];
  const label = message.role === "assistant" ? "Muse" : "You in Muse";
  return slackChunks(message.text).map((chunk) => label + "\n" + chunk);
}

export function sideChat(handoff: string) {
  return "https://muse.ai/thread/" + handoff;
}

/** What Muse receives for a new handoff, and the thread post that shows all of it. */
export function handoffTexts(task: string, channel: string, ts: string, handoff: string) {
  const muse =
    "Hermes, Leon's assistant, is handing you a task from Slack (" +
    slackPermalink(channel, ts) +
    "). Work on it in this chat and reply here. Hermes reads your replies, may follow up, " +
    "and passes your final answer to Leon. Leon may also join this chat.\n\nTask:\n" +
    task;
  return {
    muse,
    thread:
      "Handed to Muse. Side chat: " + sideChat(handoff) + "\nEverything sent to Muse:\n\n" + muse,
  };
}

export function followUpThread(text: string) {
  return "Hermes → Muse\n" + text;
}

export function handoffGuide(owner: string) {
  return [
    "[hermes22muse] Leon has Muse, his personal AI, do the work on his Slack requests, with you as the one who hands it over and checks the result:",
    '1. Work out what he is asking. Short messages such as "continue", "do it" or "try again" refer to earlier messages: read this conversation and, when needed, the channel\'s history, its Linear issue and the links there.',
    "2. If you can write a clear, self-contained task, call muse_handoff with it. Muse sees only that text, so include the goal, every relevant detail (names, links, IDs, earlier decisions, constraints) and what a good answer looks like. Don't do the task yourself first.",
    "3. If the request is too vague for that, don't call muse_handoff. Ask Leon one specific clarifying question and mention <@" +
      owner +
      "> so he is pinged.",
    "4. muse_handoff posts the full task in a thread under his message and returns Muse's reply. Judge it against his request. If it is incomplete, wrong or unclear, follow up with muse_reply; the exchange stays in that thread. If Muse is still working, call muse_wait.",
    "5. When you are satisfied, give Leon the answer in your normal reply. It goes to the channel, not the thread.",
    "Messages that ask for nothing (thanks, ok) and Hermes commands don't go to Muse.",
  ].join("\n");
}

export function threadGuide(handoff: string) {
  return (
    "[hermes22muse] This thread relays handoff " +
    handoff +
    " between you and Muse (side chat " +
    sideChat(handoff) +
    "), and Leon just wrote in it. If his message is for Muse or changes the task, pass it on with " +
    "muse_reply, written so Muse can act on it, and judge Muse's answer before you reply to Leon. " +
    "Muse's replies in this thread are posted by the relay."
  );
}

const nextSteps: Record<string, string> = {
  answered:
    "Check Muse's answer against Leon's request. If it is complete, reply to Leon with it " +
    "(your reply goes to the channel). Otherwise follow up with muse_reply.",
  working: "Muse is still working. Call muse_wait to keep waiting.",
  not_delivered:
    "Muse has not received the message yet (the Muse link may be down). Call muse_wait to keep " +
    "waiting, or tell Leon it is queued.",
};

export function nextStep(status: string) {
  return nextSteps[status] ?? "";
}

// Poll a handoff often while it is in use, then back off; one idle for a day stops polling.
const polls: [number, number][] = [
  [300, 5],
  [3600, 60],
  [86400, 600],
];

export function pollDelay(idleSeconds: number, pending: boolean, never: number) {
  if (pending) return 1;
  return polls.find(([age]) => idleSeconds < age)?.[1] ?? never;
}

export function retryDelay(failures: number, retryAfter: string) {
  const backoff = Math.min(300, 5 * 2 ** Math.min(failures, 6));
  return /^\d+$/.test(retryAfter) ? Math.max(backoff, Number(retryAfter)) : backoff;
}

interface SlackTurn {
  platform: string;
  user_id: string;
  scope_id: string;
  chat_id: string;
  thread_id?: string;
  message_id: string;
}

interface Handoff {
  id: string;
  channel: string;
  root: string;
  actor: string;
  request: string;
}

type Reply = { from: string; text: string };
type Answer = { status: string; messages: Reply[] };

export function slackTurn(current: SlackTurn | undefined, owner: string) {
  if (
    current?.platform === "slack" &&
    ownsRequest(current.user_id, owner, current.scope_id, current.chat_id, current.message_id) &&
    (!current.thread_id ||
      (current.thread_id === current.thread_id.trim() && timestamp.test(current.thread_id)))
  )
    return current;
}

export function guideForTurn(current: SlackTurn, owner: string, existing?: Handoff) {
  return {
    context:
      existing && existing.request !== current.message_id
        ? threadGuide(existing.id)
        : handoffGuide(owner),
  };
}

export function answerRows(
  job: { busy: boolean; polled: number; changed: number } | undefined,
  receipt: string | undefined,
  rows: { role: string; text: string }[],
  quiet: number,
): Answer {
  if (!job || !receipt) return { status: "not_delivered", messages: [] };
  const answered =
    rows.at(-1)?.role === "assistant" && !job.busy && job.polled - job.changed >= quiet;
  return {
    status: answered ? "answered" : "working",
    messages: rows.map((row) => ({
      from: row.role === "assistant" ? "muse" : "leon_in_muse",
      text: row.text,
    })),
  };
}

interface ToolHost {
  lookup(id: string): Promise<Handoff | undefined>;
  find(
    team: string,
    channel: string,
    request?: string,
    thread?: string,
  ): Promise<Handoff | undefined>;
  handoffId(team: string, channel: string, message: string): string;
  create(
    team: string,
    channel: string,
    request: string,
    thread: string,
    actor: string,
    museText: string,
    threadText: string,
  ): Promise<string | undefined>;
  followUp(id: string, text: string, threadText: string): Promise<unknown>;
  answer(id: string): Promise<Answer>;
  touch(id: string): Promise<void>;
  logHandoff(channel: string, message: string, job: string): void;
  now(): number;
  sleep(): Promise<void>;
}

export async function runTool(
  host: ToolHost | undefined,
  tool: "handoff" | "reply" | "wait",
  args: { task?: string; text?: string; handoff?: string },
  session: SlackTurn | undefined,
  owner: string,
  seconds: number,
) {
  if (!host) return { error: "The Muse relay is not running in this gateway." };
  const current = slackTurn(session, owner);
  let job: Handoff | undefined;
  if (tool === "handoff") {
    if (!current) return { error: "muse_handoff only hands over Leon's own Slack messages." };
    const task = String(args.task || "").trim();
    if (!task)
      return { error: "task is empty. Write the complete task, or ask Leon what he needs." };
    const { scope_id: team, chat_id: channel, message_id: message } = current;
    const thread = current.thread_id || message;
    const existing = await host.find(team, channel, message, thread);
    if (existing)
      return {
        error:
          "This message already belongs to handoff " +
          existing.id +
          ". Use muse_reply to send Muse more, or muse_wait to wait for its answer.",
      };
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
    } else if (current) {
      job = await host.find(
        current.scope_id,
        current.chat_id,
        current.message_id,
        current.thread_id,
      );
    }
    if (job && tool === "reply") await host.followUp(job.id, text, followUpThread(text));
  }
  if (!job) return { error: "No handoff found. Pass the handoff ID that muse_handoff returned." };
  const deadline = host.now() + seconds;
  for (let polls = 0; ; polls++) {
    const { status, messages } = await host.answer(job.id);
    if (status === "answered" || host.now() >= deadline)
      return {
        status,
        handoff: job.id,
        side_chat: sideChat(job.id),
        slack_thread: slackPermalink(job.channel, job.root),
        messages,
        next: nextStep(status),
      };
    if (polls % 3 === 0) await host.touch(job.id);
    await host.sleep();
  }
}

interface InboundHost {
  register(id: string): Promise<void>;
  exists(id: string): Promise<boolean>;
  history(id: string): Promise<Message[]>;
  attempted(id: string, ts: string): Promise<void>;
  accepted(id: string, ts: string, receipt: string): Promise<void>;
  send(id: string, text: string): Promise<string>;
  byteLength(text: string): number;
  clipUtf8(text: string, bytes: number): string;
}

export async function sendInbound(
  host: InboundHost,
  job: Handoff,
  entry: { ts: string; text: string; attempted: boolean },
  identifier: string,
  stamp: string,
) {
  const first = entry.ts === job.request;
  const prefix = "[Hermes delivery " + identifier + "]\n";
  if (first) await host.register(job.id);
  if (entry.attempted && (!first || (await host.exists(job.id)))) {
    const previous = (await host.history(job.id)).find(
      (message) => message.role === "user" && message.text.startsWith(stamp + prefix),
    );
    if (previous) {
      await host.accepted(job.id, entry.ts, previous.id);
      return;
    }
  }
  await host.attempted(job.id, entry.ts);
  let text = entry.text;
  if (host.byteLength(stamp + prefix + text) > 64 * 1024) {
    text =
      host.clipUtf8(text, 60 * 1024) +
      "\n\n[Shortened. The rest is in the Slack thread] " +
      slackPermalink(job.channel, job.root);
  }
  await host.accepted(job.id, entry.ts, await host.send(job.id, prefix + text));
}

export function collectPlan(
  messages: Message[],
  existing: string[],
  posted: string[],
  echoed: string[],
  full: boolean,
  busy: boolean,
  oldBusy: boolean | undefined,
  stamp: string,
) {
  const known = new Set(existing);
  const alreadyPosted = new Set(posted);
  const inbound = new Set(echoed);
  const rows = messages.filter((message) => !known.has(message.id));
  const output: { id: string; parts: string[] }[] = [];
  if (full && !known.size && !alreadyPosted.size && messages.length) {
    output.push({
      id: "gap/" + messages[0].id,
      parts: [
        "Muse’s recent history filled its read window. Some earlier messages may be missing.",
      ],
    });
  }
  for (const message of rows) {
    if (alreadyPosted.has(message.id)) continue;
    const parts = slackDelivery(message, inbound.has(message.id), stamp);
    if (parts[0] !== null) output.push({ id: message.id, parts: parts as string[] });
  }
  return {
    rows,
    output,
    activity: rows.length > 0,
    changed: rows.length > 0 || (oldBusy !== undefined && oldBusy !== busy),
  };
}

interface DirectoryEntry {
  in_directory?: boolean;
  sendable?: boolean;
  entry?: { kind?: string };
  directory_revision?: string;
  revision?: string;
}

export function directoryRegistration(current: DirectoryEntry) {
  if (current.in_directory) {
    if (current.sendable && current.entry?.kind === "side") return {};
    return { error: "registered side chat is suspended or has an unexpected kind" };
  }
  if (current.entry) return { error: "side chat ID is already present in Muse's registry" };
  const revision = current.directory_revision || current.revision;
  return revision ? { revision } : { error: "Muse directory revision unavailable" };
}
