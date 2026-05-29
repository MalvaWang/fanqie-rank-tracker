import unittest

from novel_quick_read_agent import analyze_text, split_chapters
from web_knowledge_agent import (
    extract_article_text,
    extract_article_title,
    extract_links,
    is_probably_obfuscated_text,
    summarize_article,
    title_matches,
)


class WebKnowledgeAgentTests(unittest.TestCase):
    def test_extract_wechat_album_links(self):
        html = """
        <li data-link="http://mp.weixin.qq.com/s?a=1&amp;b=2"
            data-title="短剧编剧第一课｜01期：新人入行"></li>
        """
        links = extract_links(html, "https://mp.weixin.qq.com/mp/appmsgalbum")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].title, "短剧编剧第一课｜01期：新人入行")
        self.assertIn("&b=2", links[0].url)

    def test_title_matches_all_fragments(self):
        self.assertTrue(title_matches("短剧编剧第一课｜01期", ["短剧编剧第一课"]))
        self.assertFalse(title_matches("短剧导演第一课｜01期", ["短剧编剧第一课"]))
        self.assertTrue(title_matches("第1章 天上掉下个顾小姐", [], ["第*章"]))
        self.assertFalse(title_matches("序章 天上掉下个顾小姐", [], ["第*章"]))

    def test_extract_wechat_content_noencode_text(self):
        html = r"""
        <script>
        var data = {
          content_noencode: '\x3csection\x3e\x3cp\x3e短剧编剧要重视节奏。\x3c/p\x3e\x3c/section\x3e'
        }
        </script>
        """
        self.assertIn("短剧编剧要重视节奏", extract_article_text(html))

    def test_extract_fanqie_reader_title_and_content(self):
        html = r'''
        <html>
          <h1 class="muye-reader-title">第1章 测试章节</h1>
          <script>
            window.__INITIAL_STATE__={"reader":{"chapterData":{"content":"\u003Cp\u003E第一段内容。\u003C/p\u003E\u003Cp\u003E第二段内容。\u003C/p\u003E"}}};
          </script>
        </html>
        '''
        self.assertEqual(extract_article_title(html, ""), "第1章 测试章节")
        self.assertIn("第一段内容", extract_article_text(html))

    def test_detects_private_use_font_obfuscation(self):
        text = "顾念念" + ("忆" * 20)
        self.assertTrue(is_probably_obfuscated_text(text))

    def test_summarize_article_prefers_domain_sentences(self):
        text = "普通句子没有重点。短剧编剧需要理解用户情绪和市场节奏。另一个普通句子。"
        summary = summarize_article("标题", "", text, max_points=1)
        self.assertEqual(summary, ["短剧编剧需要理解用户情绪和市场节奏。"])

    def test_split_novel_chapters(self):
        text = """
        第一章 雨夜
        林晚听见秘密。她决定复仇。
        第二章 证据
        沈淮拿出证据。真相终于出现。
        """
        chapters = split_chapters(text)
        self.assertEqual([title for title, _ in chapters], ["第一章 雨夜", "第二章 证据"])

    def test_analyze_novel_text_finds_adaptation_notes(self):
        text = "第一章 雨夜\n林晚听见秘密。她发现车祸不是意外，决定复仇。沈淮拿出了关键证据。"
        chapters = analyze_text(text)
        self.assertEqual(len(chapters), 1)
        self.assertIn("秘密", chapters[0].key_terms)
        self.assertTrue(chapters[0].adaptation_notes)


if __name__ == "__main__":
    unittest.main()
