"""
Database configuration, initialization, and shared utility functions for Soulscape Hub.
Uses SQLite with WAL mode enabled for better concurrency.
"""

import logging
import os
import sqlite3
from contextlib import contextmanager

from fastapi import HTTPException

logger = logging.getLogger("soulscape_hub")

# Ensure DB path is absolute relative to this file
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "soulscape_hub.db")


def _get_connection() -> sqlite3.Connection:
    """Creates a new SQLite connection with common pragmas."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    """Context manager for database connections, ensuring they are closed after use."""
    conn = _get_connection()
    try:
        yield conn
    finally:
        conn.close()


def charge_soul(
    cursor: sqlite3.Cursor, soul_id: str, amount: float, description: str
):
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
                    secret TEXT
                )
            """)
            # Migration: Add secret column if it doesn't exist
            try:
                cursor.execute("ALTER TABLE souls ADD COLUMN secret TEXT")
            except sqlite3.OperationalError:
                # Column already exists
                pass

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
            cursor.execute(
                "INSERT OR IGNORE INTO globals (key, value) VALUES ('essence_fund', 0.0)"
            )
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        raise
    finally:
        conn.close()
