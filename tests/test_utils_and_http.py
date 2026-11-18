from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wechat_mp_export.http_client import detect_suffix, resolve_suffix_path
from wechat_mp_export.utils import (
    CN_TZ,
    biz_to_book_id,
    book_id_to_biz,
    clean_wechat_article_url,
    extract_account_meta_from_html,
    extract_biz_from_html,
    extract_short_id,
    make_article_slug,
    normalize_wechat_short_url,
    parse_mid_idx_from_html,
    parse_mid_idx_from_url,
    same_account_id,
    sanitize_filename,
    short_id_from_review_id,
)


class TestUtilsAndHttp(unittest.TestCase):
    def test_biz_and_book_id_conversion(self) -> None:
        book_id = "MP_WXS_1234567890"
        biz = book_id_to_biz(book_id)
        self.assertEqual(biz, "MTIzNDU2Nzg5MA==")
        self.assertEqual(biz_to_book_id(biz), book_id)
        self.assertEqual(biz_to_book_id("MTIzNDU2Nzg5MA%3D%3D"), book_id)
        self.assertTrue(same_account_id(book_id, "MTIzNDU2Nzg5MA%3D%3D"))
        self.assertFalse(same_account_id(book_id, "MP_WXS_9876543210"))

    def test_short_url_and_cleaning(self) -> None:
        rid = "MP_WXS_1234567890_AbCdEf~GhIjKlMnOpQrSt"
        sid = short_id_from_review_id(rid)
        self.assertEqual(sid, "AbCdEf_GhIjKlMnOpQrSt")
        self.assertEqual(
            normalize_wechat_short_url("http://mp.weixin.qq.com/s/AbCdEf~GhIjKlMnOpQrSt"),
            "https://mp.weixin.qq.com/s/AbCdEf_GhIjKlMnOpQrSt",
        )

        dirty = (
            "http://mp.weixin.qq.com/s?__biz=MTIzNDU2Nzg5MA==&amp;mid=2247483647&amp;idx=1"
            "&amp;sn=abcdef123456&amp;chksm=deadbeef&amp;scene=21&amp;pass_ticket=secret123#wechat_redirect"
        )
        cleaned = clean_wechat_article_url(dirty)
        self.assertIn("__biz=MTIzNDU2Nzg5MA==", cleaned)
        self.assertIn("mid=2247483647", cleaned)
        self.assertIn("sn=abcdef123456", cleaned)
        self.assertNotIn("chksm", cleaned)
        self.assertNotIn("pass_ticket", cleaned)
        self.assertNotIn("wechat_redirect", cleaned)
        self.assertEqual(parse_mid_idx_from_url(cleaned), (2247483647, 1))
        self.assertEqual(extract_short_id("https://mp.weixin.qq.com/s/AbCdEf_GhIjKlMnOpQrSt?scene=1"), "AbCdEf_GhIjKlMnOpQrSt")

    def test_extract_account_and_mid_from_html(self) -> None:
        sample_html = """
        <html><body>
          <strong class="profile_nickname">示例技术周刊</strong>
          <p><span class="profile_meta_label">微信号</span><span class="profile_meta_value">tech_weekly_demo</span></p>
          <p><span class="profile_meta_label">功能介绍</span><span class="profile_meta_value">工程实践与架构笔记</span></p>
          <script>
            var biz = "" || "MTIzNDU2Nzg5MA==";
            var mid = "2247484001";
            var idx = "2";
          </script>
        </body></html>
        """
        self.assertEqual(extract_biz_from_html(sample_html), "MTIzNDU2Nzg5MA==")
        self.assertEqual(parse_mid_idx_from_html(sample_html), (2247484001, 2))
        meta = extract_account_meta_from_html(sample_html)
        self.assertEqual(meta["nickname"], "示例技术周刊")
        self.assertEqual(meta["wechat_id"], "tech_weekly_demo")
        self.assertEqual(meta["bookId"], "MP_WXS_1234567890")
        self.assertEqual(meta["intro"], "工程实践与架构笔记")

    def test_magic_bytes_and_slugs(self) -> None:
        self.assertEqual(detect_suffix(b"\x89PNG\r\n\x1a\nxxxx"), "png")
        self.assertEqual(detect_suffix(b"RIFF\x00\x00\x00\x00WEBPxxxx"), "webp")
        self.assertEqual(detect_suffix(b"\xff\xd8\xff\xe0xxxx"), "jpg")
        self.assertEqual(detect_suffix(b'<?xml version="1.0"?><svg viewBox="0 0 10 10"></svg>'), "svg")
        self.assertEqual(
            resolve_suffix_path(Path("img_001.jpg"), b"RIFF\x00\x00\x00\x00WEBP"),
            Path("img_001.webp"),
        )
        # jpeg vs jpg stays unchanged
        self.assertEqual(
            resolve_suffix_path(Path("img_001.jpeg"), b"\xff\xd8\xff\xe0"),
            Path("img_001.jpeg"),
        )
        dt = datetime(2026, 5, 1, 12, 0, tzinfo=CN_TZ)
        self.assertEqual(
            make_article_slug(dt, review_id="MP_WXS_1234567890_AbCdEf_1234567890"),
            "wx-20260501-abcdef123456",
        )
        self.assertEqual(sanitize_filename('A/B:C*D?"E<F>G|H'), "A_B_C_D__E_F_G_H")


if __name__ == "__main__":
    unittest.main()
