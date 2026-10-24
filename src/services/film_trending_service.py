"""Fetch the public film chart used by the classic homepage."""

import threading
import time
from urllib.parse import urlparse

import requests


CATEGORIES = ("电影", "电视剧", "动漫", "综艺")
_URL = "https://biz.quark.cn/api/trending/ranking/getYingshiRanking"
_CACHE_SECONDS = 12 * 60 * 60
_cache = {}
_lock = threading.Lock()


def _image_url(value):
    url = str(value or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if parsed.scheme == "http" and parsed.netloc:
        url = parsed._replace(scheme="https").geturl()
        parsed = urlparse(url)
    return url if parsed.scheme == "https" and parsed.netloc else ""


def get_film_trending(category):
    """Return chart items and whether an older cached result had to be used."""
    if category not in CATEGORIES:
        raise ValueError("Unsupported film category")

    now = time.time()
    with _lock:
        cached = _cache.get(category)
        if cached and now - cached[0] < _CACHE_SECONDS:
            return cached[1], False

    try:
        response = requests.get(
            _URL,
            params={
                "area": "全部",
                "year": "全部",
                "channel": category,
                "rank_type": "最热",
                "cate": "全部",
                "from": "hot_page",
                "start": 0,
                "hit": 12,
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=6,
        )
        response.raise_for_status()
        raw_items = response.json()["data"]["hits"]["hit"]["item"]
        if not isinstance(raw_items, list):
            raise ValueError("Unexpected chart response")
        items = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            items.append({"title": title, "poster": _image_url(item.get("src"))})
            if len(items) == 12:
                break
        if not items:
            raise ValueError("Empty chart response")
        with _lock:
            _cache[category] = (time.time(), items)
        return items, False
    except (requests.RequestException, ValueError, KeyError, TypeError):
        if cached:
            return cached[1], True
        return [], True
