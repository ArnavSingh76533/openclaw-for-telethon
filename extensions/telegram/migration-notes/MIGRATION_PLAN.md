# Telegram → Python Telethon migration plan

Source: akborana3/openclaw main at a56864b779b25031096d07a126f34810c8736af2.

## Required outcome
Python and Telethon own Telegram communication and event receipt. Preserve the existing OpenClaw plugin contracts, security/pairing/allowlists, durable ingress/drain, command routing, approvals, streaming, media, topics, polls, business actions, and Mini App/web UI. Unrelated code remains unchanged. Credentials never enter deliverables.

## Architecture
The host and its plugin contracts are TypeScript. Retain that integration and existing policy code. A Python service translates its Telegram-shaped requests to Telethon high-level or raw MTProto calls and translates Telethon updates back to the existing ingress contract. The existing grammY dependency can remain solely for host-side middleware, types, and serialization; it must not communicate directly with Telegram. This is not a standalone replacement chatbot.

## Ordered changes
1. Audit every request, dynamic method, direct HTTP call and event type. Save a complete feature/method checklist before cutover.
2. Add `extensions/telegram/python/pyproject.toml` and `openclaw_telethon/` modules for configuration, service lifetime, local authenticated IPC, Telethon session ownership, MTProto method dispatch, event/media/entity/button codecs, and durable update handoff.
3. Add `extensions/telegram/src/telethon-transport.ts` for Python process startup, authenticated local requests, uploads/downloads, abort propagation, and failure reporting.
4. Change the shared network owner `src/fetch.ts` plus any audited bypasses so bot clients, probes, raw calls, ingress workers and downloads all reach Python. Keep request authority and bot-token throttling at their existing owner.
5. Preserve `assets/`, Mini App routes/auth/HMAC, command/approval dispatch, native callbacks and web UI contracts. Map web_app buttons and menu actions through MTProto where available.
6. Adapt ingress polling/webhook semantics to Telethon's durable event queue, retaining enqueue-before-confirm and restart replay. Keep topic identifiers distinct.
7. Add focused Python and host integration tests, method-inventory coverage, restart/ack recovery, and failure cases. Run existing affected tests when dependency/runtime availability permits.
8. Perform live bot authentication/probe with supplied credentials held outside source. Full user-message E2E requires a test user/chat and a configured OpenClaw model. Report precisely what was exercised and what remains unproven.
9. Package changed extension/source patch, setup instructions, feature coverage and test report under outputs.

## Completion gates
- No unaccounted Telegram networking bypass.
- Each used API method and incoming event has an implemented mapping, retained integration, or explicit documented limitation; no silent success for unsupported operations.
- No unrelated modifications and no credentials in the patch/archive.
- Unit/contract tests plus available integration/live tests pass; unavailable proof is reported as a gap, never represented as passed.

## Completion status
- All planned implementation and packaging steps are complete; 53 changed files are confined to the Telegram extension.
- Python source and fresh installed wheel: 74/74 tests each. Runtime build/bootstrap, production TypeScript checking, typed lint, formatting and built-worker asset equality passed.
- Actual Telegram service/host/model delivery, real user-to-Gateway-to-model replies, native commands/callback recovery, document roundtrip, mentions and pin/unpin passed in isolated validation state.
- The complete retained test inventory was attempted through 17 bounded batches plus targeted final fixture checks. The full suite has unresolved failures; see TEST_REPORT.md for original logs, exact outcomes, Windows fixture issues, timeouts and proof limits.
- Mini App browser/routes/authentication and existing OpenClaw integrations remain in the full extension archive. Credentials and session/state databases are excluded. Temporary live fixtures were removed, original command scopes restored and owned test processes stopped.
