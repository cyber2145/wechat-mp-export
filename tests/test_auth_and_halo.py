from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wechat_mp_export.auth import WeReadClient, gen_signature, render_qr_terminal
from wechat_mp_export.halo_importer import HaloImportOptions, import_bundle_to_halo, to_utc_iso


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

        qr_str = render_qr_terminal("https://open.weixin.qq.com/connect/qrcode/demo")
        self.assertTrue(len(qr_str.splitlines()) >= 10)

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
