"""Offline receipt/restart and worker HTTP contract tests (no Telegram login)."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, FormData, web
from aiohttp.test_utils import TestServer
from telethon import TelegramClient, errors, functions, types

from openclaw_telethon.store import UpdateStore
from openclaw_telethon import codec


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "inbox.sqlite3"
        self.store = UpdateStore(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def reopen(self):
        self.store.close()
        self.store = UpdateStore(self.path)

    def test_restart_replays_identical_ids_until_positive_offset_ack(self):
        self.store.append("telegram-new-10", {"message": {"message_id": 10}})
        self.store.append("telegram-new-11", {"message": {"message_id": 11}})
        received = self.store.pending()
        self.reopen()
        self.assertEqual(self.store.pending(), received)
        self.store.acknowledge(received[0]["update_id"] + 1)
        self.reopen()
        self.assertEqual(self.store.pending(), [received[1]])
        self.store.acknowledge(received[1]["update_id"] + 1)
        self.assertEqual(self.store.pending(), [])
        # An MTProto difference replay after acknowledgement cannot resurrect it.
        self.assertIsNone(self.store.append("telegram-new-10", {"message": {"message_id": 10}}))
        self.reopen()
        later = self.store.append("telegram-new-12", {"message": {"message_id": 12}})
        self.assertGreater(later, received[1]["update_id"])

    def test_negative_offset_retains_last_pending_updates(self):
        for index in range(4):
            self.store.append(str(index), {"message": {"message_id": index}})
        self.store.acknowledge(-2)
        self.assertEqual([item["message"]["message_id"] for item in self.store.pending()], [2, 3])

    def test_legacy_bot_api_cursor_cannot_discard_new_mtproto_receipts(self):
        self.store.append("first-mtproto", {"message": {"message_id": 1}})
        self.store.acknowledge(2_147_483_647)
        self.store.acknowledge(2 ** 53 - 1)
        received = self.store.pending()
        self.assertEqual(len(received), 1)
        self.assertGreater(received[0]["update_id"], 2_147_483_647)
        self.assertLess(received[0]["update_id"], 2 ** 53)
        with self.assertRaises(ValueError):
            self.store.acknowledge(received[0]["update_id"] + 1000)
        self.assertEqual(self.store.pending(), received)

    def test_raw_conversion_replay_and_atomic_commit(self):
        self.store.journal_raw("raw-a", b"raw receipt")
        self.reopen()
        batch_id, encoded = self.store.next_raw()
        self.assertEqual(encoded, b"raw receipt")
        # A failure halfway through publication must retain raw and publish none.
        with self.assertRaises(TypeError):
            self.store.commit_raw(batch_id, [
                ("event-a", {"message": {"message_id": 5}}),
                ("event-b", {"message": object()}),
            ])
        self.reopen()
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.next_raw(), (batch_id, encoded))
        self.store.commit_raw(batch_id, [("event-a", {"message": {"message_id": 5}})])
        self.reopen()
        self.assertIsNone(self.store.next_raw())
        self.assertEqual(self.store.pending()[0]["message"]["message_id"], 5)

    def test_settings_survive_restart(self):
        self.store.set("webhook", {"url": "http://127.0.0.1/hook", "secret_token": "synthetic"})
        self.store.set("allowed_updates", ["message", "callback_query"])
        self.reopen()
        self.assertEqual(self.store.get("webhook")["secret_token"], "synthetic")
        self.assertEqual(self.store.get("allowed_updates"), ["message", "callback_query"])

    def test_poll_definitions_survive_restart(self):
        self.store.put_poll_definition(2 ** 62 + 1, b"synthetic TL poll")
        self.reopen()
        self.assertEqual(self.store.get_poll_definition(2 ** 62 + 1), b"synthetic TL poll")

    def test_drop_pending_retires_raw_work_already_being_converted(self):
        self.store.journal_raw("raw-to-drop", b"receipt")
        batch_id, _ = self.store.next_raw()
        self.store.drop_pending()
        self.store.commit_raw(batch_id, [("late-conversion", {"message": {"message_id": 99}})])
        self.reopen()
        self.assertEqual(self.store.pending(), [])
        self.assertIsNone(self.store.next_raw())

    def test_deferred_poll_retains_original_receipt_until_definition_arrives(self):
        self.store.journal_raw("batch-with-legacy-poll", b"batch")
        batch_id, _ = self.store.next_raw()
        self.store.commit_raw(batch_id, [], [("legacy-poll-event", 777, b"original TL envelope")])
        self.reopen()
        self.assertIsNone(self.store.next_raw())
        self.assertIsNone(self.store.next_deferred_poll())
        self.assertEqual(self.store.deferred_poll_diagnostics()["count"], 1)
        self.store.put_poll_definition(777, b"poll definition")
        self.assertEqual(self.store.next_deferred_poll(), ("legacy-poll-event", b"original TL envelope"))
        self.store.commit_deferred_poll("legacy-poll-event", [("legacy-poll-event:0", {"poll": {"id": "777"}})])
        self.reopen()
        self.assertEqual(self.store.deferred_poll_diagnostics()["count"], 0)
        self.assertEqual(self.store.pending()[0]["poll"]["id"], "777")

    def test_media_sources_survive_restart_without_losing_message_fallback(self):
        self.store.put_media_source("document", 2 ** 62, b"peer TL", 77, (888, 999))
        self.store.put_media_source("document", 2 ** 62, sticker_set=(888, 1000))
        self.store.put_media_source("photo", 2 ** 62, b"other peer", 78)
        self.reopen()
        self.assertEqual(self.store.get_media_source("document", 2 ** 62), {
            "peer": b"peer TL", "message_id": 77,
            "sticker_set_id": 888, "sticker_set_access_hash": 1000,
        })
        self.assertEqual(self.store.get_media_source("photo", 2 ** 62)["message_id"], 78)


class FakeClient:
    async def disconnect(self):
        pass


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from openclaw_telethon.service import TelethonService
        self.directory = tempfile.TemporaryDirectory()
        self.service = TelethonService({
            "secret": "synthetic-worker-secret-0123456789abcdef",
            "state_dir": self.directory.name,
        }, client=FakeClient())
        self.service.ready = True
        self.server = TestServer(self.service.app)
        await self.server.start_server()
        self.headers = {"X-OpenClaw-Telethon": self.service.secret}
        self.http = ClientSession()

    async def asyncTearDown(self):
        await self.http.close()
        await self.server.close()
        await self.service.close()
        self.directory.cleanup()

    async def rpc(self, method, payload=None):
        async with self.http.post(self.server.make_url("/rpc"), headers=self.headers,
                                  json={"method": method, "payload": payload or {}}) as response:
            return await response.json()

    async def test_authenticated_boundary_and_poll_replay_ack(self):
        async with self.http.get(self.server.make_url("/health")) as response:
            self.assertEqual(response.status, 401)
        async with self.http.get(self.server.make_url("/health"), headers=[
            ("X-OpenClaw-Telethon", self.service.secret),
            ("X-OpenClaw-Telethon", self.service.secret),
        ]) as response:
            self.assertEqual(response.status, 401)
        self.service.store.append("event-1", {"message": {"message_id": 71}})
        first = await self.rpc("getUpdates")
        self.assertEqual(first["result"][0]["message"]["message_id"], 71)
        self.assertEqual(await self.rpc("getUpdates"), first)
        result = await self.rpc("getUpdates", {"offset": first["result"][0]["update_id"] + 1})
        self.assertEqual(result, {"ok": True, "result": []})

    async def test_long_poll_wakes_and_webhook_conflict_is_explicit(self):
        pending = asyncio.create_task(self.rpc("getUpdates", {"timeout": 5}))
        # Coordinate at actual handler admission instead of a wall-clock sleep.
        for _ in range(100):
            if self.service.poll_lock.locked():
                break
            await asyncio.sleep(0)
        self.assertTrue(self.service.poll_lock.locked())
        self.service.store.append("event-2", {"message": {"message_id": 72}})
        self.service.changed.set()
        self.assertEqual((await pending)["result"][0]["message"]["message_id"], 72)
        await self.rpc("setWebhook", {"url": "http://127.0.0.1/hook"})
        self.assertEqual((await self.rpc("getUpdates"))["error_code"], 409)
        await self.rpc("deleteWebhook")
        self.assertEqual((await self.rpc("getUpdates"))["result"][0]["message"]["message_id"], 72)

    async def test_webhook_failed_receipt_is_replayed_before_ack(self):
        attempts = []
        accepted = asyncio.Event()
        fail_once = [True]
        async def webhook(request):
            attempts.append((await request.json(), request.headers.get("X-Telegram-Bot-Api-Secret-Token")))
            if fail_once:
                fail_once.pop()
                return web.Response(status=503)
            accepted.set()
            return web.Response(status=200)
        hook_app = web.Application()
        hook_app.router.add_post("/hook", webhook)
        hook_server = TestServer(hook_app)
        await hook_server.start_server()
        self.service.http = ClientSession()
        self.service.store.append("webhook-event", {"message": {"message_id": 80}})
        await self.rpc("setWebhook", {"url": str(hook_server.make_url("/hook")), "secret_token": "hook-secret"})
        task = asyncio.create_task(self.service.forward_webhook())
        self.service.tasks.append(task)
        # Failure wakes a fresh attempt through the real forwarding loop.
        for _ in range(1000):
            if attempts:
                break
            await asyncio.sleep(0)
        self.assertEqual(self.service.store.pending_count(), 1)
        self.service.changed.set()
        await asyncio.wait_for(accepted.wait(), timeout=3)
        for _ in range(1000):
            if not self.service.store.pending_count():
                break
            await asyncio.sleep(0)
        self.assertEqual(self.service.store.pending_count(), 0)
        self.assertEqual(attempts[0], attempts[1])
        await hook_server.close()

    async def test_raw_hook_commits_before_first_await(self):
        from openclaw_telethon.service import JournaledTelegramClient
        client = object.__new__(JournaledTelegramClient)
        client.transport_store = self.service.store
        client.raw_wakeup = asyncio.Event()
        client.transport_journal_failed = False
        update = types.UpdateDeleteMessages(messages=[123], pts=7, pts_count=1)
        entered_base = []
        async def preprocess_base(*args):
            self.assertIsNotNone(self.service.store.next_raw())
            entered_base.append(True)
            return [update]
        with patch.object(TelegramClient, "_preprocess_updates", preprocess_base):
            self.assertEqual(await client._preprocess_updates([update], [], []), [])
        self.assertEqual(entered_base, [True])
        self.assertTrue(client.raw_wakeup.is_set())

    async def test_upload_references_are_private_temporary_files(self):
        paths = []
        async def call(method, payload):
            self.assertEqual(method, "sendDocument")
            uploaded = payload["document"]
            paths.append(Path(uploaded["__upload__"]))
            self.assertEqual(paths[0].read_bytes(), b"synthetic upload")
            self.assertEqual(uploaded["filename"], "sample.txt")
            return {"message_id": 44}
        self.service.methods.call = call
        form = FormData()
        form.add_field("payload", json.dumps({"method": "sendDocument", "payload": {
            "chat_id": 1, "document": "attach://document",
        }}))
        form.add_field("document", b"synthetic upload", filename="sample.txt", content_type="text/plain")
        async with self.http.post(self.server.make_url("/rpc"), headers=self.headers, data=form) as response:
            self.assertEqual(await response.json(), {"ok": True, "result": {"message_id": 44}})
        self.assertFalse(paths[0].exists())

    async def test_rpc_errors_keep_existing_recovery_descriptions(self):
        for token, expected in [
            ("MESSAGE_ID_INVALID", "Bad Request: message to edit not found"),
            ("QUOTE_TEXT_INVALID", "Bad Request: quote not found"),
            ("ENTITY_BOUNDS_INVALID", "Bad Request: can't parse entities"),
        ]:
            async def call(method, payload):
                raise errors.BadRequestError(request=None, message=token)
            self.service.methods.call = call
            result = await self.rpc("editMessageText", {"chat_id": 1, "text": "synthetic"})
            self.assertEqual(result, {"ok": False, "error_code": 400, "description": expected})
        async def flood(method, payload):
            raise errors.FloodWaitError(request=None, capture=3)
        self.service.methods.call = flood
        result = await self.rpc("sendMessage", {"chat_id": 1, "text": "synthetic"})
        self.assertEqual(result["error_code"], 429)
        self.assertEqual(result["parameters"], {"retry_after": 3})

    async def test_state_cannot_be_opened_by_two_workers(self):
        from openclaw_telethon.service import TelethonService
        with self.assertRaises(OSError):
            TelethonService(self.service.config, client=FakeClient())

    async def test_parent_exit_stops_worker(self):
        from openclaw_telethon.service import watch_parent
        stop = asyncio.Event()
        await asyncio.wait_for(watch_parent(2 ** 31 - 1, stop), timeout=2)
        self.assertTrue(stop.is_set())

    async def test_journal_replay_reaches_authenticated_polling_boundary(self):
        from openclaw_telethon.service import JournaledTelegramClient
        self.service.client = JournaledTelegramClient(
            None, 12345, "synthetic-api-hash", transport_store=self.service.store,
            raw_wakeup=self.service.raw_wakeup,
        )
        message = types.Message(
            id=91, peer_id=types.PeerUser(800), from_id=types.PeerUser(800),
            date=datetime(2026, 1, 2, tzinfo=timezone.utc), message="replayed synthetic message",
        )
        await self.service.client._preprocess_updates([
            types.UpdateNewMessage(message=message, pts=9, pts_count=1),
        ], [types.User(id=800, access_hash=1, first_name="Sample")], [])
        self.assertEqual(self.service.store.pending(), [])
        # Restart the persistent owner after raw receipt, before conversion.
        self.service.store.close()
        self.service.store = UpdateStore(Path(self.directory.name) / "transport.sqlite3")
        self.service.client.transport_store = self.service.store
        self.service.tasks.append(asyncio.create_task(self.service.convert_raw()))
        await asyncio.wait_for(self.service.changed.wait(), timeout=2)
        received = await self.rpc("getUpdates")
        self.assertEqual(received["result"][0]["message"]["message_id"], 91)
        self.assertEqual(received["result"][0]["message"]["text"], "replayed synthetic message")
        self.assertIsNone(self.service.store.next_raw())

    async def test_result_only_polls_keep_definition_and_missing_fields_after_restart(self):
        poll = types.Poll(
            id=700, question=types.TextWithEntities("Question", []), hash=0,
            answers=[types.PollAnswer(types.TextWithEntities("Answer", []), b"a")],
        )
        initial = types.UpdateMessagePoll(poll_id=700, poll=poll, results=types.PollResults(
            total_voters=5, results=[types.PollAnswerVoters(option=b"a", voters=5, recent_voters=[])],
        ))
        self.service.restore_poll_definition(initial)
        self.service.store.close()
        self.service.store = UpdateStore(Path(self.directory.name) / "transport.sqlite3")
        partial = types.UpdateMessagePoll(poll_id=700, results=types.PollResults(min=True, total_voters=6))
        self.service.restore_poll_definition(partial)
        self.assertEqual(partial.poll.question.text, "Question")
        self.assertEqual(partial.results.total_voters, 6)
        self.assertEqual(partial.results.results[0].voters, 5)
        retracted = types.UpdateMessagePoll(poll_id=700, results=types.PollResults(
            min=True, total_voters=0, results=[types.PollAnswerVoters(option=b"a", voters=0, recent_voters=[])],
        ))
        self.service.restore_poll_definition(retracted)
        self.assertEqual(retracted.results.total_voters, 0)
        self.assertEqual(retracted.results.results[0].voters, 0)

    async def test_unknown_legacy_poll_does_not_block_later_message(self):
        from openclaw_telethon.service import JournaledTelegramClient
        self.service.client = JournaledTelegramClient(
            None, 12345, "synthetic-api-hash", transport_store=self.service.store,
            raw_wakeup=self.service.raw_wakeup,
        )
        message = types.Message(
            id=101, peer_id=types.PeerUser(900), from_id=types.PeerUser(900),
            date=datetime(2026, 1, 2, tzinfo=timezone.utc), message="message after legacy poll",
        )
        await self.service.client._preprocess_updates([
            types.UpdateMessagePoll(poll_id=888, results=types.PollResults(total_voters=3)),
            types.UpdateNewMessage(message=message, pts=11, pts_count=1),
        ], [types.User(id=900, access_hash=1, first_name="Sample")], [])
        self.service.tasks.append(asyncio.create_task(self.service.convert_raw()))
        await asyncio.wait_for(self.service.changed.wait(), timeout=2)
        result = await self.rpc("getUpdates")
        self.assertEqual(result["result"][0]["message"]["text"], "message after legacy poll")
        self.assertIsNone(self.service.store.next_raw())
        async with self.http.get(self.server.make_url("/health"), headers=self.headers) as response:
            health = (await response.json())["result"]
        self.assertTrue(health["ready"])
        self.assertEqual(health["deferred_poll_updates"]["count"], 1)
        self.assertEqual(health["deferred_poll_updates"]["poll_ids"], ["888"])
        self.service.store.close()
        self.service.store = UpdateStore(Path(self.directory.name) / "transport.sqlite3")
        self.service.client.transport_store = self.service.store
        self.assertEqual(self.service.store.deferred_poll_diagnostics()["count"], 1)

    async def test_outbound_send_poll_caches_definition_through_rpc_observer(self):
        poll = types.Poll(
            id=999, question=types.TextWithEntities("Choose", []), hash=0,
            answers=[types.PollAnswer(types.TextWithEntities("Yes", []), b"a"),
                     types.PollAnswer(types.TextWithEntities("No", []), b"b")],
        )
        response = types.Updates([
            types.UpdateNewMessage(types.Message(
                id=102, peer_id=types.PeerUser(901), out=True, message="",
                date=datetime(2026, 1, 2, tzinfo=timezone.utc),
                media=types.MessageMediaPoll(poll=poll, results=types.PollResults()),
            ), pts=12, pts_count=1),
        ], [types.User(id=901, access_hash=1, first_name="Sample")], [],
            datetime(2026, 1, 2, tzinfo=timezone.utc), 0)
        class PollClient(TelegramClient):
            async def get_input_entity(self, peer):
                return types.InputPeerUser(901, 1)
            async def __call__(self, request, *args, **kwargs):
                bytes(request)
                if isinstance(request, functions.users.GetUsersRequest):
                    return [types.User(id=901, access_hash=1, first_name="Sample")]
                return response
        self.service.client = PollClient(None, 12345, "synthetic-api-hash")
        self.service.methods.client = self.service.client
        result = await self.rpc("sendPoll", {"chat_id": 901, "question": "Choose", "options": ["Yes", "No"]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["poll"]["id"], "999")
        self.assertIsNotNone(self.service.store.get_poll_definition(999))
        self.assertEqual(self.service.store.pending(), [])
        update = types.UpdateMessagePoll(poll_id=999, results=types.PollResults(total_voters=1))
        self.service.restore_poll_definition(update)
        self.assertEqual(update.poll.question.text, "Choose")

    async def test_expired_media_refetches_observed_source_and_retries_same_send(self):
        old = types.Photo(321, 11, b"expired-reference", None, [types.PhotoSize("x", 10, 10, 10)], 2)
        fresh = types.Photo(321, 12, b"fresh-reference", None, [types.PhotoSize("x", 10, 10, 10)], 2)
        source = types.Message(
            id=77, peer_id=types.PeerUser(902), date=datetime(2026, 1, 2, tzinfo=timezone.utc),
            message="", media=types.MessageMediaPhoto(photo=old),
        )
        self.service.remember_media_sources(source, bytes(source.peer_id), source.id)
        sends, fetches = [], []
        class MediaClient(TelegramClient):
            async def get_input_entity(self, peer):
                return types.InputPeerUser(902, 1)
            async def get_messages(self, peer, ids):
                fetches.append((peer.user_id, ids))
                return types.Message(id=77, peer_id=peer, media=types.MessageMediaPhoto(photo=fresh))
            async def __call__(self, request, *args, **kwargs):
                bytes(request)
                if isinstance(request, functions.users.GetUsersRequest):
                    return [types.User(id=902, access_hash=1, first_name="Sample")]
                sends.append((request.media.id.file_reference, request.random_id))
                if len(sends) == 1:
                    raise errors.RPCError(request, "FILE_REFERENCE_EXPIRED", 400)
                return types.Updates([
                    types.UpdateNewMessage(types.Message(
                        id=103, peer_id=types.PeerUser(902), out=True, message="",
                        date=datetime(2026, 1, 2, tzinfo=timezone.utc),
                        media=types.MessageMediaPhoto(photo=fresh),
                    ), pts=13, pts_count=1),
                ], [types.User(id=902, access_hash=1, first_name="Sample")], [],
                    datetime(2026, 1, 2, tzinfo=timezone.utc), 0)
        self.service.client = MediaClient(None, 12345, "synthetic-api-hash")
        self.service.methods.client = self.service.client
        result = await self.rpc("sendPhoto", {"chat_id": 902, "photo": codec.pack_file_id(old)})
        self.assertTrue(result["ok"], result)
        self.assertEqual(fetches, [(902, 77)])
        self.assertEqual([item[0] for item in sends], [b"expired-reference", b"fresh-reference"])
        self.assertEqual(sends[0][1], sends[1][1])

    async def test_unknown_media_origin_does_not_guess_a_refresh_source(self):
        self.assertFalse(await self.service.refresh_media_references(types.InputPhoto(99999, 1, b"unknown")))

    async def test_expired_download_refreshes_observed_source_before_returning_bytes(self):
        old = types.Document(22, 11, b"expired", None, "text/plain", 5, 2, [])
        fresh = types.Document(22, 11, b"fresh", None, "text/plain", 5, 2, [])
        self.service.store.put_media_source("document", 22, bytes(types.PeerUser(902)), 77)
        attempts = []
        class DownloadClient(FakeClient):
            async def get_messages(self, peer, ids):
                return types.Message(id=77, peer_id=peer, media=types.MessageMediaDocument(document=fresh))
            async def download_media(self, media, file):
                attempts.append(media.file_reference)
                if len(attempts) == 1:
                    Path(file).write_bytes(b"partial")
                    raise errors.RPCError(None, "FILE_REFERENCE_EXPIRED", 400)
                Path(file).write_bytes(b"exact")
                return file
        self.service.client = DownloadClient()
        async with self.http.get(self.server.make_url("/download"), params={"file_id": codec.pack_file_id(old)}, headers=self.headers) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), b"exact")
        self.assertEqual(attempts, [b"expired", b"fresh"])

    async def test_sticker_reference_refresh_uses_known_sticker_set(self):
        self.service.store.put_media_source("document", 22, sticker_set=(88, 99))
        fresh = types.Document(22, 12, b"fresh-sticker-reference", None, "image/webp", 100, 2, [])
        requests = []
        class StickerClient(FakeClient):
            async def __call__(self, request):
                bytes(request)
                requests.append((request.stickerset.id, request.stickerset.access_hash))
                return types.messages.StickerSet(
                    types.StickerSet(88, 99, "Sample", "sample", 1, 0), [], [], [fresh],
                )
        self.service.client = StickerClient()
        reference = types.InputDocument(22, 11, b"expired-sticker-reference")
        self.assertTrue(await self.service.refresh_media_references(reference))
        self.assertEqual(requests, [(88, 99)])
        self.assertEqual(reference.file_reference, b"fresh-sticker-reference")
        self.assertEqual(reference.access_hash, 12)

    async def test_journal_write_failure_prevents_telethon_cursor_save(self):
        from openclaw_telethon.service import JournaledTelegramClient
        client = object.__new__(JournaledTelegramClient)
        client.transport_store = self.service.store
        client.raw_wakeup = asyncio.Event()
        client.transport_journal_failed = False
        update = types.UpdateDeleteMessages(messages=[123], pts=7, pts_count=1)
        saved = []
        async def save_base(*args):
            saved.append(True)
        with patch.object(self.service.store, "journal_raw", side_effect=OSError("synthetic full disk")):
            with self.assertRaises(OSError):
                await client._preprocess_updates([update], [], [])
        with patch.object(TelegramClient, "_save_states_and_entities", save_base):
            await client._save_states_and_entities()
        self.assertEqual(saved, [])
        self.assertTrue(client.transport_journal_failed)


if __name__ == "__main__":
    unittest.main()
