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

export function permalink(channel: string, ts: string) {
  return "https://app.slack.com/archives/" + channel + "/p" + ts.replaceAll(".", "");
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
    permalink(channel, ts) +
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
