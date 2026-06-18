"""Tests for the POS semantic layer — one per Phase 3 failure mode + the happy paths."""
import sqlite3
from datetime import timedelta

import pytest

from app.pos import db, service

_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, role TEXT,
    can_refund INTEGER DEFAULT 0, can_modify_prices INTEGER DEFAULT 0, status TEXT DEFAULT 'Activo');
CREATE TABLE audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
    action TEXT, details TEXT, timestamp TEXT);
CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, status TEXT DEFAULT 'Activo');
CREATE TABLE warehouses (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE inventory (product_id INTEGER, warehouse_id INTEGER,
    stock_physical INTEGER, stock_system INTEGER, min_required INTEGER);
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, pending_balance REAL DEFAULT 0);
CREATE TABLE sales (id INTEGER PRIMARY KEY, timestamp TEXT, total_amount REAL,
    total_discount REAL DEFAULT 0, status TEXT DEFAULT 'Completada');
CREATE TABLE sale_details (id INTEGER PRIMARY KEY AUTOINCREMENT, sale_id INTEGER,
    product_id INTEGER, quantity INTEGER, unit_price REAL, discount_applied REAL DEFAULT 0);
"""


@pytest.fixture
def pos_db(tmp_path, monkeypatch):
    """A throwaway DB with controlled rows, anchored to the layer's own clock."""
    path = tmp_path / "test_pos.db"
    monkeypatch.setattr(db, "DB_PATH", path)

    today = db.now_local().replace(hour=10, minute=0, second=0, microsecond=0)
    yday = today - timedelta(days=1)
    fmt = "%Y-%m-%d %H:%M:%S"

    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executemany("INSERT INTO users (id,name,role,can_refund,can_modify_prices,status) VALUES (?,?,?,?,?,?)", [
        (1, "Admin General", "Administrador", 1, 1, "Activo"),
        (2, "María López", "Cajero", 0, 1, "Activo"),
        (3, "Mario Soto", "Vendedor", 0, 0, "Activo"),
    ])
    conn.executemany("INSERT INTO products (id,name,status) VALUES (?,?,?)",
                     [(1, "Coca Cola 2L", "Activo"), (2, "Papas Tostadas", "Activo")])
    conn.execute("INSERT INTO warehouses (id,name) VALUES (1,'Bodega A')")
    conn.executemany("INSERT INTO inventory VALUES (?,?,?,?,?)", [
        (1, 1, 50, 50, 10),   # ok
        (2, 1, 3, 5, 15),     # low stock + discrepancy
    ])
    conn.execute("INSERT INTO customers (id,name,pending_balance) VALUES (1,'Esteban Quito',15000)")
    conn.executemany("INSERT INTO sales (id,timestamp,total_amount,total_discount,status) VALUES (?,?,?,?,?)", [
        (1, today.strftime(fmt), 3000, 0, "Completada"),
        (2, today.strftime(fmt), 4500, 500, "Completada"),
        (3, yday.strftime(fmt), 9000, 0, "Completada"),   # yesterday — must be excluded from "today"
    ])
    conn.executemany("INSERT INTO sale_details (sale_id,product_id,quantity,unit_price,discount_applied) VALUES (?,?,?,?,?)", [
        (1, 1, 2, 1500, 0), (2, 2, 5, 1000, 500), (3, 1, 4, 1500, 0),
    ])
    conn.commit()
    conn.close()
    db.initialize()  # WAL
    return path


# ── PHASE 3 #1: date anchoring ────────────────────────────────────────────────

def test_resolve_period_today_is_half_open_single_day():
    start, end, label = db.resolve_period("today")
    assert label == "today" and start[11:] == "00:00:00"
    # exactly 24h window
    from datetime import datetime
    s = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    e = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
    assert (e - s) == timedelta(days=1)


def test_unknown_period_falls_back_to_today():
    _s, _e, label = db.resolve_period("since the dawn of time")
    assert label == "today"


def test_sales_summary_today_excludes_yesterday(pos_db):
    out = service.sales_summary("today")
    assert "Transactions: 2" in out          # the two 'today' sales, not yesterday's
    assert "₡7,500" in out                    # 3000 + 4500
    assert "₡500" in out                      # discounts


def test_sales_summary_yesterday(pos_db):
    out = service.sales_summary("yesterday")
    assert "Transactions: 1" in out and "₡9,000" in out


# ── PHASE 3 #3: accent/case folding + wildcard safety ─────────────────────────

@pytest.mark.parametrize("query", ["maria", "MARÍA", "María", "marIa lopez"])
def test_find_user_is_accent_and_case_insensitive(pos_db, query):
    assert "María López" in service.find_user(query)


def test_find_user_wildcard_is_escaped_not_matched(pos_db):
    # '%' must be treated literally, not as "match everything".
    assert "No user matches" in service.find_user("%")


# ── PHASE 3 #2: concurrency / write-safety ────────────────────────────────────

def test_read_connection_is_query_only(pos_db):
    with db.read_connection() as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE users SET status='Bloqueado' WHERE id=1")


def test_set_permission_updates_column_and_audits(pos_db):
    out = service.set_user_permission("maria", "refund", True, actor="Tester")
    assert "María López" in out and "enabled" in out

    with db.read_connection() as conn:
        user = conn.execute("SELECT can_refund FROM users WHERE id=2").fetchone()
        audit = conn.execute(
            "SELECT action, details FROM audit_logs WHERE user_id=2 ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert user["can_refund"] == 1
    assert audit["action"] == "MODIFICACION_PERMISOS" and "can_refund=1" in audit["details"]


def test_set_permission_rejects_unknown_permission(pos_db):
    out = service.set_user_permission("maria", "delete_everything", True)
    assert "Unknown permission" in out


def test_set_permission_ambiguous_name_is_refused(pos_db):
    # "mar" matches María and Mario → must refuse rather than update both.
    out = service.set_user_permission("mar", "refund", True)
    assert "several users" in out
    with db.read_connection() as conn:
        granted = conn.execute("SELECT COUNT(*) c FROM users WHERE can_refund=1 AND id IN (2,3)").fetchone()
    assert granted["c"] == 0  # nobody changed


def test_unknown_user_does_not_write(pos_db):
    assert "No user matches" in service.set_user_permission("Zoltan", "refund", True)


# ── inventory / customers happy paths ─────────────────────────────────────────

def test_low_stock_and_discrepancies(pos_db):
    assert "Papas Tostadas" in service.low_stock_report()
    disc = service.inventory_discrepancies()
    assert "Papas Tostadas" in disc and "-2" in disc  # physical 3 vs system 5


def test_customer_balances(pos_db):
    assert "Esteban Quito" in service.customer_balances() and "₡15,000" in service.customer_balances()
