# KameraShorts — Mimari Notları

**Tarih:** 2026-05-19
**Branch:** master (v4-stable)
**Amaç:** Bu sistemde HANGİ tasarımın çalıştığını, hangisinin BAŞARISIZ olduğunu, NEDEN olduğunu kayıt altına almak. Gelecekte kim bu kodu okursa aynı hatalara düşmesin.

---

## 1. v4 — ÜRETİMDE BAŞARILI MİMARİ

### Akış şeması

```
[Kamera HLS kaynakları]
  ├─ Ankara EGO API (~50 otobüs)
  ├─ İBB Turistik (23 kamera)
  ├─ Çorum Belediye (9 kamera)
  └─ Konya OVH (8 kamera)
        │
        ▼ paralel HLS dl
┌─────────────────────────────────────┐
│  live_streamer.py — BatchBuilder    │
│  ┌────────────────────────────────┐ │
│  │ CityCollector (paralel × 4)    │ │
│  │ HLS segment → temp .ts dosyalar│ │
│  └────────────┬───────────────────┘ │
│               ▼                      │
│  ┌────────────────────────────────┐ │
│  │ transcode_city() — NORMALIZE   │ │
│  │ Her şehri AYNI codec/PMT'ye:   │ │
│  │ libx264 ultrafast 1280x720@20  │ │
│  │ drawtext = şehir + hava        │ │
│  │ -> /tmp/.../Ankara.ts          │ │
│  │ -> /tmp/.../Istanbul.ts        │ │
│  │ -> /tmp/.../Corum.ts           │ │
│  │ -> /tmp/.../Konya.ts           │ │
│  └────────────┬───────────────────┘ │
│               ▼                      │
│  ┌────────────────────────────────┐ │
│  │ ffmpeg concat-remux:           │ │
│  │ -f concat -i list.txt -c copy  │ │
│  │ -> batch_NNNN.ts (~250 MB)     │ │
│  │ Tüm şehirler tek uniform mpegts│ │
│  └────────────┬───────────────────┘ │
└─────────────────┼───────────────────┘
                  ▼
        Queue (maxsize=1)
                  │
                  ▼
┌─────────────────────────────────────┐
│  StreamManager — TEK FFmpeg         │
│                                      │
│  ┌────────────────────────────────┐ │
│  │ Writer thread:                 │ │
│  │ - SCHED_FIFO prio=50           │ │
│  │ - clock_nanosleep (~1μs jitter)│ │
│  │ - 32KB chunk pump              │ │
│  │ - batch_NNNN.ts -> FIFO        │ │
│  └────────────┬───────────────────┘ │
│               ▼ named pipe          │
│  ┌────────────────────────────────┐ │
│  │ ffmpeg -i stream.pipe          │ │
│  │  -i music/playlist.txt (loop)  │ │
│  │  -c:v libx264 ultrafast 2500k  │ │
│  │  -f flv rtmp://localhost:1935  │ │
│  │ → MediaMTX                     │ │
│  └────────────────────────────────┘ │
└─────────────────────────────────────┘
                  ▼
            MediaMTX :1935
            (tee, onfail=ignore)
                  │
            ┌─────┴─────┐
            ▼           ▼
        YouTube      Kick
```

### Niye çalıştı?

**1. Heterojenliği inputta değil, ortada çözüyor**
- Farklı kameraların HLS kaynakları farklı codec/fps/PMT'ye sahip (kaçınılmaz)
- `transcode_city()` her şehri **AYNI** uniform formata (libx264 1280×720@20fps mpegts) çevirir
- Sonra concat-remux yaparken tüm parçalar **codec uyumlu**, mpegts demuxer sync kaybı yok
- batch_NNNN.ts **tek monolitik dosya** — şehir geçişi sadece içerikte, container'da değil

**2. Stream FFmpeg HİÇ kapanmaz**
- Tek FFmpeg session ömrü uzun (saatler boyunca)
- Input: named pipe (FIFO) — writer thread besler
- Şehir geçişlerinde sadece FIFO içeriği değişir, FFmpeg'in dosya kapatması/açması yok
- MediaMTX publisher kopmaz → tee FFmpeg ölmez → YouTube/Kick reconnect derdi yok

**3. Writer rate-control hassasiyetli**
- SCHED_FIFO real-time scheduling (preempt edilmez)
- `clock_nanosleep` (~1μs jitter, Python `time.sleep` ~10ms)
- 32KB chunk yazma — pipe asla boşalmaz
- Filler 5s siyah ekran batch'ler arası geçişlerde (RTMP session canlı kalsın)

**4. Failure isolation**
- Batch hazırlama (BatchBuilder) ve streaming (StreamManager) bağımsız
- Bir kamera düşse CityCollector retry yapar, batch yine oluşur
- Batch hazırlanamazsa filler oynar, stream session ayakta kalır

### Üretim metrikleri (kanıt)

`/opt/KameraShorts/logs/pipeline.log` — 18-19 Mayıs 2026:
- 24 saatte **15 başarılı Ankara Shorts upload** (saatlik schedule)
- 25 başarı / 50 attempt = %50 YOLO geçiş oranı
- 17 farklı plaka (dedup çalışıyor)

---

## 2. v5 — BAŞARISIZ MİMARİ (ve nedenleri)

v5'i "düşük donanımlı sunucu için optimize edelim" diye tasarladım. Mikroservis ayrımı, transcode'u tek noktaya çekme, SQLite paylaşımlı state. **Teorik olarak iyi**, ama **üretimde çalışmadı**.

### v5 akış şeması

```
[Kamera HLS]
      │
      ▼
┌─────────────────────────┐
│ Ingest @ <city> (×4)    │
│ HLS dl, TRANSCODE YOK!   │  ← KRİTİK HATA BURADA
│ Raw .ts segments         │
│ → /var/lib/.../seg.ts    │
│ → SQLite metadata        │
└───────────┬─────────────┘
            ▼
   SQLite + disk segments
            │
            ▼ DB query
┌─────────────────────────┐
│ Mixer (tek process)      │
│ Writer thread:           │
│   DB'den segment çek     │
│   FIFO'ya pump et        │
│   drawtext = şehir       │
│ FFmpeg:                  │
│   -i FIFO (mpegts)       │
│   transcode + overlay    │
│   → MediaMTX             │
└─────────────────────────┘
```

### Denenmiş 4 farklı pattern — hepsi başarısız

#### Pattern 1: Per-city FFmpeg + restart
```python
for city in city_order:
    proc = subprocess.Popen([ffmpeg, "-f", "concat", "-i", f"{city}.txt", ...])
    proc.wait()  # 180s sonra biter
```
**Hata:** Her şehir geçişinde mixer FFmpeg ölür → MediaMTX `live/stream` path 1-3s "not ready" → mediamtx'in spawn ettiği tee FFmpeg `SIGINT (signal 2)` ile öldü → YouTube/Kick reconnect gecikmesi → kullanıcı tarafından "yayın offline" gözüktü.

#### Pattern 2: Tek FFmpeg + tüm şehirler tek concat
```python
# 4 şehri tek concat dosyasında, drawtext'ler enable='between(t,start,end)'
ffmpeg -i full_concat.txt -vf "drawtext=...:enable='between(t,0,180)',
                                drawtext=...:enable='between(t,180,360)'..."
```
**Hata:** 9-12 dakika sonra FFmpeg bitiyor, yeni concat dosyası için restart gerekti → aynı tee-öldürme sorunu daha düşük frekansta yaşanmaya devam etti.

#### Pattern 3: Tek FFmpeg + FIFO + writer thread (v4 pattern'i taklit)
```python
# Tek FFmpeg sürekli açık, Python writer FIFO'ya segment'leri raw byte pump
ffmpeg -re -f mpegts -i stream.pipe ...
def writer():
    for city in cycle(cities):
        for segment in city_segments:
            with open(seg, "rb") as f:
                fifo.write(f.read())
```
**Hata:** Şehir geçişinde **mpegts demuxer sync kaybediyor**. Farklı kameraların PMT/PCR yapıları farklı. FFmpeg `-fflags +genpts+discardcorrupt+nobuffer+igndts` flag'leri yetmedi. fps düşüyor (20→0), frame counter donuyor.

#### Pattern 4: Pre-transcode subprocess + h264 annexb
```python
def _pump_segment(seg):
    proc = subprocess.Popen([ffmpeg, "-i", seg, "-c", "copy",
                             "-bsf:v", "h264_mp4toannexb",
                             "-f", "h264", "pipe:1"])
    while chunk := proc.stdout.read(65536):
        fifo.write(chunk)
```
**Hata:** Her segment için subprocess açılıyor → CPU overhead patladı → ana mixer FFmpeg speed 0.4x'e düştü → MediaMTX timeout → tee öldü.

### Kök hata: "Transcode-Less Ingest" yanlış prensipti

v5'i tasarlarken şöyle düşündüm:
> "Ingest sadece HLS download yapsın, transcode'u mixer tarafına bırakalım. CPU verimli."

**Bu yanlıştı çünkü:**

1. **Heterojen kaynak problemini ortadan bir yere taşıyor değil, gizliyordu.** Kameralar farklı format → ingest diske farklı format yazar → mixer farklı format'lı segment'leri concat etmek zorunda kalır → FFmpeg mpegts demuxer KIRILIR.

2. **v4 bu sorunu transcode_city() ile çözmüştü** — her şehri batch öncesi normalize ediyor, batch içinde monolitik. Ben bu adımı atladığım için sorunu mixer'ın kucağına attım.

3. **Mixer'da pre-transcode denemek geç müdahale** — overhead patlatır, CPU bottleneck, FIFO starve.

### v5'in iyi yönleri (saklamaya değer)

| Bileşen | Niye iyi |
|---|---|
| SQLite-WAL paylaşımlı state | Log-grep yerine SQL query. Persistent. |
| Lazy YOLO subprocess | RAM 772 MB → 0 (saatte 30s peak 400 MB) |
| systemd MemoryMax cgroup | OOM riski sıfır |
| Dashboard zenginleştirme | HERO kart, alarm bar, timeline, diagnose |
| diagnose.py CLI | 11-bölüm sistem taraması, JSON/short/section |
| Mikroservis ayrımı | Cgroup limits, bağımsız restart, izolasyon |

**v5 → master'a aktarılabilecek parçalar:**
- Dashboard zenginleştirme (live_dashboard.py)
- diagnose.py adapt edilirse v4 için de çalışır
- secrets.env ayrımı (config.yaml plaintext riski)
- systemd unit'lere TimeoutStopSec=30 + KillMode=mixed

---

## 3. Tasarım Kuralları (gelecek çalışmalar için)

### ✓ DO

1. **Heterojen kaynakları MUTLAKA INPUT katmanında normalize et.** v4'ün transcode_city() patterns'ı doğru — concat'tan önce libx264 ile uniform format'a çevir.
2. **Tek FFmpeg sürekli açık** — şehir/içerik geçişlerinde session kapatma. FIFO + writer thread pattern v4'te kanıtlandı.
3. **Writer thread real-time scheduling** — SCHED_FIFO + clock_nanosleep, jitter düşük olmazsa FIFO underrun olur.
4. **MediaMTX publisher session sürekliliği KRİTİK** — tee FFmpeg publisher kopunca ölür, reconnect 5-30s alır. Yayın kopmasın istiyorsan publisher SÜREKLİ açık tut.
5. **Failure isolation** — batch hazırlama ve streaming bağımsız thread/proc. Biri çökse diğeri devam etsin.

### ✗ DON'T

1. **Ingest'i transcode-less yapma** — disk'te heterojen mpegts birikir, mixer'da concat sync kaybeder.
2. **Per-segment subprocess pre-transcode** — CPU overhead patlar, kabul edilemez (~10-15× normal).
3. **Per-city FFmpeg + restart on switch** — şehir geçişlerinde tee FFmpeg ölür, YouTube/Kick reconnect gecikmesi.
4. **mpegts FIFO'ya raw byte pump (farklı kaynaklardan)** — demuxer sync kaybeder, fps 0'a düşer.
5. **Filter chain'i çok karmaşıklaştırma** — v4'ün ilk denemesi `fps+setpts+drawtext+aevalsrc+aresample` zincirle deadlock yapıyordu. Sadeleştirildi (v4.4 minimal cmd).

### ⚠ İSTİSNALAR

- **Per-city pattern OK eğer:** sadece tek kaynak veya kameranın HLS URL'sini doğrudan okuyorsan (FFmpeg HLS demuxer'ı kaynaktan sync alır). v4 `87df478` (LiveController) bunu yapıyordu — çalışıyordu ama batch+pipe pattern'inden daha az kararlıydı (her şehir geçişinde 2-3s gap).

---

## 4. v4'te İyileştirme Adayları (gelecek refactor)

### A. CPU (transcode peak)

**Sorun:** BatchBuilder'da 4 paralel transcode_city = peak 200% CPU (batch süresince ~120s).

**Çözüm:**
- `ThreadPoolExecutor max_workers=1` (paralel→sıralı transcode) — peak CPU yarıya iner, batch süresi 2× olur ama streamer'a CPU bırakır.
- Bu zaten `live_streamer.py:957` satırında uygulanmış (`max_workers=1` comment'i ile)

### B. RAM (harvester YOLO yükü)

**Sorun:** `harvester.py` 772 MB RSS — YOLO modeli sürekli yüklü, saatte 1 kez 30s kullanılıyor.

**Çözüm:** v5'teki lazy YOLO subprocess pattern'i adapt et:
```python
# harvester.produce_ankara_direct içinde
result = subprocess.run([
    sys.executable, "-m", "src.yolo_check",
    "--clip", clip_path, "--duration", "40"
], capture_output=True, timeout=120)
# subprocess çıkışta RAM serbest
```
RAM tasarrufu: 772 MB → ~50 MB sürekli, 400 MB peak/saat 30s.

### C. Stop-timeout güvenlik

**Sorun:** Dünkü 12 saatlik sessizlik — systemd `stop-sigterm timed out` (default 90s) sonrası SIGKILL → unit "failed" state → `Restart=always` manuel stop sonrası restart yapmaz.

**Çözüm:** `/etc/systemd/system/kamerashorts-live.service`:
```ini
TimeoutStopSec=30
KillMode=mixed     # ana process SIGTERM, child'lar SIGKILL
```
30 saniyede temiz kapanış, 12 saat sessizlik tekrarlanmaz.

### D. Secrets

**Sorun:** `config.yaml` ve `mediamtx.yml` içinde YouTube stream key, Kick RTMPS, Telegram token, OWM API key plaintext.

**Çözüm:** `/etc/kamerashorts/secrets.env` (chmod 600), systemd unit'lere `EnvironmentFile=`.

### E. Dashboard zenginleştirme

**Sorun:** Mevcut `live_dashboard.py` log-tail-parse — yavaş, log dönerken kayıp.

**Çözüm:** v5 dashboard'undan kopya — HERO kart, alarm bar, diagnose button. SQLite tutmak istemezsek yine log-tail kalır, sadece UI iyileştir.

### F. Diğer şehirler için Shorts (opsiyonel)

**Mevcut:** Sadece Ankara saatlik Shorts. İstanbul/Çorum/Konya disabled.

**Çözüm:** harvester.py'de `produce_landscape()` zaten var — config'de `istanbul_times`, `corum_times`, `konya_times` schedule'e ekle. **NOT:** YouTube quota tek token = 6 video/gün limit, 4 şehir × 6 = 24 quota yetmez. Multi-token rotation gerek.

### G. Konya HLS parse fix

**Sorun:** v5'te Konya ingest segment alamadığı için skip ediyordu. v4'te `direct_random` URL parse sorunsuz olabilir — kontrol gerek.

### H. Logrotate

**Sorun:** `/opt/KameraShorts/logs/pipeline.log` her zaman büyüyor — 1.2 MB var şu an, yıllar geçtikçe GB'lar olur.

**Çözüm:** `/etc/logrotate.d/kamerashorts` haftalık rotate + 4 kopya saklama.

---

## 5. Kararlar Özeti

| Karar | Tarih | Sonuç |
|---|---|---|
| v4 (8403820) batch+pipe + Ankara direct | 18 Mayıs | ✅ Üretim çalıştı 24h, 15 upload |
| v5 microservices (transcode-less ingest) | 19 Mayıs öğle | ❌ mpegts sync sorunu, 0 upload |
| 4 farklı mixer pattern denendi | 19 Mayıs öğleden sonra | ❌ Hiçbiri stabil değil |
| v4'e rollback (master branch) | 19 Mayıs akşam | ✅ Yayın geri geldi |

**Geriye dönüp baktığımda:** v5'i denemeden önce v4 üzerinde **dashboard zenginleştirme + secrets + stop-timeout fix + lazy YOLO** gibi nokta atışı iyileştirmeler yapsaydım çok daha hızlı kazanım olurdu. v5 microservices "mimari saplantı" idi — gerçek darboğaza odaklanmak yerine.

---

## 6. Kaynak Dosyalar

- v4 mevcut kodu: `master` branch (`8403820`)
- v5 (referans, kullanılmıyor): `v5-microservices` branch (`aa98e95`)
- v4 önceki LiveController versiyonu: commit `87df478` (per-city FFmpeg restart pattern)
- Üretim kanıtı: `/opt/KameraShorts/logs/pipeline.log` (15 upload, 18-19 Mayıs)
- Stats: `/opt/KameraShorts/data/harvester_stats.json` (25 success, 17 plate)
