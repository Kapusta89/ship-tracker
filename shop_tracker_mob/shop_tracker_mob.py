"""Ship Tracker Mobile — мобильная версия радара для телефона.

Один exe поднимает небольшой сервер на компьютере: телефон в той же
Wi-Fi сети открывает адрес из консоли, и приложение работает как обычная
мобильная страница (её можно добавить на главный экран). Данные читаются
только на чтение из базы Ship Tracker, поэтому окно на компьютере и
телефон показывают одно и то же.
"""

from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_NAME = "Ship Tracker Mobile"
APP_VERSION = "1.2.0"
DEFAULT_PORT = 8080


def log_line(text: str) -> None:
    """Пишет строку в shop_tracker_mob.log рядом с exe (как в основных копиях)."""
    try:
        with (app_dir() / "shop_tracker_mob.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


def app_dir() -> Path:
    """Папка рядом с exe (или с исходником при запуске из Python."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_database() -> Path | None:
    """Ищет базу Ship Tracker: рядом и в соседних папках приложения."""
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
    """Настройки основного приложения: порты и закреплённые суда."""
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


class Data:
    """Читает базу Ship Tracker и отдаёт готовый список судов."""

    def __init__(self):
        self.database = find_database()
        self.config = find_config(self.database)
        self.ports = [port for port in (self.config.get("ports") or [])
                      if isinstance(port, dict) and port.get("lat") and port.get("lon")]
        self.pinned = {str(item) for item in (self.config.get("pinned_mmsis") or [])}
        self._cache = None
        self._cache_at = 0.0
        self._lock = threading.Lock()

    def query(self) -> list[dict]:
        with self._lock:
            if self._cache is not None and time.monotonic() - self._cache_at < 2.0:
                return self._cache
        rows = []
        self.db_updated = None
        if self.database and self.database.exists():
            try:
                # mode=rw + query_only, а не mode=ro: в режиме только чтения
                # SQLite может не увидеть свежие кадры WAL, которые ещё не
                # перенесены в основной файл, и телефон показывал вчерашние
                # координаты, хотя база на компьютере уже обновилась.
                con = sqlite3.connect(
                    f"file:{self.database}?mode=rw", uri=True, timeout=5)
                con.execute("PRAGMA query_only = 1")
                con.row_factory = sqlite3.Row
                rows = con.execute(
                    "SELECT mmsi, name, imo, type_label, destination, draft, "
                    "eta_declared, lat, lon, sog, cog, heading, port, "
                    "position_at, updated_at FROM vessels").fetchall()
                # updated_at меняется при каждом сохранении (в отличие от
                # position_at — времени сигнала судна), поэтому честно
                # показывает, обновляет ли базу компьютер прямо сейчас
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
        # Сначала самые свежие: закреплённое судно получает звёздочку, но не
        # тащит свою старую позицию в начало списка. Иначе телефон открывался
        # на судне, которое молчит сутки, и казалось, что данные устарели.
        ships.sort(key=lambda item: (item["age_minutes"] if item["age_minutes"] is not None else 10 ** 9,
                                     not item["pinned"],
                                     str(item.get("name") or item["mmsi"])))
        with self._lock:
            self._cache = ships
            self._cache_at = time.monotonic()
        return ships

    def payload(self, query: str = "", limit: int = 300, only_pinned: bool = False) -> dict:
        ships = self.query()
        if only_pinned:
            ships = [ship for ship in ships if ship["pinned"]]
        text = query.strip().lower()
        if text:
            ships = [ship for ship in ships
                     if text in str(ship.get("name") or "").lower()
                     or text in str(ship.get("destination") or "").lower()
                     or text == ship["mmsi"]]
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
            "ports": [{"name": str(port.get("name") or "")} for port in self.ports],
            "ships": ships[:max(1, min(limit, 1000))],
        }


PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b1622">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Ship Tracker Mobile</title>
<link rel="manifest" href="/manifest.webmanifest">
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
body {
  margin: 0; background: #0b1622; color: #e8f1fa;
  font: 16px/1.45 -apple-system, "Segoe UI", Roboto, sans-serif;
  padding: max(10px, env(safe-area-inset-top)) 12px max(18px, env(safe-area-inset-bottom));
}
header { position: sticky; top: 0; z-index: 5; background: #0b1622; padding-bottom: 8px; }
h1 { font-size: 19px; margin: 6px 0 2px; font-weight: 650; letter-spacing: .2px; }
.sub { font-size: 12px; color: #7d95ad; margin-bottom: 10px; }
.sub b { color: #9fd0ff; font-weight: 600; }
.state { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  background: #34c759; margin-right: 6px; vertical-align: 1px; }
.state.stale { background: #ff9f0a; }
.state.off { background: #ff453a; }
input[type=search] {
  width: 100%; padding: 13px 14px; font-size: 16px; border-radius: 14px;
  border: 1px solid #22384d; background: #12222f; color: #e8f1fa; outline: none;
}
input[type=search]::placeholder { color: #6d8299; }
.tabs { display: flex; gap: 8px; margin: 10px 0 4px; overflow-x: auto; scrollbar-width: none; }
.tabs::-webkit-scrollbar { display: none; }
.tab {
  flex: 0 0 auto; padding: 8px 14px; border-radius: 999px; border: 1px solid #22384d;
  background: #0f1e2b; color: #a9c2d8; font-size: 14px;
}
.tab.active { background: #1e4f7a; border-color: #2f6c9e; color: #fff; }
.count { font-size: 12px; color: #7d95ad; margin: 8px 2px; }
.opt { display: flex; align-items: center; gap: 8px; font-size: 13px; color: #9fb6cc; margin: 8px 2px 0; }
.opt input { width: 18px; height: 18px; accent-color: #3aa0ff; }
.card {
  background: #10202e; border: 1px solid #1d3144; border-radius: 16px;
  padding: 12px 14px; margin-bottom: 9px;
}
.card.pinned { border-color: #3f7fae; box-shadow: 0 0 0 1px #265a83 inset; }
.row { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; }
.name { font-size: 16px; font-weight: 650; }
.star { color: #ffd166; margin-left: 4px; }
.mmsi { font-size: 11px; color: #6f879e; }
.sog { font-size: 15px; font-weight: 600; color: #9fd0ff; white-space: nowrap; }
.meta { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 6px; }
.chip { font-size: 12px; padding: 3px 9px; border-radius: 999px; background: #162a3b; color: #a9c2d8; }
.chip.warn { background: #3a2a12; color: #ffcf7d; }
.chip.bad { background: #3a1a1a; color: #ff9c94; }
.detail { display: none; margin-top: 10px; padding-top: 9px; border-top: 1px dashed #22384d;
  font-size: 13px; color: #b8cbdd; }
.card.open .detail { display: block; }
.detail div { margin: 3px 0; }
a { color: #7cc4ff; }
footer { text-align: center; font-size: 12px; color: #5f7690; margin-top: 16px; }
</style>
</head>
<body>
<header>
  <h1>Ship Tracker Mobile</h1>
  <div class="sub"><span class="state" id="state"></span><span id="status">подключаюсь…</span></div>
  <input id="search" type="search" placeholder="Поиск: имя, назначение или MMSI" autocomplete="off">
  <div class="tabs">
    <button class="tab active" data-tab="near">У портов</button>
    <button class="tab" data-tab="pinned">Закреплённые</button>
    <button class="tab" data-tab="all">Все суда</button>
  </div>
  <label class="opt"><input id="fresh" type="checkbox" checked> только свежие (до 3 ч)</label>
  <div class="count" id="count"></div>
</header>
<main id="list"></main>
<footer>Данные с компьютера по Wi-Fi · обновление каждые 20 с · v{{VERSION}}</footer>
<script>
const list = document.getElementById('list');
const statusEl = document.getElementById('status');
const countEl = document.getElementById('count');
const stateEl = document.getElementById('state');
const search = document.getElementById('search');
const fresh = document.getElementById('fresh');
let tab = 'near', timer = null;

function ago(minutes) {
  if (minutes === null || minutes === undefined) return 'время неизвестно';
  if (minutes < 2) return 'только что';
  if (minutes < 60) return minutes + ' мин назад';
  const hours = Math.round(minutes / 60);
  if (hours < 24) return hours + ' ч назад';
  return Math.round(hours / 24) + ' дн назад';
}
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, s => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
}
function card(ship) {
  const stale = ship.age_minutes !== null && ship.age_minutes > 180;
  const chips = [];
  if (ship.destination) chips.push(`<span class="chip">→ ${esc(ship.destination)}</span>`);
  if (ship.port) chips.push(`<span class="chip">${esc(ship.port)}</span>`);
  if (ship.eta_declared) {
    const eta = new Date(ship.eta_declared);
    const late = !isNaN(eta) && eta < new Date();
    chips.push(`<span class="chip ${late ? 'warn' : ''}">ETA ${esc(ship.eta_declared.slice(0, 16).replace('T', ' '))}${late ? ' (прошла)' : ''}</span>`);
  }
  if (ship.type_label) chips.push(`<span class="chip">${esc(ship.type_label)}</span>`);
  if (ship.age_minutes !== null) {
    chips.push(`<span class="chip ${stale ? 'bad' : ''}">сигнал ${ago(ship.age_minutes)}</span>`);
  }
  const sog = ship.sog === null || ship.sog === undefined ? '—' : Number(ship.sog).toFixed(1);
  const coords = `${Number(ship.lat).toFixed(5)}, ${Number(ship.lon).toFixed(5)}`;
  const maps = `https://www.google.com/maps?q=${ship.lat},${ship.lon}`;
  return `<article class="card ${ship.pinned ? 'pinned' : ''}" data-card>
    <div class="row">
      <div><div class="name">${esc(ship.name || 'Без имени')}${ship.pinned ? '<span class="star">★</span>' : ''}</div>
      <div class="mmsi">MMSI ${esc(ship.mmsi)}${ship.imo ? ' · IMO ' + esc(ship.imo) : ''}</div></div>
      <div class="sog">${sog} уз</div>
    </div>
    <div class="meta">${chips.join('')}</div>
    <div class="detail">
      <div>Координаты: <a href="${maps}" target="_blank" rel="noopener">${coords}</a></div>
      <div>Курс: ${ship.cog ?? '—'}° · Осадка: ${ship.draft ?? '—'} м</div>
      <div>Обновлено в базе: ${esc((ship.updated_at || '').replace('T', ' ').slice(0, 16)) || '—'}</div>
    </div>
  </article>`;
}
function render(data) {
  let ships = data.ships;
  if (tab === 'near') ships = ships.filter(s => s.near_port || s.port);
  if (tab === 'pinned') ships = ships.filter(s => s.pinned);
  if (fresh.checked) ships = ships.filter(s => s.age_minutes !== null && s.age_minutes <= 180);
  countEl.textContent = `Показано ${ships.length} из ${data.total} в базе`;
  list.innerHTML = ships.length ? ships.map(card).join('')
    : '<div class="card">Ничего не найдено</div>';
  const newest = data.ships.reduce((min, s) => {
    if (s.age_minutes === null) return min;
    return min === null || s.age_minutes < min ? s.age_minutes : min;
  }, null);
  const dbAge = data.db_age_minutes;
  stateEl.className = 'state' + (dbAge === null || dbAge > 15 ? ' off' : dbAge > 5 ? ' stale' : '');
  const base = dbAge === null ? 'база не обновляется' : `база обновлена ${ago(dbAge)}`;
  statusEl.textContent = `${base} · свежий сигнал ${newest === null ? '—' : ago(newest)}`;
}
async function load() {
  try {
    const params = new URLSearchParams({q: search.value, limit: 400});
    const response = await fetch('/api/ships?' + params.toString(), {cache: 'no-store'});
    render(await response.json());
  } catch (error) {
    stateEl.className = 'state off';
    statusEl.textContent = 'нет связи с компьютером';
  }
}
list.addEventListener('click', event => {
  const card = event.target.closest('[data-card]');
  if (card && !event.target.closest('a')) card.classList.toggle('open');
});
document.querySelectorAll('.tab').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(item => item.classList.remove('active'));
    button.classList.add('active');
    tab = button.dataset.tab;
    load();
  });
});
fresh.addEventListener('change', load);
let wait = null;
search.addEventListener('input', () => {
  clearTimeout(wait);
  wait = setTimeout(load, 250);
});
load();
timer = setInterval(load, 20000);
</script>
</body>
</html>
"""

MANIFEST = {
    "name": "Ship Tracker Mobile",
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
    data: Data = None  # задаётся при запуске сервера

    def log_message(self, fmt, *args):
        return

    def log_error(self, fmt, *args):
        """Ошибки HTTP — в файл: консоль можно закрыть, а причину надо видеть."""
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

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            page = PAGE.replace("{{VERSION}}", APP_VERSION)
            self.send_bytes(page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/manifest.webmanifest":
            self.send_bytes(json.dumps(MANIFEST).encode("utf-8"),
                            "application/manifest+json")
            return
        if path == "/api/ships":
            params = parse_qs(parsed.query)
            try:
                limit = int((params.get("limit") or ["300"])[0])
            except ValueError:
                limit = 300
            payload = self.data.payload(
                query=(params.get("q") or [""])[0],
                limit=limit,
                only_pinned=(params.get("pinned") or [""])[0] == "1")
            self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                            "application/json; charset=utf-8")
            return
        self.send_bytes(b"not found", "text/plain; charset=utf-8", status=404)


def claim_lock():
    """Один сервер на папку: вторая копия не откроется и не перебьёт порт.

    Без этого Windows разрешает двум процессам слушать один порт, и запросы
    попадают то в одну, то в другую копию — телефон показывает то свежие
    данные, то старые.
    """
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


def main() -> int:
    # тяжёлое падение (сегфолт, память) не оставляет Python-ошибки:
    # faulthandler пишет трассировку в crash.log рядом с exe
    try:
        import faulthandler

        crash_log = (app_dir() / "crash.log").open("a", encoding="utf-8")
        faulthandler.enable(file=crash_log)
    except (OSError, ImportError):
        pass
    # консоль Windows по умолчанию в cp866: без этого адрес и подписи
    # печатаются «кракозябрами», и ссылку для телефона не прочитать
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

    log_line(f"{APP_NAME} {APP_VERSION}: старт, база {data.database or 'не найдена'}")
    print(f"{APP_NAME} {APP_VERSION}", flush=True)
    print(f"База: {data.database or 'не найдена — покажу пустой список'}", flush=True)
    print(f"Порт: {server.server_address[1]}", flush=True)
    print("Откройте на телефоне (тот же Wi-Fi):", flush=True)
    addresses = lan_addresses()
    if addresses:
        for address in addresses:
            print(f"  http://{address}:{server.server_address[1]}", flush=True)
    else:
        print(f"  http://<IP-компьютера>:{server.server_address[1]}", flush=True)
    print("Остановить: Ctrl+C или закрыть окно.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        log_line(f"Сервер остановлен ошибкой: {type(error).__name__}: {error}")
        raise
    finally:
        log_line(f"{APP_NAME} {APP_VERSION}: остановлен")
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
