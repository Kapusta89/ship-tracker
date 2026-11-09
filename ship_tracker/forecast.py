"""Прогноз маршрута судна на 3 дня вперёд.

AIS передаёт только текущую точку, курс и скорость, поэтому будущий путь
считаем сами: каждые два часа сдвигаем судно. Если в поле Destination стоит
наш порт, линия доводится до порта и судно «входит» в него в расчётное время,
а не уходит мимо. Если нашего порта в назначении нет (или назначение не
передано), строим линию по текущему курсу и скорости — она показывает, где
судно окажется через сутки, двое и трое, и куда его снесёт, если оно не
свернёт. Дальше трёх дней прогноз по курсу смысла не имеет.
"""

import math
from datetime import datetime, timedelta

import geo

# Дальше трёх дней прогноз по курсу не имеет смысла: судно успевает
# сменить рейс, а погода и течения не учитываются вовсе.
MAX_DAYS = 3.0
STEP_HOURS = 2.0
# Насколько быстро судно может довернуть на новый курс: больше 20° в час
# на практике не бывает, а мгновенный разворот на карте выглядел бы обманом.
TURN_DEG_PER_HOUR = 20.0
KNOT_KMH = 1.852
MIN_SPEED_KNOTS = 0.5


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    """Начальный азимут от точки 1 к точке 2, градусы."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def destination_point(lat, lon, bearing, distance_km):
    """Точка на расстоянии distance_km по азимуту bearing."""
    angular = distance_km / geo.EARTH_RADIUS_KM
    phi1, lambda1 = math.radians(lat), math.radians(lon)
    theta = math.radians(bearing)
    sin_phi2 = (math.sin(phi1) * math.cos(angular)
                + math.cos(phi1) * math.sin(angular) * math.cos(theta))
    phi2 = math.asin(max(-1.0, min(1.0, sin_phi2)))
    lambda2 = lambda1 + math.atan2(
        math.sin(theta) * math.sin(angular) * math.cos(phi1),
        math.cos(angular) - math.sin(phi1) * sin_phi2)
    return math.degrees(phi2), (math.degrees(lambda2) + 540.0) % 360.0 - 180.0


def _label(hours: float) -> str:
    if hours <= 0.01:
        return "сейчас"
    if hours < 24:
        return f"+{round(hours)} ч"
    days, rest = divmod(round(hours), 24)
    if rest == 0:
        return f"+{days} д"
    return f"+{days} д {rest} ч"


def _turn(current, desired, limit):
    """Поворот от current к desired не более чем на limit градусов."""
    if current is None:
        return desired
    diff = ((desired - current + 540.0) % 360.0) - 180.0
    return (current + max(-limit, min(limit, diff))) % 360.0


def estimates(vessel, track=None):
    """Скорость и курс для прогноза — из последних данных AIS.

    Если есть трек за последние часы, скорость берётся средней по нему: одна
    шумная цифра SOG не должна сдвигать весь маршрут. Возвращает (узлы, курс
    или None, источник).
    """
    vessel = vessel or {}
    speed = None
    try:
        speed = float(vessel.get("sog") or 0)
    except (TypeError, ValueError):
        speed = None
    course = vessel.get("cog")
    if course is None:
        course = vessel.get("heading")
    try:
        course = float(course) if course is not None else None
    except (TypeError, ValueError):
        course = None
    if course is not None and not (0 < course < 360):
        course = None

    points = [p for p in (track or []) if p.get("lat") is not None]
    recent = points[-6:]
    speeds = []
    for point in recent:
        try:
            value = float(point.get("sog") or 0)
        except (TypeError, ValueError):
            continue
        if MIN_SPEED_KNOTS <= value <= 40:
            speeds.append(value)
    source = "AIS"
    if len(speeds) >= 3:
        speeds.sort()
        speed = speeds[len(speeds) // 2]
        source = "средняя по треку"
    if course is None and len(points) >= 2:
        first, last = points[-2], points[-1]
        course = bearing_deg(float(first["lat"]), float(first["lon"]),
                             float(last["lat"]), float(last["lon"]))
        source += ", курс по треку"
    return speed, course, source


def target_port(vessel, ports):
    """Порт назначения из поля Destination (с кодами UN/LOCODE)."""
    destination = (vessel or {}).get("destination") or ""
    if not destination:
        return None
    aliases = geo.port_aliases(ports or [])
    text = " ".join(str(destination).upper().replace(",", " ").replace("/", " ").split())
    tokens = set(text.split())
    for name, variants in aliases.items():
        for variant in variants:
            if variant and variant in tokens:
                port = geo.port_by_name(name, ports or [])
                if port:
                    return port
    return None


def route(vessel, ports, days=MAX_DAYS, step_hours=STEP_HOURS, track=None):
    """Точки прогноза: [{'hours', 'at', 'lat', 'lon', 'label', 'course', ...}].

    Первая точка — текущее место, дальше — каждые `step_hours` часов. Если на
    Destination наш порт, маршрут ведёт в него: судно плавно доворачивает на
    порт и, дойдя, получает точку с `port` и `arrived`. Без нашего порта в
    Destination линия идёт по текущему курсу и скорости — это ответ на вопрос
    «где судно будет через сутки, двое, трое». Последняя точка — через `days`
    дней или момент прибытия, если судно успевает.
    """
    vessel = vessel or {}
    try:
        lat = float(vessel.get("lat"))
        lon = float(vessel.get("lon"))
    except (TypeError, ValueError):
        return []
    port = target_port(vessel, ports)
    target = None
    if port:
        try:
            target = (float(port["lat"]), float(port["lon"]))
        except (KeyError, TypeError, ValueError):
            port, target = None, None

    speed, course, source = estimates(vessel, track)
    if speed is None or not (MIN_SPEED_KNOTS <= speed <= 40):
        return []
    if target is None and course is None:
        # без порта и без курса рисовать нечего: получилась бы точка на месте
        return []

    speed_kmh = speed * KNOT_KMH
    started = datetime.now()
    points = [{"hours": 0.0, "at": started, "lat": lat, "lon": lon,
               "label": _label(0), "source": source, "course": course,
               "port": (port or {}).get("name")}]
    hours = 0.0
    horizon = days * 24
    while hours < horizon - 1e-9:
        step = min(step_hours, horizon - hours)
        step_km = speed_kmh * step
        if step_km <= 0:
            break
        if target is not None:
            target_lat, target_lon = target
            distance = geo.haversine_km(lat, lon, target_lat, target_lon)
            desired = bearing_deg(lat, lon, target_lat, target_lon)
            new_course = _turn(course, desired, TURN_DEG_PER_HOUR * step)
            off = abs(((desired - new_course + 540.0) % 360.0) - 180.0)
            # Входим в порт, только если он рядом и судно уже смотрит в его
            # сторону: иначе выходящее из порта судно «прибывало» бы в него же.
            if distance <= step_km and (course is None or off <= 45):
                hours += distance / speed_kmh
                points.append({"hours": hours, "at": started + timedelta(hours=hours),
                               "lat": target_lat, "lon": target_lon,
                               "label": _label(hours), "port": port.get("name"),
                               "course": desired, "arrived": True})
                break
        else:
            new_course = course
        lat, lon = destination_point(lat, lon, new_course, step_km)
        course = new_course
        hours += step
        points.append({"hours": hours, "at": started + timedelta(hours=hours),
                       "lat": lat, "lon": lon, "label": _label(hours),
                       "source": source, "course": new_course,
                       "port": (port or {}).get("name")})
    return points


def course_text(course):
    """Курс словами: 245° — юго-запад."""
    if course is None:
        return "—"
    names = ("север", "северо-восток", "восток", "юго-восток", "юг", "юго-запад",
             "запад", "северо-запад")
    index = int((float(course) % 360) / 45.0 + 0.5) % 8
    return f"{float(course) % 360:.0f}° ({names[index]})"


def distance_text(points, index=-1):
    """Сколько километров пройдёт судно от текущей точки до точки index."""
    if len(points) < 2:
        return None
    subset = points if index is None or index < 0 else points[:index + 1]
    total = 0.0
    for first, second in zip(subset, subset[1:]):
        total += geo.haversine_km(float(first["lat"]), float(first["lon"]),
                                  float(second["lat"]), float(second["lon"]))
    return total


def daily_points(points):
    """Только опорные точки: сейчас и каждые сутки (для подписи)."""
    result = [points[0]] if points else []
    seen = set()
    for point in points[1:]:
        day = int(round(point["hours"] / 24.0))
        if day >= 1 and day not in seen and abs(point["hours"] - day * 24) < 1e-6:
            seen.add(day)
            result.append(point)
    if points and points[-1] not in result:
        result.append(points[-1])
    return result


def summary(points, port_name=None):
    """Итог прогноза одной строкой: когда судно будет в порту."""
    if not points:
        return ""
    last = points[-1]
    if last.get("arrived") and last.get("port"):
        return (f"в {last['port']} через {_label(last['hours'])}"
                f" ({last['at']:%d.%m %H:%M})")
    if port_name:
        return f"за {int(MAX_DAYS)} сут до {port_name} не доходит"
    return ""
