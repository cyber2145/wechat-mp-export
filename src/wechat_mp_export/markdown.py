"""Convert cleaned WeChat HTML into GitHub-Flavored Markdown with YAML frontmatter."""

from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
import json
import re
from typing import Any


class HTMLToMarkdownParser(HTMLParser):
    """Lightweight HTML -> GitHub-Flavored Markdown converter."""

    def __init__(self, media_map: dict[str, str] | None = None, title_to_skip: str = "") -> None:
        super().__init__()
        self.media_map = media_map or {}
        self.title_to_skip = (title_to_skip or "").strip().lower()
        self.pieces: list[str] = []
        self.list_stack: list[tuple[str, int]] = []
        self.in_pre = False
        self.in_code = False
        self.in_blockquote = False
        self.blockquote_depth = 0
        self.in_table = False
        self.table_data: list[list[str]] = []
        self.current_row: list[str] = []
        self.current_cell: list[str] = []
        self.link_stack: list[str | None] = []
        self.bold_depth = 0
        self.italic_depth = 0
        self.strike_depth = 0
        self.heading_level = 0
        self.heading_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = {k: (v or "") for k, v in attrs}
        tag = tag.lower()

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.heading_level = int(tag[1])
            self.heading_text = []
        elif tag in ("p", "figure"):
            self._ensure_newline(2)
        elif tag == "figcaption":
            self._ensure_newline(1)
        elif tag == "br":
            self.pieces.append("\n")
        elif tag == "hr":
            self._ensure_newline(2)
            self.pieces.append("---\n\n")
        elif tag in ("strong", "b"):
            if self.bold_depth == 0:
                self.pieces.append("**")
            self.bold_depth += 1
        elif tag in ("em", "i"):
            if self.italic_depth == 0:
                self.pieces.append("*")
            self.italic_depth += 1
        elif tag in ("del", "s", "strike"):
            if self.strike_depth == 0:
                self.pieces.append("~~")
            self.strike_depth += 1
        elif tag == "code":
            if not self.in_pre:
                self.in_code = True
                self.pieces.append("`")
        elif tag == "pre":
            self._ensure_newline(2)
            self.in_pre = True
            lang = attr_dict.get("data-lang") or attr_dict.get("lang") or ""
            self.pieces.append(f"```{lang}\n")
        elif tag == "blockquote":
            self._ensure_newline(2)
            self.blockquote_depth += 1
            self.in_blockquote = True
            self.pieces.append("> ")
        elif tag == "ul":
            self._ensure_newline(2 if not self.list_stack else 1)
            self.list_stack.append(("ul", 0))
        elif tag == "ol":
            self._ensure_newline(2 if not self.list_stack else 1)
            self.list_stack.append(("ol", 0))
        elif tag == "li":
            self._ensure_newline(1)
            indent = "  " * (len(self.list_stack) - 1) if self.list_stack else ""
            if self.list_stack:
                ltype, count = self.list_stack[-1]
                if ltype == "ol":
                    count += 1
                    self.list_stack[-1] = (ltype, count)
                    prefix = f"{indent}{count}. "
                else:
                    prefix = f"{indent}- "
            else:
                prefix = "- "
            self.pieces.append(prefix)
        elif tag == "a":
            href = attr_dict.get("href", "").strip()
            if href and not href.startswith("javascript:") and href != "#":
                self.link_stack.append(href)
                self.pieces.append("[")
            else:
                self.link_stack.append(None)
        elif tag == "img":
            src = (attr_dict.get("src") or attr_dict.get("data-src") or "").strip()
            if src:
                local_src = self.media_map.get(src, src)
                alt = (attr_dict.get("alt") or attr_dict.get("data-title") or "").strip()
                self._ensure_newline(2)
                self.pieces.append(f"![{alt}]({local_src})\n\n")
        elif tag == "video":
            src = attr_dict.get("src", "").strip()
            if src:
                self._ensure_newline(2)
                self.pieces.append(f"[Video: {src}]({src})\n\n")
        elif tag == "audio":
            src = attr_dict.get("src", "").strip()
            if src:
                self._ensure_newline(1)
                self.pieces.append(f"[Audio: {src}]({src})\n\n")
        elif tag == "table":
            self._ensure_newline(2)
            self.in_table = True
            self.table_data = []
        elif tag == "tr" and self.in_table:
            self.current_row = []
        elif tag in ("th", "td") and self.in_table:
            self.current_cell = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = self.heading_level or 1
            text = "".join(self.heading_text).strip()
            if self.title_to_skip and text.lower() == self.title_to_skip and len(self.pieces) < 5:
                pass
            elif text:
                self._ensure_newline(2)
                self.pieces.append(f"{'#' * level} {text}\n\n")
            self.heading_level = 0
            self.heading_text = []
        elif tag in ("p", "figure"):
            self._ensure_newline(2)
        elif tag == "figcaption":
            self._ensure_newline(1)
        elif tag in ("strong", "b"):
            self.bold_depth = max(0, self.bold_depth - 1)
            if self.bold_depth == 0:
                self.pieces.append("**")
        elif tag in ("em", "i"):
            self.italic_depth = max(0, self.italic_depth - 1)
            if self.italic_depth == 0:
                self.pieces.append("*")
        elif tag in ("del", "s", "strike"):
            self.strike_depth = max(0, self.strike_depth - 1)
            if self.strike_depth == 0:
                self.pieces.append("~~")
        elif tag == "code":
            if not self.in_pre:
                self.pieces.append("`")
                self.in_code = False
        elif tag == "pre":
            self.pieces.append("\n```\n\n")
            self.in_pre = False
        elif tag == "blockquote":
            self.blockquote_depth = max(0, self.blockquote_depth - 1)
            if self.blockquote_depth == 0:
                self.in_blockquote = False
            self._ensure_newline(2)
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
            self._ensure_newline(2)
        elif tag == "li":
            self._ensure_newline(1)
        elif tag == "a":
            if self.link_stack:
                href = self.link_stack.pop()
                if href:
                    self.pieces.append(f"]({href})")
        elif tag == "table" and self.in_table:
            self.in_table = False
            self._render_table()
            self._ensure_newline(2)
        elif tag == "tr" and self.in_table:
            self.table_data.append(self.current_row)
            self.current_row = []
        elif tag in ("th", "td") and self.in_table:
            cell_txt = "".join(self.current_cell).strip().replace("|", "\\|").replace("\n", " ")
            self.current_row.append(cell_txt)
            self.current_cell = []

    def handle_data(self, data: str) -> None:
        if self.in_table:
            self.current_cell.append(data)
            return
        if self.heading_level > 0:
            self.heading_text.append(data)
            return
        if self.in_pre:
            self.pieces.append(data)
            return
        clean = data if self.in_code else re.sub(r"[ \t]+", " ", data)
        if self.in_blockquote:
            clean = "\n> ".join(clean.split("\n"))
        self.pieces.append(clean)

    def _ensure_newline(self, count: int = 1) -> None:
        trailing = 0
        idx = len(self.pieces) - 1
        while idx >= 0:
            txt = self.pieces[idx]
            for ch in reversed(txt):
                if ch == "\n":
                    trailing += 1
                elif ch in (" ", "\t", "\r"):
                    continue
                else:
                    idx = -1
                    break
            idx -= 1
        if count > trailing:
            self.pieces.append("\n" * (count - trailing))

    def _render_table(self) -> None:
        if not self.table_data:
            return
        cols = max(len(r) for r in self.table_data)
        if cols == 0:
            return
        padded = [r + [""] * (cols - len(r)) for r in self.table_data]
        lines = [
            "| " + " | ".join(padded[0]) + " |",
            "| " + " | ".join(["---"] * cols) + " |",
        ]
        for row in padded[1:]:
            lines.append("| " + " | ".join(row) + " |")
        self.pieces.append("\n" + "\n".join(lines) + "\n\n")

    def get_markdown(self) -> str:
        raw = unescape("".join(self.pieces))
        out_lines: list[str] = []
        in_fence = False
        for line in raw.split("\n"):
            if line.strip().startswith("```"):
                in_fence = not in_fence
                out_lines.append(line.strip())
                continue
            if in_fence:
                out_lines.append(line)
                continue
            stripped = line.strip()
            if stripped:
                if stripped.startswith(">"):
                    q = stripped.lstrip(">").strip()
                    if q:
                        out_lines.append(f"> {q}")
                else:
                    lead = len(line) - len(line.lstrip(" "))
                    is_list = stripped.startswith(("-", "*")) or bool(re.match(r"^\d+\.\s", stripped))
                    indent = " " * (lead if (lead > 1 and is_list) else 0)
                    out_lines.append(indent + stripped)
            elif not out_lines or out_lines[-1] != "":
                out_lines.append("")
        return "\n".join(out_lines).strip()


def html_to_markdown(html_fragment: str, media_map: dict[str, str] | None = None, title_to_skip: str = "") -> str:
    """Convert cleaned WeChat HTML fragment into clean Markdown."""
    cleaned = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "", html_fragment, flags=re.I)
    cleaned = re.sub(r"<style\b[^>]*>[\s\S]*?</style>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<noscript\b[^>]*>[\s\S]*?</noscript>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<svg\b[^>]*>[\s\S]*?</svg>", "", cleaned, flags=re.I)
    # Treat WeChat <section> blocks as paragraphs for clean line breaks
    cleaned = re.sub(r"<section\b[^>]*>", "<p>", cleaned, flags=re.I)
    cleaned = re.sub(r"</section>", "</p>", cleaned, flags=re.I)

    parser = HTMLToMarkdownParser(media_map=media_map, title_to_skip=title_to_skip)
    parser.feed(cleaned)
    return parser.get_markdown()


def build_frontmatter_markdown(
    *,
    title: str,
    slug: str,
    date_iso: str,
    author: str,
    account_name: str,
    tags: list[str],
    cover: str,
    excerpt: str,
    source_url: str,
    review_id: str = "",
    body_markdown: str,
) -> str:
    """Wrap Markdown body with standard YAML frontmatter compatible with Hugo/Hexo/Astro/Halo."""
    categories = [account_name] if account_name else []
    lines = [
        "---",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"slug: {json.dumps(slug, ensure_ascii=False)}",
        f"date: {json.dumps(date_iso, ensure_ascii=False)}",
        f"author: {json.dumps(author, ensure_ascii=False)}",
        f"categories: {json.dumps(categories, ensure_ascii=False)}",
        f"tags: {json.dumps(tags, ensure_ascii=False)}",
        f"cover: {json.dumps(cover, ensure_ascii=False)}",
        f"excerpt: {json.dumps(excerpt, ensure_ascii=False)}",
        f"wechat_source_url: {json.dumps(source_url, ensure_ascii=False)}",
    ]
    if review_id:
        lines.append(f"wechat_review_id: {json.dumps(review_id, ensure_ascii=False)}")
    lines.extend(["---", "", body_markdown, ""])
    return "\n".join(lines)
