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
    builder = ReplyKeyboardBuilder()
    for name in DEFAULT_PORT_NAMES:
        builder.button(text=name)
    builder.button(text=MAP_BUTTON)
    builder.adjust(2)
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
    """Маршрут координатами: где судно сейчас и где будет в ближайшие дни."""
    try:
        forecast = _import("forecast")
        points = forecast.route(vessel, CONFIG.get("ports") or [])
    except Exception as error:
        log.info("Прогноз для %s не построен: %s", vessel.get("mmsi"), error)
        points = []

    if len(points) < 2:
        sog = vessel.get("sog") or 0
        try:
            sog = float(sog)
        except (TypeError, ValueError):
            sog = 0.0
        if sog < 0.5:
            return "🗺 Маршрут: судно почти не движется — прогноз не строится"
        return "🗺 Маршрут: нет курса в AIS — прогноз не строится"

    lines = ["🗺 Маршрут (прогноз на 5 дней):"]
    for point in forecast.daily_points(points):
        suffix = f" ({point['port']})" if point.get("port") else ""
        when = f"{point['at']:%d.%m %H:%M}" if point.get("at") else ""
        lines.append(f"  {point['label']}: {point['lat']:.3f}, {point['lon']:.3f}"
                     + (f" · {when}" if when else "") + suffix)
    return "\n".join(lines)


def vessel_card(vessel: dict, core) -> str:
    """Карточка судна — то же, что показывает приложение."""
    core_module = _import("core")
    store_module = _import("store")
    flags = _import("flags")
    ais_feed = _import("ais_feed")
    cargo = _import("cargo")

    mmsi = str(vessel.get("mmsi"))
    _country, iso2, _emoji = flags.flag_for_mmsi(mmsi)
    lines = [
        f"🚢 <b>{vessel.get('name') or mmsi}</b>",
        f"MMSI: {mmsi}",
        f"Флаг: {flags.flag_text(mmsi)}" + (f" ({iso2})" if iso2 else ""),
    ]
    if vessel.get("imo"):
        lines.append(f"IMO: {vessel['imo']}")
    lines += [
        f"Тип: {ais_feed.type_detail(vessel.get('type_code'))} "
        f"(код AIS {vessel.get('type_code')}, {vessel.get('type_label') or '—'})",
        f"Назначение: {vessel.get('destination') or '—'}",
        f"Скорость: {core_module.fmt_number(vessel.get('sog'))} уз",
        f"Размеры: {size_text(vessel)} (длина × ширина)",
        f"Курс: {core_module.fmt_number(vessel.get('cog'))}°",
        f"Осадка: {core_module.fmt_number(vessel.get('draft'))} м",
        f"В порту: {vessel.get('port') or '—'}",
        f"Координаты: {core_module.fmt_number(vessel.get('lat'))}, "
        f"{core_module.fmt_number(vessel.get('lon'))}",
        f"Обновлено: {core_module.fmt_moment(store_module.parse_iso(vessel.get('updated_at')))}",
        route_text(vessel),
    ]

    voyage = core.voyage(vessel)
    if voyage:
        port_label = voyage.get("port_label") or voyage.get("port")
        if voyage.get("in_port"):
            lines.append(f"До прибытия: уже в порту {port_label}")
        elif voyage.get("hours") is not None:
            lines.append(f"До прибытия: {core_module.duration_text(voyage['hours'])} "
                         f"({core_module.fmt_moment(voyage['eta'])}), "
                         f"{core_module.fmt_number(voyage['distance_km'])} км до {port_label}")
        else:
            lines.append(f"До прибытия: скорость слишком мала для расчёта "
                         f"({core_module.fmt_number(voyage['distance_km'])} км до {port_label})")

    declared = store_module.parse_iso(vessel.get("eta_declared"))
    lines.append(f"Срок прибытия (AIS): {core_module.fmt_moment(declared) if declared else '—'} "
                 f"(передаёт капитан, в UTC)")
    try:
        portcalls = _import("portcalls")
    except Exception:
        portcalls = None
    if portcalls is not None:
        expected = portcalls.expected_for_mmsi(mmsi)
        if expected:
            lines.append(f"Ожидается в {expected.get('port')}: заявленный ETA "
                         f"{expected.get('eta') or '—'} (AIS-catcher)")
        gfw_text = portcalls.gfw_gap_text(portcalls.gfw_vessel(None, mmsi))
        if gfw_text:
            lines.append(gfw_text)

    stay = core.port_stay(vessel)
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
        lines.append(f"Стоянка в {stay_port}: " + ("; ".join(parts) if parts else "наблюдений пока нет"))

    cargo_info = core.cargo_estimate(vessel)
    if cargo_info:
        share = cargo_info.get("load_share")
        if share is None:
            lines.append(f"Груз (оценка): ≈ {core_module.fmt_tons(cargo_info['cargo_t'])} т")
        else:
            lines.append(f"Груз (оценка): ≈ {core_module.fmt_tons(cargo_info['cargo_t'])} т "
                         f"({round(share * 100)} % дедвейта ≈ "
                         f"{core_module.fmt_tons(cargo_info['deadweight_t'])} т)")
    elif vessel.get("draft"):
        lines.append("Груз (оценка): нет длины и ширины корпуса — "
                     "судно их ещё не передавало")

    kind, kind_detail = cargo.cargo_kind(vessel.get("type_code"))
    if kind:
        lines.append(f"Что везёт: {kind} — {kind_detail}")

    signals = core.vessel_signals(vessel) if hasattr(core, "vessel_signals") else []
    if signals:
        lines.append("<b>Признаки</b> (по наблюдениям программы):")
        lines += [f"• {signal}" for signal in signals]

    if vessel.get("imo") or mmsi:
        lines.append(f"VesselFinder: https://www.vesselfinder.com/vessels/details/"
                     f"{vessel.get('imo') or mmsi}")
    if portcalls is not None:
        lines.append("<b>Проверить в реестрах:</b>")
        for title, url in portcalls.registry_links(vessel.get("imo"), mmsi)[:3]:
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
        "Нажмите кнопку порта — бот покажет все суда, идущие в этот порт, "
        "с теми же данными, что и в приложении.\n"
        "В карточке есть маршрут: текущие координаты и прогноз на 5 дней.\n"
        "Карточки обновляются сами каждые 2 минуты.\n"
        "При выборе другого порта прошлый список удаляется.\n"
        "/expected — кто сообщил наш порт пунктом назначения (AIS-catcher).\n"
        "/map — карта-картинка со всеми судами (в том числе без позиции).\n"
        "/sources — подключения и проверка источников (живы ли и читаются ли).\n"
        "/start — вернуть клавиатуру портов.",
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
    try:
        await asyncio.to_thread(mapimage.render, vessels, ports, MAP_PATH)
    except Exception as error:
        log.warning("Карта не нарисована: %s", error)
        await message.answer(f"Карту не удалось нарисовать: {error}")
        return
    without = sum(1 for vessel in vessels
                  if vessel.get("lat") is None or vessel.get("lon") is None)
    caption = ("🗺 <b>Карта: все суда</b>\n"
               f"Всего: <b>{len(vessels)}</b> · с позицией: {len(vessels) - without} · "
               f"без позиции (○ у порта): {without}\n"
               "○ — судно без координат, показано у порта назначения "
               "(AIS-catcher, без ключа).")
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
            chat_id = (reload_config().get("telegram_chat_id") or "").strip()
            if rows and chat_id:
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
                    await bot.send_message(int(chat_id), "\n".join(lines),
                                          disable_web_page_preview=True)
                _save_expected_seen({"mmsi": sorted(current),
                                     "updated": rows[0].get("updated") or ""})
        except Exception as error:
            logging.warning("AIS-catcher: уведомление не отправлено (%s)", error)
        await asyncio.sleep(120)


async def fallback(message: Message):
    await message.answer("Выберите порт:", reply_markup=make_port_keyboard())


# ── запуск ─────────────────────────────────────────────────────────────────
def make_dispatcher() -> Tuple[Bot, Dispatcher]:
    token = (reload_config().get("telegram_token") or "").strip()
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(cmd_expected, Command("expected"))
    dp.message.register(cmd_map, Command("map"))
    dp.message.register(cmd_map, F.text == MAP_BUTTON)
    dp.message.register(cmd_sources, Command("sources"))
    dp.message.register(handle_port, F.text.in_(DEFAULT_PORT_NAMES))
    dp.message.register(fallback)
    return bot, dp


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
