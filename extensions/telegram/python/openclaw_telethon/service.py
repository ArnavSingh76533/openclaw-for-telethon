"""Authenticated loopback worker. Credentials enter once over private stdin."""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import ssl
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import aiohttp
from aiohttp import web
import telethon
from telethon import TelegramClient, errors, functions, types
from telethon.extensions import BinaryReader
from telethon.tl.tlobject import TLObject

from . import PROTOCOL, PROTOCOL_VERSION
from .codec import raw_update_to_bot, resolve_file_id
from .methods import MtprotoMethods
from .store import UpdateStore


class TransportError(Exception):
    def __init__(self, code, description):
        super().__init__(description)
        self.error_code = code


class JournaledTelegramClient(TelegramClient):
    """Journal before Telethon can persist its advanced MTProto cursor.

    This private hook is deliberately tied to Telethon 1.45.0. Its update loop
    calls this hook immediately after synchronous message-box state changes.
    There is no await before our SQLite FULL commit. Returning no dispatchable
    updates leaves conversion/replay with the durable worker below. Revalidate
    this ordering before upgrading Telethon.
    """

    def __init__(self, *args, transport_store, raw_wakeup, **kwargs):
        if telethon.__version__ != "1.45.0":
            raise RuntimeError("The durable update hook requires Telethon 1.45.0")
        self.transport_store = transport_store
        self.raw_wakeup = raw_wakeup
        self.transport_journal_failed = False
        super().__init__(*args, **kwargs)

    async def _preprocess_updates(self, updates, users, chats):
        if updates:
            try:
                envelope = bytes(types.Updates(
                    updates=updates, users=users, chats=chats,
                    date=datetime(2000, 1, 1, tzinfo=timezone.utc), seq=0,
                ))
                self.transport_store.journal_raw(hashlib.sha256(envelope).hexdigest(), envelope)
            except Exception:
                # Telethon disconnects on hook errors. Its disconnect path must
                # retain the previous cursor when this receipt did not commit.
                self.transport_journal_failed = True
                raise
            self.raw_wakeup.set()
        await super()._preprocess_updates(updates, users, chats)
        return []

    async def _save_states_and_entities(self):
        if self.transport_journal_failed:
            return
        await super()._save_states_and_entities()


def rpc_error(error, method=None):
    code = (getattr(error, "error_code", None) or getattr(error, "code", None)
            or getattr(type(error), "code", None) or 500)
    parameters = getattr(error, "parameters", None)
    if getattr(error, "seconds", None) is not None:
        code, parameters = 429, {"retry_after": error.seconds}
    if isinstance(error, (ValueError, TypeError, KeyError)):
        code = 400
    description = getattr(error, "description", None) or str(error) or type(error).__name__
    rpc_name = getattr(error, "message", None)
    rpc_descriptions = {
        "MESSAGE_NOT_MODIFIED": "Bad Request: message is not modified",
        "ENTITY_BOUNDS_INVALID": "Bad Request: can't parse entities",
        "ENTITIES_TOO_LONG": "Bad Request: can't parse entities",
        "ENTITY_MENTION_USER_INVALID": "Bad Request: can't parse entities",
        "QUOTE_TEXT_INVALID": "Bad Request: quote not found",
        "QUOTE_TEXT_EMPTY": "Bad Request: quote not found",
        "REPLY_MESSAGE_ID_INVALID": "Bad Request: replied message not found",
        "CHAT_WRITE_FORBIDDEN": "Forbidden: CHAT_WRITE_FORBIDDEN",
        "PHOTO_INVALID_DIMENSIONS": "Bad Request: PHOTO_INVALID_DIMENSIONS",
        "PHOTO_SAVE_FILE_INVALID": "Bad Request: PHOTO_INVALID_DIMENSIONS",
        "VOICE_MESSAGES_FORBIDDEN": "Bad Request: VOICE_MESSAGES_FORBIDDEN",
        "MEDIA_CAPTION_TOO_LONG": "Bad Request: caption is too long",
        "QUERY_ID_INVALID": "Bad Request: query is too old and response timeout expired or query ID is invalid",
        "USER_IS_BLOCKED": "Forbidden: bot was blocked by the user",
        "INPUT_USER_DEACTIVATED": "Forbidden: user is deactivated",
    }
    class_name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(error).__name__.removesuffix("Error")).upper()
    description = rpc_descriptions.get(rpc_name, rpc_descriptions.get(class_name, description))
    if "MESSAGE_ID_INVALID" in (rpc_name, class_name) and method and method.startswith("editMessage"):
        description = "Bad Request: message to edit not found"
    result = {"ok": False, "error_code": int(code), "description": description}
    if parameters:
        result["parameters"] = parameters
    if getattr(error, "unsupported_method", False):
        result["unsupported_method"] = True
    return result


class TelethonService:
    def __init__(self, config, client=None):
        self.config = config
        self.secret = config["secret"]
        if not isinstance(self.secret, str) or len(self.secret) < 32:
            raise ValueError("Worker secret must contain at least 32 characters")
        self.state_dir = Path(config["state_dir"])
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.state_dir.chmod(0o700)
        # Telethon's session SQLite file needs one live process owner; a second
        # Gateway must fail before opening that session, even on another port.
        self.state_lock = (self.state_dir / "telethon.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.state_lock.seek(0, os.SEEK_END)
                if self.state_lock.tell() == 0:
                    self.state_lock.write(b"\0")
                    self.state_lock.flush()
                self.state_lock.seek(0)
                msvcrt.locking(self.state_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.state_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self.state_lock.close()
            raise
        self.store = UpdateStore(self.state_dir / "transport.sqlite3")
        self.raw_wakeup = asyncio.Event()
        self.changed = asyncio.Event()
        self.stop = asyncio.Event()
        self.mode_lock = asyncio.Lock()
        self.poll_lock = asyncio.Lock()
        self.tasks = []
        self.ready = False
        self.bot_id = None
        self.conversion_error = None
        self.metadata_cache_error = None
        proxy = config.get("proxy")
        if isinstance(proxy, str):
            parsed = urlsplit(proxy)
            if parsed.scheme not in ("socks5", "socks4", "http") or not parsed.hostname:
                raise ValueError("Invalid Telethon proxy URL")
            proxy = {"proxy_type": parsed.scheme, "addr": parsed.hostname,
                     "port": parsed.port or (8080 if parsed.scheme == "http" else 1080),
                     "username": unquote(parsed.username) if parsed.username else None,
                     "password": unquote(parsed.password) if parsed.password else None,
                     "rdns": True}
        self.client = client or JournaledTelegramClient(
            str(self.state_dir / "telethon"), int(config["api_id"]), config["api_hash"],
            transport_store=self.store, raw_wakeup=self.raw_wakeup,
            sequential_updates=True, catch_up=True, proxy=proxy,
            flood_sleep_threshold=0,
        )
        self.methods = MtprotoMethods(
            self.client, update_observer=self.observe_mtproto_result,
            media_refresher=self.refresh_media_references,
        )
        self.http = None
        self.runner = None
        self.app = web.Application(middlewares=[self.authenticate], client_max_size=1024 ** 3)
        self.app.add_routes([
            web.get("/health", self.health), web.post("/rpc", self.rpc),
            web.get("/download", self.download),
        ])

    @web.middleware
    async def authenticate(self, request, handler):
        headers = request.headers.getall("X-OpenClaw-Telethon", [])
        if len(headers) != 1 or not hmac.compare_digest(headers[0].encode(), self.secret.encode()):
            response = web.json_response(
                {"ok": False, "error_code": 401, "description": "Unauthorized"}, status=401,
            )
            response.force_close()
            return response
        return await handler(request)

    async def health(self, request):
        return web.json_response({"ok": True, "result": {
            "protocol": PROTOCOL, "version": PROTOCOL_VERSION,
            "ready": self.ready and self.conversion_error is None
                     and not getattr(self.client, "transport_journal_failed", False)
                     and not getattr(self.client, "_updates_error", None),
            "bot_id": self.bot_id,
            "pending_update_count": self.store.pending_count(),
            "conversion_error": self.conversion_error,
            "deferred_poll_updates": self.store.deferred_poll_diagnostics(),
            "metadata_cache_error": self.metadata_cache_error,
        }})

    async def start(self):
        self.runner = web.AppRunner(self.app, access_log=None, shutdown_timeout=2, handler_cancellation=True)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", int(self.config["port"]))
        await site.start()
        await self.client.start(bot_token=self.config["token"])
        me = await self.client.get_me()
        expected_bot = int(self.config["token"].split(":", 1)[0])
        if not me.bot or me.id != expected_bot:
            raise RuntimeError("Persisted MTProto session belongs to a different bot")
        self.bot_id = me.id
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        self.tasks = [asyncio.create_task(self.convert_raw()), asyncio.create_task(self.forward_webhook())]
        for task in self.tasks:
            task.add_done_callback(self.task_finished)
        self.ready = True
        self.raw_wakeup.set()
        self.changed.set()
        return site._server.sockets[0].getsockname()[1]

    def task_finished(self, task):
        if not task.cancelled() and task.exception() is not None:
            self.conversion_error = type(task.exception()).__name__
            self.stop.set()

    async def convert_raw(self):
        while not self.stop.is_set():
            self.raw_wakeup.clear()
            row = self.store.next_raw()
            deferred_key = None
            if row is None:
                deferred = self.store.next_deferred_poll()
                if deferred is None:
                    await self.raw_wakeup.wait()
                    continue
                deferred_key, envelope_bytes = deferred
            else:
                batch_id, envelope_bytes = row
            try:
                with BinaryReader(envelope_bytes) as reader:
                    envelope = reader.tgread_object()
                updates = await TelegramClient._preprocess_updates(
                    self.client, envelope.updates, envelope.users, envelope.chats,
                )
                normalized, deferred_polls = [], []
                for update in updates:
                    key = hashlib.sha256(bytes(update)).hexdigest()
                    if isinstance(update, types.UpdateMessagePoll) and update.poll is None and not self.store.get_poll_definition(update.poll_id):
                        # Legacy result-only events have no chat/message location
                        # to hydrate. Retain their original TL bytes separately;
                        # unlike a transient conversion error, they must not hold
                        # unrelated chat ingress behind an unknowable definition.
                        original = bytes(types.Updates(
                            updates=[update], users=envelope.users, chats=envelope.chats,
                            date=envelope.date, seq=envelope.seq,
                        ))
                        deferred_polls.append((key, update.poll_id, original))
                        self.merge_poll_results(update.poll_id, update.results)
                        continue
                    self.restore_poll_definition(update)
                    message = getattr(update, "message", None)
                    if isinstance(message, types.Message):
                        self.remember_media_sources(message, bytes(message.peer_id), message.id)
                    if isinstance(update, types.UpdateBotBusinessConnect):
                        self.methods.remember_business_connection(update.connection)
                    translated = await raw_update_to_bot(self.client, update)
                    if translated is None:
                        continue  # MTProto transport/status events have no Bot API equivalent.
                    for index, item in enumerate(translated if isinstance(translated, list) else [translated]):
                        normalized.append(("%s:%d" % (key, index), item))
                if deferred_key is not None:
                    self.store.commit_deferred_poll(deferred_key, normalized)
                else:
                    self.store.commit_raw(batch_id, normalized, deferred_polls)
                self.conversion_error = None
                self.changed.set()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Keep the raw receipt for restart/retry. Only the exception class
                # enters health diagnostics, never message content or credentials.
                self.conversion_error = type(error).__name__
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass

    def restore_poll_definition(self, update):
        # MTProto result-only updates omit the question/options. Retain the TL
        # definition from observed incoming and outgoing poll messages so these
        # updates keep the complete Bot API poll shape across worker restarts.
        media = getattr(getattr(update, "message", None), "media", None)
        if isinstance(media, types.MessageMediaPoll):
            self.store.put_poll_definition(media.poll.id, bytes(media.poll))
            media.results = self.merge_poll_results(media.poll.id, media.results)
        if isinstance(update, types.UpdateMessagePoll):
            if update.poll is not None:
                self.store.put_poll_definition(update.poll.id, bytes(update.poll))
            else:
                definition = self.store.get_poll_definition(update.poll_id)
                if definition:
                    with BinaryReader(definition) as reader:
                        update.poll = reader.tgread_object()
            update.results = self.merge_poll_results(update.poll_id, update.results)

    def observe_mtproto_result(self, result):
        # RPC-returned Updates do not go through Telethon's incoming update
        # dispatch. Cache outbound poll definitions before returning sendPoll;
        # this observer never publishes the bot's own message as inbound work.
        updates = getattr(result, "updates", None)
        if updates is None:
            update = getattr(result, "update", None)
            updates = [update] if update is not None else []
        documents = result if isinstance(result, (list, tuple)) else getattr(result, "documents", [])
        if not updates and not documents:
            return
        try:
            for update in updates:
                self.restore_poll_definition(update)
                message = getattr(update, "message", None)
                if isinstance(message, types.Message):
                    self.remember_media_sources(message, bytes(message.peer_id), message.id)
                if isinstance(update, types.UpdateBotBusinessConnect):
                    self.methods.remember_business_connection(update.connection)
            # Sticker-set responses carry standalone documents whose sticker-set
            # attribute supplies a refresh source even without a chat message.
            self.remember_media_sources(documents)
            self.metadata_cache_error = None
            self.raw_wakeup.set()
        except Exception as error:
            # Sending already succeeded. Do not turn cache publication trouble
            # into a send failure that could cause a duplicate visible message.
            self.metadata_cache_error = type(error).__name__

    @staticmethod
    def walk_tl(value, seen=None):
        if seen is None:
            seen = set()
        if isinstance(value, TLObject):
            if id(value) in seen:
                return
            seen.add(id(value))
            yield value
            for name, child in vars(value).items():
                if not name.startswith("_"):
                    yield from TelethonService.walk_tl(child, seen)
        elif isinstance(value, (list, tuple)):
            for child in value:
                yield from TelethonService.walk_tl(child, seen)

    def remember_media_sources(self, value, peer=None, message_id=None):
        for media in self.walk_tl(value):
            if not isinstance(media, (types.Photo, types.Document)):
                continue
            kind = "photo" if isinstance(media, types.Photo) else "document"
            sticker_set = None
            for attribute in getattr(media, "attributes", []):
                source = getattr(attribute, "stickerset", None)
                if isinstance(source, types.InputStickerSetID):
                    sticker_set = (source.id, source.access_hash)
                    break
            if peer is not None or sticker_set is not None:
                self.store.put_media_source(kind, media.id, peer, message_id, sticker_set)

    async def refresh_media_references(self, request):
        targets = {}
        for media in self.walk_tl(request):
            if isinstance(media, (types.Photo, types.InputPhoto)):
                targets.setdefault(("photo", media.id), []).append(media)
            elif isinstance(media, (types.Document, types.InputDocument)):
                targets.setdefault(("document", media.id), []).append(media)
        refreshed = False
        for (kind, media_id), references in targets.items():
            source = self.store.get_media_source(kind, media_id)
            if source is None or self.stop.is_set():
                continue
            fresh = None
            if source["peer"] is not None and source["message_id"] is not None:
                with BinaryReader(source["peer"]) as reader:
                    peer = reader.tgread_object()
                message = await self.client.get_messages(peer, ids=source["message_id"])
                if isinstance(message, types.Message):
                    self.remember_media_sources(message, source["peer"], message.id)
                    fresh = self.find_media(message, kind, media_id)
            if fresh is None and source["sticker_set_id"] is not None:
                result = await self.client(functions.messages.GetStickerSetRequest(
                    types.InputStickerSetID(source["sticker_set_id"], source["sticker_set_access_hash"]), hash=0,
                ))
                self.remember_media_sources(getattr(result, "documents", []))
                fresh = self.find_media(result, kind, media_id)
            if fresh is not None and not self.stop.is_set():
                for reference in references:
                    if (reference.file_reference, reference.access_hash) != (fresh.file_reference, fresh.access_hash):
                        reference.file_reference = fresh.file_reference
                        reference.access_hash = fresh.access_hash
                        refreshed = True
        return refreshed and not self.stop.is_set()

    def find_media(self, value, kind, media_id):
        expected = types.Photo if kind == "photo" else types.Document
        return next((item for item in self.walk_tl(value)
                     if isinstance(item, expected) and item.id == media_id), None)

    def merge_poll_results(self, poll_id, results):
        prior = self.store.get_poll_results(poll_id)
        if prior:
            with BinaryReader(prior) as reader:
                previous = reader.tgread_object()
            # The TL constructor permits these fields to be absent. A present
            # results vector is authoritative, including zero vote counts; min
            # denotes missing user-specific choice data, not partial totals.
            for field in ("results", "total_voters", "solution", "solution_entities"):
                if getattr(results, field) is None:
                    setattr(results, field, getattr(previous, field))
        self.store.put_poll_results(poll_id, bytes(results))
        return results

    async def parse_rpc(self, request, directory):
        if request.content_type != "multipart/form-data":
            body = await request.json()
            return body["method"], body.get("payload", {})
        reader = await request.multipart()
        files, body = {}, None
        async for part in reader:
            if part.name == "payload" and not part.filename:
                body = json.loads(await part.text())
                continue
            if not part.name:
                raise ValueError("Multipart files require a field name")
            path = Path(directory) / ("upload-%d" % len(files))
            with path.open("wb") as target:
                while True:
                    chunk = await part.read_chunk()
                    if not chunk:
                        break
                    target.write(chunk)
            files[part.name] = {"__upload__": str(path), "filename": part.filename or part.name,
                                "content_type": part.headers.get("Content-Type", "application/octet-stream")}
        if not isinstance(body, dict):
            raise ValueError("Multipart request requires a JSON payload field")
        def replace(value):
            if isinstance(value, str) and value.startswith("attach://"):
                if value[9:] not in files:
                    raise ValueError("Missing multipart attachment")
                return files[value[9:]]
            if isinstance(value, list):
                return [replace(item) for item in value]
            if isinstance(value, dict):
                return {key: replace(item) for key, item in value.items()}
            return value
        return body["method"], replace(body.get("payload", {}))

    async def rpc(self, request):
        method = None
        try:
            if not self.ready:
                raise TransportError(503, "MTProto worker is not ready")
            with tempfile.TemporaryDirectory(prefix="upload-", dir=str(self.state_dir)) as directory:
                method, payload = await self.parse_rpc(request, directory)
                if not isinstance(method, str) or not isinstance(payload, dict):
                    raise ValueError("RPC requires method string and payload object")
                if method == "getUpdates":
                    result = await self.get_updates(payload)
                elif method in ("setWebhook", "deleteWebhook", "getWebhookInfo"):
                    result = await self.webhook_rpc(method, payload)
                else:
                    result = await self.methods.call(method, payload)
                return web.json_response({"ok": True, "result": result})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return web.json_response(rpc_error(error, method))

    def filter_updates(self, updates):
        allowed = self.store.get("allowed_updates", [])
        # Bot API's empty list excludes opt-in membership/reaction updates.
        excluded = {"chat_member", "message_reaction", "message_reaction_count"}
        selected = []
        for update in updates:
            kinds = set(update) - {"update_id"}
            if (not allowed and not kinds.intersection(excluded)) or kinds.intersection(allowed):
                selected.append(update)
            else:
                self.store.discard(update["update_id"])
        return selected

    async def get_updates(self, payload):
        if self.poll_lock.locked():
            raise TransportError(409, "Conflict: another getUpdates request is active")
        async with self.poll_lock:
            limit = int(payload.get("limit", 100))
            timeout = float(payload.get("timeout", 0))
            if not 1 <= limit <= 100 or not 0 <= timeout <= 50:
                raise ValueError("getUpdates limit must be 1..100 and timeout 0..50")
            deadline = asyncio.get_running_loop().time() + timeout
            async with self.mode_lock:
                if self.store.get("webhook", {}).get("url"):
                    raise TransportError(409, "Conflict: webhook is active; call deleteWebhook first")
                if "allowed_updates" in payload:
                    self.validate_allowed_updates(payload["allowed_updates"])
                    self.store.set("allowed_updates", payload["allowed_updates"])
                self.store.acknowledge(int(payload.get("offset", 0)))
            while True:
                self.changed.clear()
                async with self.mode_lock:
                    if self.store.get("webhook", {}).get("url"):
                        raise TransportError(409, "Conflict: webhook is active")
                    batch = self.filter_updates(self.store.pending(limit))
                remaining = deadline - asyncio.get_running_loop().time()
                if not batch and (self.conversion_error or getattr(self.client, "_updates_error", None)):
                    raise TransportError(500, "MTProto update receipt requires recovery; inspect worker health")
                if batch or remaining <= 0:
                    return batch
                # Filters may have consumed a full page; inspect the next page
                # without waiting for an unrelated network event.
                if self.store.pending(1):
                    continue
                try:
                    await asyncio.wait_for(self.changed.wait(), remaining)
                except asyncio.TimeoutError:
                    return []

    @staticmethod
    def validate_allowed_updates(value):
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("allowed_updates must be an array of update type strings")

    async def webhook_rpc(self, method, payload):
        async with self.mode_lock:
            if method == "getWebhookInfo":
                webhook = self.store.get("webhook", {})
                return {"url": webhook.get("url", ""), "has_custom_certificate": bool(webhook.get("certificate")),
                        "pending_update_count": self.store.pending_count(),
                        "allowed_updates": self.store.get("allowed_updates", [])}
            if "allowed_updates" in payload:
                self.validate_allowed_updates(payload["allowed_updates"])
            webhook = {}
            if method == "setWebhook" and payload.get("url"):
                parsed = urlsplit(payload["url"])
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    raise ValueError("Invalid webhook URL")
                webhook = {"url": payload["url"], "secret_token": payload.get("secret_token")}
                certificate = payload.get("certificate")
                if certificate:
                    if not isinstance(certificate, dict) or "__upload__" not in certificate:
                        raise ValueError("Webhook certificate requires an uploaded PEM file")
                    pem = Path(certificate["__upload__"]).read_text(encoding="ascii")
                    ssl.create_default_context(cadata=pem)
                    webhook["certificate"] = pem
            if payload.get("drop_pending_updates"):
                self.store.drop_pending()
            self.store.set("webhook", webhook)
            if "allowed_updates" in payload:
                self.store.set("allowed_updates", payload["allowed_updates"])
            self.changed.set()
            return True

    async def forward_webhook(self):
        while not self.stop.is_set():
            delivered = False
            async with self.mode_lock:
                webhook = self.store.get("webhook", {})
                if webhook.get("url"):
                    pending = self.filter_updates(self.store.pending(1))
                    if pending:
                        headers = {}
                        if webhook.get("secret_token"):
                            headers["X-Telegram-Bot-Api-Secret-Token"] = webhook["secret_token"]
                        tls = ssl.create_default_context(cadata=webhook["certificate"]) if webhook.get("certificate") else True
                        try:
                            async with self.http.post(webhook["url"], json=pending[0], headers=headers,
                                                      allow_redirects=False, ssl=tls) as response:
                                if 200 <= response.status < 300:
                                    self.store.acknowledge(pending[0]["update_id"] + 1)
                                    delivered = True
                        except (aiohttp.ClientError, asyncio.TimeoutError):
                            pass  # Preserve the receipt; the retained webhook owns spool durability.
            if delivered:
                continue
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def download(self, request):
        path, response = None, None
        try:
            if not self.ready:
                raise TransportError(503, "MTProto worker is not ready")
            file_id = request.query.get("file_id", "")
            media = resolve_file_id(file_id)
            if media is None:
                raise ValueError("Invalid Telegram file_id")
            descriptor, path = tempfile.mkstemp(prefix="download-", dir=str(self.state_dir))
            os.close(descriptor)
            try:
                downloaded = await self.client.download_media(media, file=path)
            except errors.RPCError as error:
                if (not re.fullmatch(r"FILE_REFERENCE(?:_\d+)?_(?:EXPIRED|INVALID|EMPTY)", error.message)
                        or not await self.refresh_media_references(media)):
                    raise
                downloaded = await self.client.download_media(media, file=path)
            if not downloaded:
                raise TransportError(400, "Telegram media could not be downloaded")
            response = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
            response.content_length = Path(path).stat().st_size
            await response.prepare(request)
            with open(path, "rb") as source:
                while True:
                    chunk = source.read(65536)
                    if not chunk:
                        break
                    await response.write(chunk)
            await response.write_eof()
            return response
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if response is not None and response.prepared:
                raise
            return web.json_response(rpc_error(error), status=400)
        finally:
            if path:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(path)

    async def close(self):
        self.ready = False
        self.stop.set()
        self.changed.set()
        self.raw_wakeup.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.runner:
            await self.runner.cleanup()
        if self.http:
            await self.http.close()
        try:
            await self.client.disconnect()
        finally:
            self.store.close()
            self.state_lock.close()


async def watch_parent(parent_pid, stop):
    handle = None
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000 | 0x1000, False, parent_pid)
        if not handle:
            stop.set()
            return
    try:
        while not stop.is_set():
            if handle:
                status = ctypes.c_ulong()
                if not kernel.GetExitCodeProcess(handle, ctypes.byref(status)) or status.value != 259:
                    stop.set()
                    return
            else:
                try:
                    os.kill(parent_pid, 0)
                except ProcessLookupError:
                    stop.set()
                    return
                except PermissionError:
                    pass  # A living parent can retain a different user identity.
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
    finally:
        if handle:
            kernel.CloseHandle(handle)


async def run(config):
    service = TelethonService(config)
    loop = asyncio.get_running_loop()
    parent_task = asyncio.create_task(watch_parent(int(config.get("parent_pid", os.getppid())), service.stop))
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, service.stop.set)
    try:
        startup = asyncio.create_task(service.start())
        stopped = asyncio.create_task(service.stop.wait())
        done, _ = await asyncio.wait([startup, stopped], return_when=asyncio.FIRST_COMPLETED)
        if stopped in done:
            startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
            return
        stopped.cancel()
        await asyncio.gather(stopped, return_exceptions=True)
        port = startup.result()
        print(json.dumps({"protocol": PROTOCOL, "version": PROTOCOL_VERSION,
                          "ready": True, "port": port}), flush=True)
        await service.stop.wait()
    finally:
        parent_task.cancel()
        await asyncio.gather(parent_task, return_exceptions=True)
        await service.close()


def main():
    # Never let dependency diagnostics write credentials or message content.
    logging.disable(logging.CRITICAL)
    try:
        line = sys.stdin.buffer.readline(1024 * 1024 + 1)
        if len(line) > 1024 * 1024:
            raise ValueError("Worker startup configuration is too large")
        config = json.loads(line)
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print("Telethon worker failed (%s)" % type(error).__name__, file=sys.stderr, flush=True)
        raise SystemExit(1)
