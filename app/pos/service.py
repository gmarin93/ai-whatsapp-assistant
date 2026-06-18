"""Semantic layer: the *only* DB operations the agent can perform.

Each function is one business intent with typed parameters and hand-written,
fully-parameterized SQL. The LLM selects a function and supplies values — it
never composes SQL, so SQL injection from WhatsApp text is structurally
impossible and joins/date-logic can't be hallucinated.

Every function returns a short, WhatsApp-friendly string.
"""
import logging
import sqlite3

from app.pos.db import (
    like_term,
    now_str,
    read_connection,
    resolve_period,
    write_connection,
)

logger = logging.getLogger(__name__)


def _crc(amount) -> str:
    """Format colones: 1500 → ₡1,500."""
    try:
        return f"₡{float(amount):,.0f}"
    except (TypeError, ValueError):
        return "₡0"


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {
        "1", "true", "yes", "y", "on", "enable", "enabled", "si", "sí", "activo", "active",
    }


# ── Sales metrics ─────────────────────────────────────────────────────────────

def sales_summary(period: str = "today") -> str:
    """Revenue, transaction count, discounts and average ticket for a period."""
    start, end, label = resolve_period(period)
    sql = """
        SELECT COUNT(*)                       AS n,
               COALESCE(SUM(total_amount), 0) AS total,
               COALESCE(SUM(total_discount),0)AS disc,
               COALESCE(AVG(total_amount), 0) AS avg_ticket
        FROM sales
        WHERE status = 'Completada'
          AND timestamp >= :start AND timestamp < :end
    """
    with read_connection() as conn:
        r = conn.execute(sql, {"start": start, "end": end}).fetchone()

    pretty = label.replace("_", " ")
    if r["n"] == 0:
        return f"No completed sales for {pretty} ({start[:10]})."
    return (
        f"📊 Sales — {pretty} ({start[:10]} → {end[:10]}):\n"
        f"• Transactions: {r['n']}\n"
        f"• Revenue: {_crc(r['total'])}\n"
        f"• Discounts given: {_crc(r['disc'])}\n"
        f"• Avg ticket: {_crc(r['avg_ticket'])}"
    )


def top_products(period: str = "today", limit: int = 5) -> str:
    """Best-selling products by units sold for a period."""
    start, end, label = resolve_period(period)
    limit = max(1, min(int(limit or 5), 20))
    sql = """
        SELECT p.name                                              AS name,
               SUM(sd.quantity)                                    AS qty,
               SUM(sd.quantity * sd.unit_price - sd.discount_applied) AS revenue
        FROM sale_details sd
        JOIN sales    s ON s.id = sd.sale_id
        JOIN products p ON p.id = sd.product_id
        WHERE s.status = 'Completada'
          AND s.timestamp >= :start AND s.timestamp < :end
        GROUP BY p.id
        ORDER BY qty DESC
        LIMIT :limit
    """
    with read_connection() as conn:
        rows = conn.execute(sql, {"start": start, "end": end, "limit": limit}).fetchall()

    pretty = label.replace("_", " ")
    if not rows:
        return f"No products sold for {pretty} ({start[:10]})."
    lines = [f"🏆 Top products — {pretty}:"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['name']} — {r['qty']} u · {_crc(r['revenue'])}")
    return "\n".join(lines)


# ── Inventory ─────────────────────────────────────────────────────────────────

def low_stock_report() -> str:
    """Active products whose physical stock is below the minimum required."""
    sql = """
        SELECT p.name AS product, w.name AS warehouse,
               i.stock_physical AS stock, i.min_required AS minimum
        FROM inventory i
        JOIN products   p ON p.id = i.product_id
        JOIN warehouses w ON w.id = i.warehouse_id
        WHERE i.stock_physical < i.min_required AND p.status = 'Activo'
        ORDER BY (i.min_required - i.stock_physical) DESC
    """
    with read_connection() as conn:
        rows = conn.execute(sql).fetchall()
    if not rows:
        return "✅ All products are above their minimum stock levels."
    lines = ["⚠️ Low stock:"]
    for r in rows:
        lines.append(f"• {r['product']} @ {r['warehouse']}: {r['stock']} (min {r['minimum']})")
    return "\n".join(lines)


def inventory_discrepancies() -> str:
    """Items where counted (physical) stock differs from the system figure."""
    sql = """
        SELECT p.name AS product, w.name AS warehouse,
               i.stock_physical AS physical, i.stock_system AS system,
               (i.stock_physical - i.stock_system) AS diff
        FROM inventory i
        JOIN products   p ON p.id = i.product_id
        JOIN warehouses w ON w.id = i.warehouse_id
        WHERE i.stock_physical <> i.stock_system
        ORDER BY ABS(i.stock_physical - i.stock_system) DESC
    """
    with read_connection() as conn:
        rows = conn.execute(sql).fetchall()
    if not rows:
        return "✅ No inventory discrepancies — physical matches system."
    lines = ["🔎 Inventory discrepancies:"]
    for r in rows:
        sign = "+" if r["diff"] > 0 else ""
        lines.append(
            f"• {r['product']} @ {r['warehouse']}: physical {r['physical']} vs "
            f"system {r['system']} ({sign}{r['diff']})"
        )
    return "\n".join(lines)


# ── Customers ─────────────────────────────────────────────────────────────────

def customer_balances() -> str:
    """Customers with an outstanding (pending) balance."""
    sql = """
        SELECT name, pending_balance AS balance
        FROM customers
        WHERE pending_balance > 0
        ORDER BY pending_balance DESC
    """
    with read_connection() as conn:
        rows = conn.execute(sql).fetchall()
    if not rows:
        return "✅ No customers with a pending balance."
    lines = ["💳 Pending balances:"]
    for r in rows:
        lines.append(f"• {r['name']}: {_crc(r['balance'])}")
    return "\n".join(lines)


# ── Users & permissions ───────────────────────────────────────────────────────

def find_user(name: str) -> str:
    """Look up a user (accent/case-insensitive) and show their permissions."""
    sql = """
        SELECT name, role, can_refund, can_modify_prices, status
        FROM users
        WHERE unaccent(name) LIKE unaccent(:p) ESCAPE '\\'
        ORDER BY name
    """
    with read_connection() as conn:
        rows = conn.execute(sql, {"p": like_term(name)}).fetchall()
    if not rows:
        return f"No user matches “{name}”."
    lines = []
    for r in rows:
        lines.append(
            f"👤 {r['name']} — {r['role']} [{r['status']}]\n"
            f"   refunds: {'yes' if r['can_refund'] else 'no'} · "
            f"modify prices: {'yes' if r['can_modify_prices'] else 'no'}"
        )
    return "\n".join(lines)


# Whitelist: friendly permission name → real column. User text NEVER reaches the
# SQL as an identifier — only these vetted column names do. This is what makes
# the dynamic UPDATE safe.
_PERMISSION_COLUMNS = {
    "refund": "can_refund",
    "refunds": "can_refund",
    "can_refund": "can_refund",
    "modify_prices": "can_modify_prices",
    "prices": "can_modify_prices",
    "price": "can_modify_prices",
    "can_modify_prices": "can_modify_prices",
}


def set_user_permission(name: str, permission: str, enabled, actor: str = "WhatsApp Admin") -> str:
    """Grant/revoke a permission for a user, writing an audit log entry.

    Args:
        name: User to modify (matched accent/case-insensitively; must be unique).
        permission: One of refund / modify_prices.
        enabled: Truthy to grant, falsy to revoke.
        actor: Who initiated the change (for the audit trail).
    """
    perm_key = (permission or "").strip().lower().replace(" ", "_")
    column = _PERMISSION_COLUMNS.get(perm_key)
    if column is None:
        allowed = "refund, modify_prices"
        return f"Unknown permission “{permission}”. Allowed: {allowed}."

    # Resolve to exactly one user before writing (avoid blanket updates).
    with read_connection() as conn:
        matches = conn.execute(
            "SELECT id, name FROM users WHERE unaccent(name) LIKE unaccent(:p) ESCAPE '\\'",
            {"p": like_term(name)},
        ).fetchall()
    if not matches:
        return f"No user matches “{name}”."
    if len(matches) > 1:
        names = ", ".join(m["name"] for m in matches)
        return f"“{name}” matches several users ({names}). Please be more specific."

    user_id, user_name = matches[0]["id"], matches[0]["name"]
    value = 1 if _truthy(enabled) else 0

    try:
        with write_connection() as conn:
            # `column` is from the whitelist above — safe to interpolate.
            conn.execute(
                f"UPDATE users SET {column} = :value WHERE id = :id",
                {"value": value, "id": user_id},
            )
            conn.execute(
                """INSERT INTO audit_logs (user_id, action, details, timestamp)
                   VALUES (:uid, 'MODIFICACION_PERMISOS', :details, :ts)""",
                {
                    "uid": user_id,
                    "details": f"{actor} set {column}={value} for {user_name}",
                    "ts": now_str(),
                },
            )
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            return "The system is busy right now — please try again in a moment."
        logger.exception("Permission update failed")
        return "Sorry, that update couldn't be completed."

    state = "enabled" if value else "disabled"
    return f"✅ {user_name}: “{perm_key}” {state}."
