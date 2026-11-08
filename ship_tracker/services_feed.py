"""Дополнительные подключения: данные из HTTP-API внешних AIS-сервисов.

Каждый сервис опрашивается своим потоком и отдаёт те же события
(position / static), что и живые потоки AISStream и Open Waters, поэтому
данные сразу попадают в общую базу, на карту, в «Все суда» и в «Стоянки».

Что реально умеет каждый сервис (проверено по документации):

  * Digitraffic (Финляндия) — открытый API без ключа, район только Балтика;
  * MyShipTracking — триал на 2000 кредитов: расширенный ответ /vessel стоит
    3 кредита за судно, поэтому канал не сканирует район, а обогащает суда,
    которые уже есть в базе, и не выходит за дневной лимит кредитов;
  * VesselFinder — API по ключу, /livedata отдаёт суда района подписки;
  * MarineTraffic — API по ключу, район задаётся в личном кабинете.

Добавить новый сервис — значит дописать здесь класс с методом poll_once
и зарегистрировать его в PROVIDERS: остальная цепочка уже готова.
"""

import json
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import geo
import portcalls

# Названия для журнала и статусов.
SERVICE_TITLES = {
    "aci": "AIS-catcher: ожидаемые прибытия",
    "gfw": "Global Fishing Watch (разрывы AIS)",
    "dat": "Datalastic (позиция и рейс)",
    "aishub": "AISHub (сеть приёмников)",
    "vf": "VesselFinder",
    "vfa": "VesselFinder: ожидаемые прибытия",
    "mt": "MarineTraffic",
    "mst": "MyShipTracking",
    "dt": "Digitraffic",
    "gor": "Goradar (карта судов)",
    "mlive": "MarineTraffic.live (карта судов)",
    "srd": "Ship Radar 24 (карточки судов)",
}

DIGITRAFFIC_URL = "https://meri.digitraffic.fi/api/ais/v1/locations"
VESSELFINDER_URL = "https://api.vesselfinder.com/livedata"
VESSELFINDER_ARRIVALS_URL = "https://api.vesselfinder.com/expectedarrivals"
MARINETRAFFIC_URL = "https://services.marinetraffic.com/api/exportvessels-custom-area"
MYSHIPTRACKING_URL = "https://api.myshiptracking.com/api/v2/vessel/zone"
MYSHIPTRACKING_VESSEL_URL = "https://api.myshiptracking.com/api/v2/vessel"
DATALASTIC_URL = "https://api.datalastic.com/api/v0/vessel"
AISHUB_URL = "https://data.aishub.net/ws.php"

# Публичные карты судов: те же данные, что видны на сайте в браузере.
# Ключ не нужен, но отдаются порциями, поэтому район опрашивается клетками.
GORADAR_HOME = "https://goradar.ru/"
GORADAR_URL = "https://goradar.ru/api/chunk_tile.php"
MARINETRAFFIC_LIVE_HOME = "https://marinetraffic.live/"
# Карта сайта берёт суда из своего JSON-моста (раньше был /realtime_ais,
# теперь он отдаёт пустой список). Район задаётся точкой и радиусом.
MARINETRAFFIC_LIVE_URL = "https://marinetraffic.live/bridge/vessels/nearby"
# Карточка судна на том же сайте (HTML): тип, позывной, размеры, осадка,
# порт назначения. Позиции берём JSON-мостом, а паспорт — отсюда.
SHIPRADAR_CARD_URL = "https://marinetraffic.live/vessels/{slug}/{mmsi}/"
SHIPRADAR_STATE = Path(__file__).resolve().parent / "data" / "shipradar_cards.json"
MAP_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


def _grid_boxes(bboxes, step=2.0, max_cells=64):
    """Район подписки → сетка клеток: публичные карты отдают порцию судов на запрос."""
    cells = []
    for box in bboxes or []:
        try:
            (lat1, lon1), (lat2, lon2) = box
            south, north = sorted((float(lat1), float(lat2)))
            west, east = sorted((float(lon1), float(lon2)))
        except (TypeError, ValueError):
            continue
        if north <= south or east <= west:
            continue
        lat = south
        while lat < north and len(cells) < max_cells:
            lon = west
            while lon < east and len(cells) < max_cells:
                cells.append((round(lat, 3), round(lon, 3),
                              round(min(lat + step, north), 3),
                              round(min(lon + step, east), 3)))
                lon += step
            lat += step
    return cells


def _number(value):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _mmsi(value):
    if value is None:
        return None
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    text = "".join(ch for ch in text if ch.isdigit())
    if not text:
        return None
    return text.zfill(9)


def _heading(value):
    value = _number(value)
    if value is None:
        return None
    value = int(value)
    return value if 0 <= value <= 359 else None


def _time(value):
    """Время из ISO-строки сервиса в локальное время без часового пояса."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment


def _position(mmsi, lat, lon, sog=None, cog=None, heading=None, name=None, source="",
              at=None, type_label=None):
    lat, lon = _number(lat), _number(lon)
    if lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None
    if abs(lat) < 0.001 and abs(lon) < 0.001:
        return None
    return {
        "kind": "position",
        "mmsi": mmsi,
        "lat": lat,
        "lon": lon,
        "sog": _number(sog),
        "cog": _number(cog),
        "heading": heading,
        "name": name or "",
        "type_label": type_label or "",
        "at": at or datetime.now(),
        "source": source,
    }


def _static(mmsi, name=None, imo=None, callsign=None, type_code=None,
            destination=None, draft=None, length_m=None, beam_m=None, source="",
            eta_declared=None, at=None):
    return {
        "kind": "static",
        "mmsi": mmsi,
        "name": name or "",
        "imo": str(imo or "").strip(),
        "callsign": str(callsign or "").strip(),
        "type_code": type_code,
        "destination": destination or "",
        "draft": _number(draft),
        "length_m": _number(length_m),
        "beam_m": _number(beam_m),
        "eta_declared": eta_declared,
        "at": at or datetime.now(),
        "source": source,
    }


def _type_code(value):
    number = _number(value)
    if number is None:
        return None
    return int(number)


class ServiceFeed(threading.Thread):
    """Опрос одного внешнего сервиса с паузой между запросами."""

    key = ""
    interval = 300.0
    # внешний API, а не AIS-поток: не участвует в состоянии «поток на связи»
    service = True

    def __init__(self, on_event, log, stop_event, api_key="", bboxes=None,
                 on_status=None, name=None, **options):
        super().__init__(daemon=True, name=name or f"service-{self.key}")
        self.on_event = on_event
        self.log = log or (lambda message: None)
        self.stop_event = stop_event
        self.api_key = (api_key or "").strip()
        self.bboxes = list(bboxes or [])
        self.on_status = on_status or (lambda key, text: None)
        self.status = "не подключен"
        self.events = 0
        self.last_error = ""
        self.last_count = -1

    # ------------------------------------------------------------- служебное

    @property
    def title(self):
        return SERVICE_TITLES.get(self.key, self.key)

    def set_status(self, text):
        if text != self.status:
            self.status = text
            self.on_status(self.key, text)

    def emit(self, event):
        if not event:
            return
        self.events += 1
        self.on_event(event)

    def sleep_until_next(self):
        return self.stop_event.wait(self.interval)

    def fail(self, error):
        text = str(error).strip() or error.__class__.__name__
        if text != self.last_error:
            self.last_error = text
            self.log(f"{self.title}: {text}")
        self.set_status(f"ошибка: {text[:60]}")

    def ok(self, count, detail=""):
        self.last_error = ""
        tail = f", {detail}" if detail else ""
        self.set_status(f"подключен, судов {count}{tail}")
        if count != self.last_count:
            self.last_count = count
            self.log(f"{self.title}: получено судов {count}{tail}.")

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.poll_once()
            except Exception as error:  # сеть, лимит, формат — ждём следующий цикл
                self.fail(error)
            if self.sleep_until_next():
                break

    # -------------------------------------------------------------- запросы

    def request(self, url, params=None, headers=None, timeout=30):
        import requests

        merged = {"User-Agent": "ShipTracker/1.0"}
        merged.update(headers or {})
        response = requests.get(url, params=params, headers=merged, timeout=timeout)
        response.raise_for_status()
        return response.json()

    def boxes_lat_lon(self):
        """Районы подписки [[[lat, lon], [lat, lon]], …]."""
        result = []
        for box in self.bboxes:
            try:
                (lat1, lon1), (lat2, lon2) = box
            except (TypeError, ValueError):
                continue
            try:
                result.append((float(lat1), float(lon1), float(lat2), float(lon2)))
            except (TypeError, ValueError):
                continue
        return result

    def poll_once(self):
        raise NotImplementedError


class DigitrafficFeed(ServiceFeed):
    """Открытые данные Финляндии: ключ не нужен, район — Балтика."""

    key = "dt"
    interval = 60.0

    def poll_once(self):
        data = self.request(
            DIGITRAFFIC_URL,
            headers={"Digitraffic-User": "ShipTracker/1.0",
                     "Accept-Encoding": "gzip"},
            timeout=40)
        features = data.get("features") if isinstance(data, dict) else None
        if not isinstance(features, list):
            raise RuntimeError("неожиданный ответ сервера")

        boxes = self.boxes_lat_lon()
        count = 0
        for feature in features:
            props = feature.get("properties") or {}
            coords = (feature.get("geometry") or {}).get("coordinates") or []
            if len(coords) < 2:
                continue
            mmsi = _mmsi(props.get("mmsi") or feature.get("mmsi"))
            if not mmsi:
                continue
            lon, lat = _number(coords[0]), _number(coords[1])
            if lat is None or lon is None:
                continue
            if boxes and not any(
                    min(a, c) <= lat <= max(a, c) and min(b, d) <= lon <= max(b, d)
                    for a, b, c, d in boxes):
                continue
            count += 1
            self.emit(_position(mmsi, lat, lon,
                                sog=props.get("sog"), cog=props.get("cog"),
                                heading=_heading(props.get("heading")),
                                source="digitraffic"))
        detail = "вне района подписки (Балтика)" if count == 0 else ""
        self.ok(count, detail)


class ApiError(RuntimeError):
    """Отказ API с машинным кодом и HTTP-статусом."""

    def __init__(self, message, code="", status=0):
        super().__init__(message)
        self.code = code or ""
        self.status = int(status or 0)


class MyShipTrackingFeed(ServiceFeed):
    """API v2: расширенные данные по нашим судам с учётом кредитов триала.

    Триал даёт 2000 кредитов на 10 дней, расширенный ответ стоит 3 кредита
    за судно. Сканировать район бессмысленно и дорого (там сотни чужих
    судов), поэтому канал обогащает те суда, что уже попали в базу:
    назначение, ETA, последний/следующий порт, осадка, габариты, флаг.
    Дневной лимит (mst_daily_coins) растягивает кредиты на весь срок.
    """

    key = "mst"
    interval = 7200.0

    def __init__(self, *args, vessels=None, minutes=None, daily_coins=None,
                 max_vessels=None, state_path=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.vessels = vessels or (lambda: [])
        self.daily_coins = max(0, int(daily_coins or 0))
        self.max_vessels = max(1, int(max_vessels or 40))
        self.state_path = Path(state_path) if state_path else None
        self.exhausted = False
        if minutes:
            self.interval = max(300.0, float(minutes) * 60.0)

    # ------------------------------------------------------- кредиты триала

    def credit_state(self):
        if not self.state_path:
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def spent_today(self):
        day = self.credit_state().get("mst") or {}
        if day.get("date") != datetime.now().strftime("%Y-%m-%d"):
            return 0
        try:
            return int(day.get("spent") or 0)
        except (TypeError, ValueError):
            return 0

    def spent_total(self):
        try:
            return int((self.credit_state().get("mst") or {}).get("total") or 0)
        except (TypeError, ValueError):
            return 0

    def add_coins(self, coins):
        if coins <= 0:
            return
        state = self.credit_state()
        today = datetime.now().strftime("%Y-%m-%d")
        day = state.get("mst") if isinstance(state.get("mst"), dict) else {}
        if day.get("date") != today:
            day = {"date": today, "total": day.get("total") or 0}
        day["spent"] = int(day.get("spent") or 0) + coins
        day["total"] = int(day.get("total") or 0) + coins
        state["mst"] = day
        if not self.state_path:
            return
        try:
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            pass

    # -------------------------------------------------------------- запросы

    def fetch_vessel(self, mmsi):
        """Один расширенный ответ по MMSI: (данные, списанные кредиты)."""
        import requests

        response = requests.get(
            MYSHIPTRACKING_VESSEL_URL,
            params={"mmsi": mmsi, "response": "extended"},
            headers={"x-api-key": self.api_key, "Accept": "application/json",
                     "User-Agent": "ShipTracker/1.0"},
            timeout=30)
        try:
            charged = int(response.headers.get("X-Credit-Charged") or 0)
        except (TypeError, ValueError):
            charged = 0
        try:
            payload = response.json()
        except ValueError:
            raise ApiError(f"HTTP {response.status_code}: неожиданный ответ",
                           status=response.status_code)
        if not isinstance(payload, dict) or payload.get("status") != "success":
            code = str(payload.get("code") or "") if isinstance(payload, dict) else ""
            message = str(payload.get("message") or "") if isinstance(payload, dict) else ""
            raise ApiError(message or code or f"HTTP {response.status_code}",
                           code=code, status=response.status_code)
        row = payload.get("data")
        return (row if isinstance(row, dict) else None), charged

    def emit_vessel(self, row):
        mmsi = _mmsi(row.get("mmsi"))
        if not mmsi:
            return False
        name = str(row.get("vessel_name") or "").strip()
        at = _time(row.get("received"))
        position = _position(mmsi, row.get("lat"), row.get("lng"),
                             sog=row.get("speed"), cog=row.get("course"),
                             name=name, source="myshiptracking", at=at)
        if position is None:
            return False
        self.emit(position)

        length = beam = None
        a, b = _number(row.get("size_a")), _number(row.get("size_b"))
        if a is not None and b is not None and a + b > 0:
            length = a + b
        c, d = _number(row.get("size_c")), _number(row.get("size_d"))
        if c is not None and d is not None and c + d > 0:
            beam = c + d
        eta = _time(row.get("eta"))
        self.emit(_static(mmsi, name=name,
                          imo=row.get("imo") or None,
                          callsign=row.get("callsign") or None,
                          # vtype — это группа 0-9, а не код AIS: берём ais_type
                          type_code=_type_code(row.get("ais_type")),
                          destination=row.get("destination") or None,
                          draft=row.get("draught"),
                          length_m=length, beam_m=beam,
                          eta_declared=eta.isoformat(timespec="minutes") if eta else None,
                          source="myshiptracking", at=at))
        return True

    def poll_once(self):
        if not self.api_key:
            raise RuntimeError("не задан ключ API")
        if self.exhausted:
            self.set_status("кредиты триала закончились")
            return
        mmsis = []
        for value in self.vessels() or []:
            mmsi = _mmsi(value)
            if mmsi and mmsi not in mmsis:
                mmsis.append(mmsi)
            if len(mmsis) >= self.max_vessels:
                break
        if not mmsis:
            self.set_status("нет судов в базе для обогащения")
            return
        spent = self.spent_today()
        if self.daily_coins and spent >= self.daily_coins:
            self.set_status(f"дневной лимит кредитов: {spent}/{self.daily_coins}")
            return
        enriched = 0
        coins = 0
        last_error = None
        for mmsi in mmsis:
            if self.daily_coins and spent + coins + 3 > self.daily_coins:
                break
            try:
                row, charged = self.fetch_vessel(mmsi)
            except ApiError as error:
                if error.code == "ERR_NO_CREDITS":
                    self.add_coins(coins)
                    self.exhausted = True
                    self.set_status("кредиты триала закончились")
                    self.log(f"{self.title}: кредиты триала закончились — обогащение остановлено.")
                    return
                if error.code in ("ERR_INVALID_KEY", "ERR_NO_KEY", "ERR_NOACCESS"):
                    self.add_coins(coins)
                    raise RuntimeError(str(error))
                if error.code == "ERR_RATE_LIMIT":
                    time.sleep(5)
                    continue
                last_error = error
                continue
            except Exception as error:
                last_error = error
                continue
            coins += charged
            if row and self.emit_vessel(row):
                enriched += 1
            time.sleep(0.4)  # 90 запросов в минуту на триале — с запасом
        self.add_coins(coins)
        spent += coins
        if not enriched and last_error is not None:
            raise last_error
        self.ok(enriched, f"кредитов сегодня {spent}/{self.daily_coins}, "
                          f"всего {self.spent_total()}")


class VesselFinderFeed(ServiceFeed):
    """API VesselFinder: /livedata отдаёт суда района подписки."""

    key = "vf"
    interval = 300.0

    def poll_once(self):
        if not self.api_key:
            raise RuntimeError("не задан ключ API")
        data = self.request(VESSELFINDER_URL,
                            params={"userkey": self.api_key, "format": "json"},
                            timeout=60)
        if isinstance(data, dict):
            error = data.get("error") or data.get("message")
            if error:
                raise RuntimeError(str(error))
            data = data.get("vessels") or data.get("data") or []
        if not isinstance(data, list):
            raise RuntimeError("неожиданный ответ сервера")

        count = 0
        for row in data:
            ais = row.get("AIS") if isinstance(row, dict) else None
            if not isinstance(ais, dict):
                continue
            mmsi = _mmsi(ais.get("MMSI"))
            if not mmsi:
                continue
            count += 1
            self.emit(_position(mmsi, ais.get("LATITUDE"), ais.get("LONGITUDE"),
                                sog=ais.get("SPEED"), cog=ais.get("COURSE"),
                                heading=_heading(ais.get("HEADING")),
                                name=ais.get("NAME"),
                                source="vesselfinder"))
            master = row.get("MASTERDATA") or {}
            self.emit(_static(mmsi, name=ais.get("NAME") or master.get("NAME"),
                              imo=ais.get("IMO") or master.get("IMO"),
                              callsign=ais.get("CALLSIGN"),
                              type_code=_type_code(ais.get("TYPE")),
                              destination=ais.get("DESTINATION"),
                              draft=ais.get("DRAUGHT") or master.get("MAXDRAUGHT"),
                              length_m=master.get("LENGTH"),
                              beam_m=master.get("BEAM"),
                              source="vesselfinder"))
        self.ok(count)


class VesselFinderArrivals(ServiceFeed):
    """Ожидаемые прибытия: суда, у которых в Destination стоит наш порт.

    Это единственный канал, который находит цель до того, как она подойдёт
    к порту: /expectedarrivals отдаёт суда по UN/LOCODE порта, независимо от
    расстояния до него. Стоит 5 кредитов за каждую запись AIS (PortCall и
    Voyage — отдельно), поэтому опрос редкий и с дневным лимитом кредитов.

    Ключ тот же, что у VesselFinder (vf_key), но включение отдельное: у одних
    есть подписка LiveData, у других — купленные кредиты.
    """

    key = "vfa"
    interval = 43200.0          # 12 часов по умолчанию
    CREDITS_PER_RECORD = 5     # AIS dataset в ExpectedArrivals

    def __init__(self, *args, ports=None, lookahead_minutes=None,
                 max_records=None, daily_coins=None, state_path=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.ports = list(ports or []) or list(geo.DEFAULT_PORTS)
        self.lookahead = max(60, int(lookahead_minutes or 720))
        self.max_records = max(1, int(max_records or 40))
        self.daily_coins = max(0, int(daily_coins or 0))
        self.state_path = Path(state_path) if state_path else None
        if kwargs.get("minutes"):
            self.interval = max(900.0, float(kwargs["minutes"]) * 60.0)

    # ------------------------------------------------------------- кредиты

    def credit_state(self):
        if not self.state_path:
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def spent_today(self):
        day = self.credit_state().get("vfa") or {}
        if day.get("date") != datetime.now().strftime("%Y-%m-%d"):
            return 0
        try:
            return int(day.get("spent") or 0)
        except (TypeError, ValueError):
            return 0

    def add_coins(self, coins):
        if coins <= 0 or not self.state_path:
            return
        state = self.credit_state()
        today = datetime.now().strftime("%Y-%m-%d")
        day = state.get("vfa") if isinstance(state.get("vfa"), dict) else {}
        if day.get("date") != today:
            day = {"date": today, "total": day.get("total") or 0}
        day["spent"] = int(day.get("spent") or 0) + coins
        day["total"] = int(day.get("total") or 0) + coins
        state["vfa"] = day
        try:
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            pass

    # ------------------------------------------------------------ запросы

    def fetch_port(self, locode):
        data = self.request(
            VESSELFINDER_ARRIVALS_URL,
            params={"userkey": self.api_key, "locode": locode,
                    "interval": self.lookahead, "format": "json"},
            timeout=60)
        if isinstance(data, dict):
            error = data.get("error") or data.get("message")
            if error:
                raise RuntimeError(str(error))
            data = data.get("vessels") or data.get("data") or []
        if not isinstance(data, list):
            raise RuntimeError("неожиданный ответ сервера")
        return data

    def emit_arrival(self, row, port_name):
        ais = row.get("AIS") if isinstance(row, dict) else None
        if not isinstance(ais, dict):
            return False
        mmsi = _mmsi(ais.get("MMSI"))
        if not mmsi:
            return False
        at = _time(ais.get("TIMESTAMP"))
        position = _position(mmsi, ais.get("LATITUDE"), ais.get("LONGITUDE"),
                             sog=ais.get("SPEED"), cog=ais.get("COURSE"),
                             heading=_heading(ais.get("HEADING")),
                             name=ais.get("NAME"), source="vesselfinder-arrivals",
                             at=at)
        if position is None:
            return False
        self.emit(position)
        eta = _time(ais.get("ETA"))
        destination = ais.get("DESTINATION") or port_name
        self.emit(_static(mmsi, name=ais.get("NAME"), imo=ais.get("IMO"),
                          callsign=ais.get("CALLSIGN"),
                          type_code=_type_code(ais.get("TYPE")),
                          destination=destination,
                          draft=ais.get("DRAUGHT"),
                          eta_declared=eta.isoformat(timespec="minutes") if eta else None,
                          source="vesselfinder-arrivals", at=at))
        return True

    def poll_once(self):
        if not self.api_key:
            raise RuntimeError("не задан ключ API")
        ports = [port for port in self.ports if (port.get("locode") or "").strip()]
        if not ports:
            self.set_status("нет портов с UN/LOCODE")
            return
        spent = self.spent_today()
        if self.daily_coins and spent >= self.daily_coins:
            self.set_status(f"дневной лимит кредитов: {spent}/{self.daily_coins}")
            return

        found = 0
        credits = 0
        for port in ports:
            if self.stop_event.is_set():
                break
            if found >= self.max_records:
                break
            if self.daily_coins and spent + credits + self.CREDITS_PER_RECORD > self.daily_coins:
                break
            rows = self.fetch_port(str(port["locode"]).strip())
            credits += len(rows) * self.CREDITS_PER_RECORD
            for row in rows:
                if found >= self.max_records:
                    break
                if self.emit_arrival(row, port.get("name") or ""):
                    found += 1
            time.sleep(0.4)

        self.add_coins(credits)
        spent += credits
        detail = (f"портов {len(ports)}, кредитов сегодня {spent}"
                  + (f"/{self.daily_coins}" if self.daily_coins else ""))
        self.ok(found, detail)


class AiscatcherExpected(ServiceFeed):
    """Ожидаемые прибытия из сети AIS-catcher: бесплатно, без ключа.

    Страница порта во вкладке Expected отдаёт таблицу судов, у которых
    в AIS записан наш порт, с заявленным ETA и временем последнего сигнала.
    Канал не пишет в базу позиции (их там нет), а обновляет файл
    data/expected.json — его читают вкладка «Ожидаемые» и бот.
    """

    key = "aci"
    interval = 3600.0

    def __init__(self, *args, ports=None, cache_path=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.ports = list(ports or []) or list(geo.DEFAULT_PORTS)
        self.cache_path = Path(cache_path) if cache_path else portcalls.EXPECTED_PATH
        if kwargs.get("minutes"):
            self.interval = max(900.0, float(kwargs["minutes"]) * 60.0)

    def poll_once(self):
        data = portcalls.fetch_expected(self.ports, log=self.log)
        portcalls.save_cache(data, self.cache_path)
        total = sum(len(entry.get("vessels") or [])
                    for entry in (data.get("ports") or {}).values())
        failed = [entry for entry in (data.get("ports") or {}).values()
                  if entry.get("error")]
        if failed and len(failed) == len(data.get("ports") or {}):
            raise RuntimeError(failed[0].get("error") or "страницы портов недоступны")
        detail = f"портов {len(data.get('ports') or {})}"
        if failed:
            detail += f", с ошибкой {len(failed)}"
        if data.get("stale"):
            detail += ", сохранён прошлый список"
        self.ok(total, detail)


class GfwFeed(ServiceFeed):
    """Global Fishing Watch: профиль судна и разрывы AIS (нужен токен).

    API v3 по бесплатному токену: поиск судна по MMSI в реестровом датасете
    и события GAP (пропажа AIS) за последние дни. Данные кладутся в
    data/gfw.json и показываются в карточке судна.
    """

    key = "gfw"
    interval = 43200.0

    def __init__(self, *args, vessels=None, max_vessels=None, days=None,
                 cache_path=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.vessels = vessels or (lambda: [])
        self.max_vessels = max(1, int(max_vessels or 10))
        self.days = max(1, int(days or 7))
        self.cache_path = Path(cache_path) if cache_path else portcalls.GFW_PATH
        if kwargs.get("minutes"):
            self.interval = max(3600.0, float(kwargs["minutes"]) * 60.0)

    def headers(self):
        # поле ввода может содержать вставку с мусором — собираем JWT заново
        token = portcalls.clean_gfw_token(self.api_key)
        return {"Authorization": f"Bearer {token}",
                "Accept": "application/json"}

    def vessel_id(self, mmsi):
        data = self.request(
            portcalls.GFW_SEARCH_URL,
            params={"query": mmsi, "datasets[0]": portcalls.GFW_IDENTITY_DATASET,
                    "limit": 1},
            headers=self.headers(), timeout=45)
        entries = (data or {}).get("entries") or []
        if not entries:
            return None
        entry = entries[0] if isinstance(entries[0], dict) else {}
        info = entry.get("selfReportedInfo") or []
        info = info[0] if info and isinstance(info[0], dict) else {}
        vessel_id = info.get("id") or entry.get("id")
        if not vessel_id:
            return None
        # в ответе API поле называется shipname (раньше код читал shipName
        # и имя всегда оставалось пустым)
        return {"id": str(vessel_id),
                "name": info.get("shipname") or info.get("shipName") or "",
                "flag": info.get("flag") or "", "imo": info.get("imo") or ""}

    def last_gap(self, vessel_id):
        from datetime import timedelta

        today = datetime.now().date()
        start = (today - timedelta(days=self.days)).isoformat()
        data = self.request(
            portcalls.GFW_EVENTS_URL,
            # offset обязателен, если передан limit: иначе API v3 отвечает
            # 422 «If you send the limit property then the offset property is required»
            params={"datasets[0]": portcalls.GFW_GAPS_DATASET,
                    "vessels[0]": vessel_id, "start-date": start,
                    "end-date": today.isoformat(), "limit": 5, "offset": 0},
            headers=self.headers(), timeout=45)
        entries = (data or {}).get("entries") or []
        gaps = [entry for entry in entries if isinstance(entry, dict)]
        if not gaps:
            return None
        gaps.sort(key=lambda entry: str(entry.get("end") or ""), reverse=True)
        gap = gaps[0]
        position = gap.get("position") or {}
        hours = None
        try:
            begin, end = _time(gap.get("start")), _time(gap.get("end"))
            if begin and end:
                hours = round((end - begin).total_seconds() / 3600, 1)
        except (TypeError, ValueError):
            hours = None
        where = ""
        if position.get("lat") is not None and position.get("lon") is not None:
            where = f"{float(position['lat']):.2f}, {float(position['lon']):.2f}"
        return {"start": str(gap.get("start") or ""),
                "end": str(gap.get("end") or "")[:16].replace("T", " "),
                "hours": hours, "position_text": where}

    def poll_once(self):
        cache = portcalls.load_gfw(self.cache_path)
        known = cache.get("vessels") if isinstance(cache.get("vessels"), dict) else {}
        found = gaps = 0
        errors = 0
        for mmsi in list(self.vessels())[:self.max_vessels]:
            if self.stop_event.is_set():
                break
            mmsi = str(mmsi or "")
            if not mmsi:
                continue
            try:
                profile = self.vessel_id(mmsi)
                if not profile:
                    continue
                record = dict(known.get(mmsi) or {})
                record.update(profile)
                record["gap"] = self.last_gap(profile["id"])
                record["checked"] = datetime.now().isoformat(timespec="seconds")
                known[mmsi] = record
                found += 1
                gaps += 1 if record.get("gap") else 0
            except Exception as error:
                errors += 1
                if errors == 1:
                    self.log(f"{self.title}: {error}")
            self.stop_event.wait(1.0)
        cache["vessels"] = known
        cache["updated"] = datetime.now().isoformat(timespec="seconds")
        portcalls.save_gfw(cache, self.cache_path)
        if found == 0 and errors:
            raise RuntimeError("профили не получены (проверьте токен GFW)")
        self.ok(found, f"с разрывами AIS {gaps}")


class MarineTrafficFeed(ServiceFeed):
    """API MarineTraffic: ключ в пути, район задаётся в кабинете."""

    key = "mt"
    interval = 600.0

    def poll_once(self):
        if not self.api_key:
            raise RuntimeError("не задан ключ API")
        data = self.request(
            f"{MARINETRAFFIC_URL}/{self.api_key}",
            params={"v": 2, "protocol": "jsono", "timespan": 15, "limit": 2000},
            timeout=60)
        if isinstance(data, dict) and data.get("errors"):
            errors = data.get("errors")
            text = errors if isinstance(errors, str) else "; ".join(map(str, errors))
            raise RuntimeError(text)
        rows = data.get("DATA") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise RuntimeError("неожиданный ответ сервера")

        count = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            mmsi = _mmsi(row.get("MMSI"))
            if not mmsi:
                continue
            count += 1
            speed = _number(row.get("SPEED"))
            if speed is not None and speed > 60:
                speed = speed / 10.0  # MarineTraffic отдаёт скорость в десятых узла
            self.emit(_position(mmsi, row.get("LAT"), row.get("LON"),
                                sog=speed, cog=row.get("COURSE"),
                                heading=_heading(row.get("HEADING")),
                                name=row.get("SHIPNAME"),
                                source="marinetraffic"))
            self.emit(_static(mmsi, name=row.get("SHIPNAME"),
                              imo=row.get("IMO"), callsign=row.get("CALLSIGN"),
                              type_code=_type_code(row.get("SHIPTYPE")),
                              destination=row.get("DESTINATION"),
                              draft=row.get("DRAUGHT"),
                              length_m=row.get("LENGTH"), beam_m=row.get("WIDTH"),
                              source="marinetraffic"))
        self.ok(count)


class DatalasticFeed(ServiceFeed):
    """Datalastic: живая позиция и рейс по MMSI, ключ в заголовке x-api-key.

    /api/v0/vessel стоит 1 кредит за судно и отдаёт имя, тип, координаты, ход,
    пункт назначения и осадку — поэтому канал обогащает уже известные суда,
    а не сканирует район. Ключ берётся на datalastic.com после регистрации
    (есть пробный период).
    """

    key = "dat"
    interval = 600.0

    def __init__(self, *args, vessels=None, max_vessels=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.vessels = vessels or (lambda: [])
        self.max_vessels = max(1, int(max_vessels or 10))
        if kwargs.get("minutes"):
            self.interval = max(120.0, float(kwargs["minutes"]) * 60.0)

    def headers(self):
        return {"x-api-key": self.api_key, "Accept": "application/json"}

    def poll_once(self):
        if not self.api_key:
            raise RuntimeError("не задан ключ API")
        found = 0
        errors = 0
        last_error = None
        for mmsi in list(self.vessels())[:self.max_vessels]:
            if self.stop_event.is_set():
                break
            mmsi = str(mmsi or "").strip()
            if not mmsi:
                continue
            try:
                data = self.request(DATALASTIC_URL, params={"mmsi": mmsi},
                                    headers=self.headers(), timeout=30)
            except Exception as error:
                status = getattr(getattr(error, "response", None), "status_code", 0)
                if status in (401, 403):
                    raise RuntimeError("ключ Datalastic не принят")
                errors += 1
                last_error = error
                continue
            meta = (data or {}).get("meta") or {}
            if isinstance(meta, dict) and meta.get("success") is False:
                errors += 1
                last_error = RuntimeError(
                    str((data or {}).get("message") or "отказ API")[:80])
                continue
            row = (data or {}).get("data")
            if isinstance(row, list):
                row = row[0] if row else None
            if not isinstance(row, dict):
                continue
            vessel_mmsi = _mmsi(row.get("mmsi") or mmsi)
            if not vessel_mmsi:
                continue
            name = row.get("name") or row.get("name_ais") or ""
            found += 1
            self.emit(_position(vessel_mmsi, row.get("lat"), row.get("lon"),
                                sog=row.get("speed"), cog=row.get("course"),
                                heading=_heading(row.get("heading")),
                                name=name, source="datalastic",
                                at=_time(row.get("last_position_utc")
                                         or row.get("last_position_UTC"))))
            self.emit(_static(vessel_mmsi, name=name, imo=row.get("imo"),
                              type_code=_type_code(row.get("type_code")),
                              destination=row.get("destination"),
                              draft=row.get("draught"),
                              length_m=row.get("length"), beam_m=row.get("beam"),
                              source="datalastic"))
            self.stop_event.wait(0.3)
        if found == 0 and errors:
            raise last_error or RuntimeError("данные не получены")
        self.ok(found, f"запросов до {self.max_vessels}")


class AishubFeed(ServiceFeed):
    """AISHub: агрегированные позиции сети приёмников по логину участника.

    Ключа API нет: доступ дают за собственный поток NMEA на AISHub, логин
    приходит после одобрения. Один запрос в минуту — не чаще, поэтому канал
    опрашивает район подписки не чаще раза в 10 минут.
    """

    key = "aishub"
    interval = 600.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if kwargs.get("minutes"):
            self.interval = max(120.0, float(kwargs["minutes"]) * 60.0)

    def poll_once(self):
        if not self.api_key:
            raise RuntimeError("не задан логин AISHub")
        boxes = self.boxes_lat_lon()
        if not boxes:
            raise RuntimeError("не задан район подписки")
        found = 0
        for lat1, lon1, lat2, lon2 in boxes[:4]:
            if self.stop_event.is_set():
                break
            data = self.request(
                AISHUB_URL,
                params={"username": self.api_key, "format": 1, "output": "json",
                        "compress": 0, "interval": 60,
                        "latmin": f"{min(lat1, lat2):.4f}",
                        "latmax": f"{max(lat1, lat2):.4f}",
                        "lonmin": f"{min(lon1, lon2):.4f}",
                        "lonmax": f"{max(lon1, lon2):.4f}"},
                timeout=45)
            meta, rows = {}, []
            if isinstance(data, list) and data:
                meta = data[0] if isinstance(data[0], dict) else {}
                rows = data[1] if len(data) > 1 and isinstance(data[1], list) else []
            if isinstance(meta, dict) and meta.get("ERROR"):
                raise RuntimeError(str(meta.get("ERROR_MESSAGE")
                                     or "логин AISHub не принят")[:80])
            for row in rows:
                if not isinstance(row, dict):
                    continue
                mmsi = _mmsi(row.get("MMSI"))
                if not mmsi:
                    continue
                found += 1
                self.emit(_position(mmsi, row.get("LATITUDE"), row.get("LONGITUDE"),
                                    sog=row.get("SOG"), cog=row.get("COG"),
                                    heading=_heading(row.get("HEADING")),
                                    name=row.get("NAME"), source="aishub"))
                bow, stern = _number(row.get("A")), _number(row.get("B"))
                port, starboard = _number(row.get("C")), _number(row.get("D"))
                length = bow + stern if bow is not None and stern is not None else None
                beam = (port + starboard if port is not None and starboard is not None
                        else None)
                self.emit(_static(mmsi, name=row.get("NAME"), imo=row.get("IMO"),
                                  callsign=row.get("CALLSIGN"),
                                  type_code=_type_code(row.get("TYPE")),
                                  destination=row.get("DEST"),
                                  draft=row.get("DRAUGHT"), length_m=length,
                                  beam_m=beam, source="aishub"))
            self.stop_event.wait(1.0)
        self.ok(found, f"районов {min(len(boxes), 4)}")


class GoradarFeed(ServiceFeed):
    """Goradar: те же суда, что на карте goradar.ru (без ключа).

    Сайт отдаёт позиции кусками по прямоугольникам (не больше 90 судов
    на запрос), поэтому район режется на клетки 2°×2°. В ответе есть MMSI,
    широта/долгота, курс и тип, но нет имени и скорости — они подтянутся
    из других каналов, когда судно появится в живом потоке.
    """

    key = "gor"
    interval = 900.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session = None
        if kwargs.get("minutes"):
            self.interval = max(120.0, float(kwargs["minutes"]) * 60.0)

    def http(self):
        import requests
        if self.session is None:
            session = requests.Session()
            session.headers.update(dict(MAP_HEADERS, Referer=GORADAR_HOME))
            try:
                session.get(GORADAR_HOME, timeout=30)
            except Exception as error:
                self.log(f"{self.title}: главная страница не открылась ({error})")
            self.session = session
        return self.session

    def poll_once(self):
        session = self.http()
        cells = _grid_boxes(self.bboxes)
        if not cells:
            raise RuntimeError("не задан район наблюдения")
        seen = {}
        errors = 0
        for cell in cells:
            if self.stop_event.is_set():
                break
            lat1, lon1, lat2, lon2 = cell
            try:
                response = session.post(GORADAR_URL,
                                        data={"lat1": lat1, "lat2": lat2,
                                              "lng1": lon1, "lng2": lon2},
                                        timeout=45)
                response.raise_for_status()
                rows = response.json()
            except Exception:
                errors += 1
                continue
            for row in rows if isinstance(rows, list) else []:
                try:
                    mmsi = _mmsi(row[0])
                    lat, lon = _number(row[2]), _number(row[3])
                except (IndexError, TypeError):
                    continue
                if not mmsi or lat is None or lon is None:
                    continue
                course = _number(row[4]) if len(row) > 4 else None
                if course is not None and (course <= 0 or course >= 360):
                    course = None
                heading = _heading(row[6]) if len(row) > 6 else None
                speed = _number(row[7]) if len(row) > 7 else None
                label = str(row[8] or "").strip() if len(row) > 8 else ""
                when = None
                if len(row) > 5 and row[5]:
                    try:
                        when = datetime.strptime(str(row[5]).strip(), "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        when = None
                seen[mmsi] = _position(mmsi, lat, lon, sog=speed, cog=course,
                                       heading=heading, source="goradar",
                                       at=when, type_label=label)
            self.stop_event.wait(0.4)
        for event in seen.values():
            self.emit(event)
        detail = f"клеток {len(cells)}" + (f", ошибок {errors}" if errors else "")
        self.ok(len(seen), detail)


class MarineTrafficLiveFeed(ServiceFeed):
    """MarineTraffic.live (Ship Radar 24): публичная карта судов, ключ не нужен.

    JSON-мост сайта отдаёт имя, MMSI, координаты, скорость, курс, тип и время
    последнего сигнала. Район задаётся точкой и радиусом (до 500 судов
    на ответ), поэтому опрашиваются центры у наших портов, а не вся карта:
    так один цикл — это несколько запросов, а не сотни.
    """

    key = "mlive"
    interval = 900.0
    radius_km = 120
    limit = 500

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session = None
        self.ports = [port for port in (kwargs.get("ports") or []) if isinstance(port, dict)]
        if kwargs.get("minutes"):
            self.interval = max(120.0, float(kwargs["minutes"]) * 60.0)

    def http(self):
        import requests
        if self.session is None:
            session = requests.Session()
            session.headers.update(dict(MAP_HEADERS, Referer=MARINETRAFFIC_LIVE_HOME))
            try:
                session.get(MARINETRAFFIC_LIVE_HOME, timeout=30)
            except Exception as error:
                self.log(f"{self.title}: главная страница не открылась ({error})")
            self.session = session
        return self.session

    def centers(self):
        """Точки опроса: наши порты, а если их нет — центры клеток района."""
        centers = []
        for port in self.ports:
            try:
                centers.append((float(port["lat"]), float(port["lon"])))
            except (KeyError, TypeError, ValueError):
                continue
        if not centers:
            for lat1, lon1, lat2, lon2 in self.boxes_lat_lon():
                lat = lat1
                while lat < lat2 - 0.01:
                    lon = lon1
                    while lon < lon2 - 0.01:
                        centers.append((round(lat + 1.5, 3), round(lon + 1.5, 3)))
                        lon += 3.0
                    lat += 3.0
        unique = []
        for center in centers:
            if all(abs(center[0] - other[0]) > 0.05 or abs(center[1] - other[1]) > 0.05
                   for other in unique):
                unique.append(center)
        return unique

    def poll_once(self):
        session = self.http()
        centers = self.centers()
        if not centers:
            raise RuntimeError("не задан район наблюдения")
        radius = self.radius_km if self.ports else max(self.radius_km, 220)
        seen = {}
        errors = 0
        for lat, lon in centers:
            if self.stop_event.is_set():
                break
            params = {"lat": lat, "lon": lon, "radius": radius, "limit": self.limit}
            try:
                response = session.get(MARINETRAFFIC_LIVE_URL, params=params, timeout=45)
                response.raise_for_status()
                data = ((response.json() or {}).get("data") or {})
            except Exception:
                errors += 1
                continue
            for row in data.get("vessels") or []:
                mmsi = _mmsi(row.get("mmsi"))
                if not mmsi:
                    continue
                name = str(row.get("name") or "").strip()
                # у части судов вместо имени сам MMSI («MMSI 264900454»)
                if not name or mmsi in name.replace(" ", "") or name.upper().startswith("MMSI"):
                    name = ""
                event = _position(mmsi, row.get("lat"), row.get("lng"),
                                  sog=row.get("speed_kn"), cog=row.get("course_deg"),
                                  name=name,
                                  type_label=str(row.get("type") or "").strip() or None,
                                  source="mlive", at=_time(row.get("updated")))
                if event:
                    seen[mmsi] = event
            self.stop_event.wait(0.4)
        for event in seen.values():
            self.emit(event)
        detail = f"точек {len(centers)}" + (f", ошибок {errors}" if errors else "")
        self.ok(len(seen), detail)


# ------------------------------------------------- карточки судов Ship Radar 24

CARD_KEYS = {
    "name": ("name",),
    "imo": ("imo", "imo-nummer"),
    "callsign": ("call sign", "rufzeichen", "позывной"),
    "type": ("schiffstyp", "vessel type", "тип судна"),
    "length": ("länge", "length", "длина"),
    "beam": ("breite", "beam", "ширина"),
    "draft": ("tiefgang", "draught", "осадка"),
    "destination": ("zielhafen", "ziel", "destination", "назначение"),
}


def _card_text(value) -> str:
    """Текст из HTML-ячейки карточки: без тегов, пробелов и «—»."""
    import html as html_module
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = html_module.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return "" if text in ("", "—", "-", "n/a", "N/A") else text


def parse_vessel_card(html_text) -> dict:
    """Паспорт судна из страницы MarineTraffic.live: тип, осадка, размеры."""
    import html as html_module
    pairs = re.findall(r'<span class="k">(.*?)</span>\s*<span class="v">(.*?)</span>',
                       str(html_text or ""), re.S)
    values = {}
    for key, value in pairs:
        name = html_module.unescape(re.sub(r"<[^>]+>", " ", key)).strip().lower()
        text = _card_text(value)
        if not name or not text:
            continue
        for field, keys in CARD_KEYS.items():
            if field in values:
                continue
            if name in keys:
                values[field] = text
                break
    result = {}
    if values.get("name"):
        # в названиях на карте встречаются кавычки и служебные пометки
        result["name"] = values["name"].strip("'\"«» ")[:60]
    if values.get("imo"):
        imo = "".join(ch for ch in values["imo"] if ch.isdigit())
        if len(imo) == 7:
            result["imo"] = imo
    if values.get("callsign"):
        result["callsign"] = values["callsign"][:16]
    if values.get("type"):
        result["type_text"] = values["type"][:60]
    if values.get("length"):
        result["length_m"] = _number(values["length"].replace("m", ""))
    if values.get("beam"):
        result["beam_m"] = _number(values["beam"].replace("m", ""))
    if values.get("draft"):
        result["draft"] = _number(values["draft"].replace("m", ""))
    if values.get("destination"):
        # «IZMAIL (UAIZM)» → IZMAIL
        result["destination"] = re.split(r"[(,]", values["destination"])[0].strip()[:24]
    # Проверка на явную ошибку в источнике: осадка больше четверти длины корпуса
    # или ширина больше его длины быть не может (у целого типа судов так бывает
    # только при опечатке вроде «18 м длина, 13 м осадка»).
    length = result.get("length_m")
    draft = result.get("draft")
    # Осадка больше 8 % длины корпуса — это уже ошибка AIS (встречается
    # «27 м» у 110-метрового толкача) или опечатка сайта, а не данные.
    if length and draft and (draft > max(1.5, length * 0.08) or draft < 0.3):
        result.pop("draft", None)
    beam = result.get("beam_m")
    if length and beam and beam > max(1.0, length * 0.6):
        result.pop("beam_m", None)
    if beam and (beam < 2.0):
        result.pop("beam_m", None)
    return result


class ShipRadarDetailsFeed(ServiceFeed):
    """Наполняет базу паспортными данными судов с карточек Ship Radar 24.

    Позиции приходят JSON-потоком, а тип, позывной, размеры, осадка и порт
    назначения есть только в HTML-карточке судна. Читаем только те суда,
    у которых в базе не хватает этих полей, по одному запросу на судно,
    и запоминаем попытки, чтобы не ходить на сайт каждые полчаса.
    """

    key = "srd"
    interval = 900.0
    max_per_cycle = 120
    retry_hours = 72.0
    pause = 0.6

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session = None
        self.vessel_source = kwargs.get("vessels") or (lambda: [])
        self.state_path = SHIPRADAR_STATE
        self.attempts = self._load_attempts()
        if kwargs.get("minutes"):
            self.interval = max(120.0, float(kwargs["minutes"]) * 60.0)

    # ------------------------------------------------------------ состояние
    def _load_attempts(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _tried_recently(self, mmsi, now) -> bool:
        """Недавняя попытка: удачная — на весь срок, ошибка — на 6 часов."""
        seen = self.attempts.get(mmsi)
        if not seen:
            return False
        if isinstance(seen, dict):
            at = float(seen.get("at") or 0)
            limit = self.retry_hours * 3600 if seen.get("ok") else 6 * 3600
        else:
            at, limit = float(seen), 6 * 3600
        return 0 < now - at < limit

    def _save_attempts(self):
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.attempts, ensure_ascii=False),
                                       encoding="utf-8")
        except OSError as error:
            self.log(f"{self.title}: состояние не сохранено ({error})")

    def http(self):
        import requests
        if self.session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": MAP_HEADERS["User-Agent"],
                                    "Accept-Language": "ru,en;q=0.8",
                                    "Referer": MARINETRAFFIC_LIVE_HOME})
            self.session = session
        return self.session

    def candidates(self) -> list:
        try:
            items = self.vessel_source() or []
        except Exception:
            return []
        now = time.time()
        result = []
        for item in items:
            if isinstance(item, dict):
                mmsi = _mmsi(item.get("mmsi"))
                name = str(item.get("name") or "")
            else:
                mmsi = _mmsi(item)
                name = ""
            if not mmsi:
                continue
            # без имени не собрать адрес карточки: сайт отвечает 403 на «x»
            if not str(name).strip() or self._tried_recently(mmsi, now):
                continue
            result.append((mmsi, name))
            if len(result) >= self.max_per_cycle:
                break
        return result

    def url_for(self, mmsi, name="") -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
        return SHIPRADAR_CARD_URL.format(slug=slug or "x", mmsi=mmsi)

    def poll_once(self):
        session = self.http()
        targets = self.candidates()
        if not targets:
            self.ok(0, "все карточки уже прочитаны")
            return
        filled = 0
        errors = 0
        for mmsi, name in targets:
            if self.stop_event.is_set():
                break
            try:
                response = session.get(self.url_for(mmsi, name), timeout=30)
                response.raise_for_status()
                card = parse_vessel_card(response.text)
            except Exception as error:
                errors += 1
                if errors == 1:
                    self.log(f"{self.title}: карточка не открылась "
                             f"({error.__class__.__name__}: {self.url_for(mmsi, name)})")
                self.attempts[mmsi] = {"at": time.time(), "ok": False}
                self.stop_event.wait(self.pause)
                continue
            self.attempts[mmsi] = {"at": time.time(), "ok": True}
            if card and any(card.get(key) for key in
                            ("name", "type_text", "draft", "length_m", "beam_m",
                             "destination")):
                code = None
                try:
                    import ais_feed
                    code = ais_feed.type_code_for_label(card.get("type_text"))
                except Exception:
                    code = None
                self.emit({"kind": "static", "mmsi": mmsi,
                           "name": card.get("name") or "",
                           "imo": card.get("imo") or "",
                           "callsign": card.get("callsign") or "",
                           "type_code": code,
                           "type_label": card.get("type_text") or "",
                           "destination": card.get("destination") or "",
                           "draft": card.get("draft"),
                           "length_m": card.get("length_m"),
                           "beam_m": card.get("beam_m"),
                           "eta_declared": None,
                           "at": datetime.now(),
                           "source": "srd"})
                filled += 1
            self.stop_event.wait(self.pause)
        self._save_attempts()
        detail = f"запросов {len(targets)}" + (f", ошибок {errors}" if errors else "")
        self.ok(filled, detail)


# Ключ настройки с ключом API: vf_key / mt_key / mst_key.
PROVIDERS = {
    "vf": VesselFinderFeed,
    "vfa": VesselFinderArrivals,
    "mt": MarineTrafficFeed,
    "mst": MyShipTrackingFeed,
    "dt": DigitrafficFeed,
    "aci": AiscatcherExpected,
    "gfw": GfwFeed,
    "dat": DatalasticFeed,
    "aishub": AishubFeed,
    "gor": GoradarFeed,
    "mlive": MarineTrafficLiveFeed,
    "srd": ShipRadarDetailsFeed,
}


def needs_key(key):
    return key in ("vf", "vfa", "mt", "mst", "gfw", "dat", "aishub")


def build(key, on_event, log, stop_event, api_key="", bboxes=None, on_status=None,
          vessels=None, minutes=None, daily_coins=None, max_vessels=None,
          state_path=None, ports=None, lookahead_minutes=None, max_records=None,
          cache_path=None, days=None):
    """Создаёт поток опроса сервиса. None — если сервис не умеет отдавать данные."""
    provider = PROVIDERS.get(key)
    if provider is None:
        return None
    return provider(on_event, log, stop_event, api_key=api_key, bboxes=bboxes,
                    on_status=on_status, name=f"service-{key}", vessels=vessels,
                    minutes=minutes, daily_coins=daily_coins,
                    max_vessels=max_vessels, state_path=state_path,
                    ports=ports, lookahead_minutes=lookahead_minutes,
                    max_records=max_records, cache_path=cache_path, days=days)
