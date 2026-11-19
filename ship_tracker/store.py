"""Хранилище приложения: суда, позиции, визиты в порты и журнал алертов.

SQLite в режиме WAL: один файл ship_tracker.db, к нему можно подключаться
DB Browser for SQLite, пока программа работает.
"""

import json
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
    position_at TEXT,
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

-- История поля Destination: капитан правит его в AIS, и судно может
-- «переехать» из одного порта в другой. Без этой таблицы видно только
-- последнее значение, и непонятно, почему в карточке был другой порт.
CREATE TABLE IF NOT EXISTS destination_changes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    mmsi     TEXT NOT NULL,
    old_dest TEXT,
    new_dest TEXT NOT NULL,
    at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dest_changes_mmsi ON destination_changes (mmsi, id);

-- Причалы, краны и верфи из OpenStreetMap вокруг наших портов. Позиция судна
-- в AIS всегда на воде, поэтому для карточки нужно ближайшее место на стенке:
-- скачиваем один раз на порт и держим в базе, чтобы карточка открывалась без
-- интернета.
CREATE TABLE IF NOT EXISTS quay_places (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    area     TEXT NOT NULL,
    lat      REAL NOT NULL,
    lon      REAL NOT NULL,
    name     TEXT,
    kind     TEXT,
    osm      TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quay_places_area ON quay_places (area);

-- Последняя позиция судна в каждом источнике отдельно. Нужна, чтобы сверять
-- источники между собой, а не верить одному: если AISStream и снимок дают
-- точки в разных местах, это видно в карточке, а не тонет в одной строке.
-- Свежесть каждого поля паспорта отдельно. Источники приходят в разное
-- время: имя и тип могут быть свежими в AIS, а осадка и размеры — только
-- в карточке Ship Radar 24. Храним значение вместе с его временем и
-- источником, чтобы в карточку шло самое свежее по каждому полю, а не
-- «всё от одного, кто ответил последним».
CREATE TABLE IF NOT EXISTS vessel_fields (
    mmsi   TEXT NOT NULL,
    field  TEXT NOT NULL,
    value  TEXT,
    ts     TEXT NOT NULL,
    source TEXT,
    PRIMARY KEY (mmsi, field)
);

CREATE TABLE IF NOT EXISTS source_positions (
    mmsi   TEXT NOT NULL,
    source TEXT NOT NULL,
    ts     TEXT NOT NULL,
    lat    REAL,
    lon    REAL,
    sog    REAL,
    cog    REAL,
    PRIMARY KEY (mmsi, source)
);
"""

FIELDS = ("name", "imo", "callsign", "type_code", "type_label", "destination", "draft",
          "length_m", "beam_m",
          "eta_declared")

# При равном времени поля выигрывает более надёжный источник: живой AIS и
# официальный снимок важнее карточек-агрегаторов, а пустое имя источника —
# это старые записи, они уступают всем.
SOURCE_PRIORITY = {
    "aisstream": 0, "openwaters": 1, "snapshot": 2, "shipfinder": 3,
    "srd": 4, "vesselfinder": 5, "marinetraffic": 5, "myshiptracking": 5,
    "datalastic": 5, "aishub": 5, "goradar": 6, "mlive": 6,
    "gfw": 7, "ais-catcher": 7, "": 9,
}

# Значения Destination, которые не являются портом: их нельзя показывать как
# пункт назначения и нельзя позволять им затирать настоящий порт.
JUNK_DESTINATIONS = ("", "-", "—", "–", "N/A", "NA", "NONE", "NULL", "UNKNOWN",
                     "TBN", "TBA", "FOR ORDERS", "ORDERS", "OFFSHORE", "SEA",
                     "AT SEA", "HIGH SEAS", "0")

# Колонки, добавленные позже: старые базы дополняются при открытии.
MIGRATIONS = {
    "vessels": (("eta_declared", "TEXT"), ("length_m", "REAL"), ("beam_m", "REAL"),
                ("draft_min", "REAL"), ("draft_max", "REAL"), ("tracked", "INTEGER"),
                ("position_at", "TEXT")),
    "quay_places": (("geometry", "TEXT"),),
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
        # Время последней ПОЗИЦИИ отдельно от updated_at: обогащение паспорта
        # (GFW, карточки сервисов) обновляет запись, но позицию не двигает, и
        # старое судно выглядело свежим. Восстанавливаем время из журнала позиций.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vessels_position ON vessels (position_at)")
        self.conn.execute(
            "UPDATE vessels SET position_at = COALESCE("
            " (SELECT MAX(p.ts) FROM positions p WHERE p.mmsi = vessels.mmsi),"
            " updated_at) WHERE position_at IS NULL OR position_at = ''")
        # Служебные значения AIS и мусор от агрегаторов не должны выглядеть
        # данными: чистим их при открытии базы, дальше это делает валидация.
        self.conn.execute("UPDATE vessels SET sog = NULL WHERE sog >= 102.2 OR sog < 0")
        self.conn.execute("UPDATE vessels SET cog = NULL "
                          "WHERE cog < 0 OR cog >= 360")
        self.conn.execute("UPDATE vessels SET heading = NULL "
                          "WHERE heading < 0 OR heading >= 360 OR heading = 511")
        self.conn.execute("UPDATE vessels SET draft = NULL "
                          "WHERE draft <= 0 OR draft > 30")
        self.conn.execute("UPDATE vessels SET length_m = NULL "
                          "WHERE length_m < 5 OR length_m > 500")
        self.conn.execute("UPDATE vessels SET beam_m = NULL "
                          "WHERE beam_m < 2 OR beam_m > 80")
        self.conn.execute("UPDATE vessels SET type_code = NULL "
                          "WHERE type_code < 20 OR type_code > 99")
        # IMO из части источников приходит 9-значным с нулями на конце
        self.conn.execute("UPDATE vessels SET imo = SUBSTR(TRIM(imo), 1, 7) "
                          "WHERE LENGTH(TRIM(imo)) = 9 AND SUBSTR(TRIM(imo), 8, 2) = '00'")
        self.conn.execute("UPDATE vessel_fields SET value = SUBSTR(TRIM(value), 1, 7) "
                          "WHERE field = 'imo' AND LENGTH(TRIM(value)) = 9 "
                          "AND SUBSTR(TRIM(value), 8, 2) = '00'")
        marks = ", ".join("?" for _ in JUNK_DESTINATIONS)
        self.conn.execute(
            f"UPDATE vessels SET destination = NULL WHERE UPPER(TRIM("
            f"COALESCE(destination, ''))) IN ({marks})", JUNK_DESTINATIONS)
        self.conn.execute(
            f"DELETE FROM vessel_fields WHERE field = 'destination' AND "
            f"UPPER(TRIM(value)) IN ({marks})", JUNK_DESTINATIONS)
        self.conn.execute("DELETE FROM vessel_fields WHERE field = 'draft' "
                          "AND (CAST(value AS REAL) <= 0 OR CAST(value AS REAL) > 30)")
        self.conn.execute("DELETE FROM vessel_fields WHERE field IN ('length_m', 'beam_m') "
                          "AND (CAST(value AS REAL) <= 0 OR CAST(value AS REAL) > 500)")
        self.conn.execute("DELETE FROM vessel_fields WHERE field = 'type_code' "
                          "AND (CAST(value AS INTEGER) < 20 OR CAST(value AS INTEGER) > 99)")
        # Стоянка не может закончиться раньше, чем началась: такие записи —
        # след запоздавшей точки из трека. Нулевые стоянки — шум геозоны.
        self.conn.execute("DELETE FROM port_visits WHERE left_at IS NOT NULL "
                          "AND julianday(left_at) <= julianday(entered_at)")
        # Один и тот же заход мог записаться дважды из разных процессов.
        self.conn.execute("DELETE FROM port_visits WHERE id NOT IN ("
                          "SELECT MIN(id) FROM port_visits GROUP BY mmsi, port, "
                          "entered_at, COALESCE(left_at, ''))")
        # Двух одновременно открытых стоянок у судна быть не может: оставляем
        # последнюю, остальные — дубли того же захода.
        self.conn.execute("DELETE FROM port_visits WHERE left_at IS NULL AND id NOT IN ("
                          "SELECT MAX(id) FROM port_visits WHERE left_at IS NULL GROUP BY mmsi)")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_port_visits_once "
                          "ON port_visits(mmsi, port, entered_at, COALESCE(left_at, ''))")

    # ------------------------------------------------------------------ суда
    def upsert_static(self, mmsi: str, touch=True, **values):
        """Сохраняет статические данные судна, не затирая известное пустыми полями."""
        now = iso()
        data = {key: values[key] for key in FIELDS if values.get(key) not in (None, "")}
        try:
            draft = float(values.get("draft")) if values.get("draft") not in (None, "") else None
        except (TypeError, ValueError):
            draft = None
        with self._lock, self.conn:
            row = self.conn.execute("SELECT mmsi, destination FROM vessels WHERE mmsi = ?",
                                    (mmsi,)).fetchone()
            # назначение меняется на глазах: пишем каждое изменение, иначе
            # в карточке остаётся только последний порт без объяснений
            new_destination = str(data.get("destination") or "").strip()
            old_destination = str(row[1] or "").strip() if row else ""
            if (new_destination and old_destination
                    and new_destination.upper() != old_destination.upper()):
                self.conn.execute(
                    "INSERT INTO destination_changes (mmsi, old_dest, new_dest, at) "
                    "VALUES (?, ?, ?, ?)",
                    (mmsi, old_destination, new_destination, now))
            if row is None:
                columns = ["mmsi", "first_seen", "updated_at", *data.keys()]
                params = [mmsi, now, now, *data.values()]
                placeholders = ", ".join("?" for _ in columns)
                self.conn.execute(
                    f"INSERT INTO vessels ({', '.join(columns)}) VALUES ({placeholders})", params)
            elif data:
                assignments = ", ".join(f"{key} = ?" for key in data)
                if touch:
                    self.conn.execute(
                        f"UPDATE vessels SET {assignments}, updated_at = ? WHERE mmsi = ?",
                        [*data.values(), now, mmsi])
                else:
                    # сверка при старте не должна делать все суда «только что
                    # обновлёнными» и переставлять списки
                    self.conn.execute(
                        f"UPDATE vessels SET {assignments} WHERE mmsi = ?",
                        [*data.values(), mmsi])
            if draft:
                # диапазон осадок нужен для оценки груза: полная осадка
                # судна — это максимум того, что оно передавало
                self.conn.execute(
                    "UPDATE vessels SET draft_min = MIN(COALESCE(draft_min, ?), ?), "
                    "draft_max = MAX(COALESCE(draft_max, ?), ?) WHERE mmsi = ?",
                    (draft, draft, draft, draft, mmsi))

    def quay_area_cached(self, area) -> bool:
        """Уже скачаны ли причалы для этого участка."""
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM quay_places WHERE area = ? LIMIT 1", (str(area),)).fetchone()
        return row is not None

    def save_quay_places(self, area, items) -> int:
        """Сохраняет причалы участка, заменяя прежнюю выборку."""
        stamp = iso(datetime.now())
        rows = [(str(area), float(item["lat"]), float(item["lon"]),
                 item.get("name") or "", item.get("kind") or "", item.get("osm") or "",
                 json.dumps(item.get("geometry") or []), stamp)
                for item in items
                if item.get("lat") is not None and item.get("lon") is not None]
        with self._lock:
            self.conn.execute("DELETE FROM quay_places WHERE area = ?", (str(area),))
            self.conn.executemany(
                "INSERT INTO quay_places (area, lat, lon, name, kind, osm, geometry, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            self.conn.commit()
        return len(rows)

    def quay_places(self, south, west, north, east) -> list:
        """Причалы в прямоугольнике — для поиска ближайшего к стоянке."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT lat, lon, name, kind, osm, geometry FROM quay_places "
                "WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                (float(south), float(north), float(west), float(east))).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["geometry"] = [tuple(point) for point in json.loads(item.get("geometry") or "[]")]
            except (TypeError, ValueError):
                item["geometry"] = []
            result.append(item)
        return result

    def destination_changes(self, mmsi, limit=5) -> list:
        """Последние изменения поля Destination: [{old, new, at}], новые сверху."""
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT old_dest, new_dest, at FROM destination_changes "
                    "WHERE mmsi = ? ORDER BY id DESC LIMIT ?",
                    (str(mmsi), int(limit))).fetchall()
        except Exception:
            return []
        return [{"old": row[0], "new": row[1], "at": row[2]} for row in rows]

    def upsert_position(self, mmsi: str, lat: float, lon: float, sog=None, cog=None,
                        heading=None, port=None, when=None):
        """Сохраняет позицию, но не затирает более свежую более старой.

        Часть источников («карты судов», снимки, кэш сервисов) отдаёт позиции
        с опозданием на часы. Без этой проверки они перебивали живой AIS: в базе
        оставалась отметка десятичасовой давности, и радар показывал старое
        место судна. Старшая по времени отметка всегда побеждает.
        """
        ts = iso(when)
        fresh = ("COALESCE(vessels.position_at, '') <= excluded.position_at")
        with self._lock, self.conn:
            self.conn.execute(
                f"""INSERT INTO vessels (mmsi, lat, lon, sog, cog, heading, port, updated_at,
                                        position_at, first_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(mmsi) DO UPDATE SET
                       lat = CASE WHEN {fresh} THEN excluded.lat ELSE vessels.lat END,
                       lon = CASE WHEN {fresh} THEN excluded.lon ELSE vessels.lon END,
                       sog = CASE WHEN {fresh} THEN COALESCE(excluded.sog, vessels.sog)
                                  ELSE vessels.sog END,
                       cog = CASE WHEN {fresh} THEN excluded.cog ELSE vessels.cog END,
                       heading = CASE WHEN {fresh} THEN excluded.heading
                                      ELSE vessels.heading END,
                       port = CASE WHEN {fresh} THEN excluded.port
                                   ELSE vessels.port END,
                       position_at = CASE WHEN {fresh} THEN excluded.position_at
                                          ELSE vessels.position_at END,
                       updated_at = excluded.updated_at""",
                (mmsi, lat, lon, sog, cog, heading, port, ts, ts, ts),
            )

    def save_source_position(self, mmsi, source, lat, lon, sog=None, cog=None, when=None):
        """Последняя позиция судна от конкретного источника."""
        if not source or lat is None or lon is None:
            return

        def number(value):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        try:
            with self._lock, self.conn:
                self.conn.execute(
                    "INSERT INTO source_positions (mmsi, source, ts, lat, lon, sog, cog) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(mmsi, source) DO UPDATE SET ts=excluded.ts, "
                    "lat=excluded.lat, lon=excluded.lon, sog=excluded.sog, cog=excluded.cog",
                    (str(mmsi), str(source), iso(when), float(lat), float(lon),
                     number(sog), number(cog)))
        except Exception:
            return

    def save_field(self, mmsi: str, field: str, value, when=None, source="") -> bool:
        """Запоминает значение поля, только если оно свежее сохранённого.

        Возвращает True, если значение принято: старое сообщение, пришедшее
        позже нового (обычное дело, когда источников много), не должно
        затирать свежее.
        """
        if value in (None, "") or not field:
            return False
        stamp = iso(when)
        text_value = str(value)
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT ts, value, source FROM vessel_fields WHERE mmsi = ? AND field = ?",
                (str(mmsi), field)).fetchone()
            if row and row[0] and str(row[0]) > stamp:
                return False
            if row and row[0] and str(row[0]) == stamp:
                if str(row[1]) == text_value:
                    # значение не изменилось — обновляем только метку времени
                    self.conn.execute(
                        "UPDATE vessel_fields SET ts = ?, source = ? "
                        "WHERE mmsi = ? AND field = ?",
                        (stamp, source, str(mmsi), field))
                    return True
                # время одно — решает надёжность источника
                if (SOURCE_PRIORITY.get(str(source), 9)
                        > SOURCE_PRIORITY.get(str(row[2] or ""), 9)):
                    return False
            elif row and str(row[1]) == text_value:
                self.conn.execute(
                    "UPDATE vessel_fields SET ts = ?, source = ? WHERE mmsi = ? AND field = ?",
                    (stamp, source, str(mmsi), field))
                return True
            self.conn.execute(
                "INSERT INTO vessel_fields (mmsi, field, value, ts, source) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(mmsi, field) DO UPDATE SET "
                "value = excluded.value, ts = excluded.ts, source = excluded.source",
                (str(mmsi), field, text_value, stamp, source))
        return True

    def fields(self, mmsi) -> dict:
        """Все поля паспорта с их временем и источником: {field: {value, ts, source}}."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT field, value, ts, source FROM vessel_fields WHERE mmsi = ?",
                (str(mmsi),)).fetchall()
        return {row[0]: {"value": row[1], "ts": row[2], "source": row[3]} for row in rows}

    def source_positions(self, mmsi) -> list:
        """Все источники, которые присылали позицию этого судна."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT source, ts, lat, lon, sog, cog FROM source_positions "
                "WHERE mmsi = ? ORDER BY ts DESC", (str(mmsi),)).fetchall()
        return [dict(row) for row in rows]

    def add_position(self, mmsi: str, lat: float, lon: float, sog=None, cog=None, when=None):
        # одну и ту же точку могут прислать два источника (живой AIS и снимок):
        # без проверки в базу ложились по 3–4 одинаковых строки на секунду
        stamp = iso(when)
        with self._lock, self.conn:
            exists = self.conn.execute(
                "SELECT 1 FROM positions WHERE mmsi = ? AND ts = ? LIMIT 1",
                (mmsi, stamp)).fetchone()
            if exists:
                return
            self.conn.execute(
                "INSERT INTO positions (mmsi, ts, lat, lon, sog, cog) VALUES (?, ?, ?, ?, ?, ?)",
                (mmsi, stamp, lat, lon, sog, cog))

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

    def positions_bbox(self, south, west, north, east, hours=96):
        """Точки трека в прямоугольнике за последние часы: история визитов.

        Выбираются только позиции у наших портов, а не весь трек района:
        так сборка стоянок за 4 дня занимает секунды.
        """
        since = iso(datetime.now() - timedelta(hours=float(hours)))
        with self._lock:
            rows = self.conn.execute(
                "SELECT mmsi, ts, lat, lon FROM positions WHERE ts >= ?"
                " AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
                " ORDER BY mmsi, ts",
                (since, float(south), float(north), float(west), float(east))).fetchall()
        return [(str(row["mmsi"]), row["ts"], row["lat"], row["lon"]) for row in rows]

    def vessels_in_bbox(self, south, west, north, east, limit=3000):
        """Все суда, чья последняя позиция попала в прямоугольник.

        Без фильтров по типу и назначению: так видно всё, что плавает в районе,
        а не только суда, связанные с нашими портами.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM vessels WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
                " ORDER BY COALESCE(position_at, updated_at) DESC LIMIT ?",
                (float(south), float(north), float(west), float(east), int(limit))).fetchall()
        return [dict(row) for row in rows]

    def track_between(self, mmsi: str, start, end):
        """Позиции судна за конкретный промежуток — например, перед входом в порт."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts, lat, lon, sog, cog FROM positions WHERE mmsi = ? "
                "AND ts >= ? AND ts <= ? ORDER BY ts",
                (mmsi, iso(start), iso(end))).fetchall()
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

    def open_visits(self):
        """Все незакрытые стоянки сразу: кто сейчас в каком порту."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM port_visits WHERE left_at IS NULL").fetchall()
        return [dict(row) for row in rows]

    def delete_visit(self, visit_id) -> bool:
        """Удаляет стоянку, которую нечем подтвердить (судна уже нет в базе)."""
        with self._lock, self.conn:
            cur = self.conn.execute("DELETE FROM port_visits WHERE id = ?", (int(visit_id),))
            return cur.rowcount > 0

    def close_visit_by_id(self, visit_id, when):
        """Закрывает открытую стоянку временем ухода из трека.

        Уход раньше входа — признак запоздавшей точки: такую стоянку не
        закрываем, иначе в истории появляется «пришёл в ноябре, ушёл в октябре».
        """
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE port_visits SET left_at = ? WHERE id = ? AND left_at IS NULL "
                "AND julianday(?) >= julianday(entered_at)",
                (iso(when), int(visit_id), iso(when)))
            return cur.rowcount

    def start_visit(self, mmsi: str, port: str, lat, lon, when=None, draft=None,
                    cargo_kind=None, source="live") -> bool:
        now = iso(when)
        with self._lock, self.conn:
            rows = self.conn.execute(
                "SELECT port, entered_at FROM port_visits "
                "WHERE mmsi = ? AND left_at IS NULL", (mmsi,)).fetchall()
            for row in rows:
                # запоздавшая точка не должна перебивать более новый заход
                if str(row["entered_at"] or "") > now:
                    return False
                if (str(row["port"] or "").upper() == str(port).upper()
                        and str(row["entered_at"] or "") == now):
                    return False
            self.conn.execute(
                "UPDATE port_visits SET left_at = ? WHERE mmsi = ? AND left_at IS NULL "
                "AND julianday(entered_at) <= julianday(?)", (now, mmsi, now))
            self.conn.execute(
                "INSERT INTO port_visits (mmsi, port, entered_at, lat, lon, draft_in, "
                "cargo_kind, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (mmsi, port, now, lat, lon, draft, cargo_kind, source))
        return True

    def import_visit(self, mmsi: str, port: str, entered_at, left_at=None, draft_in=None,
                     draft_out=None, cargo_kind=None, source="track") -> bool:
        """Добавляет визит, найденный по истории трека, если такого ещё нет."""
        entered = iso(entered_at)
        left = iso(left_at) if left_at else None
        # уход раньше входа, вход из будущего или нулевая стоянка — не данные
        if left and left <= entered:
            return False
        if entered > iso(datetime.now() + timedelta(minutes=5)):
            return False
        with self._lock, self.conn:
            # Тот же заход мог быть записан по живому AIS или с другого трека:
            # новый визит не добавляется, если его интервал пересекается с уже
            # записанным (открытый визит считается до текущего момента).
            exists = self.conn.execute(
                "SELECT id FROM port_visits WHERE mmsi = ? AND UPPER(port) = ?"
                " AND julianday(?) <= julianday(COALESCE(left_at, 'now'))"
                " AND julianday(entered_at) <= julianday(?) LIMIT 1",
                (mmsi, str(port).upper(), entered, left or iso())).fetchone()
            if exists:
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

    def positions_between(self, mmsi, start, end) -> list:
        """Позиции судна за отрезок времени (для поиска причала по стоянкам)."""
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT ts, lat, lon, sog FROM positions WHERE mmsi = ? "
                    "AND ts >= ? AND ts <= ? ORDER BY ts",
                    (str(mmsi), str(start), str(end))).fetchall()
        except Exception:
            return []
        return [dict(row) for row in rows]

    def usual_port(self, mmsi) -> dict | None:
        """Порт, где судно стояло чаще всего: {"port": имя, "visits": сколько}."""
        try:
            with self._lock:
                row = self.conn.execute(
                    "SELECT port, COUNT(*) FROM port_visits WHERE mmsi = ? "
                    "GROUP BY UPPER(port) ORDER BY COUNT(*) DESC, MAX(entered_at) DESC "
                    "LIMIT 1", (str(mmsi),)).fetchone()
        except Exception:
            return None
        return {"port": row[0], "visits": row[1]} if row else None

    def visit_ports(self) -> dict:
        """MMSI → порты, в которых судно было (по всем визитам в базе)."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT mmsi, UPPER(port) AS port FROM port_visits").fetchall()
        result = {}
        for row in rows:
            result.setdefault(str(row["mmsi"]), set()).add(row["port"])
        return result

    def visit_counts(self, closed_only=False) -> dict:
        """Сколько визитов и сколько судов было в каждом нашем порту.

        `closed_only` — считать только завершённые (судно уже ушло): так
        подписываются кнопки портов на вкладке «История стоянок».
        """
        where = " WHERE left_at IS NOT NULL" if closed_only else ""
        with self._lock:
            rows = self.conn.execute(
                "SELECT UPPER(port) AS port, COUNT(*) AS visits,"
                " COUNT(DISTINCT mmsi) AS vessels, MAX(entered_at) AS last"
                " FROM port_visits" + where + " GROUP BY UPPER(port)").fetchall()
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
                "WHERE mmsi = ? AND left_at IS NULL "
                "AND julianday(?) >= julianday(entered_at)", (now, draft_out, mmsi, now))
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

    def stays_by_type(self, limit=800):
        """Завершённые стоянки с типом судна — для расчётной оценки стоянки.

        Нужны, когда у судна и у порта ещё нет своей истории: берём медиану
        по судам того же типа во всех наших портах.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT v.type_label AS type_label, p.entered_at AS entered_at, "
                "p.left_at AS left_at FROM port_visits p "
                "LEFT JOIN vessels v ON v.mmsi = p.mmsi "
                "WHERE p.left_at IS NOT NULL ORDER BY p.id DESC LIMIT ?",
                (int(limit),)).fetchall()
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
