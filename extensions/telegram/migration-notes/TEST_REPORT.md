# Telegram Telethon migration: final validation report

Base: `akborana3/openclaw` at `a56864b779b25031096d07a126f34810c8736af2`.
Host: Windows; Node 24.22.1; pnpm 12.15.0; Python 3.9.13; Telethon 1.45.0.

## Delivered scope

The extension migration and its validation runs are finished. The full retained suite is **not entirely green**; exact failures and proof limits are recorded below. All 53 changed files are inside `extensions/telegram`. No unrelated source or operator configuration was changed.

Python and Telethon own Telegram communication, MTProto requests and event receipt. The existing TypeScript OpenClaw host and grammY middleware/types/serialization remain integrated with the original account policies, pairing, commands, approvals, streaming, durable spool/drain and Mini App browser/routes/authentication. This is the original extension with a migrated Telegram transport.

The local authenticated adapter serves requests, probes, receipt polling and file downloads. Raw updates commit before Telethon cursor persistence; receipt confirmation follows the host spool commit or successful retained webhook delivery. The worker is packaged as source and a checked wheel. Credentials, sessions, databases, environments and private test state are excluded.

## Results

| Check | Measured result | Scope |
| --- | --- | --- |
| Final Python source suite | 74 passed | TL request serialization, codecs, UTF-16 quotes, mentions, business routing/callbacks, topics, scopes, stale-reference recovery, local HTTP, ownership, parent exit, restart and journal/cursor/ack ordering. |
| Fresh installed wheel | Same 74 passed | Import origin asserted inside the installed wheel. This repeats the same contracts for packaging validation. |
| Runtime build and bootstrap | Passed | Selected Telegram/OpenAI qaRuntime build, CLI bootstrap and built-plugin checks. Latest native-batch preflight also completed successfully. |
| Shipped Python assets | 7/7 match source | Six Python modules plus pyproject.toml; checked against the built runtime. |
| Targeted production TypeScript check | Passed | Four changed runtime modules and transport/fetch/lookup tests using canonical SDK declarations. |
| Typed lint and formatting | Passed | Earlier 32-file check plus final eight-file fixture check; includes all final TypeScript edits. `git diff --check` passed. |
| Resumed canonical batches | 1628 passed; 10 failed; 2 existing skipped | 17 completed batches; the inventory covers 161 ordinary test files and 3 additional local E2E files, with 13 complete files reused from the earlier run. Counts below are per execution, not unique independent contracts. |
| Final custom fixture validation | 10 passed; 0 failed | Entire disabled-bindings, model-callback loopback and ingress-worker lifetime files after fixture repairs. |
| Final model fixture import correction | 2 passed | Same complete callback file after removing a duplicate static import found by lint; early mock initialization preserved. |
| Bounded native-routing reproduction | 0 passed; 2 failed | Two previously timed-out cases selected once. A passing replay does not explain or erase the earlier timeout. |
| Complete original lifecycle file | 59 passed | Scoped original-owner control, normal file order. |
| Complete migrated lifecycle file | 59 passed | Same unchanged assertions in normal file order. Three earlier broad-run failures did not reproduce; they are not claimed fixed. |
| Scoped original-owner topic control | 5 passed; 1 timed out | Private-topic first-mode timeout also occurred with the selected original transport/media fixture owners restored. This is a scoped control, not a clean-repository full-suite baseline. |

## Real Telegram and model tests

These used the supplied test bot/group and isolated OpenClaw state with the supplied `gpt-4o-mini-2024-07-18` model:

- Python service authentication/chat lookup, actual model request, text send/edit, reaction, document upload/getFile/exact-byte download, poll and rich message: passed.
- Existing OpenClaw host/client wrappers and grammY serialization through Python: HTML/buttons/edit, document caption `123`, exact bytes and deletion passed.
- Built OpenClaw CLI `agent --local --deliver`: actual agent/model returned `OPENCLAW_FULL_AGENT_TELETHON_OK` and delivered successfully.
- Real signed-in user → Gateway → durable spool → actual model → Telegram: observed `OPENCLAW_TELETHON_INBOUND_OK_7429`; recorded 3,464 model tokens and enqueue-before-offset ordering.
- Original `/think` menu, real Off-button callback and fresh callback: displayed `Thinking disabled.` successfully. A previously failed retained callback recovered after the isolated Gateway restarted with the codec repair.
- Native text mention accepted by Telegram. Independent inline keyboard removal passed.
- Live no-ID unpin removed the newest test pin, retained the earlier test pin, and restored/preserved the pre-existing pins without bot-forbidden history/search calls.
- Compiled ingress worker authenticated and completed its durable-inbox readiness poll.

All four captured bot command scopes were restored and reread; two needed changes. Owned test Gateways/Python services were stopped. Temporary media, polls, pin and mention fixtures were removed; proof conversation replies remain. Operator configuration was untouched.

## Primary batch record

| Batch | Files | Passed | Failed | Log in archive |
| --- | --- | --- | --- | --- |
| 1 | 1 | 22 | 0 | `validation/remaining-telegram-chunk-01-attempt-02.log` |
| 2 | 1 | 44 | 0 | `validation/remaining-telegram-chunk-02-attempt-02.log` |
| 3 | 5 | 70 | 0 | `validation/remaining-telegram-chunk-03-attempt-03.log` |
| 4 | 5 | 45 | 0 | `validation/remaining-telegram-chunk-04-attempt-03.log` |
| 5 | 5 | 36 | 3 | `validation/remaining-telegram-chunk-05.log` |
| 6 | 5 | 46 | 4 | `validation/remaining-telegram-chunk-06.log` |
| 7 | 5 | 44 | 1 | `validation/remaining-telegram-chunk-07.log` |
| 8 | 5 | 55 | 0 | `validation/remaining-telegram-chunk-08.log` |
| 9 | 5 | 39 | 0 | `validation/remaining-telegram-chunk-09.log` |
| 10 | 20 | 183 | 0 | `validation/remaining-telegram-chunk-10.log` |
| 11 | 20 | 155 | 0 | `validation/remaining-telegram-chunk-11.log` |
| 12 | 20 | 303 | 0 | `validation/remaining-telegram-chunk-12.log` |
| 13 | 20 | 145 | 0 | `validation/remaining-telegram-chunk-13-attempt-03.log` |
| 14 | 20 | 336 | 0 | `validation/remaining-telegram-chunk-14-attempt-03.log` |
| 15 | 11 | 66 | 2 | `validation/remaining-telegram-chunk-15-attempt-02.log` |
| 16 | 2 | 17 | 0 | `validation/remaining-telegram-chunk-16-attempt-02.log` |
| 17 | 1 | 22 | 0 | `validation/remaining-telegram-chunk-17.log` |

Primary failures are retained even when a later targeted fixture repair passes:

- × |extension-database-workers| extensions/telegram/src/bot-native-command-dispatch.routing.test.ts > registered native command routing through the message pipeline > preserves the canonical export command with a colliding workspace skill 198551ms
- × |extension-database-workers| extensions/telegram/src/bot-native-command-dispatch.routing.test.ts > registered native command routing through the message pipeline > publishes account-scoped skills and keeps omitted bound skills callable 120063ms
- × |extension-database-workers| extensions/telegram/src/bot.create-telegram-bot.disabled-bindings.test.ts > answers an ordinary inbound message with account bindings disabled without persisting bindings 17152ms
- × |extension-database-workers| extensions/telegram/src/message-dispatch-dedupe.test.ts > Telegram message dispatch replay guard > isolates identical message coordinates across bot identities 3618ms
- × |extension-database-workers| extensions/telegram/src/message-dispatch-dedupe.test.ts > Telegram message dispatch replay guard > starts a fresh dedupe window when legacy rows lack bot identity 316ms
- × |extension-database-workers| extensions/telegram/src/message-dispatch-dedupe.test.ts > Telegram message dispatch replay guard > uses one persisted namespace across Telegram accounts 337ms
- × |extension-database-workers| extensions/telegram/src/bot.media.stickers-and-fragments.test.ts > registered Telegram media > reads a trusted Bot API host mount but refuses a readable file outside that root 25ms
- × |extension-database-workers| extensions/telegram/src/model-callback.loopback.integration.test.ts > Telegram model callback loopback > sends, authorizes, resolves, persists, answers, and edits an opaque callback 188ms
- extensions/telegram/src/telegram-ingress-worker.test.ts > stopTelegramIngressWorker > stops within budget (cooperative=true) 11ms
- extensions/telegram/src/telegram-ingress-worker.test.ts > stopTelegramIngressWorker > stops within budget (cooperative=false) 8ms

## Failure diagnosis and remaining limitations

1. **Disabled bindings and model callback fixture setup:** the custom files still targeted the old HTTP boundary. Their real adapter now discovers an owned fake Python listener; assertions were retained. Their complete final-file results are reported above. Production code was unchanged by these fixture corrections.
2. **Probe socket fixture:** moved injected failures from the retired Bot API HTTP endpoint to authenticated Python IPC. Vitest clears suite-level environment stubs before cases, so fixture credentials/proxies are restored per case. All six original backoff/cancellation/body deadline assertions passed after this repair. Earlier interrupted attempts remain in the archive.
3. **Windows dispatch-dedupe cleanup:** three cases failed with EPERM removing temporary directories. No retry/skip or production workaround was added. A clean Linux or separately qualified Windows run is still needed; this report does not call these baseline failures.
4. **Windows sticker host-mount fixture:** one test tries to create a directory named `123456:local-media`, which Windows forbids. The fixture/assertions were preserved. This differs from the independently passed real Windows Telethon document roundtrip.
5. **Native routing timeouts:** the colliding export test hit its 180-second teardown hook again in the bounded reproduction; the account-scoped skill test also reproduced its 120-second case timeout. The final reproduction result is recorded above. Both stalls remain unresolved; no safe production repair was established. These are not classified as baseline failures. No unrelated skill-discovery or teardown changes were made. The bounded command selected these two cases; its 12 filtered cases are not additional skipped coverage because the primary whole-file run already attempted them.
6. **Private-topic first-mode timeout:** reproduced on the migration and scoped original owners; off/all modes passed. It remains unresolved. The original broad run also had three lifecycle failures; complete original and migrated file runs passed 59/59. Their initial discrepancy remains unexplained.
7. **Ingress-worker recorder identity:** two original lifetime cases tried to use the CPU-tracked `worker.postMessage` wrapper as a Vitest spy. The existing core owner wraps this method in `src/infra/worker-cpu.ts`. The Telegram test now retains its underlying `vi.fn` recorder and checks the same stop message, two-second budget and cooperative/forced termination counts. The core and production worker are unchanged; the complete file is included in final supplemental validation.

No assertions were weakened, functional timeouts increased, failures marked expected, or tests skipped to obtain green results. The initial broad run was interrupted after 507 passing and six failing case lines. Later interrupted native/probe attempts and all source-fingerprint changes are preserved in `validation/RESULTS.json` and the associated logs. Completed unrelated files were reused only when their sources/runtime stayed unchanged; fixture-only edits received targeted validation.

The broad production-style extension typecheck still has private test-SDK declaration gaps (including test-env); it is not claimed to pass. The targeted production check passed. Initial compiler/cache preparation failures were resolved using supported preparation budgets; these did not change functional test deadlines.

One automatic runtime-preparation invocation produced no build progress for over ten minutes. Its owned process tree was stopped after checking there were no build descendants and the canonical checkout lock could be acquired/released. The supported `scripts/build-all.mts qaRuntime` command was run directly. A short profiler check found file-open/read/hash work; its diagnostic cleanup then accidentally used an unsupported dynamic import in the inspector context, terminating that build. This was an agent tooling error, not a product verdict. The error log and stale ownership record were preserved, descendants were verified stopped, and only that task-owned lock directory was moved aside before the canonical build was rerun and unfinished test batches resumed. No repository tooling or source was changed for this infrastructure recovery.

The first isolated Gateway run logged a utility-model access error; the actual conversation used the supplied model successfully, and the isolated utility model was then set to that snapshot. Selected qaRuntime also logged an unrelated memory-core standing-intent worker import error; that subsystem is not qualified by this extension test.

The local E2E fixture files used the supported `OPENCLAW_E2E_USE_PREBUILT_DIST=1` mode after the successful unchanged qaRuntime build. A redundant declaration rebuild was stopped before runtime compilation began. All seven Python assets matched source, and compiled Telegram JavaScript was verified to predate that interrupted rebuild; hashes are included in `prebuilt-e2e-runtime-proof.json`. This reuses prepared artifacts; every selected test still runs.

Mini App browser code, routes, HMAC/tickets and native web-app buttons remain integrated. A live public-HTTPS Mini App launch was not qualified. All permission-dependent forum, channel DM, membership and business combinations were not live-tested; request/event fixtures cover their implemented mappings. Unsupported combinations return explicit errors. Expired standalone media IDs without observed origins may need the attachment re-observed. Poll results without an observed definition remain durably deferred and visible in worker health.

## Package checks

The final packager verifies the extension-only patch against the pinned base using a separate Git index, source fingerprints, wheel/source equality, archive CRC and every archived source hash. It scans source, documentation, logs and wheel contents for the supplied private credentials. `ARTIFACTS.json` records the final sizes and SHA-256 values; its successful creation is the packaging verdict.
