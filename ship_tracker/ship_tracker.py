"""Ship Tracker — рабочее приложение наблюдения за судами по живому AIS.

Данные: AISStream (бесплатный ключ, регистрация через GitHub) в районе
северо-западного Причерноморья и Дуная: Рени, Измаил, Вилково, Черноморск,
Одесса, Южный.

Что делает:
  * принимает поток AIS по веб-сокету и складывает суда, позиции и визиты в SQLite;
  * сам определяет порт отправления по геозонам портов (AIS его не передаёт);
  * считает целью танкер (AIS Type 80–89), который идёт в IZMAIL или RENI
    и вышел из ODESSA или CHORNOMORSK;
  * присылает алерт в Telegram и показывает его в окне;
  * рисует радар: порты, суда, курс, трек выбранного судна.

Запуск с окном:
    python ship_tracker.py

Без окна (фоном, без интерфейса):
    python ship_tracker.py --headless --minutes 60

Настройки: data/config.json, база: data/ship_tracker.db.
"""

import argparse
import math
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# Версия видна в заголовке окна и на вкладке «Подключение»: так сразу ясно,
# запущена ли текущая копия или где-то лежит старая сборка.
APP_VERSION = "2026.12.5"

import ais_feed
import cargo
import flags
import forecast
import geo
import metrics
import photos
import portcalls
import services_feed
from core import (AppError, CONFIG_PATH, DATA_DIR, DB_PATH, RadarCore,
                  duration_text, fmt_moment, fmt_number, fmt_tons, load_config, span_text,
                  norm, parse_chat_ids, port_matches, save_config, send_telegram, split_list,
                  telegram_call)
from store import Store, parse_iso

AIRSTREAM_SITE = "https://aisstream.io"

# Кэш фото судов: скачивается один раз и остаётся на диске.
PHOTO_DIR = DATA_DIR / "photos"

# Поля с ключами API тех сервисов, которым они нужны.
SERVICE_KEY_FIELDS = {"vf": "var_vf_key", "vfa": "var_vf_key",
                      "mt": "var_mt_key", "mst": "var_mst_key",
                      "gfw": "var_gfw_token", "dat": "var_dat_key",
                      "aishub": "var_aishub_key"}

# Дополнительные подключения: название, ключ настройки, описание и сайт с ключом.
SERVICE_BLOCKS = [
    ("VesselFinder", "vf", "API по ключу: LiveData — только по подписке"),
    ("VesselFinder: ожидаемые прибытия", "vfa",
     "суда с нашим портом в Destination — по кредитам, 5 за запись"),
    ("MarineTraffic", "mt", "живой API по ключу, район задаётся в кабинете"),
    ("MyShipTracking", "mst", "триал 2000 кредитов: расширенные данные по нашим судам"),
    ("Digitraffic", "dt", "открытые данные Финляндии, только Балтика"),
    ("Goradar", "gor", "бесплатно, без ключа: карта судов района"),
    ("MarineTraffic.live", "mlive", "бесплатно, без ключа: карта судов района"),
    ("Ship Radar 24: карточки", "srd",
     "бесплатно, без ключа: тип, осадка, размеры и назначение судов"),
    ("AIS-catcher: ожидаемые прибытия", "aci",
     "бесплатно, без ключа: суда, идущие в наши порты"),
    ("Global Fishing Watch", "gfw",
     "бесплатный токен: профиль судна и разрывы AIS"),
    ("Datalastic", "dat",
     "API по ключу: позиция и рейс по MMSI, 1 кредит за судно"),
    ("AISHub", "aishub",
     "логин участника: позиции района, доступ за свой поток NMEA"),
]

SERVICE_SITES = {
    "vf": ("vesselfinder.com", "https://www.vesselfinder.com/"),
    "vfa": ("API VesselFinder", "https://api.vesselfinder.com/expectedarrivals.html"),
    "mt": ("marinetraffic.com", "https://www.marinetraffic.com/"),
    "mst": ("myshiptracking.com", "https://www.myshiptracking.com/"),
    "dt": ("digitraffic.fi", "https://www.digitraffic.fi/en/marine-traffic/"),
    "gor": ("goradar.ru", "https://goradar.ru/"),
    "mlive": ("marinetraffic.live", "https://marinetraffic.live/"),
    "srd": ("карточки судов marinetraffic.live", "https://marinetraffic.live/"),
    "aci": ("AIS-catcher", "https://www.aiscatcher.org/port/UAIZM?tab=expected"),
    "gfw": ("Global Fishing Watch API", "https://globalfishingwatch.org/our-apis/"),
    "dat": ("Datalastic API", "https://datalastic.com/ais-data-api/"),
    "aishub": ("AISHub", "https://www.aishub.net/join-us"),
}


def service_key_var(app, key):
    """Переменная Tk с ключом API сервиса или None, если ключ не нужен."""
    name = SERVICE_KEY_FIELDS.get(key)
    return getattr(app, name, None) if name else None


def service_key_value(app, key) -> str:
    """Значение ключа сервиса с учётом общих ключей (у vfa ключ от vf)."""
    var = service_key_var(app, key)
    value = (var.get() or "").strip() if var is not None else ""
    if not value and key == "vfa":
        vf = getattr(app, "var_vf_key", None)
        value = (vf.get() or "").strip() if vf is not None else ""
    return value


# --------------------------------------------------------------------- утилиты

def console_log(message: str):
    line = f"[{datetime.now():%H:%M:%S}] {message}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(line.encode(encoding, "replace").decode(encoding, "replace"), flush=True)


def open_folder(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def clean_key(text) -> str:
    """Ключ из буфера обмена: без пробелов, переводов строк и невидимых символов.

    Копирование со страницы часто тянет за собой пробел, перевод строки или
    невидимый символ — из-за них ключ выглядит правильным, но не принимается.
    """
    if not text:
        return ""
    parts = [part for part in str(text).split() if part.strip()]
    if not parts:
        return ""
    candidate = max(parts, key=len)
    cleaned = "".join(ch for ch in candidate if ch.isprintable() and not ch.isspace())
    return cleaned[:200]


def parse_bboxes(text: str) -> list:
    """«44.05, 27.55, 45.95, 29.35; ...» → [[[s, w], [n, e]], ...] для AISStream."""
    boxes = []
    for chunk in (text or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [part for part in chunk.replace(",", " ").split() if part]
        if len(parts) != 4:
            raise AppError("Район задаётся четырьмя числами: юг, запад, север, восток.")
        try:
            south, west, north, east = (float(part) for part in parts)
        except ValueError:
            raise AppError(f"В районе «{chunk}» не числа.")
        if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
            raise AppError(f"Район «{chunk}»: нужно юг < север и запад < восток.")
        boxes.append([[south, west], [north, east]])
    if not boxes:
        raise AppError("Не задан район подписки.")
    return boxes


def format_bboxes(boxes) -> str:
    chunks = []
    for box in boxes or []:
        try:
            (south, west), (north, east) = box
        except (ValueError, TypeError):
            continue
        chunks.append(f"{south:g}, {west:g}, {north:g}, {east:g}")
    return "; ".join(chunks)


def parse_ports(text: str) -> list:
    """Строки «ГАЛАЦ; 45.4353; 28.0081; 7» → список геозон."""
    ports = []
    for number, line in enumerate((text or "").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.replace(",", ";").split(";") if part.strip()]
        if len(parts) < 4:
            raise AppError(f"Строка {number}: нужно «ИМЯ; широта; долгота; радиус_км».")
        try:
            lat, lon, radius = float(parts[1]), float(parts[2]), float(parts[3])
        except ValueError:
            raise AppError(f"Строка {number}: широта, долгота и радиус должны быть числами.")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or radius <= 0:
            raise AppError(f"Строка {number}: проверьте координаты и радиус.")
        ports.append({
            "name": norm(parts[0]),
            "label": parts[0],
            "lat": lat,
            "lon": lon,
            "radius_km": radius,
        })
    if not ports:
        raise AppError("Нужен хотя бы один порт с геозоной.")
    return ports


def format_ports(ports) -> str:
    lines = []
    for port in ports or []:
        lines.append(f"{port.get('name')}; {port.get('lat')}; {port.get('lon')}; "
                     f"{port.get('radius_km')}")
    return "\n".join(lines)


# ---------------------------------------------------------------- холст радара

class RadarCanvas:
    def __init__(self, tk, parent, config, on_select, request_redraw, store=None):
        self.tk = tk
        self.config = config
        self.on_select = on_select
        self.request_redraw = request_redraw
        self.store = store
        self.canvas = tk.Canvas(parent, bg="#061018", highlightthickness=0,
                                width=800, height=560)
        self.center_lat, self.center_lon, self.span = self._initial_view()
        self.selected = None
        self.show_tracks = True
        self.show_forecast = True
        # Устаревшие позиции по умолчанию не рисуются: точки, которым больше
        # stale_minutes, кучкой висят у причалов и на рейдах и мешают смотреть
        # свежую обстановку. Включить их можно галочкой «устаревшие».
        self.show_stale = False
        self.targets_only = False
        self.schematic_view = False
        self.track = []
        self._forecast_cache = {}
        self._hits = []
        self._press = None
        self._press_geo = None

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", lambda event: self.zoom(1.2))
        self.canvas.bind("<Button-5>", lambda event: self.zoom(1 / 1.2))
        self.canvas.bind("<Configure>", lambda event: self.request_redraw())
        self.canvas.bind("<Double-Button-1>", lambda event: self.fit_all())

    # -- вид
    def _initial_view(self):
        boxes = self.config.get("bounding_boxes") or geo.DEFAULT_BOUNDING_BOXES
        try:
            (south, west), (north, east) = boxes[0]
        except (ValueError, TypeError, IndexError):
            south, west, north, east = 44.05, 27.55, 45.95, 29.35
        span = max(0.2, (east - west) * 1.12)
        return (south + north) / 2, (west + east) / 2, span

    def fit_all(self):
        self.center_lat, self.center_lon, self.span = self._initial_view()
        self.request_redraw()

    def zoom(self, factor):
        self.span = max(0.04, min(25.0, self.span / factor))
        self.request_redraw()

    def projector(self):
        width = self.canvas.winfo_width() or 900
        height = self.canvas.winfo_height() or 560
        return geo.Projector(width, height, self.center_lat, self.center_lon, self.span)

    def select(self, mmsi):
        self.selected = mmsi
        self.request_redraw()

    # -- мышь
    def _on_press(self, event):
        self._press = (event.x, event.y)
        self._press_geo = self.projector().to_geo(event.x, event.y)

    def _on_motion(self, event):
        if not self._press or not self._press_geo:
            return
        if abs(event.x - self._press[0]) < 3 and abs(event.y - self._press[1]) < 3:
            return
        width = self.canvas.winfo_width() or 900
        height = self.canvas.winfo_height() or 560
        projector = self.projector()
        lat_g, lon_g = self._press_geo
        self.center_lon = lon_g - (event.x - width / 2) / projector.px_per_km / (
            111.32 * projector.cos_lat)
        self.center_lat = lat_g + (event.y - height / 2) / projector.px_per_km / 111.32
        self.center_lat = max(-85.0, min(85.0, self.center_lat))
        self.center_lon = max(-180.0, min(180.0, self.center_lon))
        self.request_redraw()

    def _on_release(self, event):
        moved = self._press and (abs(event.x - self._press[0]) > 3
                                 or abs(event.y - self._press[1]) > 3)
        if not moved:
            mmsi = self.pick(event.x, event.y)
            self.selected = mmsi
            self.on_select(mmsi)
            self.request_redraw()
        self._press = None
        self._press_geo = None

    def _on_wheel(self, event):
        self.zoom(1.2 if event.delta > 0 else 1 / 1.2)

    def pick(self, x, y):
        best = None
        for hit_x, hit_y, mmsi in self._hits:
            distance = ((hit_x - x) ** 2 + (hit_y - y) ** 2) ** 0.5
            if distance <= 14 and (best is None or distance < best[0]):
                best = (distance, mmsi)
        return best[1] if best else None

    # -- отрисовка
    def draw(self, vessels, target_mmsi, traffic=None):
        canvas = self.canvas
        canvas.delete("all")
        # Get canvas size; if 1x1 (before window mapping), use default
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width <= 1:
            width = 800
        if height <= 1:
            height = 560
        if width < 20 or height < 20:
            return
        projector = geo.Projector(width, height, self.center_lat, self.center_lon, self.span)
        self._draw_grid(projector, width, height)
        self._draw_ports(projector)
        self._draw_forecasts(projector, vessels)
        self._draw_track(projector)
        self._hits = []
        now = datetime.now()
        drawn = 0
        faded = 0
        stale_count = 0
        stale_drawn = 0
        stale_hidden = 0
        for vessel in vessels:
            lat, lon = vessel.get("lat"), vessel.get("lon")
            # На карту попадают только суда с реальной позицией. Суда без
            # координат (список AIS-catcher) видны в таблицах и карточке,
            # но точкой у порта их не рисуем — иначе вокруг порта клубок.
            if lat is None or lon is None:
                continue
            is_target = vessel["mmsi"] in target_mmsi
            if self.targets_only and not is_target:
                continue
            updated = parse_iso(vessel.get("updated_at"))
            age = (now - updated).total_seconds() if updated else None
            fresh = age is not None and age <= self.stale_seconds()
            # Без свежего сигнала, но ещё в пределах `sea_fresh_hours` — судно
            # остаётся на карте приглушённым: AIS-каналы отдают район не каждую
            # минуту, и скрывать такие позиции означало бы терять половину судов.
            stale = age is None or age > self.max_age_seconds()
            if stale:
                stale_count += 1
                if not self.show_stale and vessel["mmsi"] != self.selected:
                    stale_hidden += 1
                    continue
                stale_drawn += 1
            elif not fresh:
                faded += 1
            x, y = projector.to_screen(lat, lon)
            if not (-20 <= x <= width + 20 and -20 <= y <= height + 20):
                continue
            drawn += 1
            self._hits.append((x, y, vessel["mmsi"]))
            # Цвета в зависимости от типа и схемatical режима
            if is_target:
                color, radius = "#ff3b30", 5
            elif ais_feed.is_tanker_code(vessel.get("type_code")):
                color, radius = "#ffd166", 4
            else:
                color, radius = "#7fa8bf", 3
            if self.schematic_view:
                # Схемatical view: более насыщенные, упрощенные цвета
                if color == "#ff3b30": color = "#c62828"
                elif color == "#ffd166": color = "#fb8c00"
                elif color == "#7fa8bf": color = "#42a5f5"
            if stale:
                color = "#4b6472"
            elif not fresh:
                color = "#5d7786"
            if vessel["mmsi"] == self.selected:
                canvas.create_oval(x - 11, y - 11, x + 11, y + 11, outline="#ffffff", width=2)
            course = vessel.get("cog")
            if course is None:
                course = vessel.get("heading")
            if course is not None:
                angle = math.radians(float(course))
                dx, dy = math.sin(angle), -math.cos(angle)
                canvas.create_line(x, y, x + dx * 14, y + dy * 14, fill=color,
                                   width=1, tags=("vessel",))
            canvas.create_oval(x - radius, y - radius, x + radius, y + radius,
                               fill=color, outline="", tags=("vessel",))
            if drawn % 7 == 0 and vessel.get("name"):
                if self.schematic_view:
                    canvas.create_text(x + 7, y - 7, text="", anchor="w",
                                       fill="#5d7280", font=("Consolas", 5))
                else:
                    canvas.create_text(x + 7, y - 7, text=vessel["name"], anchor="w",
                                       fill="#d7e6ee" if not stale else "#5d7280",
                                       font=("Consolas", 7))
        # Весь остальной трафик района: мелкие точки, чтобы на карте были все суда.
        shown_traffic = 0
        traffic_hidden = 0
        if traffic:
            known = {str(vessel.get("mmsi")) for vessel in vessels if vessel.get("mmsi")}
            for vessel in traffic:
                mmsi = str(vessel.get("mmsi") or "")
                if not mmsi or mmsi in known:
                    continue
                lat, lon = vessel.get("lat"), vessel.get("lon")
                if lat is None or lon is None:
                    continue
                updated = parse_iso(vessel.get("updated_at"))
                if (not self.show_stale and mmsi != self.selected
                        and (not updated
                             or (now - updated).total_seconds() > self.stale_seconds())):
                    traffic_hidden += 1
                    continue
                x, y = projector.to_screen(lat, lon)
                if not (0 <= x <= width and 0 <= y <= height):
                    continue
                color = "#3f6f88" if (vessel.get("sog") or 0) > 0.5 else "#4a4f52"
                canvas.create_oval(x - 1.5, y - 1.5, x + 1.5, y + 1.5,
                                   fill=color, outline="", tags=("traffic",))
                shown_traffic += 1
        self._draw_legend(width, height, len(vessels), len(target_mmsi), drawn, stale_drawn,
                          len([v for v in vessels if v.get("lat") is None]),
                          traffic=shown_traffic, stale_hidden=stale_hidden,
                          traffic_hidden=traffic_hidden, faded=faded)

    def _draw_grid(self, projector, width, height):
        canvas = self.canvas
        # Схемatical view: simpler grid with fewer labels
        if self.schematic_view:
            step = 5.0
            color = "#304050"
            label_color = "#607080"
            font_size = 6
        else:
            step = 0.5
            color = "#0f2430"
            label_color = "#2f5b6e"
            font_size = 7
        for candidate in (5.0, 1.0, 0.5, 0.25, 0.1):
            if self.span / candidate <= 12:
                step = candidate
                break
        lat_top, lon_left = projector.to_geo(0, 0)
        lat_bottom, lon_right = projector.to_geo(width, height)
        start_lat = int(lat_bottom / step) * step
        lat = start_lat
        while lat <= lat_top + step:
            y = projector.to_screen(lat, projector.center_lon)[1]
            if 0 <= y <= height:
                canvas.create_line(0, y, width, y, fill=color)
                if not self.schematic_view or lat % 5 == 0:
                    canvas.create_text(4, y + 6, text=f"{lat:.1f}", anchor="w",
                                       fill=label_color, font=("Consolas", font_size))
            lat += step
        start_lon = int(lon_left / step) * step
        lon = start_lon
        while lon <= lon_right + step:
            x = projector.to_screen(projector.center_lat, lon)[0]
            if 0 <= x <= width:
                canvas.create_line(x, 0, x, height, fill=color)
                if not self.schematic_view or lon % 5 == 0:
                    canvas.create_text(x + 3, height - 10, text=f"{lon:.1f}", anchor="w",
                                       fill=label_color, font=("Consolas", font_size))
            lon += step

    def _draw_ports(self, projector):
        canvas = self.canvas
        for port in self.config.get("ports") or []:
            try:
                x, y = projector.to_screen(port["lat"], port["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            radius = max(4.0, projector.km_to_px(float(port.get("radius_km") or 5)))
            if self.schematic_view:
                # Схемatical view: simpler port markers (no inner circle / label simplified)
                canvas.create_oval(x - radius, y - radius, x + radius, y + radius,
                                   outline="#1c6b42", width=2)
                canvas.create_text(x, y + radius + 6, text=port.get("name") or "",
                                   fill="#607080", font=("Consolas", 6, "bold"))
            else:
                canvas.create_oval(x - radius, y - radius, x + radius, y + radius,
                                   outline="#1f7a52", dash=(3, 3))
                canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill="#35d08a", outline="")
                canvas.create_text(x, y - radius - 8, text=port.get("name") or "",
                                   fill="#7fe0b3", font=("Consolas", 8, "bold"))

    def _draw_track(self, projector):
        if not self.show_tracks or len(self.track) < 2:
            return
        points = []
        for point in self.track:
            points.extend(projector.to_screen(point["lat"], point["lon"]))
        if len(points) >= 4:
            self.canvas.create_line(*points, fill="#c8a24a", width=1, smooth=False)

    def _forecast_for(self, vessel):
        """Прогноз маршрута с кэшем: пересчёт только при новых данных AIS."""
        ports = self.config.get("ports") or []
        key = (vessel.get("updated_at"), vessel.get("sog"), vessel.get("cog"),
               vessel.get("heading"), vessel.get("destination"),
               forecast.MAX_DAYS, tuple(sorted((p.get("name") or "") for p in ports)))
        mmsi = vessel.get("mmsi")
        cached = self._forecast_cache.get(mmsi)
        if cached and cached[0] == key:
            return cached[1]
        try:
            points = forecast.route(vessel, ports)
        except Exception:
            points = []
        if len(self._forecast_cache) > 5000:
            self._forecast_cache.clear()
        self._forecast_cache[mmsi] = (key, points)
        return points

    def _forecast_ready(self, vessel, allow_stale=False):
        """Стоит ли строить прогноз: судно актуально, в море и куда-то идёт.

        allow_stale — для выбранного судна: раз пользователь кликнул именно по
        нему, прогноз по последним известным данным полезнее, чем ничего.
        """
        if vessel.get("lat") is None or vessel.get("lon") is None:
            return False
        age = metrics.freshness_hours(vessel.get("updated_at"))
        if not allow_stale and age is not None and age * 3600 > self.stale_seconds():
            return False
        ports = self.config.get("ports") or []
        if ports and geo.port_at(vessel.get("lat"), vessel.get("lon"), ports):
            return False
        try:
            sog = float(vessel.get("sog") or 0)
        except (TypeError, ValueError):
            return False
        if not (forecast.MIN_SPEED_KNOTS <= sog <= 40):
            return False
        if forecast.target_port(vessel, ports) is not None:
            return True
        # нашего порта в назначении нет — строим линию по курсу, но без курса
        # она бессмысленна (стоящее судно и так видно на карте)
        _speed, course, _source = forecast.estimates(vessel)
        return course is not None

    def _draw_forecasts(self, projector, vessels):
        """Прогноз пути до нашего порта — только для выбранного судна.

        Линии у всех судов сразу превращали карту в паутину, поэтому прогноз
        рисуется по клику: выбрали судно — видите его маршрут и время прибытия.
        """
        if not self.show_forecast or not self.selected:
            return
        vessel = next((item for item in vessels
                       if str(item.get("mmsi")) == str(self.selected)), None)
        if not vessel or not self._forecast_ready(vessel, allow_stale=True):
            return
        points = self._forecast_for(vessel)
        if len(points) < 2:
            return
        canvas = self.canvas
        coords = []
        for point in points:
            x, y = projector.to_screen(point["lat"], point["lon"])
            coords.extend((x, y))
        if len(coords) < 4:
            return
        # в наш порт — голубая, по курсу — жёлтая: видно, где прогноз твёрдый,
        # а где это просто продолжение текущего курса
        ports = self.config.get("ports") or []
        to_port = forecast.target_port(vessel, ports) is not None
        color = "#39d3ff" if to_port else "#ffc247"
        text_color = "#8fe6ff" if to_port else "#ffdf9a"
        canvas.create_line(*coords, fill=color, dash=(4, 3), width=2)
        for point in forecast.daily_points(points):
            x, y = projector.to_screen(point["lat"], point["lon"])
            canvas.create_oval(x - 3, y - 3, x + 3, y + 3, outline=color, width=1)
            canvas.create_text(x + 6, y - 6, text=point["label"], anchor="w",
                               fill=text_color, font=("Consolas", 7))
        last = points[-1]
        x, y = projector.to_screen(last["lat"], last["lon"])
        if last.get("arrived"):
            canvas.create_text(x + 8, y + 10,
                               text=f"{last.get('port') or ''} {last['at']:%d.%m %H:%M}",
                               anchor="w", fill=text_color, font=("Consolas", 7))
        else:
            canvas.create_text(x + 8, y + 10,
                               text=f"{last['at']:%d.%m %H:%M} · курс "
                                    f"{forecast.course_text(last.get('course'))}",
                               anchor="w", fill=text_color, font=("Consolas", 7))

    def stale_seconds(self):
        """Через сколько секунд без сообщений судно считается устаревшим."""
        try:
            minutes = float(self.config.get("stale_minutes") or 0)
        except (TypeError, ValueError):
            minutes = 15.0
        if minutes <= 0:
            return float("inf")
        return minutes * 60

    def _draw_legend(self, width, height, vessels, targets, drawn, stale, without_position=0,
                     traffic=0, stale_hidden=0, traffic_hidden=0, faded=0):
        fresh = drawn - stale - faded
        text = (f"источник: живой AIS (AISStream, Open Waters)\n"
                f"судов в базе: {vessels}   целей: {targets}   на экране: {drawn}\n"
                f"свежих: {fresh}   без свежего сигнала (приглушённые): {faded}\n"
                f"старше {self._age_label()}: {stale + stale_hidden}\n")
        if stale_hidden:
            text += f"из них скрыто на карте: {stale_hidden} (галочка «устаревшие»)\n"
        if without_position:
            text += (f"без позиции (только в списках): {without_position} — AIS-catcher\n")
        if traffic:
            text += f"трафик района (точки): {traffic}"
            if traffic_hidden:
                text += f", скрыто старых: {traffic_hidden}"
            text += "\n"
        if self.show_forecast:
            text += (f"пунктир — прогноз выбранного судна на {int(forecast.MAX_DAYS)} сут: "
                     f"голубой — в наш порт, жёлтый — по текущему курсу\n")
            text += (f"план на {int(forecast.PLAN_DAYS)} дня (путь, стоянка, выход в море) — "
                     f"во вкладке «Прогноз 4 дня»\n")
        text += (f"масштаб: {self.span:.2f}°   район: {self.center_lat:.2f}, {self.center_lon:.2f}")
        self.canvas.create_text(10, 10, anchor="nw", text=text,
                                fill="#5f8ea3", font=("Consolas", 8))

    def _stale_label(self):
        seconds = self.stale_seconds()
        if seconds == float("inf"):
            return "—"
        return f"{int(round(seconds / 60))} мин"

    def max_age_seconds(self):
        """Сколько позиция ещё считается пригодной для карты.

        Свежие (до `stale_minutes`) рисуются обычными цветами, более старые —
        приглушённо, пока не выйдут за `sea_fresh_hours`; дальше это уже
        «устаревшие», и без галочки их не видно.
        """
        try:
            hours = float(self.config.get("sea_fresh_hours") or 12)
        except (TypeError, ValueError):
            hours = 12.0
        return max(self.stale_seconds(), hours * 3600.0)

    def _age_label(self):
        seconds = self.max_age_seconds()
        if seconds == float("inf"):
            return "—"
        hours = seconds / 3600.0
        return f"{hours:.0f} ч" if hours >= 1 else f"{int(round(seconds / 60))} мин"


# ------------------------------------------------------------------ приложение

class App:
    def __init__(self, tk, ttk, messagebox, root, config, store, config_path):
        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.root = root
        self.config = config
        self.store = store
        self.config_path = Path(config_path)
        self.events = queue.Queue()
        self.selected = None
        self.core = RadarCore(config, store, log=self.log, on_alert=self.on_alert)
        self.core.on_service_status = self._on_service_status
        # когда источники ответили на стартовое обновление — обновить экран
        self.core.on_refresh = lambda: self.events.put(("refresh", None))
        # бот Telegram — отдельный процесс telegram_bot.py, поднимается при старте
        self.bot_process = None
        self._targets = {}
        self._banner_job = None
        self._key_checking = False
        self._photo_image = None
        self._photo_for = None

        self.root.title(f"Ship Tracker — версия {APP_VERSION}")
        self.root.geometry("1240x820")
        self.root.minsize(1000, 680)

        self.build()
        self.enable_clipboard()
        self.core.start()
        self.start_bot()
        self.root.after(250, self.drain)
        self.root.after(600, self.fast_tick)
        self.root.after(2500, self.slow_tick)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if not (config.get("api_key") or "").strip():
            self.log("Ключ AISStream не задан: откройте вкладку «Подключение» и вставьте ключ "
                     "(бесплатно на aisstream.io).")

    # -- журнал и события из фоновых потоков
    def log(self, message):
        self.events.put(("log", message))

    def on_alert(self, alert):
        self.events.put(("alert", alert))

    def drain(self):
        self.drain_once()
        self.root.after(250, self.drain)

    def drain_once(self):
        """Забирает сообщения из очереди (в главном потоке)."""
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.append_log(payload)
                elif kind == "alert":
                    self.show_banner(payload)
                elif kind == "photo":
                    self.show_photo(*payload)
                elif kind == "refresh":
                    self._slow_tick()
        except queue.Empty:
            pass

    def append_log(self, message):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{datetime.now():%H:%M:%S}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def show_banner(self, alert):
        self.banner.configure(
            text=f"🚨 {alert.get('name')}: {alert.get('origin')} → {alert.get('destination')} · "
                 f"{fmt_number(alert.get('sog'))} уз")
        self.banner.pack(fill="x", padx=10, pady=(0, 4), before=self.notebook)
        try:
            self.root.bell()
        except Exception:
            pass
        if self._banner_job:
            self.root.after_cancel(self._banner_job)
        self._banner_job = self.root.after(25000, self.hide_banner)

    def hide_banner(self):
        self._banner_job = None
        self.banner.pack_forget()

    # -- построение окна
    def build(self):
        tk, ttk = self.tk, self.ttk
        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=10, pady=(8, 2))
        ttk.Label(header, text="Ship Tracker", font=("Segoe UI", 16, "bold")).pack(side="left")
        self.conn_var = tk.StringVar(value="поток: —")
        ttk.Label(header, textvariable=self.conn_var, foreground="#555").pack(side="right")

        self.banner = ttk.Label(self.root, text="", background="#b3261e", foreground="#ffffff",
                                font=("Segoe UI", 11, "bold"), anchor="center", padding=6)
        self.banner.pack(fill="x", padx=10, pady=(0, 4))
        self.banner.pack_forget()

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10, pady=4)
        self.notebook = notebook

        self.tab_radar = ttk.Frame(notebook)
        self.tab_vessels = ttk.Frame(notebook)
        self.tab_sea = ttk.Frame(notebook)
        self.tab_forecast = ttk.Frame(notebook)
        self.tab_alerts = ttk.Frame(notebook)
        self.tab_visits = ttk.Frame(notebook)
        self.tab_visit_history = ttk.Frame(notebook)
        self.tab_expected = ttk.Frame(notebook)
        self.tab_connection = ttk.Frame(notebook)
        self.tab_filters = ttk.Frame(notebook)
        self.tab_telegram = ttk.Frame(notebook)
        self.tab_services = ttk.Frame(notebook)
        self.tab_log = ttk.Frame(notebook)
        for tab, title in ((self.tab_radar, "Радар"),
                           (self.tab_vessels, "Все суда"), (self.tab_sea, "В море"),
                           (self.tab_forecast, "Прогноз 4 дня"),
                           (self.tab_visits, "Стоянки"),
                           (self.tab_visit_history, "История стоянок"),
                           (self.tab_expected, "Ожидаемые"),
                           (self.tab_alerts, "Алерты"),
                           (self.tab_connection, "Подключение"), (self.tab_filters, "Фильтры"),
                           (self.tab_telegram, "Telegram"), (self.tab_services, "Сервисы"),
                           (self.tab_log, "Журнал")):
            notebook.add(tab, text=title)

        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_changed)

        self.build_radar()
        self.build_vessels()
        self.build_sea()
        self.build_forecast()
        self.build_visits()
        self.build_visit_history()
        self.build_expected()
        self.build_alerts()
        self.build_connection()
        self.build_filters()
        self.build_telegram()
        self.build_services()
        self.build_log()

        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=10, pady=(2, 8))
        ttk.Button(bar, text="▶ Запустить поток", command=self.start_core).pack(side="left")
        ttk.Button(bar, text="■ Остановить", command=self.stop_core).pack(side="left", padx=(6, 0))
        self.stats_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.stats_var, foreground="#555").pack(side="left", padx=16)
        ttk.Button(bar, text="Открыть папку данных",
                   command=lambda: open_folder(DATA_DIR)).pack(side="right")

    def build_radar(self):
        tk, ttk = self.tk, self.ttk
        frame = self.tab_radar

        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", pady=(6, 2))
        self.canvas_holder = ttk.PanedWindow(frame, orient="horizontal")
        self.canvas_holder.pack(fill="both", expand=True, padx=2, pady=(0, 4))
        canvas_frame = ttk.Frame(self.canvas_holder)
        panel = ttk.Frame(self.canvas_holder, width=310)
        self.canvas_holder.add(canvas_frame, weight=4)
        self.canvas_holder.add(panel, weight=1)

        self.radar = RadarCanvas(tk, canvas_frame, self.config, self.select_vessel,
                                 self.redraw_map, self.store)
        self.radar.canvas.pack(fill="both", expand=True)

        # Force geometry update so canvas gets proper size
        self.canvas_holder.update_idletasks()
        canvas_frame.update_idletasks()
        self.radar.canvas.update_idletasks()

        ttk.Button(toolbar, text="−", width=3,
                   command=lambda: self.radar.zoom(1 / 1.3)).pack(side="left")
        ttk.Button(toolbar, text="+", width=3,
                   command=lambda: self.radar.zoom(1.3)).pack(side="left", padx=(2, 0))
        ttk.Button(toolbar, text="Весь район", command=self.radar.fit_all).pack(side="left", padx=(6, 0))
        self.var_targets_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(toolbar, text="только цели", variable=self.var_targets_only,
                        command=self.toggle_targets_only).pack(side="left", padx=(12, 0))
        self.var_traffic = tk.BooleanVar(value=True)
        ttk.Checkbutton(toolbar, text="трафик района", variable=self.var_traffic,
                        command=self.redraw_map).pack(side="left", padx=(8, 0))
        # Фильтр «в порту»: только суда у причала и на рейде (ждут лоцмана)
        self.var_port_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(toolbar, text="в порту / ждёт лоцмана", variable=self.var_port_only,
                        command=self.redraw_map).pack(side="left", padx=(8, 0))
        self.var_tracks = tk.BooleanVar(value=True)
        ttk.Checkbutton(toolbar, text="трек", variable=self.var_tracks,
                        command=self.toggle_tracks).pack(side="left", padx=(8, 0))
        self.var_forecast = tk.BooleanVar(value=True)
        ttk.Checkbutton(toolbar, text="прогноз 3 дня", variable=self.var_forecast,
                        command=self.toggle_forecast).pack(side="left", padx=(8, 0))
        self.var_stale = tk.BooleanVar(value=False)
        ttk.Checkbutton(toolbar, text="устаревшие", variable=self.var_stale,
                        command=self.toggle_stale).pack(side="left", padx=(8, 0))
        self.var_schematic = tk.BooleanVar(value=False)
        ttk.Checkbutton(toolbar, text="схематический вид", variable=self.var_schematic,
                        command=self.toggle_schematic).pack(side="left", padx=(8, 0))
        ttk.Label(toolbar, text="колесо — масштаб, перетаскивание — сдвиг, клик — карточка судна",
                  foreground="#777").pack(side="left", padx=12)
        ttk.Button(toolbar, text="Обновить",
                   command=lambda: self.refresh_tab(self.tab_radar)).pack(side="right")

        self.photo_label = tk.Label(panel, text="фото судна", background="#eef2f4",
                                    foreground="#777", height=9, width=34,
                                    relief="flat", anchor="center")
        self.photo_label.pack(fill="x", padx=(8, 0), pady=(0, 4))
        self.photo_note = ttk.Label(panel, text="", foreground="#777", wraplength=300)
        self.photo_note.pack(fill="x", padx=(8, 0))

        detail_box = ttk.Frame(panel)
        detail_box.pack(fill="both", expand=True, padx=(8, 0), pady=0)
        self.detail_text = tk.Text(detail_box, width=36, height=20, wrap="word",
                                   state="disabled", font=("Consolas", 9),
                                   background="#f7f9fa")
        self.detail_scroll = ttk.Scrollbar(detail_box, orient="vertical",
                                           command=self.detail_text.yview)
        self.detail_text.configure(yscrollcommand=self.detail_scroll.set)
        self.detail_text.pack(side="left", fill="both", expand=True)
        self.detail_scroll.pack(side="right", fill="y")
        # Содержимое карточки обновляется каждые 3 с; прокрутка при этом
        # сохраняется, иначе окно прыгало бы наверх каждый раз.
        self._detail_cache = None
        self.detail_text.bind("<MouseWheel>", self.scroll_detail)

    def build_vessels(self):
        tk, ttk = self.tk, self.ttk
        frame = self.tab_vessels
        ttk.Label(frame, text="Все суда в районе наших портов: в радиусе «Район вокруг "
                              "портов, км» (по умолчанию 800–1000 км от любого порта) и все, "
                              "у кого в Destination наш порт — на любом удалении. Каждое "
                              "такое судно записывается в базу. Те, у кого пока нет "
                              "позиции в живых потоках, добавлены снизу из ожидаемых "
                              "AIS-catcher.",
                  foreground="#555", wraplength=1200, justify="left").pack(
            anchor="w", padx=8, pady=(8, 4))
        row = ttk.Frame(frame)
        row.pack(fill="x", padx=8, pady=(0, 4))
        self.vessels_info = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.vessels_info, foreground="#333",
                  wraplength=1000, justify="left").pack(side="left")
        ttk.Button(row, text="Обновить", command=self.refresh_vessels_table).pack(side="right")
        self.vessels_tree = self.make_tree(
            frame, (                    ("name", "Судно", 170), ("flag", "Флаг", 140), ("type", "Тип", 100),
                    ("size", "Размеры", 115),
                    ("dest", "Назначение", 140), ("left", "До прибытия", 150),
                    ("stay", "Стоянка в порту", 170), ("cargo", "Груз (оценка)", 130),
                    ("cargo_kind", "Что везёт", 235),
                    ("sog", "Узлы", 55),
                    ("math", "Расчёт", 210),
                    ("port", "В порту", 100),
                    ("updated", "Обновлено", 115), ("mmsi", "MMSI", 90)))
        # Строки без свежих данных AIS — серым: видно, что цифры не сегодняшние.
        self.vessels_tree.tag_configure("stale", foreground="#8c8c8c")

    def build_sea(self):
        """Вкладка «В море»: суда с позицией вне портов и без статики AIS."""
        tk, ttk = self.tk, self.ttk
        frame = self.tab_sea
        ttk.Label(frame, text="Судна в море, связанные с нашими портами: в Destination стоит "
                              "наш порт (идут в него), судно вышло из нашего порта за "
                              "последние дни или находится рядом с ним. Судно с нашим портом "
                              "в назначении остаётся в списке на любой дистанции — в том "
                              "числе из Средиземного моря за 1000 км. Расстояние и прибытие — "
                              "по прямой и текущей скорости; заявленный капитаном ETA — "
                              "в карточке.",
                  foreground="#555", wraplength=1100, justify="left").pack(
            anchor="w", padx=8, pady=(8, 4))
        port_bar = ttk.Frame(frame)
        port_bar.pack(fill="x", padx=8, pady=(0, 4))
        self.sea_port = tk.StringVar(value="")
        self.sea_port_buttons = {}
        ports = [((port.get("name") or "").upper(), port.get("label") or port.get("name"))
                 for port in self.config.get("ports") or []]
        for name, label in [("", "Все порты")] + ports:
            button = tk.Button(port_bar, text=label, command=lambda key=name: self.pick_sea_port(key),
                               relief="groove", padx=8, pady=2, font=("Segoe UI", 9))
            button.pack(side="left", padx=(0, 4))
            self.sea_port_buttons[name] = button
        ttk.Button(port_bar, text="Обновить", command=self.refresh_sea_table).pack(side="right")
        self.sea_info = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.sea_info, foreground="#333", wraplength=1200,
                  justify="left").pack(anchor="w", padx=8, pady=(0, 4))
        self.sea_tree = self.make_tree(
            frame, (("name", "Судно", 180), ("flag", "Флаг", 150),
                    ("type", "Тип", 110), ("dest", "Назначение", 150),
                    ("sog", "Узлы", 60), ("course", "Курс", 70),
                    ("distance", "До порта", 150), ("eta", "Прибытие", 155),
                    ("updated", "Обновлено", 130), ("mmsi", "MMSI", 95)))
        self.pick_sea_port("")

    def pick_sea_port(self, name):
        """Выбор порта во вкладке «В море»: подсветка кнопки и фильтр списка."""
        self.sea_port.set(name or "")
        for key, button in getattr(self, "sea_port_buttons", {}).items():
            active = (key == (name or ""))
            button.configure(relief="sunken" if active else "groove",
                             background="#d7e6f5" if active else "#f0f0f0")
        self.refresh_sea_table()

    def build_forecast(self):
        """Вкладка «Прогноз 4 дня»: путь до порта, стоянка и выход в море."""
        tk, ttk = self.tk, self.ttk
        frame = self.tab_forecast
        ttk.Label(
            frame,
            text="План на 4 дня по каждому судну, которое идёт в наш порт или уже стоит в нём: "
                 "путь до порта, сколько простоит и маршрут в море на оставшийся срок "
                 "(стоянка сутки — море три дня, стоянка двое — два дня). Стоянка оценивается "
                 "по прежним визитам этого судна, иначе по среднему в порту; если наблюдений "
                 "нет — сутки. Скорость и курс берутся из AIS. «Ит. путь» — сколько всего "
                 "времени от текущего момента до выхода из порта, включая стоянку. "
                 "Двойной щелчок открывает карточку.",
            foreground="#555", wraplength=1150, justify="left").pack(
            anchor="w", padx=8, pady=(8, 4))
        bar = ttk.Frame(frame)
        bar.pack(fill="x", padx=8, pady=(0, 4))
        self.forecast_info = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.forecast_info, foreground="#333",
                  wraplength=980, justify="left").pack(side="left", fill="x", expand=True)
        ttk.Button(bar, text="Обновить", command=self.refresh_forecast).pack(side="right")
        self.forecast_tree = self.make_tree(
            frame, (("name", "Судно", 170), ("port", "Порт", 110),
                    ("arrival", "Приход", 145), ("stay", "Стоянка", 110),
                    ("departure", "Выход", 145), ("total", "Ит. путь", 175),
                    ("sea", "Море", 100),
                    ("route", "Маршрут", 320), ("mmsi", "MMSI", 95)))
        self.forecast_tree.bind("<<TreeviewSelect>>", self.show_forecast_detail)
        self.forecast_tree.tag_configure("in_port", foreground="#1f7a52")
        self.forecast_tree.tag_configure("late", foreground="#8c8c8c")
        self.forecast_detail = tk.Text(frame, height=7, wrap="word",
                                       font=("Consolas", 9), background="#f7fafc",
                                       foreground="#20323c")
        self.forecast_detail.pack(fill="x", padx=8, pady=(0, 8))
        self.forecast_detail.insert("1.0", "Выберите судно — покажу полный план.")
        self.forecast_detail.configure(state="disabled")
        self.refresh_forecast()

    def forecast_candidates(self):
        """Суда для плана: идут в наш порт или уже стоят в нём."""
        ports = self.config.get("ports") or []
        names = {(port.get("name") or "").upper() for port in ports}
        opens = {}
        try:
            for visit in self.store.open_visits():
                if (visit.get("port") or "").upper() in names:
                    opens[str(visit.get("mmsi"))] = visit
        except Exception as error:
            self.log(f"Стоянки для прогноза: {error}")
        result = []
        for vessel in self.core.related_vessels(3000):
            mmsi = str(vessel.get("mmsi"))
            port = self.core.destination_port(vessel)
            visit = opens.get(mmsi)
            if not port:
                if not visit:
                    continue
                port = geo.port_by_name(visit.get("port"), ports)
            if not port:
                continue
            result.append((vessel, port))
        return result

    def refresh_forecast(self):
        """Пересобирает план на 4 дня по всем судам у наших портов."""
        tree = getattr(self, "forecast_tree", None)
        if not tree:
            return
        ports = self.config.get("ports") or []
        selected = tree.selection()
        keep = tree.item(selected[0], "tags")[0] if selected else None
        for item in tree.get_children():
            tree.delete(item)
        plans = {}
        rows = []
        for vessel, port in self.forecast_candidates():
            mmsi = str(vessel.get("mmsi"))
            try:
                stay = self.core.port_stay(vessel, port=port)
            except Exception:
                stay = None
            try:
                track = self.store.track(mmsi, 24)
            except Exception:
                track = None
            result = forecast.plan(vessel, ports, stay=stay, track=track)
            if not result:
                continue
            plans[mmsi] = (vessel, result)
            rows.append((vessel, result))

        rows.sort(key=lambda pair: (pair[1].get("arrival_hours") is None,
                                    pair[1].get("arrival_hours") or 0.0,
                                    (pair[0].get("name") or "")))
        for vessel, result in rows:
            mmsi = str(vessel.get("mmsi"))
            tags = (mmsi, "in_port" if result.get("in_port") else "sea_plan")
            if not result.get("complete"):
                tags = (mmsi, "late")
            arrival = result.get("arrival_at")
            departure = result.get("departure_at")
            if result.get("in_port"):
                arrival_text = "в порту"
            elif arrival:
                mark = "≈" if result.get("arrival_estimate") else ""
                arrival_text = f"{mark}{arrival:%d.%m %H:%M}"
            elif result.get("no_position"):
                arrival_text = "нет позиции"
            else:
                arrival_text = "не доходит"
            # Итоговый путь — от текущего момента до выхода из порта:
            # дорога до порта плюс стоянка.
            total_text = "—"
            if departure:
                total_hours = max(0.0, (departure - datetime.now()).total_seconds() / 3600.0)
                total_text = f"{forecast.duration_text(total_hours)} · {departure:%d.%m %H:%M}"
            tree.insert("", "end", iid=mmsi, tags=tags, values=(
                vessel.get("name") or "—",
                result.get("port_label") or result.get("port") or "—",
                arrival_text,
                forecast.duration_text(result.get("stay_hours")),
                f"{departure:%d.%m %H:%M}" if departure else "—",
                total_text,
                forecast.duration_text(result.get("sea_hours")) if result.get("sea_hours") else "—",
                forecast.plan_route_text(result),
                mmsi))
        self._forecast_plans = plans
        in_port = sum(1 for _, result in rows if result.get("in_port"))
        missing = sum(1 for _, result in rows if result.get("no_position") and not result.get("in_port"))
        late = sum(1 for _, result in rows
                   if not result.get("complete") and not result.get("no_position"))
        if hasattr(self, "forecast_info"):
            text = (f"В плане судов: {len(rows)} — в порту {in_port}, "
                    f"в пути {len(rows) - in_port - missing}.")
            if late:
                text += f" Не успевают дойти за 4 суток: {late}."
            if missing:
                text += f" Без позиции (только AIS-catcher): {missing}."
            text += (" Стоянка — по истории визитов, выход и маршрут в море — "
                     "оценка по курсу и скорости.")
            self.forecast_info.set(text)
        if keep and keep in plans:
            tree.selection_set(keep)
            self.show_forecast_detail()

    def show_forecast_detail(self, event=None):
        """Показывает под таблицей полный план выбранного судна."""
        text = getattr(self, "forecast_detail", None)
        tree = getattr(self, "forecast_tree", None)
        if not text or not tree:
            return
        selection = tree.selection()
        content = "Выберите судно — покажу полный план."
        if selection:
            plan = (getattr(self, "_forecast_plans", {}) or {}).get(selection[0])
            if plan:
                vessel, result = plan
                content = f"{vessel.get('name') or '—'} ({vessel.get('mmsi')})\n"
                content += forecast.plan_detail(result)
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.insert("1.0", content)
        text.configure(state="disabled")

    def build_visits(self):
        tk, ttk = self.tk, self.ttk
        frame = self.tab_visits
        top = ttk.Frame(frame)
        top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(top, text="Стоянки судов в наших портах: когда пришло, когда ушло, "
                            "сколько простояло, с какой осадкой и грузом.",
                  foreground="#555").pack(side="left")
        ttk.Button(top, text="Обновить", command=self.refresh_visits).pack(side="right")
        ttk.Button(top, text="Загрузить из истории", command=self.load_history_stays
                   ).pack(side="right", padx=(0, 6))
        self.visits_info = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.visits_info, foreground="#555").pack(
            anchor="w", padx=8, pady=(0, 4))
        self.visits_tree = self.make_tree(
            frame, (("name", "Судно", 180), ("port", "Порт", 110),
                    ("arrived", "Пришёл", 130), ("left", "Ушёл", 130),
                    ("stay", "Стоянка", 110), ("draft", "Осадка вход→выход", 150),
                    ("cargo", "Груз", 230), ("source", "Источник", 90)))

    def refresh_visits(self):
        tree = getattr(self, "visits_tree", None)
        if not tree:
            return
        for item in tree.get_children():
            tree.delete(item)
        open_visits = 0
        for visit in self.store.visits(300):
            vessel = self.store.vessel(visit.get("mmsi")) or {}
            entered = parse_iso(visit.get("entered_at"))
            left = parse_iso(visit.get("left_at"))
            if left and entered:
                stay = span_text((left - entered).total_seconds() / 3600)
            elif entered:
                stay = span_text((datetime.now() - entered).total_seconds() / 3600) + " (идёт)"
                open_visits += 1
            else:
                stay = "—"
            draft_in = visit.get("draft_in")
            draft_out = visit.get("draft_out")
            if draft_in is None and draft_out is None:
                draft = "—"
            else:
                draft = f"{fmt_number(draft_in)} → {fmt_number(draft_out)} м"
            tree.insert("", "end", values=(
                vessel.get("name") or f"MMSI {visit.get('mmsi')}",
                visit.get("port"),
                self.fmt_time(visit.get("entered_at")),
                self.fmt_time(visit.get("left_at")) if left else "на месте",
                stay,
                draft,
                visit.get("cargo_kind") or self.cargo_kind_text(vessel),
                "история" if visit.get("source") == "track" else "наблюдение",
            ))
        counts = self.store.counts()
        self.visits_info.set(
            f"Визитов в базе: {counts['visits']}, из них открыто сейчас: {open_visits}. "
            f"Источники: «наблюдение» — геозона порта по живому AIS (считается с момента "
            f"входа, время идёт по часам); «история» — трек за сутки и свои позиции "
            f"за {self.config.get('history_days', 7)} дн.")

    def load_history_stays(self):
        if getattr(self, "_history_loading", False):
            self.log("Загрузка истории стоянок уже идёт — подождите.")
            return
        self._history_loading = True
        threading.Thread(target=self._load_history_worker, daemon=True).start()

    def _load_history_worker(self):
        self.log("История стоянок: запрашиваю треки судов из базы…")
        try:
            result = self.core.import_history_stays(limit=25, pause=0.5)
        except Exception as error:
            self.log(f"История стоянок: ошибка — {error}")
        else:
            self.log(f"История стоянок: проверено судов {result['checked']}, "
                     f"новых визитов {result['imported']}"
                     + (f", ошибок {result['failed']}" if result.get("failed") else "") + ".")
        finally:
            self._history_loading = False

    def build_visit_history(self):
        """Вкладка «История стоянок»: стоянки в наших портах с кнопками портов."""
        tk, ttk = self.tk, self.ttk
        frame = self.tab_visit_history
        top = ttk.Frame(frame)
        top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(top, text="Кто и сколько стоял в наших портах: приход, уход, время "
                            "стоянки, осадка и груз. Кнопка порта оставляет только его "
                            "стоянки; в скобках — сколько их в базе.",
                  foreground="#555").pack(side="left")
        ttk.Button(top, text="Обновить", command=self.refresh_visit_history
                   ).pack(side="right")
        port_bar = ttk.Frame(frame)
        port_bar.pack(fill="x", padx=8, pady=(0, 4))
        self.visit_port = tk.StringVar(value="")
        self.visit_port_buttons = {}
        ports = [((port.get("name") or "").upper(), port.get("label") or port.get("name"))
                 for port in self.config.get("ports") or []]
        for name, label in [("", "Все порты")] + ports:
            button = tk.Button(port_bar, text=label, command=lambda key=name: self.pick_visit_port(key),
                               relief="groove", padx=8, pady=2, font=("Segoe UI", 9))
            button.pack(side="left", padx=(0, 4))
            self.visit_port_buttons[name] = button
        self.visit_history_info = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.visit_history_info, foreground="#333").pack(
            anchor="w", padx=8, pady=(0, 4))
        self.visit_history_tree = self.make_tree(
            frame, (("name", "Судно", 180), ("port", "Порт", 110),
                    ("arrived", "Пришёл", 130), ("left", "Ушёл", 130),
                    ("stay", "Стоянка", 110), ("draft", "Осадка вход→выход", 150),
                    ("cargo", "Груз", 220), ("source", "Источник", 100)))
        self.pick_visit_port("")

    def pick_visit_port(self, name):
        """Выбор порта кнопкой: подсвечиваем её и перестраиваем список."""
        self.visit_port.set(name or "")
        for key, button in getattr(self, "visit_port_buttons", {}).items():
            active = (key == (name or ""))
            button.configure(relief="sunken" if active else "groove",
                             background="#d7e6f5" if active else "#f0f0f0")
        self.refresh_visit_history()

    def refresh_visit_history(self):
        tree = getattr(self, "visit_history_tree", None)
        if not tree:
            return
        port = (self.visit_port.get() or "")
        for item in tree.get_children():
            tree.delete(item)
        try:
            rows = self.store.visits(500, port=port or None)
            counts = self.store.visit_counts()
        except Exception as error:
            self.log(f"История стоянок: {error}")
            return
        for name, button in getattr(self, "visit_port_buttons", {}).items():
            label = next((p.get("label") or p.get("name")
                          for p in self.config.get("ports") or []
                          if (p.get("name") or "").upper() == name), "Все порты")
            if name == "":
                total = sum(entry["visits"] for entry in counts.values())
                button.configure(text=f"Все порты ({total})")
            else:
                button.configure(text=f"{label} ({counts.get(name, {}).get('visits', 0)})")
        open_visits = 0
        for visit in rows:
            vessel = self.store.vessel(visit.get("mmsi")) or {}
            entered = parse_iso(visit.get("entered_at"))
            left = parse_iso(visit.get("left_at"))
            if left and entered:
                stay = span_text((left - entered).total_seconds() / 3600)
            elif entered:
                stay = span_text((datetime.now() - entered).total_seconds() / 3600) + " (идёт)"
                open_visits += 1
            else:
                stay = "—"
            draft_in = visit.get("draft_in")
            draft_out = visit.get("draft_out")
            if draft_in is None and draft_out is None:
                draft = "—"
            else:
                draft = f"{fmt_number(draft_in)} → {fmt_number(draft_out)} м"
            tree.insert("", "end", tags=(str(visit.get("mmsi")),), values=(
                vessel.get("name") or f"MMSI {visit.get('mmsi')}",
                visit.get("port"),
                self.fmt_time(visit.get("entered_at")),
                self.fmt_time(visit.get("left_at")) if left else "на месте",
                stay, draft,
                visit.get("cargo_kind") or self.cargo_kind_text(vessel),
                "история" if visit.get("source") == "track" else "наблюдение",
            ))
        left_count = len(rows) - open_visits
        where = f"в порту {port}" if port else "во всех наших портах"
        self.visit_history_info.set(
            f"Стоянок {where}: {len(rows)} (завершённых {left_count}, идут сейчас "
            f"{open_visits}). Портов в базе: {len(counts)}. Стоянки считаются по "
            f"геозоне порта; история из трека добавляется кнопкой «Загрузить из "
            f"истории» во вкладке «Стоянки».")

    def build_expected(self):
        tk, ttk = self.tk, self.ttk
        frame = self.tab_expected
        top = ttk.Frame(frame)
        top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(top, text="Кто указал наш порт пунктом назначения — таблица портовых "
                            "страниц AIS-catcher (открытая сеть приёмников, без ключа). "
                            "Страницы есть только у части портов, ETA передаёт сам "
                            "капитан и обновляет его не всегда.",
                  foreground="#555", wraplength=760, justify="left").pack(
            side="left", fill="x", expand=True)
        self.expected_button = ttk.Button(top, text="Обновить сейчас",
                                         command=self.refresh_expected_now)
        self.expected_button.pack(side="right")
        self.expected_info = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.expected_info, foreground="#333",
                  wraplength=1200, justify="left").pack(anchor="w", padx=8, pady=(0, 4))
        self.expected_tree = self.make_tree(
            frame, (("port", "Порт", 110), ("name", "Судно", 180),
                    ("type", "Тип", 130), ("eta", "Заявленный ETA", 200),
                    ("heard", "Последний сигнал", 150), ("mmsi", "MMSI", 100),
                    ("updated", "Обновлено", 150)))
        # Судно, чьё последнее сообщение AIS давно не приходило, — серым.
        self.expected_tree.tag_configure("old", foreground="#8c8c8c")
        self.refresh_expected()
        self.root.after(60000, self._expected_watch)

    @staticmethod
    def _heard_hours(text):
        """«2m ago», «1h 3m ago», «16h 9m ago» — сколько часов назад это было."""
        import re as _re
        text = str(text or "")
        found = _re.findall(r"(\d+)\s*([hm])", text)
        if not found:
            return None
        hours = 0.0
        for value, unit in found:
            hours += int(value) / 60 if unit == "m" else int(value)
        return hours

    # Месяцы AIS-catcher пишет по-английски («09 Nov 18:00»); разбираем
    # по словарю, а не через strptime: тот ругается на дату без года и зависит
    # от системной локали (в русской «Nov» уже не месяц).
    _MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
               "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

    @staticmethod
    def _eta_state(text):
        """Сколько часов осталось до заявленного ETA (отрицательное — прошёл)."""
        parts = str(text or "").strip().split()
        if len(parts) != 3:
            return None
        try:
            day = int(parts[0])
            month = App._MONTHS.get(parts[1].strip(".").lower())
            hour, minute = (int(piece) for piece in parts[2].split(":"))
        except (ValueError, TypeError):
            return None
        if not month:
            return None
        now = datetime.now()
        try:
            moment = datetime(now.year, month, day, hour, minute)
        except ValueError:
            return None
        if moment > now + timedelta(days=300):   # ETA «прошлого года»
            try:
                moment = moment.replace(year=moment.year - 1)
            except ValueError:
                return None
        return round((moment - now).total_seconds() / 3600, 1)

    def _expected_watch(self):
        """Раз в минуту смотрим, не обновил ли файл фоновый канал AIS-catcher."""
        try:
            stamp = portcalls.EXPECTED_PATH.stat().st_mtime
        except OSError:
            stamp = None
        if stamp and stamp != getattr(self, "_expected_stamp", None):
            self._expected_stamp = stamp
            self.refresh_expected()
        self.root.after(60000, self._expected_watch)

    def refresh_expected(self):
        tree = getattr(self, "expected_tree", None)
        if not tree:
            return
        for item in tree.get_children():
            tree.delete(item)
        cache = portcalls.load_cache()
        self._expected_stamp = portcalls.EXPECTED_PATH.stat().st_mtime \
            if portcalls.EXPECTED_PATH.exists() else None
        rows = portcalls.expected_rows(cache)
        updated_at = parse_iso(cache.get("updated"))
        local_stamp = "—"
        if updated_at is not None:
            try:
                local_stamp = (updated_at.astimezone() if updated_at.tzinfo
                               else updated_at).strftime("%d.%m.%Y %H:%M")
            except (ValueError, OSError):
                local_stamp = updated_at.strftime("%d.%m.%Y %H:%M")
        stale_rows = 0
        for row in rows:
            hours = self._heard_hours(row.get("last_heard"))
            eta_hours = self._eta_state(row.get("eta"))
            eta = row.get("eta") or "—"
            if eta_hours is not None and eta_hours < -12:
                eta += f" (прошёл {span_text(abs(eta_hours))} назад)"
            tags = ("old",) if hours is not None and hours > 12 else ()
            if tags:
                stale_rows += 1
            tree.insert("", "end", tags=tags, values=(
                row.get("port"), row.get("name"), row.get("type") or "—",
                eta, row.get("last_heard") or "—", row.get("mmsi"), local_stamp))
        ports = cache.get("ports") or {}
        failed = [entry.get("name") for entry in ports.values() if entry.get("error")]
        missing = [entry.get("name") for entry in ports.values() if entry.get("unsupported")]
        info = (f"Ожидается судов: {len(rows)} в портах: {len(ports)}."
                if ports else "Данных пока нет: нажмите «Обновить сейчас».")
        if updated_at:
            now = datetime.now(updated_at.tzinfo) if updated_at.tzinfo else datetime.now()
            minutes = int((now - updated_at).total_seconds() / 60)
            info += (f" Таблица получена {local_stamp} (местное время, "
                     f"{span_text(minutes / 60)} назад).")
            interval = float(self.config.get("aci_minutes") or 60)
            left = interval - minutes
            if left > 0:
                info += f" Автообновление примерно через {round(left)} мин."
        if failed:
            info += f" Не ответили: {', '.join(failed)}."
        if missing:
            info += f" Нет страницы у AIS-catcher: {', '.join(missing)}."
        if stale_rows:
            info += (f" Серым — {stale_rows} судов без сигнала AIS дольше 12 ч: "
                     f"их ETA уже не значит ничего.")
        if cache.get("stale"):
            info += (" Сейчас страницы отдают 0 судов — показан прошлый список "
                     f"от {(cache.get('updated') or '—')[:16].replace('T', ' ')}.")
        if rows:
            info += (" Заявленный ETA — то, что передал капитан (UTC); если ETA прошёл "
                     "несколько дней назад — капитан его не обновил, судно идёт по "
                     "старому плану.")
        self.expected_info.set(info)

    def refresh_expected_now(self):
        if getattr(self, "_expected_loading", False):
            self.log("Обновление ожидаемых уже идёт — подождите.")
            return
        self._expected_loading = True
        button = getattr(self, "expected_button", None)
        if button is not None:
            button.configure(state="disabled", text="Обновляю… (до 1 мин)")
        self.expected_info.set("Запрашиваю портовые страницы AIS-catcher: "
                               "6 портов, на каждый до 45 с. Таблица обновится сама.")
        threading.Thread(target=self._expected_worker, daemon=True).start()

    def _expected_worker(self):
        self.log("AIS-catcher: запрашиваю ожидаемые прибытия по нашим портам…")
        try:
            data = portcalls.fetch_expected(self.config.get("ports"), log=self.log)
            portcalls.save_cache(data)
            total = sum(len(entry.get("vessels") or [])
                        for entry in (data.get("ports") or {}).values())
            self.log(f"AIS-catcher: ожидаемых судов {total}.")
        except Exception as error:
            self.log(f"AIS-catcher: ошибка — {error}")
        finally:
            self._expected_loading = False
            try:
                self.root.after(0, self._expected_done)
            except Exception:
                pass

    def _expected_done(self):
        button = getattr(self, "expected_button", None)
        if button is not None:
            button.configure(state="normal", text="Обновить сейчас")
        self.refresh_expected()

    def build_alerts(self):
        ttk = self.ttk
        frame = self.tab_alerts
        top = ttk.Frame(frame)
        top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(top, text="Журнал алертов: что отправлено и с каким результатом.",
                  foreground="#555").pack(side="left")
        ttk.Button(top, text="Обновить", command=self.refresh_alerts).pack(side="right")
        self.alerts_tree = self.make_tree(
            frame, (("time", "Время", 120), ("name", "Судно", 190), ("route", "Маршрут", 220),
                    ("status", "Статус", 90), ("error", "Ошибка", 260)))

    def scrollable_tab(self, frame):
        """Внутренний фрейм с полосой прокрутки: вкладка выше окна не помещается."""
        tk, ttk = self.tk, self.ttk
        canvas = tk.Canvas(frame, highlightthickness=0)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=8)
        scroll.pack(side="right", fill="y", padx=(0, 10), pady=8)

        def wheel(event):
            # колесо работает и над полями/кнопками внутри вкладки
            widget = canvas.winfo_containing(event.x_root, event.y_root)
            while widget is not None:
                if widget is inner:
                    canvas.yview_scroll(int(-event.delta / 120), "units")
                    return "break"
                widget = getattr(widget, "master", None)
            return None

        canvas.bind_all("<MouseWheel>", wheel, add="+")
        return inner

    def build_connection(self):
        tk, ttk = self.tk, self.ttk
        frame = self.tab_connection
        inner = self.scrollable_tab(frame)
        box = ttk.Frame(inner)
        box.pack(fill="x", padx=0, pady=0)
        box.columnconfigure(1, weight=1)

        self.var_key = tk.StringVar(value=self.config.get("api_key", ""))
        ttk.Label(box, text="API-ключ").grid(row=0, column=0, sticky="nw", pady=4)
        key_row = ttk.Frame(box)
        key_row.grid(row=0, column=1, sticky="ew", pady=4)
        key_row.columnconfigure(0, weight=1)
        key_entry = ttk.Entry(key_row, textvariable=self.var_key, show="*")
        key_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(key_row, text="Вставить из буфера",
                   command=self.paste_key).grid(row=0, column=1, padx=(6, 0))
        show = tk.BooleanVar(value=False)
        ttk.Checkbutton(key_row, text="показать", variable=show,
                        command=lambda: key_entry.config(show="" if show.get() else "*")
                        ).grid(row=0, column=2, sticky="w", padx=(6, 0))
        self.key_info = tk.StringVar()
        ttk.Label(key_row, textvariable=self.key_info, foreground="#777").grid(
            row=1, column=0, columnspan=3, sticky="w")
        self.var_key.trace_add("write", lambda *_: self.update_key_info())
        self.update_key_info()

        self.var_bboxes = tk.StringVar(value=format_bboxes(self.config.get("bounding_boxes")))
        ttk.Label(box, text="Район подписки").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(box, textvariable=self.var_bboxes).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(box, text="юг, запад, север, восток; несколько районов — через «;»",
                  foreground="#777").grid(row=2, column=1, sticky="w")

        # Все каналы, с которыми работает программа
        self.var_use_ow = tk.BooleanVar(value=bool(self.config.get("use_openwaters", True)))
        self.var_ow_token = tk.StringVar(value=self.config.get("openwaters_token", ""))
        self.var_use_snapshot = tk.BooleanVar(value=bool(self.config.get("use_snapshot", True)))
        self.var_snapshot_minutes = tk.StringVar(value=str(self.config.get("snapshot_minutes", 10)))
        # Additional service toggles
        self.feed_status = {}
        self.var_use_vf = tk.BooleanVar(value=bool(self.config.get("use_vf", False)))
        self.var_use_vfa = tk.BooleanVar(value=bool(self.config.get("use_vfa", False)))
        self.var_use_mt = tk.BooleanVar(value=bool(self.config.get("use_mt", False)))
        self.var_use_mst = tk.BooleanVar(value=bool(self.config.get("use_mst", False)))
        self.var_use_dt = tk.BooleanVar(value=bool(self.config.get("use_dt", False)))
        self.var_use_gor = tk.BooleanVar(value=bool(self.config.get("use_gor", True)))
        self.var_use_mlive = tk.BooleanVar(value=bool(self.config.get("use_mlive", True)))
        self.var_use_srd = tk.BooleanVar(value=bool(self.config.get("use_srd", True)))
        self.var_use_aci = tk.BooleanVar(value=bool(self.config.get("use_aci", True)))
        self.var_use_gfw = tk.BooleanVar(value=bool(self.config.get("use_gfw", False)))
        self.var_use_dat = tk.BooleanVar(value=bool(self.config.get("use_dat", False)))
        self.var_use_aishub = tk.BooleanVar(value=bool(self.config.get("use_aishub", False)))
        # Ключи API сервисов, которым они нужны
        self.var_vf_key = tk.StringVar(value=self.config.get("vf_key", ""))
        self.var_mt_key = tk.StringVar(value=self.config.get("mt_key", ""))
        self.var_mst_key = tk.StringVar(value=self.config.get("mst_key", ""))
        self.var_gfw_token = tk.StringVar(value=self.config.get("gfw_key", ""))
        self.var_dat_key = tk.StringVar(value=self.config.get("dat_key", ""))
        self.var_aishub_key = tk.StringVar(value=self.config.get("aishub_key", ""))
        self.feed_status.update({
            "vf": tk.StringVar(), "vfa": tk.StringVar(), "mt": tk.StringVar(),
            "mst": tk.StringVar(), "dt": tk.StringVar(),
            "aci": tk.StringVar(), "gfw": tk.StringVar(),
            "dat": tk.StringVar(), "aishub": tk.StringVar(),
            "gor": tk.StringVar(), "mlive": tk.StringVar(),
            "srd": tk.StringVar(),
        })
        for key in self.feed_status:
            self.feed_status[key].set(self.service_status_text(key))

        ttk.Label(box, text="Подключения").grid(row=4, column=0, sticky="nw", pady=(16, 4))
        ttk.Label(box, text=f"версия {APP_VERSION} · папка: {APP_DIR}",
                  foreground="#777").grid(row=3, column=1, sticky="w", pady=(8, 0))
        links = ttk.LabelFrame(box, text="Каналы данных")
        links.grid(row=4, column=1, sticky="ew", pady=(16, 4))
        links.columnconfigure(1, weight=1)

        # 1. AISStream — основной поток
        ttk.Label(links, text="AISStream", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky="w", padx=(8, 6), pady=(6, 2))
        self.feed_status["ais"] = tk.StringVar(value="не запущен")
        ttk.Label(links, textvariable=self.feed_status["ais"], foreground="#555"
                  ).grid(row=0, column=1, sticky="w", pady=(6, 2))
        ais_row = ttk.Frame(links)
        ais_row.grid(row=1, column=0, columnspan=2, sticky="w", padx=8)
        ttk.Label(ais_row, text="живой поток wss://stream.aisstream.io (ключ выше)",
                  foreground="#777").pack(side="left")
        ttk.Button(ais_row, text="Проверить", command=self.check_key
                   ).pack(side="left", padx=(8, 0))
        ttk.Button(ais_row, text="Открыть aisstream.io",
                   command=lambda: webbrowser.open(AIRSTREAM_SITE)).pack(side="left", padx=(6, 0))

        # 2. Open Waters — второй живой поток
        ttk.Checkbutton(links, text="Open Waters AIS — второй живой поток (совместим с AISStream)",
                        variable=self.var_use_ow).grid(row=2, column=0, columnspan=2,
                                                       sticky="w", padx=4, pady=(10, 0))
        self.feed_status["ow"] = tk.StringVar(value="не запущен")
        ttk.Label(links, textvariable=self.feed_status["ow"], foreground="#555"
                  ).grid(row=3, column=0, columnspan=2, sticky="w", padx=30)
        ow_row = ttk.Frame(links)
        ow_row.grid(row=4, column=0, columnspan=2, sticky="ew", padx=30, pady=(2, 0))
        ttk.Label(ow_row, text="Токен:").pack(side="left")
        ttk.Entry(ow_row, textvariable=self.var_ow_token, width=28, show="*"
                  ).pack(side="left", padx=(6, 0))
        ttk.Button(ow_row, text="Проверить", command=self.check_openwaters
                   ).pack(side="left", padx=(6, 0))
        ttk.Button(ow_row, text="Открыть openwaters.io",
                   command=lambda: webbrowser.open("https://openwaters.io/ais/")
                   ).pack(side="left", padx=(6, 0))


        # 3. Снимок района — последние известные позиции всех судов вокруг портов
        ttk.Checkbutton(links, text="Снимок района — все известные суда вокруг наших портов",
                        variable=self.var_use_snapshot).grid(row=5, column=0, columnspan=3,
                                                             sticky="w", padx=4, pady=(10, 0))
        self.feed_status["snap"] = tk.StringVar(value="не запущен")
        ttk.Label(links, textvariable=self.feed_status["snap"], foreground="#555"
                  ).grid(row=6, column=0, columnspan=3, sticky="w", padx=30)
        snap_row = ttk.Frame(links)
        snap_row.grid(row=7, column=0, columnspan=3, sticky="w", padx=30, pady=(2, 8))
        ttk.Label(snap_row, text="обновлять каждые").pack(side="left")
        ttk.Entry(snap_row, textvariable=self.var_snapshot_minutes, width=5).pack(side="left", padx=(4, 0))
        ttk.Label(snap_row, text="мин (ключ не нужен)").pack(side="left", padx=(4, 8))
        ttk.Button(snap_row, text="Проверить сейчас", command=self.check_snapshot).pack(side="left")

        # 4. Дополнительные подключения — каждый сервис отдельным блоком, как Open Waters
        ttk.Separator(links, orient="horizontal").grid(
            row=8, column=0, columnspan=3, sticky="ew", padx=8, pady=(12, 0))
        ttk.Label(links, text="Дополнительные подключения",
                  font=("Segoe UI", 9, "bold")).grid(
            row=9, column=0, columnspan=3, sticky="w", padx=(8, 6), pady=(8, 2))
        ttk.Label(links, text="Каждый сервис работает своим потоком и пишет в ту же базу.",
                  foreground="#777").grid(row=10, column=0, columnspan=3,
                                          sticky="w", padx=(8, 6), pady=(0, 4))

        row = 11
        for name, key, desc in SERVICE_BLOCKS:
            var = getattr(self, f"var_use_{key}")
            ttk.Checkbutton(links, text=f"{name} — {desc}", variable=var,
                            command=lambda k=key, v=var: self._on_service_toggle(k, v)
                            ).grid(row=row, column=0, columnspan=3, sticky="w",
                                   padx=4, pady=(10, 0))
            ttk.Label(links, textvariable=self.feed_status[key], foreground="#555"
                      ).grid(row=row + 1, column=0, columnspan=3, sticky="w", padx=30)
            service_row = ttk.Frame(links)
            service_row.grid(row=row + 2, column=0, columnspan=3, sticky="w",
                             padx=30, pady=(2, 0))
            key_var = service_key_var(self, key)
            if key_var is not None:
                ttk.Label(service_row, text="Ключ:").pack(side="left")
                ttk.Entry(service_row, textvariable=key_var, width=28, show="*"
                          ).pack(side="left", padx=(6, 0))
            else:
                ttk.Label(service_row, text="ключ не нужен", foreground="#777"
                          ).pack(side="left")
            ttk.Button(service_row, text="Проверить",
                       command=lambda k=key: self.check_service(k)
                       ).pack(side="left", padx=(6, 0))
            site_name, site_url = SERVICE_SITES[key]
            ttk.Button(service_row, text=f"Открыть {site_name}",
                       command=lambda u=site_url: webbrowser.open(u)
                       ).pack(side="left", padx=(6, 0))
            row += 3

        # 5. Источники без ключа — кнопка проверки: жив ли и читается ли автоматически
        ttk.Separator(links, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", padx=8, pady=(12, 0))
        ttk.Label(links, text="Источники без ключа — проверка",
                  font=("Segoe UI", 9, "bold")).grid(
            row=row + 1, column=0, columnspan=2, sticky="w", padx=(8, 6), pady=(8, 2))
        ttk.Button(links, text="Проверить все", command=self.check_all_sources
                   ).grid(row=row + 1, column=2, sticky="e", padx=8, pady=(8, 2))
        ttk.Button(links, text="Обновить данные сейчас", command=self.refresh_sources_now
                   ).grid(row=row + 1, column=1, sticky="e", padx=8, pady=(8, 2))
        ttk.Label(links, text="Ключ не нужен. «Проверить» показывает, жив ли источник "
                              "и читается ли он автоматически.",
                  foreground="#777").grid(row=row + 2, column=0, columnspan=3,
                                           sticky="w", padx=8)
        row += 3
        self.source_status = {}
        for source in portcalls.SOURCES:
            key = source["key"]
            var = tk.StringVar(value=self.source_status_text(key))
            self.source_status[key] = var
            ttk.Label(links, text=source["title"], font=("Segoe UI", 9, "bold")
                      ).grid(row=row, column=0, columnspan=3, sticky="w",
                             padx=(8, 6), pady=(8, 0))
            ttk.Label(links, textvariable=var, foreground="#555"
                      ).grid(row=row + 1, column=0, columnspan=3, sticky="w", padx=30)
            source_row = ttk.Frame(links)
            source_row.grid(row=row + 2, column=0, columnspan=3, sticky="w",
                            padx=30, pady=(2, 0))
            ttk.Label(source_row, text=source["how"], foreground="#777").pack(side="left")
            ttk.Button(source_row, text="Проверить",
                       command=lambda k=key: self.check_source(k)
                       ).pack(side="left", padx=(6, 0))
            ttk.Button(source_row, text="Открыть",
                       command=lambda u=source["url"]: webbrowser.open(u)
                       ).pack(side="left", padx=(6, 0))
            row += 3

        buttons = ttk.Frame(box)
        buttons.grid(row=5, column=1, sticky="w", pady=(16, 0))
        ttk.Button(buttons, text="Сохранить и перезапустить",
                   command=self.save_and_restart).pack(side="left")
        ttk.Button(buttons, text="Проверить ключ AISStream",
                   command=self.check_key).pack(side="left", padx=(8, 0))

        hint = (
            "Три канала и выбранные сервисы работают одновременно и пишут в одну базу.\n"
            "Дополнительные подключения: у каждого свой блок — галочка, статус, ключ API "
            "и кнопки «Проверить» / «Открыть сайт». "
            "Digitraffic и AIS-catcher работают без ключа: Digitraffic отдаёт только Балтику, "
            "AIS-catcher — ожидаемые прибытия в наши порты. "
            "Datalastic и AISHub включаются ключом (у AISHub — логин участника).\n\n"
            "Источники без ключа (реестры и портовые администрации) в конце вкладки: "
            "кнопка «Проверить» говорит, жив ли сайт и можно ли читать его автоматически, "
            "«Проверить все» — обходит все сразу.\n"
            "Корпоративные каналы (Spire, ORBCOMM, Kpler/MarineTraffic Enterprise, Windward) "
            "дают доступ только по договору — ключ выдаёт менеджер, поэтому галочки у них нет.\n\n"
            "• AISStream — основной живой поток (нужен бесплатный ключ, см. шаги ниже).\n"
            "• Open Waters AIS — второй живой поток, тот же протокол; токен бесплатный на openwaters.io.\n"
            "• Снимок района — раз в N минут забирает последние известные позиции всех судов "
            "вокруг ваших портов, поэтому карта не пустует после запуска и при обрывах связи.\n\n"
            "Как получить ключ AISStream (бесплатно):\n"
            "1. Откройте aisstream.io и войдите через GitHub.\n"
            "2. Нажмите Create API key и скопируйте ключ.\n"
            "3. Вставьте его выше и нажмите «Проверить».\n\n"
            "Ключи хранятся только в data/config.json на этом компьютере."
        )
        ttk.Label(inner, text=hint, justify="left", wraplength=980,
                  foreground="#444").pack(anchor="w", padx=12, pady=(16, 0))

    def build_filters(self):
        tk, ttk = self.tk, self.ttk
        frame = self.tab_filters
        top = ttk.Frame(frame)
        top.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(top, text="Кнопка «Обновить» перечитывает сохранённые настройки "
                            "из файла и затирает несохранённые правки.",
                  foreground="#555").pack(side="left")
        ttk.Button(top, text="Обновить",
                   command=lambda: self.refresh_tab(self.tab_filters)).pack(side="right")
        box = ttk.Frame(frame)
        box.pack(fill="x", padx=10, pady=8)
        box.columnconfigure(1, weight=1)

        self.var_dest = tk.StringVar(value=", ".join(self.config.get("filter_destinations") or []))
        self.var_orig = tk.StringVar(value=", ".join(self.config.get("filter_origins") or []))
        self.var_window = tk.StringVar(value=str(self.config.get("trip_window_hours", 96)))
        self.var_speed = tk.StringVar(value=str(self.config.get("min_speed_knots", 2)))
        self.var_cooldown = tk.StringVar(value=str(self.config.get("alert_cooldown_hours", 6)))
        self.var_history = tk.StringVar(value=str(self.config.get("history_days", 7)))
        self.var_interval = tk.StringVar(value=str(self.config.get("position_interval_seconds", 60)))
        # отдельное имя: этот StringVar из настроек раньше затирал галочку
        # «устаревшие» на карте, и выключить её было невозможно
        self.var_stale_minutes = tk.StringVar(value=str(self.config.get("stale_minutes", 15)))
        self.var_watch_km = tk.StringVar(value=str(self.config.get("port_watch_km", 50)))
        self.var_area_km = tk.StringVar(value=str(self.config.get("area_radius_km", 900)))

        # Чекбоксы для выбора портов отслеживания
        ports_list = self.config.get("ports") or []
        port_names_unique = []
        seen_ports = set()
        for p in ports_list:
            if isinstance(p, dict) and p.get("name"):
                pname = str(p["name"]).strip().upper()
                if pname and pname not in seen_ports:
                    seen_ports.add(pname)
                    port_names_unique.append(pname)

        self.port_dest_vars = {}
        self.port_orig_vars = {}
        current_dests = [str(x).strip().upper() for x in (self.config.get("filter_destinations") or [])]
        current_origs = [str(x).strip().upper() for x in (self.config.get("filter_origins") or [])]

        ttk.Label(box, text="Отслеживать суда, идущие в порты:", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        dest_frame = ttk.Frame(box)
        dest_frame.grid(row=1, column=0, columnspan=2, sticky="w")
        for i, pname in enumerate(port_names_unique):
            var = tk.BooleanVar(value=pname in current_dests)
            self.port_dest_vars[pname] = var
            ttk.Checkbutton(dest_frame, text=pname, variable=var).grid(row=i // 4, column=i % 4, sticky="w", padx=(0, 12))

        ttk.Label(box, text="Только из портов отправления:", font=("Segoe UI", 9, "bold")).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(10, 4))
        orig_frame = ttk.Frame(box)
        orig_frame.grid(row=3, column=0, columnspan=2, sticky="w")
        for i, pname in enumerate(port_names_unique):
            var = tk.BooleanVar(value=pname in current_origs)
            self.port_orig_vars[pname] = var
            ttk.Checkbutton(orig_frame, text=pname, variable=var).grid(row=i // 4, column=i % 4, sticky="w", padx=(0, 12))

        ttk.Separator(box, orient="horizontal").grid(row=4, column=0, columnspan=2, sticky="ew", pady=(12, 8))

        fields_start = 5
        fields = [
            ("Окно рейса, часов", self.var_window,
             "Сколько часов считаются «рейсом» — визит в порт старше окна не считается."),
            ("Минимальный ход, узлы", self.var_speed, "Стоящее судно целью не считается."),
            ("Пауза повтора, часов", self.var_cooldown,
             "По одному судну и маршруту алерт не чаще; 0 — один раз."),
            ("Хранить историю, дней", self.var_history, "Старые позиции удаляются автоматически."),
            ("Запись точки раз в, сек", self.var_interval,
             "Позиции с AIS приходят часто — в базу пишем с этим шагом."),
            ("Считать устаревшим, мин", self.var_stale_minutes,
             "Судно без сообщений дольше выглядит на карте бледным; 0 — не приглушать."),
            ("Радиус интереса, км", self.var_watch_km,
             "Судно ближе этого расстояния к нашему порту считается своим и показывается "
             "на карте. 20–30 км — порт и подходы; больше — на карту попадёт транзит."),
            ("Район вокруг портов, км", self.var_area_km,
             "Граница вкладки «Все суда»: судно в этом радиусе от любого нашего порта "
             "записывается в базу и показывается в списке. 800–1000 км — всё Чёрное "
             "море с подходами; 900 — по умолчанию."),
        ]
        for index, (title, var, hint) in enumerate(fields):
            r = fields_start + index * 2
            ttk.Label(box, text=title).grid(row=r, column=0, sticky="w", pady=3)
            ttk.Entry(box, textvariable=var, width=28).grid(row=r, column=1,
                                                            sticky="w", pady=3)
            ttk.Label(box, text=hint, foreground="#777").grid(row=r + 1, column=1,
                                                              sticky="w")

        ttk.Label(frame, text="Порты и геозоны (ИМЯ; широта; долгота; радиус_км)",
                  foreground="#444").pack(anchor="w", padx=12, pady=(12, 2))
        self.ports_text = tk.Text(frame, height=8, width=80, font=("Consolas", 9))
        self.ports_text.pack(fill="x", padx=12)
        self.ports_text.insert("1.0", format_ports(self.config.get("ports")))

        ttk.Button(frame, text="Сохранить настройки", command=self.save_settings).pack(
            anchor="w", padx=12, pady=8)
        ttk.Label(frame, text="После сохранения поток перезапускается, если он был запущен.",
                  foreground="#777").pack(anchor="w", padx=12)

    def build_telegram(self):
        tk, ttk = self.tk, self.ttk
        frame = self.tab_telegram
        box = ttk.Frame(frame)
        box.pack(fill="x", padx=10, pady=8)
        box.columnconfigure(1, weight=1)

        self.var_token = tk.StringVar(value=self.config.get("telegram_token", ""))
        self.var_chat = tk.StringVar(value=self.config.get("telegram_chat_id", ""))
        self.var_commands = tk.BooleanVar(value=bool(self.config.get("telegram_commands", True)))

        ttk.Label(box, text="Токен бота").grid(row=0, column=0, sticky="w", pady=4)
        token_entry = ttk.Entry(box, textvariable=self.var_token, show="*")
        token_entry.grid(row=0, column=1, sticky="ew", pady=4)
        show = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="показать", variable=show,
                        command=lambda: token_entry.config(show="" if show.get() else "*")
                        ).grid(row=0, column=2, sticky="w")

        ttk.Label(box, text="chat_id (можно несколько) \nчерез запятую").grid(
            row=1, column=0, sticky="w", pady=4)
        ttk.Entry(box, textvariable=self.var_chat).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(box, text="например: 123456789, -1001234567890 — алерты и сводки уйдут "
                            "во все чаты (несколько устройств)",
                  foreground="#666").grid(row=1, column=2, sticky="w", padx=(8, 0))
        ttk.Checkbutton(box, text="принимать команды (/status, /last, /mute)",
                        variable=self.var_commands).grid(row=2, column=1, sticky="w")

        ttk.Button(box, text="Отправить тестовое сообщение",
                   command=self.test_telegram).grid(row=3, column=1, sticky="w", pady=8)
        ttk.Button(box, text="Обновить",
                   command=lambda: self.refresh_tab(self.tab_telegram)
                   ).grid(row=3, column=2, sticky="w", pady=8)

        ttk.Label(
            frame,
            text="1. @BotFather → /newbot → токен вида 123456:AA...\n"
                 "2. Напишите боту любое сообщение (без этого он не сможет ответить).\n"
                 "3. Нажмите «Отправить тестовое сообщение»: если chat_id пуст, программа "
                 "найдёт его сама и подставит.\n"
                 "4. Сохраните настройки — алерты пойдут во все указанные чаты.\n"
                 "5. Несколько устройств: с каждого напишите боту /id и добавьте id "
                 "в поле chat_id через запятую — сводки и алерты получат все.\n"
                 "6. Бот теперь запускается вместе с приложением и закрывается вместе с ним; "
                 "галочка «принимать команды» выключает его.",
            justify="left", foreground="#444").pack(anchor="w", padx=12, pady=(16, 0))

    def build_services(self):
        tk, ttk = self.tk, self.ttk
        frame = self.tab_services
        canvas = tk.Canvas(frame, highlightthickness=0)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=8)
        scroll.pack(side="right", fill="y", padx=(0, 10), pady=8)

        top = ttk.Frame(inner)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="Альтернативные источники AIS и сервисы для проверки судов",
                  font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(top, text="Обновить",
                   command=lambda: self.refresh_tab(self.tab_services)).pack(side="right")
        ttk.Label(inner, text="Можно использовать как запасной источник или для перепроверки по MMSI/IMO",
                  foreground="#555").pack(anchor="w", pady=(0, 12))

        box1 = ttk.LabelFrame(inner, text="Онлайн-сервисы (карточки судов)")
        box1.pack(fill="x", pady=(0, 10))
        for name, url in [
            ("VesselFinder", "https://www.vesselfinder.com/"),
            ("MarineTraffic", "https://www.marinetraffic.com/"),
            ("MyShipTracking", "https://www.myshiptracking.com/"),
            ("AIS Marine Traffic", "https://www.aismarinetraffic.com/"),
            ("Equasis (тип, дедвейт, владелец)", "https://www.equasis.org/"),
            ("IMO GISIS (реестр судов)", "https://gisis.imo.org/"),
            ("Open Waters (карта и API)", "https://openwaters.io/ais/"),
        ]:
            f = ttk.Frame(box1)
            f.pack(fill="x", padx=8, pady=2)
            ttk.Label(f, text=name, width=18).pack(side="left")
            ttk.Button(f, text="Открыть", command=lambda u=url: webbrowser.open(u)).pack(side="left")

        box2 = ttk.LabelFrame(inner, text="AIS-потоки (альтернативы AISStream)")
        box2.pack(fill="x", pady=(0, 10))
        for name, url, desc in [
            ("Open Waters AIS", "https://openwaters.io/ais/", "Совместим с протоколом AISStream, агрегирует данные AISHub и др."),
            ("AISHub", "https://www.aishub.net/", "Бесплатно при наличии своего AIS-приёмника (обмен данными)."),
            ("Digitraffic (Финляндия)", "https://www.digitraffic.fi/en/marine-traffic/", "Открытые данные, только Балтика."),
            ("OpenCpn", "https://www.opencpn.org/", "Навигация + приём NMEA AIS по UDP/TCP"),
            ("AIS-catcher", "https://github.com/jvde-github/AIS-catcher", "Декодер для RTL-SDR, веб-интерфейс, форвард в сети"),
        ]:
            f = ttk.Frame(box2)
            f.pack(fill="x", padx=8, pady=2)
            ttk.Label(f, text=name, width=22).pack(side="left")
            ttk.Label(f, text=desc, foreground="#555").pack(side="left", padx=(0, 8))
            ttk.Button(f, text="Сайт", command=lambda u=url: webbrowser.open(u)).pack(side="right")

        box3 = ttk.LabelFrame(inner, text="Десктопные программы")
        box3.pack(fill="x", pady=(0, 10))
        for name, url, desc in [
            ("OpenCPN", "https://opencpn.org/", "Навигация + приём NMEA AIS по UDP/TCP"),
            ("AIS-catcher", "https://github.com/jvde-github/AIS-catcher", "Декодер для RTL-SDR, веб-интерфейс, форвард в сети"),
            ("AIS Dispatcher (AISHub)", "https://www.aishub.net/ais-dispatcher", "Форвардинг данных с приёмника в несколько сетей"),
        ]:
            f = ttk.Frame(box3)
            f.pack(fill="x", padx=8, pady=2)
            ttk.Label(f, text=name, width=22).pack(side="left")
            ttk.Label(f, text=desc, foreground="#555").pack(side="left", padx=(0, 8))
            ttk.Button(f, text="Сайт", command=lambda u=url: webbrowser.open(u)).pack(side="right")

        ttk.Label(inner, text="Подсказка: в карточке судна (панель справа на вкладке Радар) добавлены прямые ссылки для быстрой проверки.",
                  foreground="#666").pack(anchor="w", pady=(12, 0))

    def build_log(self):
        tk, ttk = self.tk, self.ttk
        frame = self.tab_log
        top = ttk.Frame(frame)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Button(top, text="Очистить журнал",
                   command=lambda: (self.log_text.configure(state="normal"),
                                    self.log_text.delete("1.0", "end"),
                                    self.log_text.configure(state="disabled"))).pack(side="left")
        ttk.Button(top, text="Обновить",
                   command=lambda: self.refresh_tab(self.tab_log)).pack(side="left", padx=(6, 0))
        ttk.Label(top, text=f"База: {DB_PATH}", foreground="#777").pack(side="right")
        self.log_text = tk.Text(frame, state="disabled", wrap="word", font=("Consolas", 9))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        scroll.pack(side="right", fill="y", padx=(0, 8), pady=(0, 8))

    # -- буфер обмена
    def _walk_widgets(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self._walk_widgets(child)

    def enable_clipboard(self):
        """Правый клик и Ctrl+V/C/X/A во всех полях ввода.

        Tkinter сам по себе не делает контекстное меню по правой кнопке, поэтому
        скопированный из браузера ключ некуда вставить мышью. Здесь каждому
        полю добавляется меню «Вставить/Копировать/Вырезать/Выделить всё» и
        привычные сочетания клавиш.
        """
        for widget in [self.root, *self._walk_widgets(self.root)]:
            if not self._is_text_input(widget):
                continue
            self._add_input_menu(widget)
            widget.bind("<Control-Insert>", lambda event, w=widget: self._paste(w))
            widget.bind("<Shift-Insert>", lambda event, w=widget: self._paste(w))
            widget.bind("<Control-a>", lambda event, w=widget: self._select_all(w))
            widget.bind("<Control-A>", lambda event, w=widget: self._select_all(w))
            # при русской раскладке Ctrl+V — это «М», и Tk не вызывает <<Paste>>;
            # поэтому смотрим на физическую клавишу (keycode), а не на символ
            widget.bind("<Control-KeyPress>", self._hotkey)

    def _hotkey(self, event):
        widget = event.widget
        code = getattr(event, "keycode", None)
        if code == 65:      # A
            return self._select_all(widget)
        if code == 67:      # C
            widget.event_generate("<<Copy>>")
            return "break"
        if code == 88:      # X
            widget.event_generate("<<Cut>>")
            return "break"
        if code == 86:      # V
            return self._paste(widget)
        return None

    @staticmethod
    def _is_text_input(widget) -> bool:
        try:
            kind = widget.winfo_class()
        except Exception:
            return False
        return kind in ("Entry", "TEntry", "Text", "Spinbox", "TSpinbox")

    def _select_all(self, widget):
        try:
            if widget.winfo_class() == "Text":
                widget.tag_add("sel", "1.0", "end-1c")
            else:
                widget.select_range(0, "end")
                widget.icursor("end")
        except Exception:
            pass
        return "break"

    def _paste(self, widget):
        try:
            text = self.root.clipboard_get()
        except Exception:
            self.log("Буфер обмена пуст — скопируйте текст и повторите.")
            return "break"
        try:
            if widget.winfo_class() == "Text":
                widget.insert("insert", text)
            else:
                widget.insert("insert", text)
        except Exception:
            pass
        return "break"

    def _add_input_menu(self, widget):
        tk = self.tk
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label="Вырезать", command=lambda: widget.event_generate("<<Cut>>"))
        menu.add_command(label="Копировать", command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="Вставить", command=lambda: self._paste(widget))
        menu.add_separator()
        menu.add_command(label="Выделить всё", command=lambda: self._select_all(widget))

        def popup(event):
            widget.focus_set()
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        widget.bind("<Button-3>", popup)

    def update_key_info(self):
        """Подпись под полем ключа: видно, что вставка действительно сработала."""
        value = (self.var_key.get() or "").strip()
        if not value:
            self.key_info.set("ключ не введён")
        else:
            self.key_info.set(f"введено символов: {len(value)} (скрыто звёздочками; "
                              f"нажмите «показать», чтобы увидеть)")

    def paste_key(self):
        """Кнопка «Вставить из буфера»: чистит пробелы и переводы строк."""
        try:
            raw = self.root.clipboard_get()
        except Exception:
            self.messagebox.showwarning(
                "Буфер обмена",
                "В буфере обмена нет текста.\nСкопируйте ключ на aisstream.io и повторите.")
            self.log("Вставка ключа: буфер обмена пуст.")
            return
        key = clean_key(raw)
        if not key:
            self.messagebox.showwarning("Буфер обмена",
                                        "В буфере обмена нет похожего на ключ текста.")
            return
        self.var_key.set(key)
        self.log(f"Ключ вставлен из буфера: {len(key)} символов, начинается с {key[:4]}…")

    def make_tree(self, parent, columns):
        ttk = self.ttk
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=18)
        for key, title, width in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        xscroll = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=scroll.set, xscrollcommand=xscroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        xscroll.pack(side="bottom", fill="x")
        tree.bind("<Double-1>", self.on_tree_double_click)
        return tree

    # -- действия интерфейса
    def toggle_targets_only(self):
        self.radar.targets_only = bool(self.var_targets_only.get())
        self.redraw_map()

    def toggle_tracks(self):
        self.radar.show_tracks = bool(self.var_tracks.get())
        self.redraw_map()

    def toggle_forecast(self):
        self.radar.show_forecast = bool(self.var_forecast.get())
        self.redraw_map()

    def toggle_stale(self):
        """Показать или скрыть суда с устаревшей позицией."""
        self.radar.show_stale = bool(self.var_stale.get())
        self.redraw_map()

    def toggle_schematic(self):
        """Переключить схематический вид карты."""
        self.radar.schematic_view = not self.radar.schematic_view
        self.var_schematic.set(int(self.radar.schematic_view))
        self.redraw_map()

    def redraw_map(self):
        try:
            port_only = bool(getattr(self, "var_port_only", None) is not None
                             and self.var_port_only.get())
            if port_only:
                # только причал и рейд: чужие точки района и суда в море не нужны
                vessels = self.core.port_vessels(600)
                traffic = []
            elif getattr(self, "var_traffic", None) is not None and self.var_traffic.get():
                # Галочка «трафик района» включена: вся обстановка — вся база
                # района плюс внешние точки, которых в базе ещё нет.
                vessels = self.core.area_vessels(4000)
                try:
                    traffic = self.core.traffic_vessels(800)
                except Exception:
                    traffic = []
            else:
                # Галочка снята: остаются только «свои» суда (идут в наш порт,
                # стояли в нём или рядом). Вся база района не рисуется.
                vessels = self.core.related_vessels(3000)
                traffic = []
            self.radar.draw(vessels, set(self._targets.keys()), traffic)
        except Exception as error:
            self.log(f"Отрисовка радара: {error}")

    def select_vessel(self, mmsi):
        self.selected = mmsi
        self.radar.selected = mmsi
        self.update_detail()
        self.update_track()
        self.load_photo(mmsi)
        self.redraw_map()

    def on_tree_double_click(self, event):
        tree = event.widget
        # Двойной щелчок в любой таблице открывает карточку — кроме вкладок,
        # где строки не про суда (стоянки, алерты, сервисы).
        allowed = [self.vessels_tree]
        if hasattr(self, "targets_tree"):
            allowed.append(self.targets_tree)
        if hasattr(self, "forecast_tree"):
            allowed.append(self.forecast_tree)
        if tree not in allowed:
            return
        selection = tree.selection()
        if not selection:
            return
        tags = tree.item(selection[0], "tags") or ()
        if not tags:
            return
        self.radar.select(tags[0])
        self.select_vessel(tags[0])

    def update_track(self):
        if not self.selected:
            self.radar.track = []
            return
        try:
            self.radar.track = self.store.track(self.selected, self.config.get("history_days") or 7)
        except Exception:
            self.radar.track = []

    def scroll_detail(self, event):
        """Колесо мыши над карточкой: прокрутка без перехода наверх."""
        step = -1 if getattr(event, "delta", 0) > 0 else 1
        self.detail_text.yview_scroll(step * 3, "units")
        return "break"

    def update_detail(self, force=False):
        """Перерисовывает карточку, сохраняя позицию прокрутки.

        Карточка обновляется на каждом такте, потому что в ней есть время
        («связь 5 мин назад»); чтобы текст не прыгал наверх, запоминаем
        положение прокрутки и возвращаем его после перерисовки, а если текст
        не изменился — вообще ничего не трогаем.
        """
        try:
            view = self.detail_text.yview()
            top_line = self.detail_text.index("@0,0")
        except Exception:
            view, top_line = (0.0, 1.0), None
        report = self.detail_report(self.selected)
        if not force and report == self._detail_cache:
            return
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", report)
        self.detail_text.configure(state="disabled")
        self._detail_cache = report
        # Раскладка пересчитывается только после update_idletasks: без этого
        # «прокрутить к строке» упирается в старую высоту текста.
        self.detail_text.update_idletasks()
        if view[1] >= 0.999:
            self.detail_text.see("end")
        elif top_line:
            try:
                self.detail_text.yview(top_line)
            except Exception:
                self.detail_text.yview_moveto(view[0])
        else:
            self.detail_text.yview_moveto(view[0])

    # -- фото судна
    def load_photo(self, mmsi):
        """Загружает фото выбранного судна в фоне, чтобы окно не подвисало."""
        if not mmsi:
            self._photo_for = None
            self.clear_photo()
            return
        if mmsi == self._photo_for:
            return
        self._photo_for = mmsi
        vessel = self.store.vessel(mmsi) or {}
        self.clear_photo("фото: загружаю...")
        threading.Thread(target=self._photo_worker, args=(mmsi, vessel.get("imo")),
                         daemon=True, name=f"photo-{mmsi}").start()

    def _photo_worker(self, mmsi, imo):
        try:
            path = photos.fetch(mmsi, imo, PHOTO_DIR)
        except Exception:
            path = None
        self.events.put(("photo", (mmsi, path)))

    def show_photo(self, mmsi, path):
        if mmsi != self.selected:
            return
        if not path:
            self.clear_photo("фото: у VesselFinder нет снимка этого судна")
            return
        try:
            from PIL import Image, ImageTk
        except ImportError:
            self.clear_photo("фото: нужна библиотека Pillow (pip install pillow)")
            return
        try:
            image = Image.open(path)
            image.thumbnail((300, 190))
            self._photo_image = ImageTk.PhotoImage(image)
            width, height = image.size
        except Exception as error:
            self.clear_photo(f"фото: не удалось открыть файл ({error.__class__.__name__})")
            return
        # У метки с картинкой width/height задаются в пикселях, а не в знаках.
        self.photo_label.configure(image=self._photo_image, text="",
                                   width=width, height=height)
        self.photo_note.configure(text="фото: VesselFinder")

    def clear_photo(self, note=""):
        self._photo_image = None
        self.photo_label.configure(image="", text="фото судна", height=9)
        self.photo_note.configure(text=note)

    def detail_report(self, mmsi):
        if not mmsi:
            return ("Кликните по судну на радаре или дважды щёлкните строку\n"
                    "в списке «Все суда», чтобы увидеть карточку.\n\n"
                    "Красные точки — цели, проходящие фильтр.\n"
                    "Жёлтые — танкеры. Серые — остальные суда.")
        vessel = self.store.vessel(mmsi)
        if not vessel:
            expected = portcalls.expected_for_mmsi(mmsi)
            if expected:
                lines = [
                    expected.get("name") or mmsi,
                    f"MMSI: {mmsi}",
                    f"Флаг: {flags.flag_text(mmsi)}",
                    f"Тип: {expected.get('type') or '—'}",
                    f"Назначение: {expected.get('port') or '—'} (наш порт)",
                    f"Заявленный ETA: {expected.get('eta') or '—'} (UTC, передал капитан)",
                    f"Последний сигнал: {expected.get('last_heard') or '—'}",
                    "",
                    "Позиции нет: судно в зоне, которую живые потоки пока не "
                    "покрывают. Список ведёт AIS-catcher (открытая сеть приёмников).",
                    "",
                    "Проверка в других сервисах:",
                ]
                for title, url in portcalls.registry_links(None, mmsi):
                    lines.append(f"  • {title}: {url}")
                lines.append("")
                lines.append("Карты судов (открываются в браузере):")
                for title, url in portcalls.tracking_links(mmsi, expected.get("name")):
                    lines.append(f"  • {title}: {url}")
                return "\n".join(lines)
            return "Судно не найдено — возможно, оно ушло из района."
        country, iso2, emoji = flags.flag_for_mmsi(mmsi)
        lines = [
            vessel.get("name") or mmsi,
            f"MMSI: {mmsi}",
            f"Флаг: {flags.flag_text(mmsi)}" + (f" ({iso2})" if iso2 else ""),
        ]
        if vessel.get("imo"):
            lines.append(f"IMO: {vessel['imo']}")
        lines += [
            f"Тип: {ais_feed.type_detail(vessel.get('type_code'))} "
            f"(код AIS {vessel.get('type_code')}, {vessel.get('type_label') or '—'})",
            f"Назначение: {vessel.get('destination') or '—'}",
            f"Скорость: {fmt_number(vessel.get('sog'))} уз",
            f"Размеры: {self.size_text(vessel)} (длина × ширина)",
            f"Курс: {fmt_number(vessel.get('cog'))}°",
            f"Осадка: {fmt_number(vessel.get('draft'))} м",
            f"В порту: {vessel.get('port') or '—'}",
            f"Координаты: {fmt_number(vessel.get('lat'))}, {fmt_number(vessel.get('lon'))}",
            f"Обновлено: {self.fmt_time(vessel.get('updated_at'))}"
            f" ({metrics.stale_note(vessel, self.core.stale_hours())})",
        ]
        age = metrics.freshness_hours(vessel.get("updated_at"))
        if age is not None and age > self.core.stale_hours():
            lines.append(f"⚠ Данные устарели: AIS не поступал {span_text(age)}. "
                         f"Скорость, курс и позиция могут быть неактуальны — "
                         f"расчёты ниже сделаны по последним известным данным и для "
                         f"решения о движении сейчас не годятся.")
        if vessel.get("lat") is None or vessel.get("lon") is None:
            lines.append("Позиция: неизвестна — на карту не нанесено; судно есть в списках "
                         f"и в боте (порт назначения: {vessel.get('destination') or '—'})")
        declared = parse_iso(vessel.get("eta_declared"))
        voyage = self.core.voyage(vessel)
        if voyage:
            port_name = voyage.get("port_label") or voyage.get("port")
            if voyage.get("in_port"):
                lines.append(f"До прибытия: уже в порту {port_name}")
            elif voyage.get("hours") is not None:
                lines.append(f"До прибытия: {duration_text(voyage['hours'])} "
                             f"({fmt_moment(voyage['eta'])}), "
                             f"{fmt_number(voyage['distance_km'])} км до {port_name}")
            else:
                lines.append(f"До прибытия: скорость слишком мала для расчёта "
                             f"({fmt_number(voyage['distance_km'])} км до {port_name})")
        lines.append(f"Срок прибытия (AIS): {fmt_moment(declared) if declared else '—'} "
                      f"(передаёт капитан, в UTC)")
        lines += self.forecast_lines(vessel)
        expected = portcalls.expected_for_mmsi(mmsi)
        if expected:
            lines.append(f"Ожидается в {expected.get('port')}: заявленный ETA "
                         f"{expected.get('eta') or '—'} (AIS-catcher, последний сигнал "
                         f"{expected.get('last_heard') or '—'})")
        gfw_entry = portcalls.gfw_vessel(None, mmsi)
        if gfw_entry:
            lines.append(portcalls.gfw_profile_text(gfw_entry))
        gfw_text = portcalls.gfw_gap_text(gfw_entry)
        if gfw_text:
            lines.append(gfw_text)
        stay = self.core.port_stay(vessel)
        if stay:
            stay_port = stay.get("port_label") or stay.get("port")
            parts = []
            if stay.get("current_hours") is not None:
                parts.append(f"стоит {span_text(stay['current_hours'])}")
            if stay.get("typical_hours"):
                source = ("по истории судна" if stay.get("scope") == "судно"
                          else "по истории порта")
                parts.append(f"обычно {span_text(stay['typical_hours'])} "
                             f"({source}, визитов: {stay['visits']})")
            if stay.get("remaining_hours") is not None:
                parts.append(f"осталось примерно {span_text(stay['remaining_hours'])}")
            lines.append(f"Стоянка в {stay_port}: "
                         + ("; ".join(parts) if parts else "наблюдений пока нет"))

        cargo_info = self.core.cargo_estimate(vessel)
        if cargo_info:
            share = cargo_info.get("load_share")
            if share is None:
                lines.append(f"Груз (оценка): ≈ {fmt_tons(cargo_info['cargo_t'])} т; "
                             f"доля дедвейта неизвестна — полную осадку судна "
                             f"программа ещё не видела")
            else:
                lines.append(f"Груз (оценка): ≈ {fmt_tons(cargo_info['cargo_t'])} т "
                             f"({round(share * 100)} % дедвейта ≈ "
                             f"{fmt_tons(cargo_info['deadweight_t'])} т)")
            lines.append(f"  расчёт: {fmt_number(cargo_info['length_m'])} × "
                         f"{fmt_number(cargo_info['beam_m'])} м, Cb "
                         f"{cargo_info['block_coefficient']:.2f}, осадка "
                         f"{fmt_number(cargo_info['draft_m'])} из "
                         f"{fmt_number(cargo_info['full_draft_m'])} м "
                         f"({cargo_info['full_draft_source']}), "
                         f"порожнее ≈ {fmt_tons(cargo_info['lightship_t'])} т")
            if cargo_info.get("in_ballast"):
                lines.append("  похоже, судно идёт в балласте: груза мало, "
                             "остальное — балласт и запасы")
        elif vessel.get("draft"):
            lines.append("Груз (оценка): нет длины и ширины корпуса — "
                         "судно их ещё не передавало")

        kind, kind_detail = cargo.cargo_kind(vessel.get("type_code"))
        if kind:
            lines.append(f"Что везёт: {kind} — {kind_detail}")
            lines.append("  (название груза AIS не передаёт: подпись по типу судна)")
        signals = self.core.vessel_signals(vessel)
        if signals:
            lines.append("Признаки (по наблюдениям программы):")
            lines += [f"  • {signal}" for signal in signals]
        math_lines = self.core.vessel_math(vessel)
        if math_lines:
            lines.append("Расчёты по данным наблюдений:")
            lines += [f"  • {line}" for line in math_lines]
        target = self._targets.get(mmsi)
        if target and target.get("eta_calc"):
            lines.append(f"Прибытие (расчёт): {fmt_moment(target['eta_calc'])} "
                         f"{duration_text(target.get('eta_hours'))}, "
                         f"{fmt_number(target.get('distance_km'))} км")
        lines.append("")
        # Ссылки на другие сервисы для проверки реальности судна
        links = []
        imo_l = vessel.get('imo') or mmsi
        links.append(f"• VesselFinder: https://www.vesselfinder.com/vessels/details/{imo_l}")
        links.append(f"• MarineTraffic: https://www.marinetraffic.com/en/ais/details/ships/mmsi:{mmsi}")
        links.append(f"• MyShipTracking: https://www.myshiptracking.com/vessels?mmsi={mmsi}")
        links.append(f"• AIS Stream: https://aisstream.io/v0/stream?mmsi={mmsi}")
        links.append(f"• AIS Hub: https://www.aishub.net/vessels/mmsi:{mmsi}")
        links.append(f"• Digitraffic: https://www.digitraffic.fi/en/marine-traffic/?mmsi={mmsi}")
        links.append(f"• OpenCpn: https://www.opencpn.org/")
        for title, url in portcalls.registry_links(vessel.get("imo"), mmsi):
            links.append(f"• {title}: {url}")
        lines.append("Проверка в других сервисах:")
        for l in links:
            lines.append(f"  {l}")
        lines.append("")
        lines.append("Карты судов (открываются в браузере):")
        for title, url in portcalls.tracking_links(mmsi, vessel.get("name")):
            lines.append(f"  • {title}: {url}")
        lines.append("")
        if target:
            lines += [
                "СТАТУС: ЦЕЛЬ",
                f"Маршрут: {target['origin']} → {target['destination_port']}",
                f"Вышел из порта: {self.fmt_time(target.get('departed_at'))}",
                "",
            ]
        else:
            lines += ["СТАТУС: не цель", self.explain(vessel), ""]
        departures = self.store.departures(mmsi, 5)
        if departures:
            lines.append("Визиты в порты (время стоянки):")
            for visit in departures:
                entered = parse_iso(visit.get("entered_at"))
                left_at = parse_iso(visit.get("left_at"))
                left = self.fmt_time(visit.get("left_at")) if left_at else "на месте"
                span = ""
                if entered:
                    end = left_at or datetime.now()
                    span = f", {span_text((end - entered).total_seconds() / 3600)}"
                if left_at is None:
                    span += " (идёт)"
                visit_cargo = visit.get("cargo_kind") or ""
                drafts = ""
                if visit.get("draft_in") is not None or visit.get("draft_out") is not None:
                    drafts = (f" · осадка {fmt_number(visit.get('draft_in'))} → "
                              f"{fmt_number(visit.get('draft_out'))} м")
                lines.append(f"  {visit['port']}: {self.fmt_time(visit.get('entered_at'))} → {left}{span}"
                             + (f" · {visit_cargo}" if visit_cargo else "") + drafts)
        return "\n".join(lines)

    def forecast_lines(self, vessel):
        """Блок карточки: прогноз пути на 3 суток вперёд.

        Если в Destination наш порт — линия ведёт в него и показывает время
        прибытия. Если нет — прогноз идёт по текущему курсу и скорости:
        где судно окажется через сутки, двое и трое.
        """
        ports = self.config.get("ports") or []
        port = forecast.target_port(vessel, ports)
        if vessel.get("lat") is None or vessel.get("lon") is None:
            return []
        if ports and geo.port_at(vessel.get("lat"), vessel.get("lon"), ports):
            return []
        track = None
        if getattr(self, "radar", None) is not None and vessel.get("mmsi") == self.selected:
            track = self.radar.track
        speed, course, source = forecast.estimates(vessel, track)
        try:
            points = forecast.route(vessel, ports, track=track)
        except Exception:
            points = []
        if len(points) < 2:
            if speed is None or not (forecast.MIN_SPEED_KNOTS <= speed <= 40):
                return ["Прогноз пути: не строится — AIS не передал скорость"]
            return [f"Прогноз пути: не строится — нет курса (скорость "
                    f"{fmt_number(speed)} уз, но судно не передало COG/heading)"]
        name = (port or {}).get("name")
        if port:
            head = (f"Прогноз пути до {name} (до {int(forecast.MAX_DAYS)} сут, "
                    f"скорость {fmt_number(speed)} уз — {source}):")
        else:
            head = (f"Прогноз пути по курсу на {int(forecast.MAX_DAYS)} сут "
                    f"({forecast.course_text(course)}, скорость {fmt_number(speed)} уз — "
                    f"{source}):")
        lines = [head]
        daily = [point for point in forecast.daily_points(points) if point["hours"] > 0]
        for point in daily:
            index = next((i for i, item in enumerate(points) if item is point), None)
            passed = forecast.distance_text(points, index) if index is not None else None
            where = f"{point['lat']:.2f}, {point['lon']:.2f}"
            if port:
                km = geo.haversine_km(point["lat"], point["lon"],
                                      float(port["lat"]), float(port["lon"]))
                tail = f"{fmt_number(round(km))} км до {name}"
            else:
                tail = (f"пройдёт {fmt_number(round(passed))} км"
                        if passed is not None else "")
            lines.append(f"  {point['label']:<8} {point['at']:%d.%m %H:%M} · {where}"
                         + (f" · {tail}" if tail else ""))
        if port:
            lines.append("  " + forecast.summary(points, name))
        else:
            lines.append("  конечная точка — по текущему курсу без учёта поворотов; "
                         "если судно сменит курс, линия сдвинется")
        return lines

    def explain(self, vessel) -> str:
        if not ais_feed.is_tanker_code(vessel.get("type_code")):
            return "не танкер: нужен AIS Type 80–89."
        destination = vessel.get("destination") or ""
        if not port_matches(destination, self.config.get("filter_destinations")):
            return (f"назначение «{destination or '—'}» не входит в "
                    f"{', '.join(self.config.get('filter_destinations') or [])}.")
        departure = self.store.last_departure(vessel["mmsi"],
                                             self.config.get("trip_window_hours") or 96)
        if not departure:
            return ("танкер идёт куда нужно, но откуда он вышел — пока неизвестно: "
                    "нужно, чтобы судно прошло через геозону порта при работающей программе.")
        if not port_matches(departure["port"], self.config.get("filter_origins")):
            return (f"вышел из {departure['port']}, а нужны "
                    f"{', '.join(self.config.get('filter_origins') or [])}.")
        if (vessel.get("sog") or 0) < float(self.config.get("min_speed_knots") or 0):
            return "скорость ниже порога: судно стоит."
        if vessel.get("port") and port_matches(vessel["port"],
                                               self.config.get("filter_destinations")):
            return "уже в порту назначения."
        return "условия выполняются, но алерт уже был или выключен."

    def fmt_time(self, value) -> str:
        moment = parse_iso(value)
        return moment.strftime("%d.%m.%Y %H:%M") if moment else "—"

    @staticmethod
    def short_error(error) -> str:
        """Короткая причина для шапки окна: подробности остаются в журнале."""
        text = str(error or "")
        if "429" in text:
            return "· лимит подключений (429), ждём"
        if "401" in text or "403" in text:
            return "· ключ не принят"
        if "ключ" in text.lower():
            return "· нужен ключ AISStream"
        if not text:
            return ""
        return "· " + text.split(".")[0][:60]

    def eta_text(self, target) -> str:
        """Компактное прибытие для таблицы: расчёт + заявленный срок из AIS."""
        parts = []
        if target.get("eta_calc"):
            parts.append(f"{target['eta_calc']:%d.%m %H:%M} "
                         f"({duration_text(target.get('eta_hours'))})")
        declared = parse_iso(target.get("eta_declared"))
        if declared:
            parts.append(f"AIS: {declared:%d.%m %H:%M}")
        return " · ".join(parts) if parts else "—"

    # -- периодические обновления
    def fast_tick(self):
        try:
            self._fast_tick()
        except Exception as error:
            self.log(f"Обновление окна: {error}")
        finally:
            try:
                self.root.after(700, self.fast_tick)
            except Exception:
                pass

    def _fast_tick(self):
        state = self.core.feed_state()
        if getattr(self.core, "feed_elsewhere", False) and not self.core.running():
            conn = "поток: принимает другой процесс · данные из общей базы"
        elif not state["running"]:
            conn = "поток остановлен"
        elif state["connected"]:
            conn = "поток: на связи"
        else:
            conn = f"поток: переподключение {self.short_error(state.get('error'))}".strip()
        conn += " · живой AIS: реальные суда"
        self.conn_var.set(conn)
        self.update_feed_status()
        self.stats_var.set(
            f"сообщений AIS: {self.core.stats['events']} · точек записано: "
            f"{self.core.stats['positions']} · алертов: {self.core.stats['alerts']}")
        self.redraw_map()

    def update_feed_status(self):
        """Показывает состояние каждого канала на вкладке «Подключение»."""
        labels = getattr(self, "feed_status", None)
        if not labels:
            return
        states = {}
        try:
            for state in self.core.feeds_state():
                name = state.get("name") or ""
                if name.startswith("AISStream"):
                    states["ais"] = state
                elif name.startswith("Open Waters"):
                    states["ow"] = state
                elif name.startswith("Снимок"):
                    states["snap"] = state
        except Exception:
            states = {}
        running = self.core.running()
        elsewhere = bool(getattr(self.core, "feed_elsewhere", False))
        service_state = getattr(self.core, "service_status", {}) or {}
        for key, var in labels.items():
            if key not in ("ais", "ow", "snap"):
                # внешние API: берём актуальный статус из ядра (в том числе
                # если сигнал пришёл раньше, чем окно успело его показать)
                text = service_state.get(key)
                if not text:
                    text = self.service_status_text(key)
                if var.get() != text:
                    var.set(text)
                continue
            state = states.get(key)
            if not running:
                text = "принимает другой процесс (общая база)" if elsewhere else "не запущен"
            elif not state:
                text = "отключён в настройках"
            elif state.get("connected"):
                text = f"на связи, сообщений {state.get('events', 0)}"
            elif state.get("error"):
                text = f"переподключение: {self.short_error(state.get('error'))}".strip()
            else:
                text = "переподключение"
            if var.get() != text:
                var.set(text)

    def slow_tick(self):
        try:
            self._slow_tick()
        except Exception as error:
            self.log(f"Обновление таблиц: {error}")
        finally:
            try:
                self.root.after(3000, self.slow_tick)
            except Exception:
                pass

    def _slow_tick(self):
        try:
            self._targets = {target["mmsi"]: target for target in self.core.targets()}
        except Exception as error:
            self.log(f"Расчёт целей: {error}")
            self._targets = {}
        if hasattr(self, "targets_tree"):
            self.refresh_targets_table()
        # «Все суда» — самая тяжёлая таблица (тысячи строк): не пересобираем её
        # каждые три секунды, иначе окно постоянно занято и карта тормозит.
        # Заход на вкладку и кнопка «Обновить» пересобирают её сразу.
        now = time.monotonic()
        if now - getattr(self, "_vessels_drawn_at", 0.0) >= 8.0:
            self.refresh_vessels_table()
            self._vessels_drawn_at = now
        self.refresh_sea_table()
        if now - getattr(self, "_forecast_drawn_at", 0.0) >= 8.0:
            self.refresh_forecast()
            self._forecast_drawn_at = now
        self.refresh_visits()
        if hasattr(self, "visit_history_tree"):
            self.refresh_visit_history()
        self.refresh_alerts()
        self.update_detail()
        self.update_track()
        self.redraw_map()

    def on_tab_changed(self, event=None):
        """При переключении вкладки сразу читаем данные, а не ждём такта."""
        try:
            tab = self.notebook.nametowidget(self.notebook.select())
        except Exception:
            return
        self.refresh_tab(tab)

    def refresh_tab(self, tab=None):
        """Обновляет данные выбранной вкладки — при заходе и по кнопке."""
        if tab is None:
            try:
                tab = self.notebook.nametowidget(self.notebook.select())
            except Exception:
                return
        try:
            if tab is getattr(self, "tab_radar", None):
                self.redraw_map()
                if hasattr(self, "targets_tree"):
                    self.refresh_targets_table()
                self.update_detail(force=True)
            elif tab is getattr(self, "tab_visit_history", None):
                self.refresh_visit_history()
            elif tab is getattr(self, "tab_visits", None):
                self.refresh_visits()
            elif tab is getattr(self, "tab_expected", None):
                self.refresh_expected()
            elif tab is getattr(self, "tab_vessels", None):
                self.refresh_vessels_table()
            elif tab is getattr(self, "tab_sea", None):
                self.refresh_sea_table()
            elif tab is getattr(self, "tab_forecast", None):
                self.refresh_forecast()
            elif tab is getattr(self, "tab_alerts", None):
                self.refresh_alerts()
            elif tab is getattr(self, "tab_connection", None):
                self.update_feed_status()
                self.update_key_info()
            elif tab is getattr(self, "tab_filters", None):
                self.reload_filter_fields()
            elif tab is getattr(self, "tab_telegram", None):
                self.reload_telegram_fields()
            elif tab is getattr(self, "tab_services", None):
                self.update_feed_status()
            elif tab is getattr(self, "tab_log", None):
                self.log_text.see("end")
        except Exception as error:
            self.log(f"Обновление вкладки: {error}")

    def reload_filter_fields(self):
        """Перечитывает сохранённые настройки фильтров в поля вкладки."""
        try:
            config = self.core.config
        except Exception:
            return
        pairs = (("var_dest", ", ".join(config.get("filter_destinations") or [])),
                 ("var_orig", ", ".join(config.get("filter_origins") or [])),
                 ("var_window", config.get("trip_window_hours", 96)),
                 ("var_speed", config.get("min_speed_knots", 2)),
                 ("var_cooldown", config.get("alert_cooldown_hours", 6)),
                 ("var_history", config.get("history_days", 7)),
                 ("var_interval", config.get("position_interval_seconds", 60)),
                 ("var_stale_minutes", config.get("stale_minutes", 15)),
                 ("var_watch_km", config.get("port_watch_km", 50)),
                 ("var_area_km", config.get("area_radius_km", 900)))
        for name, value in pairs:
            var = getattr(self, name, None)
            if var is not None:
                var.set(str(value).replace(".0", "") if isinstance(value, float) else str(value))

    def reload_telegram_fields(self):
        """Перечитывает токен и chat_id из сохранённых настроек."""
        try:
            config = self.core.config
        except Exception:
            return
        token = getattr(self, "var_token", None)
        chat = getattr(self, "var_chat", None)
        commands = getattr(self, "var_commands", None)
        if token is not None:
            token.set(str(config.get("telegram_token") or ""))
        if chat is not None:
            chat.set(str(config.get("telegram_chat_id") or ""))
        if commands is not None:
            commands.set(bool(config.get("telegram_commands", True)))

    def refresh_targets_table(self):
        tree = self.targets_tree
        for item in tree.get_children():
            tree.delete(item)
        for target in self._targets.values():
            tree.insert("", "end", tags=(target["mmsi"],), values=(
                target.get("name") or "—",
                flags.flag_text(target["mmsi"]),
                target.get("imo") or "—",
                target.get("origin") or "—",
                self.fmt_time(target.get("departed_at")),
                target.get("destination") or "—",
                fmt_number(target.get("sog")),
                fmt_number(target.get("draft")),
                self.eta_text(target),
                self.fmt_time(target.get("updated_at")),
            ))
        self.targets_info.set(
            f"Целей сейчас: {len(self._targets)}. Цель = танкер, который идёт в порт из "
            f"списка «назначение» и вышел из порта из списка «отправление» (вкладка "
            f"«Настройки»). Прибытие — расчёт по расстоянию и скорости "
            f"(курс по прямой), в скобках — срок из AIS, если судно его передало.")

    def refresh_vessels_table(self):
        tree = self.vessels_tree
        for item in tree.get_children():
            tree.delete(item)
        rows = portcalls.expected_rows()
        updated = (rows[0].get("updated") or "")[:16].replace("T", " ") if rows else ""
        expected_map = {str(row.get("mmsi") or ""): row for row in rows}
        seen = set()
        try:
            vessels = self.core.area_vessels(2000)
        except Exception as error:
            self.log(f"Список «Все суда»: {error}")
            vessels = []
        radius = self.core.area_radius_km()
        for vessel in vessels:
            mmsi = str(vessel.get("mmsi"))
            seen.add(mmsi)
            info = expected_map.get(mmsi)
            # судно из AIS-catcher: координат у него нет, вместо расчёта — заявленный ETA
            no_position = info and (vessel.get("lat") is None or vessel.get("lon") is None)
            tags = (vessel["mmsi"],)
            age = metrics.freshness_hours(vessel.get("updated_at"))
            if age is not None and age > self.core.stale_hours():
                tags = (vessel["mmsi"], "stale")
            tree.insert("", "end", tags=tags, values=(
                vessel.get("name") or "—",
                flags.flag_text(vessel["mmsi"]),
                vessel.get("type_label") or "—",
                "нет позиции" if no_position else self.size_text(vessel),
                vessel.get("destination") or "—",
                (f"ETA {info.get('eta') or '—'}" if no_position
                 else self.voyage_text(vessel)),
                "—" if no_position else self.stay_text(vessel),
                self.cargo_text(vessel),
                self.cargo_kind_text(vessel),
                fmt_number(vessel.get("sog")),
                "—" if no_position else (metrics.row_note(
                    vessel, self.core.stale_hours()) or "—"),
                "ожидается" if no_position else self.port_text(vessel),
                (f"сигнал {info.get('last_heard') or '—'} · AIS-catcher {updated}"
                 if no_position else self.fmt_time(vessel.get("updated_at"))),
                vessel.get("mmsi"),
            ))
        # Если база ещё не успела принять список — показываем строки напрямую.
        for row in rows:
            mmsi = str(row.get("mmsi") or "")
            if not mmsi or mmsi in seen:
                continue
            seen.add(mmsi)
            tree.insert("", "end", tags=(mmsi,), values=(
                row.get("name") or "—",
                flags.flag_text(mmsi),
                row.get("type") or "—",
                "нет позиции",
                row.get("port") or "—",
                f"ETA {row.get('eta') or '—'}",
                "—",
                "—",
                "—",
                "ожидается",
                f"сигнал {row.get('last_heard') or '—'} · AIS-catcher {updated}",
                mmsi,
            ))
        try:
            near = sum(1 for vessel in vessels if vessel.get("lat") is not None)
        except Exception:
            near = 0
        if hasattr(self, "vessels_info"):
            self.vessels_info.set(
                f"Судов в базе района: {len(vessels)} (с позицией {near}), "
                f"ожидаемых из AIS-catcher: {len(rows)}. Радиус района: {radius:.0f} км "
                f"от каждого порта — меняется во вкладке «Фильтры». Суда без связи с "
                f"портами дольше двух суток убираются из базы и с карты. "
                f"Обновляется само; двойной щелчок открывает карточку.")

    def refresh_sea_table(self):
        """Обновляет вкладку «В море»: только свежие позиции вне портов."""
        if not hasattr(self, "sea_tree"):
            return
        tree = self.sea_tree
        for item in tree.get_children():
            tree.delete(item)
        try:
            rows = self.core.sea_vessels(3000)
        except Exception as error:
            self.log(f"Список «В море»: {error}")
            rows = []
        try:
            visit_ports = self.store.visit_ports()
        except Exception:
            visit_ports = {}
        linked = []
        for vessel in rows:
            try:
                vessel["links"] = self.core.vessel_links(vessel, visit_ports)
            except Exception:
                vessel["links"] = {}
            ours = self.core.destination_is_ours(vessel.get("destination"))
            was = self.core.was_in_our_port(vessel.get("mmsi"))
            vessel["dest_ours"] = ours
            vessel["was_in_port"] = was
            if ours or was:
                linked.append(vessel)
        hidden = len(rows) - len(linked)
        counts = {}
        for vessel in linked:
            for name in vessel.get("links") or {}:
                counts[name] = counts.get(name, 0) + 1
        chosen = self.sea_port.get() if getattr(self, "sea_port", None) is not None else ""
        chosen = (chosen or "").upper()
        for name, button in getattr(self, "sea_port_buttons", {}).items():
            label = next((p.get("label") or p.get("name")
                          for p in self.config.get("ports") or []
                          if (p.get("name") or "").upper() == name), "Все порты")
            if name:
                button.configure(text=f"{label} ({counts.get(name, 0)})")
            else:
                button.configure(text=f"Все порты ({len(linked)})")
        # во вкладке только суда, связанные с нашими портами: чужой трафик
        # района здесь больше не показывается (он остаётся слоем на карте)
        rows = linked
        if chosen:
            rows = [vessel for vessel in rows if chosen in (vessel.get("links") or {})]
        moved = 0
        for vessel in rows:
            try:
                voyage = self.core.voyage(vessel) or {}
            except Exception:
                voyage = {}
            if vessel.get("underway"):
                moved += 1
            distance = "—"
            if voyage.get("distance_km") is not None:
                distance = (f"{fmt_number(round(voyage['distance_km']))} км → "
                            f"{voyage.get('port') or '—'}")
            elif voyage.get("port"):
                distance = f"→ {voyage.get('port')}"
            note = ""
            if vessel.get("dest_ours"):
                note = " · наш порт"
            elif vessel.get("was_in_port"):
                note = " · вышло из нашего порта"
            tree.insert("", "end", tags=(vessel["mmsi"],), values=(
                vessel.get("name") or "—",
                flags.flag_text(vessel.get("mmsi")),
                vessel.get("type_label") or "—",
                (vessel.get("destination") or "—") + note,
                fmt_number(vessel.get("sog")),
                self.course_text(vessel),
                distance,
                self.arrival_text(voyage),
                self.fmt_time(vessel.get("updated_at")),
                vessel.get("mmsi"),
            ))
        try:
            silent = self.core.sea_without_position(3000)
        except Exception:
            silent = []
        far = sum(1 for vessel in rows
                  if not any((link or {}).get("kind") == "рядом"
                             for link in (vessel.get("links") or {}).values()))
        top = f"по порту {chosen}" if chosen else "по всем нашим портам"
        self.sea_info.set(
            f"В море и связаны с нашими портами {top}: {len(rows)} "
            f"(под ходом {moved}, на рейде/якоре {len(rows) - moved}). "
            f"Из них далеко от порта (только по Destination или визиту): {far}. "
            f"Не показываются: {hidden} чужих судов района."
            f" Без сигнала AIS: {len(silent)} — они остаются во вкладке «Все суда». "
            f"Обновляется само; двойной щелчок открывает карточку.")

    def course_text(self, vessel):
        """Курс судна в градусах и по стороне света."""
        value = vessel.get("heading")
        if value in (None, 0, 511):
            value = vessel.get("cog")
        try:
            degrees = float(value)
        except (TypeError, ValueError):
            return "—"
        if degrees <= 0 or degrees >= 360:
            return "—"
        sides = ("С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ")
        return f"{round(degrees)}° {sides[int((degrees % 360) / 45) % 8]}"

    def arrival_text(self, voyage):
        """Когда судно будет в порту: сколько осталось и время прибытия."""
        if not voyage:
            return "—"
        if voyage.get("in_port"):
            return "в порту"
        eta = voyage.get("eta")
        hours = voyage.get("hours")
        if eta is None or hours is None:
            return "нет расчёта (не под ходом)"
        return f"{duration_text(hours)} · {eta:%d.%m %H:%M}"

    def size_text(self, vessel):
        """Габариты корпуса из AIS: длина × ширина в метрах."""
        length = vessel.get("length_m")
        beam = vessel.get("beam_m")
        if length and beam:
            return f"{fmt_number(length)} × {fmt_number(beam)} м"
        if length:
            return f"{fmt_number(length)} м (ширина не передана)"
        if beam:
            return f"ширина {fmt_number(beam)} м (длина не передана)"
        return "судно ещё не передавало"

    def voyage_text(self, vessel):
        """Сколько осталось идти до порта назначения (по прямой и текущей скорости)."""
        try:
            voyage = self.core.voyage(vessel)
        except Exception:
            return "—"
        if not voyage:
            return "—"
        if voyage.get("in_port"):
            return "уже в порту"
        if voyage.get("hours") is None:
            return "нет расчёта"
        return f"{duration_text(voyage['hours'])} → {voyage['port']}"

    def stay_text(self, vessel):
        """Сколько судно стоит (и пробудет) в порту назначения."""
        try:
            stay = self.core.port_stay(vessel)
        except Exception:
            return "—"
        if not stay:
            return "—"
        current = stay.get("current_hours")
        typical = stay.get("typical_hours")
        if current is not None and typical:
            return f"{span_text(current)} из ≈{span_text(typical)}"
        if current is not None:
            return f"стоит {span_text(current)}"
        if typical:
            return f"обычно ≈{span_text(typical)}"
        return "нет наблюдений"

    def cargo_text(self, vessel):
        """Короткая оценка груза для таблицы «Все суда»."""
        try:
            info = self.core.cargo_estimate(vessel)
        except Exception:
            return "—"
        if not info:
            return "—"
        text = f"≈{fmt_tons(info['cargo_t'])} т"
        share = info.get("load_share")
        if share is not None:
            text += f" ({round(share * 100)} %)"
        if info.get("in_ballast"):
            text += " балласт"
        return text

    def port_text(self, vessel):
        """Порт и место стоянки: у причала, на рейде или просто в порту."""
        place = None
        try:
            place = self.core.port_place(vessel)
        except Exception:
            place = None
        if place:
            short = place.get("place") or ""
            short = short.replace("рейд: ждёт лоцмана", "рейд").replace("у причала", "причал")
            return f"{place.get('port_name')} · {short}"
        return vessel.get("port") or "—"

    def cargo_kind_text(self, vessel):
        """Что за груз везёт судно — по типу AIS и по имени судна (поиск по ключевым словам)."""
        # 1) По коду типа AIS (надежный источник: танкер 80 = нефть/нефтепродукты)
        kind, _ = cargo.cargo_kind((vessel or {}).get("type_code"))
        # 2) Доп. подсказка по имени судна (AIS не передаёт груз, но имя часто содержит подсказку)
        name_hint = ""
        name = (vessel or {}).get("name") or ""
        if name and kind:
            lower = name.lower()
            # Простые ключевые слова в имени судна
            keywords = [
                ("oil", "нефть"), ("chem", "химия"), ("carrier", "танкер"),
                ("gas", "газ"), ("bulk", "балк"), ("product", "продукт"),
            ]
            for en, ru in keywords:
                if en in lower:
                    name_hint = f" (по имени: {ru})"
                    break
        # Если тип АIS известен, используем его, иначе — подсказка по имени
        if kind:
            return kind + name_hint
        if name_hint:
            return name_hint.strip().lstrip('(').rstrip(')')
        return "—"

    def refresh_alerts(self):
        tree = self.alerts_tree
        for item in tree.get_children():
            tree.delete(item)
        for alert in self.store.alerts(200):
            tree.insert("", "end", values=(
                self.fmt_time(alert.get("sent_at")),
                alert.get("name") or "—",
                f"{alert.get('origin') or '—'} → {alert.get('destination') or '—'}",
                "отправлен" if alert.get("ok") else
                ("в окне" if (alert.get("error") or "").startswith("только в окне") else "ошибка"),
                alert.get("error") or "",
            ))

    # -- настройки
    def collect_config(self) -> dict:
        try:
            boxes = parse_bboxes(self.var_bboxes.get())
            ports = parse_ports(self.ports_text.get("1.0", "end"))
        except AppError as error:
            self.messagebox.showwarning("Проверьте настройки", str(error))
            raise
        try:
            window = float(str(self.var_window.get()).replace(",", "."))
            speed = float(str(self.var_speed.get()).replace(",", "."))
            cooldown = float(str(self.var_cooldown.get()).replace(",", "."))
            history = float(str(self.var_history.get()).replace(",", "."))
            interval = float(str(self.var_interval.get()).replace(",", "."))
            stale = float(str(self.var_stale_minutes.get()).replace(",", "."))
            watch_km = float(str(getattr(self, "var_watch_km", None).get() or 50).replace(",", "."))
            area_km = float(str(getattr(self, "var_area_km", None).get() or 900).replace(",", "."))
            snapshot_minutes = float(str(getattr(self, "var_snapshot_minutes", None).get() or 10)
                                     .replace(",", "."))
        except ValueError:
            self.messagebox.showwarning("Проверьте настройки",
                                        "Числовые поля должны быть числами.")
            raise AppError("числовые поля")

        # Приоритет — чекбоксы, если они есть
        dests = []
        origs = []
        if getattr(self, "port_dest_vars", None):
            for pname, var in self.port_dest_vars.items():
                try:
                    if var.get():
                        dests.append(pname)
                except Exception:
                    pass
        else:
            dests = split_list(self.var_dest.get())
        if getattr(self, "port_orig_vars", None):
            for pname, var in self.port_orig_vars.items():
                try:
                    if var.get():
                        origs.append(pname)
                except Exception:
                    pass
        else:
            origs = split_list(self.var_orig.get())

        # Store service checkbox states
        self.config["use_vf"] = bool(getattr(self, "var_use_vf", __import__("tkinter").BooleanVar(value=False)).get())
        self.config["use_vfa"] = bool(getattr(self, "var_use_vfa", __import__("tkinter").BooleanVar(value=False)).get())
        self.config["use_mt"] = bool(getattr(self, "var_use_mt", __import__("tkinter").BooleanVar(value=False)).get())
        self.config["use_mst"] = bool(getattr(self, "var_use_mst", __import__("tkinter").BooleanVar(value=False)).get())
        self.config["use_dt"] = bool(getattr(self, "var_use_dt", __import__("tkinter").BooleanVar(value=False)).get())
        self.config["use_gor"] = bool(getattr(self, "var_use_gor", __import__("tkinter").BooleanVar(value=True)).get())
        self.config["use_mlive"] = bool(getattr(self, "var_use_mlive", __import__("tkinter").BooleanVar(value=True)).get())
        self.config["use_srd"] = bool(getattr(self, "var_use_srd", __import__("tkinter").BooleanVar(value=True)).get())
        self.config["use_aci"] = bool(getattr(self, "var_use_aci", __import__("tkinter").BooleanVar(value=True)).get())
        self.config["use_gfw"] = bool(getattr(self, "var_use_gfw", __import__("tkinter").BooleanVar(value=False)).get())
        self.config["use_dat"] = bool(getattr(self, "var_use_dat", __import__("tkinter").BooleanVar(value=False)).get())
        self.config["use_aishub"] = bool(getattr(self, "var_use_aishub", __import__("tkinter").BooleanVar(value=False)).get())
        # Ключи API внешних сервисов
        for key, field in SERVICE_KEY_FIELDS.items():
            var = getattr(self, field, None)
            if var is not None:
                value = var.get().strip()
                if key == "gfw":
                    # вставка поверх старого токена склеивает две строки:
                    # собираем JWT заново, иначе сервер ответит «invalid token»
                    value = portcalls.clean_gfw_token(value)
                self.config[f"{key}_key"] = value

        self.config.update({
            "api_key": self.var_key.get().strip(),
            "openwaters_token": getattr(self, "var_ow_token", None).get().strip() if getattr(self, "var_ow_token", None) else self.config.get("openwaters_token", ""),
            "use_openwaters": bool(getattr(self, "var_use_ow", None).get()) if getattr(self, "var_use_ow", None) else bool(self.config.get("use_openwaters", True)),
            "use_snapshot": bool(getattr(self, "var_use_snapshot", None).get()) if getattr(self, "var_use_snapshot", None) else bool(self.config.get("use_snapshot", True)),
            "snapshot_minutes": max(1.0, snapshot_minutes),
            "bounding_boxes": boxes,
            "ports": ports,
            "filter_destinations": dests,
            "filter_origins": origs,
            "trip_window_hours": window,
            "min_speed_knots": speed,
            "alert_cooldown_hours": cooldown,
            "history_days": history,
            "position_interval_seconds": interval,
            "stale_minutes": max(0.0, stale),
            "port_watch_km": max(0.0, watch_km),
            "area_radius_km": max(50.0, min(1500.0, area_km)),
            "telegram_token": self.var_token.get().strip(),
            "telegram_chat_id": self.var_chat.get().strip(),
            "telegram_commands": bool(self.var_commands.get()),
        })
        try:
            save_config(self.config, self.config_path)
        except OSError as error:
            self.messagebox.showerror("Ошибка", f"Не удалось сохранить настройки: {error}")
            raise AppError(str(error))
        return self.config

    # ----------------------------------------------- статусы внешних сервисов

    def service_status_text(self, key) -> str:
        """Что показать в статусе сервиса до запуска потока."""
        var = getattr(self, f"var_use_{key}", None)
        enabled = bool(var.get()) if var is not None else False
        if not enabled:
            return "не подключен"
        if service_key_var(self, key) is not None and not service_key_value(self, key):
            return "нужен ключ API"
        return "ожидает запуска"

    def _on_service_toggle(self, key, var):
        self.feed_status[key].set(self.service_status_text(key))

    def _on_service_status(self, key, text):
        """Поток сервиса сообщает реальный статус — обновляем окно."""
        if key not in getattr(self, "feed_status", {}):
            return
        try:
            self.root.after(0, lambda: self.feed_status[key].set(text))
        except Exception:
            pass

    def save_settings(self) -> bool:
        try:
            self.collect_config()
        except AppError:
            return False
        self.log("Настройки сохранены.")
        self.redraw_map()
        self.start_bot(restart=True)
        if self.core.running():
            self.restart_core()
        return True

    def save_and_restart(self):
        if self.save_settings() and not self.core.running():
            self.start_core()

    def restart_core(self):
        self.core.stop()
        self.core.start()
        self.log("Поток AIS перезапущен.")

    def start_core(self):
        try:
            self.collect_config()
        except AppError:
            return
        self.core.start()
        self.start_bot()
        self.log("Поток AIS запущен.")

    def stop_core(self):
        self.core.stop()
        self.log("Поток AIS остановлен.")

    def check_key(self):
        if getattr(self, "_key_checking", False):
            self.log("Проверка ключа уже идёт — подождите 15 секунд.")
            return
        try:
            self.collect_config()
        except AppError:
            return
        self._key_checking = True
        threading.Thread(target=self._check_key_worker, daemon=True).start()

    def check_openwaters(self):
        if getattr(self, "_ow_checking", False):
            self.log("Проверка Open Waters уже идёт — подождите 15 секунд.")
            return
        try:
            self.collect_config()
        except AppError:
            return
        self._ow_checking = True
        threading.Thread(target=self._check_openwaters_worker, daemon=True).start()

    def _check_openwaters_worker(self):
        ow_token = (self.config.get("openwaters_token") or "").strip()
        api_key = (self.config.get("api_key") or "").strip()
        token = ow_token or api_key or "public"
        if not (self.config.get("use_openwaters", True)):
            self._ow_checking = False
            self.log("Open Waters отключён в настройках.")
            return
        was_running = self.core.running()
        if was_running:
            self.core.stop()
            self.log("Проверка Open Waters: основной поток на время проверки остановлен.")
        self.log("Проверка Open Waters: подключаюсь на 15 секунд…")
        stop = threading.Event()
        received = {"count": 0, "ships": []}

        def on_event(event):
            received["count"] += 1
            name = (event.get("name") or "").strip()
            mmsi = event.get("mmsi") or ""
            if name and len(received["ships"]) < 3:
                received["ships"].append(f"{name} ({flags.flag_text(mmsi)}, MMSI {mmsi})")

        try:
            feed = ais_feed.AISStreamFeed(token, self.config.get("bounding_boxes"),
                                          self.config.get("message_types"), on_event,
                                          self.log, stop,
                                          stream_url=ais_feed.OPENWATERS_URL, name="ais-feed-ow-check")
            feed.start()
            deadline = time.time() + 15
            while time.time() < deadline and received["count"] == 0 and not feed.last_error:
                time.sleep(0.5)
            connected, error = feed.connected, feed.last_error
        finally:
            stop.set()
        try:
            feed.join(timeout=5)
        except Exception:
            pass
        if connected and received["count"] > 0:
            self.log(f"Open Waters: подключение работает, получено сообщений {received['count']}.")
            for ship in received["ships"]:
                self.log(f"  реальное судно: {ship}")
        elif connected:
            self.log("Open Waters: подключение установлено, но за 15 секунд сообщений не было — возможно тихий район.")
        else:
            self.log(f"Open Waters: не подключился: {error or 'сервер закрыл соединение'}.")
        self._ow_checking = False
        if was_running:
            try:
                self.core.start()
                self.log("Основной поток снова запущен.")
            except Exception:
                pass


    def _check_key_worker(self):
        key = (self.config.get("api_key") or "").strip()
        if not key:
            self._key_checking = False
            self.log("Проверка ключа: ключ не задан.")
            return
        # AISStream разрешает 3 подключения на аккаунт: своё основное соединение
        # на время проверки освобождаем, иначе оба получают HTTP 429
        was_running = self.core.running()
        if was_running:
            self.core.stop()
            self.log("Проверка ключа: основной поток на время проверки остановлен.")
        self.log("Проверка ключа: подключаюсь к AISStream на 15 секунд…")
        stop = threading.Event()
        received = {"count": 0, "ships": []}

        def on_event(event):
            received["count"] += 1
            name = (event.get("name") or "").strip()
            mmsi = event.get("mmsi") or ""
            if name and len(received["ships"]) < 3:
                received["ships"].append(f"{name} ({flags.flag_text(mmsi)}, MMSI {mmsi})")

        feed = ais_feed.AISStreamFeed(key, self.config.get("bounding_boxes"),
                                      self.config.get("message_types"), on_event,
                                      self.log, stop)
        try:
            feed.start()
            deadline = time.time() + 15
            while time.time() < deadline and received["count"] == 0 and not feed.last_error:
                time.sleep(0.5)
            connected, error = feed.connected, feed.last_error
        finally:
            stop.set()
        feed.join(timeout=5)
        if connected and received["count"] > 0:
            self.log(f"Ключ рабочий: за сеанс получено сообщений {received['count']} — "
                     f"это живые данные настоящих судов.")
            for ship in received["ships"]:
                self.log(f"  реальное судно: {ship}")
        elif connected:
            self.log("Ключ принят, но сообщений за 15 секунд не было — "
                     "в этом районе сейчас тихо, попробуйте позже.")
        else:
            self.log(f"Ключ не принят: {error or 'сервер закрыл соединение'}.")
        self._key_checking = False
        if was_running:
            self.core.start()
            self.log("Основной поток снова запущен.")

    def test_telegram(self):
        """Проверка Telegram в фоне: окно не замирает на время сетевых запросов."""
        if getattr(self, "_tg_checking", False):
            self.log("Проверка Telegram уже идёт — подождите.")
            return
        try:
            self.collect_config()
        except AppError:
            return
        self._tg_checking = True
        threading.Thread(target=self._test_telegram_worker, daemon=True,
                         name="tg-test").start()

    def _test_telegram_worker(self):
        token = self.config.get("telegram_token")
        chat = str(self.config.get("telegram_chat_id") or "").strip()
        try:
            if not chat:
                if self.bot_process is not None and self.bot_process.poll() is None:
                    # бот уже слушает getUpdates — второй запрос даст 409
                    raise AppError("chat_id не задан. Напишите боту в Telegram команду /id — "
                                   "он пришлёт chat_id, вставьте его в поле и повторите.")
                updates = telegram_call(token, "getUpdates", params={"timeout": 1},
                                        timeout=15) or []
                chat_ids = []
                for update in updates:
                    message = update.get("message") or {}
                    chat_id = (message.get("chat") or {}).get("id")
                    if chat_id and chat_id not in chat_ids:
                        chat_ids.append(chat_id)
                if not chat_ids:
                    raise AppError("chat_id пуст, и сообщений боту ещё не было. "
                                   "Напишите боту любое сообщение и повторите.")
                chat = str(chat_ids[-1])
                self.root.after(0, lambda value=chat: self._apply_found_chat(value))
            targets = parse_chat_ids(chat) or [chat]
            for target in targets:
                send_telegram(token, target,
                              "✅ Ship Tracker на связи. Алерты будут приходить сюда.")
            if len(targets) > 1:
                self.log(f"Тест Telegram: отправлено в {len(targets)} чата.")
        except AppError as error:
            self.log(f"Тест Telegram: {error}")
            text = str(error)
            self.root.after(0, lambda: self.messagebox.showerror("Telegram", text))
        else:
            self.log("Тест Telegram: сообщение отправлено.")
            self.root.after(0, lambda: self.messagebox.showinfo(
                "Telegram", "Сообщение отправлено — проверьте чат с ботом."))
        finally:
            self._tg_checking = False

    def _apply_found_chat(self, chat):
        self.var_chat.set(str(chat))
        self.config["telegram_chat_id"] = str(chat)
        save_config(self.config, self.config_path)
        self.log(f"chat_id найден и сохранён: {chat}")

    # -- бот Telegram
    def start_bot(self, restart=False):
        """Поднимает полного бота отдельным процессом — при открытии приложения.

        Бот тот же, что запускают руками (telegram_bot.py): его кнопки
        повторяют вкладки приложения, а данные он берёт из общей базы. Если
        бот уже запущен — руками или прошлым окном — data/bot.lock у него,
        и новый процесс молча выйдет: второго поллера не будет.
        """
        if not self.config.get("telegram_commands"):
            self.stop_bot_process()
            return
        if not (self.config.get("telegram_token") or "").strip():
            self.stop_bot_process()
            return
        process = self.bot_process
        if process is not None and process.poll() is None:
            if not restart:
                return
            self.stop_bot_process()
        try:
            log_path = DATA_DIR / "bot_run.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(log_path, "a", encoding="utf-8", errors="replace")
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.bot_process = subprocess.Popen(
                [sys.executable, "-X", "utf8", str(APP_DIR / "telegram_bot.py")],
                cwd=str(APP_DIR), stdout=handle, stderr=subprocess.STDOUT,
                creationflags=flags)
            self.log("Бот Telegram запущен вместе с приложением.")
        except Exception as error:
            self.bot_process = None
            self.log(f"Бот Telegram не запущен: {error}")

    def stop_bot_process(self):
        """Останавливает только тот бот, который запустило это окно."""
        process = self.bot_process
        self.bot_process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


    def check_snapshot(self):
        """Одна проверка снимка района: сколько судов вокруг портов сейчас известно."""
        if getattr(self, "_snap_checking", False):
            self.log("Проверка снимка района уже идёт — подождите.")
            return
        try:
            self.collect_config()
        except AppError:
            return
        self._snap_checking = True
        threading.Thread(target=self._check_snapshot_worker, daemon=True).start()
    def _check_snapshot_worker(self):
        boxes = ais_feed.tile_boxes(self.config.get("bounding_boxes"))
        if not boxes:
            self._snap_checking = False
            self.log("Снимок района: не задан район подписки.")
            return
        self.log(f"Снимок района ({len(boxes)} часть(ей) подписки): "
                 "запрашиваю последние позиции судов…")
        feed = ais_feed.SnapshotFeed(
            boxes, lambda event: None, self.log, threading.Event(),
            interval_minutes=self.config.get("snapshot_minutes") or 10,
            max_age_hours=self.config.get("snapshot_max_age_hours") or 6,
            name="snapshot-check")
        ships = feed.fetch_once()
        if feed.last_error:
            self.log(f"Снимок района: {feed.last_error}")
        elif ships:
            self.log(f"Снимок района: доступен, судов в районе сейчас {ships}.")
        else:
            self.log("Снимок района: доступен, но свежих судов в районе портов нет "
                     "(данные старше допустимого возраста пропущены).")
        self._snap_checking = False

    def check_service(self, key):
        """Одна проверка дополнительного подключения: один запрос и число судов."""
        checking = getattr(self, "_service_checking", None)
        if checking is None:
            checking = self._service_checking = set()
        title = services_feed.SERVICE_TITLES.get(key, key)
        if key in checking:
            self.log(f"Проверка {title} уже идёт — подождите.")
            return
        try:
            self.collect_config()
        except AppError:
            return
        checking.add(key)
        threading.Thread(target=self._check_service_worker, args=(key,),
                         daemon=True).start()

    def _check_service_worker(self, key):
        title = services_feed.SERVICE_TITLES.get(key, key)
        api_key = service_key_value(self, key) or (self.config.get(f"{key}_key") or "").strip()
        self._on_service_status(key, f"проверка {title.lower()}…")

        def finish(text):
            self._on_service_status(key, text)
            self._service_checking.discard(key)

        if services_feed.needs_key(key) and not api_key:
            self.log(f"{title}: не задан ключ API — проверка невозможна.")
            finish("нужен ключ API")
            return
        feed = services_feed.build(
            key, lambda event: None, self.log, threading.Event(),
            api_key=api_key, bboxes=self.config.get("bounding_boxes"),
            ports=self.config.get("ports"),
            vessels=(self.core.enrich_candidates if key == "srd" else None),
            # проверка не должна тратить много кредитов: только 5 записей
            max_records=5)
        if feed is None:
            self.log(f"{title}: нет доступного API.")
            finish("нет доступного API")
            return
        self.log(f"{title}: проверяю подключение (один запрос)…")
        try:
            feed.poll_once()
        except Exception as error:
            text = str(error).strip() or error.__class__.__name__
            self.log(f"{title}: не подключился — {text}")
            finish(f"ошибка: {text[:60]}")
            return
        status = feed.status or f"подключен, судов {feed.events}"
        self.log(f"{title}: {status}.")
        finish(status)

    # -------------------------------------- источники без ключа: проверка

    @staticmethod
    def format_source_result(result) -> str:
        """«работает — судов 13 (07.11 23:50)» для окна и бота."""
        result = result or {}
        if result.get("ok"):
            state = "работает"
        elif result.get("warn"):
            state = "только в браузере"
        else:
            state = "не отвечает"
        detail = str(result.get("detail") or "")
        text = f"{state} — {detail}" if detail else state
        when = str(result.get("checked") or "").replace("T", " ")
        when = when[5:16] if when else ""
        return f"{text} ({when})" if when else text

    def source_status_text(self, key) -> str:
        result = portcalls.source_result(key)
        return self.format_source_result(result) if result else "не проверялось"

    def _store_source_result(self, result):
        """Складывает результат в кэш и обновляет подпись в окне."""
        key = (result or {}).get("key")
        items = [item for item in (portcalls.load_sources().get("sources") or [])
                 if isinstance(item, dict) and item.get("key") != key]
        items.append(result)
        try:
            portcalls.save_sources(items)
        except OSError as error:
            self.log(f"Не удалось сохранить результат проверки: {error}")
        if key not in getattr(self, "source_status", {}):
            return
        text = self.format_source_result(result)
        try:
            self.root.after(0, lambda: self.source_status[key].set(text))
        except Exception:
            pass

    def check_source(self, key):
        """Одна проверка источника без ключа — в своём потоке."""
        source = portcalls.SOURCES_BY_KEY.get(key)
        if not source:
            return
        checking = getattr(self, "_source_checking", None)
        if checking is None:
            checking = self._source_checking = set()
        if key in checking:
            self.log("Проверка этого источника уже идёт — подождите.")
            return
        checking.add(key)
        if key in getattr(self, "source_status", {}):
            self.source_status[key].set("проверяю…")
        threading.Thread(target=self._check_source_worker, args=(source,),
                         daemon=True).start()

    def _check_source_worker(self, source):
        try:
            result = portcalls.check_source(source, log=self.log)
        except Exception as error:                     # не должно случаться
            result = {"key": source["key"], "title": source["title"],
                      "ok": False, "warn": False,
                      "detail": str(error)[:120],
                      "checked": datetime.now().isoformat(timespec="seconds")}
        finally:
            self._source_checking.discard(source["key"])
        self._store_source_result(result)

    def refresh_sources_now(self):
        """Внеочередной опрос всех включённых источников — одним заходом."""
        asked = 0
        try:
            asked = self.core.refresh_all_sources()
        except Exception as error:
            self.log(f"Обновление источников: {error}")
        if asked:
            self.log(f"Обновление данных запрошено у всех включённых источников ({asked}) — "
                     f"ответы придут одновременно.")
        else:
            self.log("Нет запущенных источников: включите их галочками выше или нажмите «Старт».")

    def check_all_sources(self):
        if getattr(self, "_sources_busy", False):
            self.log("Проверка источников уже идёт — подождите.")
            return
        self._sources_busy = True
        threading.Thread(target=self._check_all_sources_worker, daemon=True).start()

    def _check_all_sources_worker(self):
        self.log(f"Проверяю источники без ключа ({len(portcalls.SOURCES)} сайтов)…")
        try:
            for source in portcalls.SOURCES:
                try:
                    result = portcalls.check_source(source, log=self.log)
                except Exception as error:
                    result = {"key": source["key"], "title": source["title"],
                              "ok": False, "warn": False,
                              "detail": str(error)[:120],
                              "checked": datetime.now().isoformat(timespec="seconds")}
                self._store_source_result(result)
                time.sleep(0.5)
        finally:
            self._sources_busy = False
        self.log("Проверка источников завершена.")

    def close(self):
        try:
            self.core.stop()
            self.stop_bot_process()
        except Exception:
            pass
        finally:
            self.store.close()
            self.root.destroy()


# ------------------------------------------------------------------------ вход

def gui():
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except ImportError:
        raise AppError("Не найден Tkinter — установите Python с python.org (галочка tcl/tk).")

    config_path = CONFIG_PATH
    config = load_config(config_path)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        save_config(config, config_path)
    store = Store(DB_PATH)

    root = tk.Tk()
    try:
        app = App(tk, ttk, messagebox, root, config, store, config_path)
    except Exception:
        store.close()
        raise
    root.mainloop()
    return app


def report_startup_error(error: BaseException) -> Path:
    """Показывает причину сбоя запуска и пишет её в data/errors.log.

    Без этого окно приложения просто исчезает вместе с консолью, и по
    сообщению «мигнуло и закрылось» причину не найти.
    """
    import traceback

    text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    log_path = DATA_DIR / "errors.log"
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
            handle.write(text)
    except OSError:
        pass
    console_log(f"ОШИБКА ЗАПУСКА: {error.__class__.__name__}: {error}")
    console_log(text.strip())

    summary = f"{error.__class__.__name__}: {error}"
    body = (f"Приложение не смогло запуститься.\n\n{summary}\n\n"
            f"Подробности сохранены в файл:\n{log_path}")
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, body, "Ship Tracker — ошибка запуска", 0x10)
            return log_path
        except Exception:
            pass
    try:
        from tkinter import messagebox
        messagebox.showerror("Ship Tracker — ошибка запуска", body)
    except Exception:
        pass
    return log_path


def headless(args) -> int:
    config_path = Path(args.config) if args.config else CONFIG_PATH
    config = load_config(config_path)
    if args.api_key:
        config["api_key"] = args.api_key
    save_config(config, config_path)

    store = Store(Path(config_path).parent / DB_PATH.name)
    core = RadarCore(config, store, log=console_log,
                     on_alert=lambda alert: console_log(
                         f"АЛЕРТ: {alert['name']} {alert['origin']} → {alert['destination']} "
                         f"({fmt_number(alert.get('sog'))} уз)"
                         + (" [Telegram]" if alert.get("ok") else
                            f" [в окно: {alert.get('error')}]")))

    minutes = float(args.minutes or 1)
    console_log(f"Запуск без окна на {minutes:g} мин, источник: живой AISStream.")
    if not (config.get("api_key") or "").strip():
        console_log("Внимание: ключ AISStream не задан, живой поток не подключится.")
    core.start()
    deadline = time.time() + minutes * 60
    try:
        while time.time() < deadline:
            time.sleep(1)
    except KeyboardInterrupt:
        console_log("Остановка по Ctrl+C.")
    core.stop()

    counts = store.counts()
    targets = core.targets()
    console_log(f"Итог: судов {counts['vessels']}, позиций {counts['positions']}, "
                f"визитов в порты {counts['visits']}, целей сейчас {len(targets)}, "
                f"алертов поднято за сеанс {core.stats['alerts']}, "
                f"отправлено в Telegram сегодня {store.alerts_count_today()}.")
    for target in targets:
        console_log(f"  цель: {target['name']} ({target['mmsi']}) "
                    f"{target['origin']} → {target['destination']} "
                    f"{fmt_number(target['sog'])} уз, вышел {target.get('departed_at')}")
    for alert in store.alerts(5):
        console_log(f"  алерт: {alert['sent_at']} {alert['name']} "
                    f"{alert['origin']} → {alert['destination']} "
                    f"{'отправлен' if alert['ok'] else alert['error']}")
    store.close()
    return 0


def test_telegram_cli(config) -> int:
    try:
        send_telegram(config.get("telegram_token"), config.get("telegram_chat_id"),
                      "✅ Ship Tracker на связи.")
    except AppError as error:
        console_log(f"ОШИБКА: {error}")
        return 1
    console_log("Тестовое сообщение отправлено.")
    return 0


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        description="Ship Tracker — наблюдение за судами по живому AIS")
    parser.add_argument("--headless", action="store_true", help="без окна, только поток и база")
    parser.add_argument("--minutes", type=float, default=60,
                        help="сколько минут работать в режиме без окна")
    parser.add_argument("--api-key", help="ключ AISStream (сохранится в настройки)")
    parser.add_argument("--config", help="путь к своему config.json")
    parser.add_argument("--test-telegram", action="store_true", help="проверить Telegram")
    args = parser.parse_args()

    if args.test_telegram:
        path = Path(args.config) if args.config else CONFIG_PATH
        sys.exit(test_telegram_cli(load_config(path)))
    if args.headless:
        sys.exit(headless(args))
    try:
        gui()
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
    except BaseException as error:
        # Любой сбой запуска должен быть виден, а не закрывать окно молча.
        report_startup_error(error)
        sys.exit(1)


if __name__ == "__main__":
    main()


