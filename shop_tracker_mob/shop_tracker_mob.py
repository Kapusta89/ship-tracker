"""Ship Tracker Mobile — мобильная версия радара для телефона.

Один exe поднимает небольшой сервер на компьютере: телефон в той же
Wi-Fi сети открывает приложение (или Android-обёртку из этой же папки)
и видит те же вкладки, что и настольная версия: «Радар», «В море»,
«Прогноз 4 дня», «Прогноз всех судов», «Стоянки» и «Подключения».
Данные читаются только на чтение из базы Ship Tracker; ключи и подключения
задаются в варианте, который запускается через run.bat.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import socket
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mobile_page import PAGE

APP_NAME = "Ship Tracker Mobile"
APP_VERSION = "2.1.1"
DEFAULT_PORT = 8080
DISCOVERY_PORT = 50505

# Как в настольной версии: позиция старше этого — уже не обстановка.
MAX_VIEW_AGE_HOURS = 9.0
KNOT_KMH = 1.852
MIN_SPEED_KNOTS = 0.5
DEFAULT_STAY_HOURS = 24.0
GUESS_MAX_KM = 700.0
GUESS_MAX_OFFSET = 35.0


def app_dir() -> Path:
    """Папка рядом с exe (или с исходником при запуске из Python)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def log_line(text: str) -> None:
    """Пишет строку в shop_tracker_mob.log рядом с exe."""
    try:
        with (app_dir() / "shop_tracker_mob.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


def find_database() -> Path | None:
    """Берёт самую свежую базу из копий Ship Tracker.

    Ключи и подключения живут в варианте, который запускается через run.bat,
    поэтому телефон показывает данные той копии, что обновляется сейчас.
    """
    base = app_dir()
    candidates = [
        base / "data" / "ship_tracker.db",
        base / "ship_tracker.db",
        base.parent / "ship_tracker.exe" / "data" / "ship_tracker.db",
        base.parent / "ship_tracker" / "data" / "ship_tracker.db",
    ]
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


def find_config(database: Path | None) -> dict:
    if database is None:
        return {}
    for path in (database.parent / "config.json",
                 database.parent / "config.example.json"):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def parse_moment(text) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    """Начальный азимут от точки 1 к точке 2, градусы."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def move_point(lat, lon, bearing, distance_km):
    """Точка на расстоянии и азимуте (как в forecast.destination_point)."""
    angular = distance_km / 6371.0
    phi1, lambda1 = math.radians(lat), math.radians(lon)
    theta = math.radians(bearing)
    sin_phi2 = (math.sin(phi1) * math.cos(angular)
                + math.cos(phi1) * math.sin(angular) * math.cos(theta))
    phi2 = math.asin(max(-1.0, min(1.0, sin_phi2)))
    lambda2 = lambda1 + math.atan2(
        math.sin(theta) * math.sin(angular) * math.cos(phi1),
        math.cos(angular) - math.sin(phi1) * sin_phi2)
    return math.degrees(phi2), (math.degrees(lambda2) + 540.0) % 360.0 - 180.0


def duration_text(hours) -> str:
    """Длительность словами: «40 мин», «14 ч», «1 д 6 ч»."""
    if hours is None:
        return "—"
    hours = max(0.0, float(hours))
    if hours < 1:
        return f"{round(hours * 60)} мин"
    if hours < 24:
        return f"{round(hours)} ч"
    days, rest = divmod(round(hours), 24)
    return f"{days} д" + (f" {rest} ч" if rest else "")


def moment_text(moment: datetime | None) -> str:
    return f"{moment:%d.%m %H:%M}" if moment else "—"


def normalize_destination(text) -> set[str]:
    cleaned = re.sub(r"[^0-9A-ZА-ЯЁ]+", " ", str(text or "").upper())
    return {token for token in cleaned.split() if token}


# Алиасы портов: в AIS пишут и старое имя, и код UN/LOCODE.
PORT_ALIASES = {
    "IZMAIL": {"UAIZM"},
    "RENI": {"UARNI"},
    "KILIYA": {"UAKIL"},
    "VYLKOVE": {"UAVYL"},
    "ZATOKA": {"BILHOROD", "DNISTROVSKYI", "BILHOROD-DNISTROVSKYI"},
    "CHORNOMORSK": {"CHERNOMORSK", "ILICHEVSK", "CHORNOMORSK PORT"},
    "ODESSA": {"ODESA", "ODESSA UA"},
    "YUZHNY": {"PIVDENNYI", "YUZHNE", "PIVDENNY"},
    "OCHAKIV": {"OCHAKOV"},
    "MYKOLAIV": {"NIKOLAEV", "MYKOLAIV PORT"},
}


class Data:
    """Читает базу Ship Tracker и считает всё, что есть в настольной версии."""

    def __init__(self):
        self.database = find_database()
        self.config = find_config(self.database)
        self.ports = [port for port in (self.config.get("ports") or [])
                      if isinstance(port, dict) and port.get("lat") and port.get("lon")]
        for port in self.ports:
            port.setdefault("radius_km", 6.0)
        self.pinned = {str(item) for item in (self.config.get("pinned_mmsis") or [])}
        self.db_updated = None
        self._cache = None
        self._cache_at = 0.0
        self._memo: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def connect(self):
        con = sqlite3.connect(f"file:{self.database}?mode=rw", uri=True, timeout=5)
        con.execute("PRAGMA query_only = 1")
        con.row_factory = sqlite3.Row
        return con

    def memo(self, name: str, ttl: float, builder):
        """Кэш тяжёлых расчётов: прогноз и «в море» считаются не каждый раз."""
        now = time.monotonic()
        with self._lock:
            cached = self._memo.get(name)
            if cached and now - cached[0] < ttl:
                return cached[1]
        value = builder()
        with self._lock:
            self._memo[name] = (now, value)
        return value

    # ------------------------------------------------------------ суда
    def query(self) -> list[dict]:
        with self._lock:
            if self._cache is not None and time.monotonic() - self._cache_at < 2.0:
                return self._cache
        rows = []
        if self.database and self.database.exists():
            try:
                con = self.connect()
                rows = con.execute(
                    "SELECT mmsi, name, imo, callsign, type_label, destination, draft, "
                    "length_m, beam_m, eta_declared, lat, lon, sog, cog, heading, port, "
                    "position_at, updated_at FROM vessels").fetchall()
                self.db_updated = con.execute(
                    "SELECT MAX(updated_at) FROM vessels").fetchone()[0]
                con.close()
            except sqlite3.Error:
                rows = []
        ships = []
        for row in rows:
            ship = dict(row)
            lat, lon = ship.get("lat"), ship.get("lon")
            if lat is None or lon is None:
                continue
            ship["mmsi"] = str(ship.get("mmsi"))
            ship["pinned"] = ship["mmsi"] in self.pinned
            moment = parse_moment(ship.get("position_at"))
            ship["age_minutes"] = (round((datetime.now() - moment).total_seconds() / 60)
                                   if moment else None)
            nearest, distance = None, None
            for port in self.ports:
                try:
                    km = haversine_km(lat, lon, float(port["lat"]), float(port["lon"]))
                except (TypeError, ValueError):
                    continue
                if distance is None or km < distance:
                    nearest, distance = str(port.get("name") or ""), km
            ship["near_port"] = nearest if distance is not None and distance <= 25 else None
            ship["distance_km"] = round(distance, 1) if distance is not None else None
            ship["port"] = ship.get("port") or ship.get("near_port")
            ships.append(ship)
        ships.sort(key=lambda item: (
            item["age_minutes"] if item["age_minutes"] is not None else 10 ** 9,
            not item["pinned"],
            str(item.get("name") or item["mmsi"])))
        with self._lock:
            self._cache = ships
            self._cache_at = time.monotonic()
        return ships

    def port_stats(self, ships=None) -> list[dict]:
        """Порты с количеством судов: всего, свежих, под ходом."""
        ships = self.query() if ships is None else ships
        result = []
        for port in self.ports:
            name = str(port.get("name") or "")
            near = [ship for ship in ships
                    if str(ship.get("near_port") or "").lower() == name.lower()]
            fresh = [ship for ship in near
                     if ship["age_minutes"] is not None and ship["age_minutes"] <= 180]
            moving = [ship for ship in near if (ship.get("sog") or 0) >= 0.5]
            result.append({"name": name, "label": port.get("label") or name,
                           "total": len(near), "fresh": len(fresh),
                           "moving": len(moving), "lat": port.get("lat"),
                           "lon": port.get("lon")})
        return result

    def payload(self, query: str = "", limit: int = 300, only_pinned: bool = False,
                port: str = "") -> dict:
        ships = self.query()
        if only_pinned:
            ships = [ship for ship in ships if ship["pinned"]]
        text = query.strip().lower()
        if text:
            ships = [ship for ship in ships
                     if text in str(ship.get("name") or "").lower()
                     or text in str(ship.get("destination") or "").lower()
                     or text == ship["mmsi"]]
        if port:
            target = port.strip().lower()
            ships = [ship for ship in ships
                     if target in (str(ship.get("near_port") or "").lower(),
                                   str(ship.get("port") or "").lower())]
        total = len(ships)
        moment = parse_moment(getattr(self, "db_updated", None))
        db_age = (round((datetime.now() - moment).total_seconds() / 60)
                  if moment else None)
        return {
            "app": APP_NAME,
            "version": APP_VERSION,
            "database": str(self.database or ""),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "db_updated_at": self.db_updated,
            "db_age_minutes": db_age,
            "total": total,
            "ports": self.port_stats(ships if not (query or port or only_pinned)
                                     else None),
            "ships": ships[:max(1, min(limit, 2000))],
        }

    # -------------------------------------------------- порты и треки
    def port_at(self, lat, lon):
        for port in self.ports:
            try:
                km = haversine_km(lat, lon, float(port["lat"]), float(port["lon"]))
            except (TypeError, ValueError):
                continue
            if km <= float(port.get("radius_km") or 6.0):
                return port
        return None

    def destination_port(self, vessel):
        """Порт из поля Destination: имя, подпись или известный алиас."""
        tokens = normalize_destination(vessel.get("destination"))
        if not tokens:
            return None
        for port in self.ports:
            variants = set()
            for source in (port.get("name"), port.get("label")):
                variants |= normalize_destination(source)
            variants |= PORT_ALIASES.get(str(port.get("name") or "").upper(), set())
            if variants & tokens:
                return port
        return None

    def tracks_map(self, hours=24.0) -> dict:
        """Все треки за сутки одним запросом: иначе прогноз считался бы минутами."""
        if not self.database:
            return {}
        since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        result: dict[str, list] = {}
        try:
            con = self.connect()
            rows = con.execute(
                "SELECT mmsi, ts, lat, lon, sog, cog FROM positions "
                "WHERE ts >= ? ORDER BY mmsi, ts", (since,)).fetchall()
            con.close()
        except sqlite3.Error:
            return {}
        for row in rows:
            result.setdefault(str(row["mmsi"]), []).append(dict(row))
        return result

    def track(self, mmsi, hours=24.0) -> list[dict]:
        if not self.database:
            return []
        since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        try:
            con = self.connect()
            rows = [dict(row) for row in con.execute(
                "SELECT ts, lat, lon, sog, cog FROM positions "
                "WHERE mmsi = ? AND ts >= ? ORDER BY ts", (str(mmsi), since)).fetchall()]
            con.close()
            return rows
        except sqlite3.Error:
            return []

    def estimates(self, vessel, track=None):
        """Ход и курс: из AIS, а если молчит — по треку (как в forecast.py)."""
        speed = None
        try:
            speed = float(vessel.get("sog") or 0)
        except (TypeError, ValueError):
            speed = None
        course = vessel.get("cog")
        if course is None:
            course = vessel.get("heading")
        try:
            course = float(course) if course is not None else None
        except (TypeError, ValueError):
            course = None
        if course is not None and not (0 < course < 360):
            course = None
        points = [p for p in (track or []) if p.get("lat") is not None]
        speeds = []
        for point in points[-6:]:
            try:
                value = float(point.get("sog") or 0)
            except (TypeError, ValueError):
                continue
            if MIN_SPEED_KNOTS <= value <= 40:
                speeds.append(value)
        source = "AIS"
        if len(speeds) >= 3:
            speeds.sort()
            speed = speeds[len(speeds) // 2]
            source = "средняя по треку"
        if course is None and len(points) >= 2:
            first, last = points[-2], points[-1]
            course = bearing_deg(float(first["lat"]), float(first["lon"]),
                                 float(last["lat"]), float(last["lon"]))
            source += ", курс по треку"
        if speed is None or not (MIN_SPEED_KNOTS <= float(speed) <= 40):
            derived = []
            stamped = [p for p in points if p.get("ts")][-8:]
            for first, second in zip(stamped, stamped[1:]):
                try:
                    t1 = parse_moment(first["ts"])
                    t2 = parse_moment(second["ts"])
                    km = haversine_km(float(first["lat"]), float(first["lon"]),
                                      float(second["lat"]), float(second["lon"]))
                except (TypeError, ValueError, KeyError):
                    continue
                hours = (t2 - t1).total_seconds() / 3600.0 if t1 and t2 else 0
                if hours <= 0 or km <= 0:
                    continue
                knots = km / hours / KNOT_KMH
                if MIN_SPEED_KNOTS <= knots <= 40:
                    derived.append(knots)
            if len(derived) >= 2:
                derived.sort()
                speed = derived[len(derived) // 2]
                source = f"по треку ({len(derived)} отрезков)"
            else:
                speed = None
                source = "хода нет в AIS"
        return speed, course, source

    def stay_hours(self) -> dict:
        """Медиана стоянки по порту — из наших визитов (для прогноза)."""
        result = {}
        if not self.database:
            return result
        try:
            con = self.connect()
            rows = con.execute(
                "SELECT port, entered_at, left_at FROM port_visits "
                "WHERE left_at IS NOT NULL").fetchall()
            con.close()
        except sqlite3.Error:
            return result
        buckets = {}
        for port, entered, left in rows:
            start, end = parse_moment(entered), parse_moment(left)
            if not start or not end or end <= start:
                continue
            buckets.setdefault(str(port), []).append((end - start).total_seconds() / 3600)
        for port, values in buckets.items():
            values.sort()
            result[port] = round(values[len(values) // 2], 1)
        return result

    # ------------------------------------------------------- «в море»
    def sea(self) -> dict:
        ships = self.query()
        fresh_hours = float(self.config.get("sea_fresh_hours") or 12)
        rows = []
        for ship in ships:
            if ship.get("lat") is None or ship.get("lon") is None:
                continue
            port = self.destination_port(ship)
            if self.port_at(ship["lat"], ship["lon"]):
                continue
            distance = None
            for candidate in self.ports:
                km = haversine_km(ship["lat"], ship["lon"],
                                  float(candidate["lat"]), float(candidate["lon"]))
                if distance is None or km < distance:
                    distance = km
            ours = port is not None
            age = ship.get("age_minutes")
            if not ours and (age is None or age / 60.0 > fresh_hours):
                continue
            if ours and (age is None or age / 60.0 > MAX_VIEW_AGE_HOURS * 2):
                continue
            if distance is not None and distance > 120 and not ours:
                continue
            speed = ship.get("sog") or 0
            eta_hours = None
            if distance and speed >= MIN_SPEED_KNOTS:
                eta_hours = round(distance / (float(speed) * KNOT_KMH), 1)
            row = dict(ship)
            row["port"] = str((port or {}).get("name") or ship.get("near_port") or "")
            row["distance_text"] = f"{distance:.0f} км" if distance is not None else "—"
            row["eta_hours"] = eta_hours
            row["eta_text"] = ("прибытие " + moment_text(datetime.now() + timedelta(hours=eta_hours))
                               if eta_hours else "ход не подтверждён")
            if port:
                row["port"] = str(port.get("name"))
            rows.append(row)
        rows.sort(key=lambda r: (r["eta_hours"] is None, r["eta_hours"] or 10 ** 6))
        return {
            "rows": rows[:300],
            "ports": [str(port.get("name")) for port in self.ports],
            "note": ("Суда, связанные с нашими портами: идут к нам (порт в назначении) "
                     "или находятся рядом с портом. Стоящие в порту — во вкладке «Стоянки». "
                     "Расстояние и прибытие — по прямой и текущему ходу."),
        }

    # ---------------------------------------------- прогноз 4 дня (план)
    def forecast_plan(self) -> dict:
        ships = self.query()
        stay = self.stay_hours()
        tracks = self.tracks_map(24)
        rows = []
        in_port = no_position = stale = 0
        for ship in ships:
            port = self.destination_port(ship)
            if not port:
                continue
            age = ship.get("age_minutes")
            if age is None or age / 60.0 > MAX_VIEW_AGE_HOURS:
                stale += 1
                continue
            if self.port_at(ship["lat"], ship["lon"]):
                in_port += 1
                continue
            try:
                distance = haversine_km(ship["lat"], ship["lon"],
                                        float(port["lat"]), float(port["lon"]))
            except (TypeError, ValueError):
                no_position += 1
                continue
            track = tracks.get(ship["mmsi"], [])
            speed, course, source = self.estimates(ship, track)
            moving = speed is not None and MIN_SPEED_KNOTS <= speed <= 40
            travel = (distance / (float(speed) * KNOT_KMH)) if moving else None
            stay_hours = stay.get(str(port.get("name")))
            total = None
            if travel is not None:
                total = travel + (stay_hours or DEFAULT_STAY_HOURS)
            departure = (datetime.now() + timedelta(hours=total)) if total else None
            row = {
                "mmsi": ship["mmsi"], "name": ship.get("name") or ship["mmsi"],
                "port": str(port.get("name")), "destination": ship.get("destination") or "",
                "route": f"{ship.get('destination') or '—'} → {port.get('name')}",
                "speed_text": (f"{float(speed):.1f} уз" if moving else "стоит"),
                "distance_text": f"{distance:.0f} км",
                "travel_text": duration_text(travel) if travel is not None else "—",
                "stay_text": (duration_text(stay_hours) if stay_hours else "≈ " + duration_text(DEFAULT_STAY_HOURS)),
                "total_text": duration_text(total) if total else "—",
                "departure_text": moment_text(departure),
                "age_minutes": age,
            }
            rows.append(row)
        rows.sort(key=lambda r: (r["total_text"] == "—", r["total_text"]))
        return {
            "rows": rows,
            "note": ("Только суда, идущие с моря в наши порты. «В пути» — по прямой и "
                     "текущему ходу, «стоянка» — медиана наших визитов в этот порт (иначе "
                     "≈ сутки), «всего до выхода» — путь плюс стоянка. Стоящие в порту "
                     f"считаются в «Стоянках» — здесь их нет ({in_port}). "
                     f"Скрыто устаревших: {stale}, без позиции: {no_position}."),
        }

    # ------------------------------------- прогноз всех судов (4 суток)
    def forecast_all(self) -> dict:
        ships = self.query()
        boxes = self.config.get("bounding_boxes") or [[[40.5, 26.0], [47.5, 42.0]]]
        south = min(float(box[0][0]) for box in boxes)
        west = min(float(box[0][1]) for box in boxes)
        north = max(float(box[1][0]) for box in boxes)
        east = max(float(box[1][1]) for box in boxes)
        ports = self.ports
        tracks = self.tracks_map(24)
        rows = []
        for ship in ships:
            lat, lon = ship["lat"], ship["lon"]
            if not (south <= lat <= north and west <= lon <= east):
                continue
            age = ship.get("age_minutes")
            if age is None or age / 60.0 > MAX_VIEW_AGE_HOURS:
                continue
            guessed = False
            port = self.destination_port(ship)
            track = tracks.get(ship["mmsi"], [])
            if not port:
                speed, course, source = self.estimates(ship, track)
                guess = None
                if course is not None and speed:
                    reach = float(speed) * KNOT_KMH * 96.0
                    for candidate in ports:
                        km = haversine_km(lat, lon, float(candidate["lat"]), float(candidate["lon"]))
                        if km > min(GUESS_MAX_KM, reach) or km < 0.5:
                            continue
                        offset = abs(((bearing_deg(lat, lon, float(candidate["lat"]),
                                                   float(candidate["lon"])) - course
                                       + 540.0) % 360.0) - 180.0)
                        if offset > GUESS_MAX_OFFSET:
                            continue
                        if guess is None or offset < guess[0]:
                            guess = (offset, candidate)
                port = guess[1] if guess else None
                guessed = bool(port)
            if not port:
                continue
            speed, course, source = self.estimates(ship, track)
            distance = haversine_km(lat, lon, float(port["lat"]), float(port["lon"]))
            moving = speed is not None and MIN_SPEED_KNOTS <= speed <= 40
            hours = distance / (float(speed) * KNOT_KMH) if moving and distance else None
            row = {
                "mmsi": ship["mmsi"], "name": ship.get("name") or ship["mmsi"],
                "type_label": ship.get("type_label") or "", "port": str(port.get("name")),
                "guessed": guessed, "destination": ship.get("destination") or "",
                "speed_text": (f"{float(speed):.1f} уз" if moving else "стоит"),
                "distance_text": f"{distance:.0f} км",
                "eta_text": ("через " + duration_text(hours) + " · "
                             + moment_text(datetime.now() + timedelta(hours=hours))
                             if hours else "—"),
                "day1": self.position_after(lat, lon, port, speed, 24.0, distance),
                "day2": self.position_after(lat, lon, port, speed, 48.0, distance),
                "day4": self.position_after(lat, lon, port, speed, 96.0, distance),
                "age_minutes": age,
            }
            rows.append(row)
        rows.sort(key=lambda r: (r["eta_text"] == "—", r["name"].lower()))
        return {
            "rows": rows[:300],
            "note": ("Все суда в рабочем районе, идущие в наши порты: порт из назначения, "
                     "а если поле пустое — по линии курса с проверкой, что судно успевает "
                     "дойти. «Через день / 2 дня / 4 дня» — где судно будет на этот срок."),
        }

    def position_after(self, lat, lon, port, speed, hours, distance):
        """Где судно окажется через N часов пути в наш порт."""
        if not speed or speed < MIN_SPEED_KNOTS:
            return "стоит"
        reach = float(speed) * KNOT_KMH * hours
        if distance <= reach:
            moment = datetime.now() + timedelta(hours=distance / (float(speed) * KNOT_KMH))
            return f"в порту с {moment:%d.%m %H:%M}"
        new_lat, new_lon = move_point(lat, lon,
                                      bearing_deg(lat, lon, float(port["lat"]),
                                                  float(port["lon"])), reach)
        return f"{new_lat:.2f}, {new_lon:.2f}"

    # ------------------------------------------------------- стоянки
    def visits(self, limit: int = 80) -> list[dict]:
        rows = []
        try:
            con = self.connect()
            rows = [dict(row) for row in con.execute(
                "SELECT v.mmsi, v.port, v.entered_at, v.left_at, v.source, "
                "COALESCE(u.name, '') AS name "
                "FROM port_visits v LEFT JOIN vessels u ON u.mmsi = v.mmsi "
                "ORDER BY v.entered_at DESC LIMIT ?", (max(1, min(limit, 400)),)).fetchall()]
            con.close()
        except sqlite3.Error:
            pass
        now = datetime.now()
        for row in rows:
            entered = parse_moment(row.get("entered_at"))
            left = parse_moment(row.get("left_at"))
            row["hours"] = (round(((left or now) - entered).total_seconds() / 3600, 1)
                            if entered else None)
            row["open"] = not row.get("left_at")
        return rows

    def vessel(self, mmsi) -> dict:
        """Судно целиком: паспорт, позиция, последние точки и стоянки."""
        mmsi = str(mmsi or "").strip()
        for ship in self.query():
            if ship["mmsi"] == mmsi:
                break
        else:
            return {}
        history, visits = [], []
        try:
            con = self.connect()
            history = [dict(row) for row in con.execute(
                "SELECT ts, lat, lon, sog FROM positions WHERE mmsi = ? "
                "ORDER BY ts DESC LIMIT 120", (mmsi,)).fetchall()]
            visits = [dict(row) for row in con.execute(
                "SELECT port, entered_at, left_at, draft_in, draft_out, cargo_kind "
                "FROM port_visits WHERE mmsi = ? ORDER BY entered_at DESC LIMIT 20",
                (mmsi,)).fetchall()]
            con.close()
        except sqlite3.Error:
            pass
        return dict(ship, history=history, visits=visits)

    # ---------------------------------------------------- подключения
    def connections(self) -> dict:
        """Состояние каналов по настройкам варианта run.bat.

        Ключи живут в `ship_tracker/data/config.json`; если этой копии рядом
        нет, показываем настройки той копии, из которой читается база.
        """
        config = self.config
        run_config = app_dir().parent / "ship_tracker" / "data" / "config.json"
        try:
            loaded = json.loads(run_config.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                config = loaded
        except (OSError, ValueError):
            pass

        def has(key):
            return bool(str(config.get(key) or "").strip())

        rows = []

        def add(title, enabled, key=None, detail=""):
            keyed = key is None or has(key)
            if not enabled:
                state, text = "off", "выключено"
            elif keyed:
                state, text = "on", "ключ есть" if key else "включено"
            else:
                state, text = "warn", "нужен ключ"
            rows.append({"title": title, "detail": detail, "state": state,
                         "state_text": text})

        add("AISStream — районный поток", bool(config.get("use_area_feed", True)),
            "api_key", "живой поток wss://stream.aisstream.io")
        add("AISStream — слежение по MMSI", bool(config.get("use_mmsi_watch", False)),
            "api_key", "отдельные подписки на наши суда")
        add("Open Waters — живой поток", bool(config.get("use_openwaters", True)),
            "openwaters_token", "второй живой поток AIS")
        add("Open Waters — снимок района", bool(config.get("use_snapshot", True)),
            None, "позиции судов вокруг портов, ключ не нужен")
        add("GFW — история заходов", bool(config.get("use_gfw", False)),
            "gfw_key", "порт-заходы и паспорта судов")
        add("VesselFinder", bool(config.get("use_vf", False)), "vf_key", "")
        add("VesselFinder AIS", bool(config.get("use_vfa", False)), "vfa_key", "")
        add("MarineTraffic", bool(config.get("use_mt", False)), "mt_key", "")
        add("MyShipTracking", bool(config.get("use_mst", False)), "mst_key", "")
        add("AIS-catcher", bool(config.get("use_aci", True)), None,
            "ожидаемые прибытия по портам")
        add("Goradar", bool(config.get("use_gor", True)), None, "")
        add("MarineTraffic.live", bool(config.get("use_mlive", True)), None, "")
        add("Ship Radar", bool(config.get("use_srd", True)), None, "")
        add("ShipFinder", bool(config.get("use_sfp", True)), None, "")
        add("Maritime Optima", bool(config.get("use_mo", True)), None,
            "паспорт судна с публичной карточки")
        add("ShipAtlas", bool(config.get("use_sa", True)), None,
            "рейс и статус судна с публичной карточки")
        add("MarineLink", bool(config.get("use_mlink", True)), None,
            "тип судна из открытого справочника")
        add("ShipNext", bool(config.get("use_sn", False)), "sn_key",
            "каталог судов по токену аккаунта")
        add("Telegram-бот", has("telegram_token"), "telegram_token", "")

        desktop_version = "—"
        source = app_dir().parent / "ship_tracker" / "ship_tracker.py"
        try:
            match = re.search(r'APP_VERSION\s*=\s*"([^"]+)"',
                              source.read_text(encoding="utf-8"))
            if match:
                desktop_version = match.group(1)
        except OSError:
            pass
        return {
            "rows": rows,
            "version": APP_VERSION,
            "desktop_version": desktop_version,
            "database": str(self.database or ""),
            "note": ("Ключи и подключения задаются в варианте Ship Tracker, который "
                     "запускается через run.bat (папка ship_tracker). Здесь видно "
                     "только состояние этой копии: что включено и есть ли ключ."),
        }


MANIFEST = {
    "name": "Ship Tracker",
    "short_name": "Ship Tracker",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#0b1622",
    "theme_color": "#0b1622",
}


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    data: Data = None

    def log_message(self, fmt, *args):
        return

    def log_error(self, fmt, *args):
        try:
            log_line("HTTP: " + (fmt % args))
        except Exception:
            pass

    def send_bytes(self, body: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload):
        self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        if path == "/":
            self.send_bytes(PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/manifest.webmanifest":
            self.send_bytes(json.dumps(MANIFEST).encode("utf-8"),
                            "application/manifest+json")
            return
        if path == "/api/ships":
            try:
                limit = int((params.get("limit") or ["300"])[0])
            except ValueError:
                limit = 300
            self.send_json(self.data.payload(
                query=(params.get("q") or [""])[0], limit=limit,
                only_pinned=(params.get("pinned") or [""])[0] == "1",
                port=(params.get("port") or [""])[0]))
            return
        if path == "/api/sea":
            self.send_json(self.data.memo("sea", 15.0, self.data.sea))
            return
        if path == "/api/forecast":
            self.send_json(self.data.memo("plan", 20.0, self.data.forecast_plan))
            return
        if path == "/api/forecast_all":
            self.send_json(self.data.memo("all", 30.0, self.data.forecast_all))
            return
        if path == "/api/ports":
            self.send_json({"ports": self.data.port_stats()})
            return
        if path == "/api/connections":
            self.send_json(self.data.connections())
            return
        if path == "/api/vessel":
            self.send_json(self.data.vessel((params.get("mmsi") or [""])[0]))
            return
        if path == "/api/visits":
            try:
                limit = int((params.get("limit") or ["80"])[0])
            except ValueError:
                limit = 80
            self.send_json({"rows": self.data.visits(limit)})
            return
        self.send_bytes(b"not found", "text/plain; charset=utf-8", status=404)


def claim_lock():
    """Один сервер на папку: вторая копия не откроется и не перебьёт порт."""
    path = app_dir() / "shop_tracker_mob.lock"
    try:
        handle = path.open("a+")
    except OSError:
        return None
    try:
        try:
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except ImportError:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def lan_addresses() -> list[str]:
    addresses = []
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            address = info[4][0]
            if address and not address.startswith("127.") and address not in addresses:
                addresses.append(address)
    except OSError:
        pass
    return addresses


def start_discovery(port: int):
    """Отвечает телефону на UDP-поиск: адрес сервера вводить не нужно."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", DISCOVERY_PORT))
    except OSError as error:
        log_line(f"Автопоиск недоступен: {error}")
        return None
    reply = json.dumps({"app": "ship_tracker_mob", "port": port}).encode("utf-8")

    def worker():
        while True:
            try:
                _, address = sock.recvfrom(1024)
                sock.sendto(reply, address)
            except OSError:
                break

    threading.Thread(target=worker, daemon=True, name="discovery").start()
    return sock


def main() -> int:
    try:
        import faulthandler

        crash_log = (app_dir() / "crash.log").open("a", encoding="utf-8")
        faulthandler.enable(file=crash_log)
    except (OSError, ImportError):
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="Ship Tracker для телефона")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--db", default="", help="путь к базе ship_tracker.db")
    args = parser.parse_args()

    lock = claim_lock()
    if lock is None:
        print("Ship Tracker Mobile уже запущен — второе окно не открываю.", flush=True)
        return 0

    data = Data()
    if args.db:
        data.database = Path(args.db)
        data.config = find_config(data.database)
        data.ports = [port for port in (data.config.get("ports") or [])
                      if isinstance(port, dict) and port.get("lat") and port.get("lon")]
        data.pinned = {str(item) for item in (data.config.get("pinned_mmsis") or [])}
    Handler.data = data

    server = None
    for port in range(args.port, args.port + 10):
        try:
            server = Server(("0.0.0.0", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        print("Не удалось занять порт: закройте другое приложение и повторите.", flush=True)
        return 1

    opened_port = server.server_address[1]
    start_discovery(opened_port)
    log_line(f"{APP_NAME} {APP_VERSION}: старт, база {data.database or 'не найдена'}, порт {opened_port}")
    print(f"{APP_NAME} {APP_VERSION}", flush=True)
    print(f"База: {data.database or 'не найдена — покажу пустой список'}", flush=True)
    print(f"Порт: {opened_port}", flush=True)
    print("Откройте на телефоне (тот же Wi-Fi):", flush=True)
    addresses = lan_addresses()
    if addresses:
        for address in addresses:
            print(f"  http://{address}:{opened_port}", flush=True)
    else:
        print(f"  http://<IP-компьютера>:{opened_port}", flush=True)
    print("Остановить: Ctrl+C или закрыть окно.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log_line(f"{APP_NAME} {APP_VERSION}: остановлен")
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
