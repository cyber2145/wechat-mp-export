"""Security & PII preflight auditor for source repositories and exported archives."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re

_FORBIDDEN_FILE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|[_-])session\.json$", re.I),
    re.compile(r"^auth_status\.json$", re.I),
    re.compile(r"^account_pool\.json$", re.I),
    re.compile(r"credentials.*\.json$", re.I),
    re.compile(r"^\.env(?:\..+)?$"),
    re.compile(r"\.(?:pem|key|p12|pfx)$", re.I),
    re.compile(r"^id_(?:rsa|ed25519|ecdsa)(?:\.pub)?$", re.I),
    re.compile(r"login_qr.*\.png$", re.I),
    re.compile(r"\.(?:swp|swo|swn|bak|orig)$", re.I),
    re.compile(r"~$"),
)

_ALLOWED_ENV_EXAMPLES = frozenset({".env.example", ".env.sample", ".env.template"})

_SECRET_CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("halo_pat_token", re.compile(r"\bpat_[A-Za-z0-9_-]{20,}\b")),
    ("github_pat_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "json_credential_value",
        re.compile(
            r'"(?:accessToken|refreshToken|wxAccessToken|wr_skey|pass_ticket|wap_sid2)"\s*:\s*"(?![*<])[^"\s]{8,}"'
        ),
    ),
    (
        "cookie_credential_value",
        re.compile(r"(?:wr_skey|wap_sid2|wxtokenkey|pass_ticket)=(?![*<])[A-Za-z0-9._~+/=-]{10,}"),
    ),
    (
        "posix_user_home_path",
        re.compile(r"/(?:home|Users)/(?!user\b|username\b|example\b|runner\b)[A-Za-z0-9._-]+/"),
    ),
    (
        "windows_user_home_path",
        re.compile(r"[A-Za-z]:\\Users\\(?!user\b|username\b|example\b|Public\b)[A-Za-z0-9._-]+\\", re.I),
    ),
    (
        "ssh_key_reference",
        re.compile(r"\.ssh/id_(?:rsa|ed25519|ecdsa)\b"),
    ),
    (
        "non_loopback_ip_url",
        re.compile(r"https?://(?!127\.0\.0\.1\b|0\.0\.0\.0\b)(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"),
    ),
    (
        "email_address",
        re.compile(r"\b[A-Za-z0-9._%+-]+@(?!([A-Za-z0-9-]+\.)*example\.(?:com|org|net)\b|users\.noreply\.github\.com\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ),
)

_SKIP_DIRS = frozenset({".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache", ".mypy_cache"})
_BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".heic",
    ".mp4", ".mov", ".m4v", ".webm", ".mp3", ".m4a", ".wav", ".ogg",
    ".pdf", ".zip", ".tar", ".gz", ".woff", ".woff2", ".ttf", ".pyc",
})


@dataclass
class AuditFinding:
    kind: str
    path: str
    line: int
    detail: str


def audit_directory(root: Path) -> list[AuditFinding]:
    """Scan `root` for credential files, leaked tokens/keys, IPs, emails, or local OS user home paths."""
    root = root.resolve()
    findings: list[AuditFinding] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            rel = fpath.relative_to(root).as_posix()

            if fname not in _ALLOWED_ENV_EXAMPLES:
                for fpat in _FORBIDDEN_FILE_PATTERNS:
                    if fpat.search(fname):
                        findings.append(
                            AuditFinding(
                                kind="sensitive_filename",
                                path=rel,
                                line=0,
                                detail=f"Potentially sensitive file present: {fname}",
                            )
                        )
                        break

            if fpath.suffix.lower() in _BINARY_SUFFIXES:
                continue

            try:
                if fpath.stat().st_size > 8 * 1024 * 1024:
                    continue
                text = fpath.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue

            for lineno, line in enumerate(text.splitlines(), 1):
                for label, spat in _SECRET_CONTENT_PATTERNS:
                    m = spat.search(line)
                    if not m:
                        continue
                    matched = m.group(0)
                    if label == "halo_pat_token" and set(matched.removeprefix("pat_")) <= {"x", "X", "*"}:
                        continue
                    findings.append(
                        AuditFinding(
                            kind=label,
                            path=rel,
                            line=lineno,
                            detail=f"Matched `{label}` pattern",
                        )
                    )
    return findings
