"""Penyimpanan toko; uang dalam rupiah bulat, stok direservasi saat memesan."""

import os
import time
import uuid
from contextlib import contextmanager

import psycopg2
import psycopg2.extras


class ShopError(Exception):
    pass


class Store:
    def __init__(self, dsn=None, admin_ids=None, ttl_hours=24):
        dsn = dsn or os.getenv("PG_DSN", "dbname=postgres user=postgres password=postgres host=localhost port=5432")
        self.db = psycopg2.connect(dsn)
        self.db.autocommit = False
        self.ttl = ttl_hours * 3600
        self.admin_ids = set(admin_ids or [])

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _audit(self, actor, action, detail):
        with self.db.cursor() as cur:
            cur.execute("INSERT INTO audit(actor_id, action, detail, created_at) VALUES (%s, %s, %s, %s)",
                        (actor, action, detail, int(time.time())))

    def register(self, user_id, name, username=""):
        with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""INSERT INTO users(id, name, username, role, created_at)
                VALUES (%s, %s, %s, %s, %s) ON CONFLICT(id) DO UPDATE SET
                name=EXCLUDED.name, username=EXCLUDED.username""",
                        (user_id, name[:150], username[:100],
                         "admin" if user_id in self.admin_ids else "customer", int(time.time())))
            if user_id in self.admin_ids:
                cur.execute("UPDATE users SET role='admin' WHERE id=%s", (user_id,))
            else:
                cur.execute("UPDATE users SET role='customer' WHERE id=%s AND role='admin'", (user_id,))
            return self.user(user_id)

    def user(self, user_id):
        with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
            row = cur.fetchone()
        if not row:
            raise ShopError("Pengguna belum menjalankan /start.")
        return row

    def require_admin(self, user_id):
        if user_id not in self.admin_ids:
            raise ShopError("Perintah ini hanya untuk admin.")

    def user_stats(self, user_id):
        with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT count(*) AS completed_orders,
                       coalesce(sum(total), 0) AS total_spending
                FROM orders WHERE user_id=%s AND status IN ('paid', 'shipped', 'completed')
            """, (user_id,))
            return dict(cur.fetchone())

    def request_reseller(self, user_id):
        with self.transaction():
            user = self.user(user_id)
            if user["role"] != "customer":
                raise ShopError("Akun Anda sudah memiliki akses reseller/admin.")
            if user["reseller_request"]:
                raise ShopError("Pengajuan sebelumnya masih menunggu admin.")
            with self.db.cursor() as cur:
                cur.execute("UPDATE users SET reseller_request=1 WHERE id=%s", (user_id,))
            self._audit(user_id, "request_reseller", str(user_id))

    def set_reseller(self, actor, user_id, enabled):
        self.require_admin(actor)
        with self.transaction():
            self.user(user_id)
            if user_id in self.admin_ids:
                raise ShopError("Hak admin diatur melalui ADMIN_IDS, bukan perintah reseller.")
            with self.db.cursor() as cur:
                cur.execute("UPDATE users SET role=%s, reseller_request=0 WHERE id=%s",
                            ("reseller" if enabled else "customer", user_id))
            self._audit(actor, "set_reseller", f"{user_id}: {enabled}")

    def reseller_requests(self, actor):
        self.require_admin(actor)
        with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE reseller_request=1 ORDER BY id LIMIT 30")
            return cur.fetchall()

    def save_product(self, actor, sku, name, price, reseller_price, stock, description):
        self.require_admin(actor)
        sku = sku.upper()
        if not (1 <= len(sku) <= 30 and sku.isascii() and
                all(c.isalnum() or c in "_-" for c in sku)):
            raise ShopError("SKU harus 1–30 karakter: huruf, angka, garis bawah atau tanda minus.")
        if not name.strip() or len(name) > 120 or len(description) > 1000:
            raise ShopError("Nama wajib diisi (maks. 120 karakter), deskripsi maks. 1000 karakter.")
        if not all(type(value) is int for value in (price, reseller_price, stock)):
            raise ShopError("Harga dan stok harus bilangan bulat.")
        if not (0 < reseller_price <= price <= 1_000_000_000 and 0 <= stock <= 1_000_000):
            raise ShopError("Harga harus 1–1.000.000.000, harga reseller ≤ harga normal, stok 0–1.000.000.")
        with self.transaction():
            with self.db.cursor() as cur:
                cur.execute("""INSERT INTO products
                    (sku, name, price, reseller_price, stock, description) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT(sku) DO UPDATE SET name=EXCLUDED.name, price=EXCLUDED.price,
                    reseller_price=EXCLUDED.reseller_price, stock=EXCLUDED.stock,
                    description=EXCLUDED.description, active=1, seller_id=NULL""",
                    (sku, name, price, reseller_price, stock, description))
            self._audit(actor, "save_product", sku)

    def save_reseller_product(self, actor, sku, name, price, stock, description, category=""):
        user = self.user(actor)
        if user["role"] != "reseller":
            raise ShopError("Hanya reseller yang dapat menambahkan produk.")
        sku = sku.upper()
        if not (1 <= len(sku) <= 30 and sku.isascii() and
                all(c.isalnum() or c in "_-" for c in sku)):
            raise ShopError("SKU harus 1–30 karakter: huruf, angka, garis bawah atau tanda minus.")
        if not name.strip() or len(name) > 120 or len(description) > 1000:
            raise ShopError("Nama wajib diisi (maks. 120 karakter), deskripsi maks. 1000 karakter.")
        if not all(type(value) is int for value in (price, stock)):
            raise ShopError("Harga dan stok harus bilangan bulat.")
        if not (0 < price <= 1_000_000_000 and 0 <= stock <= 1_000_000):
            raise ShopError("Harga harus 1–1.000.000.000, stok 0–1.000.000.")
        with self.transaction():
            with self.db.cursor() as cur:
                cur.execute("SELECT seller_id FROM products WHERE sku=%s", (sku,))
                existing = cur.fetchone()
                if existing and existing[0] != actor:
                    raise ShopError("SKU sudah dimiliki seller lain.")
                cur.execute("""INSERT INTO products
                    (sku, name, category, price, reseller_price, stock, description, seller_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(sku) DO UPDATE SET name=EXCLUDED.name, category=EXCLUDED.category,
                    price=EXCLUDED.price, reseller_price=EXCLUDED.price, stock=EXCLUDED.stock,
                    description=EXCLUDED.description, seller_id=EXCLUDED.seller_id""",
                    (sku, name, category, price, price, stock, description, actor))
            self._audit(actor, "save_reseller_product", sku)

    def delete_product(self, actor, sku):
        user = self.user(actor)
        if user["role"] != "reseller":
            raise ShopError("Hanya reseller yang dapat menghapus produk.")
        with self.transaction():
            with self.db.cursor() as cur:
                cur.execute("SELECT * FROM products WHERE sku=%s AND seller_id=%s", (sku.upper(), actor))
                product = cur.fetchone()
                if not product:
                    raise ShopError("Produk tidak ditemukan atau bukan milik Anda.")
                cur.execute("SELECT count(*) FROM orders WHERE sku=%s AND status IN ('pending_payment', 'awaiting_confirmation')",
                            (sku.upper(),))
                pending = cur.fetchone()[0]
                if pending:
                    raise ShopError("Tidak dapat menghapus produk yang sedang dalam pesanan aktif.")
                cur.execute("DELETE FROM products WHERE sku=%s AND seller_id=%s", (sku.upper(), actor))
            self._audit(actor, "delete_product", sku.upper())

    def my_products(self, seller_id, page=0, query=""):
        with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT * FROM products WHERE seller_id=%s
                AND (lower(name) LIKE lower(%s) OR lower(sku) LIKE lower(%s))
                ORDER BY sku LIMIT 8 OFFSET %s""", (seller_id, f"%{query}%", f"%{query}%", page * 8))
            return cur.fetchall()

    def set_stock(self, actor, sku, stock):
        self.require_admin(actor)
        if type(stock) is not int or not 0 <= stock <= 1_000_000:
            raise ShopError("Stok harus 0–1.000.000.")
        with self.transaction():
            self.product(sku)
            with self.db.cursor() as cur:
                cur.execute("UPDATE products SET stock=%s WHERE sku=%s", (stock, sku.upper()))
            self._audit(actor, "set_stock", f"{sku.upper()}: {stock}")

    def set_active(self, actor, sku, active):
        self.require_admin(actor)
        with self.transaction():
            self.product(sku)
            with self.db.cursor() as cur:
                cur.execute("UPDATE products SET active=%s WHERE sku=%s", (int(active), sku.upper()))
            self._audit(actor, "set_active", f"{sku.upper()}: {active}")

    def product(self, sku):
        with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM products WHERE sku=%s", (sku.upper(),))
            row = cur.fetchone()
        if not row:
            raise ShopError("Produk tidak ditemukan.")
        return row

    def catalog(self, page=0, query="", include_hidden=False, category=""):
        with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if category:
                cur.execute("""SELECT * FROM products WHERE (active=1 OR %s)
                    AND category=%s
                    AND (lower(name) LIKE lower(%s) OR lower(sku) LIKE lower(%s))
                    ORDER BY sku LIMIT 8 OFFSET %s""", (bool(include_hidden), category, f"%{query}%", f"%{query}%", page * 8))
            else:
                cur.execute("""SELECT * FROM products WHERE (active=1 OR %s)
                    AND (lower(name) LIKE lower(%s) OR lower(sku) LIKE lower(%s))
                    ORDER BY sku LIMIT 8 OFFSET %s""", (bool(include_hidden), f"%{query}%", f"%{query}%", page * 8))
            return cur.fetchall()

    def _expire(self, now):
        with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT id, sku, qty FROM orders WHERE status='pending_payment' AND expires_at<=%s
                AND NOT EXISTS (SELECT 1 FROM payments WHERE payments.order_id=orders.id)""", (now,))
            rows = cur.fetchall()
            for row in rows:
                cur.execute("UPDATE products SET stock=stock+%s WHERE sku=%s", (row["qty"], row["sku"]))
                cur.execute("UPDATE orders SET status='expired', updated_at=%s WHERE id=%s", (now, row["id"]))
            if rows:
                self._audit(0, "expire_order", str([r["id"] for r in rows]))

    def expire(self):
        with self.transaction():
            self._expire(int(time.time()))

    def create_order(self, user_id, sku, qty, note, request_key, payment_project=None):
        if type(qty) is not int or not 1 <= qty <= 1000:
            raise ShopError("Jumlah pembelian harus 1–1000.")
        if not note.strip() or len(note) > 1000:
            raise ShopError("Isi catatan pembelian, maks. 1000 karakter.")
        now = int(time.time())
        with self.transaction():
            self._expire(now)
            with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM orders WHERE request_key=%s", (request_key,))
                existing = cur.fetchone()
                if existing:
                    if existing["user_id"] != user_id:
                        raise ShopError("Identitas permintaan tidak cocok.")
                    return existing
                user = self.user(user_id)
                product = self.product(sku)
                if not product["active"]:
                    raise ShopError("Produk sedang dinonaktifkan.")
                cur.execute("""SELECT count(*) FROM orders WHERE user_id=%s
                    AND status IN ('pending_payment', 'awaiting_confirmation')""", (user_id,))
                pending = cur.fetchone()["count"]
                if pending >= 5:
                    raise ShopError("Maksimal 5 pesanan belum dibayar/dikonfirmasi. Selesaikan atau batalkan dahulu.")
                cur.execute("UPDATE products SET stock=stock-%s WHERE sku=%s AND stock>=%s",
                            (qty, product["sku"], qty))
                if cur.rowcount == 0:
                    raise ShopError("Stok tidak cukup.")
                reseller = user["role"] == "reseller"
                price = product["reseller_price"] if reseller else product["price"]
                cur.execute("""INSERT INTO orders(request_key, user_id, sku, product_name,
                    qty, unit_price, retail_price, total, reseller, note, created_at, expires_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id""",
                    (request_key, user_id, product["sku"], product["name"], qty,
                     price, product["price"], price * qty, 1 if reseller else 0, note, now, now + self.ttl, now))
                order_id = cur.fetchone()["id"]
                self._audit(user_id, "create_order", str(order_id))
                if payment_project:
                    cur.execute("INSERT INTO payments(order_id, reference, project) VALUES (%s, %s, %s)",
                                (order_id, "TG-" + uuid.uuid4().hex, payment_project))
                return self.order(order_id, user_id)

    def order(self, order_id, actor):
        with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
            row = cur.fetchone()
        if not row or (row["user_id"] != actor and actor not in self.admin_ids):
            raise ShopError("Pesanan tidak ditemukan atau bukan milik Anda.")
        return row

    def orders(self, actor, page=0, admin=False, status=None):
        if admin:
            self.require_admin(actor)
        with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT * FROM orders WHERE (user_id=%s OR %s)
                AND (%s IS NULL OR status=%s) ORDER BY id DESC LIMIT 8 OFFSET %s""",
                        (actor, bool(admin), status, status, page * 8))
            return cur.fetchall()

    def submit_proof(self, actor, order_id, file_id, kind):
        if kind not in ("photo", "document") or not file_id:
            raise ShopError("Bukti harus berupa foto atau dokumen.")
        self.expire()
        with self.transaction():
            row = self.order(order_id, actor)
            if self.payment(order_id, actor):
                raise ShopError("Pesanan Pakasir diverifikasi otomatis. Gunakan /cekbayar ID, bukan bukti transfer.")
            if row["user_id"] != actor:
                raise ShopError("Hanya pemilik pesanan yang dapat mengirim bukti.")
            if row["status"] not in ("pending_payment", "awaiting_confirmation"):
                raise ShopError("Pesanan ini tidak menerima bukti pembayaran lagi.")
            with self.db.cursor() as cur:
                cur.execute("""UPDATE orders SET status='awaiting_confirmation', proof_file_id=%s,
                    proof_kind=%s, updated_at=%s WHERE id=%s""", (file_id, kind, int(time.time()), order_id))
            self._audit(actor, "submit_proof", str(order_id))

    def cancel(self, actor, order_id):
        with self.transaction():
            row = self.order(order_id, actor)
            if self.payment(order_id, actor):
                raise ShopError("Pembatalan Pakasir harus diperiksa melalui gateway terlebih dahulu.")
            if row["status"] != "pending_payment":
                raise ShopError("Hanya pesanan menunggu pembayaran yang dapat dibatalkan. Hubungi admin untuk status lain.")
            with self.db.cursor() as cur:
                cur.execute("UPDATE orders SET status='cancelled', updated_at=%s WHERE id=%s",
                            (int(time.time()), order_id))
                cur.execute("UPDATE products SET stock=stock+%s WHERE sku=%s", (row["qty"], row["sku"]))
            self._audit(actor, "cancel_order", str(order_id))

    def transition(self, actor, order_id, action, detail=""):
        self.require_admin(actor)
        transitions = {"approve": ("awaiting_confirmation", "paid"),
                       "reject": ("awaiting_confirmation", "pending_payment"),
                       "complete": ("paid", "completed")}
        if action not in transitions:
            raise ShopError("Aksi pesanan tidak dikenal.")
        if action == "reject" and (not detail.strip() or len(detail) > 500):
            raise ShopError("Isi alasan penolakan (maks. 500 karakter).")
        with self.transaction():
            row = self.order(order_id, actor)
            if action in ("approve", "reject") and self.payment(order_id, actor):
                raise ShopError("Pembayaran Pakasir hanya boleh dikonfirmasi oleh API Pakasir.")
            before, after = transitions[action]
            if row["status"] != before:
                raise ShopError(f"Status tidak cocok untuk aksi ini: {row['status']}.")
            now = int(time.time())
            with self.db.cursor() as cur:
                cur.execute("""UPDATE orders SET status=%s, updated_at=%s, expires_at=%s,
                    proof_file_id=%s, proof_kind=%s WHERE id=%s""",
                            (after, now,
                             now + self.ttl if action == "reject" else row["expires_at"],
                             None if action == "reject" else row["proof_file_id"],
                             None if action == "reject" else row["proof_kind"], order_id))
            self._audit(actor, action, f"{order_id}: {detail}")
            return self.order(order_id, actor)

    def stats(self, actor, admin=False):
        if admin:
            self.require_admin(actor)
        with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT status, count(*) AS count, coalesce(sum(total), 0) AS total,
                coalesce(sum(CASE WHEN reseller=1 THEN (retail_price-unit_price)*qty ELSE 0 END), 0) AS saving
                FROM orders WHERE (user_id=%s OR %s) GROUP BY status""", (actor, bool(admin)))
            return {row["status"]: dict(row) for row in cur.fetchall()}

    def payment(self, order_id, actor):
        self.order(order_id, actor)
        with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM payments WHERE order_id=%s", (order_id,))
            return cur.fetchone()

    def due_payments(self, now, limit=3):
        with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT p.*, o.user_id, o.total, o.expires_at FROM payments p
                JOIN orders o ON o.id=p.order_id WHERE p.state IN ('pending', 'closed') AND p.check_after<=%s
                ORDER BY p.check_after, p.order_id LIMIT %s""", (now, limit))
            return cur.fetchall()

    def schedule_payment_check(self, order_id, after):
        with self.db.cursor() as cur:
            cur.execute("UPDATE payments SET check_after=%s WHERE order_id=%s", (after, order_id))

    def apply_payment_status(self, order_id, reference, project, amount, provider_status, close_as=None):
        if close_as not in (None, "cancelled", "expired"):
            raise ShopError("Status penutupan pembayaran tidak valid.")
        if close_as and provider_status not in ("not_found", "canceled", "cancelled", "expired"):
            raise ShopError("Gateway belum memastikan transaksi aman untuk ditutup.")
        now = int(time.time())
        with self.transaction():
            with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM payments WHERE order_id=%s", (order_id,))
                payment = cur.fetchone()
                cur.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
                order = cur.fetchone()
                if (not payment or not order or payment["reference"] != reference or payment["project"] != project
                        or type(amount) is not int or order["total"] != amount):
                    raise ShopError("Identitas/nominal pembayaran tidak cocok dengan invoice.")
                if payment["state"] in ("completed", "late_completed"):
                    return None
                event = None
                state = payment["state"]
                if provider_status == "completed":
                    if order["status"] == "pending_payment":
                        cur.execute("UPDATE orders SET status='paid', updated_at=%s WHERE id=%s", (now, order_id))
                        state, event = "completed", "paid"
                    else:
                        state, event = "late_completed", "late_completed"
                elif close_as and order["status"] == "pending_payment":
                    cur.execute("UPDATE orders SET status=%s, updated_at=%s WHERE id=%s", (close_as, now, order_id))
                    cur.execute("UPDATE products SET stock=stock+%s WHERE sku=%s", (order["qty"], order["sku"]))
                    state, event = "closed", close_as
                cur.execute("""UPDATE payments SET state=%s, provider_status=%s, checked_at=%s, check_after=%s
                    WHERE order_id=%s""", (state, provider_status, now, now + (3600 if state == "closed" else 60), order_id))
                if event:
                    self._audit(0, "pakasir_" + event, str(order_id))
                return event

    def payment_issues(self, actor):
        self.require_admin(actor)
        with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT o.id, o.user_id, o.total, p.reference FROM payments p
                JOIN orders o ON o.id=p.order_id WHERE p.state='late_completed' ORDER BY o.id DESC LIMIT 30""")
            return cur.fetchall()

    def admin_counts(self, actor):
        self.require_admin(actor)
        with self.db.cursor() as cur:
            cur.execute("SELECT count(*) FROM users")
            users = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM users WHERE role='reseller'")
            resellers = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM products WHERE active=1")
            products = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM products WHERE active=1 AND stock<=5")
            low_stock = cur.fetchone()[0]
        return {"users": users, "resellers": resellers, "products": products, "low_stock": low_stock}

    def audit(self, actor):
        self.require_admin(actor)
        with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 15")
            return cur.fetchall()

    def get_offset(self):
        with self.db.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key='telegram_offset'")
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def set_offset(self, offset):
        with self.db.cursor() as cur:
            cur.execute("INSERT INTO settings(key, value) VALUES ('telegram_offset', %s) "
                        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (str(offset),))

    def create_ticket(self, user_id, subject, message):
        now = int(time.time())
        with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""INSERT INTO tickets (user_id, subject, message, status, created_at, updated_at)
                VALUES (%s, %s, %s, 'open', %s, %s) RETURNING id""",
                        (user_id, subject[:100], message[:2000], now, now))
            ticket_id = cur.fetchone()["id"]
        self._audit(user_id, "create_ticket", f"#{ticket_id}: {subject[:50]}")
        return ticket_id

    def reply_ticket(self, admin_id, ticket_id, reply):
        now = int(time.time())
        self.require_admin(admin_id)
        with self.transaction():
            with self.db.cursor() as cur:
                cur.execute("SELECT * FROM tickets WHERE id=%s", (ticket_id,))
                ticket = cur.fetchone()
                if not ticket:
                    raise ShopError("Tiket tidak ditemukan.")
                cur.execute("UPDATE tickets SET status='replied', admin_reply=%s, updated_at=%s WHERE id=%s",
                            (reply[:2000], now, ticket_id))
        self._audit(admin_id, "reply_ticket", f"#{ticket_id}")
        return ticket

    def close_ticket(self, admin_id, ticket_id):
        now = int(time.time())
        self.require_admin(admin_id)
        with self.db.cursor() as cur:
            cur.execute("UPDATE tickets SET status='closed', updated_at=%s WHERE id=%s AND status != 'closed'", (now, ticket_id))
        self._audit(admin_id, "close_ticket", f"#{ticket_id}")

    def user_tickets(self, user_id, limit=5):
        with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM tickets WHERE user_id=%s ORDER BY id DESC LIMIT %s", (user_id, limit))
            return cur.fetchall()

    def open_tickets(self, limit=20):
        with self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT t.*, u.name, u.username FROM tickets t
                JOIN users u ON t.user_id = u.id
                WHERE t.status IN ('open', 'replied')
                ORDER BY t.id DESC LIMIT %s""", (limit,))
            return cur.fetchall()
