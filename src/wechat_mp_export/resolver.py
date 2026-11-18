"""Generic Official Account resolution via article URL, __biz, bookId, or keyword search."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import urllib.parse
from typing import Any

from wechat_mp_export.auth import WeReadClient
from wechat_mp_export.config import get_proxies
from wechat_mp_export.http_client import check_wechat_risk_control, get_data_client, get_wechat_mobile_headers
from wechat_mp_export.utils import (
    biz_to_book_id,
    book_id_to_biz,
    clean_wechat_article_url,
    extract_account_meta_from_html,
    extract_biz_from_url,
)


@dataclass
class AccountInfo:
    nickname: str = ""
    wechat_id: str = ""
    biz: str = ""
    book_id: str = ""
    intro: str = ""
    cover: str = ""
    aliases: list[str] = field(default_factory=list)
    matched_via: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize Official Account metadata (contains zero user credentials)."""
        data = asdict(self)
        data["bookId"] = self.book_id
        data["fakeid"] = self.biz
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AccountInfo":
        biz = str(raw.get("biz") or raw.get("fakeid") or "").strip()
        book_id = str(raw.get("book_id") or raw.get("bookId") or "").strip()
        if biz and not book_id:
            book_id = biz_to_book_id(biz)
        if book_id and not biz:
            biz = book_id_to_biz(book_id)
        return cls(
            nickname=str(raw.get("nickname") or raw.get("name") or "").strip(),
            wechat_id=str(raw.get("wechat_id") or "").strip(),
            biz=biz,
            book_id=book_id,
            intro=str(raw.get("intro") or "").strip(),
            cover=str(raw.get("cover") or "").strip(),
            aliases=list(raw.get("aliases") or []),
            matched_via=str(raw.get("matched_via") or "").strip(),
        )


def _fetch_public_article_html_once(url: str, proxy_url: str | None = None) -> str:
    clean_u = clean_wechat_article_url(url)
    if not clean_u:
        return ""
    client = get_data_client()
    client.cookies.clear()
    try:
        resp = client.get(
            clean_u,
            headers=get_wechat_mobile_headers(),
            proxies=get_proxies(proxy_url),
            timeout=20,
            allow_redirects=True,
        )
        if resp.status_code == 200:
            text = resp.content.decode("utf-8", errors="replace")
            blocked, _ = check_wechat_risk_control(text, resp.url)
            if not blocked:
                return text
    except Exception:
        pass
    return ""


def search_accounts(
    weread: WeReadClient,
    keywords: list[str],
    include_subscriptions: bool = True,
) -> list[AccountInfo]:
    """Search Official Accounts across the user's WeRead subscriptions and global MP store."""
    clean_kws = [k.strip() for k in keywords if k and k.strip()]
    if not clean_kws:
        return []

    candidates: list[AccountInfo] = []
    seen_book_ids: set[str] = set()

    def add_candidate(
        book_id: str,
        name: str,
        intro: str = "",
        cover: str = "",
        source: str = "",
    ) -> None:
        bid = biz_to_book_id(book_id)
        if not bid or bid in seen_book_ids:
            return
        seen_book_ids.add(bid)
        candidates.append(
            AccountInfo(
                nickname=(name or "").strip(),
                biz=book_id_to_biz(bid),
                book_id=bid,
                intro=(intro or "").strip(),
                cover=(cover or "").strip(),
                matched_via=source,
            )
        )

    if include_subscriptions:
        for list_type in (0, 1):
            offset = 0
            for _ in range(10):
                u = (
                    f"https://i.weread.qq.com/mp/list?listType={list_type}&count=50&synckey=0"
                    + (f"&offset={offset}" if offset > 0 else "")
                )
                try:
                    data = weread.get_json(u, max_attempts=2)
                except Exception:
                    break
                items = data.get("lists") or data.get("items") or []
                if not items:
                    break
                for it in items:
                    b_info = it.get("bookInfo", it)
                    bid = str(b_info.get("bookId") or it.get("bookId") or "")
                    title = str(it.get("account") or b_info.get("title") or it.get("title") or "")
                    intro = str(b_info.get("intro") or "")
                    cover = str(it.get("mpavatar") or it.get("thumbUrl") or b_info.get("cover") or "")
                    haystack = f"{title} {intro}".lower()
                    if any(k.lower() in haystack for k in clean_kws):
                        add_candidate(bid, title, intro, cover, f"subscription(type={list_type})")
                if len(items) < 50:
                    break
                offset += len(items)

        try:
            shelf = weread.get_json(
                "https://i.weread.qq.com/shelf/sync?synckey=0&teenmode=0&album=1&onlyBookid=0&localBookCount=0",
                max_attempts=2,
            )
            for b in shelf.get("books") or []:
                bid = str(b.get("bookId") or "")
                if not bid.startswith("MP_"):
                    continue
                title = str(b.get("title") or "")
                intro = str(b.get("intro") or "")
                if any(k.lower() in f"{title} {intro}".lower() for k in clean_kws):
                    add_candidate(bid, title, intro, str(b.get("cover") or ""), "shelf")
        except Exception:
            pass

    for kw in clean_kws:
        enc = urllib.parse.quote(kw)
        for endpoint in (
            f"https://i.weread.qq.com/store/search?keyword={enc}&scope=2&v=2&count=20",
            f"https://i.weread.qq.com/store/search?keyword={enc}&v=2&count=20",
        ):
            try:
                data = weread.get_json(endpoint, max_attempts=3)
            except Exception:
                continue
            for entry in data.get("books") or []:
                b_info = entry.get("bookInfo") or {}
                bid = str(b_info.get("bookId") or "")
                is_mp = bid.startswith("MP_") or b_info.get("type") == 3 or b_info.get("author") == "公众号"
                if not is_mp:
                    continue
                add_candidate(
                    bid,
                    str(b_info.get("title") or ""),
                    str(b_info.get("intro") or ""),
                    str(b_info.get("cover") or ""),
                    f"search:{kw}",
                )

    primary_kw = clean_kws[0].lower()
    alias_kws = [k.lower() for k in clean_kws[1:]]

    def rank_key(item: AccountInfo) -> tuple[int, int, int]:
        name_l = item.nickname.lower()
        exact = 3 if name_l == primary_kw else (2 if name_l in alias_kws else (1 if primary_kw in name_l else 0))
        sub = 1 if item.matched_via.startswith(("subscription", "shelf")) else 0
        has_intro = 1 if item.intro else 0
        return (exact, sub, has_intro)

    candidates.sort(key=rank_key, reverse=True)
    return candidates


def resolve_account(
    *,
    weread: WeReadClient | None = None,
    article_url: str = "",
    biz: str = "",
    book_id: str = "",
    account_query: str = "",
    aliases: list[str] | None = None,
    wechat_id: str = "",
    proxy_url: str | None = None,
) -> AccountInfo:
    """Resolve target Official Account metadata via any supported identifier."""
    alias_list = [a.strip() for a in (aliases or []) if a and a.strip()]
    has_weread = bool(weread and weread.has_valid_session())

    def enrich_from_weread(info: AccountInfo) -> AccountInfo:
        if has_weread and info.book_id:
            try:
                book = weread.get_json(f"https://i.weread.qq.com/book/info?bookId={info.book_id}", max_attempts=3)
                info.nickname = info.nickname or str(book.get("title") or "").strip()
                info.intro = info.intro or str(book.get("intro") or "").strip()
                info.cover = info.cover or str(book.get("cover") or "").strip()
            except Exception:
                pass
        if wechat_id and not info.wechat_id:
            info.wechat_id = wechat_id.strip()
        for al in alias_list:
            if al not in info.aliases:
                info.aliases.append(al)
        return info

    # 1. Explicit bookId or __biz
    if book_id or biz:
        bid = biz_to_book_id(book_id or biz)
        resolved_biz = book_id_to_biz(bid) or biz.strip()
        info = AccountInfo(
            nickname=account_query.strip(),
            wechat_id=wechat_id.strip(),
            biz=resolved_biz,
            book_id=bid,
            aliases=list(alias_list),
            matched_via="book_id" if book_id else "biz",
        )
        return enrich_from_weread(info)

    # 2. Sample article URL (works both with and without WeRead login!)
    if article_url:
        url_biz = extract_biz_from_url(article_url)
        html_meta: dict[str, str] = {}
        raw_html = _fetch_public_article_html_once(article_url, proxy_url=proxy_url)
        if raw_html:
            html_meta = extract_account_meta_from_html(raw_html)

        resolved_bid = ""
        if url_biz:
            resolved_bid = biz_to_book_id(url_biz)
        elif html_meta.get("bookId"):
            resolved_bid = html_meta["bookId"]
        elif has_weread:
            try:
                res = weread.post_json("https://i.weread.qq.com/mp/getreviewid", {"urls": [clean_wechat_article_url(article_url)]})
                resolved_bid = str(
                    res.get("bookId")
                    or ((res.get("reviewIds") or [{}])[0].get("bookId") or "")
                ).strip()
            except Exception:
                pass

        resolved_biz = book_id_to_biz(resolved_bid) or url_biz or html_meta.get("biz", "")
        info = AccountInfo(
            nickname=(account_query.strip() or html_meta.get("nickname", "")),
            wechat_id=(wechat_id.strip() or html_meta.get("wechat_id", "")),
            biz=resolved_biz,
            book_id=resolved_bid,
            intro=html_meta.get("intro", ""),
            aliases=list(alias_list),
            matched_via="article_url",
        )
        info = enrich_from_weread(info)
        if not info.biz and not info.book_id and not info.nickname:
            raise RuntimeError(
                "Could not extract Official Account identifier from `--article-url`. "
                "Check that the article URL is valid and publicly accessible, or pass `--biz` / `--book-id`."
            )
        return info

    # 3. Keyword search via WeRead
    if account_query:
        if not has_weread or weread is None:
            raise RuntimeError(
                "Searching an Official Account by name (`--account`) requires WeRead QR authentication. "
                "Either run `wechat-mp-export auth login` first, or pass `--article-url` / `--biz`."
            )
        kws = [account_query.strip(), *alias_list]
        if wechat_id.strip():
            kws.append(wechat_id.strip())
        matches = search_accounts(weread, kws, include_subscriptions=True)
        if not matches:
            raise RuntimeError(
                f"No Official Account matching '{account_query}' was found in subscriptions or search. "
                "Tip: pass `--article-url <any_article_link>` from that Official Account for exact resolution."
            )
        chosen = matches[0]
        return enrich_from_weread(chosen)

    raise ValueError(
        "Please specify a target Official Account using `--article-url`, `--account`, `--biz`, `--book-id`, or `--url`."
    )
