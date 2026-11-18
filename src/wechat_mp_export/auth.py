"""WeRead official QR code authentication, session management, and signed API client.

Credentials are stored in a private state directory with mode 0600, outside the
project by default. Known credential fields are redacted from error messages.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import random
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Callable

import qrcode
import requests
from PIL import Image, ImageOps

from wechat_mp_export.config import (
    ensure_private_dir,
    get_default_state_dir,
    get_proxies,
    mask_secret,
    read_json,
    redact_text,
    write_private_json,
)
from wechat_mp_export.http_client import get_data_client

# Public WeRead mobile client protocol constants
APK_VERSION = "10.2.2"
APK_VERSION_CODE = "10167650"
APK_FULL_VERSION = f"{APK_VERSION}.{APK_VERSION_CODE}"
WEREAD_OPENSDK_APPID = "wxab9b71ad2b90ff34"
# Shared guest-signing fallback, not a QR-login accessToken/refreshToken.
# Pinned upstream source:
# https://github.com/x554960766/wechat-mp-tools/blob/68fab018e5d17b1c506e2cc105efc970b3cd99ac/backend/weread_signer.py#L20-L21
# sync_guest_token() refreshes it from the public /feature endpoint.
DEFAULT_GUEST_TOKEN = "5ecdcfd7f"
GUEST_TOKEN_TTL = 6 * 3600
PROACTIVE_REFRESH_SECONDS = 18 * 60

APK_USER_AGENT = (
    f"WeRead/{APK_VERSION} WRBrand/Android "
    "Dalvik/2.1.0 (Linux; U; Android 13; GenericDevice Build/TQ3A.230901.001)"
)

APK_BASE_HEADERS = {
    "User-Agent": APK_USER_AGENT,
    "osver": "13",
    "appver": APK_FULL_VERSION,
    "basever": APK_FULL_VERSION,
    "channelId": "990",
    "baseapi": "33",
    "Content-Type": "application/json; charset=UTF-8",
}

_DEVICE_MODELS: tuple[str, ...] = (
    "Pixel 8",
    "Pixel 7 Pro",
    "Galaxy S23",
    "Galaxy S24",
    "OnePlus 12",
    "Xiaomi 14",
)

# Public S-Box & salt used by WeRead client signature verification
_SBOX: tuple[int, ...] = (
    52, 202, 85, 64, 29, 182, 147, 198, 49, 48, 41, 53, 50, 167, 184, 17,
    194, 181, 22, 250, 139, 177, 36, 164, 16, 144, 4, 233, 8, 248, 59, 138,
    156, 140, 68, 249, 188, 92, 105, 226, 161, 218, 210, 211, 117, 137, 247, 30,
    45, 80, 86, 215, 114, 83, 191, 34, 251, 32, 15, 1, 46, 69, 135, 110,
    102, 72, 242, 224, 205, 254, 103, 169, 67, 244, 148, 81, 206, 165, 74, 238,
    19, 38, 142, 204, 170, 51, 20, 93, 14, 57, 187, 207, 145, 43, 129, 77,
    234, 153, 236, 26, 44, 133, 197, 217, 54, 116, 75, 24, 225, 241, 61, 157,
    65, 159, 180, 23, 13, 214, 76, 190, 220, 175, 151, 40, 119, 240, 98, 255,
    113, 193, 200, 39, 143, 108, 104, 168, 155, 230, 89, 28, 27, 18, 9, 152,
    78, 63, 6, 55, 0, 186, 31, 10, 25, 47, 201, 213, 208, 87, 73, 111,
    253, 37, 228, 97, 12, 66, 203, 150, 100, 95, 219, 173, 96, 35, 141, 154,
    109, 195, 196, 94, 62, 185, 146, 106, 189, 91, 7, 127, 118, 149, 237, 79,
    171, 132, 122, 128, 231, 120, 199, 229, 235, 115, 131, 107, 252, 56, 70, 125,
    71, 101, 179, 82, 99, 58, 5, 209, 239, 163, 166, 222, 158, 60, 2, 174,
    178, 123, 160, 246, 243, 42, 192, 172, 134, 3, 90, 84, 11, 245, 130, 212,
    126, 227, 223, 176, 216, 221, 33, 232, 124, 136, 162, 121, 88, 112, 183, 21,
)
_SALT = b"5a6f1"


class WeReadAuthError(RuntimeError):
    """Raised when WeRead credentials are missing or expired."""


class WeReadRateLimitError(RuntimeError):
    """Raised when WeRead enforces -2014 frequency throttling."""


def generate_device_profile() -> dict[str, Any]:
    """Generate a random per-session device profile (no static device ID)."""
    return {
        "deviceId": hashlib.md5(uuid.uuid4().bytes).hexdigest(),
        "deviceName": random.choice(_DEVICE_MODELS),
        "installId": str(uuid.uuid4()),
        "deviceType": 1,
        "userAgent": APK_USER_AGENT,
    }


def gen_signature(parts: list[str]) -> str:
    """Compute WeRead mobile client request signature."""
    remapped = [bytes(_SBOX[b] for b in s.encode("utf-8")) for s in parts]
    remapped.append(_SALT)
    remapped.sort()
    joined = b"".join(remapped)
    if not joined:
        return ""

    xor1 = 0
    for b in joined:
        xor1 ^= b
    shift1 = xor1 % 11
    n1 = len(joined)
    shifted1 = bytearray(n1)
    for i, b in enumerate(joined):
        shifted1[(i + shift1) % n1] = b

    hash1 = hashlib.sha256(shifted1).hexdigest().encode("ascii")
    xor2 = 0
    for b in hash1:
        xor2 ^= b
    shift2 = xor2 % 11
    n2 = len(hash1)
    shifted2 = bytearray(n2)
    for i, b in enumerate(hash1):
        shifted2[(i + shift2) % n2] = b

    return hashlib.sha256(shifted2).hexdigest()


def render_qr_terminal(url: str) -> str:
    """Render a compact Unicode half-block QR code for terminal scanning."""
    qr = qrcode.QRCode(box_size=1, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    lines: list[str] = []
    rows = len(matrix)
    cols = len(matrix[0]) if rows else 0
    for r in range(0, rows, 2):
        row_chars: list[str] = []
        top_row = matrix[r]
        bot_row = matrix[r + 1] if (r + 1) < rows else [False] * cols
        for c in range(cols):
            top = bool(top_row[c])
            bot = bool(bot_row[c])
            if top and bot:
                row_chars.append(" ")
            elif top and not bot:
                row_chars.append("▄")
            elif not top and bot:
                row_chars.append("▀")
            else:
                row_chars.append("█")
        lines.append("".join(row_chars))
    return "\n".join(lines)


class WeReadClient:
    """Self-contained WeRead authentication & rate-paced API client."""

    def __init__(
        self,
        state_dir: Path | None = None,
        proxy_url: str | None = None,
        min_interval: float = 0.42,
    ) -> None:
        self.state_dir = ensure_private_dir(state_dir or get_default_state_dir())
        self.proxy_url = proxy_url
        self.min_interval = max(0.2, float(min_interval))
        self.session_file = self.state_dir / "session.json"
        self.qr_state_file = self.state_dir / "qr_session.json"
        self.default_qr_png = self.state_dir / "login_qr.png"

        self._pace_lock = threading.Lock()
        self._last_req_at = 0.0
        self._rate_limit_until = 0.0
        self._refresh_lock = threading.Lock()
        self._guest_token = DEFAULT_GUEST_TOKEN
        self._guest_token_ts = 0.0

    @property
    def proxies(self) -> dict[str, str] | None:
        return get_proxies(self.proxy_url)

    def _pace(self, url: str) -> None:
        if "weread.qq.com" not in url:
            return
        gap = self.min_interval + random.uniform(0.05, 0.22)
        with self._pace_lock:
            now = time.time()
            target = max(now, self._last_req_at + gap, self._rate_limit_until)
            self._last_req_at = target
            wait_s = target - now
        if wait_s > 0:
            time.sleep(wait_s)

    def build_apk_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = dict(APK_BASE_HEADERS)
        if extra:
            h.update(extra)
        if h.get("accessToken"):
            h.pop("skey", None)
            h.pop("Cookie", None)
        return h

    def has_valid_session(self) -> bool:
        sess = read_json(self.session_file)
        if not isinstance(sess, dict):
            return False
        if not sess.get("vid"):
            return False
        if sess.get("refreshTokenExpired") and not sess.get("accessToken"):
            return False
        return bool(sess.get("accessToken") or sess.get("refreshToken"))

    def load_session(self) -> dict[str, Any]:
        sess = read_json(self.session_file)
        if not isinstance(sess, dict) or not sess.get("vid"):
            raise WeReadAuthError(
                "No active WeRead login session found. Run `wechat-mp-export auth login` "
                "(or pass `--no-weread` with `--article-url` for public URL/album export)."
            )
        return sess

    def get_status(self, show_raw_ids: bool = False) -> dict[str, Any]:
        sess = read_json(self.session_file)
        qr = read_json(self.qr_state_file)
        if not isinstance(sess, dict) or not sess.get("vid"):
            return {
                "authenticated": False,
                "state_dir": str(self.state_dir),
                "pending_qr": bool(isinstance(qr, dict) and qr.get("expires_at", 0) > time.time()),
            }
        vid = str(sess.get("vid") or "")
        nick = str(sess.get("nickname") or "")
        return {
            "authenticated": not bool(sess.get("refreshTokenExpired") and not sess.get("accessToken")),
            "vid": vid if show_raw_ids else mask_secret(vid, 2, 2),
            "nickname": nick if show_raw_ids else (mask_secret(nick, 1, 1) if nick else ""),
            "refresh_token_valid": not bool(sess.get("refreshTokenExpired")),
            "login_at": sess.get("loginAt"),
            "refreshed_at": sess.get("refreshedAt"),
            "state_dir": str(self.state_dir),
        }

    def logout(self) -> None:
        for fpath in (self.session_file, self.qr_state_file, self.default_qr_png):
            if fpath.exists():
                try:
                    fpath.unlink()
                except OSError:
                    pass

    # ── Official QR Login Flow ─────────────────────────────────────────

    def _fetch_qr_data(self) -> dict[str, Any]:
        s = get_data_client()
        s.cookies.clear()
        self._pace("https://i.weread.qq.com/wxticket")
        t_resp = s.get(
            "https://i.weread.qq.com/wxticket?nonceStr=weread",
            headers=self.build_apk_headers(),
            proxies=self.proxies,
            timeout=12,
        )
        t_resp.raise_for_status()
        ticket = t_resp.json()
        params = urllib.parse.urlencode({
            "appid": WEREAD_OPENSDK_APPID,
            "noncestr": "weread",
            "timestamp": ticket["timeStamp"],
            "scope": "snsapi_userinfo,snsapi_timeline,snsapi_friend,snsapi_favorites",
            "signature": ticket["signature"],
        })
        qr_resp = s.get(
            f"https://open.weixin.qq.com/connect/sdk/qrconnect?{params}",
            headers=self.build_apk_headers(),
            proxies=self.proxies,
            timeout=12,
        )
        qr_resp.raise_for_status()
        return qr_resp.json()

    def _save_qr_png(self, qr_info: dict[str, Any], qr_url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        b64 = ((qr_info.get("qrcode") or {}).get("qrcodebase64") or "").strip()
        if b64:
            raw = base64.b64decode(b64)
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            img = ImageOps.expand(img, border=24, fill="white")
            img.save(dest, format="PNG")
        else:
            qr = qrcode.QRCode(box_size=10, border=4)
            qr.add_data(qr_url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            img.save(dest)
        if os.name != "nt":
            try:
                os.chmod(dest, 0o600)
            except OSError:
                pass
        return dest

    def start_qr_login(self, qr_output: Path | None = None) -> dict[str, Any]:
        qr_info = self._fetch_qr_data()
        uuid_val = str(qr_info.get("uuid") or "").strip()
        if not uuid_val:
            raise RuntimeError(f"Failed to obtain QR login UUID: {redact_text(str(qr_info))}")
        qr_url = f"https://open.weixin.qq.com/connect/qrcode/{uuid_val}"
        png_path = self._save_qr_png(qr_info, qr_url, qr_output or self.default_qr_png)
        now = time.time()
        state = {
            "uuid": uuid_val,
            "qr_url": qr_url,
            "qr_image": str(png_path),
            "created_at": now,
            "expires_at": now + 300,
            "device_profile": generate_device_profile(),
        }
        write_private_json(self.qr_state_file, state)
        return {
            "uuid": uuid_val,
            "qr_url": qr_url,
            "qr_image": str(png_path),
            "expires_in_seconds": 300,
            "ascii_qr": render_qr_terminal(qr_url),
        }

    def _exchange_auth_code(self, wx_code: str, device_profile: dict[str, Any]) -> dict[str, Any]:
        device_id = str(device_profile.get("deviceId") or hashlib.md5(uuid.uuid4().bytes).hexdigest())
        device_name = str(device_profile.get("deviceName") or "Pixel 8")
        install_id = str(device_profile.get("installId") or uuid.uuid4())
        ua = str(device_profile.get("userAgent") or APK_USER_AGENT)

        ts_ms = int(time.time() * 1000)
        rand_val = random.randint(0, 999)
        anti_replay = hashlib.sha256(f"{ts_ms}{device_id}{rand_val}".encode("utf-8")).hexdigest()

        payload = {
            "code": wx_code,
            "deviceId": device_id,
            "trackId": "",
            "timestamp": ts_ms,
            "random": rand_val,
            "signature": anti_replay,
            "wxToken": 1,
            "appFirstInstall": 0,
            "virtualChannelId": "",
            "installId": install_id,
            "isFromQrcode": 1,
            "isAutoLogout": 0,
            "deviceName": device_name,
            "deviceType": 1,
        }
        s = get_data_client()
        s.cookies.clear()
        self._pace("https://i.weread.qq.com/login")
        resp = s.post(
            "https://i.weread.qq.com/login",
            json=payload,
            headers=self.build_apk_headers({"User-Agent": ua, "Host": "i.weread.qq.com"}),
            proxies=self.proxies,
            timeout=15,
        )
        resp.raise_for_status()
        res = resp.json()
        vid = res.get("vid")
        tok = res.get("accessToken")
        if not vid or not tok:
            raise RuntimeError(f"Token exchange failed: {redact_text(str(res))}")

        now = time.time()
        user_obj = res.get("user") or {}
        sess = {
            "vid": str(vid),
            "accessToken": str(tok),
            "refreshToken": str(res.get("refreshToken") or ""),
            "skey": str(res.get("skey") or ""),
            "deviceId": device_id,
            "deviceName": device_name,
            "installId": install_id,
            "userAgent": ua,
            "nickname": str(user_obj.get("name") or ""),
            "loginAt": now,
            "refreshedAt": now,
            "refreshTokenExpired": False,
        }
        write_private_json(self.session_file, sess)
        self._rate_limit_until = 0.0
        self.activate_terminal_session(sess, include_feature=True)
        return sess

    def poll_qr_login(
        self,
        timeout_sec: int = 300,
        on_scanned: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        state = read_json(self.qr_state_file)
        if not isinstance(state, dict) or not state.get("uuid"):
            raise WeReadAuthError("No pending QR session found. Run `wechat-mp-export auth start` first.")

        uuid_val = state["uuid"]
        device_profile = state.get("device_profile") or generate_device_profile()
        poll_base = f"https://long.open.weixin.qq.com/connect/l/qrconnect?f=json&uuid={uuid_val}"
        poll_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            )
        }

        sess_http = requests.Session()
        sess_http.trust_env = False
        deadline = min(float(state.get("expires_at") or (time.time() + timeout_sec)), time.time() + timeout_sec)
        last_code: str | None = None
        notified_scan = False

        while time.time() < deadline:
            try:
                curr_url = poll_base + (f"&last={last_code}" if last_code else "")
                resp = sess_http.get(curr_url, headers=poll_headers, proxies=self.proxies, timeout=28)
                data = resp.json()
                errcode = data.get("wx_errcode") if "wx_errcode" in data else data.get("errcode")
                if errcode == 405:
                    wx_code = str(data.get("wx_code") or "")
                    saved = self._exchange_auth_code(wx_code, device_profile)
                    # Clean up ephemeral QR state & QR image once authenticated
                    for p in (self.qr_state_file, Path(state.get("qr_image", ""))):
                        if p and p.exists():
                            try:
                                p.unlink()
                            except OSError:
                                pass
                    return {
                        "authenticated": True,
                        "vid_masked": mask_secret(saved["vid"], 2, 2),
                        "state_dir": str(self.state_dir),
                    }
                if errcode == 404:
                    last_code = "404"
                    if not notified_scan and on_scanned:
                        notified_scan = True
                        on_scanned()
                elif errcode in (402, 403):
                    raise WeReadAuthError("QR code expired or was cancelled. Please start a fresh login.")
            except WeReadAuthError:
                raise
            except Exception:
                time.sleep(1.0)

        raise TimeoutError("Timed out waiting for WeChat QR scan confirmation.")

    def save_manual_session(
        self,
        vid: str,
        access_token: str = "",
        refresh_token: str = "",
    ) -> dict[str, Any]:
        vid = str(vid or "").strip()
        access_token = str(access_token or "").strip()
        refresh_token = str(refresh_token or "").strip()
        if not vid or (not access_token and not refresh_token):
            raise ValueError("`vid` and at least one of `access_token` or `refresh_token` are required.")
        profile = generate_device_profile()
        now = time.time()
        sess = {
            "vid": vid,
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "deviceId": profile["deviceId"],
            "deviceName": profile["deviceName"],
            "installId": profile["installId"],
            "userAgent": profile["userAgent"],
            "loginAt": now,
            "refreshedAt": now if access_token else 0.0,
            "refreshTokenExpired": False,
        }
        write_private_json(self.session_file, sess)
        if refresh_token and not access_token:
            self.refresh_session(force=True)
        return self.get_status()

    # ── Session Activation & Token Renewal ─────────────────────────────

    def sync_guest_token(self, sess: dict[str, Any] | None = None, force: bool = False) -> str:
        now = time.time()
        if not force and self._guest_token_ts > 0 and (now - self._guest_token_ts) < GUEST_TOKEN_TTL:
            return self._guest_token
        extra = {"Host": "i.weread.qq.com"}
        if sess and sess.get("vid") and sess.get("accessToken"):
            extra["vid"] = str(sess["vid"])
            extra["accessToken"] = str(sess["accessToken"])
        try:
            s = get_data_client()
            s.cookies.clear()
            u = "https://i.weread.qq.com/feature?synckey=0"
            self._pace(u)
            resp = s.get(u, headers=self.build_apk_headers(extra), proxies=self.proxies, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                gt = (
                    data.get("guest_token")
                    or data.get("guestToken")
                    or (data.get("feature") or {}).get("guest_token")
                )
                if gt:
                    self._guest_token = str(gt).strip()
                    self._guest_token_ts = time.time()
        except Exception:
            pass
        return self._guest_token

    def activate_terminal_session(self, sess: dict[str, Any] | None = None, include_feature: bool = False) -> bool:
        sess = sess or self.load_session()
        if not sess.get("vid") or not sess.get("accessToken"):
            return False
        h = self.build_apk_headers({
            "vid": str(sess["vid"]),
            "accessToken": str(sess["accessToken"]),
            "Host": "i.weread.qq.com",
        })
        s = get_data_client()
        s.cookies.clear()
        try:
            payload = {
                "shelf": 0, "booklist": 0, "shelfLecture": 0, "notifications": 0,
                "config": 0, "gift": 0, "payItem": 0, "balance": 0.0, "reviewTimeline": 0,
                "follower": 0, "readingExchange": 0, "chat": 0, "chatRemovedSynckey": 0,
                "wechatFriend": 0, "inBackground": 0, "refluxSynckey": 0, "following": 0,
                "applyList": 0, "mcard": 0, "discoverFeedSynckey": 0, "rateSynckey": 0,
                "browse": 0, "medal": 0, "medalUrl": 0, "regionResource": 0,
                "discoverColumnSynckey": 0, "friendReviewSynckey": 0, "wehearSyncKey": 0,
                "preferTab": 1, "localBrowseTab": "", "localCommunityTab": "",
            }
            self._pace("https://i.weread.qq.com/mobileSync")
            s.post("https://i.weread.qq.com/mobileSync", json=payload, headers=h, proxies=self.proxies, timeout=10)
            if include_feature:
                self.sync_guest_token(sess=sess, force=True)
            u_shelf = "https://i.weread.qq.com/shelf/sync?synckey=0&teenmode=0&album=1&onlyBookid=1&localBookCount=0"
            self._pace(u_shelf)
            s.get(u_shelf, headers=h, proxies=self.proxies, timeout=8)
            u_mp = "https://i.weread.qq.com/mp/list?listType=0&count=50&synckey=0"
            self._pace(u_mp)
            s.get(u_mp, headers=h, proxies=self.proxies, timeout=8)
            return True
        except Exception:
            return False

    def refresh_session(self, force: bool = False) -> dict[str, Any]:
        with self._refresh_lock:
            sess = self.load_session()
            now = time.time()
            last_ref = float(sess.get("refreshedAt") or sess.get("loginAt") or 0.0)
            if not force and sess.get("accessToken") and (now - last_ref) < PROACTIVE_REFRESH_SECONDS:
                return sess

            rf = str(sess.get("refreshToken") or "").strip()
            if not rf or (sess.get("refreshTokenExpired") and not force):
                if sess.get("accessToken") and not force:
                    return sess
                raise WeReadAuthError("WeRead session expired. Please run `wechat-mp-export auth login` again.")

            device_id = str(sess.get("deviceId") or "")
            if not device_id:
                prof = generate_device_profile()
                device_id = prof["deviceId"]
                sess["deviceId"] = device_id
                sess["deviceName"] = prof["deviceName"]
            device_name = str(sess.get("deviceName") or "Pixel 8")

            for attempt in range(2):
                gt = self.sync_guest_token(sess=sess, force=(attempt > 0))
                now_ms = int(time.time() * 1000)
                rand_val = random.randint(0, 999)
                sig = gen_signature([gt, device_id, str(now_ms), str(rand_val), rf])
                payload = {
                    "refreshToken": rf,
                    "deviceId": device_id,
                    "wxToken": 0,
                    "inBackground": 0,
                    "trackId": "",
                    "kickType": 1,
                    "refCgi": "",
                    "timestamp": now_ms,
                    "random": rand_val,
                    "signature": sig,
                    "virtualChannelId": "",
                    "deviceName": device_name,
                }
                s = get_data_client()
                s.cookies.clear()
                self._pace("https://i.weread.qq.com/login")
                resp = s.post(
                    "https://i.weread.qq.com/login",
                    json=payload,
                    headers=self.build_apk_headers({"Host": "i.weread.qq.com"}),
                    proxies=self.proxies,
                    timeout=12,
                )
                data = resp.json() if resp.content else {}
                errcode = data.get("errcode") if isinstance(data, dict) else None
                if resp.status_code == 200 and isinstance(data, dict) and data.get("accessToken") and not errcode:
                    sess["accessToken"] = str(data["accessToken"])
                    if data.get("refreshToken"):
                        sess["refreshToken"] = str(data["refreshToken"])
                    if data.get("skey"):
                        sess["skey"] = str(data["skey"])
                    sess["refreshedAt"] = time.time()
                    sess["refreshTokenExpired"] = False
                    write_private_json(self.session_file, sess)
                    return sess
                if errcode == -2013:
                    sess["refreshTokenExpired"] = True
                    if data.get("accessToken"):
                        sess["accessToken"] = str(data["accessToken"])
                        sess["refreshedAt"] = time.time()
                        write_private_json(self.session_file, sess)
                        return sess
                    write_private_json(self.session_file, sess)
                    raise WeReadAuthError("WeRead refreshToken expired (-2013). Please re-authenticate via QR scan.")

            if sess.get("accessToken") and not force:
                return sess
            raise WeReadAuthError("Unable to refresh WeRead accessToken. Please re-authenticate via QR scan.")

    def get_auth_headers(self, force_refresh: bool = False) -> dict[str, str]:
        sess = self.refresh_session(force=force_refresh)
        return self.build_apk_headers({
            "vid": str(sess["vid"]),
            "accessToken": str(sess["accessToken"]),
            "Host": "i.weread.qq.com",
        })

    # ── Resilient JSON Requests ────────────────────────────────────────

    def request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        max_attempts: int = 4,
        cooldown_on_429: float = 20.0,
    ) -> dict[str, Any]:
        s = get_data_client()
        last_err = ""
        for attempt in range(max_attempts):
            headers = self.get_auth_headers(force_refresh=False)
            self._pace(url)
            try:
                s.cookies.clear()
                resp = s.request(
                    method.upper(),
                    url,
                    json=json_body,
                    headers=headers,
                    proxies=self.proxies,
                    timeout=15,
                )
            except Exception as exc:
                last_err = redact_text(str(exc))
                time.sleep(1.5 * (attempt + 1))
                continue

            if resp.status_code == 401:
                self.refresh_session(force=True)
                continue

            if resp.status_code in (429, 499):
                body_txt = resp.text or ""
                if "-2041" in body_txt:
                    self.activate_terminal_session(include_feature=True)
                    time.sleep(1.5)
                    continue
                if "-2014" in body_txt or resp.status_code == 429:
                    wait_s = cooldown_on_429 + (attempt * 5.0)
                    with self._pace_lock:
                        self._rate_limit_until = max(self._rate_limit_until, time.time() + wait_s)
                    if attempt + 1 >= max_attempts:
                        raise WeReadRateLimitError("WeRead API rate limit (-2014) reached.")
                    time.sleep(wait_s)
                    continue
                if "-2012" in body_txt:
                    time.sleep(2.0)
                    continue

            if resp.status_code == 200:
                data = resp.json() if resp.content else {}
                if isinstance(data, dict) and data.get("errcode") == -2014:
                    wait_s = cooldown_on_429 + (attempt * 5.0)
                    with self._pace_lock:
                        self._rate_limit_until = max(self._rate_limit_until, time.time() + wait_s)
                    if attempt + 1 >= max_attempts:
                        raise WeReadRateLimitError("WeRead API rate limit (-2014) reached.")
                    time.sleep(wait_s)
                    continue
                return data if isinstance(data, dict) else {"data": data}

            last_err = f"HTTP {resp.status_code}: {redact_text((resp.text or '')[:200])}"
            time.sleep(1.5 * (attempt + 1))

        raise RuntimeError(f"WeRead request failed after {max_attempts} attempts ({last_err})")

    def get_json(self, url: str, max_attempts: int = 4) -> dict[str, Any]:
        return self.request_json("GET", url, max_attempts=max_attempts)

    def post_json(self, url: str, payload: Any, max_attempts: int = 3) -> dict[str, Any]:
        return self.request_json("POST", url, json_body=payload, max_attempts=max_attempts)

    def fetch_web_article_html(self, review_id: str) -> str:
        """Fallback: fetch article HTML through WeRead Web content endpoint when available."""
        if not review_id or not self.has_valid_session():
            return ""
        try:
            sess = self.refresh_session(force=False)
            vid = str(sess.get("vid") or "")
            skey = str(sess.get("skey") or sess.get("accessToken") or "")
            if not vid or not skey:
                return ""
            s = get_data_client()
            s.cookies.clear()
            headers = {
                "Cookie": f"wr_vid={vid}; wr_skey={skey}",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                "Referer": "https://weread.qq.com/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
            self._pace("https://weread.qq.com/web/mp/content")
            resp = s.get(
                "https://weread.qq.com/web/mp/content",
                params={"reviewId": review_id},
                headers=headers,
                proxies=self.proxies,
                timeout=15,
            )
            if resp.status_code == 200 and ('id="js_content"' in resp.text or "rich_media_content" in resp.text):
                return resp.text
        except Exception:
            pass
        return ""
