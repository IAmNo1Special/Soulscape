"""
Database configuration, initialization, and shared utility functions for Soulscape Hub.
Uses SQLite with WAL mode enabled for better concurrency.
"""

import logging
import os
import sqlite3
import time
from contextlib import contextmanager

from argon2 import PasswordHasher
from fastapi import HTTPException

logger = logging.getLogger("soulscape_hub")

# Argon2 hasher for soul secrets
_hasher = PasswordHasher()

# Ensure DB path is absolute relative to this file
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "soulscape_hub.db")


def _get_connection() -> sqlite3.Connection:
    """Creates a new SQLite connection with common pragmas."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def hash_secret(secret: str) -> str:
    """Hash a soul secret using Argon2id."""
    return _hasher.hash(secret)


def verify_secret_hash(secret: str, secret_hash: str) -> bool:
    """Verify a soul secret against its Argon2id hash."""
    try:
        _hasher.verify(secret_hash, secret)
        return True
    except Exception:
        return False


def create_ws_session(owner_id: str, soul_id: str, hmac_key: str, ttl_seconds: int = 86400) -> str:
    """Create a new WebSocket session with HMAC key. Returns session_id."""
    import secrets as pysecrets
    session_id = pysecrets.token_urlsafe(32)
    now = time.time()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO ws_sessions (session_id, owner_id, soul_id, hmac_key, created_at, last_used_at, expires_at, used_nonces)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, owner_id, soul_id, hmac_key, now, now, now + ttl_seconds, "[]"),
        )
        conn.commit()
    return session_id


def get_ws_session(session_id: str) -> dict | None:
    """Get a valid WebSocket session by ID, updating last_used_at."""
    now = time.time()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT session_id, owner_id, soul_id, hmac_key, expires_at, used_nonces FROM ws_sessions
               WHERE session_id = ? AND expires_at > ?""",
            (session_id, now),
        )
        row = cursor.fetchone()
        if row:
            cursor.execute(
                "UPDATE ws_sessions SET last_used_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            conn.commit()
            return dict(row)
    return None


def validate_and_store_nonce(session_id: str, nonce: str, max_age_seconds: int = 300) -> bool:
    """
    Validate a nonce hasn't been used recently and store it.
    Returns True if nonce is new, False if replay detected.
    """
    import json
    now = time.time()
    cutoff = now - max_age_seconds
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT used_nonces FROM ws_sessions WHERE session_id = ? AND expires_at > ?",
            (session_id, now),
        )
        row = cursor.fetchone()
        if not row:
            return False
        try:
            used = json.loads(row["used_nonces"])
        except (json.JSONDecodeError, TypeError):
            used = []
        # Remove old nonces
        used = [n for n in used if n.get("ts", 0) > cutoff]
        if any(n["nonce"] == nonce for n in used):
            return False  # Replay detected
        used.append({"nonce": nonce, "ts": now})
        cursor.execute(
            "UPDATE ws_sessions SET used_nonces = ? WHERE session_id = ?",
            (json.dumps(used), session_id),
        )
        conn.commit()
        return True


def delete_ws_session(session_id: str) -> None:
    """Delete a WebSocket session."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM ws_sessions WHERE session_id = ?", (session_id,))
        conn.commit()


def cleanup_expired_ws_sessions() -> int:
    """Remove expired WebSocket sessions. Returns count deleted."""
    now = time.time()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM ws_sessions WHERE expires_at <= ?", (now,))
        conn.commit()
        return cursor.rowcount


# Token Bucket Rate Limiting (per owner_id)
DEFAULT_MAX_TOKENS = 100.0
DEFAULT_REFILL_RATE = 50.0  # tokens per second


def check_rate_limit(owner_id: str, cost: float = 1.0) -> bool:
    """
    Check and consume tokens from the owner's rate limit bucket.
    Returns True if allowed, False if rate limited.
    """
    now = time.time()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tokens, last_refill, max_tokens, refill_rate FROM rate_limits WHERE owner_id = ?",
            (owner_id,),
        )
        row = cursor.fetchone()
        if row:
            tokens = row["tokens"]
            last_refill = row["last_refill"]
            max_tokens = row["max_tokens"]
            refill_rate = row["refill_rate"]
        else:
            tokens = DEFAULT_MAX_TOKENS
            last_refill = now
            max_tokens = DEFAULT_MAX_TOKENS
            refill_rate = DEFAULT_REFILL_RATE

        # Refill tokens based on elapsed time
        elapsed = now - last_refill
        tokens = min(max_tokens, tokens + elapsed * refill_rate)

        if tokens >= cost:
            tokens -= cost
            cursor.execute(
                """INSERT OR REPLACE INTO rate_limits 
                   (owner_id, tokens, last_refill, max_tokens, refill_rate)
                   VALUES (?, ?, ?, ?, ?)""",
                (owner_id, tokens, now, max_tokens, refill_rate),
            )
            conn.commit()
            return True
        else:
            # Update tokens anyway (refill happened)
            cursor.execute(
                """INSERT OR REPLACE INTO rate_limits 
                   (owner_id, tokens, last_refill, max_tokens, refill_rate)
                   VALUES (?, ?, ?, ?, ?)""",
                (owner_id, tokens, now, max_tokens, refill_rate),
            )
            conn.commit()
            return False


def get_rate_limit_status(owner_id: str) -> dict:
    """Get current rate limit status for an owner."""
    now = time.time()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tokens, last_refill, max_tokens, refill_rate FROM rate_limits WHERE owner_id = ?",
            (owner_id,),
        )
        row = cursor.fetchone()
        if row:
            tokens = min(row["max_tokens"], row["tokens"] + (now - row["last_refill"]) * row["refill_rate"])
            return {
                "tokens": tokens,
                "max_tokens": row["max_tokens"],
                "refill_rate": row["refill_rate"],
            }
    return {
        "tokens": DEFAULT_MAX_TOKENS,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "refill_rate": DEFAULT_REFILL_RATE,
    }


# Movement validation constants
MAX_BASE_SPEED = 300.0  # pixels per second base
SPEED_PER_DEX = 5.0     # additional pixels per second per SPE stat point
MAX_TELEPORT_DISTANCE = 50.0  # max allowed position jump between updates (pixels)
SCREEN_BOUNDS = (1920, 1080)  # default, can be overridden


def validate_soul_movement(
    soul_id: str,
    prev_x: float,
    prev_y: float,
    new_x: float,
    new_y: float,
    dt: float,
    spe_stat: int = 0,
    screen_width: float = SCREEN_BOUNDS[0],
    screen_height: float = SCREEN_BOUNDS[1],
) -> tuple[float, float]:
    """
    Validate and clamp soul movement server-side.
    
    Returns (clamped_x, clamped_y) - the validated position.
    Raises ValueError if movement is invalid.
    """
    # Clamp to screen bounds with small margin
    margin = 10.0
    new_x = max(margin, min(new_x, screen_width - margin))
    new_y = max(margin, min(new_y, screen_height - margin))

    # Calculate max allowed distance based on stats and time
    max_speed = MAX_BASE_SPEED + (spe_stat * SPEED_PER_DEX)
    max_distance = max_speed * dt + MAX_TELEPORT_DISTANCE

    dx = new_x - prev_x
    dy = new_y - prev_y
    actual_distance = (dx * dx + dy * dy) ** 0.5

    if actual_distance > max_distance:
        # Clamp to max allowed distance in the same direction
        if actual_distance > 0:
            ratio = max_distance / actual_distance
            new_x = prev_x + dx * ratio
            new_y = prev_y + dy * ratio
        else:
            new_x, new_y = prev_x, prev_y
        logger.warning(
            f"Soul {soul_id} movement clamped: tried {actual_distance:.1f}px, "
            f"max {max_distance:.1f}px in {dt:.3f}s"
        )

    return new_x, new_y
    """Creates a new SQLite connection with common pragmas."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def get_db():
    """Context manager for database connections, ensuring they are closed after use."""
    conn = _get_connection()
    try:
        yield conn
    finally:
        conn.close()


def charge_soul(cursor: sqlite3.Cursor, soul_id: str, amount: float, description: str):
    """
    Deducts essence from a soul. Raises HTTPException if soul not found or insufficient essence.
    """
    cursor.execute(
        "UPDATE souls SET essence = essence - ? WHERE soul_id = ? AND essence >= ?",
        (amount, soul_id, amount),
    )
    if cursor.rowcount == 0:
        cursor.execute("SELECT 1 FROM souls WHERE soul_id = ?", (soul_id,))
        if not cursor.fetchone():
            raise HTTPException(
                status_code=404,
                detail=f"Soul {soul_id} not found ({description})",
            )
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient essence for {description}. Required: {amount}",
        )


def log_audit(
    cursor: sqlite3.Cursor,
    operator_id: str,
    action: str,
    target_type: str = "",
    target_id: str = "",
    details: str = "",
):
    cursor.execute(
        "INSERT INTO audit_log (operator_id, action, target_type, target_id, details) "
        "VALUES (?, ?, ?, ?, ?)",
        (operator_id, action, target_type, target_id, details),
    )


def audit_log(
    operator_id: str,
    action: str,
    target_type: str = "",
    target_id: str = "",
    details: str = "",
) -> None:
    """Standalone audit log entry (opens its own connection)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO audit_log (operator_id, action, target_type, target_id, details, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (operator_id, action, target_type, target_id, details, time.time()),
        )
        conn.commit()


def init_db():
    """Initializes the database schema if it doesn't already exist."""
    conn = _get_connection()
    try:
        with conn:
            cursor = conn.cursor()
            # Marketplace Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS marketplace (
                    listing_id TEXT PRIMARY KEY,
                    seller_id TEXT,
                    seller_name TEXT,
                    item TEXT,
                    price REAL,
                    timestamp REAL
                )
            """)
            # Social Posts Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS social_posts (
                    message_id TEXT PRIMARY KEY,
                    author_id TEXT,
                    author_name TEXT,
                    title TEXT,
                    content TEXT,
                    timestamp REAL
                )
            """)
            # Social Replies Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS social_replies (
                    reply_id TEXT PRIMARY KEY,
                    parent_id TEXT,
                    author_id TEXT,
                    author_name TEXT,
                    content TEXT,
                    timestamp REAL,
                    FOREIGN KEY (parent_id) REFERENCES social_posts (message_id) ON DELETE CASCADE
                )
            """)
            # Souls Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS souls (
                    soul_id TEXT PRIMARY KEY,
                    owner_id TEXT,
                    name TEXT,
                    first_name TEXT,
                    family_name TEXT,
                    species TEXT,
                    gender TEXT,
                    level INTEGER,
                    essence REAL,
                    hp REAL,
                    max_hp REAL,
                    satiety REAL,
                    hydration REAL,
                    xp INTEGER,
                    position TEXT,
                    hometown TEXT,
                    birth_date TEXT,
                    activity TEXT,
                    mother_id TEXT,
                    father_id TEXT,
                    orb_color TEXT,
                    aura_color TEXT,
                    aura_visible INTEGER,
                    stat_hp_base INTEGER,
                    stat_atk_base INTEGER,
                    stat_def_base INTEGER,
                    stat_spa_base INTEGER,
                    stat_spd_base INTEGER,
                    stat_spe_base INTEGER,
                    stat_vis_base INTEGER,
                    stat_hp_iv INTEGER,
                    stat_atk_iv INTEGER,
                    stat_def_iv INTEGER,
                    stat_spa_iv INTEGER,
                    stat_spd_iv INTEGER,
                    stat_spe_iv INTEGER,
                    stat_vis_iv INTEGER,
                    stat_hp_ev INTEGER,
                    stat_atk_ev INTEGER,
                    stat_def_ev INTEGER,
                    stat_spa_ev INTEGER,
                    stat_spd_ev INTEGER,
                    stat_spe_ev INTEGER,
                    stat_vis_ev INTEGER,
                    nature TEXT,
                    secret_hash TEXT,
                    secret_prefix TEXT,
                    updated_at REAL,
                    token_expiry REAL,
                    is_revoked INTEGER DEFAULT 0
                )
            """)
            # Inventory Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS soul_inventory (
                    inventory_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    soul_id TEXT,
                    item_name TEXT,
                    quantity INTEGER,
                    metadata TEXT,
                    FOREIGN KEY (soul_id) REFERENCES souls (soul_id) ON DELETE CASCADE
                )
                """)
            # Globals Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS globals (
                    key TEXT PRIMARY KEY,
                    value REAL
                )
            """)
            # Instances Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS instances (
                    owner_id TEXT PRIMARY KEY,
                    last_seen REAL
                )
            """)
            # WebSocket Sessions Table for HMAC binding
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ws_sessions (
                    session_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    soul_id TEXT,
                    hmac_key TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_used_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used_nonces TEXT NOT NULL DEFAULT '[]',
                    FOREIGN KEY (soul_id) REFERENCES souls (soul_id) ON DELETE CASCADE
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_ws_sessions_owner_id ON ws_sessions(owner_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_ws_sessions_expires_at ON ws_sessions(expires_at)
            """)
            cursor.execute(
                "INSERT OR IGNORE INTO globals (key, value) VALUES ('essence_fund', 0.0)"
            )
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_souls_owner_id ON souls(owner_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_souls_secret_prefix ON souls(secret_prefix)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_soul_inventory_soul_id
                ON soul_inventory(soul_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_marketplace_listing_id
                ON marketplace(listing_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_social_posts_message_id
                ON social_posts(message_id)
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operator_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_type TEXT,
                    target_id TEXT,
                    details TEXT,
                    timestamp REAL DEFAULT (strftime('%s', 'now'))
                )
            """)
            # Rate Limiting Table (Token Bucket per owner_id)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS rate_limits (
                    owner_id TEXT PRIMARY KEY,
                    tokens REAL NOT NULL DEFAULT 100.0,
                    last_refill REAL NOT NULL,
                    max_tokens REAL NOT NULL DEFAULT 100.0,
                    refill_rate REAL NOT NULL DEFAULT 50.0
                )
            """)
            _migrate_souls(cursor)
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        raise
    finally:
        conn.close()


def _add_column_if_missing(cursor, table: str, column_def: str) -> None:
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row["name"] for row in cursor.fetchall()}
    col_name = column_def.split()[0]
    if col_name not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
        logger.info(f"Migration: added {table}.{col_name}")


def _migrate_souls(cursor) -> None:
    _add_column_if_missing(cursor, "souls", "secret_hash TEXT")
    _add_column_if_missing(cursor, "souls", "secret_prefix TEXT")
    _add_column_if_missing(cursor, "souls", "updated_at REAL")
    cursor.execute("PRAGMA table_info(souls)")
    cols = {row["name"] for row in cursor.fetchall()}
    if "secret" in cols:
        cursor.execute(
            "SELECT soul_id, secret FROM souls "
            "WHERE secret IS NOT NULL AND secret_hash IS NULL"
        )
        migrated = 0
        for row in cursor.fetchall():
            secret_hash = hash_secret(row["secret"])
            cursor.execute(
                "UPDATE souls SET secret_hash = ?, secret_prefix = ? "
                "WHERE soul_id = ?",
                (secret_hash, secret_hash[:16], row["soul_id"]),
            )
            migrated += 1
        cursor.execute("ALTER TABLE souls DROP COLUMN secret")
        logger.info(
            f"Migration: hashed {migrated} legacy plaintext secrets, "
            "dropped souls.secret"
        )
