"""Bot Telegram auto-order katalog apps premium + Midtrans Snap.

Stdlib saja (urllib, sqlite3). Jalankan: python3 bot.py
Konfigurasi lewat .env di folder yang sama (lihat .env.example).
"""
import base64
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html import escape

# ── konfigurasi ──────────────────────────────────────────────
def load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_env()
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
SHOP = os.environ.get("SHOP_NAME", "Premium Store")
SUPPORT = os.environ.get("SUPPORT_CONTACT", "@admin")
MT_SERVER_KEY = os.environ["MIDTRANS_SERVER_KEY"]
MT_PROD = os.environ.get("MIDTRANS_PRODUCTION", "false").lower() == "true"
MT_SNAP = "https://app.midtrans.com/snap/v1/transactions" if MT_PROD else "https://app.sandbox.midtrans.com/snap/v1/transactions"
MT_API = "https://api.midtrans.com/v2" if MT_PROD else "https://api.sandbox.midtrans.com/v2"
ORDER_TTL_MIN = int(os.environ.get("ORDER_TTL_MINUTES", "30"))
DB_PATH = os.environ.get("DATABASE_PATH", "shop.sqlite3")
TG = f"https://api.telegram.org/bot{TOKEN}/"

# ── database ─────────────────────────────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db_lock = threading.Lock()
db.executescript("""
CREATE TABLE IF NOT EXISTS products(sku TEXT PRIMARY KEY, name TEXT, price INTEGER, description TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS stock(id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT, content TEXT, order_id TEXT);
CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, user_id INTEGER, sku TEXT, qty INTEGER, amount INTEGER,
    status TEXT DEFAULT 'pending', pay_url TEXT, created_at INTEGER);
""")

def q(sql, *args):
    with db_lock:
        cur = db.execute(sql, args)
        db.commit()
        return cur

def stock_count(sku):
    return q("SELECT COUNT(*) c FROM stock WHERE sku=? AND order_id IS NULL", sku).fetchone()["c"]

def rp(n):
    return "Rp " + f"{n:,}".replace(",", ".")

# ── telegram api ─────────────────────────────────────────────
def tg(method, **params):
    data = json.dumps(params).encode()
    req = urllib.request.Request(TG + method, data, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r).get("result")
    except urllib.error.HTTPError as e:
        print("TG error", method, e.read().decode()[:200])

def send(chat_id, text, buttons=None):
    return tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
              disable_web_page_preview=True, reply_markup=kb(buttons))

def edit(chat_id, msg_id, text, buttons=None):
    r = tg("editMessageText", chat_id=chat_id, message_id=msg_id, text=text, parse_mode="HTML",
           disable_web_page_preview=True, reply_markup=kb(buttons))
    if r is None:  # pesan lama/identik: kirim baru saja
        send(chat_id, text, buttons)

def kb(rows):
    if not rows:
        return None
    return {"inline_keyboard": [[{"text": t, **({"url": d} if d.startswith("http") else {"callback_data": d})}
                                 for t, d in row] for row in rows]}

# ── midtrans ─────────────────────────────────────────────────
def mt_request(url, body=None):
    auth = base64.b64encode(f"{MT_SERVER_KEY}:".encode()).decode()
    req = urllib.request.Request(url, json.dumps(body).encode() if body else None,
                                 {"Content-Type": "application/json", "Accept": "application/json",
                                  "Authorization": "Basic " + auth})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        print("Midtrans error", e.read().decode()[:300])
        return {}

def mt_create(order_id, amount, name, qty, user):
    return mt_request(MT_SNAP, {
        "transaction_details": {"order_id": order_id, "gross_amount": amount},
        "item_details": [{"id": order_id, "price": amount // qty, "quantity": qty, "name": name[:50]}],
        "customer_details": {"first_name": (user.get("first_name") or "Telegram")[:50]},
        "expiry": {"unit": "minutes", "duration": ORDER_TTL_MIN},
    })

def mt_status(order_id):
    return mt_request(f"{MT_API}/{order_id}/status").get("transaction_status", "")

# ── tampilan ─────────────────────────────────────────────────
LINE = "━━━━━━━━━━━━━━━━━━"

def home_text():
    return (f"✨ <b>{escape(SHOP)}</b>\n{LINE}\n"
            "Katalog apps premium, proses <b>otomatis 24 jam</b>.\n"
            "Bayar via Midtrans, akun langsung dikirim ke chat ini.\n\n"
            f"Pilih produk di bawah 👇\n{LINE}\n<i>Bantuan: {escape(SUPPORT)}</i>")

def home_buttons(admin=False):
    rows = [[(f"{p['name']}  ·  {rp(p['price'])}", f"p:{p['sku']}")]
            for p in q("SELECT * FROM products ORDER BY name").fetchall()]
    rows.append([("📦 Pesanan Saya", "orders")])
    if admin:
        rows.append([("⚙️ Panel Admin", "admin")])
    return rows

def product_text(p):
    n = stock_count(p["sku"])
    stok = f"✅ Tersedia ({n})" if n else "❌ Habis"
    return (f"🛍 <b>{escape(p['name'])}</b>\n{LINE}\n"
            f"💰 Harga  : <b>{rp(p['price'])}</b>\n📦 Stok   : {stok}\n\n"
            f"{escape(p['description']) or '<i>Tanpa deskripsi</i>'}\n{LINE}")

def product_buttons(p, admin=False):
    n = stock_count(p["sku"])
    rows = []
    if n:
        rows.append([(f"🛒 Beli {i}", f"buy:{p['sku']}:{i}") for i in (1, 2, 3) if i <= n])
    if admin:
        rows.append([("➕ Restok", f"a:restock:{p['sku']}"), ("💰 Harga", f"a:price:{p['sku']}")])
        rows.append([("📝 Deskripsi", f"a:desc:{p['sku']}"), ("🗑 Hapus", f"a:del:{p['sku']}")])
    rows.append([("‹ Kembali", "home")])
    return rows

def order_text(o, p):
    icon = {"pending": "⏳", "paid": "✅", "expired": "⌛", "cancelled": "❌"}[o["status"]]
    return (f"🧾 <b>Invoice</b> <code>{o['id']}</code>\n{LINE}\n"
            f"Produk : {escape(p['name'])} × {o['qty']}\n"
            f"Total  : <b>{rp(o['amount'])}</b>\n"
            f"Status : {icon} {o['status'].upper()}\n{LINE}")

# ── alur pembeli ─────────────────────────────────────────────
def create_order(user, sku, qty, chat_id):
    p = q("SELECT * FROM products WHERE sku=?", sku).fetchone()
    if not p:
        return send(chat_id, "Produk tidak ditemukan.")
    order_id = "ORD-" + uuid.uuid4().hex[:10].upper()
    with db_lock:
        items = db.execute("SELECT id FROM stock WHERE sku=? AND order_id IS NULL LIMIT ?", (sku, qty)).fetchall()
        if len(items) < qty:
            return send(chat_id, "Stok tidak cukup 🙏")
        db.executemany("UPDATE stock SET order_id=? WHERE id=?", [(order_id, i["id"]) for i in items])
        db.commit()
    amount = p["price"] * qty
    snap = mt_create(order_id, amount, p["name"], qty, user)
    if not snap.get("redirect_url"):
        q("UPDATE stock SET order_id=NULL WHERE order_id=?", order_id)
        return send(chat_id, "Gagal membuat pembayaran, coba lagi nanti.")
    q("INSERT INTO orders VALUES(?,?,?,?,?,'pending',?,?)", order_id, user["id"], sku, qty, amount,
      snap["redirect_url"], int(time.time()))
    o = q("SELECT * FROM orders WHERE id=?", order_id).fetchone()
    send(chat_id, order_text(o, p) + f"\nBayar dalam <b>{ORDER_TTL_MIN} menit</b> ⏱",
         [[("💳 Bayar Sekarang", snap["redirect_url"])], [("🔄 Cek Pembayaran", f"chk:{order_id}")]])

def check_order(order_id, notify_chat=None):
    o = q("SELECT * FROM orders WHERE id=?", order_id).fetchone()
    if not o or o["status"] != "pending":
        return o
    st = mt_status(order_id)
    if st in ("settlement", "capture"):
        q("UPDATE orders SET status='paid' WHERE id=?", order_id)
        deliver(o)
    elif st in ("expire", "cancel", "deny", "failure") or time.time() - o["created_at"] > (ORDER_TTL_MIN + 5) * 60:
        q("UPDATE orders SET status='expired' WHERE id=?", order_id)
        q("UPDATE stock SET order_id=NULL WHERE order_id=?", order_id)
        send(o["user_id"], f"⌛ Pesanan <code>{order_id}</code> kedaluwarsa. Stok dikembalikan.")
    elif notify_chat:
        send(notify_chat, "⏳ Pembayaran belum masuk. Coba cek lagi beberapa saat.")
    return q("SELECT * FROM orders WHERE id=?", order_id).fetchone()

def deliver(o):
    p = q("SELECT * FROM products WHERE sku=?", o["sku"]).fetchone()
    items = q("SELECT content FROM stock WHERE order_id=?", o["id"]).fetchall()
    body = "\n".join(f"<code>{escape(i['content'])}</code>" for i in items)
    send(o["user_id"], f"✅ <b>Pembayaran diterima!</b>\n{LINE}\n🛍 {escape(p['name'])} × {o['qty']}\n\n{body}\n{LINE}\n"
                       f"Terima kasih 💙 Ada masalah? {escape(SUPPORT)}")
    for a in ADMIN_IDS:
        send(a, f"💰 Terjual: {escape(p['name'])} × {o['qty']} — {rp(o['amount'])}\n<code>{o['id']}</code>")

def my_orders(chat_id, user_id):
    rows = q("SELECT o.*, p.name FROM orders o JOIN products p ON p.sku=o.sku WHERE user_id=? "
             "ORDER BY created_at DESC LIMIT 10", user_id).fetchall()
    if not rows:
        return f"📦 <b>Pesanan Saya</b>\n{LINE}\nBelum ada pesanan."
    lines = [f"{ {'pending':'⏳','paid':'✅','expired':'⌛','cancelled':'❌'}[r['status']] } <code>{r['id']}</code> "
             f"{escape(r['name'])} × {r['qty']}" for r in rows]
    return f"📦 <b>Pesanan Saya</b>\n{LINE}\n" + "\n".join(lines)

# ── admin ────────────────────────────────────────────────────
pending_input = {}  # user_id -> (action, sku)

def admin_text():
    rows = q("SELECT * FROM products ORDER BY name").fetchall()
    total = q("SELECT COALESCE(SUM(amount),0) s, COUNT(*) c FROM orders WHERE status='paid'").fetchone()
    lines = [f"• {escape(r['name'])} — {rp(r['price'])} — stok {stock_count(r['sku'])}" for r in rows]
    return (f"⚙️ <b>Panel Admin</b>\n{LINE}\n💰 Omzet: <b>{rp(total['s'])}</b> ({total['c']} terbayar)\n\n"
            + ("\n".join(lines) or "<i>Belum ada produk</i>") + f"\n{LINE}\nPilih produk untuk kelola:")

def admin_buttons():
    rows = [[(p["name"], f"p:{p['sku']}")] for p in q("SELECT * FROM products ORDER BY name").fetchall()]
    rows.append([("➕ Tambah Produk", "a:add:-"), ("‹ Beranda", "home")])
    return rows

PROMPTS = {
    "add": "Kirim produk baru dengan format:\n<code>sku | Nama Produk | harga | deskripsi</code>",
    "restock": "Kirim stok baru, <b>satu item per baris</b> (contoh: email:password).",
    "price": "Kirim harga baru (angka saja).",
    "desc": "Kirim deskripsi baru.",
}

def admin_input(user_id, chat_id, text):
    action, sku = pending_input.pop(user_id)
    if action == "add":
        parts = [x.strip() for x in text.split("|")]
        if len(parts) < 3 or not parts[2].isdigit():
            return send(chat_id, "Format salah. " + PROMPTS["add"])
        q("INSERT OR REPLACE INTO products VALUES(?,?,?,?)", parts[0], parts[1], int(parts[2]),
          parts[3] if len(parts) > 3 else "")
        sku = parts[0]
    elif action == "restock":
        items = [(sku, l.strip()) for l in text.splitlines() if l.strip()]
        q("BEGIN")
        db.executemany("INSERT INTO stock(sku, content) VALUES(?,?)", items)
        db.commit()
        send(chat_id, f"✅ {len(items)} item ditambahkan.")
    elif action == "price":
        if not text.strip().isdigit():
            return send(chat_id, "Harga harus angka.")
        q("UPDATE products SET price=? WHERE sku=?", int(text), sku)
    elif action == "desc":
        q("UPDATE products SET description=? WHERE sku=?", text.strip(), sku)
    p = q("SELECT * FROM products WHERE sku=?", sku).fetchone()
    send(chat_id, product_text(p), product_buttons(p, admin=True))

# ── router ───────────────────────────────────────────────────
def on_message(m):
    user, chat_id, text = m["from"], m["chat"]["id"], m.get("text", "")
    is_admin = user["id"] in ADMIN_IDS
    if user["id"] in pending_input and not text.startswith("/"):
        return admin_input(user["id"], chat_id, text) if is_admin else None
    if text.startswith("/admin") and is_admin:
        return send(chat_id, admin_text(), admin_buttons())
    send(chat_id, home_text(), home_buttons(is_admin))

def on_callback(c):
    user, msg, data = c["from"], c["message"], c["data"]
    chat_id, msg_id, is_admin = msg["chat"]["id"], msg["message_id"], user["id"] in ADMIN_IDS
    tg("answerCallbackQuery", callback_query_id=c["id"])
    cmd, *args = data.split(":")
    if cmd == "home":
        edit(chat_id, msg_id, home_text(), home_buttons(is_admin))
    elif cmd == "orders":
        edit(chat_id, msg_id, my_orders(chat_id, user["id"]), [[("‹ Kembali", "home")]])
    elif cmd == "p":
        p = q("SELECT * FROM products WHERE sku=?", args[0]).fetchone()
        if p:
            edit(chat_id, msg_id, product_text(p), product_buttons(p, is_admin))
    elif cmd == "buy":
        create_order(user, args[0], int(args[1]), chat_id)
    elif cmd == "chk":
        o = check_order(args[0], notify_chat=chat_id)
        if o:
            p = q("SELECT * FROM products WHERE sku=?", o["sku"]).fetchone()
            btns = [[("💳 Bayar Sekarang", o["pay_url"])], [("🔄 Cek Pembayaran", f"chk:{o['id']}")]] \
                if o["status"] == "pending" else [[("‹ Beranda", "home")]]
            edit(chat_id, msg_id, order_text(o, p), btns)
    elif cmd == "admin" and is_admin:
        edit(chat_id, msg_id, admin_text(), admin_buttons())
    elif cmd == "a" and is_admin:
        action, sku = args
        if action == "del":
            q("DELETE FROM products WHERE sku=?", sku)
            q("DELETE FROM stock WHERE sku=? AND order_id IS NULL", sku)
            return edit(chat_id, msg_id, admin_text(), admin_buttons())
        pending_input[user["id"]] = (action, sku)
        send(chat_id, f"✏️ {PROMPTS[action]}\n<i>Kirim /admin untuk batal.</i>")

def poll_payments():
    while True:
        for o in q("SELECT id FROM orders WHERE status='pending'").fetchall():
            try:
                check_order(o["id"])
            except Exception as e:  # jangan matikan loop karena satu order
                print("poll error", o["id"], e)
        time.sleep(20)

def main():
    threading.Thread(target=poll_payments, daemon=True).start()
    print(f"{SHOP} berjalan… (Midtrans {'PRODUCTION' if MT_PROD else 'SANDBOX'})")
    offset = 0
    while True:
        updates = tg("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "callback_query"]) or []
        for u in updates:
            offset = u["update_id"] + 1
            try:
                if "message" in u and u["message"]["chat"]["type"] == "private":
                    on_message(u["message"])
                elif "callback_query" in u:
                    on_callback(u["callback_query"])
            except Exception as e:
                print("handler error", e)

if __name__ == "__main__":
    main()
