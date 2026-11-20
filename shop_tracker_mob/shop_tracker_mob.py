"""Ship Tracker Mobile — мобильная версия радара для телефона.

Один exe поднимает небольшой сервер на компьютере: телефон в той же
Wi-Fi сети открывает приложение (или Android-обёртку из этой же папки)
и видит те же суда, порты, ожидаемые прибытия и стоянки, что и на компьютере.
Данные читаются только на чтение из базы Ship Tracker.
"""

from __future__ import annotations

import argparse
import json
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

APP_NAME = "Ship Tracker Mobile"
APP_VERSION = "2.0.0"
DEFAULT_PORT = 8080
DISCOVERY_PORT = 50505


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
    base = app_dir()
    candidates = [
        base / "data" / "ship_tracker.db",
        base / "ship_tracker.db",
        base.parent / "ship_tracker.exe" / "data" / "ship_tracker.db",
        base.parent / "ship_tracker" / "data" / "ship_tracker.db",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


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
    from math import asin, cos, radians, sin, sqrt

    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * 6371.0 * asin(min(1.0, sqrt(a)))


_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def heard_hours(text):
    """«2m ago», «16h 9m ago» -> часы с последнего сигнала."""
    found = re.findall(r"(\d+)\s*([hm])", str(text or ""))
    if not found:
        return None
    total = 0.0
    for value, unit in found:
        total += int(value) / 60 if unit == "m" else int(value)
    return total


def eta_hours_left(text):
    """«17 Oct 12:00» -> часов до ETA (минус — срок прошёл)."""
    parts = str(text or "").strip().split()
    if len(parts) != 3:
        return None
    try:
        day = int(parts[0])
        month = _MONTHS.get(parts[1].strip(".").lower())
        hour, minute = (int(piece) for piece in parts[2].split(":"))
        moment = datetime(datetime.now().year, month, day, hour, minute)
    except (ValueError, TypeError):
        return None
    if moment > datetime.now() + timedelta(days=300):
        try:
            moment = moment.replace(year=moment.year - 1)
        except ValueError:
            return None
    return round((moment - datetime.now()).total_seconds() / 3600, 1)


class Data:
    """Читает базу Ship Tracker и отдаёт готовые данные для телефона."""

    def __init__(self):
        self.database = find_database()
        self.config = find_config(self.database)
        self.ports = [port for port in (self.config.get("ports") or [])
                      if isinstance(port, dict) and port.get("lat") and port.get("lon")]
        self.pinned = {str(item) for item in (self.config.get("pinned_mmsis") or [])}
        self.db_updated = None
        self._cache = None
        self._cache_at = 0.0
        self._lock = threading.Lock()

    def connect(self):
        con = sqlite3.connect(f"file:{self.database}?mode=rw", uri=True, timeout=5)
        con.execute("PRAGMA query_only = 1")
        con.row_factory = sqlite3.Row
        return con

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
                # updated_at меняется при каждом сохранении — по нему видно,
                # обновляет ли базу компьютер прямо сейчас
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
            moving = [ship for ship in near
                      if (ship.get("sog") or 0) >= 0.5]
            result.append({"name": name, "total": len(near), "fresh": len(fresh),
                           "moving": len(moving), "lat": port.get("lat"),
                           "lon": port.get("lon")})
        return result

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

    def visits(self, limit: int = 80) -> list[dict]:
        rows = []
        try:
            con = self.connect()
            rows = [dict(row) for row in con.execute(
                "SELECT v.mmsi, v.port, v.entered_at, v.left_at, v.source, "
                "COALESCE(u.name, '') AS name "
                "FROM port_visits v LEFT JOIN vessels u ON u.mmsi = v.mmsi "
                "ORDER BY v.entered_at DESC LIMIT ?", (max(1, min(limit, 300)),)).fetchall()]
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

    def expected(self) -> dict:
        """Ожидаемые прибытия AIS-catcher (файл expected.json рядом с базой)."""
        cache = {}
        if self.database:
            try:
                cache = json.loads((self.database.parent / "expected.json")
                                   .read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cache = {}
        rows = []
        for locode, entry in (cache.get("ports") or {}).items():
            if not isinstance(entry, dict):
                continue
            for vessel in entry.get("vessels") or []:
                if not isinstance(vessel, dict):
                    continue
                heard = heard_hours(vessel.get("last_heard"))
                eta_left = eta_hours_left(vessel.get("eta"))
                stale = ((heard is not None and heard > 6)
                         or (eta_left is not None and eta_left < -12))
                rows.append({
                    "port": entry.get("name") or locode,
                    "locode": locode,
                    "mmsi": str(vessel.get("mmsi") or ""),
                    "name": vessel.get("name") or "",
                    "from": vessel.get("from") or vessel.get("origin") or "",
                    "eta": vessel.get("eta") or "",
                    "last_heard": vessel.get("last_heard") or "",
                    "heard_hours": heard,
                    "eta_hours": eta_left,
                    "stale": stale,
                })
        rows.sort(key=lambda row: (row["stale"], row["eta"] or ""))
        return {"updated": cache.get("updated") or "",
                "stale": bool(cache.get("stale")), "rows": rows}


PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, maximum-scale=1">
<meta name="theme-color" content="#0b1622">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Ship Tracker</title>
<link rel="manifest" href="/manifest.webmanifest">
<style>
:root { color-scheme: dark; --bg: #0b1622; --card: #10202e; --line: #1d3144;
  --text: #e8f1fa; --dim: #7d95ad; --accent: #4aa8ff; }
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { margin: 0; height: 100%; background: var(--bg); color: var(--text); }
body { font: 16px/1.4 -apple-system, "Segoe UI", Roboto, sans-serif;
  padding-bottom: calc(66px + env(safe-area-inset-bottom)); }
header { position: sticky; top: 0; z-index: 8; background: rgba(11,22,34,.96);
  backdrop-filter: blur(8px); padding: calc(8px + env(safe-area-inset-top)) 12px 8px; }
h1 { font-size: 18px; margin: 0 0 2px; font-weight: 650; }
.sub { font-size: 12px; color: var(--dim); display: flex; align-items: center; gap: 6px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: #34c759; }
.dot.stale { background: #ff9f0a; } .dot.off { background: #ff453a; }
main { padding: 0 12px; }
input[type=search] { width: 100%; padding: 12px 14px; font-size: 16px; border-radius: 14px;
  border: 1px solid var(--line); background: #12222f; color: var(--text); outline: none; margin-top: 8px; }
input[type=search]::placeholder { color: #6d8299; }
.chips { display: flex; gap: 8px; overflow-x: auto; padding: 10px 0 4px; scrollbar-width: none; }
.chips::-webkit-scrollbar { display: none; }
.chip { flex: 0 0 auto; padding: 7px 13px; border-radius: 999px; border: 1px solid var(--line);
  background: #0f1e2b; color: #a9c2d8; font-size: 13px; }
.chip.on { background: #1e4f7a; border-color: #2f6c9e; color: #fff; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 16px;
  padding: 12px 14px; margin-bottom: 9px; }
.card.pinned { border-color: #3f7fae; }
.row { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; }
.name { font-size: 16px; font-weight: 650; }
.star { color: #ffd166; margin-left: 4px; }
.small { font-size: 11px; color: #6f879e; }
.sog { font-size: 15px; font-weight: 600; color: #9fd0ff; white-space: nowrap; }
.meta { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 6px; }
.tag { font-size: 12px; padding: 3px 9px; border-radius: 999px; background: #162a3b; color: #a9c2d8; }
.tag.warn { background: #3a2a12; color: #ffcf7d; }
.tag.bad { background: #3a1a1a; color: #ff9c94; }
.tag.ok { background: #12301f; color: #8ee0ab; }
.hint { font-size: 12px; color: var(--dim); margin: 6px 2px 10px; }
.empty { text-align: center; color: var(--dim); padding: 40px 10px; font-size: 14px; }
nav { position: fixed; left: 0; right: 0; bottom: 0; z-index: 9; display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  background: rgba(13,26,38,.98); border-top: 1px solid var(--line);
  padding: 6px 4px calc(6px + env(safe-area-inset-bottom)); }
nav button { min-width: 0; background: none; border: 0; color: #7d95ad;
  font-size: 10px; display: flex; flex-direction: column; align-items: center; gap: 2px;
  padding: 4px 0; white-space: nowrap; }
nav button.on { color: var(--accent); }
nav svg { width: 22px; height: 22px; }
.sheet { position: fixed; inset: 0; z-index: 20; display: none; }
.sheet.open { display: block; }
.sheet .backdrop { position: absolute; inset: 0; background: rgba(0,0,0,.55); }
.sheet .panel { position: absolute; left: 0; right: 0; bottom: 0; max-height: 88%;
  overflow-y: auto; background: #0e1c29; border-radius: 18px 18px 0 0;
  padding: 14px 14px calc(18px + env(safe-area-inset-bottom)); border-top: 1px solid var(--line); }
.sheet .handle { width: 42px; height: 4px; border-radius: 2px; background: #2b4256;
  margin: 0 auto 10px; }
.kv { display: grid; grid-template-columns: 130px 1fr; gap: 4px 8px; font-size: 14px; margin-top: 10px; }
.kv dt { color: var(--dim); } .kv dd { margin: 0; }
canvas { width: 100%; display: block; border-radius: 16px; background: #0a1a27; }
#map { height: calc(100vh - 300px); min-height: 280px; max-height: 640px; }
.portRow { display: flex; justify-content: space-between; align-items: center; }
a { color: #7cc4ff; }
</style>
</head>
<body>
<header>
  <h1>Ship Tracker</h1>
  <div class="sub"><span class="dot" id="dot"></span><span id="status">подключаюсь…</span></div>
  <input id="search" type="search" placeholder="Поиск: имя, назначение, MMSI" autocomplete="off">
</header>
<main id="view"></main>
<nav>
  <button data-tab="map" class="on"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M9 3 3 5v16l6-2 6 2 6-2V3l-6 2-6-2Z"/><path d="M9 3v16M15 5v16"/></svg>Карта</button>
  <button data-tab="ships"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 17h18M5 17V9l7-5 7 5v8"/><path d="M9 17v-4h6v4"/></svg>Суда</button>
  <button data-tab="ports"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/></svg>Порты</button>
  <button data-tab="expected"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Ожид.</button>
  <button data-tab="visits"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 6h16M4 12h16M4 18h10"/></svg>Стоянки</button>
</nav>
<div class="sheet" id="sheet"><div class="backdrop" data-close></div>
  <div class="panel"><div class="handle"></div><div id="sheetBody"></div></div>
</div>
<script>
const view = document.getElementById('view');
const statusEl = document.getElementById('status');
const dot = document.getElementById('dot');
const search = document.getElementById('search');
const sheet = document.getElementById('sheet');
const sheetBody = document.getElementById('sheetBody');
let tab = 'map', data = null, portFilter = '', timer = null, mapShips = [];

const esc = v => String(v ?? '').replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
function ago(m) {
  if (m === null || m === undefined) return 'нет данных';
  if (m < 2) return 'только что';
  if (m < 60) return m + ' мин назад';
  const h = Math.round(m / 60);
  if (h < 24) return h + ' ч назад';
  return Math.round(h / 24) + ' дн назад';
}
const etaText = v => v ? String(v).slice(0, 16).replace('T', ' ') : '';
const fmtCoord = v => Number(v).toFixed(5);

async function load() {
  try {
    const r = await fetch('/api/ships?limit=2000', {cache: 'no-store'});
    data = await r.json();
    const age = data.db_age_minutes;
    dot.className = 'dot' + (age === null || age > 15 ? ' off' : age > 5 ? ' stale' : '');
    statusEl.textContent = (age === null ? 'база не обновляется' : 'база обновлена ' + ago(age))
      + ' · судов ' + data.total;
    render();
  } catch (e) {
    dot.className = 'dot off';
    statusEl.textContent = 'нет связи с компьютером';
    view.innerHTML = '<div class="empty">Нет связи.<br>Проверьте, что Ship Tracker запущен на компьютере,<br>и телефон в той же Wi-Fi сети.</div>';
  }
}

function shipCard(s) {
  const stale = s.age_minutes !== null && s.age_minutes > 180;
  const tags = [];
  if (s.destination) tags.push(`<span class="tag">→ ${esc(s.destination)}</span>`);
  if (s.near_port) tags.push(`<span class="tag ok">${esc(s.near_port)}</span>`);
  if (s.type_label) tags.push(`<span class="tag">${esc(s.type_label)}</span>`);
  tags.push(`<span class="tag ${stale ? 'bad' : ''}">${ago(s.age_minutes)}</span>`);
      const sog = s.sog == null ? '—' : Number(s.sog).toFixed(1);
  return `<article class="card ${s.pinned ? 'pinned' : ''}" data-mmsi="${esc(s.mmsi)}">
    <div class="row"><div><div class="name">${esc(s.name || 'Без имени')}${s.pinned ? '<span class="star">★</span>' : ''}</div>
    <div class="small">MMSI ${esc(s.mmsi)}${s.imo ? ' · IMO ' + esc(s.imo) : ''}</div></div>
    <div class="sog">${sog} уз</div></div>
    <div class="meta">${tags.join('')}</div></article>`;
}

function renderMap() {
  const ports = data.ports.filter(p => p.lat && p.lon);
  const ships = data.ships.filter(s => s.age_minutes !== null && s.age_minutes <= 720);
  mapShips = ships;
  view.innerHTML = `<div class="hint">Порты и суда за последние 12 часов. Нажмите на точку.</div>
    <canvas id="map" height="520"></canvas>
    <div class="meta" style="margin:10px 2px">
      <span class="tag ok">● у порта</span><span class="tag">● в море</span>
      <span class="tag bad">● сигнал старше 3 ч</span></div>`;
  const canvas = document.getElementById('map');
  drawMap(canvas, ports, ships);
  window.addEventListener('resize', () => drawMap(canvas, ports, ships), {once: true});
  canvas.addEventListener('click', ev => pickShip(canvas, ev));
}

function mapProjection(canvas, ports, ships) {
  const pts = ports.map(p => [Number(p.lat), Number(p.lon)])
    .concat(ships.map(s => [s.lat, s.lon]));
  let minLat = 90, maxLat = -90, minLon = 180, maxLon = -180;
  pts.forEach(([la, lo]) => { minLat = Math.min(minLat, la); maxLat = Math.max(maxLat, la);
    minLon = Math.min(minLon, lo); maxLon = Math.max(maxLon, lo); });
  const padLat = Math.max(0.25, (maxLat - minLat) * 0.12);
  const padLon = Math.max(0.25, (maxLon - minLon) * 0.12);
  minLat -= padLat; maxLat += padLat; minLon -= padLon; maxLon += padLon;
  const w = canvas.width, h = canvas.height;
  return ([la, lo]) => [ (lo - minLon) / (maxLon - minLon) * w,
                         h - (la - minLat) / (maxLat - minLat) * h ];
}
function drawMap(canvas, ports, ships) {
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 360;
  const height = Math.max(260, Math.round(canvas.clientHeight || 340));
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  const g = canvas.getContext('2d');
  g.scale(dpr, dpr);
  g.clearRect(0, 0, width, height);
  const project = mapProjection({width, height}, ports, ships);
  g.strokeStyle = 'rgba(70,110,145,.18)';
  for (let i = 0; i <= 6; i++) {
    g.beginPath(); g.moveTo(0, i * height / 6); g.lineTo(width, i * height / 6); g.stroke();
    g.beginPath(); g.moveTo(i * width / 6, 0); g.lineTo(i * width / 6, height); g.stroke();
  }
  ships.forEach(s => {
    const [x, y] = project([s.lat, s.lon]);
    const near = !!s.near_port;
    g.beginPath(); g.arc(x, y, near ? 4 : 3, 0, Math.PI * 2);
    g.fillStyle = (s.age_minutes ?? 999) > 180 ? '#ff9c94' : near ? '#8ee0ab' : '#7cc4ff';
    g.globalAlpha = 0.85; g.fill(); g.globalAlpha = 1;
  });
  const boxes = [];
  const labels = ports.slice().sort((a, b) => (b.total || 0) - (a.total || 0));
  // сначала все отметки портов, потом подписи: иначе маркер соседа ложится на текст
  ports.forEach(p => {
    const [x, y] = project([Number(p.lat), Number(p.lon)]);
    g.fillStyle = '#ffd166';
    g.fillRect(x - 3, y - 3, 6, 6);
  });
  labels.forEach(p => {
    const [x, y] = project([Number(p.lat), Number(p.lon)]);
    g.font = '600 10px -apple-system, Segoe UI, sans-serif';
    const text = p.name;
    const w = g.measureText(text).width;
    const tries = [[7, 4], [7, -12], [7, 20], [7, -28], [7, 36]];
    for (const [dx, dy] of tries) {
      const left = x + dx - 2, top = y + dy - 10;
      const box = [left, top, left + w + 5, top + 14];
      if (box[0] < 0 || box[2] > width) continue;
      const clash = boxes.some(b => !(box[2] < b[0] || box[0] > b[2] || box[3] < b[1] || box[1] > b[3]));
      if (clash) continue;
      boxes.push(box);
      g.fillStyle = 'rgba(11,22,34,.82)';
      g.fillRect(box[0], box[1], box[2] - box[0], box[3] - box[1]);
      g.fillStyle = '#e8f1fa';
      g.fillText(text, x + dx, y + dy);
      break;
    }
  });
  canvas._project = project;
}
function pickShip(canvas, ev) {
  const rect = canvas.getBoundingClientRect();
  const x = ev.clientX - rect.left, y = ev.clientY - rect.top;
  let best = null, bestD = 22;
  mapShips.forEach(s => {
    const [sx, sy] = canvas._project([s.lat, s.lon]);
    const d = Math.hypot(sx - x, sy - y);
    if (d < bestD) { best = s; bestD = d; }
  });
  if (best) openVessel(best.mmsi);
}

function renderShips() {
  let ships = data.ships;
  if (portFilter) ships = ships.filter(s => (s.near_port || s.port || '').toLowerCase() === portFilter.toLowerCase());
  const q = search.value.trim().toLowerCase();
  if (q) ships = ships.filter(s => (s.name || '').toLowerCase().includes(q)
    || (s.destination || '').toLowerCase().includes(q) || s.mmsi === q);
  view.innerHTML = (portFilter ? `<div class="hint">Порт: <b>${esc(portFilter)}</b> · <a href="#" id="clearPort">показать все</a></div>` : '')
    + (ships.length ? ships.slice(0, 400).map(shipCard).join('') : '<div class="empty">Ничего не найдено</div>');
  const clear = document.getElementById('clearPort');
  if (clear) clear.addEventListener('click', e => { e.preventDefault(); portFilter = ''; render(); });
}

function renderPorts() {
  view.innerHTML = data.ports.map(p => `<article class="card" data-port="${esc(p.name)}">
    <div class="portRow"><div><div class="name">${esc(p.name)}</div>
    <div class="small">${p.total} судов у порта · ${p.fresh} со свежим сигналом</div></div>
    <div class="sog">${p.moving} в ходу</div></div></article>`).join('')
    || '<div class="empty">Порты не настроены</div>';
  view.querySelectorAll('[data-port]').forEach(el => el.addEventListener('click', () => {
    portFilter = el.dataset.port; tab = 'ships'; setTab('ships');
  }));
}

async function renderExpected() {
  view.innerHTML = '<div class="empty">Загружаю…</div>';
  try {
    const r = await fetch('/api/expected', {cache: 'no-store'});
    const exp = await r.json();
    const rows = exp.rows || [];
    if (!rows.length) { view.innerHTML = '<div class="empty">Ожидаемых прибытий нет</div>'; return; }
    view.innerHTML = `<div class="hint">Обновлено: ${esc((exp.updated || '').slice(0, 16).replace('T', ' '))}. Устаревшие скрыты.</div>`
      + rows.filter(r => !r.stale).map(r => `<article class="card">
        <div class="row"><div><div class="name">${esc(r.name || r.mmsi)}</div>
        <div class="small">${esc(r.from || '—')} → ${esc(r.port)}</div></div>
        <div class="sog">${esc(r.eta || '—')}</div></div>
        <div class="meta"><span class="tag">сигнал ${esc(r.last_heard || '—')}</span>
        <span class="tag">MMSI ${esc(r.mmsi)}</span></div></article>`).join('')
      || '<div class="empty">Все ожидания устарели</div>';
  } catch (e) { view.innerHTML = '<div class="empty">Не удалось получить ожидаемые</div>'; }
}

async function renderVisits() {
  view.innerHTML = '<div class="empty">Загружаю…</div>';
  try {
    const r = await fetch('/api/visits', {cache: 'no-store'});
    const data2 = await r.json();
    const rows = data2.rows || [];
    view.innerHTML = rows.length ? rows.map(v => `<article class="card">
      <div class="row"><div><div class="name">${esc(v.name || v.mmsi)}</div>
      <div class="small">${esc(v.port)} · ${esc((v.entered_at || '').slice(0, 16).replace('T', ' '))}</div></div>
      <div class="sog">${v.hours === null ? '—' : v.hours + ' ч'}</div></div>
      <div class="meta"><span class="tag ${v.open ? 'ok' : ''}">${v.open ? 'стоит сейчас' : 'ушло'}</span>
      <span class="tag">MMSI ${esc(v.mmsi)}</span></div></article>`).join('')
      : '<div class="empty">Стоянок пока нет</div>';
  } catch (e) { view.innerHTML = '<div class="empty">Не удалось получить стоянки</div>'; }
}

function render() {
  if (!data) return;
  if (tab === 'map') renderMap();
  else if (tab === 'ships') renderShips();
  else if (tab === 'ports') renderPorts();
  else if (tab === 'expected') renderExpected();
  else renderVisits();
}

function setTab(name) {
  tab = name;
  if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  document.querySelectorAll('nav button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
  search.style.display = name === 'ships' ? 'block' : 'none';
  render();
}
document.querySelectorAll('nav button').forEach(b => b.addEventListener('click', () => setTab(b.dataset.tab)));
search.addEventListener('input', () => { if (tab === 'ships') renderShips(); });

async function openVessel(mmsi) {
  sheet.classList.add('open');
  sheetBody.innerHTML = '<div class="empty">Загружаю…</div>';
  try {
    const r = await fetch('/api/vessel?mmsi=' + encodeURIComponent(mmsi), {cache: 'no-store'});
    const v = await r.json();
    if (!v.mmsi) { sheetBody.innerHTML = '<div class="empty">Судно не найдено</div>'; return; }
    const coords = `${fmtCoord(v.lat)}, ${fmtCoord(v.lon)}`;
    const mapUrl = `https://www.google.com/maps?q=${v.lat},${v.lon}`;
    const hist = (v.history || []).slice().reverse();
    const visits = (v.visits || []).map(p => `<div>${esc(p.port)} · ${esc((p.entered_at || '').slice(0, 16).replace('T', ' '))}${p.left_at ? ' → ' + esc((p.left_at || '').slice(0, 16).replace('T', ' ')) : ' · стоит'}</div>`).join('');
    sheetBody.innerHTML = `
      <div class="row"><div><div class="name" style="font-size:19px">${esc(v.name || 'Без имени')}</div>
      <div class="small">MMSI ${esc(v.mmsi)}${v.imo ? ' · IMO ' + esc(v.imo) : ''}${v.callsign ? ' · ' + esc(v.callsign) : ''}</div></div>
      <div class="sog">${v.sog == null ? '—' : Number(v.sog).toFixed(1)} уз</div></div>
      <div class="meta">
        ${v.destination ? `<span class="tag">→ ${esc(v.destination)}</span>` : ''}
        ${v.near_port ? `<span class="tag ok">${esc(v.near_port)}</span>` : ''}
        <span class="tag ${(v.age_minutes ?? 99) > 180 ? 'bad' : ''}">${ago(v.age_minutes)}</span>
      </div>
      <dl class="kv">
        <dt>Координаты</dt><dd><a href="${mapUrl}" target="_blank" rel="noopener">${coords}</a></dd>
        <dt>Курс / скорость</dt><dd>${v.cog ?? '—'}° · ${v.sog ?? '—'} уз</dd>
        <dt>Назначение</dt><dd>${esc(v.destination || '—')}</dd>
        <dt>ETA по AIS</dt><dd>${esc(etaText(v.eta_declared) || '—')}</dd>
        <dt>Осадка</dt><dd>${v.draft ?? '—'} м</dd>
        <dt>Размеры</dt><dd>${v.length_m ?? '—'} × ${v.beam_m ?? '—'} м</dd>
        <dt>Тип</dt><dd>${esc(v.type_label || '—')}</dd>
        <dt>До порта</dt><dd>${v.distance_km ?? '—'} км</dd>
      </dl>
      <div class="hint" style="margin-top:14px">История позиций (${hist.length})</div>
      <canvas id="track" height="120"></canvas>
      <div class="hint" style="margin-top:14px">Стоянки</div>
      ${visits || '<div class="small">нет записей</div>'}`;
    drawTrack(document.getElementById('track'), hist);
  } catch (e) { sheetBody.innerHTML = '<div class="empty">Не удалось загрузить судно</div>'; }
}
function drawTrack(canvas, points) {
  if (!canvas || points.length < 2) return;
  const dpr = window.devicePixelRatio || 1, width = canvas.clientWidth || 320;
  canvas.width = width * dpr; canvas.height = 110 * dpr;
  canvas.style.height = '110px';
  const g = canvas.getContext('2d'); g.scale(dpr, dpr);
  const lat = points.map(p => p.lat), lon = points.map(p => p.lon);
  const minLat = Math.min(...lat), maxLat = Math.max(...lat);
  const minLon = Math.min(...lon), maxLon = Math.max(...lon);
  const sx = v => 8 + (v - minLon) / (maxLon - minLon || 1) * (width - 16);
  const sy = v => 100 - (v - minLat) / (maxLat - minLat || 1) * 88;
  g.strokeStyle = '#4aa8ff'; g.lineWidth = 2; g.beginPath();
  points.forEach((p, i) => i ? g.lineTo(sx(p.lon), sy(p.lat)) : g.moveTo(sx(p.lon), sy(p.lat)));
  g.stroke();
  const last = points[points.length - 1];
  g.fillStyle = '#ffd166'; g.beginPath(); g.arc(sx(last.lon), sy(last.lat), 4, 0, 7); g.fill();
}
document.getElementById('sheet').addEventListener('click', e => {
  if (e.target.dataset.close !== undefined) sheet.classList.remove('open');
});
view.addEventListener('click', e => {
  const card = e.target.closest('[data-mmsi]');
  if (card) { openVessel(card.dataset.mmsi); return; }
});

search.style.display = 'none';
if (location.hash) setTab(location.hash.slice(1));
load();
timer = setInterval(load, 20000);
</script>
</body>
</html>
"""

MANIFEST = {
    "name": "Ship Tracker",
    "short_name": "Ship Tracker",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#0b1622",
    "theme_color": "#0b1622",
}


class Server(ThreadingHTTPServer):
    """Порт освобождается сразу после закрытия (SO_REUSEADDR)."""

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
            page = PAGE.replace("{{VERSION}}", APP_VERSION)
            self.send_bytes(page.encode("utf-8"), "text/html; charset=utf-8")
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
                query=(params.get("q") or [""])[0],
                limit=limit,
                only_pinned=(params.get("pinned") or [""])[0] == "1",
                port=(params.get("port") or [""])[0]))
            return
        if path == "/api/ports":
            self.send_json({"ports": self.data.port_stats()})
            return
        if path == "/api/vessel":
            self.send_json(self.data.vessel((params.get("mmsi") or [""])[0]))
            return
        if path == "/api/expected":
            self.send_json(self.data.expected())
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
        print("Ship Tracker Mobile уже запущен — второе окно не открываю.",
              flush=True)
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
