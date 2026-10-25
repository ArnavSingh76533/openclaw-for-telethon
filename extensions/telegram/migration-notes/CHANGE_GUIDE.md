# File-by-file migration guide

All paths below are relative to `extensions/telegram`. The patch is against commit `a56864b779b25031096d07a126f34810c8736af2`.

| Step | Files | Purpose |
| --- | --- | --- |
| 1. Install the Python runtime | `python/pyproject.toml`, `python/.gitignore` | Pin Telethon 1.45.0 and declare Python/dependency requirements; exclude caches/build output. |
| 2. Start the worker | `python/openclaw_telethon/__init__.py`, `python/openclaw_telethon/__main__.py` | Define the private IPC protocol and Python module entry point. |
| 3. Own Telegram communication | `python/openclaw_telethon/service.py` | Authenticate the bot through Telethon, own/lock its session, journal MTProto events, serve authenticated localhost requests/downloads, forward retained webhooks, and follow the parent lifetime. |
| 4. Preserve durable receipt behavior | `python/openclaw_telethon/store.py` | Keep raw batches, normalized receipts, acknowledgements, event deduplication, poll definitions and media origins in SQLite. The existing OpenClaw spool/drain still owns channel processing. |
| 5. Preserve Telegram data shapes | `python/openclaw_telethon/codec.py` | Convert peers, messages, media/file IDs, UTF-16 entities/quotes, keyboards, Mini App buttons, rich content, topics, polls, reactions, membership and business updates. |
| 6. Implement requests in MTProto | `python/openclaw_telethon/methods.py` | Map the extension's API operations to native requests, including sends/edits/media/albums, callbacks, commands, topics, polls, reactions, pins, business routing and definite stale-reference recovery. Resolve native text mentions. |
| 7. Connect the existing host | `src/telethon-transport.ts` | Start/discover Python, pass credentials over stdin, authenticate local IPC, preserve serialized JSON/multipart payloads and downloads, propagate cancellation, and check send authority at admission. |
| 8. Cut over shared networking | `src/fetch.ts` | Keep the existing transport contract and token-scoped callers while routing Telegram requests through Python. Remove the old HTTP fallback/replay implementation. |
| 9. Close audited bypasses | `src/api-fetch.ts`, `src/bot/delivery.resolve-media.ts` | Route default chat lookups and fallback Telegram media fetches through the Telethon transport. |
| 10. Ship/install the worker | `package.json`, `README.md`, `TELETHON.md` | Include Python modules in built plugin assets; document setup, state, migration behavior, checks and limitations. |
| 11. Test Python behavior | `python/tests/test_codec.py`, `python/tests/test_methods.py`, `python/tests/test_store.py` | Verify actual TL serialization, contract shapes, local HTTP, state ownership, restart/crash windows, acknowledgements, webhook retry, media refresh and mentions without contacting a real chat. |
| 12. Test the host boundary | `src/telethon-transport.test.ts`, `src/fetch.test.ts` | Exercise real grammY serialization, multipart bytes/captions, local routing, send-authority revocation, proxy selection, cancellation ownership and uncertain-send handling. |
| 13. Retain lookup/audit contracts | `src/api-fetch.test.ts`, `src/audit.test.ts` | Move network mocks to the new transport boundary while retaining body/timeout/disposal checks. Check cancellation lifetime instead of signal-object identity. |
| 14. Remove obsolete HTTP-only proof | `src/fetch.keepalive.test.ts` | The old Undici-pool implementation is gone; its implementation-specific suite is replaced by the new transport ownership/lifetime tests. |
| 15. Preserve retained integration fixtures | `src/telethon-ipc.test-support.ts`, `src/polling-session.test.ts`, `src/send.telegram-http.test-support.ts`, `src/bot-message-dispatch.telegram-http.test-support.ts`, `src/bot.create-telegram-bot.native-pipeline.test-support.ts`, `src/model-callback.loopback.integration.test.ts` | Put the fake server at the Python IPC boundary. The actual host adapter, authenticated discovery, JSON/multipart serialization and original spool acknowledgements still run. Forward injected errors without Node's automatic HTTP 421 retry. Prepare a listener for each native fixture token/state at its owned API root before the independent outbound client starts. Nested fixtures retain their own listener and request recorder. Custom model callback fixtures also own and close their IPC listener. |
| 16. Check new lifetime/recovery semantics | `src/send.transport-close-proof.test.ts`, `src/send.telegram-http.test.ts` | Verify cancellation of evicted/reset callers through real sockets while the shared service remains available; verify uncertain calls are not replayed. |
| 17. Initialize the IPC fixture before consumers | The test files listed below | Add one early fixture import; retain their existing behavior assertions. |
| 18. Preserve diagnostic socket contracts | `src/probe.response-body-timeout.integration.test.ts` | Inject backoff, stalled/trickling JSON and cancellation at an authenticated fake Python IPC socket. Run the real host bridge and retain the original deadline, request-count and cancellation assertions. |
| 19. Preserve worker lifetime proof | `src/telegram-ingress-worker.test.ts` | Keep the underlying message spy when existing CPU tracking wraps the worker method. Preserve stop-message, two-second budget and forced-termination assertions. |

Step 17 applies to these files under `src/` (the two step-16 files additionally contain the changes described there):

- `action-progress-edit.telegram-http.test.ts`
- `action-runtime.test.ts`
- `bot-message-dispatch.preview.telegram-http.test.ts`
- `bot-message-dispatch.progress-lifecycle.telegram-http.test.ts`
- `bot-message-dispatch.recovery.telegram-http.test.ts`
- `bot-message-dispatch.reply-targets.telegram-http.test.ts`
- `bot-message-context.bot-threads.test.ts`
- `bot.create-telegram-bot.disabled-bindings.test.ts`
- `bot/delivery.paragraphs.test.ts`
- `bot/delivery.resolve-media-local.test.ts`
- `bot-native-command-dispatch.delivery.test.ts`
- `outbound-adapter.telegram-http.test.ts`
- `question-finalization.test.ts`
- `reply-scaffolding.telegram-http.test.ts`
- `send.caption-checkbox.telegram-http.test.ts`
- `send.history.test.ts`
- `send.recovery.telegram-http.test.ts`
- `send.telegram-http.test.ts`
- `send.transport-close-proof.test.ts`
- `telegram-ingress-callback.integration.test.ts`
- `transport-payload.test.ts`

`src/bot.media.e2e.test-harness.ts` moves its existing media-response spy from Undici to the Telethon transport factory. Media/context assertions remain in their original owner.

The existing Mini App assets and routes, HMAC/launch-ticket checks, commands, approvals, account policy, streaming delivery, host ingress spool/drain and other extension files remain part of the full extension archive. They were not replaced with a separate Python chatbot. JavaScript grammY remains as host middleware/types/request serialization; Python Telethon performs runtime Telegram communication.

See `TEST_REPORT.md` for measured results and remaining proof gaps, and `SETUP.md` for installation.
