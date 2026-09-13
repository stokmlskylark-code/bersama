# Bot Auto Order Apps Premium (Telegram + Midtrans)

Satu file `bot.py`, **tanpa pip install** (stdlib Python 3.10+, SQLite). Tidak perlu webhook/server publik:
bot mengecek status transaksi ke API Midtrans setiap 20 detik dan lewat tombol **Cek Pembayaran**.

## Jalankan

```sh
cp .env.example .env   # isi token bot, ADMIN_IDS, MIDTRANS_SERVER_KEY
python3 bot.py
```

## Alur pembeli
`/start` → pilih produk (tombol) → **Beli 1/2/3** → tombol **Bayar Sekarang** (Midtrans Snap) →
setelah settlement, item stok (akun/lisensi) dikirim otomatis ke chat. Pesanan kedaluwarsa mengembalikan stok.

## Admin
`/admin` → panel omzet + daftar produk → pilih produk → tombol **Restok / Harga / Deskripsi / Hapus**,
atau **Tambah Produk** dengan format `sku | Nama | harga | deskripsi`.
Restok: kirim satu item per baris (misal `email:password`); setiap baris = 1 stok yang dikirim ke pembeli.
