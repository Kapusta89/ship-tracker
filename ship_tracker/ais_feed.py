"""Поток данных AIS: живой веб-сокет AISStream, разбор сообщений и тестовый фид.

Живой режим: wss://stream.aisstream.io/v0/stream
  * ключ берётся в личном кабинете aisstream.io (регистрация через GitHub);
  * подписка отправляется одним JSON в течение 3 секунд после подключения;
  * сервер присылает бинарные кадры UTF-8 JSON;
  * тип судна приходит в ShipStaticData (Type: 80–89 — танкеры).

Тестовый режим повторяет тот же формат сообщений, поэтому вся цепочка
(разбор → геозоны → фильтр → алерты) проверяется без ключа и без интернета.
"""

import json
import math
import random
import threading
import time
from datetime import datetime

import geo

STREAM_URL = "wss://stream.aisstream.io/v0/stream"
# Снимок всех известных судов района через REST (ключ не нужен).
# В отличие от живого потока отдаёт и те суда, которые давно не передавали,
# поэтому карта наполняется сразу после запуска и не пустует при обрывах.
SNAPSHOT_URL = "https://ais.openwaters.io/v1/vessels"
OPENWATERS_URL = "wss://ais.openwaters.io/v0/stream"
# Второго адреса (stream.openwaters.io) не существует: DNS отвечает NXDOMAIN,
# поэтому отдельный поток на него не поднимаем — он давал только ошибки в логе.

POSITION_TYPES = ("PositionReport", "ExtendedClassBPositionReport", "StandardClassBPositionReport")
STATIC_TYPES = ("ShipStaticData", "StaticDataReport")
TANKER_CODES = range(80, 90)

# Полная таблица типов судов AIS (ITU-R M.1371, поле Ship type 0–99).
TYPE_DETAIL = {
    0: "тип не указан",
    20: "судно на воздушной подушке",
    30: "рыболовное",
    31: "буксир (тяга)",
    32: "буксир (тяга, большой)",
    33: "земснаряд",
    34: "водолазные работы",
    35: "военное",
    36: "парусное",
    37: "прогулочное",
    40: "высокоскоростное",
    50: "лоцманское",
    51: "спасательное",
    52: "буксир",
    53: "портовая служба",
    54: "противопожарное / против загрязнения",
    55: "полицейское",
    58: "медицинское",
    59: "невоенное (Res. 18)",
    60: "пассажирское",
    70: "грузовое",
    80: "танкер",
    81: "танкер (опасные грузы A)",
    82: "танкер (опасные грузы B)",
    83: "танкер (опасные грузы C)",
    84: "танкер (опасные грузы D)",
    89: "танкер (прочее)",
    90: "прочее",
}

# Короткие подписи для таблиц (полный текст — в карточке судна).
TYPE_LABELS = {
    0: "не указан", 20: "СВП", 30: "рыболовное", 31: "буксир", 32: "буксир",
    33: "земснаряд", 34: "водолазное", 35: "военное", 36: "парусное", 37: "прогулочное",
    40: "скоростное", 50: "лоцман", 51: "спасательное", 52: "буксир",
    53: "портовая служба", 54: "противозагрязнение", 55: "полицейское",
    58: "медицинское", 59: "невоенное", 60: "пассажирское", 70: "грузовое",
    80: "танкер", 90: "прочее",
}


def type_detail(code) -> str:
    """Подробный тип судна по коду AIS (для карточки судна)."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "тип не указан"
    if code in TYPE_DETAIL:
        return TYPE_DETAIL[code]
    if 20 <= code <= 29:
        return "судно на воздушной подушке"
    if 40 <= code <= 49:
        return "высокоскоростное"
    if 60 <= code <= 69:
        return "пассажирское"
    if 70 <= code <= 79:
        return "грузовое"
    if 80 <= code <= 89:
        return "танкер"
    if 90 <= code <= 99:
        return "прочее"
    return "тип не указан"


def type_label(code) -> str:
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "не указан"
    if code in TYPE_LABELS:
        return TYPE_LABELS[code]
    for low, label in ((80, "танкер"), (70, "грузовое"), (60, "пассажирское"),
                       (40, "скоростное"), (30, "служебное"), (20, "СВП")):
        if low <= code < low + 10:
            return label
    return "прочее"


def is_tanker_code(code) -> bool:
    try:
        return int(code) in TANKER_CODES
    except (TypeError, ValueError):
        return False


def clean_text(value) -> str:
    """AIS дополняет строки символами @ и пробелами — убираем их."""
    if value is None:
        return ""
    return str(value).replace("@", " ").strip()


def to_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_dimensions(value):
    """Габариты корпуса из Dimension (A — до носа, B — до кормы, C/D — полуширина).

    Возвращает (длина, ширина) в метрах или (None, None), если данные
    не переданы или выглядят недостоверно.
    """
    if not isinstance(value, dict):
        return None, None
    try:
        to_bow = float(value.get("A") or 0)
        to_stern = float(value.get("B") or 0)
        to_port = float(value.get("C") or 0)
        to_starboard = float(value.get("D") or 0)
    except (TypeError, ValueError):
        return None, None
    length = to_bow + to_stern
    beam = to_port + to_starboard
    if not 5.0 <= length <= 500.0:
        length = None
    if not 2.0 <= beam <= 90.0:
        beam = None
    return length, beam


def parse_eta(value):
    """Срок прибытия из ShipStaticData: поля Month/Day/Hour/Minute без года.

    По спецификации AIS время указано в UTC. Год не передаётся, поэтому берётся
    текущий; если дата ушла больше чем на полгода в прошлое — значит, речь о
    следующем годе.
    """
    if not isinstance(value, dict):
        return None
    try:
        month = int(value.get("Month") or 0)
        day = int(value.get("Day") or 0)
        hour = int(value.get("Hour") or 0)
        minute = int(value.get("Minute") or 0)
    except (TypeError, ValueError):
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    now = datetime.now()
    try:
        moment = datetime(now.year, month, day, hour, minute)
    except ValueError:
        return None
    if (now - moment).days > 180:
        try:
            moment = moment.replace(year=now.year + 1)
        except ValueError:
            return None
    return moment.isoformat(timespec="minutes")


def first_set(*values):
    """Первое значение, отличное от None: ноль — валидные данные, а не «пусто»."""
    for value in values:
        if value is not None:
            return value
    return None


def parse_message(raw):
    """Приводит сообщение AISStream к простому словарю события."""
    if not isinstance(raw, dict):
        return None
    message_type = raw.get("MessageType") or raw.get("messageType") or raw.get("type")
    message = raw.get("Message") or raw.get("message") or {}
    meta = raw.get("MetaData") or raw.get("metaData") or raw.get("Meta") or {}

    if message_type == "SubscriptionConfirmation" or raw.get("event") == "subscribed":
        enabled = message.get("CompressionEnabled") if isinstance(message, dict) else message
        return {"kind": "confirmation", "compression": bool(enabled)}

    body = None
    if isinstance(message, dict):
        body = message.get(message_type) or message.get(str(message_type)) if message_type else None
    if not isinstance(body, dict) and isinstance(message, dict):
        body = message
    if not isinstance(body, dict):
        body = raw.get("data") if isinstance(raw.get("data"), dict) else {}

    mmsi = meta.get("MMSI") or meta.get("mmsi") or body.get("UserID") or body.get("userId") or raw.get("mmsi")
    if not mmsi:
        return None
    mmsi = str(mmsi).strip().zfill(9)

    if message_type in POSITION_TYPES or raw.get("class") == "position" or (isinstance(body, dict) and ("Latitude" in body or "latitude" in body)):
        lat = to_float(body.get("Latitude") or body.get("latitude") or meta.get("Latitude") or meta.get("latitude"))
        lon = to_float(body.get("Longitude") or body.get("longitude") or meta.get("Longitude") or meta.get("longitude"))
        if lat is None or lon is None:
            return None
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180) or (abs(lat) < 0.001 and abs(lon) < 0.001):
            return None
        heading = first_set(body.get("TrueHeading"), body.get("trueHeading"),
                            body.get("Heading"))
        try:
            heading = int(heading)
        except (TypeError, ValueError):
            heading = None
        if heading is not None and not 0 <= heading <= 359:
            heading = None
        return {
            "kind": "position",
            "mmsi": mmsi,
            "lat": lat,
            "lon": lon,
            "sog": to_float(first_set(body.get("Sog"), body.get("sog"),
                                     body.get("SpeedOverGround"))),
            "cog": to_float(first_set(body.get("Cog"), body.get("cog"))),
            "heading": heading,
            "name": clean_text(meta.get("ShipName") or meta.get("shipName") or body.get("ShipName")),
            "at": datetime.now(),
        }

    if message_type in STATIC_TYPES or raw.get("class") == "static" or (isinstance(body, dict) and ("Destination" in body or "destination" in body or "Name" in body)):
        code = first_set(body.get("Type"), body.get("ShipType"), body.get("type"),
                         body.get("shipType"))
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = None
        length, beam = parse_dimensions(body.get("Dimension") or body.get("dimension"))
        return {
            "kind": "static",
            "mmsi": mmsi,
            "name": clean_text(body.get("Name") or body.get("ShipName") or meta.get("ShipName") or meta.get("shipName")),
            "imo": clean_text(body.get("ImoNumber") or body.get("IMO") or body.get("imo")),
            "callsign": clean_text(body.get("CallSign") or body.get("callsign")),
            "type_code": code,
            "destination": clean_text(body.get("Destination") or body.get("destination")),
            "draft": to_float(body.get("MaximumStaticDraught") or body.get("Draft") or body.get("draft")),
            "length_m": length,
            "beam_m": beam,
            "eta_declared": parse_eta(body.get("Eta") or body.get("eta")),
            "at": datetime.now(),
        }

    return None


def http_status(error):
    """HTTP-код отказа рукопожатия (AISStream отвечает 401/403/429)."""
    response = getattr(error, "response", None)
    for candidate in (getattr(response, "status_code", None),
                      getattr(response, "status", None)):
        if isinstance(candidate, int):
            return candidate
    return None


def describe_error(error) -> tuple:
    """Возвращает (текст для пользователя, код HTTP или None).

    Сервер AISStream отклоняет само рукопожатие:
      401 — ключ неверный или отключён;
      429 — превышен лимит подключений (3 на аккаунт и 3 на IP-адрес),
            ключ при этом может быть совершенно правильный.
    """
    status = http_status(error)
    if status == 401:
        return ("ключ не принят (HTTP 401): проверьте, что скопирован весь ключ "
                "и он не отключён на aisstream.io", status)
    if status == 403:
        return "доступ запрещён (HTTP 403)", status
    if status == 429:
        return ("слишком много подключений (HTTP 429). Лимит AISStream: 3 подключения "
                "на аккаунт и 3 на IP. Закройте другие копии программы, подождите "
                "1–2 минуты — и поток поднимется сам"), status
    if status:
        return f"сервер AISStream ответил HTTP {status}", status
    text = str(error) or error.__class__.__name__
    return text, None


def open_stream(url):
    """Подключение к AISStream с запасным вариантом для старых websockets.

    В websockets 14+ интервалы пинга — параметры connect(), а в 13 и старше их
    там нет (они уходят в socket.create_connection и падают с TypeError).
    В старых версиях пинг включён по умолчанию, поэтому просто не передаём их.
    """
    from websockets.sync.client import connect

    options = dict(compression="deflate", open_timeout=20, max_size=8 * 1024 * 1024)
    try:
        return connect(url, ping_interval=20, ping_timeout=20, **options)
    except TypeError:
        return connect(url, **options)


class AISStreamFeed(threading.Thread):
    """Живой фид AISStream с переподключением и экспоненциальной паузой."""

    feed_title = "AISStream (основной поток)"

    def __init__(self, api_key, bboxes, message_types, on_event, log, stop_event,
                 stream_url=None, name="ais-feed"):
        super().__init__(daemon=True, name=name)
        self.api_key = (api_key or "").strip()
        self.bboxes = bboxes
        self.message_types = list(message_types)
        self.on_event = on_event
        self.log = log or (lambda message: None)
        self.stop_event = stop_event
        self.stream_url = stream_url or STREAM_URL
        self.feed_title = ("Open Waters AIS (доп. поток)"
                           if "openwaters" in self.stream_url.lower()
                           else "AISStream (основной поток)")
        self.connected = False
        self.events = 0
        self.dropped = 0
        self.last_error = ""
        self.connected_since = None

    def subscription(self) -> dict:
        url = getattr(self, "stream_url", "") or ""
        if "openwaters" in url.lower():
            # Попробуем несколько вариантов совместимости с Open Waters
            if self.bboxes:
                return {
                    "APIKey": self.api_key,
                    "BoundingBoxes": self.bboxes,
                    "FilterMessageTypes": self.message_types,
                }
            return {
                "APIKey": self.api_key,
                "FilterMessageTypes": self.message_types,
            }
        return {
            "APIKey": self.api_key,
            "BoundingBoxes": self.bboxes,
            "FilterMessageTypes": self.message_types,
        }

    def run(self):
        if not self.api_key:
            self.last_error = "не задан API-ключ потока"
            self.log(f"Нет API-ключа для {self.stream_url} — живой поток не запустить.")
            return

        try:
            import websockets.sync.client  # noqa: F401
        except ImportError:
            self.last_error = "не установлена библиотека websockets"
            self.log("Не установлена библиотека websockets — выполните pip install -r requirements.txt")
            return

        backoff = 2.0
        while not self.stop_event.is_set():
            opened_at = time.monotonic()
            events_before = self.events
            try:
                with open_stream(self.stream_url) as socket:
                    try:
                        payload = self.subscription()
                        if not payload:
                            self.last_error = "нет судов для слежения по MMSI"
                            self.log(f"{self.feed_title}: пока нет судов в базе — "
                                     f"подписка не отправлена.")
                            return
                        socket.send(json.dumps(payload))
                        self.log(f"Подключено к {self.stream_url}, подписка отправлена.")
                    except Exception as sub_error:
                        if "openwaters" in (self.stream_url or "").lower():
                            self.log(f"Open Waters: попытка подписки не удалась ({sub_error}), слушаем без явной подписки...")
                        else:
                            raise
                    self.connected = True
                    self.connected_since = datetime.now()
                    self.last_error = ""
                    for frame in socket:
                        if self.stop_event.is_set():
                            break
                        self.handle_frame(frame)
            except Exception as error:  # сеть, ключ, лимиты — всё лечится переподключением
                text, status = describe_error(error)
                if text != self.last_error:
                    self.last_error = text
                    if not self.stop_event.is_set():
                        self.log(f"Поток AIS прерван: {text}")
                if status == 429:
                    # повторять сразу нельзя: сервер только усилит отказ
                    backoff = max(backoff * 2, 45.0)
                elif status in (401, 403):
                    backoff = max(backoff, 60.0)
            finally:
                self.connected = False

            uptime = time.monotonic() - opened_at
            if uptime < 5 and self.events == events_before and self.last_error == "":
                self.log("Соединение закрылось без ошибки — сервер закрыл поток.")
            # долгое соединение — начинаем паузу заново, короткие сбои — наращиваем
            if uptime > 120:
                backoff = 2.0
            elif uptime >= 5:
                backoff = min(backoff * 2, 60.0)
            if self.stop_event.wait(backoff + random.uniform(0, 1.5)):
                break
        self.connected = False

    def handle_frame(self, frame):
        try:
            text = frame.decode("utf-8") if isinstance(frame, (bytes, bytearray)) else frame
            event = parse_message(json.loads(text))
        except (ValueError, UnicodeDecodeError):
            return
        if not event:
            return
        self.events += 1
        self.on_event(event)


class MmsiWatchFeed(AISStreamFeed):
    """Слежение за нашими судами по MMSI в любой точке мира.

    Обычные подписки ограничены районом портов, поэтому судно, которое уже
    ушло в Средиземное море, из кадра пропадает. AISStream умеет фильтр
    FiltersShipMMSI: подписка без прямоугольников присылает сообщения только
    по перечисленным судам, где бы они ни находились. Список берётся из базы
    и обновляется при каждом переподключении.
    """

    feed_title = "AISStream — слежение по MMSI (весь мир)"

    def __init__(self, api_key, mmsis, message_types, on_event, log, stop_event,
                 stream_url=None, name="ais-feed-mmsi"):
        super().__init__(api_key, [], message_types, on_event, log, stop_event,
                         stream_url=stream_url, name=name)
        self.mmsi_source = mmsis if callable(mmsis) else (lambda: list(mmsis or []))

    def mmsis(self) -> list:
        try:
            values = self.mmsi_source() or []
        except Exception:
            return []
        result = []
        for value in values:
            text = str(value or "").strip()
            if text.isdigit():
                result.append(text)
        return result

    def subscription(self) -> dict:
        mmsis = self.mmsis()
        if not mmsis:
            return {}
        return {"APIKey": self.api_key, "FiltersShipMMSI": mmsis,
                "FilterMessageTypes": self.message_types}


# --------------------------------------------------------------- снимок района

def ports_bbox(ports, margin=0.35, max_area=95.0):
    """Прямоугольник вокруг портов наблюдения: (юг, запад, север, восток).

    Анонимный доступ Open Waters ограничен 100 кв. градусами, поэтому район
    снимка считается именно по портам, а не по всей акватории подписки.
    """
    lats, lons = [], []
    for port in ports or []:
        try:
            lats.append(float(port["lat"]))
            lons.append(float(port["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not lats:
        return None
    south, north = min(lats) - margin, max(lats) + margin
    west, east = min(lons) - margin, max(lons) + margin
    height = north - south
    width = east - west
    if height * width > max_area and height > 0:
        # слишком большой район — сужаем, чтобы запрос не был отклонён
        scale = (max_area / (height * width)) ** 0.5
        center_lat, center_lon = (south + north) / 2, (west + east) / 2
        south, north = center_lat - height * scale / 2, center_lat + height * scale / 2
        west, east = center_lon - width * scale / 2, center_lon + width * scale / 2
    return (round(south, 3), round(west, 3), round(north, 3), round(east, 3))


def tile_boxes(boxes, max_area=90.0):
    """Делит районы подписки на части ≤ max_area кв. градусов.

    Анонимный доступ Open Waters отказывает в районе больше 100 кв. градусов,
    поэтому большой район нарезается на полосы и запрашивается по частям.
    """
    tiles = []
    for box in boxes or []:
        try:
            (lat1, lon1), (lat2, lon2) = box
            south, north = sorted((float(lat1), float(lat2)))
            west, east = sorted((float(lon1), float(lon2)))
        except (TypeError, ValueError):
            continue
        height, width = north - south, east - west
        if height <= 0 or width <= 0:
            continue
        parts = max(1, math.ceil((height * width) / float(max_area)))
        if width >= height:
            step = width / parts
            for index in range(parts):
                tiles.append((south, west + index * step, north, west + (index + 1) * step))
        else:
            step = height / parts
            for index in range(parts):
                tiles.append((south + index * step, west, south + (index + 1) * step, east))
    return tiles


def parse_seen(value):
    """Время последнего сообщения из снимка: «2026-10-31T13:17:49Z» или ISO."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment


def snapshot_event(feature, max_age_hours=6.0):
    """GeoJSON-объект снимка → событие позиции (или None, если данные устарели)."""
    if not isinstance(feature, dict):
        return None
    props = feature.get("properties") or {}
    geometry = feature.get("geometry") or {}
    coords = geometry.get("coordinates") or []
    if len(coords) < 2:
        return None
    try:
        lon, lat = float(coords[0]), float(coords[1])
    except (TypeError, ValueError):
        return None
    mmsi = props.get("mmsi") or feature.get("id")
    if not mmsi:
        return None
    moment = parse_seen(props.get("seen") or props.get("timestamp"))
    if moment is None:
        return None
    if max_age_hours and (datetime.now() - moment).total_seconds() > float(max_age_hours) * 3600:
        return None
    heading = props.get("heading")
    try:
        heading = int(heading)
    except (TypeError, ValueError):
        heading = None
    if heading is not None and not 0 <= heading <= 359:
        heading = None
    return {
        "kind": "position",
        "mmsi": str(mmsi).strip().zfill(9),
        "lat": lat,
        "lon": lon,
        "sog": to_float(props.get("sog")),
        "cog": to_float(props.get("cog")),
        "heading": heading,
        "name": clean_text(props.get("name") or props.get("shipname")),
        "at": moment,
        "source": "snapshot",
    }


class SnapshotFeed(threading.Thread):
    """Периодический снимок всех известных судов района (REST, без ключа).

    Живой веб-сокет присылает только свежие сообщения: судно, стоящее в порту
    и передающее раз в несколько минут, может не попасть в кадр, а после обрыва
    связи карта пустеет. Снимок закрывает оба случая — он отдаёт последнюю
    известную позицию каждого судна района.
    """

    feed_title = "Снимок района (REST, без ключа)"

    def __init__(self, bboxes, on_event, log, stop_event, interval_minutes=10,
                 max_age_hours=6.0, name="ais-snapshot"):
        super().__init__(daemon=True, name=name)
        if isinstance(bboxes, tuple) and len(bboxes) == 4:
            bboxes = [bboxes]
        self.bboxes = list(bboxes or [])
        self.on_event = on_event
        self.log = log or (lambda message: None)
        self.stop_event = stop_event
        self.interval_minutes = max(1.0, float(interval_minutes or 10))
        self.max_age_hours = float(max_age_hours or 6.0)
        self.connected = False
        self.events = 0
        self.last_error = ""
        self.connected_since = None
        self.last_ships = 0
        self.last_stale = 0

    def run(self):
        while not self.stop_event.is_set():
            self.fetch_once()
            if self.stop_event.wait(self.interval_minutes * 60):
                break
        self.connected = False

    def fetch_once(self) -> int:
        if not self.bboxes:
            self.last_error = "не задан район наблюдения"
            return 0
        try:
            import requests
        except ImportError:
            self.last_error = "не установлена библиотека requests"
            self.log("Снимок района: не установлена requests — выполните pip install -r requirements.txt")
            return 0

        ships = stale = 0
        failed = None
        for box in self.bboxes:
            query = ",".join(f"{value:.3f}" for value in box)
            try:
                response = requests.get(SNAPSHOT_URL, params={"bbox": query}, timeout=25)
                response.raise_for_status()
                data = response.json()
            except Exception as error:  # сеть, лимит, формат — просто ждём следующий цикл
                failed = f"снимок района недоступен: {error}"
                continue
            features = data.get("features") if isinstance(data, dict) else None
            if not isinstance(features, list):
                failed = "снимок района: неожиданный ответ сервера"
                continue
            for feature in features:
                event = snapshot_event(feature, self.max_age_hours)
                if not event:
                    stale += 1
                    continue
                ships += 1
                self.events += 1
                self.on_event(event)

        if failed and not ships:
            self.connected = False
            if failed != self.last_error:
                self.last_error = failed
                if not self.stop_event.is_set():
                    self.log(failed)
            return 0

        self.connected = True
        self.connected_since = self.connected_since or datetime.now()
        self.last_error = failed or ""
        self.last_ships = ships
        self.last_stale = stale
        tail = f", устаревших пропущено {stale}" if stale else ""
        boxes_text = " + ".join(", ".join(f"{value:.2f}" for value in box) for box in self.bboxes)
        self.log(f"Снимок района ({boxes_text}): судов {ships}{tail}.")
        return ships


# ---------------------------------------------------- история трека и стоянки

TRACK_URL = "https://ais.openwaters.io/v1/vessels/{mmsi}/track"


def parse_track(data) -> list:
    """GeoJSON-трек (LineString + times/sog) → список точек с временем."""
    if not isinstance(data, dict):
        return []
    coords = (data.get("geometry") or {}).get("coordinates") or []
    props = data.get("properties") or {}
    times = props.get("times") or []
    sogs = props.get("sog") or []
    points = []
    for index, coord in enumerate(coords):
        if not isinstance(coord, (list, tuple)) or len(coord) < 2:
            continue
        try:
            lon, lat = float(coord[0]), float(coord[1])
        except (TypeError, ValueError):
            continue
        moment = parse_seen(times[index]) if index < len(times) else None
        if moment is None:
            continue
        sog = to_float(sogs[index]) if index < len(sogs) else None
        points.append({"at": moment, "lat": lat, "lon": lon, "sog": sog})
    points.sort(key=lambda point: point["at"])
    return points


def fetch_track(mmsi, timeout=25) -> list:
    """История трека судна за сутки — Open Waters, без ключа."""
    import requests

    response = requests.get(TRACK_URL.format(mmsi=mmsi), timeout=timeout)
    response.raise_for_status()
    return parse_track(response.json())


def stays_from_track(points, ports, min_hours=0.25, gap_minutes=180):
    """Стоянки судна в наших портах по истории трека.

    Точки трека, попавшие в геозону порта, складываются в визит: время входа —
    первая такая точка, время выхода — последняя. Пропуск в данных больше
    gap_minutes закрывает визит, чтобы «дырка» в треке не превращалась
    в многодневную стоянку.
    """
    if not points or not ports:
        return []
    stays = []
    current = None
    for point in points:
        port = geo.port_at(point["lat"], point["lon"], ports)
        name = port["name"] if port else None
        if current and name == current["port"]:
            if (point["at"] - current["last"]).total_seconds() > gap_minutes * 60:
                stays.append(current)
                current = None
            else:
                current["last"] = point["at"]
                continue
        if current:
            stays.append(current)
            current = None
        if name:
            current = {"port": name, "first": point["at"], "last": point["at"]}
    if current:
        stays.append(current)

    now = datetime.now()
    result = []
    for stay in stays:
        hours = (stay["last"] - stay["first"]).total_seconds() / 3600
        if hours < min_hours:
            continue
        # «стоит сейчас», только если последняя точка совсем свежая
        ongoing = (now - stay["last"]).total_seconds() < 90 * 60
        result.append({
            "port": stay["port"],
            "entered_at": stay["first"],
            "left_at": None if ongoing else stay["last"],
            "hours": hours,
            "ongoing": ongoing,
        })
    return result
