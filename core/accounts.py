"""Persistent user accounts for the web control panel."""

from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import uuid


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=16384,
        r=8,
        p=1,
        dklen=32,
    )
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password, encoded):
    try:
        algorithm, n, r, p, salt_hex, digest_hex = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


class AccountStore:
    def __init__(self, database_path):
        self.database_path = database_path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(database_path), exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    email TEXT NOT NULL DEFAULT '',
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_login_at TEXT,
                    last_seen_at TEXT,
                    last_ip TEXT,
                    login_count INTEGER NOT NULL DEFAULT 0,
                    session_version INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS users_role_idx ON users(role);
                """
            )

    @staticmethod
    def _public(row):
        if row is None:
            return None
        return {
            key: row[key]
            for key in row.keys()
            if key != "password_hash"
        }

    def bootstrap_admin(self, username, password_hash):
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM users WHERE role = 'admin' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if existing:
                conflict = connection.execute(
                    "SELECT id FROM users WHERE username = ? AND id != ?",
                    (username, existing["id"]),
                ).fetchone()
                if conflict:
                    raise RuntimeError(
                        "Yönetici kullanıcı adı başka bir hesap tarafından kullanılıyor."
                    )
                credentials_changed = (
                    existing["username"].casefold() != username.casefold()
                    or existing["password_hash"] != password_hash
                )
                connection.execute(
                    """
                    UPDATE users
                    SET username = ?, password_hash = ?, active = 1,
                        session_version = session_version + ?
                    WHERE id = ?
                    """,
                    (
                        username,
                        password_hash,
                        1 if credentials_changed else 0,
                        existing["id"],
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM users WHERE id = ?",
                    (existing["id"],),
                ).fetchone()
                return self._public(updated)
            user_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO users (
                    id, username, password_hash, role, active, created_at
                ) VALUES (?, ?, ?, 'admin', 1, ?)
                """,
                (user_id, username, password_hash, utc_now()),
            )
        return self.get(user_id)

    def create_user(self, username, email, password):
        user_id = uuid.uuid4().hex
        with self._lock, self._connection() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO users (
                        id, username, email, password_hash, role, active, created_at
                    ) VALUES (?, ?, ?, ?, 'user', 1, ?)
                    """,
                    (
                        user_id,
                        username,
                        email or "",
                        hash_password(password),
                        utc_now(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("Bu kullanıcı adı zaten kullanılıyor.") from error
        return self.get(user_id)

    def authenticate(self, username, password, ip_address):
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if not row or not verify_password(password, row["password_hash"]):
                return None
            if not row["active"]:
                raise ValueError("Bu hesap yönetici tarafından devre dışı bırakıldı.")
            now = utc_now()
            connection.execute(
                """
                UPDATE users
                SET last_login_at = ?, last_seen_at = ?, last_ip = ?,
                    login_count = login_count + 1
                WHERE id = ?
                """,
                (now, now, ip_address, row["id"]),
            )
        return self.get(row["id"])

    def get(self, user_id):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return self._public(row)

    def get_by_username(self, username):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        return self._public(row)

    def list_users(self):
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM users
                ORDER BY role = 'admin' DESC, created_at DESC
                """
            ).fetchall()
        return [self._public(row) for row in rows]

    def touch(self, user_id):
        with self._connection() as connection:
            connection.execute(
                "UPDATE users SET last_seen_at = ? WHERE id = ?",
                (utc_now(), user_id),
            )

    def set_active(self, user_id, active):
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT role FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if not row:
                raise ValueError("Kullanıcı bulunamadı.")
            if row["role"] == "admin" and not active:
                raise ValueError("Ana yönetici hesabı devre dışı bırakılamaz.")
            connection.execute(
                """
                UPDATE users
                SET active = ?, session_version = session_version + 1
                WHERE id = ?
                """,
                (1 if active else 0, user_id),
            )
        return self.get(user_id)

    def invalidate_sessions(self, user_id):
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE users
                SET session_version = session_version + 1
                WHERE id = ?
                """,
                (user_id,),
            )
        return self.get(user_id)
