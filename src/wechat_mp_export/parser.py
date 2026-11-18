"""Parse WeChat Official Account HTML (#js_content, gallery posts, audio, video, links, and metadata)."""

from __future__ import annotations

from dataclasses import dataclass, field
import html
import re
from typing import Any
from urllib.parse import quote, unquote, urlparse

from bs4 import BeautifulSoup

from wechat_mp_export.utils import (
    clean_wechat_article_url,
    extract_account_meta_from_html,
    extract_biz_from_url,
    parse_mid_idx_from_html,
    parse_mid_idx_from_url,
    same_account_id,
)

_MPVIDEO_RE = re.compile(r'(?:https?:)?//mpvideo\.qpic\.cn/[^"\'\s<>]+?\.mp4(?:\?[^"\'\s<>]+)?', re.I)

_PERMANENT_UNAVAILABLE_KEYWORDS: tuple[str, ...] = (
    "内容已被作者删除",
    "已被作者删除",
    "该内容暂时无法查看",
    "此内容因违规无法查看",
    "此内容无法查看",
    "链接已过期",
)


@dataclass
class ParsedArticle:
    status: str  # "ok" | "permanent_unavailable" | "parse_failed"
    error: str = ""
    share_redirect_url: str = ""
    title: str = ""
    author: str = ""
    digest: str = ""
    cover_url: str = ""
    publish_time: int = 0
    ip_location: str = ""
    is_original: bool = False
    biz: str = ""
    account_name: str = ""
    wechat_id: str = ""
    mid_idx: tuple[int, int] | None = None
    albums: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    content_html: str = ""
    image_urls: list[str] = field(default_factory=list)
    voice_items: list[dict[str, str]] = field(default_factory=list)
    video_items: list[dict[str, str]] = field(default_factory=list)
    discovered_album_ids: list[str] = field(default_factory=list)
    discovered_article_urls: list[str] = field(default_factory=list)


def normalize_asset_url(url: str) -> str:
    if not url:
        return ""
    cleaned = (
        html.unescape(url)
        .replace("\\x26amp;", "&")
        .replace("\\x26", "&")
        .replace("\\u0026amp;", "&")
        .replace("\\u0026", "&")
        .replace("&amp;", "&")
        .strip()
    )
    if cleaned.startswith("//"):
        return "https:" + cleaned
    if cleaned.startswith("://"):
        return "https" + cleaned
    return cleaned


def extract_share_redirect_url(raw_html: str) -> str:
    """Return target URL if page is a WeChat `js_share_source` share wrapper card."""
    if not raw_html or 'id="js_content"' in raw_html:
        return ""
    m = (
        re.search(r'data-url="([^"]+)"[^>]*id="js_share_source"', raw_html)
        or re.search(r'id="js_share_source"[^>]*data-url="([^"]+)"', raw_html)
    )
    if m:
        return clean_wechat_article_url(html.unescape(m.group(1)))
    return ""


def try_extract_gallery_html(raw_html: str) -> str | None:
    """Reconstruct HTML for WeChat photo-gallery ('小绿书') posts (`picture_page_info_list`)."""
    pos = raw_html.find("picture_page_info_list")
    if pos < 0:
        return None

    tail = raw_html[pos:]
    brackets = 0
    started = False
    chars: list[str] = []
    for ch in tail:
        if ch == "[":
            brackets += 1
            started = True
        elif ch == "]":
            brackets -= 1
            if started and brackets == 0:
                chars.append(ch)
                break
        if started:
            chars.append(ch)

    list_str = "".join(chars)
    if not list_str:
        return None

    blocks: list[str] = []
    depth = 0
    buf: list[str] = []
    in_obj = False
    for ch in list_str:
        if ch == "{":
            if depth == 0:
                in_obj = True
            depth += 1
        if in_obj:
            buf.append(ch)
        if ch == "}":
            depth -= 1
            if depth == 0 and in_obj:
                blocks.append("".join(buf))
                buf = []
                in_obj = False

    img_urls: list[str] = []
    for blk in blocks:
        m = re.search(r'cdn_url\s*:\s*["\']([^"\']+)["\']', blk)
        if m:
            u = re.sub(r"\\x([0-9a-fA-F]{2})", lambda x: chr(int(x.group(1), 16)), m.group(1))
            u = normalize_asset_url(u)
            if u and u not in img_urls:
                img_urls.append(u)

    desc_match = (
        re.search(r'window\.desc\s*=\s*"((?:[^"\\]|\\.)*)"', raw_html, re.DOTALL)
        or re.search(r"window\.desc\s*=\s*'((?:[^'\\]|\\.)*)'", raw_html, re.DOTALL)
    )
    desc_text = ""
    if desc_match:
        raw_desc = desc_match.group(1)
        desc_text = re.sub(r"\\x([0-9a-fA-F]{2})", lambda x: chr(int(x.group(1), 16)), raw_desc)
        desc_text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda x: chr(int(x.group(1), 16)), desc_text)
        for esc, rep in (("\\n", "\n"), ("\\t", "\t"), ("\\r", "\r"), ('\\"', '"'), ("\\'", "'"), ("\\\\", "\\")):
            desc_text = desc_text.replace(esc, rep)

    if not img_urls and not desc_text:
        return None

    parts = ['<div class="wechat-gallery" style="margin-bottom:20px;">']
    for u in img_urls:
        esc_u = html.escape(u, quote=True)
        parts.append(
            f'  <img src="{esc_u}" style="max-width:100%;height:auto;display:block;margin:12px auto;border-radius:6px;" />'
        )
    parts.append("</div>")
    if desc_text:
        parts.append('<div class="wechat-gallery-desc">')
        for para in (p.strip() for p in desc_text.split("\n") if p.strip()):
            parts.append(f"  <p>{html.escape(para)}</p>")
        parts.append("</div>")
    return "\n".join(parts)


def extract_body_html(raw_html: str) -> str:
    """Extract `#js_content` HTML or fall back to photo-gallery reconstruction."""
    marker = 'id="js_content"'
    pos = raw_html.find(marker)
    if pos >= 0:
        soup = BeautifulSoup(raw_html, "html.parser")
        node = soup.find(id="js_content")
        if node is not None:
            inner = node.decode_contents().strip()
            if inner:
                return inner

        start_end = raw_html.find(">", pos)
        if start_end > 0:
            end_candidates = [
                raw_html.find('id="js_tags_preview_toast"', start_end),
                raw_html.find('id="js_article_bottom_bar"', start_end),
                raw_html.find('id="js_pc_qr_code"', start_end),
            ]
            valid_ends = [p for p in end_candidates if p > start_end]
            end = min(valid_ends) if valid_ends else len(raw_html)
            chunk = raw_html[start_end + 1:end]
            last_div = chunk.rfind("</div>")
            if last_div >= 0:
                chunk = chunk[:last_div]
            if chunk.strip():
                return chunk

    gallery = try_extract_gallery_html(raw_html)
    return gallery or ""


def extract_voice_items(raw_html: str) -> list[dict[str, str]]:
    """Extract embedded WeChat `<mpvoice>` audio items."""
    voices: list[dict[str, str]] = []
    seen: set[str] = set()
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup.find_all("mpvoice"):
        fid = str(tag.get("voice_encode_fileid") or "").strip()
        if not fid or fid in seen:
            continue
        seen.add(fid)
        voices.append({
            "fileid": fid,
            "name": html.unescape(str(tag.get("name") or "Audio").strip()),
            "play_length": str(tag.get("play_length") or "").strip(),
            "url": f"https://res.wx.qq.com/voice/getvoice?mediaid={fid}",
        })
    for m in re.finditer(r'voice_encode_fileid["\']?\s*[:=]\s*["\']([A-Za-z0-9_-]{10,})["\']', raw_html):
        fid = m.group(1)
        if fid not in seen:
            seen.add(fid)
            voices.append({
                "fileid": fid,
                "name": "Audio",
                "play_length": "",
                "url": f"https://res.wx.qq.com/voice/getvoice?mediaid={fid}",
            })
    return voices


def extract_video_items(raw_html: str) -> tuple[list[dict[str, str]], set[str]]:
    """Extract WeChat video iframes paired with resolved MP4 stream URLs and poster covers."""
    iframes: list[dict[str, str]] = []
    video_covers: set[str] = set()
    for match in re.finditer(r'<iframe\b[^>]*class="[^"]*video_iframe[^"]*"[^>]*>', raw_html, re.I):
        iframe = match.group(0)
        vid_m = re.search(r'data-mpvid="([^"]+)"', iframe)
        src_m = re.search(r'data-src="([^"]+)"', iframe)
        cov_m = re.search(r'data-cover="([^"]+)"', iframe)
        if not vid_m and not src_m:
            continue
        cov = normalize_asset_url(unquote(cov_m.group(1))) if cov_m else ""
        if cov:
            video_covers.add(cov)
        iframes.append({
            "vid": vid_m.group(1) if vid_m else "",
            "iframe": iframe,
            "src": normalize_asset_url(src_m.group(1)) if src_m else "",
            "cover": cov,
        })

    decoded = html.unescape(raw_html).replace("\\/", "/")
    raw_vurls: list[str] = []
    for u in _MPVIDEO_RE.findall(decoded):
        nu = normalize_asset_url(u).rstrip("\\")
        if nu and nu not in raw_vurls:
            raw_vurls.append(nu)

    groups: list[list[str]] = []
    by_stem: dict[str, list[str]] = {}
    for u in raw_vurls:
        stem = re.sub(r"\.f\d+\.mp4$", "", urlparse(u).path, flags=re.I)
        if stem not in by_stem:
            by_stem[stem] = []
            groups.append(by_stem[stem])
        by_stem[stem].append(u)

    def pick_best(cands: list[str]) -> str:
        if not cands:
            return ""
        for quality in (".f10004.", ".f10002.", ".f10104.", ".f10102."):
            for u in cands:
                if quality in u:
                    return u
        return cands[0]

    resolved: list[dict[str, str]] = []
    used_urls: set[str] = set()
    for idx, info in enumerate(iframes):
        best = pick_best(groups[idx]) if idx < len(groups) else ""
        if best and best not in used_urls:
            resolved.append({**info, "url": best})
            used_urls.add(best)

    if not resolved:
        for grp in groups:
            best = pick_best(grp)
            if best and best not in used_urls:
                resolved.append({"vid": "", "iframe": "", "src": "", "cover": "", "url": best})
                used_urls.add(best)

    return resolved, video_covers


def extract_image_urls(content_html: str, exclude_urls: set[str] | None = None) -> list[str]:
    """Extract ordered WeChat CDN image URLs from body HTML."""
    excluded = exclude_urls or set()
    candidates: list[str] = []
    seen: set[str] = set()

    for m in re.finditer(r'(?:data-src|src)=["\']([^"\']*mmbiz[^"\']*)["\']', content_html, re.I):
        u = normalize_asset_url(m.group(1))
        if u and u not in seen and u not in excluded:
            seen.add(u)
            candidates.append(u)

    dec = content_html.replace("&amp;", "&")
    for m in re.finditer(r'(?:https?:)?//mmbiz\.qpic\.cn/[^"\'\s<>)]+', dec):
        u = normalize_asset_url(m.group(0))
        if u and u not in seen and u not in excluded:
            seen.add(u)
            candidates.append(u)

    for m in re.finditer(r'https?%3A%2F%2Fmmbiz\.qpic\.cn%2F[^\'"\s<>)]+', content_html, re.I):
        u = normalize_asset_url(unquote(m.group(0)))
        if u and u not in seen and u not in excluded:
            seen.add(u)
            candidates.append(u)

    return candidates


def extract_albums_and_tags(raw_html: str) -> tuple[list[str], list[str]]:
    albums: list[str] = []
    tags: list[str] = []
    for m in re.finditer(r'album_title["\']?\s*[:=]\s*["\']([^"\']+)["\']', raw_html):
        t = html.unescape(m.group(1)).strip()
        if t and t not in albums:
            albums.append(t)
        if t and t not in tags:
            tags.append(t)
    for m in re.finditer(r'tag_name["\']?\s*[:=]\s*["\']([^"\']+)["\']', raw_html):
        t = html.unescape(m.group(1)).strip()
        if t and t not in tags:
            tags.append(t)
    return albums, tags


def scan_html_for_discoveries(raw_html: str, target_biz: str = "") -> tuple[list[str], list[str]]:
    """Discover ordered `album_id`s and inline `mp.weixin.qq.com/s/...` links from raw article HTML."""
    album_ids: list[str] = []
    seen_aids: set[str] = set()
    for m in re.finditer(r'album_id["\']?\s*[:=]\s*["\']?(\d{12,24})', raw_html):
        aid = m.group(1)
        if aid not in seen_aids:
            seen_aids.add(aid)
            album_ids.append(aid)

    urls: list[str] = []
    seen_u: set[str] = set()
    for m in re.finditer(r'https?://mp\.weixin\.qq\.com/s(?:/[A-Za-z0-9_~-]{15,}|\?[^\s"\'<>\\]+)', raw_html):
        u = clean_wechat_article_url(m.group(0).rstrip("',\")"))
        if not u:
            continue
        if "__biz=" in u:
            if "mid=" not in u or "sn=" not in u:
                continue
            if target_biz:
                link_biz = extract_biz_from_url(u)
                if link_biz and not same_account_id(link_biz, target_biz):
                    continue
        if u not in seen_u:
            seen_u.add(u)
            urls.append(u)

    return album_ids, urls


def parse_article_html(
    raw_html: str,
    *,
    source_url: str = "",
    title_hint: str = "",
    publish_ts_hint: int = 0,
    author_hint: str = "",
    digest_hint: str = "",
    cover_hint: str = "",
    is_original_hint: bool = False,
    target_biz: str = "",
) -> ParsedArticle:
    """Parse raw WeChat article HTML into structured metadata, media items, and discoveries."""
    if not raw_html:
        return ParsedArticle(status="parse_failed", error="empty_html")

    redirect_u = extract_share_redirect_url(raw_html)
    if redirect_u:
        return ParsedArticle(status="share_redirect", share_redirect_url=redirect_u)

    has_body_marker = any(k in raw_html for k in ('id="js_content"', 'id="js_article"', "picture_page_info_list"))
    body_html = extract_body_html(raw_html) if has_body_marker else ""
    if not body_html:
        if any(kw in raw_html for kw in _PERMANENT_UNAVAILABLE_KEYWORDS):
            return ParsedArticle(
                status="permanent_unavailable",
                error="Article deleted by author or unavailable on WeChat",
                title=title_hint,
                publish_time=publish_ts_hint,
            )
        return ParsedArticle(
            status="parse_failed",
            error="Could not locate article body (#js_content or gallery)",
            title=title_hint,
            publish_time=publish_ts_hint,
        )

    acct_meta = extract_account_meta_from_html(raw_html)
    soup_meta = BeautifulSoup(raw_html, "html.parser")

    og_title = soup_meta.find("meta", attrs={"property": "og:title"})
    title = (
        html.unescape(str(og_title["content"]).strip())
        if (og_title and og_title.get("content"))
        else (title_hint or "Untitled")
    )

    og_img = soup_meta.find("meta", attrs={"property": "og:image"})
    cover_url = (
        normalize_asset_url(str(og_img["content"]))
        if (og_img and og_img.get("content"))
        else normalize_asset_url(cover_hint)
    )

    og_desc = soup_meta.find("meta", attrs={"property": "og:description"})
    digest = (
        html.unescape(str(og_desc["content"]).strip())
        if (og_desc and og_desc.get("content"))
        else digest_hint
    )

    author_meta = soup_meta.find("meta", attrs={"name": "author"})
    author = (
        html.unescape(str(author_meta["content"]).strip())
        if (author_meta and author_meta.get("content"))
        else author_hint
    )

    ct_m = re.search(r'\bct\s*=\s*["\']?(\d{9,11})["\']?', raw_html)
    pub_ts = int(ct_m.group(1)) if ct_m else int(publish_ts_hint or 0)

    prov_m = re.search(r'provinceName["\']?\s*[:=]\s*["\']([^"\']+)["\']', raw_html)
    ip_location = html.unescape(prov_m.group(1)).strip() if prov_m else ""

    is_original = (
        bool(is_original_hint)
        or ('copyright_stat = "1"' in raw_html)
        or ("copyright_stat: '1'" in raw_html)
    )

    mid_idx = parse_mid_idx_from_html(raw_html) or parse_mid_idx_from_url(source_url)
    albums, tags = extract_albums_and_tags(raw_html)

    voices = extract_voice_items(raw_html)
    videos, video_covers = extract_video_items(raw_html)
    excluded_imgs = set(video_covers)
    if cover_url:
        # Still download cover separately; if cover also appears inline, url_map rewrites it too!
        pass
    images = extract_image_urls(body_html, exclude_urls=excluded_imgs)
    disc_albums, disc_urls = scan_html_for_discoveries(raw_html, target_biz=target_biz or acct_meta.get("biz", ""))

    return ParsedArticle(
        status="ok",
        title=title,
        author=author,
        digest=digest,
        cover_url=cover_url,
        publish_time=pub_ts,
        ip_location=ip_location,
        is_original=is_original,
        biz=acct_meta.get("biz", ""),
        account_name=acct_meta.get("nickname", ""),
        wechat_id=acct_meta.get("wechat_id", ""),
        mid_idx=mid_idx,
        albums=albums,
        tags=tags,
        content_html=body_html,
        image_urls=images,
        voice_items=voices,
        video_items=videos,
        discovered_album_ids=disc_albums,
        discovered_article_urls=disc_urls,
    )


def _replace_url_variants(html_text: str, orig_url: str, local_rel: str) -> str:
    out = html_text
    for variant in {orig_url, orig_url.replace("&", "&amp;")}:
        out = out.replace(f'"{variant}"', f'"{local_rel}"')
        out = out.replace(f"'{variant}'", f"'{local_rel}'")
        out = out.replace(f"url({variant})", f"url({local_rel})")
        out = out.replace(f"url('{variant}')", f"url('{local_rel}')")
        out = out.replace(f'url("{variant}")', f'url("{local_rel}")')
    if orig_url.startswith("https:"):
        proto_rel = orig_url[len("https:"):]
        out = out.replace(f'"{proto_rel}"', f'"{local_rel}"')
        out = out.replace(f"'{proto_rel}'", f"'{local_rel}'")
    enc_orig = quote(orig_url, safe="")
    enc_local = quote(local_rel, safe="")
    if enc_orig != orig_url:
        out = out.replace(enc_orig, enc_local)
    return out


def _replace_video_iframe(html_text: str, video_info: dict[str, str], video_tag_html: str) -> str:
    iframe_raw = video_info.get("iframe", "")
    if iframe_raw and iframe_raw in html_text:
        return html_text.replace(iframe_raw, video_tag_html)
    vid = video_info.get("vid", "")
    if vid:
        pat = re.compile(
            r'<iframe\b(?=[^>]*class="[^"]*video_iframe[^"]*")(?=[^>]*data-mpvid="' + re.escape(vid) + r'")[^>]*>(?:</iframe>)?',
            re.I,
        )
        return pat.sub(video_tag_html, html_text)
    return html_text


def render_clean_html(
    fragment_html: str,
    *,
    url_map: dict[str, str],
    audio_map: dict[str, tuple[str, str]],
    video_replacements: list[tuple[dict[str, str], str]],
) -> str:
    """Localize asset URLs, convert WeChat media tags to HTML5, and strip lazy-load traps."""
    localized = fragment_html
    for v_info, v_html in video_replacements:
        localized = _replace_video_iframe(localized, v_info, v_html)
    for orig_u, local_rel in url_map.items():
        localized = _replace_url_variants(localized, orig_u, local_rel)

    soup = BeautifulSoup(localized, "html.parser")

    for bad in soup.find_all(["script", "noscript", "style"]):
        bad.decompose()

    for vtag in soup.find_all("mpvoice"):
        fid = str(vtag.get("voice_encode_fileid") or "").strip()
        if fid and fid in audio_map:
            local_rel, vname = audio_map[fid]
            fig = soup.new_tag(
                "figure",
                attrs={
                    "class": "wechat-audio",
                    "style": "margin:16px 0;padding:12px 16px;background:#f7f8fa;border-radius:8px;",
                },
            )
            cap = soup.new_tag(
                "figcaption",
                attrs={"style": "font-size:14px;color:#333;margin-bottom:8px;font-weight:500;"},
            )
            cap.string = f"🎵 {vname}"
            aud = soup.new_tag(
                "audio",
                attrs={"controls": "", "preload": "metadata", "src": local_rel, "style": "width:100%;"},
            )
            fig.append(cap)
            fig.append(aud)
            vtag.replace_with(fig)

    for img in soup.find_all("img"):
        dsrc = img.get("data-src") or img.get("src") or ""
        if dsrc:
            img["src"] = url_map.get(str(dsrc), str(dsrc))
        for attr in list(img.attrs.keys()):
            if attr.startswith("data-") or attr in ("crossorigin", "_width"):
                del img.attrs[attr]
        classes = [c for c in (img.get("class") or []) if "img_loading" not in c and "bg_gif" not in c]
        if classes:
            img["class"] = classes
        elif "class" in img.attrs:
            del img.attrs["class"]

        style = str(img.get("style") or "")
        if style:
            style = re.sub(r"visibility\s*:\s*hidden\s*;?", "", style, flags=re.I)
            style = re.sub(r"opacity\s*:\s*0(?:\.0+)?\s*;?", "", style, flags=re.I)
            style = re.sub(r"width\s*:\s*1px\s*!important\s*;?", "", style, flags=re.I)
            style = re.sub(r"height\s*:\s*1px\s*!important\s*;?", "", style, flags=re.I)
            if "max-width" not in style.lower():
                style = style.rstrip(";") + ";max-width:100%;"
            if "height" not in style.lower():
                style = style.rstrip(";") + ";height:auto;"
            img["style"] = style.strip(";")
        else:
            img["style"] = "max-width:100%;height:auto;"

    for el in soup.find_all(style=True):
        st = str(el.get("style") or "")
        if "visibility" in st.lower() or "opacity" in st.lower():
            st = re.sub(r"visibility\s*:\s*hidden\s*;?", "", st, flags=re.I)
            st = re.sub(r"opacity\s*:\s*0(?:\.0+)?\s*;?", "", st, flags=re.I)
            el["style"] = st

    return str(soup).strip()
