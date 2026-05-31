import sqlite3
import tempfile
import unittest
from pathlib import Path

from fanqie_rank_agent import (
    RankSource,
    build_html_report,
    build_markdown_report,
    build_static_report_data,
    clean_shortdramas_search_keyword,
    connect_db,
    extract_initial_state,
    extract_rank_sources,
    parse_rank_url,
    query_books,
    save_snapshot,
    update_book_tag,
)


class FanqieRankAgentTests(unittest.TestCase):
    def test_parse_rank_url(self):
        source = parse_rank_url("https://fanqienovel.com/rank/1_2_1141")
        self.assertEqual(source.gender, 1)
        self.assertEqual(source.rank_mold, 2)
        self.assertEqual(source.category_id, 1141)
        self.assertEqual(source.key, "1_2_1141")

    def test_extract_initial_state(self):
        html = '<script>window.__INITIAL_STATE__={"rank":{"rankVersion":"1","book_list":[]}};</script>'
        state = extract_initial_state(html)
        self.assertEqual(state["rank"]["rankVersion"], "1")

    def test_extract_rank_sources(self):
        html = """
        <a href="/rank/1_2_1141"><span>西方奇幻</span></a>
        <a href="/rank/0_1_23">种田</a>
        <a href="/rank/1_2_1141">西方奇幻</a>
        """
        sources = extract_rank_sources(html, "https://fanqienovel.com/rank/1_2_1141")
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0].display_name, "男频阅读榜-西方奇幻")
        self.assertEqual(sources[1].display_name, "女频新书榜-种田")

    def test_clean_shortdramas_search_keyword(self):
        self.assertEqual(clean_shortdramas_search_keyword("废土拾荒：\ue521\ue4ec\ue50e\ue4e8合\ue4e9"), "废土拾荒")
        self.assertEqual(clean_shortdramas_search_keyword("无乱码\ue000\ue001很好"), "无乱码")
        self.assertEqual(clean_shortdramas_search_keyword("\ue557，\ue50c号\ue45a"), "号")
        self.assertEqual(clean_shortdramas_search_keyword("鬼医"), "鬼医")

    def test_snapshot_query_and_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "rank.sqlite3"
            source = RankSource("https://fanqienovel.com/rank/1_2_1141", 1, 2, 1141)
            day1 = [
                {"bookId": "a", "bookName": "A", "author": "AA", "currentPos": 10, "rankPosDiff": 0, "read_count": "100"},
                {"bookId": "b", "bookName": "B", "author": "BB", "currentPos": 20, "rankPosDiff": 0, "read_count": "200"},
            ]
            day2 = [
                {"bookId": "a", "bookName": "A", "author": "AA", "currentPos": 5, "rankPosDiff": 5, "read_count": "160"},
                {"bookId": "b", "bookName": "B", "author": "BB", "currentPos": 22, "rankPosDiff": -2, "read_count": "210"},
            ]
            with connect_db(db) as conn:
                save_snapshot(conn, source, {"rank_version": "1", "total_num": 2}, day1, "2026-05-28")
                save_snapshot(conn, source, {"rank_version": "2", "total_num": 2}, day2, "2026-05-29")
                books = query_books(conn)
                self.assertEqual(books[0]["book_id"], "a")
                self.assertEqual(books[0]["rank_change_1d"], 5)
                self.assertEqual(books[0]["read_growth_1d"], 60)
                self.assertGreater(books[0]["selection_score"], 0)
                scored = query_books(conn, order="score")
                self.assertIn("selection_score", scored[0])
                update_book_tag(conn, "a", "已选择", "重点看")
                selected = query_books(conn, tag="已选择")
                self.assertEqual(len(selected), 1)
                self.assertEqual(selected[0]["note"], "重点看")

    def test_static_report_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "rank.sqlite3"
            source = RankSource("https://fanqienovel.com/rank/1_2_1141", 1, 2, 1141, "西方奇幻")
            rows = [
                {"bookId": "a", "bookName": "A|书", "author": "AA", "currentPos": 3, "rankPosDiff": 8, "read_count": "1000"},
            ]
            with connect_db(db) as conn:
                save_snapshot(conn, source, {"rank_version": "1", "total_num": 1}, rows, "2026-05-29")
                data = build_static_report_data(conn, top=1)
            markdown = build_markdown_report(data)
            html = build_html_report(data)
            self.assertIn("番茄榜单日报 2026-05-29", markdown)
            self.assertIn("A\\|书", markdown)
            self.assertIn("评分", markdown)
            self.assertIn('data-status="已完结"', html)
            self.assertIn('data-sort="selection_score"', html)
            self.assertIn('data-sort="read_count"', html)
            self.assertIn('data-tag="不要"', html)
            self.assertIn('data-book-tag', html)
            self.assertIn('fanqie-rank-report-tags:v1', html)
            self.assertIn('/api/books/', html)


if __name__ == "__main__":
    unittest.main()
