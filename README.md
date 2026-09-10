# Bot toko Telegram + Pakasir

Bot jual-beli **Python 3.10+ / SQLite** dengan dashboard admin dan reseller di chat Telegram. Tanpa library runtime tambahan. Pembayaran default memakai **checkout QRIS resmi Pakasir** dan verifikasi status melalui API; tidak perlu domain atau server webhook.

## Fitur

- Pembeli: katalog berhalaman, pencarian, harga sesuai role, detail produk, invoice, catatan tujuan/alamat, riwayat pesanan, tombol bayar QRIS, cek status, pembatalan yang diperiksa ke gateway.
- Admin: produk/stok, dashboard omzet dan status pesanan, stok menipis, pengiriman/resi, penyelesaian, persetujuan reseller, audit, daftar pembayaran terlambat yang memerlukan rekonsiliasi.
- Reseller: pengajuan, harga beli khusus, riwayat sendiri, dashboard belanja dan penghematan. Harga di invoice tidak berubah ketika harga produk atau role diperbarui.
- Pembayaran Pakasir: referensi acak unik per invoice, nominal dari database, checkout tanpa API key, pengecekan otomatis saat bot berjalan, tombol `/cekbayar`, notifikasi ketika terkonfirmasi.
- Stok direservasi secara transaksional. Update Telegram berulang tidak membuat pesanan/reservasi ganda. Konfirmasi pembayaran berulang tidak menggandakan perubahan stok atau omzet.
- Hak admin berasal dari `ADMIN_IDS`; command dan callback tetap memeriksa otorisasi. Hanya chat privat yang dilayani.
- Mode manual tetap tersedia secara eksplisit, termasuk unggah bukti foto/PDF dan pemeriksaan admin. Tidak ada fallback manual otomatis ketika Pakasir bermasalah.

## Instalasi

1. Buat bot lewat **@BotFather** menggunakan `/newbot`.
2. Daftar/login di https://app.pakasir.com, buat proyek, lalu catat **Slug** dan **API Key** dari halaman detail proyek. Gunakan proyek mode **Sandbox** saat pengujian pertama.
3. Salin dan edit konfigurasi:

   ```sh
   cp .env.example .env
   ```

   Windows: `copy .env.example .env`, atau salin lewat editor.

   | Variabel | Isi |
   | --- | --- |
   | `TELEGRAM_BOT_TOKEN` | Token asli dari BotFather |
   | `ADMIN_IDS` | ID Telegram numerik; pisahkan koma untuk beberapa admin |
   | `SHOP_NAME` | Nama toko, maksimal 100 karakter |
   | `PAYMENT_PROVIDER` | `pakasir` (default), atau `manual` jika dipilih secara sengaja |
   | `PAKASIR_PROJECT` | Slug proyek Pakasir, bukan nama tampilan atau URL |
   | `PAKASIR_API_KEY` | API key rahasia dari proyek yang sama |
   | `PAYMENT_INSTRUCTIONS` | Kosongkan untuk Pakasir; wajib berisi instruksi rekening jika mode manual |
   | `SUPPORT_CONTACT` | Kontak admin, misalnya `@admin_toko` |
   | `DATABASE_PATH` | Default `data/shop.sqlite3`; gunakan disk persisten |
   | `ORDER_TTL_HOURS` | Tenggat toko 1–168 jam, default 24 |

   Ganti nilai contoh sebelum menjalankan. Environment OS lebih diprioritaskan daripada `.env`. Parser mendukung `KEY=value`, kutip luar opsional, dan komentar di baris tersendiri; tanpa interpolasi variabel.
4. Dari folder proyek jalankan:

   ```sh
   python3 bot.py
   ```

   `python3 main.py` juga menjalankan bot yang sama. Pada Windows gunakan `python`. Tidak perlu `pip install`.
5. Kirim `/start` ke bot dari semua akun admin agar notifikasi bisa diterima.

   **Belum tahu ID sendiri?** Untuk penyiapan awal saja, gunakan sementara `ADMIN_IDS=123456789`, jalankan bot, lalu kirim `/id` ke chat bot. Hentikan, ganti dengan ID asli yang ditampilkan, dan jalankan ulang. Jangan menerima transaksi sebelum konfigurasi admin dan proyek pembayaran benar.
6. Tambahkan produk dari akun admin:

   ```text
   /produkset KAOS01 | Kaos polos | 75000 | 60000 | 20 | Ukuran M, warna hitam. Konfirmasikan ongkir sebelum membayar.
   ```

### Docker (opsional)

Jika host Anda memiliki Docker Compose:

```sh
mkdir -p data
docker compose up -d --build
docker compose logs -f bot
```

`.env` diberikan saat runtime, tidak dimasukkan ke image. Folder `./data` dipasang ke `/app/data` untuk persistensi SQLite. Biarkan `DATABASE_PATH=data/shop.sqlite3`, atau sesuaikan volume bila mengubah path. Jalankan hanya satu container/proses untuk satu token. Container memerlukan akses keluar HTTPS ke Telegram dan Pakasir, bukan port masuk.

## Pembayaran Pakasir

Contoh pembelian:

```text
/katalog
/produk KAOS01
/pesan KAOS01 | 2 | Budi, alamat lengkap, nomor kontak, ukuran M
```

`/pesan` langsung membuat invoice dan mereservasi stok. Satu invoice berisi satu SKU, maksimal 1000 unit; maksimal 5 invoice belum dibayar/diperiksa per akun. Nominal adalah rupiah bulat, tanpa titik/koma pada input.

1. Pembeli membuka invoice dan menekan **Bayar QRIS via Pakasir**.
2. Halaman resmi `app.pakasir.com` membuat/menampilkan pembayaran untuk referensi invoice tersebut. QRIS ditampilkan oleh Pakasir, bukan dikirim sebagai gambar buatan bot.
3. Pembeli membayar **nominal akhir yang ditampilkan checkout**, termasuk biaya layanan Pakasir. Bot tidak mengasumsikan fee tetap. Total invoice/omzet bot hanya harga produk, bukan biaya layanan.
4. Bot memeriksa **Transaction Detail API** secara berkala. Pembeli juga dapat menekan **Cek pembayaran** atau `/cekbayar ID`.
5. Pesanan menjadi `paid` hanya ketika API mengembalikan `status=completed`, dengan `project`, `order_id`, dan `amount` persis cocok dengan catatan invoice. Redirect browser, pesan pengguna, dan gambar bukti bukan bukti pembayaran gateway.
6. Admin mengirim barang: `/kirim ID JNE REG resi ABC123`, lalu `/selesai ID` setelah diterima.

Tombol `/bayar ID` membuka kembali invoice dan referensi yang sama; tidak membuat invoice baru. Biaya ongkir/pajak tidak dihitung otomatis—konfirmasikan sebelum membayar. Jangan mengirim OTP/password/data kartu ke bot.

### Polling, pembatalan, dan pembayaran terlambat

- Tidak ada listener webhook. Bot memeriksa maksimal **3 invoice jatuh tempo per putaran** long polling Telegram; interval normal tiap invoice sekitar **60 detik**, dapat lebih lama saat antrean/jaringan sibuk. Cek manual menggunakan cache hasil sukses selama 10 detik. Bot harus tetap berjalan untuk pembaruan otomatis.
- Pembuatan invoice hanya menyimpan referensi dan link lokal; transaksi gateway dimulai oleh halaman checkout resmi. Status `not_found` sebelum checkout dibuka belum berarti pembayaran gagal.
- `/batal ID` memeriksa API terlebih dahulu. Jika pembayaran sudah masuk, pesanan menjadi lunas, bukan dibatalkan. Jika transaksi ada, bot meminta `transactioncancel` lalu memeriksa detail lagi; respons HTTP sukses saja tidak dianggap bukti pembatalan.
- Di tenggat toko, bot juga memeriksa gateway sebelum melepas stok. Status terverifikasi `canceled`/`cancelled`/`expired`, atau transaksi yang belum ditemukan (HTTP 404), memungkinkan penutupan. Status tidak dikenal, error jaringan, respons tidak cocok, atau pembatalan yang masih pending **menahan stok**, lalu diperiksa kembali. Tenggat di checkout dapat berbeda dengan tenggat toko; ikuti yang lebih awal.
- Jangan membayar invoice yang sudah dibatalkan/kedaluwarsa atau menggunakan link lamanya. Link checkout pihak ketiga yang sudah tersalin tidak bisa ditarik dari perangkat pengguna. Invoice tertutup tetap direkonsiliasi berkala (sekitar **1 jam**, tanpa batas waktu selama database dan bot tetap berjalan).
- Jika pembayaran masuk setelah stok dilepas, invoice diberi flag **pembayaran terlambat**, admin/pembeli diberi notifikasi, dan `/masalahbayar` menampilkannya. **Tidak otomatis mengirim barang, mengambil stok kembali, atau menambah omzet pesanan lunas.** Admin harus memeriksa dana lalu menangani pemenuhan/refund secara manual di luar bot. Daftar ini adalah catatan insiden, belum memiliki tombol resolusi/refund.
- Jangan mengganti slug proyek ketika masih ada invoice terbuka/tertutup yang perlu direkonsiliasi. Bot menyimpan slug pada setiap invoice dan menolak memakai proyek berbeda. Rotasi API key dalam proyek yang sama diperbolehkan. Jika beralih ke mode manual, tetap simpan kredensial proyek lama agar rekonsiliasi gateway berjalan.

```text
Pakasir: pending_payment ──API completed──> paid ──kirim──> shipped ──selesai──> completed
              └──pemeriksaan pembatalan/kedaluwarsa──> cancelled / expired
                                                         └──dana masuk belakangan──> flag rekonsiliasi manual
```

## Command

### Pembeli dan reseller

| Command | Fungsi |
| --- | --- |
| `/start`, `/help`, `/id` | Menu, bantuan, ID Telegram |
| `/katalog`, `/cari kata`, `/produk SKU` | Katalog, pencarian, detail |
| `/pesan SKU \| jumlah \| tujuan/alamat dan catatan` | Buat invoice dan reservasi stok |
| `/riwayat`, `/pesanan ID` | Riwayat dan invoice |
| `/bayar ID`, `/cekbayar ID` | Buka pembayaran / cek status Pakasir |
| `/batal ID` | Pembatalan sebelum lunas, dengan pemeriksaan gateway untuk Pakasir |
| `/daftarreseller`, `/reseller` | Ajukan akses / dashboard reseller |
| Caption `/bukti ID` pada foto/file | Hanya invoice manual |

### Admin

| Command | Fungsi |
| --- | --- |
| `/admin`, `/laporan` | Dashboard dan omzet sepanjang waktu |
| `/produkadmin` | Semua produk termasuk nonaktif |
| `/produkset SKU \| nama \| harga \| harga_reseller \| stok_tersedia \| deskripsi` | Tambah/edit produk |
| `/stok SKU jumlah` | **SET** stok tersedia, bukan penambahan |
| `/aktif SKU`, `/nonaktif SKU` | Aktif/nonaktif tanpa menghapus riwayat |
| `/orderadmin [status]` | Pesanan seluruh pembeli, filter opsional |
| `/pesanan ID`, `/cekbayar ID` | Detail invoice dan verifikasi Pakasir |
| `/masalahbayar` | Maksimal 30 pembayaran terlambat terbaru |
| `/kirim ID informasi_pengiriman` | Tandai dikirim dan simpan resi |
| `/selesai ID` | Selesaikan pesanan yang sudah dikirim |
| `/pengajuan` | Permintaan reseller |
| `/setreseller ID`, `/cabutreseller ID` | Setujui, tolak, atau cabut role reseller |
| `/audit` | 15 aksi terbaru |
| `/bukti ID`, `/konfirmasi ID`, `/tolak ID alasan` | Pemeriksaan pembayaran **manual saja** |

Status filter: `pending_payment`, `awaiting_confirmation`, `paid`, `shipped`, `completed`, `cancelled`, `expired`.

Admin tidak mendapat harga reseller; gunakan akun terpisah untuk pengujian. Penghematan reseller bukan komisi, saldo, atau laba bersih; penjualan ke pelanggan akhir dikelola sendiri. Katalog/riwayat memiliki tombol halaman; pencarian maksimal 8 hasil, gunakan kata yang lebih spesifik.

**Stok tersedia tidak termasuk reservasi.** Jika stok tersedia 8 dan ada pesanan 2 unit, pembatalan mengembalikannya menjadi 10. Saat `/produkset` atau `/stok`, jangan memasukkan unit yang sedang direservasi sebagai stok tersedia. Mengedit produk nonaktif tidak otomatis mengaktifkannya kembali.

## Mode manual dan database lama

Untuk memilih transfer manual, set `PAYMENT_PROVIDER=manual` dan isi `PAYMENT_INSTRUCTIONS`. Pembeli mengirim foto/JPG/PNG/PDF maksimal 10 MB dengan caption `/bukti ID`. Admin memeriksa mutasi rekening lalu `/konfirmasi ID`, atau `/tolak ID alasan`. Penolakan membuka tenggat pembayaran baru. Bukti menunggu pemeriksaan tidak kedaluwarsa/dibatalkan sendiri.

Pesanan Pakasir **tidak bisa dilunaskan lewat `/konfirmasi` atau bukti gambar**, bahkan oleh admin. Pesanan manual lama tetap manual; mengubah konfigurasi hanya memengaruhi pesanan baru. Tabel pembayaran ditambahkan otomatis tanpa menghapus tabel/data lama. Backup database sebelum upgrade.

## Pengujian

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile bot.py store.py pakasir.py main.py setup.py
```

Test memakai SQLite sementara dan API tiruan, tanpa kredensial/jaringan. Cakupan meliputi stok/transaksi, role, command/callback, kontrak HTTP Pakasir, validasi nominal/referensi, pembayaran berulang, pembatalan yang berlomba dengan pembayaran, kegagalan API, dan pembayaran terlambat.

Sebelum produksi, jalankan uji **Pakasir Sandbox** dengan akun admin dan pembeli terpisah: buat pesanan, buka checkout, gunakan fitur/API resmi `paymentsimulation` dari lingkungan pengujian yang aman, lalu periksa status lunas dan stok. Jangan menaruh key pada perintah/log yang dibagikan. Bot tidak mengekspos command simulasi pembayaran. Pastikan proyek bukan Sandbox sebelum menerima transaksi sungguhan. Test lokal tidak membuktikan token, key, checkout live, penerimaan dana, notifikasi Telegram langsung, atau image Docker telah bekerja pada host Anda.

**Validasi kontrak pembatalan di Sandbox:** dokumentasi resmi mencontohkan `completed`, tetapi belum merinci semua status terminal atau format transaksi tidak ditemukan. Implementasi memperlakukan HTTP 404 sebagai `not_found` serta menerima `canceled`/`cancelled`/`expired` sebagai status penutupan. Pastikan kredensial/proyek salah tidak menghasilkan respons yang sama dengan transaksi yang memang tidak ada, dan periksa hasil sesudah pembatalan sebelum mengaktifkan toko produksi. Respons lain menahan stok; pembayaran setelah penutupan tetap masuk antrean rekonsiliasi, bukan pemenuhan otomatis.

## Operasional dan privasi

- Satu proses/token/database. Gunakan pengelola proses OS atau Compose, direktori kerja tetap, disk persisten, backup, dan akses keluar HTTPS. Tidak perlu port publik.
- Jika token pernah memakai webhook Telegram, hapus melalui Bot API `deleteWebhook` **tanpa** `drop_pending_updates=true`. Jangan menjalankan bot lama bersamaan. Kode 409 umumnya konflik proses/webhook; 401 umumnya token tidak valid.
- `.env`, database, dan backup berisi data sensitif. Batasi izin (`chmod 600 .env` di Linux). `.gitignore` dan `.dockerignore` mengecualikan secret/data. Untuk lokasi database kustom, pastikan tidak ikut Git/image.
- Nama, ID Telegram, alamat, informasi pengiriman, referensi pembayaran, dan `file_id` bukti manual tersimpan tanpa enkripsi di SQLite. Batasi akses admin/server dan tetapkan kebijakan retensi. Tidak ada penyimpanan API key Pakasir di database; jangan aktifkan log URL HTTP karena API detail mengirim key di query sesuai kontrak resmi.
- File bukti manual tetap di Telegram. Validasi MIME/ukuran bukan pemindaian malware. Jangan kirim OTP/password atau detail rekening yang tidak diperlukan.
- Backup konsisten ketika WAL aktif menggunakan API SQLite, bukan menyalin satu file database:

  ```sh
  mkdir -p data/backups
  python3 -c "import sqlite3; s=sqlite3.connect('file:data/shop.sqlite3?mode=ro', uri=True); d=sqlite3.connect('data/backups/shop.sqlite3'); s.backup(d); d.close(); s.close()"
  ```

  Sesuaikan path jika diperlukan. Simpan backup terenkripsi di lokasi terlindungi. Untuk restore: hentikan bot, simpan database lama beserta `-wal`/`-shm`, lalu pulihkan ke direktori bersih. Jangan mencampur WAL lama dengan backup. Restore backup lama dapat kehilangan invoice terbaru; cocokkan dengan riwayat transaksi Pakasir sebelum membuka toko kembali.
- Notifikasi best-effort, tanpa antrean kirim ulang: kegagalan Telegram tidak membatalkan pencatatan pembayaran. Periksa dashboard/audit rutin. Update dapat dibaca ulang setelah gangguan; invoice idempoten, tetapi balasan/audit tertentu dapat berulang.
- Omzet hanya total produk pesanan `paid`, `shipped`, `completed`, bukan laba bersih dan tidak mencakup biaya gateway/ongkir/refund. Refund belum dicatat sebagai pembukuan otomatis.
- Cakupan: satu toko, satu SKU per invoice, QRIS hosted checkout, pengiriman manual. Belum ada keranjang multi-produk, kalkulasi ongkir/pajak, kupon, dompet/withdraw, komisi afiliasi, refund otomatis, pengiriman lisensi otomatis, ekspor laporan, dashboard web, atau webhook.
- `ponytail:` SQLite + polling dipilih untuk toko kecil satu proses. Rekonsiliasi invoice lama bertambah seiring pemakaian; volume tinggi perlu worker/antrean dan webhook yang tetap diverifikasi ke API, lalu PostgreSQL untuk multi-worker. Batas lima pesanan per akun bukan perlindungan anti-abuse lintas akun.

## Referensi

- [Dokumentasi resmi Pakasir](https://pakasir.com/p/docs), ditinjau 8 September 2026 (halaman diperbarui 7 September 2026): bagian B/B.2 hosted checkout QRIS, C.5 pembatalan, E Transaction Detail API.
- [Telegram Bot API](https://core.telegram.org/bots/api).
