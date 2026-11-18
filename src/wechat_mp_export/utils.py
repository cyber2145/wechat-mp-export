"""Pure helper functions for WeChat URLs, Official Account IDs, slugs, and hashing."""

from __future__ import annotations

import base64
import hashlib
import html
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, unquote, urlparse, urlsplit, urlunparse, urlunsplit

CN_TZ = timezone(timedelta(hours=8))

_REVIEW_ID_RE = re.compile(r"^MP_WXS_[0-9]+_(?P<short_id>[A-Za-z0-9_~-]{15,})$")
_SHORT_ID_RE = re.compile(r"^[A-Za-z0-9_~-]{15,}$")
_DROP_QUERY_KEYS = frozenset({
    "scene",
    "subscene",
    "sessionid",
    "chksm",
    "sharer_shareid",
    "sharer_sharetime",
    "key",
    "pass_ticket",
    "uin",
    "devicetype",
    "version",
    "x5",
    "src",
    "asc",
    "clicktime",
    "enterid",
    "rd2exitd",
    "item_show_type",
    "exportkey",
    "poc_token",
})
_WIN_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})


def biz_to_book_id(biz_or_book_id: str) -> str:
    """Convert WeChat Base64 `__biz` (or numeric UIN) to WeRead `MP_WXS_{uin}`."""
    if not biz_or_book_id:
        return ""
    val = str(biz_or_book_id).strip()
    if val.startswith("MP_WXS_"):
        uin = val[len("MP_WXS_"):]
        if uin.isdigit():
            return val
    if val.isdigit():
        return f"MP_WXS_{val}"

    raw = unquote(val).strip()
    rem = len(raw) % 4
    if rem:
        raw += "=" * (4 - rem)
    try:
        decoded = base64.b64decode(raw).decode("utf-8", errors="ignore").strip()
        if decoded.isdigit():
            return f"MP_WXS_{decoded}"
    except Exception:
        pass
    return val if val.startswith("MP_") else ""


def book_id_to_biz(book_id_or_biz: str) -> str:
    """Convert WeRead `MP_WXS_{uin}` to canonical Base64 `__biz`."""
    if not book_id_or_biz:
        return ""
    val = str(book_id_or_biz).strip()
    if val.startswith("MP_WXS_"):
        uin = val[len("MP_WXS_"):]
        if uin.isdigit():
            return base64.b64encode(uin.encode("ascii")).decode("ascii")
        return ""
    bid = biz_to_book_id(val)
    if bid.startswith("MP_WXS_"):
        return book_id_to_biz(bid)
    return ""


def same_account_id(a: str, b: str) -> bool:
    """Check whether two `__biz` or `MP_WXS_` strings refer to the same Official Account."""
    if not a or not b:
        return False
    ba = biz_to_book_id(a)
    bb = biz_to_book_id(b)
    if ba and bb:
        return ba == bb
    return a.strip() == b.strip()


def short_id_from_review_id(review_id: str) -> str:
    """Extract canonical WeChat article short_id from WeRead reviewId."""
    if not isinstance(review_id, str):
        return ""
    match = _REVIEW_ID_RE.fullmatch(review_id.strip())
    if not match:
        return ""
    return match.group("short_id").replace("~", "_")


def normalize_wechat_short_url(url: str) -> str:
    """Restore underscores (`~` -> `_`) on `mp.weixin.qq.com/s/<short_id>` URLs."""
    if not isinstance(url, str) or not url:
        return ""
    try:
        parsed = urlsplit(url.strip())
        if parsed.scheme.lower() not in {"http", "https"}:
            return url
        if parsed.hostname != "mp.weixin.qq.com" or parsed.username or parsed.password:
            return url
        if parsed.port not in {None, 80, 443}:
            return url
        if not parsed.path.startswith("/s/"):
            return url

        short_id = parsed.path[3:].strip("/")
        if "/" in short_id or not _SHORT_ID_RE.fullmatch(short_id):
            return url
        normalized_id = short_id.replace("~", "_")
        return urlunsplit(("https", "mp.weixin.qq.com", f"/s/{normalized_id}", parsed.query, ""))
    except (TypeError, ValueError):
        return url


def clean_wechat_article_url(url: str) -> str:
    """Normalize a WeChat article URL and strip tracking/risk-control query parameters."""
    if not url:
        return ""
    raw = (
        html.unescape(str(url))
        .replace("\\x26amp;", "&")
        .replace("\\x26", "&")
        .replace("\\u0026amp;", "&")
        .replace("\\u0026", "&")
        .strip()
    )
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("http://mp.weixin.qq.com"):
        raw = "https://" + raw[len("http://"):]

    raw = normalize_wechat_short_url(raw)
    try:
        parsed = urlparse(raw)
        if parsed.hostname != "mp.weixin.qq.com":
            return raw
        query = parse_qs(parsed.query, keep_blank_values=False)
        clean_q = {k: query[k] for k in sorted(query.keys()) if k not in _DROP_QUERY_KEYS}
        fragment = "" if parsed.fragment == "wechat_redirect" else ""
        return urlunparse((
            "https",
            "mp.weixin.qq.com",
            parsed.path,
            "",
            urlencode(clean_q, doseq=True, safe="=:/%"),
            fragment,
        ))
    except Exception:
        return raw


def extract_short_id(url: str) -> str:
    """Extract `<short_id>` from `https://mp.weixin.qq.com/s/<short_id>` (non-__biz URLs)."""
    if not url:
        return ""
    norm = clean_wechat_article_url(url)
    parsed = urlparse(norm)
    if parsed.hostname != "mp.weixin.qq.com":
        return ""
    if "__biz=" in parsed.query:
        return ""
    if parsed.path.startswith("/s/"):
        sid = parsed.path[3:].strip("/")
        if len(sid) >= 10 and "/" not in sid:
            return sid.replace("~", "_")
    return ""


def extract_biz_from_url(url: str) -> str:
    """Extract canonical Base64 `__biz` from a WeChat URL if present."""
    if not url:
        return ""
    try:
        qs = parse_qs(urlparse(clean_wechat_article_url(url)).query)
        if qs.get("__biz"):
            return book_id_to_biz(biz_to_book_id(qs["__biz"][0]))
    except Exception:
        pass
    return ""


def parse_mid_idx_from_url(url: str) -> tuple[int, int] | None:
    """Extract `(mid, idx)` message tuple from a long WeChat article URL."""
    if not url:
        return None
    try:
        qs = parse_qs(urlparse(clean_wechat_article_url(url)).query)
        mid_vals = qs.get("mid") or qs.get("appmsgid")
        if mid_vals:
            mid = int(mid_vals[0])
            idx_vals = qs.get("idx") or qs.get("itemidx") or ["1"]
            idx = int(idx_vals[0])
            return (mid, idx)
    except (ValueError, TypeError, IndexError):
        return None
    return None


def parse_mid_idx_from_html(raw_html: str) -> tuple[int, int] | None:
    """Extract `(mid, idx)` message tuple from WeChat article HTML/JS variables."""
    if not raw_html:
        return None
    m_mid = (
        re.search(r'\bvar\s+mid\s*=\s*(?:""\s*\|\|\s*)?["\'](\d{6,})["\']', raw_html)
        or re.search(r'\bappmsgid["\']?\s*[:=]\s*["\']?(\d{6,})', raw_html)
        or re.search(r'[?&]mid=(\d{8,14})', raw_html)
    )
    if not m_mid:
        return None
    m_idx = (
        re.search(r'\bvar\s+idx\s*=\s*(?:""\s*\|\|\s*)?["\'](\d+)["\']', raw_html)
        or re.search(r'\bitemidx["\']?\s*[:=]\s*["\']?(\d+)', raw_html)
        or re.search(r'[?&]idx=(\d+)', raw_html)
    )
    try:
        return (int(m_mid.group(1)), int(m_idx.group(1)) if m_idx else 1)
    except ValueError:
        return None


def extract_biz_from_html(raw_html: str) -> str:
    """Extract validated Official Account Base64 `__biz` from WeChat article HTML."""
    if not raw_html:
        return ""
    patterns = (
        r'\bvar\s+biz\s*=\s*(?:""\s*\|\|\s*|\'\'\s*\|\|\s*)?["\']([A-Za-z0-9+/=]{10,24})["\']',
        r'\bwindow\.__biz\s*=\s*["\']([A-Za-z0-9+/=]{10,24})["\']',
        r'\bbiz\s*:\s*["\']([A-Za-z0-9+/=]{10,24})["\']',
        r'__biz=([A-Za-z0-9+/=%]{10,30})',
    )
    for pat in patterns:
        for m in re.finditer(pat, raw_html):
            cand = book_id_to_biz(biz_to_book_id(m.group(1)))
            if cand:
                return cand
    return ""


def _decode_js_str(val: str) -> str:
    if not val:
        return ""
    out = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), val)
    out = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), out)
    out = out.replace("\\'", "'").replace('\\"', '"').replace("\\n", " ").strip()
    return html.unescape(out).strip()


def extract_account_meta_from_html(raw_html: str) -> dict[str, str]:
    """Extract Official Account metadata (`biz`, `bookId`, `nickname`, `wechat_id`, `intro`) from article HTML."""
    biz = extract_biz_from_html(raw_html)
    book_id = biz_to_book_id(biz) if biz else ""

    nickname = ""
    for pat in (
        r'<strong[^>]*class="[^"]*profile_nickname[^"]*"[^>]*>([^<]+)</strong>',
        r'<a[^>]*id="js_name"[^>]*>([\s\S]*?)</a>',
        r'\bvar\s+nickname\s*=\s*htmlDecode\(["\']([^"\']+)["\']\)',
        r'\bnick_name\s*:\s*["\']([^"\']+)["\']',
    ):
        m = re.search(pat, raw_html, re.I)
        if m:
            cand = _decode_js_str(re.sub(r"<[^>]+>", "", m.group(1)))
            if cand and cand not in ("微信公众平台", "WeChat"):
                nickname = cand
                break

    wechat_id = ""
    m_wxid = re.search(
        r'微信号</span>\s*<span[^>]*class="[^"]*profile_meta_value[^"]*"[^>]*>([^<]*)</span>',
        raw_html,
        re.I,
    )
    if m_wxid:
        wechat_id = html.unescape(m_wxid.group(1)).strip()
    if not wechat_id:
        m_un = re.search(r'\bvar\s+user_name\s*=\s*["\']([^"\']+)["\']', raw_html)
        if m_un:
            wechat_id = m_un.group(1).strip()

    intro = ""
    m_intro = re.search(
        r'功能介绍</span>\s*<span[^>]*class="[^"]*profile_meta_value[^"]*"[^>]*>([\s\S]*?)</span>',
        raw_html,
        re.I,
    )
    if m_intro:
        intro = html.unescape(re.sub(r"<[^>]+>", "", m_intro.group(1))).strip()

    return {
        "biz": biz,
        "bookId": book_id,
        "nickname": nickname,
        "wechat_id": wechat_id,
        "intro": intro,
    }


def sanitize_filename(name: str, max_len: int = 48) -> str:
    """Produce a safe cross-platform directory/file name stem."""
    cleaned = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    if not cleaned:
        cleaned = "article"
    cleaned = cleaned[:max_len].rstrip(" ._") or "article"
    if cleaned.upper() in _WIN_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def make_article_slug(
    dt: datetime,
    review_id: str = "",
    url: str = "",
    mid_idx: tuple[int, int] | None = None,
    prefix: str = "wx",
) -> str:
    """Create a deterministic, lowercase ASCII slug suitable for static sites & CMSes."""
    sid = short_id_from_review_id(review_id) or extract_short_id(url)
    token = re.sub(r"[^a-zA-Z0-9]", "", sid)[:12].lower()
    if not token and mid_idx:
        token = f"m{mid_idx[0]}i{mid_idx[1]}"
    if not token:
        seed = review_id or clean_wechat_article_url(url) or dt.isoformat()
        token = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    clean_prefix = re.sub(r"[^a-z0-9-]", "", (prefix or "wx").lower()).strip("-") or "wx"
    return f"{clean_prefix}-{dt.strftime('%Y%m%d')}-{token}"


def sha256_file(path: Path) -> str:
    """Compute hex SHA-256 digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
