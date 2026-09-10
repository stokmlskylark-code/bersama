import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import psycopg2
from store import ShopError, Store

PG_DSN = os.getenv("PG_DSN", "dbname=postgres user=postgres password=postgres host=localhost port=5432")


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id BIGINT PRIMARY KEY, name TEXT NOT NULL, username TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'customer'
        CHECK(role IN ('customer', 'reseller', 'admin')),
    reseller_request INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
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
"""


class StoreTests(unittest.TestCase):
    ADMIN = 1
    CUSTOMER = 2
    OTHER = 3
    NOW = 1_800_000_000

    @classmethod
    def setUpClass(cls):
        conn = psycopg2.connect(PG_DSN)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS settings, payments, audit, orders, products, users CASCADE")
            cur.execute(SCHEMA)
        conn.close()

    def setUp(self):
        conn = psycopg2.connect(PG_DSN)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("TRUNCATE settings, payments, audit, orders, products, users RESTART IDENTITY CASCADE")
        conn.close()
        self.store = Store(PG_DSN, {self.ADMIN}, ttl_hours=1)
        self.addCleanup(self.store.close)
        clock = patch("store.time.time", return_value=self.NOW)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        for user_id in (self.ADMIN, self.CUSTOMER, self.OTHER):
            self.store.register(user_id, f"User {user_id}")
        self.save_product()

    def save_product(self, price=100, reseller_price=70, stock=20):
        self.store.save_product(
            self.ADMIN, "SKU", "Original product", price, reseller_price, stock, "Description"
        )

    def buy(self, key="request-1", user=None, qty=2):
        return self.store.create_order(
            self.CUSTOMER if user is None else user, "sku", qty, "Delivery address", key
        )

    def state(self):
        result = {}
        for table in ("users", "products", "orders", "audit"):
            with self.store.db.cursor() as cur:
                col = "id" if table != "products" else "sku"
                cur.execute(f"SELECT * FROM {table} ORDER BY {col}")
                result[table] = [tuple(row) for row in cur.fetchall()]
        return result

    def test_price_and_stock_bounds_reject_without_mutation(self):
        for price, reseller_price, stock in (
            (0, 1, 20), (-1, 1, 20), (100, 0, 20), (100, -1, 20),
            (100, 101, 20), (1_000_000_001, 70, 20),
            (100, 70, -1), (100, 70, 1_000_001),
        ):
            with self.subTest(price=price, reseller_price=reseller_price, stock=stock):
                before = self.state()
                with self.assertRaises(ShopError):
                    self.save_product(price, reseller_price, stock)
                self.assertEqual(before, self.state())

    def test_fractional_prices_are_rejected(self):
        for price, reseller_price in ((100.5, 70), (100, 70.5)):
            with self.subTest(price=price, reseller_price=reseller_price):
                before = self.state()
                with self.assertRaises(ShopError):
                    self.save_product(price, reseller_price)
                self.assertEqual(before, self.state())

    def test_boolean_product_prices_and_stock_are_rejected(self):
        for field in ("price", "reseller_price", "stock"):
            for value in (True, False):
                with self.subTest(field=field, value=value):
                    values = {"price": 100, "reseller_price": 1, "stock": 20}
                    values[field] = value
                    before = self.state()
                    with self.assertRaises(ShopError):
                        self.save_product(**values)
                    self.assertEqual(before, self.state())

    def test_fractional_and_float_product_stock_are_rejected(self):
        for stock in (1.5, 1.0):
            with self.subTest(stock=stock):
                before = self.state()
                with self.assertRaises(ShopError):
                    self.save_product(stock=stock)
                self.assertEqual(before, self.state())

    def test_set_stock_rejects_fractional_float_and_boolean_values(self):
        for stock in (1.5, 1.0, True, False):
            with self.subTest(stock=stock, value_type=type(stock).__name__):
                before = self.state()
                with self.assertRaises(ShopError):
                    self.store.set_stock(self.ADMIN, "SKU", stock)
                self.assertEqual(before, self.state())

    def test_order_quantity_rejects_fractional_float_and_boolean_values(self):
        for qty in (1.5, 1.0, True, False):
            with self.subTest(qty=qty, value_type=type(qty).__name__):
                before = self.state()
                with self.assertRaises(ShopError):
                    self.buy(qty=qty)
                self.assertEqual(before, self.state())

    def test_price_and_stock_inclusive_boundaries(self):
        for price, reseller_price, stock in ((1, 1, 0), (1_000_000_000, 1_000_000_000, 1_000_000)):
            self.save_product(price, reseller_price, stock)
            product = self.store.product("sku")
            self.assertEqual((price, reseller_price, stock),
                             (product["price"], product["reseller_price"], product["stock"]))

    def test_all_admin_operations_reject_customer(self):
        order = self.buy()
        calls = (
            lambda: self.store.require_admin(self.CUSTOMER),
            lambda: self.store.set_reseller(self.CUSTOMER, self.OTHER, True),
            lambda: self.store.reseller_requests(self.CUSTOMER),
            lambda: self.store.save_product(self.CUSTOMER, "NEW", "Name", 10, 8, 1, ""),
            lambda: self.store.set_stock(self.CUSTOMER, "SKU", 100),
            lambda: self.store.set_active(self.CUSTOMER, "SKU", False),
            lambda: self.store.transition(self.CUSTOMER, order["id"], "approve"),
            lambda: self.store.orders(self.CUSTOMER, admin=True),
            lambda: self.store.stats(self.CUSTOMER, admin=True),
            lambda: self.store.admin_counts(self.CUSTOMER),
            lambda: self.store.audit(self.CUSTOMER),
        )
        before = self.state()
        for index, call in enumerate(calls):
            with self.subTest(operation=index), self.assertRaises(ShopError):
                call()
            self.assertEqual(before, self.state())

    def test_admin_authority_comes_from_configuration_not_database_role(self):
        with self.store.db.cursor() as cur:
            cur.execute("UPDATE users SET role='admin' WHERE id=%s", (self.CUSTOMER,))
        with self.assertRaises(ShopError):
            self.store.require_admin(self.CUSTOMER)
        self.assertEqual("customer", self.store.register(self.CUSTOMER, "Updated")["role"])
        with self.store.db.cursor() as cur:
            cur.execute("UPDATE users SET role='customer' WHERE id=%s", (self.ADMIN,))
        self.store.require_admin(self.ADMIN)
        self.assertEqual("admin", self.store.register(self.ADMIN, "Admin")["role"])

    def test_reseller_request_approval_re_registration_and_revocation(self):
        self.store.request_reseller(self.CUSTOMER)
        self.assertEqual([self.CUSTOMER], [row["id"] for row in self.store.reseller_requests(self.ADMIN)])
        with self.assertRaises(ShopError):
            self.store.request_reseller(self.CUSTOMER)
        self.store.set_reseller(self.ADMIN, self.CUSTOMER, True)
        self.assertEqual([], self.store.reseller_requests(self.ADMIN))
        self.assertEqual("reseller", self.store.register(self.CUSTOMER, "Renamed")["role"])
        with self.assertRaises(ShopError):
            self.store.request_reseller(self.CUSTOMER)
        self.assertEqual(70, self.buy()["unit_price"])
        self.store.set_reseller(self.ADMIN, self.CUSTOMER, False)
        self.assertEqual("customer", self.store.user(self.CUSTOMER)["role"])
        self.assertEqual(100, self.buy("retail")["unit_price"])

    def test_admin_cannot_be_converted_to_reseller(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled), self.assertRaises(ShopError):
                self.store.set_reseller(self.ADMIN, self.ADMIN, enabled)
        with self.assertRaises(ShopError):
            self.store.request_reseller(self.ADMIN)
        self.assertEqual("admin", self.store.user(self.ADMIN)["role"])

    def test_order_snapshots_price_name_and_reseller_role(self):
        self.store.set_reseller(self.ADMIN, self.CUSTOMER, True)
        original = dict(self.buy(qty=3))
        self.store.save_product(self.ADMIN, "SKU", "Renamed", 200, 150, 17, "New")
        self.store.set_reseller(self.ADMIN, self.CUSTOMER, False)
        self.assertEqual(original, dict(self.store.order(original["id"], self.CUSTOMER)))
        self.assertEqual((70, 100, 210, 1, "Original product"),
                         tuple(original[key] for key in ("unit_price", "retail_price", "total", "reseller", "product_name")))
        self.assertEqual(200, self.buy("new-price")["unit_price"])

    def test_idempotency_preserves_original_order_and_stock(self):
        original = dict(self.buy())
        before = self.state()
        duplicate = self.store.create_order(self.CUSTOMER, "UNKNOWN", 3, "Changed address", "request-1")
        self.assertEqual(original, dict(duplicate))
        self.assertEqual(before, self.state())

    def test_idempotency_key_cannot_be_reused_by_another_user(self):
        self.buy()
        before = self.state()
        with self.assertRaises(ShopError):
            self.buy(user=self.OTHER)
        self.assertEqual(before, self.state())

    def test_cancelled_idempotency_key_does_not_reserve_again(self):
        order = self.buy()
        self.store.cancel(self.CUSTOMER, order["id"])
        before = self.state()
        self.assertEqual("cancelled", self.buy()["status"])
        self.assertEqual(before, self.state())

    def concurrent_orders(self, same_key=False):
        barrier = threading.Barrier(2)

        def reserve(index):
            connection = Store(PG_DSN, {self.ADMIN}, ttl_hours=1)
            try:
                barrier.wait(timeout=5)
                try:
                    order = connection.create_order(
                        self.CUSTOMER, "SKU", 1, "Address", "same" if same_key else f"concurrent-{index}"
                    )
                    return order["id"]
                except (ShopError, psycopg2.IntegrityError):
                    return None
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(reserve, range(2)))

    def test_two_sqlite_connections_cannot_oversell_last_item(self):
        self.store.set_stock(self.ADMIN, "SKU", 1)
        results = self.concurrent_orders()
        self.assertEqual(1, sum(result is not None for result in results))
        self.assertEqual(0, self.store.product("SKU")["stock"])
        self.assertEqual(1, len(self.store.orders(self.CUSTOMER)))

    def test_two_sqlite_connections_same_request_reserve_only_once(self):
        self.store.set_stock(self.ADMIN, "SKU", 1)
        results = self.concurrent_orders(same_key=True)
        non_none = [r for r in results if r is not None]
        if non_none:
            self.assertEqual(non_none[0], non_none[-1])
        self.assertEqual(0, self.store.product("SKU")["stock"])
        self.assertEqual(1, len(self.store.orders(self.CUSTOMER)))

    def test_insufficient_stock_rolls_back(self):
        before = self.state()
        with self.assertRaises(ShopError):
            self.buy(qty=21)
        self.assertEqual(before, self.state())

    def test_invalid_order_input_does_not_mutate(self):
        for qty, note in ((0, "Address"), (-1, "Address"), (1001, "Address"), (1, " "), (1, "x" * 1001)):
            with self.subTest(qty=qty, note_length=len(note)):
                before = self.state()
                with self.assertRaises(ShopError):
                    self.store.create_order(self.CUSTOMER, "SKU", qty, note, "invalid")
                self.assertEqual(before, self.state())

    def test_hidden_product_cannot_be_ordered(self):
        self.store.set_active(self.ADMIN, "SKU", False)
        before = self.state()
        with self.assertRaises(ShopError):
            self.buy()
        self.assertEqual(before, self.state())

    def test_expiry_at_deadline_restocks_exactly_once(self):
        order = self.buy(qty=3)
        self.clock.return_value = order["expires_at"] - 1
        self.store.expire()
        self.assertEqual("pending_payment", self.store.order(order["id"], self.CUSTOMER)["status"])
        self.assertEqual(17, self.store.product("SKU")["stock"])
        self.clock.return_value += 1
        self.store.expire()
        self.assertEqual("expired", self.store.order(order["id"], self.CUSTOMER)["status"])
        self.assertEqual(20, self.store.product("SKU")["stock"])
        before = self.state()
        self.store.expire()
        self.assertEqual(before, self.state())
        with self.assertRaises(ShopError):
            self.store.cancel(self.CUSTOMER, order["id"])

    def test_create_order_releases_expired_reservations(self):
        self.store.set_stock(self.ADMIN, "SKU", 2)
        expired = self.buy()
        self.clock.return_value = expired["expires_at"]
        fresh = self.buy("fresh", user=self.OTHER)
        self.assertEqual("expired", self.store.order(expired["id"], self.CUSTOMER)["status"])
        self.assertEqual("pending_payment", fresh["status"])
        self.assertEqual(0, self.store.product("SKU")["stock"])

    def test_awaiting_confirmation_never_expires_or_allows_cancellation(self):
        order = self.buy()
        self.store.submit_proof(self.CUSTOMER, order["id"], "file", "photo")
        self.clock.return_value = order["expires_at"] + 30 * 86400
        self.store.expire()
        row = self.store.order(order["id"], self.CUSTOMER)
        self.assertEqual(("awaiting_confirmation", "file"), (row["status"], row["proof_file_id"]))
        for actor in (self.CUSTOMER, self.ADMIN):
            with self.subTest(actor=actor), self.assertRaises(ShopError):
                self.store.cancel(actor, order["id"])
        self.assertEqual(18, self.store.product("SKU")["stock"])

    def test_proof_at_expiry_is_rejected_and_stock_returned(self):
        order = self.buy()
        self.clock.return_value = order["expires_at"]
        with self.assertRaises(ShopError):
            self.store.submit_proof(self.CUSTOMER, order["id"], "late", "document")
        self.assertEqual("expired", self.store.order(order["id"], self.CUSTOMER)["status"])
        self.assertEqual(20, self.store.product("SKU")["stock"])

    def test_cancellation_cannot_double_restock_even_after_deadline(self):
        order = self.buy(qty=4)
        self.store.cancel(self.CUSTOMER, order["id"])
        self.assertEqual(20, self.store.product("SKU")["stock"])
        before = self.state()
        for actor in (self.CUSTOMER, self.ADMIN):
            with self.subTest(actor=actor), self.assertRaises(ShopError):
                self.store.cancel(actor, order["id"])
        self.clock.return_value = order["expires_at"] + 1
        self.store.expire()
        self.assertEqual(before, self.state())

    def test_order_and_proof_ownership(self):
        order = self.buy()
        self.assertEqual(order["id"], self.store.order(order["id"], self.ADMIN)["id"])
        self.assertEqual([], self.store.orders(self.OTHER))
        before = self.state()
        for operation in (
            lambda: self.store.order(order["id"], self.OTHER),
            lambda: self.store.cancel(self.OTHER, order["id"]),
            lambda: self.store.submit_proof(self.OTHER, order["id"], "stolen", "photo"),
            lambda: self.store.submit_proof(self.ADMIN, order["id"], "admin-file", "photo"),
        ):
            with self.assertRaises(ShopError):
                operation()
            self.assertEqual(before, self.state())

    def test_proof_validation_and_owner_replacement(self):
        order = self.buy()
        for file_id, kind in (("", "photo"), ("file", "video")):
            with self.subTest(file_id=file_id, kind=kind), self.assertRaises(ShopError):
                self.store.submit_proof(self.CUSTOMER, order["id"], file_id, kind)
        self.store.submit_proof(self.CUSTOMER, order["id"], "first", "photo")
        self.store.submit_proof(self.CUSTOMER, order["id"], "second", "document")
        row = self.store.order(order["id"], self.CUSTOMER)
        self.assertEqual(("awaiting_confirmation", "second", "document"),
                         (row["status"], row["proof_file_id"], row["proof_kind"]))
        self.assertEqual(18, self.store.product("SKU")["stock"])

    def test_reject_resets_proof_and_payment_deadline_then_full_lifecycle(self):
        order = self.buy()
        self.store.submit_proof(self.CUSTOMER, order["id"], "bad", "photo")
        self.clock.return_value = order["expires_at"] + 100
        row = self.store.transition(self.ADMIN, order["id"], "reject", "Unreadable")
        self.assertEqual("pending_payment", row["status"])
        self.assertIsNone(row["proof_file_id"])
        self.assertIsNone(row["proof_kind"])
        self.assertEqual(self.clock.return_value + 3600, row["expires_at"])
        self.store.submit_proof(self.CUSTOMER, order["id"], "good", "document")
        for action, status, detail in (("approve", "paid", ""), ("complete", "completed", "")):
            row = self.store.transition(self.ADMIN, order["id"], action, detail)
            self.assertEqual(status, row["status"])
            self.assertEqual("good", row["proof_file_id"])
            before = self.state()
            with self.assertRaises(ShopError):
                self.store.submit_proof(self.CUSTOMER, order["id"], "late", "photo")
            with self.assertRaises(ShopError):
                self.store.cancel(self.CUSTOMER, order["id"])
            self.assertEqual(before, self.state())
        self.clock.return_value += 86400
        self.store.expire()
        self.assertEqual(18, self.store.product("SKU")["stock"])

    def test_invalid_transitions_preserve_state(self):
        order = self.buy()
        actions = ("approve", "reject", "complete")
        for status, valid in (("pending_payment", ()), ("awaiting_confirmation", ("approve", "reject")),
                              ("paid", ("complete",)),
                              ("completed", ()), ("cancelled", ()), ("expired", ())):
            with self.store.db.cursor() as cur:
                cur.execute("UPDATE orders SET status=%s WHERE id=%s", (status, order["id"]))
            self.store.db.commit()
            for action in actions + ("unknown",):
                if action in valid:
                    continue
                with self.subTest(status=status, action=action):
                    before = self.state()
                    with self.assertRaises(ShopError):
                        self.store.transition(self.ADMIN, order["id"], action, "Detail")
                    self.assertEqual(before, self.state())

    def test_reject_requires_bounded_nonblank_detail(self):
        order = self.buy()
        self.store.submit_proof(self.CUSTOMER, order["id"], "file", "photo")
        for detail in ("", " ", "x" * 501):
            with self.subTest(detail_length=len(detail)):
                before = self.state()
                with self.assertRaises(ShopError):
                    self.store.transition(self.ADMIN, order["id"], "reject", detail)
                self.assertEqual(before, self.state())

    def test_transaction_rolls_back_and_connection_remains_usable(self):
        before = self.state()
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.store.transaction():
                with self.store.db.cursor() as cur:
                    cur.execute("UPDATE products SET stock=0")
                raise RuntimeError("abort")
        self.assertEqual(before, self.state())
        self.assertEqual("pending_payment", self.buy()["status"])

    def test_failed_order_audit_rolls_back_stock_and_insert(self):
        import psycopg2 as dberrors
        before = self.state()
        with patch.object(self.store, "_audit", side_effect=dberrors.OperationalError("audit failed")):
            with self.assertRaisesRegex(dberrors.OperationalError, "audit failed"):
                self.buy()
        self.assertEqual(before, self.state())
        self.assertEqual(200, self.buy()["total"])

    def test_failed_cancellation_audit_rolls_back_status_and_restock(self):
        order = self.buy()
        before = self.state()
        with patch.object(self.store, "_audit", side_effect=RuntimeError("audit failed")):
            with self.assertRaises(RuntimeError):
                self.store.cancel(self.CUSTOMER, order["id"])
        self.assertEqual(before, self.state())

    def test_dashboard_stats_scope_status_totals_and_snapshot_savings(self):
        self.assertEqual({}, self.store.stats(self.CUSTOMER))
        self.store.set_reseller(self.ADMIN, self.CUSTOMER, True)
        paid = self.buy("paid", qty=3)
        self.store.submit_proof(self.CUSTOMER, paid["id"], "file", "photo")
        self.store.transition(self.ADMIN, paid["id"], "approve")
        self.buy("pending", qty=2)
        cancelled = self.buy("cancelled", qty=1)
        self.store.cancel(self.CUSTOMER, cancelled["id"])
        self.buy("other", user=self.OTHER, qty=4)
        self.save_product(price=300, reseller_price=100, stock=10)
        self.store.set_reseller(self.ADMIN, self.CUSTOMER, False)
        expected = {
            "paid": {"status": "paid", "count": 1, "total": 210, "saving": 90},
            "pending_payment": {"status": "pending_payment", "count": 1, "total": 140, "saving": 60},
            "cancelled": {"status": "cancelled", "count": 1, "total": 70, "saving": 30},
        }
        self.assertEqual(expected, self.store.stats(self.CUSTOMER))
        self.assertEqual(0, self.store.stats(self.OTHER)["pending_payment"]["saving"])
        expected["pending_payment"] = {"status": "pending_payment", "count": 2, "total": 540, "saving": 60}
        self.assertEqual(expected, self.store.stats(self.ADMIN, admin=True))
        self.assertEqual({}, self.store.stats(self.ADMIN))

    def test_admin_dashboard_counts_only_active_products(self):
        self.store.set_reseller(self.ADMIN, self.CUSTOMER, True)
        self.store.save_product(self.ADMIN, "LOW", "Low stock", 10, 5, 5, "")
        self.store.save_product(self.ADMIN, "HIDDEN", "Hidden", 10, 5, 0, "")
        self.store.set_active(self.ADMIN, "HIDDEN", False)
        self.assertEqual({"users": 3, "resellers": 1, "products": 2, "low_stock": 1},
                         self.store.admin_counts(self.ADMIN))


if __name__ == "__main__":
    unittest.main()
