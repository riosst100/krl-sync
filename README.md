# krl-sync

Sync jadwal KRL dari API web KCI (`www.kci.id/api/krl`), lalu hasilnya dikirim ke
**api-krl.inovasionline.com** (my-krl / krl-app). Project ini di-deploy ke **Vercel**.

Ini jalur cadangan untuk berjaga-jaga kalau Cloudflare memblokir IP VPS krl-app: yang
mengambil data dari KCI adalah Vercel (IP berbeda, region Singapura), dan VPS cukup
menerima hasilnya.

```
Vercel Cron (otomatis) ─┐
Halaman web (manual)  ──┴─► /api/sync ─► kci.id (curl_cffi, sidik jari browser)
                               │
                               ├─► GET  api-krl…/api/v1/ingest/config   (stasiun + on/off sync otomatis dari admin krl-app)
                               └─► POST api-krl…/api/v1/ingest/{stations,start,schedules,stops,finish}
```

## Halaman web (`/`)

Buka `https://<project>.vercel.app/`, lalu masuk dengan `CRON_SECRET`. Secret disimpan di
`sessionStorage` tab itu saja.

- **Konfigurasi**: stasiun yang di-sync dan sumbernya (setting admin krl-app, atau env
  cadangan), status dan jadwal sync otomatis, jenis data, serta tujuan pengiriman.
- **Sync manual**: tombol *Sync sekarang*, dengan opsi stasiun (kosong = ikut
  konfigurasi), jenis data, batas kereta untuk uji, dan *dry run*. Sync biasa **jalan di
  background** seperti cron (`?background=1`): Vercel membuka run di api-krl, lalu rantai
  invocation melanjutkannya sendiri. Tab boleh ditutup. Progres dilaporkan ke api-krl
  (`POST /ingest/progress`) setiap langkah, dan halaman membacanya tiap 5 detik
  (`?status=1` → `GET /ingest/runs/latest`), termasuk run dari cron. *Hentikan* menutup run
  di api-krl dengan status `failed`; Vercel berhenti di langkah berikutnya. *Dry run* tidak
  membuat run di api-krl, jadi tetap dijalankan per langkah oleh browser (tab harus terbuka).
- **Cek koneksi KCI**: 1 request stasiun + 1 request jadwal dari Vercel. Hasilnya
  menunjukkan apakah Vercel diblokir Cloudflare.

## Konfigurasi stasiun & sync otomatis

Daftar stasiun **mengikuti setting admin krl-app** (Sinkronisasi → Sync Data → stasiun),
yang dibaca dari `GET $INGEST_URL/config` setiap kali sync dimulai. Kalau endpoint itu
belum ada atau gagal, dipakai env `KCI_SYNC_STATIONS` sebagai cadangan. Halaman web
menunjukkan sumber mana yang sedang dipakai.

**Sync otomatis** = Vercel Cron (`vercel.json`, default `30 17 * * *` UTC = **00:30 WIB**).
Cron bisa dimatikan tanpa redeploy lewat `auto_sync: false` di `/ingest/config`.
Contohnya: selama VPS krl-app tidak diblokir, jalur Vercel dimatikan supaya tidak sync dua
kali; begitu VPS diblokir, dinyalakan dari admin. Tanpa endpoint itu, yang dipakai env
`AUTO_SYNC` (default `true`). Sync manual dari halaman web tetap bisa jalan walau sync
otomatis mati.

## Logika sync = krl-app

Port langsung dari backend krl-app, dengan aturan yang sama:

| krl-app | di sini |
| --- | --- |
| `KciService::parseStationList` (baris area `WIL0 / AREA …` dibuang, nama di-title-case) | `parse.parse_stations` |
| `KciService::parseSchedules` (validasi per baris, baris invalid dilewati, unik per kereta) | `parse.parse_schedules` |
| `TrainStopSyncService::parse` | `parse.parse_stops` |
| `KciUrlClient` + sidecar `kci-fetch` (curl_cffi chrome → safari → firefox, deteksi blokir Cloudflare, retry HTTP 429 sesuai `Retry-After`, jeda saat `X-RateLimit-Remaining` hampir habis, jeda antar request) | `kci.KciClient` |
| `ScheduleSyncService` / `TrainStopSyncService` (stasiun gagal → partial; kereta gagal di-retry sekali; berhenti dini setelah 10 kegagalan tanpa satu pun sukses; diblokir → seluruh run berhenti) | `runner.Runner` |

Nama variabel `KCI_*` dan nilai default-nya sama dengan `backend/config/kci.php`.

## Jeda antar request (anti-blokir)

Request ke KCI dibuat mirip orang yang sedang browsing:

- **satu per satu**, tidak paralel (`KCI_TRAIN_STOPS_CONCURRENCY=1`);
- **jeda acak 3–8 detik** antar request (`KCI_REQUEST_DELAY_MS=3000-8000`), dihitung dari
  selesainya request sebelumnya;
- **istirahat acak 30–90 detik** setiap 20–40 request (`KCI_BREAK_EVERY`, `KCI_BREAK_SECONDS`).
  Istirahat dilewati kalau tidak selesai sebelum batas waktu invocation.

Rata-ratanya sekitar 5 request per menit, jauh di bawah batas KCI (60 per menit). Jeda
tetap 1 menit tidak dipakai karena terlalu lambat: THB saja punya 360 kereta (6 jam).
Dengan pengaturan ini, THB selesai sekitar 1–1,5 jam dan semua stasiun sekitar 4–5 jam.

## Batas waktu Vercel: run dipecah

Satu sync bisa makan waktu lama. Di Tanah Abang saja ada 360 kereta, KCI butuh 3–7 detik
per kereta, dan dibatasi 60 request per menit. Itu jauh melebihi batas 300 detik per
invocation Vercel, jadi satu run adalah state machine:

```
stations → schedules (1 stasiun per unit) → stops (8 kereta per unit) → finish → done
```

Setiap invocation bekerja selama `SYNC_TIME_BUDGET_SECONDS` (default 150 detik). Kalau
belum selesai, ia mengirim state-nya (JSON) dengan `POST /api/sync` ke invocation
berikutnya, lalu berhenti menunggu setelah 10 detik. Invocation berikutnya tetap jalan
dan melanjutkan dari titik yang sama. Progres tiap langkah terlihat di **Vercel → Logs**
(`[krl-sync] …`).

## Deploy ke Vercel

1. Push folder ini ke repo Git, lalu **Import Project** di vercel.com. Bisa juga lewat
   `npx vercel` dari folder ini. Tidak perlu build step: Vercel mendeteksi
   `api/sync.py` + `requirements.txt` (Python 3.12, lihat `.python-version`).
2. **Settings → Environment Variables** (lihat `.env.example`):

   | Variabel | Isi |
   | --- | --- |
   | `CRON_SECRET` | string acak panjang (`openssl rand -hex 32`). Vercel Cron otomatis mengirim `Authorization: Bearer $CRON_SECRET` |
   | `INGEST_URL` | `https://api-krl.inovasionline.com/api/v1/ingest` |
   | `INGEST_TOKEN` | token yang sama dengan `SYNC_INGEST_TOKEN` di backend krl-app |
   | `KCI_SYNC_STATIONS` | cadangan kalau `/ingest/config` tidak tersedia, mis. `THB`. Kosong = semua stasiun yang aktif di KCI (`fg_enable`) |
   | `AUTO_SYNC` | cadangan on/off sync otomatis (default `true`) |
   | `KCI_SYNC_TYPES` | `stations,schedules,trains` (default) |

3. Deploy ulang setelah env diisi.

**Jadwal cron** ada di `vercel.json` (`30 17 * * *` UTC = **00:30 WIB**). Di paket
**Hobby**, cron hanya boleh sekali sehari dan bisa meleset sampai satu jam. Di paket Pro,
jadwal bisa lebih sering dan lebih presisi.

**Region** `sin1` (Singapura), supaya dekat dengan KCI. Ubah `regions` di `vercel.json`
kalau perlu.

## Menjalankan / menguji

Cara termudah adalah lewat halaman web. Lewat curl, semua request butuh header
`Authorization: Bearer $CRON_SECRET`.

```bash
URL=https://<project>.vercel.app/api/sync
AUTH="Authorization: Bearer $CRON_SECRET"

# 1. Apakah Vercel bisa menembus Cloudflare KCI? (1 request stasiun + 1 request jadwal, tidak mengirim apa pun)
curl -H "$AUTH" "$URL?check=1&station=THB"

# 2. Dry run: ambil dari KCI seperti biasa, tapi tidak mengirim ke api-krl
curl -H "$AUTH" "$URL?dry=1&stations=THB&limit_trains=5"

# 3. Sync sungguhan sekarang (sama seperti cron)
curl -H "$AUTH" "$URL"
```

Parameter GET: `dry=1`, `types=schedules,trains`, `stations=THB,SUD`, `limit_trains=N`,
`check=1[&station=THB]`, `info=1` (konfigurasi efektif), `chain=0&budget=N` (kembalikan
state setelah ±N detik, untuk dilanjutkan sendiri dengan `POST {"state":…,"chain":false}`,
seperti yang dilakukan halaman web). Respons berisi ringkasan (`status`, `progress`, `stats`, contoh
kegagalan), plus `continued` kalau run diteruskan ke invocation berikutnya. Kode HTTP
500 berarti run gagal.

Status akhir: `success`, `partial` (sebagian stasiun/kereta gagal), `failed`, atau
`skipped` (api-krl membalas 409 karena sedang menjalankan sync lain).

### Lokal (tanpa batas waktu)

```bash
uv venv && uv pip install -r requirements.txt pytest      # atau python -m venv + pip
cp .env.example .env                                        # isi INGEST_TOKEN dst.
.venv/bin/python -m krl_sync --dry --stations THB --limit-trains 3
.venv/bin/python -m krl_sync                                # sync penuh + kirim
.venv/bin/python -m pytest
```

### Docker (lokal)

```bash
cp .env.example .env          # isi INGEST_TOKEN dst.
docker compose up -d --build  # http://localhost:3500
docker compose exec sync python -m pytest
```

`docker/server.py` meniru routing Vercel (`/` → `index.html`, `/api/sync` → handler).
Compose memasang `NO_AUTH=true`, jadi halaman langsung terbuka tanpa `CRON_SECRET`. Ini
aman selama port tetap terikat ke `127.0.0.1`. Vercel Cron tidak ikut jalan di Docker.

`python -m krl_sync` menjalankan satu run penuh dalam satu proses. Cara ini juga bisa
dipakai sebagai cadangan lain (mis. GitHub Actions terjadwal) kalau rantai invocation
Vercel bermasalah.

## Kontrak endpoint penerima (api-krl)

Endpoint ini ada di krl-app (`IngestController`, dikembalikan di commit `dd9e9a6`), dijaga
middleware `ingest` dengan `SYNC_INGEST_TOKEN`.

**Tidak ada truncate.** Saat `start`, krl-app menyalin jadwal terakhir tiap stasiun (dan stops
tiap kereta) ke tanggal run kalau tanggal itu belum punya data. Setiap push hanya mengganti
yang dikirim: jadwal satu stasiun (tanggal itu dan tanggal-tanggal lama stasiun itu), atau
stops satu kereta. Stasiun dan kereta lain tidak disentuh, dan `finish` tidak menghapus apa
pun. Aturan yang sama berlaku untuk semua sumber sync di krl-app. Stasiun atau kereta yang gagal di-sync tetap memakai data lama. Command
`schedules:carry-forward` di krl-app melakukan hal yang sama setiap hari pukul 00:01 WIB,
jadi data tetap ada walau Vercel tidak jalan.

Semua request memakai JSON dan `Authorization: Bearer <INGEST_TOKEN>`. Urutannya:

| # | Endpoint | Body | Balasan yang diharapkan |
| --- | --- | --- | --- |
| 0 | `GET config` (**opsional**) | – | `{"data":{"stations":["THB","SUD"],"auto_sync":true}}`. Kalau gagal atau tidak ada, dipakai env |
| 1 | `stations` (per 200 baris, kalau `stations` di-sync) | `{"stations":[{"code":"THB","name":"Tanah Abang","operational_area":0,"kci_enabled":true,"is_active":true}]}` | 2xx |
| 2 | `start` | `{"date":"2026-10-06","stations":["THB"]}` | `201 {"data":{"run_id":123}}`, atau **409** kalau sedang ada sync |
| 3 | `schedules` (sekali per stasiun) | `{"run_id":123,"date":"…","station":"THB","schedules":[{"train_number":"5701C","line_name":"COMMUTER LINE CIKARANG","line_color":"#0084D8","route_name":"KAMPUNGBANDAN-CIKARANG","destination":"CIKARANG","departure_time":"04:31:00","destination_arrival_time":"05:40:00"}]}` | 2xx. **422** = stasiun itu dilewati (dicatat gagal), run tetap jalan |
| 4 | `stops` (per ≤8 kereta) | `{"run_id":123,"date":"…","trains":[{"train_number":"5701C","stops":[{"station_code":"THB","time":"04:31:00","is_transit":false}]}]}` | 2xx |
| 5 | `finish` | `{"run_id":123,"date":"…","status":"success\|partial\|failed","stations":["THB"],"error":null}` | 2xx |

`config` mengembalikan setting admin "stasiun yang di-sync" (kosong = semua stasiun aktif)
dan `auto_sync` dari setting `kci.ingest_auto_sync` (default `true`). Belum ada tombolnya di
admin. Untuk sementara ubah lewat tinker: `Setting::put(Setting::INGEST_AUTO_SYNC, 'false')`.

`destination` dikirim apa adanya dari KCI (`JAKARTAKOTA`). Penerima memetakannya ke nama
stasiun (`ScheduleSyncService::persist`). Untuk 5xx atau error koneksi, request dicoba
ulang 3× dengan jeda 2 detik. Error 4xx lain (selain 409/422) menghentikan run, dan
pengirim tetap mencoba memanggil `finish` dengan status `failed`.

## Catatan

- **IP Vercel juga bisa diblokir.** Sidik jari TLS-nya sudah benar, tapi Cloudflare bisa
  saja menilai IP datacenter AWS lebih ketat. Uji dulu dengan `?check=1` setelah deploy.
  Kalau diblokir, hasilnya `"blocked": true`.
- **Run yang terputus.** Rantai invocation mengandalkan Vercel yang tetap menjalankan
  invocation berikutnya walau pemanggilnya sudah berhenti menunggu. Kalau suatu saat
  rantai putus (lihat Logs), run di api-krl tertinggal berstatus `running` sampai
  dianggap stale oleh krl-app, dan cron berikutnya memulai run baru.
- Deployment Protection Vercel tidak menghalangi rantai, karena step berikutnya dipanggil
  lewat domain produksi (`VERCEL_PROJECT_PRODUCTION_URL`). Jika memakai domain lain, isi
  `SELF_URL`.
