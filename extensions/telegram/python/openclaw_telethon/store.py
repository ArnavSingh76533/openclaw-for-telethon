"""Worker-owned SQLite inbox; acknowledged event identities survive restarts.

This is a transport receipt queue, not a second OpenClaw processing/claim loop.
The Node ingress spool remains the owner of channel work. A positive polling
offset acknowledges transport receipts only after that spool has committed them.
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class UpdateStore:
    def __init__(self, path: Path):
        self.connection = sqlite3.connect(str(path))
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS transport_events (
                update_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                payload TEXT
            );
            CREATE INDEX IF NOT EXISTS transport_pending
                ON transport_events(update_id) WHERE payload IS NOT NULL;
            CREATE TABLE IF NOT EXISTS transport_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transport_raw_batches (
                batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_key TEXT NOT NULL UNIQUE,
                envelope BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transport_poll_definitions (
                poll_id TEXT PRIMARY KEY,
                definition BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transport_poll_results (
                poll_id TEXT PRIMARY KEY,
                results BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transport_deferred_polls (
                deferred_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                poll_id TEXT NOT NULL,
                envelope BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transport_media_sources (
                kind TEXT NOT NULL,
                media_id TEXT NOT NULL,
                peer BLOB,
                message_id INTEGER,
                sticker_set_id TEXT,
                sticker_set_access_hash TEXT,
                PRIMARY KEY(kind,media_id)
            );
        """)
        # Bot API offsets from before migration are 32-bit; start above them so
        # the existing committed ingress watermark cannot discard new receipts.
        self.connection.execute(
            "INSERT INTO sqlite_sequence(name,seq) SELECT 'transport_events',? "
            "WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name='transport_events')",
            (time.time_ns() // 1000,),
        )
        self.connection.commit()

    def append(self, event_key: str, update: Dict[str, Any]) -> Optional[int]:
        encoded = json.dumps(update, separators=(",", ":"), ensure_ascii=False)
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO transport_events(event_key,payload) VALUES (?,?)",
                (event_key, encoded),
            )
            return int(cursor.lastrowid) if cursor.rowcount else None

    def journal_raw(self, batch_key: str, envelope: bytes) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO transport_raw_batches(batch_key,envelope) VALUES (?,?)",
                (batch_key, envelope),
            )

    def next_raw(self):
        return self.connection.execute(
            "SELECT batch_id,envelope FROM transport_raw_batches ORDER BY batch_id LIMIT 1"
        ).fetchone()

    def commit_raw(self, batch_id: int, updates, deferred_polls=()) -> None:
        # Raw retirement and normalized publication are one commit. A crash on
        # either side replays raw conversion or exposes the already durable inbox.
        with self.connection:
            if not self.connection.execute(
                "SELECT 1 FROM transport_raw_batches WHERE batch_id=?", (batch_id,),
            ).fetchone():
                return  # An explicit drop_pending_updates retired this receipt.
            self._publish(updates)
            for event_key, poll_id, envelope in deferred_polls:
                self.connection.execute(
                    "INSERT OR IGNORE INTO transport_deferred_polls(event_key,poll_id,envelope) VALUES (?,?,?)",
                    (event_key, str(poll_id), envelope),
                )
            self.connection.execute("DELETE FROM transport_raw_batches WHERE batch_id=?", (batch_id,))

    def _publish(self, updates) -> None:
        for event_key, update in updates:
            self.connection.execute(
                "INSERT OR IGNORE INTO transport_events(event_key,payload) VALUES (?,?)",
                (event_key, json.dumps(update, separators=(",", ":"), ensure_ascii=False)),
            )

    def next_deferred_poll(self):
        return self.connection.execute(
            "SELECT deferred.event_key,deferred.envelope FROM transport_deferred_polls deferred "
            "JOIN transport_poll_definitions definition ON definition.poll_id=deferred.poll_id "
            "ORDER BY deferred.deferred_id LIMIT 1"
        ).fetchone()

    def commit_deferred_poll(self, event_key: str, updates) -> None:
        with self.connection:
            if not self.connection.execute(
                "SELECT 1 FROM transport_deferred_polls WHERE event_key=?", (event_key,),
            ).fetchone():
                return
            self._publish(updates)
            self.connection.execute("DELETE FROM transport_deferred_polls WHERE event_key=?", (event_key,))

    def deferred_poll_diagnostics(self):
        count = self.connection.execute("SELECT count(*) FROM transport_deferred_polls").fetchone()[0]
        poll_ids = [row[0] for row in self.connection.execute(
            "SELECT DISTINCT poll_id FROM transport_deferred_polls ORDER BY poll_id LIMIT 20"
        )]
        return {"count": count, "poll_ids": poll_ids,
                "reason": "awaiting_observed_poll_definition_or_replay" if count else None}

    def acknowledge(self, offset: int) -> None:
        with self.connection:
            if offset < 0:
                # Bot API negative offsets retain only the last N pending updates.
                self.connection.execute(
                    "UPDATE transport_events SET payload=NULL WHERE payload IS NOT NULL "
                    "AND update_id NOT IN (SELECT update_id FROM transport_events "
                    "WHERE payload IS NOT NULL ORDER BY update_id DESC LIMIT ?)",
                    (-offset,),
                )
            elif offset > 0:
                offered = self.get("last_offered_update_id", 0)
                if not offered:
                    return  # A pre-migration cursor cannot acknowledge new receipts.
                if offset > offered + 1:
                    raise ValueError("Polling offset exceeds the last delivered update")
                self.connection.execute(
                    "UPDATE transport_events SET payload=NULL WHERE update_id < ? "
                    "AND payload IS NOT NULL", (offset,),
                )

    def pending(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT update_id,payload FROM transport_events WHERE payload IS NOT NULL "
            "ORDER BY update_id LIMIT ?", (limit,),
        ).fetchall()
        if rows and rows[-1][0] > self.get("last_offered_update_id", 0):
            self.set("last_offered_update_id", rows[-1][0])
        return [{**json.loads(payload), "update_id": update_id} for update_id, payload in rows]

    def discard(self, update_id: int) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE transport_events SET payload=NULL WHERE update_id=?", (update_id,),
            )

    def drop_pending(self) -> None:
        with self.connection:
            self.connection.execute("UPDATE transport_events SET payload=NULL WHERE payload IS NOT NULL")
            self.connection.execute("DELETE FROM transport_raw_batches")
            self.connection.execute("DELETE FROM transport_deferred_polls")

    def pending_count(self) -> int:
        return int(self.connection.execute(
            "SELECT count(*) FROM transport_events WHERE payload IS NOT NULL"
        ).fetchone()[0])

    def get(self, key: str, default: Any = None) -> Any:
        row = self.connection.execute(
            "SELECT value FROM transport_settings WHERE key=?", (key,),
        ).fetchone()
        return json.loads(row[0]) if row else default

    def put_poll_definition(self, poll_id: int, definition: bytes) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO transport_poll_definitions(poll_id,definition) VALUES (?,?) "
                "ON CONFLICT(poll_id) DO UPDATE SET definition=excluded.definition",
                (str(poll_id), definition),
            )

    def get_poll_definition(self, poll_id: int) -> Optional[bytes]:
        row = self.connection.execute(
            "SELECT definition FROM transport_poll_definitions WHERE poll_id=?", (str(poll_id),),
        ).fetchone()
        return row[0] if row else None

    def put_poll_results(self, poll_id: int, results: bytes) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO transport_poll_results(poll_id,results) VALUES (?,?) "
                "ON CONFLICT(poll_id) DO UPDATE SET results=excluded.results",
                (str(poll_id), results),
            )

    def get_poll_results(self, poll_id: int) -> Optional[bytes]:
        row = self.connection.execute(
            "SELECT results FROM transport_poll_results WHERE poll_id=?", (str(poll_id),),
        ).fetchone()
        return row[0] if row else None

    def put_media_source(self, kind, media_id, peer=None, message_id=None, sticker_set=None):
        sticker_id, sticker_hash = sticker_set if sticker_set else (None, None)
        with self.connection:
            self.connection.execute(
                "INSERT INTO transport_media_sources(kind,media_id,peer,message_id,sticker_set_id,sticker_set_access_hash) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(kind,media_id) DO UPDATE SET "
                "peer=COALESCE(excluded.peer,transport_media_sources.peer), "
                "message_id=COALESCE(excluded.message_id,transport_media_sources.message_id), "
                "sticker_set_id=COALESCE(excluded.sticker_set_id,transport_media_sources.sticker_set_id), "
                "sticker_set_access_hash=COALESCE(excluded.sticker_set_access_hash,transport_media_sources.sticker_set_access_hash)",
                (kind, str(media_id), peer, message_id,
                 str(sticker_id) if sticker_id is not None else None,
                 str(sticker_hash) if sticker_hash is not None else None),
            )

    def get_media_source(self, kind, media_id):
        row = self.connection.execute(
            "SELECT peer,message_id,sticker_set_id,sticker_set_access_hash FROM transport_media_sources "
            "WHERE kind=? AND media_id=?", (kind, str(media_id)),
        ).fetchone()
        if row is None:
            return None
        return {"peer": row[0], "message_id": row[1],
                "sticker_set_id": int(row[2]) if row[2] is not None else None,
                "sticker_set_access_hash": int(row[3]) if row[3] is not None else None}

    def set(self, key: str, value: Any) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO transport_settings(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, separators=(",", ":"))),
            )

    def close(self) -> None:
        self.connection.close()
