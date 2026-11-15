"""Причалы, краны и верфи из OpenStreetMap вокруг наших портов.

Позиция судна в AIS всегда лежит на воде: судно стоит у стенки, а не на ней,
поэтому координаты стоянки часто попадают «в море» рядом с причалом. Модуль
скачивает портовую инфраструктуру вокруг порта по прямоугольнику, а приложение
держит её в базе (`quay_places`), чтобы карточка показывала ближайший причал
или верфь с их координатами без запросов при каждом открытии.
"""

import json
import time
import urllib.parse
import urllib.request

import geo

ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

# Какие объекты считаем местом разгрузки и как называем их по-русски.
KEEP = {
    ("man_made", "pier"): "причал",
    ("man_made", "quay"): "причал",
    ("man_made", "crane"): "кран у причала",
    ("waterway", "dock"): "док",
    ("seamark:type", "berth"): "причал",
    ("seamark:type", "mooring"): "швартовная точка",
    ("seamark:type", "terminal"): "терминал",
    ("seamark:type", "harbour_basin"): "ковш порта",
    ("industrial", "shipyard"): "верфь",
}


def _kind(tags):
    for (key, value), label in KEEP.items():
        if str(tags.get(key) or "") == value:
            return label
    if str(tags.get("harbour") or ""):
        return "порт"
    return ""


def _query(south, west, north, east):
    box = f"{south:.4f},{west:.4f},{north:.4f},{east:.4f}"
    parts = [
        f'way["man_made"="pier"]({box});',
        f'way["man_made"="quay"]({box});',
        f'way["waterway"="dock"]({box});',
        f'way["industrial"="shipyard"]({box});',
        f'way["harbour"]({box});',
        f'node["seamark:type"="berth"]({box});',
        f'node["seamark:type"="mooring"]({box});',
        f'way["seamark:type"="berth"]({box});',
        f'node["man_made"="crane"]({box});',
    ]
    # geom даёт вершины причала: ближайшая вершина точнее центра длинного пирса
    return "[out:json][timeout:60];(" + "".join(parts) + ");out geom tags 2000;"


def fetch_quays(south, west, north, east):
    """Список объектов портовой инфраструктуры в прямоугольнике.

    Каждый элемент: {lat, lon, name, kind, osm}. Пустой список, если OSM
    недоступен — вызывающий код тогда просто не показывает точку разгрузки.
    """
    payload = urllib.parse.urlencode({"data": _query(south, west, north, east)}).encode()
    answer = None
    for url in ENDPOINTS:
        try:
            request = urllib.request.Request(
                url, data=payload,
                headers={"User-Agent": "ShipTracker/1.0 (study project)"})
            with urllib.request.urlopen(request, timeout=70) as response:
                answer = json.load(response)
            break
        except Exception:
            time.sleep(1)
            continue
    if not answer:
        return []
    result = []
    for item in answer.get("elements") or []:
        tags = item.get("tags") or {}
        kind = _kind(tags)
        if not kind:
            continue
        center = item.get("center") or {}
        lat = center.get("lat", item.get("lat"))
        lon = center.get("lon", item.get("lon"))
        geometry = [(float(point["lat"]), float(point["lon"]))
                    for point in (item.get("geometry") or [])
                    if point.get("lat") is not None and point.get("lon") is not None]
        if lat is None and geometry:
            lat = sum(point[0] for point in geometry) / len(geometry)
            lon = sum(point[1] for point in geometry) / len(geometry)
        if lat is None or lon is None:
            continue
        name = (tags.get("name") or tags.get("name:ru") or tags.get("name:uk")
                or tags.get("operator") or "").strip()
        result.append({"lat": float(lat), "lon": float(lon), "name": name,
                       "kind": kind, "osm": f'{item.get("type")}/{item.get("id")}',
                       "geometry": geometry})
    return result


def nearest(items, lat, lon, km=0.8):
    """Ближайший объект: {name, kind, lat, lon, m} или None.

    Координата берётся по ближайшей вершине причала, а не по центру: у длинного
    пирса центр может отстоять на сотни метров от места швартовки.
    """
    best = None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    for item in items:
        points = item.get("geometry") or [(item.get("lat"), item.get("lon"))]
        for point_lat, point_lon in points:
            try:
                distance = geo.haversine_km(lat, lon, float(point_lat), float(point_lon))
            except (TypeError, ValueError):
                continue
            if distance > km:
                continue
            meters = distance * 1000.0
            if best is None or meters < best["m"]:
                best = dict(item, lat=float(point_lat), lon=float(point_lon),
                            m=meters)
    if best:
        best.pop("geometry", None)
    return best


def describe(place):
    """Строка для карточки: «причал «Измаильский СРЗ» (45.31820, 28.85880), 70 м от стоянки»."""
    if not place:
        return ""
    name = place.get("name") or ""
    head = f'{place["kind"]} «{name}»' if name else place["kind"]
    return (f"{head} ({place['lat']:.5f}, {place['lon']:.5f}), "
            f"{place['m']:.0f} м от стоянки")
