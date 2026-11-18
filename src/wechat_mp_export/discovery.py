"""Catalog & multi-hop discovery: WeRead chapters, WeChat public albums (合集), and short-link batching."""

from __future__ import annotations

from datetime import datetime
import html
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from wechat_mp_export.auth import WeReadClient, WeReadRateLimitError
from wechat_mp_export.config import get_proxies, read_json, redact_text, write_atomic_json
from wechat_mp_export.http_client import get_data_client
from wechat_mp_export.utils import (
    CN_TZ,
    biz_to_book_id,
    book_id_to_biz,
    clean_wechat_article_url,
    normalize_wechat_short_url,
    short_id_from_review_id,
)


def _check_is_original(item: dict[str, Any], mp_info: dict[str, Any]) -> bool:
    for src in (mp_info, item):
        if not isinstance(src, dict):
            continue
        c_type = (
            src.get("copyright_type")
            or src.get("copyrightType")
            or src.get("copyright")
            or src.get("copyright_stat")
        )
        if c_type is not None:
            try:
                if int(c_type) > 0:
                    return True
            except (ValueError, TypeError):
                if str(c_type).strip().lower() in ("1", "2", "true", "yes", "original"):
                    return True
        orig_flag = src.get("is_original") or src.get("isOriginal") or src.get("original")
        if orig_flag is True or str(orig_flag).strip().lower() in ("1", "true", "yes"):
            return True
    return False


def _resolve_single_review(weread: WeReadClient, review_id: str) -> dict[str, Any]:
    out = {"doc_url": "", "create_time": 0, "title": ""}
    sid = short_id_from_review_id(review_id)
    if sid:
        out["doc_url"] = f"https://mp.weixin.qq.com/s/{sid}"
    try:
        data = weread.get_json(f"https://i.weread.qq.com/review/single?reviewId={review_id}", max_attempts=2)
        rev = data.get("review") or {}
        mp_i = rev.get("mpInfo") or {}
        orig_id = str(mp_i.get("originalId") or "").strip()
        if len(orig_id) >= 15:
            out["doc_url"] = normalize_wechat_short_url(f"https://mp.weixin.qq.com/s/{orig_id}")
        elif mp_i.get("doc_url"):
            out["doc_url"] = normalize_wechat_short_url(str(mp_i["doc_url"]))
        out["create_time"] = int(
            rev.get("createTime")
            or rev.get("updateTime")
            or mp_i.get("time")
            or mp_i.get("create_time")
            or 0
        )
        out["title"] = str(mp_i.get("title") or rev.get("title") or "")
    except Exception:
        pass
    return out


def fetch_weread_catalog(
    weread: WeReadClient,
    book_id: str,
    account_name: str,
    out_dir: Path,
    *,
    max_entries: int = 0,
    page_delay: float = 3.2,
    refresh_catalog: bool = False,
    log: Any = print,
) -> list[dict[str, Any]]:
    """Paginate `/mp/chapters` with checkpointing, optional `max_entries` cap, and rate-limit fallback."""
    checkpoint_path = out_dir / "article_list_checkpoint.json"
    entries_by_key: dict[str, dict[str, Any]] = {}
    ordered_keys: list[str] = []
    start_offset = 0

    if checkpoint_path.exists() and not refresh_catalog:
        saved = read_json(checkpoint_path, {})
        if isinstance(saved, dict) and saved.get("bookId") == book_id:
            cached_articles = saved.get("articles") if isinstance(saved.get("articles"), list) else []
            if saved.get("complete") and cached_articles is not None:
                log(f"Loaded cached WeRead catalog: {len(cached_articles)} articles.")
                return cached_articles[:max_entries] if max_entries > 0 else cached_articles
            for item in cached_articles or []:
                k = item.get("id") or item.get("link") or f"{item.get('title')}::{item.get('update_time')}"
                if k and k not in entries_by_key:
                    entries_by_key[k] = item
                    ordered_keys.append(k)
            start_offset = int(saved.get("next_offset") or 0)
            if max_entries > 0 and len(ordered_keys) >= max_entries:
                log(f"Loaded {max_entries} articles from cached WeRead checkpoint.")
                return [entries_by_key[k] for k in ordered_keys[:max_entries]]
            if ordered_keys:
                log(f"Resuming WeRead catalog from offset={start_offset} ({len(ordered_keys)} cached)...")

    page_size = 20
    offset = start_offset
    empty_streak = 0
    incomplete_stop = False

    while True:
        if offset == 0:
            url = f"https://i.weread.qq.com/mp/chapters?bookId={book_id}&count={page_size}&pf=android&synckey=0"
        else:
            url = f"https://i.weread.qq.com/mp/chapters?bookId={book_id}&count={page_size}&offset={offset}"

        try:
            data = weread.get_json(url, max_attempts=3)
        except WeReadRateLimitError:
            log(
                f"Notice: WeRead catalog hit pagination rate/window limit at offset={offset} "
                f"with {len(ordered_keys)} articles cached; continuing with deep album & link discovery."
            )
            incomplete_stop = True
            break
        except Exception as exc:
            log(
                f"Notice: WeRead catalog stopped at offset={offset} ({redact_text(str(exc))}); "
                f"continuing with {len(ordered_keys)} cached articles."
            )
            incomplete_stop = True
            break

        raw_items = data.get("data") or data.get("chapters") or data.get("list") or []
        if "hasMore" in data:
            has_more = bool(data["hasMore"])
        elif "isCompleted" in data:
            has_more = not bool(data["isCompleted"])
        else:
            has_more = len(raw_items) > 0

        if not raw_items:
            if offset == 0:
                try:
                    cov = weread.get_json(f"https://i.weread.qq.com/mp/cover?bookId={book_id}", max_attempts=2)
                    rid = str(cov.get("reviewId") or "")
                    if rid:
                        info = _resolve_single_review(weread, rid)
                        sid = short_id_from_review_id(rid)
                        doc_u = info["doc_url"] or (f"https://mp.weixin.qq.com/s/{sid}" if sid else "")
                        entries_by_key[rid] = {
                            "id": rid,
                            "title": html.unescape(info["title"] or str(cov.get("title") or "")),
                            "link": doc_u,
                            "cover": str(cov.get("pic") or ""),
                            "digest": "",
                            "author": account_name,
                            "update_time": int(info["create_time"] or cov.get("createTime") or 0),
                            "is_original": False,
                        }
                        ordered_keys.append(rid)
                except Exception:
                    pass
            empty_streak += 1
            if not has_more or empty_streak >= 2:
                break
            offset += page_size
            continue

        empty_streak = 0
        for it in raw_items:
            mp_i = it.get("mpInfo") or it.get("review") or {}
            if isinstance(mp_i, dict) and "mpInfo" in mp_i:
                mp_i = mp_i.get("mpInfo") or {}
            rid = str(it.get("reviewId") or mp_i.get("reviewId") or "")
            sid = short_id_from_review_id(rid)
            doc_u = (
                f"https://mp.weixin.qq.com/s/{sid}"
                if sid
                else normalize_wechat_short_url(str(mp_i.get("doc_url") or mp_i.get("linkUrl") or it.get("url") or ""))
            )
            c_time = int(
                it.get("createTime")
                or it.get("updateTime")
                or it.get("publishTime")
                or mp_i.get("time")
                or mp_i.get("create_time")
                or mp_i.get("publish_time")
                or 0
            )
            title = str(mp_i.get("title") or it.get("title") or "").strip()
            digest = str(mp_i.get("digest") or "").strip()
            author = str(mp_i.get("author") or account_name or "").strip()
            pic = str(mp_i.get("pic_url") or mp_i.get("thumbUrl") or mp_i.get("picUrl") or it.get("cover") or "")
            is_orig = _check_is_original(it, mp_i)

            if not doc_u and rid:
                rev_info = _resolve_single_review(weread, rid)
                doc_u = rev_info["doc_url"]
                c_time = c_time or rev_info["create_time"]
                title = title or rev_info["title"]

            key = rid or doc_u or f"{title}::{c_time}"
            if key and key not in entries_by_key:
                entries_by_key[key] = {
                    "id": rid,
                    "title": html.unescape(title),
                    "link": doc_u,
                    "cover": pic,
                    "digest": html.unescape(digest),
                    "author": html.unescape(author),
                    "update_time": c_time,
                    "is_original": bool(is_orig),
                }
                ordered_keys.append(key)

        offset += len(raw_items)
        snapshot = [entries_by_key[k] for k in ordered_keys]
        write_atomic_json(
            checkpoint_path,
            {
                "bookId": book_id,
                "account_name": account_name,
                "next_offset": offset,
                "complete": not has_more,
                "updated_at": datetime.now(CN_TZ).isoformat(),
                "articles": snapshot,
            },
        )
        log(f"  Catalog progress: {len(snapshot)} articles (offset={offset}, hasMore={has_more})")

        if not has_more:
            break
        if max_entries > 0 and len(ordered_keys) >= max_entries:
            incomplete_stop = True
            break
        time.sleep(max(0.8, page_delay))

    final_list = [entries_by_key[k] for k in ordered_keys]
    write_atomic_json(
        checkpoint_path,
        {
            "bookId": book_id,
            "account_name": account_name,
            "next_offset": offset,
            "complete": not incomplete_stop,
            "updated_at": datetime.now(CN_TZ).isoformat(),
            "articles": final_list,
        },
    )
    return final_list[:max_entries] if max_entries > 0 else final_list


def parse_album_url(album_url: str) -> tuple[str, str]:
    """Extract `(biz, album_id)` from a `mp.weixin.qq.com/mp/appmsgalbum` URL."""
    qs = parse_qs(urlparse(album_url.strip()).query)
    biz = ""
    if qs.get("__biz"):
        biz = book_id_to_biz(biz_to_book_id(qs["__biz"][0]))
    aid = (qs.get("album_id") or [""])[0].strip()
    return biz, aid


def fetch_album_articles(
    biz: str,
    album_id: str,
    *,
    proxy_url: str | None = None,
    page_delay: float = 0.35,
) -> tuple[str, list[dict[str, Any]]]:
    """Fetch all articles in a public WeChat Official Account album (`appmsgalbum`)."""
    if not biz or not album_id:
        return album_id, []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 "
            "Chrome/126.0.0.0 Mobile Safari/537.36 MicroMessenger/8.0.50"
        ),
        "Referer": "https://mp.weixin.qq.com/",
    }
    client = get_data_client()
    proxies = get_proxies(proxy_url)

    begin_msgid = ""
    begin_itemidx = ""
    album_title = ""
    collected: list[dict[str, Any]] = []

    while True:
        url = (
            f"https://mp.weixin.qq.com/mp/appmsgalbum?__biz={biz}"
            f"&action=getalbum&album_id={album_id}&count=20&f=json"
        )
        if begin_msgid:
            url += f"&begin_msgid={begin_msgid}&begin_itemidx={begin_itemidx or '1'}"

        client.cookies.clear()
        resp = client.get(url, headers=headers, proxies=proxies, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        album_resp = payload.get("getalbum_resp") or {}
        base_info = album_resp.get("base_info") or {}
        album_title = html.unescape(str(base_info.get("title") or album_title or album_id)).strip()

        items = album_resp.get("article_list") or []
        if isinstance(items, dict):
            items = [items]
        if not items:
            break

        for it in items:
            art_url = clean_wechat_article_url(str(it.get("url") or ""))
            if not art_url:
                continue
            collected.append({
                "url": art_url,
                "title": html.unescape(str(it.get("title") or "")).strip(),
                "create_time": int(it.get("create_time") or 0),
                "cover": str(it.get("cover_img_1_1") or ""),
                "msgid": str(it.get("msgid") or ""),
                "itemidx": str(it.get("itemidx") or "1"),
                "album": album_title,
            })

        cont = str(album_resp.get("continue_flag") or "0")
        last = items[-1]
        next_msgid = str(last.get("msgid") or "")
        next_idx = str(last.get("itemidx") or "1")
        if cont != "1" or not next_msgid or (next_msgid == begin_msgid and next_idx == begin_itemidx):
            break
        begin_msgid, begin_itemidx = next_msgid, next_idx
        time.sleep(max(0.2, page_delay))

    return album_title, collected


def batch_resolve_short_urls(
    weread: WeReadClient | None,
    urls: list[str],
) -> dict[str, tuple[str, str]]:
    """Batch-convert long `?__biz=...` URLs into captcha-free short `/s/<short_id>` URLs via WeRead."""
    out: dict[str, tuple[str, str]] = {}
    if not weread or not weread.has_valid_session():
        return out
    long_urls: list[str] = []
    seen: set[str] = set()
    for u in urls:
        cu = clean_wechat_article_url(u)
        if "__biz=" in cu and cu not in seen:
            seen.add(cu)
            long_urls.append(cu)
    if not long_urls:
        return out

    for i in range(0, len(long_urls), 20):
        chunk = long_urls[i:i + 20]
        try:
            res = weread.post_json("https://i.weread.qq.com/mp/getreviewid", {"urls": chunk}, max_attempts=2)
            for item in res.get("reviewIds") or []:
                orig = clean_wechat_article_url(str(item.get("url") or ""))
                rid = str(item.get("reviewId") or "")
                sid = short_id_from_review_id(rid)
                if orig and sid:
                    out[orig] = (f"https://mp.weixin.qq.com/s/{sid}", rid)
        except Exception:
            break
    return out
