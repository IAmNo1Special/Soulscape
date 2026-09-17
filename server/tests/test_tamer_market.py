"""Issue #40: tamer wallets, account inventories, inventory-backed listings.

Tamers are first-class market actors: they register with a starter
essence grant, list from their account inventory or a custodied soul's
inventory, buy into their account inventory, and can fund their souls
one-way. Listings are backed by real inventory for every actor.
"""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from .. import database
from .. import dormancy
from .. import market
from .. import persistence
from ..main import app


def _public_client() -> TestClient:
    return TestClient(app)


def _register_tamer(username: str):
    c = _public_client()
    r = c.post(
        "/tamers/register",
        json={"username": username, "password": "s3cur3pass"},
    )
    assert r.status_code == 201, r.text
    tamer_id = r.json()["tamer_id"]
    r = c.post(
        "/tamers/login",
        json={"username": username, "password": "s3cur3pass"},
    )
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    tc = TestClient(app, headers={"X-Hub-Secret": token})
    return tamer_id, tc


def _register_soul(client, soul_id, custodian_id=None, essence=100.0):
    payload = {
        "owner_id": f"owner_{soul_id}",
        "souls": [
            {
                "soul_id": soul_id,
                "owner_id": f"owner_{soul_id}",
                "name": soul_id,
                "essence": essence,
                "hp": 100,
                "max_hp": 100,
                "satiety": 100,
                "hydration": 100,
            }
        ],
    }
    if custodian_id:
        payload["custodian_id"] = custodian_id
    r = client.post("/souls", json=payload)
    assert r.status_code == 200, r.text


def _seed_tamer_inventory(tamer_id, item_name, qty=5, metadata=None):
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO tamer_inventory "
            "(tamer_id, item_name, quantity, metadata) VALUES (?, ?, ?, ?)",
            (
                tamer_id,
                item_name,
                qty,
                json.dumps(metadata) if metadata else None,
            ),
        )
        conn.commit()


def _seed_soul_inventory(soul_id, item_name, qty=5):
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO soul_inventory (soul_id, item_name, quantity) "
            "VALUES (?, ?, ?)",
            (soul_id, item_name, qty),
        )
        conn.commit()


def _tamer_inventory_qty(tamer_id, item_name):
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT quantity FROM tamer_inventory "
            "WHERE tamer_id = ? AND item_name = ?",
            (tamer_id, item_name),
        ).fetchone()
    return row["quantity"] if row else 0


def _soul_inventory_qty(soul_id, item_name):
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT quantity FROM soul_inventory "
            "WHERE soul_id = ? AND item_name = ?",
            (soul_id, item_name),
        ).fetchone()
    return row["quantity"] if row else 0


def _tamer_essence(tamer_id):
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT essence FROM tamers WHERE tamer_id = ?", (tamer_id,)
        ).fetchone()
    return float(row["essence"])


def _soul_essence(soul_id):
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT essence FROM souls WHERE soul_id = ?", (soul_id,)
        ).fetchone()
    return float(row["essence"])


def _ledger_rows(intent_id=None):
    with database.get_db() as conn:
        if intent_id:
            rows = conn.execute(
                "SELECT entry_type, actor_type, soul_id, amount FROM ledger "
                "WHERE intent_id = ? ORDER BY ledger_id",
                (intent_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT entry_type, actor_type, soul_id, amount FROM ledger "
                "ORDER BY ledger_id"
            ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Registration: starter grant + mint ledger row
# ---------------------------------------------------------------------------


def test_register_mints_starter_grant_to_tamer_wallet():
    tamer_id, _ = _register_tamer("grant_tamer")
    assert _tamer_essence(tamer_id) == pytest.approx(dormancy.STARTER_GRANT)
    rows = _ledger_rows(f"register:{tamer_id}")
    assert len(rows) == 1
    row = rows[0]
    assert row["entry_type"] == "mint"
    assert row["actor_type"] == database.ACTOR_TAMER
    assert row["soul_id"] == tamer_id
    assert row["amount"] == pytest.approx(dormancy.STARTER_GRANT)


def test_soul_birth_mint_still_soul_typed(client):
    _register_soul(client, "birth_soul")
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT entry_type, actor_type, soul_id, amount FROM ledger "
            "WHERE entry_type = 'mint' AND soul_id = 'birth_soul'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_type"] == database.ACTOR_SOUL
    assert rows[0]["amount"] == pytest.approx(dormancy.STARTER_GRANT)


# ---------------------------------------------------------------------------
# Tamer listings: account inventory and custodied-soul inventory
# ---------------------------------------------------------------------------


def test_tamer_lists_from_account_inventory():
    tamer_id, tc = _register_tamer("lister_tamer")
    _seed_tamer_inventory(tamer_id, "Tamer Trinket", qty=3)
    r = tc.post(
        "/marketplace/list",
        json={
            "seller_type": "tamer",
            "seller_name": "Lister",
            "item": {"name": "Tamer Trinket", "rarity": "common"},
            "price": 25.0,
        },
    )
    assert r.status_code == 200, r.text
    listing_id = r.json()["listing_id"]
    assert _tamer_inventory_qty(tamer_id, "Tamer Trinket") == 2
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT seller_id, seller_type, item FROM marketplace "
            "WHERE listing_id = ?",
            (listing_id,),
        ).fetchone()
    assert row["seller_id"] == tamer_id
    assert row["seller_type"] == database.ACTOR_TAMER
    item = json.loads(row["item"])
    assert item["name"] == "Tamer Trinket"
    assert item["rarity"] == "common"


def test_tamer_lists_from_custodied_soul_inventory(client):
    tamer_id, tc = _register_tamer("custody_lister")
    _register_soul(client, "pet_soul", custodian_id=tamer_id)
    _seed_soul_inventory("pet_soul", "Soul Relic", qty=2)
    r = tc.post(
        "/marketplace/list",
        json={
            "seller_type": "soul",
            "seller_id": "pet_soul",
            "seller_name": "Lister",
            "item": {"name": "Soul Relic"},
            "price": 10.0,
        },
    )
    assert r.status_code == 200, r.text
    assert _soul_inventory_qty("pet_soul", "Soul Relic") == 1


def test_tamer_cannot_list_from_unowned_soul(client):
    _, tc = _register_tamer("sneaky_tamer")
    _register_soul(client, "victim_soul", custodian_id="someone_else")
    _seed_soul_inventory("victim_soul", "Victim Relic", qty=2)
    r = tc.post(
        "/marketplace/list",
        json={
            "seller_type": "soul",
            "seller_id": "victim_soul",
            "seller_name": "Sneaky",
            "item": {"name": "Victim Relic"},
            "price": 10.0,
        },
    )
    assert r.status_code == 403, r.text
    assert _soul_inventory_qty("victim_soul", "Victim Relic") == 2


def test_tamer_list_without_inventory_refused():
    _, tc = _register_tamer("broke_lister")
    r = tc.post(
        "/marketplace/list",
        json={
            "seller_type": "tamer",
            "seller_name": "Broke",
            "item": {"name": "Ghost Item"},
            "price": 10.0,
        },
    )
    assert r.status_code == 400, r.text
    assert "holds less than" in r.json()["detail"].lower()


def test_soul_list_without_inventory_refused(client):
    _register_soul(client, "empty_seller")
    r = client.post(
        "/marketplace/list",
        json={
            "seller_id": "empty_seller",
            "seller_name": "Empty",
            "item": {"name": "Nothing"},
            "price": 10.0,
        },
    )
    assert r.status_code == 400, r.text
    assert "holds less than" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Tamer buying: escrow debits the tamer wallet, item lands in account inventory
# ---------------------------------------------------------------------------


def test_tamer_buys_into_account_inventory(client):
    tamer_id, tc = _register_tamer("buyer_tamer")
    _register_soul(client, "vendor_soul", essence=100.0)
    _seed_soul_inventory("vendor_soul", "Vendor Widget", qty=1)
    r = client.post(
        "/marketplace/list",
        json={
            "seller_id": "vendor_soul",
            "seller_name": "Vendor",
            "item": {"name": "Vendor Widget", "kind": "gadget"},
            "price": 40.0,
        },
    )
    assert r.status_code == 200, r.text
    listing_id = r.json()["listing_id"]

    tamer_before = _tamer_essence(tamer_id)
    r = tc.post(
        f"/marketplace/buy/{listing_id}",
        json={"buyer_type": "tamer"},
    )
    assert r.status_code == 200, r.text
    assert _tamer_essence(tamer_id) == pytest.approx(tamer_before - 40.0)
    assert _tamer_inventory_qty(tamer_id, "Vendor Widget") == 1
    assert _soul_essence("vendor_soul") == pytest.approx(100.0 + 39.2)

    with database.get_db() as conn:
        meta = conn.execute(
            "SELECT metadata FROM tamer_inventory "
            "WHERE tamer_id = ? AND item_name = ?",
            (tamer_id, "Vendor Widget"),
        ).fetchone()["metadata"]
    assert json.loads(meta)["kind"] == "gadget"

    debits = [row for row in _ledger_rows() if row["entry_type"] == "debit"]
    tamer_debits = [d for d in debits if d["actor_type"] == "tamer"]
    assert any(
        d["soul_id"] == tamer_id and d["amount"] == pytest.approx(40.0)
        for d in tamer_debits
    )
    credits = [row for row in _ledger_rows() if row["entry_type"] == "credit"]
    assert any(
        c["actor_type"] == "soul"
        and c["soul_id"] == "vendor_soul"
        and c["amount"] == pytest.approx(39.2)
        for c in credits
    )


def test_soul_buys_tamer_listing_no_sale_xp_for_tamer(client):
    tamer_id, tc = _register_tamer("vendor_tamer")
    _register_soul(client, "shopper_soul", essence=500.0)
    _seed_tamer_inventory(tamer_id, "Tamer Good", qty=1)
    r = tc.post(
        "/marketplace/list",
        json={
            "seller_type": "tamer",
            "seller_name": "Vendor",
            "item": {"name": "Tamer Good"},
            "price": 50.0,
        },
    )
    assert r.status_code == 200, r.text
    listing_id = r.json()["listing_id"]

    r = client.post(
        f"/marketplace/buy/{listing_id}",
        json={"buyer_id": "shopper_soul"},
    )
    assert r.status_code == 200, r.text
    assert _tamer_essence(tamer_id) == pytest.approx(100.0 + 49.0)
    assert _soul_inventory_qty("shopper_soul", "Tamer Good") == 1
    with database.get_db() as conn:
        xp = conn.execute(
            "SELECT xp FROM souls WHERE soul_id = 'shopper_soul'"
        ).fetchone()
        assert xp is None or float(xp["xp"] or 0.0) == 0.0


# ---------------------------------------------------------------------------
# Cancel restores reserved inventory with metadata
# ---------------------------------------------------------------------------


def test_cancel_restores_tamer_inventory_with_metadata():
    tamer_id, tc = _register_tamer("cancel_tamer")
    _seed_tamer_inventory(
        tamer_id, "Keepsake", qty=1, metadata={"rarity": "rare"}
    )
    r = tc.post(
        "/marketplace/list",
        json={
            "seller_type": "tamer",
            "seller_name": "Cancel",
            "item": {"name": "Keepsake", "rarity": "rare"},
            "price": 15.0,
        },
    )
    assert r.status_code == 200, r.text
    listing_id = r.json()["listing_id"]
    assert _tamer_inventory_qty(tamer_id, "Keepsake") == 0

    r = tc.post(f"/marketplace/cancel/{listing_id}")
    assert r.status_code == 200, r.text
    assert _tamer_inventory_qty(tamer_id, "Keepsake") == 1
    with database.get_db() as conn:
        meta = conn.execute(
            "SELECT metadata FROM tamer_inventory "
            "WHERE tamer_id = ? AND item_name = ?",
            (tamer_id, "Keepsake"),
        ).fetchone()["metadata"]
    assert json.loads(meta)["rarity"] == "rare"


# ---------------------------------------------------------------------------
# POST /tamers/fund-soul
# ---------------------------------------------------------------------------


def test_fund_soul_moves_essence_one_way(client):
    tamer_id, tc = _register_tamer("generous_tamer")
    _register_soul(client, "funded_soul", custodian_id=tamer_id)
    r = tc.post(
        "/tamers/fund-soul",
        json={"soul_id": "funded_soul", "amount": 30.0},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["tamer_essence"] == pytest.approx(70.0)
    assert data["soul_essence"] == pytest.approx(130.0)
    assert _tamer_essence(tamer_id) == pytest.approx(70.0)
    assert _soul_essence("funded_soul") == pytest.approx(130.0)
    rows = _ledger_rows()
    assert any(
        row["entry_type"] == "debit"
        and row["actor_type"] == "tamer"
        and row["soul_id"] == tamer_id
        and row["amount"] == pytest.approx(30.0)
        for row in rows
    )
    assert any(
        row["entry_type"] == "credit"
        and row["actor_type"] == "soul"
        and row["soul_id"] == "funded_soul"
        and row["amount"] == pytest.approx(30.0)
        for row in rows
    )


def test_fund_soul_insufficient_funds(client):
    tamer_id, tc = _register_tamer("poor_tamer")
    _register_soul(client, "hungry_soul", custodian_id=tamer_id, essence=0.0)
    r = tc.post(
        "/tamers/fund-soul",
        json={"soul_id": "hungry_soul", "amount": 500.0},
    )
    assert r.status_code == 400, r.text
    assert _soul_essence("hungry_soul") == pytest.approx(100.0)


def test_fund_soul_requires_custody(client):
    _, tc = _register_tamer("stranger_tamer")
    _register_soul(client, "claimed_soul", custodian_id="someone_else")
    r = tc.post(
        "/tamers/fund-soul",
        json={"soul_id": "claimed_soul", "amount": 10.0},
    )
    assert r.status_code == 403, r.text


def test_fund_soul_requires_tamer_identity(client):
    _register_soul(client, "lonely_soul")
    r = client.post(
        "/tamers/fund-soul",
        json={"soul_id": "lonely_soul", "amount": 10.0},
    )
    assert r.status_code == 403, r.text


@pytest.mark.parametrize("amount", [0, -5.0])
def test_fund_soul_rejects_non_positive_amount(client, amount):
    tamer_id, tc = _register_tamer("careful_tamer")
    _register_soul(client, "patient_soul", custodian_id=tamer_id)
    r = tc.post(
        "/tamers/fund-soul",
        json={"soul_id": "patient_soul", "amount": amount},
    )
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# Conservation accounting covers tamer wallets
# ---------------------------------------------------------------------------


def test_verify_balances_covers_tamer_mint_and_trades(client):
    tamer_id, tc = _register_tamer("audit_tamer")
    _register_soul(client, "audit_vendor", essence=200.0)
    _seed_soul_inventory("audit_vendor", "Audit Item", qty=1)
    r = client.post(
        "/marketplace/list",
        json={
            "seller_id": "audit_vendor",
            "seller_name": "Auditor",
            "item": {"name": "Audit Item"},
            "price": 60.0,
        },
    )
    listing_id = r.json()["listing_id"]
    r = tc.post(
        f"/marketplace/buy/{listing_id}", json={"buyer_type": "tamer"}
    )
    assert r.status_code == 200, r.text
    with database.get_db() as conn:
        report = market.verify_balances(conn)
    assert report["ok"], (
        report["soul_drifts"],
        report["tamer_drifts"],
    )
    assert report["tamer_drifts"] == []


def test_verify_balances_detects_tamer_drift():
    tamer_id, _ = _register_tamer("drift_tamer")
    with database.get_db() as conn:
        first = market.verify_balances(conn)
        assert first["ok"]
        conn.execute(
            "UPDATE tamers SET essence = essence + 5.0 WHERE tamer_id = ?",
            (tamer_id,),
        )
        conn.commit()
        report = market.verify_balances(conn)
    assert not report["ok"]
    assert any(
        d["tamer_id"] == tamer_id for d in report["tamer_drifts"]
    )


# ---------------------------------------------------------------------------
# Additive migration: legacy rows default to soul actors
# ---------------------------------------------------------------------------


def _legacy_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tamers (tamer_id TEXT PRIMARY KEY, "
        "username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, "
        "created_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE marketplace (listing_id TEXT PRIMARY KEY, "
        "seller_id TEXT NOT NULL, seller_name TEXT, item TEXT NOT NULL, "
        "price REAL NOT NULL, timestamp REAL NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE escrows (escrow_id TEXT PRIMARY KEY, "
        "intent_id TEXT NOT NULL, soul_id TEXT NOT NULL, "
        "amount REAL NOT NULL, status TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE ledger (ledger_id INTEGER PRIMARY KEY, "
        "tick_id INTEGER NOT NULL, intent_id TEXT, entry_type TEXT NOT NULL, "
        "soul_id TEXT NOT NULL, amount REAL NOT NULL, created_at REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO tamers (tamer_id, username, password_hash, created_at) "
        "VALUES ('tmr_old', 'oldie', 'hash', 1.0)"
    )
    conn.execute(
        "INSERT INTO marketplace (listing_id, seller_id, item, price, "
        "timestamp) VALUES ('l1', 's1', '{}', 5.0, 1.0)"
    )
    conn.execute(
        "INSERT INTO escrows (escrow_id, intent_id, soul_id, amount, status) "
        "VALUES ('e1', 'n1', 's1', 5.0, 'held')"
    )
    conn.execute(
        "INSERT INTO ledger (tick_id, entry_type, soul_id, amount, created_at) "
        "VALUES (1, 'debit', 's1', 5.0, 1.0)"
    )
    conn.commit()
    return conn


def test_migration_backfills_actor_columns_as_soul():
    conn = _legacy_conn()
    database._migrate_wallets(conn.cursor())
    cols = {
        table: {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for table in ("tamers", "marketplace", "escrows", "ledger")
    }
    assert "essence" in cols["tamers"]
    assert "seller_type" in cols["marketplace"]
    assert "actor_type" in cols["escrows"]
    assert "actor_type" in cols["ledger"]
    assert conn.execute(
        "SELECT essence FROM tamers WHERE tamer_id = 'tmr_old'"
    ).fetchone()[0] == pytest.approx(0.0)
    assert (
        conn.execute(
            "SELECT seller_type FROM marketplace WHERE listing_id = 'l1'"
        ).fetchone()[0]
        == "soul"
    )
    assert (
        conn.execute(
            "SELECT actor_type FROM escrows WHERE escrow_id = 'e1'"
        ).fetchone()[0]
        == "soul"
    )
    assert (
        conn.execute(
            "SELECT actor_type FROM ledger WHERE ledger_id = 1"
        ).fetchone()[0]
        == "soul"
    )


def test_legacy_escrow_reconciles_to_soul_wallet(client):
    _register_soul(client, "legacy_soul", essence=50.0)
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO escrows (escrow_id, intent_id, soul_id, amount, "
            "status, created_at) VALUES ('legacy_e1', 'missing_intent', "
            "'legacy_soul', 12.5, 'held', 1.0)"
        )
        conn.execute(
            "UPDATE souls SET essence = 37.5 WHERE soul_id = 'legacy_soul'"
        )
        conn.commit()
        settled = persistence.reconcile_escrows(conn)
        conn.commit()
    assert settled == 1
    assert _soul_essence("legacy_soul") == pytest.approx(50.0)
