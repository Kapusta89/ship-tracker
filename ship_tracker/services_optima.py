"""Четыре новых источника: Maritime Optima, ShipAtlas, MarineLink, ShipNext.

У всех четырёх нет ключа для карточки судна, кроме ShipNext: он открывает
каталог только по токену аккаунта. Поэтому каналы устроены так:

  * Maritime Optima (mo) — публичная карточка судна без входа: флаг, тип,
    постройка, размеры, осадка, позывной. Заполняет паспорт судна в базе;
  * ShipAtlas (sa) — то же приложение Maritime Optima, вторая его витрина:
    читает рейс (куда идёт), район, навигационный статус и свежесть AIS;
    порт назначения пишет только там, где AIS его не сообщил;
  * MarineLink (mlink) — открытый справочник судов по типам: заполняет тип
    у судов, где своего типа нет (сверка по точному имени);
  * ShipNext (sn) — каталог судов по токену аккаунта: /api/v1/vessels без
    токена отвечает 401, поэтому канал включается ключом.

Классы наследуют ServiceFeed из services_feed: тот модуль подключает этот
в конце, когда базовый класс уже определён.
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path

import ais_feed
import services_feed as base

BASE_DIR = Path(__file__).resolve().parent

MARITIMEOPTIMA_VESSEL_URL = ("https://maritimeoptima.com/public/vessels/pages/"
                             "imo:{imo}/mmsi:{mmsi}/{slug}.html")
MARITIMEOPTIMA_HOME = "https://maritimeoptima.com/"
MARITIMEOPTIMA_STATE = BASE_DIR / "data" / "maritimeoptima.json"
SHIPATLAS_STATE = BASE_DIR / "data" / "shipatlas.json"
SHIPNEXT_URL = "https://shipnext.com/api/v1/vessels"

# Типы в справочнике MarineLink: часть адреса страницы и подпись типа.
MARINELINK_TYPES = (
    ("barge", "Barge"),
    ("bulk_carrier", "Bulk Carrier"),
    ("chemical_carrier", "Chemical Carrier"),
    ("container_ship", "Container Ship"),
    ("general_cargo_carrier", "General Cargo Carrier"),
    ("oil_carrier", "Oil Carrier"),
    ("fishing", "Fishing"),
    ("tug", "Tug"),
    ("pilot", "Pilot"),
    ("dredging", "Dredging"),
    ("offshore_supply_vessel", "Offshore Supply Vessel"),
    ("passenger", "Passenger"),
)

_MO_PAGES = {}
_MO_TTL = 1800.0


def _page_text(html_text):
    """Текст страницы без разметки: по нему удобно искать поля регуляркой."""
    import html as html_module

    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html_text or "",
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return html_module.unescape(re.sub(r"\s+", " ", text)).strip()


def parse_maritimeoptima(html_text):
    """Разбирает публичную карточку судна Maritime Optima / ShipAtlas."""
    text = _page_text(html_text)
    card = {}
    match = re.search(r"is a (.+?) vessel built in (\d{4}) and sailing under the flag "
                      r"of (.+?)\.", text)
    if match:
        card["type_text"] = match.group(1).strip()
        card["year_built"] = int(match.group(2))
        card["flag"] = match.group(3).strip()
    for pattern, key, cast in (
            (r"LOA\) of ([\d,.]+) meters", "length_m", float),
            (r"beam of ([\d,.]+) meters", "beam_m", float),
            (r"deadweight capacity is ([\d,]+) tonnes", "dwt", int),
            (r"Draft \(summer\): ([\d.]+) m", "draft", float),
            (r"Callsign: ([A-Z0-9]+)", "callsign", str)):
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            card[key] = cast(match.group(1).replace(",", ""))
        except ValueError:
            continue
    match = re.search(r"is currently in (.+?) and reports its navigational status as "
                      r"(.+?), based on AIS data received (.+?)\.", text)
    if match:
        card["area"] = match.group(1).strip()
        card["nav_status"] = match.group(2).strip()
        card["seen_text"] = match.group(3).strip()
    else:
        match = re.search(r"is currently in (.+?), based on AIS data received (.+?)\.",
                          text)
        if match:
            card["area"] = match.group(1).strip()
            card["seen_text"] = match.group(2).strip()
    match = re.search(r"departed from (.+?) on (.+?) and is heading to (.+?),", text)
    if match:
        card["departed_from"] = match.group(1).strip()
        card["departed_at"] = match.group(2).strip()
        card["to_port"] = match.group(3).strip()
    elif "has no reported destination" in text:
        card["to_port"] = ""
    return card


def maritimeoptima_page(mmsi, imo, name, session=None, timeout=25):
    """Карточка судна с кэшем на полчаса: её читают два канала сразу."""
    mmsi = str(mmsi or "").strip()
    imo = str(imo or "").strip()
    if not mmsi or not imo:
        return {}
    cached = _MO_PAGES.get(mmsi)
    if cached and time.time() - cached[0] < _MO_TTL:
        return cached[1]
    import requests

    slug = re.sub(r"[^A-Za-z0-9]+", "_", str(name or "")).strip("_") or "vessel"
    url = MARITIMEOPTIMA_VESSEL_URL.format(imo=imo, mmsi=mmsi, slug=slug)
    headers = {"User-Agent": base.MAP_HEADERS["User-Agent"], "Accept-Language": "en"}
    response = (session or requests).get(url, headers=headers, timeout=timeout)
    if response.status_code == 404:
        _MO_PAGES[mmsi] = (time.time(), {})
        return {}
    response.raise_for_status()
    card = parse_maritimeoptima(response.text or "")
    card["url"] = url
    _MO_PAGES[mmsi] = (time.time(), card)
    return card


class MaritimeOptimaBase(base.ServiceFeed):
    """Общее для двух каналов: выбор судов, кэш попыток и сессия."""

    interval = 1800.0
    max_per_cycle = 60
    retry_hours = 24.0
    pause = 0.5
    state_path = MARITIMEOPTIMA_STATE

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session = None
        self.vessel_source = kwargs.get("vessels") or (lambda: [])
        self.attempts = self._load_attempts()
        if kwargs.get("minutes"):
            self.interval = max(300.0, float(kwargs["minutes"]) * 60.0)

    def _load_attempts(self):
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_attempts(self, data=None):
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(data or self.attempts,
                                                 ensure_ascii=False),
                                       encoding="utf-8")
        except OSError as error:
            self.log(f"{self.title}: состояние не сохранено ({error})")

    def http(self):
        # Главную страницу не открываем: сайт отвечает на неё кодом 103 (Early
        # Hints) с пустым телом, после чего сессия отдаёт пустые карточки.
        # Карточки читаются и без прогрева — прямо по адресу судна.
        import requests
        if self.session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": base.MAP_HEADERS["User-Agent"],
                                    "Accept-Language": "en"})
            self.session = session
        return self.session

    def candidates(self):
        """Суда с IMO, которые ещё не читали: без IMO адрес карточки не собрать."""
        try:
            items = self.vessel_source() or []
        except Exception:
            return []
        now = time.time()
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            mmsi = str(item.get("mmsi") or "").strip()
            imo = str(item.get("imo") or "").strip()
            if not mmsi or not imo:
                continue
            seen = self.attempts.get(mmsi)
            if isinstance(seen, dict):
                at = float(seen.get("at") or 0)
                limit = self.retry_hours * 3600 if seen.get("ok") else 6 * 3600
                if 0 < now - at < limit:
                    continue
            result.append(item)
            if len(result) >= self.max_per_cycle:
                break
        return result


class MaritimeOptimaFeed(MaritimeOptimaBase):
    """Паспорт судна из публичной карточки Maritime Optima (без ключа)."""

    key = "mo"
    interval = 1800.0
    state_path = MARITIMEOPTIMA_STATE

    def poll_once(self):
        session = self.http()
        saved = errors = 0
        for item in self.candidates():
            if self.stop_event.is_set():
                break
            mmsi = str(item.get("mmsi") or "")
            try:
                card = maritimeoptima_page(mmsi, item.get("imo"), item.get("name"),
                                           session=session)
            except Exception as error:
                errors += 1
                self.attempts[mmsi] = {"at": time.time(), "ok": False}
                if errors <= 3:
                    self.log(f"{self.title}: {mmsi} — {error}")
                continue
            self.attempts[mmsi] = {"at": time.time(), "ok": bool(card)}
            if not card:
                continue
            try:
                code = ais_feed.type_code_for_label(card.get("type_text"))
            except Exception:
                code = None
            self.emit({"kind": "static", "mmsi": mmsi,
                       "name": card.get("name") or item.get("name") or "",
                       "imo": str(item.get("imo") or ""),
                       "callsign": card.get("callsign") or "",
                       "type_code": code,
                       "type_label": card.get("type_text") or "",
                       "destination": "",
                       "draft": card.get("draft"),
                       "length_m": card.get("length_m"),
                       "beam_m": card.get("beam_m"),
                       "eta_declared": None,
                       "at": datetime.now(),
                       "source": "mo"})
            saved += 1
            time.sleep(self.pause)
        self._save_attempts()
        self.ok(saved, f"ошибок {errors}")


class ShipAtlasFeed(MaritimeOptimaBase):
    """Рейс судна из публичной карточки ShipAtlas (то же приложение).

    В базу пишется только порт назначения и только там, где AIS молчит.
    Остальное (район, навигационный статус, свежесть сигнала) кладётся в
    data/shipatlas.json — по нему видно, что судно стоит на якоре, хотя
    своих координат оно давно не передавало.
    """

    key = "sa"
    interval = 2400.0
    max_per_cycle = 60
    state_path = SHIPATLAS_STATE

    def poll_once(self):
        session = self.http()
        saved = errors = 0
        cache = self._load_attempts()
        for item in self.candidates():
            if self.stop_event.is_set():
                break
            mmsi = str(item.get("mmsi") or "")
            try:
                card = maritimeoptima_page(mmsi, item.get("imo"), item.get("name"),
                                           session=session)
            except Exception as error:
                errors += 1
                if errors <= 3:
                    self.log(f"{self.title}: {mmsi} — {error}")
                continue
            if not card:
                continue
            cache[mmsi] = {"name": item.get("name") or "",
                           "imo": item.get("imo") or "",
                           "area": card.get("area") or "",
                           "status": card.get("nav_status") or "",
                           "seen": card.get("seen_text") or "",
                           "to_port": card.get("to_port") or "",
                           "updated": datetime.now().isoformat(timespec="seconds")}
            destination = str(card.get("to_port") or "").strip()
            # своё назначение AIS не перебиваем: карточка нужна там, где пусто
            if destination and not str(item.get("destination") or "").strip():
                self.emit({"kind": "static", "mmsi": mmsi,
                           "destination": destination, "at": datetime.now(),
                           "source": "sa"})
                saved += 1
            time.sleep(self.pause)
        self._save_attempts(cache)
        self.ok(saved, f"ошибок {errors}")


class MarineLinkTypesFeed(base.ServiceFeed):
    """Тип судна из открытого справочника MarineLink (без ключа).

    Справочник держит списки судов по типам: читаем страницы типов и
    заполняем тип там, где своего нет. Имена сверяются точно, поэтому
    чужие данные в базу не попадают.
    """

    key = "mlink"
    interval = 21600.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session = None
        self.vessel_source = kwargs.get("vessels") or (lambda: [])
        self.types = {}

    def http(self):
        import requests
        if self.session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": base.MAP_HEADERS["User-Agent"]})
            self.session = session
        return self.session

    def load_types(self):
        session = self.http()
        for slug, label in MARINELINK_TYPES:
            if self.stop_event.is_set():
                break
            try:
                response = session.get("http://directory.marinelink.com/ships/type-"
                                       + slug, timeout=30)
                if response.status_code == 429:
                    # справочник просит сбавить темп: не долбим остальные
                    # 198 страниц подряд, иначе лимит только углубится
                    minutes = max(1, int(self.rate_limit_for // 60))
                    self.blocked_until = max(self.blocked_until,
                                             time.monotonic() + self.rate_limit_for)
                    self.set_status(f"лимит запросов, ждём {minutes} мин")
                    self.log(f"{self.title}: справочник ограничил частоту запросов "
                             f"(HTTP 429) — пауза {minutes} мин, типы остаются прежние.")
                    return
                response.raise_for_status()
            except Exception as error:
                self.log(f"{self.title}: тип {label} не прочитан ({error})")
                continue
            for name in re.findall(r"<h3[^>]*>(.*?)</h3>", response.text or "", re.S):
                clean = re.sub(r"<[^>]+>", "", name).strip()
                if clean and clean.lower() != "filters":
                    self.types.setdefault(clean.upper(), label)
            time.sleep(0.4)

    def poll_once(self):
        self.load_types()
        if not self.types:
            raise RuntimeError("справочник MarineLink не отдал списки судов")
        try:
            items = self.vessel_source() or []
        except Exception:
            items = []
        saved = 0
        for item in items:
            if not isinstance(item, dict) or item.get("type_label"):
                continue
            name = str(item.get("name") or "").strip().upper()
            mmsi = str(item.get("mmsi") or "").strip()
            label = self.types.get(name)
            if not name or not mmsi or not label:
                continue
            try:
                code = ais_feed.type_code_for_label(label)
            except Exception:
                code = None
            self.emit({"kind": "static", "mmsi": mmsi, "type_code": code,
                       "type_label": label, "at": datetime.now(), "source": "mlink"})
            saved += 1
        self.ok(saved, f"типов в справочнике {len(self.types)}")


class ShipNextFeed(base.ServiceFeed):
    """ShipNext: поиск судна в каталоге по токену аккаунта.

    Открыт только /api/v1/ports, а /api/v1/vessels отвечает 401 без токена.
    Токен берётся в личном кабинете shipnext.com и вставляется в поле ключа.
    """

    key = "sn"
    interval = 3600.0
    max_per_cycle = 40
    pause = 0.7

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.vessel_source = kwargs.get("vessels") or (lambda: [])

    def poll_once(self):
        token = (self.api_key or "").strip()
        if not token:
            raise RuntimeError("не задан токен аккаунта ShipNext")
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        import requests

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        saved = errors = 0
        for mmsi in (self.vessel_source() or [])[:self.max_per_cycle]:
            if self.stop_event.is_set():
                break
            try:
                response = requests.get(SHIPNEXT_URL, params={"search": str(mmsi)},
                                        headers=headers, timeout=25)
            except Exception as error:
                errors += 1
                if errors <= 3:
                    self.log(f"{self.title}: {mmsi} — {error}")
                continue
            if response.status_code in (401, 403):
                raise RuntimeError("ShipNext не принял токен: нужен токен аккаунта "
                                   "(Authorization: Bearer)")
            if response.status_code == 429:
                raise RuntimeError("ShipNext: слишком много запросов, пауза")
            if response.status_code >= 400:
                errors += 1
                continue
            try:
                data = response.json() or {}
            except ValueError:
                continue
            rows = data.get("data") if isinstance(data, dict) else None
            for row in (rows or [])[:5]:
                if not isinstance(row, dict):
                    continue
                found = str(row.get("mmsi") or row.get("MMSI") or "").strip()
                if found and found != str(mmsi):
                    continue
                name = ais_feed.clean_text(row.get("name") or row.get("vesselName")) or ""
                imo = str(row.get("imo") or row.get("IMO") or "").strip()
                if not name and not imo:
                    continue
                self.emit({"kind": "static", "mmsi": str(mmsi), "name": name,
                           "imo": imo, "at": datetime.now(), "source": "sn"})
                saved += 1
                break
            time.sleep(self.pause)
        self.ok(saved, f"ошибок {errors}")


# Регистрация каналов: services_feed уже загружен (его класс ServiceFeed здесь
# базовый), поэтому просто дописываем себя в его таблицу провайдеров.
base.PROVIDERS.update({
    "mo": MaritimeOptimaFeed,
    "sa": ShipAtlasFeed,
    "mlink": MarineLinkTypesFeed,
    "sn": ShipNextFeed,
})
