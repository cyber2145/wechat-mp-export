"""Command-line interface for `wechat-mp-export`."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from wechat_mp_export import __version__
from wechat_mp_export.auth import WeReadAuthError, WeReadClient
from wechat_mp_export.config import read_json, redact_text
from wechat_mp_export.exporter import AccountExporter, ExportOptions, build_archive_outputs
from wechat_mp_export.halo_importer import HaloImportOptions, import_bundle_to_halo
from wechat_mp_export.privacy_audit import audit_directory
from wechat_mp_export.resolver import AccountInfo, resolve_account, search_accounts
from wechat_mp_export.utils import sanitize_filename


def _non_negative_int(value: str) -> int:
    try:
        iv = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer value: {value!r}") from exc
    if iv < 0:
        raise argparse.ArgumentTypeError("value must be >= 0 (0 means unlimited)")
    return iv


def _add_common_state_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-dir",
        default="",
        help="Directory for local 0600 login state (default: ~/.config/wechat-mp-export)",
    )
    parser.add_argument(
        "--proxy",
        default="",
        help="Optional HTTP/HTTPS proxy URL (or set WECHAT_MP_EXPORT_PROXY)",
    )


def _make_weread_client(args: argparse.Namespace) -> WeReadClient:
    sdir = Path(args.state_dir).expanduser().resolve() if getattr(args, "state_dir", "") else None
    return WeReadClient(state_dir=sdir, proxy_url=(getattr(args, "proxy", "") or None))


def _cmd_auth(args: argparse.Namespace) -> int:
    client = _make_weread_client(args)
    action = args.auth_action

    if action == "status":
        st = client.get_status(show_raw_ids=bool(args.show_ids))
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0 if st.get("authenticated") else 1

    if action == "logout":
        client.logout()
        print("Cleared local WeRead session and temporary QR files.")
        return 0

    if action == "start":
        qr_out = Path(args.qr_output).expanduser().resolve() if args.qr_output else None
        info = client.start_qr_login(qr_output=qr_out)
        if not args.json:
            print(info["ascii_qr"])
            print(f"\nScan with WeChat within 5 minutes:")
            print(f"  QR image saved to: {info['qr_image']}")
            print(f"  Browser QR URL   : {info['qr_url']}")
            print("\nThen run `wechat-mp-export auth poll` to complete login.")
        else:
            print(json.dumps({k: v for k, v in info.items() if k != "ascii_qr"}, ensure_ascii=False, indent=2))
        return 0

    if action == "poll":
        res = client.poll_qr_login(
            timeout_sec=args.timeout,
            on_scanned=lambda: print("QR scanned! Please tap Confirm on your phone...", flush=True),
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    if action == "login":
        qr_out = Path(args.qr_output).expanduser().resolve() if args.qr_output else None
        info = client.start_qr_login(qr_output=qr_out)
        print(info["ascii_qr"])
        print(f"\nPlease scan the QR code above using WeChat (valid for 5 minutes).")
        print(f"  QR image file : {info['qr_image']}")
        print(f"  Official URL  : {info['qr_url']}\n", flush=True)
        res = client.poll_qr_login(
            timeout_sec=args.timeout,
            on_scanned=lambda: print("Scanned! Waiting for confirmation on phone...", flush=True),
        )
        print(f"Authenticated successfully (VID: {res['vid_masked']}). Credentials saved with mode 0600.")
        return 0

    if action == "import-env":
        vid = os.environ.get("WECHAT_MP_WEREAD_VID", "")
        atok = os.environ.get("WECHAT_MP_WEREAD_ACCESS_TOKEN", "")
        rtok = os.environ.get("WECHAT_MP_WEREAD_REFRESH_TOKEN", "")
        st = client.save_manual_session(vid=vid, access_token=atok, refresh_token=rtok)
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0

    return 1


def _cmd_search(args: argparse.Namespace) -> int:
    client = _make_weread_client(args)
    if not client.has_valid_session():
        print("Error: `search` requires WeRead authentication. Run `wechat-mp-export auth login` first.", file=sys.stderr)
        return 2
    kws = [args.keyword, *(args.alias or [])]
    results = search_accounts(client, kws, include_subscriptions=(not args.no_subscriptions))
    if args.json:
        print(json.dumps([r.to_public_dict() for r in results], ensure_ascii=False, indent=2))
        return 0
    if not results:
        print("No matching Official Accounts found.")
        return 1
    print(f"Found {len(results)} candidate Official Account(s):\n")
    for idx, acc in enumerate(results, 1):
        print(f"  [{idx}] {acc.nickname}")
        print(f"      bookId : {acc.book_id}")
        print(f"      __biz  : {acc.biz}")
        print(f"      source : {acc.matched_via}")
        if acc.intro:
            print(f"      intro  : {acc.intro[:90]}")
    return 0


def _read_url_list_file(path: Path) -> list[str]:
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            urls.append(s)
    return urls


def _cmd_export(args: argparse.Namespace) -> int:
    weread = None if args.no_weread else _make_weread_client(args)
    has_weread = bool(weread and weread.has_valid_session())

    explicit_urls: list[str] = list(args.url or [])
    if args.url_file:
        explicit_urls.extend(_read_url_list_file(Path(args.url_file).expanduser()))
    seed_urls: list[str] = [args.article_url] if args.article_url else []
    album_urls: list[str] = list(args.album_url or [])

    explicit_out = Path(args.out_dir).expanduser().resolve() if args.out_dir else None
    cached_acct_file = (explicit_out / "account_info.json") if explicit_out else None

    has_target_selector = bool(args.article_url or args.account or args.biz or args.book_id)
    if cached_acct_file and cached_acct_file.exists() and not has_target_selector:
        account = AccountInfo.from_dict(read_json(cached_acct_file, {}))
    elif has_target_selector:
        account = resolve_account(
            weread=weread if has_weread else None,
            article_url=args.article_url,
            biz=args.biz,
            book_id=args.book_id,
            account_query=args.account,
            aliases=args.alias,
            wechat_id=args.wechat_id,
            proxy_url=(args.proxy or None),
        )
    elif explicit_urls or seed_urls or album_urls:
        # Pure URL-list / album-URL export without a single named account selector
        account = AccountInfo(nickname=args.account or "", wechat_id=args.wechat_id or "")
    else:
        print(
            "Error: specify a target Official Account (`--article-url`, `--account`, `--biz`, `--book-id`) "
            "or one or more URLs (`--url`, `--url-file`, `--album-url`).",
            file=sys.stderr,
        )
        return 2

    if not explicit_out:
        folder_stem = sanitize_filename(account.nickname or account.wechat_id or account.book_id or "wechat-archive", 48)
        out_dir = (Path.cwd() / "exports" / folder_stem).resolve()
    else:
        out_dir = explicit_out

    deep = (not args.no_deep_discover) and bool(has_target_selector or album_urls or args.deep_discover)
    opts = ExportOptions(
        out_dir=out_dir,
        workers=args.workers,
        article_delay=args.delay,
        list_only=args.list_only,
        download_cached_only=args.download_cached,
        deep_discover=deep,
        crawl_albums=(not args.no_crawl_albums),
        crawl_inline_links=(not args.no_crawl_links),
        refresh_catalog=args.refresh_catalog,
        refresh_albums=args.refresh_albums,
        max_hops=args.max_hops,
        max_articles=args.max_articles,
        max_new=args.max_new,
        order=args.order,
        save_raw_html=(not args.no_raw_html),
        export_pdf=args.pdf,
        enable_filter=(not args.no_filter),
        filter_rules_path=Path(args.filter_rules).expanduser().resolve() if args.filter_rules else None,
        browser_fallback=args.browser_fallback,
        allow_external_links=args.allow_external_links or (not has_target_selector and not album_urls),
        retry_unavailable=args.retry_unavailable,
        slug_prefix=args.slug_prefix,
        proxy_url=(args.proxy or None),
    )

    if account.nickname or account.book_id:
        print(f"Target Official Account: {account.nickname or '(auto)'} ({account.book_id or account.biz})")
    if not has_weread and not args.no_weread and has_target_selector:
        print(
            "Note: No active WeRead login found; running public seed/album/link discovery mode. "
            "(Run `wechat-mp-export auth login` for full WeRead chapter catalog access.)"
        )

    exporter = AccountExporter(opts, weread=weread if has_weread else None)
    result = exporter.run(
        account=account,
        seed_urls=seed_urls,
        explicit_urls=explicit_urls,
        album_urls=album_urls,
        use_weread_catalog=(has_weread and not args.no_weread),
    )
    if args.list_only:
        print(f"Catalog saved to {out_dir / 'article_list_checkpoint.json'} ({result.get('listed', 0)} articles).")
        return 0

    summary = result.get("summary") or {}
    print(
        f"\nExport complete! {summary.get('exported_ok', 0)}/{summary.get('total_listed', 0)} articles archived "
        f"({summary.get('total_images', 0)} images, {summary.get('total_audios', 0)} audio, "
        f"{summary.get('total_videos', 0)} video, {summary.get('total_media_mb', 0)} MB) -> {out_dir}"
    )
    return 0


def _cmd_rebuild_index(args: argparse.Namespace) -> int:
    out_dir = Path(args.archive_dir).expanduser().resolve()
    articles_root = out_dir / "articles"
    if not articles_root.is_dir():
        print(f"Error: {articles_root} does not exist.", file=sys.stderr)
        return 2
    account = AccountInfo.from_dict(read_json(out_dir / "account_info.json", {}))
    records = []
    for mp in sorted(articles_root.glob("*/meta.json")):
        rec = read_json(mp)
        if isinstance(rec, dict):
            records.append(rec)
    manifest = build_archive_outputs(out_dir, account, records)
    print(f"Rebuilt archive index for {manifest['summary']['exported_ok']} articles in {out_dir}.")
    return 0


def _cmd_import_halo(args: argparse.Namespace) -> int:
    base_url = (args.url or os.environ.get("HALO_BASE_URL") or "").strip()
    if not base_url:
        print("Error: provide Halo site URL via `--url` or `HALO_BASE_URL`.", file=sys.stderr)
        return 2
    token = (args.token or os.environ.get("HALO_PAT") or os.environ.get("HALO_TOKEN") or "").strip()
    password = os.environ.get(args.password_env, "") if args.password_env else os.environ.get("HALO_PASSWORD", "")

    opts = HaloImportOptions(
        bundle_dir=Path(args.bundle_dir).expanduser().resolve(),
        base_url=base_url,
        token=token,
        username=args.username,
        password=password,
        owner=args.owner,
        category_display_name=args.category,
        category_slug=args.category_slug,
        group_display_name=args.group,
        policy_name=args.policy,
        visibility=args.visibility,
        publish=(not args.draft),
        limit=args.limit,
        dry_run=args.dry_run,
    )
    summary = import_bundle_to_halo(opts)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    target = Path(args.path).expanduser().resolve()
    if not target.exists():
        print(f"Error: path does not exist: {target}", file=sys.stderr)
        return 2
    findings = audit_directory(target)
    if not findings:
        print(f"Audit passed: 0 credential or PII findings in {target.name}/")
        return 0
    print(f"Audit found {len(findings)} potential issue(s):", file=sys.stderr)
    for f in findings:
        loc = f"{f.path}:{f.line}" if f.line else f.path
        print(f"  - [{f.kind}] {loc}: {f.detail}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wechat-mp-export",
        description="Generic WeChat Official Account (微信公众号) article & media exporter.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # auth
    p_auth = sub.add_parser("auth", help="Manage local WeChat / WeRead QR authentication")
    _add_common_state_args(p_auth)
    auth_sub = p_auth.add_subparsers(dest="auth_action", required=True)

    p_login = auth_sub.add_parser("login", help="Interactive QR code scan login in terminal")
    p_login.add_argument("--qr-output", default="", help="Optional path to save QR code PNG")
    p_login.add_argument("--timeout", type=int, default=300, help="Scan timeout in seconds (default: 300)")

    p_start = auth_sub.add_parser("start", help="Generate login QR code without blocking")
    p_start.add_argument("--qr-output", default="", help="Optional path to save QR code PNG")
    p_start.add_argument("--json", action="store_true", help="Print JSON metadata instead of terminal QR")

    p_poll = auth_sub.add_parser("poll", help="Poll pending QR code started by `auth start`")
    p_poll.add_argument("--timeout", type=int, default=300, help="Poll timeout in seconds (default: 300)")

    p_status = auth_sub.add_parser("status", help="Show masked authentication state")
    p_status.add_argument("--show-ids", action="store_true", help="Show unmasked VID and nickname")

    auth_sub.add_parser("logout", help="Delete local credentials and temporary QR files")
    auth_sub.add_parser("import-env", help="Import session from WECHAT_MP_WEREAD_* environment variables")
    p_auth.set_defaults(func=_cmd_auth)

    # search
    p_search = sub.add_parser("search", help="Search WeChat Official Accounts via WeRead")
    _add_common_state_args(p_search)
    p_search.add_argument("keyword", help="Official Account name or keyword")
    p_search.add_argument("--alias", action="append", default=[], help="Optional former name / alias")
    p_search.add_argument("--no-subscriptions", action="store_true", help="Skip scanning subscribed Official Accounts")
    p_search.add_argument("--json", action="store_true", help="Output JSON")
    p_search.set_defaults(func=_cmd_search)

    # export
    p_exp = sub.add_parser("export", help="Export Official Account articles and localized media")
    _add_common_state_args(p_exp)
    p_exp.add_argument("--article-url", default="", help="Any sample article URL from the target Official Account")
    p_exp.add_argument("--account", default="", help="Official Account name (search or display override)")
    p_exp.add_argument("--alias", action="append", default=[], help="Former Official Account name(s), repeatable")
    p_exp.add_argument("--wechat-id", default="", help="Optional WeChat ID (微信号) for archive metadata")
    p_exp.add_argument("--biz", default="", help="Target Official Account Base64 `__biz`")
    p_exp.add_argument("--book-id", default="", help="Target WeRead `MP_WXS_...` bookId")
    p_exp.add_argument("--url", action="append", default=[], help="Specific article URL(s) to export, repeatable")
    p_exp.add_argument("--url-file", default="", help="Text file with one article URL per line")
    p_exp.add_argument("--album-url", action="append", default=[], help="WeChat album URL(s) (`appmsgalbum`), repeatable")
    p_exp.add_argument("--out-dir", default="", help="Output archive directory (default: ./exports/<account>)")
    p_exp.add_argument("--no-weread", action="store_true", help="Run without WeRead API (public URL / album / BFS mode)")
    p_exp.add_argument("--deep-discover", action="store_true", help="Enable multi-hop album & inline link discovery")
    p_exp.add_argument("--no-deep-discover", action="store_true", help="Disable multi-hop album & inline link discovery")
    p_exp.add_argument("--no-crawl-albums", action="store_true", help="Skip crawling WeChat albums (`appmsgalbum`)")
    p_exp.add_argument("--no-crawl-links", action="store_true", help="Skip crawling inline article links")
    p_exp.add_argument("--refresh-catalog", action="store_true", help="Ignore cached WeRead catalog checkpoint")
    p_exp.add_argument("--refresh-albums", action="store_true", help="Re-crawl previously cached WeChat albums")
    p_exp.add_argument("--max-hops", type=int, default=10, help="Maximum BFS discovery hops (default: 10)")
    p_exp.add_argument("--workers", type=int, default=8, help="Concurrent CDN media download threads (default: 8)")
    p_exp.add_argument("--delay", type=float, default=0.65, help="Average delay in seconds between articles (default: 0.65)")
    p_exp.add_argument(
        "-n", "--max-articles", "--limit",
        dest="max_articles",
        type=_non_negative_int,
        default=0,
        metavar="N",
        help="Maximum total articles to export (0 = unlimited; stops once N articles are exported)",
    )
    p_exp.add_argument(
        "--max-new",
        dest="max_new",
        type=_non_negative_int,
        default=0,
        metavar="N",
        help="Maximum newly downloaded articles in this run (0 = unlimited)",
    )
    p_exp.add_argument(
        "--order",
        choices=["newest", "oldest"],
        default="newest",
        help="Priority order when selecting articles to export (default: newest)",
    )
    p_exp.add_argument("--list-only", action="store_true", help="Only resolve account and save catalog checkpoint")
    p_exp.add_argument("--download-cached", action="store_true", help="Export from cached catalog without querying WeRead")
    p_exp.add_argument("--no-raw-html", action="store_true", help="Do not save raw.html alongside cleaned outputs")
    p_exp.add_argument("--pdf", action="store_true", help="Also render article.pdf via local headless Chrome/Edge/Chromium")
    p_exp.add_argument("--no-filter", action="store_true", help="Disable QR/ad/banner HTML content filtering")
    p_exp.add_argument("--filter-rules", default="", help="Path to custom JSON content filter rules")
    p_exp.add_argument("--browser-fallback", action="store_true", help="Use Playwright Chromium fallback if HTTP hits captcha")
    p_exp.add_argument("--allow-external-links", action="store_true", help="Allow archiving inline links from other Official Accounts")
    p_exp.add_argument("--retry-unavailable", action="store_true", help="Retry articles previously marked deleted/unavailable")
    p_exp.add_argument("--slug-prefix", default="wx", help="Prefix for generated article slugs (default: wx)")
    p_exp.set_defaults(func=_cmd_export)

    # rebuild-index
    p_reb = sub.add_parser("rebuild-index", help="Regenerate manifest, CSV, index.html, and Halo bundle from articles/")
    p_reb.add_argument("archive_dir", help="Path to an existing exported archive directory")
    p_reb.set_defaults(func=_cmd_rebuild_index)

    # import-halo
    p_halo = sub.add_parser("import-halo", help="Idempotently import an exported archive into Halo 2.x")
    p_halo.add_argument("--bundle-dir", required=True, help="Exported archive directory containing halo_import_bundle.json")
    p_halo.add_argument("--url", default="", help="Halo base URL, e.g. https://blog.example.com (or HALO_BASE_URL)")
    p_halo.add_argument("--token", default="", help="Halo Personal Access Token (prefer HALO_PAT env var over CLI flag)")
    p_halo.add_argument("--username", default="", help="Halo admin username (fallback if not using PAT)")
    p_halo.add_argument("--password-env", default="HALO_PASSWORD", help="Env var holding Halo password (default: HALO_PASSWORD)")
    p_halo.add_argument("--owner", default="", help="Target Halo post owner username (defaults to authenticated user)")
    p_halo.add_argument("--category", default="", help="Override Halo category display name")
    p_halo.add_argument("--category-slug", default="", help="Override Halo category slug")
    p_halo.add_argument("--group", default="", help="Override Halo attachment group display name")
    p_halo.add_argument("--policy", default="", help="Halo storage policy name (auto-detected if omitted)")
    p_halo.add_argument("--visibility", choices=["PUBLIC", "PRIVATE"], default="PUBLIC", help="Post visibility")
    p_halo.add_argument("--draft", action="store_true", help="Create posts as drafts without publishing")
    p_halo.add_argument(
        "-n", "--max-articles", "--limit",
        dest="limit",
        type=_non_negative_int,
        default=0,
        metavar="N",
        help="Maximum number of posts to import (0 = all)",
    )
    p_halo.add_argument("--dry-run", action="store_true", help="Preview import counts without connecting or writing to Halo")
    p_halo.set_defaults(func=_cmd_import_halo)

    # audit
    p_audit = sub.add_parser("audit", help="Scan a directory for leaked credentials, tokens, or host PII")
    p_audit.add_argument("path", nargs="?", default=".", help="Directory to scan (default: current directory)")
    p_audit.set_defaults(func=_cmd_audit)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130
    except WeReadAuthError as exc:
        print(f"Authentication error: {redact_text(str(exc))}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Error: {redact_text(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
