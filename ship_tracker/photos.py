"""Фото судна: несколько источников с кэшем на диске.

AIS фото не передаёт, поэтому снимок ищется по очереди в открытых карточках
судов: VesselFinder, MyShipTracking, MarineTraffic, ShipSpotting, FleetMon и
Wikimedia Commons. Первый источник, у которого нашлось фото, и выигрывает —
его название показывается под снимком.

Скачанное складываем в data/photos: при повторном выборе судна сайты уже не
дёргаем. Если фото нет нигде, ставим отметку и не ищем его снова несколько дней.
"""

import re
import time
from pathlib import Path
from urllib.parse import quote

import requests
import net

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

VESSELFINDER_DETAILS = "https://www.vesselfinder.com/vessels/details/{key}"
PHOTO_PATTERN = re.compile(r"https://static\.vesselfinder\.net/ship-photo/[^\"'\s]+")
_META = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]*?'
    r'content=["\']([^"\']+)', re.I)
_META_REVERSED = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*?'
    r'(?:property|name)=["\'](?:og:image|twitter:image)', re.I)
_IMG = re.compile(r'https?://[^"\'\s>]+?\.(?:jpe?g|png|webp)(?:\?[^"\'\s>]*)?', re.I)
_JUNK = ("logo", "icon", "avatar", "placeholder", "favicon", "sprite", "flag",
         "banner", "no-photo", "no_photo", "noimage", "default", "blank",
         "spinner", "loader", "badge", "emoji")

# Порядок важен: сверху источники, где фото судна точнее всего.
PROVIDERS = (
    ("vesselfinder", "VesselFinder", "https://www.vesselfinder.com/"),
    ("myshiptracking", "MyShipTracking", "https://www.myshiptracking.com/"),
    ("marinetraffic", "MarineTraffic", "https://www.marinetraffic.com/"),
    ("shipspotting", "ShipSpotting", "https://www.shipspotting.com/"),
    ("fleetmon", "FleetMon", "https://www.fleetmon.com/"),
    ("commons", "Wikimedia Commons", "https://commons.wikimedia.org/"),
)
PROVIDER_TITLES = {key: title for key, title, _ in PROVIDERS}

# Сколько помнить, что у судна нет фото (секунды).
MISSING_TTL = 3 * 24 * 3600
# Меньше этого файл считать битым.
MIN_IMAGE_BYTES = 1000


def photo_file(cache_dir, mmsi) -> Path:
    return Path(cache_dir) / f"{mmsi}.jpg"


def _source_file(cache_dir, mmsi) -> Path:
    return Path(cache_dir) / f"{mmsi}.src"


def _slug(text) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")


def _get(url, timeout):
    """Страница источника или None: сеть и Cloudflare не должны ломать карточку."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=timeout)
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    return response


def _page_images(html: str) -> list:
    """Ссылки на картинки со страницы: og:image и обычные <img>."""
    found = _META.findall(html or "") + _META_REVERSED.findall(html or "")
    found += _IMG.findall(html or "")
    result = []
    for url in found:
        url = url.strip()
        if not url.startswith("http"):
            continue
        if any(word in url.lower() for word in _JUNK):
            continue
        if url not in result:
            result.append(url)
    return result


def _vesselfinder(mmsi, imo, name, timeout):
    for key in [value for value in (imo, mmsi) if value]:
        page = _get(VESSELFINDER_DETAILS.format(key=key), timeout)
        if page is None:
            continue
        found = PHOTO_PATTERN.findall(page.content.decode("utf-8", "replace"))
        if found:
            return found
    return []


def _myshiptracking(mmsi, imo, name, timeout):
    if not name:
        return []
    url = (f"https://www.myshiptracking.com/vessels/{_slug(name)}"
           f"-mmsi-{mmsi}-imo-{imo or 0}")
    page = _get(url, timeout)
    if page is None:
        return []
    # у карточки свои фото в /upload/ и /photos/; логотипы отсеиваются _JUNK
    return [link for link in _page_images(page.text)
            if "/upload/" in link or "/photos/" in link or "ship" in link.lower()]


def _marinetraffic(mmsi, imo, name, timeout):
    """Старый публичный адрес фото: отвечает картинкой, только если она есть."""
    for key, value in (("mmsi", mmsi), ("imo", imo)):
        if not value:
            continue
        url = f"https://photos.marinetraffic.com/ais/showphoto.aspx?{key}={value}"
        try:
            response = requests.get(url, headers=HEADERS, timeout=timeout)
        except requests.RequestException:
            continue
        kind = (response.headers.get("content-type") or "").lower()
        if response.status_code == 200 and kind.startswith("image/"):
            return [response.url]
    return []


def _shipspotting(mmsi, imo, name, timeout):
    query = imo or name
    if not query:
        return []
    page = _get(f"https://www.shipspotting.com/photos/search?query={quote(str(query))}",
                timeout)
    if page is None:
        return []
    return [link for link in _page_images(page.text)
            if "shipspotting" in link.lower() or "cdn" in link.lower()]


def _fleetmon(mmsi, imo, name, timeout):
    query = name or imo
    if not query:
        return []
    page = _get(f"https://www.fleetmon.com/vessels/?q={quote(str(query))}", timeout)
    if page is None:
        return []
    return [link for link in _page_images(page.text)
            if "fleetmon" in link.lower()]


def _commons(mmsi, imo, name, timeout):
    """Свободные фото Wikimedia Commons: единственный источник без блокировок."""
    search = f'"{name}" ship' if name else str(imo or mmsi)
    try:
        response = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={"action": "query", "format": "json", "generator": "search",
                    "gsrsearch": search, "gsrnamespace": 6, "gsrlimit": 10,
                    "prop": "imageinfo", "iiprop": "url", "iiurlwidth": 900},
            headers=HEADERS, timeout=timeout)
        data = response.json()
    except (requests.RequestException, ValueError):
        return []
    tokens = [token for token in re.split(r"[^0-9A-Za-z]+", str(name or "").upper())
              if len(token) >= 2]
    result = []
    for page in ((data.get("query") or {}).get("pages") or {}).values():
        title = str(page.get("title") or "")
        upper = title.upper()
        # чужое фото хуже, чем никакого: имя судна должно быть в названии файла
        if tokens and not any(token in upper for token in tokens):
            continue
        if not tokens and str(imo or "") not in title:
            continue
        info = (page.get("imageinfo") or [{}])[0]
        url = info.get("thumburl") or info.get("url")
        if url and url not in result:
            result.append(url)
    return result


_FETCHERS = {
    "vesselfinder": _vesselfinder,
    "myshiptracking": _myshiptracking,
    "marinetraffic": _marinetraffic,
    "shipspotting": _shipspotting,
    "fleetmon": _fleetmon,
    "commons": _commons,
}


def _looks_like_image(data: bytes) -> bool:
    if not data or len(data) < MIN_IMAGE_BYTES:
        return False
    if data.startswith(b"\xff\xd8\xff"):
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def _download(url, timeout):
    try:
        response = requests.get(url, headers=HEADERS, timeout=timeout)
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    kind = (response.headers.get("content-type") or "").lower()
    if kind and not kind.startswith("image/") and not kind.startswith("application/octet"):
        return None
    return response.content if _looks_like_image(response.content) else None


def _cached_source(cache_dir, mmsi):
    try:
        text = _source_file(cache_dir, mmsi).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def fetch_with_source(mmsi, imo=None, cache_dir=None, timeout=20, name=None):
    """Возвращает (путь к файлу, название источника) или (None, None)."""
    if not mmsi:
        return None, None
    mmsi = str(mmsi)
    cache_dir = Path(cache_dir)
    cached = photo_file(cache_dir, mmsi)
    missing = cache_dir / f"{mmsi}.none"
    try:
        if cached.exists() and cached.stat().st_size >= MIN_IMAGE_BYTES:
            return cached, _cached_source(cache_dir, mmsi)
        if missing.exists() and time.time() - missing.stat().st_mtime < MISSING_TTL:
            return None, None
    except OSError:
        return None, None

    # Общий срок поиска: карточка не должна ждать шесть сайтов по очереди.
    deadline = time.monotonic() + max(10.0, timeout)
    for key, title, _site in PROVIDERS:
        if time.monotonic() >= deadline:
            break
        fetcher = _FETCHERS[key]
        try:
            urls = fetcher(mmsi, imo, name, min(timeout, 10))
        except Exception:
            urls = []
        for url in urls[:3]:
            data = _download(url, timeout)
            if not data:
                continue
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(data)
                _source_file(cache_dir, mmsi).write_text(title, encoding="utf-8")
                if missing.exists():
                    missing.unlink()
            except OSError:
                return None, None
            return cached, title

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"{mmsi}.none").write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass
    return None, None


def fetch(mmsi, imo=None, cache_dir=None, timeout=20, name=None):
    """Возвращает путь к локальному файлу с фото или None.

    Сеть может быть недоступна — тогда просто None, без исключений:
    карточка судна должна открываться и без интернета.
    """
    return fetch_with_source(mmsi, imo, cache_dir, timeout, name)[0]


def check_sources(timeout=12) -> list:
    """Проверка доступности фото-источников: [{key, title, ok, detail}]."""
    result = []
    for key, title, site in PROVIDERS:
        if key == "commons":
            try:
                response = requests.get(
                    "https://commons.wikimedia.org/w/api.php",
                    params={"action": "query", "format": "json", "meta": "siteinfo"},
                    headers=HEADERS, timeout=timeout)
                ok = response.status_code == 200
                detail = "отвечает" if ok else f"HTTP {response.status_code}"
            except requests.RequestException as error:
                ok, detail = False, net.brief_error(error)
        else:
            try:
                response = requests.get(site, headers=HEADERS, timeout=timeout,
                                        allow_redirects=True)
                ok = response.status_code < 400
                if response.status_code in (403, 429):
                    detail = "закрыт для автоматического чтения"
                else:
                    detail = f"HTTP {response.status_code}"
            except requests.RequestException as error:
                ok, detail = False, net.brief_error(error)
        result.append({"key": key, "title": title, "ok": ok, "detail": detail})
    return result
