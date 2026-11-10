"""Telegram-бот Ship Tracker.

Сценарий:
1. /start — приветствие и клавиатура с портами.
2. Нажали порт — бот показывает все суда, которые идут в этот порт.
   Карточка судна содержит те же данные, что и в приложении: флаг, тип,
   назначение, скорость, габариты, курс, осадку, координаты, время
   обновления, расчёт прибытия, заявленный ETA, стоянку и оценку груза.
3. Выбрали другой порт — прошлая выдача удаляется из чата.
4. Любое другое сообщение — снова клавиатура с портами.

Данные берутся из той же базы, что и приложение (data/ship_tracker.db).
Порт назначения распознаётся по кодам UN/LOCODE из geo.PORT_ALIASES.
Список отправленных сообщений хранится в data/bot_messages.json, поэтому
старая выдача удаляется даже после перезапуска бота.
"""

import asyncio
import html
import importlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message, ReplyKeyboardMarkup
from aiogram.utils.keyboard import ReplyKeyboardBuilder

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "data" / "config.json"
DB_PATH = BASE_DIR / "data" / "ship_tracker.db"
PHOTO_DIR = BASE_DIR / "data" / "photos"
SENT_PATH = BASE_DIR / "data" / "bot_messages.json"
LOCK_PATH = BASE_DIR / "data" / "bot.lock"
EXPECTED_SEEN_PATH = BASE_DIR / "data" / "aci_seen.json"
MAP_PATH = BASE_DIR / "data" / "map.png"

CAPTION_LIMIT = 1024
MAP_BUTTON = "🗺 Карта"
ALL_BUTTON = "🚢 Все суда"
SEA_BUTTON = "🌊 В море"
VISITS_BUTTON = "⚓ Стоянки"
HISTORY_BUTTON = "🕘 История стоянок"
PLAN_BUTTON = "📅 Прогноз 4 дня"
EXPECTED_BUTTON = "⏱ Ожидаемые"
ALERTS_BUTTON = "🚨 Алерты"
SOURCES_BUTTON = "🔌 Источники"
CONNECTION_BUTTON = "🔑 Подключение"
FILTERS_BUTTON = "🎛 Фильтры"
TELEGRAM_BUTTON = "🤖 Telegram"
SERVICES_BUTTON = "🛠 Сервисы"
LOG_BUTTON = "📜 Журнал"

# Пункты меню — те же, что вкладки в приложении (порядок как у вкладок)
MENU_BUTTONS: List[str] = [
    MAP_BUTTON, ALL_BUTTON, SEA_BUTTON, PLAN_BUTTON, VISITS_BUTTON,
    HISTORY_BUTTON, EXPECTED_BUTTON, ALERTS_BUTTON, CONNECTION_BUTTON,
    FILTERS_BUTTON, TELEGRAM_BUTTON, SERVICES_BUTTON, LOG_BUTTON, SOURCES_BUTTON,
]

STARTED_AT = datetime.now()

DEFAULT_PORT_NAMES: List[str] = [
    "RENI", "IZMAIL", "VYLKOVE", "CHORNOMORSK", "ODESSA", "YUZHNY",
]

log = logging.getLogger("telegram_bot")


# ── конфигурация и база ────────────────────────────────────────────────────
def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


CONFIG: dict = load_config()

_store = None
_core = None
_lock_handle = None


def reload_config() -> dict:
    """Перечитывает config.json: настройки меняются в приложении на ходу."""
    global CONFIG
    try:
        CONFIG = load_config()
    except (OSError, ValueError):
        pass
    if _core is not None:
        _core.config = CONFIG
    return CONFIG


def _import(name):
    """Модуль приложения: и при запуске скриптом, и при запуске пакетом."""
    try:
        return importlib.import_module(name)
    except ImportError:
        return importlib.import_module(f"ship_tracker.{name}")


def get_store():
    global _store
    if _store is None:
        store_module = _import("store")
        _store = store_module.Store(str(DB_PATH))
    return _store


def get_core():
    """Движок приложения: те же расчёты рейса, стоянки и груза."""
    global _core
    if _core is None:
        core_module = _import("core")
        # без журнала ошибки AIS и внешних сервисов молчали бы: в фоне их
        # не видно ни в окне, ни в консоли
        _core = core_module.RadarCore(CONFIG, get_store(), log=logging.info)
    return _core


# ── клавиатура ─────────────────────────────────────────────────────────────
def make_port_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура с портами и пунктами меню, как вкладки приложения."""
    builder = ReplyKeyboardBuilder()
    for name in DEFAULT_PORT_NAMES:
        builder.button(text=name)
    for text in MENU_BUTTONS:
        builder.button(text=text)
    # по три кнопки в ряд: портов и пунктов меню стало много, иначе
    # клавиатура занимает весь экран
    builder.adjust(3)
    return builder.as_markup(resize_keyboard=True)


# ── учёт отправленных сообщений ────────────────────────────────────────────
def _load_sent() -> dict:
    try:
        with open(SENT_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {str(chat): [int(mid) for mid in ids] for chat, ids in (raw or {}).items()}
    except (OSError, ValueError, TypeError):
        return {}


def _save_sent(data: dict) -> None:
    tmp = SENT_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, SENT_PATH)
    except OSError:
        pass


_sent: dict = _load_sent()


def remember(chat_id: int, message) -> None:
    if message is None:
        return
    key = str(chat_id)
    _sent.setdefault(key, []).append(int(message.message_id))
    _save_sent(_sent)


async def clear_previous(bot: Bot, chat_id: int) -> None:
    """Удаляет сообщения прошлой выдачи, чтобы остался только новый порт."""
    ids = _sent.pop(str(chat_id), [])
    if not ids:
        return
    for message_id in reversed(ids):
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception as error:
            # старше 48 часов или уже удалено — просто пропускаем
            log.info("Сообщение %s не удалено: %s", message_id, error)
    _save_sent(_sent)


async def try_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


# ── выборка судов ──────────────────────────────────────────────────────────
def _port_targets(port_name: str, config: dict) -> Tuple[str, ...]:
    """Все написания порта в поле Destination: имя и коды UN/LOCODE."""
    try:
        geo = _import("geo")
        aliases = geo.port_aliases(config.get("ports") or [])
        return aliases.get(port_name.upper(), (port_name.upper(),))
    except Exception:
        return (port_name.upper(),)


def _matches(value: str, targets: Tuple[str, ...]) -> bool:
    try:
        return _import("core").port_matches(value, list(targets))
    except Exception:
        text = (value or "").upper()
        return any(t and t in text for t in targets)


def size_text(vessel: dict) -> str:
    """Габариты как в приложении: «107 × 18 м», «—» если неизвестны."""
    length = vessel.get("length_m")
    beam = vessel.get("beam_m")
    try:
        length = float(length) if length else None
        beam = float(beam) if beam else None
    except (TypeError, ValueError):
        return "—"
    if length and beam:
        return f"{length:g} × {beam:g} м"
    if length:
        return f"{length:g} м"
    if beam:
        return f"ширина {beam:g} м"
    return "—"


def get_vessels_for_port(port_name: str) -> List[dict]:
    """Все суда, идущие в указанный порт (любых типов)."""
    reload_config()
    port_name = (port_name or "").strip().upper()
    if port_name not in DEFAULT_PORT_NAMES:
        return []

    config = CONFIG
    geo = _import("geo")
    port = geo.port_by_name(port_name, config.get("ports") or [])
    if not port:
        return []

    targets = _port_targets(port_name, config)
    vessels: List[dict] = []

    for vessel in get_store().vessels(limit=5000):
        if not vessel.get("mmsi"):
            continue
        if not _matches(vessel.get("destination") or "", targets):
            continue
        # уже стоит в целевом порту — это не «идёт в порт»
        if _matches(vessel.get("port") or "", (port_name,)):
            continue
        vessels.append(vessel)

    def sort_key(vessel):
        try:
            distance = geo.haversine_km(float(vessel.get("lat")), float(vessel.get("lon")),
                                        float(port["lat"]), float(port["lon"]))
        except (TypeError, ValueError, KeyError):
            distance = None
        fast = distance is None or distance == float("inf")
        return (fast, 0.0 if fast else distance, -float(vessel.get("sog") or 0))

    vessels.sort(key=sort_key)
    return vessels


def route_text(vessel: dict) -> str:
    """Маршрут координатами: где судно сейчас и где будет через 1–3 суток."""
    ports = CONFIG.get("ports") or []
    try:
        forecast = _import("forecast")
        points = forecast.route(vessel, ports)
    except Exception as error:
        log.info("Прогноз для %s не построен: %s", vessel.get("mmsi"), error)
        return "🗺 Маршрут: прогноз не построен"

    if len(points) < 2:
        sog = vessel.get("sog") or 0
        try:
            sog = float(sog)
        except (TypeError, ValueError):
            sog = 0.0
        if sog < 0.5:
            return "🗺 Маршрут: судно почти не движется — прогноз не строится"
        return "🗺 Маршрут: нет курса в AIS — прогноз не строится"

    port = forecast.target_port(vessel, ports)
    speed, course, _source = forecast.estimates(vessel)
    if port:
        head = (f"🗺 Маршрут (прогноз до нашего порта, до "
                f"{int(forecast.MAX_DAYS)} сут):")
    else:
        head = (f"🗺 Маршрут (прогноз по курсу {forecast.course_text(course)}, "
                f"{float(speed or 0):.1f} уз, до {int(forecast.MAX_DAYS)} сут):")
    lines = [head]
    daily = [point for point in forecast.daily_points(points) if point["hours"] > 0]
    for point in daily:
        suffix = f" ({point['port']})" if point.get("port") else ""
        when = f"{point['at']:%d.%m %H:%M}" if point.get("at") else ""
        lines.append(f"  {point['label']}: {point['lat']:.3f}, {point['lon']:.3f}"
                     + (f" · {when}" if when else "") + suffix)
    if not port:
        lines.append("  конечная точка — по текущему курсу; при повороте сдвинется")
    return "\n".join(lines)


def vessel_card(vessel: dict, core) -> str:
    """Карточка судна — то же, что показывает приложение."""
    core_module = _import("core")
    store_module = _import("store")
    flags = _import("flags")
    ais_feed = _import("ais_feed")
    cargo = _import("cargo")
    try:
        portcalls = _import("portcalls")
    except Exception:
        portcalls = None

    mmsi = str(vessel.get("mmsi"))
    _country, iso2, _emoji = flags.flag_for_mmsi(mmsi)

    def section(title, rows):
        """Блок карточки: заголовок и только заполненные строки."""
        rows = [row for row in rows if row]
        return ([f"<b>{title}</b>"] + rows + [""]) if rows else []

    def field(label, value, suffix=""):
        """Строка «поле: значение» или None, чтобы не было прочерков."""
        text = "" if value is None else str(value).strip()
        if not text or text in {"—", "None"}:
            return None
        return f"{label}: {text}{suffix}"

    type_code = vessel.get("type_code")
    type_text = ais_feed.type_detail(type_code) if type_code is not None else ""
    if not type_text or type_text == "—":
        type_text = vessel.get("type_label") or ""
    if type_code is not None and type_text:
        type_text = f"{type_text} (AIS {type_code})"

    lines = [f"🚢 <b>{vessel.get('name') or 'судно без имени'}</b>", ""]
    lines += section("Судно", [
        f"MMSI: {mmsi}",
        f"Флаг: {flags.flag_text(mmsi)}" + (f" ({iso2})" if iso2 else ""),
        field("IMO", vessel.get("imo")),
        field("Позывной", vessel.get("callsign")),
    ])

    gfw_entry = portcalls.gfw_vessel(None, mmsi) if portcalls is not None else None
    length = vessel.get("length_m")
    beam = vessel.get("beam_m")
    sizes = size_text(vessel)
    if sizes == "—" and gfw_entry and gfw_entry.get("length_m"):
        sizes = f"{gfw_entry['length_m']:g} м (GFW)"
    lines += section("Что за судно", [
        field("Тип", type_text),
        field("Размеры", None if sizes == "—" else sizes),
        field("Осадка", core_module.fmt_number(vessel.get("draft")) if vessel.get("draft") else None, " м"),
        field("Тоннаж", gfw_entry.get("tonnage_gt") if gfw_entry else None, " GT"),
        field("Макс. скорость", gfw_entry.get("max_speed_kn") if gfw_entry else None, " уз"),
    ])

    position = None
    if vessel.get("lat") is not None and vessel.get("lon") is not None:
        position = (f"{core_module.fmt_number(vessel.get('lat'))}, "
                    f"{core_module.fmt_number(vessel.get('lon'))}")
    sog = vessel.get("sog")
    lines += section("Сейчас", [
        field("Скорость", f"{core_module.fmt_number(sog)} уз" if sog is not None else None),
        field("Курс", f"{core_module.fmt_number(vessel.get('cog'))}°" if vessel.get("cog") not in (None, -1) else None),
        field("Координаты", position),
        field("В порту", vessel.get("port")),
        f"Обновлено: {core_module.fmt_moment(store_module.parse_iso(vessel.get('updated_at')))}",
    ])

    voyage_rows = [field("Назначение", vessel.get("destination")), route_text(vessel)]
    voyage = core.voyage(vessel)
    if voyage:
        port_label = voyage.get("port_label") or voyage.get("port")
        if voyage.get("in_port"):
            voyage_rows.append(f"До прибытия: уже в порту {port_label}")
        elif voyage.get("hours") is not None:
            voyage_rows.append(
                f"До прибытия: {core_module.duration_text(voyage['hours'])} "
                f"({core_module.fmt_moment(voyage['eta'])}), "
                f"{core_module.fmt_number(voyage['distance_km'])} км до {port_label}")
        else:
            voyage_rows.append(
                f"До прибытия: скорость слишком мала для расчёта "
                f"({core_module.fmt_number(voyage['distance_km'])} км до {port_label})")
    declared = store_module.parse_iso(vessel.get("eta_declared"))
    if declared:
        voyage_rows.append(f"Срок прибытия (AIS): {core_module.fmt_moment(declared)} "
                           f"(передаёт капитан, в UTC)")
    if portcalls is not None:
        expected = portcalls.expected_for_mmsi(mmsi)
        if expected:
            voyage_rows.append(f"Ожидается в {expected.get('port')}: заявленный ETA "
                               f"{expected.get('eta') or 'не указан'} (AIS-catcher)")
    lines += section("Рейс", voyage_rows)

    if portcalls is not None and gfw_entry:
        checked = str(gfw_entry.get("checked") or "")[:16].replace("T", " ")
        lines += section("Проверка по реестрам", [
            f"Паспорт GFW получен" + (f": {checked}" if checked else ""),
            portcalls.gfw_gap_text(gfw_entry),
        ])
    try:
        metrics = _import("metrics")
    except Exception:
        metrics = None
    if metrics is not None:
        stale_hours = (core.stale_hours() if hasattr(core, "stale_hours") else 0.25)
        age = metrics.freshness_hours(vessel.get("updated_at"))
        if age is not None and stale_hours and age > stale_hours:
            lines.append(f"⚠ Данные устарели: AIS не поступал {core_module.span_text(age)} — "
                         f"скорость, курс и позиция могут быть неактуальны.")

    stay = core.port_stay(vessel)
    stay_rows = []
    if stay:
        stay_port = stay.get("port_label") or stay.get("port")
        parts = []
        if stay.get("current_hours") is not None:
            parts.append(f"стоит {core_module.span_text(stay['current_hours'])}")
        if stay.get("typical_hours"):
            source = ("по истории судна" if stay.get("scope") == "судно"
                      else "по истории порта")
            parts.append(f"обычно {core_module.span_text(stay['typical_hours'])} "
                         f"({source}, визитов: {stay['visits']})")
        if stay.get("remaining_hours") is not None:
            parts.append(f"осталось примерно {core_module.span_text(stay['remaining_hours'])}")
        stay_rows.append(f"Стоянка в {stay_port}: "
                         + ("; ".join(parts) if parts else "наблюдений пока нет"))

    cargo_info = core.cargo_estimate(vessel)
    if cargo_info:
        share = cargo_info.get("load_share")
        if share is None:
            stay_rows.append(f"Груз (оценка): ≈ {core_module.fmt_tons(cargo_info['cargo_t'])} т")
        else:
            stay_rows.append(f"Груз (оценка): ≈ {core_module.fmt_tons(cargo_info['cargo_t'])} т "
                             f"({round(share * 100)} % дедвейта ≈ "
                             f"{core_module.fmt_tons(cargo_info['deadweight_t'])} т)")
    elif vessel.get("draft"):
        stay_rows.append("Груз (оценка): нет длины и ширины корпуса — "
                         "судно их ещё не передавало")

    kind, kind_detail = cargo.cargo_kind(vessel.get("type_code"))
    if kind:
        stay_rows.append(f"Что везёт: {kind} — {kind_detail}")
    lines += section("Стоянка и груз", stay_rows)

    signals = core.vessel_signals(vessel) if hasattr(core, "vessel_signals") else []
    if signals:
        lines.append("<b>Признаки</b> (по наблюдениям программы):")
        lines += [f"• {signal}" for signal in signals]

    math_lines = core.vessel_math(vessel) if hasattr(core, "vessel_math") else []
    if math_lines:
        lines.append("<b>Расчёты по данным наблюдений:</b>")
        lines += [f"• {line}" for line in math_lines]

    if vessel.get("imo") or mmsi:
        lines.append(f"VesselFinder: https://www.vesselfinder.com/vessels/details/"
                     f"{vessel.get('imo') or mmsi}")
    if portcalls is not None:
        lines.append("<b>Проверить в реестрах:</b>")
        for title, url in portcalls.registry_links(vessel.get("imo"), mmsi)[:3]:
            lines.append(f'• <a href="{url}">{title}</a>')
        lines.append("<b>Карты судов:</b>")
        for title, url in portcalls.tracking_links(mmsi, vessel.get("name"))[:4]:
            lines.append(f'• <a href="{url}">{title}</a>')
    return "\n".join(lines)


def _fetch_photo(vessel: dict) -> Optional[Path]:
    try:
        photos = _import("photos")
        return photos.fetch(str(vessel.get("mmsi")), vessel.get("imo") or None,
                            PHOTO_DIR, timeout=15)
    except Exception:
        return None


# ── выдача списка и её автообновление ──────────────────────────────────────
# Как часто перерисовывать карточки: позиция и скорость в AIS меняются часто.
REFRESH_SECONDS = 120
# Один чат — один открытый порт: что показано, из каких сообщений состоит.
_chats: dict = {}


def header_text(port_name: str, vessels: List[dict]) -> str:
    store_module = _import("store")
    moments = [store_module.parse_iso(v.get("updated_at")) for v in vessels]
    moments = [m for m in moments if m]
    lines = [f"📍 <b>Порт {port_name}</b>",
             f"Всего судов: <b>{len(vessels)}</b>"]
    if not vessels:
        lines.append("Судов, идущих в этот порт, сейчас нет.")
    if moments:
        lines.append(f"Данные AIS: {max(moments):%d.%m %H:%M} · "
                     f"обновлено {datetime.now():%H:%M:%S}")
    else:
        lines.append(f"Обновлено {datetime.now():%H:%M:%S}")
    return "\n".join(lines)


def vessel_text(vessel: dict, core) -> str:
    try:
        return vessel_card(vessel, core)
    except Exception as error:
        log.warning("Карточка %s не собрана: %s", vessel.get("name"), error)
        return (f"🚢 <b>{vessel.get('name') or vessel.get('mmsi')}</b>\n"
                f"MMSI: {vessel.get('mmsi')}\n"
                f"Тип: {vessel.get('type_label') or '—'}\n"
                f"Назначение: {vessel.get('destination') or '—'}\n"
                f"Скорость: {vessel.get('sog') or 0} уз\n"
                f"Размеры: {size_text(vessel)}\n"
                f"Координаты: {vessel.get('lat')}, {vessel.get('lon')}")


async def load_photos(vessels: List[dict]) -> List[Optional[Path]]:
    """Фото пачкой, но не больше 4 запросов одновременно."""
    limit = asyncio.Semaphore(4)

    async def one(vessel):
        async with limit:
            return await asyncio.to_thread(_fetch_photo, vessel)

    return await asyncio.gather(*(one(v) for v in vessels))


async def send_vessel(bot: Bot, chat_id: int, vessel: dict, core,
                      photo: Optional[Path] = None) -> dict:
    """Отправляет фото и карточку, возвращает id сообщений для обновления."""
    text = vessel_text(vessel, core)
    item = {"photo": None, "text": None}
    if photo and Path(photo).exists():
        caption = text if len(text) <= CAPTION_LIMIT else None
        try:
            sent = await bot.send_photo(chat_id=chat_id,
                                        photo=types.FSInputFile(str(photo)),
                                        caption=caption)
            item["photo"] = sent.message_id
            remember(chat_id, sent)
            if caption is None:
                extra = await bot.send_message(chat_id, text, disable_web_page_preview=True)
                item["text"] = extra.message_id
                remember(chat_id, extra)
            return item
        except Exception as error:
            log.warning("Фото %s не отправлено: %s", vessel.get("name"), error)

    sent = await bot.send_message(chat_id, text, disable_web_page_preview=True)
    item["text"] = sent.message_id
    remember(chat_id, sent)
    return item


async def update_vessel(bot: Bot, chat_id: int, item: dict, vessel: dict, core) -> None:
    """Переписывает карточку на месте: позиция, скорость, прогноз, ETA."""
    text = vessel_text(vessel, core)
    try:
        if item.get("text"):
            await bot.edit_message_text(text=text, chat_id=chat_id,
                                        message_id=item["text"],
                                        disable_web_page_preview=True)
        elif item.get("photo"):
            if len(text) <= CAPTION_LIMIT:
                await bot.edit_message_caption(chat_id=chat_id,
                                               message_id=item["photo"],
                                               caption=text)
            else:
                extra = await bot.send_message(chat_id, text, disable_web_page_preview=True)
                item["text"] = extra.message_id
                remember(chat_id, extra)
    except Exception as error:
        if "not modified" not in str(error).lower():
            log.info("Карточка %s не обновлена: %s", vessel.get("name"), error)


def forget_message(chat_id: int, message_id: int) -> None:
    ids = _sent.get(str(chat_id))
    if ids and message_id in ids:
        ids.remove(message_id)
        _save_sent(_sent)


def cancel_refresher(chat_id: int) -> None:
    state = _chats.pop(chat_id, None)
    if state and state.get("task"):
        state["task"].cancel()


async def refresher(bot: Bot, chat_id: int, port_name: str) -> None:
    """Каждые две минуты обновляет карточки по свежим данным AIS."""
    while True:
        await asyncio.sleep(REFRESH_SECONDS)
        state = _chats.get(chat_id)
        if not state or state.get("port") != port_name:
            return
        try:
            vessels = await asyncio.to_thread(get_vessels_for_port, port_name)
        except Exception as error:
            log.warning("Обновление порта %s: %s", port_name, error)
            continue
        state = _chats.get(chat_id)
        if not state or state.get("port") != port_name:
            return
        core = get_core()

        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=state["header"],
                                        text=header_text(port_name, vessels))
        except Exception as error:
            if "not modified" not in str(error).lower():
                log.info("Заголовок порта %s не обновлён: %s", port_name, error)

        by_mmsi = {str(v.get("mmsi")): v for v in vessels}
        for mmsi in list(state["items"]):
            if mmsi not in by_mmsi:
                item = state["items"].pop(mmsi)
                for mid in (item.get("photo"), item.get("text")):
                    if mid:
                        await try_delete(bot, chat_id, mid)
                        forget_message(chat_id, mid)

        existing = [v for v in vessels if str(v.get("mmsi")) in state["items"]]
        if existing:
            await asyncio.gather(*(update_vessel(bot, chat_id,
                                                 state["items"][str(v["mmsi"])], v, core)
                                   for v in existing))

        for vessel in vessels:
            mmsi = str(vessel.get("mmsi"))
            if mmsi in state["items"]:
                continue
            photo = await asyncio.to_thread(_fetch_photo, vessel)
            state["items"][mmsi] = await send_vessel(bot, chat_id, vessel, core, photo)


# ── обработчики ────────────────────────────────────────────────────────────
def _config_ready() -> bool:
    return bool((reload_config().get("telegram_token") or "").strip())


async def cmd_start(message: Message):
    cancel_refresher(message.chat.id)
    await clear_previous(message.bot, message.chat.id)
    if not _config_ready():
        await message.answer(
            "⚙️ В конфигурации не задан <code>telegram_token</code>.\n"
            "Добавьте его в <code>data/config.json</code> и перезапустите бота."
        )
        return
    sent = await message.answer(
        "🛳 <b>Ship Radar Bot</b>\n\n"
        "Выберите порт — покажу все суда, которые в него идут.\n"
        "Или нажмите «🗺 Карта» — пришлю картинку со всеми судами.",
        reply_markup=make_port_keyboard(),
    )
    remember(message.chat.id, sent)


async def cmd_help(message: Message):
    await message.answer(
        "🆘 <b>Справка</b>\n\n"
        "Кнопки меню — те же пункты, что вкладки в приложении.\n"
        "Нажмите кнопку порта — бот покажет все суда, идущие в этот порт, "
        "с теми же данными, что и в приложении.\n"
        "В карточке есть маршрут: текущие координаты и прогноз на 3 дня "
        "(в наш порт или по текущему курсу).\n"
        "Карточки обновляются сами каждые 2 минуты.\n"
        "При выборе другого порта прошлый список удаляется.\n\n"
        "🗺 /map — карта-картинка с портами и судами.\n"
        "🚢 /all — все суда в базе: сколько, типы, назначения, свежесть.\n"
        "🌊 /sea — кто сейчас в море: скорость, расстояние и прибытие.\n"
        "⚓ /port — кто стоит в портах и на рейде, с грузом и осадкой.\n"
        "⚓ /visits — открытые стоянки сейчас (приход и длительность).\n"
        "🕘 /history — история завершённых стоянок по портам.\n"
        "📅 /plan — прогноз на 4 дня: путь до порта, стоянка и выход в море.\n"
        "⏱ /expected — кто сообщил наш порт пунктом назначения (AIS-catcher).\n"
        "🚨 /alerts — журнал алертов: что отправлено и с каким результатом.\n"
        "🚢 /vessel MMSI или название — карточка любого судна.\n"
        "🔌 /sources — подключения и проверка источников (живы ли и читаются ли).\n"
        "🔑 /connection — какие каналы и ключи настроены (без самих ключей).\n"
        "🎛 /filters — текущие фильтры: порты, окно рейса, скорость, пороги устаревания.\n"
        "🤖 /status — состояние бота: чаты, включён ли приём команд, база.\n"
        "🛠 /services — сервисы и ссылки для проверки судов.\n"
        "📜 /log — последние строки журнала бота.\n"
        "/start — вернуть клавиатуру портов и меню.",
        reply_markup=make_port_keyboard(),
    )


async def handle_port(message: Message):
    port_name = (message.text or "").strip().upper()
    if port_name not in DEFAULT_PORT_NAMES:
        await message.answer("Выберите порт кнопкой ниже.", reply_markup=make_port_keyboard())
        return

    chat_id = message.chat.id
    bot = message.bot

    # новая выдача полностью заменяет прошлую
    cancel_refresher(chat_id)
    await clear_previous(bot, chat_id)
    await try_delete(bot, chat_id, message.message_id)

    vessels = await asyncio.to_thread(get_vessels_for_port, port_name)
    core = get_core()

    sent = await message.answer(header_text(port_name, vessels),
                                reply_markup=make_port_keyboard())
    remember(chat_id, sent)
    state = {"port": port_name, "header": sent.message_id, "items": {}, "task": None}
    _chats[chat_id] = state

    photos = await load_photos(vessels)
    for vessel, photo in zip(vessels, photos):
        item = await send_vessel(bot, chat_id, vessel, core, photo)
        state["items"][str(vessel.get("mmsi"))] = item

    # дальше карточки живут своей жизнью: позиция и скорость обновляются сами
    state["task"] = asyncio.create_task(refresher(bot, chat_id, port_name))


def _expected_lines(rows) -> list:
    """Строки для сообщения: по портам, с ETA, MMSI и последним сигналом."""
    by_port: dict = {}
    for row in rows:
        by_port.setdefault(row.get("port") or row.get("locode"), []).append(row)
    lines = []
    for port, items in by_port.items():
        lines.append(f"\n<b>{port}</b> — {len(items)}")
        for row in items[:10]:
            tail = f" · MMSI {row['mmsi']}" if row.get("mmsi") else ""
            heard = row.get("last_heard") or "—"
            lines.append(f"• {html.escape(str(row.get('name') or '—'))} — "
                         f"ETA {row.get('eta') or '—'} · сигнал {heard}{tail}")
        if len(items) > 10:
            lines.append(f"  …и ещё {len(items) - 10}")
    return lines


async def cmd_expected(message: Message):
    """Ожидаемые прибытия по всем нашим портам из бесплатного AIS-catcher."""
    try:
        portcalls = _import("portcalls")
    except Exception as error:
        await message.answer(f"Список ожидаемых недоступен: {error}")
        return
    rows = portcalls.expected_rows()
    if not rows:
        await message.answer(
            "Списка ожидаемых пока нет — его собирает приложение "
            "(включён канал «AIS-catcher: ожидаемые прибытия»).")
        return
    lines = ["⏱ <b>Ожидаемые прибытия</b>",
             f"Всего судов: <b>{len(rows)}</b>. Источник: AIS-catcher (без ключа)."]
    lines += _expected_lines(rows)
    cache = portcalls.load_cache()
    when = (cache.get("updated") or "")[:16].replace("T", " ")
    if when:
        lines.append(f"\nСписок обновлён: {when} UTC.")
    if cache.get("stale"):
        lines.append("Сейчас страницы AIS-catcher отдают 0 судов — "
                     "показан прошлый список.")
    lines.append("ETA — заявленный капитаном, время UTC.")
    await message.answer("\n".join(lines), disable_web_page_preview=True)


def _sea_lines(vessels: List[dict], core) -> List[str]:
    """Строки списка судов в море: курс, скорость, расстояние и прибытие."""
    lines = []
    for vessel in vessels:
        try:
            voyage = core.voyage(vessel) or {}
        except Exception:
            voyage = {}
        name = html.escape(str(vessel.get("name") or vessel.get("mmsi") or "—"))
        tail = html.escape(str(vessel.get("destination") or "—"))
        sog = vessel.get("sog")
        try:
            speed = float(sog)
        except (TypeError, ValueError):
            speed = 0.0
        tail += f" · {speed:.1f} уз" if speed > 0.5 else " · без хода"
        distance = voyage.get("distance_km")
        if distance is not None:
            tail += f" · {round(distance)} км"
        elif vessel.get("nearest_km") is not None:
            tail += f" · {round(vessel['nearest_km'])} км от {vessel.get('nearest_port') or '—'}"
        hours = voyage.get("hours")
        eta = voyage.get("eta")
        if hours is not None and eta is not None:
            tail += f" · через {hours:.0f} ч ({eta:%d.%m %H:%M})"
        lines.append(f"• <b>{name}</b> — {tail}")
    return lines


def _port_lines(vessels: List[dict], core, per_port: int = 12) -> List[str]:
    """Строки по портам: кто у причала, кто ждёт лоцмана на рейде."""
    cargo = _import("cargo")
    groups: dict = {}
    for vessel in vessels:
        groups.setdefault(str(vessel.get("port_name") or "—"), []).append(vessel)
    lines = []
    for port, items in sorted(groups.items()):
        at_berth = sum(1 for item in items if item.get("port_km") == 0)
        waiting = len(items) - at_berth
        lines.append("")
        lines.append(f"⚓ <b>{html.escape(port)}</b> — у причала {at_berth}, "
                     f"на рейде {waiting}")
        for vessel in items[:per_port]:
            name = html.escape(str(vessel.get("name") or vessel.get("mmsi") or "—"))
            kind, _ = cargo.cargo_kind(vessel.get("type_code"))
            kind = kind or vessel.get("type_label") or "тип неизвестен"
            place = vessel.get("place") or "в порту"
            tail = [html.escape(f"{place}")]
            cargo_info = None
            try:
                cargo_info = core.cargo_estimate(vessel)
            except Exception:
                cargo_info = None
            if cargo_info and cargo_info.get("cargo_t"):
                tail.append(f"груз ≈ {core_module.fmt_tons(cargo_info['cargo_t'])} т")
            draft = vessel.get("draft")
            if draft:
                tail.append(f"осадка {draft:g} м" if isinstance(draft, float)
                            else f"осадка {draft} м")
            lines.append(f"• <b>{name}</b> ({html.escape(str(kind))}) — " + ", ".join(tail))
        if len(items) > per_port:
            lines.append(f"…и ещё {len(items) - per_port}")
    return lines


async def cmd_port(message: Message):
    """Суда в портах и на рейде: стоянка и ожидание лоцмана."""
    core = get_core()
    try:
        vessels = await asyncio.to_thread(core.port_vessels, 600)
    except Exception as error:
        await message.answer(f"Список судов в портах недоступен: {error}")
        return
    at_berth = sum(1 for vessel in vessels if vessel.get("port_km") == 0)
    lines = ["⚓ <b>Суда в портах и на рейде</b>",
             f"Всего: <b>{len(vessels)}</b> — у причала {at_berth}, "
             f"на рейде (ждут лоцмана или очереди) {len(vessels) - at_berth}.",
             "Порты: RENI, IZMAIL, VYLKOVE, CHORNOMORSK, ODESSA, YUZHNY."]
    if not vessels:
        lines.append("Сейчас в портах и на рейдах наших портов судов нет.")
    else:
        lines += _port_lines(vessels, core)
    lines.append("")
    lines.append("Данные: AIS района и карточки судов (тип, осадка, груз).")
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n…"
    await message.answer(text, disable_web_page_preview=True)


async def cmd_sea(message: Message):
    """Суда в море прямо сейчас: позиции из всех каналов AIS."""
    core = get_core()
    try:
        vessels = await asyncio.to_thread(core.sea_vessels, 500)
        silent = await asyncio.to_thread(core.sea_without_position, 500)
    except Exception as error:
        await message.answer(f"Список судов в море недоступен: {error}")
        return
    limit = _fresh_limit_hours(reload_config())
    # В приложении вкладка «В море» без галочки «устаревшие» показывает только
    # свежие позиции — в боте то же правило.
    stale_count = sum(1 for vessel in vessels if _is_stale(vessel, limit))
    vessels = [vessel for vessel in vessels if not _is_stale(vessel, limit)]
    moved = sum(1 for vessel in vessels if vessel.get("underway"))
    lines = ["🌊 <b>Суда в море сейчас</b>",
             f"Всего: <b>{len(vessels)}</b> (под ходом {moved}, "
             f"на рейде/якоре {len(vessels) - moved})."
             + (f" Устаревших позиций скрыто: {stale_count}." if stale_count else ""),
             "Позиции собраны всеми каналами: AISStream, Open Waters, снимок района "
             "(включая Средиземное море) и глобальное слежение по MMSI. Сюда попадают "
             "и суда с нашим портом в Destination за 1000 км от него."]
    lines += _sea_lines(vessels[:25], core)
    if len(vessels) > 25:
        lines.append(f"…и ещё {len(vessels) - 25}")
    if not vessels:
        lines.append("Сейчас ни у одного связанного судна нет свежей позиции вне порта: "
                     "они могут стоять в порту или не передавать AIS.")
    if silent:
        names = [html.escape(str(vessel.get("name") or vessel.get("mmsi"))) for vessel in silent[:10]]
        lines.append("")
        lines.append(f"📵 Без сигнала AIS: <b>{len(silent)}</b> — " + ", ".join(names)
                     + ("…" if len(silent) > 10 else ""))
    await message.answer("\n".join(lines), disable_web_page_preview=True)


async def cmd_plan(message: Message):
    """Прогноз на 4 дня: путь до порта, стоянка и выход в море.

    Одини и тот же план, что во вкладке «Прогноз 4 дня»: стройтся для каждого
    судна, которое идёт в наш порт или уже стоит в нём.
    """
    core = get_core()
    store = get_store()
    forecast = _import("forecast")
    geo = _import("geo")
    ports = reload_config().get("ports") or []

    def build():
        names = {(port.get("name") or "").upper() for port in ports}
        opens = {}
        try:
            for visit in store.open_visits():
                if (visit.get("port") or "").upper() in names:
                    opens[str(visit.get("mmsi"))] = visit
        except Exception:
            opens = {}
        result = []
        limit = _fresh_limit_hours(reload_config())
        for vessel in core.related_vessels(3000):
            # план по устаревшей позиции — фантазия: как и в приложении,
            # без галочки «устаревшие» такие суда в расчёт не берём
            if _is_stale(vessel, limit):
                continue
            mmsi = str(vessel.get("mmsi"))
            port = core.destination_port(vessel)
            if not port:
                visit = opens.get(mmsi)
                if not visit:
                    continue
                port = geo.port_by_name(visit.get("port"), ports)
            if not port:
                continue
            try:
                stay = core.port_stay(vessel, port=port)
            except Exception:
                stay = None
            plan = forecast.plan(vessel, ports, stay=stay, track=store.track(mmsi, 24))
            if plan:
                result.append((vessel, plan))
        result.sort(key=lambda pair: (pair[1].get("arrival_hours") is None,
                                      pair[1].get("arrival_hours") or 0.0))
        return result

    try:
        rows = await asyncio.to_thread(build)
    except Exception as error:
        await message.answer(f"Прогноз недоступен: {error}")
        return

    in_port = sum(1 for _, plan in rows if plan.get("in_port"))
    missing = sum(1 for _, plan in rows
                  if plan.get("no_position") and not plan.get("in_port"))
    lines = ["📅 <b>Прогноз на 4 дня</b>",
             f"Судов в плане: <b>{len(rows)}</b> — в порту {in_port}, "
             f"в пути {len(rows) - in_port - missing}."
             + (f" Без позиции: {missing}." if missing else ""),
             "Путь до порта — по курсу и скорости AIS, стоянка — по истории визитов, "
             "выход и маршрут в море — оценка на остаток четырёх дней.", ""]
    for vessel, plan in rows[:25]:
        name = html.escape(str(vessel.get("name") or f"MMSI {vessel.get('mmsi')}"))
        parts = [html.escape(str(plan.get("port_label") or plan.get("port") or "—"))]
        if plan.get("in_port"):
            parts.append("в порту")
        elif plan.get("arrival_at"):
            mark = "≈" if plan.get("arrival_estimate") else ""
            parts.append(f"приход {mark}{plan['arrival_at']:%d.%m %H:%M}")
        else:
            parts.append("нет позиции")
        if plan.get("departure_at"):
            parts.append(f"стоянка {forecast.duration_text(plan.get('stay_hours'))}")
            parts.append(f"выход {plan['departure_at']:%d.%m %H:%M}")
        if plan.get("sea_hours"):
            parts.append(f"море {forecast.duration_text(plan.get('sea_hours'))}")
        lines.append(f"• <b>{name}</b> — " + " · ".join(parts))
    if len(rows) > 25:
        lines.append(f"…и ещё {len(rows) - 25}")
    lines.append("")
    lines.append("Карточка судна: /vessel MMSI или название.")
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n…"
    await message.answer(text, disable_web_page_preview=True)


def _fresh_limit_hours(config: dict) -> float:
    """Порог устаревания — тот же, что у карты и вкладок приложения."""
    try:
        return max(0.25, float(config.get("sea_fresh_hours") or 12))
    except (TypeError, ValueError):
        return 12.0


def _is_stale(vessel: dict, limit_hours: float) -> bool:
    """Позиция старше порога: такие суда приложение без галочки не показывает."""
    core_module = _import("core")
    age = _age_hours(core_module.position_stamp(vessel))
    return age is None or age > limit_hours


def _age_hours(value) -> Optional[float]:
    """Сколько часов прошло с момента в базе — для отметки устаревших."""
    store_module = _import("store")
    try:
        moment = store_module.parse_iso(value)
    except Exception:
        return None
    if moment is None:
        return None
    return max(0.0, (datetime.now() - moment).total_seconds() / 3600.0)


async def cmd_all(message: Message):
    """Все суда в базе — сводка и последние по времени AIS."""
    store = get_store()
    core_module = _import("core")
    try:
        vessels = await asyncio.to_thread(store.vessels, 3000)
    except Exception as error:
        await message.answer(f"Список судов недоступен: {error}")
        return
    if not vessels:
        await message.answer("В базе пока нет судов — поток AIS ещё не собрал данные.")
        return
    config = reload_config()
    try:
        stale_hours = max(0.0, float(config.get("stale_minutes") or 15)) / 60.0
    except (TypeError, ValueError):
        stale_hours = 0.25
    limit = _fresh_limit_hours(config)
    with_pos = sum(1 for vessel in vessels if vessel.get("lat") is not None)
    board = [vessel for vessel in vessels if not _is_stale(vessel, limit)]
    stale_count = len(vessels) - len(board)
    lines = ["🚢 <b>Все суда</b>",
             f"В базе: <b>{len(vessels)}</b>, с позицией: {with_pos}, "
             f"без позиции: {len(vessels) - with_pos}.",
             f"С позицией не старше {limit:g} ч: <b>{len(board)}</b> · "
             f"устаревших скрыто: {stale_count}", ""]

    kinds: dict = {}
    for vessel in vessels:
        label = vessel.get("type_label") or "тип неизвестен"
        kinds[label] = kinds.get(label, 0) + 1
    top_kinds = sorted(kinds.items(), key=lambda item: -item[1])[:5]
    if top_kinds:
        lines.append("<b>Типы</b>: " + ", ".join(f"{name} — {count}"
                                                 for name, count in top_kinds))
    destinations: dict = {}
    for vessel in vessels:
        destination = (vessel.get("destination") or "").strip()
        if destination:
            destinations[destination] = destinations.get(destination, 0) + 1
    top_dest = sorted(destinations.items(), key=lambda item: -item[1])[:8]
    if top_dest:
        lines.append("<b>Назначение</b>: " + ", ".join(f"{name} — {count}"
                                                    for name, count in top_dest))
    lines.append("")
    lines.append(f"<b>Последние {min(25, len(board))} по времени AIS</b>")
    for vessel in board[:25]:
        name = html.escape(str(vessel.get("name") or f"MMSI {vessel.get('mmsi')}"))
        tail = [html.escape(str(vessel.get("type_label") or "—"))]
        if vessel.get("destination"):
            tail.append("→ " + html.escape(str(vessel["destination"])))
        sog = vessel.get("sog")
        try:
            value = float(sog)
        except (TypeError, ValueError):
            value = 0.0
        tail.append(f"{value:.1f} уз" if value > 0.5 else "без хода")
        age = _age_hours(core_module.position_stamp(vessel))
        if age is not None:
            tail.append(core_module.span_text(age) + " назад")
        stale = age is not None and age > stale_hours
        marker = "🕓 " if stale else ""
        lines.append(f"• {marker}<b>{name}</b> — " + " · ".join(tail))
    lines.append("")
    lines.append("Карточка любого судна: /vessel MMSI или /vessel название.")
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n…"
    await message.answer(text, disable_web_page_preview=True)


async def cmd_visits(message: Message):
    """Стоянки в наших портах, которые идут сейчас."""
    store = get_store()
    core_module = _import("core")
    try:
        visits = await asyncio.to_thread(store.visits, 600)
    except Exception as error:
        await message.answer(f"Стоянки недоступны: {error}")
        return
    current = [visit for visit in visits if not visit.get("left_at")]
    # открытая стоянка судна без свежей позиции — догадка: оно могло уйти незамеченным
    limit = _fresh_limit_hours(reload_config())
    kept = []
    for visit in current:
        if _is_stale(store.vessel(visit.get("mmsi")) or {}, limit):
            continue
        kept.append(visit)
    stale_current = len(current) - len(kept)
    current = kept
    lines = ["⚓ <b>Стоянки сейчас</b>",
             f"Открытых стоянок: <b>{len(current)}</b> из {len(visits)} в базе."
             + (f" Скрыто по устаревшей позиции: {stale_current}." if stale_current else ""),
             ""]
    if not current:
        lines.append("Сейчас никто из наблюдаемых судов не стоит в наших портах.")
    groups: dict = {}
    for visit in current:
        groups.setdefault(str(visit.get("port") or "—"), []).append(visit)
    for port, items in sorted(groups.items()):
        lines.append(f"<b>{html.escape(port)}</b> — {len(items)}")
        for visit in items[:15]:
            vessel = store.vessel(visit.get("mmsi")) or {}
            name = html.escape(str(vessel.get("name") or f"MMSI {visit.get('mmsi')}"))
            entered = _import("store").parse_iso(visit.get("entered_at"))
            if entered is not None:
                hours = (datetime.now() - entered).total_seconds() / 3600.0
                tail = f"стоит {core_module.span_text(hours)}, с {entered:%d.%m %H:%M}"
            else:
                tail = "время входа неизвестно"
            draft = visit.get("draft_in")
            if draft:
                tail += f", осадка {draft:g} м" if isinstance(draft, float) else f", осадка {draft} м"
            cargo = visit.get("cargo_kind")
            if cargo:
                tail += f", {html.escape(str(cargo))}"
            lines.append(f"  • <b>{name}</b> — {tail}")
        if len(items) > 15:
            lines.append(f"  …и ещё {len(items) - 15}")
    lines.append("")
    lines.append("Источник — геозона порта по живому AIS; время идёт по часам.")
    await message.answer("\n".join(lines), disable_web_page_preview=True)


async def cmd_history(message: Message):
    """История завершённых стоянок: приход, уход, длительность, осадка."""
    store = get_store()
    store_module = _import("store")
    core_module = _import("core")
    try:
        visits = await asyncio.to_thread(store.visits, 500)
        counts = await asyncio.to_thread(store.visit_counts)
    except Exception as error:
        await message.answer(f"История стоянок недоступна: {error}")
        return
    ended = [visit for visit in visits if visit.get("left_at")]
    lines = ["🕘 <b>История стоянок</b>",
             f"В базе: <b>{len(visits)}</b> стоянок, завершённых {len(ended)}.", ""]
    if counts:
        lines.append("<b>По портам</b>: " + ", ".join(
            f"{html.escape(port)} — {data.get('visits', 0)}"
            for port, data in sorted(counts.items())))
        lines.append("")
    if not ended:
        lines.append("Завершённых стоянок пока нет — программа только начала наблюдение.")
    for visit in ended[:20]:
        vessel = store.vessel(visit.get("mmsi")) or {}
        name = html.escape(str(vessel.get("name") or f"MMSI {visit.get('mmsi')}"))
        entered = store_module.parse_iso(visit.get("entered_at"))
        left = store_module.parse_iso(visit.get("left_at"))
        if entered is not None and left is not None:
            stay = core_module.span_text((left - entered).total_seconds() / 3600.0)
            when = f"{entered:%d.%m %H:%M} → {left:%d.%m %H:%M}"
        else:
            stay, when = "—", "—"
        tail = [html.escape(str(visit.get("port") or "—")), when, stay]
        draft_in, draft_out = visit.get("draft_in"), visit.get("draft_out")
        if draft_in is not None or draft_out is not None:
            tail.append(f"осадка {core_module.fmt_number(draft_in)} → "
                        f"{core_module.fmt_number(draft_out)} м")
        if visit.get("cargo_kind"):
            tail.append(html.escape(str(visit["cargo_kind"])))
        lines.append(f"• <b>{name}</b> — " + " · ".join(tail))
    if len(ended) > 20:
        lines.append(f"…и ещё {len(ended) - 20}")
    await message.answer("\n".join(lines), disable_web_page_preview=True)


async def cmd_alerts(message: Message):
    """Журнал алертов: что отправлено и с каким результатом."""
    store = get_store()
    store_module = _import("store")
    try:
        rows = await asyncio.to_thread(store.alerts, 30)
        today = await asyncio.to_thread(store.alerts_count_today)
    except Exception as error:
        await message.answer(f"Журнал алертов недоступен: {error}")
        return
    lines = ["🚨 <b>Алерты</b>",
             f"Отправлено сегодня: <b>{today}</b> · в журнале последних: {len(rows)}.", ""]
    if not rows:
        lines.append("Алертов пока не было.")
    for row in rows:
        sent = store_module.parse_iso(row.get("sent_at"))
        when = f"{sent:%d.%m %H:%M}" if sent is not None else "—"
        icon = "✅" if row.get("ok") else "❌"
        name = html.escape(str(row.get("name") or f"MMSI {row.get('mmsi')}"))
        route = " → ".join(part for part in (row.get("origin"), row.get("destination"))
                            if part)
        tail = [when]
        if route:
            tail.append(html.escape(route))
        error = (row.get("error") or "").strip()
        if error:
            tail.append(html.escape(error[:80]))
        lines.append(f"{icon} <b>{name}</b> — " + " · ".join(tail))
    lines.append("")
    lines.append("Алерт — судно-танкер вышло из порта и идёт в один из наших портов.")
    await message.answer("\n".join(lines), disable_web_page_preview=True)


def _find_vessels(query: str) -> Tuple[Optional[dict], List[dict]]:
    """Поиск судна по MMSI, IMO или части названия."""
    store = get_store()
    query = (query or "").strip()
    if not query:
        return None, []
    if query.isdigit():
        vessel = store.vessel(query)
        if vessel:
            return vessel, []
        if len(query) >= 7:
            for vessel in store.vessels(3000):
                if str(vessel.get("imo") or "") == query:
                    return vessel, []
    needle = query.lower()
    matches = [vessel for vessel in store.vessels(3000)
               if needle in str(vessel.get("name") or "").lower()]
    if len(matches) == 1:
        return matches[0], []
    return None, matches[:15]


async def cmd_vessel(message: Message):
    """Карточка любого судна: /vessel MMSI или /vessel название."""
    parts = (message.text or "").split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    if not query:
        await message.answer("Напишите MMSI или название: <code>/vessel 271072871</code>\n"
                             "Найти MMSI можно в «🚢 Все суда» или «🌊 В море».",
                             reply_markup=make_port_keyboard())
        return
    try:
        vessel, matches = await asyncio.to_thread(_find_vessels, query)
    except Exception as error:
        await message.answer(f"Поиск не удался: {error}")
        return
    if vessel:
        core = get_core()
        photo = await asyncio.to_thread(_fetch_photo, vessel)
        await send_vessel(message.bot, message.chat.id, vessel, core, photo)
        return
    if not matches:
        await message.answer(f"Судно «{html.escape(query)}» не найдено в базе.",
                             reply_markup=make_port_keyboard())
        return
    lines = [f"Нашлось несколько судов по «{html.escape(query)}» — уточните:"]
    for vessel in matches:
        name = html.escape(str(vessel.get("name") or "—"))
        lines.append(f"• {name} — <code>/vessel {vessel.get('mmsi')}</code>")
    await message.answer("\n".join(lines), reply_markup=make_port_keyboard())


async def cmd_map(message: Message):
    """Карта-картинка: все суда, включая те, у кого нет координат."""
    try:
        mapimage = _import("mapimage")
    except Exception as error:
        await message.answer(f"Карта недоступна: {error}")
        return
    config = reload_config()
    vessels = await asyncio.to_thread(lambda: get_core().related_vessels(1000))
    ports = config.get("ports") or []
    without = sum(1 for vessel in vessels
                  if vessel.get("lat") is None or vessel.get("lon") is None)
    try:
        await asyncio.to_thread(mapimage.render, vessels, ports, MAP_PATH, without)
    except Exception as error:
        log.warning("Карта не нарисована: %s", error)
        await message.answer(f"Карту не удалось нарисовать: {error}")
        return
    caption = ("🗺 <b>Карта</b>\n"
               f"Судов с позицией на карте: <b>{len(vessels) - without}</b>\n"
               f"Без позиции (только в списках): {without}")
    await message.answer_photo(FSInputFile(str(MAP_PATH)), caption=caption)


async def cmd_sources(message: Message):
    """Состояние подключений и живая проверка источников без ключа."""
    try:
        portcalls = _import("portcalls")
        services_feed = _import("services_feed")
    except Exception as error:
        await message.answer(f"Проверка источников недоступна: {error}")
        return

    config = reload_config()
    statuses = dict(getattr(get_core(), "service_status", {}) or {})
    lines = ["🔌 <b>Подключения</b>", ""]

    # 1. Каналы данных: включены ли и что говорит поток
    lines.append("<b>Каналы данных</b>")
    ais_key = bool((config.get("api_key") or "").strip())
    lines.append(f"• AISStream — {'ключ задан' if ais_key else 'нет ключа'}")
    services = ["vf", "vfa", "mt", "mst", "dt", "aci", "gfw", "dat", "aishub"]
    for key in services:
        title = services_feed.SERVICE_TITLES.get(key, key)
        enabled = bool(config.get(f"use_{key}"))
        api_key = (config.get(f"{key}_key") or "").strip()
        if key == "vfa" and not api_key:
            api_key = (config.get("vf_key") or "").strip()
        if not enabled:
            state = "выключен"
        elif services_feed.needs_key(key) and not api_key:
            state = "включён, нужен ключ"
        else:
            state = "включён"
        runtime = (statuses.get(key) or "").strip()
        if runtime and runtime not in ("запуск…", "не подключен"):
            state += f" — {runtime}"
        lines.append(f"• {title} — {state}")

    # 2. Источники без ключа: проверяем прямо сейчас
    lines.append("")
    lines.append("<b>Источники без ключа (проверка сейчас)</b>")
    try:
        results = await asyncio.to_thread(portcalls.check_sources)
    except Exception as error:
        await message.answer("\n".join(lines + [f"Проверка не удалась: {error}"]),
                             disable_web_page_preview=True)
        return
    # 3. Что реально нашёл бесплатный AIS-catcher — главный источник без ключа
    try:
        expected = portcalls.expected_rows()
        cache = portcalls.load_cache()
        when = (cache.get("updated") or "")[:16].replace("T", " ")
        if expected:
            lines.append("")
            lines.append("<b>AIS-catcher: найденные суда</b>")
            lines.append(f"В Destination наш порт: <b>{len(expected)}</b>"
                         + (f" (список от {when} UTC)" if when else ""))
            lines += _expected_lines(expected)
            if cache.get("stale"):
                lines.append("Страницы сейчас отдают 0 — показан прошлый список.")
    except Exception as error:
        lines.append(f"AIS-catcher: список не прочитан ({error}).")

    for result in results:
        if result.get("ok"):
            icon = "✅"
        elif result.get("warn"):
            icon = "⚠️"
        else:
            icon = "❌"
        detail = str(result.get("detail") or "")
        tail = f" — {detail}" if detail else ""
        lines.append(f"{icon} {result.get('title')}{tail}")

    lines.append("")
    lines.append("✅ читается автоматически, ⚠️ сайт жив, но только глазами, "
                 "❌ не отвечает. Ключи задаются во вкладке «Подключение».")
    await message.answer("\n".join(lines), disable_web_page_preview=True)


def _load_expected_seen() -> dict:
    try:
        data = json.loads(EXPECTED_SEEN_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_expected_seen(data: dict) -> None:
    tmp = EXPECTED_SEEN_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, EXPECTED_SEEN_PATH)
    except OSError:
        pass


async def expected_watcher(bot: Bot) -> None:
    """Сам показывает в чате результат бесплатного AIS-catcher.

    Раз в 2 минуты смотрит на data/expected.json: если появились новые суда
    (или список пришёл впервые), отправляет их в чат. Те же MMSI повторно
    не присылает — журнал в data/aci_seen.json.
    """
    await asyncio.sleep(10)
    while True:
        try:
            portcalls = _import("portcalls")
            rows = portcalls.expected_rows()
            core_module = _import("core")
            # уведомления — во все чаты из настроек, по одному на устройство
            chats = core_module.parse_chat_ids(reload_config().get("telegram_chat_id"))
            if rows and chats:
                seen = _load_expected_seen()
                known = {str(m) for m in (seen.get("mmsi") or [])}
                current = {str(row.get("mmsi")) for row in rows if row.get("mmsi")}
                fresh = [row for row in rows
                         if str(row.get("mmsi")) not in known]
                if fresh:
                    lines = ["🆕 <b>AIS-catcher: суда с нашим портом в назначении</b>",
                             f"Новых: <b>{len(fresh)}</b> · всего в списке: {len(rows)}."]
                    lines += _expected_lines(fresh)
                    when = (rows[0].get("updated") or "")[:16].replace("T", " ")
                    if when:
                        lines.append(f"\nСписок обновлён: {when} UTC.")
                    for chat in chats:
                        try:
                            await bot.send_message(int(chat), "\n".join(lines),
                                                   disable_web_page_preview=True)
                        except Exception as error:
                            logging.warning("AIS-catcher: чат %s не получил сводку (%s)",
                                            chat, error)
                _save_expected_seen({"mmsi": sorted(current),
                                     "updated": rows[0].get("updated") or ""})
        except Exception as error:
            logging.warning("AIS-catcher: уведомление не отправлено (%s)", error)
        await asyncio.sleep(120)


# ── остальные вкладки приложения: подключение, фильтры, Telegram,
#    сервисы и журнал ────────────────────────────────────────────────────────
def _app_version() -> str:
    """Версия приложения из ship_tracker.py — без импорта Tk-модуля."""
    try:
        text = (BASE_DIR / "ship_tracker.py").read_text(encoding="utf-8")
    except OSError:
        return "—"
    found = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
    return found.group(1) if found else "—"


def _key_state(config: dict, key: str) -> str:
    """«ключ задан» или «нет ключа» — сам ключ в чат не отправляется."""
    return "ключ задан" if (config.get(key) or "").strip() else "нет ключа"


def _num(value) -> str:
    """Число из настроек без хвоста «.0»: 96.0 → 96, 17.5 → 17.5."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:g}"


def _boxes_text(boxes) -> str:
    """Район подписки человекопонятно: «40.5, 26.0 – 47.5, 42.0»."""
    parts = []
    try:
        for box in boxes or []:
            south, west = box[0][0], box[0][1]
            north, east = box[1][0], box[1][1]
            parts.append(f"{_num(south)}, {_num(west)} – {_num(north)}, {_num(east)}")
    except (TypeError, IndexError, ValueError):
        return str(boxes or "—")
    return "; ".join(parts) or "—"


async def cmd_connection(message: Message):
    """Вкладка «Подключение»: какие каналы и ключи настроены (без значений)."""
    config = reload_config()
    lines = ["🔑 <b>Подключение</b>", "",
             f"Версия приложения: {_app_version()}",
             f"AISStream (живой поток): {_key_state(config, 'api_key')}"]

    # 2. Остальные каналы
    ow = "включён" if config.get("use_openwaters", True) else "выключен"
    lines.append(f"Open Waters AIS: {ow}, {_key_state(config, 'openwaters_token')}")
    snap = "включён" if config.get("use_snapshot", True) else "выключен"
    lines.append(f"Снимок района: {snap}, каждые "
                 f"{_num(config.get('snapshot_minutes', 10))} мин")
    lines.append(f"Район подписки: {_boxes_text(config.get('bounding_boxes'))}")

    # 3. Дополнительные сервисы
    try:
        services_feed = _import("services_feed")
    except Exception:
        services_feed = None
    extras = []
    for key, fallback_title in (("vf", "VesselFinder"), ("vfa", "VesselFinder API"),
                                ("mt", "MarineTraffic"), ("mst", "MyShipTracking"),
                                ("dt", "Digitraffic"), ("gor", "Goradar"),
                                ("mlive", "MarineTraffic.live"), ("srd", "Ship Radar 24"),
                                ("aci", "AIS-catcher (ожидаемые)"),
                                ("gfw", "Global Fishing Watch"),
                                ("dat", "Datalastic"), ("aishub", "AISHub")):
        enabled = bool(config.get(f"use_{key}"))
        title = (services_feed.SERVICE_TITLES.get(key, fallback_title)
                 if services_feed else fallback_title)
        state = "включён" if enabled else "выключен"
        if enabled and services_feed and services_feed.needs_key(key):
            api_key = (config.get(f"{key}_key") or "").strip()
            if key == "vfa" and not api_key:
                api_key = (config.get("vf_key") or "").strip()
            state += f", {'ключ задан' if api_key else 'нужен ключ'}"
        extras.append(f"• {title} — {state}")
    if extras:
        lines.append("")
        lines.append("<b>Дополнительные сервисы</b>")
        lines += extras

    ports = config.get("ports") or []
    lines.append("")
    lines.append(f"Портов отслеживается: {len(ports)} — "
                 + ", ".join((port.get("name") or "").upper() for port in ports))
    lines.append("Ключи и токены в чат не отправляются: они меняются во вкладке "
                 "«Подключение» в приложении.")
    await message.answer("\n".join(lines), disable_web_page_preview=True)


async def cmd_filters(message: Message):
    """Вкладка «Фильтры»: текущие настройки одним сообщением."""
    config = reload_config()
    ports = [(port.get("name") or "").upper() for port in (config.get("ports") or [])]
    dests = [str(name).upper() for name in (config.get("filter_destinations") or [])]
    origs = [str(name).upper() for name in (config.get("filter_origins") or [])]
    stale = config.get("stale_minutes", 15)
    fresh = config.get("sea_fresh_hours", 12)
    lines = ["🎛 <b>Фильтры</b>", "",
             "Порты: " + (", ".join(ports) if ports else "—"),
             "Идут в порты: " + (", ".join(dests) if dests else "все"),
             "Только из портов: " + (", ".join(origs) if origs else "любые"),
             f"Окно рейса: {_num(config.get('trip_window_hours', 96))} ч",
             f"Минимальный ход: {_num(config.get('min_speed_knots', 2))} уз",
             f"Пауза повтора алерта: {_num(config.get('alert_cooldown_hours', 6))} ч",
             f"Хранить историю: {_num(config.get('history_days', 7))} дн",
             f"Точка в базу раз в: {_num(config.get('position_interval_seconds', 60))} с",
             f"Бледным на карте: старше {_num(stale)} мин",
             f"Устаревшие (скрыты без галочки): старше {_num(fresh)} ч",
             f"Радиус интереса: {_num(config.get('port_watch_km', 50))} км",
             f"Район вокруг портов: {_num(config.get('area_radius_km', 900))} км"]
    lines.append("")
    lines.append("Меняются во вкладке «Фильтры» в приложении; там же галочка «устаревшие» "
                 "на карте показывает и устаревшие строки во всех списках.")
    await message.answer("\n".join(lines))


async def cmd_telegram(message: Message):
    """Вкладка «Telegram»: состояние бота и чаты, куда он пишет."""
    config = reload_config()
    core_module = _import("core")
    chats = core_module.parse_chat_ids(config.get("telegram_chat_id"))
    started = globals().get("STARTED_AT")
    uptime = "—"
    if started:
        hours = (datetime.now() - started).total_seconds() / 3600
        uptime = core_module.span_text(hours)
    lines = ["🤖 <b>Telegram</b>", "",
             f"Токен: {_key_state(config, 'telegram_token')}",
             f"Команды принимаются: "
             + ("да" if config.get("telegram_commands", True) else "нет"),
             f"Чатов в настройках: {len(chats)}"
             + (" — " + ", ".join(chats) if chats else ""),
             f"Бот работает: {uptime}, PID {os.getpid()}",
             f"Меню команд: {len(BOT_COMMANDS)} — полный список в /help",
             "Запускается вместе с приложением и закрывается вместе с ним; "
             "второй экземпляр не стартует (data/bot.lock)."]
    try:
        counts = get_store().counts()
        lines.append(f"База: судов {counts.get('vessels', 0)}, позиций "
                     f"{counts.get('positions', 0)}, визитов {counts.get('visits', 0)}.")
    except Exception:
        pass
    lines.append(f"Журнал бота: data/bot_run.log — смотрите /log.")
    await message.answer("\n".join(lines))


async def cmd_services(message: Message):
    """Вкладка «Сервисы»: те же ссылки, что в приложении."""
    lines = ["🛠 <b>Сервисы</b>", ""]
    lines.append("<b>Онлайн-сервисы (карточки судов)</b>")
    for name, url in (("VesselFinder", "https://www.vesselfinder.com/"),
                      ("MarineTraffic", "https://www.marinetraffic.com/"),
                      ("MyShipTracking", "https://www.myshiptracking.com/"),
                      ("Equasis", "https://www.equasis.org/"),
                      ("IMO GISIS", "https://gisis.imo.org/"),
                      ("Open Waters", "https://openwaters.io/ais/")):
        lines.append(f'• <a href="{url}">{name}</a>')
    lines.append("")
    lines.append("<b>AIS-потоки и десктоп</b>")
    for name, url in (("Open Waters AIS", "https://openwaters.io/ais/"),
                      ("AISHub", "https://www.aishub.net/"),
                      ("Digitraffic", "https://www.digitraffic.fi/en/marine-traffic/"),
                      ("OpenCPN", "https://opencpn.org/"),
                      ("AIS-catcher", "https://github.com/jvde-github/AIS-catcher"),
                      ("AIS Dispatcher", "https://www.aishub.net/ais-dispatcher")):
        lines.append(f'• <a href="{url}">{name}</a>')
    lines.append("")
    lines.append("Карточка любого судна — /vessel MMSI или название; "
                 "какие потоки включены — /sources.")
    await message.answer("\n".join(lines), disable_web_page_preview=True)


async def cmd_log(message: Message):
    """Вкладка «Журнал»: последние строки журнала бота."""
    path = BASE_DIR / "data" / "bot_run.log"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        await message.answer(f"📜 Журнал недоступен: {error}")
        return
    rows = [row.strip() for row in text.splitlines() if row.strip()][-25:]
    body = "\n".join(html.escape(row) for row in rows) or "Журнал пуст."
    if len(body) > 3400:
        body = body[-3400:]
    await message.answer(f"📜 <b>Журнал бота</b> (последние {len(rows)} строк)\n"
                         f"<pre>{body}</pre>")


async def fallback(message: Message):
    await message.answer("Выберите порт:", reply_markup=make_port_keyboard())


# ── запуск ─────────────────────────────────────────────────────────────────
def make_dispatcher() -> Tuple[Bot, Dispatcher]:
    token = (reload_config().get("telegram_token") or "").strip()
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(cmd_plan, Command("plan"))
    dp.message.register(cmd_expected, Command("expected"))
    dp.message.register(cmd_sea, Command("sea"))
    dp.message.register(cmd_port, Command("port"))
    dp.message.register(cmd_all, Command("all"))
    dp.message.register(cmd_visits, Command("visits"))
    dp.message.register(cmd_history, Command("history"))
    dp.message.register(cmd_alerts, Command("alerts"))
    dp.message.register(cmd_vessel, Command("vessel"))
    dp.message.register(cmd_map, Command("map"))
    dp.message.register(cmd_sources, Command("sources"))
    dp.message.register(cmd_connection, Command("connection"))
    dp.message.register(cmd_filters, Command("filters"))
    dp.message.register(cmd_telegram, Command("status"))
    dp.message.register(cmd_services, Command("services"))
    dp.message.register(cmd_log, Command("log"))
    # кнопки меню — те же пункты, что вкладки в приложении
    dp.message.register(cmd_map, F.text == MAP_BUTTON)
    dp.message.register(cmd_all, F.text == ALL_BUTTON)
    dp.message.register(cmd_sea, F.text == SEA_BUTTON)
    dp.message.register(cmd_plan, F.text == PLAN_BUTTON)
    dp.message.register(cmd_visits, F.text == VISITS_BUTTON)
    dp.message.register(cmd_history, F.text == HISTORY_BUTTON)
    dp.message.register(cmd_expected, F.text == EXPECTED_BUTTON)
    dp.message.register(cmd_alerts, F.text == ALERTS_BUTTON)
    dp.message.register(cmd_sources, F.text == SOURCES_BUTTON)
    dp.message.register(cmd_connection, F.text == CONNECTION_BUTTON)
    dp.message.register(cmd_filters, F.text == FILTERS_BUTTON)
    dp.message.register(cmd_telegram, F.text == TELEGRAM_BUTTON)
    dp.message.register(cmd_services, F.text == SERVICES_BUTTON)
    dp.message.register(cmd_log, F.text == LOG_BUTTON)
    dp.message.register(handle_port, F.text.in_(DEFAULT_PORT_NAMES))
    dp.message.register(fallback)
    return bot, dp


BOT_COMMANDS: List[Tuple[str, str]] = [
    ("start", "клавиатура портов и меню"),
    ("map", "карта с судами"),
    ("all", "все суда в базе"),
    ("sea", "кто сейчас в море"),
    ("plan", "прогноз 4 дня: приход, стоянка, выход"),
    ("port", "кто в портах и на рейде"),
    ("visits", "стоянки сейчас"),
    ("history", "история стоянок"),
    ("expected", "ожидаемые прибытия"),
    ("alerts", "журнал алертов"),
    ("vessel", "карточка судна по MMSI или названию"),
    ("connection", "какие каналы и ключи настроены"),
    ("filters", "текущие фильтры и пороги"),
    ("status", "состояние бота, чаты, база"),
    ("services", "сервисы и ссылки для проверки судов"),
    ("log", "последние строки журнала"),
    ("sources", "подключения и источники"),
    ("help", "справка"),
]


async def publish_commands(bot: Bot) -> None:
    """Меню команд в Telegram — те же пункты, что вкладки приложения."""
    try:
        await bot.set_my_commands([types.BotCommand(command=name, description=text)
                                   for name, text in BOT_COMMANDS])
    except Exception as error:
        log.warning("Меню команд не обновлено: %s", error)


def acquire_single_instance() -> bool:
    """Не даёт запустить второй бот на том же токене: они мешают друг другу."""
    global _lock_handle
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "a+")
    try:
        try:
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except ImportError:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _lock_handle = handle
    return True


def start_ingest() -> bool:
    """Принимает AIS сам, если поток не ведёт приложение.

    Данные в боте меняются только тогда, когда кто-то пишет их в базу:
    либо открытое окно приложения, либо этот процесс. Поток забирает только
    один из них — через data/feed.lock, чтобы не было двойных подключений.
    """
    config = reload_config()
    if not ((config.get("api_key") or "").strip()
            or (config.get("openwaters_token") or "").strip()):
        logging.info("Ключей AIS нет — бот показывает то, что уже есть в базе.")
        return False
    core = get_core()
    if core.running():
        return True
    try:
        core.start()
    except Exception as error:
        logging.warning("Поток AIS не запущен: %s", error)
        return False
    if getattr(core, "feed_elsewhere", False):
        logging.info("AIS уже принимает другой процесс — бот читает общую базу.")
        return False
    logging.info("Бот принимает AIS сам: данные в базе обновляются.")
    logging.info("Поток AIS: %s", core.feed_state())
    return True


async def run_bot():
    token = (reload_config().get("telegram_token") or "").strip()
    if not token:
        logging.error("Не задан telegram_token в data/config.json — бот не запущен.")
        return
    bot, dp = make_dispatcher()
    await publish_commands(bot)
    ingesting = start_ingest()
    watcher = asyncio.create_task(expected_watcher(bot))
    logging.info("Ship Radar Bot запущен, ожидаю сообщения.")
    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        watcher.cancel()
        await bot.session.close()
        if ingesting:
            try:
                get_core().stop()
            except Exception:
                pass
        if _store is not None:
            _store.close()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not acquire_single_instance():
        print("Бот уже запущен — отдельным окном или приложением. "
              "Закройте тот процесс и запустите снова.")
        return
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
