"""Аналитические признаки судна — то, что не видно в одной точке AIS.

Собрано по методике разбора: флаг, «тёмные» периоды, стоянка вне акватории
порта, цикличность заходов и изменение осадки после выхода. Все признаки
считаются по данным самой программы:

* позиции пишутся при каждом сообщении AIS (`position_interval_seconds`),
  поэтому длинный пропуск в них — судно не передавало AIS, а не сбой разбора;
  если программа была закрыта, пропуска тоже будут, поэтому окно наблюдения
  указывается прямо в подписи;
* осадка до входа и после выхода сохраняется в визитах (`draft_in`,
  `draft_out`), по её разнице видно, разгрузилось судно или загрузилось;
* цикл заходов считается по времени входов в порты из истории визитов.

Точность ограничена открытым AIS: груз в тоннах оценивает `cargo.py`,
а здесь только признаки поведения, которые можно перепроверить.
"""

import math
from datetime import datetime, timedelta

import flags
import geo

# Удобные флаги из записки: офшорные юрисдикции и «теневой флот».
OFFSHORE_FLAGS = {
    "BB": "Барбадос", "SL": "Сьерра-Леоне", "LU": "Люксембург",
    "MT": "Мальта", "PA": "Панама", "LR": "Либерия", "MH": "Маршалловы Острова",
    "TZ": "Танзания", "CM": "Камерун", "GA": "Габон", "PW": "Палау",
    "KM": "Коморы", "VC": "Сент-Винсент и Гренадины", "AG": "Антигуа и Барбуда",
    "BZ": "Белиз", "TG": "Того", "MD": "Молдова", "MN": "Монголия",
}

# Страны НАТО и их союзники: прямые поставки, а не «теневой флот».
NATO_FLAGS = {
    "US": "США", "GB": "Великобритания", "CA": "Канада", "FR": "Франция",
    "DE": "Германия", "IT": "Италия", "ES": "Испания", "PT": "Португалия",
    "NL": "Нидерланды", "BE": "Бельгия", "NO": "Норвегия", "DK": "Дания",
    "IS": "Исландия", "GR": "Греция", "TR": "Турция", "RO": "Румыния",
    "BG": "Болгария", "PL": "Польша", "EE": "Эстония", "LV": "Латвия",
    "LT": "Литва", "HR": "Хорватия", "SI": "Словения", "AL": "Албания",
    "ME": "Черногория", "FI": "Финляндия", "SE": "Швеция",
}

# Минимальный пропуск AIS, который считаем «тёмным» периодом.
DARK_MINUTES = 180
# Насколько далеко от геозоны порта судно ещё считается стоящим на рейде.
ROADSTEAD_KM = 25.0


def _parse(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def span_text(hours) -> str:
    """«6 ч 20 мин» — без секунд, для подписей признаков."""
    try:
        minutes = int(round(float(hours) * 60))
    except (TypeError, ValueError):
        return "—"
    days, rest = divmod(minutes, 24 * 60)
    hours_part, minutes_part = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} дн")
    if hours_part:
        parts.append(f"{hours_part} ч")
    if minutes_part or not parts:
        parts.append(f"{minutes_part} мин")
    return " ".join(parts)


def moment_text(when) -> str:
    when = _parse(when)
    return f"{when:%d.%m %H:%M}" if when else "—"


def flag_note(mmsi) -> str:
    """Признак флага: удобный флаг или страна НАТО (иначе пустая строка)."""
    try:
        _country, iso2, _emoji = flags.flag_for_mmsi(mmsi)
    except Exception:
        return ""
    iso2 = (iso2 or "").upper()
    country = flags.flag_text(mmsi)
    if iso2 in OFFSHORE_FLAGS:
        return f"Флаг: {country} — удобный флаг (офшорная юрисдикция, «теневой флот»)"
    if iso2 in NATO_FLAGS:
        return f"Флаг: {country} — страна НАТО (прямые поставки)"
    return ""


def dark_periods(track_rows, min_minutes=DARK_MINUTES) -> list:
    """Пропуски в приёме AIS: от последнего сообщения до следующего.

    Возвращает список словарей {start, end, hours}, самые длинные первыми.
    """
    moments = []
    for row in track_rows or []:
        at = _parse(row.get("ts") if isinstance(row, dict) else row)
        if at is not None:
            moments.append(at)
    moments.sort()
    gaps = []
    for before, after in zip(moments, moments[1:]):
        minutes = (after - before).total_seconds() / 60.0
        if minutes >= min_minutes:
            gaps.append({"start": before, "end": after, "hours": minutes / 60.0})
    gaps.sort(key=lambda gap: gap["hours"], reverse=True)
    return gaps


def roadstead(vessel, ports, reid_km=ROADSTEAD_KM):
    """Стоит с малой скоростью вне геозоны порта — рейд, очередь или перевалка.

    Возвращает {port, port_label, distance_km, sog} или None. Внутри акватории
    порта (когда сработала геозона) признак не нужен: это обычная стоянка.
    """
    if not vessel or vessel.get("port"):
        return None
    lat, lon = vessel.get("lat"), vessel.get("lon")
    sog = vessel.get("sog")
    if lat is None or lon is None or sog is None:
        return None
    try:
        if float(sog) >= 1.0:
            return None
    except (TypeError, ValueError):
        return None

    nearest = None
    for port in ports or []:
        try:
            distance = geo.haversine_km(float(lat), float(lon),
                                        float(port["lat"]), float(port["lon"]))
            limit = float(port.get("radius_km") or 5.0) + float(reid_km)
        except (KeyError, TypeError, ValueError):
            continue
        if distance <= limit and (nearest is None or distance < nearest[1]):
            nearest = (port, distance)
    if not nearest:
        return None
    port, distance = nearest
    return {"port": port.get("name"), "port_label": port.get("label") or port.get("name"),
            "distance_km": distance, "sog": float(sog)}


def visit_cycle(visits) -> dict:
    """Цикличность заходов: сколько визитов, средний интервал, следующий заход.

    Считаются только визиты с временем входа; открытый (текущий) визит тоже
    участвует — он показывает, что судно на месте прямо сейчас.
    """
    entries = []
    for visit in visits or []:
        at = _parse(visit.get("entered_at"))
        if at is not None:
            entries.append(at)
    entries.sort()
    if len(entries) < 2:
        return {}

    interval_days = []
    for before, after in zip(entries, entries[1:]):
        days = (after - before).total_seconds() / 86400.0
        if days > 0:
            interval_days.append(days)
    if not interval_days:
        return {}

    average = sum(interval_days) / len(interval_days)
    return {
        "visits": len(entries),
        "average_days": average,
        "min_days": min(interval_days),
        "max_days": max(interval_days),
        "last_entered": entries[-1],
        "next_expected": entries[-1] + timedelta(days=average),
        "ports": sorted({visit.get("port") for visit in visits or [] if visit.get("port")}),
    }


def draft_change(visits) -> dict:
    """Осадка до входа и после выхода у последнего завершённого визита.

    Разница больше 0.3 м — заметная загрузка или разгрузка в порту.
    """
    for visit in visits or []:
        draft_in, draft_out = visit.get("draft_in"), visit.get("draft_out")
        if draft_in is None or draft_out is None:
            continue
        try:
            delta = float(draft_out) - float(draft_in)
        except (TypeError, ValueError):
            continue
        if abs(delta) < 0.3:
            continue
        return {"port": visit.get("port"), "draft_in": float(draft_in),
                "draft_out": float(draft_out), "delta_m": delta,
                "left_at": _parse(visit.get("left_at"))}
    return {}


def signals(vessel, track_rows, visits, ports, history_days=7) -> list:
    """Признаки судна для карточки: список готовых строк."""
    if not vessel:
        return []
    result = []
    labels = {str(port.get("name")): port.get("label") or port.get("name")
              for port in ports or []}

    note = flag_note(vessel.get("mmsi"))
    if note:
        result.append(note)

    gaps = dark_periods(track_rows)
    if gaps:
        longest = gaps[0]
        result.append(
            f"Тёмные периоды AIS: {len(gaps)} за {float(history_days):g} дн., "
            f"самый длинный {span_text(longest['hours'])} "
            f"({moment_text(longest['start'])} → {moment_text(longest['end'])}) — "
            f"судно не передавало AIS")

    stop = roadstead(vessel, ports)
    if stop:
        result.append(
            f"Стоит вне акватории: {float(stop['distance_km']):.0f} км от "
            f"{stop['port_label']}, скорость {float(stop['sog']):.1f} уз — "
            f"рейд: ожидание лоцмана/очереди или возможная перевалка")

    cycle = visit_cycle(visits)
    if cycle:
        ports_text = (", ".join(labels.get(name, name) for name in cycle["ports"])
                      if cycle["ports"] else "наши порты")
        spread = (f"{cycle['min_days']:.1f}–{cycle['max_days']:.1f} дн."
                  if cycle["max_days"] - cycle["min_days"] > 0.5
                  else f"каждые {cycle['average_days']:.1f} дн.")
        result.append(
            f"Цикл заходов: визитов {cycle['visits']} ({ports_text}), "
            f"в среднем {spread}; следующий ожидается около "
            f"{moment_text(cycle['next_expected'])}")

    change = draft_change(visits)
    if change:
        loaded = change["delta_m"] > 0
        result.append(
            f"Осадка после выхода: {change['draft_in']:g} → {change['draft_out']:g} м "
            f"({abs(change['delta_m']):.1f} м {'больше — загрузилось' if loaded else 'меньше — выгрузилось'})"
            f", {labels.get(str(change['port']), change['port'])} "
            f"{moment_text(change['left_at'])}")

    return result


# Стороны света для подписи смещения за сутки.
COMPASS = ("север", "северо-восток", "восток", "юго-восток",
           "юг", "юго-запад", "запад", "северо-запад")


def bearing_deg(lat1, lon1, lat2, lon2):
    """Начальный пеленг между точками, градусы от севера по часовой."""
    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
    delta = math.radians(float(lon2) - float(lon1))
    x = math.sin(delta) * math.cos(phi2)
    y = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(delta))
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def compass_word(bearing):
    """Пеленг в стороны света: 8 румбов."""
    index = int((float(bearing) % 360.0) / 45.0 + 0.5) % 8
    return COMPASS[index]


def track_path(rows, gap_minutes=720.0):
    """Сводка пройденного пути по точкам трека — «откуда и куда идёт».

    Считается по своим позициям: расстояние по сегментам между соседними
    точками (пропуски больше `gap_minutes` не соединяем — это разрыв AIS,
    а не ход судна; отрезки без смещения — стоянка — в путь не входят),
    время в пути и средняя скорость на ходу.

    Возвращает словарь: сколько точек, откуда стартовало, сколько прошло
    километров, за сколько часов и с какой средней скоростью, а также
    «прямое» расстояние между первой и последней точкой (насколько путь
    отличается от прямой).
    """
    points = [row for row in (rows or []) if row.get("lat") is not None and row.get("lon") is not None]
    if len(points) < 2:
        return {"points": len(points), "km": 0.0, "straight_km": 0.0,
                "hours": 0.0, "avg_kn": None, "first_at": None}
    km = 0.0
    moving_hours = 0.0
    moving_km = 0.0
    for prev, cur in zip(points, points[1:]):
        start, finish = _parse(prev.get("ts")), _parse(cur.get("ts"))
        if not start or not finish:
            continue
        gap = (finish - start).total_seconds() / 60.0
        if gap <= 0 or gap > gap_minutes:
            continue
        leg = geo.haversine_km(float(prev["lat"]), float(prev["lon"]),
                               float(cur["lat"]), float(cur["lon"]))
        if leg < 0.01:
            # Стоит на месте: те же координаты в нескольких сообщениях.
            continue
        km += leg
        moving_km += leg
        moving_hours += gap / 60.0
    first, last = points[0], points[-1]
    straight = geo.haversine_km(float(first["lat"]), float(first["lon"]),
                                float(last["lat"]), float(last["lon"]))
    start, finish = _parse(first.get("ts")), _parse(last.get("ts"))
    hours = (finish - start).total_seconds() / 3600.0 if start and finish else 0.0
    avg_kn = moving_km / moving_hours / 1.852 if moving_hours > 0.05 and moving_km > 0.2 else None
    bearing = bearing_deg(first["lat"], first["lon"], last["lat"], last["lon"])
    return {"points": len(points), "km": km, "straight_km": straight,
            "hours": hours, "avg_kn": avg_kn, "first_at": start,
            "first_lat": float(first["lat"]), "first_lon": float(first["lon"]),
            "last_at": finish, "bearing": bearing,
            "compass": compass_word(bearing)}
