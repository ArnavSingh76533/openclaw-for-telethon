"""Bot API-shaped operations backed by Telethon's MTProto schema.

Unknown options fail explicitly instead of publishing to a different surface
or silently changing the requested Telegram behavior.
"""
import inspect
import asyncio
import mimetypes
import re
import struct
import base64
from datetime import datetime, timezone
from urllib.parse import quote

from telethon import errors, functions, types, utils
from telethon.extensions import html
from telethon.helpers import add_surrogate, del_surrogate
from telethon.tl.tlobject import TLObject

from . import codec


class UnsupportedMethodError(Exception):
    error_code = 501
    unsupported_method = True

    @property
    def description(self):
        return str(self)


def _check(payload, allowed):
    extra = set(payload) - set(allowed)
    if extra:
        raise UnsupportedMethodError("MTProto mapping is unavailable for: " + ", ".join(sorted(extra)))


def _integer(value):
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return value


class _BotHtmlParser(html.HTMLToTelegramParser):
    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag == "tg-spoiler" or (tag == "span" and dict(attrs).get("class") == "tg-spoiler"):
            self._building_entities[tag] = types.MessageEntitySpoiler(len(self.text), 0)


def _markdown(text, modern):
    """Parse Bot API formatting with Telegram's UTF-16 entity offsets."""
    source = add_surrogate(text)
    output, entities, stack = [], [], []
    delimiters = [("```", types.MessageEntityPre), ("`", types.MessageEntityCode)]
    if modern:
        delimiters += [("__", types.MessageEntityUnderline), ("||", types.MessageEntitySpoiler)]
    delimiters += [("*", types.MessageEntityBold), ("_", types.MessageEntityItalic)]
    if modern:
        delimiters.append(("~", types.MessageEntityStrike))
    index = 0
    while index < len(source):
        char = source[index]
        if modern and char == ">" and (index == 0 or source[index - 1] == "\n"):
            lines = []
            cursor = index
            while cursor < len(source) and source[cursor] == ">":
                line_end = source.find("\n", cursor)
                line_end = len(source) if line_end < 0 else line_end
                line = source[cursor + 1:line_end]
                lines.append(line[1:] if line.startswith(" ") else line)
                if line_end == len(source) or source[line_end + 1:line_end + 2] != ">":
                    break
                cursor = line_end + 1
            body = "\n".join(lines)
            collapsed = body.endswith("||")
            if collapsed:
                body = body[:-2]
            plain, inner = _markdown(del_surrogate(body), True)
            offset = len(output)
            body = add_surrogate(plain)
            output.extend(body)
            entities.append(types.MessageEntityBlockquote(offset, len(body), collapsed=collapsed))
            for entity in inner:
                entity.offset += offset
                entities.append(entity)
            index = line_end
            continue
        if char == "\\" and index + 1 < len(source):
            output.append(source[index + 1])
            index += 2
            continue
        if stack and stack[-1][0] in {"`", "```"} and not source.startswith(stack[-1][0], index):
            output.append(char)
            index += 1
            continue
        custom = modern and source.startswith("![", index)
        if char == "[" or custom:
            opening = index + (2 if custom else 1)
            closing = source.find("](", opening)
            if closing >= 0:
                end = closing + 2
                escaped = False
                while end < len(source):
                    if source[end] == ")" and not escaped:
                        break
                    escaped = source[end] == "\\" and not escaped
                    if source[end] != "\\":
                        escaped = False
                    end += 1
                if end >= len(source):
                    raise ValueError("Bad Request: can't parse entities: unclosed Markdown URL")
                label, inner = _markdown(del_surrogate(source[opening:closing]), modern)
                label = add_surrogate(label)
                offset = len(output)
                output.extend(label)
                for entity in inner:
                    entity.offset += offset
                    entities.append(entity)
                url = del_surrogate(source[closing + 2:end]).replace("\\)", ")").replace("\\\\", "\\")
                if custom:
                    match = re.fullmatch(r"tg://emoji\?id=(\d+)", url)
                    if not match:
                        raise ValueError("Bad Request: can't parse entities: invalid custom emoji URL")
                    entities.append(types.MessageEntityCustomEmoji(offset, len(label), int(match.group(1))))
                else:
                    entities.append(types.MessageEntityTextUrl(offset, len(label), url))
                index = end + 1
                continue
        matched = False
        for delimiter, entity_type in delimiters:
            if not source.startswith(delimiter, index):
                continue
            if stack and stack[-1][0] == delimiter:
                _, offset, constructor, language = stack.pop()
                length = len(output) - offset
                if length:
                    entities.append(constructor(offset, length, language) if constructor is types.MessageEntityPre else constructor(offset, length))
            else:
                language = ""
                if delimiter == "```":
                    newline = source.find("\n", index + 3)
                    if newline >= 0:
                        language = del_surrogate(source[index + 3:newline])
                        index = newline + 1 - len(delimiter)
                stack.append((delimiter, len(output), entity_type, language))
            index += len(delimiter)
            matched = True
            break
        if matched:
            continue
        # MarkdownV2 carriage returns are ignored and can disambiguate ___.
        if modern and char == "\r":
            index += 1
            continue
        output.append(char)
        index += 1
    if stack:
        raise ValueError("Bad Request: can't parse entities: unclosed Markdown formatting")
    return del_surrogate("".join(output)), sorted(entities, key=lambda item: (item.offset, -item.length))


COMMON = {
    "chat_id", "message_thread_id", "direct_messages_topic_id", "business_connection_id",
    "disable_notification", "protect_content", "allow_paid_broadcast", "message_effect_id",
    "reply_parameters", "reply_to_message_id", "allow_sending_without_reply", "reply_markup",
    "show_caption_above_media",
}
MEDIA = {
    "caption", "parse_mode", "caption_entities", "duration", "width", "height", "length",
    "thumbnail", "thumb", "supports_streaming", "has_spoiler", "performer", "title",
    "disable_content_type_detection", "emoji", "cover", "start_timestamp",
}


class MtprotoMethods:
    def __init__(self, client, update_observer=None, media_refresher=None):
        self.client = client
        self._business_dcs = {}
        self._update_observer = update_observer
        self._media_refresher = media_refresher

    def remember_business_connection(self, connection):
        self._business_dcs[connection.connection_id] = None if connection.disabled else connection.dc_id

    async def _peer(self, value):
        return await codec.resolve_input_peer(self.client, _integer(value))

    async def _full_chat(self, entity):
        if isinstance(entity, types.Channel):
            return (await self.client(functions.channels.GetFullChannelRequest(utils.get_input_channel(entity)))).full_chat
        if isinstance(entity, types.Chat):
            return (await self.client(functions.messages.GetFullChatRequest(entity.id))).full_chat
        return (await self.client(functions.users.GetFullUserRequest(utils.get_input_user(entity)))).full_user

    async def _resolve_mentions(self, value):
        # Raw requests bypass Telethon's high-level message parser. Resolve the
        # input-user form here for explicit entities, HTML links and quotes.
        user_id = None
        if isinstance(value, types.MessageEntityMentionName):
            user_id = value.user_id
        elif isinstance(value, types.MessageEntityTextUrl):
            match = re.fullmatch(r"tg://user\?id=(\d+)", value.url)
            if match:
                user_id = int(match.group(1))
        if user_id is not None:
            return types.InputMessageEntityMentionName(
                value.offset, value.length, utils.get_input_user(await self._peer(user_id)),
            )
        if isinstance(value, TLObject):
            for name, child in list(vars(value).items()):
                if not name.startswith("_"):
                    setattr(value, name, await self._resolve_mentions(child))
        elif isinstance(value, list):
            return [await self._resolve_mentions(child) for child in value]
        return value

    async def _invoke(self, request, payload):
        await self._resolve_mentions(request)
        connection = payload.get("business_connection_id")
        if connection and isinstance(request, functions.messages.UploadMediaRequest):
            request.business_connection_id = str(connection)
            wrapped = request
        else:
            wrapped = functions.InvokeWithBusinessConnectionRequest(str(connection), request) if connection else request
        business_dc = None
        if connection:
            if connection not in self._business_dcs:
                info = await self.client(functions.account.GetBotBusinessConnectionRequest(str(connection)))
                updates = getattr(info, "updates", [getattr(info, "update", None)])
                for update in updates:
                    if isinstance(update, types.UpdateBotBusinessConnect):
                        self.remember_business_connection(update.connection)
            if connection not in self._business_dcs:
                raise RuntimeError("Telegram did not return the requested business connection")
            business_dc = self._business_dcs[connection]
            if business_dc is None:
                raise ValueError("Forbidden: business connection is disabled")

        async def invoke():
            if business_dc is None or business_dc == self.client.session.dc_id:
                result = await self.client(wrapped)
            else:
                sender = await self.client._borrow_exported_sender(business_dc)
                try:
                    result = await self.client._call(sender, wrapped)
                finally:
                    await self.client._return_exported_sender(sender)
            # RPC-returned updates do not necessarily enter Telethon's inbound
            # queue. The service observes metadata here without emitting events.
            if self._update_observer is not None:
                self._update_observer(result)
            return result
        try:
            return await invoke()
        except errors.RPCError as error:
            if (self._media_refresher is not None
                    and re.fullmatch(r"FILE_REFERENCE(?:_\d+)?_(?:EXPIRED|INVALID|EMPTY)", error.message)
                    and await self._media_refresher(request)):
                # Telegram definitively rejected the stale reference. Refresh
                # only an observed source and reuse the same random_id/target.
                return await invoke()
            if error.message not in {"REPLY_MESSAGE_ID_INVALID", "REPLY_TO_MSG_ID_INVALID"}:
                raise
            params = payload.get("reply_parameters") or {}
            allowed = params.get("allow_sending_without_reply", payload.get("allow_sending_without_reply", False))
            if not allowed or not isinstance(getattr(request, "reply_to", None), types.InputReplyToMessage):
                raise
            # Bot API permits a missing reply while MTProto has no allow flag.
            # Keep the explicit surface on this definite rejection and retry.
            remaining = {key: payload[key] for key in ("message_thread_id", "direct_messages_topic_id") if key in payload}
            request.reply_to = await self._reply(remaining)
            return await invoke()

    def _text(self, payload, key="text", entities_key="entities"):
        text = payload.get(key, "")
        if entities_key in payload:
            return text, codec.entities_to_mtproto(payload[entities_key] or [])
        mode = payload.get("parse_mode")
        if not mode:
            return text, []
        if mode.lower() == "html":
            parser = _BotHtmlParser()
            parser.feed(add_surrogate(text))
            return del_surrogate(parser.text), sorted(parser.entities, key=lambda item: (item.offset, -item.length))
        if mode.lower() in {"markdown", "markdownv2"}:
            return _markdown(text, mode.lower() == "markdownv2")
        raise ValueError("Bad Request: unsupported parse_mode: " + str(mode))

    async def _reply(self, payload):
        params = payload.get("reply_parameters") or {}
        _check(params, {"message_id", "chat_id", "allow_sending_without_reply", "quote", "quote_parse_mode", "quote_entities", "quote_position", "checklist_task_id"})
        reply_id = params.get("message_id", payload.get("reply_to_message_id"))
        topic = payload.get("message_thread_id")
        direct = payload.get("direct_messages_topic_id")
        if direct is not None and topic is not None:
            raise ValueError("Forum and Direct Messages topics cannot be combined")
        mono = await self._peer(direct) if direct is not None else None
        if reply_id is None:
            if mono is not None:
                return types.InputReplyToMonoForum(mono)
            if topic is not None:
                return types.InputReplyToMessage(int(topic), top_msg_id=int(topic))
            return None
        quote_payload = {"text": params.get("quote", ""), "parse_mode": params.get("quote_parse_mode")}
        if "quote_entities" in params:
            quote_payload["entities"] = params["quote_entities"]
        quote, entities = self._text(quote_payload)
        return types.InputReplyToMessage(
            int(reply_id), top_msg_id=int(topic) if topic is not None else None,
            reply_to_peer_id=await self._peer(params["chat_id"]) if "chat_id" in params else None,
            quote_text=quote if "quote" in params else None,
            quote_entities=entities if "quote" in params else None,
            quote_offset=params.get("quote_position"), monoforum_peer_id=mono,
            todo_item_id=params.get("checklist_task_id"),
        )

    async def _common(self, payload):
        return {
            "reply_to": await self._reply(payload),
            "reply_markup": codec.markup_to_mtproto(payload["reply_markup"]) if "reply_markup" in payload else None,
            "silent": payload.get("disable_notification"),
            "noforwards": payload.get("protect_content"),
            "allow_paid_floodskip": payload.get("allow_paid_broadcast"),
            "effect": int(payload["message_effect_id"]) if payload.get("message_effect_id") is not None else None,
            "invert_media": payload.get("show_caption_above_media"),
        }

    async def _messages(self, request, result, peer, payload):
        if isinstance(result, types.UpdateShortSentMessage):
            reply = getattr(request, "reply_to", None)
            reply_header = None
            if isinstance(reply, types.InputReplyToMessage):
                reply_header = types.MessageReplyHeader(
                    reply_to_msg_id=reply.reply_to_msg_id,
                    reply_to_top_id=reply.top_msg_id,
                    forum_topic=payload.get("message_thread_id") is not None,
                    quote_text=reply.quote_text, quote_entities=reply.quote_entities,
                    quote_offset=reply.quote_offset,
                )
            message = types.Message(id=result.id, peer_id=utils.get_peer(peer), date=result.date,
                                    message=getattr(request, "message", ""), out=True,
                                    media=result.media, entities=result.entities,
                                    reply_to=reply_header,
                                    reply_markup=getattr(request, "reply_markup", None))
            message._finish_init(self.client, {}, peer)
            messages = [message]
        else:
            messages = []
            entities = {utils.get_peer_id(entity): entity for entity in [*getattr(result, "users", []), *getattr(result, "chats", [])]}
            updates = getattr(result, "updates", [getattr(result, "update", None)])
            random_ids = {item.random_id for item in request.multi_media} if isinstance(request, functions.messages.SendMultiMediaRequest) else {getattr(request, "random_id", None)}
            matched_ids = {update.id for update in updates if isinstance(update, types.UpdateMessageID) and update.random_id in random_ids}
            for update in updates:
                message = getattr(update, "message", None)
                if isinstance(message, types.Message):
                    if matched_ids and message.id not in matched_ids:
                        continue
                    if isinstance(request, functions.messages.EditMessageRequest) and message.id != request.id:
                        continue
                    if utils.get_peer_id(message.peer_id) != utils.get_peer_id(peer):
                        continue
                    message._finish_init(self.client, entities, peer)
                    messages.append(message)
            if isinstance(request, functions.messages.SendMultiMediaRequest) and matched_ids:
                by_id = {message.id: message for message in messages}
                random_to_id = {update.random_id: update.id for update in updates if isinstance(update, types.UpdateMessageID)}
                messages = [by_id[random_to_id[item.random_id]] for item in request.multi_media if random_to_id.get(item.random_id) in by_id]
            if not messages:
                raise RuntimeError("MTProto returned no message for the completed operation; do not resend automatically")
        converted = []
        for message in messages:
            item = await codec.message_to_bot(self.client, message)
            if payload.get("business_connection_id"):
                item["business_connection_id"] = payload["business_connection_id"]
            converted.append(item)
        return converted

    async def _send(self, payload, media):
        peer = await self._peer(payload["chat_id"])
        text, entities = self._text(payload, "caption", "caption_entities")
        request = functions.messages.SendMediaRequest(peer, media, text, entities=entities, **await self._common(payload))
        return (await self._messages(request, await self._invoke(request, payload), peer, payload))[0]

    async def _file(self, value):
        if isinstance(value, dict) and "__upload__" in value:
            return value["__upload__"]
        if not isinstance(value, str):
            raise ValueError("Expected an upload, Bot API file_id, or HTTP URL")
        if value.startswith(("http://", "https://")):
            return value
        resolved = codec.resolve_file_id(value)
        if resolved is None:
            raise UnsupportedMethodError("Telethon cannot decode this Bot API file_id; resend or re-observe the attachment to refresh its MTProto reference")
        return resolved

    async def _media(self, kind, value, payload):
        source = await self._file(value)
        metadata = None
        if isinstance(value, dict) and kind in {"video", "video_note", "animation", "voice", "audio"}:
            # Probe while the original bytes are available. Once uploaded the
            # InputFile only contains a handle and Telethon cannot inspect it.
            metadata = await asyncio.to_thread(utils._get_metadata, source)

        def meta(key, default):
            return metadata.get(key) if metadata and metadata.has(key) else default

        duration = meta("duration", None)
        duration = duration.total_seconds() if duration is not None else 0
        attributes = []
        if kind in {"video", "video_note", "animation"}:
            attributes.append(types.DocumentAttributeVideo(
                payload.get("duration", duration), payload.get("width", payload.get("length", meta("width", 1))),
                payload.get("height", payload.get("length", meta("height", 1))),
                round_message=kind == "video_note", supports_streaming=payload.get("supports_streaming"),
            ))
        if kind == "animation":
            attributes.append(types.DocumentAttributeAnimated())
        if kind in {"voice", "audio"}:
            attributes.append(types.DocumentAttributeAudio(payload.get("duration", int(duration)), voice=kind == "voice", title=payload.get("title", meta("title", None)), performer=payload.get("performer", meta("author", meta("artist", None)))))
        if kind == "sticker":
            attributes.append(types.DocumentAttributeSticker(payload.get("emoji", ""), types.InputStickerSetEmpty()))
        mime_type = None
        if isinstance(value, dict):
            mime_type = value.get("content_type")
            if not mime_type or mime_type == "application/octet-stream":
                filename = value.get("filename", "")
                mime_type = mimetypes.guess_type(filename)[0]
                if not mime_type:
                    mime_type = {"voice": "audio/ogg", "video": "video/mp4", "video_note": "video/mp4", "animation": "video/mp4"}.get(kind)
                if kind == "sticker" and filename.lower().endswith(".tgs"):
                    mime_type = "application/x-tgsticker"
            source = await self.client.upload_file(source, file_name=value.get("filename"))
            if value.get("filename"):
                attributes.append(types.DocumentAttributeFilename(value["filename"]))
        thumb = payload.get("thumbnail", payload.get("thumb"))
        if thumb is not None:
            thumb_value = thumb
            thumb = await self._file(thumb)
            if isinstance(thumb_value, dict):
                thumb = await self.client.upload_file(thumb, file_name=thumb_value.get("filename"))
        # The pinned Telethon helper owns URL, reusable ID and upload conversion.
        # Raw requests preserve topic and business options absent in send_file().
        _, media, _ = await self.client._file_to_media(
            source, force_document=kind != "photo", attributes=attributes or None,
            thumb=thumb, voice_note=kind == "voice", video_note=kind == "video_note",
            supports_streaming=payload.get("supports_streaming", False), mime_type=mime_type,
            nosound_video=kind == "animation", as_image=kind == "photo",
        )
        if media is None:
            raise ValueError("Telethon could not convert the Telegram media")
        for source_key, target_key in {"has_spoiler": "spoiler", "start_timestamp": "video_timestamp", "disable_content_type_detection": "force_file"}.items():
            if source_key in payload:
                if not hasattr(media, target_key):
                    raise UnsupportedMethodError("Cannot preserve media option " + source_key)
                setattr(media, target_key, payload[source_key])
        if "cover" in payload:
            if not hasattr(media, "video_cover"):
                raise UnsupportedMethodError("Video cover mapping is unavailable for this media")
            _, cover, _ = await self.client._file_to_media(await self._file(payload["cover"]), as_image=True)
            if not isinstance(cover, types.InputMediaPhoto):
                uploaded = await self._invoke(functions.messages.UploadMediaRequest(await self._peer(payload["chat_id"]), cover), payload)
                cover = utils.get_input_media(uploaded)
            media.video_cover = cover.id
        return media

    async def _scope(self, payload):
        scope = payload.get("scope") or {"type": "default"}
        _check(scope, {"type", "chat_id", "user_id"})
        simple = {"default": types.BotCommandScopeDefault, "all_private_chats": types.BotCommandScopeUsers,
                  "all_group_chats": types.BotCommandScopeChats, "all_chat_administrators": types.BotCommandScopeChatAdmins}
        kind = scope.get("type", "default")
        if kind in simple:
            return simple[kind]()
        peer = await self._peer(scope["chat_id"])
        if kind == "chat":
            return types.BotCommandScopePeer(peer)
        if kind == "chat_administrators":
            return types.BotCommandScopePeerAdmins(peer)
        if kind == "chat_member":
            return types.BotCommandScopePeerUser(peer, utils.get_input_user(await self._peer(scope["user_id"])))
        raise UnsupportedMethodError("Unknown Telegram command scope: " + kind)

    def _preview(self, payload, text, entities):
        preview = payload.get("link_preview_options") or {}
        _check(preview, {"is_disabled", "url", "prefer_small_media", "prefer_large_media", "show_above_text"})
        disabled = preview.get("is_disabled", payload.get("disable_web_page_preview"))
        media = None
        url = preview.get("url")
        if not url and (preview.get("prefer_small_media") or preview.get("prefer_large_media")):
            url = next((entity.url for entity in entities if isinstance(entity, types.MessageEntityTextUrl)), None)
            if not url:
                match = re.search(r"https?://[^\s<>]+", text)
                url = match.group(0) if match else None
        if url and not disabled:
            media = types.InputMediaWebPage(url, force_small_media=preview.get("prefer_small_media"), force_large_media=preview.get("prefer_large_media"))
        return disabled, media, preview.get("show_above_text")

    async def _rich(self, payload):
        async def blocks(values):
            result = []
            for original in values:
                block = dict(original)
                kind = block.get("type")
                if kind in {"photo", "video", "audio", "animation", "voice_note", "document"}:
                    descriptor = dict(block[kind])
                    source = descriptor.get("file_id") or descriptor.get("media")
                    if isinstance(source, dict) or isinstance(source, str) and source.startswith(("https://", "http://")):
                        media_payload = {**payload, **descriptor}
                        media = await self._media("voice" if kind == "voice_note" else kind, source, media_payload)
                        peer = await self._peer(payload["chat_id"]) if "chat_id" in payload else types.InputPeerSelf()
                        uploaded = await self._invoke(functions.messages.UploadMediaRequest(peer, media), payload)
                        obj = uploaded.photo if isinstance(uploaded, types.MessageMediaPhoto) else uploaded.document
                        descriptor["media"] = codec.pack_file_id(obj)
                        descriptor.pop("file_id", None)
                    block[kind] = descriptor
                if "blocks" in block:
                    block["blocks"] = await blocks(block["blocks"])
                if kind == "list":
                    block["items"] = [{**item, "blocks": await blocks(item["blocks"])} for item in block["items"]]
                result.append(block)
            return result

        rich = payload["rich_message"]
        if "blocks" not in rich:
            converted = codec.rich_to_mtproto({key: value for key, value in rich.items() if key != "media"})
            files = []
            for item in rich.get("media", []):
                _check(item, {"id", "media"})
                descriptor = item["media"]
                kind = descriptor["type"]
                if kind not in {"photo", "video", "audio", "animation", "voice_note", "document"}:
                    raise ValueError("Unsupported rich attachment media type: " + str(kind))
                source = descriptor.get("file_id") or descriptor.get("media")
                media = await self._media("voice" if kind == "voice_note" else kind, source, {**payload, **descriptor})
                if not isinstance(media, (types.InputMediaPhoto, types.InputMediaDocument)):
                    peer = await self._peer(payload["chat_id"]) if "chat_id" in payload else types.InputPeerSelf()
                    media = utils.get_input_media(await self._invoke(functions.messages.UploadMediaRequest(peer, media), payload))
                if isinstance(media, types.InputMediaPhoto):
                    files.append(types.InputRichFilePhoto(str(item["id"]), media.id))
                else:
                    files.append(types.InputRichFileDocument(str(item["id"]), media.id))
            if files:
                converted.files = files
            return converted
        return codec.rich_to_mtproto({**rich, "blocks": await blocks(rich["blocks"])})

    @staticmethod
    def _inline_id(value):
        try:
            data = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            if len(data) == 20:
                dc, message_id, peer_id, access_hash = struct.unpack("<iiiq", data)
                combined_id = (peer_id & 0xFFFFFFFF) << 32 | message_id & 0xFFFFFFFF
                if combined_id >= 1 << 63:
                    combined_id -= 1 << 64
                return types.InputBotInlineMessageID(dc, combined_id, access_hash)
            if len(data) == 24:
                dc, owner, message_id, access_hash = struct.unpack("<iqiq", data)
                return types.InputBotInlineMessageID64(dc, owner, message_id, access_hash)
        except (ValueError, TypeError, struct.error):
            pass
        raise ValueError("Bad Request: invalid inline_message_id")

    async def call(self, method, payload):
        if method in {"sendMessage", "sendRichMessage"}:
            _check(payload, COMMON | {"text", "parse_mode", "entities", "link_preview_options", "disable_web_page_preview", "rich_message"})
            rich = await self._rich(payload) if method == "sendRichMessage" else None
            peer = await self._peer(payload["chat_id"])
            text, entities = self._text(payload)
            disabled, preview_media, above = self._preview(payload, text, entities)
            common = await self._common(payload)
            if above is not None:
                common["invert_media"] = above
            if preview_media is not None:
                if rich is not None:
                    raise UnsupportedMethodError("Explicit web preview media cannot be combined with a native rich message")
                request = functions.messages.SendMediaRequest(peer, preview_media, text, entities=entities, **common)
            else:
                request = functions.messages.SendMessageRequest(peer, text, entities=entities, rich_message=rich, no_webpage=disabled, **common)
            return (await self._messages(request, await self._invoke(request, payload), peer, payload))[0]
        media_methods = {"sendPhoto": "photo", "sendAnimation": "animation", "sendVideo": "video", "sendVideoNote": "video_note", "sendVoice": "voice", "sendAudio": "audio", "sendDocument": "document", "sendSticker": "sticker"}
        if method in media_methods:
            kind = media_methods[method]
            _check(payload, COMMON | MEDIA | {kind})
            self._text(payload, "caption", "caption_entities")
            await self._common(payload)
            return await self._send(payload, await self._media(kind, payload[kind], payload))
        if method == "sendMediaGroup":
            _check(payload, COMMON | {"media"})
            if "reply_markup" in payload:
                raise UnsupportedMethodError("Telegram albums do not support reply_markup")
            items = payload["media"]
            if not 2 <= len(items) <= 10:
                raise ValueError("Telegram albums require between 2 and 10 items")
            for item in items:
                _check(item, MEDIA | {"type", "media"})
                if item["type"] not in {"photo", "video", "audio", "document"}:
                    raise ValueError("Unsupported Telegram album media type")
                self._text(item, "caption", "caption_entities")
            common = await self._common(payload)
            common.pop("reply_markup")
            peer = await self._peer(payload["chat_id"])
            converted = []
            for item in items:
                text, entities = self._text(item, "caption", "caption_entities")
                media = await self._media(item["type"], item["media"], item)
                if isinstance(media, (types.InputMediaUploadedPhoto, types.InputMediaUploadedDocument, types.InputMediaPhotoExternal, types.InputMediaDocumentExternal)):
                    uploaded = await self._invoke(functions.messages.UploadMediaRequest(peer, media), payload)
                    media = utils.get_input_media(uploaded)
                    if "has_spoiler" in item:
                        media.spoiler = item["has_spoiler"]
                converted.append(types.InputSingleMedia(media, text, entities=entities))
            request = functions.messages.SendMultiMediaRequest(peer, converted, **common)
            return await self._messages(request, await self._invoke(request, payload), peer, payload)
        if method in {"editMessageText", "editMessageCaption", "editMessageReplyMarkup"}:
            _check(payload, {"chat_id", "message_id", "business_connection_id", "inline_message_id", "text", "caption", "parse_mode", "entities", "caption_entities", "reply_markup", "link_preview_options", "disable_web_page_preview", "show_caption_above_media", "rich_message"})
            inline_id = self._inline_id(payload["inline_message_id"]) if "inline_message_id" in payload else None
            peer = await self._peer(payload["chat_id"]) if inline_id is None else None
            options = {}
            if method != "editMessageReplyMarkup":
                key = "caption" if method == "editMessageCaption" else "text"
                text, entities = self._text(payload, key, "caption_entities" if key == "caption" else "entities")
                options.update(message=text, entities=entities)
            if "reply_markup" in payload or method == "editMessageReplyMarkup":
                options["reply_markup"] = codec.markup_to_mtproto(payload.get("reply_markup") or {"inline_keyboard": []})
            disabled, preview_media, above = self._preview(payload, options.get("message", ""), options.get("entities", []))
            options.update(no_webpage=disabled, invert_media=above if above is not None else payload.get("show_caption_above_media"))
            if preview_media is not None:
                options["media"] = preview_media
            if "rich_message" in payload:
                options["rich_message"] = await self._rich(payload)
            if inline_id is not None:
                request = functions.messages.EditInlineBotMessageRequest(inline_id, **options)
                if inline_id.dc_id != self.client.session.dc_id:
                    await self._resolve_mentions(request)
                    sender = await self.client._borrow_exported_sender(inline_id.dc_id)
                    try:
                        await self.client._call(sender, request)
                    finally:
                        await self.client._return_exported_sender(sender)
                else:
                    await self._invoke(request, payload)
                return True
            request = functions.messages.EditMessageRequest(peer, int(payload["message_id"]), **options)
            return (await self._messages(request, await self._invoke(request, payload), peer, payload))[0]
        return await self._other(method, payload)

    async def _other(self, method, payload):
        if method == "getMe":
            _check(payload, set())
            return codec.user_to_bot(await self.client.get_me())
        if method == "getFile":
            _check(payload, {"file_id"})
            file_id = payload["file_id"]
            media = codec.resolve_file_id(file_id)
            if media is None:
                raise ValueError("Bad Request: invalid file_id or unavailable file reference")
            result = codec._file_info(media)
            result.update(file_id=file_id, file_path="telethon/" + quote(file_id, safe=""))
            if not result.get("file_size"):
                result.pop("file_size", None)
            return result
        if method == "getChat":
            _check(payload, {"chat_id"})
            entity = await self.client.get_entity(await self._peer(payload["chat_id"]))
            chat = codec.chat_to_bot(entity)
            full = await self._full_chat(entity)
            if getattr(full, "about", None):
                chat["bio" if isinstance(entity, types.User) else "description"] = full.about
            if getattr(full, "linked_chat_id", None):
                chat["linked_chat_id"] = utils.get_peer_id(types.PeerChannel(full.linked_chat_id))
            available = getattr(full, "available_reactions", None)
            if isinstance(available, types.ChatReactionsSome):
                chat["available_reactions"] = [self._reaction_bot(item) for item in available.reactions]
            pinned_id = getattr(full, "pinned_msg_id", None)
            if pinned_id:
                pinned = await self.client.get_messages(entity, ids=pinned_id)
                if isinstance(pinned, types.Message):
                    chat["pinned_message"] = await codec.message_to_bot(self.client, pinned)
            return chat
        if method == "getChatMember":
            _check(payload, {"chat_id", "user_id"})
            peer = await self._peer(payload["chat_id"])
            user = await self.client.get_entity(await self._peer(payload["user_id"]))
            permissions = await self.client.get_permissions(peer, user)
            participant = permissions.participant
            restricted = isinstance(participant, types.ChannelParticipantBanned)
            kicked = restricted and bool(participant.banned_rights.view_messages)
            status = "creator" if permissions.is_creator else "administrator" if permissions.is_admin else "kicked" if kicked else "restricted" if restricted else "left" if permissions.has_left else "member"
            result = {"status": status, "user": codec.user_to_bot(user)}
            if restricted:
                banned = participant.banned_rights
                result["is_member"] = not bool(participant.left)
                result["until_date"] = int(banned.until_date.timestamp()) if isinstance(banned.until_date, datetime) else banned.until_date or 0
                for api, mt in {"can_send_messages": "send_messages", "can_send_audios": "send_audios", "can_send_documents": "send_docs", "can_send_photos": "send_photos", "can_send_videos": "send_videos", "can_send_video_notes": "send_roundvideos", "can_send_voice_notes": "send_voices", "can_send_polls": "send_polls", "can_send_other_messages": "send_stickers", "can_add_web_page_previews": "embed_links", "can_change_info": "change_info", "can_invite_users": "invite_users", "can_pin_messages": "pin_messages", "can_manage_topics": "manage_topics"}.items():
                    result[api] = not bool(getattr(banned, mt, False))
            rights = getattr(participant, "admin_rights", None)
            if status in {"administrator", "creator"}:
                for api, mt in {"can_manage_chat":"other", "can_delete_messages":"delete_messages", "can_manage_video_chats":"manage_call", "can_restrict_members":"ban_users", "can_promote_members":"add_admins", "can_change_info":"change_info", "can_invite_users":"invite_users", "can_post_messages":"post_messages", "can_edit_messages":"edit_messages", "can_pin_messages":"pin_messages", "can_manage_topics":"manage_topics", "can_post_stories":"post_stories", "can_edit_stories":"edit_stories", "can_delete_stories":"delete_stories", "can_manage_direct_messages":"manage_direct_messages"}.items():
                    result[api] = status == "creator" or bool(getattr(rights, mt, False))
                result["is_anonymous"] = bool(getattr(rights, "anonymous", False))
                result["can_be_edited"] = bool(getattr(participant, "can_edit", False))
                if getattr(participant, "rank", None):
                    result["custom_title"] = participant.rank
            return result
        if method == "getUserProfilePhotos":
            _check(payload, {"user_id", "offset", "limit"})
            result = await self.client(functions.photos.GetUserPhotosRequest(utils.get_input_user(await self._peer(payload["user_id"])), payload.get("offset", 0), 0, payload.get("limit", 100)))
            photos = []
            for photo in result.photos:
                photos.append(codec.photo_to_bot(photo))
            return {"total_count": getattr(result, "count", len(photos)), "photos": photos}
        if method in {"deleteMessage", "deleteBusinessMessages"}:
            _check(payload, {"chat_id", "message_id"} if method == "deleteMessage" else {"business_connection_id", "message_ids"})
            if method == "deleteMessage":
                await self.client.delete_messages(await self._peer(payload["chat_id"]), [int(payload["message_id"])], revoke=True)
            else:
                await self._invoke(functions.messages.DeleteMessagesRequest([int(value) for value in payload["message_ids"]], revoke=True), payload)
            return True
        if method == "answerCallbackQuery":
            _check(payload, {"callback_query_id", "text", "show_alert", "url", "cache_time"})
            await self.client(functions.messages.SetBotCallbackAnswerRequest(int(payload["callback_query_id"]), payload.get("cache_time", 0), alert=payload.get("show_alert"), message=payload.get("text"), url=payload.get("url")))
            return True
        if method == "sendChatAction":
            _check(payload, {"chat_id", "message_thread_id", "business_connection_id", "action"})
            actions = {"typing": types.SendMessageTypingAction, "upload_photo": types.SendMessageUploadPhotoAction, "record_video": types.SendMessageRecordVideoAction, "upload_video": types.SendMessageUploadVideoAction, "record_voice": types.SendMessageRecordAudioAction, "upload_voice": types.SendMessageUploadAudioAction, "upload_document": types.SendMessageUploadDocumentAction, "choose_sticker": types.SendMessageChooseStickerAction, "find_location": types.SendMessageGeoLocationAction, "record_video_note": types.SendMessageRecordRoundAction, "upload_video_note": types.SendMessageUploadRoundAction, "cancel": types.SendMessageCancelAction}
            action_type = actions.get(payload["action"])
            if action_type is None:
                raise ValueError("Unsupported chat action")
            action = action_type(progress=0) if "progress" in inspect.signature(action_type).parameters else action_type()
            await self._invoke(functions.messages.SetTypingRequest(await self._peer(payload["chat_id"]), action, top_msg_id=payload.get("message_thread_id")), payload)
            return True
        if method in {"setMyCommands", "deleteMyCommands"}:
            _check(payload, {"scope", "language_code", "commands"} if method == "setMyCommands" else {"scope", "language_code"})
            scope = await self._scope(payload)
            if method == "setMyCommands":
                for command in payload["commands"]:
                    _check(command, {"command", "description"})
                request = functions.bots.SetBotCommandsRequest(scope, payload.get("language_code", ""), [types.BotCommand(item["command"], item["description"]) for item in payload["commands"]])
            else:
                request = functions.bots.ResetBotCommandsRequest(scope, payload.get("language_code", ""))
            await self.client(request)
            return True
        return await self._special(method, payload)

    async def _special(self, method, payload):
        if method in {"sendLocation", "sendVenue"}:
            _check(payload, COMMON | {"latitude", "longitude", "horizontal_accuracy", "live_period", "heading", "proximity_alert_radius", "title", "address", "foursquare_id", "foursquare_type", "google_place_id", "google_place_type"})
            point = types.InputGeoPoint(payload["latitude"], payload["longitude"], accuracy_radius=round(payload["horizontal_accuracy"]) if payload.get("horizontal_accuracy") is not None else None)
            if method == "sendVenue":
                if payload.get("foursquare_id") and payload.get("google_place_id"):
                    raise UnsupportedMethodError("MTProto venues accept one provider; choose either Google Places or Foursquare")
                google = "google_place_id" in payload
                media = types.InputMediaVenue(point, payload["title"], payload["address"], "gplaces" if google else "foursquare", payload.get("google_place_id" if google else "foursquare_id", ""), payload.get("google_place_type" if google else "foursquare_type", ""))
            elif "live_period" in payload:
                media = types.InputMediaGeoLive(point, period=payload["live_period"], heading=payload.get("heading"), proximity_notification_radius=payload.get("proximity_alert_radius"))
            else:
                if "heading" in payload or "proximity_alert_radius" in payload:
                    raise ValueError("Heading and proximity radius require a live location")
                media = types.InputMediaGeoPoint(point)
            return await self._send(payload, media)
        if method == "sendPoll":
            _check(payload, COMMON | {"question", "question_parse_mode", "question_entities", "options", "is_anonymous", "type", "allows_multiple_answers", "correct_option_id", "correct_option_ids", "explanation", "explanation_parse_mode", "explanation_entities", "open_period", "close_date", "is_closed", "allows_revoting", "shuffle_options", "hide_results_until_close", "allows_new_options"})
            question_payload = {"text": payload["question"], "parse_mode": payload.get("question_parse_mode")}
            if "question_entities" in payload:
                question_payload["entities"] = payload["question_entities"]
            question, question_entities = self._text(question_payload)
            answers = []
            for index, item in enumerate(payload["options"]):
                item = {"text": item} if isinstance(item, str) else item
                _check(item, {"text", "text_parse_mode", "text_entities"})
                option_payload = {"text": item["text"], "parse_mode": item.get("text_parse_mode")}
                if "text_entities" in item:
                    option_payload["entities"] = item["text_entities"]
                text, entities = self._text(option_payload)
                answers.append(types.PollAnswer(types.TextWithEntities(text, entities), bytes([index])))
            solution_payload = {"text": payload.get("explanation", ""), "parse_mode": payload.get("explanation_parse_mode")}
            if "explanation_entities" in payload:
                solution_payload["entities"] = payload["explanation_entities"]
            explanation, explanation_entities = self._text(solution_payload)
            poll = types.Poll(0, types.TextWithEntities(question, question_entities), answers, 0,
                closed=payload.get("is_closed"), public_voters=not payload.get("is_anonymous", True), multiple_choice=payload.get("allows_multiple_answers"), quiz=payload.get("type") == "quiz", close_period=payload.get("open_period"), close_date=datetime.fromtimestamp(payload["close_date"], timezone.utc) if "close_date" in payload else None,
                revoting_disabled=not payload["allows_revoting"] if "allows_revoting" in payload else None, shuffle_answers=payload.get("shuffle_options"), hide_results_until_close=payload.get("hide_results_until_close"), open_answers=payload.get("allows_new_options"))
            correct = payload.get("correct_option_ids", [payload["correct_option_id"]] if "correct_option_id" in payload else None)
            return await self._send(payload, types.InputMediaPoll(poll, correct_answers=correct, solution=explanation if "explanation" in payload else None, solution_entities=explanation_entities if "explanation" in payload else None))
        if method == "setMessageReaction":
            _check(payload, {"chat_id", "message_id", "reaction", "is_big"})
            reactions = []
            for item in payload.get("reaction", []):
                _check(item, {"type", "emoji", "custom_emoji_id"})
                if item["type"] == "emoji":
                    reactions.append(types.ReactionEmoji(item["emoji"]))
                elif item["type"] == "custom_emoji":
                    reactions.append(types.ReactionCustomEmoji(int(item["custom_emoji_id"])))
                else:
                    raise UnsupportedMethodError("Unsupported reaction type")
            await self.client(functions.messages.SendReactionRequest(await self._peer(payload["chat_id"]), int(payload["message_id"]), big=payload.get("is_big"), reaction=reactions))
            return True
        if method in {"pinChatMessage", "unpinChatMessage"}:
            _check(payload, {"chat_id", "message_id", "business_connection_id", "disable_notification"})
            if "message_id" not in payload:
                if method == "pinChatMessage":
                    raise ValueError("pinChatMessage requires message_id")
                if payload.get("business_connection_id"):
                    raise UnsupportedMethodError("Business unpin requires an explicit message_id; the ordinary bot dialog cannot identify its pin")
                peer = await self._peer(payload["chat_id"])
                entity = await self.client.get_entity(peer)
                # messages.search is user-only. Full peer metadata is bot-safe
                # and identifies the same most recent pin without history access.
                pinned_id = getattr(await self._full_chat(entity), "pinned_msg_id", None)
                if not pinned_id:
                    return True
                payload = {**payload, "message_id": pinned_id}
            await self._invoke(functions.messages.UpdatePinnedMessageRequest(await self._peer(payload["chat_id"]), int(payload["message_id"]), silent=payload.get("disable_notification"), unpin=method == "unpinChatMessage"), payload)
            return True
        if method == "createForumTopic":
            _check(payload, {"chat_id", "name", "icon_color", "icon_custom_emoji_id"})
            request = functions.messages.CreateForumTopicRequest(await self._peer(payload["chat_id"]), payload["name"], icon_color=payload.get("icon_color"), icon_emoji_id=int(payload["icon_custom_emoji_id"]) if "icon_custom_emoji_id" in payload else None)
            result = await self.client(request)
            for update in getattr(result, "updates", []):
                message = getattr(update, "message", None)
                action = getattr(message, "action", None)
                if isinstance(action, types.MessageActionTopicCreate):
                    topic = {"message_thread_id": message.id, "name": action.title, "icon_color": action.icon_color}
                    if action.icon_emoji_id is not None:
                        topic["icon_custom_emoji_id"] = str(action.icon_emoji_id)
                    return topic
            raise RuntimeError("MTProto returned no topic ID after topic creation; do not retry automatically")
        if method in {"editForumTopic", "closeForumTopic", "reopenForumTopic", "hideGeneralForumTopic", "unhideGeneralForumTopic"}:
            _check(payload, {"chat_id", "message_thread_id", "name", "icon_custom_emoji_id"})
            await self.client(functions.messages.EditForumTopicRequest(await self._peer(payload["chat_id"]), int(payload.get("message_thread_id", 1)), title=payload.get("name"), icon_emoji_id=int(payload["icon_custom_emoji_id"] or 0) if "icon_custom_emoji_id" in payload else None, closed=method == "closeForumTopic" if method in {"closeForumTopic", "reopenForumTopic"} else None, hidden=method == "hideGeneralForumTopic" if method in {"hideGeneralForumTopic", "unhideGeneralForumTopic"} else None))
            return True
        raise UnsupportedMethodError("No MTProto mapping for " + method)

    @staticmethod
    def _reaction_bot(reaction):
        if isinstance(reaction, types.ReactionEmoji):
            return {"type": "emoji", "emoji": reaction.emoticon}
        if isinstance(reaction, types.ReactionCustomEmoji):
            return {"type": "custom_emoji", "custom_emoji_id": str(reaction.document_id)}
        return {"type": "paid"}
