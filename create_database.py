import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "pos_assistant.db"


def create_database():
    if DB_PATH.exists():
        print(f"Removing existing database at {DB_PATH.name}...")
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Enable Foreign Keys
    cursor.execute("PRAGMA foreign_keys = ON;")

    print("Creating tables...")

    # 1. USERS & PERMISSIONS
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('Administrador', 'Cajero', 'Vendedor')),
        can_refund INTEGER DEFAULT 0 CHECK(can_refund IN (0,1)),
        can_modify_prices INTEGER DEFAULT 0 CHECK(can_modify_prices IN (0,1)),
        status TEXT DEFAULT 'Activo' CHECK(status IN ('Activo', 'Bloqueado')),
        updated_by INTEGER,
        FOREIGN KEY(updated_by) REFERENCES users(id)
    )''')

    # 2. CATEGORIES & PRODUCTS
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    )''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        barcode TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        category_id INTEGER,
        cost_price REAL NOT NULL,
        sell_price REAL NOT NULL,
        status TEXT DEFAULT 'Activo' CHECK(status IN ('Activo', 'Inactivo')),
        FOREIGN KEY(category_id) REFERENCES categories(id)
    )''')

    # 3. WAREHOUSES & INVENTORY
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS warehouses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    )''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS inventory (
        product_id INTEGER,
        warehouse_id INTEGER,
        stock_physical INTEGER NOT NULL DEFAULT 0,
        stock_system INTEGER NOT NULL DEFAULT 0,
        min_required INTEGER DEFAULT 5,
        PRIMARY KEY(product_id, warehouse_id),
        FOREIGN KEY(product_id) REFERENCES products(id),
        FOREIGN KEY(warehouse_id) REFERENCES warehouses(id)
    )''')

    # 4. CUSTOMERS
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT,
        pending_balance REAL DEFAULT 0.0
    )''')

    # 5. POS TERMINALS & SHIFTS (Cajas)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS pos_terminals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    )''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS cash_shifts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        terminal_id INTEGER,
        user_id INTEGER,
        open_time TEXT NOT NULL,
        close_time TEXT,
        expected_amount REAL NOT NULL,
        actual_amount REAL,
        status TEXT DEFAULT 'Abierta' CHECK(status IN ('Abierta', 'Cerrada')),
        FOREIGN KEY(terminal_id) REFERENCES pos_terminals(id),
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')

    # 6. SALES & INVOICES
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER,
        user_id INTEGER,
        terminal_id INTEGER,
        timestamp TEXT NOT NULL,
        total_discount REAL DEFAULT 0.0,
        total_amount REAL NOT NULL,
        status TEXT DEFAULT 'Completada' CHECK(status IN ('Completada', 'Devuelta')),
        authorized_by INTEGER,
        FOREIGN KEY(customer_id) REFERENCES customers(id),
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(terminal_id) REFERENCES pos_terminals(id),
        FOREIGN KEY(authorized_by) REFERENCES users(id)
    )''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sale_details (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sale_id INTEGER,
        product_id INTEGER,
        quantity INTEGER NOT NULL,
        unit_price REAL NOT NULL,
        discount_applied REAL DEFAULT 0.0,
        FOREIGN KEY(sale_id) REFERENCES sales(id),
        FOREIGN KEY(product_id) REFERENCES products(id)
    )''')

    # 7. LOGS & AUDIT TRAILS
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT NOT NULL,
        details TEXT,
        timestamp TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')

    print("Populating database with demo data...")
    
    # --- INSERT DATA ---
    # Users
    cursor.executemany("INSERT INTO users (id, name, role, can_refund, can_modify_prices, status) VALUES (?,?,?,?,?,?)", [
        (1, 'Admin General', 'Administrador', 1, 1, 'Activo'),
        (2, 'María López', 'Cajero', 0, 1, 'Activo'),
        (3, 'Juan Pérez', 'Cajero', 0, 0, 'Activo'),
        (4, 'Carlos Ex-empleado', 'Vendedor', 0, 0, 'Bloqueado')
    ])

    # Categories
    cursor.executemany("INSERT INTO categories (id, name) VALUES (?,?)", [(1, 'Bebidas'), (2, 'Snacks'), (3, 'Abarrotes')])

    # Products
    cursor.executemany("INSERT INTO products (id, barcode, name, category_id, cost_price, sell_price, status) VALUES (?,?,?,?,?,?,?)", [
        (1, '74410011', 'Coca Cola 2L', 1, 1000, 1500, 'Activo'),
        (2, '74410022', 'Papas Tostadas', 2, 600, 1000, 'Activo'),
        (3, '74410033', 'Arroz 1kg', 3, 700, 900, 'Activo'),
        (4, '74410044', 'Café Tarrazú 500g', 3, 2500, 5000, 'Activo'),
        (5, '74410055', 'Producto Vencido X', 3, 500, 1200, 'Activo')
    ])

    # Warehouses
    cursor.executemany("INSERT INTO warehouses (id, name) VALUES (?,?)", [(1, 'Bodega A (Principal)'), (2, 'Bodega B (Secundaria)')])

    # Inventory
    cursor.executemany("INSERT INTO inventory (product_id, warehouse_id, stock_physical, stock_system, min_required) VALUES (?,?,?,?,?)", [
        (1, 1, 50, 50, 10), (1, 2, 20, 20, 5),
        (2, 1, 3, 5, 15),   # <--- Low Stock & discrepancy!
        (3, 1, 100, 100, 20),
        (4, 1, 40, 40, 8),
        (5, 1, 0, 10, 5)    # Lost inventory
    ])

    # Customers
    cursor.executemany("INSERT INTO customers (id, name, phone, pending_balance) VALUES (?,?,?,?)", [
        (1, 'Juan Pérez', '50688888888', 0.0),
        (2, 'Esteban Quito', '50677777777', 15000.0) # Has pending balance
    ])

    # Terminals
    cursor.executemany("INSERT INTO pos_terminals (id, name) VALUES (?,?)", [(1, 'Caja Principal 1'), (2, 'Caja Rápida 2')])

    # Dates calculation for Sales History
    today = datetime.now().strftime('%Y-%m-%d')
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    last_month = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')

    # Cash Shifts
    cursor.executemany("INSERT INTO cash_shifts (terminal_id, user_id, open_time, close_time, expected_amount, actual_amount, status) VALUES (?,?,?,?,?,?,?)", [
        (2, 2, f"{today} 08:00:00", f"{today} 16:00:00", 45000, 44500, 'Cerrada'), # Discrepancy 
        (1, 3, f"{today} 09:00:00", None, 12000, None, 'Abierta')
    ])

    # Sales
    cursor.executemany("INSERT INTO sales (id, customer_id, user_id, terminal_id, timestamp, total_discount, total_amount, status, authorized_by) VALUES (?,?,?,?,?,?,?,?,?)", [
        (1, 1, 3, 1, f"{today} 10:15:00", 0.0, 3000, 'Completada', None),
        (2, 2, 2, 2, f"{today} 11:30:00", 500.0, 4500, 'Completada', 1), # Approved discount by Admin
        (3, 1, 2, 2, f"{yesterday} 14:00:00", 0.0, 9000, 'Completada', None),
        (4, 2, 3, 1, f"{last_month} 09:00:00", 0.0, 1500, 'Completada', None)
    ])

    # Sale Details
    cursor.executemany("INSERT INTO sale_details (sale_id, product_id, quantity, unit_price, discount_applied) VALUES (?,?,?,?,?)", [
        (1, 1, 2, 1500, 0.0), # 2 Cokes
        (2, 4, 1, 5000, 500.0), # 1 Café with discount
        (3, 4, 1, 5000, 0.0), (3, 1, 2, 1500, 0.0), (3, 2, 1, 1000, 0.0), # Big sale yesterday
        (4, 1, 1, 1500, 0.0) # Last month sale
    ])

    # Audit Logs
    cursor.executemany("INSERT INTO audit_logs (user_id, action, details, timestamp) VALUES (?,?,?,?)", [
        (1, 'MODIFICACION_PERMISOS', 'Se otorgó permiso de modificar precios a María López', f"{yesterday} 16:30:00"),
        (3, 'INGRESO_SISTEMA', 'Juan Pérez ingresó al POS', f"{today} 09:00:00"),
        (2, 'INGRESO_SISTEMA', 'María López ingresó al POS', f"{today} 08:00:00")
    ])

    conn.commit()
    conn.close()
    print(f"Database '{DB_PATH.name}' created successfully at {DB_PATH}!")

if __name__ == '__main__':
    create_database()
