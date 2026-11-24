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
      )
    )
      continue;
    if (role === "assistant" && unfinished.has(payload.status ?? "")) continue;
    const id = payload.message_id || event.message_id || payload.id;
    const text = [payload.display_text, event.display_text, payload.content, payload.text].find(
      (body): body is string => typeof body === "string" && Boolean(body.trim()),
    );
    if (typeof id === "string" && id && text) messages.set(id, { id, role, text });
  }
  return { messages: [...messages.values()], full: events.length >= limit };
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

export function requestBody(text: string, channel: string, ts: string) {
  return (
    text.trim() ||
    "The Slack request has no text. Its original message is available at " +
      permalink(channel, ts) +
      "."
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

export function museDelivery(first: boolean, channel: string, ts: string, text: string) {
  if (!first) return "Follow-up from the linked Slack thread\n\n" + text;
  return (
    "This side chat was opened for a Hermes request. Your chat replies are relayed " +
    "to the matching Slack thread. Work on the request below and reply here.\n" +
    "Original Slack message: " +
    permalink(channel, ts) +
    "\n\n" +
    text
  );
}

export function retryDelay(failures: number, retryAfter: string) {
  const backoff = Math.min(300, 5 * 2 ** Math.min(failures, 6));
  return /^\d+$/.test(retryAfter) ? Math.max(backoff, Number(retryAfter)) : backoff;
}
