import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import psycopg2
from bot import Config, ShopBot, Telegram, TelegramError, load_env, main
from store import Store

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


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.blocked = set()

    def call(self, method, **payload):
        self.calls.append((method, payload))
        if payload.get("chat_id") in self.blocked:
            raise TelegramError(403)
        return True


class BotTests(unittest.TestCase):
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
        self.store = Store(PG_DSN, {1})
        self.addCleanup(self.store.close)
        self.api = FakeTelegram()
        self.config = Config("123:token", frozenset({1}), "Toko Test", "Rekening test", "@admin", PG_DSN, 24)
        self.bot = ShopBot(self.config, self.store, self.api)
        self.update_id = 0
        for actor in (1, 2, 3):
            self.send(actor, "/start")
        self.send(1, "/produkset A | Produk A | 10000 | 8000 | 10 | Deskripsi")
        self.api.calls.clear()

    def send(self, actor, text=None, callback=None, **extra):
        self.update_id += 1
        message = {"from": {"id": actor, "first_name": f"User {actor}"},
                   "chat": {"id": actor, "type": "private"}, **extra}
        if text is not None:
            message["text"] = text
        update = {"update_id": self.update_id, "message": message}
        if callback is not None:
            update = {"update_id": self.update_id, "callback_query": {
                "id": str(self.update_id), "data": callback, "from": message["from"], "message": message}}
        self.bot.handle(update)
        return update

    def texts(self, actor):
        return "\n".join(payload.get("text", "") for method, payload in self.api.calls
                         if method == "sendMessage" and payload["chat_id"] == actor)

    def order(self, actor=2):
        self.send(actor, "/pesan A | 2 | Tujuan test")
        return self.store.orders(actor)[0]["id"]

    def proof(self, actor, order_id):
        self.send(actor, photo=[{"file_id": "proof123", "file_size": 100}], caption=f"/bukti {order_id}")

    def test_full_customer_admin_flow(self):
        order_id = self.order()
        self.assertIn("Rp20.000", self.texts(2))
        self.proof(2, order_id)
        self.send(1, callback=f"proof:{order_id}")
        self.assertTrue(any(method == "sendPhoto" and payload["photo"] == "proof123" for method, payload in self.api.calls))
        self.send(1, callback=f"approve_ask:{order_id}")
        self.assertEqual(self.store.order(order_id, 2)["status"], "awaiting_confirmation")
        self.send(1, callback=f"approve:{order_id}")
        self.send(1, callback=f"complete:{order_id}")
        self.send(1, "/admin")
        self.assertEqual(self.store.order(order_id, 2)["status"], "completed")
        self.assertIn("Omzet terkonfirmasi: Rp20.000", self.texts(1))

    def test_reseller_application_dashboard_and_prices(self):
        self.send(2, callback="request_reseller")
        self.send(1, "/pengajuan")
        self.assertIn("Setujui: /setreseller 2", self.texts(1))
        self.send(1, "/setreseller 2")
        order_id = self.order()
        self.assertEqual(self.store.order(order_id, 2)["total"], 16000)
        self.proof(2, order_id)
        self.send(1, f"/konfirmasi {order_id}")
        self.send(2, "/reseller")
        self.assertIn("Penghematan harga reseller: Rp4.000", self.texts(2))

    def test_admin_commands_and_forged_callbacks_denied(self):
        order_id = self.order()
        self.proof(2, order_id)
        for command in ("/admin", "/laporan", "/produkadmin", "/pengajuan", "/audit",
                        "/setreseller 2", "/stok A 999", "/nonaktif A", f"/konfirmasi {order_id}"):
            self.send(2, command)
        for callback in ("admin", "products_admin:0", "admin_orders:all:0", "requests", "audit", f"approve:{order_id}"):
            self.send(2, callback=callback)
        self.assertEqual(self.store.product("A")["stock"], 8)
        self.assertEqual(self.store.product("A")["active"], 1)
        self.assertEqual(self.store.user(2)["role"], "customer")
        self.assertEqual(self.store.order(order_id, 2)["status"], "awaiting_confirmation")
        self.assertIn("hanya untuk admin", self.texts(2))

    def test_foreign_invoice_and_proof_are_private(self):
        order_id = self.order()
        self.proof(2, order_id)
        self.api.calls.clear()
        self.send(3, f"/pesanan {order_id}")
        self.send(3, callback=f"proof:{order_id}")
        self.proof(3, order_id)
        self.assertNotIn("Tujuan test", self.texts(3))
        self.assertIn("bukan milik Anda", self.texts(3))
        self.assertFalse(any(method in ("sendPhoto", "sendDocument") for method, _ in self.api.calls))

    def test_group_and_mismatched_sender_ignored(self):
        self.api.calls.clear()
        self.bot.handle({"update_id": 100, "message": {"from": {"id": 1},
                        "chat": {"id": -123, "type": "group"}, "text": "/admin"}})
        self.bot.handle({"update_id": 101, "message": {"from": {"id": 1},
                        "chat": {"id": 2, "type": "private"}, "text": "/admin"}})
        self.assertEqual(self.api.calls, [])

    def test_bad_inputs_do_not_create_orders(self):
        for command in ("/pesan", "/pesan A | -1 | tujuan", "/pesan A | 1.5 | tujuan",
                        "/pesan A | 0 | tujuan", "/pesan A | 1 |", "/pesanan abc"):
            self.send(2, command)
        for data in ("approve", "admin_orders:invalid:0", "catalog:-1", "catalog:999999999999999", "bad"):
            self.send(2, callback=data)
        self.assertEqual(self.store.orders(2), [])
        self.assertEqual(self.store.product("A")["stock"], 10)

    def test_proof_validation(self):
        order_id = self.order()
        for document in ({"file_id": "bad", "mime_type": "application/zip"},
                         {"file_id": "big", "mime_type": "application/pdf", "file_size": 11 * 1024 * 1024}):
            self.send(2, document=document, caption=f"/bukti {order_id}")
        self.assertEqual(self.store.order(order_id, 2)["status"], "pending_payment")
        self.send(2, document={"file_id": "pdf", "mime_type": "application/pdf"}, caption=f"/bukti {order_id}")
        self.assertEqual(self.store.order(order_id, 2)["proof_kind"], "document")

    def test_duplicate_update_does_not_reserve_twice(self):
        update = self.send(2, "/pesan A | 2 | Tujuan")
        self.bot.handle(update)
        self.assertEqual(len(self.store.orders(2)), 1)
        self.assertEqual(self.store.product("A")["stock"], 8)

    def test_notification_failure_does_not_rollback_payment(self):
        order_id = self.order()
        self.proof(2, order_id)
        self.api.blocked.add(2)
        with self.assertLogs("shopbot", level="WARNING"):
            self.send(1, f"/konfirmasi {order_id}")
        self.assertEqual(self.store.order(order_id, 1)["status"], "paid")

    def test_failed_admin_reply_does_not_skip_buyer_notification(self):
        order_id = self.order()
        self.proof(2, order_id)
        self.api.blocked.add(1)
        with self.assertLogs("shopbot", level="WARNING"):
            self.send(1, f"/tolak {order_id} Mutasi tidak ditemukan")
        self.assertEqual(self.store.order(order_id, 2)["status"], "pending_payment")
        self.assertIn("Alasan penolakan bukti: Mutasi tidak ditemukan", self.texts(2))

    def test_failed_proof_reply_does_not_skip_admin_notification(self):
        order_id = self.order()
        self.api.blocked.add(2)
        with self.assertLogs("shopbot", level="WARNING"):
            self.proof(2, order_id)
        self.assertIn(f"Bukti pembayaran baru untuk pesanan #{order_id}", self.texts(1))

    def test_cancellation_confirmation_then_restores_stock(self):
        order_id = self.order()
        self.send(2, callback=f"cancel_ask:{order_id}")
        self.assertEqual(self.store.product("A")["stock"], 8)
        self.send(2, callback=f"cancel:{order_id}")
        self.send(2, callback=f"cancel:{order_id}")
        self.assertEqual(self.store.product("A")["stock"], 10)

    def test_message_chunks_and_callback_size(self):
        self.bot.send(2, "😀" * 5000, {"inline_keyboard": []})
        for method, payload in self.api.calls:
            if method == "sendMessage":
                self.assertLessEqual(len(payload["text"].encode("utf-16-le")) // 2, 4096)
                for row in payload.get("reply_markup", {}).get("inline_keyboard", []):
                    for button in row:
                        self.assertLessEqual(len(button["callback_data"].encode()), 64)


class ConfigAndTransportTests(unittest.TestCase):
    def test_env_file_does_not_override_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SHOP_NAME": "Env"}, clear=True):
            path = Path(directory) / ".env"
            path.write_text('# comment\nSHOP_NAME="File"\nSUPPORT_CONTACT=\'@admin\'\n', encoding="utf-8")
            load_env(path)
            self.assertEqual(os.environ["SHOP_NAME"], "Env")
            self.assertEqual(os.environ["SUPPORT_CONTACT"], "@admin")

    def test_config_validation(self):
        env = {"TELEGRAM_BOT_TOKEN": "123:secret", "ADMIN_IDS": "1,2", "PAYMENT_INSTRUCTIONS": "Bank",
               "SUPPORT_CONTACT": "@a", "PAYMENT_PROVIDER": "manual"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(Config.from_env().admins, frozenset({1, 2}))
            for key, value in (("ADMIN_IDS", "0"), ("ADMIN_IDS", "@username"),
                               ("ORDER_TTL_HOURS", "0"), ("TELEGRAM_BOT_TOKEN", "placeholder"),
                               ("PAYMENT_INSTRUCTIONS", "")):
                with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                    with self.assertRaises(ValueError):
                        Config.from_env()

    def test_transport_errors_never_expose_token(self):
        api = Telegram("123:VERY_SECRET")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(api.base)):
            with self.assertRaises(TelegramError) as caught:
                api.call("getMe")
        self.assertNotIn("VERY_SECRET", str(caught.exception))

    def test_pakasir_is_default_and_requires_matching_project_credentials(self):
        env = {"TELEGRAM_BOT_TOKEN": "123:token-secret", "ADMIN_IDS": "1", "SUPPORT_CONTACT": "@admin",
               "PAKASIR_PROJECT": "toko-test", "PAKASIR_API_KEY": "gateway-secret"}
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
            self.assertEqual(config.provider, "pakasir")
            self.assertEqual(config.payment, "")
            self.assertNotIn("gateway-secret", repr(config))
            self.assertNotIn("token-secret", repr(config))
            for key, value in (("PAKASIR_PROJECT", ""), ("PAKASIR_PROJECT", "https://example.test"),
                               ("PAKASIR_API_KEY", ""), ("PAYMENT_PROVIDER", "other")):
                with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                    with self.assertRaises(ValueError):
                        Config.from_env()

    def test_admin_menu_failure_does_not_prevent_first_start(self):
        config = Config("123:token", frozenset({1}), "Test", "Bank", "@a", PG_DSN, 24)

        def call(method, **payload):
            if method == "getWebhookInfo":
                return {"url": ""}
            if method == "setMyCommands" and "scope" in payload:
                raise TelegramError(400)
            if method == "getUpdates":
                raise KeyboardInterrupt
            return True

        with patch("bot.load_env"), patch("bot.Config.from_env", return_value=config), \
                patch("bot.Telegram.call", side_effect=call), self.assertLogs("shopbot", level="INFO"):
            self.assertEqual(main(), 0)


if __name__ == "__main__":
    unittest.main()
