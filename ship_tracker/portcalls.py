"""Ожидаемые прибытия и проверка судна по открытым источникам.

AIS-catcher (aiscatcher.org) — бесплатная сеть приёмников сообщества. На
странице порта во вкладке Expected сервер сам отдаёт таблицу судов, у которых
в AIS записан этот порт: имя, MMSI, тип, заявленный ETA и время последнего
сигнала. Ключ не нужен, ограничение — один запрос на порт.

Global Fishing Watch — официальный API по бесплатному токену: профиль судна
по MMSI и разрывы AIS (события GAP) за последние дни. Токен выдаётся после
регистрации на globalfishingwatch.org.

Реестры (Equasis, ITU MARS, IMO GISIS) и портовые администрации (USPA, APDM
Galați, APM Constanța) программного API не дают — для них строятся прямые
ссылки, чтобы проверить судно вручную.

Для источников без ключа есть проверка доступности (check_sources): она
отвечает, жив ли сайт и годится ли он для автоматического чтения. Так видно,
когда источник «лёг», а не только когда пропали данные.
"""
from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
EXPECTED_PATH = DATA_DIR / "expected.json"
GFW_PATH = DATA_DIR / "gfw.json"
SOURCES_PATH = DATA_DIR / "sources.json"

EXPECTED_URL = "https://www.aiscatcher.org/port/{locode}?tab=expected"
GFW_SEARCH_URL = "https://gateway.api.globalfishingwatch.org/v3/vessels/search"
GFW_EVENTS_URL = "https://gateway.api.globalfishingwatch.org/v3/events"
GFW_IDENTITY_DATASET = "public-global-vessel-identity:latest"
GFW_GAPS_DATASET = "public-global-gaps-events:latest"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

# строка таблицы Expected: MMSI, имя, тип, ETA, время последнего сигнала
_ROW = re.compile(
    r'<a href="/ship/details/(\d+)"[^>]*title="([^"]*)"[^>]*>[^<]*</a>'
    r'\s*<small>([^<]*)</small>.*?</td>\s*<td>([^<]*)</td>'
    r'\s*<td[^>]*>\s*<span title="([^"]*)"[^>]*>([^<]*)</span>',
    re.S)


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_expected_page(html: str) -> list[dict]:
    """Разбирает таблицу Expected со страницы порта AIS-catcher."""
    rows = []
    for match in _ROW.finditer(html or ""):
        mmsi, title, type_text, eta, heard_at, heard_ago = match.groups()
        name = _clean(title).split("·")[0].strip()
        rows.append({
            "mmsi": mmsi,
            "name": name or mmsi,
            "type": _clean(type_text),
            "eta": _clean(eta),
            "last_heard": _clean(heard_ago),
            "last_heard_at": _clean(heard_at),
        })
    return rows


def fetch_expected(ports, timeout=45, pause=1.5, log=None, retries=1) -> dict:
    """Ожидаемые прибытия по всем нашим портам: {locode: {name, vessels}}.

    Ошибка одного порта не мешает остальным: она попадает в поле error.
    Порт, который не ответил с первого раза, переспрашивается — так список
    при открытии программы заполняется целиком, а не наполовину.
    """
    import requests
    import time

    result = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "ports": {}}

    def ask(port):
        locode = _clean((port or {}).get("locode")).upper()
        label = _clean((port or {}).get("label") or (port or {}).get("name"))
        if not locode:
            return None
        entry = {"name": label, "vessels": []}
        try:
            response = requests.get(EXPECTED_URL.format(locode=locode),
                                    headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            entry["vessels"] = parse_expected_page(response.text)
            if not entry["vessels"] and "expected-table" not in response.text:
                # вместо страницы порта пришла заглушка (Cloudflare, ошибка сайта)
                entry["error"] = "страница без таблицы Expected"
        except Exception as error:                     # сеть, Cloudflare, формат
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status == 404:
                # у сети AIS-catcher есть не все порты — это не сбой канала
                entry["unsupported"] = True
            else:
                entry["error"] = str(error).strip()[:120] or error.__class__.__name__
                if log:
                    log(f"AIS-catcher {locode}: {entry['error']}")
        return locode, entry

    for port in ports or []:
        asked = ask(port)
        if not asked:
            continue
        result["ports"][asked[0]] = asked[1]
        time.sleep(max(0.0, pause))
    # второй подход к портам, которые не отдали таблицу с первого раза
    for attempt in range(max(0, int(retries or 0))):
        failed = [port for port in (ports or [])
                  if (result["ports"].get(_clean((port or {}).get("locode")).upper()) or {})
                  .get("error") and not (result["ports"].get(
                      _clean((port or {}).get("locode")).upper()) or {}).get("unsupported")]
        if not failed:
            break
        if attempt or failed:
            time.sleep(3)
        for port in failed:
            asked = ask(port)
            if not asked:
                continue
            if asked[1].get("vessels") or not asked[1].get("error"):
                result["ports"][asked[0]] = asked[1]
            time.sleep(max(0.0, pause))
    # Пустой ответ по всем портам — почти всегда сбой сайта, а не уход судов:
    # оставляем прошлый список, чтобы вкладка и бот не опустели.
    if not any(entry.get("vessels") for entry in result["ports"].values()):
        previous = load_cache()
        if expected_rows(previous):
            previous = dict(previous)
            previous["stale"] = True
            previous["stale_reason"] = "портовые страницы сейчас отдают 0 судов"
            return previous
    return result


def save_cache(data, path=None) -> None:
    path = Path(path) if path else EXPECTED_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def load_cache(path=None) -> dict:
    path = Path(path) if path else EXPECTED_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def expected_rows(cache=None) -> list[dict]:
    """Плоский список ожидаемых судов: по одной строке на судно."""
    cache = cache if isinstance(cache, dict) else load_cache()
    rows = []
    updated = cache.get("updated") or ""
    for locode, entry in (cache.get("ports") or {}).items():
        if not isinstance(entry, dict):
            continue
        for vessel in entry.get("vessels") or []:
            if not isinstance(vessel, dict):
                continue
            rows.append({
                "locode": locode,
                "port": entry.get("name") or locode,
                "updated": updated,
                **vessel,
            })
    return rows


def expected_for_mmsi(mmsi, cache=None) -> dict | None:
    """Запись об ожидании для конкретного судна (если оно есть в списке)."""
    target = str(mmsi or "")
    if not target:
        return None
    for row in expected_rows(cache):
        if str(row.get("mmsi")) == target:
            return row
    return None


# ------------------------------------------------------------------- GFW

# Классы судов GFW → код типа AIS и подпись по-русски. Нужно, чтобы данные
# из реестра вставали в те же поля, что и статика AIS, и не спорили с ней.
GFW_CLASSES = {
    "CARGO": (70, "грузовое"),
    "TANKER": (80, "танкер"),
    "PASSENGER": (60, "пассажирское"),
    "FISHING": (30, "рыболовное"),
    "TUG": (52, "буксир"),
    "TUG_TOW": (52, "буксир"),
    "PLEASURE": (37, "прогулочное"),
    "SEARCH_AND_RESCUE": (55, "спасательное"),
    "DREDGING": (33, "земснаряд"),
    "PILOT": (50, "лоцманское"),
    "MILITARY": (35, "военное"),
    "SAR": (55, "спасательное"),
}


def _gfw_value(items):
    """Первое значение из списка оценок GFW ({'value': ...})."""
    if isinstance(items, list) and items:
        first = items[0] if isinstance(items[0], dict) else {}
        return first.get("value")
    return None


def gfw_identity(entry) -> dict:
    """Паспорт судна из ответа GFW: имя, флаг, IMO, тип, длина, тоннаж.

    GFW отдаёт справочник (selfReportedInfo: имя, флаг, позывной, IMO) и
    оценки модели (combinedSourcesInfo: класс судна, длина, тоннаж, скорость).
    Оценки модель даёт не всегда — тогда поле просто не заполняется.
    """
    entry = entry if isinstance(entry, dict) else {}
    info = entry.get("selfReportedInfo") or []
    info = info[0] if info and isinstance(info[0], dict) else {}
    combined = entry.get("combinedSourcesInfo") or []
    combined = combined[0] if combined and isinstance(combined[0], dict) else {}
    classes = combined.get("vesselClassSc") or []
    klass = str(_gfw_value(classes) or "").upper().replace(" ", "_")
    code, label = GFW_CLASSES.get(klass, (None, ""))
    if not label:
        shiptype = str(_gfw_value(combined.get("shiptypes")) or "").upper().replace(" ", "_")
        code, label = GFW_CLASSES.get(shiptype, (None, ""))
    length = _gfw_value(combined.get("bestLengthM"))
    tonnage = _gfw_value(combined.get("bestTonnageGt"))
    power = _gfw_value(combined.get("bestEnginePowerKw"))
    speed = _gfw_value(combined.get("maxSpeedKn"))
    try:
        length = round(float(length), 1) if length is not None else None
    except (TypeError, ValueError):
        length = None
    try:
        tonnage = round(float(tonnage))
    except (TypeError, ValueError):
        tonnage = None
    try:
        power = round(float(power))
    except (TypeError, ValueError):
        power = None
    try:
        speed = round(float(speed), 1)
    except (TypeError, ValueError):
        speed = None
    return {
        "id": str(info.get("id") or entry.get("id") or ""),
        "mmsi": str(info.get("ssvid") or ""),
        "name": _clean(info.get("shipname") or info.get("shipName")),
        "flag": _clean(info.get("flag")),
        "imo": _clean(info.get("imo")),
        "callsign": _clean(info.get("callsign")),
        "type_code": code,
        "type_label": label,
        "vessel_class": klass,
        "length_m": length,
        "tonnage_gt": tonnage,
        "engine_power_kw": power,
        "max_speed_kn": speed,
    }


def gfw_vessel(gfw, mmsi):
    """Данные GFW по MMSI из кэша (если профиль уже запрашивался)."""
    gfw = gfw if isinstance(gfw, dict) else load_gfw()
    return ((gfw.get("vessels") or {}).get(str(mmsi or "")) or None)


def load_gfw(path=None) -> dict:
    return load_cache(path or GFW_PATH)


def save_gfw(data, path=None) -> None:
    save_cache(data, path or GFW_PATH)


def _jwt_kind(piece: str) -> str:
    """Что это за кусок JWT: header, payload или ничего (подпись)."""
    text = piece.strip()
    if not text:
        return ""
    for pad in ("", "=", "==", "==="):
        try:
            raw = base64.urlsafe_b64decode(text + pad).decode("utf-8")
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            if "alg" in data:
                return "header"
            if "aud" in data or "data" in data or "userId" in data:
                return "payload"
    return ""


def clean_gfw_token(value) -> str:
    """Токен GFW из поля ввода: убирает мусор и склеенные вставки.

    Токен — JWT из трёх частей. Если вставить его дважды или вставить поверх
    старого, в строке оказываются подпись, заголовок и полезная часть в
    произвольном порядке — такой токен сервер отвергает («invalid token»).
    Здесь он собирается заново: заголовок + полезная часть + самая длинная
    подпись. Правильный токен возвращается без изменений.
    """
    text = "".join(ch for ch in str(value or "") if not ch.isspace())
    if text.lower().startswith("bearer "):
        text = text[7:]
    text = text.strip('"').strip("'").strip()
    if not text:
        return ""
    parts = text.split(".")
    if len(parts) == 3 and all(_jwt_kind(part) in ("header", "", "payload") for part in parts):
        return text

    header = payload = ""
    # подпись ищется рядом с заголовком: при склеенной вставке она оказывается
    # в том же куске строки, что и заголовок, а не отдельным куском
    near_header = []
    far = []
    for part in parts:
        rest = re.sub(r"eyJ[A-Za-z0-9_-]+", "|", part)
        chunks = [chunk for chunk in rest.split("|") if len(chunk) >= 20]
        has_header = False
        for match in re.finditer(r"eyJ[A-Za-z0-9_-]+", part):
            piece = match.group(0)
            kind = _jwt_kind(piece)
            if kind == "header" and not header:
                header = piece
                has_header = True
            elif kind == "payload" and not payload:
                payload = piece
            else:
                far.append(piece)
        (near_header if has_header else far).extend(chunks)
    if header and payload:
        signature = max(near_header or far, key=len, default="")
        if signature:
            return f"{header}.{payload}.{signature}"
    return text


def gfw_profile_text(entry) -> str:
    """Строка с профилем судна из GFW: имя, флаг, IMO, тип, размеры, тоннаж."""
    entry = entry or {}
    parts = []
    name = _clean(entry.get("name"))
    flag = _clean(entry.get("flag"))
    imo = _clean(entry.get("imo"))
    callsign = _clean(entry.get("callsign"))
    label = _clean(entry.get("type_label"))
    if name:
        parts.append(name)
    if flag:
        parts.append(f"флаг {flag}")
    if imo:
        parts.append(f"IMO {imo}")
    if callsign:
        parts.append(f"позывной {callsign}")
    if label:
        parts.append(label)
    if entry.get("length_m"):
        parts.append(f"длина {entry['length_m']:.1f} м".replace(".0 м", " м"))
    if entry.get("tonnage_gt"):
        parts.append(f"{int(entry['tonnage_gt'])} GT")
    if entry.get("max_speed_kn"):
        parts.append(f"макс. {entry['max_speed_kn']} уз")
    text = "GFW: " + (" · ".join(parts) if parts else "профиль судна найден")
    checked = _clean(str(entry.get("checked") or "").replace("T", " ")[:16])
    if checked:
        text += f" (проверено {checked})"
    return text


def gfw_gap_text(entry) -> str:
    """Короткая фраза про последний разрыв AIS по данным GFW."""
    gap = (entry or {}).get("gap") or {}
    if not gap:
        return ""
    hours = gap.get("hours")
    when = _clean(gap.get("end"))
    parts = ["GFW: разрыв AIS"]
    if hours:
        parts.append(f"≈ {hours:.1f} ч")
    if when:
        parts.append(f"до {when}")
    where = gap.get("position_text")
    if where:
        parts.append(f"в точке {where}")
    return " ".join(parts)


# --------------------------------------------------------------- реестры

# Публичные карты судов: у Goradar и MarineTraffic.live есть машинный JSON,
# он уже читается потоками (см. services_feed). Остальные сайты данные только
# показывают в браузере, поэтому для них строим ссылки на карточку судна —
# открыть и посмотреть глазами. Oceanook и СпутникСервис не принимают поиск
# в адресе (у oceanook — защита от роботов), поэтому ведём на список судов.
TRACKING_SITES = [
    ("Goradar: позиция и карта", "https://goradar.ru/vessels_map_marinetraffic_mmsi_{mmsi}", True),
    ("MarineTraffic.live (Ship Radar 24)", "https://marinetraffic.live/vessels/{slug}/{mmsi}/", True),
    ("Ship-Tracking.net (карта MarineTraffic)", "https://ship-tracking.net/?s={mmsi}", True),
    ("ShippingExplorer: поиск судна", "https://www.shippingexplorer.net/ru/ships?search={mmsi}", True),
    ("Oceanook: список судов", "https://oceanook.com/vessels", False),
    ("СпутникСервис: система контроля флота", "https://sputnik-flot.ru/", False),
    ("FleetMon: поиск судна", "https://www.fleetmon.com/vessels/?search={mmsi}", True),
    ("Radar24: суда и самолёты (витрина)", "https://radar-24.ru/", False),
    ("Ships.com.ua (карта VesselFinder)", "https://www.ships.com.ua/", False),
    ("Rynda: биржа фрахта", "https://rynda.io/vessels?q={mmsi}", True),
    # ShipAIS ведёт только воды Великобритании: наших судов там нет.
    ("ShipAIS (воды Великобритании)", "https://shipais.uk/", False),
]


def tracking_links(mmsi=None, name=None) -> list[tuple[str, str]]:
    """Ссылки на публичные карты судов: [(подпись, url), …]."""
    mmsi_text = _clean(mmsi)
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-") or "x"
    links = []
    for title, pattern, needs_mmsi in TRACKING_SITES:
        if needs_mmsi and not mmsi_text:
            continue
        links.append((title, pattern.format(mmsi=mmsi_text, slug=slug)))
    return links


def registry_links(imo=None, mmsi=None) -> list[tuple[str, str]]:
    """Ссылки на реестры и портовые сводки: [(подпись, url), …]."""
    imo_text = _clean(imo)
    gisis = ("https://gisis.imo.org/Public/SHIPS/ShipDetails.aspx?IMONumber=" + imo_text
             if imo_text else "https://gisis.imo.org/Public/SHIPS/Default.aspx")
    # ITU MARS: форма поиска по MMSI/позывному (прямые ссылки на карточку
    # ITU не публикует — параметры меняются, и ссылка ведёт на 404).
    mars = "https://www.itu.int/mars/"
    return [
        ("Equasis (владелец, флаг, история)", "https://www.equasis.org/EquasisWeb/public/HomePage"),
        ("IMO GISIS (судно по IMO)", gisis),
        ("ITU MARS (MMSI, позывной)", mars),
        ("Global Fishing Watch (карта и профиль)", "https://globalfishingwatch.org/map/"),
        ("USPA: суда на рейде и судозаходы",
         "https://www.uspa.gov.ua/en/project/list-of-ships-on-a-roadstead"),
        ("APDM Galați: сводка по судам (PDF)",
         "https://apdmgalati.ro/actualizare/ccmnmfgalati.pdf"),
        ("APM Terminals: расписание судов",
         "https://www.apmterminals.com/track-and-trace/vessel-schedule"),
    ]


# ------------------------------------------------- источники без ключа API
#
# Эти сервисы не требуют ключа, но и не все годятся для автоматического
# чтения: у части таблицы рисует браузерный скрипт, у APDM — скан PDF.
# kind: ais — разбираем HTML-таблицу; pdf — проверяем файл; page — только
# доступность сайта (данные открываются глазами).

SOURCES = [
    {"key": "aci", "title": "AIS-catcher: ожидаемые прибытия", "kind": "ais",
     "url": EXPECTED_URL.format(locode="UAIZM"),
     "how": "читается автоматически: HTML-таблица Expected, ключ не нужен"},
    {"key": "uspa", "title": "USPA: суда на рейде и судозаходы", "kind": "page",
     "url": "https://www.uspa.gov.ua/en/project/list-of-ships-on-a-roadstead",
     "how": "только глазами: таблицы грузятся браузерным скриптом, API нет"},
    {"key": "apdm", "title": "APDM Galați: сводка по судам", "kind": "pdf",
     "url": "https://apdmgalati.ro/actualizare/ccmnmfgalati.pdf",
     "how": "только глазами: PDF-скан без текстового слоя"},
    {"key": "apm", "title": "APM Terminals: расписание судов", "kind": "page",
     "url": "https://www.apmterminals.com/track-and-trace/vessel-schedule",
     "how": "только глазами: интерактивное расписание, API нет"},
    {"key": "equasis", "title": "Equasis: владелец, флаг, история", "kind": "page",
     "url": "https://www.equasis.org/EquasisWeb/public/HomePage",
     "how": "только глазами: вход по логину и капче"},
    {"key": "gisis", "title": "IMO GISIS: карточка судна", "kind": "page",
     "url": "https://gisis.imo.org/Public/Default.aspx",
     "how": "только глазами: форма поиска, API нет"},
    {"key": "itu", "title": "ITU MARS: MMSI и позывной", "kind": "page",
     "url": "https://www.itu.int/mars/",
     "how": "только глазами: форма поиска, API нет"},
    # Публичные карты судов из списка пользователя. У Goradar и MarineTraffic.live
    # есть машинный JSON — они уже читаются потоками (см. services_feed) и суда
    # ставятся на карту; здесь только те сайты, у которых данных для чтения нет.
    {"key": "oceanook", "title": "Oceanook: отслеживание судов", "kind": "page",
     "url": "https://oceanook.com/",
     "how": "только глазами: защита от роботов, данные в браузере по токену страницы"},
    {"key": "sputnik", "title": "СпутникСервис (sputnik-flot.ru)", "kind": "page",
     "url": "https://sputnik-flot.ru/",
     "how": "только глазами: сайт услуги, открытой выдачи позиций нет"},
    {"key": "shiptracking", "title": "Ship-Tracking.net", "kind": "page",
     "url": "https://ship-tracking.net/",
     "how": "только глазами: показывает карту MarineTraffic во встроенном окне"},
    {"key": "fleetmon", "title": "FleetMon", "kind": "page",
     "url": "https://www.fleetmon.com/",
     "how": "нужен платный ключ API; сайт с этого компьютера не открывается (DNS)"},
    {"key": "shipais", "title": "ShipAIS.uk", "kind": "page",
     "url": "https://shipais.uk/",
     "how": "только глазами: карты и история только по водам Великобритании"},
    {"key": "shipexplorer", "title": "ShippingExplorer", "kind": "page",
     "url": "https://www.shippingexplorer.net/ru/map",
     "how": "только глазами: карта отдаёт позиции только с antiforgery-токеном браузера"},
    {"key": "liveshipsmap", "title": "LiveShipsMap", "kind": "page",
     "url": "https://liveshipsmap.com/",
     "how": "недоступен: домен не резолвится с этого компьютера"},
    {"key": "portmaps", "title": "Portmaps", "kind": "page",
     "url": "https://portmaps.com/",
     "how": "недоступен: сайт не отвечает на запросы"},
    {"key": "shipsua", "title": "Ships.com.ua", "kind": "page",
     "url": "https://www.ships.com.ua/",
     "how": "только глазами: показывает карту VesselFinder во встроенном окне"},
    {"key": "radar24", "title": "Radar24.ru", "kind": "page",
     "url": "https://radar-24.ru/",
     "how": "только глазами: витрина сервиса, карты судов на странице нет"},
    {"key": "rynda", "title": "Rynda.io", "kind": "page",
     "url": "https://rynda.io/",
     "how": "только глазами: биржа фрахта, публичного API нет"},
    # Eurostat CROS: проект статистики судозаходов на данных AIS агентства EMSA.
    # Это официальная статистика (квартальные оценки), а не позиции судов:
    # открытого API с координатами у неё нет, поэтому только проверка
    # доступности и ссылка — в поток позиций такая страница не годится.
    {"key": "cros", "title": "Eurostat CROS: статистика судов по AIS (EMSA)", "kind": "page",
     "url": ("https://cros.ec.europa.eu/book-page/"
             "estimation-maritime-vessel-traffic-using-automatic-identification-system-ais-data"),
     "how": "только глазами: официальная статистика порт-заходов Eurostat/EMSA, "
            "не позиции судов; API с координатами нет"},
]

SOURCES_BY_KEY = {source["key"]: source for source in SOURCES}


def _human_size(size) -> str:
    try:
        size = float(size)
    except (TypeError, ValueError):
        return ""
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:.1f} {unit}" if unit != "Б" else f"{int(size)} {unit}"
        size /= 1024
    return ""


def check_source(source, timeout=25, log=None) -> dict:
    """Проверка одного источника без ключа: жив ли и читается ли автоматически.

    ok=True — сайт отдаёт данные (или хотя бы открывается); warn=True — сайт
    жив, но закрыт от автоматических запросов (нужен браузер); ok=False — сбой.
    """
    import requests

    source = dict(source or {})
    key = source.get("key") or ""
    started = time.monotonic()
    result = {"key": key, "title": source.get("title") or key,
              "how": source.get("how") or "", "url": source.get("url") or "",
              "ok": False, "warn": False, "detail": "", "status": 0,
              "checked": datetime.now().isoformat(timespec="seconds")}
    try:
        response = requests.get(source.get("url"), headers=HEADERS, timeout=timeout,
                                stream=True)
        status = int(response.status_code)
        result["status"] = status
        if source.get("kind") == "ais" and status == 200:
            text = response.text
            rows = parse_expected_page(text)
            result["ok"] = bool(rows)
            result["detail"] = f"судов в списке {len(rows)}" if rows else "таблица пуста"
        elif source.get("kind") == "pdf":
            size = response.headers.get("Content-Length")
            head = next(response.iter_content(2048), b"") or b""
            is_pdf = head.lstrip()[:5] == b"%PDF-"
            parts = [f"HTTP {status}"]
            if is_pdf:
                parts.append("PDF")
                if size:
                    parts.append(_human_size(size))
                parts.append("скан, без текстового слоя")
            result["detail"] = ", ".join(parts)
            if status < 400 and is_pdf:
                # файл живой, но разобрать его автоматически нельзя
                result["warn"] = True
        else:
            result["detail"] = f"HTTP {status}"
            if status < 400:
                # сайт открывается, но таблицы/формы рисует браузер
                result["warn"] = True
                result["detail"] = f"HTTP {status}"
            elif 400 <= status < 500:
                result["warn"] = True
                result["detail"] = (f"HTTP {status}: сайт отвечает, но закрыт "
                                    "для автоматических запросов")
        response.close()
    except Exception as error:                      # сеть, DNS, таймаут
        result["detail"] = str(error).strip()[:120] or error.__class__.__name__
    result["seconds"] = round(time.monotonic() - started, 1)
    if log:
        state = "работает" if result["ok"] else "не отвечает" if not result["warn"] \
            else "только в браузере"
        log(f"Проверка источника {result['title']}: {state} — {result['detail']}")
    return result


def check_sources(keys=None, timeout=25, pause=0.5, log=None) -> list[dict]:
    """Проверяет все источники без ключа (или только указанные).

    Результат кладётся в data/sources.json, чтобы окно показало его сразу
    после запуска, без повторного обхода сайтов.
    """
    from concurrent.futures import ThreadPoolExecutor

    wanted = set(keys or [])
    chosen = [source for source in SOURCES
              if not wanted or source["key"] in wanted]
    if not chosen:
        return []
    # все сайты проверяются одновременно: обход не ждёт самый медленный
    workers = max(1, min(8, len(chosen)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda source: check_source(source, timeout=timeout,
                                                            log=log), chosen))
    save_sources(results)
    return results


def save_sources(results, path=None) -> None:
    save_cache({"updated": datetime.now().isoformat(timespec="seconds"),
                "sources": list(results or [])}, path or SOURCES_PATH)


def load_sources(path=None) -> dict:
    return load_cache(path or SOURCES_PATH)


def source_result(key, cache=None) -> dict | None:
    """Последний сохранённый результат проверки источника."""
    cache = cache if isinstance(cache, dict) else load_sources()
    for item in cache.get("sources") or []:
        if isinstance(item, dict) and item.get("key") == key:
            return item
    return None
