# hermes22muse

Hermes hands its configured user's Slack requests to Meta Muse side chats, checks Muse's answers, and replies in the channel. It also [works the Muse app as the user](#the-muse-app): it answers Muse's approval requests and can call anything the app can.

1. Hermes works out the request from the conversation. A short message such as "continue" becomes a full task built from the earlier messages, the channel and its links. If the request is too vague for a self-contained task, Hermes asks one clarifying question and mentions the user so they are pinged.
2. `muse_handoff` opens a new side chat and sends the task as the user's own message: Hermes writes it the way the user would, in the first person, and Muse receives exactly that text, with no mention of Hermes, Slack or a relay. A reply in a thread under the user's message shows that text and links the side chat.
3. The `slack22muse` app posts Muse’s replies as **Muse** in the original channel thread. For requests made in a one-to-one DM with Hermes, it opens a separate thread in the **Muse** DM and links back to the original request. Hermes checks them and sends follow-ups with `muse_reply`, also as the user, shown as "Sent to Muse as you". `muse_wait` keeps waiting when Muse needs more than about five minutes.
4. Hermes gives a concise, reviewed answer in its normal reply. [slack-flat-replies](../hermes-plugin-slack-flat-replies/README.md) puts channel answers at the top level.

A short guide is added to each of the user's Slack turns so Hermes follows these steps; the user's messages in a handoff thread get the thread's own guide. The guide does not outrank the profile's own instructions. If `SOUL.md` tells Hermes to delegate requests to other agents, add a rule there that the user's Slack requests go to Muse through `muse_handoff`, then run `hermes sessions refresh-prompts`. Without it, Hermes on Hermes Cloud answered through `delegate_task` instead. Messages you type directly in the side chat appear in Slack with a "You in Muse" label. File bytes, edits and deletions do not sync. Treat both the thread and the side chat as visible to everyone with access to the Slack conversation.

## The Muse app

Hermes can also do what the user does in the Muse app, without a browser or screen control. The plugin speaks the app's own protocol: it trades a saved muse.ai login for a short-lived token, then calls the user's personal Muse computer over the app's encrypted WebSocket ([`museweb.py`](museweb.py)). It runs inside the Hermes gateway and needs no other machine.

| Tool               | What it does                                                                                                         |
| ------------------ | -------------------------------------------------------------------------------------------------------------------- |
| `muse_approvals`   | Lists what Muse is waiting for the user to approve (the app's **Needs review**), for one handoff or for every chat   |
| `muse_approve`     | Answers a request the way the app's buttons do: `allow_once`, `allow_always`, `deny` or `deny_always`                |
| `muse_app_methods` | Finds the app's methods by what they do. The table in [`muse-routes.json`](muse-routes.json) has all 444             |
| `muse_app`         | Calls any of them: chats, scheduled tasks, goals, ideas, feed, files, memory, skills, connected apps, settings, mail |
| `muse_file`        | Downloads a file Muse made to `$HERMES_HOME/hermes22muse/files/`                                                     |

These tools act as the user, so they only run on the user's own Slack messages and on jobs with no chat platform, such as scheduled ones. A message from a bot or another person gets an error.

Some actions wait for the user's yes in the conversation, which Hermes passes as `leon_confirmed`:

- Allowing an approval that spends or moves money, or one that doesn't say what it allows.
- Any "always" choice, since it changes what Muse may do from then on.
- App methods that erase data, share the account or change what Muse may do unasked: resets, the data export, permission and human-review settings, saved logins, mailbox creation and deletion, SSH access, peer connections and sharing.

Watching or steering Muse's live browser is the one part of the app the tools leave out, because it is a remote screen.

## Approvals

When Muse needs the user's approval in a side chat, Hermes answers it the way the user would. While a tool waits, the relay counts the side chat's pending approvals: after about 20 seconds of work, every 30 seconds after that, and once more before an answer is returned. A pending approval makes the tool return `waiting_for_approval` with the requests themselves. Hermes applies the user's approval rule: it calls `muse_approve` with `allow_once` when the action is what the user asked for, and otherwise denies it or asks the user with a mention.

Without a saved muse.ai login, the relay counts approvals through muse-mcp's `muse_chat_status` and Hermes answers them through Beeper's Muse bridge (`!muse approval-allow <code>`), which needs Hermes's Beeper inbox.

## Sign Hermes in to muse.ai

The Muse app tools need the user's muse.ai login, saved once as `$HERMES_HOME/hermes22muse/muse-login.json` (mode 600). Give Hermes a login of its own: muse.ai can replace the login cookie whenever it is used and drop the old one, so a login shared with a browser signs one of them out.

1. Sign in at <https://muse.ai/> in a browser profile used for nothing else, and export its cookies as JSON (`agent-browser cookies get --json`, or the `Cookie:` request header of any muse.ai request).
2. Turn them into the login file. This also checks the login against muse.ai and finds the Muse computer:

   ```sh
   python3 museweb.py login /tmp/muse-login < cookies.json
   ```

3. Upload `/tmp/muse-login/muse-login.json` to `$HERMES_HOME/hermes22muse/` on the Hermes host, delete the local copy, and clear the browser profile's cookies without signing out there (signing out ends the login).

`python3 museweb.py call $HERMES_HOME/hermes22muse identity` checks it on the host. The relay reads the session every six hours, which makes muse.ai extend the login by 30 days, and saves every cookie muse.ai reissues at once. If muse.ai refuses the login twice, Hermes sends the user one Slack DM asking them to sign it in again; handoffs keep working meanwhile.

The method table comes from the Muse desktop app's own page. Refresh it after a Muse update with `python3 museweb.py routes /Applications/Muse.app/Contents/Resources/hatch/index.html`, keeping the size and time limits the old table has for methods whose limits the page names by variable.

## Set up Muse

Pair the [official Muse Gadget SDK Linux client](https://github.com/facebookincubator/muse-gadget-sdk/tree/fbfe3b0f717f7f468d0a94aa370ea0ca1a94ebfe/linux) on a Linux host with Bluetooth LE. Get an [SDK token](https://gadgets.muse.ai/settings/sdk-tokens), approve pairing in the Muse phone app, then check `sudo musegadget doctor` on the host.

Install [niclydon/muse-mcp](https://github.com/niclydon/muse-mcp) at `43299b2f4270e8994eff3978a72d1345abf93e4d` on the paired host. Follow its install instructions, using a service account that can read the SDK’s pairing state. A dedicated instance can start with `[]` in `directory.json`. This plugin registers each fresh side-chat ID in that directory and the first Link send opens it. The Muse-side bridge worker is only needed for other lifecycle operations such as archive or rename.

Run the server with `TRANSPORT=http` and its `ORIGIN_SHARED_SECRET` set to a random secret of at least 32 characters. Keep the endpoint private. Use loopback HTTP if it runs beside Hermes or is forwarded over SSH, or use HTTPS through a private network if it runs on another host. Set the server’s allowed hosts as described in its README when using a private hostname.

## Set up the Slack app

Create an app from the [slack22muse manifest](@assets/slack-app-manifest.json) and install it in the same workspace as Hermes. Slack shows the bot as **Muse**. The manifest lets it post to public channels and open a DM with the person who requested a handoff. Invite `@Muse` to each private channel where it should relay. Copy the app’s Bot User OAuth Token into `SLACK22MUSE_BOT_TOKEN` in the Hermes profile’s environment. [Slack app setup](https://docs.slack.dev/app-manifests/configuring-apps-with-app-manifests/)

## Set up Hermes

Use a Hermes gateway (v0.23.3 or later) with the native Slack adapter and the slack-flat-replies plugin, so answers to top-level channel messages go to the channel. The Hermes installation needs its MCP 2.x Python dependencies. The Muse app tools use `cryptography`, `httpx` and `websockets`, which Hermes ships. The Slack app needs `chat:write` for the conversations it handles.

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

If the same profile runs the AutoLinear Hermes plugin, update it to 1.5.0 or later. Each request still gets its own Linear issue and channel, and Hermes hands it to Muse there instead of AutoLinear’s Codex handoff. Replies in a handoff’s thread stay in that thread. Keep the separate AutoLinear Slack app’s message event subscriptions paused: its Worker can’t tell such a reply from a new request outside an issue channel, so it would move the reply before Hermes sees it.

For an existing installation, run `hermes plugins update hermes22muse` after setting the key. Restart the gateway and look for `hermes22muse: Muse handoff relay registered`. Ask Hermes for something harmless in a channel it answers, check that the thread shows Hermes’s handoff and Muse’s reply under their respective names, and check that the answer arrives in the channel. For a DM request, check the separate conversation with Muse. Leave the gateway running for ongoing sync.

## Delivery and recovery

The SQLite database in `$HERMES_HOME/hermes22muse/relay.db` stores each handoff, the side chat's transcript, and pending deliveries in both directions with their receipts. The relay recognizes its own Muse echoes by their receipts, retries outages with backoff, and finds a Muse send whose receipt was lost by its exact text, unless it has already fallen outside the readable history. Messages carry no delivery marker. muse-mcp stamps every send with its provenance (`⟦via muse-mcp · from …⟧`); the [Muse Gadget launcher](../muse-gadget/README.md) on Hermes Cloud removes that stamp so Muse sees only the user's words. If the Muse bot cannot post to Slack, the relay keeps those posts queued and Hermes can still work with Muse. Check the token and channel membership when the log reports a Slack delivery retry. If Slack accepted a thread post before its receipt could be stored, the post can repeat.

A Muse reply counts only once its row has a final status such as `completed`: while Muse streams, the row has no status and holds only the text so far. If Muse later changes a stored message, the transcript follows, and a reply first stored cut off is posted again in full as "Muse (full reply)". Muse counts as finished once its newest message is a completed reply and nothing has changed for eight seconds, the rule muse-mcp's `muse_chat_ask` uses, because agentic work can continue after a first answer. Each tool call waits up to 330 seconds, inside Hermes's default 420-second tool limit. The relay reads the side chat every five seconds while a handoff is in use, every minute for its first hour, every ten minutes for its first day, and then stops. The Link exposes the latest 50 events, and the relay posts a notice if a full window loses overlap. Oversized text is shortened with a link to the Slack thread.

Handoffs from 1.x keep their top-level threads, and messages they already relayed are not posted again. Hermes 2.0 no longer takes requests at gateway admission, so the user's replies in a handoff thread reach Hermes, which passes them on with `muse_reply`.

From the repository root, build the policy with `vp -C apps/hermes-plugin-hermes22muse pack`. Then run `uv run --no-project --python 3.14 --with pythonmonkey==1.5.1 --with cryptography --with pytest python -m pytest --rootdir=. --import-mode=importlib apps/hermes-plugin-hermes22muse/__tests__`.

## Muse projects evaluated

- [niclydon/muse-mcp](https://github.com/niclydon/muse-mcp) exposes classified chat sends and scoped transcript reads over authenticated MCP. It uses Meta’s paired Gadget transport and needs neither a desktop browser session nor a Muse worker for these operations.
- [finik/pi-muse-bridge](https://github.com/finik/pi-muse-bridge) uses the same Gadget transport and maps Pi sessions to separate Muse chats. Its integration is specific to Pi.
- [99apps-id/muse-relay-mcp](https://github.com/99apps-id/muse-relay-mcp) uses a file relay and a Muse-side polling worker to run questions and retrieve answers. The relay here needs transcript reads for messages entered directly into a side chat.
- [nikships/muse-cli](https://github.com/nikships/muse-cli) and its TypeScript port [OpenSWE/muse-bot](https://github.com/OpenSWE/muse-bot) call the Muse app's own gateway with a muse.ai login. Their protocol notes are the basis of [`museweb.py`](museweb.py), which keeps to the libraries Hermes already has. The gateway is unversioned: when a call starts failing, compare it with the current app.
