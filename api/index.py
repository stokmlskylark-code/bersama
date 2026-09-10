import json
import os
import time
from pathlib import Path

import psycopg2
import psycopg2.extras

from telegram_auth import authenticate_init_data

PG_DSN = os.getenv("PG_DSN")
TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
MAX_REQUEST_BODY = 64 * 1024


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
    except Exception:
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
                ON CONFLICT (sku) DO UPDATE SET name=EXCLUDED.name, category=EXCLUDED.category,
                price=EXCLUDED.price, reseller_price=EXCLUDED.price, stock=EXCLUDED.stock,
                description=EXCLUDED.description, seller_id=EXCLUDED.seller_id""",
                (sku, name, category, price, price, stock, description, user_id))
            _audit(db, user_id, "add_product_web", sku)
        db.commit()
        return True, f"Produk {sku} berhasil disimpan."
    except Exception:
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
    except Exception:
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
    except Exception:
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


def _json_response(start_response, status, payload):
    body = json.dumps(payload).encode()
    start_response(f"{status} {'OK' if status == 200 else 'BAD REQUEST' if status == 400 else 'UNAUTHORIZED'}", [
        ("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
    return [body]

def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")

    if method == "GET" and path == "/":
        tpl = TEMPLATE_DIR / "reseller.html"
        if tpl.exists():
            body = tpl.read_bytes()
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body)))])
            return [body]
        start_response("404 NOT FOUND", [("Content-Type", "text/plain")])
        return [b"Not Found"]

    if method == "GET" and path == "/dashboard":
        tpl = TEMPLATE_DIR / "dashboard.html"
        if tpl.exists():
            body = tpl.read_bytes()
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body)))])
            return [body]
        start_response("404 NOT FOUND", [("Content-Type", "text/plain")])
        return [b"Not Found"]

    if method == "POST":
        try:
            length = int(environ.get("CONTENT_LENGTH", 0))
        except (TypeError, ValueError):
            return _json_response(start_response, 400, {"success": False, "message": "Ukuran permintaan tidak valid."})
        if length < 0 or length > MAX_REQUEST_BODY:
            return _json_response(start_response, 400, {"success": False, "message": "Ukuran permintaan terlalu besar."})
        raw = environ["wsgi.input"].read(length)
        try:
            data = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            return _json_response(start_response, 400, {"success": False, "message": "Invalid JSON"})
        if not isinstance(data, dict):
            return _json_response(start_response, 400, {"success": False, "message": "Invalid JSON"})
        if path not in {"/api/register", "/api/products", "/api/product/add", "/api/product/stock", "/api/product/delete", "/api/stats"}:
            start_response("404 NOT FOUND", [("Content-Type", "text/plain")])
            return [b"Not Found"]
        user_id, auth_error = authenticate_init_data(data.get("init_data"))
        if auth_error:
            return _json_response(start_response, 401, {"success": False, "message": auth_error})

        if path == "/api/register":
            shop_name = (data.get("shop_name") or "").strip()
            description = (data.get("description") or "").strip()
            alamat_lengkap = (data.get("alamat_lengkap") or "").strip()
            domisili = (data.get("domisili") or "").strip()
            if not shop_name or len(shop_name) > 50:
                resp = {"success": False, "message": "Nama toko tidak valid"}
            elif not description or len(description) > 200:
                resp = {"success": False, "message": "Deskripsi tidak valid"}
            elif not alamat_lengkap or len(alamat_lengkap) > 500:
                resp = {"success": False, "message": "Alamat tidak valid"}
            elif not domisili or len(domisili) > 100:
                resp = {"success": False, "message": "Domisili tidak valid"}
            else:
                ok, msg = register_reseller(user_id, shop_name, description, alamat_lengkap, domisili)
                resp = {"success": ok, "message": msg}
        else:
            user, err = verify_user(user_id)
            if err:
                resp = {"success": False, "message": err}
            elif path == "/api/products":
                resp = {"success": True, "products": get_my_products(user_id, data.get("page", 0), data.get("query", ""))}
            elif path == "/api/product/add":
                ok, msg = add_product(user_id, (data.get("sku") or "").strip(),
                    (data.get("name") or "").strip(), data.get("price"), data.get("stock"),
                    (data.get("description") or "").strip(), data.get("category", ""))
                resp = {"success": ok, "message": msg}
            elif path == "/api/product/stock":
                ok, msg = update_stock(user_id, (data.get("sku") or "").strip(), data.get("stock"))
                resp = {"success": ok, "message": msg}
            elif path == "/api/product/delete":
                ok, msg = delete_product(user_id, (data.get("sku") or "").strip())
                resp = {"success": ok, "message": msg}
            else:
                resp = {"success": True, "stats": get_reseller_stats(user_id)}
        return _json_response(start_response, 200, resp)

    start_response("405 METHOD NOT ALLOWED", [("Content-Type", "text/plain")])
    return [b"Method Not Allowed"]
