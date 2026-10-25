# Telegram migration coverage

Base: `a56864b779b25031096d07a126f34810c8736af2`. This is an implementation inventory; measured results and proof limits are in `TEST_REPORT.md`.

## Communication owners

| Existing entry | Migrated owner |
| --- | --- |
| Bot creation, outbound clients, probes and token checks | Existing client wrappers → `fetch.ts` → authenticated Python IPC → Telethon. |
| Direct chat lookups | `api-fetch.ts` defaults to the same transport. |
| Telegram media retrieval | Existing media checks/storage → adapter download → Telethon. |
| Ingress worker `getUpdates` contract | Local durable receipt inbox; Telegram updates arrive through MTProto. |
| Webhook subscription contract | Local Telethon inbox forwarding to the retained authenticated receiver; acknowledge after receiver success. |
| Mini App browser requests | Existing browser/web routes, auth and assets remain in place. |

## Request mappings

| API-shaped operation retained by the host | Python / raw MTProto implementation |
| --- | --- |
| `getMe`, `getChat`, `getChatMember`, `getUserProfilePhotos` | Telethon identity/entity helpers, full-peer metadata, participant permissions and `photos.getUserPhotos`. |
| `sendMessage`, `sendRichMessage` | `messages.sendMessage` or preview-media send; native entities/rich content, replies, topics and markup. |
| `sendPhoto`, `sendAnimation`, `sendVideo`, `sendVideoNote`, `sendVoice`, `sendAudio`, `sendDocument`, `sendSticker` | Upload/file-ID/external-media conversion and `messages.sendMedia`. |
| `sendMediaGroup` | Per-item media preparation/upload and `messages.sendMultiMedia`, preserving captions and topic/reply context. |
| `getFile` and file download | Version 2/4 file-ID decoding and Telethon download. Known source references can refresh after a definite expired-reference rejection. |
| `editMessageText`, `editMessageCaption`, `editMessageReplyMarkup` | `messages.editMessage` / `messages.editInlineBotMessage`; inline messages on another DC use the exported sender. |
| `deleteMessage`, `deleteBusinessMessages` | Telethon deletion or business-wrapped raw deletion. |
| `answerCallbackQuery`, `sendChatAction` | `messages.setBotCallbackAnswer`, `messages.setTyping`. |
| `setMyCommands`, `deleteMyCommands` | `bots.setBotCommands` / `bots.resetBotCommands`, including existing scopes and languages. |
| `sendLocation`, `sendVenue`, `sendPoll` | Native geo/venue/poll media, including live-location options and supported poll fields. |
| `setMessageReaction` | `messages.sendReaction`. |
| `pinChatMessage`, `unpinChatMessage` | `messages.updatePinnedMessage`; no-ID unpin resolves the latest pin through bot-allowed full-peer metadata. |
| `createForumTopic`, `editForumTopic`, close/reopen and hide/unhide | Native forum-topic methods; topic IDs remain explicit. |
| `getUpdates`, `setWebhook`, `deleteWebhook`, `getWebhookInfo` | Worker-owned receipt/subscription operations, preserving the host-facing contract without cloud Bot API polling. |

Business context uses Telegram's business connection and DC routing. Unsupported fields/combinations return explicit errors. The worker does not silently switch topics or report a successful operation that it did not perform.

## Incoming events and retained integrations

| Behavior | Implementation |
| --- | --- |
| New/edited private, group and channel messages | MTProto updates → existing Telegram-shaped messages → original ingress spool and handlers. |
| Business messages, edits, deletions and callbacks | Business update conversion preserves connection identity and supplied reply context. |
| Ordinary and inline button callbacks | Callback conversion preserves query identity, sender, opaque inline ID and message context. |
| Reactions, poll definitions/results/answers and membership | Native update conversion into the original event contract. |
| Photos, documents, audio/voice/video, stickers, forwards, replies, quotes and service messages | Message/media/entity conversion; original context and policy handlers remain. |
| Pairing, allowlists, accounts, routes, commands, approvals and streaming | Existing OpenClaw owners retained; their Telegram effects use the adapter. |
| Mini App launch buttons, browser code, HMAC, tickets and web routes | Existing integration retained; supported buttons serialize to native MTProto markup. |
| Restart durability | Raw batch commits before cursor persistence; normalized inbox commits before raw retirement; confirmation follows host spool commit or receiver success. |

Unobserved legacy poll result definitions remain durably deferred and visible in health. A standalone expired file ID without a known origin can require re-observing the attachment. Mini App public HTTPS hosting and all permission-dependent live forum/business combinations were not fully exercised; see the test report.
