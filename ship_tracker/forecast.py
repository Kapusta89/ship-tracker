"""Прогноз маршрута судна на несколько дней вперёд.

AIS передаёт только текущую точку, курс и скорость, поэтому будущий путь
считаем сами: каждые несколько часов сдвигаем судно по его курсу с его
скоростью. Если в поле Destination указан наш порт — линия доводится до
порта, и судно «входит» в него в расчётное время, а не уходит мимо.
"""

import math
from datetime import datetime, timedelta

import geo

# Дальше пяти дней прогноз по курсу не имеет смысла.
MAX_DAYS = 5.0
STEP_HOURS = 3.0
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


def route(vessel, ports, days=MAX_DAYS, step_hours=STEP_HOURS):
    """Точки прогноза: [{'hours', 'at', 'lat', 'lon', 'label'}, ...].

    Первая точка — текущее место, последняя — через `days` дней или порт
    назначения, если судно успевает в него дойти.
    """
    vessel = vessel or {}
    try:
        lat = float(vessel.get("lat"))
        lon = float(vessel.get("lon"))
        sog = float(vessel.get("sog") or 0)
    except (TypeError, ValueError):
        return []
    if sog < MIN_SPEED_KNOTS or sog > 40:
        return []

    course = vessel.get("cog")
    if course is None:
        course = vessel.get("heading")
    try:
        course = float(course) if course is not None else None
    except (TypeError, ValueError):
        course = None

    port = target_port(vessel, ports)
    target = None
    if port:
        try:
            target = (float(port["lat"]), float(port["lon"]), port.get("name"))
        except (KeyError, TypeError, ValueError):
            target = None

    speed_kmh = sog * KNOT_KMH
    step_km = speed_kmh * step_hours
    started = datetime.now()
    points = [{"hours": 0.0, "at": started, "lat": lat, "lon": lon,
               "label": _label(0)}]
    hours = 0.0

    while hours < days * 24:
        if target:
            target_lat, target_lon, target_name = target
            distance = geo.haversine_km(lat, lon, target_lat, target_lon)
            if distance <= step_km:
                hours += distance / speed_kmh
                points.append({"hours": hours,
                               "at": started + timedelta(hours=hours),
                               "lat": target_lat, "lon": target_lon,
                               "label": _label(hours), "port": target_name})
                break
            bearing = bearing_deg(lat, lon, target_lat, target_lon)
        elif course is not None:
            bearing = course
        else:
            break

        lat, lon = destination_point(lat, lon, bearing, step_km)
        hours += step_hours
        points.append({"hours": hours, "at": started + timedelta(hours=hours),
                       "lat": lat, "lon": lon, "label": _label(hours)})

    return points


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
