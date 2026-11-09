"""Ядро приложения: настройки, приём AIS, геозоны, фильтр цели и алерты.

Логика целиком повторяет условие задания, но уже на реальных данных:

    тип = танкер (AIS Type 80–89)
    назначение (AIS Destination) содержит IZMAIL или RENI
    отправление (последний порт, из которого судно вышло по нашим данным)
        содержит ODESSA или CHORNOMORSK

Порт отправления AIS не передаёт, поэтому программа определяет его сама:
входит судно в геозону порта — открывается визит, выходит — визит
закрывается и порт становится «откуда вышел».
"""

import json
import queue
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

import ais_feed
import analysis
import cargo
import flags
import geo
import metrics
import portcalls
import services_feed
from store import Store, parse_iso

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "ship_tracker.db"

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


class AppError(Exception):
    """Ошибка, которую можно показать пользователю как есть."""


DEFAULT_CONFIG = {
    "api_key": "",
    "openwaters_token": "",
    "use_openwaters": True,
    "use_snapshot": True,
    "snapshot_minutes": 10,
    "snapshot_max_age_hours": 6,
    # слежение за нашими судами по MMSI по всему миру (AISStream FiltersShipMMSI):
    # единственный бесплатный способ увидеть судно, ушедшее за пределы района
    "use_mmsi_watch": True,
    # сколько часов позиция считается «сейчас в море», а не историей
    "sea_fresh_hours": 12,
    "port_watch_km": 30,
    # всё, что попадает в этот радиус от любого нашего порта, — «своё» судно:
    # пишется в базу и показывается во вкладке «Все суда» (800–1000 км)
    "area_radius_km": 900,
    "use_history": True,
    "use_vf": False,
    "use_vfa": False,
    "use_mt": False,
    "use_mst": False,
    "use_dt": False,
    # бесплатные открытые источники: ожидаемые прибытия AIS-catcher,
    # профили и разрывы AIS в Global Fishing Watch (нужен токен)
    "use_aci": True,
    "aci_minutes": 60,
    # публичные карты судов без ключа: дают позиции всех судов района
    "use_gor": True,
    "gor_minutes": 15,
    "use_mlive": True,
    "mlive_minutes": 15,
    # карточки судов Ship Radar 24: тип, осадка, размеры, позывной, назначение
    "use_srd": True,
    "srd_minutes": 15,
    "use_gfw": False,
    "gfw_key": "",
    "gfw_minutes": 720,
    "gfw_days": 7,
    "gfw_max_vessels": 20,
    # платные/ключевые источники: Datalastic (x-api-key) и AISHub (логин)
    "use_dat": False,
    "dat_key": "",
    "dat_minutes": 600,
    "dat_max_vessels": 10,
    "use_aishub": False,
    "aishub_key": "",
    "aishub_minutes": 600,
    "vf_key": "",
    "mt_key": "",
    "mst_key": "",
    "mst_minutes": 120,
    "mst_daily_coins": 150,
    "mst_max_vessels": 40,
    # ожидаемые прибытия VesselFinder: суда с нашим портом в Destination
    "vfa_minutes": 720,
    "vfa_lookahead_minutes": 720,
    "vfa_max_records": 40,
    "vfa_daily_coins": 300,
    "bounding_boxes": geo.DEFAULT_BOUNDING_BOXES,
    "message_types": ["PositionReport", "ExtendedClassBPositionReport",
                      "StandardClassBPositionReport", "ShipStaticData",
                      "StaticDataReport"],
    "ports": geo.DEFAULT_PORTS,
    "filter_destinations": ["IZMAIL", "RENI"],
    "filter_origins": ["ODESSA", "CHORNOMORSK"],
    "trip_window_hours": 96,
    "min_speed_knots": 2.0,
    "alert_cooldown_hours": 6,
    "position_interval_seconds": 60,
    "stale_minutes": 15,
    "history_days": 7,
    "forget_vessels_hours": 24,
    "telegram_token": "",
    "telegram_chat_id": "",
    "telegram_commands": True,
}

# MMSI демо-судов из старых версий с тестовым потоком: их удаляем из базы,
# чтобы на карте оставались только настоящие суда AIS.
LEGACY_DEMO_MMSI = ("272123456", "272654321", "272998877", "272001122",
                    "272445566", "272334455")


# ------------------------------------------------------------------ настройки

def merge_ports(saved, defaults) -> list:
    """Дополняет список портов новыми по умолчанию, не трогая свои геозоны."""
    result = []
    seen = set()
    defaults_by_name = {str(port["name"]).strip().upper(): port for port in defaults}
    for port in list(saved or []):
        if not isinstance(port, dict) or not port.get("name"):
            continue
        port = dict(port)
        # UN/LOCODE нужен внешним API; в старых настройках его нет
        default = defaults_by_name.get(str(port["name"]).strip().upper())
        if default and not port.get("locode") and default.get("locode"):
            port["locode"] = default["locode"]
        result.append(port)
        seen.add(str(port["name"]).strip().upper())
    for port in defaults:
        if port["name"].upper() not in seen:
            result.append(dict(port))
    return result


def _is_legacy_boxes(boxes) -> bool:
    """Совпадает ли район подписки со старым узким прямоугольником вдоль Дуная."""
    def normalized(value):
        try:
            return sorted(tuple(sorted((round(p[0], 3), round(p[1], 3)) for p in box))
                          for box in value)
        except (TypeError, ValueError, IndexError):
            return None

    current = normalized(boxes)
    if current is None:
        return False
    for legacy in geo.LEGACY_BOUNDING_BOXES:
        if normalized(legacy) == current:
            return True
    return False


def ensure_boxes(boxes, ports) -> list:
    """Расширяет район подписки, чтобы в него попадали все настроенные порты.

    Без этого порт есть в списке, но сообщений AIS по нему не приходит:
    AISStream присылает только то, что попало в прямоугольник подписки.
    """
    cleaned = []
    for box in boxes or []:
        try:
            (lat1, lon1), (lat2, lon2) = box
            cleaned.append([[float(lat1), float(lon1)], [float(lat2), float(lon2)]])
        except (TypeError, ValueError):
            continue
    if not cleaned:
        return json.loads(json.dumps(geo.DEFAULT_BOUNDING_BOXES))
    if _is_legacy_boxes(cleaned):
        # старый район вдоль Дуная без моря — приёмников AIS там нет, поток молчит
        return json.loads(json.dumps(geo.DEFAULT_BOUNDING_BOXES))

    south = min(min(corner[0][0], corner[1][0]) for corner in cleaned)
    west = min(min(corner[0][1], corner[1][1]) for corner in cleaned)
    north = max(max(corner[0][0], corner[1][0]) for corner in cleaned)
    east = max(max(corner[0][1], corner[1][1]) for corner in cleaned)
    changed = False
    for port in ports or []:
        try:
            lat, lon = float(port["lat"]), float(port["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (south <= lat <= north and west <= lon <= east):
            south, west = min(south, lat), min(west, lon)
            north, east = max(north, lat), max(east, lon)
            changed = True
    if not changed:
        return cleaned
    radius = 0.35  # запас вокруг дальнего порта, чтобы видеть подходы
    return [[[round(south - radius, 2), round(west - radius, 2)],
             [round(north + radius, 2), round(east + radius, 2)]]]


def load_config(path=None) -> dict:
    path = Path(path) if path else CONFIG_PATH
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise AppError(f"Не удалось прочитать настройки {path.name}: {error}")
        if isinstance(saved, dict):
            config.update(saved)
    config["ports"] = merge_ports(config.get("ports"), geo.DEFAULT_PORTS)
    config["bounding_boxes"] = ensure_boxes(config.get("bounding_boxes"), config["ports"])
    return config


def save_config(config: dict, path=None) -> Path:
    path = Path(path) if path else CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# -------------------------------------------------------------- сопоставление

def norm(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().upper()


def port_matches(value, targets) -> bool:
    """Ищет название порта внутри строки: «IZMAIL», «IZMAIL, UA», «IZMAIL>RENI»."""
    text = norm(value)
    if not text:
        return False
    for target in targets or []:
        target = norm(target)
        if target and re.search(r"\b" + re.escape(target) + r"\b", text):
            return True
    return False


def matched_port(value, targets):
    """Название цели, которая совпала (для подписи в интерфейсе)."""
    for target in targets or []:
        if port_matches(value, [target]):
            return norm(target)
    return None


def split_list(text) -> list:
    return [part.strip() for part in re.split(r"[,;\n]+", text or "") if part.strip()]


# ------------------------------------------------------------------ телеграм

class TelegramError(AppError):
    pass


def telegram_call(token, method, payload=None, params=None, timeout=30):
    if not (token or "").strip():
        raise TelegramError("Не задан токен бота.")
    try:
        response = requests.post(
            TELEGRAM_API.format(token=token.strip(), method=method),
            json=payload, params=params, timeout=timeout)
    except requests.RequestException as error:
        raise TelegramError(f"Нет связи с Telegram: {error}")
    try:
        data = response.json()
    except ValueError:
        raise TelegramError(f"Telegram ответил HTTP {response.status_code}: {response.text[:200]}")
    if not data.get("ok"):
        raise TelegramError(f"Telegram отказал ({response.status_code}): {data.get('description')}")
    return data.get("result")


def send_telegram(token, chat_id, text):
    if not str(chat_id or "").strip():
        raise TelegramError("Не задан chat_id получателя.")
    return telegram_call(token, "sendMessage", {
        "chat_id": str(chat_id).strip(),
        "text": text,
        "disable_web_page_preview": True,
    })


def fmt_number(value) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return str(int(number)) if number.is_integer() else f"{number:.1f}"


def fmt_tons(value) -> str:
    """Тонны с разделением разрядов: «5 112»."""
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{round(number):,}".replace(",", " ")


def build_alert_text(target: dict) -> str:
    lines = [
        "🚨 ЦЕЛЬ В ДВИЖЕНИИ",
        f"Судно: {target.get('name') or target.get('mmsi')}",
    ]
    if target.get("imo"):
        lines.append(f"IMO: {target['imo']}")
    lines += [
        f"MMSI: {target.get('mmsi')}",
        f"Флаг: {flag_line(target)}",
        f"Маршрут: {target.get('origin')} → {target.get('destination')}",
        f"Скорость: {fmt_number(target.get('sog'))} уз · Осадка: {fmt_number(target.get('draft'))} м",
        f"Координаты: {fmt_number(target.get('lat'))}, {fmt_number(target.get('lon'))}",
        f"Прибытие: {eta_line(target)}",
        f"Время: {datetime.now():%d.%m.%Y %H:%M}",
        f"https://www.marinetraffic.com/en/ais/details/ships/mmsi:{target.get('mmsi')}",
    ]
    return "\n".join(lines)


def flag_line(target: dict) -> str:
    """Страна флага для алерта: эмодзи + название + ISO-код."""
    country = target.get("country") or ""
    emoji = target.get("flag_emoji") or ""
    iso2 = target.get("flag_iso") or ""
    if not country:
        return "не определён по MMSI"
    text = f"{emoji} {country}".strip()
    if iso2:
        text += f" ({iso2})"
    return text


def fmt_moment(moment) -> str:
    return moment.strftime("%d.%m.%Y %H:%M") if moment else "—"


def duration_text(hours) -> str:
    """«через 2 д 4 ч» / «через 35 мин»."""
    if hours is None:
        return ""
    minutes = int(round(float(hours) * 60))
    if minutes < 60:
        return f"через {max(1, minutes)} мин"
    days, rest = divmod(minutes, 24 * 60)
    hours_left, minutes_left = divmod(rest, 60)
    if days:
        return f"через {days} д {hours_left} ч"
    return f"через {hours_left} ч {minutes_left:02d} мин"


def span_text(hours) -> str:
    """Длительность без «через»: «35 мин», «2 ч 05 мин», «1 д 3 ч»."""
    if hours is None:
        return "—"
    minutes = int(round(float(hours) * 60))
    if minutes < 1:
        return "меньше минуты"
    if minutes < 60:
        return f"{minutes} мин"
    days, rest = divmod(minutes, 24 * 60)
    hours_left, minutes_left = divmod(rest, 60)
    if days:
        return f"{days} д {hours_left} ч"
    return f"{hours_left} ч {minutes_left:02d} мин"


def eta_line(target: dict) -> str:
    """Расчётное прибытие + заявленное капитаном (если AIS его передал)."""
    parts = []
    calculated = target.get("eta_calc")
    if calculated:
        parts.append(f"{fmt_moment(calculated)} (расчёт {duration_text(target.get('eta_hours'))},"
                     f" {fmt_number(target.get('distance_km'))} км)")
    declared = parse_iso(target.get("eta_declared"))
    if target.get("eta_declared"):
        parts.append(f"{fmt_moment(declared)} (по AIS)" if declared
                     else str(target["eta_declared"]))
    return "; ".join(parts) if parts else "нет данных (нет скорости или порта)"


# ---------------------------------------------------------------------- ядро

class RadarCore:
    """Крутит поток AIS, пишет в базу и поднимает алерты."""

    def __init__(self, config: dict, store: Store, log=None, on_alert=None):
        self.config = config
        self.store = store
        self.log = log or (lambda message: None)
        self.on_alert = on_alert or (lambda alert: None)
        self.stop_event = threading.Event()
        self.events = queue.Queue(maxsize=20000)
        self.feed = None
        self.feeds = []
        self.consumer = None
        self.alerts_enabled = True
        self.stats = {
            "events": 0, "positions": 0, "static": 0, "alerts": 0,
            "dropped": 0, "last_event": None, "started_at": None,
        }
        self._last_stored = {}
        self._local_alerts = {}
        self._alert_queue = queue.Queue()
        self.alert_worker = None
        self._last_housekeeping = 0.0
        self.service_status = {}
        self.on_service_status = None
        self._feed_lock = None
        self.feed_elsewhere = False

    # ------------------------------------------------------------ запуск
    def start(self):
        if self.feed and self.feed.is_alive():
            return
        if self.feed_elsewhere and any(feed.is_alive() for feed in self.feeds):
            return
        # AIS принимает только один процесс: второй читает уже готовую базу
        lock = claim_feed_lock(self.store)
        if lock is None:
            self.feed_elsewhere = True
            self.log("Поток AIS уже принимается другим процессом — "
                     "данные берутся из общей базы.")
            # свои AIS-потоки не поднимаем, но внешние API по ключу работают:
            # их события всё равно надо разбирать и складывать в общую базу
            self.stop_event = threading.Event()
            self.events = queue.Queue(maxsize=20000)
            self._start_consumer()
            self._start_services()
            self._start_startup_refresh()
            return
        self._feed_lock = lock
        self.feed_elsewhere = False
        # новый event и очередь на каждый запуск: старые потоки видят свой (уже поднятый) флаг
        self.stop_event = threading.Event()
        self.events = queue.Queue(maxsize=20000)
        self.stats["started_at"] = datetime.now()
        self._start_alert_worker()
        self._start_consumer()
        self._start_feed()
        self._start_startup_refresh()

    def _start_feed(self):
        config = self.config
        # в базе не должно остаться демо-судов прошлых версий: только настоящие
        removed = self.store.purge_vessels(list(LEGACY_DEMO_MMSI))
        self._last_stored.clear()
        self._local_alerts.clear()
        if removed:
            self.log(f"Из базы убраны демо-суда прежней версии: {removed}. "
                     f"Остаются только реальные суда AIS.")
        dropped = self.purge_unrelated()
        if dropped:
            self.log(f"Из базы убраны суда вне наших портов: {dropped}. "
                     f"В списке только суда, связанные с портами наблюдения.")
        self.feed = None
        self.feeds = []
        # Основной поток AISStream
        api_key = (config.get("api_key") or "").strip()
        if api_key:
            feed1 = ais_feed.AISStreamFeed(
                api_key, config.get("bounding_boxes"),
                config.get("message_types"), self._receive, self.log, self.stop_event,
                stream_url=ais_feed.STREAM_URL, name="ais-feed")
            feed1.start()
            self.feed = feed1
            self.feeds.append(feed1)
        else:
            self.log("API-ключ AISStream не задан — основной поток не запущен.")
        # Дополнительный поток Open Waters AIS (совместим с протоколом)
        if config.get("use_openwaters", True):
            ow_token = (config.get("openwaters_token") or "").strip() or "public"
            self.log(f"Запуск Open Waters AIS (токен {'задан' if (config.get('openwaters_token') or '').strip() else 'публичный'}, боксы: {len(config.get('bounding_boxes') or [])})")
            url = getattr(ais_feed, "OPENWATERS_URL", None)
            if url:
                try:
                    feed2 = ais_feed.AISStreamFeed(
                        ow_token, config.get("bounding_boxes"),
                        config.get("message_types"), self._receive, self.log, self.stop_event,
                        stream_url=url, name="ais-feed-ow")
                    feed2.start()
                    if not self.feed:
                        self.feed = feed2
                    self.feeds.append(feed2)
                except Exception as error:
                    self.log(f"Open Waters AIS: не удалось запустить ({error})")
        # Слежение по MMSI: наши суда в любой точке мира, где есть приём AIS
        if config.get("use_mmsi_watch", True) and api_key:
            try:
                watch = ais_feed.MmsiWatchFeed(
                    api_key, self.watch_mmsis, config.get("message_types"),
                    self._receive, self.log, self.stop_event,
                    stream_url=ais_feed.STREAM_URL, name="ais-feed-mmsi")
                watch.start()
                self.feeds.append(watch)
                if not self.feed:
                    self.feed = watch
            except Exception as error:
                self.log(f"Слежение по MMSI: не удалось запустить ({error})")
        # Снимок района: последние известные позиции всех судов вокруг портов
        if config.get("use_snapshot", True):
            boxes = ais_feed.tile_boxes(config.get("bounding_boxes"))
            if not boxes:
                port_box = ais_feed.ports_bbox(config.get("ports") or [])
                if port_box:
                    south, west, north, east = port_box
                    boxes = ais_feed.tile_boxes([[[south, west], [north, east]]])
            if boxes:
                try:
                    feed3 = ais_feed.SnapshotFeed(
                        boxes, self._receive, self.log, self.stop_event,
                        interval_minutes=config.get("snapshot_minutes") or 10,
                        max_age_hours=config.get("snapshot_max_age_hours") or 6)
                    feed3.start()
                    self.feeds.append(feed3)
                except Exception as error:
                    self.log(f"Снимок района: не удалось запустить ({error})")
            else:
                self.log("Снимок района: не заданы порты наблюдения.")
        # История стоянок: визиты, которые программа сама не видела
        if config.get("use_history", True):
            threading.Thread(target=self._history_worker, daemon=True,
                             name="history-stays").start()
        self._start_services()

    def _start_startup_refresh(self):
        """Обновление данных при открытии: ждёт первый ответ от всех источников.

        Все включённые источники опрашиваются одновременно (каждый в своём
        потоке), поэтому при открытии программы данные обновляются за один
        заход, а не по очереди. Когда все ответили — в журнал пишется итог,
        и окно перерисовывает таблицы и карту.
        """
        threading.Thread(target=self._startup_refresh_worker, daemon=True,
                         name="startup-refresh").start()

    def _startup_refresh_worker(self):
        feeds = [feed for feed in list(self.feeds) if hasattr(feed, "first_done")]
        if not feeds:
            return
        started = time.time()
        answered = 0
        for feed in feeds:
            left = max(1.0, 180.0 - (time.time() - started))
            if feed.first_done.wait(left):
                answered += 1
        try:
            vessels = len(self.store.vessels(3000))
        except Exception:
            vessels = 0
        self.log(f"Данные при открытии обновлены за {time.time() - started:.0f} с: "
                 f"источников ответило {answered} из {len(feeds)}, "
                 f"судов в базе {vessels}.")
        callback = getattr(self, "on_refresh", None)
        if callback:
            try:
                callback()
            except Exception:
                pass

    def refresh_all_sources(self):
        """Внеочередной опрос всех включённых источников — одним заходом.

        Не ждёт расписания и не трогает уже запущенные потоки: просто просит
        каждый из них выполнить следующий цикл сейчас. Возвращает число
        источников, которым отправлен запрос.
        """
        asked = 0
        for feed in list(self.feeds):
            wake = getattr(feed, "wake", None)
            if not callable(wake):
                continue
            try:
                wake()
                asked += 1
            except Exception:
                continue
        if asked:
            self.log(f"Внеочередное обновление: опрос отправлен источникам — {asked}.")
            self._start_startup_refresh()
        return asked

    def _start_services(self):
        """Поднимает опрос выбранных внешних сервисов (см. services_feed).

        Все включённые источники стартуют сразу, каждый в своём потоке:
        запросы уходят одновременно, а не по очереди.
        """
        config = self.config
        self.service_status = {}
        started = []
        for key in ("vf", "vfa", "mt", "mst", "dt", "aci", "gfw", "dat", "aishub",
                    "gor", "mlive", "srd"):
            if not config.get(f"use_{key}"):
                continue
            title = services_feed.SERVICE_TITLES.get(key, key)
            api_key = (config.get(f"{key}_key") or "").strip()
            if key == "vfa" and not api_key:
                # ожидаемые прибытия — тот же ключ VesselFinder
                api_key = (config.get("vf_key") or "").strip()
            if services_feed.needs_key(key) and not api_key:
                self._set_service_status(key, "нужен ключ API")
                self.log(f"{title}: не задан ключ API — поток не запущен.")
                continue
            feed = services_feed.build(
                key, self._receive, self.log, self.stop_event,
                api_key=api_key,
                bboxes=config.get("bounding_boxes"),
                on_status=self._set_service_status,
                vessels=(self.enrich_candidates if key == "srd" else self.tracked_mmsis),
                minutes=config.get(f"{key}_minutes"),
                daily_coins=(config.get("vfa_daily_coins") if key == "vfa"
                             else config.get("mst_daily_coins")),
                max_vessels=config.get(f"{key}_max_vessels"),
                state_path=DATA_DIR / "credits.json",
                ports=config.get("ports"),
                lookahead_minutes=config.get("vfa_lookahead_minutes"),
                max_records=config.get("vfa_max_records"),
                cache_path=(DATA_DIR / "expected.json" if key == "aci"
                            else DATA_DIR / "gfw.json" if key == "gfw" else None),
                days=config.get("gfw_days"))
            if feed is None:
                self._set_service_status(key, "нет доступного API")
                continue
            self._set_service_status(key, "запуск…")
            feed.start()
            self.feeds.append(feed)
            started.append(title)
        if started:
            self.log(f"Источники запущены одновременно ({len(started)}): "
                     + ", ".join(started) + ".")

    def tracked_mmsis(self):
        """MMSI всех судов в базе — для обогащения внешними API."""
        try:
            rows = self.store.vessels(2000)
        except Exception:
            return []
        return [str(row.get("mmsi") or "") for row in rows if row.get("mmsi")]

    def watch_mmsis(self, limit=40):
        """Суда для глобального слежения по MMSI.

        AISStream держит подписку с десятками MMSI, а на сотни рвёт соединение
        сразу после отправки (проверено: 260 MMSI — тишина и переподключение
        каждые 5 секунд, 20 MMSI — стабильный поток). После подключения карт
        района в базе оказались сотни судов у портов, поэтому в подписку идут
        только суда с нашим портом в назначении и самые свежие из базы.
        """
        try:
            rows = self.store.vessels(2000)
        except Exception:
            return []
        tokens = set()
        for name in self.config.get("filter_destinations") or []:
            if name:
                tokens.add(str(name).strip().upper())
        for port in self.config.get("ports") or []:
            for field in ("name", "locode"):
                value = str(port.get(field) or "").strip().upper()
                if value:
                    tokens.add(value)

        def ours(row):
            destination = str(row.get("destination") or "").upper()
            return any(token in destination for token in tokens if token)

        ordered = sorted(rows, key=lambda row: str(row.get("updated_at") or ""),
                         reverse=True)
        ordered.sort(key=lambda row: 0 if ours(row) else 1)
        result = []
        for row in ordered:
            mmsi = str(row.get("mmsi") or "")
            if mmsi and mmsi not in result:
                result.append(mmsi)
            if len(result) >= limit:
                break
        return result

    def _history_worker(self):
        """Подтягивает стоянки из трека Open Waters после старта потока."""
        if self.stop_event.wait(20):
            return
        while not self.stop_event.is_set():
            try:
                result = self.import_history_stays(limit=60, pause=0.5)
            except Exception as error:
                self.log(f"История стоянок: {error}")
                break
            if result.get("imported"):
                self.log(f"История стоянок: добавлено визитов {result['imported']} "
                         f"(проверено судов {result['checked']}).")
            # повторяем регулярно: судно могло зайти в порт уже после запуска
            if self.stop_event.wait(20 * 60):
                break

    def _start_consumer(self):
        self.consumer = threading.Thread(target=self._consume, args=(self.stop_event,),
                                        name="ais-consumer", daemon=True)
        self.consumer.start()

    def _start_alert_worker(self):
        self.alert_worker = threading.Thread(target=self._alert_worker, daemon=True,
                                            name="alerts-send")
        self.alert_worker.start()

    def _alert_worker(self):
        """Шлёт алерты в Telegram в своём потоке, чтобы не задерживать AIS."""
        while not self.stop_event.is_set() or not self._alert_queue.empty():
            try:
                target, text = self._alert_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._send_alert(target, text)
            except Exception as error:
                self.log(f"Алерт по {target.get('name')}: сбой отправки ({error})")

    def stop(self, wait=3):
        self.stop_event.set()
        for thread in list(getattr(self, "feeds", [])) + [self.feed, self.consumer,
                                                          getattr(self, "alert_worker", None)]:
            if thread and thread.is_alive():
                thread.join(timeout=wait)
        self.feed = None
        self.feeds = []
        self.consumer = None
        lock = getattr(self, "_feed_lock", None)
        if lock is not None:
            try:
                lock.close()
            except OSError:
                pass
            self._feed_lock = None

    def ais_feeds(self) -> list:
        """Только AIS-потоки: внешние API по ключу сюда не входят."""
        return [feed for feed in getattr(self, "feeds", [])
                if feed and not getattr(feed, "service", False)]

    def running(self) -> bool:
        if self.feed and self.feed.is_alive():
            return True
        return any(feed.is_alive() for feed in self.ais_feeds())

    def feed_state(self) -> dict:
        """Сводное состояние AIS: на связи, если отвечает хотя бы один канал."""
        feeds = self.ais_feeds()
        connected = [feed for feed in feeds if getattr(feed, "connected", False)]
        error = next((getattr(feed, "last_error", "") for feed in feeds
                      if getattr(feed, "last_error", "")), "")
        since = next((feed.connected_since for feed in connected
                      if getattr(feed, "connected_since", None)), None)
        return {
            "running": self.running(),
            "connected": bool(connected),
            "error": error,
            "connected_since": since,
        }

    def feeds_state(self) -> list:
        """Состояние каждого канала отдельно — для вкладки «Подключение»."""
        rows = []
        for feed in getattr(self, "feeds", []):
            if not feed:
                continue
            rows.append({
                "name": getattr(feed, "feed_title", None) or feed.name,
                "connected": bool(getattr(feed, "connected", False)),
                "events": int(getattr(feed, "events", 0)),
                "error": getattr(feed, "last_error", "") or "",
                "since": getattr(feed, "connected_since", None),
            })
        return rows

    # ------------------------------------------------ только «наши» суда
    def _vessel_related(self, mmsi, lat=None, lon=None, vessel=None, destination=None,
                        area=False) -> bool:
        """Относится ли судно к нашим портам: стоит/проходит рядом, был в порту
        или следует в него/из него по данным AIS.

        area=True добавляет проверку по широкому району (`area_radius_km`):
        так судно, идущее через Чёрное море в сотнях километров от порта,
        тоже записывается в базу и попадает во вкладку «Все суда».
        """
        mmsi = str(mmsi)
        ports = self.config.get("ports") or []
        if ports and lat is not None and lon is not None:
            if geo.port_at(lat, lon, ports):
                return True
        if self._history_related(mmsi):
            return True
        variants = geo.port_aliases(ports)
        names = [variant for group in variants.values() for variant in group]
        text = destination if destination is not None else (vessel or {}).get("destination")
        if text and matched_port(text, names):
            return True
        if ports and lat is not None and lon is not None:
            watch = float(self.config.get("port_watch_km") or 0)
            if area:
                watch = max(watch, self.area_radius_km())
            if watch > 0:
                for port in ports:
                    try:
                        distance = geo.haversine_km(lat, lon, float(port["lat"]), float(port["lon"]))
                    except (KeyError, TypeError, ValueError):
                        continue
                    if distance <= watch:
                        return True
        return False

    def area_radius_km(self) -> float:
        """Радиус района вокруг портов, по которому суда считаются своими."""
        try:
            value = float(self.config.get("area_radius_km") or 0)
        except (TypeError, ValueError):
            value = 900.0
        return max(0.0, min(1500.0, value))

    def vessel_links(self, vessel, visit_ports=None) -> dict:
        """Связи судна с нашими портами: {порт: {"kind": «идёт» | «был»}}.

        «Идёт» — порт в поле Destination; «был» — программа видела визит
        (стоял в порту или выходил оттуда). Расстояние не важно: далёкое
        судно с нашим портом в Destination тоже связано с ним. Судно, которое
        просто проходит рядом с портом, связанным не считается.
        """
        links = {}
        if not vessel:
            return links
        ports = self.config.get("ports") or []
        destination = str(vessel.get("destination") or "")
        if destination:
            for name, variants in geo.port_aliases(ports).items():
                if matched_port(destination, list(variants)):
                    links[name] = {"kind": "идёт"}
        mmsi = str(vessel.get("mmsi") or "")
        if mmsi:
            # visit_ports — один запрос на все суда списка: иначе вкладка
            # «В море» делала бы по запросу в базу на каждую строку
            if isinstance(visit_ports, dict):
                for name in visit_ports.get(mmsi, ()):
                    links.setdefault(name, {"kind": "был"})
            else:
                try:
                    for visit in self.store.visits(20, mmsi=mmsi):
                        name = str(visit.get("port") or "").strip().upper()
                        if name and name not in links:
                            links[name] = {"kind": "был"}
                except Exception:
                    pass
        return links

    def linked_vessels(self, limit=2000) -> list:
        """Суда, связанные с нашими портами по данным AIS, без ограничения
        по расстоянию: наш порт в Destination или визит в наш порт.
        Просто проходящие рядом суда сюда не попадают.
        """
        try:
            visit_ports = self.store.visit_ports()
        except Exception:
            visit_ports = {}
        result = []
        try:
            rows = self.store.vessels(limit)
        except Exception:
            return result
        for vessel in rows:
            if self.vessel_links(vessel, visit_ports):
                result.append(vessel)
        return result

    def _history_related(self, mmsi) -> bool:
        """Был ли визит в наш порт / выход из него. Ответ кэшируется на минуту:
        иначе на каждое сообщение AIS шло бы по два запроса в базу."""
        cache = getattr(self, "_related_cache", None)
        if cache is None:
            cache = self._related_cache = {}
        now = time.monotonic()
        hit = cache.get(mmsi)
        if hit and now - hit[1] < 60:
            return hit[0]
        value = bool(self.store.has_visits(mmsi) or
                     self.store.last_departure(mmsi, self.config.get("trip_window_hours") or 96))
        if len(cache) > 5000:
            cache.clear()
        cache[mmsi] = (value, now)
        return value

    def related_vessels(self, limit=1000) -> list:
        """Только суда, связанные с нашими портами — для карты и списков.

        База чистится реже, чем приходят сообщения, поэтому судно, которое
        прошло рядом и ушло в море, может ещё полежать в базе. На экран оно
        уже не попадает: фильтр проверяется при каждой отрисовке.
        """
        return [vessel for vessel in self.store.vessels(limit)
                if self._vessel_related(vessel.get("mmsi"), vessel.get("lat"),
                                        vessel.get("lon"), vessel)]

    def area_vessels(self, limit=2000) -> list:
        """Все суда района — в радиусе `area_radius_km` от любого нашего порта.

        В базу такие суда уже пишутся (проверка района есть при сохранении),
        поэтому здесь достаточно отдать записи: они и есть «все суда» вкладки.
        """
        try:
            rows = self.store.vessels(limit)
        except Exception:
            return []
        radius = self.area_radius_km()
        ports = self.config.get("ports") or []
        result = []
        for row in rows:
            lat, lon = row.get("lat"), row.get("lon")
            if lat is None or lon is None:
                result.append(row)
                continue
            if self._vessel_related(row.get("mmsi"), lat, lon, row, area=True):
                result.append(row)
                continue
            nearest = min((geo.haversine_km(lat, lon, float(port["lat"]), float(port["lon"]))
                           for port in ports
                           if port.get("lat") is not None and port.get("lon") is not None),
                          default=None)
            if nearest is not None and nearest <= radius:
                result.append(row)
        return result

    def sea_vessels(self, limit=1000) -> list:
        """Суда, которые прямо сейчас находятся в море.

        Берём только связанные с нашими портами суда (наш порт в Destination
        или визит в наш порт) с свежей позицией вне портовых геозон; суда,
        которые лишь проходят рядом, не показываются. «В море» — это и идущие
        под ходом, и те, кто стоит на рейде или на якоре перед входом: любой,
        кто не в порту. Порядок: сначала под ходом (быстрее — выше).
        """
        try:
            fresh_hours = float(self.config.get("sea_fresh_hours") or 12)
        except (TypeError, ValueError):
            fresh_hours = 12.0
        ports = self.config.get("ports") or []
        now = datetime.now()
        rows = []
        for vessel in self.linked_vessels(limit):
            lat, lon = vessel.get("lat"), vessel.get("lon")
            if lat is None or lon is None:
                continue
            if ports and geo.port_at(lat, lon, ports):
                continue
            age = None
            updated = vessel.get("updated_at")
            if updated:
                try:
                    age = (now - datetime.fromisoformat(str(updated)[:19])).total_seconds() / 3600
                except ValueError:
                    age = None
            if age is not None and age > fresh_hours:
                continue
            row = dict(vessel)
            row["age_hours"] = age
            row["underway"] = (vessel.get("sog") or 0) > 0.5
            rows.append(row)
        rows.sort(key=lambda item: (0 if item["underway"] else 1,
                                    -(item.get("sog") or 0),
                                    str(item.get("name") or "")))
        return rows

    def sea_without_position(self, limit=1000) -> list:
        """Связанные суда без позиции: сигнала AIS нет, но они в списке."""
        return [vessel for vessel in self.related_vessels(limit)
                if vessel.get("lat") is None or vessel.get("lon") is None]

    def enrich_candidates(self, limit=200) -> list:
        """Суда, у которых не хватает паспортных данных для нашего стандарта.

        Отдаёт записи из базы, у которых нет типа, осадки или размеров —
        их карточки дочитывает поток Ship Radar 24.
        """
        try:
            rows = self.store.vessels(2000)
        except Exception:
            return []
        result = []
        for row in rows:
            if self.is_station_mmsi(row.get("mmsi")):
                continue
            if row.get("lat") is None and not row.get("destination"):
                continue
            missing = (row.get("type_code") is None or row.get("draft") is None
                       or row.get("length_m") is None or row.get("beam_m") is None
                       or not row.get("imo"))
            if not missing:
                continue
            result.append(dict(row))
            if len(result) >= limit:
                break
        return result

    def port_place(self, vessel) -> dict | None:
        """Где судно относительно портов: у причала, на рейде или нигде.

        Рейд — это стоянка почти без хода в пределах 12 км от границы геозоны
        порта: так стоят суда в ожидании лоцмана и очереди на обработку.
        """
        ports = self.config.get("ports") or []
        lat, lon = vessel.get("lat"), vessel.get("lon")
        if lat is None or lon is None or not ports:
            return None
        try:
            speed = float(vessel.get("sog") or 0)
        except (TypeError, ValueError):
            speed = 0.0
        port = geo.port_at(lat, lon, ports)
        if port:
            place = "у причала" if speed < 0.5 else "в порту"
            return {"port_name": port.get("name"), "place": place, "port_km": 0.0}
        best = None
        for candidate in ports:
            try:
                km = geo.haversine_km(lat, lon, float(candidate["lat"]), float(candidate["lon"]))
                radius = float(candidate.get("radius_km") or 5.0)
            except (KeyError, TypeError, ValueError):
                continue
            gap = km - radius
            if best is None or gap < best[0]:
                best = (gap, candidate, km)
        if not best or best[0] > 12 or speed > 1.5:
            return None
        place = "рейд: ждёт лоцмана" if speed < 0.5 else "подход к порту"
        return {"port_name": best[1].get("name"), "place": place, "port_km": best[2]}

    def port_vessels(self, limit=600) -> list:
        """Суда у причалов и на рейде — для галочки «в порту» и бота.

        Сортировка: сначала те, кто у причала, потом ожидающие лоцмана;
        внутри — по названию порта и имени судна.
        """
        result = []
        for vessel in self.related_vessels(limit):
            place = self.port_place(vessel)
            if not place:
                continue
            row = dict(vessel)
            row.update(place)
            result.append(row)
        result.sort(key=lambda item: (0 if item.get("port_km") == 0 else 1,
                                      str(item.get("port_name") or ""),
                                      str(item.get("name") or "")))
        return result

    def traffic_vessels(self, limit=400) -> list:
        """Чужой трафик района: суда в море, не идущие в наши порты.

        Нужен, чтобы вкладка «В море» показывала всю обстановку, а не только
        наши суда: у каждого — расстояние до ближайшего порта наблюдения.
        """
        ports = self.config.get("ports") or []
        try:
            fresh = float(self.config.get("sea_fresh_hours") or 12)
        except (TypeError, ValueError):
            fresh = 12.0
        rows = self.store.sea_traffic(fresh, limit=2000)
        result = []
        for row in rows:
            lat, lon = row.get("lat"), row.get("lon")
            if lat is None or lon is None:
                continue
            if ports and geo.port_at(lat, lon, ports):
                continue
            nearest = None
            distance = None
            for port in ports:
                try:
                    km = geo.haversine_km(float(lat), float(lon),
                                          float(port["lat"]), float(port["lon"]))
                except (KeyError, TypeError, ValueError):
                    continue
                if distance is None or km < distance:
                    nearest, distance = port, km
            row["nearest_port"] = (nearest or {}).get("name")
            row["nearest_km"] = distance
            row["underway"] = (row.get("sog") or 0) > 0.5
            result.append(row)
        result.sort(key=lambda item: (0 if item["underway"] else 1,
                                      item.get("nearest_km") if item.get("nearest_km") is not None else 9e9))
        return result[:limit]

    def purge_unrelated(self) -> int:
        """Удаляет из базы суда, никак не связанные с нашими портами."""
        removed = 0
        for vessel in self.store.vessels(100000):
            mmsi = vessel.get("mmsi")
            if not mmsi:
                continue
            if self._vessel_related(mmsi, vessel.get("lat"), vessel.get("lon"), vessel,
                                    area=True):
                self.store.set_tracked(mmsi, True)
            else:
                self.store.drop_vessel(mmsi)
                removed += 1
        return removed

    # ------------------------------------------------------------ приём
    def _receive(self, event):
        try:
            self.events.put_nowait(event)
        except queue.Full:
            self.stats["dropped"] += 1

    def _set_service_status(self, key, text):
        """Статус внешнего сервиса: в словарь и, если есть, в интерфейс."""
        self.service_status[key] = text
        callback = getattr(self, "on_service_status", None)
        if callback:
            try:
                callback(key, text)
            except Exception:
                pass

    def _consume(self, stop_event):
        while not stop_event.is_set():
            try:
                event = self.events.get(timeout=0.5)
            except queue.Empty:
                self._housekeeping()
                continue
            try:
                self.process(event)
            except Exception as error:  # одно плохое сообщение не должно ронять поток
                self.log(f"Ошибка обработки сообщения: {error}")
            self._housekeeping()

    def _housekeeping(self):
        now = time.monotonic()
        if now - self._last_housekeeping < 300:
            return
        self._last_housekeeping = now
        try:
            removed = self.store.prune_positions(self.config.get("history_days") or 7)
            if removed:
                self.log(f"История позиций: удалено старых записей {removed}.")
            self.store.prune_sea_traffic(48)
            # служебные объекты AIS (AtoN, базовые станции) — не суда
            stations = [str(row.get("mmsi")) for row in self.store.vessels(3000)
                        if self.is_station_mmsi(row.get("mmsi"))]
            if stations:
                self.store.purge_vessels(stations)
                self.log(f"Убраны служебные объекты AIS: {len(stations)}.")
            forgotten = self.store.forget_vessels(self.config.get("forget_vessels_hours") or 24)
            if forgotten:
                self.log(f"Убрано из списка судов без связи: {forgotten}.")
            added = self.sync_expected()
            if added:
                self.log(f"AIS-catcher: в базу добавлено ожидаемых судов {added}.")
            dropped = self.purge_unrelated()
            if dropped:
                self.log(f"Убрано судов вне наших портов: {dropped}.")
        except Exception as error:
            self.log(f"Обслуживание базы: {error}")

    def sync_expected(self) -> int:
        """Заносит суда из бесплатного AIS-catcher в базу — без координат.

        Портовые страницы AIS-catcher — единственный источник без ключа,
        который знает наши суда. Координат он не отдаёт, поэтому судно живёт
        в базе с пустой позицией: на карту оно не попадает, зато видно в
        списках приложения и в боте. Возвращает число новых судов.
        """
        try:
            stamp = portcalls.EXPECTED_PATH.stat().st_mtime
        except OSError:
            return 0
        if stamp == getattr(self, "_expected_stamp", None):
            return 0
        rows = portcalls.expected_rows()
        self._expected_stamp = stamp
        added = 0
        for row in rows:
            mmsi = str(row.get("mmsi") or "").strip()
            if not mmsi.isdigit():
                continue
            port = row.get("port") or row.get("locode")
            if not port:
                continue
            known = self.store.vessel(mmsi)
            values = {"destination": port}
            if not known:
                values["name"] = row.get("name") or None
                if row.get("type"):
                    values["type_label"] = row["type"]
                added += 1
            self.store.upsert_static(mmsi, **values)
            self.store.set_tracked(mmsi, True)
        return added

    def process(self, event):
        kind = event.get("kind")
        if kind == "confirmation":
            self.log(f"Подписка AIS подтверждена (сжатие: {'да' if event.get('compression') else 'нет'}).")
            return
        if kind == "static":
            self._process_static(event)
        elif kind == "position":
            self._process_position(event)

    @staticmethod
    def is_station_mmsi(mmsi) -> bool:
        """MMSI служебных объектов AIS, а не судов.

        99xxxxxxx — плавучие предупреждения (AtoN), 111xxxxxx — поисковые
        самолёты, 00xxxxxxx — береговые станции и базовые объекты.
        На карте они выгляели бы как стоящие в порту «суда» с именами
        вроде «OBSTN WITH DEPTH 17».
        """
        text = "".join(ch for ch in str(mmsi or "") if ch.isdigit())
        return (text.startswith("99") or text.startswith("111")
                or text.startswith("00") or text.startswith("000"))

    def _process_static(self, event):
        self.stats["static"] += 1
        mmsi = str(event.get("mmsi") or "")
        if self.is_station_mmsi(mmsi):
            return
        # Фильтр: пропускаем явно тестовые/демонстрационные суда
        name_cand = (event.get("name") or "").upper()
        if any(x in name_cand for x in ["TEST", "DEMO", "EXAMPLE", "SAMPLE"]) or mmsi in LEGACY_DEMO_MMSI:
            return
        before = self.store.vessel(mmsi)
        # в базу попадают только суда, связанные с нашими портами
        if not self._vessel_related(mmsi, None, None, before,
                                    destination=event.get("destination"), area=True):
            if before is not None:
                self.store.drop_vessel(mmsi)
            return
        code = event.get("type_code")
        self.store.upsert_static(
            mmsi,
            name=event.get("name") or None,
            imo=event.get("imo") or None,
            callsign=event.get("callsign") or None,
            type_code=code,
            type_label=ais_feed.type_label(code) if code is not None else None,
            destination=event.get("destination") or None,
            draft=event.get("draft"),
            length_m=event.get("length_m"),
            beam_m=event.get("beam_m"),
            eta_declared=event.get("eta_declared") or None,
        )
        self.store.set_tracked(mmsi, True)
        after = self.store.vessel(mmsi)
        if after and (before is None or before.get("name") != after.get("name")):
            self.log(f"Новое судно: {after.get('name') or mmsi} "
                     f"({after.get('type_label') or 'тип неизвестен'}), "
                     f"флаг: {flags.flag_text(mmsi)}, "
                     f"назначение: {after.get('destination') or '—'}.")

    def _process_position(self, event):
        self.stats["events"] += 1
        self.stats["last_event"] = event.get("at") or datetime.now()
        mmsi = event["mmsi"]
        if self.is_station_mmsi(mmsi):
            self.stats["skipped"] = self.stats.get("skipped", 0) + 1
            return
        lat, lon = event["lat"], event["lon"]

        port = geo.port_at(lat, lon, self.config.get("ports") or [])
        port_name = port["name"] if port else None
        vessel = self.store.vessel(mmsi)
        if not self._vessel_related(mmsi, lat, lon, vessel, area=True):
            # чужое судно: в базу не пишем, а ранее записанное — убираем,
            # но позицию сохраняем в трафик района для вкладки «В море»
            if vessel is not None:
                self.store.drop_vessel(mmsi)
            self._record_sea_traffic(mmsi, event, lat, lon, vessel)
            self.stats["skipped"] = self.stats.get("skipped", 0) + 1
            return
        if not vessel or vessel.get("tracked") != 1:
            self.store.set_tracked(mmsi, True)
        self.store.upsert_position(
            mmsi, lat, lon, event.get("sog"), event.get("cog"),
            event.get("heading"), port_name, event.get("at"))

        if event.get("name"):
            existing = self.store.vessel(mmsi)
            if existing and not existing.get("name"):
                self.store.upsert_static(mmsi, name=event["name"])

        last_stored = self._last_stored.get(mmsi)
        interval = float(self.config.get("position_interval_seconds") or 60)
        now_mono = time.monotonic()
        if last_stored is None or now_mono - last_stored >= interval:
            self._last_stored[mmsi] = now_mono
            self.store.add_position(mmsi, lat, lon, event.get("sog"), event.get("cog"),
                                    event.get("at"))
            self.stats["positions"] += 1

        self._track_geofence(mmsi, port_name, lat, lon, event.get("at"))
        self._maybe_alert(mmsi)

    def _record_sea_traffic(self, mmsi, event, lat, lon, vessel=None):
        """Копит чужой трафик района для вкладки «В море».

        Пишем не чаще раза в 10 минут на судно: снимок приходит сотнями судов,
        иначе база переписывалась бы на каждое сообщение.
        """
        cache = getattr(self, "_sea_written", None)
        if cache is None:
            cache = self._sea_written = {}
        now = time.monotonic()
        last = cache.get(mmsi)
        if last is not None and now - last < 600:
            return
        if len(cache) > 20000:
            cache.clear()
        cache[mmsi] = now
        when = event.get("at") or datetime.now()
        try:
            stamp = when.isoformat(timespec="seconds")
        except AttributeError:
            stamp = datetime.now().isoformat(timespec="seconds")
        vessel = vessel or {}
        try:
            self.store.save_sea_traffic([{
                "mmsi": mmsi,
                "name": event.get("name") or vessel.get("name"),
                "type_label": event.get("type_label") or vessel.get("type_label"),
                "destination": vessel.get("destination"),
                "sog": event.get("sog"),
                "cog": event.get("cog"),
                "lat": lat, "lon": lon,
                "updated_at": stamp,
            }])
        except Exception as error:
            self.log(f"Трафик района: запись не удалась ({error})")

    def _track_geofence(self, mmsi, port_name, lat, lon, when):
        visit = self.store.open_visit(mmsi)
        vessel = self.store.vessel(mmsi)
        name = (vessel or {}).get("name") or mmsi
        draft = (vessel or {}).get("draft")
        kind, _ = cargo.cargo_kind((vessel or {}).get("type_code"))
        if port_name:
            if not visit or visit["port"] != port_name:
                self.store.start_visit(mmsi, port_name, lat, lon, when,
                                       draft=draft, cargo_kind=kind, source="live")
                self.log(f"{name}: вход в порт {port_name}."
                         + (f" Осадка {draft:g} м — груз: {kind}." if draft and kind else ""))
        elif visit:
            self.store.close_visit(mmsi, when, draft_out=draft)
            self.log(f"{name}: вышел из порта {visit['port']}.")

    # ------------------------------------------------------------- фильтр
    def evaluate(self, mmsi: str):
        """Возвращает цель, если судно проходит фильтр, иначе None."""
        config = self.config
        vessel = self.store.vessel(mmsi)
        if not vessel:
            return None
        if not ais_feed.is_tanker_code(vessel.get("type_code")):
            return None
        destination = vessel.get("destination") or ""
        dest_hit = matched_port(destination, config.get("filter_destinations"))
        if not dest_hit:
            return None

        departure = self.store.last_departure(mmsi, config.get("trip_window_hours") or 96)
        if not departure:
            return None
        if not port_matches(departure["port"], config.get("filter_origins")):
            return None

        sog = vessel.get("sog") or 0
        if sog < float(config.get("min_speed_knots") or 0):
            return None

        # если судно опять стоит в порту отправления или уже пришло в порт назначения,
        # это не «цель в движении»
        current_port = vessel.get("port")
        if current_port:
            if port_matches(current_port, [departure["port"]]):
                return None
            if port_matches(current_port, config.get("filter_destinations")):
                return None

        country, iso2, emoji = flags.flag_for_mmsi(mmsi)
        distance_km = None
        eta_calc = None
        eta_hours = None
        port = geo.port_by_name(dest_hit, config.get("ports") or [])
        if port and sog:
            try:
                distance_km = geo.haversine_km(float(vessel.get("lat")), float(vessel.get("lon")),
                                               float(port["lat"]), float(port["lon"]))
            except (TypeError, ValueError, KeyError):
                distance_km = None
            if distance_km is not None and sog > 0:
                eta_hours = distance_km / (float(sog) * 1.852)
                eta_calc = datetime.now() + timedelta(hours=eta_hours)

        return {
            "mmsi": mmsi,
            "name": vessel.get("name") or mmsi,
            "imo": vessel.get("imo") or "",
            "country": country,
            "flag_iso": iso2,
            "flag_emoji": emoji,
            "eta_declared": vessel.get("eta_declared"),
            "eta_calc": eta_calc,
            "eta_hours": eta_hours,
            "distance_km": distance_km,
            "type_label": vessel.get("type_label") or "танкер",
            "origin": departure["port"],
            "departed_at": departure["left_at"],
            "destination": destination,
            "destination_port": dest_hit,
            "sog": sog,
            "draft": vessel.get("draft"),
            "lat": vessel.get("lat"),
            "lon": vessel.get("lon"),
            "updated_at": vessel.get("updated_at"),
            "key": f"{mmsi}|{departure['port']}|{dest_hit}",
        }

    def destination_port(self, vessel):
        """Порт назначения судна из AIS — любой известный порт, не только фильтр."""
        destination = (vessel or {}).get("destination") or ""
        if not destination:
            return None
        for port in self.config.get("ports") or []:
            if port_matches(destination, [port.get("name")]):
                if not port.get("label"):
                    # в настройках порт может быть сохранён без подписи
                    for default in geo.DEFAULT_PORTS:
                        if default["name"] == port.get("name"):
                            port = dict(port, label=default["label"])
                            break
                return port
        return None

    def voyage(self, vessel):
        """Сколько судну ещё идти до порта назначения.

        Возвращает словарь с портом, расстоянием по прямой, оставшимися часами
        и временем прибытия; None — если порт неизвестен или нет координат.
        """
        if not vessel:
            return None
        port = self.destination_port(vessel)
        if not port:
            return None
        lat, lon = vessel.get("lat"), vessel.get("lon")
        if lat is None or lon is None:
            return None
        try:
            distance = geo.haversine_km(float(lat), float(lon),
                                        float(port["lat"]), float(port["lon"]))
        except (KeyError, TypeError, ValueError):
            return None
        sog = vessel.get("sog") or 0
        hours = None
        eta = None
        if sog and float(sog) > 0.5:
            hours = distance / (float(sog) * 1.852)
            eta = datetime.now() + timedelta(hours=hours)
        in_port = bool(vessel.get("port")) and port_matches(
            vessel.get("port"), [port.get("name")])
        return {
            "port": port.get("name"),
            "port_label": port.get("label"),
            "distance_km": distance,
            "hours": hours,
            "eta": eta,
            "in_port": in_port,
        }

    def port_stay(self, vessel):
        """Сколько судно стоит (и будет стоять) в своём порту назначения.

        Время стоянки AIS не передаёт, поэтому считаем по своим наблюдениям:
        сколько уже стоит (открытый визит) и сколько обычно стоит — сначала
        по истории этого судна, а если её нет — по истории всех судов в порту.
        """
        if not vessel:
            return None
        port = self.destination_port(vessel)
        if not port:
            return None
        port_name = port.get("name")
        if not port_name:
            return None
        now = datetime.now()

        current_hours = None
        visit = self.store.open_visit(vessel["mmsi"])
        if visit and visit.get("port") == port_name:
            entered = parse_iso(visit.get("entered_at"))
            if entered:
                current_hours = max(0.0, (now - entered).total_seconds() / 3600)

        def average(rows):
            lengths = []
            for row in rows:
                started = parse_iso(row.get("entered_at"))
                ended = parse_iso(row.get("left_at"))
                if started and ended and ended > started:
                    lengths.append((ended - started).total_seconds() / 3600)
            if not lengths:
                return None, 0
            return sum(lengths) / len(lengths), len(lengths)

        own_average, own_count = average(
            self.store.ended_visits(port_name, mmsi=vessel["mmsi"]))
        port_average, port_count = average(self.store.ended_visits(port_name))
        if own_count:
            typical, visits, scope = own_average, own_count, "судно"
        else:
            typical, visits, scope = port_average, port_count, "порт"

        remaining = None
        if current_hours is not None and typical:
            remaining = max(0.0, typical - current_hours)

        return {
            "port": port_name,
            "port_label": port.get("label"),
            "current_hours": current_hours,
            "typical_hours": typical,
            "visits": visits,
            "scope": scope,
            "remaining_hours": remaining,
        }

    def history_stays(self, mmsi) -> list:
        """Стоянки в наших портах из всех доступных источников за прошлое.

        1) трек Open Waters — последние сутки, бесплатно и без ключа;
        2) наши собственные позиции — сколько дней хранится история;
        побеждает более полный вариант, дубликаты склеиваются по входу в порт.
        """
        ports = self.config.get("ports") or []
        stays = []
        try:
            points = ais_feed.fetch_track(mmsi)
            stays = ais_feed.stays_from_track(points, ports)
        except Exception:
            stays = []
        known = {(stay["port"], stay["entered_at"].isoformat(timespec="minutes"))
                 for stay in stays}
        for stay in self.local_stays(mmsi):
            key = (stay["port"], stay["entered_at"].isoformat(timespec="minutes"))
            if key in known:
                continue
            stays.append(stay)
            known.add(key)
        stays.sort(key=lambda stay: stay["entered_at"])
        return stays

    def local_stays(self, mmsi) -> list:
        """Стоянки по нашим позициям: работает дольше, чем суточный трек.

        Позиции пишутся каждые `position_interval_seconds`, поэтому по ним
        видно вход и выход из геозоны даже за несколько дней назад.
        """
        hours = float(self.config.get("history_days") or 7) * 24
        points = []
        for row in self.store.track(mmsi, hours):
            moment = parse_iso(row.get("ts"))
            if moment is None or row.get("lat") is None or row.get("lon") is None:
                continue
            points.append({"lat": row["lat"], "lon": row["lon"], "at": moment})
        return ais_feed.stays_from_track(points, self.config.get("ports") or [])

    def import_history_stays(self, limit=25, pause=0.4) -> dict:
        """Подтягивает стоянки из истории трека для судов в базе.

        Так появляются визиты, которые программа не видела сама: до её запуска
        или пока не было связи с потоком.
        """
        vessels = self.related_vessels(limit)
        checked = imported = failed = 0
        for vessel in vessels:
            stop = getattr(self, "stop_event", None)
            if stop is not None and stop.is_set():
                break
            mmsi = vessel.get("mmsi")
            if not mmsi:
                continue
            checked += 1
            try:
                stays = self.history_stays(mmsi)
            except Exception as error:
                failed += 1
                if failed == 1:
                    self.log(f"История стоянок: сервис ответил ошибкой ({error}).")
                continue
            kind, _ = cargo.cargo_kind(vessel.get("type_code"))
            for stay in stays:
                if self.store.import_visit(
                        mmsi, stay["port"], stay["entered_at"], stay["left_at"],
                        cargo_kind=kind, source="track"):
                    imported += 1
                    name = vessel.get("name") or mmsi
                    self.log(f"История: {name} стоял в {stay['port']} "
                             f"{duration_text(stay['hours'])}"
                             + (" и стоит сейчас." if stay.get("ongoing") else "."))
            if pause:
                time.sleep(pause)
        return {"checked": checked, "imported": imported, "failed": failed}

    def vessel_signals(self, vessel) -> list:
        """Признаки судна по наблюдениям: флаг, тёмные периоды, рейд, цикл.

        Считается в analysis.py по нашим позициям и визитам. Список может быть
        пустым: признаков нет или данных ещё мало.
        """
        if not vessel:
            return []
        mmsi = vessel.get("mmsi")
        if not mmsi:
            return []
        days = float(self.config.get("history_days") or 7)
        try:
            track = self.store.track(mmsi, days * 24)
            visits = self.store.visits(200, mmsi=mmsi)
        except Exception:
            return [analysis.flag_note(mmsi)] if analysis.flag_note(mmsi) else []
        return analysis.signals(vessel, track, visits,
                                self.config.get("ports") or [], days)

    def stale_hours(self) -> float:
        """Через сколько часов без AIS данные судна считаются устаревшими."""
        try:
            minutes = float(self.config.get("stale_minutes") or 0)
        except (TypeError, ValueError):
            minutes = 15.0
        return max(0.0, minutes) / 60.0

    def vessel_math(self, vessel) -> list:
        """Расчёты по судну: расстояния, скорости, корпус, стоянка, AIS, ETA.

        Всё считается в metrics.py по нашим позициям, визитам и статике судна;
        недостающие данные не додумываются — об этом пишется отдельной строкой.
        """
        if not vessel or not vessel.get("mmsi"):
            return []
        mmsi = vessel["mmsi"]
        days = float(self.config.get("history_days") or 7)
        try:
            track = self.store.track(mmsi, days * 24)
            visits = self.store.visits(200, mmsi=mmsi)
        except Exception:
            track, visits = [], []
        try:
            stay = self.port_stay(vessel)
        except Exception:
            stay = None
        try:
            voyage = self.voyage(vessel)
        except Exception:
            voyage = None
        return metrics.report(vessel, track, visits, stay, voyage, days * 24)

    def cargo_estimate(self, vessel):
        """Оценка груза по осадке и габаритам корпуса — см. cargo.py."""
        if not vessel:
            return None
        draft = vessel.get("draft")
        length = vessel.get("length_m")
        beam = vessel.get("beam_m")
        if not draft or not length:
            return None
        if not beam:
            # ширину корпуса AIS часто не передаёт: берём типичную для этой длины
            beam = self.store.beam_reference(length)
            if not beam:
                return None
            estimate = cargo.estimate(length, beam, draft,
                                     draft_max=vessel.get("draft_max"),
                                     reference_max=self.store.draft_reference(length),
                                     type_code=vessel.get("type_code"))
            if estimate:
                estimate["beam_source"] = "типичная ширина для такой длины"
            return estimate
        return cargo.estimate(length, beam, draft,
                              draft_max=vessel.get("draft_max"),
                              reference_max=self.store.draft_reference(length),
                              type_code=vessel.get("type_code"))

    def targets(self) -> list:
        result = []
        for vessel in self.store.vessels(1000):
            target = self.evaluate(vessel["mmsi"])
            if target:
                result.append(target)
        result.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return result

    def _telegram_ready(self) -> bool:
        return bool((self.config.get("telegram_token") or "").strip()
                    and str(self.config.get("telegram_chat_id") or "").strip())

    def _maybe_alert(self, mmsi):
        if not self.alerts_enabled:
            return
        target = self.evaluate(mmsi)
        if not target:
            return
        key = target["key"]
        cooldown = float(self.config.get("alert_cooldown_hours") or 0)
        if self.store.alert_recent(key, cooldown):
            return
        # не повторяем ни алерт, ни ошибку отправки чаще, чем раз в паузу
        guard = max(300.0, (cooldown if cooldown > 0 else 1) * 3600)
        last_try = self._local_alerts.get(key)
        if last_try and time.monotonic() - last_try < guard:
            return

        text = build_alert_text(target)
        if not self._telegram_ready():
            self._local_alerts[key] = time.monotonic()
            self.store.log_alert(key, target["mmsi"], target["name"], target["origin"],
                                 target["destination"], False,
                                 "только в окне: Telegram не настроен", text)
            self.stats["alerts"] += 1
            self.log(f"ЦЕЛЬ: {target['name']} {target['origin']} → {target['destination']} "
                     f"(Telegram не настроен — алерт показан в окне).")
            self.on_alert({**target, "ok": False, "error": "только в окне: Telegram не настроен",
                           "text": text})
            return

        # отправка — в отдельном потоке: приём AIS не ждёт ответа Telegram
        self._local_alerts[key] = time.monotonic()
        self._alert_queue.put((target, text))

    def _send_alert(self, target, text):
        key = target["key"]
        try:
            send_telegram(self.config.get("telegram_token"),
                          self.config.get("telegram_chat_id"), text)
        except AppError as error:
            self.store.log_alert(key, target["mmsi"], target["name"], target["origin"],
                                 target["destination"], False, str(error), text)
            self.log(f"Алерт по {target['name']} не отправлен: {error}")
            self.on_alert({**target, "ok": False, "error": str(error), "text": text})
            return

        self.store.log_alert(key, target["mmsi"], target["name"], target["origin"],
                             target["destination"], True, None, text)
        self.stats["alerts"] += 1
        self.log(f"Алерт отправлен: {target['name']} {target['origin']} → {target['destination']}.")
        self.on_alert({**target, "ok": True, "error": "", "text": text})

    # ------------------------------------------------------------- отчёты
    def status_text(self) -> str:
        state = self.feed_state()
        if not state["running"]:
            status = "остановлено"
        elif state["connected"]:
            status = "на связи"
        else:
            status = "переподключение"
        counts = self.store.counts()
        targets = self.targets()
        config = self.config
        last_event = self.stats.get("last_event")
        last_text = f"{last_event:%d.%m.%Y %H:%M:%S}" if last_event else "—"
        return "\n".join([
            "📡 Ship Tracker",
            f"Состояние: {status}, источник: живой AISStream (реальные суда)",
            f"Судов в базе: {counts['vessels']}, целей сейчас: {len(targets)}",
            f"Алертов в Telegram сегодня: {self.store.alerts_count_today()}",
            f"Алертов поднято за сеанс: {self.stats['alerts']}",
            f"Фильтр: танкер {'/'.join(config.get('filter_origins') or [])} → "
            f"{'/'.join(config.get('filter_destinations') or [])}",
            f"Последнее сообщение AIS: {last_text}",
        ])


# ------------------------------------------------------------- команды в боте

COMMAND_HELP = (
    "Команды Ship Tracker:\n"
    "/status — состояние и статистика\n"
    "/last — последние алерты\n"
    "/mute — не присылать алерты\n"
    "/unmute — снова присылать алерты\n"
    "/id — узнать chat_id этого чата"
)


def _claim_lock(path: Path):
    """Неблокирующая блокировка файла: None, если её уже держит другой процесс."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")
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


def claim_telegram_lock(store: Store):
    """Занимает data/bot.lock: два поллера на один токен Telegram не работают."""
    return _claim_lock(Path(store.path).parent / "bot.lock")


def claim_feed_lock(store: Store):
    """Занимает data/feed.lock: AIS-поток должен принимать один процесс."""
    return _claim_lock(Path(store.path).parent / "feed.lock")


class BotCommander(threading.Thread):
    """Слушает Telegram и выполняет команды владельца."""

    def __init__(self, config: dict, store: Store, core: RadarCore, log=None):
        super().__init__(daemon=True, name="tg-commands")
        self.config = config
        self.store = store
        self.core = core
        self.log = log or (lambda message: None)
        self.stop_event = threading.Event()
        self.offset = int(store.kv_get("tg_offset", "0") or 0)
        self._lock = None

    def reply(self, chat_id, text: str):
        """Ответ в чат: ошибки только пишем в журнал, поток команд не должен умирать."""
        token = (self.config.get("telegram_token") or "").strip()
        try:
            send_telegram(token, chat_id, text)
        except AppError as error:
            self.log(f"Команды Telegram: ответ не отправлен: {error}")

    def run(self):
        self._lock = claim_telegram_lock(self.store)
        if self._lock is None:
            self.log("Команды в Telegram: уже работает отдельный бот — в приложении "
                     "опрос выключен (бот отвечает на команды и порты).")
            return
        self.log("Команды в Telegram включены.")
        while not self.stop_event.is_set():
            token = (self.config.get("telegram_token") or "").strip()
            if not token:
                self.stop_event.wait(5)
                continue
            try:
                updates = telegram_call(token, "getUpdates",
                                        params={"offset": self.offset, "timeout": 25},
                                        timeout=40) or []
            except AppError as error:
                if self.stop_event.is_set():
                    break
                self.log(f"Команды Telegram: {error}")
                self.stop_event.wait(15)
                continue
            except Exception as error:
                if self.stop_event.is_set():
                    break
                self.log(f"Команды Telegram: {error.__class__.__name__}: {error}")
                self.stop_event.wait(30)
                continue
            for update in updates:
                self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
                self.store.kv_set("tg_offset", self.offset)
                try:
                    self.handle(update)
                except AppError as error:
                    self.log(f"Команды Telegram: {error}")
                except Exception as error:
                    self.log(f"Команды Telegram: сбой обработки "
                             f"({error.__class__.__name__}: {error})")

    def handle(self, update):
        message = update.get("message") or update.get("edited_message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if not chat_id or not text.startswith("/"):
            return
        command = text.split()[0].split("@")[0].lower()

        if command in ("/id", "/chatid"):
            self.reply(chat_id, f"chat_id этого чата: {chat_id}")
            return

        owner = str(self.config.get("telegram_chat_id") or "").strip()
        if owner and str(chat_id) != owner:
            self.reply(chat_id, "Доступ запрещён: этот чат не указан в настройках.")
            return

        if command in ("/start", "/help"):
            self.reply(chat_id, COMMAND_HELP)
        elif command == "/status":
            self.reply(chat_id, self.core.status_text())
        elif command == "/last":
            rows = self.store.alerts(10)
            if not rows:
                self.reply(chat_id, "Алертов пока нет.")
                return
            lines = ["Последние алерты:"]
            for row in rows:
                mark = "✅" if row["ok"] else "⚠️"
                lines.append(f"{mark} {row['sent_at'][11:16]} {row['name']} — "
                             f"{row['origin']} → {row['destination']}")
            self.reply(chat_id, "\n".join(lines))
        elif command == "/mute":
            self.core.alerts_enabled = False
            self.reply(chat_id, "Алерты выключены. /unmute — включить.")
        elif command == "/unmute":
            self.core.alerts_enabled = True
            self.reply(chat_id, "Алерты включены.")
        else:
            self.reply(chat_id, COMMAND_HELP)
