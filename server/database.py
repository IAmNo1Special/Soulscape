"""
Database configuration, initialization, and shared utility functions for Soulscape Hub.
Uses SQLite with WAL mode enabled for better concurrency.
"""

import hashlib
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

# Ensure DB path is absolute relative to this file. SOULSCAPE_DB_PATH overrides
# the default location (used by isolated integration tests and deployments).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SOULSCAPE_DB_PATH") or os.path.join(
    BASE_DIR, "soulscape_hub.db"
)


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


def create_ws_session(
    owner_id: str, soul_id: str, hmac_key: str, ttl_seconds: int = 86400
) -> str:
    """Create a new WebSocket session with HMAC key. Returns session_id."""
    import secrets as pysecrets

    session_id = pysecrets.token_urlsafe(32)
    now = time.time()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO ws_sessions (session_id, owner_id, soul_id, hmac_key, created_at, last_used_at, expires_at, used_nonces)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                owner_id,
                soul_id,
                hmac_key,
                now,
                now,
                now + ttl_seconds,
                "[]",
            ),
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


def validate_and_store_nonce(
    session_id: str, nonce: str, max_age_seconds: int = 300
) -> bool:
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


def delete_ws_sessions_for_owner(owner_id: str) -> int:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM ws_sessions WHERE owner_id = ?", (owner_id,))
        conn.commit()
        return cursor.rowcount


def cleanup_expired_ws_sessions() -> int:
    """Remove expired WebSocket sessions. Returns count deleted."""
    now = time.time()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM ws_sessions WHERE expires_at <= ?", (now,))
        conn.commit()
        return cursor.rowcount


def create_tamer(tamer_id: str, username: str, password_hash: str) -> None:
    """Insert a new tamer. Raises sqlite3.IntegrityError on duplicate."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO tamers (tamer_id, username, password_hash, created_at) "
            "VALUES (?, ?, ?, ?)",
            (tamer_id, username, password_hash, time.time()),
        )
        conn.commit()


def get_tamer_by_username(username: str) -> dict | None:
    """Fetch a tamer row by username, or None."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tamer_id, username, password_hash, created_at "
            "FROM tamers WHERE username = ?",
            (username,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def create_tamer_session(tamer_id: str, session_hash: str, expires_at: float) -> str:
    """Store a hashed tamer session. Returns session_id."""
    import secrets as pysecrets

    session_id = pysecrets.token_urlsafe(32)
    now = time.time()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO tamer_sessions
               (session_id, tamer_id, session_hash, created_at, expires_at,
                revoked)
               VALUES (?, ?, ?, ?, ?, 0)""",
            (session_id, tamer_id, session_hash, now, expires_at),
        )
        conn.commit()
    return session_id


def get_tamer_session(session_hash: str) -> dict | None:
    """Fetch a live tamer session by token hash. None if missing,
    expired, or revoked."""
    now = time.time()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT s.session_id, s.tamer_id, t.username, s.expires_at
               FROM tamer_sessions s
               JOIN tamers t ON t.tamer_id = s.tamer_id
               WHERE s.session_hash = ? AND s.revoked = 0
                 AND s.expires_at > ?""",
            (session_hash, now),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def revoke_tamer_session(session_hash: str) -> bool:
    """Revoke a tamer session by token hash. True if one was revoked."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE tamer_sessions SET revoked = 1 "
            "WHERE session_hash = ? AND revoked = 0",
            (session_hash,),
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_expired_tamer_sessions() -> int:
    """Remove expired tamer sessions. Returns count deleted."""
    now = time.time()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tamer_sessions WHERE expires_at <= ?", (now,))
        conn.commit()
        return cursor.rowcount


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def create_ws_ticket(
    custodian_id: str,
    role: str,
    soul_id: str | None = None,
    ttl_seconds: int = 60,
) -> str:
    import secrets as pysecrets

    ticket = "wst_" + pysecrets.token_urlsafe(32)
    now = time.time()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO ws_tickets
               (ticket_hash, custodian_id, soul_id, role, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                _sha256_hex(ticket),
                custodian_id,
                soul_id,
                role,
                now,
                now + ttl_seconds,
            ),
        )
        conn.commit()
    return ticket


def redeem_ws_ticket(ticket: str) -> dict | None:
    digest = _sha256_hex(ticket)
    now = time.time()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT ticket_hash, custodian_id, soul_id, role, expires_at "
            "FROM ws_tickets WHERE ticket_hash = ?",
            (digest,),
        )
        row = cursor.fetchone()
        cursor.execute("DELETE FROM ws_tickets WHERE ticket_hash = ?", (digest,))
        conn.commit()
    if row is None or row["expires_at"] <= now:
        return None
    return dict(row)


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
            tokens = min(
                row["max_tokens"],
                row["tokens"] + (now - row["last_refill"]) * row["refill_rate"],
            )
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
SPEED_PER_DEX = 5.0  # additional pixels per second per SPE stat point
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
            # Unified social messages (issue #18): self-referencing
            # parent_id (NULL = top-level post), typed authors.
            # Legacy social_posts/social_replies are migrated into this
            # table by _migrate_social() and then dropped.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    parent_id TEXT,
                    author_type TEXT NOT NULL,
                    author_id TEXT NOT NULL,
                    author_name TEXT NOT NULL DEFAULT '',
                    title TEXT,
                    body TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    edited_at REAL,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (parent_id) REFERENCES messages (message_id),
                    CONSTRAINT chk_messages_author_type
                        CHECK (author_type IN ('soul', 'tamer')),
                    CONSTRAINT chk_messages_title CHECK (
                        (parent_id IS NULL
                         AND title IS NOT NULL
                         AND trim(title) <> '')
                        OR (parent_id IS NOT NULL AND title IS NULL)
                    ),
                    CONSTRAINT chk_messages_deleted CHECK (deleted IN (0, 1))
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_parent_id
                ON messages(parent_id)
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
                    is_revoked INTEGER DEFAULT 0,
                    state TEXT DEFAULT 'normal'
                        CHECK (state IN ('normal','traveling','collapsed')),
                    fed_flag INTEGER DEFAULT 0,
                    rest_started_at REAL
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
            # Tamer accounts (issue #8)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tamers (
                    tamer_id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tamer_sessions (
                    session_id TEXT PRIMARY KEY,
                    tamer_id TEXT NOT NULL,
                    session_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (tamer_id) REFERENCES tamers (tamer_id)
                        ON DELETE CASCADE
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_tamers_username
                ON tamers(username)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_tamer_sessions_hash
                ON tamer_sessions(session_hash)
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ws_tickets (
                    ticket_hash TEXT PRIMARY KEY,
                    custodian_id TEXT NOT NULL,
                    soul_id TEXT,
                    role TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
            """)
            # Encrypted BYO LLM provider key vault (issue #23):
            # ciphertext-only at rest; plaintext never stored.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS llm_keys (
                    key_id TEXT PRIMARY KEY,
                    tamer_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    last4 TEXT NOT NULL DEFAULT '',
                    nonce BLOB NOT NULL,
                    ciphertext BLOB NOT NULL,
                    created_at REAL NOT NULL,
                    rotated_at REAL,
                    revoked_at REAL,
                    superseded_by TEXT
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_llm_keys_owner
                ON llm_keys(tamer_id, provider)
            """)
            # Per-deliberation LLM metering (issue #25): one row per
            # deliberation, including heuristic fallbacks; #27 consumes.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS llm_usage (
                    usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    soul_id TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    estimated_cost_usd REAL NOT NULL,
                    latency_ms REAL NOT NULL,
                    fallback_used INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_llm_usage_soul
                ON llm_usage(soul_id)
            """)
            # Metering stage-1 (issue #27): idempotent usage events per
            # #25 llm_usage row, decision traces joining deliberation ->
            # usage event -> intents -> outcomes, and the runtime pricing
            # knob (per-model USD/1k rates + essence_per_usd).
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS metering_events (
                    event_id TEXT PRIMARY KEY,
                    soul_id TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    cost_usd_estimate REAL NOT NULL,
                    essence_charged REAL,
                    shortfall_essence REAL NOT NULL DEFAULT 0.0,
                    batch_id TEXT,
                    settled_at REAL,
                    settled_by TEXT,
                    created_at REAL NOT NULL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_metering_events_soul
                ON metering_events(soul_id, settled_at)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_metering_events_batch
                ON metering_events(batch_id)
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS decision_traces (
                    trace_id TEXT PRIMARY KEY,
                    soul_id TEXT NOT NULL,
                    deliberation_id INTEGER,
                    usage_event_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'deliberated',
                    rationale TEXT NOT NULL DEFAULT '',
                    intents_json TEXT NOT NULL DEFAULT '[]',
                    intent_ids_json TEXT NOT NULL DEFAULT '[]',
                    outcomes_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_decision_traces_soul
                ON decision_traces(soul_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_decision_traces_event
                ON decision_traces(usage_event_id)
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS metering_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            # Memory tiers (issue #26): episodic log, weekly digests,
            # SQLite-backed semantic store. vocab_version stamps every
            # row so memories stay interpretable across vocabulary
            # bumps. "Per-soul collections" = logical partitioning by
            # soul_id (see server/agents/memory.py for the rationale).
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS episodes (
                    episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    soul_id TEXT NOT NULL,
                    ts REAL NOT NULL,
                    kind TEXT NOT NULL,
                    salience REAL NOT NULL DEFAULT 0.5,
                    content TEXT NOT NULL DEFAULT '',
                    vocab_version INTEGER NOT NULL DEFAULT 1,
                    summarized INTEGER NOT NULL DEFAULT 0
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_episodes_soul
                ON episodes(soul_id, summarized, ts)
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS weekly_digests (
                    digest_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    soul_id TEXT NOT NULL,
                    week_start REAL NOT NULL,
                    digest_text TEXT NOT NULL DEFAULT '',
                    episode_count INTEGER NOT NULL DEFAULT 0,
                    kept_count INTEGER NOT NULL DEFAULT 0,
                    vocab_version INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    UNIQUE (soul_id, week_start)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_digests_soul
                ON weekly_digests(soul_id, week_start)
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS semantic_memories (
                    memory_id TEXT PRIMARY KEY,
                    soul_id TEXT NOT NULL,
                    episode_id INTEGER,
                    text TEXT NOT NULL DEFAULT '',
                    embedding BLOB NOT NULL,
                    dim INTEGER NOT NULL DEFAULT 256,
                    salience REAL NOT NULL DEFAULT 0.5,
                    kind TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    vocab_version INTEGER NOT NULL DEFAULT 1
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_semmem_soul
                ON semantic_memories(soul_id, created_at)
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS intents (
                    intent_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    custodian_id TEXT,
                    soul_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    result TEXT,
                    UNIQUE (session_id, nonce)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_intents_status
                ON intents(status, created_at)
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS journal (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    tick_id INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_journal_tick
                ON journal(tick_id)
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tick_id INTEGER NOT NULL,
                    journal_seq INTEGER NOT NULL,
                    blob BLOB NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            # Market escrows (issue #17): buyer funds held between intent
            # ack and tick-boundary adjudication.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS escrows (
                    escrow_id TEXT PRIMARY KEY,
                    intent_id TEXT UNIQUE NOT NULL,
                    soul_id TEXT NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'held',
                    created_at REAL NOT NULL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_escrows_status
                ON escrows(status)
            """)
            # Append-only essence ledger (issue #17): every market essence
            # movement is a row. souls.essence and the essence_fund global
            # are caches derived from these rows.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ledger (
                    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tick_id INTEGER NOT NULL,
                    intent_id TEXT NOT NULL,
                    entry_type TEXT NOT NULL,
                    soul_id TEXT,
                    amount REAL NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_ledger_intent
                ON ledger(intent_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_ledger_soul
                ON ledger(soul_id)
            """)
            # Plot grid (issue #19): square-plot territory layer. The
            # origin plot is the unclaimable origin Commons; rings
            # divisible by 3 are unclaimable road rings; the rest are
            # claimable. access_policy is 'open' or 'closed'.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS plots (
                    plot_id TEXT PRIMARY KEY,
                    grid_x INTEGER NOT NULL,
                    grid_y INTEGER NOT NULL,
                    ring INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    owner_type TEXT,
                    owner_id TEXT,
                    access_policy TEXT NOT NULL DEFAULT 'open',
                    claimed_at REAL,
                    claim_seq INTEGER,
                    CONSTRAINT chk_plots_kind CHECK (
                        kind IN ('commons', 'road', 'claimable')),
                    CONSTRAINT chk_plots_access CHECK (
                        access_policy IN ('open', 'closed'))
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_plots_owner
                ON plots(owner_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_plots_ring
                ON plots(ring)
            """)
            cursor.execute(
                "INSERT OR IGNORE INTO globals (key, value) "
                "VALUES ('plot_claim_seq', 0.0)"
            )
            # Issue #28: tamer presence (privacy-gated redacted reports).
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tamer_presence (
                    tamer_id TEXT PRIMARY KEY,
                    presence TEXT NOT NULL,
                    idle_bucket TEXT NOT NULL,
                    last_event TEXT,
                    app_category TEXT,
                    updated_at REAL NOT NULL
                )
            """)
            _add_column_if_missing(
                cursor, "tamers", "presence_app_opt_in INTEGER DEFAULT 0"
            )
            from . import plots as plots_module

            plots_module.seed_plots(conn)
            _migrate_souls(cursor)
        _migrate_social(conn)
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
    _add_column_if_missing(cursor, "souls", "velocity TEXT")
    _add_column_if_missing(cursor, "souls", "custodian_id TEXT")
    _add_column_if_missing(cursor, "souls", "move_target TEXT")
    # Issue #21: collapsed state machine + feed_soul.
    _add_column_if_missing(
        cursor,
        "souls",
        "state TEXT DEFAULT 'normal' "
        "CHECK (state IN ('normal','traveling','collapsed'))",
    )
    _add_column_if_missing(cursor, "souls", "fed_flag INTEGER DEFAULT 0")
    _add_column_if_missing(cursor, "souls", "rest_started_at REAL")
    cursor.execute("UPDATE souls SET state = 'normal' WHERE state IS NULL")
    cursor.execute("UPDATE souls SET fed_flag = 0 WHERE fed_flag IS NULL")
    # Legacy rows may carry NULL biology fields; the server default for a
    # new soul is full (100), matching the REST layer's defaults. Add the
    # columns first for ultra-legacy tables that predate them entirely.
    _add_column_if_missing(cursor, "souls", "satiety REAL")
    _add_column_if_missing(cursor, "souls", "hydration REAL")
    _add_column_if_missing(cursor, "souls", "hp REAL")
    _add_column_if_missing(cursor, "souls", "max_hp REAL")
    cursor.execute("UPDATE souls SET satiety = 100.0 WHERE satiety IS NULL")
    cursor.execute("UPDATE souls SET hydration = 100.0 WHERE hydration IS NULL")
    cursor.execute("UPDATE souls SET hp = COALESCE(max_hp, 100.0) WHERE hp IS NULL")
    cursor.execute(
        "UPDATE souls SET custodian_id = owner_id "
        "WHERE custodian_id IS NULL AND owner_id IS NOT NULL"
    )
    if cursor.rowcount:
        logger.info(
            f"Migration: backfilled souls.custodian_id on {cursor.rowcount} rows"
        )
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
                "UPDATE souls SET secret_hash = ?, secret_prefix = ? WHERE soul_id = ?",
                (secret_hash, secret_hash[:16], row["soul_id"]),
            )
            migrated += 1
        cursor.execute("ALTER TABLE souls DROP COLUMN secret")
        logger.info(
            f"Migration: hashed {migrated} legacy plaintext secrets, "
            "dropped souls.secret"
        )


def _migrate_social(conn: sqlite3.Connection) -> None:
    """Expand-contract migration from social_posts/social_replies.

    Expand: backfill every legacy row into messages -- posts as
    title-bearing roots, replies as children of their parent's message
    id -- preserving ids, authors, and timestamps. Legacy posts with
    empty titles get the '(untitled)' placeholder so the messages
    title CHECK holds. Contract: drop the legacy tables once the
    migrated row count verifies.

    Idempotent: a no-op once the legacy tables are gone. Foreign keys
    are toggled off for the backfill only (toggling is a no-op inside
    a transaction, so this runs outside the init_db transaction);
    orphaned replies keep their original parent_id and are reported
    by foreign_key_check instead of being dropped or re-parented.
    """
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "social_posts" not in tables and "social_replies" not in tables:
        return
    if conn.in_transaction:
        conn.commit()
    post_count = (
        conn.execute("SELECT COUNT(*) AS n FROM social_posts").fetchone()["n"]
        if "social_posts" in tables
        else 0
    )
    reply_count = (
        conn.execute("SELECT COUNT(*) AS n FROM social_replies").fetchone()["n"]
        if "social_replies" in tables
        else 0
    )
    before = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if "social_posts" in tables:
                conn.execute(
                    "INSERT INTO messages (message_id, parent_id, "
                    "author_type, author_id, author_name, title, body, "
                    "created_at, edited_at, deleted) "
                    "SELECT message_id, NULL, 'soul', author_id, "
                    "author_name, "
                    "CASE WHEN title IS NULL OR trim(title) = '' "
                    "THEN '(untitled)' ELSE title END, "
                    "content, timestamp, NULL, 0 "
                    "FROM social_posts"
                )
            if "social_replies" in tables:
                conn.execute(
                    "INSERT INTO messages (message_id, parent_id, "
                    "author_type, author_id, author_name, title, body, "
                    "created_at, edited_at, deleted) "
                    "SELECT reply_id, parent_id, 'soul', author_id, "
                    "author_name, NULL, content, timestamp, NULL, 0 "
                    "FROM social_replies"
                )
            after = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
            if after - before != post_count + reply_count:
                raise RuntimeError(
                    "social migration row-count mismatch: "
                    f"legacy={post_count + reply_count} "
                    f"migrated={after - before}"
                )
            conn.execute("DROP TABLE IF EXISTS social_replies")
            conn.execute("DROP TABLE IF EXISTS social_posts")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    violations = conn.execute("PRAGMA foreign_key_check(messages)").fetchall()
    if violations:
        logger.warning(
            "social migration: %d orphaned message(s) kept with dangling parent_id: %s",
            len(violations),
            [dict(v) for v in violations],
        )
    logger.info(
        f"Migration: moved {post_count} posts + {reply_count} replies into "
        "messages; dropped social_posts/social_replies"
    )
