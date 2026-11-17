"""Call journal and idempotency ledger. Scheduling belongs to Hermes cron."""

import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ID = re.compile(r"[a-f0-9]{32}\Z")
SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")


def validate_chat_ref(value: str) -> str:
    if not isinstance(value, str) or not SESSION.fullmatch(value) or value == "latest":
        raise ValueError("chat_ref must be an explicit Hermes session ID")
    return value


def validate_call(data: dict, allowed: tuple[str, ...]) -> dict:
    if set(data) - {"extension", "message", "request_id", "chat_ref"}:
        raise ValueError("unknown field (scheduling belongs to Hermes cron)")
    ext, message, key = data.get("extension"), data.get("message"), data.get("request_id")
    if not isinstance(ext, str) or ext not in allowed:
        raise ValueError("extension is not allowed")
    if not isinstance(message, str) or not message.strip() or len(message) > 2000:
        raise ValueError("message must contain 1..2000 characters")
    if not isinstance(key, str) or not ID.fullmatch(key):
        raise ValueError("request_id must be 32 lowercase hex characters; reuse on retries")
    reference = data.get("chat_ref")
    if reference is not None:
        validate_chat_ref(reference)
    return {"id": key, "extension": ext, "message": message.strip(), "chat_ref": reference}


class Store:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, extension TEXT NOT NULL, message TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL,
                state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')""")
            columns = {r["name"] for r in db.execute("PRAGMA table_info(calls)")}
            if "chat_ref" not in columns:
                db.execute("ALTER TABLE calls ADD COLUMN chat_ref TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS bindings (
                extension TEXT PRIMARY KEY, chat_ref TEXT NOT NULL, updated REAL NOT NULL)""")
        if os.name == "posix":
            path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, call: dict) -> tuple[dict, bool]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM calls WHERE id=?", (call["id"],)).fetchone()
            if existing:
                if (
                    existing["extension"] != call["extension"]
                    or existing["message"] != call["message"]
                    or existing["chat_ref"] != call.get("chat_ref")
                ):
                    raise ValueError("request_id already used for a different call")
                return dict(existing), False
            now = time.time()
            db.execute(
                "INSERT INTO calls VALUES (?,?,?,?,?,?,?,?)",
                (
                    call["id"],
                    call["extension"],
                    call["message"],
                    now,
                    now,
                    "preparing",
                    "",
                    call.get("chat_ref"),
                ),
            )
            return dict(
                db.execute("SELECT * FROM calls WHERE id=?", (call["id"],)).fetchone()
            ), True

    def list(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                dict(r) for r in db.execute("SELECT * FROM calls ORDER BY created DESC LIMIT 100")
            ]

    def get(self, call_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
            return dict(row) if row else None

    def transition(self, call_id: str, old: str, new: str, detail: str = "") -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "UPDATE calls SET state=?,detail=?,updated=? WHERE id=? AND state=?",
                    (new, detail, time.time(), call_id, old),
                ).rowcount
                == 1
            )

    def recover(self) -> None:
        with self.connect() as db:
            db.execute(
                """UPDATE calls SET state='unknown',
                detail='service restarted; inspect PBX before redialing', updated=?
                WHERE state IN ('preparing','dialing','active')""",
                (time.time(),),
            )

    def bind(self, extension: str, chat_ref: str) -> dict:
        validate_chat_ref(chat_ref)
        with self.connect() as db:
            db.execute(
                """INSERT INTO bindings VALUES (?,?,?) ON CONFLICT(extension)
                DO UPDATE SET chat_ref=excluded.chat_ref, updated=excluded.updated""",
                (extension, chat_ref, time.time()),
            )
        return {"extension": extension, "chat_ref": chat_ref}

    def binding(self, extension: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT chat_ref FROM bindings WHERE extension=?", (extension,)
            ).fetchone()
            return row[0] if row else None

    def unbind(self, extension: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM bindings WHERE extension=?", (extension,))


def new_id() -> str:
    return uuid.uuid4().hex
