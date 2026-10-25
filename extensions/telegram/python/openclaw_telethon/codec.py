"""Translate Telegram's TL schema to the existing OpenClaw Telegram contract.

Entity offsets are already UTF-16 in both protocols. Never slice/recalculate them
with Python character indexes. IDs which are TL longs outside the Bot API's
numeric peer/message fields travel as strings across the JavaScript boundary.
"""
from __future__ import annotations

import base64
import struct
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from telethon import functions, utils
from telethon.errors import RPCError
from telethon.extensions import BinaryReader
from telethon.tl import types as t

_UPDATE_ENTITIES: ContextVar[dict] = ContextVar("telegram_update_entities", default={})


class UnsupportedCodec(ValueError):
    """A Telegram field cannot be represented without changing its behavior."""

    unsupported_method = True
    error_code = 501


def _base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _zero_encode(value: bytes) -> bytes:
    encoded = bytearray()
    position = 0
    while position < len(value):
        if value[position]:
            encoded.append(value[position])
            position += 1
            continue
        end = position
        while end < len(value) and value[end] == 0 and end - position < 255:
            end += 1
        encoded.extend((0, end - position))
        position = end
    return bytes(encoded)


def _zero_decode(value: bytes) -> bytes:
    decoded = bytearray()
    position = 0
    while position < len(value):
        octet = value[position]
        position += 1
        if octet:
            decoded.append(octet)
        elif position < len(value):
            decoded.extend(bytes(value[position]))
            position += 1
        else:
            raise ValueError("Truncated Telegram file ID")
    return bytes(decoded)


def _tl_bytes(value: bytes) -> bytes:
    size = len(value)
    prefix = bytes([size]) if size < 254 else b"\xfe" + size.to_bytes(3, "little")
    wire = prefix + value
    return wire + bytes(-len(wire) % 4)


def pack_file_id(media: Any, thumbnail_size: str | None = None) -> str:
    """Produce reference-preserving Bot API v4 IDs.

    Telethon's pack_bot_file_id only emits v2 and its Photo implementation
    requires a removed PhotoSize.location field. The v4 layout is documented by
    the independent protocol implementation at
    https://github.com/pyrogram/pyrogram/blob/master/pyrogram/file_id.py .
    Use that wire layout, preserving native file references for MTProto reuse.
    """
    if isinstance(media, t.Photo):
        kind = 2
    elif isinstance(media, t.Document):
        legacy = utils.pack_bot_file_id(media)
        legacy_wire = _zero_decode(base64.urlsafe_b64decode(legacy + "=" * (-len(legacy) % 4)))
        kind = struct.unpack_from("<i", legacy_wire)[0]
    else:
        raise ValueError("Telegram file IDs require a Photo or Document")
    ref = media.file_reference or b""
    wire = struct.pack("<ii", kind | ((1 << 25) if ref else 0), media.dc_id)
    if ref:
        wire += _tl_bytes(ref)
    wire += struct.pack("<qq", media.id, media.access_hash)
    if kind == 2:
        sizes = [s for s in media.sizes if isinstance(s, (t.PhotoSize, t.PhotoCachedSize, t.PhotoSizeProgressive))]
        thumb = thumbnail_size or (sizes[-1].type if sizes else "x")
        wire += struct.pack("<qiiii", 0, 1, 2, ord(thumb), 0)
    else:
        wire += struct.pack("<ii", 30, 4)
    return _base64(_zero_encode(wire + bytes((30, 4))))


def resolve_file_id(file_id: str) -> Any:
    """Read modern Bot API IDs and Telethon's legacy v2 IDs without network I/O."""
    try:
        wire = _zero_decode(base64.urlsafe_b64decode(file_id + "=" * (-len(file_id) % 4)))
        if len(wire) < 24:
            return None
        major = wire[-1]
        if major < 4:
            return utils.resolve_bot_file_id(file_id)
        if major != 4:
            raise UnsupportedCodec(f"Unsupported Telegram file ID version {major}")
        with BinaryReader(wire[:-2]) as reader:
            flags = reader.read_int()
            dc = reader.read_int()
            if flags & (1 << 24):
                raise UnsupportedCodec("Telegram web-location file IDs need their source URL")
            ref = reader.tgread_bytes() if flags & (1 << 25) else b""
            kind = flags & ~((1 << 24) | (1 << 25))
            media_id, access_hash = reader.read_long(), reader.read_long()
            if kind == 2:
                reader.read_long()  # Deprecated volume ID.
                source = reader.read_int()
                thumb = "x"
                if source == 1:
                    reader.read_int()
                    thumb = chr(reader.read_int())
                elif source != 0:
                    raise UnsupportedCodec("Profile/sticker thumbnails cannot be used as whole photos")
                return t.Photo(media_id, access_hash, ref, None, [t.PhotoSize(thumb, 0, 0, 0)], dc)
            if kind not in {3, 4, 5, 8, 9, 10, 13, 17}:
                raise UnsupportedCodec(f"Unsupported Telegram file ID media type {kind}")
            attributes = []
            mime = "application/octet-stream"
            if kind in (3, 9):
                attributes.append(t.DocumentAttributeAudio(0, voice=kind == 3))
                mime = "audio/ogg" if kind == 3 else "audio/mpeg"
            elif kind in (4, 13):
                attributes.append(t.DocumentAttributeVideo(0, 0, 0, round_message=kind == 13))
                mime = "video/mp4"
            elif kind == 8:
                attributes.append(t.DocumentAttributeSticker("", t.InputStickerSetEmpty()))
                mime = "image/webp"
            elif kind == 10:
                attributes.append(t.DocumentAttributeAnimated())
                mime = "video/mp4"
            return t.Document(media_id, access_hash, ref, None, mime, 0, dc, attributes)
    except UnsupportedCodec:
        raise
    except (ValueError, TypeError, struct.error, EOFError, IndexError):
        return None


def _put(result: dict, **values: Any) -> dict:
    result.update({key: value for key, value in values.items() if value is not None})
    return result


def _date_flags(value: dict) -> dict:
    format = value.get("date_time_format", "")
    return {"date": datetime.fromtimestamp(value["unix_time"], timezone.utc),
            "relative": format == "r", "day_of_week": "w" in format,
            "short_date": "d" in format, "long_date": "D" in format,
            "short_time": "t" in format, "long_time": "T" in format}


def _date_fields(value: Any) -> dict:
    format = "r" if value.relative else ("w" if value.day_of_week else "") + ("d" if value.short_date else "D" if value.long_date else "") + ("t" if value.short_time else "T" if value.long_time else "")
    return {"unix_time": timestamp(value.date), "date_time_format": format}


def timestamp(value: datetime | int | None) -> int:
    return int(value.timestamp()) if isinstance(value, datetime) else int(value or 0)


def user_to_bot(user: Any) -> dict:
    result = {"id": user.id, "is_bot": bool(getattr(user, "bot", False)),
              "first_name": getattr(user, "first_name", None) or ""}
    _put(result, last_name=getattr(user, "last_name", None),
         username=getattr(user, "username", None), language_code=getattr(user, "lang_code", None))
    if getattr(user, "premium", False):
        result["is_premium"] = True
    if getattr(user, "bot", False):
        _put(result, can_join_groups=not bool(user.bot_nochats),
             can_read_all_group_messages=bool(user.bot_chat_history),
             supports_inline_queries=user.bot_inline_placeholder is not None,
             can_connect_to_business=bool(user.bot_business),
             has_main_web_app=bool(user.bot_has_main_app),
             has_topics_enabled=bool(user.bot_forum_view),
             allows_users_to_create_topics=bool(user.bot_forum_can_manage_topics),
             can_manage_bots=bool(user.bot_can_manage_bots))
    return result


def chat_to_bot(chat: Any) -> dict:
    if isinstance(chat, (t.User, t.UserEmpty, t.PeerUser)):
        result = {"id": utils.get_peer_id(chat), "type": "private",
                  "first_name": getattr(chat, "first_name", None) or ""}
        return _put(result, last_name=getattr(chat, "last_name", None),
                    username=getattr(chat, "username", None))
    is_channel = isinstance(chat, (t.Channel, t.ChannelForbidden, t.PeerChannel))
    result = {"id": utils.get_peer_id(chat),
              "type": "channel" if is_channel and getattr(chat, "broadcast", False) else
              "supergroup" if is_channel else "group"}
    _put(result, title=getattr(chat, "title", None), username=getattr(chat, "username", None))
    if getattr(chat, "forum", False):
        result["is_forum"] = True
    if getattr(chat, "monoforum", False):
        result["is_direct_messages"] = True
    if getattr(chat, "noforwards", False):
        result["has_protected_content"] = True
    return result


async def resolve_input_peer(client: Any, peer: Any) -> Any:
    try:
        return await client.get_input_entity(peer)
    except ValueError:
        # Bot authorization accepts a zero access hash for numeric peers it can
        # access. A fresh migrated session has not observed every configured
        # chat yet; Telegram still validates membership and permissions.
        if isinstance(peer, str) and peer.lstrip("-").isdigit():
            peer = int(peer)
        if isinstance(peer, int):
            peer_id, kind = utils.resolve_id(peer)
        elif isinstance(peer, (t.PeerUser, t.PeerChat, t.PeerChannel)):
            peer_id, kind = utils.resolve_id(utils.get_peer_id(peer))
        else:
            raise
        if kind is t.PeerUser:
            return t.InputPeerUser(peer_id, 0)
        if kind is t.PeerChannel:
            return t.InputPeerChannel(peer_id, 0)
        return t.InputPeerChat(peer_id)


async def _entity(client: Any, peer: Any) -> Any:
    if peer is None:
        return None
    if isinstance(peer, (t.User, t.UserEmpty, t.Chat, t.ChatForbidden, t.Channel, t.ChannelForbidden)):
        return peer
    entities = _UPDATE_ENTITIES.get()
    if entities and utils.get_peer_id(peer) in entities:
        return entities[utils.get_peer_id(peer)]
    try:
        return await client.get_entity(peer)
    except ValueError:
        return await client.get_entity(await resolve_input_peer(client, peer))


_ENTITIES = {
    "mention": t.MessageEntityMention, "hashtag": t.MessageEntityHashtag,
    "cashtag": t.MessageEntityCashtag, "bot_command": t.MessageEntityBotCommand,
    "url": t.MessageEntityUrl, "email": t.MessageEntityEmail,
    "phone_number": t.MessageEntityPhone, "bold": t.MessageEntityBold,
    "italic": t.MessageEntityItalic, "underline": t.MessageEntityUnderline,
    "strikethrough": t.MessageEntityStrike, "spoiler": t.MessageEntitySpoiler,
    "code": t.MessageEntityCode,
}
_ENTITY_NAMES = {value: key for key, value in _ENTITIES.items()}


def entities_to_mtproto(entities: list[dict] | None) -> list:
    result = []
    for entity in entities or []:
        kind = entity["type"]
        args = {"offset": entity["offset"], "length": entity["length"]}
        if kind in _ENTITIES:
            result.append(_ENTITIES[kind](**args))
        elif kind == "pre":
            result.append(t.MessageEntityPre(**args, language=entity.get("language", "")))
        elif kind == "text_link":
            result.append(t.MessageEntityTextUrl(**args, url=entity["url"]))
        elif kind == "text_mention":
            result.append(t.MessageEntityMentionName(**args, user_id=int(entity["user"]["id"])))
        elif kind == "custom_emoji":
            result.append(t.MessageEntityCustomEmoji(**args, document_id=int(entity["custom_emoji_id"])))
        elif kind in ("blockquote", "expandable_blockquote"):
            result.append(t.MessageEntityBlockquote(**args, collapsed=kind == "expandable_blockquote"))
        elif kind == "date_time":
            result.append(t.MessageEntityFormattedDate(**args, **_date_flags(entity)))
        else:
            raise UnsupportedCodec(f"Unsupported Telegram entity: {kind}")
    return result


def entities_to_bot(entities: list | None) -> list[dict]:
    result = []
    for entity in entities or []:
        kind = _ENTITY_NAMES.get(type(entity))
        item = {"offset": entity.offset, "length": entity.length}
        if kind:
            item["type"] = kind
        elif isinstance(entity, t.MessageEntityPre):
            item.update(type="pre", language=entity.language)
        elif isinstance(entity, t.MessageEntityTextUrl):
            item.update(type="text_link", url=entity.url)
        elif isinstance(entity, (t.MessageEntityMentionName, t.InputMessageEntityMentionName)):
            uid = entity.user_id
            if not isinstance(uid, int):
                uid = uid.user_id
            item.update(type="text_mention", user={"id": uid, "is_bot": False, "first_name": ""})
        elif isinstance(entity, t.MessageEntityCustomEmoji):
            item.update(type="custom_emoji", custom_emoji_id=str(entity.document_id))
        elif isinstance(entity, t.MessageEntityBlockquote):
            item["type"] = "expandable_blockquote" if entity.collapsed else "blockquote"
        elif isinstance(entity, t.MessageEntityFormattedDate):
            item.update(type="date_time", **_date_fields(entity))
        else:
            raise UnsupportedCodec(f"Unsupported incoming entity: {type(entity).__name__}")
        result.append(item)
    return result


def _button_type(button: dict, inline: bool = True) -> Any:
    supported = {"text", "style", "icon_custom_emoji_id", "callback_data", "url", "web_app",
                 "switch_inline_query", "switch_inline_query_current_chat", "switch_inline_query_chosen_chat",
                 "copy_text", "callback_game", "pay", "request_contact", "request_location", "request_poll",
                 "login_url", "disabled"}
    if set(button) - supported:
        raise UnsupportedCodec("Unsupported Telegram button fields: " + ", ".join(sorted(set(button) - supported)))
    actions = set(button) - {"text", "style", "icon_custom_emoji_id"}
    if len(actions) > 1:
        raise ValueError("A Telegram button must have exactly one action")
    if "callback_data" in button:
        data = button["callback_data"]
        encoded = data if isinstance(data, bytes) else data.encode("utf-8")
        if not 1 <= len(encoded) <= 64:
            raise ValueError("Telegram callback_data must contain 1–64 UTF-8 bytes")
        return t.InlineButtonTypeCallback(encoded)
    if "url" in button:
        return t.InlineButtonTypeUrl(button["url"])
    if "web_app" in button:
        constructor = t.InlineButtonTypeWebView if inline else t.ButtonTypeSimpleWebView
        return constructor(button["web_app"]["url"])
    if "switch_inline_query_current_chat" in button:
        return t.InlineButtonTypeSwitchInline(button["switch_inline_query_current_chat"], same_peer=True)
    if "switch_inline_query" in button:
        return t.InlineButtonTypeSwitchInline(button["switch_inline_query"])
    if "switch_inline_query_chosen_chat" in button:
        chosen = button["switch_inline_query_chosen_chat"]
        peers = []
        if chosen.get("allow_user_chats"):
            peers.append(t.InlineQueryPeerTypePM())
        if chosen.get("allow_bot_chats"):
            peers.append(t.InlineQueryPeerTypeBotPM())
        if chosen.get("allow_group_chats"):
            peers.extend([t.InlineQueryPeerTypeChat(), t.InlineQueryPeerTypeMegagroup()])
        if chosen.get("allow_channel_chats"):
            peers.append(t.InlineQueryPeerTypeBroadcast())
        return t.InlineButtonTypeSwitchInline(chosen.get("query", ""), peer_types=peers)
    if "login_url" in button:
        login = button["login_url"]
        if login.get("bot_username"):
            raise UnsupportedCodec("login_url.bot_username requires input user resolution")
        return t.InputInlineButtonTypeUrlAuth(login["url"], request_write_access=login.get("request_write_access"), fwd_text=login.get("forward_text"))
    if "disabled" in button:
        return t.InlineButtonTypeDisabled()
    if "copy_text" in button:
        return t.InlineButtonTypeCopy(button["copy_text"]["text"])
    if button.get("callback_game") is not None:
        return t.InlineButtonTypeGame()
    if button.get("pay"):
        return t.InlineButtonTypeBuy()
    if not inline:
        if button.get("request_contact"):
            return t.ButtonTypeRequestPhone()
        if button.get("request_location"):
            return t.ButtonTypeRequestGeoLocation()
        if "request_poll" in button:
            return t.ButtonTypeRequestPoll(quiz={"quiz": True, "regular": False}.get(button["request_poll"].get("type")))
        if set(button) <= {"text", "style", "icon_custom_emoji_id"}:
            return t.ButtonTypeDefault()
    raise UnsupportedCodec(f"Unsupported Telegram button fields: {', '.join(sorted(button))}")


def _style_to_mtproto(button: dict, rich: bool = False) -> Any:
    style = button.get("style")
    if style is not None and style not in ("primary", "danger", "success", "link"):
        raise UnsupportedCodec(f"Unsupported Telegram button style: {style}")
    if not style and not button.get("icon_custom_emoji_id"):
        return None
    args = {f"bg_{style}": True} if style and style != "link" else {"link": True} if style else {}
    if button.get("icon_custom_emoji_id"):
        if rich:
            raise UnsupportedCodec("Rich button icon_custom_emoji_id has no TL field")
        args["icon"] = int(button["icon_custom_emoji_id"])
    if not rich and style == "link":
        raise UnsupportedCodec("Inline keyboard link style has no TL field")
    return (t.RichButtonStyle if rich else t.KeyboardButtonStyle)(**args)


def markup_to_mtproto(markup: dict | None) -> Any:
    if markup is None:
        return None
    extra = set(markup) - {"inline_keyboard", "keyboard", "remove_keyboard", "force_reply",
                           "selective", "input_field_placeholder", "resize_keyboard", "one_time_keyboard", "is_persistent"}
    if extra:
        raise UnsupportedCodec("Unsupported reply markup fields: " + ", ".join(sorted(extra)))
    if markup.get("remove_keyboard"):
        return t.ReplyKeyboardHide(selective=markup.get("selective"))
    if markup.get("force_reply"):
        return t.ReplyKeyboardForceReply(single_use=True, selective=markup.get("selective"),
                                        placeholder=markup.get("input_field_placeholder"))
    inline = "inline_keyboard" in markup
    rows = markup.get("inline_keyboard" if inline else "keyboard")
    if rows is None:
        raise UnsupportedCodec("Telegram reply markup requires inline_keyboard or keyboard")
    converted = []
    for row in rows:
        buttons = []
        for button in row:
            button = {"text": button} if isinstance(button, str) else button
            buttons.append(t.KeyboardButton(text=button["text"], type=_button_type(button, inline),
                                            style=_style_to_mtproto(button)))
        converted.append(t.KeyboardButtonRow(buttons))
    if inline:
        # MTProto removes inline buttons with an absent markup. Telegram
        # rejects an empty ReplyInlineMarkup, although Bot API accepts [].
        return t.ReplyInlineMarkup(converted) if converted else None
    return t.ReplyKeyboardMarkup(converted, resize=markup.get("resize_keyboard"),
                                 single_use=markup.get("one_time_keyboard"),
                                 persistent=markup.get("is_persistent"), selective=markup.get("selective"),
                                 placeholder=markup.get("input_field_placeholder"))


def _button_to_bot(button: Any, rich: bool = False) -> dict:
    result = {"text": rich_text_to_bot(button.text) if rich else button.text}
    kind = button.type
    if isinstance(kind, t.InlineButtonTypeCallback):
        # Bot API callback data is a UTF-8 string. Invalid UTF-8 cannot be passed
        # through JavaScript without changing its bytes; fail visibly instead.
        result["callback_data"] = kind.data.decode("utf-8")
    elif isinstance(kind, t.InlineButtonTypeUrl):
        result["url"] = kind.url
    elif isinstance(kind, (t.InlineButtonTypeWebView, t.ButtonTypeSimpleWebView)):
        result["web_app"] = {"url": kind.url}
    elif isinstance(kind, t.InlineButtonTypeSwitchInline):
        if kind.peer_types:
            result["switch_inline_query_chosen_chat"] = {"query": kind.query,
                "allow_user_chats": any(isinstance(p, t.InlineQueryPeerTypePM) for p in kind.peer_types),
                "allow_bot_chats": any(isinstance(p, (t.InlineQueryPeerTypeBotPM, t.InlineQueryPeerTypeSameBotPM)) for p in kind.peer_types),
                "allow_group_chats": any(isinstance(p, (t.InlineQueryPeerTypeChat, t.InlineQueryPeerTypeMegagroup)) for p in kind.peer_types),
                "allow_channel_chats": any(isinstance(p, t.InlineQueryPeerTypeBroadcast) for p in kind.peer_types)}
        else:
            result["switch_inline_query_current_chat" if kind.same_peer else "switch_inline_query"] = kind.query
    elif isinstance(kind, (t.InlineButtonTypeUrlAuth, t.InputInlineButtonTypeUrlAuth)):
        result["login_url"] = _put({"url": kind.url}, forward_text=kind.fwd_text,
                                    request_write_access=getattr(kind, "request_write_access", None))
    elif isinstance(kind, t.InlineButtonTypeUserProfile):
        result["url"] = f"tg://user?id={kind.user_id}"
    elif isinstance(kind, t.InlineButtonTypeDisabled):
        result["disabled"] = {}
    elif isinstance(kind, t.InlineButtonTypeCopy):
        result["copy_text"] = {"text": kind.copy_text}
    elif isinstance(kind, t.InlineButtonTypeGame):
        result["callback_game"] = {}
    elif isinstance(kind, t.InlineButtonTypeBuy):
        result["pay"] = True
    elif isinstance(kind, t.ButtonTypeRequestPhone):
        result["request_contact"] = True
    elif isinstance(kind, t.ButtonTypeRequestGeoLocation):
        result["request_location"] = True
    elif isinstance(kind, t.ButtonTypeRequestPoll):
        result["request_poll"] = {} if kind.quiz is None else {"type": "quiz" if kind.quiz else "regular"}
    elif not isinstance(kind, t.ButtonTypeDefault):
        raise UnsupportedCodec(f"Unsupported incoming button: {type(kind).__name__}")
    style = button.style
    if style:
        for name in ("primary", "danger", "success"):
            if getattr(style, f"bg_{name}", False):
                result["style"] = name
        if getattr(style, "link", False):
            result["style"] = "link"
        if getattr(style, "icon", None):
            result["icon_custom_emoji_id"] = str(style.icon)
    return result


def markup_to_bot(markup: Any) -> dict | None:
    if markup is None:
        return None
    if isinstance(markup, t.ReplyInlineMarkup):
        return {"inline_keyboard": [[_button_to_bot(b) for b in row.buttons] for row in markup.rows]}
    if isinstance(markup, t.ReplyKeyboardHide):
        return _put({"remove_keyboard": True}, selective=markup.selective)
    if isinstance(markup, t.ReplyKeyboardForceReply):
        return _put({"force_reply": True}, selective=markup.selective, input_field_placeholder=markup.placeholder)
    if isinstance(markup, t.ReplyKeyboardMarkup):
        return _put({"keyboard": [[_button_to_bot(b) for b in row.buttons] for row in markup.rows]},
                    resize_keyboard=markup.resize, one_time_keyboard=markup.single_use,
                    is_persistent=markup.persistent, selective=markup.selective,
                    input_field_placeholder=markup.placeholder)
    raise UnsupportedCodec(f"Unsupported reply markup: {type(markup).__name__}")


def _file_info(media: Any) -> dict:
    unique_kind = 1 if isinstance(media, t.Photo) else 2
    return {"file_id": pack_file_id(media),
            "file_unique_id": _base64(_zero_encode(struct.pack("<iq", unique_kind, media.id))),
            "file_size": getattr(media, "size", None) or 0}


def photo_to_bot(photo: Any) -> list[dict]:
    result = []
    if not isinstance(photo, t.Photo):
        return result
    for size in photo.sizes:
        if isinstance(size, (t.PhotoSize, t.PhotoCachedSize, t.PhotoSizeProgressive)):
            item = _file_info(photo)
            item["file_id"] = pack_file_id(photo, size.type)
            item.update(width=size.w, height=size.h)
            item["file_size"] = getattr(size, "size", None) or len(getattr(size, "bytes", b"")) or max(getattr(size, "sizes", []) or [0])
            result.append(item)
    return result


def document_to_bot(document: Any) -> tuple[str, dict]:
    item = _file_info(document)
    _put(item, mime_type=document.mime_type)
    kind = "document"
    for attr in document.attributes:
        if isinstance(attr, t.DocumentAttributeFilename):
            item["file_name"] = attr.file_name
        elif isinstance(attr, t.DocumentAttributeImageSize):
            item.update(width=attr.w, height=attr.h)
        elif isinstance(attr, t.DocumentAttributeVideo):
            kind = "video_note" if attr.round_message else "video"
            item.update(width=attr.w, height=attr.h, duration=int(attr.duration))
            if attr.round_message:
                item["length"] = attr.w
        elif isinstance(attr, t.DocumentAttributeAudio):
            kind = "voice" if attr.voice else "audio"
            _put(item, duration=attr.duration, performer=attr.performer, title=attr.title)
        elif isinstance(attr, (t.DocumentAttributeSticker, t.DocumentAttributeCustomEmoji)):
            kind = "sticker"
            item.update(type="custom_emoji" if isinstance(attr, t.DocumentAttributeCustomEmoji) else "regular",
                        is_animated=document.mime_type == "application/x-tgsticker",
                        is_video=document.mime_type == "video/webm", emoji=attr.alt)
            if isinstance(attr, t.DocumentAttributeCustomEmoji):
                item["custom_emoji_id"] = str(document.id)
            if isinstance(attr.stickerset, t.InputStickerSetShortName):
                item["set_name"] = attr.stickerset.short_name
    if kind != "sticker" and any(isinstance(a, t.DocumentAttributeAnimated) for a in document.attributes):
        kind = "animation"
    return kind, item


async def _sticker_set_name(client: Any, document: Any) -> str | None:
    for attribute in document.attributes:
        if not isinstance(attribute, (t.DocumentAttributeSticker, t.DocumentAttributeCustomEmoji)):
            continue
        sticker_set = attribute.stickerset
        if isinstance(sticker_set, t.InputStickerSetShortName):
            return sticker_set.short_name
        if not isinstance(sticker_set, t.InputStickerSetID):
            return None
        # Unlike the Bot API's Sticker, MTProto supplies a set ID/hash instead
        # of its short name. Resolve it once per live client for existing sticker
        # search/descriptions; this cache carries no authority or durable state.
        cache = getattr(client, "_openclaw_sticker_names", None)
        if cache is None:
            cache = {}
            client._openclaw_sticker_names = cache
        if sticker_set.id not in cache:
            try:
                result = await client(functions.messages.GetStickerSetRequest(sticker_set, hash=0))
            except RPCError:
                return None  # Deleted/inaccessible sets have no usable set name.
            if len(cache) >= 256:
                cache.pop(next(iter(cache)))
            cache[sticker_set.id] = result.set.short_name
        return cache[sticker_set.id]
    return None


def poll_to_bot(poll: Any, results: Any = None) -> dict:
    votes = {answer.option: answer for answer in (getattr(results, "results", None) or [])}
    question = poll.question
    item = {"id": str(poll.id), "question": question.text,
            "question_entities": entities_to_bot(question.entities),
            "options": [{"text": answer.text.text, "text_entities": entities_to_bot(answer.text.entities),
                         "voter_count": getattr(votes.get(answer.option), "voters", None) or 0} for answer in poll.answers],
            "total_voter_count": getattr(results, "total_voters", None) or 0,
            "is_closed": bool(poll.closed), "is_anonymous": not bool(poll.public_voters),
            "type": "quiz" if poll.quiz else "regular", "allows_multiple_answers": bool(poll.multiple_choice)}
    correct = [index for index, answer in enumerate(poll.answers) if getattr(votes.get(answer.option), "correct", False)]
    if correct:
        item["correct_option_ids"] = correct
        item["correct_option_id"] = correct[0]
    return _put(item, open_period=poll.close_period,
                close_date=timestamp(poll.close_date) if poll.close_date else None,
                explanation=getattr(results, "solution", None),
                explanation_entities=entities_to_bot(results.solution_entities) if getattr(results, "solution_entities", None) else None)


async def _forward_to_bot(client: Any, forward: Any) -> dict:
    result = {"date": timestamp(forward.date)}
    if forward.from_id is not None:
        origin = await _entity(client, forward.from_id)
        if isinstance(origin, (t.User, t.UserEmpty)):
            result.update(type="user", sender_user=user_to_bot(origin))
        elif forward.channel_post is not None:
            result.update(type="channel", chat=chat_to_bot(origin), message_id=forward.channel_post)
        else:
            result.update(type="chat", sender_chat=chat_to_bot(origin))
    else:
        result.update(type="hidden_user", sender_user_name=forward.from_name or "")
    return _put(result, author_signature=forward.post_author)


async def _service_to_bot(client: Any, message: Any, result: dict) -> None:
    action = message.action
    if isinstance(action, t.MessageActionTopicCreate):
        result["forum_topic_created"] = _put({"name": action.title, "icon_color": action.icon_color},
                                            icon_custom_emoji_id=str(action.icon_emoji_id) if action.icon_emoji_id else None)
        result["message_thread_id"] = message.id
        result["is_topic_message"] = True
    elif isinstance(action, t.MessageActionTopicEdit):
        if action.title is not None or action.icon_emoji_id is not None:
            result["forum_topic_edited"] = _put({}, name=action.title,
                                               icon_custom_emoji_id=str(action.icon_emoji_id) if action.icon_emoji_id is not None else None)
        if action.closed is not None:
            result["forum_topic_closed" if action.closed else "forum_topic_reopened"] = {}
        if action.hidden is not None:
            result["general_forum_topic_hidden" if action.hidden else "general_forum_topic_unhidden"] = {}
    elif isinstance(action, t.MessageActionChatMigrateTo):
        result["migrate_to_chat_id"] = utils.get_peer_id(t.PeerChannel(action.channel_id))
    elif isinstance(action, t.MessageActionChannelMigrateFrom):
        result["migrate_from_chat_id"] = -action.chat_id
    elif isinstance(action, t.MessageActionChatAddUser):
        result["new_chat_members"] = [user_to_bot(await _entity(client, t.PeerUser(uid))) for uid in action.users]
    elif isinstance(action, (t.MessageActionChatJoinedByLink, t.MessageActionChatJoinedByRequest)):
        if "from" in result:
            result["new_chat_members"] = [result["from"]]
    elif isinstance(action, t.MessageActionChatCreate):
        result["group_chat_created"] = True
    elif isinstance(action, t.MessageActionChannelCreate):
        result["supergroup_chat_created" if result["chat"]["type"] == "supergroup" else "channel_chat_created"] = True
    elif isinstance(action, t.MessageActionChatDeleteUser):
        result["left_chat_member"] = user_to_bot(await _entity(client, t.PeerUser(action.user_id)))
    elif isinstance(action, t.MessageActionChatEditTitle):
        result["new_chat_title"] = action.title
    elif isinstance(action, t.MessageActionChatEditPhoto):
        result["new_chat_photo"] = photo_to_bot(action.photo)
    elif isinstance(action, t.MessageActionChatDeletePhoto):
        result["delete_chat_photo"] = True
    elif isinstance(action, t.MessageActionWebViewDataSentMe):
        result["web_app_data"] = {"data": action.data, "button_text": action.text}
    elif isinstance(action, t.MessageActionSetMessagesTTL):
        result["message_auto_delete_timer_changed"] = {"message_auto_delete_time": action.period}


async def message_to_bot(client: Any, message: Any, depth: int = 0) -> dict:
    if isinstance(message, t.MessageEmpty):
        raise ValueError("Telegram returned an unavailable message")
    chat = getattr(message, "chat", None) or await _entity(client, message.peer_id)
    result = {"message_id": message.id, "date": timestamp(message.date), "chat": chat_to_bot(chat)}
    sender_peer = message.from_id
    sender = getattr(message, "sender", None)
    if sender is None and sender_peer is not None:
        sender = await _entity(client, sender_peer)
    if sender is None and result["chat"]["type"] == "private":
        sender = await client.get_me() if message.out else chat
    if isinstance(sender, (t.User, t.UserEmpty)):
        result["from"] = user_to_bot(sender)
    elif sender is not None:
        result["sender_chat"] = chat_to_bot(sender)
    elif result["chat"]["type"] == "channel":
        result["sender_chat"] = result["chat"]
    _put(result, edit_date=timestamp(message.edit_date) if getattr(message, "edit_date", None) else None,
         media_group_id=str(message.grouped_id) if getattr(message, "grouped_id", None) else None,
         author_signature=getattr(message, "post_author", None),
         reply_markup=markup_to_bot(getattr(message, "reply_markup", None)))
    if getattr(message, "noforwards", False):
        result["has_protected_content"] = True
    if getattr(message, "invert_media", False):
        result["show_caption_above_media"] = True
    if getattr(message, "effect", None):
        result["effect_id"] = str(message.effect)
    if getattr(message, "via_bot_id", None):
        result["via_bot"] = user_to_bot(await _entity(client, t.PeerUser(message.via_bot_id)))
    if getattr(message, "via_business_bot_id", None):
        result["sender_business_bot"] = user_to_bot(await _entity(client, t.PeerUser(message.via_business_bot_id)))
    if getattr(message, "fwd_from", None):
        result["forward_origin"] = await _forward_to_bot(client, message.fwd_from)
        result["forward_date"] = result["forward_origin"]["date"]
        if "sender_user" in result["forward_origin"]:
            result["forward_from"] = result["forward_origin"]["sender_user"]
    reply = getattr(message, "reply_to", None)
    saved_peer = getattr(message, "saved_peer_id", None)
    if getattr(chat, "monoforum", False) and saved_peer is not None:
        # MTProto uses saved_peer_id as the subdialog peer in channel DMs.
        # reply_to_peer_id instead identifies a cross-chat replied-to message.
        result["direct_messages_topic"] = {"topic_id": abs(utils.get_peer_id(saved_peer))}
    if isinstance(reply, t.MessageReplyHeader):
        if reply.forum_topic:
            result["is_topic_message"] = True
            result["message_thread_id"] = reply.reply_to_top_id or reply.reply_to_msg_id or 1
        if getattr(chat, "monoforum", False):
            result.pop("message_thread_id", None)
            result.pop("is_topic_message", None)
        if reply.quote_text is not None:
            result["quote"] = _put({"text": reply.quote_text, "position": reply.quote_offset or 0,
                                    "is_manual": bool(reply.quote)}, entities=entities_to_bot(reply.quote_entities))
        if reply.reply_from is not None:
            external = {"origin": await _forward_to_bot(client, reply.reply_from)}
            target = reply.reply_to_peer_id
            if target is not None:
                external["chat"] = chat_to_bot(await _entity(client, target))
            if reply.reply_to_msg_id is not None:
                external["message_id"] = reply.reply_to_msg_id
            if reply.reply_media is not None:
                synthetic = t.Message(id=reply.reply_to_msg_id or 0, peer_id=target or message.peer_id,
                                      date=reply.reply_from.date, media=reply.reply_media)
                encoded_media = await message_to_bot(client, synthetic, depth=1)
                for key, value in encoded_media.items():
                    if key not in {"message_id", "date", "chat", "from", "sender_chat"}:
                        external[key] = value
            result["external_reply"] = external
        if depth < 1 and reply.reply_to_msg_id:
            original = getattr(message, "reply_to_message", None)
            if original is None and hasattr(message, "get_reply_message"):
                try:
                    original = await message.get_reply_message()
                except (RPCError, ValueError):
                    # Telegram may withhold a deleted/inaccessible reply. Its
                    # identifier stays available without fabricating its body.
                    original = None
            if original is not None and not isinstance(original, t.MessageEmpty):
                result["reply_to_message"] = await message_to_bot(client, original, depth + 1)
    media = getattr(message, "media", None)
    caption_media = False
    if isinstance(media, t.MessageMediaPhoto) and isinstance(media.photo, t.Photo):
        result["photo"] = photo_to_bot(media.photo)
        caption_media = True
    elif isinstance(media, t.MessageMediaDocument) and isinstance(media.document, t.Document):
        kind, data = document_to_bot(media.document)
        result[kind] = data
        if kind == "sticker":
            _put(data, set_name=await _sticker_set_name(client, media.document))
        if getattr(media, "video_cover", None):
            data["cover"] = photo_to_bot(media.video_cover)
        if media.video_timestamp is not None:
            data["start_timestamp"] = media.video_timestamp
        caption_media = True
    elif isinstance(media, t.MessageMediaPoll):
        result["poll"] = poll_to_bot(media.poll, media.results)
    elif isinstance(media, (t.MessageMediaGeo, t.MessageMediaGeoLive, t.MessageMediaVenue)):
        location = {"latitude": media.geo.lat, "longitude": media.geo.long}
        _put(location, horizontal_accuracy=getattr(media.geo, "accuracy_radius", None),
             live_period=getattr(media, "period", None), heading=getattr(media, "heading", None),
             proximity_alert_radius=getattr(media, "proximity_notification_radius", None))
        result["location"] = location
        if isinstance(media, t.MessageMediaVenue):
            result["venue"] = {"location": location, "title": media.title, "address": media.address}
            provider = {"foursquare": "foursquare", "gplaces": "google_place"}.get(media.provider)
            if provider:
                result["venue"].update({provider + "_id": media.venue_id, provider + "_type": media.venue_type})
    elif isinstance(media, t.MessageMediaContact):
        result["contact"] = _put({"phone_number": media.phone_number, "first_name": media.first_name,
                                   "last_name": media.last_name}, user_id=media.user_id or None, vcard=media.vcard or None)
    elif isinstance(media, t.MessageMediaDice):
        result["dice"] = {"emoji": media.emoticon, "value": media.value}
    elif isinstance(media, t.MessageMediaWebPage):
        url = getattr(media.webpage, "url", None)
        result["link_preview_options"] = _put({}, url=url, prefer_small_media=media.force_small_media,
                                              prefer_large_media=media.force_large_media,
                                              show_above_text=getattr(message, "invert_media", None))
    if getattr(media, "spoiler", False):
        result["has_media_spoiler"] = True
    text = getattr(message, "message", None)
    if text:
        result["caption" if caption_media else "text"] = text
        entities = entities_to_bot(getattr(message, "entities", None))
        if entities:
            result["caption_entities" if caption_media else "entities"] = entities
    if getattr(message, "rich_message", None):
        result["rich_message"] = rich_to_bot(message.rich_message)
    if getattr(message, "action", None):
        await _service_to_bot(client, message, result)
    return result


def reaction_to_bot(reaction: Any) -> dict:
    if isinstance(reaction, t.ReactionEmoji):
        return {"type": "emoji", "emoji": reaction.emoticon}
    if isinstance(reaction, t.ReactionCustomEmoji):
        return {"type": "custom_emoji", "custom_emoji_id": str(reaction.document_id)}
    if isinstance(reaction, t.ReactionPaid):
        return {"type": "paid"}
    raise UnsupportedCodec(f"Unsupported reaction {type(reaction).__name__}")


def member_to_bot(participant: Any, user: dict) -> dict:
    name = type(participant).__name__
    result = {"user": user, "status": "member"}
    if participant is None or name.endswith("Left"):
        result["status"] = "left"
    elif name.endswith("Creator"):
        result["status"] = "creator"
        result["is_anonymous"] = bool(getattr(getattr(participant, "admin_rights", None), "anonymous", False))
    elif name.endswith("Admin"):
        result.update(status="administrator", can_be_edited=bool(getattr(participant, "can_edit", False)))
        rights = getattr(participant, "admin_rights", None)
        for source, dest in {"anonymous": "is_anonymous", "change_info": "can_change_info",
                             "post_messages": "can_post_messages", "edit_messages": "can_edit_messages",
                             "delete_messages": "can_delete_messages", "ban_users": "can_restrict_members",
                             "invite_users": "can_invite_users", "pin_messages": "can_pin_messages",
                             "add_admins": "can_promote_members", "manage_topics": "can_manage_topics",
                             "manage_call": "can_manage_video_chats", "other": "can_manage_chat",
                             "post_stories": "can_post_stories", "edit_stories": "can_edit_stories",
                             "delete_stories": "can_delete_stories", "manage_direct_messages": "can_manage_direct_messages"}.items():
            result[dest] = bool(getattr(rights, source, False))
    elif name.endswith("Banned"):
        rights = participant.banned_rights
        result.update(status="kicked" if rights.view_messages else "restricted",
                      until_date=timestamp(rights.until_date))
        if not rights.view_messages:
            result["is_member"] = not bool(participant.left)
            for source, dest in {"send_messages": "can_send_messages", "send_audios": "can_send_audios",
                                 "send_docs": "can_send_documents", "send_photos": "can_send_photos",
                                 "send_videos": "can_send_videos", "send_roundvideos": "can_send_video_notes",
                                 "send_voices": "can_send_voice_notes", "send_polls": "can_send_polls",
                                 "send_stickers": "can_send_other_messages", "embed_links": "can_add_web_page_previews",
                                 "change_info": "can_change_info", "invite_users": "can_invite_users",
                                 "pin_messages": "can_pin_messages", "manage_topics": "can_manage_topics"}.items():
                result[dest] = not bool(getattr(rights, source, False))
    _put(result, custom_title=getattr(participant, "rank", None))
    return result


async def _business_message_to_bot(client: Any, update: Any) -> dict:
    original = getattr(update, "reply_to_message", None)
    if original is not None:
        # Business history is scoped to its connection. Use Telegram's supplied
        # reply before ordinary message conversion can try a bot-history lookup.
        update.message.reply_to_message = original
    message = await message_to_bot(client, update.message)
    message["business_connection_id"] = update.connection_id
    if original is not None and "reply_to_message" not in message:
        message["reply_to_message"] = await message_to_bot(client, original, 1)
    return message


async def raw_update_to_bot(client: Any, update: Any) -> dict | list[dict] | None:
    # Keep receipted metadata available after channel access changes, scoped to this update.
    scope = _UPDATE_ENTITIES.set(getattr(update, "_entities", None) or {})
    try:
        return await _raw_update_to_bot(client, update)
    finally:
        _UPDATE_ENTITIES.reset(scope)


async def _raw_update_to_bot(client: Any, update: Any) -> dict | list[dict] | None:
    if isinstance(update, (t.UpdateShortMessage, t.UpdateShortChatMessage)):
        if update.out:
            return None
        group = isinstance(update, t.UpdateShortChatMessage)
        message = t.Message(
            id=update.id, peer_id=t.PeerChat(update.chat_id) if group else t.PeerUser(update.user_id),
            from_id=t.PeerUser(update.from_id if group else update.user_id),
            date=update.date, message=update.message, mentioned=update.mentioned,
            media_unread=update.media_unread, silent=update.silent, fwd_from=update.fwd_from,
            via_bot_id=update.via_bot_id, reply_to=update.reply_to, entities=update.entities,
            ttl_period=update.ttl_period,
        )
        message._finish_init(client, getattr(update, "_entities", {}), None)
        return {"message": await message_to_bot(client, message)}
    raw_message = getattr(update, "message", None)
    if isinstance(raw_message, (t.Message, t.MessageService)):
        # Journal replay uses _preprocess_updates but deliberately bypasses
        # Telethon's _dispatch_update, where this initialization normally lives.
        raw_message._finish_init(client, getattr(update, "_entities", {}), None)
    if isinstance(update, (t.UpdateNewMessage, t.UpdateNewChannelMessage, t.UpdateEditMessage, t.UpdateEditChannelMessage)):
        # Deleted/inaccessible placeholders have no message body to deliver.
        if isinstance(update.message, t.MessageEmpty):
            return None
        # Outbound MTProto updates must not create inbound agent turns.
        if getattr(update.message, "out", False):
            return None
        message = await message_to_bot(client, update.message)
        channel = message["chat"]["type"] == "channel"
        edited = isinstance(update, (t.UpdateEditMessage, t.UpdateEditChannelMessage))
        key = ("edited_" if edited else "") + ("channel_post" if channel else "message")
        return {key: message}
    if isinstance(update, (t.UpdateBotNewBusinessMessage, t.UpdateBotEditBusinessMessage)):
        message = await _business_message_to_bot(client, update)
        return {"edited_business_message" if isinstance(update, t.UpdateBotEditBusinessMessage) else "business_message": message}
    if isinstance(update, t.UpdateBotDeleteBusinessMessage):
        return {"deleted_business_messages": {"business_connection_id": update.connection_id,
                                              "chat": chat_to_bot(await _entity(client, update.peer)),
                                              "message_ids": update.messages}}
    if isinstance(update, (t.UpdateBotCallbackQuery, t.UpdateInlineBotCallbackQuery, t.UpdateBusinessBotCallbackQuery)):
        callback = {"id": str(update.query_id), "from": user_to_bot(await _entity(client, t.PeerUser(update.user_id))),
                    "chat_instance": str(update.chat_instance)}
        if update.data is not None:
            callback["data"] = update.data.decode("utf-8")
        _put(callback, game_short_name=getattr(update, "game_short_name", None))
        if isinstance(update, t.UpdateBusinessBotCallbackQuery):
            callback["message"] = await _business_message_to_bot(client, update)
        elif isinstance(update, t.UpdateBotCallbackQuery):
            message = await client.get_messages(update.peer, ids=update.msg_id)
            if message is not None and not isinstance(message, t.MessageEmpty):
                callback["message"] = await message_to_bot(client, message)
            else:
                callback["message"] = {"message_id": update.msg_id, "date": 0,
                                       "chat": chat_to_bot(await _entity(client, update.peer))}
        else:
            # This is a Telegram opaque binary ID, not its object repr.
            import struct
            iid = update.msg_id
            if isinstance(iid, t.InputBotInlineMessageID):
                wire = struct.pack("<iqq", iid.dc_id, iid.id, iid.access_hash)
            else:
                wire = struct.pack("<iqiq", iid.dc_id, iid.owner_id, iid.id, iid.access_hash)
            callback["inline_message_id"] = base64.urlsafe_b64encode(wire).decode().rstrip("=")
        return {"callback_query": callback}
    if isinstance(update, t.UpdateBotMessageReaction):
        actor = await _entity(client, update.actor)
        result = {"chat": chat_to_bot(await _entity(client, update.peer)), "message_id": update.msg_id,
                  "date": timestamp(update.date), "old_reaction": [reaction_to_bot(r) for r in update.old_reactions],
                  "new_reaction": [reaction_to_bot(r) for r in update.new_reactions]}
        result["user" if isinstance(actor, (t.User, t.UserEmpty)) else "actor_chat"] = user_to_bot(actor) if isinstance(actor, (t.User, t.UserEmpty)) else chat_to_bot(actor)
        return {"message_reaction": result}
    if isinstance(update, t.UpdateBotMessageReactions):
        return {"message_reaction_count": {"chat": chat_to_bot(await _entity(client, update.peer)),
                                            "message_id": update.msg_id, "date": timestamp(update.date),
                                            "reactions": [{"type": reaction_to_bot(r.reaction), "total_count": r.count} for r in update.reactions]}}
    if isinstance(update, t.UpdateMessagePoll):
        if update.poll is None:
            # Result-only updates lack the question/options. The service can
            # replay its prior full poll through this function after merging.
            raise UnsupportedCodec("Poll result update requires its prior poll definition")
        return {"poll": poll_to_bot(update.poll, update.results)}
    if isinstance(update, t.UpdateMessagePollVote):
        voter = await _entity(client, update.peer)
        result = {"poll_id": str(update.poll_id), "option_ids": list(update.positions)}
        result["user" if isinstance(voter, (t.User, t.UserEmpty)) else "voter_chat"] = user_to_bot(voter) if isinstance(voter, (t.User, t.UserEmpty)) else chat_to_bot(voter)
        return {"poll_answer": result}
    if isinstance(update, (t.UpdateChannelParticipant, t.UpdateChatParticipant)):
        peer = t.PeerChannel(update.channel_id) if isinstance(update, t.UpdateChannelParticipant) else t.PeerChat(update.chat_id)
        user = user_to_bot(await _entity(client, t.PeerUser(update.user_id)))
        result = {"chat": chat_to_bot(await _entity(client, peer)),
                  "from": user_to_bot(await _entity(client, t.PeerUser(update.actor_id))),
                  "date": timestamp(update.date), "old_chat_member": member_to_bot(update.prev_participant, user),
                  "new_chat_member": member_to_bot(update.new_participant, user)}
        me = await client.get_me()
        return {"my_chat_member" if update.user_id == me.id else "chat_member": result}
    if isinstance(update, t.UpdateBotStopped):
        user = await _entity(client, t.PeerUser(update.user_id))
        bot = user_to_bot(await client.get_me())
        return {"my_chat_member": {"chat": chat_to_bot(user), "from": user_to_bot(user),
                                    "date": timestamp(update.date),
                                    "old_chat_member": {"user": bot, "status": "member" if update.stopped else "kicked"},
                                    "new_chat_member": {"user": bot, "status": "kicked" if update.stopped else "member"}}}
    return None


_RICH_WRAPPERS = {"bold": t.TextBold, "italic": t.TextItalic, "underline": t.TextUnderline,
                  "strikethrough": t.TextStrike, "code": t.TextFixed, "spoiler": t.TextSpoiler,
                  "marked": t.TextMarked, "subscript": t.TextSubscript, "superscript": t.TextSuperscript,
                  "mention": t.TextMention, "hashtag": t.TextHashtag, "cashtag": t.TextCashtag,
                  "bot_command": t.TextBotCommand}


def rich_text_to_mtproto(value: Any) -> Any:
    if value is None:
        return t.TextEmpty()
    if isinstance(value, str):
        return t.TextPlain(value)
    if isinstance(value, list):
        return t.TextConcat([rich_text_to_mtproto(v) for v in value])
    kind = value["type"]
    if kind in _RICH_WRAPPERS:
        return _RICH_WRAPPERS[kind](rich_text_to_mtproto(value["text"]))
    if kind == "url":
        return t.TextUrl(rich_text_to_mtproto(value["text"]), value["url"], 0)
    if kind == "text_mention":
        return t.TextMentionName(rich_text_to_mtproto(value["text"]), int(value["user"]["id"]))
    if kind == "anchor_link":
        return t.TextUrl(rich_text_to_mtproto(value["text"]), "#" + value["anchor_name"], 0)
    if kind == "anchor":
        return t.TextAnchor(rich_text_to_mtproto(value.get("text", "")), value["name"])
    if kind == "reference":
        return t.TextAnchor(rich_text_to_mtproto(value["text"]), value["name"])
    if kind == "reference_link":
        return t.TextUrl(rich_text_to_mtproto(value["text"]), "#" + value["reference_name"], 0)
    if kind == "date_time":
        return t.TextDate(rich_text_to_mtproto(value["text"]), **_date_flags(value))
    if kind == "email_address":
        return t.TextEmail(rich_text_to_mtproto(value["text"]), value["email_address"])
    if kind == "phone_number":
        return t.TextPhone(rich_text_to_mtproto(value["text"]), value["phone_number"])
    if kind == "bank_card_number":
        return t.TextBankCard(rich_text_to_mtproto(value["text"]))
    if kind == "custom_emoji":
        return t.TextCustomEmoji(int(value["custom_emoji_id"]), value["alternative_text"])
    if kind == "mathematical_expression":
        return t.TextMath(value["expression"])
    if kind == "button":
        b = value["button"]
        return t.TextButton(rich_text_to_mtproto(b["text"]), _button_type(b), _style_to_mtproto(b, True))
    raise UnsupportedCodec(f"Unsupported rich inline type: {kind}")


def rich_text_to_bot(value: Any) -> Any:
    if isinstance(value, t.TextEmpty):
        return ""
    if isinstance(value, t.TextPlain):
        return value.text
    if isinstance(value, t.TextConcat):
        return [rich_text_to_bot(v) for v in value.texts]
    for kind, constructor in _RICH_WRAPPERS.items():
        if isinstance(value, constructor):
            item = {"type": kind, "text": rich_text_to_bot(value.text)}
            field = {"mention": "username", "hashtag": "hashtag", "cashtag": "cashtag", "bot_command": "bot_command"}.get(kind)
            if field:
                plain = _rich_plain(value.text)
                item[field] = plain.lstrip("@#$") if kind != "bot_command" else plain
            return item
    if isinstance(value, t.TextUrl):
        if value.url.startswith("#"):
            return {"type": "anchor_link", "text": rich_text_to_bot(value.text), "anchor_name": value.url[1:]}
        return {"type": "url", "text": rich_text_to_bot(value.text), "url": value.url}
    if isinstance(value, t.TextMentionName):
        return {"type": "text_mention", "text": rich_text_to_bot(value.text),
                "user": {"id": value.user_id, "first_name": "", "is_bot": False}}
    if isinstance(value, t.TextCustomEmoji):
        return {"type": "custom_emoji", "custom_emoji_id": str(value.document_id), "alternative_text": value.alt}
    if isinstance(value, t.TextMath):
        return {"type": "mathematical_expression", "expression": value.source}
    if isinstance(value, t.TextAnchor):
        if _rich_plain(value.text):
            return {"type": "reference", "name": value.name, "text": rich_text_to_bot(value.text)}
        return {"type": "anchor", "name": value.name}
    if isinstance(value, t.TextDate):
        return {"type": "date_time", "text": rich_text_to_bot(value.text), **_date_fields(value)}
    if isinstance(value, t.TextButton):
        return {"type": "button", "button": _button_to_bot(value, True)}
    for constructor, kind, field, raw_field in [(t.TextEmail, "email_address", "email_address", "email"), (t.TextPhone, "phone_number", "phone_number", "phone")]:
        if isinstance(value, constructor):
            return {"type": kind, "text": rich_text_to_bot(value.text), field: getattr(value, raw_field)}
    if isinstance(value, (t.TextAutoEmail, t.TextAutoPhone, t.TextAutoUrl, t.TextBankCard)):
        kind, field = {t.TextAutoEmail: ("email_address", "email_address"), t.TextAutoPhone: ("phone_number", "phone_number"),
                       t.TextAutoUrl: ("url", "url"), t.TextBankCard: ("bank_card_number", "bank_card_number")}[type(value)]
        return {"type": kind, "text": rich_text_to_bot(value.text), field: _rich_plain(value.text)}
    raise UnsupportedCodec(f"Unsupported incoming rich inline type: {type(value).__name__}")


def _rich_plain(value: Any) -> str:
    if isinstance(value, t.TextPlain):
        return value.text
    if isinstance(value, t.TextConcat):
        return "".join(_rich_plain(part) for part in value.texts)
    if isinstance(value, t.TextEmpty):
        return ""
    if isinstance(value, t.TextCustomEmoji):
        return value.alt
    return _rich_plain(value.text) if hasattr(value, "text") else ""


def _caption_to_mtproto(value: dict | None) -> Any:
    value = value or {}
    return t.PageCaption(rich_text_to_mtproto(value.get("text", "")), rich_text_to_mtproto(value.get("credit", "")))


def _caption_to_bot(value: Any) -> dict:
    return {"text": rich_text_to_bot(value.text), "credit": rich_text_to_bot(value.credit)}


def rich_block_to_mtproto(block: dict) -> Any:
    kind = block["type"]
    fields = {
        "paragraph": {"text"}, "footer": {"text"}, "thinking": {"text"},
        "heading": {"text", "size"}, "pre": {"text", "language"}, "divider": set(),
        "anchor": {"name"}, "mathematical_expression": {"expression"},
        "blockquote": {"blocks", "credit"}, "expandable_blockquote": {"text", "credit"},
        "pullquote": {"text", "credit"}, "details": {"blocks", "summary", "is_open"},
        "collage": {"blocks", "caption"}, "slideshow": {"blocks", "caption"},
        "list": {"items"}, "table": {"cells", "caption", "is_bordered", "is_striped", "is_compact"},
        "map": {"location", "zoom", "width", "height", "caption"}, "buttons": {"buttons", "align"},
    }
    allowed = fields.get(kind, {kind, "caption", "has_spoiler"} if kind in ("photo", "video", "audio", "animation", "voice_note", "document") else set())
    extra = set(block) - allowed - {"type"}
    if extra:
        raise UnsupportedCodec("Unsupported rich block fields: " + ", ".join(sorted(extra)))
    text = lambda key="text": rich_text_to_mtproto(block.get(key, ""))
    children = lambda: [rich_block_to_mtproto(b) for b in block["blocks"]]
    simple = {"paragraph": t.PageBlockParagraph, "footer": t.PageBlockFooter, "thinking": t.PageBlockThinking}
    if kind in simple:
        return simple[kind](text())
    if kind == "heading":
        return getattr(t, f"PageBlockHeading{block['size']}")(text())
    if kind == "pre":
        return t.PageBlockPreformatted(text(), block.get("language", ""))
    if kind == "divider":
        return t.PageBlockDivider()
    if kind == "anchor":
        return t.PageBlockAnchor(block["name"])
    if kind == "mathematical_expression":
        return t.PageBlockMath(block["expression"])
    if kind == "blockquote":
        return t.PageBlockBlockquoteBlocks(children(), text("credit"))
    if kind == "expandable_blockquote":
        return t.PageBlockBlockquote(text(), text("credit"), collapsed=True)
    if kind == "pullquote":
        return t.PageBlockPullquote(text(), text("credit"))
    if kind == "details":
        return t.PageBlockDetails(children(), text("summary"), open=block.get("is_open"))
    if kind in ("collage", "slideshow"):
        return (t.PageBlockCollage if kind == "collage" else t.PageBlockSlideshow)(children(), _caption_to_mtproto(block.get("caption")))
    if kind == "list":
        ordered = any("value" in item or "type" in item for item in block["items"])
        constructor = t.PageListOrderedItemBlocks if ordered else t.PageListItemBlocks
        items = []
        for item in block["items"]:
            options = {"checkbox": item.get("has_checkbox"), "checked": item.get("is_checked")}
            if ordered:
                options.update(value=item.get("value"), type=item.get("type"))
            items.append(constructor([rich_block_to_mtproto(b) for b in item["blocks"]], **options))
        return t.PageBlockOrderedList(items) if ordered else t.PageBlockList(items)
    if kind == "table":
        rows = []
        for row in block["cells"]:
            cells = []
            for cell in row:
                cells.append(t.PageTableCell(text=rich_text_to_mtproto(cell.get("text", "")),
                                             header=cell.get("is_header"), colspan=cell.get("colspan"), rowspan=cell.get("rowspan"),
                                             align_center=cell.get("align") == "center", align_right=cell.get("align") == "right",
                                             valign_middle=cell.get("valign") == "middle", valign_bottom=cell.get("valign") == "bottom"))
            rows.append(t.PageTableRow(cells))
        return t.PageBlockTable(text("caption"), rows, bordered=block.get("is_bordered"), striped=block.get("is_striped"), compact=block.get("is_compact"))
    if kind == "map":
        location = block["location"]
        return t.PageBlockMap(t.GeoPoint(long=location["longitude"], lat=location["latitude"], access_hash=0,
                                        accuracy_radius=location.get("horizontal_accuracy")),
                              block["zoom"], block["width"], block["height"], _caption_to_mtproto(block.get("caption")))
    if kind == "buttons":
        return t.PageBlockButtonRow([t.PageButton(rich_text_to_mtproto(b["text"]), _button_type(b), _style_to_mtproto(b, True)) for b in block["buttons"]],
                                    align_left=block.get("align") == "left", align_center=block.get("align") == "center", align_right=block.get("align") == "right")
    if kind in ("photo", "video", "audio", "animation", "voice_note", "document"):
        media = block[kind]
        file_id = media.get("file_id") or media.get("media")
        resolved = resolve_file_id(file_id) if file_id else None
        if resolved is None:
            raise UnsupportedCodec("Rich media URL/upload must be resolved before TL block conversion")
        constructors = {"photo": t.PageBlockPhoto, "video": t.PageBlockVideo, "animation": t.PageBlockVideo,
                        "audio": t.PageBlockAudio, "voice_note": t.PageBlockAudio, "document": t.PageBlockDocument}
        args = {}
        if kind in ("photo", "video", "animation"):
            args["spoiler"] = media.get("has_spoiler", block.get("has_spoiler"))
        if kind == "animation":
            args.update(autoplay=True, loop=True)
        return constructors[kind](resolved.id, _caption_to_mtproto(block.get("caption")), **args)
    raise UnsupportedCodec(f"Unsupported rich block: {kind}")


def rich_to_mtproto(value: dict) -> Any:
    extra = set(value) - {"blocks", "html", "markdown", "media", "is_rtl", "skip_entity_detection"}
    if extra:
        raise UnsupportedCodec("Unsupported rich message fields: " + ", ".join(sorted(extra)))
    modes = [name for name in ("blocks", "html", "markdown") if name in value]
    if len(modes) != 1:
        raise ValueError("Exactly one of blocks, html, markdown is required for a rich message")
    if modes[0] != "blocks":
        if value.get("media"):
            raise UnsupportedCodec("Rich HTML/Markdown attachment maps must be resolved before conversion")
        constructor = t.InputRichMessageHTML if modes[0] == "html" else t.InputRichMessageMarkdown
        return constructor(value[modes[0]], rtl=value.get("is_rtl"), noautolink=value.get("skip_entity_detection"))
    if value.get("media"):
        raise UnsupportedCodec("Rich blocks cannot use an HTML/Markdown attachment map")
    photos, documents = {}, {}
    def collect(blocks: list[dict]) -> None:
        for block in blocks:
            kind = block["type"]
            if kind in ("photo", "video", "audio", "animation", "voice_note", "document"):
                media = block[kind]
                obj = resolve_file_id(media.get("file_id") or media.get("media") or "")
                if isinstance(obj, t.Photo):
                    photos[obj.id] = utils.get_input_photo(obj)
                elif isinstance(obj, t.Document):
                    documents[obj.id] = utils.get_input_document(obj)
            collect(block.get("blocks", []))
            if kind == "list":
                for item in block["items"]:
                    collect(item["blocks"])
    collect(value["blocks"])
    return t.InputRichMessage([rich_block_to_mtproto(b) for b in value["blocks"]],
                              rtl=value.get("is_rtl"), noautolink=value.get("skip_entity_detection"),
                              photos=list(photos.values()) or None, documents=list(documents.values()) or None)


def rich_block_to_bot(block: Any, media: dict[int, Any]) -> dict:
    name = type(block).__name__
    simple = {"PageBlockParagraph": "paragraph", "PageBlockFooter": "footer", "PageBlockThinking": "thinking"}
    if name in simple:
        return {"type": simple[name], "text": rich_text_to_bot(block.text)}
    if name.startswith("PageBlockHeading"):
        return {"type": "heading", "size": int(name[-1]), "text": rich_text_to_bot(block.text)}
    if isinstance(block, (t.PageBlockTitle, t.PageBlockHeader, t.PageBlockSubtitle, t.PageBlockSubheader, t.PageBlockKicker)):
        return {"type": "heading", "size": 1 if isinstance(block, (t.PageBlockTitle, t.PageBlockHeader)) else 2, "text": rich_text_to_bot(block.text)}
    if isinstance(block, t.PageBlockPreformatted):
        return {"type": "pre", "text": rich_text_to_bot(block.text), "language": block.language}
    if isinstance(block, t.PageBlockDivider):
        return {"type": "divider"}
    if isinstance(block, t.PageBlockAnchor):
        return {"type": "anchor", "name": block.name}
    if isinstance(block, t.PageBlockMath):
        return {"type": "mathematical_expression", "expression": block.source}
    if isinstance(block, t.PageBlockBlockquoteBlocks):
        return {"type": "blockquote", "blocks": [rich_block_to_bot(b, media) for b in block.blocks], "credit": rich_text_to_bot(block.caption)}
    if isinstance(block, (t.PageBlockBlockquote, t.PageBlockPullquote)):
        return {"type": "pullquote" if isinstance(block, t.PageBlockPullquote) else "expandable_blockquote",
                "text": rich_text_to_bot(block.text), "credit": rich_text_to_bot(block.caption)}
    if isinstance(block, t.PageBlockDetails):
        return {"type": "details", "summary": rich_text_to_bot(block.title), "is_open": bool(block.open),
                "blocks": [rich_block_to_bot(b, media) for b in block.blocks]}
    if isinstance(block, (t.PageBlockCollage, t.PageBlockSlideshow)):
        return {"type": "collage" if isinstance(block, t.PageBlockCollage) else "slideshow",
                "blocks": [rich_block_to_bot(b, media) for b in block.items], "caption": _caption_to_bot(block.caption)}
    if isinstance(block, (t.PageBlockList, t.PageBlockOrderedList)):
        items = []
        for index, item in enumerate(block.items):
            children = [rich_block_to_bot(b, media) for b in item.blocks] if hasattr(item, "blocks") else [{"type": "paragraph", "text": rich_text_to_bot(item.text)}]
            label = getattr(item, "num", None)
            if label is None:
                label = str(getattr(item, "value", None) or index + (block.start or 1)) + "." if isinstance(block, t.PageBlockOrderedList) else "•"
            items.append(_put({"blocks": children, "label": label}, has_checkbox=item.checkbox, is_checked=item.checked,
                              value=getattr(item, "value", None), type=getattr(item, "type", None)))
        return {"type": "list", "items": items}
    if isinstance(block, t.PageBlockTable):
        return _put({"type": "table", "caption": rich_text_to_bot(block.title),
                     "cells": [[_put({"text": rich_text_to_bot(c.text) if c.text else "",
                                       "align": "center" if c.align_center else "right" if c.align_right else "left",
                                       "valign": "middle" if c.valign_middle else "bottom" if c.valign_bottom else "top"},
                                      is_header=c.header, colspan=c.colspan, rowspan=c.rowspan) for c in row.cells] for row in block.rows]},
                    is_bordered=block.bordered, is_striped=block.striped, is_compact=block.compact)
    if isinstance(block, t.PageBlockMap):
        return {"type": "map", "location": _put({"latitude": block.geo.lat, "longitude": block.geo.long}, horizontal_accuracy=block.geo.accuracy_radius),
                "zoom": block.zoom, "width": block.w, "height": block.h, "caption": _caption_to_bot(block.caption)}
    if isinstance(block, t.PageBlockButtonRow):
        return _put({"type": "buttons", "buttons": [_button_to_bot(b, True) for b in block.buttons]},
                    align="left" if block.align_left else "center" if block.align_center else "right" if block.align_right else None)
    field = {t.PageBlockPhoto: "photo_id", t.PageBlockVideo: "video_id", t.PageBlockAudio: "audio_id", t.PageBlockDocument: "document_id"}.get(type(block))
    if field:
        obj = media.get(getattr(block, field))
        if obj is None:
            raise UnsupportedCodec("Rich message omitted a referenced media object")
        if isinstance(obj, t.Photo):
            kind, data = "photo", photo_to_bot(obj)
        else:
            kind, data = document_to_bot(obj)
            if kind == "voice":
                kind = "voice_note"
        return _put({"type": kind, kind: data, "caption": _caption_to_bot(block.caption)}, has_spoiler=getattr(block, "spoiler", None))
    raise UnsupportedCodec(f"Unsupported incoming rich block: {name}")


def rich_to_bot(value: Any) -> dict:
    media = {obj.id: obj for obj in (getattr(value, "photos", None) or []) + (getattr(value, "documents", None) or [])}
    return _put({"blocks": [rich_block_to_bot(block, media) for block in value.blocks]}, is_rtl=value.rtl)
