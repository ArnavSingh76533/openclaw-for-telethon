# Telethon transport

This extension keeps the existing OpenClaw channel plugin, commands, access policies, approvals, streaming delivery, Mini App, assets, and web routes. Python + Telethon 1.45.0 perform Telegram requests and receive MTProto updates. The TypeScript grammY dependency remains for the existing middleware, types, and request serialization; its network requests are routed to the authenticated local Python worker.

## Install

Use the Node and pnpm versions required by the repository. From the OpenClaw root:

```powershell
python -m venv .venv-telethon
.venv-telethon/Scripts/python.exe -m pip install ./extensions/telegram/python
$env:OPENCLAW_TELETHON_PYTHON = (Resolve-Path .venv-telethon/Scripts/python.exe).Path
$env:TELEGRAM_API_ID = '<your Telegram API ID>'
$env:TELEGRAM_API_HASH = '<your Telegram API hash>'
# Keep the existing OpenClaw botToken/tokenFile/TELEGRAM_BOT_TOKEN configuration.
pnpm openclaw channels status --probe
```

Linux/macOS use `python3 -m venv .venv-telethon` and `.venv-telethon/bin/python`. Export the same three environment variables. The bot token stays in its existing OpenClaw configuration or environment. Do not put real credentials in the source tree. API_ID/API_HASH environment aliases are also accepted.

Run OpenClaw with these variables in its actual service environment. The worker starts on demand and exits after its owning Gateway process exits. The installation does not change your existing Gateway, agent, model, pairing approvals, group allowlists, or web hosting settings.

## Transport behavior

- One locked Telethon SQLite session and persistent receipt queue per bot token under the OpenClaw state directory's `telegram/telethon/` folder. These files contain authorization keys and message receipts; protect and back them up with OpenClaw state.
- The local service binds only to 127.0.0.1. Callers discover it through authenticated health checks across a deterministic sequence of local ports, skipping occupied ports and Windows reservations. Every request authenticates with a derived secret. Credentials enter over stdin, not process arguments or local request URLs.
- Incoming raw TL batches commit to a FULL-sync SQLite journal before Telethon can save its advanced update cursor. Conversion and normalized inbox publication commit before raw retirement. Polling only confirms receipts after the existing OpenClaw spool commits. Stable update IDs and event tombstones survive restart.
- The durability hook relies on the audited ordering in Telethon 1.45.0. Upgrade the pinned version only after reviewing that ordering and rerunning the journal/cursor tests.
- The retained polling worker now polls the local durable inbox; it does not call Telegram getUpdates. Webhook mode forwards the same Telethon inbox to the retained authenticated webhook receiver and confirms only after a successful HTTP response.
- Existing HTTP apiRoot and HTTP DNS/fallback settings remain in configuration for host contract compatibility; Telegram communication uses MTProto data centers. Use the MTProto proxy setting/environment rather than an HTTP Bot API server to route Telegram traffic.
- Existing `channels.telegram.proxy` URLs are passed to Telethon. `OPENCLAW_TELETHON_PROXY` provides an environment override when no account proxy is set (SOCKS4/SOCKS5/HTTP CONNECT). TLS interception settings for the old Bot API transport do not configure MTProto.
- Native rich messages, buttons, callbacks, replies, forum topics, channel direct-message topics, polls, and business requests use their raw MTProto equivalents. Mini App browser code and HMAC/launch-ticket checks remain unchanged.
- Old v2 and current v4 Bot API file IDs are decoded. New file IDs preserve file references. When Telegram explicitly rejects an expired reference, the worker refreshes it from the recorded source message or sticker set and retries once with the same request identity. A standalone old ID whose source was never observed may require re-observing/resending the attachment. No automatic send retry is used after uncertain IPC settlement.
- Unknown fields and unsupported combinations fail explicitly. They never return a fake success or silently send to a different topic.

## Checks

```powershell
.venv-telethon/Scripts/python.exe -m unittest discover -s extensions/telegram/python/tests -v
pnpm test extensions/telegram/src/telethon-transport.test.ts --maxWorkers=1
pnpm test extensions/telegram/src/miniapp --maxWorkers=1
```

The Python suite exercises raw TL request construction and serialization, media and rich content, authenticated local HTTP, durable restart replay, commit/ack ordering, webhook retry, and parent/session ownership. The host test drives the existing grammY serialization into the bridge and verifies payload bytes and authority checks. These fixtures do not prove Telegram server permissions, real user conversations, every live media type, or model/Gateway integration. Consult the accompanying test report for actual runs and remaining gaps.
