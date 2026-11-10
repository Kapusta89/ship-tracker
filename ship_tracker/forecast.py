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
# План на четыре дня: путь до порта + стоянка + выход в море на остаток срока.
PLAN_DAYS = 4.0
# Сколько судно обычно стоит, если наблюдений ещё нет.
DEFAULT_STAY_HOURS = 24.0
# Скорость на выходе из порта, если AIS её не передал (обычный ход сухогруза).
DEFAULT_SEA_KNOTS = 8.0
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
    if speed is None or not (MIN_SPEED_KNOTS <= float(speed) <= 40):
        # хода в AIS нет — считаем его сами по треку: расстояние между
        # соседними точками делим на время между ними
        derived = []
        stamped = [point for point in points if point.get("ts")][-8:]
        for first, second in zip(stamped, stamped[1:]):
            try:
                t1 = datetime.fromisoformat(str(first["ts"])[:19])
                t2 = datetime.fromisoformat(str(second["ts"])[:19])
                km = geo.haversine_km(float(first["lat"]), float(first["lon"]),
                                      float(second["lat"]), float(second["lon"]))
            except (TypeError, ValueError, KeyError):
                continue
            hours = (t2 - t1).total_seconds() / 3600.0
            if hours <= 0 or km <= 0:
                continue
            knots = km / hours / KNOT_KMH
            if MIN_SPEED_KNOTS <= knots <= 40:
                derived.append(knots)
        if len(derived) >= 2:
            derived.sort()
            speed = derived[len(derived) // 2]
            source = f"по треку (расчёт: {len(derived)} отрезков)"
        else:
            speed = None
            source = "AIS ход не передаёт" + (", курс по треку" if "курс по треку" in source else "")
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


def duration_text(hours):
    """Длительность словами: «40 мин», «14 ч», «1 д 6 ч», «3 д»."""
    if hours is None:
        return "—"
    hours = max(0.0, float(hours))
    if hours < 1:
        return f"{round(hours * 60)} мин"
    if hours < 24:
        return f"{hours:.0f} ч"
    days, rest = divmod(round(hours), 24)
    if rest == 0:
        return f"{days} д"
    return f"{days} д {rest} ч"


def plan(vessel, ports, stay=None, days=PLAN_DAYS, track=None, now=None, pilot=None,
         fallback_speed=None, fallback_count=0):
    """План судна на 4 дня: путь до порта, стоянка в нём и выход в море.

    AIS не знает ни времени стоянки, ни дальнейшего рейса, поэтому план
    собирается из того, что видно: маршрут до порта считается по курсу и
    скорости (как в `route`), стоянка — по нашим наблюдениям (`core.port_stay`),
    а после выхода судно идёт в море тем же ходом в сторону, откуда пришло.

    `stay` — ответ `core.port_stay`: current_hours (уже стоит), typical_hours
    (сколько обычно стоит) и remaining_hours (сколько осталось). Если стоянка
    займёт сутки, на море в четырёхдневном плане остаётся три дня; если двое —
    два дня, и так далее.

    `pilot` — ответ `core.pilot_wait`: сколько у этого порта обычно ждут
    лоцмана и по скольким визитам это видно. Время до выхода из порта =
    путь + ожидание лоцмана + стоянка; каждое слагаемое возвращается вместе
    с источником, чтобы цифры можно было проверить.
    """
    vessel = vessel or {}
    stay = stay or {}
    now = now or datetime.now()
    port = target_port(vessel, ports) or geo.port_by_name(stay.get("port"), ports or [])
    if not port:
        return None
    try:
        port_lat, port_lon = float(port["lat"]), float(port["lon"])
    except (KeyError, TypeError, ValueError):
        return None

    speed, course, source = estimates(vessel, track)
    # Ход нигде не нашёлся — берём средний по судам района: это тоже расчёт,
    # а не выдуманное число, и в разборе видно, сколько судов дали среднее.
    if not speed and fallback_speed:
        speed = float(fallback_speed)
        source = (f"нет в AIS — средний ход по району {speed:.1f} уз "
                  f"({fallback_count} судов)")
    # измерен ли ход прямо в AIS или посчитан математически
    speed_computed = bool(speed) and not str(source).startswith(("AIS", "средняя по треку"))
    in_port = stay.get("current_hours") is not None
    position_age = None
    stamp = vessel.get("position_at") or vessel.get("updated_at")
    if stamp:
        try:
            position_age = max(0.0, (now - datetime.fromisoformat(
                str(stamp)[:19])).total_seconds() / 3600.0)
        except (TypeError, ValueError):
            position_age = None
    horizon_end = now + timedelta(days=days)

    points = []
    arrival = None
    estimate = False
    if not in_port:
        route_points = route(vessel, [port], days=days, track=track)
        points.extend(dict(point, phase="approach") for point in route_points
                      if not point.get("arrived"))
        arrival = next((point for point in route_points if point.get("arrived")), None)
        if arrival:
            points.append(dict(arrival, phase="port"))
        elif not route_points:
            # Маршрут не строится без хода и курса (судно на рейде или в списке
            # AIS-catcher без координат). Если позиция есть — считаем приход
            # по прямой, чтобы план не был пустым.
            arrival = _arrival_from_distance(vessel, port, port_lat, port_lon, now,
                                            speed, course)
            if arrival:
                estimate = True
                points = [{"hours": 0.0, "at": now, "lat": float(vessel["lat"]),
                           "lon": float(vessel["lon"]), "label": "сейчас",
                           "phase": "approach", "course": course},
                          dict(arrival, phase="port")]

    # Стоянка: сначала остаток по истории судна, потом среднее по порту,
    # и только если наблюдений нет вовсе — сутки по умолчанию.
    remaining = stay.get("remaining_hours")
    typical = stay.get("typical_hours")
    if remaining is not None and float(remaining) > 0.5:
        stay_hours, stay_scope = float(remaining), stay.get("scope") or "порт"
    elif typical:
        # Норматив уже пересижен — точного времени выхода нет: даём
        # короткий запас (не больше шести часов), а не «ноль минут».
        if remaining is not None:
            stay_hours, stay_scope = min(float(typical), 6.0), "перестой"
        else:
            stay_hours, stay_scope = float(typical), stay.get("scope") or "порт"
    else:
        stay_hours, stay_scope = DEFAULT_STAY_HOURS, "оценка"

    # Ожидание лоцмана берём только для тех, кто ещё в море: у стоящего
    # в порту это ожидание уже позади, и второй раз его считать нельзя.
    pilot = pilot or {}
    observed_pilot = pilot.get("hours")
    pilot_hours = float(observed_pilot) if (observed_pilot is not None and not in_port) else 0.0

    arrival_hours = arrival_at = departure_at = None
    if in_port:
        arrival_hours, arrival_at = 0.0, now
        departure_at = now + timedelta(hours=stay_hours)
        points.insert(0, {"hours": 0.0, "at": now, "lat": port_lat, "lon": port_lon,
                          "label": "в порту", "phase": "port", "port": port.get("name")})
    elif arrival:
        arrival_hours, arrival_at = float(arrival["hours"]), arrival["at"]
        departure_at = arrival_at + timedelta(hours=pilot_hours + stay_hours)
        points[-1] = dict(arrival, phase="port", label=f"стоянка {duration_text(stay_hours)}")

    # Курс выхода: обратно туда, откуда судно пришло. Для стоящего в порту —
    # противоположный его последнему курсу.
    out_course = None
    if in_port and course is not None:
        out_course = (float(course) + 180.0) % 360.0
    else:
        try:
            out_course = bearing_deg(port_lat, port_lon,
                                     float(vessel.get("lat")), float(vessel.get("lon")))
        except (TypeError, ValueError):
            if course is not None:
                out_course = (float(course) + 180.0) % 360.0

    sea_hours = 0.0
    if departure_at is not None:
        sea_hours = max(0.0, (horizon_end - departure_at).total_seconds() / 3600.0)
    knots = speed if speed and MIN_SPEED_KNOTS <= speed <= 40 else DEFAULT_SEA_KNOTS
    if sea_hours > 0.01 and out_course is not None:
        kmh = knots * KNOT_KMH
        lat, lon, hours = port_lat, port_lon, 0.0
        while hours < sea_hours - 1e-9:
            step = min(STEP_HOURS, sea_hours - hours)
            lat, lon = destination_point(lat, lon, out_course, kmh * step)
            hours += step
            points.append({"hours": (arrival_hours + pilot_hours + stay_hours + hours
                                          if arrival_hours is not None else hours),
                           "at": departure_at + timedelta(hours=hours),
                           "lat": lat, "lon": lon, "phase": "sea",
                           "label": f"море {duration_text(hours)}",
                           "course": out_course, "port": port.get("name")})

    # Сколько осталось идти — по самому маршруту, а не по прямой «на глаз».
    approach_km = distance_text([point for point in points
                                 if point.get("phase") != "sea"])

    evidence = []
    if in_port:
        evidence.append("Стоянка: открытый визит в порту — время считается "
                        "с входа в геозону по нашим наблюдениям")
    else:
        if speed:
            evidence.append(f"Ход: {speed:.1f} уз — {source}")
        else:
            evidence.append(f"Ход: данных нет — в расчёте резервные "
                            f"{DEFAULT_SEA_KNOTS:.1f} уз (оценка)")
        if approach_km:
            evidence.append(
                f"Остаток пути: {approach_km:.0f} км — "
                + ("по прямой до порта (курс/ход не годятся для маршрута)"
                   if estimate else "маршрут по курсу AIS, шаг 2 ч"))
        if arrival_at:
            evidence.append(
                f"Приход: {arrival_at:%d.%m %H:%M} — "
                + ("оценка по расстоянию" if estimate else "расчёт по курсу и ходу AIS"))
        if position_age is not None and position_age > 1.0:
            evidence.append(f"Позиция: {duration_text(position_age)} назад — путь считается от неё")
    if arrival_at or in_port:
        if in_port:
            evidence.append("Лоцман: судно уже в порту — ожидание в сумму не входит")
        elif observed_pilot is None:
            evidence.append(
                f"Лоцман: своих наблюдений за {port.get('name')} нет — "
                "в сумму не входит (не выдумываем)")
        elif pilot.get("scope") == "все порты":
            evidence.append(
                f"Лоцман: {duration_text(pilot_hours)} — расчёт по всем портам "
                f"({pilot.get('samples')} визитов за {pilot.get('window_days')} дн., "
                f"разброс {duration_text(pilot.get('min_hours'))}…"
                f"{duration_text(pilot.get('max_hours'))})")
        else:
            evidence.append(
                f"Лоцман: {duration_text(pilot_hours)} — медиана по {pilot.get('samples')} "
                f"визитам порта за {pilot.get('window_days')} дн. "
                f"(разброс {duration_text(pilot.get('min_hours'))}…"
                f"{duration_text(pilot.get('max_hours'))})")
        if stay_scope in ("тип", "все порты", "норматив"):
            evidence.append(
                f"Стоянка: {duration_text(stay_hours)} — расчёт по «{stay_scope}» "
                f"({stay.get('visits') or 0} визитов): своей истории у судна и порта нет")
        elif stay_scope == "оценка":
            evidence.append(f"Стоянка: {duration_text(stay_hours)} — истории визитов нет, "
                            "значение по умолчанию")
        else:
            evidence.append(f"Стоянка: {duration_text(stay_hours)} — по истории «{stay_scope}», "
                            f"визитов {stay.get('visits') or 0}")
        evidence.append("Всего до выхода: путь + лоцман + стоянка")

    return {
        "port": port.get("name"),
        "port_label": port.get("label") or port.get("name"),
        "points": points,
        "speed": speed,
        "course": course,
        "source": source,
        "speed_computed": speed_computed,
        "arrival_hours": arrival_hours,
        "arrival_at": arrival_at,
        "arrival_text": arrival and arrival.get("label"),
        "stay_hours": stay_hours,
        "stay_scope": stay_scope,
        "stay_visits": (stay or {}).get("visits"),
        "pilot_hours": observed_pilot if not in_port else None,
        "pilot_samples": pilot.get("samples"),
        "pilot_window_days": pilot.get("window_days"),
        "pilot_min_hours": pilot.get("min_hours"),
        "pilot_max_hours": pilot.get("max_hours"),
        "pilot_used": pilot_hours,
        "approach_km": approach_km,
        "position_age_hours": position_age,
        "total_hours": ((departure_at - now).total_seconds() / 3600.0
                        if departure_at else None),
        "evidence": evidence,
        "departure_at": departure_at,
        "sea_hours": sea_hours,
        "sea_knots": knots if sea_hours > 0.01 else None,
        "out_course": out_course,
        "in_port": in_port,
        "complete": bool(arrival or in_port),
        "arrival_estimate": estimate,
        "no_position": vessel.get("lat") is None or vessel.get("lon") is None,
        "days": days,
    }


def _arrival_from_distance(vessel, port, port_lat, port_lon, now, speed, course):
    """Приход по прямой, когда маршрут не строится (на рейде, без хода)."""
    try:
        lat, lon = float(vessel.get("lat")), float(vessel.get("lon"))
    except (TypeError, ValueError):
        return None
    km = geo.haversine_km(lat, lon, port_lat, port_lon)
    knots = speed if speed and MIN_SPEED_KNOTS <= speed <= 40 else DEFAULT_SEA_KNOTS
    hours = max(0.25, km / (knots * KNOT_KMH))
    return {"hours": hours, "at": now + timedelta(hours=hours),
            "lat": port_lat, "lon": port_lon, "label": _label(hours),
            "course": bearing_deg(lat, lon, port_lat, port_lon),
            "port": (port or {}).get("name"), "estimated": True}


def plan_route_text(result):
    """План одной строкой для таблицы."""
    if not result:
        return "—"
    parts = []
    if result.get("no_position") and not result.get("in_port"):
        parts.append("нет позиции AIS")
        return " → ".join(parts)
    if result.get("in_port"):
        parts.append(f"в порту, выход через {duration_text(result.get('stay_hours'))}")
    elif result.get("arrival_at"):
        parts.append(f"в пути {duration_text(result.get('arrival_hours'))}")
        if result.get("pilot_hours") is not None:
            parts.append(f"лоцман {duration_text(result.get('pilot_hours'))}")
        parts.append(f"стоянка {duration_text(result.get('stay_hours'))}")
        if result.get("total_hours") is not None:
            parts.append(f"всего до выхода {duration_text(result.get('total_hours'))}")
    else:
        parts.append(f"за {int(result.get('days') or PLAN_DAYS)} сут не доходит")
    if result.get("sea_hours"):
        parts.append(f"в море {duration_text(result.get('sea_hours'))}")
    return " → ".join(parts)


def plan_detail(result):
    """Подробный план для карточки во вкладке «Прогноз 4 дня»."""
    if not result:
        return "Нет данных для плана."
    lines = [f"Порт: {result.get('port_label') or result.get('port')}"]
    if result.get("in_port"):
        lines.append("Сейчас: в порту")
    elif result.get("arrival_at"):
        mark = "≈" if result.get("arrival_estimate") else ""
        source = " — оценка по расстоянию" if result.get("arrival_estimate") else ""
        lines.append(f"Приход: {mark}{result['arrival_at']:%d.%m %H:%M} "
                     f"(через {duration_text(result.get('arrival_hours'))}){source}")
    elif result.get("no_position"):
        lines.append("Приход: нет позиции AIS — судно только в списке AIS-catcher")
    else:
        lines.append(f"Приход: за {int(result.get('days') or PLAN_DAYS)} сут не доходит")
    if result.get("approach_km"):
        lines.append(f"Остаток пути: {result['approach_km']:.0f} км")
    if result.get("departure_at"):
        pilot = result.get("pilot_hours")
        if pilot is None:
            lines.append("Лоцман: своих наблюдений за этот порт нет — в сумму не входит")
        else:
            lines.append(
                f"Лоцман: {duration_text(pilot)} — медиана по {result.get('pilot_samples')} "
                f"визитам за {result.get('pilot_window_days')} дн. "
                f"(разброс {duration_text(result.get('pilot_min_hours'))}…"
                f"{duration_text(result.get('pilot_max_hours'))})")
        lines.append(f"Стоянка: {duration_text(result.get('stay_hours'))} "
                     f"(по истории: {result.get('stay_scope')}, визитов "
                     f"{result.get('stay_visits') or 0})")
        lines.append(f"Выход: {result['departure_at']:%d.%m %H:%M}")
        if result.get("total_hours") is not None:
            lines.append(f"Всего до выхода: {duration_text(result.get('total_hours'))} — "
                         "путь + лоцман + стоянка")
        if result.get("sea_hours"):
            knots = result.get("sea_knots")
            speed_text = f" при {knots:.1f} уз" if knots else ""
            lines.append(f"Море после выхода: {duration_text(result.get('sea_hours'))}"
                         f"{speed_text}")
            lines.append(f"Курс выхода: {course_text(result.get('out_course'))}")
    if result.get("speed"):
        lines.append(f"Ход по AIS: {result['speed']:.1f} уз ({result.get('source')})")
    if result.get("evidence"):
        lines.append("Чем подтверждено:")
        lines.extend(f"  · {item}" for item in result["evidence"])
    sea = [point for point in result.get("points") or [] if point.get("phase") == "sea"]
    if sea:
        marks = []
        for point in sea:
            if point["at"].hour >= 0:
                marks.append(f"{point['at']:%d.%m %H:%M} — {point['lat']:.2f}, {point['lon']:.2f}")
        lines.append("Путь после выхода:")
        lines.extend(f"  · {mark}" for mark in marks[-8:])
    return "\n".join(lines)


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
