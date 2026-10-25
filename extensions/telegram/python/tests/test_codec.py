"""Fixed TL fixtures exercise the real compatibility codec without credentials."""
import asyncio
import struct
import unittest
from unittest.mock import AsyncMock
from datetime import datetime, timezone
from types import SimpleNamespace

from telethon import utils
from telethon.tl import types as t

from openclaw_telethon import codec


DATE = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


class FixtureClient:
    def __init__(self):
        self._self_id = 99
        self._mb_entity_cache = SimpleNamespace(get=lambda peer: None)
        self.me = t.User(id=99, bot=True, first_name="Fixture bot")
        self.alice = t.User(id=7, first_name="Alice", last_name="Example", username="alice_fixture")
        self.group = t.Channel(id=123, title="Forum", photo=t.ChatPhotoEmpty(), date=DATE,
                               megagroup=True, forum=True, access_hash=10)
        self.entities = {7: self.alice, 99: self.me, -1000000000123: self.group}

    async def get_entity(self, peer):
        return self.entities[utils.get_peer_id(peer)]

    async def get_me(self):
        return self.me

    async def get_messages(self, peer, ids):
        return t.Message(id=ids, peer_id=peer, date=DATE, message="Bot question", from_id=t.PeerUser(99))


class CodecTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_update_does_not_block_following_messages(self):
        client = FixtureClient()
        for kind in (t.UpdateNewMessage, t.UpdateNewChannelMessage,
                     t.UpdateEditMessage, t.UpdateEditChannelMessage):
            update = kind(message=t.MessageEmpty(id=8, peer_id=t.PeerChannel(123)), pts=1, pts_count=1)
            self.assertIsNone(await codec.raw_update_to_bot(client, update))
        message = t.Message(id=9, peer_id=t.PeerChannel(123), from_id=t.PeerUser(7),
                            date=DATE, message="Still deliver this message")
        result = await codec.raw_update_to_bot(client, t.UpdateNewChannelMessage(message, 2, 1))
        self.assertEqual(result["message"]["text"], "Still deliver this message")

    async def test_cold_bot_session_resolves_configured_numeric_peers(self):
        class ColdClient:
            async def get_input_entity(self, peer):
                raise ValueError("No entity has been observed yet")
        client = ColdClient()
        group = await codec.resolve_input_peer(client, -1001234567890)
        self.assertIsInstance(group, t.InputPeerChannel)
        self.assertEqual((group.channel_id, group.access_hash), (1234567890, 0))
        user = await codec.resolve_input_peer(client, "12345")
        self.assertIsInstance(user, t.InputPeerUser)
        self.assertEqual((user.user_id, user.access_hash), (12345, 0))
        with self.assertRaises(ValueError):
            await codec.resolve_input_peer(client, "@unknown_username")

    def test_peer_ids_preserve_bot_api_chat_numbering(self):
        group = t.Chat(id=45, title="Basic", photo=t.ChatPhotoEmpty(), participants_count=2,
                       date=DATE, version=1)
        self.assertEqual(codec.chat_to_bot(group), {"id": -45, "type": "group", "title": "Basic"})
        channel = FixtureClient().group
        self.assertEqual(codec.chat_to_bot(channel), {"id": -1000000000123, "type": "supergroup", "title": "Forum", "is_forum": True})

    def test_bot_capabilities_and_user_fields(self):
        user = t.User(id=7, bot=True, first_name="Fixture", last_name="Bot", username="fixture_bot",
                      lang_code="en", bot_chat_history=True, bot_business=True, bot_has_main_app=True,
                      bot_inline_placeholder="Search")
        result = codec.user_to_bot(user)
        self.assertEqual(result["id"], 7)
        self.assertEqual(result["language_code"], "en")
        self.assertTrue(result["can_read_all_group_messages"])
        self.assertTrue(result["can_connect_to_business"])
        self.assertTrue(result["supports_inline_queries"])

    def test_utf16_entity_coordinates_are_not_reinterpreted(self):
        # In "😀bold", the b starts at UTF-16 offset 2, not Python index 1.
        incoming = [t.MessageEntityBold(2, 4), t.MessageEntityCustomEmoji(0, 2, 9007199254740999),
                    t.MessageEntityPre(6, 3, "py"), t.MessageEntityBlockquote(9, 2, collapsed=True)]
        wire = codec.entities_to_bot(incoming)
        self.assertEqual(wire[0], {"type": "bold", "offset": 2, "length": 4})
        self.assertEqual(wire[1]["custom_emoji_id"], "9007199254740999")
        self.assertEqual(wire[3]["type"], "expandable_blockquote")
        converted = codec.entities_to_mtproto(wire)
        self.assertEqual(converted[0].offset, 2)
        self.assertEqual(converted[2].language, "py")
        self.assertTrue(converted[3].collapsed)
        date = {"type": "date_time", "offset": 2, "length": 4, "unix_time": 1767323045, "date_time_format": "wDT"}
        self.assertEqual(codec.entities_to_bot(codec.entities_to_mtproto([date])), [date])

    def test_keyboard_styles_callback_bytes_and_miniapp_roundtrip(self):
        markup = {"inline_keyboard": [[{"text": "Choose", "callback_data": "env|prod|😀", "style": "primary", "icon_custom_emoji_id": "9007199254740999"}],
                                      [{"text": "Control UI", "web_app": {"url": "https://example.test/control/#ticket=xyz"}}]]}
        raw = codec.markup_to_mtproto(markup)
        button = raw.rows[0].buttons[0]
        self.assertEqual(button.type.data, b"env|prod|\xf0\x9f\x98\x80")
        self.assertTrue(button.style.bg_primary)
        self.assertEqual(button.style.icon, 9007199254740999)
        self.assertIsInstance(raw.rows[1].buttons[0].type, t.InlineButtonTypeWebView)
        self.assertEqual(codec.markup_to_bot(raw), markup)
        with self.assertRaisesRegex(codec.UnsupportedCodec, "style"):
            codec.markup_to_mtproto({"inline_keyboard": [[{"text": "x", "callback_data": "y", "style": "unknown"}]]})
        with self.assertRaises(ValueError):
            codec.markup_to_mtproto({"inline_keyboard": [[{"text": "x", "callback_data": "😀" * 17}]]})

    def test_document_ids_preserve_references_and_legacy_ids(self):
        document = t.Document(id=123456789, access_hash=-987654321, file_reference=b"live\x00reference",
                              date=DATE, mime_type="audio/ogg", size=321, dc_id=2,
                              attributes=[t.DocumentAttributeAudio(duration=6, voice=True)])
        kind, data = codec.document_to_bot(document)
        self.assertEqual(kind, "voice")
        self.assertEqual(data["duration"], 6)
        resolved = codec.resolve_file_id(data["file_id"])
        self.assertEqual((resolved.id, resolved.access_hash, resolved.file_reference, resolved.dc_id),
                         (123456789, -987654321, b"live\x00reference", 2))
        self.assertTrue(resolved.attributes[0].voice)
        legacy = utils.pack_bot_file_id(document)
        self.assertEqual(codec.resolve_file_id(legacy).id, 123456789)

    def test_modern_external_document_fixture(self):
        # An independently assembled v4 wire fixture, not pack_file_id output.
        payload = struct.pack("<ii", (1 << 25) | 5, 4) + b"\x03ref" + struct.pack("<qqii", 88, 91, 30, 4) + b"\x1e\x04"
        encoded = codec._base64(codec._zero_encode(payload))
        result = codec.resolve_file_id(encoded)
        self.assertEqual((result.id, result.access_hash, result.dc_id, result.file_reference), (88, 91, 4, b"ref"))
        self.assertIsNone(codec.resolve_file_id("garbage"))

    def test_photo_sizes_keep_distinct_reusable_identifiers(self):
        photo = t.Photo(id=81, access_hash=82, file_reference=b"photo-ref", date=DATE,
                        sizes=[t.PhotoSize("s", 90, 60, 300), t.PhotoSizeProgressive("y", 1280, 800, [500, 3000])], dc_id=2)
        result = codec.photo_to_bot(photo)
        self.assertEqual([(p["width"], p["height"], p["file_size"]) for p in result], [(90, 60, 300), (1280, 800, 3000)])
        self.assertNotEqual(result[0]["file_id"], result[1]["file_id"])
        resolved = codec.resolve_file_id(result[1]["file_id"])
        self.assertEqual(resolved.file_reference, b"photo-ref")
        self.assertEqual(resolved.sizes[0].type, "y")

    def test_poll_answers_match_opaque_option_bytes(self):
        poll = t.Poll(id=9007199254740999, question=t.TextWithEntities("Question", []), hash=0,
                      answers=[t.PollAnswer(t.TextWithEntities("A", []), b"second"), t.PollAnswer(t.TextWithEntities("B", []), b"first")],
                      public_voters=True, multiple_choice=True, quiz=True)
        results = t.PollResults(results=[t.PollAnswerVoters(b"first", voters=3, correct=True), t.PollAnswerVoters(b"second", voters=1)], total_voters=4)
        result = codec.poll_to_bot(poll, results)
        self.assertEqual(result["id"], "9007199254740999")
        self.assertEqual([o["voter_count"] for o in result["options"]], [1, 3])
        self.assertEqual(result["correct_option_ids"], [1])
        self.assertFalse(result["is_anonymous"])

    async def test_forum_message_quote_forward_and_caption(self):
        client = FixtureClient()
        document = t.Document(id=5, access_hash=6, file_reference=b"ref", date=DATE,
                              mime_type="text/plain", size=4, dc_id=2,
                              attributes=[t.DocumentAttributeFilename("notes.txt")])
        message = t.Message(id=55, peer_id=t.PeerChannel(123), from_id=t.PeerUser(7), date=DATE,
                            message="😀bold", entities=[t.MessageEntityBold(2, 4)],
                            reply_to=t.MessageReplyHeader(forum_topic=True, reply_to_msg_id=50, reply_to_top_id=11,
                                                         quote=True, quote_text="quote", quote_offset=2),
                            media=t.MessageMediaDocument(document=document), grouped_id=9007199254740999,
                            fwd_from=t.MessageFwdHeader(date=DATE, from_name="Hidden sender"))
        result = await codec.message_to_bot(client, message)
        self.assertEqual(result["caption"], "😀bold")
        self.assertEqual(result["caption_entities"], [{"type": "bold", "offset": 2, "length": 4}])
        self.assertEqual(result["message_thread_id"], 11)
        self.assertEqual(result["quote"], {"text": "quote", "position": 2, "is_manual": True, "entities": []})
        self.assertEqual(result["media_group_id"], "9007199254740999")
        self.assertEqual(result["from"]["username"], "alice_fixture")
        self.assertEqual(result["document"]["file_name"], "notes.txt")
        self.assertEqual(result["forward_origin"]["type"], "hidden_user")

    async def test_topic_create_and_migrate_services(self):
        client = FixtureClient()
        message = t.Message(id=11, peer_id=t.PeerChannel(123), date=DATE,
                            action=t.MessageActionTopicCreate("Topic", 7322096, icon_emoji_id=9007199254740999))
        result = await codec.message_to_bot(client, message)
        self.assertEqual(result["forum_topic_created"], {"name": "Topic", "icon_color": 7322096, "icon_custom_emoji_id": "9007199254740999"})
        self.assertEqual(result["message_thread_id"], 11)
        message.action = t.MessageActionChatMigrateTo(456)
        result = await codec.message_to_bot(client, message)
        self.assertEqual(result["migrate_to_chat_id"], -1000000000456)

    async def test_raw_poll_vote_reaction_and_callback(self):
        client = FixtureClient()
        vote = t.UpdateMessagePollVote(poll_id=9007199254740999, peer=t.PeerUser(7), options=[b"opaque"], positions=[2], qts=5)
        self.assertEqual((await codec.raw_update_to_bot(client, vote))["poll_answer"]["option_ids"], [2])
        reaction = t.UpdateBotMessageReaction(t.PeerChannel(123), 55, DATE, t.PeerUser(7),
                                               [t.ReactionEmoji("👍")], [t.ReactionCustomEmoji(9007199254740999)], 6)
        value = (await codec.raw_update_to_bot(client, reaction))["message_reaction"]
        self.assertEqual(value["old_reaction"], [{"type": "emoji", "emoji": "👍"}])
        self.assertEqual(value["new_reaction"][0]["custom_emoji_id"], "9007199254740999")
        callback = t.UpdateBotCallbackQuery(9007199254740999, 7, t.PeerChannel(123), 55, -9007199254740999,
                                           data=b"env|prod")
        value = (await codec.raw_update_to_bot(client, callback))["callback_query"]
        self.assertEqual(value["id"], "9007199254740999")
        self.assertEqual(value["chat_instance"], "-9007199254740999")
        self.assertEqual(value["data"], "env|prod")
        self.assertEqual(value["message"]["text"], "Bot question")

    async def test_optimized_private_updates_and_channel_dm_topics(self):
        client = FixtureClient()
        update = t.UpdateShortMessage(id=55, user_id=7, message="😀bold", pts=10, pts_count=1, date=DATE,
                                      entities=[t.MessageEntityBold(2, 4)])
        # No network-backed Telethon client is needed for fixed entities.
        message = (await codec.raw_update_to_bot(client, update))["message"]
        self.assertEqual(message["text"], "😀bold")
        self.assertEqual(message["entities"][0]["offset"], 2)
        mono = client.group
        mono.monoforum = True
        message = t.Message(id=66, peer_id=t.PeerChannel(123), date=DATE, from_id=t.PeerUser(7),
                            saved_peer_id=t.PeerUser(7), message="DM topic",
                            reply_to=t.MessageReplyHeader(reply_to_msg_id=50, reply_to_peer_id=t.PeerUser(99)))
        value = await codec.message_to_bot(client, message, depth=1)
        self.assertEqual(value["direct_messages_topic"], {"topic_id": 7})
        self.assertNotIn("message_thread_id", value)

    async def test_sticker_set_name_keeps_existing_search_metadata(self):
        class StickerClient(FixtureClient):
            calls = 0
            async def __call__(self, request):
                self.calls += 1
                return SimpleNamespace(set=SimpleNamespace(short_name="FixtureStickers"))
        client = StickerClient()
        doc = t.Document(id=91, access_hash=92, file_reference=b"ref", date=DATE,
                         mime_type="image/webp", size=128, dc_id=2,
                         attributes=[t.DocumentAttributeSticker("👍", t.InputStickerSetID(100, 101)),
                                     t.DocumentAttributeImageSize(512, 512)])
        message = t.Message(id=81, peer_id=t.PeerUser(7), from_id=t.PeerUser(7), date=DATE,
                            media=t.MessageMediaDocument(document=doc))
        for _ in range(2):
            value = await codec.message_to_bot(client, message)
            self.assertEqual(value["sticker"]["set_name"], "FixtureStickers")
            self.assertEqual(value["sticker"]["emoji"], "👍")
        self.assertEqual(client.calls, 1)

    def test_rich_messages_retain_structure_and_limits(self):
        blocks = [{"type": "heading", "size": 3, "text": "Title"},
                  {"type": "paragraph", "text": [{"type": "bold", "text": "bold"}, "😀"]},
                  {"type": "blockquote", "blocks": [{"type": "paragraph", "text": "quoted"}], "credit": "Alice"},
                  {"type": "list", "items": [{"blocks": [{"type": "paragraph", "text": "done"}], "has_checkbox": True, "is_checked": True}]},
                  {"type": "table", "cells": [[{"text": "A", "is_header": True, "align": "center", "valign": "top"}]], "is_bordered": True},
                  {"type": "pre", "text": "x" * 5000, "language": "python"}]
        raw = codec.rich_to_mtproto({"blocks": blocks, "skip_entity_detection": True})
        self.assertIsInstance(raw, t.InputRichMessage)
        self.assertTrue(raw.noautolink)
        self.assertEqual(len(raw.blocks[-1].text.text), 5000)
        parsed = codec.rich_to_bot(t.RichMessage(raw.blocks, [], []))
        self.assertEqual(parsed["blocks"][0], blocks[0])
        self.assertEqual(parsed["blocks"][1], blocks[1])
        self.assertEqual(parsed["blocks"][2], blocks[2])
        self.assertTrue(parsed["blocks"][3]["items"][0]["is_checked"])
        self.assertEqual(parsed["blocks"][3]["items"][0]["label"], "•")
        self.assertEqual(parsed["blocks"][4]["cells"][0][0]["align"], "center")
        self.assertEqual(parsed["blocks"][-1]["text"], "x" * 5000)

    def test_rich_schema_metadata_and_native_html(self):
        inline = [{"type": "email_address", "text": "Contact", "email_address": "alice@example.test"},
                  {"type": "phone_number", "text": "Call", "phone_number": "+123456789"},
                  {"type": "date_time", "text": "Tomorrow", "unix_time": 1767323045, "date_time_format": "r"}]
        self.assertEqual(codec.rich_text_to_bot(codec.rich_text_to_mtproto(inline)), inline)
        block = {"type": "buttons", "align": "right", "buttons": [{"text": "Unavailable", "disabled": {}, "style": "link"}]}
        converted = codec.rich_block_to_mtproto(block)
        self.assertTrue(converted.align_right)
        self.assertEqual(codec.rich_block_to_bot(converted, {}), block)
        raw = codec.rich_to_mtproto({"html": "<h1>Title</h1>", "is_rtl": True})
        self.assertIsInstance(raw, t.InputRichMessageHTML)
        self.assertEqual(raw.html, "<h1>Title</h1>")
        self.assertTrue(raw.rtl)
        with self.assertRaisesRegex(codec.UnsupportedCodec, "extra_option"):
            codec.rich_to_mtproto({"blocks": [{"type": "paragraph", "text": "x", "extra_option": True}]})


class BusinessCallbackRegressionTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        client = FixtureClient()
        client.get_messages = AsyncMock(side_effect=AssertionError('Business reply already supplied; ordinary history lookup is not allowed'))
        reply = t.Message(id=5, peer_id=t.PeerUser(7), from_id=t.PeerUser(7), date=DATE, message='Original business request')
        message = t.Message(id=8, peer_id=t.PeerUser(7), from_id=t.PeerUser(99), date=DATE, message='Business controls', out=True, reply_to=t.MessageReplyHeader(reply_to_msg_id=5))
        return client, message, reply

    async def test_business_callback_preserves_connection_message_reply_and_data(self):
        client, message, reply = self.fixture()
        update = t.UpdateBusinessBotCallbackQuery(query_id=9007199254740999, user_id=7, connection_id='business-fixture', message=message, chat_instance=-9007199254740998, reply_to_message=reply, data='env|prod|😀'.encode())
        bytes(update)
        result = await codec.raw_update_to_bot(client, update)
        self.assertIsNotNone(result, 'Business callback must reach the retained callback dispatcher')
        callback = result['callback_query']
        self.assertEqual(callback['id'], '9007199254740999')
        self.assertEqual(callback['chat_instance'], '-9007199254740998')
        self.assertEqual(callback['data'], 'env|prod|😀')
        self.assertEqual(callback['from']['id'], 7)
        self.assertEqual(callback['message']['message_id'], 8)
        self.assertEqual(callback['message']['business_connection_id'], 'business-fixture')
        self.assertEqual(callback['message']['reply_to_message']['text'], 'Original business request')
        client.get_messages.assert_not_awaited()

    async def test_business_message_uses_supplied_reply_without_ordinary_history_lookup(self):
        client, message, reply = self.fixture()
        message.out = False
        message.from_id = t.PeerUser(7)
        update = t.UpdateBotNewBusinessMessage(connection_id='business-fixture', message=message, qts=11, reply_to_message=reply)
        bytes(update)
        result = await codec.raw_update_to_bot(client, update)
        self.assertEqual(result['business_message']['business_connection_id'], 'business-fixture')
        self.assertEqual(result['business_message']['reply_to_message']['message_id'], 5)
        client.get_messages.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
