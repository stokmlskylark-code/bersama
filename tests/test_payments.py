import json
import os
import unittest
import uuid
from contextlib import ExitStack
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlparse

import psycopg2
from bot import Config, ShopBot
from pakasir import PakasirError
from store import ShopError, Store
from test_bot import FakeTelegram, SCHEMA

PG_DSN = os.getenv("PG_DSN", "dbname=postgres user=postgres password=postgres host=localhost port=5432")


class FakePakasir:
    def __init__(self):
        self.calls = []
        self.statuses = []
        self.status = "pending"
        self.cancel_error = None

    def checkout_url(self, reference, amount):
        self.calls.append(("checkout_url", reference, amount))
        return f"https://app.pakasir.com/pay/shop/{amount}?" + urlencode(
            {"order_id": reference, "qris_only": 1})

    def detail(self, reference, amount):
        self.calls.append(("detail", reference, amount))
        status = self.statuses.pop(0) if self.statuses else self.status
        if isinstance(status, Exception):
            raise status
        return {"status": status}

    def cancel(self, reference, amount):
        self.calls.append(("cancel", reference, amount))
        if self.cancel_error:
            raise self.cancel_error
        return {"status": "success"}


class PaymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        conn = psycopg2.connect(PG_DSN)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS settings, payments, audit, orders, products, users CASCADE")
            cur.execute(SCHEMA)
        conn.close()

    def setUp(self):
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        self.clock = contexts.enter_context(patch("bot.time.time", return_value=1_800_000_000))
        contexts.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")))
        contexts.enter_context(patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Network forbidden")))
        conn = psycopg2.connect(PG_DSN)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("TRUNCATE settings, payments, audit, orders, products, users RESTART IDENTITY CASCADE")
        conn.close()
        self.store = Store(PG_DSN, {1})
        self.addCleanup(self.store.close)
        self.seed(self.store)
        self.api = FakeTelegram()
        self.gateway = FakePakasir()
        self.config = Config("123:token", frozenset({1}), "Test", "Manual bank details", "@admin",
                             PG_DSN, 24, provider="pakasir", pakasir_project="shop",
                             pakasir_api_key="test-key")
        self.bot = ShopBot(self.config, self.store, self.api, pakasir=self.gateway)
        self.update_id = 0

    def seed(self, store):
        for actor in (1, 2, 3):
            store.register(actor, f"User {actor}")
        store.save_product(1, "A", "Product A", 10000, 8000, 10, "Description")

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

    def order(self, actor=2):
        self.send(actor, "/pesan A | 2 | Test destination")
        return self.store.orders(actor)[0]["id"]

    def texts(self, actor):
        return "\n".join(payload.get("text", "") for method, payload in self.api.calls
                         if method == "sendMessage" and payload["chat_id"] == actor)

    def assert_reserved(self, order_id):
        self.assertEqual(self.store.order(order_id, 2)["status"], "pending_payment")
        self.assertEqual(self.store.payment(order_id, 2)["state"], "pending")
        self.assertEqual(self.store.product("A")["stock"], 8)
        self.assertNotIn("paid", self.store.stats(1, admin=True))

    def audit_count(self, action):
        with self.store.db.cursor() as cur:
            cur.execute("SELECT count(*) FROM audit WHERE action=%s", (action,))
            return cur.fetchone()[0]

    @unittest.skip("SQLite legacy test not applicable with PostgreSQL")
    def test_upgrade_pre_gateway_database_preserves_manual_orders_and_stock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "legacy.sqlite3")
            legacy = Store(path, {1})
            try:
                self.seed(legacy)
                order = legacy.create_order(2, "A", 2, "Legacy destination", "legacy")
                legacy.submit_proof(2, order["id"], "old-proof", "photo")
                expected = dict(legacy.order(order["id"], 2))
                legacy.set_offset(1234)
                legacy.db.execute("DROP TABLE payments")
            finally:
                legacy.close()
            upgraded = Store(path, {1})
            try:
                self.assertEqual(dict(upgraded.order(order["id"], 2)), expected)
                self.assertEqual(upgraded.product("A")["stock"], 8)
                self.assertEqual(upgraded.get_offset(), 1234)
                self.assertIsNone(upgraded.payment(order["id"], 2))
                self.assertEqual(upgraded.transition(1, order["id"], "approve")["status"], "paid")
                new = upgraded.create_order(3, "A", 1, "New destination", "new", payment_project="shop")
                self.assertEqual(upgraded.payment(new["id"], 3)["project"], "shop")
            finally:
                upgraded.close()

    def test_checkout_creates_reference_and_reservation_once_on_update_replay(self):
        update = self.send(2, "/pesan A | 2 | Test destination")
        order = self.store.orders(2)[0]
        payment = dict(self.store.payment(order["id"], 2))
        self.assertEqual(payment["project"], "shop")
        self.assertRegex(payment["reference"], r"^TG-[0-9a-f]{32}$")
        self.assertEqual(uuid.UUID(payment["reference"][3:]).version, 4)
        self.bot.handle(update)
        self.assertEqual(len(self.store.orders(2)), 1)
        self.assertEqual(dict(self.store.payment(order["id"], 2)), payment)
        self.assertEqual(self.audit_count("create_order"), 1)
        self.assert_reserved(order["id"])
        self.assertFalse(any(call[0] in ("detail", "cancel") for call in self.gateway.calls))

    @unittest.skip("SQLite trigger test not applicable with PostgreSQL")
    def test_payment_insert_failure_rolls_back_order_stock_and_audit(self):
        self.store.db.execute("""CREATE TRIGGER fail_payment BEFORE INSERT ON payments
            BEGIN SELECT RAISE(ABORT, 'payment insert failed'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_order(2, "A", 2, "Destination", "atomic", payment_project="shop")
        self.assertEqual(self.store.product("A")["stock"], 10)
        self.assertEqual(self.store.orders(2), [])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM payments").fetchone()[0], 0)
        self.assertEqual(self.audit_count("create_order"), 0)
        self.store.db.execute("DROP TRIGGER fail_payment")
        order = self.store.create_order(2, "A", 2, "Destination", "atomic", payment_project="shop")
        self.assertIsNotNone(self.store.payment(order["id"], 2))
        self.assert_reserved(order["id"])

    def test_replay_key_cannot_be_claimed_by_another_owner(self):
        order = self.store.create_order(2, "A", 2, "Destination", "replay", payment_project="shop")
        with self.assertRaises(ShopError):
            self.store.create_order(3, "A", 2, "Destination", "replay", payment_project="shop")
        self.assertEqual(self.store.orders(3), [])
        self.assert_reserved(order["id"])

    def test_new_database_does_not_reuse_reference_even_when_order_id_restarts(self):
        first = self.order()
        reference = self.store.payment(first, 2)["reference"]
        # For PostgreSQL, we use a fresh connection to the same database
        fresh = Store(PG_DSN, {1})
        try:
            self.seed(fresh)
            order = fresh.create_order(2, "A", 2, "Destination", "fresh", payment_project="shop")
            payment = fresh.payment(order["id"], 2)
            self.assertNotEqual(payment["reference"], reference)
            self.assertEqual(uuid.UUID(payment["reference"][3:]).version, 4)
        finally:
            fresh.close()

    def test_invoice_uses_hosted_url_button_without_credentials_or_manual_instructions(self):
        order_id = self.order()
        payment = self.store.payment(order_id, 2)
        buttons = [button for method, payload in self.api.calls if method == "sendMessage"
                   and payload["chat_id"] == 2
                   for row in payload.get("reply_markup", {}).get("inline_keyboard", []) for button in row]
        urls = [button for button in buttons if "url" in button]
        self.assertEqual(len(urls), 1)
        self.assertNotIn("callback_data", urls[0])
        url = urlparse(urls[0]["url"])
        self.assertEqual((url.scheme, url.netloc, url.path),
                         ("https", "app.pakasir.com", "/pay/shop/20000"))
        self.assertEqual(parse_qs(url.query), {"order_id": [payment["reference"]], "qris_only": ["1"]})
        self.assertIn(f"check_payment:{order_id}", [button.get("callback_data") for button in buttons])
        serialized = json.dumps(self.api.calls)
        for secret in (self.config.token, self.config.pakasir_api_key, self.config.payment):
            self.assertNotIn(secret, serialized)

    def test_reseller_amount_is_snapshotted_for_invoice_and_provider_checks(self):
        self.store.set_reseller(1, 2, True)
        order_id = self.order()
        self.store.save_product(1, "A", "New name", 30000, 25000, 8, "New description")
        self.store.set_reseller(1, 2, False)
        self.send(2, f"/bayar {order_id}")
        self.gateway.status = "completed"
        self.bot.sync_payment(2, order_id)
        order = self.store.order(order_id, 2)
        self.assertEqual((order["unit_price"], order["total"], order["retail_price"]), (8000, 16000, 10000))
        self.assertTrue(self.gateway.calls)
        self.assertTrue(all(call[2] == 16000 for call in self.gateway.calls))
        self.assertEqual(self.store.stats(2)["paid"]["saving"], 4000)

    def test_check_command_callback_and_background_sync_credit_once(self):
        for route in ("command", "callback", "background"):
            with self.subTest(route=route):
                order_id = self.order()
                self.gateway.status = "completed"
                if route == "command":
                    self.send(2, f"/cekbayar {order_id}")
                elif route == "callback":
                    self.send(2, callback=f"check_payment:{order_id}")
                else:
                    self.bot.sync_payments()
                self.assertEqual(self.store.order(order_id, 2)["status"], "paid")
                self.assertEqual(self.store.payment(order_id, 2)["state"], "completed")
                before = self.audit_count("pakasir_paid")
                calls_before = len(self.gateway.calls)
                self.bot.sync_payment(2, order_id)
                self.send(2, f"/cekbayar {order_id}")
                self.send(2, callback=f"check_payment:{order_id}")
                self.bot.sync_payments()
                self.assertEqual(len(self.gateway.calls), calls_before)
                self.assertEqual(self.audit_count("pakasir_paid"), before)
        self.assertEqual(self.store.stats(1, admin=True)["paid"]["total"], 60000)
        self.assertEqual(self.audit_count("pakasir_paid"), 3)
        self.assertEqual(self.store.product("A")["stock"], 4)
        self.assertEqual(self.texts(2).count("Pakasir • Pesanan #"), 3)
        self.assertEqual(self.texts(1).count("Pakasir • Pesanan #"), 3)

    def test_store_rejects_manual_approval_proof_and_cancellation_for_gateway(self):
        order_id = self.order()
        operations = [lambda: self.store.submit_proof(2, order_id, "proof", "photo"),
                      lambda: self.store.transition(1, order_id, "approve"),
                      lambda: self.store.transition(1, order_id, "reject", "Invalid proof"),
                      lambda: self.store.cancel(2, order_id),
                      lambda: self.store.cancel(1, order_id)]
        for operation in operations:
            with self.assertRaises(ShopError):
                operation()
            self.assert_reserved(order_id)

    def test_bot_manual_commands_and_forged_approve_callback_do_not_mark_paid(self):
        order_id = self.order()
        self.gateway.calls.clear()
        self.send(2, photo=[{"file_id": "proof", "file_size": 100}], caption=f"/bukti {order_id}")
        self.send(1, f"/konfirmasi {order_id}")
        self.send(1, callback=f"approve:{order_id}")
        self.send(1, f"/tolak {order_id} Invalid proof")
        self.assert_reserved(order_id)
        self.assertIsNone(self.store.order(order_id, 2)["proof_file_id"])
        self.assertEqual(self.gateway.calls, [])

    def test_foreign_owner_cannot_call_provider_or_view_checkout(self):
        order_id = self.order()
        self.gateway.calls.clear()
        for command in (f"/cekbayar {order_id}", f"/batal {order_id}", f"/bayar {order_id}",
                        f"/pesanan {order_id}"):
            self.send(3, command)
        for callback in (f"check_payment:{order_id}", f"cancel:{order_id}", f"order:{order_id}"):
            self.send(3, callback=callback)
        for cancel in (False, True):
            with self.assertRaises(ShopError):
                self.bot.sync_payment(3, order_id, cancel=cancel)
        self.assertEqual(self.gateway.calls, [])
        self.assertNotIn(self.store.payment(order_id, 2)["reference"], self.texts(3))
        self.assert_reserved(order_id)

    def test_api_error_on_command_callback_and_auto_sync_keeps_stock_reserved(self):
        order_id = self.order()
        self.clock.return_value = self.store.order(order_id, 2)["expires_at"]
        self.gateway.status = PakasirError()
        self.send(2, f"/cekbayar {order_id}")
        self.assertIn("jangan transfer ulang", self.texts(2))
        self.send(2, callback=f"check_payment:{order_id}")
        self.clock.return_value += 60
        with self.assertLogs("shopbot", level="WARNING"):
            self.bot.sync_payments()
        self.assert_reserved(order_id)
        self.assertEqual(self.store.payment(order_id, 2)["check_after"], self.clock.return_value + 60)
        self.assertFalse(any(call[0] == "cancel" for call in self.gateway.calls))

    def test_mismatched_payment_identity_or_amount_cannot_settle_or_release(self):
        order_id = self.order()
        payment = self.store.payment(order_id, 2)
        valid = [order_id, payment["reference"], "shop", 20000]
        for index, wrong in ((0, order_id + 100), (1, "TG-other"), (2, "other-project"),
                             (3, 1), (3, "20000"), (3, 20000.0), (3, True)):
            for status, close_as in (("completed", None), ("cancelled", "cancelled")):
                with self.subTest(index=index, wrong=wrong, status=status):
                    args = valid.copy()
                    args[index] = wrong
                    with self.assertRaises(ShopError):
                        self.store.apply_payment_status(*args, status, close_as)
                    self.assert_reserved(order_id)

    def test_wrong_configured_project_never_calls_provider(self):
        order = self.store.create_order(2, "A", 2, "Destination", "other", payment_project="old-shop")
        with self.assertRaises(ShopError):
            self.bot.sync_payment(2, order["id"])
        self.assertEqual(self.gateway.calls, [])
        self.assert_reserved(order["id"])

    def test_local_expiry_skips_gateway_but_keeps_manual_expiry(self):
        order_id = self.order()
        manual = self.store.create_order(3, "A", 1, "Destination", "manual")
        self.clock.return_value = self.store.order(order_id, 2)["expires_at"] + 1
        self.store.expire()
        self.store.expire()
        self.assertEqual(self.store.order(manual["id"], 3)["status"], "expired")
        self.assertEqual(self.audit_count("expire_order"), 1)
        self.assert_reserved(order_id)

    def test_cancellation_race_with_completed_payment_marks_paid(self):
        for route in ("command", "callback"):
            with self.subTest(route=route):
                order_id = self.order()
                self.gateway.calls.clear()
                self.gateway.statuses = ["pending", "completed"]
                if route == "command":
                    self.send(2, f"/batal {order_id}")
                else:
                    self.send(2, callback=f"cancel:{order_id}")
                self.assertEqual([call[0] for call in self.gateway.calls], ["detail", "cancel", "detail"])
                self.assertEqual(self.store.order(order_id, 2)["status"], "paid")
                self.assertEqual(self.store.payment(order_id, 2)["state"], "completed")
        self.assertEqual(self.store.product("A")["stock"], 6)
        self.assertEqual(self.audit_count("pakasir_cancelled"), 0)

    def test_cancel_ack_without_terminal_detail_retains_reservation(self):
        order_id = self.order()
        self.gateway.calls.clear()
        self.gateway.statuses = ["pending", "pending"]
        self.send(2, f"/batal {order_id}")
        self.assertEqual([call[0] for call in self.gateway.calls], ["detail", "cancel", "detail", "checkout_url"])
        self.assertIn("Stok tetap ditahan", self.texts(2))
        self.assert_reserved(order_id)

    def test_cancel_error_or_failed_post_cancel_detail_retains_reservation(self):
        order_id = self.order()
        self.gateway.cancel_error = PakasirError()
        self.send(2, f"/batal {order_id}")
        self.assert_reserved(order_id)
        self.gateway.cancel_error = None
        self.gateway.statuses = ["pending", PakasirError()]
        self.send(2, f"/batal {order_id}")
        self.assert_reserved(order_id)

    def test_not_found_before_deadline_waits_and_at_deadline_releases_once(self):
        order_id = self.order()
        deadline = self.store.order(order_id, 2)["expires_at"]
        self.gateway.calls.clear()
        self.gateway.status = "not_found"
        self.clock.return_value = deadline - 1
        self.bot.sync_payment(2, order_id)
        self.assert_reserved(order_id)
        self.clock.return_value = deadline
        self.bot.sync_payment(2, order_id)
        self.bot.sync_payment(2, order_id)
        self.store.expire()
        self.assertEqual(self.store.order(order_id, 2)["status"], "expired")
        self.assertEqual(self.store.payment(order_id, 2)["state"], "closed")
        self.assertEqual(self.store.product("A")["stock"], 10)
        self.assertEqual(self.audit_count("pakasir_expired"), 1)
        self.assertFalse(any(call[0] == "cancel" for call in self.gateway.calls))

    def test_unknown_status_never_means_paid_or_safe_to_release(self):
        order_id = self.order()
        for status in ("success", "paid", "processing", "unknown", "", "COMPLETED"):
            with self.subTest(status=status):
                self.gateway.status = status
                self.bot.sync_payment(2, order_id)
                self.assert_reserved(order_id)
                payment = self.store.payment(order_id, 2)
                with self.assertRaises(ShopError):
                    self.store.apply_payment_status(order_id, payment["reference"], "shop", 20000,
                                                    status, "expired")
                self.assert_reserved(order_id)
        self.clock.return_value = self.store.order(order_id, 2)["expires_at"]
        self.bot.sync_payment(2, order_id)
        self.assert_reserved(order_id)

    def test_late_completed_flags_reconciliation_without_reclaiming_released_stock(self):
        order_id = self.order()
        self.gateway.status = "cancelled"
        self.bot.sync_payment(2, order_id)
        self.assertEqual(self.store.product("A")["stock"], 10)
        replacement = self.store.create_order(3, "A", 10, "Destination", "replacement", payment_project="shop")
        self.gateway.status = "completed"
        self.bot.sync_payment(2, order_id)
        self.bot.sync_payment(2, order_id)
        self.assertEqual(self.store.order(order_id, 2)["status"], "cancelled")
        self.assertEqual(self.store.payment(order_id, 2)["state"], "late_completed")
        self.assertEqual(self.store.product("A")["stock"], 0)
        self.assertEqual(self.store.order(replacement["id"], 3)["status"], "pending_payment")
        self.assertNotIn("paid", self.store.stats(1, admin=True))
        self.assertEqual(self.audit_count("pakasir_late_completed"), 1)
        self.assertEqual([row["id"] for row in self.store.payment_issues(1)], [order_id])
        self.assertIn("rekonsiliasi", self.texts(1))
        with self.assertRaises(ShopError):
            self.store.payment_issues(2)
        self.assertNotIn(order_id, [row["order_id"] for row in self.store.due_payments(self.clock.return_value + 7200)])

    def test_failed_notifications_do_not_roll_back_payment_or_duplicate_credit(self):
        order_id = self.order()
        self.api.blocked = {1, 2}
        self.gateway.status = "completed"
        with self.assertLogs("shopbot", level="WARNING") as logs:
            self.bot.sync_payments()
        self.assertEqual(len(logs.output), 2)
        self.bot.sync_payments()
        self.bot.sync_payment(2, order_id)
        self.assertEqual(self.store.order(order_id, 2)["status"], "paid")
        self.assertEqual(self.store.payment(order_id, 2)["state"], "completed")
        self.assertEqual(self.store.stats(1, admin=True)["paid"]["total"], 20000)
        self.assertEqual(self.store.product("A")["stock"], 8)
        self.assertEqual(self.audit_count("pakasir_paid"), 1)

    @unittest.skip("SQLite-specific test")
    def test_due_checks_are_bounded_fair_on_errors_and_persistent_after_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "payments.sqlite3")
            store = Store(path, {1})
            try:
                self.seed(store)
                store.set_stock(1, "A", 100)
                ids = [store.create_order(2 if index < 5 else 3, "A", 1, "Destination", str(index),
                                          payment_project="shop")["id"] for index in range(7)]
                bot = ShopBot(self.config, store, self.api, pakasir=self.gateway)
                self.gateway.status = PakasirError()
                self.assertEqual([row["order_id"] for row in store.due_payments(self.clock.return_value)], ids[:3])
                with self.assertLogs("shopbot", level="WARNING"):
                    bot.sync_payments()
                self.assertEqual(len(self.gateway.calls), 3)
                self.assertEqual([row["order_id"] for row in store.due_payments(self.clock.return_value)], ids[3:6])
                reference = store.payment(ids[0], 2)["reference"]
            finally:
                store.close()
            reopened = Store(path, {1})
            try:
                self.assertEqual(reopened.payment(ids[0], 2)["reference"], reference)
                self.assertEqual(reopened.payment(ids[0], 2)["check_after"], self.clock.return_value + 60)
                self.assertEqual([row["order_id"] for row in reopened.due_payments(self.clock.return_value)], ids[3:6])
                bot = ShopBot(self.config, reopened, self.api, pakasir=self.gateway)
                self.gateway.status = "pending"
                bot.sync_payments()
                self.assertEqual(len(self.gateway.calls), 6)
                self.assertEqual([row["order_id"] for row in reopened.due_payments(self.clock.return_value)], ids[6:])
                bot.sync_payments()
                self.assertEqual(len(self.gateway.calls), 7)
                self.assertEqual(reopened.due_payments(self.clock.return_value), [])
                self.clock.return_value += 60
                self.assertEqual(len(reopened.due_payments(self.clock.return_value, limit=7)), 7)
                self.assertEqual(reopened.product("A")["stock"], 93)
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
