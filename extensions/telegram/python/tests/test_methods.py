"""Request-boundary checks; no Telegram network or real chat effects."""
import unittest
import base64
import struct
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import TelegramClient, errors, functions, types, utils
from telethon.sessions import MemorySession
from telethon.tl.custom.participantpermissions import ParticipantPermissions

from openclaw_telethon.methods import MtprotoMethods, UnsupportedMethodError
from openclaw_telethon import codec


class BoundaryClient(TelegramClient):
    def __init__(self):
        super().__init__(MemorySession(), 12345, "0" * 32)
        self.requests = []
        self.entity = types.User(7, first_name="Test", access_hash=9)
        self.response = None
        self.uploads = []
        self.reject_reply_once = False

    async def get_input_entity(self, entity):
        if isinstance(entity, (types.InputPeerUser, types.InputPeerChannel)):
            return entity
        if int(entity) < -1000000000000:
            return types.InputPeerChannel(-int(entity) - 1000000000000, 9)
        return types.InputPeerUser(int(entity), 9)

    async def get_entity(self, entity):
        return self.entity

    async def get_me(self):
        return self.entity

    async def __call__(self, request, *args, **kwargs):
        # Serialize actual Telethon TL constructors, catching schema mismatches
        # that a permissive mocked request object would otherwise conceal.
        bytes(request)
        self.requests.append(request)
        if self.reject_reply_once:
            self.reject_reply_once = False
            raise errors.RPCError(request, "REPLY_MESSAGE_ID_INVALID", 400)
        if self.response is not None:
            return self.response
        inner = request.query if isinstance(request, functions.InvokeWithBusinessConnectionRequest) else request
        if isinstance(inner, functions.messages.UploadMediaRequest):
            if isinstance(inner.media, (types.InputMediaPhotoExternal, types.InputMediaUploadedPhoto)):
                return types.MessageMediaPhoto(photo=types.Photo(123, 456, b"photo-reference", None, [types.PhotoSize("x", 10, 10, 100)], 2))
            return types.MessageMediaDocument(document=types.Document(789, 456, b"document-reference", None, "application/pdf", 22, 2, []))
        if isinstance(inner, functions.messages.SendMultiMediaRequest):
            return types.Updates([types.UpdateNewMessage(types.Message(id=51 + index, peer_id=utils.get_peer(inner.peer), message=item.message, date=datetime.now(timezone.utc), out=True), 1, 1) for index, item in enumerate(inner.multi_media)], [self.entity], [], datetime.now(timezone.utc), 1)
        if isinstance(inner, (functions.messages.SendMessageRequest, functions.messages.SendMediaRequest, functions.messages.EditMessageRequest)):
            return types.Updates([types.UpdateNewMessage(types.Message(
                id=getattr(inner, "id", 51), peer_id=utils.get_peer(inner.peer),
                message=getattr(inner, "message", ""), date=datetime.now(timezone.utc),
                out=True, entities=getattr(inner, "entities", None),
                media=types.MessageMediaPoll(poll=inner.media.poll, results=types.PollResults()) if isinstance(getattr(inner, "media", None), types.InputMediaPoll) else None,
            ), 1, 1)], [self.entity], [], datetime.now(timezone.utc), 1)
        return True

    async def upload_file(self, file, **kwargs):
        self.uploads.append((file, kwargs))
        return types.InputFile(123, 1, kwargs.get("file_name") or "file.bin", "0000")

    async def _file_to_media(self, file, **kwargs):
        self.media_options = kwargs
        if isinstance(file, types.InputFile):
            media = types.InputMediaUploadedPhoto(file) if kwargs.get("as_image") else types.InputMediaUploadedDocument(file, kwargs.get("mime_type") or "application/octet-stream", kwargs.get("attributes") or [])
        elif isinstance(file, str) and file.startswith(("https://", "http://")):
            media = types.InputMediaPhotoExternal(file) if kwargs.get("as_image") else types.InputMediaDocumentExternal(file)
        else:
            media = utils.get_input_media(file)
        return file, media, False


class MethodsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = BoundaryClient()
        self.methods = MtprotoMethods(self.client)
        for connection_id in ("connection", "business"):
            self.methods.remember_business_connection(types.BotBusinessConnection(connection_id, 7, self.client.session.dc_id, None))

    async def test_unpin_latest_uses_bot_allowed_full_metadata_without_search(self):
        date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        entities = (
            (types.User(7, first_name="Fixture", access_hash=9), functions.users.GetFullUserRequest),
            (types.Chat(55, "Fixture", types.ChatPhotoEmpty(), 2, date, 1), functions.messages.GetFullChatRequest),
            (types.Channel(66, "Fixture", types.ChatPhotoEmpty(), date, megagroup=True, access_hash=9), functions.channels.GetFullChannelRequest),
        )
        for entity, request_type in entities:
            for pinned_id in (93, None):
                with self.subTest(peer=type(entity).__name__, pinned_id=pinned_id):
                    client = BoundaryClient()
                    client.entity = entity
                    client.get_input_entity = AsyncMock(return_value=utils.get_input_peer(entity))
                    client.get_messages = AsyncMock(side_effect=AssertionError("Bot credentials cannot search/history"))
                    full = SimpleNamespace(pinned_msg_id=pinned_id)
                    client.response = SimpleNamespace(full_chat=full, full_user=full)
                    result = await MtprotoMethods(client).call("unpinChatMessage", {"chat_id": str(utils.get_peer_id(entity))})
                    self.assertTrue(result)
                    self.assertIsInstance(client.requests[0], request_type)
                    client.get_messages.assert_not_awaited()
                    if pinned_id:
                        self.assertEqual(len(client.requests), 2)
                        request = client.requests[-1]
                        self.assertIsInstance(request, functions.messages.UpdatePinnedMessageRequest)
                        self.assertTrue(request.unpin)
                        self.assertEqual(request.id, pinned_id)
                    else:
                        self.assertEqual(len(client.requests), 1)

    async def test_pin_requests_never_guess_a_missing_message_id(self):
        self.client.get_messages = AsyncMock(side_effect=AssertionError("Unexpected bot history/search"))
        for method, payload, error in (
            ("pinChatMessage", {"chat_id": 7}, ValueError),
            ("unpinChatMessage", {"chat_id": 7, "business_connection_id": "connection"}, UnsupportedMethodError),
        ):
            with self.subTest(method=method):
                with self.assertRaises(error):
                    await self.methods.call(method, payload)
                self.assertEqual(self.client.requests, [])

    async def test_empty_inline_keyboard_clears_markup_at_raw_boundary(self):
        # Telegram rejects ReplyInlineMarkup([]). Its MTProto edit contract
        # removes buttons when reply_markup is absent, as Telethon does.
        for payload in ({"reply_markup": {"inline_keyboard": []}}, {}):
            await self.methods.call("editMessageReplyMarkup", {
                "chat_id": "7", "message_id": 51, **payload,
            })
            request = self.client.requests[-1]
            self.assertIsInstance(request, functions.messages.EditMessageRequest)
            self.assertIsNone(request.reply_markup)
            self.assertIsNone(codec.markup_to_mtproto({"inline_keyboard": []}))

    async def test_send_html_quote_topic_flags_and_buttons_at_raw_boundary(self):
        result = await self.methods.call("sendMessage", {
            "chat_id": "7", "text": "<b>Hello</b>", "parse_mode": "HTML",
            "message_thread_id": 12, "disable_notification": False,
            "protect_content": True, "message_effect_id": "0",
            "reply_parameters": {"message_id": 10, "quote": "Hi", "quote_position": 0, "allow_sending_without_reply": True},
            "reply_markup": {"inline_keyboard": [[{"text": "Open", "callback_data": "env|prod"}]]},
            "link_preview_options": {"is_disabled": True},
        })
        request = self.client.requests[0]
        self.assertEqual(request.message, "Hello")
        self.assertIsInstance(request.entities[0], types.MessageEntityBold)
        self.assertEqual(request.reply_to.top_msg_id, 12)
        self.assertEqual(request.reply_to.quote_offset, 0)
        self.assertEqual(request.reply_to.quote_text, "Hi")
        self.assertIs(request.silent, False)
        self.assertTrue(request.noforwards)
        self.assertEqual(request.effect, 0)
        self.assertTrue(request.no_webpage)
        self.assertEqual(request.reply_markup.rows[0].buttons[0].type.data, b"env|prod")
        self.assertEqual(result["message_id"], 51)

    async def test_business_send_and_monoforum_are_native(self):
        await self.methods.call("sendMessage", {"chat_id": 7, "text": "Hello", "business_connection_id": "connection", "direct_messages_topic_id": 42})
        request = self.client.requests[0]
        self.assertIsInstance(request, functions.InvokeWithBusinessConnectionRequest)
        self.assertEqual(request.connection_id, "connection")
        self.assertIsInstance(request.query.reply_to, types.InputReplyToMonoForum)
        self.assertEqual(request.query.reply_to.monoforum_peer_id.user_id, 42)

    async def test_unsupported_options_fail_before_any_telegram_effect(self):
        for payload in [
            {"chat_id": 7, "text": "x", "unknown_new_option": True},
            {"chat_id": 7, "text": "x", "link_preview_options": {"unknown_preview": True}},
        ]:
            with self.assertRaises(UnsupportedMethodError):
                await self.methods.call("sendMessage", payload)
        self.assertEqual(self.client.requests, [])

    async def test_chat_lookup_preserves_the_pinned_room_context(self):
        # OpenClaw uses this metadata when the bot joins a room. Fetch the
        # server-provided pin ID, rather than fabricating a cached message.
        self.client.response = SimpleNamespace(full_user=SimpleNamespace(pinned_msg_id=93, about="Room guide"))
        self.client.get_messages = AsyncMock(return_value=types.Message(
            id=93, peer_id=types.PeerUser(7), message="Read the incident checklist",
            date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ))
        result = await self.methods.call("getChat", {"chat_id": 7})
        self.assertEqual(result["pinned_message"]["message_id"], 93)
        self.assertEqual(result["pinned_message"]["text"], "Read the incident checklist")
        self.client.get_messages.assert_awaited_once_with(self.client.entity, ids=93)

    async def test_caption_edit_clears_caption_and_keyboard(self):
        await self.methods.call("editMessageCaption", {"chat_id": 7, "message_id": 51, "caption": "", "reply_markup": {"inline_keyboard": []}, "show_caption_above_media": False})
        request = self.client.requests[0]
        self.assertEqual(request.message, "")
        self.assertIs(request.invert_media, False)
        self.assertIsNone(request.reply_markup)

    async def test_native_text_mentions_resolve_users_in_text_and_reply_quotes(self):
        await self.methods.call("sendMessage", {
            "chat_id": 7, "text": '<a href="tg://user?id=8">User</a> <a href="https://example.com">site</a>',
            "parse_mode": "HTML", "reply_parameters": {
                "message_id": 3, "quote": "User",
                "quote_entities": [{"type": "text_mention", "offset": 0, "length": 4, "user": {"id": 8}}],
            },
        })
        request = self.client.requests[0]
        self.assertIsInstance(request.entities[0], types.InputMessageEntityMentionName)
        self.assertEqual(request.entities[0].user_id.user_id, 8)
        self.assertEqual(request.entities[0].user_id.access_hash, 9)
        self.assertIsInstance(request.entities[1], types.MessageEntityTextUrl)
        self.assertEqual(request.entities[1].url, "https://example.com")
        self.assertIsInstance(request.reply_to.quote_entities[0], types.InputMessageEntityMentionName)

    async def test_voice_upload_keeps_filename_mime_and_voice_attributes(self):
        with patch("openclaw_telethon.methods.utils._get_metadata", return_value=None):
            await self.methods.call("sendVoice", {"chat_id": 7, "voice": {"__upload__": "C:/temp/audio", "filename": "recording.ogg", "content_type": "application/octet-stream"}, "duration": 3, "caption": "Audio", "message_thread_id": 6})
        request = self.client.requests[0]
        self.assertIsInstance(request, functions.messages.SendMediaRequest)
        self.assertEqual(request.reply_to.top_msg_id, 6)
        self.assertEqual(request.media.mime_type, "audio/ogg")
        audio = next(item for item in request.media.attributes if isinstance(item, types.DocumentAttributeAudio))
        self.assertTrue(audio.voice)
        self.assertEqual(audio.duration, 3)
        self.assertEqual(self.client.uploads[0][1]["file_name"], "recording.ogg")

    async def test_legacy_document_file_id_stays_reusable(self):
        document = types.Document(123, 456, b"", None, "application/pdf", 22, 2, [types.DocumentAttributeFilename("file.pdf")])
        file_id = utils.pack_bot_file_id(document)
        await self.methods.call("sendDocument", {"chat_id": 7, "document": file_id})
        request = self.client.requests[0]
        self.assertIsInstance(request.media, types.InputMediaDocument)
        self.assertEqual(request.media.id.id, 123)
        self.assertEqual(self.client.uploads, [])

    async def test_poll_options_quiz_solution_and_nonanonymous_votes(self):
        await self.methods.call("sendPoll", {"chat_id": 7, "question": "Pick", "options": ["One", {"text": "Two"}], "is_anonymous": False, "type": "quiz", "correct_option_id": 0, "explanation": "Reason", "open_period": 20, "message_thread_id": 4})
        media = self.client.requests[0].media
        self.assertIsInstance(media, types.InputMediaPoll)
        self.assertTrue(media.poll.public_voters)
        self.assertTrue(media.poll.quiz)
        self.assertEqual(media.poll.close_period, 20)
        self.assertEqual(media.correct_answers, [0])
        self.assertEqual(media.poll.answers[1].option, b"\x01")
        self.assertEqual(media.solution, "Reason")

    async def test_live_location_zero_coordinates_and_heading(self):
        await self.methods.call("sendLocation", {"chat_id": 7, "latitude": 0, "longitude": 0, "live_period": 60, "heading": 0, "proximity_alert_radius": 0})
        media = self.client.requests[0].media
        self.assertIsInstance(media, types.InputMediaGeoLive)
        self.assertEqual((media.geo_point.lat, media.geo_point.long), (0, 0))
        self.assertEqual(media.heading, 0)

    async def test_commands_keep_scopes_language_and_descriptions(self):
        await self.methods.call("setMyCommands", {"scope": {"type": "chat_member", "chat_id": 7, "user_id": 8}, "language_code": "en", "commands": [{"command": "controlui", "description": "Open Mini App"}]})
        request = self.client.requests[0]
        self.assertIsInstance(request.scope, types.BotCommandScopePeerUser)
        self.assertEqual(request.scope.user_id.user_id, 8)
        self.assertEqual(request.commands[0].description, "Open Mini App")
        await self.methods.call("deleteMyCommands", {"scope": {"type": "all_group_chats"}})
        self.assertIsInstance(self.client.requests[1], functions.bots.ResetBotCommandsRequest)

    async def test_callbacks_reactions_pins_and_business_delete(self):
        await self.methods.call("answerCallbackQuery", {"callback_query_id": "123", "text": "Okay", "show_alert": False, "cache_time": 0})
        self.assertIs(self.client.requests[-1].alert, False)
        await self.methods.call("setMessageReaction", {"chat_id": 7, "message_id": 4, "reaction": [], "is_big": False})
        self.assertEqual(self.client.requests[-1].reaction, [])
        await self.methods.call("unpinChatMessage", {"chat_id": 7, "message_id": 4})
        self.assertTrue(self.client.requests[-1].unpin)
        await self.methods.call("deleteBusinessMessages", {"business_connection_id": "business", "message_ids": [4, 5]})
        self.assertEqual(self.client.requests[-1].query.id, [4, 5])

    async def test_forum_topic_creation_and_icon_removal(self):
        action = types.MessageActionTopicCreate("Topic", 7322096, icon_emoji_id=111)
        self.client.response = SimpleNamespace(updates=[SimpleNamespace(message=types.Message(id=77, peer_id=types.PeerUser(7), action=action))])
        result = await self.methods.call("createForumTopic", {"chat_id": 7, "name": "Topic", "icon_color": 7322096})
        self.assertEqual(result, {"message_thread_id": 77, "name": "Topic", "icon_color": 7322096, "icon_custom_emoji_id": "111"})
        await self.methods.call("editForumTopic", {"chat_id": 7, "message_thread_id": 77, "icon_custom_emoji_id": ""})
        self.assertEqual(self.client.requests[-1].icon_emoji_id, 0)

    async def test_chat_member_permissions_use_real_telethon_contract(self):
        self.client.get_permissions = AsyncMock(return_value=ParticipantPermissions(types.ChannelParticipant(7, None), False))
        result = await self.methods.call("getChatMember", {"chat_id": 7, "user_id": 7})
        self.assertEqual(result["status"], "member")
        self.client.get_permissions.return_value = ParticipantPermissions(types.ChannelParticipantBanned(types.PeerUser(7), 9, None, types.ChatBannedRights(None, send_messages=True), left=False), False)
        result = await self.methods.call("getChatMember", {"chat_id": 7, "user_id": 7})
        self.assertEqual(result["status"], "restricted")
        self.assertFalse(result["can_send_messages"])
        self.assertTrue(result["is_member"])

    async def test_missing_send_result_is_never_signaled_as_safe_fallback(self):
        self.client.response = SimpleNamespace(updates=[])
        with self.assertRaisesRegex(RuntimeError, "do not resend"):
            await self.methods.call("sendMessage", {"chat_id": 7, "text": "Hello"})
        self.assertEqual(len(self.client.requests), 1)

    async def test_html_spoilers_and_markdown_use_utf16_offsets(self):
        await self.methods.call("sendMessage", {"chat_id": 7, "text": '😀 <tg-spoiler>secret</tg-spoiler>', "parse_mode": "HTML"})
        request = self.client.requests[-1]
        self.assertEqual(request.message, "😀 secret")
        self.assertEqual((request.entities[0].offset, request.entities[0].length), (3, 6))
        self.assertIsInstance(request.entities[0], types.MessageEntitySpoiler)
        await self.methods.call("sendMessage", {"chat_id": 7, "text": r'😀 *bold* __under__ [site](https://example.com) ||hide||', "parse_mode": "MarkdownV2"})
        request = self.client.requests[-1]
        self.assertEqual(request.message, "😀 bold under site hide")
        self.assertEqual(request.entities[0].offset, 3)
        self.assertEqual([type(item) for item in request.entities], [types.MessageEntityBold, types.MessageEntityUnderline, types.MessageEntityTextUrl, types.MessageEntitySpoiler])

    async def test_explicit_preview_keeps_url_size_and_placement(self):
        await self.methods.call("sendMessage", {"chat_id": 7, "text": "Preview", "link_preview_options": {"url": "https://example.com", "prefer_large_media": True, "show_above_text": True}})
        request = self.client.requests[-1]
        self.assertIsInstance(request, functions.messages.SendMediaRequest)
        self.assertEqual(request.media.url, "https://example.com")
        self.assertTrue(request.media.force_large_media)
        self.assertTrue(request.invert_media)

    async def test_album_is_one_native_request_and_keeps_each_caption(self):
        document = types.Document(123, 456, b"", None, "application/pdf", 22, 2, [])
        file_id = utils.pack_bot_file_id(document)
        result = await self.methods.call("sendMediaGroup", {"chat_id": 7, "message_thread_id": 8, "media": [{"type": "document", "media": file_id, "caption": "One"}, {"type": "document", "media": file_id, "caption": "Two"}]})
        self.assertEqual(len(self.client.requests), 1)
        request = self.client.requests[0]
        self.assertIsInstance(request, functions.messages.SendMultiMediaRequest)
        self.assertEqual(request.reply_to.top_msg_id, 8)
        self.assertEqual([item.message for item in request.multi_media], ["One", "Two"])
        self.assertEqual([item["message_id"] for item in result], [51, 52])

    async def test_missing_reply_retry_keeps_explicit_topic(self):
        self.client.reject_reply_once = True
        await self.methods.call("sendMessage", {"chat_id": 7, "text": "Hello", "message_thread_id": 11, "reply_parameters": {"message_id": 55, "allow_sending_without_reply": True}})
        self.assertEqual(len(self.client.requests), 2)
        request = self.client.requests[-1]
        self.assertEqual(request.reply_to.top_msg_id, 11)
        self.assertEqual(request.reply_to.reply_to_msg_id, 11)

    async def test_native_rich_message_keeps_blocks_and_keyboard(self):
        await self.methods.call("sendRichMessage", {"chat_id": 7, "rich_message": {"blocks": [{"type": "paragraph", "text": "Hello"}]}, "reply_markup": {"inline_keyboard": [[{"text": "Open Mini App", "web_app": {"url": "https://example.com/app"}}]]}})
        request = self.client.requests[-1]
        self.assertIsInstance(request.rich_message, types.InputRichMessage)
        self.assertIsInstance(request.rich_message.blocks[0], types.PageBlockParagraph)
        self.assertIsInstance(request.reply_markup.rows[0].buttons[0].type, types.InlineButtonTypeWebView)

    async def test_modern_file_reference_survives_getfile_and_send(self):
        document = types.Document(123, 456, b"reference-bytes", None, "application/pdf", 22, 2, [])
        file_id = codec.pack_file_id(document)
        result = await self.methods.call("getFile", {"file_id": file_id})
        self.assertEqual(result["file_path"], "telethon/" + file_id)
        self.assertEqual(self.client.requests, [])
        await self.methods.call("sendDocument", {"chat_id": 7, "document": file_id})
        self.assertEqual(self.client.requests[-1].media.id.file_reference, b"reference-bytes")

    async def test_inline_edit_uses_native_inline_request(self):
        inline = base64.urlsafe_b64encode(struct.pack("<iiiq", self.client.session.dc_id, 51, 7, 123)).decode().rstrip("=")
        result = await self.methods.call("editMessageText", {"inline_message_id": inline, "text": "New text"})
        self.assertTrue(result)
        self.assertIsInstance(self.client.requests[-1], functions.messages.EditInlineBotMessageRequest)
        self.assertEqual(self.client.requests[-1].message, "New text")

    async def test_getme_preserves_topic_and_mini_app_capabilities(self):
        self.client.entity = types.User(7, bot=True, first_name="Bot", bot_forum_view=True, bot_forum_can_manage_topics=True, bot_has_main_app=True, bot_business=True, bot_chat_history=True)
        result = await self.methods.call("getMe", {})
        self.assertTrue(result["has_topics_enabled"])
        self.assertTrue(result["allows_users_to_create_topics"])
        self.assertTrue(result["has_main_web_app"])
        self.assertTrue(result["can_connect_to_business"])

    async def test_video_metadata_is_read_before_upload_and_explicit_values_win(self):
        values = {"width": 1920, "height": 1080, "duration": timedelta(seconds=2.5)}
        metadata = SimpleNamespace(has=lambda key: key in values, get=lambda key: values[key])
        with patch("openclaw_telethon.methods.utils._get_metadata", return_value=metadata):
            await self.methods.call("sendVideo", {"chat_id": 7, "video": {"__upload__": "C:/temp/video", "filename": "clip.mp4", "content_type": "video/mp4"}, "width": 640, "supports_streaming": True})
        video = next(item for item in self.client.requests[-1].media.attributes if isinstance(item, types.DocumentAttributeVideo))
        self.assertEqual((video.w, video.h, video.duration), (640, 1080, 2.5))
        self.assertTrue(video.supports_streaming)

    async def test_rich_media_url_is_uploaded_before_nested_block_conversion(self):
        await self.methods.call("sendRichMessage", {"chat_id": 7, "rich_message": {"blocks": [{"type": "details", "summary": "Picture", "blocks": [{"type": "photo", "photo": {"type": "photo", "media": "https://example.com/image.jpg"}}]}]}})
        self.assertEqual(len(self.client.requests), 2)
        upload, send = self.client.requests
        self.assertIsInstance(upload, functions.messages.UploadMediaRequest)
        self.assertEqual(upload.media.url, "https://example.com/image.jpg")
        self.assertEqual(send.rich_message.photos[0].file_reference, b"photo-reference")
        self.assertEqual(send.rich_message.blocks[0].blocks[0].photo_id, 123)

    async def test_rpc_poll_metadata_reaches_service_observer(self):
        observed = []
        methods = MtprotoMethods(self.client, update_observer=observed.append)
        await methods.call("sendPoll", {"chat_id": 7, "question": "Choose", "options": ["Yes", "No"]})
        self.assertEqual(len(observed), 1)
        media = observed[0].updates[0].message.media
        self.assertIsInstance(media, types.MessageMediaPoll)
        self.assertEqual(media.poll.answers[0].text.text, "Yes")

    async def test_rich_html_attachment_alias_uses_native_inputrichfile(self):
        await self.methods.call("sendRichMessage", {"chat_id": 7, "rich_message": {"html": '<img src="tg://photo?id=picture">', "media": [{"id": "picture", "media": {"type": "photo", "media": "https://example.com/image.jpg"}}]}})
        request = self.client.requests[-1]
        self.assertIsInstance(request.rich_message, types.InputRichMessageHTML)
        self.assertEqual(request.rich_message.files[0].id, "picture")
        self.assertEqual(request.rich_message.files[0].photo.file_reference, b"photo-reference")


if __name__ == "__main__":
    unittest.main()
