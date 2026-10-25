# Install the Telethon Telegram extension

Base repository: `akborana3/openclaw`, commit `a56864b779b25031096d07a126f34810c8736af2`.

The patch changes only `extensions/telegram`. The complete extension archive also includes its unchanged OpenClaw integrations and Mini App code. Node remains the OpenClaw host; Python and Telethon own Telegram networking, MTProto requests and event receipt. This is an extension migration, not a separate chatbot.

## Apply

Use a clean checkout of the base commit, or back up your existing changes before applying the patch:

```powershell
git apply --check /path/to/telegram-telethon.patch
git apply /path/to/telegram-telethon.patch
```

Alternatively, extract the archive into a separate directory and copy its complete `extensions/telegram` folder into the corresponding OpenClaw checkout. If copying over the original folder, also remove the obsolete src/fetch.keepalive.test.ts file. The patch applies that removal explicitly. Do not replace your OpenClaw configuration, agent workspace, or state directory.

## Python and environment

Use the Node/pnpm versions required by this OpenClaw commit. From the repository root:

```powershell
python -m venv .venv-telethon
.venv-telethon/Scripts/python.exe -m pip install ./extensions/telegram/python
$env:OPENCLAW_TELETHON_PYTHON = (Resolve-Path .venv-telethon/Scripts/python.exe).Path
$env:TELEGRAM_API_ID = '<your API ID>'
$env:TELEGRAM_API_HASH = '<your API hash>'
# Existing botToken/tokenFile configuration works, or set:
$env:TELEGRAM_BOT_TOKEN = '<your bot token>'
pnpm install --frozen-lockfile
pnpm build
pnpm openclaw channels status --probe
```

Python 3.9 or later is required. On Linux/macOS, use `python3` and `.venv-telethon/bin/python`, and export the same environment variables. Configure these variables in the actual Gateway service environment, then start the Gateway using your existing procedure.

The archive also includes `wheels/openclaw_telethon-0.1.0-py3-none-any.whl`. You can install that wheel instead of building `./extensions/telegram/python`; use its full extracted path with the virtual environment's `python -m pip install` command. Runtime dependencies still need to be installed. The wheel was validated with Python 3.9 and the same 74-test suite.

Keep the existing Telegram pairing, sender/group allowlists, agent/model, streaming, webhook, and Mini App hosting configuration. The supplied test group was used only in isolated validation state; this package does not change your operational allowlists or model settings. An HTTP Bot API `apiRoot` is retained as a logical client setting; Telegram traffic now uses MTProto data centers. Existing proxy URLs support SOCKS4/SOCKS5 or HTTP CONNECT.

When replacing a deployment that has a cloud Bot API webhook, retire that old cloud registration through the previous deployment before starting the MTProto receiver. The new `setWebhook`/`deleteWebhook` operations control the local Telethon forwarding subscription. See [Telegram's webhook administration methods](https://core.telegram.org/bots/api#deletewebhook). The supplied bot had no active cloud webhook during validation.

## State and recovery

The worker creates a token-specific Telethon session, raw update journal and receipt inbox under the OpenClaw state directory's `telegram/telethon` directory. These databases contain authorization material and message receipts. Keep them private and include them in state backups. One process locks each session; the worker shuts down when its owning host exits.

Known-source expired media references refresh automatically after a definite server rejection. Old standalone file IDs without an observed source may require the attachment to be sent again. Poll result updates without an observed question/options definition remain durably deferred and appear in worker health until that definition is observed. Unsupported combinations fail explicitly.

## Validate

```powershell
.venv-telethon/Scripts/python.exe -m unittest discover -s extensions/telegram/python/tests -v
pnpm test extensions/telegram/src/telethon-transport.test.ts extensions/telegram/src/fetch.test.ts extensions/telegram/src/api-fetch.test.ts extensions/telegram/src/audit.test.ts --maxWorkers=1
pnpm test extensions/telegram/src/miniapp --maxWorkers=1
```

Read `TEST_REPORT.md` for the actual validation results and gaps. No credentials, Python virtual environment, authorization sessions, or test-state databases are included in the deliverables.
