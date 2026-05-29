import sqlite3
import tempfile
import unittest
from pathlib import Path

from fanqie_rank_agent import (
    RankSource,
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
                update_book_tag(conn, "a", "已选择", "重点看")
                selected = query_books(conn, tag="已选择")
                self.assertEqual(len(selected), 1)
                self.assertEqual(selected[0]["note"], "重点看")


if __name__ == "__main__":
    unittest.main()
