# wechat-mp-export

[中文文档](./README.md)

A generic, resumable **WeChat Official Account (微信公众号) full-history article & media exporter** designed for local offline archiving, Markdown static site migration (Hugo, Hexo, Astro, Obsidian), and optional **Halo 2.x** CMS import.

## Highlights

- **Multi-Source History Discovery**
  1. **WeRead Official QR Catalog**: Paginates recent Official Account chapters with automatic pacing, checkpoints, and silent token refresh.
  2. **Public WeChat Album (`appmsgalbum`) Crawling**: Discovers album IDs inside articles and paginates full album histories back to older years.
  3. **Multi-Hop Inline Link BFS**: Recursively discovers same-account historical links inside article bodies, batch-resolves captcha-free `/s/<short_id>` URLs, filters out foreign accounts, and deduplicates by `(mid, idx)` and `short_id`.
  4. **Zero-Login Mode (`--no-weread`)**: Export individual URLs, URL lists, public albums, or run multi-hop album/link discovery from a single seed article URL without scanning a QR code.
- **Complete Content & Asset Localization**
  - Supports standard `#js_content` posts, `js_share_source` share cards, and WeChat photo-gallery (`picture_page_info_list`) posts.
  - Multi-threaded Cookie-less / Referer-less CDN downloader with **magic-byte file signature detection** (`jpg`, `png`, `webp`, `gif`, `svg`, `avif`, `mp4`, `mp3`).
  - Converts `<mpvoice>` audio and WeChat video iframes into standard HTML5 `<audio>` and `<video>` elements and removes lazy-load visibility traps.
- **Privacy & Security by Design**
  - Credentials default to a user configuration directory outside the repository (`~/.config/wechat-mp-export` on Linux, `0700` dir / `0600` files, auto-generated `.gitignore`). Keep custom state directories outside repositories and archives too.
  - Manifests use archive-relative local file references and do not copy login sessions. Archives retain source account names, article URLs, authors, and content; review them separately before sharing.
  - Includes `wechat-mp-export audit` to scan directories for accidental credential or PII leaks.

## Installation

Requires Python **3.10+**.

```bash
git clone https://github.com/qiao-y/wechat-mp-export.git
cd wechat-mp-export

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Quick Start

```bash
# 1. Optional: Authenticate via WeChat scan for full catalog access
wechat-mp-export auth login

# 2. Export all articles & media using any sample article URL from the Official Account
wechat-mp-export export \
  --article-url "https://mp.weixin.qq.com/s/EXAMPLE_ARTICLE_ID" \
  --out-dir "./exports/demo-account"

# Limit total exported articles with -n / --max-articles (default order: newest first)
wechat-mp-export export \
  --article-url "https://mp.weixin.qq.com/s/EXAMPLE_ARTICLE_ID" \
  --max-articles 20 \
  --out-dir "./exports/demo-account"

# Or export without login using public seed / album discovery
wechat-mp-export export \
  --no-weread \
  --article-url "https://mp.weixin.qq.com/s/EXAMPLE_ARTICLE_ID" \
  --out-dir "./exports/demo-account"
```

### Optional Halo 2.x Import

```bash
export HALO_PAT="pat_xxxxxxxxxxxxxxxxxxxxxxxx"
wechat-mp-export import-halo \
  --bundle-dir "./exports/demo-account" \
  --url "https://blog.example.com" \
  --owner "target-author"
```

### Run Tests & Security Audit

```bash
python3 -B -m unittest discover -s tests -v
python3 -B -m wechat_mp_export audit .
```

The audit is a filename/text-pattern preflight, not proof that all personal data is absent. It skips Git history, selected cache directories, common binary formats, undecodable files, and files larger than 8 MiB. Also inspect hidden files, editor backups, build archives, and Git history before publishing. Keep login state and real article archives out of the upload; `.gitignore` does not remove data from tracked files or archives.

`DEFAULT_GUEST_TOKEN` is a shared guest-signing default from the upstream WeRead protocol implementation ([pinned source](https://github.com/x554960766/wechat-mp-tools/blob/68fab018e5d17b1c506e2cc105efc970b3cd99ac/backend/weread_signer.py#L20-L21)), not a personal QR-login token. Account access still requires private session credentials such as runtime `accessToken` and `refreshToken` values.

## License

[MIT License](./LICENSE)
