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


def fetch_expected(ports, timeout=45, pause=1.5, log=None) -> dict:
    """Ожидаемые прибытия по всем нашим портам: {locode: {name, vessels}}.

    Ошибка одного порта не мешает остальным: она попадает в поле error.
    """
    import requests
    import time

    result = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "ports": {}}
    for port in ports or []:
        locode = _clean((port or {}).get("locode")).upper()
        label = _clean((port or {}).get("label") or (port or {}).get("name"))
        if not locode:
            continue
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
        result["ports"][locode] = entry
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

def gfw_vessel(gfw, mmsi):
    """Данные GFW по MMSI из кэша (если профиль уже запрашивался)."""
    gfw = gfw if isinstance(gfw, dict) else load_gfw()
    return ((gfw.get("vessels") or {}).get(str(mmsi or "")) or None)


def load_gfw(path=None) -> dict:
    return load_cache(path or GFW_PATH)


def save_gfw(data, path=None) -> None:
    save_cache(data, path or GFW_PATH)


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
    wanted = set(keys or [])
    results = []
    for source in SOURCES:
        if wanted and source["key"] not in wanted:
            continue
        results.append(check_source(source, timeout=timeout, log=log))
        time.sleep(max(0.0, pause))
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
