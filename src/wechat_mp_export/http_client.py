"""Dual isolated HTTP sessions (Data vs Cookie-less CDN), magic-byte detection, and throttling."""

from __future__ import annotations

import math
import os
import random
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from wechat_mp_export.config import get_proxies

FILE_SIGNATURES: tuple[tuple[int, bytes, str], ...] = (
    (0, b"\xff\xd8\xff", "jpg"),
    (0, b"\x89PNG\r\n\x1a\n", "png"),
    (0, b"GIF87a", "gif"),
    (0, b"GIF89a", "gif"),
    (8, b"WEBP", "webp"),
    (4, b"ftypavif", "avif"),
    (4, b"ftypavis", "avif"),
    (4, b"ftypheic", "heic"),
    (4, b"ftypheix", "heic"),
    (4, b"ftypmif1", "heic"),
    (4, b"ftypisom", "mp4"),
    (4, b"ftypmp42", "mp4"),
    (4, b"ftypMSNV", "mp4"),
    (4, b"ftypM4V", "m4v"),
    (4, b"ftypM4A", "m4a"),
    (4, b"ftypqt  ", "mov"),
    (0, b"\x1a\x45\xdf\xa3", "webm"),
    (0, b"ID3", "mp3"),
    (0, b"\xff\xfb", "mp3"),
    (0, b"\xff\xf3", "mp3"),
    (0, b"OggS", "ogg"),
    (8, b"WAVE", "wav"),
    (0, b"\x00\x00\x01\xba", "mpg"),
    (0, b"FLV\x01", "flv"),
)

SIGNATURE_WINDOW: int = 512

_EXT_EQUIV = {
    "jpeg": "jpg",
    "jpe": "jpg",
    "jfif": "jpg",
    "tif": "tiff",
    "heif": "heic",
}

CDN_HOSTS: tuple[str, ...] = (
    "mmbiz.qpic.cn",
    "mmbiz.qlogo.cn",
    "mpvideo.qpic.cn",
    "res.wx.qq.com",
    "wx.qlogo.cn",
)

_CDN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

_WECHAT_VERSIONS: tuple[tuple[str, str, str], ...] = (
    ("8.0.48.2580", "122.0.6261.119", "XWEB/1220133 MMWEBSDK/20240404"),
    ("8.0.49.2600", "126.0.6478.122", "XWEB/1260079 MMWEBSDK/20240501"),
    ("8.0.50.2620", "126.0.6478.182", "XWEB/1260121 MMWEBSDK/20240601"),
)

_local = threading.local()


def normalize_ext(ext: str) -> str:
    ext = (ext or "").lower().lstrip(".")
    return _EXT_EQUIV.get(ext, ext)


def detect_suffix(head: bytes) -> str | None:
    """Detect file extension from leading magic bytes."""
    if not head:
        return None
    for offset, signature, suffix in FILE_SIGNATURES:
        if len(head) >= offset + len(signature) and head[offset:offset + len(signature)] == signature:
            return suffix
    sample = head[:512].lstrip()
    if sample.startswith((b"<svg", b"<?xml")) and b"<svg" in head[:512].lower():
        return "svg"
    return None


def resolve_suffix_path(path: Path, head: bytes) -> Path:
    real = detect_suffix(head)
    if not real:
        return path
    if normalize_ext(real) == normalize_ext(path.suffix):
        return path
    return path.with_suffix("." + real)


def guess_url_ext(url: str, default: str = "jpg") -> str:
    qs = parse_qs(urlparse(url).query)
    if qs.get("wx_fmt"):
        fmt = qs["wx_fmt"][0].strip().lower()[:8]
        if fmt.isalnum():
            return _EXT_EQUIV.get(fmt, fmt)
    fn = urlparse(url).path.rsplit("/", 1)[-1].split("?")[0]
    if "." in fn:
        ext = fn.rsplit(".", 1)[-1].lower()
        if 1 <= len(ext) <= 5 and ext.isalnum():
            return _EXT_EQUIV.get(ext, ext)
    return default


def guess_video_ext(url: str) -> str:
    path = urlparse(url).path
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return ext if ext in {"mp4", "m3u8", "mov", "webm"} else "mp4"


def get_wait_time(
    avg_delay: float = 0.8,
    sigma: float = 0.45,
    min_delay: float | None = None,
    max_delay: float | None = None,
) -> float:
    avg = max(0.05, float(avg_delay))
    mu = math.log(avg) - (sigma ** 2) / 2
    delay = random.lognormvariate(mu, sigma)
    lo = avg * 0.35 if min_delay is None else max(0.0, float(min_delay))
    hi = avg * 3.5 if max_delay is None else max(lo, float(max_delay))
    return max(lo, min(hi, delay))


def sleep_throttled(
    avg_delay: float = 0.8,
    sigma: float = 0.45,
    min_delay: float | None = None,
    max_delay: float | None = None,
) -> float:
    delay = get_wait_time(avg_delay, sigma, min_delay, max_delay)
    time.sleep(delay)
    return delay


def _build_cdn_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.clear()
    session.headers.update({"User-Agent": _CDN_UA})
    session.cookies.clear()
    retry = Retry(
        total=3,
        connect=2,
        read=2,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=12, pool_maxsize=20)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _build_data_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    adapter = HTTPAdapter(pool_connections=6, pool_maxsize=12)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_cdn_client() -> requests.Session:
    """Thread-local Cookie-less, Referer-less CDN HTTP session."""
    sess = getattr(_local, "cdn", None)
    if sess is None:
        sess = _build_cdn_session()
        _local.cdn = sess
    return sess


def get_data_client() -> requests.Session:
    """Thread-local HTTP session for WeChat MP / WeRead API calls."""
    sess = getattr(_local, "data", None)
    if sess is None:
        sess = _build_data_session()
        _local.data = sess
    return sess


def get_wechat_mobile_headers() -> dict[str, str]:
    """Build mobile WeChat XWEB request headers for public article pages."""
    ver, chrome_ver, mmweb = random.choice(_WECHAT_VERSIONS)
    ua = (
        f"Mozilla/5.0 (Linux; Android 14; Mobile; SM-S9280 Build/UP1A.231005.007; wv) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/{chrome_ver} Mobile Safari/537.36 "
        f"{mmweb} MMWEBID/1000 MicroMessenger/{ver} NetType/WIFI Language/zh_CN ABI/arm64"
    )
    return {
        "User-Agent": ua,
        "X-Requested-With": "com.tencent.mm",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://mp.weixin.qq.com/",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Upgrade-Insecure-Requests": "1",
    }


def check_wechat_risk_control(html_text: str, final_url: str = "") -> tuple[bool, str]:
    """Detect WeChat risk-control / captcha / rate-limit interstitials."""
    if not html_text:
        return True, "empty_response"
    if "wappoc_appmsgcaptcha" in (final_url or "") or "wappoc_appmsgcaptcha" in html_text:
        return True, "wechat_captcha (wappoc_appmsgcaptcha)"
    if "当前环境异常，完成验证后即可继续访问" in html_text:
        return True, "environment_verification_required"
    if "操作频繁，请稍后再试" in html_text:
        return True, "rate_limited"
    if "未知错误，请稍后再试" in html_text and 'id="js_content"' not in html_text:
        return True, "temporary_unknown_error"
    return False, ""


def download_cdn_asset(
    url: str,
    dest_path: Path,
    proxy_url: str | None = None,
    timeout: int = 60,
) -> str | None:
    """Download a CDN media resource into `dest_path.parent`, returning the saved filename."""
    if not url:
        return None
    part = dest_path.with_name(f".{dest_path.name}.part.{threading.get_ident()}")
    proxies = get_proxies(proxy_url)
    try:
        client = get_cdn_client()
        client.cookies.clear()
        resp = client.get(url, proxies=proxies, timeout=timeout, stream=True)
        resp.raise_for_status()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        head = b""
        with open(part, "wb") as fh:
            for chunk in resp.iter_content(65536):
                if not chunk:
                    continue
                if len(head) < SIGNATURE_WINDOW:
                    head += chunk[: SIGNATURE_WINDOW - len(head)]
                fh.write(chunk)
        if not head:
            raise RuntimeError("empty asset body")
        final_path = resolve_suffix_path(dest_path, head)
        os.replace(part, final_path)
        return final_path.name
    except Exception:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
        return None
