"""Resumable WeChat Official Account exporter with multi-hop BFS discovery and archive builder."""

from __future__ import annotations

from collections import deque
import concurrent.futures
import csv
from dataclasses import dataclass
from datetime import datetime
import hashlib
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from bs4 import BeautifulSoup

from wechat_mp_export.auth import WeReadClient
from wechat_mp_export.config import get_proxies, read_json, redact_text, write_atomic_json
from wechat_mp_export.content_filter import filter_article_html, load_filter_rules
from wechat_mp_export.discovery import (
    batch_resolve_short_urls,
    fetch_album_articles,
    fetch_weread_catalog,
    parse_album_url,
)
from wechat_mp_export.http_client import (
    check_wechat_risk_control,
    download_cdn_asset,
    get_data_client,
    get_wechat_mobile_headers,
    guess_url_ext,
    guess_video_ext,
    sleep_throttled,
)
from wechat_mp_export.markdown import build_frontmatter_markdown, html_to_markdown
from wechat_mp_export.parser import (
    extract_share_redirect_url,
    parse_article_html,
    render_clean_html,
    scan_html_for_discoveries,
)
from wechat_mp_export.resolver import AccountInfo
from wechat_mp_export.utils import (
    CN_TZ,
    biz_to_book_id,
    clean_wechat_article_url,
    extract_biz_from_url,
    extract_short_id,
    make_article_slug,
    parse_mid_idx_from_html,
    parse_mid_idx_from_url,
    same_account_id,
    sanitize_filename,
    sha256_file,
    short_id_from_review_id,
)


@dataclass
class ExportOptions:
    out_dir: Path
    workers: int = 8
    article_delay: float = 0.65
    list_only: bool = False
    download_cached_only: bool = False
    deep_discover: bool = True
    crawl_albums: bool = True
    crawl_inline_links: bool = True
    refresh_catalog: bool = False
    refresh_albums: bool = False
    max_hops: int = 10
    max_articles: int = 0
    max_new: int = 0
    limit: int = 0
    order: str = "newest"
    save_raw_html: bool = True
    export_pdf: bool = False
    enable_filter: bool = True
    filter_rules_path: Path | None = None
    browser_fallback: bool = False
    allow_external_links: bool = False
    retry_unavailable: bool = False
    slug_prefix: str = "wx"
    proxy_url: str | None = None

    def __post_init__(self) -> None:
        if self.max_articles < 0 or self.max_new < 0 or self.limit < 0:
            raise ValueError("max_articles / max_new must be >= 0 (0 means unlimited)")
        if self.max_articles == 0 and self.limit > 0:
            self.max_articles = self.limit
        if self.order not in ("newest", "oldest"):
            raise ValueError("order must be 'newest' or 'oldest'")


def _find_chromium_binary() -> str | None:
    env_bin = os.environ.get("CHROME_PATH") or os.environ.get("CHROMIUM_PATH")
    if env_bin and Path(env_bin).exists():
        return env_bin
    if sys.platform == "darwin":
        for cand in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ):
            if Path(cand).exists():
                return cand
    elif sys.platform == "win32":
        for env_key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_key)
            if base:
                for rel in (
                    "Google/Chrome/Application/chrome.exe",
                    "Microsoft/Edge/Application/msedge.exe",
                ):
                    p = Path(base) / rel
                    if p.exists():
                        return str(p)
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
            found = shutil.which(name)
            if found:
                return found
    return None


def render_html_to_pdf(html_path: Path, pdf_path: Path) -> bool:
    """Render standalone preview HTML to PDF using local headless Chrome/Edge/Chromium."""
    chrome = _find_chromium_binary()
    if not chrome:
        return False
    cmd = [
        chrome,
        "--headless",
        "--disable-gpu",
        "--no-pdf-header-footer",
        "--disable-crash-reporter",
        "--disable-breakpad",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--disable-background-networking",
        "--mute-audio",
        f"--print-to-pdf={str(pdf_path.resolve())}",
        str(html_path.resolve()),
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        cmd.insert(1, "--no-sandbox")

    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=(sys.platform != "win32"),
        )
        proc.communicate(timeout=60)
        return pdf_path.exists() and pdf_path.stat().st_size > 0
    except Exception:
        return False
    finally:
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
            if sys.platform != "win32" and hasattr(os, "killpg"):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass


def _fetch_via_playwright(url: str, proxy_url: str | None = None) -> tuple[str, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "", "playwright_not_installed"

    headers = get_wechat_mobile_headers()
    proxy_cfg = {"server": proxy_url} if proxy_url else None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
                proxy=proxy_cfg,
            )
            try:
                ctx = browser.new_context(
                    user_agent=headers["User-Agent"],
                    viewport={"width": 414, "height": 896},
                    extra_http_headers={"X-Requested-With": "com.tencent.mm"},
                )
                ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                try:
                    page.wait_for_selector("#js_content, #js_article, .rich_media_content", timeout=7000)
                except Exception:
                    pass
                content = page.content()
                blocked, reason = check_wechat_risk_control(content, page.url)
                if blocked:
                    return "", f"browser_blocked:{reason}"
                return content, ""
            finally:
                browser.close()
    except Exception as exc:
        return "", redact_text(str(exc))


def fetch_article_html(
    url: str,
    *,
    review_id: str = "",
    weread: WeReadClient | None = None,
    allow_weread_fallback: bool = True,
    browser_fallback: bool = False,
    proxy_url: str | None = None,
    _depth: int = 0,
) -> tuple[str, str, str]:
    """Fetch raw WeChat article HTML using multi-tier anti-risk-control strategy."""
    clean_orig = clean_wechat_article_url(url)
    sid = short_id_from_review_id(review_id)
    short_u = f"https://mp.weixin.qq.com/s/{sid}" if sid else ""

    urls_to_try: list[str] = []
    if short_u:
        urls_to_try.append(short_u)
    if clean_orig and clean_orig not in urls_to_try:
        urls_to_try.append(clean_orig)

    client = get_data_client()
    proxies = get_proxies(proxy_url)
    last_err = ""

    for candidate_url in urls_to_try:
        for attempt in range(2):
            try:
                client.cookies.clear()
                resp = client.get(
                    candidate_url,
                    headers=get_wechat_mobile_headers(),
                    proxies=proxies,
                    timeout=25,
                    allow_redirects=True,
                )
                if resp.status_code == 200:
                    try:
                        raw_html = resp.content.decode("utf-8")
                    except UnicodeDecodeError:
                        raw_html = resp.content.decode("gb18030", errors="replace")
                    final_url = clean_wechat_article_url(resp.url or candidate_url)
                    blocked, reason = check_wechat_risk_control(raw_html, final_url)
                    if not blocked:
                        if _depth < 2:
                            redir = extract_share_redirect_url(raw_html)
                            if redir and redir != candidate_url:
                                r2, f2, e2 = fetch_article_html(
                                    redir,
                                    weread=weread,
                                    allow_weread_fallback=allow_weread_fallback,
                                    browser_fallback=browser_fallback,
                                    proxy_url=proxy_url,
                                    _depth=_depth + 1,
                                )
                                if r2:
                                    return r2, f2, ""
                        return raw_html, final_url, ""
                    last_err = reason
                else:
                    last_err = f"HTTP {resp.status_code}"
            except Exception as exc:
                last_err = redact_text(str(exc))
            if attempt == 0:
                time.sleep(1.2)

    if allow_weread_fallback and review_id and weread and weread.has_valid_session():
        wr_html = weread.fetch_web_article_html(review_id)
        if wr_html:
            return wr_html, short_u or clean_orig, ""

    if browser_fallback and urls_to_try:
        pw_html, pw_err = _fetch_via_playwright(urls_to_try[0], proxy_url=proxy_url)
        if pw_html:
            return pw_html, urls_to_try[0], ""
        last_err = last_err or pw_err

    return "", short_u or clean_orig, last_err or "fetch_failed"


def _render_preview_html(
    *,
    title: str,
    dt_str: str,
    author: str,
    ip_location: str,
    tags: list[str],
    body_html: str,
) -> str:
    loc_span = f'<span>📍 {html.escape(ip_location)}</span>' if ip_location else ""
    tag_span = f'<span>🏷️ {html.escape(", ".join(tags))}</span>' if tags else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(title)}</title>
  <style>
    body {{ max-width: 740px; margin: 0 auto; padding: 28px 18px 64px; font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif; line-height: 1.8; color: #222; background: #fff; }}
    h1 {{ font-size: 24px; line-height: 1.4; margin: 0 0 12px; color: #111; }}
    .meta {{ color: #666; font-size: 14px; margin-bottom: 24px; padding-bottom: 14px; border-bottom: 1px solid #eee; display: flex; flex-wrap: wrap; gap: 12px; }}
    #js_content img, #js_content video {{ max-width: 100% !important; height: auto !important; border-radius: 4px; }}
    blockquote {{ margin: 16px 0; padding: 10px 16px; border-left: 4px solid #07c160; background: #f7f8fa; color: #444; }}
    pre {{ background: #f6f8fa; padding: 14px; border-radius: 6px; overflow-x: auto; }}
    table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
    th, td {{ border: 1px solid #dfe2e5; padding: 8px 12px; }}
  </style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <div class="meta">
    <span>📅 {html.escape(dt_str)}</span>
    <span>👤 {html.escape(author)}</span>
    {loc_span}
    {tag_span}
  </div>
  <div id="js_content">{body_html}</div>
</body>
</html>
"""


def build_archive_outputs(
    out_dir: Path,
    account: AccountInfo,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Generate `manifest.json`, `articles_index.csv`, `index.html`, `EXPORT_REPORT.md`, and `halo_import_bundle.json`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    sorted_records = sorted(
        records,
        key=lambda r: (int(r.get("publish_time") or 0), str(r.get("slug") or r.get("id") or "")),
    )
    for idx, rec in enumerate(sorted_records, 1):
        rec["index"] = idx

    ok_items = [r for r in sorted_records if r.get("status") == "ok"]
    failed_items = [r for r in sorted_records if r.get("status") != "ok"]

    total_media_bytes = sum(int(r.get("media_bytes") or 0) for r in ok_items)
    total_images = sum(int(r.get("image_count") or 0) for r in ok_items)
    total_audios = sum(int(r.get("audio_count") or 0) for r in ok_items)
    total_videos = sum(int(r.get("video_count") or 0) for r in ok_items)
    account_name = account.nickname or (ok_items[0].get("account_name", "") if ok_items else "WeChat Archive")

    now_iso = datetime.now(CN_TZ).isoformat()
    manifest = {
        "schema": "wechat-mp-export-manifest/v1",
        "exported_at": now_iso,
        "account": {
            "name": account_name,
            "wechat_id": account.wechat_id,
            "aliases": list(account.aliases),
            "bookId": account.book_id,
            "biz": account.biz,
            "intro": account.intro,
            "cover": account.cover,
        },
        "summary": {
            "total_listed": len(sorted_records),
            "exported_ok": len(ok_items),
            "unavailable_or_failed": len(failed_items),
            "date_range": {
                "earliest": ok_items[0]["publish_date"] if ok_items else None,
                "latest": ok_items[-1]["publish_date"] if ok_items else None,
            },
            "total_images": total_images,
            "total_audios": total_audios,
            "total_videos": total_videos,
            "total_media_bytes": total_media_bytes,
            "total_media_mb": round(total_media_bytes / (1024 * 1024), 2),
        },
        "articles": sorted_records,
    }
    write_atomic_json(out_dir / "manifest.json", manifest)

    # Halo 2.x portable import bundle (zero hardcoded target site or OS user PII)
    halo_posts: list[dict[str, Any]] = []
    for r in ok_items:
        folder = r["folder"]
        halo_posts.append({
            "local_folder": f"articles/{folder}",
            "halo_content_path": r["content_file"],
            "markdown_path": r["markdown_file"],
            "cover_local": f"articles/{folder}/{r['cover_local']}" if r.get("cover_local") else "",
            "media": [
                {**m, "bundle_path": f"articles/{folder}/{m['local_path']}"}
                for m in (r.get("media") or [])
            ],
            "post_spec": {
                "title": r["title"],
                "slug": r["slug"],
                "publishTime": r["publish_time_iso"],
                "visible": "PUBLIC",
                "pinned": False,
                "allowComment": True,
                "deleted": False,
                "excerpt": {
                    "autoGenerate": False,
                    "raw": str(r.get("excerpt") or "")[:200],
                },
                "categories_names": [account_name] if account_name else [],
                "tags_names": list(r.get("tags") or []),
                "rawType": "HTML",
                "source_metadata": {
                    "wechat_id": r.get("account_id") or account.wechat_id,
                    "wechat_review_id": r.get("id", ""),
                    "wechat_url": r.get("source_url", ""),
                    "author": r.get("author", ""),
                    "ip_location": r.get("ip_location", ""),
                },
            },
        })
    write_atomic_json(
        out_dir / "halo_import_bundle.json",
        {
            "schema": "halo-2.x-wechat-import-bundle/v1",
            "generated_at": now_iso,
            "account_name": account_name,
            "count": len(halo_posts),
            "posts": halo_posts,
        },
    )

    # UTF-8-SIG CSV spreadsheet index
    with open(out_dir / "articles_index.csv", "w", encoding="utf-8-sig", newline="") as cf:
        writer = csv.writer(cf)
        writer.writerow([
            "index", "publish_date", "title", "author", "is_original",
            "tags", "word_count", "image_count", "audio_count", "video_count",
            "status", "folder", "source_url",
        ])
        for r in sorted_records:
            writer.writerow([
                r.get("index"),
                r.get("publish_date") or str(r.get("publish_time_iso") or "")[:16],
                r.get("title", ""),
                r.get("author", ""),
                "yes" if r.get("is_original") else "no",
                "; ".join(r.get("tags") or []),
                r.get("word_count", 0),
                r.get("image_count", 0),
                r.get("audio_count", 0),
                r.get("video_count", 0),
                r.get("status", ""),
                r.get("folder", ""),
                r.get("source_url", ""),
            ])

    # Offline searchable index.html (newest first)
    rows_html: list[str] = []
    for r in reversed(ok_items):
        tags = r.get("tags") or []
        tags_attr = html.escape("|".join(tags), quote=True)
        search_blob = html.escape(f"{r['title']} {r.get('excerpt', '')} {' '.join(tags)}".lower(), quote=True)
        tags_badges = " ".join(f'<span class="tag">{html.escape(t)}</span>' for t in tags)
        md_link = f'<a class="md-link" href="{html.escape(r["markdown_file"], quote=True)}">MD</a>'
        rows_html.append(
            f'<tr data-search="{search_blob}" data-tags="{tags_attr}">'
            f'<td class="date">{html.escape(r["publish_date"])}</td>'
            f'<td><a href="{html.escape(r["preview_file"], quote=True)}" target="_blank">{html.escape(r["title"])}</a> '
            f'{tags_badges} {md_link}</td>'
            f'<td class="num">{r["word_count"]}</td>'
            f'<td class="num">{r["image_count"]}</td>'
            f'<td class="num">{int(r["audio_count"]) + int(r["video_count"])}</td>'
            f"</tr>"
        )

    sub_parts = []
    if account.wechat_id:
        sub_parts.append(f"WeChat ID: {html.escape(account.wechat_id)}")
    if account.aliases:
        sub_parts.append(f"Former names: {html.escape(', '.join(account.aliases))}")
    if account.intro:
        sub_parts.append(html.escape(account.intro))
    sub_line = " ｜ ".join(sub_parts)

    index_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(account_name)} - Offline Archive</title>
  <style>
    body {{ max-width: 980px; margin: 0 auto; padding: 28px 20px 64px; font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif; color: #222; background: #fff; }}
    h1 {{ margin: 0 0 8px; }}
    .sub {{ color: #666; margin-bottom: 20px; font-size: 14px; }}
    .stats {{ display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 20px; }}
    .card {{ background: #f7f8fa; padding: 12px 18px; border-radius: 8px; font-size: 13px; min-width: 120px; }}
    .card b {{ font-size: 20px; display: block; color: #07c160; margin-bottom: 2px; }}
    .toolbar {{ margin-bottom: 16px; display: flex; gap: 12px; align-items: center; }}
    .toolbar input {{ flex: 1; padding: 9px 12px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ padding: 10px 8px; border-bottom: 1px solid #eee; text-align: left; }}
    th {{ background: #fafafa; font-weight: 600; }}
    td.date {{ white-space: nowrap; color: #666; width: 145px; }}
    td.num {{ text-align: right; color: #666; width: 70px; }}
    a {{ color: #1a73e8; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .tag {{ display: inline-block; font-size: 12px; padding: 1px 6px; background: #eef6ff; color: #1a73e8; border-radius: 4px; margin-left: 6px; }}
    .md-link {{ display: inline-block; font-size: 11px; padding: 1px 5px; background: #f0f2f5; color: #555; border-radius: 3px; margin-left: 6px; }}
  </style>
</head>
<body>
  <h1>{html.escape(account_name)}</h1>
  <div class="sub">{sub_line}</div>
  <div class="stats">
    <div class="card"><b>{len(ok_items)}</b>Articles</div>
    <div class="card"><b>{total_images}</b>Images</div>
    <div class="card"><b>{total_audios + total_videos}</b>Audio / Video</div>
    <div class="card"><b>{total_media_bytes / (1024 * 1024):.1f} MB</b>Total Media</div>
  </div>
  <div class="toolbar">
    <input id="q" type="search" placeholder="Filter articles by title, summary, or tag..." oninput="filterRows(this.value)">
    <span id="cnt" style="color:#666;font-size:13px;">{len(ok_items)} shown</span>
  </div>
  <table>
    <thead>
      <tr>
        <th>Published</th>
        <th>Title</th>
        <th style="text-align:right">Chars</th>
        <th style="text-align:right">Images</th>
        <th style="text-align:right">A/V</th>
      </tr>
    </thead>
    <tbody id="rows">{''.join(rows_html)}</tbody>
  </table>
  <script>
    function filterRows(q) {{
      var kw = (q || '').trim().toLowerCase();
      var trs = document.querySelectorAll('#rows tr');
      var shown = 0;
      trs.forEach(function(tr) {{
        var ok = !kw || (tr.getAttribute('data-search') || '').indexOf(kw) !== -1;
        tr.style.display = ok ? '' : 'none';
        if (ok) shown++;
      }});
      document.getElementById('cnt').textContent = shown + ' shown';
    }}
  </script>
</body>
</html>
"""
    (out_dir / "index.html").write_text(index_doc, encoding="utf-8")

    # EXPORT_REPORT.md
    earliest = manifest["summary"]["date_range"]["earliest"] or "-"
    latest = manifest["summary"]["date_range"]["latest"] or "-"
    report_lines = [
        f"# Export Report: {account_name}",
        "",
        f"- **Account Name**: {account_name}",
    ]
    if account.wechat_id:
        report_lines.append(f"- **WeChat ID**: `{account.wechat_id}`")
    if account.book_id or account.biz:
        report_lines.append(f"- **Account Identifiers**: `bookId={account.book_id}` / `__biz={account.biz}`")
    report_lines.extend([
        f"- **Exported At**: {now_iso}",
        f"- **Total Discovered**: {len(sorted_records)}",
        f"- **Exported Successfully**: {len(ok_items)}",
        f"- **Unavailable / Failed**: {len(failed_items)}",
        f"- **Date Span**: {earliest} ~ {latest}",
        f"- **Localized Media**: {total_images} images, {total_audios} audio clips, {total_videos} videos ({manifest['summary']['total_media_mb']} MB)",
        "",
        "## Archive Layout",
        "",
        "- `index.html`: Offline searchable article directory",
        "- `manifest.json`: Full machine-readable archive manifest",
        "- `articles_index.csv`: Spreadsheet-friendly catalog (UTF-8 with BOM)",
        "- `halo_import_bundle.json`: Portable Halo 2.x import bundle (`wechat-mp-export import-halo`)",
        "- `articles/<YYYY-MM-DD_id_title>/`: Per-article Markdown (`article.md`), clean HTML (`content.html`), standalone preview (`preview.html`), metadata (`meta.json`), and `media/` assets",
    ])
    if failed_items:
        report_lines.extend(["", "## Unavailable or Failed Articles", ""])
        for fi in failed_items:
            d_str = str(fi.get("publish_time_iso") or "")[:10]
            report_lines.append(f"- [{fi.get('index')}] `{d_str}` {fi.get('title') or fi.get('source_url')}: {fi.get('error', '')}")
    (out_dir / "EXPORT_REPORT.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    return manifest


class AccountExporter:
    """Orchestrates catalog discovery, multi-hop BFS, asset localization, and archive generation."""

    def __init__(
        self,
        options: ExportOptions,
        *,
        weread: WeReadClient | None = None,
        html_fetcher: Callable[..., tuple[str, str, str]] | None = None,
        asset_downloader: Callable[[str, Path, str | None], str | None] | None = None,
        album_fetcher: Callable[[str, str], tuple[str, list[dict[str, Any]]]] | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.opts = options
        self.weread = weread
        self.log = log
        self._fetch_html = html_fetcher or self._default_fetch_html
        self._download_asset = asset_downloader or self._default_download_asset
        self._fetch_album = album_fetcher or self._default_fetch_album
        self.filter_rules = (
            load_filter_rules(self.opts.filter_rules_path)
            if self.opts.enable_filter
            else {"enabled": False}
        )

    def _default_fetch_html(self, url: str, review_id: str = "") -> tuple[str, str, str]:
        return fetch_article_html(
            url,
            review_id=review_id,
            weread=self.weread,
            allow_weread_fallback=(not self.opts.download_cached_only),
            browser_fallback=self.opts.browser_fallback,
            proxy_url=self.opts.proxy_url,
        )

    def _default_download_asset(self, url: str, dest: Path, proxy_url: str | None) -> str | None:
        return download_cdn_asset(url, dest, proxy_url=proxy_url)

    def _default_fetch_album(self, biz: str, album_id: str) -> tuple[str, list[dict[str, Any]]]:
        return fetch_album_articles(biz, album_id, proxy_url=self.opts.proxy_url)

    def _export_single_article(
        self,
        *,
        url: str,
        articles_root: Path,
        account: AccountInfo,
        strict_account_match: bool,
        review_id: str = "",
        title_hint: str = "",
        ts_hint: int = 0,
        author_hint: str = "",
        digest_hint: str = "",
        cover_hint: str = "",
        is_original_hint: bool = False,
        extra_albums: list[str] | None = None,
    ) -> tuple[dict[str, Any], str]:
        clean_url = clean_wechat_article_url(url)
        raw_html, final_url, fetch_err = self._fetch_html(clean_url, review_id=review_id)

        if not raw_html:
            dt_g = datetime.fromtimestamp(ts_hint, CN_TZ) if ts_hint > 0 else datetime.now(CN_TZ)
            return {
                "id": review_id,
                "title": title_hint or clean_url,
                "source_url": clean_url,
                "publish_time": ts_hint,
                "publish_time_iso": dt_g.isoformat() if ts_hint > 0 else "",
                "status": "fetch_failed",
                "error": fetch_err or "empty_html",
            }, ""

        parsed = parse_article_html(
            raw_html,
            source_url=final_url or clean_url,
            title_hint=title_hint,
            publish_ts_hint=ts_hint,
            author_hint=author_hint,
            digest_hint=digest_hint,
            cover_hint=cover_hint,
            is_original_hint=is_original_hint,
            target_biz=account.biz,
        )

        if parsed.status != "ok":
            dt_g = datetime.fromtimestamp(ts_hint, CN_TZ) if ts_hint > 0 else datetime.now(CN_TZ)
            return {
                "id": review_id,
                "title": parsed.title or title_hint or clean_url,
                "source_url": final_url or clean_url,
                "publish_time": ts_hint,
                "publish_time_iso": dt_g.isoformat() if ts_hint > 0 else "",
                "status": parsed.status,
                "error": parsed.error,
            }, raw_html

        # Foreign-account check when exporting a specific Official Account
        if strict_account_match and account.biz and parsed.biz:
            if not same_account_id(parsed.biz, account.biz):
                return {"status": "foreign_account_skipped", "source_url": final_url or clean_url}, ""

        # Populate missing account metadata opportunistically from first parsed article
        if not account.biz and parsed.biz:
            account.biz = parsed.biz
            account.book_id = account.book_id or biz_to_book_id(parsed.biz)
        if not account.nickname and parsed.account_name:
            account.nickname = parsed.account_name
        if not account.wechat_id and parsed.wechat_id:
            account.wechat_id = parsed.wechat_id

        pub_ts = parsed.publish_time or ts_hint
        dt_cn = datetime.fromtimestamp(pub_ts, CN_TZ) if pub_ts > 0 else datetime.now(CN_TZ)
        sid = (
            short_id_from_review_id(review_id)
            or extract_short_id(final_url)
            or extract_short_id(clean_url)
        )
        mid_idx = parsed.mid_idx
        if sid:
            unique_tok = re.sub(r"[^a-zA-Z0-9_-]", "", sid)[:12]
        elif mid_idx:
            unique_tok = f"m{mid_idx[0]}_{mid_idx[1]}"
        else:
            unique_tok = hashlib.sha1((final_url or clean_url).encode("utf-8")).hexdigest()[:10]

        folder_name = f"{dt_cn.strftime('%Y-%m-%d')}_{unique_tok}_{sanitize_filename(parsed.title, 44)}"
        art_dir = articles_root / folder_name
        media_dir = art_dir / "media"
        media_dir.mkdir(parents=True, exist_ok=True)

        if self.opts.save_raw_html:
            (art_dir / "raw.html").write_text(raw_html, encoding="utf-8")

        filtered_html = filter_article_html(parsed.content_html, rules=self.filter_rules)

        url_map: dict[str, str] = {}
        media_manifest: list[dict[str, Any]] = []

        def record_media(rel_path: str, orig_url: str, kind: str) -> None:
            fpath = art_dir / rel_path
            if fpath.exists():
                media_manifest.append({
                    "local_path": rel_path,
                    "original_url": orig_url,
                    "kind": kind,
                    "size_bytes": fpath.stat().st_size,
                    "sha256": sha256_file(fpath),
                })

        # 1. Cover image
        local_cover = ""
        if parsed.cover_url:
            cov_ext = guess_url_ext(parsed.cover_url, "jpg")
            saved_cov = self._download_asset(parsed.cover_url, media_dir / f"cover.{cov_ext}", self.opts.proxy_url)
            if saved_cov:
                local_cover = f"media/{saved_cov}"
                url_map[parsed.cover_url] = local_cover
                record_media(local_cover, parsed.cover_url, "cover")

        # 2. Parallel inline CDN images
        img_urls = [u for u in parsed.image_urls if u not in url_map]
        if img_urls:
            def _dl_one(pair: tuple[int, str]) -> tuple[int, str, str | None]:
                idx_i, img_u = pair
                ext = guess_url_ext(img_u, "jpg")
                fname = self._download_asset(img_u, media_dir / f"img_{idx_i:03d}.{ext}", self.opts.proxy_url)
                return idx_i, img_u, fname

            workers = max(1, min(int(self.opts.workers or 8), len(img_urls)))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                for _, img_u, saved_name in pool.map(_dl_one, enumerate(img_urls, 1)):
                    if saved_name:
                        rel = f"media/{saved_name}"
                        url_map[img_u] = rel
                        record_media(rel, img_u, "image")

        # 3. Embedded WeChat voice/audio (<mpvoice>)
        audio_map: dict[str, tuple[str, str]] = {}
        for idx_a, vitem in enumerate(parsed.voice_items, 1):
            saved_aud = self._download_asset(vitem["url"], media_dir / f"audio_{idx_a:03d}.mp3", self.opts.proxy_url)
            if saved_aud:
                rel_a = f"media/{saved_aud}"
                audio_map[vitem["fileid"]] = (rel_a, vitem["name"])
                record_media(rel_a, vitem["url"], "audio")

        # 4. Embedded WeChat videos + posters
        video_replacements: list[tuple[dict[str, str], str]] = []
        for idx_v, vinfo in enumerate(parsed.video_items, 1):
            vurl = vinfo["url"]
            vext = guess_video_ext(vurl)
            saved_vid = self._download_asset(vurl, media_dir / f"video_{idx_v:02d}.{vext}", self.opts.proxy_url)
            if saved_vid:
                rel_v = f"media/{saved_vid}"
                url_map[vurl] = rel_v
                record_media(rel_v, vurl, "video")
                poster_attr = ""
                if vinfo.get("cover"):
                    p_ext = guess_url_ext(vinfo["cover"], "jpg")
                    saved_poster = self._download_asset(
                        vinfo["cover"],
                        media_dir / f"video_cover_{idx_v:02d}.{p_ext}",
                        self.opts.proxy_url,
                    )
                    if saved_poster:
                        rel_p = f"media/{saved_poster}"
                        poster_attr = f' poster="{rel_p}"'
                        url_map[vinfo["cover"]] = rel_p
                        record_media(rel_p, vinfo["cover"], "video_poster")
                video_replacements.append((
                    vinfo,
                    f'<video controls preload="metadata" style="max-width:100%;height:auto;display:block;margin:14px auto;" src="{rel_v}"{poster_attr}></video>',
                ))

        clean_body = render_clean_html(
            filtered_html,
            url_map=url_map,
            audio_map=audio_map,
            video_replacements=video_replacements,
        )
        (art_dir / "content.html").write_text(clean_body, encoding="utf-8")

        plain_text = BeautifulSoup(clean_body, "html.parser").get_text("\n", strip=True)
        excerpt = parsed.digest or re.sub(r"\s+", " ", plain_text)[:150].strip()

        albums = list(parsed.albums)
        tags = list(parsed.tags)
        for ea in extra_albums or []:
            if ea and ea not in albums:
                albums.append(ea)
            if ea and ea not in tags:
                tags.append(ea)
        if parsed.is_original and "原创" not in tags:
            tags.append("原创")

        canonical_rid = review_id
        if not canonical_rid and sid and account.book_id:
            canonical_rid = f"{account.book_id}_{sid}"
        slug = make_article_slug(
            dt_cn,
            review_id=canonical_rid,
            url=final_url or clean_url,
            mid_idx=mid_idx,
            prefix=self.opts.slug_prefix,
        )
        effective_author = parsed.author or account.nickname or parsed.account_name
        effective_account = account.nickname or parsed.account_name

        md_body = html_to_markdown(clean_body, media_map=url_map, title_to_skip=parsed.title)
        full_md = build_frontmatter_markdown(
            title=parsed.title,
            slug=slug,
            date_iso=dt_cn.isoformat(),
            author=effective_author,
            account_name=effective_account,
            tags=tags,
            cover=local_cover,
            excerpt=excerpt,
            source_url=final_url or clean_url,
            review_id=canonical_rid,
            body_markdown=md_body,
        )
        (art_dir / "article.md").write_text(full_md, encoding="utf-8")

        preview_html = _render_preview_html(
            title=parsed.title,
            dt_str=dt_cn.strftime("%Y-%m-%d %H:%M"),
            author=effective_author,
            ip_location=parsed.ip_location,
            tags=tags,
            body_html=clean_body,
        )
        preview_path = art_dir / "preview.html"
        preview_path.write_text(preview_html, encoding="utf-8")

        pdf_rel = ""
        if self.opts.export_pdf:
            pdf_path = art_dir / "article.pdf"
            if render_html_to_pdf(preview_path, pdf_path):
                pdf_rel = f"articles/{folder_name}/article.pdf"

        meta_record: dict[str, Any] = {
            "index": 0,
            "id": canonical_rid,
            "mid_idx": list(mid_idx) if mid_idx else None,
            "slug": slug,
            "title": parsed.title,
            "author": effective_author,
            "account_name": effective_account,
            "account_id": account.wechat_id or parsed.wechat_id,
            "publish_time": pub_ts,
            "publish_time_iso": dt_cn.isoformat(),
            "publish_date": dt_cn.strftime("%Y-%m-%d %H:%M"),
            "is_original": parsed.is_original,
            "ip_location": parsed.ip_location,
            "albums": albums,
            "tags": tags,
            "excerpt": excerpt,
            "source_url": final_url or clean_url,
            "cover_local": local_cover,
            "cover_remote": parsed.cover_url,
            "folder": folder_name,
            "content_file": f"articles/{folder_name}/content.html",
            "markdown_file": f"articles/{folder_name}/article.md",
            "preview_file": f"articles/{folder_name}/preview.html",
            "raw_html_file": f"articles/{folder_name}/raw.html" if self.opts.save_raw_html else "",
            "pdf_file": pdf_rel,
            "word_count": len(re.sub(r"\s+", "", plain_text)),
            "image_count": sum(1 for m in media_manifest if m["kind"] in ("image", "cover")),
            "audio_count": sum(1 for m in media_manifest if m["kind"] == "audio"),
            "video_count": sum(1 for m in media_manifest if m["kind"] == "video"),
            "media_bytes": sum(int(m["size_bytes"]) for m in media_manifest),
            "media": media_manifest,
            "status": "ok",
        }
        write_atomic_json(art_dir / "meta.json", meta_record)
        return meta_record, raw_html

    def run(
        self,
        *,
        account: AccountInfo,
        seed_urls: list[str] | None = None,
        explicit_urls: list[str] | None = None,
        album_urls: list[str] | None = None,
        use_weread_catalog: bool = True,
    ) -> dict[str, Any]:
        out_dir = self.opts.out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        articles_root = out_dir / "articles"
        articles_root.mkdir(parents=True, exist_ok=True)

        strict_account_match = bool(account.biz or account.book_id) and not self.opts.allow_external_links

        # Persist public Official Account info (no user credentials)
        if account.biz or account.book_id or account.nickname:
            write_atomic_json(out_dir / "account_info.json", account.to_public_dict())

        # Index existing completed articles for fast resume & deduplication
        by_mid_idx: dict[tuple[int, int], dict[str, Any]] = {}
        by_sid: dict[str, dict[str, Any]] = {}
        by_rid: dict[str, dict[str, Any]] = {}
        all_records: list[dict[str, Any]] = []

        crawled_albums: set[str] = set()
        albums_cache_path = out_dir / "albums_discovered.json"
        albums_cache: dict[str, Any] = {"albums": {}, "articles": []}
        if albums_cache_path.exists() and not self.opts.refresh_albums:
            loaded_alb = read_json(albums_cache_path, {})
            if isinstance(loaded_alb, dict):
                albums_cache = {
                    "albums": dict(loaded_alb.get("albums") or {}),
                    "articles": list(loaded_alb.get("articles") or []),
                }
                crawled_albums.update(albums_cache["albums"].keys())

        pending_albums: deque[str] = deque()
        pending_urls: deque[dict[str, Any]] = deque()
        queued_mid_idx: set[tuple[int, int]] = set()
        queued_sids: set[str] = set()
        queued_rids: set[str] = set()
        queued_urls: set[str] = set()

        def merge_album_tags(rec: dict[str, Any], new_albums: list[str] | None) -> None:
            if not new_albums:
                return
            changed = False
            for alb in new_albums:
                if alb and alb not in rec.setdefault("albums", []):
                    rec["albums"].append(alb)
                    changed = True
                if alb and alb not in rec.setdefault("tags", []):
                    rec["tags"].append(alb)
                    changed = True
            if changed and rec.get("folder"):
                art_folder = articles_root / rec["folder"]
                write_atomic_json(art_folder / "meta.json", rec)
                md_file = art_folder / "article.md"
                if md_file.exists():
                    md_txt = md_file.read_text(encoding="utf-8")
                    new_tags_line = f"tags: {json.dumps(rec['tags'], ensure_ascii=False)}"
                    md_txt = re.sub(r"^tags:\s*\[.*\]$", new_tags_line, md_txt, count=1, flags=re.M)
                    md_file.write_text(md_txt, encoding="utf-8")

        seq_counter = 0

        def enqueue_candidate(
            url: str,
            *,
            title: str = "",
            ctime: int = 0,
            rid: str = "",
            author: str = "",
            digest: str = "",
            cover: str = "",
            is_original: bool = False,
            albums: list[str] | None = None,
            priority: int = 1,
        ) -> None:
            nonlocal seq_counter
            cu = clean_wechat_article_url(url)
            if not cu and rid:
                sid_from_r = short_id_from_review_id(rid)
                if sid_from_r:
                    cu = f"https://mp.weixin.qq.com/s/{sid_from_r}"
            if not cu:
                return

            if strict_account_match and account.biz and "__biz=" in cu:
                ubiz = extract_biz_from_url(cu)
                if ubiz and not same_account_id(ubiz, account.biz):
                    return

            mi = parse_mid_idx_from_url(cu)
            sid = extract_short_id(cu) or short_id_from_review_id(rid)

            if mi and mi in by_mid_idx:
                merge_album_tags(by_mid_idx[mi], albums)
                return
            if sid and sid in by_sid:
                merge_album_tags(by_sid[sid], albums)
                return
            if rid and rid in by_rid:
                merge_album_tags(by_rid[rid], albums)
                return

            if (mi and mi in queued_mid_idx) or (sid and sid in queued_sids) or (rid and rid in queued_rids) or (cu in queued_urls):
                return

            if mi:
                queued_mid_idx.add(mi)
            if sid:
                queued_sids.add(sid)
            if rid:
                queued_rids.add(rid)
            queued_urls.add(cu)
            seq_counter += 1
            pending_urls.append({
                "url": cu,
                "title": title,
                "ctime": int(ctime or 0),
                "rid": rid,
                "author": author,
                "digest": digest,
                "cover": cover,
                "is_original": is_original,
                "albums": list(albums or []),
                "priority": priority,
                "seq": seq_counter,
            })

        # 1. Scan existing local article folders (self-heal interrupted folders & index completed ones)
        for sub in sorted(p for p in articles_root.iterdir() if p.is_dir()):
            mp = sub / "meta.json"
            if not mp.exists():
                shutil.rmtree(sub, ignore_errors=True)
                continue
            meta = read_json(mp)
            if not isinstance(meta, dict):
                shutil.rmtree(sub, ignore_errors=True)
                continue
            st = meta.get("status")
            allow_skip_unavail = (st == "permanent_unavailable" and not self.opts.retry_unavailable)
            if st != "ok" and not allow_skip_unavail:
                retry_u = str(meta.get("source_url") or "")
                shutil.rmtree(sub, ignore_errors=True)
                if retry_u:
                    enqueue_candidate(
                        retry_u,
                        title=str(meta.get("title") or ""),
                        ctime=int(meta.get("publish_time") or 0),
                        rid=str(meta.get("id") or ""),
                    )
                continue

            raw_p = sub / "raw.html"
            raw_txt = raw_p.read_text(encoding="utf-8", errors="ignore") if raw_p.exists() else ""
            if st == "ok":
                mi_saved = tuple(meta["mid_idx"]) if isinstance(meta.get("mid_idx"), list) and len(meta["mid_idx"]) == 2 else None
                if not mi_saved and raw_txt:
                    mi_saved = parse_mid_idx_from_html(raw_txt)
                    if mi_saved:
                        meta["mid_idx"] = list(mi_saved)
                        write_atomic_json(mp, meta)
                if mi_saved:
                    by_mid_idx[(int(mi_saved[0]), int(mi_saved[1]))] = meta
                sid_saved = extract_short_id(str(meta.get("source_url") or "")) or short_id_from_review_id(str(meta.get("id") or ""))
                if sid_saved:
                    by_sid[sid_saved] = meta
                if meta.get("id"):
                    by_rid[str(meta["id"])] = meta

                if self.opts.deep_discover and raw_txt:
                    aids, iurls = scan_html_for_discoveries(raw_txt, target_biz=account.biz)
                    if self.opts.crawl_albums:
                        for aid in aids:
                            if aid not in crawled_albums:
                                pending_albums.append(aid)
                    if self.opts.crawl_inline_links:
                        for iu in iurls:
                            enqueue_candidate(iu, priority=2)

            all_records.append(meta)

        ok_total_count = sum(1 for r in all_records if r.get("status") == "ok")
        downloaded_this_run = 0

        if all_records:
            self.log(f"Indexed {ok_total_count} previously exported articles in {out_dir}.")

        def remaining_budget() -> int | None:
            caps: list[int] = []
            if self.opts.max_articles > 0:
                caps.append(max(0, self.opts.max_articles - ok_total_count))
            if self.opts.max_new > 0:
                caps.append(max(0, self.opts.max_new - downloaded_this_run))
            return min(caps) if caps else None

        init_rem = remaining_budget()
        if init_rem is not None and init_rem <= 0 and not self.opts.list_only:
            self.log(
                f"Archive already has {ok_total_count} exported article(s) "
                f"(reaching max_articles={self.opts.max_articles}); nothing more to download."
            )
            if account.biz or account.book_id or account.nickname:
                write_atomic_json(out_dir / "account_info.json", account.to_public_dict())
            return build_archive_outputs(out_dir, account, all_records)

        # Enqueue explicitly requested `--url` / `--url-file` entries with top priority (0)
        for exp_u in explicit_urls or []:
            enqueue_candidate(exp_u, priority=0)

        # 2. Fetch or load WeRead catalog when applicable
        if self.opts.download_cached_only:
            cp = read_json(out_dir / "article_list_checkpoint.json", {})
            for entry in (cp.get("articles") or []) if isinstance(cp, dict) else []:
                enqueue_candidate(
                    str(entry.get("link") or ""),
                    title=str(entry.get("title") or ""),
                    ctime=int(entry.get("update_time") or 0),
                    rid=str(entry.get("id") or ""),
                    author=str(entry.get("author") or ""),
                    digest=str(entry.get("digest") or ""),
                    cover=str(entry.get("cover") or ""),
                    is_original=bool(entry.get("is_original")),
                    priority=1,
                )
        elif use_weread_catalog and account.book_id and self.weread and self.weread.has_valid_session():
            if self.opts.list_only:
                cat_cap = self.opts.max_articles if self.opts.max_articles > 0 else 0
            elif init_rem is not None and self.opts.order == "newest":
                # Only need enough newest catalog items to cover existing + remaining + small buffer
                cat_cap = ok_total_count + init_rem + 5
            else:
                cat_cap = 0

            catalog = fetch_weread_catalog(
                self.weread,
                account.book_id,
                account.nickname,
                out_dir,
                max_entries=cat_cap,
                refresh_catalog=self.opts.refresh_catalog,
                log=self.log,
            )
            self.log(f"WeRead catalog ready: {len(catalog)} entries.")
            if self.opts.list_only:
                return {"listed": len(catalog), "exported_ok": ok_total_count, "out_dir": str(out_dir)}
            for entry in catalog:
                enqueue_candidate(
                    str(entry.get("link") or ""),
                    title=str(entry.get("title") or ""),
                    ctime=int(entry.get("update_time") or 0),
                    rid=str(entry.get("id") or ""),
                    author=str(entry.get("author") or ""),
                    digest=str(entry.get("digest") or ""),
                    cover=str(entry.get("cover") or ""),
                    is_original=bool(entry.get("is_original")),
                    priority=1,
                )

        if self.opts.list_only:
            return {"listed": len(pending_urls), "exported_ok": ok_total_count, "out_dir": str(out_dir)}

        # 3. Enqueue cached album entries, explicit album URLs, and seed URLs
        for cached_art in albums_cache.get("articles") or []:
            enqueue_candidate(
                str(cached_art.get("url") or cached_art.get("link") or ""),
                title=str(cached_art.get("title") or ""),
                ctime=int(cached_art.get("create_time") or cached_art.get("update_time") or 0),
                albums=list(cached_art.get("albums") or ([cached_art["album"]] if cached_art.get("album") else [])),
                priority=1,
            )

        for a_url in album_urls or []:
            abiz, aid = parse_album_url(a_url)
            if abiz and not account.biz:
                account.biz = abiz
                account.book_id = account.book_id or biz_to_book_id(abiz)
            if aid and aid not in crawled_albums:
                pending_albums.append(aid)

        for s_url in seed_urls or []:
            # If no catalog entries exist yet (e.g. --no-weread), fetch the seed URL first to bootstrap discovery
            seed_prio = 0 if not pending_urls else 2
            enqueue_candidate(s_url, priority=seed_prio)

        def sort_candidate_list(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            def key_fn(it: dict[str, Any]) -> tuple[int, int, int, int]:
                prio = int(it.get("priority", 1))
                ct = int(it.get("ctime") or 0)
                has_no_ts = 0 if prio == 0 else (0 if ct > 0 else 1)
                ts_sort = (-ct if self.opts.order == "newest" else ct) if ct > 0 else 0
                return (prio, has_no_ts, ts_sort, int(it.get("seq", 0)))

            return sorted(items, key=key_fn)

        def crawl_one_album() -> bool:
            while pending_albums:
                aid = pending_albums.popleft()
                if aid in crawled_albums:
                    continue
                crawled_albums.add(aid)
                try:
                    atitle, items = self._fetch_album(account.biz, aid)
                    self.log(f"  [Album] {atitle} ({aid}): {len(items)} articles")
                    albums_cache["albums"][aid] = {"album_id": aid, "title": atitle, "count": len(items)}
                    albums_cache["articles"].extend(items)
                    write_atomic_json(albums_cache_path, albums_cache)
                    for it in items:
                        enqueue_candidate(
                            it["url"],
                            title=it["title"],
                            ctime=it["create_time"],
                            cover=it.get("cover", ""),
                            albums=[it["album"]] if it.get("album") else [],
                            priority=1,
                        )
                    return True
                except Exception as exc:
                    self.log(f"  [Album Warning] {aid}: {redact_text(str(exc))}")
            return False

        # 4. Multi-hop BFS discovery + export loop
        hop = 0
        max_hops = max(1, int(self.opts.max_hops or 1)) if self.opts.deep_discover else 1

        while (pending_albums or pending_urls) and hop < max_hops:
            rem_top = remaining_budget()
            if rem_top is not None and rem_top <= 0:
                break

            hop += 1

            if self.opts.crawl_albums and account.biz:
                if rem_top is None:
                    while crawl_one_album():
                        pass
                else:
                    # On-demand album crawling: only fetch another album when candidate URLs run dry
                    while not pending_urls and pending_albums:
                        crawl_one_album()
            else:
                pending_albums.clear()

            if not pending_urls:
                break

            wave_queue = deque(sort_candidate_list(list(pending_urls)))
            pending_urls.clear()
            wave_total = len(wave_queue)
            processed_in_wave = 0
            self.log(f"Hop {hop}: {wave_total} candidate article(s) queued...")

            while wave_queue:
                rem_now = remaining_budget()
                if rem_now is not None and rem_now <= 0:
                    break

                chunk_size = min(len(wave_queue), 20 if rem_now is None else max(1, min(20, rem_now)))
                chunk = [wave_queue.popleft() for _ in range(chunk_size)]
                resolved_shorts = batch_resolve_short_urls(
                    self.weread if not self.opts.download_cached_only else None,
                    [it["url"] for it in chunk],
                )

                for item in chunk:
                    rem_item = remaining_budget()
                    if rem_item is not None and rem_item <= 0:
                        break

                    processed_in_wave += 1
                    orig_u = item["url"]
                    mi_pre = parse_mid_idx_from_url(orig_u)
                    sid_pre = extract_short_id(orig_u)
                    if (mi_pre and mi_pre in by_mid_idx) or (sid_pre and sid_pre in by_sid):
                        if mi_pre and mi_pre in by_mid_idx:
                            merge_album_tags(by_mid_idx[mi_pre], item["albums"])
                        continue

                    short_u, resolved_rid = resolved_shorts.get(orig_u, (orig_u, item["rid"]))
                    sid_res = extract_short_id(short_u)
                    if sid_res and sid_res in by_sid:
                        merge_album_tags(by_sid[sid_res], item["albums"])
                        continue

                    label = item["title"] or short_u[:64]
                    target_disp = f"/{self.opts.max_articles}" if self.opts.max_articles > 0 else ""
                    self.log(f"  [{processed_in_wave}/{wave_total} | exported={ok_total_count + 1}{target_disp}] Exporting: {label}")
                    meta, raw_html = self._export_single_article(
                        url=short_u,
                        articles_root=articles_root,
                        account=account,
                        strict_account_match=strict_account_match,
                        review_id=resolved_rid or item["rid"],
                        title_hint=item["title"],
                        ts_hint=item["ctime"],
                        author_hint=item["author"],
                        digest_hint=item["digest"],
                        cover_hint=item["cover"],
                        is_original_hint=item["is_original"],
                        extra_albums=item["albums"],
                    )

                    st = meta.get("status")
                    if st == "foreign_account_skipped":
                        continue
                    if st != "ok":
                        self.log(f"    -> {st}: {meta.get('error', '')}")
                        all_records.append(meta)
                        continue

                    # Deduplicate if redirected/resolved to an already-archived (mid, idx)
                    mi_real = tuple(meta["mid_idx"]) if meta.get("mid_idx") else None
                    if mi_real:
                        mi_key = (int(mi_real[0]), int(mi_real[1]))
                        if mi_key in by_mid_idx:
                            existing = by_mid_idx[mi_key]
                            merge_album_tags(existing, meta.get("albums"))
                            if existing.get("folder") != meta.get("folder") and meta.get("folder"):
                                shutil.rmtree(articles_root / meta["folder"], ignore_errors=True)
                            continue
                        by_mid_idx[mi_key] = meta

                    sid_real = extract_short_id(str(meta.get("source_url") or ""))
                    if sid_real:
                        by_sid[sid_real] = meta
                    if meta.get("id"):
                        by_rid[str(meta["id"])] = meta

                    all_records.append(meta)
                    ok_total_count += 1
                    downloaded_this_run += 1

                    rem_after = remaining_budget()
                    if self.opts.deep_discover and raw_html and (rem_after is None or rem_after > 0):
                        new_aids, new_urls = scan_html_for_discoveries(raw_html, target_biz=account.biz)
                        if self.opts.crawl_albums:
                            for na in new_aids:
                                if na not in crawled_albums:
                                    pending_albums.append(na)
                        if self.opts.crawl_inline_links:
                            for nu in new_urls:
                                enqueue_candidate(nu, priority=2)

                    if len(all_records) % 15 == 0:
                        build_archive_outputs(out_dir, account, all_records)

                    if rem_after is not None and rem_after <= 0:
                        break

                    if self.opts.article_delay > 0:
                        sleep_throttled(
                            avg_delay=self.opts.article_delay,
                            min_delay=max(0.15, self.opts.article_delay * 0.5),
                            max_delay=max(0.5, self.opts.article_delay * 2.2),
                        )

        # Finalize account metadata & archive manifests
        if account.biz or account.book_id or account.nickname:
            write_atomic_json(out_dir / "account_info.json", account.to_public_dict())

        manifest = build_archive_outputs(out_dir, account, all_records)
        # Persist sorted `index` back into per-article meta.json
        for rec in manifest["articles"]:
            if rec.get("status") == "ok" and rec.get("folder"):
                mp = articles_root / rec["folder"] / "meta.json"
                if mp.exists():
                    write_atomic_json(mp, rec)

        return manifest
