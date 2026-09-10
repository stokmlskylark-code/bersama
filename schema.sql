CREATE TABLE IF NOT EXISTS users (
    id BIGINT PRIMARY KEY,
    name TEXT NOT NULL,
    username TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'customer'
        CHECK(role IN ('customer', 'reseller', 'admin')),
    reseller_request INTEGER NOT NULL DEFAULT 0,
    alamat_lengkap TEXT NOT NULL DEFAULT '',
    domisili TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
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
    product_name TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK(qty > 0),
    unit_price INTEGER NOT NULL,
    retail_price INTEGER NOT NULL,
    total INTEGER NOT NULL,
    reseller INTEGER NOT NULL,
    note TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_payment'
        CHECK(status IN ('pending_payment', 'awaiting_confirmation', 'paid',
            'shipped', 'completed', 'cancelled', 'expired')),
    proof_file_id TEXT,
    proof_kind TEXT,
    tracking TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS orders_user ON orders(user_id, id DESC);
CREATE INDEX IF NOT EXISTS orders_status ON orders(status, expires_at);

CREATE TABLE IF NOT EXISTS audit (
    id SERIAL PRIMARY KEY,
    actor_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    order_id INTEGER PRIMARY KEY REFERENCES orders(id),
    reference TEXT NOT NULL UNIQUE,
    project TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending', 'completed', 'closed', 'late_completed')),
    provider_status TEXT NOT NULL DEFAULT 'not_checked',
    checked_at INTEGER NOT NULL DEFAULT 0,
    check_after INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS payments_due ON payments(state, check_after);

CREATE TABLE IF NOT EXISTS tickets (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    subject TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open', 'replied', 'closed')),
    admin_reply TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS tickets_user ON tickets(user_id, id DESC);
CREATE INDEX IF NOT EXISTS tickets_status ON tickets(status);
