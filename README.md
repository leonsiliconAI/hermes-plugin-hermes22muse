# hermes22muse

Every ordinary Slack request from the configured Hermes user gets its own Meta Muse side chat. Hermes posts a new top-level handoff message in the same Slack conversation with links to the original request and the side chat. Reply under the handoff message to continue in that Muse chat. Muse replies and messages you type directly in that side chat appear under the same Slack handoff. A request outside a linked thread starts a fresh handoff, even when it was sent in another Slack thread.

The gateway consumes these requests after its normal Slack routing and authorization checks. Hermes commands and other users’ messages follow their existing gateway paths. In channels configured to require a mention on every thread reply, mention Hermes in your follow-ups. Slack attachments can contribute any text the adapter supplies. File bytes, edits and deletions do not sync. Both endpoints should be treated as visible to everyone with access to the linked Slack conversation.

## Set up Muse

Pair the [official Muse Gadget SDK Linux client](https://github.com/facebookincubator/muse-gadget-sdk/tree/fbfe3b0f717f7f468d0a94aa370ea0ca1a94ebfe/linux) on a Linux host with Bluetooth LE. Get an [SDK token](https://gadgets.muse.ai/settings/sdk-tokens), approve pairing in the Muse phone app, then check `sudo musegadget doctor` on the host.

Install [niclydon/muse-mcp](https://github.com/niclydon/muse-mcp) at `43299b2f4270e8994eff3978a72d1345abf93e4d` on the paired host. Follow its install instructions, using a service account that can read the SDK’s pairing state. A dedicated instance can start with `[]` in `directory.json`. This plugin registers each fresh side-chat ID in that directory and the first Link send opens it. The Muse-side bridge worker is only needed for other lifecycle operations such as archive or rename.

Run the server with `TRANSPORT=http` and its `ORIGIN_SHARED_SECRET` set to a random secret of at least 32 characters. Keep the endpoint private. Use loopback HTTP if it runs beside Hermes or is forwarded over SSH, or use HTTPS through a private network if it runs on another host. Set the server’s allowed hosts as described in its README when using a private hostname.

## Set up Hermes

Use a Hermes gateway with the native Slack adapter and the `post_gateway_admission` hook (available in v0.23.2). The Hermes installation needs its MCP 2.x Python dependencies. The Slack app needs `chat:write` and the history scopes for the conversations it handles. History access lets the gateway find its own handoff after a lost Slack receipt.

Set these values in the Hermes profile’s environment and install the plugin. On Hermes Cloud, use the profile’s Keys page.

| Key                          | Value                                                                            |
| ---------------------------- | -------------------------------------------------------------------------------- |
| `HERMES22MUSE_SLACK_USER_ID` | Slack user ID whose requests and follow-ups should go to the paired Muse account |
| `HERMES22MUSE_MCP_URL`       | Private `https://…/mcp` or `http://127.0.0.1:7899/mcp`                           |
| `HERMES22MUSE_MCP_SECRET`    | The paired service’s `ORIGIN_SHARED_SECRET`                                      |

```sh
hermes plugins install leonsiliconAI/hermes-plugin-hermes22muse --enable
```

If the same profile runs the AutoLinear Hermes plugin, update it to 1.4.6 or later. Its gateway intake yields to hermes22muse for the configured user. If the separate AutoLinear Slack app also subscribes to that user’s messages for fast intake, pause those subscriptions before enabling this integration so the Worker does not move or edit an original request ahead of Hermes.

Restart the gateway and look for `hermes22muse: Slack handoff relay registered`. Send a harmless text request in the Hermes Slack DM, open the side chat from Hermes’s handoff, and send a reply from both Slack and Muse. Leave the gateway running for ongoing sync.

## Delivery and recovery

The SQLite database in `$HERMES_HOME/hermes22muse/relay.db` stores the handoff, pending text and delivery receipts. Slack event IDs deduplicate requests and replies. The relay recognizes its own Muse echoes, retries outages with backoff, and links a recovered Slack receipt to the same Muse chat. If Slack accepted a thread reply before its receipt could be stored, Slack delivery can repeat. A Muse send with a lost receipt can repeat if its delivery marker has already fallen outside the readable history.

Muse history is polled about every five seconds while active and every minute when idle, subject to worker load, delivery retries and transport latency. The Link exposes the latest 50 events. The relay posts a notice if a full window loses overlap. Oversized Slack text is shortened with a link to the original Slack message.

Run the relay tests from the repository root with Python 3.11 or newer and `python3 -m unittest discover -s apps/hermes-plugin-hermes22muse/__tests__`.

## Muse projects evaluated

- [niclydon/muse-mcp](https://github.com/niclydon/muse-mcp) exposes classified chat sends and scoped transcript reads over authenticated MCP. It uses Meta’s paired Gadget transport and needs neither a desktop browser session nor a Muse worker for these operations.
- [finik/pi-muse-bridge](https://github.com/finik/pi-muse-bridge) uses the same Gadget transport and maps Pi sessions to separate Muse chats. Its integration is specific to Pi.
- [99apps-id/muse-relay-mcp](https://github.com/99apps-id/muse-relay-mcp) uses a file relay and a Muse-side polling worker to run questions and retrieve answers. The relay here needs transcript reads for messages entered directly into a side chat.
- [OpenSWE/muse-bot](https://github.com/OpenSWE/muse-bot) exposes side-chat commands through Muse’s internal web gateway. Its documentation describes that gateway as unversioned.
