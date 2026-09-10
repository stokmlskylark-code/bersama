"""Migrate SQLite database to PostgreSQL."""

import os
import sqlite3
import psycopg2
import psycopg2.extras

SQLITE_PATH = os.getenv("DATABASE_PATH", "data/shop.sqlite3")
PG_DSN = os.getenv("PG_DSN", "dbname=postgres user=postgres password=postgres host=localhost port=5432")


def create_pg_tables(pg):
    cur = pg.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id BIGINT PRIMARY KEY, name TEXT NOT NULL, username TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'customer'
                CHECK(role IN ('customer', 'reseller', 'admin')),
            reseller_request INTEGER NOT NULL DEFAULT 0,
            alamat_lengkap TEXT NOT NULL DEFAULT '',
            domisili TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS products (
            sku TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '',
            price INTEGER NOT NULL CHECK(price > 0),
            reseller_price INTEGER NOT NULL CHECK(reseller_price > 0 AND reseller_price <= price),
            stock INTEGER NOT NULL CHECK(stock >= 0),
            active INTEGER NOT NULL DEFAULT 1,
            seller_id BIGINT REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            request_key TEXT NOT NULL UNIQUE,
            user_id BIGINT NOT NULL REFERENCES users(id),
            sku TEXT NOT NULL REFERENCES products(sku),
            product_name TEXT NOT NULL, qty INTEGER NOT NULL CHECK(qty > 0),
            unit_price INTEGER NOT NULL, retail_price INTEGER NOT NULL,
            total INTEGER NOT NULL, reseller INTEGER NOT NULL,
            note TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending_payment'
                CHECK(status IN ('pending_payment', 'awaiting_confirmation', 'paid',
                    'shipped', 'completed', 'cancelled', 'expired')),
            proof_file_id TEXT, proof_kind TEXT, tracking TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS orders_user ON orders(user_id, id DESC);
        CREATE INDEX IF NOT EXISTS orders_status ON orders(status, expires_at);
        CREATE TABLE IF NOT EXISTS audit (
            id SERIAL PRIMARY KEY, actor_id BIGINT NOT NULL,
            action TEXT NOT NULL, detail TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS payments (
            order_id INTEGER PRIMARY KEY REFERENCES orders(id),
            reference TEXT NOT NULL UNIQUE, project TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN ('pending', 'completed', 'closed', 'late_completed')),
            provider_status TEXT NOT NULL DEFAULT 'not_checked',
            checked_at INTEGER NOT NULL DEFAULT 0,
            check_after INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS payments_due ON payments(state, check_after);
        CREATE TABLE IF NOT EXISTS tickets (
            id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id),
            subject TEXT NOT NULL, message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
                CHECK(status IN ('open', 'replied', 'closed')),
            admin_reply TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS tickets_user ON tickets(user_id, id DESC);
        CREATE INDEX IF NOT EXISTS tickets_status ON tickets(status);
    """)
    pg.commit()
    print("Tables created.")


def migrate_data(sqlite, pg):
    cur_sql = sqlite.cursor()
    cur_pg = pg.cursor()

    # Users
    users = cur_sql.execute("SELECT * FROM users").fetchall()
    cols = [d[0] for d in cur_sql.description]
    for row in users:
        data = dict(zip(cols, row))
        cur_pg.execute(
            "INSERT INTO users (id, name, username, role, reseller_request, created_at) "
            "VALUES (%(id)s, %(name)s, %(username)s, %(role)s, %(reseller_request)s, %(created_at)s) "
            "ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, username=EXCLUDED.username, "
            "role=EXCLUDED.role, reseller_request=EXCLUDED.reseller_request",
            data
        )
    print(f"Users: {len(users)} migrated.")

    # Products
    products = cur_sql.execute("SELECT * FROM products").fetchall()
    cols = [d[0] for d in cur_sql.description]
    for row in products:
        data = dict(zip(cols, row))
        cur_pg.execute(
            "INSERT INTO products (sku, name, description, price, reseller_price, stock, active, seller_id) "
            "VALUES (%(sku)s, %(name)s, %(description)s, %(price)s, %(reseller_price)s, %(stock)s, %(active)s, %(seller_id)s) "
            "ON CONFLICT (sku) DO UPDATE SET name=EXCLUDED.name, description=EXCLUDED.description, "
            "price=EXCLUDED.price, reseller_price=EXCLUDED.reseller_price, stock=EXCLUDED.stock, "
            "active=EXCLUDED.active, seller_id=EXCLUDED.seller_id",
            data
        )
    print(f"Products: {len(products)} migrated.")

    # Orders
    orders = cur_sql.execute("SELECT * FROM orders").fetchall()
    cols = [d[0] for d in cur_sql.description]
    for row in orders:
        data = dict(zip(cols, row))
        cur_pg.execute(
            "INSERT INTO orders (id, request_key, user_id, sku, product_name, qty, unit_price, "
            "retail_price, total, reseller, note, status, proof_file_id, proof_kind, tracking, "
            "created_at, expires_at, updated_at) "
            "VALUES (%(id)s, %(request_key)s, %(user_id)s, %(sku)s, %(product_name)s, %(qty)s, "
            "%(unit_price)s, %(retail_price)s, %(total)s, %(reseller)s, %(note)s, %(status)s, "
            "%(proof_file_id)s, %(proof_kind)s, %(tracking)s, %(created_at)s, %(expires_at)s, %(updated_at)s) "
            "ON CONFLICT (request_key) DO NOTHING",
            data
        )
    if orders:
        cur_pg.execute("SELECT setval('orders_id_seq', (SELECT COALESCE(MAX(id), 1) FROM orders))")
    print(f"Orders: {len(orders)} migrated.")

    # Payments
    payments = cur_sql.execute("SELECT * FROM payments").fetchall()
    cols = [d[0] for d in cur_sql.description]
    for row in payments:
        data = dict(zip(cols, row))
        cur_pg.execute(
            "INSERT INTO payments (order_id, reference, project, state, provider_status, checked_at, check_after) "
            "VALUES (%(order_id)s, %(reference)s, %(project)s, %(state)s, %(provider_status)s, "
            "%(checked_at)s, %(check_after)s) ON CONFLICT (order_id) DO NOTHING",
            data
        )
    print(f"Payments: {len(payments)} migrated.")

    # Audit
    audits = cur_sql.execute("SELECT * FROM audit").fetchall()
    cols = [d[0] for d in cur_sql.description]
    for row in audits:
        data = dict(zip(cols, row))
        cur_pg.execute(
            "INSERT INTO audit (actor_id, action, detail, created_at) "
            "VALUES (%(actor_id)s, %(action)s, %(detail)s, %(created_at)s)",
            data
        )
    if audits:
        cur_pg.execute("SELECT setval('audit_id_seq', (SELECT COALESCE(MAX(id), 1) FROM audit))")
    print(f"Audit: {len(audits)} migrated.")

    # Settings
    settings = cur_sql.execute("SELECT * FROM settings").fetchall()
    for row in settings:
        cur_pg.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            (row[0], row[1])
        )
    print(f"Settings: {len(settings)} migrated.")

    pg.commit()


def main():
    print(f"SQLite: {SQLITE_PATH}")
    print(f"PostgreSQL: {PG_DSN}")

    sqlite = sqlite3.connect(SQLITE_PATH)
    pg = psycopg2.connect(PG_DSN)

    create_pg_tables(pg)
    migrate_data(sqlite, pg)

    sqlite.close()
    pg.close()
    print("Migration complete!")


if __name__ == "__main__":
    main()
