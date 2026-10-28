# Antigravity CLI models for OpenClaw

An optional, separate OpenClaw provider that delegates Google login and inference to Google's official `agy` CLI. Your existing providers, default model, Telegram transport, and browser are unchanged.

**Google's current Antigravity terms explicitly prohibit third-party OAuth use, including OpenClaw, and warn of suspension or termination. This adapter is opt-in and is not endorsed by Google or Nous Research.** Read https://antigravity.google/terms before connecting. A Gemini subscription does not guarantee access to any particular AGY model or unlimited requests.

## Origin

The approach comes from the community `antigravity-oauth` entry in the official [Hermes catalog](https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/antigravity-oauth.yaml), which points to [neerazz/hermes-antigravity-oauth](https://github.com/neerazz/hermes-antigravity-oauth). The catalog reference inspected was `466fafb9b6e8099735d79ed32f97005a477ba144`. This is a small independent OpenClaw adapter, not a copy of Hermes's Python runtime. No private OAuth endpoint, copied client identity, or token export is used.

## Install and connect

Requires OpenClaw 2026.10.6 or later, Node 24, and the [official Antigravity CLI](https://antigravity.google/docs/getting-started?tab=cli). The tested CLI is 1.4.8.

1. Install `agy` from Google's official instructions on the machine running the Gateway.
2. Run `openclaw plugins install /path/to/openclaw-agy-0.1.1.tgz` and enable `antigravity-cli` if prompted. Restart the Gateway.
3. Open **Settings → Models → Connect provider → Antigravity CLI Google sign-in**. Review the notice, copy the official Google sign-in URL into a new browser tab, complete consent yourself, and return to the visible masked OpenClaw field to paste Google's authorization code. AGY expires its login attempt after 60 seconds; retry if needed. This is manual code entry; it does not wait for an automatic OpenClaw browser callback.
4. Choose an exact `antigravity-cli/<model-id>` from Models. Setup does not change your existing default model. You can select a model for one conversation first.

The provider uses its own HOME at `$OPENCLAW_STATE_DIR/agy-home` (normally `~/.openclaw/agy-home`). AGY owns its credentials there; include this directory in private OpenClaw backups. Do not publish it. An existing Gmail browser login is separate from CLI authentication.

For an ephemeral Docker host such as Hugging Face, put the plugin source in the image and install it as a local link. OpenClaw's native backups deliberately omit managed npm directories, so an npm-pack install alone will not survive a fresh container restore. The Space uses `/home/node/agy-plugin` with its `node_modules/openclaw` link pointing to `/opt/openclaw`; its managed install record and load path are retained in the private state backup. The CLI executable and CLI-owned Google HOME live inside the backed-up state.

Optional plugin configuration in `plugins.entries.antigravity-cli.config`:

```json
{
  "command": "/usr/local/bin/agy",
  "homeDir": "/home/node/.openclaw/agy-home",
  "timeoutSeconds": 300
}
```

`command` is a single executable path, never a shell command. The plugin does not read your ordinary `~/.gemini` credentials or modify your ordinary AGY settings. To log out, run the CLI with this same HOME and use its official `/logout` command. Disable the plugin to stop OpenClaw using it.

## Behavior and limits

- Model IDs and labels come from `agy models`; no invented catalog or silent fallback. Effort suffixes are preserved verbatim. Token limits are conservative defaults (32,768 context, 8,192 output), because the CLI catalog does not publish them.
- Each completion sends the complete OpenClaw text conversation, system prompt, and currently allowed tool schemas to a fresh CLI conversation. OpenClaw remains responsible for compaction, tool execution, and approvals.
- CLI native file, command, web, and MCP operations are denied in a private provider-owned settings file. Requests use a disposable working directory, not your agent's workspace. A native tool event aborts the response.
- OpenClaw tool calls use AGY structured output, validate against the actual tool schema, and return through OpenClaw's normal tool loop. There is no additional tool executor.
- Text only. Attachments produce an explicit error; images, audio, and video are not silently dropped. Select another provider for those requests.
- Replies appear when the structured result completes; token-by-token streaming and separate reasoning controls are not implemented. Subscription usage tokens are reported when the CLI supplies them; zero monetary cost fields mean pricing is unknown, not that Google usage is free.
- Authentication, model access, quota errors, malformed output, cancellation, and timeouts are surfaced. Neither this plugin nor the Hermes plugin can guarantee Google's account eligibility or subscription terms.

## Tests

`npm test` runs parser, environment, permission settings, login, and real subprocess fixture checks. On Windows, the Linux subprocess cases are skipped; run the suite on Linux for full coverage. SDK contract tests additionally require an OpenClaw source tree:

```sh
export OPENCLAW_SOURCE_ROOT=/path/to/openclaw
node --import "$OPENCLAW_SOURCE_ROOT/scripts/tsx.mjs" --import ./test/sdk-loader.mjs -e "import('./test/sdk-runtime.test.mjs')"
```

Fixture tests do not prove live Google inference. A real account login and a live OpenClaw tool round trip must be verified separately.
