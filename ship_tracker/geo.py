"""География: геозоны портов, расстояния и проекция координат на экран радара."""

import math

# Порты наблюдения: Дунай и северо-западное Причерноморье (включая Южный).
# radius_km — радиус геозоны вокруг точки: вход в неё = судно в порту.
DEFAULT_PORTS = [
    {"name": "RENI", "label": "Рени", "lat": 45.4528, "lon": 28.2822, "radius_km": 5.0},
    {"name": "IZMAIL", "label": "Измаил", "lat": 45.3517, "lon": 28.8367, "radius_km": 5.0},
    {"name": "VYLKOVE", "label": "Вилково", "lat": 45.3975, "lon": 29.5853, "radius_km": 4.0},
    {"name": "CHORNOMORSK", "label": "Черноморск", "lat": 46.3000, "lon": 30.6667, "radius_km": 8.0},
    {"name": "ODESSA", "label": "Одесса", "lat": 46.4825, "lon": 30.7233, "radius_km": 8.0},
    {"name": "YUZHNY", "label": "Южный", "lat": 46.6233, "lon": 31.0167, "radius_km": 8.0},
]

# Район подписки по умолчанию: [[юг/запад], [север/восток]] в формате AISStream.
# Внутренний Дунай (Измаил — Вилково) и северо-западное Причерноморье
# наземными приёмниками AIS почти не покрыты: по узкому прямоугольнику вдоль реки
# не приходит ни одного сообщения. Поэтому район — вся доступная акватория
# Чёрного моря (Болгария, Румыния, Турция, Кавказ), а свои порты и геозоны
# остаются как есть: 7° x 16° = 112 кв. градуса, это в пределах лимита
# персонального токена Open Waters (400 кв. градусов).
DEFAULT_BOUNDING_BOXES = [[[40.5, 26.0], [47.5, 42.0]]]

# Старые узкие районы подписки: только Дунай без моря. Такие настройки
# при загрузке заменяются на DEFAULT_BOUNDING_BOXES, иначе живого потока не будет.
LEGACY_BOUNDING_BOXES = (
    [[[44.05, 27.55], [45.95, 29.35]]],
    [[[44.05, 27.55], [46.85, 31.25]]],
)

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Расстояние между двумя точками в километрах."""
    if None in (lat1, lon1, lat2, lon2):
        return float("inf")
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def port_at(lat, lon, ports):
    """Порт, в геозону которого попадает точка (ближайший, если их несколько)."""
    found = None
    for port in ports or []:
        try:
            distance = haversine_km(lat, lon, float(port["lat"]), float(port["lon"]))
            radius = float(port.get("radius_km") or 5.0)
        except (KeyError, TypeError, ValueError):
            continue
        if distance <= radius and (found is None or distance < found[1]):
            found = (port, distance)
    return found[0] if found else None


# Как порты пишутся в поле Destination у судов: коды UN/LOCODE и устоявшиеся
# написания. Нужно, чтобы судно, идущее в наш порт, не считалось чужим.
PORT_ALIASES = {
    "RENI": ("UDREN", "RENY"),
    "IZMAIL": ("UDIZM", "IZMAIL."),
    "VYLKOVE": ("UDVYL", "VILKOVO"),
    "CHORNOMORSK": ("CHORNOMORSK", "ILLICHIVSK", "ILICHEVSK"),
    "ODESSA": ("UDODS", "ODESA"),
    "YUZHNY": ("UAYUZ", "PIVDENNYI", "PIVDENNY", "YUZHNYY", "YUZHNYI"),
}


def port_aliases(ports) -> dict:
    """Название порта → все варианты написания в поле Destination."""
    result = {}
    for port in ports or []:
        name = str(port.get("name") or "").strip().upper()
        if not name:
            continue
        variants = {name, *PORT_ALIASES.get(name, ())}
        for extra in port.get("aliases") or []:
            extra = str(extra).strip().upper()
            if extra:
                variants.add(extra)
        result[name] = tuple(sorted(variants))
    return result


def port_by_name(name, ports):
    name = (name or "").strip().upper()
    for port in ports or []:
        if (port.get("name") or "").strip().upper() == name:
            return port
    return None


def in_bbox(lat, lon, boxes) -> bool:
    """Лежит ли точка хотя бы в одном прямоугольнике [[lat, lon], [lat, lon]]."""
    for box in boxes or []:
        try:
            (lat1, lon1), (lat2, lon2) = box
        except (ValueError, TypeError):
            continue
        if min(lat1, lat2) <= lat <= max(lat1, lat2) and min(lon1, lon2) <= lon <= max(lon1, lon2):
            return True
    return False


class Projector:
    """Переводит широту/долготу в пиксели холста и обратно.

    Проекция равнопромежуточная: для района Днестра и Дуная на таких масштабах
    искажение незаметно, зато координаты остаются простыми и без вычислений сферы.
    """

    def __init__(self, width, height, center_lat, center_lon, span_lon):
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.center_lat = float(center_lat)
        self.center_lon = float(center_lon)
        self.span_lon = max(0.02, float(span_lon))
        self.cos_lat = max(0.05, math.cos(math.radians(self.center_lat)))
        # одинаковый масштаб по обеим осям: пикселей на километр
        self.px_per_km = self.width / (self.span_lon * 111.32 * self.cos_lat)

    def to_screen(self, lat, lon):
        x = (float(lon) - self.center_lon) * 111.32 * self.cos_lat * self.px_per_km + self.width / 2
        y = (self.center_lat - float(lat)) * 111.32 * self.px_per_km + self.height / 2
        return x, y

    def to_geo(self, x, y):
        lon = (float(x) - self.width / 2) / self.px_per_km / (111.32 * self.cos_lat) + self.center_lon
        lat = self.center_lat - (float(y) - self.height / 2) / self.px_per_km / 111.32
        return lat, lon

    def km_to_px(self, km: float) -> float:
        return float(km) * self.px_per_km
