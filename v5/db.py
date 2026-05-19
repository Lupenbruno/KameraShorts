"""KameraShorts v5 — SQLite-WAL paylaşımlı state.

Ingest süreçleri segment metadata'sını WRITE,
mixer + shorts-producer + dashboard READ eder.

WAL mode: yazıcı okuyucuları bloklamaz.
"""
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path("/var/lib/kamerashorts/media.db")
_thread_local = threading.local()

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA temp_store = MEMORY;

CREATE TABLE IF NOT EXISTS segments (
    id            TEXT PRIMARY KEY,        -- <city>_<ts>_<seq>
    city          TEXT NOT NULL,           -- ankara, istanbul, corum, konya
    path          TEXT NOT NULL,           -- /var/lib/kamerashorts/<city>/...
    start_ts      INTEGER NOT NULL,        -- unix epoch (s)
    duration_ms   INTEGER NOT NULL,        -- segment süresi
    size_bytes    INTEGER,
    brightness    REAL,                    -- 0-255 mean
    motion        REAL,                    -- adjacent frame diff
    plate         TEXT,                    -- Ankara için otobüs plakası
    vehicle_type  TEXT,                    -- Solo, Korüklü, ELK vb.
    used_at       INTEGER,                 -- shorts'a alındıysa unix ts
    expires_at    INTEGER NOT NULL,        -- TTL cleanup için
    created_at    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_city_latest
    ON segments(city, start_ts DESC);

CREATE INDEX IF NOT EXISTS idx_unused
    ON segments(city, brightness, motion)
    WHERE used_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_expires
    ON segments(expires_at);

CREATE TABLE IF NOT EXISTS used_plates (
    plate         TEXT NOT NULL,
    used_at       INTEGER NOT NULL,
    youtube_url   TEXT,
    PRIMARY KEY (plate, used_at)
);

CREATE INDEX IF NOT EXISTS idx_plates_recent
    ON used_plates(used_at DESC);

CREATE TABLE IF NOT EXISTS service_health (
    service_name  TEXT PRIMARY KEY,
    last_heartbeat INTEGER NOT NULL,
    status        TEXT,                    -- ok, degraded, error
    detail        TEXT
);

CREATE TABLE IF NOT EXISTS upload_log (
    video_id      TEXT PRIMARY KEY,
    city          TEXT NOT NULL,
    title         TEXT,
    youtube_url   TEXT,
    uploaded_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_upload_today
    ON upload_log(city, uploaded_at DESC);
"""


def conn() -> sqlite3.Connection:
    """Thread-local connection (her thread kendi conn'unu açar)."""
    c = getattr(_thread_local, "conn", None)
    if c is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=10)
        c.row_factory = sqlite3.Row
        c.executescript(SCHEMA)
        _thread_local.conn = c
    return c


def add_segment(
    seg_id: str, city: str, path: str, start_ts: int, duration_ms: int,
    size_bytes: int = 0, brightness: float = 128.0, motion: float = 0.0,
    plate: str = "", vehicle_type: str = "",
    ttl_seconds: int = 3600,
) -> None:
    """Ingest segment metadata yazar."""
    now = int(time.time())
    conn().execute(
        """INSERT OR REPLACE INTO segments
           (id, city, path, start_ts, duration_ms, size_bytes,
            brightness, motion, plate, vehicle_type,
            expires_at, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (seg_id, city, path, start_ts, duration_ms, size_bytes,
         brightness, motion, plate, vehicle_type,
         now + ttl_seconds, now),
    )


def latest_segments(city: str, limit: int = 50) -> list[sqlite3.Row]:
    """Mixer: en yeni N segment, kronolojik."""
    return list(conn().execute(
        """SELECT * FROM segments
           WHERE city = ? AND expires_at > ?
           ORDER BY start_ts DESC LIMIT ?""",
        (city, int(time.time()), limit),
    ))


def good_candidates(
    city: str, min_bright: float = 50.0, min_motion: float = 5.0,
    since_ts: Optional[int] = None, limit: int = 20,
) -> list[sqlite3.Row]:
    """Shorts: kullanılmamış, parlak, hareketli segmentler."""
    since = since_ts if since_ts is not None else int(time.time()) - 1800
    return list(conn().execute(
        """SELECT * FROM segments
           WHERE city = ? AND used_at IS NULL
             AND brightness >= ? AND motion >= ?
             AND start_ts >= ? AND expires_at > ?
           ORDER BY (brightness + motion * 5) DESC LIMIT ?""",
        (city, min_bright, min_motion, since, int(time.time()), limit),
    ))


def mark_used(seg_id: str, youtube_url: str = "") -> None:
    """Shorts uyarısı kullanılan segment için."""
    conn().execute(
        "UPDATE segments SET used_at = ? WHERE id = ?",
        (int(time.time()), seg_id),
    )


def record_plate(plate: str, youtube_url: str = "") -> None:
    """Ankara plaka dedup."""
    if not plate:
        return
    conn().execute(
        "INSERT INTO used_plates(plate, used_at, youtube_url) VALUES (?,?,?)",
        (plate, int(time.time()), youtube_url),
    )


def recent_plates(hours: int = 24) -> set[str]:
    """Son N saatte kullanılmış plakalar."""
    cutoff = int(time.time()) - hours * 3600
    rows = conn().execute(
        "SELECT DISTINCT plate FROM used_plates WHERE used_at >= ?",
        (cutoff,),
    )
    return {r[0] for r in rows}


def heartbeat(service: str, status: str = "ok", detail: str = "") -> None:
    """Servis kendi sağlığını rapor eder (dashboard okur)."""
    conn().execute(
        """INSERT OR REPLACE INTO service_health
           (service_name, last_heartbeat, status, detail)
           VALUES (?, ?, ?, ?)""",
        (service, int(time.time()), status, detail),
    )


def log_upload(video_id: str, city: str, title: str, url: str) -> None:
    """Quota tracking için."""
    conn().execute(
        """INSERT OR REPLACE INTO upload_log
           (video_id, city, title, youtube_url, uploaded_at)
           VALUES (?,?,?,?,?)""",
        (video_id, city, title, url, int(time.time())),
    )


def uploads_today(city: str) -> int:
    """Bugünkü upload sayısı (TR saatine göre, 03:00 UTC = 00:00 TR)."""
    # 24h pencere yerine basitleştirme: son 24 saat
    cutoff = int(time.time()) - 86400
    row = conn().execute(
        "SELECT COUNT(*) FROM upload_log WHERE city = ? AND uploaded_at >= ?",
        (city, cutoff),
    ).fetchone()
    return row[0] if row else 0


def cleanup_expired() -> int:
    """TTL'i geçmiş segmentleri DB'den sil. Dosyalar cleaner.py işi."""
    cur = conn().execute(
        "DELETE FROM segments WHERE expires_at < ?",
        (int(time.time()),),
    )
    return cur.rowcount


if __name__ == "__main__":
    # Schema migration test
    c = conn()
    print(f"DB: {DB_PATH}, journal_mode:",
          c.execute("PRAGMA journal_mode").fetchone()[0])
    print("Tables:", [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")])
