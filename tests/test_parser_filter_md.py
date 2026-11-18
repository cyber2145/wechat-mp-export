from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wechat_mp_export.content_filter import filter_article_html
from wechat_mp_export.markdown import build_frontmatter_markdown, html_to_markdown
from wechat_mp_export.parser import parse_article_html, render_clean_html


class TestParserFilterMarkdown(unittest.TestCase):
    def test_standard_article_with_media_and_lazy_load(self) -> None:
        raw_html = """
        <html>
        <head>
          <meta property="og:title" content="深入理解分布式存储" />
          <meta property="og:image" content="https://mmbiz.qpic.cn/mmbiz_jpg/cover123/0?wx_fmt=jpeg" />
          <meta property="og:description" content="本文介绍一致性协议与工程实现。" />
          <meta name="author" content="架构组" />
        </head>
        <body>
          <div id="js_content" style="visibility: hidden; opacity: 0;">
            <section><p>第一段<strong>核心内容</strong>。</p></section>
            <p><img class="rich_pages wxw-img __bg_gif img_loading" data-src="https://mmbiz.qpic.cn/mmbiz_png/pic001/640?wx_fmt=png" style="width: 1px !important; visibility: hidden;" /></p>
            <mpvoice name="播客导读" voice_encode_fileid="voice_abc_1234567890"></mpvoice>
            <iframe class="video_iframe" data-mpvid="wxv_001" data-cover="https%3A%2F%2Fmmbiz.qpic.cn%2Fmmbiz_jpg%2Fvcover%2F0"></iframe>
            <p>参见往期：<a href="https://mp.weixin.qq.com/s/OldArticleShortId12345">上一篇</a></p>
          </div>
          <script>
            var biz = "MTIzNDU2Nzg5MA==";
            var ct = "1715000000";
            var copyright_stat = "1";
            var provinceName = "北京";
            var publicTagInfo = [{album_id: '1900000000000001', album_title: '分布式系列'}];
            var videoUrl = "https:\\/\\/mpvideo.qpic.cn\\/0b2e_demo.f10002.mp4?dis_k=1";
          </script>
        </body>
        </html>
        """
        parsed = parse_article_html(raw_html)
        self.assertEqual(parsed.status, "ok")
        self.assertEqual(parsed.title, "深入理解分布式存储")
        self.assertEqual(parsed.author, "架构组")
        self.assertEqual(parsed.publish_time, 1715000000)
        self.assertTrue(parsed.is_original)
        self.assertEqual(parsed.ip_location, "北京")
        self.assertIn("分布式系列", parsed.albums)
        self.assertIn("1900000000000001", parsed.discovered_album_ids)
        self.assertIn("https://mp.weixin.qq.com/s/OldArticleShortId12345", parsed.discovered_article_urls)
        self.assertEqual(len(parsed.image_urls), 1)
        self.assertEqual(len(parsed.voice_items), 1)
        self.assertEqual(len(parsed.video_items), 1)

        clean_html = render_clean_html(
            parsed.content_html,
            url_map={"https://mmbiz.qpic.cn/mmbiz_png/pic001/640?wx_fmt=png": "media/img_001.png"},
            audio_map={"voice_abc_1234567890": ("media/audio_001.mp3", "播客导读")},
            video_replacements=[
                (
                    parsed.video_items[0],
                    '<video controls src="media/video_01.mp4" poster="media/video_cover_01.jpg"></video>',
                )
            ],
        )
        self.assertIn('src="media/img_001.png"', clean_html)
        self.assertNotIn("visibility: hidden", clean_html)
        self.assertNotIn("img_loading", clean_html)
        self.assertIn('<audio controls="" preload="metadata" src="media/audio_001.mp3"', clean_html)
        self.assertIn('src="media/video_01.mp4"', clean_html)

        md = html_to_markdown(clean_html)
        self.assertIn("**核心内容**", md)
        self.assertIn("![](media/img_001.png)", md)
        self.assertIn("[Audio: media/audio_001.mp3](media/audio_001.mp3)", md)
        self.assertIn("[Video: media/video_01.mp4](media/video_01.mp4)", md)

        full_md = build_frontmatter_markdown(
            title=parsed.title,
            slug="wx-20240506-demo",
            date_iso="2024-05-06T20:53:20+08:00",
            author=parsed.author,
            account_name="示例公众号",
            tags=["分布式系列", "原创"],
            cover="media/cover.jpg",
            excerpt=parsed.digest,
            source_url="https://mp.weixin.qq.com/s/DemoShortId12345",
            body_markdown=md,
        )
        self.assertTrue(full_md.startswith("---\n"))
        self.assertIn('title: "深入理解分布式存储"', full_md)

    def test_gallery_article_and_deleted_page(self) -> None:
        gallery_html = """
        <html><head><meta property="og:title" content="秋日摄影集" /></head>
        <body>
        <script>
          var picture_page_info_list = [
            {cdn_url: 'https://mmbiz.qpic.cn/mmbiz_jpg/gal1/0?wx_fmt=jpeg'},
            {cdn_url: 'https://mmbiz.qpic.cn/mmbiz_jpg/gal2/0?wx_fmt=jpeg'}
          ];
          window.desc = "第一行图集说明\\n第二行图集说明";
        </script>
        </body></html>
        """
        parsed = parse_article_html(gallery_html)
        self.assertEqual(parsed.status, "ok")
        self.assertEqual(len(parsed.image_urls), 2)
        self.assertIn("第一行图集说明", parsed.content_html)

        deleted_html = "<html><body><div>该内容已被作者删除</div></body></html>"
        del_parsed = parse_article_html(deleted_html, title_hint="旧文章")
        self.assertEqual(del_parsed.status, "permanent_unavailable")

    def test_content_filter_and_safety_valve(self) -> None:
        long_body = "这是一段非常充实的正文内容，包含大量技术细节与分析说明，模拟真实公众号文章的正文篇幅长度。" * 3
        html_with_ads = f"""
        <div>
          <p>{long_body}</p>
          <div class="reward_area">赞赏作者</div>
          <p>点击上方蓝字关注我们</p>
          <img data-w="640" data-ratio="0.1" src="https://mmbiz.qpic.cn/banner.png" />
          <img data-w="640" data-ratio="0.75" src="https://mmbiz.qpic.cn/real_figure.png" />
        </div>
        """
        filtered = filter_article_html(html_with_ads)
        self.assertNotIn("赞赏作者", filtered)
        self.assertNotIn("点击上方蓝字", filtered)
        self.assertNotIn("banner.png", filtered)
        self.assertIn("real_figure.png", filtered)

        # Safety valve: if almost the entire article matches a keyword, filter must abort and preserve text
        tiny_promo_only = "<p>扫码关注获取完整活动日程安排</p>"
        preserved = filter_article_html(tiny_promo_only)
        self.assertIn("扫码关注获取完整活动日程安排", preserved)


if __name__ == "__main__":
    unittest.main()
