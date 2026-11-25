# hermes22muse

Hermes hands its configured user's Slack requests to Meta Muse side chats, checks Muse's answers, and replies in the channel.

1. Hermes works out the request from the conversation. A short message such as "continue" becomes a full task built from the earlier messages, the channel and its links. If the request is too vague for a self-contained task, Hermes asks one clarifying question and mentions the user so they are pinged.
2. `muse_handoff` opens a new side chat and sends the task as the user's own message: Hermes writes it the way the user would, in the first person, and Muse receives exactly that text, with no mention of Hermes, Slack or a relay. A reply in a thread under the user's message shows that text and links the side chat.
3. The `slack22muse` app posts Muse’s replies as **Muse** in the original channel thread. For requests made in a one-to-one DM with Hermes, it opens a separate thread in the **Muse** DM and links back to the original request. Hermes checks them and sends follow-ups with `muse_reply`, also as the user, shown as "Sent to Muse as you". `muse_wait` keeps waiting when Muse needs more than about five minutes.
4. Hermes gives a concise, reviewed answer in its normal reply. [slack-flat-replies](../hermes-plugin-slack-flat-replies/README.md) puts channel answers at the top level.

A short guide is added to each of the user's Slack turns so Hermes follows these steps; the user's messages in a handoff thread get the thread's own guide. The guide does not outrank the profile's own instructions. If `SOUL.md` tells Hermes to delegate requests to other agents, add a rule there that the user's Slack requests go to Muse through `muse_handoff`, then run `hermes sessions refresh-prompts`. Without it, Hermes on Hermes Cloud answered through `delegate_task` instead. Messages you type directly in the side chat appear in Slack with a "You in Muse" label. File bytes, edits and deletions do not sync. Treat both the thread and the side chat as visible to everyone with access to the Slack conversation.

## Approvals

When Muse needs the user's approval in a side chat, Hermes answers it the way the user would, without UI automation. While a tool waits, the relay reads the side chat's pending approval count from muse-mcp's `muse_chat_status`: after about 20 seconds of work, every 30 seconds after that, and once more before an answer is returned. A pending approval makes the tool return `waiting_for_approval`. Hermes then finds Muse's request in Beeper (the Muse bridge's "Muse is waiting for your approval … `!muse approval-allow <code>`" message) and applies the user's approval rule: it sends `!muse approval-allow <code>` when the action is what the user asked for, and otherwise denies it or asks the user with a mention. Payments, purchases, money transfers and password, login or security changes always go to the user. If no request reaches Beeper within a minute, Hermes tells the user what Muse is waiting for.

The Gadget link answers 403 for Muse's approvals API, and muse-mcp can only observe approvals, so Beeper's Muse bridge is the route. It needs Hermes's Beeper inbox and a rule in `SOUL.md` describing the approval commands.

## Set up Muse

Pair the [official Muse Gadget SDK Linux client](https://github.com/facebookincubator/muse-gadget-sdk/tree/fbfe3b0f717f7f468d0a94aa370ea0ca1a94ebfe/linux) on a Linux host with Bluetooth LE. Get an [SDK token](https://gadgets.muse.ai/settings/sdk-tokens), approve pairing in the Muse phone app, then check `sudo musegadget doctor` on the host.

Install [niclydon/muse-mcp](https://github.com/niclydon/muse-mcp) at `43299b2f4270e8994eff3978a72d1345abf93e4d` on the paired host. Follow its install instructions, using a service account that can read the SDK’s pairing state. A dedicated instance can start with `[]` in `directory.json`. This plugin registers each fresh side-chat ID in that directory and the first Link send opens it. The Muse-side bridge worker is only needed for other lifecycle operations such as archive or rename.

Run the server with `TRANSPORT=http` and its `ORIGIN_SHARED_SECRET` set to a random secret of at least 32 characters. Keep the endpoint private. Use loopback HTTP if it runs beside Hermes or is forwarded over SSH, or use HTTPS through a private network if it runs on another host. Set the server’s allowed hosts as described in its README when using a private hostname.

## Set up the Slack app

Create an app from the [slack22muse manifest](@assets/slack-app-manifest.json) and install it in the same workspace as Hermes. Slack shows the bot as **Muse**. The manifest lets it post to public channels and open a DM with the person who requested a handoff. Invite `@Muse` to each private channel where it should relay. Copy the app’s Bot User OAuth Token into `SLACK22MUSE_BOT_TOKEN` in the Hermes profile’s environment. [Slack app setup](https://docs.slack.dev/app-manifests/configuring-apps-with-app-manifests/)

## Set up Hermes

Use a Hermes gateway (v0.23.3 or later) with the native Slack adapter and the slack-flat-replies plugin, so answers to top-level channel messages go to the channel. The Hermes installation needs its MCP 2.x Python dependencies. The Slack app needs `chat:write` for the conversations it handles.

Set these values in the Hermes profile’s environment and install the plugin. On Hermes Cloud, use the profile’s Keys page.

| Key                          | Value                                                                |
| ---------------------------- | -------------------------------------------------------------------- |
| `HERMES22MUSE_SLACK_USER_ID` | Slack user ID whose requests Hermes hands to the paired Muse account |
| `HERMES22MUSE_MCP_URL`       | Private `https://…/mcp` or `http://127.0.0.1:7899/mcp`               |
| `HERMES22MUSE_MCP_SECRET`    | The paired service’s `ORIGIN_SHARED_SECRET`                          |
| `SLACK22MUSE_BOT_TOKEN`      | The slack22muse app’s Bot User OAuth Token (`xoxb-…`)                |

```sh
hermes plugins install leonsiliconAI/hermes-plugin-hermes22muse --enable --yes-deps
```

If the same profile runs the AutoLinear Hermes plugin, update it to 1.4.6 or later. Its gateway intake leaves the configured user's requests to hermes22muse. If the separate AutoLinear Slack app also subscribes to that user’s messages for fast intake, pause those subscriptions before enabling this integration so the Worker does not move or edit an original request ahead of Hermes.

For an existing installation, run `hermes plugins update hermes22muse` after setting the key. Restart the gateway and look for `hermes22muse: Muse handoff relay registered`. Ask Hermes for something harmless in a channel it answers, check that the thread shows Hermes’s handoff and Muse’s reply under their respective names, and check that the answer arrives in the channel. For a DM request, check the separate conversation with Muse. Leave the gateway running for ongoing sync.

## Delivery and recovery

The SQLite database in `$HERMES_HOME/hermes22muse/relay.db` stores each handoff, the side chat's transcript, and pending deliveries in both directions with their receipts. The relay recognizes its own Muse echoes by their receipts, retries outages with backoff, and finds a Muse send whose receipt was lost by its exact text, unless it has already fallen outside the readable history. Messages carry no delivery marker. muse-mcp stamps every send with its provenance (`⟦via muse-mcp · from …⟧`); the [Muse Gadget launcher](../muse-gadget/README.md) on Hermes Cloud removes that stamp so Muse sees only the user's words. If the Muse bot cannot post to Slack, the relay keeps those posts queued and Hermes can still work with Muse. Check the token and channel membership when the log reports a Slack delivery retry. If Slack accepted a thread post before its receipt could be stored, the post can repeat.

Muse counts as finished once its newest message is a completed reply and nothing has changed for eight seconds, the rule muse-mcp's `muse_chat_ask` uses, because agentic work can continue after a first answer. Each tool call waits up to 330 seconds, inside Hermes's default 420-second tool limit. The relay reads the side chat every five seconds while a handoff is in use, every minute for its first hour, every ten minutes for its first day, and then stops. The Link exposes the latest 50 events, and the relay posts a notice if a full window loses overlap. Oversized text is shortened with a link to the Slack thread.

Handoffs from 1.x keep their top-level threads, and messages they already relayed are not posted again. Hermes 2.0 no longer takes requests at gateway admission, so the user's replies in a handoff thread reach Hermes, which passes them on with `muse_reply`.

From the repository root, build the policy with `vp -C apps/hermes-plugin-hermes22muse pack`. Then run `uv run --no-project --python 3.14 --with pythonmonkey==1.5.1 --with pytest python -m pytest apps/hermes-plugin-hermes22muse/__tests__`.

## Muse projects evaluated

- [niclydon/muse-mcp](https://github.com/niclydon/muse-mcp) exposes classified chat sends and scoped transcript reads over authenticated MCP. It uses Meta’s paired Gadget transport and needs neither a desktop browser session nor a Muse worker for these operations.
- [finik/pi-muse-bridge](https://github.com/finik/pi-muse-bridge) uses the same Gadget transport and maps Pi sessions to separate Muse chats. Its integration is specific to Pi.
- [99apps-id/muse-relay-mcp](https://github.com/99apps-id/muse-relay-mcp) uses a file relay and a Muse-side polling worker to run questions and retrieve answers. The relay here needs transcript reads for messages entered directly into a side chat.
- [OpenSWE/muse-bot](https://github.com/OpenSWE/muse-bot) exposes side-chat commands through Muse’s internal web gateway. Its documentation describes that gateway as unversioned.
