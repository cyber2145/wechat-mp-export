"""Generic, idempotent Halo 2.x importer for archives produced by `wechat-mp-export`."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import getpass
import hashlib
import http.cookiejar
from html.parser import HTMLParser
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request
import uuid

from wechat_mp_export.config import read_json, redact_text, write_atomic_json


@dataclass
class HaloImportOptions:
    bundle_dir: Path
    base_url: str
    token: str = ""
    username: str = ""
    password: str = ""
    owner: str = ""
    category_display_name: str = ""
    category_slug: str = ""
    group_display_name: str = ""
    policy_name: str = ""
    visibility: str = "PUBLIC"
    publish: bool = True
    limit: int = 0
    dry_run: bool = False


class _FormInputsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = {k: (v or "") for k, v in attrs}
        if tag.lower() == "input" and d.get("name"):
            self.values[d["name"]] = d.get("value", "")


def _rsa_encrypt_pkcs1_v15(public_key_b64: str, plaintext: str) -> bytes:
    pem = (
        "-----BEGIN PUBLIC KEY-----\n"
        + "\n".join(public_key_b64[i:i + 64] for i in range(0, len(public_key_b64), 64))
        + "\n-----END PUBLIC KEY-----\n"
    )
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        pub = serialization.load_pem_public_key(pem.encode("ascii"))
        return pub.encrypt(plaintext.encode("utf-8"), padding.PKCS1v15())
    except ImportError:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".pem") as pk_file:
            pk_file.write(pem)
            pk_file.flush()
            return subprocess.run(
                [
                    "openssl",
                    "pkeyutl",
                    "-encrypt",
                    "-pubin",
                    "-inkey",
                    pk_file.name,
                    "-pkeyopt",
                    "rsa_padding_mode:pkcs1",
                ],
                input=plaintext.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            ).stdout


def to_utc_iso(iso_str: str) -> str:
    """Convert any ISO-8601 timestamp to Halo-compatible UTC `YYYY-MM-DDTHH:MM:SSZ`."""
    raw = (iso_str or "").strip()
    if not raw:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return raw


def _slugify_id(prefix: str, text: str) -> str:
    ascii_part = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if len(ascii_part) >= 3:
        return f"{prefix}-{ascii_part[:32]}"
    digest = hashlib.sha1((text or "default").encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{digest}"


class HaloClient:
    """Halo 2.x REST API client supporting Personal Access Tokens (PAT) or session login."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        username: str = "",
        password: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("Halo `--url` must start with http:// or https://")
        self.token = token.strip()
        self.username = username.strip()
        self.password = password
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        base_headers = [
            ("Accept", "application/json"),
            ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8"),
            ("User-Agent", "wechat-mp-export/0.1"),
        ]
        if self.token:
            base_headers.append(("Authorization", f"Bearer {self.token}"))
        self.opener.addheaders = base_headers
        self.authenticated_user = ""

        if not self.token:
            if not self.username or not self.password:
                raise ValueError(
                    "Halo authentication requires either `--token` / `HALO_PAT` (recommended) "
                    "or `--username` with `HALO_PASSWORD`."
                )
            self._login_form()
        else:
            self.authenticated_user = self._verify_current_user()

    def _verify_current_user(self) -> str:
        _, me = self.request("GET", "/apis/api.console.halo.run/v1alpha1/users/-", retry_auth=False)
        uname = (
            ((me or {}).get("user") or {}).get("metadata", {}).get("name")
            or (me or {}).get("metadata", {}).get("name")
            or ""
        )
        if not uname or uname == "anonymousUser":
            raise RuntimeError("Halo authentication failed (anonymous user returned). Check token/credentials.")
        return str(uname)

    def _login_form(self) -> None:
        login_page = self.opener.open(f"{self.base_url}/login?language=zh-CN", timeout=30).read().decode("utf-8")
        parser = _FormInputsParser()
        parser.feed(login_page)
        m = re.search(r'const\s+publicKey\s*=\s*("[^"]+")', login_page)
        if not m:
            raise RuntimeError("Could not find RSA publicKey on Halo `/login` page; prefer using `--token` (PAT).")
        pub_key = json.loads(m.group(1))
        encrypted_pw = _rsa_encrypt_pkcs1_v15(pub_key, self.password)
        form = {
            "username": self.username,
            "password": base64.b64encode(encrypted_pw).decode("ascii"),
            "_csrf": parser.values.get("_csrf", ""),
        }
        req = urllib.request.Request(
            f"{self.base_url}/login",
            data=urllib.parse.urlencode(form).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        self.opener.open(req, timeout=30).read()
        self.authenticated_user = self._verify_current_user()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: int = 60,
        retry_auth: bool = True,
    ) -> tuple[int, Any]:
        req_headers = dict(headers or {})
        payload_bytes: bytes | None = None
        if body is not None:
            if isinstance(body, (dict, list)):
                payload_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
                req_headers.setdefault("Content-Type", "application/json; charset=utf-8")
            elif isinstance(body, bytes):
                payload_bytes = body
            else:
                payload_bytes = str(body).encode("utf-8")

        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=payload_bytes,
            headers=req_headers,
            method=method.upper(),
        )
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            if exc.code in (401, 403) and retry_auth and not self.token and self.username:
                self._login_form()
                return self.request(method, path, body=body, headers=headers, timeout=timeout, retry_auth=False)
            raise RuntimeError(f"Halo HTTP {method} {path} -> {exc.code}: {redact_text(err_body[:300])}") from exc

    def resolve_storage_policy(self, preferred: str = "") -> str:
        if preferred:
            return preferred
        try:
            _, data = self.request("GET", "/apis/storage.halo.run/v1alpha1/policies?size=100")
            names = [it["metadata"]["name"] for it in (data or {}).get("items") or [] if it.get("metadata", {}).get("name")]
            if "default-policy" in names:
                return "default-policy"
            if names:
                return names[0]
        except Exception:
            pass
        return "default-policy"

    def ensure_group(self, display_name: str) -> str:
        group_id = _slugify_id("wechat", display_name)
        _, data = self.request("GET", "/apis/storage.halo.run/v1alpha1/groups?size=1000")
        for it in (data or {}).get("items") or []:
            if it["metadata"]["name"] == group_id or (it.get("spec") or {}).get("displayName") == display_name:
                return str(it["metadata"]["name"])
        _, created = self.request(
            "POST",
            "/apis/storage.halo.run/v1alpha1/groups",
            body={
                "apiVersion": "storage.halo.run/v1alpha1",
                "kind": "Group",
                "metadata": {"name": group_id},
                "spec": {"displayName": display_name},
            },
        )
        return str(created["metadata"]["name"])

    def ensure_category(self, display_name: str, slug: str = "") -> str:
        cat_slug = slug or _slugify_id("cat", display_name).removeprefix("cat-")
        cat_id = f"cat-{cat_slug}"[:48]
        _, data = self.request("GET", "/apis/content.halo.run/v1alpha1/categories?size=1000")
        for it in (data or {}).get("items") or []:
            spec = it.get("spec") or {}
            if spec.get("displayName") == display_name or spec.get("slug") == cat_slug:
                return str(it["metadata"]["name"])
        _, created = self.request(
            "POST",
            "/apis/content.halo.run/v1alpha1/categories",
            body={
                "apiVersion": "content.halo.run/v1alpha1",
                "kind": "Category",
                "metadata": {"name": cat_id},
                "spec": {
                    "displayName": display_name,
                    "slug": cat_slug,
                    "description": f"{display_name} archive",
                    "cover": "",
                    "template": "",
                    "priority": 0,
                    "children": [],
                    "preventParentPostCascadeQuery": False,
                    "hideFromList": False,
                },
            },
        )
        return str(created["metadata"]["name"])

    def ensure_tags(self, tag_names: list[str]) -> dict[str, str]:
        _, data = self.request("GET", "/apis/content.halo.run/v1alpha1/tags?size=1000")
        mapping: dict[str, str] = {}
        for it in (data or {}).get("items") or []:
            dn = (it.get("spec") or {}).get("displayName")
            if dn:
                mapping[str(dn)] = str(it["metadata"]["name"])
        for tname in sorted({t.strip() for t in tag_names if t and t.strip()}):
            if tname in mapping:
                continue
            tid = "wx-tag-" + hashlib.sha1(tname.encode("utf-8")).hexdigest()[:10]
            _, created = self.request(
                "POST",
                "/apis/content.halo.run/v1alpha1/tags",
                body={
                    "apiVersion": "content.halo.run/v1alpha1",
                    "kind": "Tag",
                    "metadata": {"name": tid},
                    "spec": {"displayName": tname, "slug": tid, "cover": ""},
                },
            )
            mapping[tname] = str(created["metadata"]["name"])
        return mapping

    def list_existing_attachments(self) -> dict[str, dict[str, str]]:
        existing: dict[str, dict[str, str]] = {}
        page = 1
        while True:
            _, data = self.request("GET", f"/apis/storage.halo.run/v1alpha1/attachments?page={page}&size=500")
            for it in (data or {}).get("items") or []:
                dn = (it.get("spec") or {}).get("displayName")
                pm = (it.get("status") or {}).get("permalink")
                if dn and pm:
                    existing[str(dn)] = {"name": str(it["metadata"]["name"]), "permalink": str(pm)}
            if not (data or {}).get("hasNext"):
                break
            page += 1
        return existing

    def list_existing_posts(self) -> dict[str, dict[str, str]]:
        existing: dict[str, dict[str, str]] = {}
        page = 1
        while True:
            _, data = self.request("GET", f"/apis/api.console.halo.run/v1alpha1/posts?page={page}&size=500")
            for it in (data or {}).get("items") or []:
                post_obj = it.get("post") or it
                slug = (post_obj.get("spec") or {}).get("slug")
                name = (post_obj.get("metadata") or {}).get("name")
                phase = (post_obj.get("status") or {}).get("phase") or ""
                if slug and name:
                    existing[str(slug)] = {"name": str(name), "phase": str(phase)}
            if not (data or {}).get("hasNext"):
                break
            page += 1
        return existing

    def upload_attachment(
        self,
        file_path: Path,
        upload_filename: str,
        *,
        policy_name: str,
        group_name: str,
        owner_username: str,
    ) -> dict[str, str]:
        ctype, _ = mimetypes.guess_type(upload_filename)
        if not ctype:
            ctype = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".gif": "image/gif",
                ".webp": "image/webp",
                ".svg": "image/svg+xml",
                ".mp4": "video/mp4",
                ".mp3": "audio/mpeg",
                ".mov": "video/quicktime",
            }.get(Path(upload_filename).suffix.lower(), "application/octet-stream")

        raw_bytes = file_path.read_bytes()
        boundary = "----HaloUpload" + uuid.uuid4().hex
        parts: list[bytes] = []
        for k, v in (("policyName", policy_name), ("groupName", group_name)):
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8")
            )
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{upload_filename}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n"
            ).encode("utf-8")
            + raw_bytes
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body_bytes = b"".join(parts)

        att: dict[str, Any] | None = None
        last_err: Exception | None = None
        for attempt in range(4):
            try:
                _, att = self.request(
                    "POST",
                    "/apis/api.console.halo.run/v1alpha1/attachments/upload",
                    body=body_bytes,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    timeout=120,
                )
                break
            except Exception as exc:
                last_err = exc
                time.sleep(1.5 * (attempt + 1))

        if not att:
            raise RuntimeError(f"Failed uploading {upload_filename}: {redact_text(str(last_err))}")

        att_name = str(att["metadata"]["name"])
        if owner_username and (att.get("spec") or {}).get("ownerName") != owner_username:
            for _ in range(4):
                try:
                    _, cur = self.request("GET", f"/apis/storage.halo.run/v1alpha1/attachments/{att_name}")
                    if cur["spec"].get("ownerName") == owner_username:
                        att = cur
                        break
                    cur["spec"]["ownerName"] = owner_username
                    _, att = self.request("PUT", f"/apis/storage.halo.run/v1alpha1/attachments/{att_name}", body=cur)
                    break
                except Exception as exc:
                    if "409" in str(exc):
                        time.sleep(0.15)
                        continue
                    break

        # Poll briefly if permalink is populated asynchronously by Halo storage controller
        permalink = str((att.get("status") or {}).get("permalink") or "")
        if not permalink:
            for _ in range(8):
                time.sleep(0.25)
                _, cur = self.request("GET", f"/apis/storage.halo.run/v1alpha1/attachments/{att_name}")
                permalink = str((cur.get("status") or {}).get("permalink") or "")
                if permalink:
                    break

        return {"name": att_name, "permalink": permalink}


def import_bundle_to_halo(
    opts: HaloImportOptions,
    *,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Import an exported `halo_import_bundle.json` into a Halo 2.x site idempotently."""
    bundle_file = opts.bundle_dir / "halo_import_bundle.json"
    if not bundle_file.exists():
        raise FileNotFoundError(f"No `halo_import_bundle.json` found in {opts.bundle_dir}")

    bundle = read_json(bundle_file, {})
    posts: list[dict[str, Any]] = list((bundle or {}).get("posts") or [])
    if opts.limit > 0:
        posts = posts[: opts.limit]

    account_name = str(
        opts.category_display_name
        or (bundle or {}).get("account_name")
        or (posts[0]["post_spec"]["categories_names"][0] if (posts and posts[0]["post_spec"].get("categories_names")) else "WeChat Archive")
    )
    group_display = opts.group_display_name or account_name

    if opts.dry_run:
        media_total = sum(len(p.get("media") or []) for p in posts)
        log(
            f"[Dry Run] Would import {len(posts)} posts and {media_total} media files "
            f"from {opts.bundle_dir} into {opts.base_url} (category='{account_name}')."
        )
        return {"dry_run": True, "posts_count": len(posts), "media_count": media_total}

    password = opts.password
    if not opts.token and opts.username and not password:
        password = getpass.getpass(f"Halo password for {opts.username}: ")

    client = HaloClient(
        opts.base_url,
        token=opts.token,
        username=opts.username,
        password=password,
    )
    owner = opts.owner or client.authenticated_user
    policy = client.resolve_storage_policy(opts.policy_name)
    group_name = client.ensure_group(group_display)
    cat_name = client.ensure_category(account_name, slug=opts.category_slug)

    all_tags: list[str] = []
    for p in posts:
        all_tags.extend((p.get("post_spec") or {}).get("tags_names") or [])
    tag_map = client.ensure_tags(all_tags)

    existing_atts = client.list_existing_attachments()
    existing_posts = client.list_existing_posts()

    state_path = opts.bundle_dir / "halo_import_state.json"
    state = read_json(state_path, {"attachments": {}, "posts": {}})
    if not isinstance(state, dict):
        state = {"attachments": {}, "posts": {}}
    state.setdefault("attachments", {})
    state.setdefault("posts", {})

    imported = 0
    skipped = 0

    for idx, item in enumerate(posts, 1):
        spec = item["post_spec"]
        slug = str(spec["slug"])
        title = str(spec["title"])

        if slug in existing_posts and (existing_posts[slug].get("phase") == "PUBLISHED" or not opts.publish):
            skipped += 1
            continue

        rel_to_permalink: dict[str, str] = {}
        for m in item.get("media") or []:
            bpath = str(m["bundle_path"])
            local_rel = str(m["local_path"])
            fpath = opts.bundle_dir / bpath
            if not fpath.exists():
                continue
            if bpath in state["attachments"] and state["attachments"][bpath].get("permalink"):
                rel_to_permalink[local_rel] = state["attachments"][bpath]["permalink"]
                continue
            upload_name = f"{slug}__{Path(local_rel).name}"
            if upload_name in existing_atts:
                info = existing_atts[upload_name]
                state["attachments"][bpath] = info
                rel_to_permalink[local_rel] = info["permalink"]
                continue

            info = client.upload_attachment(
                fpath,
                upload_name,
                policy_name=policy,
                group_name=group_name,
                owner_username=owner,
            )
            state["attachments"][bpath] = info
            existing_atts[upload_name] = info
            rel_to_permalink[local_rel] = info["permalink"]

        html_body = (opts.bundle_dir / item["halo_content_path"]).read_text(encoding="utf-8")
        for local_rel in sorted(rel_to_permalink.keys(), key=len, reverse=True):
            if rel_to_permalink[local_rel]:
                html_body = html_body.replace(local_rel, rel_to_permalink[local_rel])

        cover_permalink = ""
        if item.get("cover_local"):
            cov_key = f"media/{Path(item['cover_local']).name}"
            cover_permalink = rel_to_permalink.get(cov_key, "")

        utc_pub = to_utc_iso(str(spec.get("publishTime") or ""))
        tag_ids = [tag_map[t] for t in (spec.get("tags_names") or []) if t in tag_map]

        if slug not in existing_posts:
            post_req = {
                "post": {
                    "apiVersion": "content.halo.run/v1alpha1",
                    "kind": "Post",
                    "metadata": {"name": slug},
                    "spec": {
                        "title": title,
                        "slug": slug,
                        "owner": owner,
                        "template": "",
                        "cover": cover_permalink,
                        "deleted": False,
                        "publish": False,
                        "publishTime": utc_pub,
                        "pinned": False,
                        "allowComment": True,
                        "visible": opts.visibility,
                        "priority": 0,
                        "excerpt": spec.get("excerpt") or {"autoGenerate": False, "raw": ""},
                        "categories": [cat_name],
                        "tags": tag_ids,
                        "htmlMetas": [],
                    },
                },
                "content": {
                    "raw": html_body,
                    "content": html_body,
                    "rawType": "HTML",
                },
            }
            _, created = client.request("POST", "/apis/api.console.halo.run/v1alpha1/posts", body=post_req)
            post_name = str(created["metadata"]["name"])
        else:
            post_name = existing_posts[slug]["name"]

        permalink = ""
        if opts.publish:
            _, pub_res = client.request("PUT", f"/apis/api.console.halo.run/v1alpha1/posts/{post_name}/publish")
            permalink = str((pub_res.get("status") or {}).get("permalink") or "")
            if owner and (pub_res.get("spec") or {}).get("owner") != owner:
                for _ in range(4):
                    try:
                        _, cur_p = client.request("GET", f"/apis/content.halo.run/v1alpha1/posts/{post_name}")
                        cur_p["spec"]["owner"] = owner
                        cur_p["spec"]["publishTime"] = utc_pub
                        client.request("PUT", f"/apis/content.halo.run/v1alpha1/posts/{post_name}", body=cur_p)
                        break
                    except Exception as exc:
                        if "409" in str(exc):
                            time.sleep(0.15)
                            continue
                        raise

        state["posts"][slug] = {
            "name": post_name,
            "title": title,
            "publishTime": utc_pub,
            "published": bool(opts.publish),
            "permalink": permalink,
        }
        write_atomic_json(state_path, state)
        imported += 1
        log(f"[{idx}/{len(posts)}] Imported: {utc_pub[:10]} | {title[:40]} (media={len(rel_to_permalink)})")

    return {"imported": imported, "skipped": skipped, "total": len(posts), "owner": owner}
