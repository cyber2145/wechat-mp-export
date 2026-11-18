from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wechat_mp_export.cli import main as cli_main
from wechat_mp_export.exporter import AccountExporter, ExportOptions
from wechat_mp_export.http_client import resolve_suffix_path
from wechat_mp_export.privacy_audit import audit_directory
from wechat_mp_export.resolver import AccountInfo


class TestExporterEndToEnd(unittest.TestCase):
    def test_multi_hop_bfs_dedup_and_archive_outputs(self) -> None:
        target_biz = "MTIzNDU2Nzg5MA=="  # MP_WXS_1234567890
        foreign_biz = "OTg3NjU0MzIxMA=="  # MP_WXS_9876543210

        pages = {
            "https://mp.weixin.qq.com/s/SeedArticleShort0001": f"""
            <html><head>
              <meta property="og:title" content="第一篇：系统总览" />
              <meta property="og:image" content="https://mmbiz.qpic.cn/mmbiz_jpg/cov1/0?wx_fmt=jpeg" />
              <meta name="author" content="测试作者" />
            </head><body>
              <div id="js_content">
                <p>欢迎阅读第一篇。</p>
                <img data-src="https://mmbiz.qpic.cn/mmbiz_jpg/img1/0?wx_fmt=jpeg" />
                <a href="https://mp.weixin.qq.com/s/InlineGallery000002">延伸图集</a>
                <a href="https://mp.weixin.qq.com/s/ForeignArticle00009">外部公众号文章</a>
              </div>
              <script>
                var biz = "{target_biz}";
                var mid = "2247480001";
                var idx = "1";
                var ct = "1710000000";
                var album_id = "1800000000000001";
              </script>
            </body></html>
            """,
            "https://mp.weixin.qq.com/s/InlineGallery000002": f"""
            <html><head><meta property="og:title" content="第二篇：架构图集" /></head>
            <body><script>
              var biz = "{target_biz}";
              var mid = "2247480002";
              var idx = "1";
              var ct = "1710086400";
              var picture_page_info_list = [{{cdn_url: 'https://mmbiz.qpic.cn/mmbiz_png/gal_a/0?wx_fmt=png'}}];
              window.desc = "图集描述内容";
            </script></body></html>
            """,
            f"https://mp.weixin.qq.com/s?__biz={target_biz}&idx=1&mid=2247480001&sn=dup111": f"""
            <html><head><meta property="og:title" content="第一篇：系统总览" /></head>
            <body><div id="js_content"><p>欢迎阅读第一篇。</p></div>
            <script>var biz="{target_biz}"; var mid="2247480001"; var idx="1"; var ct="1710000000";</script></body></html>
            """,
            f"https://mp.weixin.qq.com/s?__biz={target_biz}&idx=1&mid=2247480003&sn=alb333": f"""
            <html><head><meta property="og:title" content="第三篇：合集历史文章" /></head>
            <body><div id="js_content"><p>来自合集的早期文章。</p></div>
            <script>var biz="{target_biz}"; var mid="2247480003"; var idx="1"; var ct="1680000000";</script></body></html>
            """,
            "https://mp.weixin.qq.com/s/ForeignArticle00009": f"""
            <html><head><meta property="og:title" content="其他账号文章" /></head>
            <body><div id="js_content"><p>不应归档到目标账号中。</p></div>
            <script>var biz="{foreign_biz}"; var mid="9999990001"; var idx="1"; var ct="1710000500";</script></body></html>
            """,
        }

        def fake_fetch_html(url: str, review_id: str = "") -> tuple[str, str, str]:
            if url in pages:
                return pages[url], url, ""
            return "", url, "not_found"

        def fake_download_asset(url: str, dest: Path, proxy_url: str | None) -> str | None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Simulate WebP bytes even when URL claims wx_fmt=jpeg to verify magic-byte suffix resolution
            webp_bytes = b"RIFF\x10\x00\x00\x00WEBPVP8 "
            final_p = resolve_suffix_path(dest, webp_bytes)
            final_p.write_bytes(webp_bytes)
            return final_p.name

        def fake_fetch_album(biz: str, album_id: str) -> tuple[str, list[dict]]:
            self.assertEqual(biz, target_biz)
            self.assertEqual(album_id, "1800000000000001")
            return "核心技术合集", [
                {
                    "url": f"https://mp.weixin.qq.com/s?__biz={target_biz}&mid=2247480001&idx=1&sn=dup111",
                    "title": "第一篇：系统总览",
                    "create_time": 1710000000,
                    "cover": "",
                    "msgid": "2247480001",
                    "itemidx": "1",
                    "album": "核心技术合集",
                },
                {
                    "url": f"https://mp.weixin.qq.com/s?__biz={target_biz}&mid=2247480003&idx=1&sn=alb333",
                    "title": "第三篇：合集历史文章",
                    "create_time": 1680000000,
                    "cover": "",
                    "msgid": "2247480003",
                    "itemidx": "1",
                    "album": "核心技术合集",
                },
            ]

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "demo_archive"
            opts = ExportOptions(out_dir=out_dir, article_delay=0.0, deep_discover=True)
            account = AccountInfo(
                nickname="示例工程公众号",
                wechat_id="demo_eng_mp",
                biz=target_biz,
                book_id="MP_WXS_1234567890",
            )
            exporter = AccountExporter(
                opts,
                html_fetcher=fake_fetch_html,
                asset_downloader=fake_download_asset,
                album_fetcher=fake_fetch_album,
                log=lambda _: None,
            )
            manifest = exporter.run(
                account=account,
                seed_urls=["https://mp.weixin.qq.com/s/SeedArticleShort0001"],
                use_weread_catalog=False,
            )

            self.assertEqual(manifest["summary"]["exported_ok"], 3)
            titles = [a["title"] for a in manifest["articles"]]
            self.assertEqual(
                titles,
                ["第三篇：合集历史文章", "第一篇：系统总览", "第二篇：架构图集"],
            )
            # Album tag from duplicate long URL must have merged onto SeedArticleShort0001!
            art1 = next(a for a in manifest["articles"] if a["title"] == "第一篇：系统总览")
            self.assertIn("核心技术合集", art1["tags"])

            # Verify WebP magic-byte suffix rewrite in HTML & Markdown
            art1_html = (out_dir / art1["content_file"]).read_text(encoding="utf-8")
            art1_md = (out_dir / art1["markdown_file"]).read_text(encoding="utf-8")
            self.assertIn("media/img_001.webp", art1_html)
            self.assertIn("media/img_001.webp", art1_md)

            # Verify CLI rebuild-index, dry-run Halo import, and privacy audit on archive!
            self.assertEqual(cli_main(["rebuild-index", str(out_dir)]), 0)
            self.assertEqual(
                cli_main(["import-halo", "--bundle-dir", str(out_dir), "--url", "https://blog.example.com", "--dry-run"]),
                0,
            )
            self.assertEqual(audit_directory(out_dir), [])
            # Ensure manifest contains zero absolute paths
            manifest_raw = json.dumps(manifest, ensure_ascii=False)
            self.assertNotIn(str(out_dir), manifest_raw)

    def test_max_articles_cap_lazy_albums_and_incremental_resume(self) -> None:
        target_biz = "MTIzNDU2Nzg5MA=="
        fetched_urls: list[str] = []
        fetched_albums: list[str] = []

        pages = {
            "https://mp.weixin.qq.com/s/SeedPost0000001": f"""
            <html><head><meta property="og:title" content="Post 1" /></head>
            <body>
              <div id="js_content">
                <p>Body 1</p>
                <a href="https://mp.weixin.qq.com/s/DeletedPost00099">Deleted</a>
              </div>
              <script>
                var biz="{target_biz}"; var mid="3000000001"; var idx="1"; var ct="1710000100";
                var albums = [{{album_id: "1900000000000001"}}, {{album_id: "1900000000000002"}}];
              </script>
            </body></html>
            """,
            "https://mp.weixin.qq.com/s/DeletedPost00099": """
            <html><body><div>该内容已被作者删除</div></body></html>
            """,
            f"https://mp.weixin.qq.com/s?__biz={target_biz}&idx=1&mid=3000000002&sn=p2": f"""
            <html><head><meta property="og:title" content="Post 2" /></head>
            <body><div id="js_content"><p>Body 2</p></div>
            <script>var biz="{target_biz}"; var mid="3000000002"; var idx="1"; var ct="1710000200";</script></body></html>
            """,
            f"https://mp.weixin.qq.com/s?__biz={target_biz}&idx=1&mid=3000000003&sn=p3": f"""
            <html><head><meta property="og:title" content="Post 3" /></head>
            <body><div id="js_content"><p>Body 3</p></div>
            <script>var biz="{target_biz}"; var mid="3000000003"; var idx="1"; var ct="1710000300";</script></body></html>
            """,
            f"https://mp.weixin.qq.com/s?__biz={target_biz}&idx=1&mid=3000000004&sn=p4": f"""
            <html><head><meta property="og:title" content="Post 4" /></head>
            <body><div id="js_content"><p>Body 4</p></div>
            <script>var biz="{target_biz}"; var mid="3000000004"; var idx="1"; var ct="1710000400";</script></body></html>
            """,
        }

        def fake_fetch_html(url: str, review_id: str = "") -> tuple[str, str, str]:
            fetched_urls.append(url)
            return pages.get(url, ""), url, ("" if url in pages else "404")

        def fake_fetch_album(biz: str, album_id: str) -> tuple[str, list[dict]]:
            fetched_albums.append(album_id)
            if album_id == "1900000000000001":
                return "Album One", [
                    {
                        "url": f"https://mp.weixin.qq.com/s?__biz={target_biz}&mid=3000000002&idx=1&sn=p2",
                        "title": "Post 2",
                        "create_time": 1710000200,
                        "album": "Album One",
                    },
                    {
                        "url": f"https://mp.weixin.qq.com/s?__biz={target_biz}&mid=3000000003&idx=1&sn=p3",
                        "title": "Post 3",
                        "create_time": 1710000300,
                        "album": "Album One",
                    },
                ]
            return "Album Two", [
                {
                    "url": f"https://mp.weixin.qq.com/s?__biz={target_biz}&mid=3000000004&idx=1&sn=p4",
                    "title": "Post 4",
                    "create_time": 1710000400,
                    "album": "Album Two",
                }
            ]

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "capped_archive"
            account = AccountInfo(nickname="Demo", biz=target_biz, book_id="MP_WXS_1234567890")

            # 1) max_articles=2: DeletedPost does NOT count as ok; Album 1 satisfies the 2nd article; Album 2 is NOT crawled!
            exp1 = AccountExporter(
                ExportOptions(out_dir=out_dir, article_delay=0.0, max_articles=2),
                html_fetcher=fake_fetch_html,
                asset_downloader=lambda u, d, p: None,
                album_fetcher=fake_fetch_album,
                log=lambda _: None,
            )
            m1 = exp1.run(account=account, seed_urls=["https://mp.weixin.qq.com/s/SeedPost0000001"], use_weread_catalog=False)
            self.assertEqual(m1["summary"]["exported_ok"], 2)
            self.assertEqual(fetched_albums, ["1900000000000001"])

            # 2) Re-running with max_articles=2 makes ZERO network calls because 2 ok articles already exist
            fetched_urls.clear()
            fetched_albums.clear()
            m2 = exp1.run(account=account, seed_urls=["https://mp.weixin.qq.com/s/SeedPost0000001"], use_weread_catalog=False)
            self.assertEqual(m2["summary"]["exported_ok"], 2)
            self.assertEqual(fetched_urls, [])
            self.assertEqual(fetched_albums, [])

            # 3) Raising max_articles=3 exports 1 more article to reach 3 total
            exp3 = AccountExporter(
                ExportOptions(out_dir=out_dir, article_delay=0.0, max_articles=3),
                html_fetcher=fake_fetch_html,
                asset_downloader=lambda u, d, p: None,
                album_fetcher=fake_fetch_album,
                log=lambda _: None,
            )
            m3 = exp3.run(account=account, use_weread_catalog=False)
            self.assertEqual(m3["summary"]["exported_ok"], 3)

            # 4) max_new=1 downloads 1 additional article (crawling Album 2 on demand) -> total 4
            exp4 = AccountExporter(
                ExportOptions(out_dir=out_dir, article_delay=0.0, max_new=1),
                html_fetcher=fake_fetch_html,
                asset_downloader=lambda u, d, p: None,
                album_fetcher=fake_fetch_album,
                log=lambda _: None,
            )
            m4 = exp4.run(account=account, use_weread_catalog=False)
            self.assertEqual(m4["summary"]["exported_ok"], 4)
            self.assertIn("1900000000000002", fetched_albums)

    def test_order_newest_vs_oldest_and_cli_validation(self) -> None:
        target_biz = "MTIzNDU2Nzg5MA=="
        pages = {
            f"https://mp.weixin.qq.com/s/Art_{i:02d}_0000000000": f"""
            <html><head><meta property="og:title" content="Article {i}" /></head>
            <body><div id="js_content"><p>Content {i}</p></div>
            <script>var biz="{target_biz}"; var mid="{4000000000 + i}"; var idx="1"; var ct="{1700000000 + i * 86400}";</script></body></html>
            """
            for i in (1, 2, 3)
        }
        checkpoint = {
            "bookId": "MP_WXS_1234567890",
            "account_name": "OrderDemo",
            "next_offset": 3,
            "complete": True,
            "articles": [
                {"id": f"MP_WXS_1234567890_Art_{i:02d}_0000000000", "title": f"Article {i}", "link": f"https://mp.weixin.qq.com/s/Art_{i:02d}_0000000000", "update_time": 1700000000 + i * 86400}
                for i in (1, 2, 3)
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            # Newest 2 -> Article 2 & Article 3
            dir_new = Path(tmp) / "newest"
            dir_new.mkdir()
            (dir_new / "article_list_checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
            exp_new = AccountExporter(
                ExportOptions(out_dir=dir_new, article_delay=0.0, download_cached_only=True, deep_discover=False, max_articles=2, order="newest"),
                html_fetcher=lambda u, review_id="": (pages[u], u, ""),
                asset_downloader=lambda u, d, p: None,
                log=lambda _: None,
            )
            m_new = exp_new.run(account=AccountInfo(nickname="OrderDemo", biz=target_biz, book_id="MP_WXS_1234567890"), use_weread_catalog=False)
            self.assertEqual([a["title"] for a in m_new["articles"]], ["Article 2", "Article 3"])

            # Oldest 2 -> Article 1 & Article 2
            dir_old = Path(tmp) / "oldest"
            dir_old.mkdir()
            (dir_old / "article_list_checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
            exp_old = AccountExporter(
                ExportOptions(out_dir=dir_old, article_delay=0.0, download_cached_only=True, deep_discover=False, max_articles=2, order="oldest"),
                html_fetcher=lambda u, review_id="": (pages[u], u, ""),
                asset_downloader=lambda u, d, p: None,
                log=lambda _: None,
            )
            m_old = exp_old.run(account=AccountInfo(nickname="OrderDemo", biz=target_biz, book_id="MP_WXS_1234567890"), use_weread_catalog=False)
            self.assertEqual([a["title"] for a in m_old["articles"]], ["Article 1", "Article 2"])

        # Negative -n / --max-articles must be rejected by CLI parser
        with self.assertRaises(SystemExit) as ctx:
            cli_main(["export", "--url", "https://mp.weixin.qq.com/s/Art_01_0000000000", "-n", "-1"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
