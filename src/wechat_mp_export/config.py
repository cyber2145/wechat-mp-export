"""Configuration, private credential storage (0600), and secret redaction."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

APP_NAME = "wechat-mp-export"

_SECRET_FIELD = (
    r"(?:access[_-]?token|refresh[_-]?token|wx[_-]?access[_-]?token|id[_-]?token|"
    r"skey|wr[_-]?skey|wx[_-]?code|pass[_-]?ticket|wap[_-]?sid2|password|passwd|"
    r"token|api[_-]?key|client[_-]?secret)"
)

_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Error responses can be JSON or Python dict reprs. Consume the whole quoted
    # value, including spaces and escaped quotes, before handling bare values.
    (
        re.compile(
            rf'''((?<![\w])["']?(?:{_SECRET_FIELD}|authorization|cookie|set-cookie)["']?\s*[:=]\s*)'''
            r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''',
            re.I,
        ),
        r'\1"***REDACTED***"',
    ),
    (
        re.compile(
            rf'''((?<![\w])["']?{_SECRET_FIELD}["']?\s*[:=]\s*)([^"',\s}}&;]+)''',
            re.I,
        ),
        r"\1***REDACTED***",
    ),
    (re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.I), r"\1***REDACTED***"),
    (re.compile(r"\bpat_[A-Za-z0-9_-]{12,}\b"), "pat_***REDACTED***"),
    (re.compile(r"((?:wr_skey|pass_ticket|wap_sid2|key)=)[^;\s&\"']{6,}", re.I), r"\1***REDACTED***"),
)


def get_default_state_dir() -> Path:
    """Return user-scoped private state directory outside any project checkout."""
    env_override = (
        os.environ.get("WECHAT_MP_EXPORT_STATE_DIR")
        or os.environ.get("WECHAT_MP_EXPORT_HOME")
        or ""
    ).strip()
    if env_override:
        return Path(env_override).expanduser().resolve()

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else (Path.home() / "AppData" / "Roaming")
        return base / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME

    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else (Path.home() / ".config")
    return base / APP_NAME


def ensure_private_dir(path: Path) -> Path:
    """Create a private state directory (0700 on POSIX) with a protective .gitignore."""
    path = path.expanduser()
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    gitignore = path / ".gitignore"
    if not gitignore.exists():
        try:
            gitignore.write_text("*\n!.gitignore\n", encoding="utf-8")
        except OSError:
            pass
    return path


def write_private_json(path: Path, payload: Any) -> None:
    """Atomically write sensitive JSON data with strict 0600 permissions."""
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
    fd = os.open(str(tmp), flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
        if os.name != "nt":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def write_atomic_json(path: Path, payload: Any) -> None:
    """Atomically write non-secret JSON metadata/manifests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def read_json(path: Path, default: Any = None) -> Any:
    """Read UTF-8 JSON file or return default when missing/invalid."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def mask_secret(value: str | None, keep_prefix: int = 3, keep_suffix: int = 2) -> str:
    """Mask an identifier or secret for safe terminal display."""
    s = str(value or "").strip()
    if not s:
        return ""
    if len(s) <= keep_prefix + keep_suffix + 2:
        return s[:1] + "***"
    return f"{s[:keep_prefix]}***{s[-keep_suffix:]}"


def mask_url_credentials(url: str | None) -> str:
    """Strip userinfo credentials from proxy or HTTP URLs before logging."""
    if not url:
        return ""
    try:
        sp = urlsplit(url)
        if sp.username or sp.password:
            host = sp.hostname or ""
            netloc = f"***:***@{host}" + (f":{sp.port}" if sp.port else "")
            return urlunsplit((sp.scheme, netloc, sp.path, sp.query, sp.fragment))
    except Exception:
        pass
    return url


def redact_text(text: str | None) -> str:
    """Scrub tokens, cookies, PATs, and auth codes from arbitrary text."""
    if not text:
        return ""
    out = str(text)
    for pattern, repl in _REDACT_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def get_proxies(proxy_url: str | None = None) -> dict[str, str] | None:
    """Build a requests-compatible proxies dict from CLI option or environment."""
    raw = (proxy_url or os.environ.get("WECHAT_MP_EXPORT_PROXY") or "").strip()
    if not raw:
        return None
    return {"http": raw, "https": raw}
