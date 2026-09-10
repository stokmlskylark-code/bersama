#!/usr/bin/env python3
"""Bot toko Telegram tanpa dependensi pihak ketiga. Jalankan: python3 bot.py."""

import json
import http.client
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from store import ShopError, Store
from pakasir import Pakasir, PakasirError


LOG = logging.getLogger("shopbot")
STATUS = {
    "pending_payment": "Menunggu pembayaran",
    "awaiting_confirmation": "Bukti menunggu pemeriksaan",
    "paid": "Lunas / sedang diproses",
    "shipped": "Sudah dikirim",
    "completed": "Selesai",
    "cancelled": "Dibatalkan",
    "expired": "Kedaluwarsa",
}


def rupiah(amount):
    return "Rp" + f"{amount:,}".replace(",", ".")


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime("%d-%m-%Y %H:%M UTC")


def number(value):
    if not re.fullmatch(r"[0-9]{1,15}", value.strip()):
        raise ShopError("Angka harus bilangan bulat tanpa titik, koma, atau tanda minus.")
    return int(value)


def load_env(path=".env"):
    if not Path(path).exists():
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
            raise ValueError("Format .env tidak valid; gunakan NAMA=nilai.")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    admins: frozenset
    shop_name: str
    payment: str
    support: str
    pg_dsn: str
    ttl_hours: int
    provider: str = "manual"
    pakasir_project: str = ""
    pakasir_api_key: str = field(default="", repr=False)
    webapp_url: str = ""
    channel_username: str = ""

    @classmethod
    def from_env(cls):
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        raw_admins = os.getenv("ADMIN_IDS", "")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ValueError("Isi TELEGRAM_BOT_TOKEN yang valid dari @BotFather.")
        try:
            admins = frozenset(int(item.strip()) for item in raw_admins.split(","))
            ttl = int(os.getenv("ORDER_TTL_HOURS", "24"))
        except ValueError:
            raise ValueError("ADMIN_IDS harus ID numerik dipisahkan koma; ORDER_TTL_HOURS harus angka.") from None
        if not admins or any(item <= 0 for item in admins) or not 1 <= ttl <= 168:
            raise ValueError("ADMIN_IDS harus positif dan ORDER_TTL_HOURS harus 1–168.")
        payment = os.getenv("PAYMENT_INSTRUCTIONS", "").strip()
        provider = os.getenv("PAYMENT_PROVIDER", "pakasir").strip().lower()
        project = os.getenv("PAKASIR_PROJECT", "").strip()
        api_key = os.getenv("PAKASIR_API_KEY", "").strip()
        if provider not in ("pakasir", "manual"):
            raise ValueError("PAYMENT_PROVIDER harus pakasir atau manual.")
        if provider == "pakasir" or project or api_key:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", project) or not api_key:
                raise ValueError("Isi PAKASIR_PROJECT (slug) dan PAKASIR_API_KEY dari proyek Pakasir.")
        support = os.getenv("SUPPORT_CONTACT", "").strip()
        shop_name = os.getenv("SHOP_NAME", "Toko Telegram").strip()
        if not support or (provider == "manual" and not payment):
            raise ValueError("Isi SUPPORT_CONTACT, serta PAYMENT_INSTRUCTIONS jika memakai pembayaran manual.")
        if len(payment) > 1000 or len(support) > 150 or not 1 <= len(shop_name) <= 100:
            raise ValueError("Batas karakter: instruksi pembayaran 1000, kontak 150, nama toko 1–100.")
        webapp_url = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
        pg_dsn = os.getenv("PG_DSN", "dbname=postgres user=postgres password=postgres host=localhost port=5432")
        channel_username = os.getenv("CHANNEL_USERNAME", "").strip().lstrip("@")
        return cls(token, admins, shop_name, payment, support,
                   pg_dsn, ttl, provider, project, api_key, webapp_url, channel_username)


class TelegramError(Exception):
    def __init__(self, code=0, retry_after=0):
        # Jangan masukkan URL API ke log karena mengandung token.
        super().__init__(f"Telegram API gagal (kode {code}).")
        self.code = code
        self.retry_after = retry_after


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, **payload):
        request = urllib.request.Request(self.base + method,
                                         data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            retry_after = 0
            try:
                retry_after = json.loads(exc.read()).get("parameters", {}).get("retry_after", 0)
            except (ValueError, OSError):
                pass
            raise TelegramError(exc.code, retry_after) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException):
            raise TelegramError() from None
        if not result.get("ok"):
            raise TelegramError(result.get("error_code", 0), result.get("parameters", {}).get("retry_after", 0))
        return result["result"]

    def send_photo(self, chat_id, photo, caption="", reply_markup=None):
        boundary = "----Boundary" + str(id(photo))
        body = b""

        def field(name, value):
            nonlocal body
            body += f"--{boundary}\r\n".encode()
            body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            body += f"{value}\r\n".encode()

        field("chat_id", chat_id)
        if caption:
            field("caption", caption)
        if reply_markup:
            field("reply_markup", json.dumps(reply_markup))

        body += f"--{boundary}\r\n".encode()
        body += b'Content-Disposition: form-data; name="photo"; filename="banner.png"\r\n'
        body += b"Content-Type: image/png\r\n\r\n"
        body += photo
        body += b"\r\n"
        body += f"--{boundary}--\r\n".encode()

        request = urllib.request.Request(
            self.base + "sendPhoto", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(request, timeout=45) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise TelegramError(exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise TelegramError()
        if not result.get("ok"):
            raise TelegramError(result.get("error_code", 0))
        return result["result"]


def keyboard(*rows):
    return {"inline_keyboard": [[{"text": text, "callback_data": data} for text, data in row] for row in rows]}


class ShopBot:
    def __init__(self, config, store, api, pakasir=None, banner=b''):
        self.config, self.store, self.api = config, store, api
        self.banner = banner
        self.pakasir = pakasir or (Pakasir(config.pakasir_project, config.pakasir_api_key)
                                   if config.pakasir_project and config.pakasir_api_key else None)

    def is_channel_member(self, user_id):
        if not self.config.channel_username:
            return True
        try:
            result = self.api.call("getChatMember",
                                   chat_id=f"@{self.config.channel_username}",
                                   user_id=user_id)
            status = result.get("status", "")
            return status in ("member", "administrator", "creator")
        except TelegramError:
            return True

    def send(self, chat_id, text, markup=None):
        chunks = [text[i:i + 2000] for i in range(0, len(text), 2000)] or ["—"]
        for index, chunk in enumerate(chunks):
            payload = {"chat_id": chat_id, "text": chunk}
            if markup and index == len(chunks) - 1:
                payload["reply_markup"] = markup
            self.api.call("sendMessage", **payload)

    def notify(self, chat_id, text, markup=None):
        try:
            self.send(chat_id, text, markup)
        except TelegramError as exc:
            LOG.warning("Notifikasi tidak terkirim, kode=%s; periksa dashboard untuk status terbaru.", exc.code)

    def notify_admins(self, text, markup=None):
        for admin in self.config.admins:
            self.notify(admin, text, markup)

    def payment_client(self, payment):
        if not self.pakasir or payment["project"] != self.config.pakasir_project:
            raise ShopError("Konfigurasi Pakasir untuk invoice ini tidak tersedia. Hubungi admin; jangan transfer manual.")
        return self.pakasir

    def sync_payment(self, actor, order_id, cancel=False):
        order = self.store.order(order_id, actor)
        payment = self.store.payment(order_id, actor)
        if not payment:
            raise ShopError("Invoice ini memakai pembayaran manual, bukan Pakasir.")
        if payment["state"] in ("completed", "late_completed"):
            return
        client = self.payment_client(payment)
        now = int(time.time())
        self.store.schedule_payment_check(order_id, now + (3600 if payment["state"] == "closed" else 60))
        status = client.detail(payment["reference"], order["total"])["status"]
        close_as = None
        terminals = ("canceled", "cancelled", "expired")
        if status in terminals:
            close_as = "expired" if status == "expired" else "cancelled"
        elif status != "completed" and (cancel or now >= order["expires_at"]):
            if status != "not_found":
                client.cancel(payment["reference"], order["total"])
                status = client.detail(payment["reference"], order["total"])["status"]
            if status in (*terminals, "not_found"):
                close_as = "cancelled" if cancel else "expired"
        event = self.store.apply_payment_status(order_id, payment["reference"], payment["project"],
                                                order["total"], status, close_as)
        if event:
            label = "Pembayaran diterima setelah pesanan ditutup; perlu rekonsiliasi admin, jangan kirim barang otomatis." \
                if event == "late_completed" else STATUS[event]
            markup = keyboard([("Lihat invoice", f"order:{order_id}")])
            self.notify(order["user_id"], f"Pakasir • Pesanan #{order_id}: {label}", markup)
            self.notify_admins(f"Pakasir • Pesanan #{order_id}: {label}", markup)

    def sync_payments(self):
        # ponytail: tiga pemeriksaan per putaran untuk satu toko kecil; volume besar perlu worker/webhook terverifikasi.
        for payment in self.store.due_payments(int(time.time())):
            try:
                self.sync_payment(payment["user_id"], payment["order_id"])
            except (PakasirError, ShopError):
                self.store.schedule_payment_check(payment["order_id"], int(time.time()) + 60)
                LOG.warning("Pemeriksaan Pakasir invoice #%s tertunda; stok tidak dilepas tanpa hasil pemeriksaan.", payment["order_id"])

    def check_payment(self, actor, order_id):
        payment = self.store.payment(order_id, actor)
        if not payment or int(time.time()) - payment["checked_at"] >= 10:
            self.sync_payment(actor, order_id)
        self.order_detail(actor, order_id)

    def payment_issues(self, actor):
        rows = self.store.payment_issues(actor)
        self.send(actor, "PEMBAYARAN TERLAMBAT — maks. 30 terbaru\n"
                  "Dana diterima setelah stok dilepas. Rekonsiliasi/refund manual diperlukan; jangan langsung kirim barang.\n\n"
                  + ("\n".join(f"#{row['id']} • ID pembeli {row['user_id']} • {rupiah(row['total'])}\n"
                               f"Ref: {row['reference']}\n/pesanan {row['id']}" for row in rows) or "Tidak ada."))

    def menu(self, actor):
        user = self.store.user(actor)
        stats = self.store.user_stats(actor)
        role = user["role"]
        role_label = {"customer": "Member", "reseller": "Seller", "admin": "Admin"}
        role_icon = {"customer": "🟢", "reseller": "🛍", "admin": "👑"}
        name = user["name"] or "Pengguna"
        text = (
            f"Halo {name}\n"
            f"Selamat datang di {self.config.shop_name}👋\n"
            f"\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"💰 Saldo    : Rp0\n"
            f"🏷️ Status   : {role_icon.get(role, '🟢')} {role_label.get(role, role)}\n"
            f"📦 Order    : {stats['completed_orders']} Selesai\n"
            f"💳 Belanja : {rupiah(stats['total_spending'])}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🎁 Jadi Seller & Reseller\n"
            f"❓ Punya produk digital sendiri?\n"
            f"💰 Jual produk Anda\n"
            f"📈 Dapatkan lebih banyak pelanggan\n"
            f"⚙️ Kelola harga & produk sendiri\n"
            f"📊 Pantau penjualan real-time\n"
            f"🏦 Withdraw keuntungan kapan saja\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🚀 Bergabung sekarang dan mulai menghasilkan bersama {self.config.shop_name}"
        )
        rows = [[("🛍 Katalog", "catalog:0"), ("📦 Pesanan saya", "orders:0")],
                [("🎫 Buat Tiket", "create_ticket"), ("❓ Bantuan", "help")]]
        if actor in self.config.admins:
            rows.append([("⚙ Dashboard admin", "admin")])
        markup = keyboard(*rows)
        if self.config.webapp_url:
            if user["role"] == "customer" and not user["reseller_request"]:
                markup["inline_keyboard"].append([{
                    "text": "🎁 Daftar Seller",
                    "web_app": {"url": self.config.webapp_url}
                }])
            elif user["role"] == "reseller":
                markup["inline_keyboard"].append([{
                    "text": "📊 Dashboard Seller",
                    "web_app": {"url": self.config.webapp_url + "/dashboard"}
                }])
        if self.banner:
            try:
                self.api.send_photo(actor, self.banner, caption=text, reply_markup=markup)
                return
            except TelegramError:
                pass
        self.send(actor, text, markup)

    def help(self, actor):
        text = (
            "COMMAND PEMBELI\n"
            "/start — menu utama\n/id — ID Telegram Anda\n/katalog — katalog dan harga sesuai role\n"
            "/cari kata — cari produk\n/produk SKU — detail produk\n"
            "/pesan SKU | jumlah | catatan — buat pesanan\n"
            "/riwayat — daftar pesanan Anda\n/pesanan ID — invoice dan status\n"
            "/batal ID — batalkan pesanan yang belum dibayar\n"
            "/bayar ID — buka invoice dan tombol pembayaran Pakasir\n"
            "/cekbayar ID — cek pembayaran langsung ke Pakasir\n"
            "Kirim foto/PDF dengan caption /bukti ID — hanya untuk invoice manual\n"
            f"\nCustomer Service\n/tiket subjek | pesan — buat tiket CS\n/lihattiket — lihat tiket Anda\n"
            f"\nBantuan toko: {self.config.support}\n"
            "Jangan kirim password, OTP, atau nomor kartu."
        )
        user = self.store.user(actor)
        if user["role"] == "reseller":
            text += (
                "\n\nKenapa jadi Seller?\n"
                "Kelola produk dan stok melalui Dashboard Seller di web app.\n"
                "Buka menu bot → klik Dashboard Seller."
            )
        if actor in self.config.admins:
            text += (
                "\n\nCOMMAND ADMIN\n/admin — dashboard\n/produkadmin — semua produk termasuk nonaktif\n"
                "/produkset SKU | nama | harga | harga_reseller | stok_tersedia | deskripsi\n"
                "/stok SKU jumlah — SET stok tersedia, bukan tambah stok\n"
                "/aktif SKU atau /nonaktif SKU\n/orderadmin [status] — semua pesanan/filter\n"
                "/bukti ID — lihat bukti pembayaran\n/konfirmasi ID — tandai pembayaran valid\n"
                "/tolak ID alasan — tolak bukti, buka ulang waktu pembayaran\n"
                "/selesai ID — tutup pesanan yang sudah dibayar\n"
                "/pengajuan — permintaan reseller\n/setreseller ID — setujui reseller\n"
                "/cabutreseller ID — tolak/cabut akses reseller\n/laporan — omzet sepanjang waktu\n"
                "/masalahbayar — pembayaran Pakasir setelah pesanan ditutup\n"
                "/lihattiket — lihat tiket CS terbuka\n/balastiket ID pesan — balas tiket\n/tutuptiket ID — tutup tiket\n"
                "/audit — 15 aksi terakhir\n\n"
                "Status filter: " + ", ".join(STATUS)
            )
        self.send(actor, text)

    CATEGORIES = {
        "apk_premium": "📱 APK Premium",
        "ai": "🤖 AI Tools",
        "games": "🎮 Games",
        "streaming": "📺 Streaming",
        "lainnya": "📦 Lainnya",
    }

    def catalog(self, actor, page=0, query="", admin=False, category=""):
        if admin:
            self.store.require_admin(actor)
        if not category and not admin and not query:
            lines = ["Pilih kategori produk:"]
            rows = []
            cat_items = list(self.CATEGORIES.items())
            for i in range(0, len(cat_items), 2):
                row = [(label, f"catalog:{key}:0") for key, label in cat_items[i:i+2]]
                rows.append(row)
            rows.append([("📦 Semua", "catalog::0")])
            self.send(actor, "\n".join(lines), keyboard(*rows))
            return
        products = self.store.catalog(page, query, admin, category)
        reseller = self.store.user(actor)["role"] == "reseller"
        cat_label = self.CATEGORIES.get(category, "Semua") if not query else "Pencarian"
        lines = [f"Katalog {cat_label} — halaman {page + 1}"]
        rows = []
        for product in products:
            price = product["reseller_price"] if reseller else product["price"]
            line = f"\n{product['sku']} • {product['name']}\n{rupiah(price)} | Stok tersedia: {product['stock']}"
            if admin:
                line += f" | Reseller: {rupiah(product['reseller_price'])} | {'aktif' if product['active'] else 'nonaktif'}"
            lines.append(line)
            rows.append([(f"Lihat {product['sku']}", f"product:{product['sku']}")])
        if not products:
            lines.append("Belum ada produk yang cocok.")
        if query:
            lines.append("Pencarian menampilkan maksimal 8 hasil. Gunakan kata yang lebih spesifik bila perlu.")
        else:
            prefix = "products_admin" if admin else f"catalog:{category}"
            nav = []
            if page:
                nav.append(("⬅ Sebelumnya", f"{prefix}:{page - 1}"))
            if len(products) == 8:
                nav.append(("Berikutnya ➡", f"{prefix}:{page + 1}"))
            if nav:
                rows.append(nav)
            if not admin:
                rows.append([("⬅ Kembali", "catalog_back")])
        self.send(actor, "\n".join(lines), keyboard(*rows) if rows else None)

    def my_products(self, actor, page=0, query=""):
        user = self.store.user(actor)
        if user["role"] != "reseller":
            raise ShopError("Anda belum menjadi reseller. Gunakan /daftarreseller untuk mengajukan.")
        products = self.store.my_products(actor, page, query)
        lines = [f"Produk Saya — halaman {page + 1}"]
        rows = []
        for product in products:
            line = f"\n{product['sku']} • {product['name']}\n{rupiah(product['price'])} | Stok: {product['stock']}"
            lines.append(line)
            rows.append([(f"Lihat {product['sku']}", f"product:{product['sku']}")])
        if not products:
            lines.append("Belum ada produk. Tambahkan dengan /addproduk.")
        if query:
            lines.append("Pencarian menampilkan maksimal 8 hasil.")
        else:
            nav = []
            if page:
                nav.append(("⬅ Sebelumnya", f"my_products:{page - 1}"))
            if len(products) == 8:
                nav.append(("Berikutnya ➡", f"my_products:{page + 1}"))
            if nav:
                rows.append(nav)
        rows.append([("➕ Tambah Produk", "addproduct")])
        self.send(actor, "\n".join(lines), keyboard(*rows) if rows else None)

    def product(self, actor, sku):
        product = self.store.product(sku)
        if not product["active"] and actor not in self.config.admins:
            raise ShopError("Produk sedang tidak tersedia.")
        reseller = self.store.user(actor)["role"] == "reseller"
        price = product["reseller_price"] if reseller else product["price"]
        self.send(actor, f"{product['name']} ({product['sku']})\n{product['description']}\n\n"
                  f"Harga {'reseller' if reseller else 'normal'}: {rupiah(price)}\nStok tersedia: {product['stock']}\n\n"
                  f"Untuk memesan:\n/pesan {product['sku']} | 1 | catatan\n\n"
                  "Periksa jumlah sebelum mengirim. Perintah tersebut langsung mereservasi stok.")

    def order_detail(self, actor, order_id):
        order = self.store.order(order_id, actor)
        payment = self.store.payment(order_id, actor)
        text = (f"INVOICE #{order['id']} • {self.config.shop_name}\n"
                f"{order['product_name']} ({order['sku']})\n"
                f"{order['qty']} × {rupiah(order['unit_price'])} = {rupiah(order['total'])}\n"
                f"Status: {STATUS[order['status']]}\nDibuat: {timestamp(order['created_at'])}\n"
                f"Catatan: {order['note']}\n")
        rows = []
        if order["status"] == "pending_payment":
            text += f"\nBatas pembayaran toko: {timestamp(order['expires_at'])}\n"
            if payment:
                text += ("Bayar melalui tombol QRIS Pakasir. Biaya layanan dan nominal akhir ditampilkan di checkout.\n"
                         "Tidak perlu kirim bukti transfer; bot memeriksa pembayaran otomatis.\n")
            else:
                text += f"{self.config.payment}\n\nKirim foto/PDF bukti transfer dengan caption:\n/bukti {order_id}\n"
            rows.append([("Batalkan pesanan", f"cancel_ask:{order_id}")])
        if actor in self.config.admins:
            user = self.store.user(order["user_id"])
            text += f"\n\nPembeli: {user['name']} (@{user['username'] or '-'})\nID: {order['user_id']}"
            if order["status"] == "awaiting_confirmation":
                rows.append([("Lihat bukti", f"proof:{order_id}")])
                rows.append([("✅ Konfirmasi pembayaran", f"approve_ask:{order_id}")])
                text += f"\nTolak bukti: /tolak {order_id} alasan"
            elif order["status"] == "paid":
                rows.append([("✅ Tandai selesai", f"complete:{order_id}")])
        rows.append([("🔄 Refresh", f"order:{order_id}")])
        markup = keyboard(*rows)
        if payment:
            text += f"\n\nMetode: Pakasir QRIS\nReferensi: {payment['reference']}"
            if payment["checked_at"]:
                text += f"\nPemeriksaan terakhir: {timestamp(payment['checked_at'])}"
            if payment["state"] == "late_completed":
                text += "\n⚠ Pembayaran terlambat sudah diterima. Hubungi admin untuk rekonsiliasi/refund; pesanan tidak diproses otomatis."
            if order["status"] == "pending_payment":
                if int(time.time()) < order["expires_at"]:
                    client = self.payment_client(payment)
                    markup["inline_keyboard"].insert(0, [{"text": "💳 Bayar QRIS via Pakasir", "url":
                                                          client.checkout_url(payment["reference"], order["total"])}])
                else:
                    text += "\nTenggat toko terlewati; jangan bayar. Bot sedang memeriksa gateway sebelum melepas stok."
            markup["inline_keyboard"].append([{"text": "🔎 Cek pembayaran", "callback_data": f"check_payment:{order_id}"}])
        self.send(actor, text, markup)

    def list_orders(self, actor, page=0, admin=False, status=None):
        orders = self.store.orders(actor, page, admin, status)
        title = "Semua pesanan" if admin else "Pesanan saya"
        lines = [f"{title} — halaman {page + 1}" + (f" • {STATUS[status]}" if status else "")]
        rows = []
        for order in orders:
            lines.append(f"#{order['id']} • {order['product_name']} × {order['qty']}\n"
                         f"{rupiah(order['total'])} • {STATUS[order['status']]}")
            rows.append([(f"Invoice #{order['id']}", f"order:{order['id']}")])
        if not orders:
            lines.append("Belum ada pesanan.")
        prefix = f"admin_orders:{status or 'all'}" if admin else "orders"
        nav = []
        if page:
            nav.append(("⬅ Sebelumnya", f"{prefix}:{page - 1}"))
        if len(orders) == 8:
            nav.append(("Berikutnya ➡", f"{prefix}:{page + 1}"))
        if nav:
            rows.append(nav)
        self.send(actor, "\n\n".join(lines), keyboard(*rows) if rows else None)

    def dashboard(self, actor, admin=False):
        user = self.store.user(actor)
        if not admin and user["role"] != "reseller":
            self.send(actor, "Program seller memberi Anda akses untuk menjual produk digital.\n"
                      "Kelola produk, stok, dan harga melalui Dashboard Seller di web app.\n"
                      + ("Pengajuan Anda sedang ditinjau admin." if user["reseller_request"] else "Ajukan melalui /daftarreseller."),
                      keyboard([("Ajukan seller", "request_reseller")]) if user["role"] == "customer" and not user["reseller_request"] else None)
            return
        if not admin and user["role"] == "reseller":
            if self.config.webapp_url:
                self.send(actor, "Buka Dashboard Seller untuk mengelola produk Anda:",
                          keyboard([("📊 Dashboard Seller", "dashboard_web")]))
            else:
                self.send(actor, "Dashboard Seller belum tersedia.")
            return
        stats = self.store.stats(actor, admin)
        paid_states = ("paid", "shipped", "completed")
        revenue = sum(stats.get(s, {}).get("total", 0) for s in paid_states)
        saving = sum(stats.get(s, {}).get("saving", 0) for s in paid_states)
        text = "DASHBOARD ADMIN" if admin else "DASHBOARD RESELLER"
        text += "\nRingkasan sepanjang waktu\n\n"
        text += "\n".join(f"{label}: {stats.get(state, {}).get('count', 0)}" for state, label in STATUS.items())
        text += f"\n\n{'Omzet terkonfirmasi' if admin else 'Total belanja terkonfirmasi'}: {rupiah(revenue)}"
        if admin:
            counts = self.store.admin_counts(actor)
            text += (f"\nPengguna: {counts['users']} | Reseller: {counts['resellers']}\n"
                     f"Produk aktif: {counts['products']} | Stok ≤5: {counts['low_stock']}\n"
                     "Omzet bukan laba bersih; belum menghitung biaya/refund.")
            rows = [[("📥 Bukti menunggu", "admin_orders:awaiting_confirmation:0")],
                    [("📦 Semua pesanan", "admin_orders:all:0"), ("🛍 Produk", "products_admin:0")],
                    [("🤝 Pengajuan seller", "requests"), ("📋 Audit", "audit")],
                    [("⚠ Pembayaran terlambat", "payment_issues")],
                    [("Command admin", "help")]]
        else:
            rows = [[("❓ Bantuan", "help")]]
        self.send(actor, text, keyboard(*rows))

    def requests(self, actor):
        requests = self.store.reseller_requests(actor)
        self.send(actor, "PENGAJUAN SELLER (maks. 30; proses lalu refresh)\n\n" + ("\n\n".join(
            f"{user['name']} (@{user['username'] or '-'}) • {user['id']}\n"
            f"Domisili: {user['domisili'] or '-'}\n"
            f"Alamat: {user['alamat_lengkap'] or '-'}\n"
            f"Setujui: /setreseller {user['id']}\nTolak: /cabutreseller {user['id']}"
            for user in requests) or "Tidak ada pengajuan."))

    def audit(self, actor):
        entries = self.store.audit(actor)
        self.send(actor, "AUDIT TERBARU\n\n" + ("\n".join(
            f"{timestamp(row['created_at'])} | {row['actor_id']} | {row['action']} | {row['detail']}"
            for row in entries) or "Belum ada aktivitas."))

    def show_proof(self, actor, order_id):
        order = self.store.order(order_id, actor)
        if self.store.payment(order_id, actor):
            self.check_payment(actor, order_id)
            return
        if actor not in self.config.admins:
            self.send(actor, f"Kirim foto atau PDF dengan caption /bukti {order_id}.\n"
                      "Sensor informasi rekening pribadi yang tidak diperlukan, tetapi jangan ubah jumlah dan referensi transaksi.")
            return
        if not order["proof_file_id"]:
            raise ShopError("Belum ada bukti pembayaran untuk pesanan ini.")
        kind = order["proof_kind"]
        self.api.call("sendPhoto" if kind == "photo" else "sendDocument", chat_id=actor,
                      **{kind: order["proof_file_id"]}, caption=f"Bukti pesanan #{order_id}. "
                      "Periksa mutasi rekening sebelum konfirmasi; gambar bukan jaminan pembayaran.")

    def proof(self, actor, message):
        caption = message.get("caption", "")
        match = re.fullmatch(r"/bukti(?:@[A-Za-z0-9_]+)?\s+([0-9]{1,15})", caption.strip(), re.IGNORECASE)
        if not match:
            raise ShopError("Gunakan caption /bukti ID_PESANAN pada foto atau dokumen bukti transfer.")
        photos = message.get("photo", [])
        kind = "photo" if photos else "document"
        file = photos[-1] if photos else message.get("document", {})
        if file.get("file_size", 0) > 10 * 1024 * 1024:
            raise ShopError("Bukti maksimal 10 MB.")
        if kind == "document" and file.get("mime_type") not in ("application/pdf", "image/jpeg", "image/png"):
            raise ShopError("Format bukti: foto Telegram, JPG, PNG, atau PDF.")
        order_id = int(match[1])
        self.store.submit_proof(actor, order_id, file.get("file_id"), kind)
        self.notify(actor, f"Bukti #{order_id} diterima. Tunggu pemeriksaan admin; jangan transfer ulang.")
        self.notify_admins(f"Bukti pembayaran baru untuk pesanan #{order_id}.",
                           keyboard([("Tinjau invoice dan bukti", f"order:{order_id}")]))

    def change_order(self, actor, order_id, action, detail=""):
        order = self.store.transition(actor, order_id, action, detail)
        self.notify(actor, f"Pesanan #{order_id}: {STATUS[order['status']]}.")
        text = f"Pesanan #{order_id}: {STATUS[order['status']]}."
        if detail:
            text += f"\n{'Alasan penolakan bukti' if action == 'reject' else 'Pengiriman'}: {detail}"
        if action == "reject":
            text += "\nPeriksa invoice untuk tenggat baru dan kirim bukti yang benar. Hubungi admin jika sudah transfer."
        self.notify(order["user_id"], text, keyboard([("Lihat invoice", f"order:{order_id}")]))

    def cancel(self, actor, order_id):
        order = self.store.order(order_id, actor)
        if self.store.payment(order_id, actor):
            self.sync_payment(actor, order_id, cancel=True)
            if self.store.order(order_id, actor)["status"] == "pending_payment":
                self.send(actor, "Gateway belum memastikan pembatalan. Stok tetap ditahan; coba /cekbayar nanti.")
            self.order_detail(actor, order_id)
            return
        self.store.cancel(actor, order_id)
        self.notify(actor, f"Pesanan #{order_id} dibatalkan. Stok sudah dikembalikan.")
        if actor != order["user_id"]:
            self.notify(order["user_id"], f"Pesanan #{order_id} dibatalkan admin.")

    def request_reseller(self, actor):
        self.store.request_reseller(actor)
        self.notify(actor, "Pengajuan seller terkirim. Tunggu persetujuan admin.")
        self.notify_admins(f"Pengajuan seller dari ID {actor}.\nSetujui: /setreseller {actor}\nTolak: /cabutreseller {actor}")

    def command(self, actor, text, update_id):
        parts = text.split(maxsplit=1)
        command = parts[0].split("@")[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        if command == "/start":
            if not self.is_channel_member(actor):
                self.send(actor, f"👋 Anda harus join channel kami terlebih dahulu.\n\n"
                          f"👉 https://t.me/{self.config.channel_username}\n\n"
                          f"Setelah join, klik /start lagi.",
                          {"inline_keyboard": [[{"text": "✅ Cek Join", "callback_data": "check_join"}]]})
                return
            self.menu(actor)
        elif command == "/help" or command == "/bantuan":
            self.help(actor)
        elif command == "/id":
            self.send(actor, f"ID Telegram Anda: {actor}")
        elif command in ("/katalog", "/produkadmin"):
            self.catalog(actor, admin=command == "/produkadmin")
        elif command == "/cari":
            if not arg or len(arg) > 100:
                raise ShopError("Gunakan /cari kata (maks. 100 karakter).")
            self.catalog(actor, query=arg)
        elif command == "/produk":
            self.product(actor, arg)
        elif command == "/pesan":
            values = [value.strip() for value in arg.split("|", 2)]
            if len(values) != 3:
                raise ShopError("Format: /pesan SKU | jumlah | catatan")
            order = self.store.create_order(actor, values[0], number(values[1]), values[2], f"telegram:{update_id}",
                                            self.config.pakasir_project if self.config.provider == "pakasir" else None)
            self.notify_admins(f"Pesanan baru #{order['id']} — {rupiah(order['total'])}.",
                               keyboard([("Lihat pesanan", f"order:{order['id']}")]))
            self.order_detail(actor, order["id"])
        elif command == "/pesanan":
            self.order_detail(actor, number(arg))
        elif command == "/bayar":
            self.order_detail(actor, number(arg))
        elif command == "/cekbayar":
            self.check_payment(actor, number(arg))
        elif command == "/masalahbayar":
            self.payment_issues(actor)
        elif command == "/riwayat":
            self.list_orders(actor)
        elif command == "/batal":
            self.cancel(actor, number(arg))
        elif command == "/bukti":
            self.show_proof(actor, number(arg))
        elif command in ("/admin", "/laporan"):
            self.dashboard(actor, admin=command != "/reseller")
        elif command == "/daftarreseller":
            self.request_reseller(actor)
        elif command == "/orderadmin":
            self.store.require_admin(actor)
            if arg and arg not in STATUS:
                raise ShopError("Status tidak dikenal. Lihat /help untuk daftar status.")
            self.list_orders(actor, admin=True, status=arg or None)
        elif command == "/produkset":
            self.store.require_admin(actor)
            values = [value.strip() for value in arg.split("|", 5)]
            if len(values) != 6:
                raise ShopError("Format: /produkset SKU | nama | harga | harga_reseller | stok_tersedia | deskripsi")
            sku, name, price, reseller_price, stock, description = values
            self.store.save_product(actor, sku, name, number(price), number(reseller_price), number(stock), description)
            self.send(actor, f"Produk {sku.upper()} disimpan. Stok tersedia di-set menjadi {stock}; status aktif sebelumnya tidak diubah.")
        elif command == "/stok":
            self.store.require_admin(actor)
            values = arg.split()
            if len(values) != 2:
                raise ShopError("Format: /stok SKU jumlah_stok_tersedia (bukan penambahan).")
            self.store.set_stock(actor, values[0], number(values[1]))
            self.send(actor, "Stok tersedia diperbarui.")
        elif command in ("/aktif", "/nonaktif"):
            self.store.set_active(actor, arg, command == "/aktif")
            self.send(actor, "Status produk diperbarui.")
        elif command in ("/konfirmasi", "/selesai"):
            self.change_order(actor, number(arg), "approve" if command == "/konfirmasi" else "complete")
        elif command == "/tolak":
            self.store.require_admin(actor)
            values = arg.split(maxsplit=1)
            if len(values) != 2:
                raise ShopError(f"Format: {command} ID alasan")
            self.change_order(actor, number(values[0]), "reject", values[1])
        elif command == "/pengajuan":
            self.requests(actor)
        elif command in ("/setreseller", "/cabutreseller"):
            user_id = number(arg)
            enabled = command == "/setreseller"
            self.store.set_reseller(actor, user_id, enabled)
            self.notify(actor, f"Role pengguna {user_id} diperbarui.")
            self.notify(user_id, "Selamat! Anda sekarang adalah Seller. Buka /start lalu klik Dashboard Seller." if enabled else
                        f"Akses seller dinonaktifkan admin. Hubungi {self.config.support} untuk informasi.")
        elif command == "/lihattiket" and actor in self.config.admins:
            tickets = self.store.open_tickets()
            if not tickets:
                self.send(actor, "Tidak ada tiket terbuka.")
            else:
                lines = ["TIKET TERBUKA:\n"]
                for t in tickets:
                    status_icon = {"open": "🟢", "replied": "💬"}.get(t["status"], "")
                    lines.append(f"#{t['id']} {status_icon} @{t['username'] or '-'} ({t['name']})")
                    lines.append(f"Subjek: {t['subject']}")
                    lines.append(f"Pesan: {t['message'][:150]}")
                    if t["admin_reply"]:
                        lines.append(f"Balasan: {t['admin_reply'][:100]}")
                    lines.append("")
                self.send(actor, "\n".join(lines))
        elif command == "/audit":
            self.audit(actor)
        elif command == "/produk saya":
            self.my_products(actor)
        elif command == "/addproduk":
            values = [value.strip() for value in arg.split("|", 4)]
            if len(values) != 5:
                raise ShopError("Format: /addproduk SKU | nama | harga | stok | deskripsi")
            sku, name, price, stock, description = values
            self.store.save_reseller_product(actor, sku, name, number(price), number(stock), description)
            self.send(actor, f"Produk {sku.upper()} berhasil ditambahkan.")
        elif command == "/hapusproduk":
            if not arg:
                raise ShopError("Format: /hapusproduk SKU")
            self.store.delete_product(actor, arg)
            self.send(actor, f"Produk {arg.upper()} berhasil dihapus.")
        elif command == "/tiket":
            parts2 = arg.split("|", 1)
            if len(parts2) != 2 or not parts2[0].strip() or not parts2[1].strip():
                raise ShopError("Format: /tiket subjek | pesan")
            subject = parts2[0].strip()
            message = parts2[1].strip()
            ticket_id = self.store.create_ticket(actor, subject, message)
            self.send(actor, f"✅ Tiket #{ticket_id} dibuat.\nAdmin akan segera merespon.")
            self.notify_admins(f"🎫 Tiket baru #{ticket_id}\nDari: {actor}\nSubjek: {subject}\nPesan: {message[:200]}")
        elif command == "/lihattiket":
            tickets = self.store.user_tickets(actor)
            if not tickets:
                self.send(actor, "Anda belum memiliki tiket.")
            else:
                lines = ["TIKET ANDA:\n"]
                for t in tickets:
                    status_icon = {"open": "🟢", "replied": "💬", "closed": "🔒"}.get(t["status"], "")
                    lines.append(f"#{t['id']} {status_icon} {t['subject']}\nStatus: {t['status']}")
                    if t["admin_reply"]:
                        lines.append(f"Balasan: {t['admin_reply'][:100]}")
                self.send(actor, "\n".join(lines))
        elif command == "/lihattiket" and actor in self.config.admins:
            pass
        elif command == "/balastiket" and actor in self.config.admins:
            parts2 = arg.split(maxsplit=1)
            if len(parts2) < 2 or not parts2[0].isdigit():
                raise ShopError("Format: /balastiket ID pesan")
            ticket_id = int(parts2[0])
            reply = parts2[1].strip()
            ticket = self.store.reply_ticket(actor, ticket_id, reply)
            self.notify(ticket["user_id"], f"💬 Balasan tiket #{ticket_id}:\n{reply}")
            self.send(actor, f"Tiket #{ticket_id} dibalas.")
        elif command == "/tutuptiket" and actor in self.config.admins:
            if not arg or not arg.isdigit():
                raise ShopError("Format: /tutuptiket ID")
            self.store.close_ticket(actor, int(arg))
            self.send(actor, f"Tiket #{arg} ditutup.")
        elif command == "/tutuptiketuser":
            if not arg or not arg.isdigit():
                raise ShopError("Format: /tutuptiketuser ID")
            ticket_id = int(arg)
            tickets = self.store.user_tickets(actor)
            if not any(t["id"] == ticket_id for t in tickets):
                raise ShopError("Tiket tidak ditemukan atau bukan milik Anda.")
            self.store.close_ticket(actor, ticket_id)
            self.send(actor, f"Tiket #{ticket_id} ditutup.")
        else:
            self.send(actor, "Perintah tidak dikenal. Gunakan /start atau /help.")

    def callback(self, actor, data):
        parts = data.split(":")
        action = parts[0]
        if action == "check_join" and len(parts) == 1:
            if self.is_channel_member(actor):
                self.send(actor, "✅ Verified! Selamat datang.")
                self.menu(actor)
            else:
                self.send(actor, f"❌ Anda belum join channel.\n\n👉 https://t.me/{self.config.channel_username}",
                          {"inline_keyboard": [[{"text": "✅ Cek Join", "callback_data": "check_join"}]]})
        elif action == "create_ticket" and len(parts) == 1:
            self.send(actor, "Untuk membuat tiket, kirim:\n/tiket subjek | pesan Anda\n\nContoh:\n/tiket Pembayaran | Saya sudah transfer tapi belum masuk")
        elif action == "help" and len(parts) == 1:
            self.help(actor)
        elif action in ("admin",) and len(parts) == 1:
            self.dashboard(actor, admin=True)
        elif action == "dashboard_web" and len(parts) == 1:
            if self.config.webapp_url:
                import json as _json
                self.api.call("sendMessage", {
                    "chat_id": actor,
                    "text": "Membuka Dashboard Seller...",
                    "reply_markup": _json.dumps({
                        "inline_keyboard": [[{
                            "text": "📊 Buka Dashboard",
                            "web_app": {"url": self.config.webapp_url + "/dashboard"}
                        }]]
                    })
                })
        elif action == "request_reseller" and len(parts) == 1:
            self.request_reseller(actor)
        elif action == "requests" and len(parts) == 1:
            self.requests(actor)
        elif action == "audit" and len(parts) == 1:
            self.audit(actor)
        elif action == "payment_issues" and len(parts) == 1:
            self.payment_issues(actor)
        elif action == "check_payment" and len(parts) == 2:
            self.check_payment(actor, number(parts[1]))
        elif action == "catalog_back" and len(parts) == 1:
            self.catalog(actor)
        elif action == "catalog" and len(parts) == 3:
            category = parts[1]
            page = number(parts[2])
            if page > 100_000:
                raise ShopError("Halaman di luar batas.")
            self.catalog(actor, page, category=category)
        elif action in ("catalog", "products_admin", "orders") and len(parts) == 2:
            page = number(parts[1])
            if page > 100_000:
                raise ShopError("Halaman di luar batas.")
            if action == "orders":
                self.list_orders(actor, page)
            else:
                self.catalog(actor, page, admin=action == "products_admin")
        elif action == "admin_orders" and len(parts) == 3:
            self.store.require_admin(actor)
            if parts[1] not in (*STATUS, "all"):
                raise ShopError("Filter pesanan tidak valid.")
            page = number(parts[2])
            if page > 100_000:
                raise ShopError("Halaman di luar batas.")
            self.list_orders(actor, page, True, None if parts[1] == "all" else parts[1])
        elif action == "product" and len(parts) == 2:
            self.product(actor, parts[1])
        elif action == "my_products" and len(parts) == 2:
            page = number(parts[1])
            if page > 100_000:
                raise ShopError("Halaman di luar batas.")
            self.my_products(actor, page)
        elif action == "addproduct" and len(parts) == 1:
            self.send(actor, "Format: /addproduk SKU | nama | harga | stok | deskripsi\n\n"
                      "Contoh: /addproduk PROD1 | Produk Saya | 50000 | 10 | Deskripsi produk")
        elif action in ("order", "proof", "cancel_ask", "cancel", "approve_ask", "approve", "complete") and len(parts) == 2:
            order_id = number(parts[1])
            self.store.order(order_id, actor)
            if action == "order":
                self.order_detail(actor, order_id)
            elif action == "proof":
                self.show_proof(actor, order_id)
            elif action == "cancel_ask":
                self.send(actor, f"Yakin membatalkan pesanan #{order_id}? Hanya batalkan jika belum transfer.",
                          keyboard([("Ya, batalkan", f"cancel:{order_id}"), ("Kembali", f"order:{order_id}")]))
            elif action == "approve_ask":
                self.store.require_admin(actor)
                self.send(actor, f"Konfirmasi pesanan #{order_id} hanya jika uang benar-benar sudah masuk ke rekening.",
                          keyboard([("Uang sudah masuk", f"approve:{order_id}"), ("Kembali", f"order:{order_id}")]))
            elif action == "cancel":
                self.cancel(actor, order_id)
            else:
                self.change_order(actor, order_id, action)
        else:
            raise ShopError("Tombol tidak dikenali. Buka /start untuk menu terbaru.")

    def handle(self, update):
        callback = update.get("callback_query")
        message = callback.get("message", {}) if callback else update.get("message", {})
        sender = callback.get("from", {}) if callback else message.get("from", {})
        chat = message.get("chat", {})
        if callback:
            try:
                self.api.call("answerCallbackQuery", callback_query_id=callback["id"])
            except TelegramError:
                pass
        if not sender.get("id") or chat.get("type") != "private" or chat.get("id") != sender["id"]:
            return
        actor = sender["id"]
        self.store.register(actor, sender.get("first_name", "Pengguna"), sender.get("username", ""))
        self.store.expire()
        try:
            if callback:
                self.callback(actor, callback.get("data", ""))
            elif "photo" in message or "document" in message:
                self.proof(actor, message)
            else:
                self.command(actor, message.get("text", ""), update["update_id"])
        except ShopError as exc:
            self.send(actor, str(exc))
        except PakasirError:
            self.send(actor, "Pakasir belum dapat memverifikasi pembayaran. Status tidak diubah; jangan transfer ulang. "
                      "Coba /cekbayar ID nanti atau hubungi admin.")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        load_env()
        config = Config.from_env()
    except (ValueError, OSError) as exc:
        LOG.error("Konfigurasi gagal: %s", exc)
        return 1
    store = Store(config.pg_dsn, config.admins, config.ttl_hours)
    api = Telegram(config.token)
    banner_path = os.getenv("BANNER_PATH", "banner.png")
    try:
        banner_image = Path(banner_path).read_bytes() if Path(banner_path).exists() else b""
    except Exception:
        banner_image = b""
    bot = ShopBot(config, store, api, banner=banner_image)
    try:
        api.call("getMe")
        webhook = api.call("getWebhookInfo")
        if webhook.get("url"):
            LOG.error("Webhook masih aktif. Hapus melalui Bot API deleteWebhook sebelum memakai polling; jangan buang pending updates.")
            return 1
        api.call("setMyCommands", commands=[
            {"command": command, "description": description} for command, description in [
                ("start", "Menu utama"), ("katalog", "Lihat produk"), ("riwayat", "Pesanan saya"),
                ("id", "ID Telegram saya"), ("help", "Panduan command")]])
        for admin in config.admins:
            try:
                api.call("setMyCommands", scope={"type": "chat", "chat_id": admin}, commands=[
                    {"command": command, "description": description} for command, description in [
                        ("admin", "Dashboard admin"), ("orderadmin", "Kelola pesanan"),
                        ("produkadmin", "Kelola produk"), ("pengajuan", "Pengajuan reseller"),
                        ("laporan", "Laporan penjualan"), ("help", "Semua command")]])
            except TelegramError as exc:
                if exc.code not in (400, 403):
                    raise
                LOG.warning("Menu admin belum dapat dipasang. Admin dapat /start lalu /admin; restart untuk memperbarui menu.")
        LOG.info("Bot siap. Provider pembayaran: %s; hentikan dengan Ctrl+C.", config.provider)
        delay = 1
        # ponytail: satu proses polling + SQLite untuk toko kecil; skala multi-worker perlu webhook, antrean, dan PostgreSQL.
        while True:
            try:
                store.expire()
                bot.sync_payments()
                updates = api.call("getUpdates", offset=store.get_offset(), timeout=30,
                                   allowed_updates=["message", "callback_query"])
                for update in updates:
                    try:
                        bot.handle(update)
                    except TelegramError as exc:
                        if exc.code not in (400, 403):
                            raise
                        LOG.warning("Balasan tidak dapat dikirim (kode=%s). Status transaksi tetap tersedia di dashboard.", exc.code)
                    store.set_offset(update["update_id"] + 1)
                delay = 1
            except TelegramError as exc:
                if exc.code in (401, 409):
                    LOG.error("Polling dihentikan (kode=%s). Periksa token, webhook, dan proses bot duplikat.", exc.code)
                    return 1
                LOG.warning("Koneksi Telegram terganggu (kode=%s); mencoba ulang.", exc.code)
                time.sleep(max(delay, min(exc.retry_after, 300)))
                delay = min(delay * 2, 30)
    except KeyboardInterrupt:
        LOG.info("Bot dihentikan.")
    except TelegramError as exc:
        LOG.error("Startup Telegram gagal (kode=%s). Periksa token, jaringan, dan pastikan semua admin telah /start.", exc.code)
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
