"""Хранилище приложения: суда, позиции, визиты в порты и журнал алертов.

SQLite в режиме WAL: один файл ship_tracker.db, к нему можно подключаться
DB Browser for SQLite, пока программа работает.
"""

import sqlite3
import threading
from datetime import datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS vessels (
    mmsi        TEXT PRIMARY KEY,
    name        TEXT,
    imo         TEXT,
    callsign    TEXT,
    type_code   INTEGER,
    type_label  TEXT,
    destination TEXT,
    draft       REAL,
    length_m    REAL,
    beam_m      REAL,
    draft_min   REAL,
    draft_max   REAL,
    eta_declared TEXT,
    lat         REAL,
    lon         REAL,
    sog         REAL,
    cog         REAL,
    heading     INTEGER,
    port        TEXT,
    updated_at  TEXT,
    first_seen  TEXT
);
CREATE INDEX IF NOT EXISTS idx_vessels_seen ON vessels (updated_at);

CREATE TABLE IF NOT EXISTS positions (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    mmsi TEXT NOT NULL,
    ts   TEXT NOT NULL,
    lat  REAL,
    lon  REAL,
    sog  REAL,
    cog  REAL
);
CREATE INDEX IF NOT EXISTS idx_positions_mmsi_ts ON positions (mmsi, ts);

CREATE TABLE IF NOT EXISTS port_visits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mmsi       TEXT NOT NULL,
    port       TEXT NOT NULL,
    entered_at TEXT NOT NULL,
    left_at    TEXT,
    lat        REAL,
    lon        REAL,
    draft_in   REAL,
    draft_out  REAL,
    cargo_kind TEXT,
    source     TEXT
);
CREATE INDEX IF NOT EXISTS idx_visits_mmsi ON port_visits (mmsi, entered_at);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at     TEXT NOT NULL,
    key         TEXT NOT NULL,
    mmsi        TEXT,
    name        TEXT,
    origin      TEXT,
    destination TEXT,
    ok          INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    message     TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_key ON alerts (key, sent_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Трафик в море: все суда района с позицией, даже чужие для наших портов.
-- Живёт отдельно от vessels: на карту и в списки «наших» судов не влияет.
CREATE TABLE IF NOT EXISTS sea_traffic (
    mmsi        TEXT PRIMARY KEY,
    name        TEXT,
    type_label  TEXT,
    destination TEXT,
    sog         REAL,
    cog         REAL,
    lat         REAL,
    lon         REAL,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sea_traffic_seen ON sea_traffic (updated_at);
"""

FIELDS = ("name", "imo", "callsign", "type_code", "type_label", "destination", "draft",
          "length_m", "beam_m",
          "eta_declared")

# Колонки, добавленные позже: старые базы дополняются при открытии.
MIGRATIONS = {
    "vessels": (("eta_declared", "TEXT"), ("length_m", "REAL"), ("beam_m", "REAL"),
                ("draft_min", "REAL"), ("draft_max", "REAL"), ("tracked", "INTEGER")),
    "port_visits": (("draft_in", "REAL"), ("draft_out", "REAL"),
                    ("cargo_kind", "TEXT"), ("source", "TEXT")),
}


def iso(when=None) -> str:
    return (when or datetime.now()).isoformat(timespec="seconds")


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


class Store:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock, self.conn:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self):
        """Добавляет недостающие колонки в базу, созданную прошлой версией."""
        for table, columns in MIGRATIONS.items():
            existing = {row["name"] for row in
                        self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, sql_type in columns:
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")

    # ------------------------------------------------------------------ суда
    def upsert_static(self, mmsi: str, **values):
        """Сохраняет статические данные судна, не затирая известное пустыми полями."""
        now = iso()
        data = {key: values[key] for key in FIELDS if values.get(key) not in (None, "")}
        try:
            draft = float(values.get("draft")) if values.get("draft") not in (None, "") else None
        except (TypeError, ValueError):
            draft = None
        with self._lock, self.conn:
            row = self.conn.execute("SELECT mmsi FROM vessels WHERE mmsi = ?", (mmsi,)).fetchone()
            if row is None:
                columns = ["mmsi", "first_seen", "updated_at", *data.keys()]
                params = [mmsi, now, now, *data.values()]
                placeholders = ", ".join("?" for _ in columns)
                self.conn.execute(
                    f"INSERT INTO vessels ({', '.join(columns)}) VALUES ({placeholders})", params)
            elif data:
                assignments = ", ".join(f"{key} = ?" for key in data)
                self.conn.execute(
                    f"UPDATE vessels SET {assignments}, updated_at = ? WHERE mmsi = ?",
                    [*data.values(), now, mmsi])
            if draft:
                # диапазон осадок нужен для оценки груза: полная осадка
                # судна — это максимум того, что оно передавало
                self.conn.execute(
                    "UPDATE vessels SET draft_min = MIN(COALESCE(draft_min, ?), ?), "
                    "draft_max = MAX(COALESCE(draft_max, ?), ?) WHERE mmsi = ?",
                    (draft, draft, draft, draft, mmsi))

    def upsert_position(self, mmsi: str, lat: float, lon: float, sog=None, cog=None,
                        heading=None, port=None, when=None):
        ts = iso(when)
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO vessels (mmsi, lat, lon, sog, cog, heading, port, updated_at, first_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(mmsi) DO UPDATE SET
                       lat = excluded.lat, lon = excluded.lon, sog = excluded.sog,
                       cog = excluded.cog, heading = excluded.heading, port = excluded.port,
                       updated_at = excluded.updated_at""",
                (mmsi, lat, lon, sog, cog, heading, port, ts, ts),
            )

    def add_position(self, mmsi: str, lat: float, lon: float, sog=None, cog=None, when=None):
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO positions (mmsi, ts, lat, lon, sog, cog) VALUES (?, ?, ?, ?, ?, ?)",
                (mmsi, iso(when), lat, lon, sog, cog))

    def vessel(self, mmsi: str):
        with self._lock:
            row = self.conn.execute("SELECT * FROM vessels WHERE mmsi = ?", (mmsi,)).fetchone()
        return dict(row) if row else None

    def vessels(self, limit=500):
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM vessels ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    # --------------------------------------------------------- трафик в море
    def save_sea_traffic(self, rows) -> int:
        """Последние позиции чужих для портов судов (для вкладки «В море»)."""
        rows = [row for row in (rows or []) if row.get("mmsi")]
        if not rows:
            return 0
        with self._lock, self.conn:
            self.conn.executemany(
                "INSERT INTO sea_traffic (mmsi, name, type_label, destination, sog, cog,"
                " lat, lon, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(mmsi) DO UPDATE SET name=excluded.name,"
                " type_label=excluded.type_label, destination=excluded.destination,"
                " sog=excluded.sog, cog=excluded.cog, lat=excluded.lat, lon=excluded.lon,"
                " updated_at=excluded.updated_at",
                [(str(row.get("mmsi")), row.get("name"), row.get("type_label"),
                  row.get("destination"), row.get("sog"), row.get("cog"),
                  row.get("lat"), row.get("lon"), row.get("updated_at") or iso())
                 for row in rows])
        return len(rows)

    def sea_traffic(self, hours=12.0, limit=2000):
        since = iso(datetime.now() - timedelta(hours=float(hours)))
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM sea_traffic WHERE updated_at >= ?"
                " ORDER BY updated_at DESC LIMIT ?", (since, limit)).fetchall()
        return [dict(row) for row in rows]

    def prune_sea_traffic(self, keep_hours=48.0) -> int:
        since = iso(datetime.now() - timedelta(hours=float(keep_hours)))
        with self._lock, self.conn:
            cur = self.conn.execute("DELETE FROM sea_traffic WHERE updated_at < ?", (since,))
            return cur.rowcount

    def track(self, mmsi: str, hours=24):
        since = iso(datetime.now() - timedelta(hours=float(hours)))
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts, lat, lon, sog, cog FROM positions WHERE mmsi = ? AND ts >= ? "
                "ORDER BY ts",
                (mmsi, since)).fetchall()
        return [dict(row) for row in rows]

    def last_position_time(self, mmsi: str):
        with self._lock:
            row = self.conn.execute(
                "SELECT ts FROM positions WHERE mmsi = ? ORDER BY id DESC LIMIT 1", (mmsi,)).fetchone()
        return parse_iso(row["ts"]) if row else None

    # ---------------------------------------------------------------- визиты
    def open_visit(self, mmsi: str):
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM port_visits WHERE mmsi = ? AND left_at IS NULL "
                "ORDER BY id DESC LIMIT 1", (mmsi,)).fetchone()
        return dict(row) if row else None

    def start_visit(self, mmsi: str, port: str, lat, lon, when=None, draft=None,
                    cargo_kind=None, source="live"):
        now = iso(when)
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE port_visits SET left_at = ? WHERE mmsi = ? AND left_at IS NULL", (now, mmsi))
            self.conn.execute(
                "INSERT INTO port_visits (mmsi, port, entered_at, lat, lon, draft_in, "
                "cargo_kind, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (mmsi, port, now, lat, lon, draft, cargo_kind, source))

    def import_visit(self, mmsi: str, port: str, entered_at, left_at=None, draft_in=None,
                     draft_out=None, cargo_kind=None, source="track") -> bool:
        """Добавляет визит, найденный по истории трека, если такого ещё нет."""
        entered = iso(entered_at)
        left = iso(left_at) if left_at else None
        with self._lock, self.conn:
            exists = self.conn.execute(
                "SELECT 1 FROM port_visits WHERE mmsi = ? AND port = ? AND entered_at = ?",
                (mmsi, port, entered)).fetchone()
            if exists:
                if left:
                    self.conn.execute(
                        "UPDATE port_visits SET left_at = COALESCE(left_at, ?) "
                        "WHERE mmsi = ? AND port = ? AND entered_at = ?",
                        (left, mmsi, port, entered))
                return False
            self.conn.execute(
                "INSERT INTO port_visits (mmsi, port, entered_at, left_at, draft_in, draft_out, "
                "cargo_kind, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (mmsi, port, entered, left, draft_in, draft_out, cargo_kind, source))
            return True

    def visits(self, limit=300, mmsi=None, port=None):
        """Все визиты в порты: кто, когда пришёл/ушёл и сколько стоял."""
        query = "SELECT * FROM port_visits"
        params = []
        conditions = []
        if mmsi:
            conditions.append("mmsi = ?")
            params.append(str(mmsi))
        if port:
            conditions.append("UPPER(port) = ?")
            params.append(str(port).strip().upper())
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY entered_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def visit_ports(self) -> dict:
        """MMSI → порты, в которых судно было (по всем визитам в базе)."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT mmsi, UPPER(port) AS port FROM port_visits").fetchall()
        result = {}
        for row in rows:
            result.setdefault(str(row["mmsi"]), set()).add(row["port"])
        return result

    def visit_counts(self) -> dict:
        """Сколько визитов и сколько судов было в каждом нашем порту."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT UPPER(port) AS port, COUNT(*) AS visits,"
                " COUNT(DISTINCT mmsi) AS vessels, MAX(entered_at) AS last"
                " FROM port_visits GROUP BY UPPER(port)").fetchall()
        return {row["port"]: {"visits": int(row["visits"]),
                              "vessels": int(row["vessels"]),
                              "last": row["last"]} for row in rows}

    def has_visits(self, mmsi: str) -> bool:
        """Было ли судно хотя бы раз в одном из наших портов."""
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM port_visits WHERE mmsi = ? LIMIT 1", (str(mmsi),)).fetchone()
        return row is not None

    def set_tracked(self, mmsi: str, tracked=True):
        """Отмечает судно как относящееся к нашим портам (или снимает отметку)."""
        with self._lock, self.conn:
            self.conn.execute("UPDATE vessels SET tracked = ? WHERE mmsi = ?",
                              (1 if tracked else 0, str(mmsi)))

    def drop_vessel(self, mmsi: str) -> bool:
        """Удаляет судно и все его следы: позиции, визиты, привязку алертов."""
        mmsi = str(mmsi)
        with self._lock, self.conn:
            found = self.conn.execute(
                "SELECT 1 FROM vessels WHERE mmsi = ?", (mmsi,)).fetchone() is not None
            self.conn.execute("DELETE FROM positions WHERE mmsi = ?", (mmsi,))
            self.conn.execute("DELETE FROM port_visits WHERE mmsi = ?", (mmsi,))
            self.conn.execute("DELETE FROM vessels WHERE mmsi = ?", (mmsi,))
        return found

    def close_visit(self, mmsi: str, when=None, draft_out=None) -> bool:
        now = iso(when)
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE port_visits SET left_at = ?, draft_out = COALESCE(?, draft_out) "
                "WHERE mmsi = ? AND left_at IS NULL", (now, draft_out, mmsi))
            return cur.rowcount > 0

    def last_departure(self, mmsi: str, window_hours: float = 96):
        """Последний порт, из которого судно реально вышло (по нашим наблюдениям)."""
        since = iso(datetime.now() - timedelta(hours=float(window_hours)))
        with self._lock:
            row = self.conn.execute(
                """SELECT port, left_at, entered_at FROM port_visits
                   WHERE mmsi = ? AND left_at IS NOT NULL AND left_at >= ?
                   ORDER BY left_at DESC LIMIT 1""",
                (mmsi, since)).fetchone()
        return dict(row) if row else None

    def departures(self, mmsi: str, limit=10):
        """Последние визиты судна с осадкой на входе и выходе.

        Осадка и вид груза нужны карточке: по разнице видно, загрузилось
        судно в порту или выгрузилось.
        """
        with self._lock:
            rows = self.conn.execute(
                """SELECT port, entered_at, left_at, draft_in, draft_out, cargo_kind
                   FROM port_visits WHERE mmsi = ? ORDER BY id DESC LIMIT ?""",
                (mmsi, limit)).fetchall()
        return [dict(row) for row in rows]

    def ended_visits(self, port: str, mmsi: str = None, limit=200):
        """Завершённые визиты в порт: по одному судну или по всем (для среднего)."""
        query = ("SELECT mmsi, entered_at, left_at FROM port_visits "
                 "WHERE port = ? AND left_at IS NOT NULL")
        params = [port]
        if mmsi:
            query += " AND mmsi = ?"
            params.append(mmsi)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def draft_reference(self, length_m, band=0.3):
        """Максимальная наблюдённая осадка у судов похожей длины.

        Нужна как запасной ориентир полной осадки, пока само судно не видели
        в грузу: по ней считается дедвейт в оценке груза.
        """
        try:
            length = float(length_m)
        except (TypeError, ValueError):
            return None
        with self._lock:
            row = self.conn.execute(
                "SELECT MAX(draft_max) AS value FROM vessels "
                "WHERE draft_max IS NOT NULL AND length_m BETWEEN ? AND ?",
                (length * (1 - band), length * (1 + band))).fetchone()
        return row["value"] if row else None

    def beam_reference(self, length_m, band=0.25):
        """Обычная ширина корпуса у судов похожей длины.

        Нужна как запасной вариант: у многих судов AIS ширину не передаёт,
        а без неё оценку груза не посчитать. Значение берётся по медиане
        известных ширин судов такой же длины — только дл подписи «≈».
        """
        try:
            length = float(length_m)
        except (TypeError, ValueError):
            return None
        with self._lock:
            rows = self.conn.execute(
                "SELECT beam_m FROM vessels "
                "WHERE beam_m IS NOT NULL AND beam_m > 0 AND length_m BETWEEN ? AND ?",
                (length * (1 - band), length * (1 + band))).fetchall()
        values = sorted(float(row["beam_m"]) for row in rows)
        if not values:
            return None
        middle = values[len(values) // 2]
        return middle

    # ---------------------------------------------------------------- алерты
    def alert_recent(self, key: str, hours: float) -> bool:
        query = "SELECT 1 FROM alerts WHERE key = ? AND ok = 1"
        params = [key]
        if hours and float(hours) > 0:
            since = iso(datetime.now() - timedelta(hours=float(hours)))
            query += " AND sent_at >= ?"
            params.append(since)
        query += " ORDER BY id DESC LIMIT 1"
        with self._lock:
            return self.conn.execute(query, params).fetchone() is not None

    def log_alert(self, key: str, mmsi, name, origin, destination, ok, error, message, when=None):
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO alerts (sent_at, key, mmsi, name, origin, destination, ok, error, message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (iso(when), key, mmsi, name, origin, destination, 1 if ok else 0, error, message))

    def alerts(self, limit=200):
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def alerts_count_today(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM alerts WHERE ok = 1 AND date(sent_at) = date(?)", (today + "T00:00:00",)).fetchone()
        return int(row["c"])

    # ------------------------------------------------------------ служебное
    def counts(self) -> dict:
        with self._lock:
            vessels = self.conn.execute("SELECT COUNT(*) AS c FROM vessels").fetchone()["c"]
            positions = self.conn.execute("SELECT COUNT(*) AS c FROM positions").fetchone()["c"]
            visits = self.conn.execute("SELECT COUNT(*) AS c FROM port_visits").fetchone()["c"]
        return {"vessels": int(vessels), "positions": int(positions), "visits": int(visits)}

    def prune_positions(self, keep_days: float) -> int:
        since = iso(datetime.now() - timedelta(days=float(keep_days)))
        with self._lock, self.conn:
            cur = self.conn.execute("DELETE FROM positions WHERE ts < ?", (since,))
            return cur.rowcount

    def forget_vessels(self, idle_hours: float) -> int:
        """Убирает из списка суда, которые давно не выходили на связь."""
        since = iso(datetime.now() - timedelta(hours=float(idle_hours)))
        with self._lock, self.conn:
            cur = self.conn.execute("DELETE FROM vessels WHERE updated_at < ?", (since,))
            return cur.rowcount

    def purge_vessels(self, mmsis) -> int:
        """Удаляет суда и всё, что с ними связано (демо-данные тестового потока)."""
        mmsis = [str(m) for m in mmsis if m]
        if not mmsis:
            return 0
        marks = ", ".join("?" for _ in mmsis)
        removed = 0
        with self._lock, self.conn:
            for table in ("positions", "port_visits", "alerts", "vessels"):
                cur = self.conn.execute(f"DELETE FROM {table} WHERE mmsi IN ({marks})", mmsis)
                if table == "vessels":
                    removed = cur.rowcount
        return removed

    def kv_get(self, key: str, default=None):
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str):
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))

    def close(self):
        with self._lock:
            self.conn.close()
