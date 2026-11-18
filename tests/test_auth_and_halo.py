from __future__ import annotations

import base64
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wechat_mp_export.auth import WeReadClient, gen_signature
from wechat_mp_export.cli import main
from wechat_mp_export.halo_importer import HaloImportOptions, import_bundle_to_halo, to_utc_iso


# QR modules for a synthetic payload ("FIXTURE LOGIN").
_QR_MODULES = (
    "111111100001001111111",
    "100000101101001000001",
    "101110100111101011101",
    "101110100101001011101",
    "101110101001101011101",
    "100000100001001000001",
    "111111101010101111111",
    "000000000010000000000",
    "101010100010100010010",
    "100111000011001001100",
    "011110111001001111011",
    "111101001111100000101",
    "100010101001001001100",
    "000000001000011100011",
    "111111100100110101100",
    "100000100010011010011",
    "101110101100101011111",
    "101110100001010001110",
    "101110101011001100101",
    "100000100011110100011",
    "111111101111001111101",
)


class _InMemoryHaloHTTPHandler(urllib.request.HTTPHandler):
    """Socket-free urllib handler simulating Halo 2.x Console & Content APIs."""

    def __init__(self) -> None:
        self.uploaded_files: list[bytes] = []
        self.created_posts: list[dict] = []
        self.published_posts: list[str] = []

    def _resp(self, req: urllib.request.Request, payload: dict, status: int = 200) -> urllib.response.addinfourl:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        r = urllib.response.addinfourl(io.BytesIO(raw), headers, req.full_url, status)
        r.msg = 'OK'
        return r

    def http_open(self, req: urllib.request.Request) -> urllib.response.addinfourl:
        auth_h = req.get_header("Authorization") or req.headers.get("Authorization", "")
        if auth_h != "Bearer test_halo_token":
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"Unauthorized"))

        method = req.get_method()
        path = "/" + req.full_url.split("://", 1)[-1].split("/", 1)[-1]
        body = req.data or b""

        if method == "GET":
            if path.startswith("/apis/api.console.halo.run/v1alpha1/users/-"):
                return self._resp(req, {"user": {"metadata": {"name": "demo-author"}}})
            if path.startswith("/apis/storage.halo.run/v1alpha1/policies"):
                return self._resp(req, {"items": [{"metadata": {"name": "default-policy"}}]})
            if path.startswith("/apis/storage.halo.run/v1alpha1/groups"):
                return self._resp(req, {"items": []})
            if path.startswith("/apis/content.halo.run/v1alpha1/categories"):
                return self._resp(req, {"items": []})
            if path.startswith("/apis/content.halo.run/v1alpha1/tags"):
                return self._resp(req, {"items": []})
            if path.startswith("/apis/storage.halo.run/v1alpha1/attachments"):
                return self._resp(req, {"items": [], "hasNext": False})
            if path.startswith("/apis/api.console.halo.run/v1alpha1/posts"):
                items = [
                    {
                        "post": {
                            "metadata": {"name": p["post"]["metadata"]["name"]},
                            "spec": {"slug": p["post"]["spec"]["slug"]},
                            "status": {"phase": "PUBLISHED"},
                        }
                    }
                    for p in self.created_posts
                ]
                return self._resp(req, {"items": items, "hasNext": False})

        if method == "POST":
            if path in (
                "/apis/storage.halo.run/v1alpha1/groups",
                "/apis/content.halo.run/v1alpha1/categories",
                "/apis/content.halo.run/v1alpha1/tags",
            ):
                return self._resp(req, json.loads(body.decode("utf-8")), 201)
            if path == "/apis/api.console.halo.run/v1alpha1/attachments/upload":
                idx = len(self.uploaded_files) + 1
                self.uploaded_files.append(body)
                return self._resp(
                    req,
                    {
                        "metadata": {"name": f"att-{idx}"},
                        "spec": {"ownerName": "demo-author"},
                        "status": {"permalink": f"/upload/att_{idx}.bin"},
                    },
                    201,
                )
            if path == "/apis/api.console.halo.run/v1alpha1/posts":
                data = json.loads(body.decode("utf-8"))
                self.created_posts.append(data)
                return self._resp(req, data["post"], 201)

        if method == "PUT" and path.endswith("/publish"):
            pname = path.split("/")[-2]
            self.published_posts.append(pname)
            return self._resp(
                req,
                {
                    "metadata": {"name": pname},
                    "spec": {"owner": "demo-author"},
                    "status": {"permalink": f"/archives/{pname}"},
                },
            )

        return self._resp(req, {}, 404)


class TestAuthAndHaloImporter(unittest.TestCase):
    def test_signature_and_session_masking(self) -> None:
        sig1 = gen_signature(["5ecdcfd7f", "dev1", "1700000000000", "42", "rtok"])
        sig2 = gen_signature(["5ecdcfd7f", "dev1", "1700000000000", "42", "rtok"])
        self.assertEqual(len(sig1), 64)
        self.assertEqual(sig1, sig2)

        with tempfile.TemporaryDirectory() as tmp:
            client = WeReadClient(state_dir=Path(tmp) / "state")
            self.assertFalse(client.has_valid_session())
            client.save_manual_session(vid="12345678", access_token="atok_example_1", refresh_token="rtok_example_1")
            self.assertTrue(client.has_valid_session())
            masked = client.get_status(show_raw_ids=False)
            self.assertEqual(masked["vid"], "12***78")
            unmasked = client.get_status(show_raw_ids=True)
            self.assertEqual(unmasked["vid"], "12345678")
            client.logout()
            self.assertFalse(client.has_valid_session())

    def test_auth_commands_display_modules_from_the_official_image(self) -> None:
        image = Image.new("L", (21, 21))
        image.putdata([0 if bit == "1" else 255 for row in _QR_MODULES for bit in row])
        image = image.resize((105, 126), Image.Resampling.NEAREST)
        image = ImageOps.expand(image, border=(11, 13, 14, 15), fill=255)
        jpeg = io.BytesIO()
        image.save(jpeg, format="JPEG")
        official_jpeg = jpeg.getvalue()
        encoded = base64.b64encode(official_jpeg).decode("ascii")
        proxy = "http://127.0.0.1:7890"

        for action, embedded in (("login", encoded), ("start", None), ("start", "invalid image")):
            with (
                self.subTest(action=action, embedded_image=embedded == encoded),
                tempfile.TemporaryDirectory() as tmp,
            ):
                state_dir = Path(tmp) / "state"
                dest = Path(tmp) / "custom_qr.png"
                qr_info = {"uuid": "test-session", "qrcode": {"qrcodebase64": embedded}}
                stdout = io.StringIO()
                with (
                    patch.object(WeReadClient, "_fetch_qr_data", return_value=qr_info),
                    patch.object(WeReadClient, "poll_qr_login", return_value={"vid_masked": "1***"}) as poll,
                    patch("wechat_mp_export.auth.get_data_client") as http,
                    redirect_stdout(stdout),
                ):
                    http.return_value.get.return_value.content = official_jpeg
                    code = main([
                        "auth", "--state-dir", str(state_dir), "--proxy", proxy,
                        action, "--qr-output", str(dest),
                    ])
                    self.assertEqual(code, 0)
                    self.assertEqual(poll.call_count, int(action == "login"))
                    if embedded == encoded:
                        http.assert_not_called()
                    else:
                        http.return_value.get.assert_called_once_with(
                            "https://open.weixin.qq.com/connect/qrcode/test-session",
                            proxies={"http": proxy, "https": proxy}, timeout=12,
                        )

                # Reconstruct the QR from stdout and compare every module, including its white border.
                rows = []
                upper = {" ": "0", "▀": "1", "▄": "0", "█": "1"}
                lower = {" ": "0", "▀": "0", "▄": "1", "█": "1"}
                for line in stdout.getvalue().split("\n\n", 1)[0].splitlines():
                    self.assertTrue(line.startswith("\x1b[30;107m") and line.endswith("\x1b[0m"))
                    blocks = line.removeprefix("\x1b[30;107m").removesuffix("\x1b[0m")
                    rows.extend(("".join(upper[b] for b in blocks), "".join(lower[b] for b in blocks)))
                white = "0" * 29
                expected = [white] * 4 + ["0000" + row + "0000" for row in _QR_MODULES] + [white] * 5
                self.assertEqual(rows, expected)
                with Image.open(dest) as png, Image.open(io.BytesIO(official_jpeg)) as original:
                    saved_qr = png.crop((24, 24, png.width - 24, png.height - 24))
                    self.assertEqual(saved_qr.tobytes(), original.convert("RGB").tobytes())

    def test_auth_without_an_official_image_reports_an_error(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(WeReadClient, "_fetch_qr_data", return_value={"uuid": "test-session"}),
            patch("wechat_mp_export.auth.get_data_client") as http,
            redirect_stdout(io.StringIO()) as stdout,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            http.return_value.get.return_value.content = b"<html>Image unavailable</html>"
            code = main(["auth", "--state-dir", tmp, "start"])
            self.assertEqual(code, 2)
            self.assertIn("Unable to retrieve the WeChat login QR image", stderr.getvalue())
            self.assertEqual(stdout.getvalue(), "")
            self.assertFalse((Path(tmp) / "qr_session.json").exists())
            self.assertFalse((Path(tmp) / "login_qr.png").exists())

    def test_utc_conversion_and_halo_idempotent_import(self) -> None:
        self.assertEqual(to_utc_iso("2024-05-06T09:30:00+08:00"), "2024-05-06T01:30:00Z")
        self.assertEqual(to_utc_iso("2024-05-06T01:30:00Z"), "2024-05-06T01:30:00Z")

        handler = _InMemoryHaloHTTPHandler()
        orig_build_opener = urllib.request.build_opener

        def build_in_memory_opener(*_handlers: object) -> urllib.request.OpenerDirector:
            return orig_build_opener(handler)

        with patch("urllib.request.build_opener", side_effect=build_in_memory_opener):
            with tempfile.TemporaryDirectory() as tmp:
                bdir = Path(tmp)
                art_dir = bdir / "articles" / "2024-05-06_demo_Post"
                (art_dir / "media").mkdir(parents=True)
                (art_dir / "media" / "img_1.webp").write_bytes(b"img1")
                (art_dir / "media" / "img_10.webp").write_bytes(b"img10")
                (art_dir / "content.html").write_text(
                    '<p><img src="media/img_1.webp" /><img src="media/img_10.webp" /></p>',
                    encoding="utf-8",
                )
                bundle = {
                    "schema": "halo-2.x-wechat-import-bundle/v1",
                    "account_name": "Demo Account",
                    "count": 1,
                    "posts": [
                        {
                            "local_folder": "articles/2024-05-06_demo_Post",
                            "halo_content_path": "articles/2024-05-06_demo_Post/content.html",
                            "markdown_path": "articles/2024-05-06_demo_Post/article.md",
                            "cover_local": "articles/2024-05-06_demo_Post/media/img_1.webp",
                            "media": [
                                {
                                    "local_path": "media/img_1.webp",
                                    "bundle_path": "articles/2024-05-06_demo_Post/media/img_1.webp",
                                },
                                {
                                    "local_path": "media/img_10.webp",
                                    "bundle_path": "articles/2024-05-06_demo_Post/media/img_10.webp",
                                },
                            ],
                            "post_spec": {
                                "title": "Demo Post",
                                "slug": "wx-20240506-demo",
                                "publishTime": "2024-05-06T09:30:00+08:00",
                                "excerpt": {"autoGenerate": False, "raw": "Summary"},
                                "categories_names": ["Demo Account"],
                                "tags_names": ["Architecture"],
                            },
                        }
                    ],
                }
                (bdir / "halo_import_bundle.json").write_text(json.dumps(bundle), encoding="utf-8")

                opts = HaloImportOptions(
                    bundle_dir=bdir,
                    base_url="http://halo.example.com",
                    token="test_halo_token",
                )
                res1 = import_bundle_to_halo(opts, log=lambda _: None)
                self.assertEqual(res1["imported"], 1)
                self.assertEqual(res1["owner"], "demo-author")
                posted_html = handler.created_posts[0]["content"]["raw"]
                self.assertIn('src="/upload/att_1.bin"', posted_html)
                self.assertIn('src="/upload/att_2.bin"', posted_html)

                # Second run must skip already-published post idempotently
                res2 = import_bundle_to_halo(opts, log=lambda _: None)
                self.assertEqual(res2["imported"], 0)
                self.assertEqual(res2["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
