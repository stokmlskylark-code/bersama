"""Web server untuk Telegram Mini Apps — reseller product management."""

import json
import logging
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import psycopg2
import psycopg2.extras

from telegram_auth import authenticate_init_data

LOG = logging.getLogger("webapp")
PG_DSN = os.getenv("PG_DSN", "dbname=postgres user=postgres password=postgres host=localhost port=5432")
HOST = os.getenv("WEB_HOST", "0.0.0.0")
PORT = int(os.getenv("SERVER_PORT", os.getenv("WEB_PORT", "8080")))
MAX_REQUEST_BODY = 64 * 1024

TEMPLATE_DIR = Path(__file__).parent / "templates"


def get_db():
    db = psycopg2.connect(PG_DSN)
    db.autocommit = False
    return db


def _audit(db, actor, action, detail):
    with db.cursor() as cur:
        cur.execute("INSERT INTO audit(actor_id, action, detail, created_at) VALUES (%s, %s, %s, %s)",
                    (actor, action, detail, int(time.time())))


def verify_user(user_id):
    db = get_db()
    try:
        with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
            user = cur.fetchone()
        if not user:
            return None, "User belum terdaftar di bot."
        if user["role"] != "reseller":
            return None, "Hanya reseller yang dapat mengakses."
        return dict(user), None
    finally:
        db.close()


def register_reseller(user_id, shop_name, description, alamat_lengkap, domisili):
    db = get_db()
    try:
        with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
            user = cur.fetchone()
            if not user:
                return False, "User belum terdaftar di bot."
            if user["role"] == "reseller":
                return False, "Anda sudah menjadi reseller."
            if user["reseller_request"]:
                return False, "Pengajuan sebelumnya masih menunggu admin."
            cur.execute("UPDATE users SET reseller_request=1, alamat_lengkap=%s, domisili=%s WHERE id=%s",
                        (alamat_lengkap, domisili, user_id))
            _audit(db, user_id, "request_reseller_web", f"{shop_name}: {description} | {domisili}")
        db.commit()
        return True, "Berhasil"
    except Exception as exc:
        LOG.error("Register error: %s", exc)
        db.rollback()
        return False, "Terjadi kesalahan."
    finally:
        db.close()


def get_my_products(user_id, page=0, query=""):
    db = get_db()
    try:
        with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT * FROM products WHERE seller_id=%s
                AND (lower(name) LIKE lower(%s) OR lower(sku) LIKE lower(%s))
                ORDER BY sku LIMIT 8 OFFSET %s""",
                        (user_id, f"%{query}%", f"%{query}%", page * 8))
            return [dict(r) for r in cur.fetchall()]
    finally:
        db.close()


def add_product(user_id, sku, name, price, stock, description, category=""):
    db = get_db()
    try:
        sku = sku.upper().strip()
        category = category.strip() if isinstance(category, str) else None
        if not (1 <= len(sku) <= 30 and sku.isascii() and
                all(c.isalnum() or c in "_-" for c in sku)):
            return False, "SKU harus 1–30 karakter: huruf, angka, garis bawah atau tanda minus."
        if not name.strip() or len(name) > 120:
            return False, "Nama wajib diisi (maks. 120 karakter)."
        if len(description) > 1000:
            return False, "Deskripsi maks. 1000 karakter."
        if category is None or len(category) > 100:
            return False, "Kategori maks. 100 karakter."
        if not isinstance(price, int) or price <= 0 or price > 1_000_000_000:
            return False, "Harga harus 1–1.000.000.000."
        if not isinstance(stock, int) or stock < 0 or stock > 1_000_000:
            return False, "Stok harus 0–1.000.000."
        with db.cursor() as cur:
            cur.execute("SELECT seller_id FROM products WHERE sku=%s", (sku,))
            existing = cur.fetchone()
            if existing and existing[0] != user_id:
                return False, "SKU sudah dimiliki seller lain."
            cur.execute("""INSERT INTO products (sku, name, category, price, reseller_price, stock, description, seller_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (sku) DO UPDATE SET name=EXCLUDED.name, category=EXCLUDED.category, price=EXCLUDED.price,
                reseller_price=EXCLUDED.price, stock=EXCLUDED.stock,
                description=EXCLUDED.description, seller_id=EXCLUDED.seller_id""",
                (sku, name, category, price, price, stock, description, user_id))
            _audit(db, user_id, "add_product_web", sku)
        db.commit()
        return True, f"Produk {sku} berhasil disimpan."
    except Exception as exc:
        LOG.error("Add product error: %s", exc)
        db.rollback()
        return False, "Terjadi kesalahan."
    finally:
        db.close()


def update_stock(user_id, sku, stock):
    db = get_db()
    try:
        if not isinstance(stock, int) or stock < 0 or stock > 1_000_000:
            return False, "Stok harus 0–1.000.000."
        with db.cursor() as cur:
            cur.execute("SELECT * FROM products WHERE sku=%s AND seller_id=%s", (sku.upper(), user_id))
            product = cur.fetchone()
            if not product:
                return False, "Produk tidak ditemukan atau bukan milik Anda."
            cur.execute("UPDATE products SET stock=%s WHERE sku=%s AND seller_id=%s",
                        (stock, sku.upper(), user_id))
            _audit(db, user_id, "update_stock_web", f"{sku.upper()}: {stock}")
        db.commit()
        return True, f"Stok {sku.upper()} diperbarui."
    except Exception as exc:
        LOG.error("Update stock error: %s", exc)
        db.rollback()
        return False, "Terjadi kesalahan."
    finally:
        db.close()


def delete_product(user_id, sku):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM products WHERE sku=%s AND seller_id=%s", (sku.upper(), user_id))
            product = cur.fetchone()
            if not product:
                return False, "Produk tidak ditemukan atau bukan milik Anda."
            cur.execute("SELECT count(*) FROM orders WHERE sku=%s AND status IN ('pending_payment', 'awaiting_confirmation')",
                        (sku.upper(),))
            pending = cur.fetchone()[0]
            if pending:
                return False, "Tidak dapat menghapus produk yang sedang dalam pesanan aktif."
            cur.execute("DELETE FROM products WHERE sku=%s AND seller_id=%s", (sku.upper(), user_id))
            _audit(db, user_id, "delete_product_web", sku.upper())
        db.commit()
        return True, f"Produk {sku.upper()} berhasil dihapus."
    except Exception as exc:
        LOG.error("Delete product error: %s", exc)
        db.rollback()
        return False, "Terjadi kesalahan."
    finally:
        db.close()


def get_reseller_stats(user_id):
    db = get_db()
    try:
        with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT count(*) as cnt, coalesce(sum(stock), 0) as total_stock FROM products WHERE seller_id=%s",
                        (user_id,))
            products = cur.fetchone()
            cur.execute("""SELECT count(*) as cnt, coalesce(sum(total), 0) as revenue
                FROM orders WHERE sku IN (SELECT sku FROM products WHERE seller_id=%s) AND status IN ('paid', 'shipped', 'completed')""",
                        (user_id,))
            orders = cur.fetchone()
        return {
            "total_products": products["cnt"],
            "total_stock": products["total_stock"],
            "total_orders": orders["cnt"],
            "total_revenue": orders["revenue"]
        }
    finally:
        db.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        LOG.info(format, *args)

    def _send(self, code, content_type, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None, "Ukuran permintaan tidak valid."
        if length < 0 or length > MAX_REQUEST_BODY:
            return None, "Ukuran permintaan terlalu besar."
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
            return (data, None) if isinstance(data, dict) else (None, "Invalid JSON")
        except (json.JSONDecodeError, ValueError):
            return None, "Invalid JSON"

    def _authenticated_user_id(self, data):
        user_id, err = authenticate_init_data(data.get("init_data"))
        if err:
            self._send(401, "application/json", json.dumps({"success": False, "message": err}))
            return None
        return user_id

    def do_GET(self):
        parsed = self.path.split("?")[0]
        if parsed == "/":
            self._serve_template("reseller.html", "text/html")
        elif parsed == "/dashboard":
            self._serve_template("dashboard.html", "text/html")
        else:
            self._send(404, "text/plain", "Not Found")

    def do_POST(self):
        parsed = self.path.split("?")[0]
        routes = {
            "/api/register": self._handle_register,
            "/api/products": self._handle_products,
            "/api/product/add": self._handle_add_product,
            "/api/product/stock": self._handle_update_stock,
            "/api/product/delete": self._handle_delete_product,
            "/api/stats": self._handle_stats,
        }
        handler = routes.get(parsed)
        if handler:
            handler()
        else:
            self._send(404, "text/plain", "Not Found")

    def _serve_template(self, filename, content_type):
        path = TEMPLATE_DIR / filename
        if not path.exists():
            self._send(404, "text/plain", "Template not found")
            return
        self._send(200, content_type, path.read_bytes())

    def _handle_register(self):
        data, err = self._json_body()
        if err:
            self._send(400, "application/json", json.dumps({"success": False, "message": err}))
            return
        user_id = self._authenticated_user_id(data)
        shop_name = (data.get("shop_name") or "").strip()
        description = (data.get("description") or "").strip()
        alamat_lengkap = (data.get("alamat_lengkap") or "").strip()
        domisili = (data.get("domisili") or "").strip()
        if user_id is None:
            return
        if not shop_name or len(shop_name) > 50:
            self._send(400, "application/json", json.dumps({"success": False, "message": "Nama toko tidak valid"}))
            return
        if not description or len(description) > 200:
            self._send(400, "application/json", json.dumps({"success": False, "message": "Deskripsi tidak valid"}))
            return
        if not alamat_lengkap or len(alamat_lengkap) > 500:
            self._send(400, "application/json", json.dumps({"success": False, "message": "Alamat tidak valid"}))
            return
        if not domisili or len(domisili) > 100:
            self._send(400, "application/json", json.dumps({"success": False, "message": "Domisili tidak valid"}))
            return
        ok, msg = register_reseller(user_id, shop_name, description, alamat_lengkap, domisili)
        self._send(200, "application/json", json.dumps({"success": ok, "message": msg}))

    def _handle_products(self):
        data, err = self._json_body()
        if err:
            self._send(400, "application/json", json.dumps({"success": False, "message": err}))
            return
        user_id = self._authenticated_user_id(data)
        if user_id is None:
            return
        user, err = verify_user(user_id)
        if err:
            self._send(403, "application/json", json.dumps({"success": False, "message": err}))
            return
        page = data.get("page", 0)
        query = data.get("query", "")
        products = get_my_products(user_id, page, query)
        self._send(200, "application/json", json.dumps({"success": True, "products": products}))

    def _handle_add_product(self):
        data, err = self._json_body()
        if err:
            self._send(400, "application/json", json.dumps({"success": False, "message": err}))
            return
        user_id = self._authenticated_user_id(data)
        if user_id is None:
            return
        user, err = verify_user(user_id)
        if err:
            self._send(403, "application/json", json.dumps({"success": False, "message": err}))
            return
        sku = (data.get("sku") or "").strip()
        name = (data.get("name") or "").strip()
        price = data.get("price")
        stock = data.get("stock")
        description = (data.get("description") or "").strip()
        if not all([sku, name, isinstance(price, int), isinstance(stock, int)]):
            self._send(400, "application/json", json.dumps({"success": False, "message": "Field tidak lengkap"}))
            return
        ok, msg = add_product(user_id, sku, name, price, stock, description, data.get("category", ""))
        self._send(200, "application/json", json.dumps({"success": ok, "message": msg}))

    def _handle_update_stock(self):
        data, err = self._json_body()
        if err:
            self._send(400, "application/json", json.dumps({"success": False, "message": err}))
            return
        user_id = self._authenticated_user_id(data)
        if user_id is None:
            return
        user, err = verify_user(user_id)
        if err:
            self._send(403, "application/json", json.dumps({"success": False, "message": err}))
            return
        sku = (data.get("sku") or "").strip()
        stock = data.get("stock")
        if not sku or not isinstance(stock, int):
            self._send(400, "application/json", json.dumps({"success": False, "message": "sku & stock required"}))
            return
        ok, msg = update_stock(user_id, sku, stock)
        self._send(200, "application/json", json.dumps({"success": ok, "message": msg}))

    def _handle_delete_product(self):
        data, err = self._json_body()
        if err:
            self._send(400, "application/json", json.dumps({"success": False, "message": err}))
            return
        user_id = self._authenticated_user_id(data)
        if user_id is None:
            return
        user, err = verify_user(user_id)
        if err:
            self._send(403, "application/json", json.dumps({"success": False, "message": err}))
            return
        sku = (data.get("sku") or "").strip()
        if not sku:
            self._send(400, "application/json", json.dumps({"success": False, "message": "sku required"}))
            return
        ok, msg = delete_product(user_id, sku)
        self._send(200, "application/json", json.dumps({"success": ok, "message": msg}))

    def _handle_stats(self):
        data, err = self._json_body()
        if err:
            self._send(400, "application/json", json.dumps({"success": False, "message": err}))
            return
        user_id = self._authenticated_user_id(data)
        if user_id is None:
            return
        user, err = verify_user(user_id)
        if err:
            self._send(403, "application/json", json.dumps({"success": False, "message": err}))
            return
        stats = get_reseller_stats(user_id)
        self._send(200, "application/json", json.dumps({"success": True, "stats": stats}))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = HTTPServer((HOST, PORT), Handler)
    LOG.info("Web app berjalan di http://%s:%s", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Web app dihentikan.")
    server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
