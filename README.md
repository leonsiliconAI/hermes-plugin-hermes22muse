# hermes22muse

Hermes hands its configured user's Slack requests to Meta Muse side chats, checks Muse's answers, and replies in the channel.

1. Hermes works out the request from the conversation. A short message such as "continue" becomes a full task built from the earlier messages, the channel and its links. If the request is too vague for a self-contained task, Hermes asks one clarifying question and mentions the user so they are pinged.
2. `muse_handoff` opens a new side chat, sends the task, and posts a reply in a thread under the user's message showing everything sent to Muse and a link to the side chat.
3. Muse's replies appear in that thread. Hermes judges them and follows up with `muse_reply`, shown as "Hermes → Muse", until it is satisfied. `muse_wait` keeps waiting when Muse needs more than about five minutes.
4. Hermes gives the answer in its normal reply, which [slack-flat-replies](../hermes-plugin-slack-flat-replies/README.md) puts at the top of the channel. In DMs, Hermes's replies stay in their thread.

A short guide is added to each of the user's Slack turns so Hermes follows these steps; the user's messages in a handoff thread get the thread's own guide. The guide does not outrank the profile's own instructions. If `SOUL.md` tells Hermes to delegate requests to other agents, add a rule there that the user's Slack requests go to Muse through `muse_handoff`, then run `hermes sessions refresh-prompts`. Without it, Hermes on Hermes Cloud answered through `delegate_task` instead. Messages typed directly in the side chat also appear in the thread. File bytes, edits and deletions do not sync. Treat both the thread and the side chat as visible to everyone with access to the Slack conversation.

## Set up Muse

Pair the [official Muse Gadget SDK Linux client](https://github.com/facebookincubator/muse-gadget-sdk/tree/fbfe3b0f717f7f468d0a94aa370ea0ca1a94ebfe/linux) on a Linux host with Bluetooth LE. Get an [SDK token](https://gadgets.muse.ai/settings/sdk-tokens), approve pairing in the Muse phone app, then check `sudo musegadget doctor` on the host.

Install [niclydon/muse-mcp](https://github.com/niclydon/muse-mcp) at `43299b2f4270e8994eff3978a72d1345abf93e4d` on the paired host. Follow its install instructions, using a service account that can read the SDK’s pairing state. A dedicated instance can start with `[]` in `directory.json`. This plugin registers each fresh side-chat ID in that directory and the first Link send opens it. The Muse-side bridge worker is only needed for other lifecycle operations such as archive or rename.

Run the server with `TRANSPORT=http` and its `ORIGIN_SHARED_SECRET` set to a random secret of at least 32 characters. Keep the endpoint private. Use loopback HTTP if it runs beside Hermes or is forwarded over SSH, or use HTTPS through a private network if it runs on another host. Set the server’s allowed hosts as described in its README when using a private hostname.

## Set up Hermes

Use a Hermes gateway (v0.23.3 or later) with the native Slack adapter and the slack-flat-replies plugin, so answers to top-level channel messages go to the channel. The Hermes installation needs its MCP 2.x Python dependencies. The Slack app needs `chat:write` for the conversations it handles.

Set these values in the Hermes profile’s environment and install the plugin. On Hermes Cloud, use the profile’s Keys page.

| Key                          | Value                                                                |
| ---------------------------- | -------------------------------------------------------------------- |
| `HERMES22MUSE_SLACK_USER_ID` | Slack user ID whose requests Hermes hands to the paired Muse account |
| `HERMES22MUSE_MCP_URL`       | Private `https://…/mcp` or `http://127.0.0.1:7899/mcp`               |
| `HERMES22MUSE_MCP_SECRET`    | The paired service’s `ORIGIN_SHARED_SECRET`                          |

```sh
hermes plugins install leonsiliconAI/hermes-plugin-hermes22muse --enable --yes-deps
```

If the same profile runs the AutoLinear Hermes plugin, update it to 1.4.6 or later. Its gateway intake leaves the configured user's requests to hermes22muse. If the separate AutoLinear Slack app also subscribes to that user’s messages for fast intake, pause those subscriptions before enabling this integration so the Worker does not move or edit an original request ahead of Hermes.

Restart the gateway and look for `hermes22muse: Muse handoff relay registered`. Ask Hermes for something harmless in a channel it answers, check the thread under your message and the side chat, and check that the answer arrives in the channel. Leave the gateway running for ongoing sync.

## Delivery and recovery

The SQLite database in `$HERMES_HOME/hermes22muse/relay.db` stores each handoff, the side chat's transcript, and pending deliveries in both directions with their receipts. The relay recognizes its own Muse echoes, retries outages with backoff, and finds a Muse send whose receipt was lost, unless its delivery marker has already fallen outside the readable history. If Slack accepted a thread post before its receipt could be stored, the post can repeat.

Muse counts as finished once its newest message is a completed reply and nothing has changed for eight seconds, the rule muse-mcp's `muse_chat_ask` uses, because agentic work can continue after a first answer. Each tool call waits up to 330 seconds, inside Hermes's default 420-second tool limit. The relay reads the side chat every five seconds while a handoff is in use, every minute for its first hour, every ten minutes for its first day, and then stops. The Link exposes the latest 50 events, and the relay posts a notice if a full window loses overlap. Oversized text is shortened with a link to the Slack thread.

Handoffs from 1.x keep their top-level threads, and messages they already relayed are not posted again. Hermes 2.0 no longer takes requests at gateway admission, so the user's replies in a handoff thread reach Hermes, which passes them on with `muse_reply`.

Run the relay tests from the repository root with Python 3.11 or newer and `python3 -m unittest discover -s apps/hermes-plugin-hermes22muse/__tests__`.

## Muse projects evaluated

- [niclydon/muse-mcp](https://github.com/niclydon/muse-mcp) exposes classified chat sends and scoped transcript reads over authenticated MCP. It uses Meta’s paired Gadget transport and needs neither a desktop browser session nor a Muse worker for these operations.
- [finik/pi-muse-bridge](https://github.com/finik/pi-muse-bridge) uses the same Gadget transport and maps Pi sessions to separate Muse chats. Its integration is specific to Pi.
- [99apps-id/muse-relay-mcp](https://github.com/99apps-id/muse-relay-mcp) uses a file relay and a Muse-side polling worker to run questions and retrieve answers. The relay here needs transcript reads for messages entered directly into a side chat.
- [OpenSWE/muse-bot](https://github.com/OpenSWE/muse-bot) exposes side-chat commands through Muse’s internal web gateway. Its documentation describes that gateway as unversioned.
