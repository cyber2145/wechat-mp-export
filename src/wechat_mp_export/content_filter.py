"""Configurable HTML content filter for removing WeChat ad/QR/banner chrome safely."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from wechat_mp_export.config import read_json

DEFAULT_FILTER_RULES: dict[str, Any] = {
    "enabled": True,
    "remove_selectors": [
        "#js_pc_qr_code",
        ".qr_code_pc",
        ".reward_area",
        ".js_ad_link",
        ".rich_media_tool",
    ],
    "remove_text_keywords": [
        "点击上方蓝字",
        "长按识别二维码",
        "扫码关注",
        "点击蓝字关注",
    ],
    "remove_image_keywords": [
        "分割线",
        "小节标题",
        "二维码",
        "扫码",
    ],
    "banner_images": {
        "enabled": True,
        "max_ratio": 0.22,
        "min_width": 500,
    },
    "remove_empty_blocks": True,
    "max_text_loss": 0.20,
}


def merge_rules(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_rules(out[k], v)
        else:
            out[k] = v
    return out


def load_filter_rules(rules_path: Path | None = None) -> dict[str, Any]:
    if not rules_path:
        return dict(DEFAULT_FILTER_RULES)
    user_rules = read_json(rules_path)
    if isinstance(user_rules, dict):
        return merge_rules(DEFAULT_FILTER_RULES, user_rules)
    return dict(DEFAULT_FILTER_RULES)


def _image_width(img_tag: Any) -> int:
    for key in ("data-w", "width"):
        raw = img_tag.get(key)
        if not raw:
            continue
        try:
            return int(float(str(raw).replace("px", "").strip()))
        except (TypeError, ValueError):
            continue
    return 0


def _image_ratio(img_tag: Any) -> float | None:
    raw = img_tag.get("data-ratio")
    if raw:
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            pass
    w = _image_width(img_tag)
    h = 0
    for key in ("data-h", "height"):
        val = img_tag.get(key)
        if val:
            try:
                h = int(float(str(val).replace("px", "").strip()))
                break
            except (TypeError, ValueError):
                continue
    return (h / w) if (w > 0 and h > 0) else None


def _is_leaf_block(node: Any) -> bool:
    for child in node.find_all(True):
        if child.name in ("p", "div", "section", "article", "ul", "ol", "table"):
            return False
    return True


def filter_article_html(html_fragment: str, rules: dict[str, Any] | None = None) -> str:
    """Filter non-content WeChat widgets/banners with a max-text-loss safety rollback."""
    if not html_fragment:
        return html_fragment

    active_rules = rules if rules is not None else DEFAULT_FILTER_RULES
    if not active_rules.get("enabled", True):
        return html_fragment

    try:
        soup = BeautifulSoup(html_fragment, "html.parser")
    except Exception:
        return html_fragment

    before_len = len(soup.get_text("", strip=True))

    for selector in active_rules.get("remove_selectors") or []:
        try:
            for node in soup.select(selector):
                node.decompose()
        except Exception:
            continue

    banner_cfg = active_rules.get("banner_images") or {}
    banner_on = bool(banner_cfg.get("enabled", True))
    max_ratio = float(banner_cfg.get("max_ratio", 0.22) or 0.22)
    min_width = int(banner_cfg.get("min_width", 500) or 500)
    img_kws = [str(k).lower() for k in (active_rules.get("remove_image_keywords") or []) if k]

    for img in soup.find_all("img"):
        hay = " ".join(str(img.get(a) or "") for a in ("alt", "title", "src", "data-src")).lower()
        if img_kws and any(kw in hay for kw in img_kws):
            img.decompose()
            continue
        if banner_on:
            ratio = _image_ratio(img)
            width = _image_width(img)
            if ratio is not None and width >= min_width and 0 < ratio <= max_ratio:
                img.decompose()

    text_kws = [str(k) for k in (active_rules.get("remove_text_keywords") or []) if k]
    if text_kws:
        to_remove = []
        for node in soup.find_all(["p", "section", "blockquote", "li", "h1", "h2", "h3", "h4"]):
            txt = node.get_text(" ", strip=True)
            if not txt or len(txt) > 200 or not _is_leaf_block(node):
                continue
            if any(kw in txt for kw in text_kws):
                to_remove.append(node)
        for node in to_remove:
            node.decompose()

    if active_rules.get("remove_empty_blocks", True):
        empties = []
        for node in soup.find_all(["p", "section"]):
            if node.find(["img", "video", "iframe", "table", "audio", "mpvoice"]):
                continue
            if node.get_text(" ", strip=True):
                continue
            empties.append(node)
        for node in empties:
            node.decompose()

    max_loss = float(active_rules.get("max_text_loss", 0.20) or 0.0)
    after_len = len(soup.get_text("", strip=True))
    if before_len > 0 and max_loss > 0 and after_len < before_len * (1.0 - max_loss):
        return html_fragment

    return str(soup)
