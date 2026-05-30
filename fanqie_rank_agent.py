#!/usr/bin/env python3
"""Track Fanqie ranking snapshots in a local SQLite database."""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import hashlib
import html
import hmac
import json
import math
import mimetypes
import os
import re
import smtplib
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
APP_HTML = ROOT / "fanqie_rank_app.html"
DEFAULT_DB = ROOT / "fanqie_rank_tracker" / "rank_tracker.sqlite3"
DEFAULT_RANK_URL = "https://fanqienovel.com/rank/1_2_1141"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
VALID_TAGS = ("待观察", "已选择", "不要")
API_URL = "https://fanqienovel.com/api/rank/category/list"
GENDER_NAMES = {0: "女频", 1: "男频", 2: "女频"}
RANK_MOLD_NAMES = {1: "新书榜", 2: "阅读榜"}
CATEGORY_NAMES = {1141: "西方奇幻"}


@dataclass(frozen=True)
class RankSource:
    url: str
    gender: int
    rank_mold: int
    category_id: int
    category_name: str = ""

    @property
    def key(self) -> str:
        return f"{self.gender}_{self.rank_mold}_{self.category_id}"

    @property
    def display_name(self) -> str:
        gender = GENDER_NAMES.get(self.gender, f"频道{self.gender}")
        mold = RANK_MOLD_NAMES.get(self.rank_mold, f"榜单{self.rank_mold}")
        category = self.category_name or CATEGORY_NAMES.get(self.category_id, f"分类{self.category_id}")
        return f"{gender}{mold}-{category}"


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_rank_url(url: str, category_name: str = "") -> RankSource:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc.endswith("fanqienovel.com"):
        raise ValueError("请输入 fanqienovel.com 的榜单 URL。")
    match = re.fullmatch(r"/rank/(\d+)_(\d+)_(\d+)", parsed.path.rstrip("/"))
    if not match:
        raise ValueError("榜单 URL 格式应类似：https://fanqienovel.com/rank/1_2_1141")
    gender, rank_mold, category_id = (int(part) for part in match.groups())
    clean_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))
    return RankSource(url=clean_url, gender=gender, rank_mold=rank_mold, category_id=category_id, category_name=category_name)


def extract_rank_sources(html_text: str, base_url: str) -> list[RankSource]:
    """Extract rank URLs from Fanqie's ranking menu HTML."""
    by_url: dict[str, RankSource] = {}
    pattern = re.compile(r'<a[^>]+href="(/rank/(\d+)_(\d+)_(\d+))"[^>]*>(.*?)</a>', re.S)
    for match in pattern.finditer(html_text):
        href, _gender, _rank_mold, _category_id, label_html = match.groups()
        label = re.sub(r"<[^>]+>", "", label_html)
        label = html.unescape(re.sub(r"\s+", " ", label).strip())
        full_url = urllib.parse.urljoin(base_url, href)
        source = parse_rank_url(full_url, category_name=label)
        by_url.setdefault(source.url, source)
    return list(by_url.values())


def discover_rank_sources(url: str = DEFAULT_RANK_URL, timeout: int = 20) -> list[RankSource]:
    """Discover all rank URLs from Fanqie's ranking menu."""
    return extract_rank_sources(fetch_text(url, timeout=timeout), url)


def fetch_text(url: str, timeout: int = 20, referer: str | None = None) -> str:
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json,text/plain,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    }
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def extract_initial_state(html: str) -> dict[str, Any]:
    marker = "window.__INITIAL_STATE__="
    start = html.find(marker)
    if start < 0:
        raise ValueError("页面里没有找到番茄初始化榜单数据。")
    start += len(marker)
    depth = 0
    in_string = False
    escaped = False
    end = None
    for idx, ch in enumerate(html[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = idx + 1
                break
    if end is None:
        raise ValueError("番茄初始化数据解析失败。")
    return json.loads(html[start:end])


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS rank_sources (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          url TEXT NOT NULL UNIQUE,
          source_key TEXT NOT NULL,
          gender INTEGER NOT NULL,
          rank_mold INTEGER NOT NULL,
          category_id INTEGER NOT NULL,
          name TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS books (
          book_id TEXT PRIMARY KEY,
          latest_title TEXT NOT NULL DEFAULT '',
          latest_author TEXT NOT NULL DEFAULT '',
          latest_abstract TEXT NOT NULL DEFAULT '',
          latest_cover_url TEXT NOT NULL DEFAULT '',
          latest_book_url TEXT NOT NULL DEFAULT '',
          latest_chapter_title TEXT NOT NULL DEFAULT '',
          latest_chapter_url TEXT NOT NULL DEFAULT '',
          latest_word_number INTEGER NOT NULL DEFAULT 0,
          status_tag TEXT NOT NULL DEFAULT '待观察'
            CHECK(status_tag IN ('待观察', '已选择', '不要')),
          note TEXT NOT NULL DEFAULT '',
          first_seen_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rank_snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_id INTEGER NOT NULL REFERENCES rank_sources(id) ON DELETE CASCADE,
          snapshot_date TEXT NOT NULL,
          captured_at TEXT NOT NULL,
          rank_version TEXT NOT NULL DEFAULT '',
          total_num INTEGER NOT NULL DEFAULT 0,
          font_css TEXT NOT NULL DEFAULT '',
          raw_url TEXT NOT NULL DEFAULT '',
          UNIQUE(source_id, snapshot_date)
        );

        CREATE TABLE IF NOT EXISTS rank_entries (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          snapshot_id INTEGER NOT NULL REFERENCES rank_snapshots(id) ON DELETE CASCADE,
          book_id TEXT NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
          rank_pos INTEGER NOT NULL,
          rank_pos_diff INTEGER NOT NULL DEFAULT 0,
          read_count INTEGER NOT NULL DEFAULT 0,
          word_number INTEGER NOT NULL DEFAULT 0,
          creation_status TEXT NOT NULL DEFAULT '',
          last_chapter_title TEXT NOT NULL DEFAULT '',
          last_chapter_update_time INTEGER NOT NULL DEFAULT 0,
          raw_json TEXT NOT NULL DEFAULT '',
          UNIQUE(snapshot_id, book_id)
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_source_date
          ON rank_snapshots(source_id, snapshot_date);
        CREATE INDEX IF NOT EXISTS idx_entries_book
          ON rank_entries(book_id);
        CREATE INDEX IF NOT EXISTS idx_entries_snapshot_rank
          ON rank_entries(snapshot_id, rank_pos);
        """
    )
    conn.commit()


def upsert_source(conn: sqlite3.Connection, source: RankSource) -> int:
    timestamp = now_iso()
    conn.execute(
        """
        INSERT INTO rank_sources
          (url, source_key, gender, rank_mold, category_id, name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
          source_key=excluded.source_key,
          gender=excluded.gender,
          rank_mold=excluded.rank_mold,
          category_id=excluded.category_id,
          name=excluded.name,
          updated_at=excluded.updated_at
        """,
        (
            source.url,
            source.key,
            source.gender,
            source.rank_mold,
            source.category_id,
            source.display_name,
            timestamp,
            timestamp,
        ),
    )
    row = conn.execute("SELECT id FROM rank_sources WHERE url = ?", (source.url,)).fetchone()
    return int(row["id"])


def api_rank_page(source: RankSource, rank_version: str, offset: int, limit: int, timeout: int) -> list[dict[str, Any]]:
    params = {
        "app_id": 2503,
        "rank_list_type": 3,
        "offset": offset,
        "limit": limit,
        "category_id": source.category_id,
        "rank_version": rank_version,
        "gender": source.gender,
        "rankMold": source.rank_mold,
    }
    url = API_URL + "?" + urllib.parse.urlencode(params)
    data = json.loads(fetch_text(url, timeout=timeout, referer=source.url))
    if data.get("code") != 0:
        raise ValueError(f"榜单接口返回异常：{data.get('message') or data.get('code')}")
    return list((data.get("data") or {}).get("book_list") or [])


def collect_rank_items(source: RankSource, html: str, limit: int, timeout: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state = extract_initial_state(html)
    rank = state.get("rank") or {}
    rank_version = str(rank.get("rankVersion") or "")
    if not rank_version:
        raise ValueError("页面没有返回 rankVersion，无法稳定分页。")
    total_num = safe_int(rank.get("total_num"), len(rank.get("book_list") or []))
    target = min(max(limit, 1), total_num or limit)
    page_size = 10
    items: list[dict[str, Any]] = []
    for offset in range(0, target, page_size):
        items.extend(api_rank_page(source, rank_version, offset, min(page_size, target - offset), timeout))
    if not items:
        items = list(rank.get("book_list") or [])[:target]
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        book_id = str(item.get("bookId") or "").strip()
        if not book_id or book_id in seen:
            continue
        seen.add(book_id)
        deduped.append(item)
    state_meta = {
        "rank_version": rank_version,
        "total_num": total_num,
        "font_css": str((state.get("common") or {}).get("css") or ""),
        "rank_type_text": str(rank.get("rankTypeText") or ""),
    }
    return state_meta, deduped


def save_snapshot(
    conn: sqlite3.Connection,
    source: RankSource,
    state_meta: dict[str, Any],
    items: Iterable[dict[str, Any]],
    snapshot_date: str,
) -> dict[str, Any]:
    timestamp = now_iso()
    source_id = upsert_source(conn, source)
    conn.execute(
        """
        INSERT INTO rank_snapshots
          (source_id, snapshot_date, captured_at, rank_version, total_num, font_css, raw_url)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, snapshot_date) DO UPDATE SET
          captured_at=excluded.captured_at,
          rank_version=excluded.rank_version,
          total_num=excluded.total_num,
          font_css=excluded.font_css,
          raw_url=excluded.raw_url
        """,
        (
            source_id,
            snapshot_date,
            timestamp,
            str(state_meta.get("rank_version") or ""),
            safe_int(state_meta.get("total_num")),
            str(state_meta.get("font_css") or ""),
            source.url,
        ),
    )
    snapshot = conn.execute(
        "SELECT id FROM rank_snapshots WHERE source_id = ? AND snapshot_date = ?",
        (source_id, snapshot_date),
    ).fetchone()
    snapshot_id = int(snapshot["id"])
    conn.execute("DELETE FROM rank_entries WHERE snapshot_id = ?", (snapshot_id,))

    count = 0
    for item in items:
        book_id = str(item.get("bookId") or "").strip()
        if not book_id:
            continue
        title = str(item.get("bookName") or "")
        author = str(item.get("author") or "")
        abstract = str(item.get("abstract") or "")
        cover_url = str(item.get("thumbUri") or "")
        first_chapter = str(item.get("firstChapterItemId") or "")
        last_chapter = str(item.get("lastChapterItemId") or "")
        word_number = safe_int(item.get("wordNumber"))
        last_chapter_title = str(item.get("lastChapterTitle") or "")
        book_url = f"https://fanqienovel.com/page/{book_id}"
        chapter_target = last_chapter or first_chapter
        chapter_url = f"https://fanqienovel.com/reader/{chapter_target}" if chapter_target else ""
        conn.execute(
            """
            INSERT INTO books
              (book_id, latest_title, latest_author, latest_abstract, latest_cover_url,
               latest_book_url, latest_chapter_title, latest_chapter_url,
               latest_word_number, first_seen_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(book_id) DO UPDATE SET
              latest_title=excluded.latest_title,
              latest_author=excluded.latest_author,
              latest_abstract=excluded.latest_abstract,
              latest_cover_url=excluded.latest_cover_url,
              latest_book_url=excluded.latest_book_url,
              latest_chapter_title=excluded.latest_chapter_title,
              latest_chapter_url=excluded.latest_chapter_url,
              latest_word_number=excluded.latest_word_number,
              updated_at=excluded.updated_at
            """,
            (
                book_id,
                title,
                author,
                abstract,
                cover_url,
                book_url,
                last_chapter_title,
                chapter_url,
                word_number,
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO rank_entries
              (snapshot_id, book_id, rank_pos, rank_pos_diff, read_count, word_number,
               creation_status, last_chapter_title, last_chapter_update_time, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                book_id,
                safe_int(item.get("currentPos")),
                safe_int(item.get("rankPosDiff")),
                safe_int(item.get("read_count") or item.get("readCount")),
                word_number,
                str(item.get("creationStatus") or ""),
                last_chapter_title,
                safe_int(item.get("lastChapterUpdateTime")),
                json.dumps(item, ensure_ascii=False, sort_keys=True),
            ),
        )
        count += 1
    conn.commit()
    return {
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "snapshot_date": snapshot_date,
        "rank_version": state_meta.get("rank_version") or "",
        "total_num": safe_int(state_meta.get("total_num")),
        "entry_count": count,
    }


def capture_rank_source(
    source: RankSource,
    db_path: Path,
    limit: int = 100,
    snapshot_date: str | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    html = fetch_text(source.url, timeout=timeout)
    if not source.category_name:
        for discovered in extract_rank_sources(html, source.url):
            if discovered.url == source.url:
                source = discovered
                break
    state_meta, items = collect_rank_items(source, html, limit=limit, timeout=timeout)
    day = snapshot_date or dt.date.today().isoformat()
    with connect_db(db_path) as conn:
        result = save_snapshot(conn, source, state_meta, items, day)
    result["url"] = source.url
    result["name"] = source.display_name
    result["db"] = str(db_path)
    return result


def capture_rank(url: str, db_path: Path, limit: int = 100, snapshot_date: str | None = None, timeout: int = 20) -> dict[str, Any]:
    source = parse_rank_url(url)
    return capture_rank_source(source, db_path, limit=limit, snapshot_date=snapshot_date, timeout=timeout)


def capture_all_ranks(
    index_url: str,
    db_path: Path,
    limit: int = 100,
    snapshot_date: str | None = None,
    timeout: int = 20,
    include_gender: set[int] | None = None,
    include_rank_mold: set[int] | None = None,
) -> dict[str, Any]:
    sources = discover_rank_sources(index_url, timeout=timeout)
    if include_gender is not None:
        sources = [source for source in sources if source.gender in include_gender]
    if include_rank_mold is not None:
        sources = [source for source in sources if source.rank_mold in include_rank_mold]

    results = []
    failed = []
    for index, source in enumerate(sources, start=1):
        print(f"[{index}/{len(sources)}] {source.display_name} {source.url}", flush=True)
        try:
            results.append(
                capture_rank_source(
                    source,
                    db_path,
                    limit=limit,
                    snapshot_date=snapshot_date,
                    timeout=timeout,
                )
            )
        except Exception as exc:
            failed.append({"url": source.url, "name": source.display_name, "error": str(exc)})
            print(f"  ERROR: {exc}", flush=True)

    return {
        "db": str(db_path),
        "source_count": len(sources),
        "captured_source_count": len(results),
        "failed_source_count": len(failed),
        "entry_count": sum(safe_int(item.get("entry_count")) for item in results),
        "results": results,
        "failed": failed,
    }


def list_sources(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.*,
               (SELECT COUNT(*) FROM rank_snapshots rs WHERE rs.source_id = s.id) AS snapshot_count,
               (SELECT MAX(snapshot_date) FROM rank_snapshots rs WHERE rs.source_id = s.id) AS latest_date
        FROM rank_sources s
        ORDER BY s.updated_at DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def latest_snapshot_ids(conn: sqlite3.Connection, source_id: int | None) -> list[sqlite3.Row]:
    args: tuple[Any, ...] = ()
    where = ""
    if source_id:
        where = "WHERE source_id = ?"
        args = (source_id,)
    return conn.execute(
        f"SELECT * FROM rank_snapshots {where} ORDER BY snapshot_date DESC, id DESC",
        args,
    ).fetchall()


def scoped_snapshots(conn: sqlite3.Connection, source_id: int | None) -> tuple[list[sqlite3.Row], dict[int, sqlite3.Row]]:
    if source_id:
        rows = latest_snapshot_ids(conn, source_id)
        latest = rows[:1]
        previous = {source_id: rows[1]} if len(rows) > 1 else {}
        return latest, previous

    rows = conn.execute(
        "SELECT * FROM rank_snapshots ORDER BY source_id ASC, snapshot_date DESC, id DESC"
    ).fetchall()
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(int(row["source_id"]), []).append(row)
    latest = [items[0] for items in grouped.values() if items]
    previous = {source: items[1] for source, items in grouped.items() if len(items) > 1}
    latest.sort(key=lambda row: (row["snapshot_date"], row["id"]), reverse=True)
    return latest, previous


def placeholders(count: int) -> str:
    return ",".join("?" for _ in range(count))


def query_summary(conn: sqlite3.Connection, source_id: int | None = None) -> dict[str, Any]:
    latest_rows, _previous = scoped_snapshots(conn, source_id)
    latest_ids = [int(row["id"]) for row in latest_rows]
    latest = None
    if latest_rows:
        latest = dict(latest_rows[0])
        latest["total_num"] = sum(safe_int(row["total_num"]) for row in latest_rows)
    books = query_books(conn, source_id=source_id, tag="", search="", order="fastest", limit=500)
    if latest_ids:
        marks = placeholders(len(latest_ids))
        tag_rows = conn.execute(
            f"""
            SELECT b.status_tag, COUNT(*) AS count
            FROM rank_entries e
            JOIN books b ON b.book_id = e.book_id
            WHERE e.snapshot_id IN ({marks})
            GROUP BY b.status_tag
            ORDER BY b.status_tag
            """,
            latest_ids,
        ).fetchall()
        book_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM rank_entries WHERE snapshot_id IN ({marks})",
            latest_ids,
        ).fetchone()["c"]
    else:
        tag_rows = []
        book_count = 0
    fastest = books[0] if books else None
    return {
        "latest_snapshot": latest,
        "snapshot_count": len(latest_rows),
        "book_count": book_count,
        "tag_counts": {row["status_tag"]: row["count"] for row in tag_rows},
        "fastest": public_book(fastest) if fastest else None,
    }


def public_book(item: dict[str, Any]) -> dict[str, Any]:
    hidden = {"raw_json", "latest_abstract"}
    return {key: value for key, value in item.items() if key not in hidden}


def rows_by_book(rows: Iterable[sqlite3.Row]) -> dict[str, sqlite3.Row]:
    return {str(row["book_id"]): row for row in rows}


def query_books(
    conn: sqlite3.Connection,
    source_id: int | None = None,
    tag: str = "",
    search: str = "",
    order: str = "fastest",
    creation_status: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    latest_snapshots, previous_snapshots_by_source = scoped_snapshots(conn, source_id)
    if not latest_snapshots:
        return []
    latest_ids = [int(row["id"]) for row in latest_snapshots]
    latest_marks = placeholders(len(latest_ids))

    latest_rows = conn.execute(
        f"""
        SELECT e.*, s.source_id, src.name AS source_name,
               b.latest_title, b.latest_author, b.latest_abstract, b.latest_cover_url,
               b.latest_book_url, b.latest_chapter_url, b.status_tag, b.note, b.first_seen_at,
               s.snapshot_date
        FROM rank_entries e
        JOIN books b ON b.book_id = e.book_id
        JOIN rank_snapshots s ON s.id = e.snapshot_id
        JOIN rank_sources src ON src.id = s.source_id
        WHERE e.snapshot_id IN ({latest_marks})
        ORDER BY e.rank_pos ASC
        """,
        latest_ids,
    ).fetchall()
    previous_by_key: dict[tuple[int, str], sqlite3.Row] = {}
    previous_ids = [int(row["id"]) for row in previous_snapshots_by_source.values()]
    previous_source_by_snapshot = {
        int(row["id"]): int(row["source_id"]) for row in previous_snapshots_by_source.values()
    }
    if previous_ids:
        previous_marks = placeholders(len(previous_ids))
        for row in conn.execute(
            f"SELECT * FROM rank_entries WHERE snapshot_id IN ({previous_marks})",
            previous_ids,
        ).fetchall():
            previous_by_key[(previous_source_by_snapshot[int(row["snapshot_id"])], str(row["book_id"]))] = row

    history_rows = conn.execute(
        """
        SELECT e.book_id, e.rank_pos, e.read_count, s.source_id, s.snapshot_date
        FROM rank_entries e
        JOIN rank_snapshots s ON s.id = e.snapshot_id
        WHERE (? IS NULL OR s.source_id = ?)
        ORDER BY s.snapshot_date ASC, s.id ASC
        """,
        (source_id, source_id),
    ).fetchall()
    first_by_key: dict[tuple[int, str], sqlite3.Row] = {}
    best_by_key: dict[tuple[int, str], int] = {}
    seen_days: dict[tuple[int, str], set[str]] = {}
    for row in history_rows:
        key = (int(row["source_id"]), str(row["book_id"]))
        first_by_key.setdefault(key, row)
        best_by_key[key] = min(best_by_key.get(key, 10**9), safe_int(row["rank_pos"]))
        seen_days.setdefault(key, set()).add(str(row["snapshot_date"]))

    needle = search.strip()
    result: list[dict[str, Any]] = []
    for row in latest_rows:
        item = dict(row)
        if tag and item["status_tag"] != tag:
            continue
        if creation_status and str(item["creation_status"]) != creation_status:
            continue
        haystack = f"{item['book_id']} {item['latest_title']} {item['latest_author']} {item['note']}"
        if needle and needle not in haystack:
            continue
        key = (int(item["source_id"]), str(item["book_id"]))
        previous_row = previous_by_key.get(key)
        first_row = first_by_key.get(key)
        prev_rank = safe_int(previous_row["rank_pos"]) if previous_row else None
        prev_read = safe_int(previous_row["read_count"]) if previous_row else None
        first_rank = safe_int(first_row["rank_pos"]) if first_row else safe_int(item["rank_pos"])
        current_rank = safe_int(item["rank_pos"])
        current_read = safe_int(item["read_count"])
        rank_change_1d = (prev_rank - current_rank) if prev_rank is not None else None
        read_growth_1d = (current_read - prev_read) if prev_read is not None else None
        total_climb = first_rank - current_rank
        fallback_climb = safe_int(item["rank_pos_diff"])
        effective_climb = rank_change_1d if rank_change_1d is not None else fallback_climb
        selection_score = calculate_selection_score(
            {
                **item,
                "rank_change_1d": rank_change_1d,
                "read_growth_1d": read_growth_1d,
                "total_climb": total_climb,
                "effective_climb": effective_climb,
            }
        )
        item.update(
            {
                "latest_snapshot_date": item["snapshot_date"],
                "creation_status_label": creation_status_label(item["creation_status"]),
                "previous_rank": prev_rank,
                "first_rank": first_rank,
                "best_rank": best_by_key.get(key, current_rank),
                "days_seen": len(seen_days.get(key, set())),
                "rank_change_1d": rank_change_1d,
                "read_growth_1d": read_growth_1d,
                "total_climb": total_climb,
                "effective_climb": effective_climb,
                "selection_score": selection_score,
            }
        )
        result.append(item)

    if order == "rank":
        result.sort(key=lambda item: safe_int(item["rank_pos"]))
    elif order == "score":
        result.sort(key=lambda item: (-(item["selection_score"] or 0), safe_int(item["rank_pos"])))
    elif order == "selected":
        result.sort(key=lambda item: (item["status_tag"] != "已选择", safe_int(item["rank_pos"])))
    elif order == "read_growth":
        result.sort(key=lambda item: (item["read_growth_1d"] is None, -(item["read_growth_1d"] or 0), safe_int(item["rank_pos"])))
    else:
        result.sort(
            key=lambda item: (
                -(item["effective_climb"] or 0),
                -(item["total_climb"] or 0),
                -(item["read_growth_1d"] or 0),
                safe_int(item["rank_pos"]),
            )
        )
    return result[: max(1, limit)]


def calculate_selection_score(item: dict[str, Any]) -> float:
    """Score how worth-tracking a ranked book is today, on a 0-100 scale."""
    climb = max(0, safe_int(item.get("effective_climb")))
    total_climb = max(0, safe_int(item.get("total_climb")))
    rank_pos = max(1, safe_int(item.get("rank_pos"), 999))
    read_count = max(0, safe_int(item.get("read_count")))
    read_growth = max(0, safe_int(item.get("read_growth_1d") or 0))

    climb_score = min(36.0, climb * 4.0)
    total_climb_score = min(16.0, total_climb * 1.5)
    rank_score = max(0.0, 24.0 - ((rank_pos - 1) * 0.24))
    read_score = min(12.0, math.log10(read_count + 1) * 2.0)
    growth_score = min(12.0, math.log10(read_growth + 1) * 3.0)
    return round(climb_score + total_climb_score + rank_score + read_score + growth_score, 1)


def creation_status_label(value: Any) -> str:
    text = str(value)
    if text == "0":
        return "已完结"
    if text == "1":
        return "连载中"
    return text or "-"


def update_book_tag(conn: sqlite3.Connection, book_id: str, status_tag: str, note: str | None = None) -> dict[str, Any]:
    if status_tag not in VALID_TAGS:
        raise ValueError(f"状态只能是：{'、'.join(VALID_TAGS)}")
    row = conn.execute("SELECT book_id FROM books WHERE book_id = ?", (book_id,)).fetchone()
    if not row:
        raise ValueError("数据库里没有这本书。")
    if note is None:
        conn.execute(
            "UPDATE books SET status_tag = ?, updated_at = ? WHERE book_id = ?",
            (status_tag, now_iso(), book_id),
        )
    else:
        conn.execute(
            "UPDATE books SET status_tag = ?, note = ?, updated_at = ? WHERE book_id = ?",
            (status_tag, note, now_iso(), book_id),
        )
    conn.commit()
    return {"book_id": book_id, "status_tag": status_tag, "note": note}


def export_csv(
    conn: sqlite3.Connection,
    path: Path,
    source_id: int | None = None,
    creation_status: str = "",
) -> Path:
    rows = query_books(conn, source_id=source_id, creation_status=creation_status, limit=1000)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "book_id",
        "rank_pos",
        "previous_rank",
        "rank_change_1d",
        "total_climb",
        "selection_score",
        "read_count",
        "read_growth_1d",
        "latest_title",
        "latest_author",
        "creation_status_label",
        "status_tag",
        "note",
        "latest_book_url",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})
    return path


def feishu_sign(secret: str, timestamp: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_rank_report(
    conn: sqlite3.Connection,
    top: int = 10,
    source_id: int | None = None,
    completed_only: bool = False,
) -> str:
    creation_status = "0" if completed_only else ""
    summary = query_summary(conn, source_id=source_id)
    books = query_books(conn, source_id=source_id, order="fastest", creation_status=creation_status, limit=top)
    latest = summary.get("latest_snapshot") or {}
    title = f"番茄榜单日报 {latest.get('snapshot_date') or dt.date.today().isoformat()}"
    if completed_only:
        title += "（已完结）"

    lines = [
        title,
        f"榜单源：{summary.get('snapshot_count', 0)} 个",
        f"入库记录：{summary.get('book_count', 0)} 本",
        "爬榜最快 Top " + str(top),
        "",
    ]
    if not books:
        lines.append("暂无榜单数据。")
        return "\n" + "\n".join(lines)

    for index, item in enumerate(books, start=1):
        climb = item.get("rank_change_1d")
        if climb is None:
            climb = item.get("rank_pos_diff")
        climb_text = f"+{climb}" if safe_int(climb) > 0 else str(climb)
        lines.extend(
            [
                f"{index}. {item.get('latest_title') or item.get('book_id')}",
                f"   评分 {score_label(item)}｜爬升 {climb_text}｜榜位 #{item.get('rank_pos')}｜{item.get('source_name', '')}",
                f"   在读 {safe_int(item.get('read_count')):,}｜{item.get('creation_status_label', '-')}",
                f"   {item.get('latest_book_url', '')}",
            ]
        )
    lines.append("")
    lines.append("注：番茄书名有字体混淆，通知里可能显示为怪字；链接和排名数据不受影响。")
    return "\n".join(lines)


def climb_value(item: dict[str, Any]) -> int:
    climb = item.get("rank_change_1d")
    if climb is None:
        climb = item.get("rank_pos_diff")
    return safe_int(climb)


def climb_label(item: dict[str, Any]) -> str:
    climb = climb_value(item)
    return f"+{climb}" if climb > 0 else str(climb)


def score_label(item: dict[str, Any]) -> str:
    score = safe_float(item.get("selection_score"))
    text = f"{score:.1f}"
    return text[:-2] if text.endswith(".0") else text


def markdown_cell(value: Any) -> str:
    text = str(value or "").replace("\n", " ")
    return text.replace("|", "\\|")


TITLE_EDGE_PUNCTUATION = " \t\r\n:：,，.。!！?？-—_·、|/\\()（）[]【】{}《》<>\"'“”‘’"


def is_garbled_title_char(char: str) -> bool:
    if not char:
        return False
    code = ord(char)
    return (
        char in {"\ufffd", "□", "■", "▢", "▯"}
        or 0xE000 <= code <= 0xF8FF
        or 0xF0000 <= code <= 0xFFFFD
        or 0x100000 <= code <= 0x10FFFD
    )


def clean_shortdramas_search_keyword(title: Any) -> str:
    text = str(title or "").strip()
    if not text:
        return ""
    chars: list[str] = []
    started = False
    last_was_space = False
    for char in text:
        if is_garbled_title_char(char):
            if started:
                break
            continue
        if char.isspace():
            if started and chars and not last_was_space:
                chars.append(" ")
                last_was_space = True
            continue
        if not started and char.strip(TITLE_EDGE_PUNCTUATION) == "":
            continue
        chars.append(char)
        started = True
        last_was_space = False
    cleaned = "".join(chars).strip(TITLE_EDGE_PUNCTUATION)
    return cleaned or text


def shortdramas_search_url(title: Any) -> str:
    keyword = clean_shortdramas_search_keyword(title)
    if not keyword:
        return "https://www.shortdramas.com/page/ip-application"
    query = urllib.parse.urlencode({"page_no": 1, "search_keyword": keyword})
    return f"https://www.shortdramas.com/page/ip-application?{query}"


def build_static_report_data(
    conn: sqlite3.Connection,
    top: int = 30,
    source_id: int | None = None,
    completed_only: bool = False,
) -> dict[str, Any]:
    creation_status = "0" if completed_only else ""
    summary = query_summary(conn, source_id=source_id)
    books = query_books(conn, source_id=source_id, order="fastest", creation_status=creation_status, limit=top)
    latest = summary.get("latest_snapshot") or {}
    return {
        "generated_at": now_iso(),
        "snapshot_date": latest.get("snapshot_date") or dt.date.today().isoformat(),
        "top": top,
        "completed_only": completed_only,
        "summary": summary,
        "books": [public_book(item) for item in books],
    }


def build_markdown_report(data: dict[str, Any]) -> str:
    title = f"番茄榜单日报 {data.get('snapshot_date')}"
    if data.get("completed_only"):
        title += "（已完结）"
    summary = data.get("summary") or {}
    books = data.get("books") or []
    lines = [
        f"# {title}",
        "",
        f"- 生成时间：{data.get('generated_at')}",
        f"- 榜单源：{summary.get('snapshot_count', 0)} 个",
        f"- 入库记录：{summary.get('book_count', 0)} 本",
        f"- 展示数量：Top {data.get('top', len(books))}",
        "",
        "| # | 书名 | 评分 | 爬升 | 榜位 | 榜单 | 在读 | 状态 | 链接 |",
        "|---:|---|---:|---:|---:|---|---:|---|---|",
    ]
    if not books:
        lines.append("| - | 暂无榜单数据 | - | - | - | - | - | - | - |")
    for index, item in enumerate(books, start=1):
        url = str(item.get("latest_book_url") or "")
        title_cell = markdown_cell(item.get("latest_title") or item.get("book_id"))
        ip_url = shortdramas_search_url(item.get("latest_title") or item.get("book_id"))
        link_parts = []
        if url:
            link_parts.append(f"[打开]({url})")
        if ip_url:
            link_parts.append(f"[搜IP]({ip_url})")
        link_cell = " / ".join(link_parts) or "-"
        lines.append(
            "| "
            + " | ".join(
                [
                    str(index),
                    title_cell,
                    score_label(item),
                    climb_label(item),
                    str(item.get("rank_pos") or "-"),
                    markdown_cell(item.get("source_name")),
                    f"{safe_int(item.get('read_count')):,}",
                    markdown_cell(item.get("creation_status_label")),
                    link_cell,
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "> 注：番茄书名有字体混淆，报告里可能显示为怪字；链接和排名数据不受影响。",
            "",
        ]
    )
    return "\n".join(lines)


def build_html_report(data: dict[str, Any]) -> str:
    title = f"番茄榜单日报 {data.get('snapshot_date')}"
    if data.get("completed_only"):
        title += "（已完结）"
    summary = data.get("summary") or {}
    data_json = html.escape(json.dumps(data, ensure_ascii=False), quote=False)
    template = Template("""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>$title</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1f2328;
      --muted: #667085;
      --line: #d8dee4;
      --accent: #0969da;
      --rise: #cf222e;
      --fall: #1a7f37;
      --soft: #eef2f6;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }
    main {
      max-width: 1280px;
      margin: 0 auto;
      padding: 28px 18px 44px;
    }
    h1 {
      margin: 0 0 10px;
      font-size: clamp(24px, 4vw, 38px);
      letter-spacing: 0;
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin: 16px 0 18px;
    }
    .pill {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      padding: 7px 10px;
      color: var(--muted);
      font-size: 14px;
    }
    .toolbar {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) minmax(220px, 1fr) minmax(240px, .9fr);
      gap: 12px;
      align-items: end;
      margin: 0 0 14px;
    }
    .control-label, .search-label {
      display: block;
      margin: 0 0 6px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 600;
    }
    .segmented {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(64px, 1fr));
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      background: var(--panel);
    }
    .segmented button {
      min-height: 36px;
      border: 0;
      border-right: 1px solid var(--line);
      background: transparent;
      color: var(--muted);
      font: inherit;
      font-size: 14px;
      cursor: pointer;
    }
    .segmented button:last-child {
      border-right: 0;
    }
    .segmented button[aria-pressed="true"] {
      background: var(--text);
      color: #fff;
      font-weight: 700;
    }
    .search input {
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 0 11px;
      background: var(--panel);
      color: var(--text);
      font: inherit;
      font-size: 14px;
    }
    .table-caption {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin: 10px 0;
      color: var(--muted);
      font-size: 13px;
    }
    .table-wrap {
      overflow-x: auto;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    table {
      width: 100%;
      min-width: 1120px;
      border-collapse: collapse;
    }
    th, td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      font-size: 14px;
    }
    th {
      color: var(--muted);
      font-weight: 600;
      background: #fbfbfc;
      white-space: nowrap;
    }
    th button {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      min-height: 28px;
      border: 0;
      border-radius: 6px;
      padding: 0 4px;
      background: transparent;
      color: inherit;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }
    th button[data-active="true"] {
      color: var(--text);
      background: var(--soft);
    }
    .sort-mark {
      display: inline-block;
      width: 12px;
      color: var(--accent);
      font-weight: 800;
    }
    td:first-child, th:first-child, td:nth-child(3), th:nth-child(3), td:nth-child(4), th:nth-child(4), td:nth-child(5), th:nth-child(5), td:nth-child(7), th:nth-child(7) {
      text-align: right;
      white-space: nowrap;
    }
    .title {
      min-width: 260px;
      font-weight: 600;
    }
    .muted {
      color: var(--muted);
      font-size: 12px;
      margin-top: 2px;
    }
    .climb {
      color: var(--rise);
      font-weight: 700;
    }
    .status, .tag {
      display: inline-block;
      min-width: 52px;
      text-align: center;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 7px;
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    .tag[data-tag="已选择"] {
      color: #1a7f37;
      border-color: #4ac26b;
      background: #ecfdf0;
    }
    .tag[data-tag="不要"] {
      color: #a40e26;
      border-color: #ffb3c1;
      background: #fff1f3;
    }
    a {
      color: var(--accent);
      text-decoration: none;
      font-weight: 600;
    }
    a:hover { text-decoration: underline; }
    .action-links {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-start;
      gap: 8px;
      min-width: 92px;
    }
    .action-links button {
      min-height: 24px;
      border: 0;
      border-radius: 6px;
      padding: 0;
      background: transparent;
      color: var(--accent);
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }
    .action-links button:hover {
      text-decoration: underline;
    }
    .empty {
      padding: 22px;
      text-align: center;
      color: var(--muted);
    }
    .note {
      color: var(--muted);
      font-size: 13px;
      margin-top: 14px;
    }
    @media (max-width: 760px) {
      main {
        padding: 20px 12px 36px;
      }
      .toolbar {
        grid-template-columns: 1fr;
      }
      .table-caption {
        display: block;
      }
    }
  </style>
</head>
<body>
  <main>
    <h1>$title</h1>
    <div class="meta">
      <span class="pill">生成时间：$generated</span>
      <span class="pill">榜单源：$snapshot_count 个</span>
      <span class="pill">入库记录：$book_count 本</span>
      <span class="pill">Top $top_count</span>
    </div>
    <section class="toolbar" aria-label="筛选和排序">
      <div class="control">
        <span class="control-label">作品状态</span>
        <div class="segmented" id="statusFilter">
          <button type="button" data-status="all" aria-pressed="true">全部</button>
          <button type="button" data-status="连载中" aria-pressed="false">连载中</button>
          <button type="button" data-status="已完结" aria-pressed="false">已完结</button>
        </div>
      </div>
      <div class="control">
        <span class="control-label">标注状态</span>
        <div class="segmented" id="tagFilter">
          <button type="button" data-tag="all" aria-pressed="true">全部</button>
          <button type="button" data-tag="待观察" aria-pressed="false">待观察</button>
          <button type="button" data-tag="已选择" aria-pressed="false">已选择</button>
          <button type="button" data-tag="不要" aria-pressed="false">不要</button>
        </div>
      </div>
      <label class="search">
        <span class="search-label">搜索</span>
        <input id="searchInput" type="search" placeholder="书名 / 作者 / 榜单">
      </label>
    </section>
    <div class="table-caption">
      <span id="visibleCount">0 本</span>
      <span id="sortState">默认按爬升降序</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th><button type="button" data-sort="latest_title">书名<span class="sort-mark"></span></button></th>
            <th><button type="button" data-sort="effective_climb" data-active="true">爬升<span class="sort-mark">↓</span></button></th>
            <th><button type="button" data-sort="selection_score">评分<span class="sort-mark"></span></button></th>
            <th><button type="button" data-sort="rank_pos">榜位<span class="sort-mark"></span></button></th>
            <th>榜单</th>
            <th><button type="button" data-sort="read_count">在读<span class="sort-mark"></span></button></th>
            <th>作品状态</th>
            <th>标注</th>
            <th>链接</th>
          </tr>
        </thead>
        <tbody id="bookRows"></tbody>
      </table>
    </div>
    <p class="note">注：番茄书名有字体混淆，报告里可能显示为怪字；链接和排名数据不受影响。</p>
  </main>
  <script type="application/json" id="report-data">$data_json</script>
  <script>
    const report = JSON.parse(document.getElementById("report-data").textContent);
    const books = Array.isArray(report.books) ? report.books : [];
    const state = {
      status: "all",
      tag: "all",
      search: "",
      sortKey: "effective_climb",
      sortDir: "desc"
    };
    const numberFormat = new Intl.NumberFormat("zh-CN");
    const bookRows = document.getElementById("bookRows");
    const visibleCount = document.getElementById("visibleCount");
    const sortState = document.getElementById("sortState");
    const sortLabels = {
      latest_title: "书名",
      effective_climb: "爬升",
      selection_score: "评分",
      rank_pos: "榜位",
      read_count: "在读"
    };

    function esc(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function numberValue(book, key) {
      if (key === "effective_climb") {
        return Number(book.effective_climb ?? book.rank_change_1d ?? book.rank_pos_diff ?? 0);
      }
      return Number(book[key] ?? 0);
    }

    function climbLabel(book) {
      const value = numberValue(book, "effective_climb");
      return value > 0 ? "+" + value : String(value);
    }

    function scoreLabel(book) {
      const value = numberValue(book, "selection_score");
      return Number.isFinite(value) ? value.toFixed(1).replace(/\\.0$$/, "") : "-";
    }

    function isGarbledTitleChar(char) {
      if (!char) return false;
      const code = char.codePointAt(0);
      return char === "\\uFFFD"
        || char === "□"
        || char === "■"
        || char === "▢"
        || char === "▯"
        || (code >= 0xE000 && code <= 0xF8FF)
        || (code >= 0xF0000 && code <= 0xFFFFD)
        || (code >= 0x100000 && code <= 0x10FFFD);
    }

    function cleanShortdramasKeyword(title) {
      const text = String(title || "").trim();
      if (!text) return "";
      let keyword = "";
      let started = false;
      let lastWasSpace = false;
      for (const char of text) {
        if (isGarbledTitleChar(char)) {
          if (started) break;
          continue;
        }
        if (/\\s/.test(char)) {
          if (started && keyword && !lastWasSpace) {
            keyword += " ";
            lastWasSpace = true;
          }
          continue;
        }
        const edgePunctuationChar = /^[\\s:：,，.。!！?？\\-—_·、|/\\\\()[\\]{}（）【】《》<>"'“”‘’]$$/;
        if (!started && edgePunctuationChar.test(char)) continue;
        keyword += char;
        started = true;
        lastWasSpace = false;
      }
      const edgePunctuation = /^[\\s:：,，.。!！?？\\-—_·、|/\\\\()[\\]{}（）【】《》<>"'“”‘’]+|[\\s:：,，.。!！?？\\-—_·、|/\\\\()[\\]{}（）【】《》<>"'“”‘’]+$$/g;
      const cleaned = keyword.replace(/\\s+/g, " ").trim().replace(edgePunctuation, "");
      return cleaned || text;
    }

    function shortdramasSearchUrl(title) {
      const keyword = cleanShortdramasKeyword(title);
      const params = new URLSearchParams({
        page_no: "1",
        search_keyword: keyword || ""
      });
      return "https://www.shortdramas.com/page/ip-application?" + params.toString();
    }

    function filteredBooks() {
      const needle = state.search.trim().toLowerCase();
      return books.filter((book) => {
        if (state.status !== "all" && book.creation_status_label !== state.status) return false;
        if (state.tag !== "all" && book.status_tag !== state.tag) return false;
        if (!needle) return true;
        const haystack = [
          book.latest_title,
          book.latest_author,
          book.source_name,
          book.book_id,
          book.note
        ].join(" ").toLowerCase();
        return haystack.includes(needle);
      });
    }

    function sortedBooks(items) {
      const sorted = [...items];
      const dir = state.sortDir === "asc" ? 1 : -1;
      sorted.sort((a, b) => {
        if (state.sortKey === "latest_title") {
          const compared = String(a.latest_title || "").localeCompare(String(b.latest_title || ""), "zh-Hans-CN");
          return compared * dir || Number(a.rank_pos || 0) - Number(b.rank_pos || 0);
        }
        const compared = numberValue(a, state.sortKey) - numberValue(b, state.sortKey);
        return compared * dir || Number(a.rank_pos || 0) - Number(b.rank_pos || 0);
      });
      return sorted;
    }

    function renderRows() {
      const items = sortedBooks(filteredBooks());
      visibleCount.textContent = numberFormat.format(items.length) + " 本";
      sortState.textContent = sortLabels[state.sortKey] + (state.sortDir === "asc" ? "升序" : "降序");
      if (!items.length) {
        bookRows.innerHTML = '<tr><td class="empty" colspan="10">暂无匹配数据</td></tr>';
        return;
      }
      bookRows.innerHTML = items.map((book, index) => {
        const url = book.latest_book_url || "";
        const link = url ? '<a href="' + esc(url) + '" target="_blank" rel="noopener">打开</a>' : "";
        const ipButton = '<button type="button" data-ip-index="' + index + '">搜IP</button>';
        const actions = '<div class="action-links">' + link + ipButton + "</div>";
        return "<tr>"
          + "<td>" + (index + 1) + "</td>"
          + '<td class="title">' + esc(book.latest_title || book.book_id) + '<div class="muted">' + esc(book.latest_author || "") + "</div></td>"
          + '<td class="climb">' + esc(climbLabel(book)) + "</td>"
          + "<td>" + esc(scoreLabel(book)) + "</td>"
          + "<td>" + esc(book.rank_pos || "-") + "</td>"
          + "<td>" + esc(book.source_name || "") + "</td>"
          + "<td>" + numberFormat.format(numberValue(book, "read_count")) + "</td>"
          + '<td><span class="status">' + esc(book.creation_status_label || "-") + "</span></td>"
          + '<td><span class="tag" data-tag="' + esc(book.status_tag || "") + '">' + esc(book.status_tag || "-") + "</span></td>"
          + "<td>" + actions + "</td>"
          + "</tr>";
      }).join("");
      bookRows.querySelectorAll("[data-ip-index]").forEach((button) => {
        button.addEventListener("click", () => {
          const book = items[Number(button.dataset.ipIndex)] || {};
          const suggestedTitle = cleanShortdramasKeyword(book.latest_title || book.book_id || "");
          const title = window.prompt("确认 ShortDramas 搜索书名", suggestedTitle);
          if (title === null) return;
          const keyword = title.trim();
          if (!keyword) return;
          window.open(shortdramasSearchUrl(keyword), "_blank", "noopener");
        });
      });
    }

    function setPressed(containerId, dataName, value) {
      document.querySelectorAll("#" + containerId + " [data-" + dataName + "]").forEach((button) => {
        button.setAttribute("aria-pressed", String(button.dataset[dataName] === value));
      });
    }

    document.querySelectorAll("[data-status]").forEach((button) => {
      button.addEventListener("click", () => {
        state.status = button.dataset.status;
        setPressed("statusFilter", "status", state.status);
        renderRows();
      });
    });

    document.querySelectorAll("[data-tag]").forEach((button) => {
      button.addEventListener("click", () => {
        state.tag = button.dataset.tag;
        setPressed("tagFilter", "tag", state.tag);
        renderRows();
      });
    });

    document.getElementById("searchInput").addEventListener("input", (event) => {
      state.search = event.target.value;
      renderRows();
    });

    document.querySelectorAll("[data-sort]").forEach((button) => {
      button.addEventListener("click", () => {
        const nextKey = button.dataset.sort;
        if (state.sortKey === nextKey) {
          state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
        } else {
          state.sortKey = nextKey;
          state.sortDir = nextKey === "rank_pos" || nextKey === "latest_title" ? "asc" : "desc";
        }
        document.querySelectorAll("[data-sort]").forEach((item) => {
          const active = item.dataset.sort === state.sortKey;
          item.dataset.active = String(active);
          item.querySelector(".sort-mark").textContent = active ? (state.sortDir === "asc" ? "↑" : "↓") : "";
        });
        renderRows();
      });
    });

    renderRows();
  </script>
</body>
</html>
""")
    return template.substitute(
        title=html.escape(title),
        generated=html.escape(str(data.get("generated_at") or "")),
        snapshot_count=str(safe_int(summary.get("snapshot_count"))),
        book_count=f"{safe_int(summary.get('book_count')):,}",
        top_count=str(safe_int(data.get("top"))),
        data_json=data_json,
    )


def resolve_output_path(path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path
    return ROOT / path


def write_static_report(
    db_path: Path,
    *,
    output_dir: Path,
    docs_dir: Path | None = None,
    top: int = 30,
    source_id: int | None = None,
    completed_only: bool = False,
) -> dict[str, str]:
    with connect_db(db_path) as conn:
        data = build_static_report_data(conn, top=top, source_id=source_id, completed_only=completed_only)
    markdown = build_markdown_report(data)
    html_report = build_html_report(data)
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = output_dir / "latest.md"
    html_path = output_dir / "latest.html"
    json_path = output_dir / "latest.json"
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(html_report, encoding="utf-8")
    json_path.write_bytes(json_bytes(data))

    result = {
        "markdown": str(markdown_path),
        "html": str(html_path),
        "json": str(json_path),
    }
    if docs_dir:
        docs_dir.mkdir(parents=True, exist_ok=True)
        docs_index = docs_dir / "index.html"
        docs_json = docs_dir / "latest.json"
        docs_index.write_text(html_report, encoding="utf-8")
        docs_json.write_bytes(json_bytes(data))
        result["docs_index"] = str(docs_index)
        result["docs_json"] = str(docs_json)
    return result


def build_feishu_report(
    conn: sqlite3.Connection,
    top: int = 10,
    source_id: int | None = None,
    completed_only: bool = False,
) -> str:
    return build_rank_report(conn, top=top, source_id=source_id, completed_only=completed_only)


def send_feishu_text(webhook: str, text: str, secret: str = "", timeout: int = 20) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "msg_type": "text",
        "content": {"text": text},
    }
    if secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = feishu_sign(secret, timestamp)

    request = urllib.request.Request(
        webhook,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    text_response = raw.decode(charset, errors="replace")
    try:
        return json.loads(text_response)
    except json.JSONDecodeError:
        return {"raw": text_response}


def push_feishu_report(
    db_path: Path,
    webhook: str,
    secret: str = "",
    top: int = 10,
    source_id: int | None = None,
    completed_only: bool = False,
    dry_run: bool = False,
    timeout: int = 20,
) -> dict[str, Any]:
    with connect_db(db_path) as conn:
        report = build_feishu_report(conn, top=top, source_id=source_id, completed_only=completed_only)
    if dry_run:
        print(report)
        return {"dry_run": True, "message": report}
    if not webhook:
        raise ValueError("缺少飞书 Webhook。请设置 FEISHU_WEBHOOK 或传入 --webhook。")
    response = send_feishu_text(webhook, report, secret=secret, timeout=timeout)
    return {"ok": True, "response": response}


def parse_bool(value: str, default: bool = False) -> bool:
    if value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def split_recipients(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[;,]", value) if part.strip()]


def send_email_text(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
    mail_from: str,
    mail_to: list[str],
    subject: str,
    body: str,
    use_ssl: bool = False,
    use_tls: bool = True,
    timeout: int = 30,
) -> dict[str, Any]:
    if not smtp_host:
        raise ValueError("缺少 SMTP_HOST。")
    if not mail_from:
        raise ValueError("缺少 EMAIL_FROM；也可以设置 SMTP_USERNAME 作为默认发件人。")
    if not mail_to:
        raise ValueError("缺少 EMAIL_TO。")

    message = EmailMessage()
    message["From"] = mail_from
    message["To"] = ", ".join(mail_to)
    message["Subject"] = subject
    message.set_content(body)

    smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_cls(smtp_host, smtp_port, timeout=timeout) as smtp:
        if not use_ssl and use_tls:
            smtp.starttls()
        if smtp_username or smtp_password:
            smtp.login(smtp_username, smtp_password)
        smtp.send_message(message)
    return {"sent": True, "to": mail_to, "subject": subject}


def push_email_report(
    db_path: Path,
    *,
    smtp_host: str,
    smtp_port: int = 587,
    smtp_username: str = "",
    smtp_password: str = "",
    mail_from: str = "",
    mail_to: str = "",
    subject: str = "",
    top: int = 10,
    source_id: int | None = None,
    completed_only: bool = False,
    dry_run: bool = False,
    use_ssl: bool = False,
    use_tls: bool = True,
    timeout: int = 30,
) -> dict[str, Any]:
    with connect_db(db_path) as conn:
        report = build_rank_report(conn, top=top, source_id=source_id, completed_only=completed_only)
    if not subject:
        subject = f"番茄榜单日报 Top {top}"
        if completed_only:
            subject += "（已完结）"
    if dry_run:
        print(f"Subject: {subject}\n")
        print(report)
        return {"dry_run": True, "message": report}

    recipients = split_recipients(mail_to)
    response = send_email_text(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        mail_from=mail_from or smtp_username,
        mail_to=recipients,
        subject=subject,
        body=report,
        use_ssl=use_ssl,
        use_tls=use_tls,
        timeout=timeout,
    )
    return {"ok": True, "response": response}


SERVER_DB = DEFAULT_DB
SERVER_DEFAULT_URL = DEFAULT_RANK_URL


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.send_file(APP_HTML)
        elif parsed.path == "/api/health":
            with connect_db(SERVER_DB) as conn:
                self.send_json({"ok": True, "summary": query_summary(conn)})
        elif parsed.path == "/api/sources":
            with connect_db(SERVER_DB) as conn:
                self.send_json({"sources": list_sources(conn)})
        elif parsed.path == "/api/summary":
            source_id = safe_int(urllib.parse.parse_qs(parsed.query).get("source_id", [0])[0]) or None
            with connect_db(SERVER_DB) as conn:
                self.send_json(query_summary(conn, source_id=source_id))
        elif parsed.path == "/api/books":
            qs = urllib.parse.parse_qs(parsed.query)
            source_id = safe_int(qs.get("source_id", [0])[0]) or None
            tag = qs.get("tag", [""])[0]
            search = qs.get("search", [""])[0]
            order = qs.get("order", ["fastest"])[0]
            creation_status = qs.get("creation_status", [""])[0]
            limit = safe_int(qs.get("limit", [200])[0], 200)
            with connect_db(SERVER_DB) as conn:
                books = query_books(
                    conn,
                    source_id=source_id,
                    tag=tag,
                    search=search,
                    order=order,
                    creation_status=creation_status,
                    limit=limit,
                )
                self.send_json({"books": [public_book(book) for book in books]})
        elif parsed.path == "/api/export.csv":
            qs = urllib.parse.parse_qs(parsed.query)
            source_id = safe_int(qs.get("source_id", [0])[0]) or None
            creation_status = qs.get("creation_status", [""])[0]
            with connect_db(SERVER_DB) as conn:
                path = export_csv(
                    conn,
                    SERVER_DB.parent / "rank_export.csv",
                    source_id=source_id,
                    creation_status=creation_status,
                )
            self.send_file(path)
        else:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/api/snapshot":
                result = capture_rank(
                    str(payload.get("url") or SERVER_DEFAULT_URL),
                    SERVER_DB,
                    limit=safe_int(payload.get("limit"), 100),
                    snapshot_date=str(payload.get("snapshot_date") or "") or None,
                    timeout=safe_int(payload.get("timeout"), 20),
                )
                self.send_json(result, HTTPStatus.CREATED)
                return
            tag_match = re.fullmatch(r"/api/books/([^/]+)/tag", parsed.path)
            if tag_match:
                with connect_db(SERVER_DB) as conn:
                    result = update_book_tag(
                        conn,
                        urllib.parse.unquote(tag_match.group(1)),
                        str(payload.get("status_tag") or "待观察"),
                        payload.get("note"),
                    )
                self.send_json(result)
                return
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def send_file(self, path: Path) -> None:
        if not path.exists():
            self.send_json({"error": "file not found"}, HTTPStatus.NOT_FOUND)
            return
        content = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.suffix == ".html":
            ctype = "text/html; charset=utf-8"
        elif path.suffix == ".json":
            ctype = "application/json; charset=utf-8"
        elif path.suffix == ".csv":
            ctype = "text/csv; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        content = json_bytes(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: Any) -> None:
        return


def print_report(db_path: Path, source_id: int | None, limit: int, creation_status: str = "") -> None:
    with connect_db(db_path) as conn:
        summary = query_summary(conn, source_id=source_id)
        books = query_books(conn, source_id=source_id, creation_status=creation_status, limit=limit)
    latest = summary.get("latest_snapshot") or {}
    print(f"数据库：{db_path}")
    print(f"最新快照：{latest.get('snapshot_date') or '-'}，记录：{len(books)}")
    print("排名 | 爬升 | 评分 | 首见涨幅 | 在读 | 完结 | 状态 | 书名")
    for item in books[:limit]:
        climb = item.get("rank_change_1d")
        if climb is None:
            climb = item.get("rank_pos_diff")
        print(
            f"{item['rank_pos']:>3} | {climb:>4} | {score_label(item):>4} | {item['total_climb']:>6} | "
            f"{item['read_count']:>8} | {item['creation_status_label']} | {item['status_tag']} | {item['latest_title']}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fanqie ranking tracker agent.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径。")
    sub = parser.add_subparsers(dest="command")

    snapshot = sub.add_parser("snapshot", help="抓取并写入一份每日榜单快照。")
    snapshot.add_argument("url", nargs="?", default=DEFAULT_RANK_URL)
    snapshot.add_argument("--limit", type=int, default=100)
    snapshot.add_argument("--date", dest="snapshot_date", default="")
    snapshot.add_argument("--timeout", type=int, default=20)

    snapshot_all = sub.add_parser("snapshot-all", help="发现并抓取排行榜菜单里的全部榜单。")
    snapshot_all.add_argument("url", nargs="?", default=DEFAULT_RANK_URL)
    snapshot_all.add_argument("--limit", type=int, default=100)
    snapshot_all.add_argument("--date", dest="snapshot_date", default="")
    snapshot_all.add_argument("--timeout", type=int, default=20)
    snapshot_all.add_argument("--gender", choices=["all", "male", "female"], default="all")
    snapshot_all.add_argument("--rank-mold", choices=["all", "read", "new"], default="all")

    discover = sub.add_parser("discover", help="列出排行榜菜单里的全部榜单 URL。")
    discover.add_argument("url", nargs="?", default=DEFAULT_RANK_URL)
    discover.add_argument("--timeout", type=int, default=20)

    serve = sub.add_parser("serve", help="启动本地榜单观察页面。")
    serve.add_argument("--url", default=DEFAULT_RANK_URL)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8791)

    report = sub.add_parser("report", help="输出最快爬榜书。")
    report.add_argument("--source-id", type=int, default=0)
    report.add_argument("--limit", type=int, default=30)
    report.add_argument("--completed-only", action="store_true", help="只显示已完结作品。")

    tag = sub.add_parser("tag", help="修改一本书的人工状态。")
    tag.add_argument("book_id")
    tag.add_argument("status_tag", choices=VALID_TAGS)
    tag.add_argument("--note", default=None)

    export = sub.add_parser("export", help="导出最新榜单 CSV。")
    export.add_argument("--source-id", type=int, default=0)
    export.add_argument("--completed-only", action="store_true", help="只导出已完结作品。")
    export.add_argument("--output", default="")

    static_report = sub.add_parser("static-report", help="生成 GitHub 可直接查看的静态日报页面。")
    static_report.add_argument("--output", default="reports")
    static_report.add_argument("--docs-output", default="docs")
    static_report.add_argument("--top", type=int, default=30)
    static_report.add_argument("--source-id", type=int, default=0)
    static_report.add_argument("--completed-only", action="store_true", help="只展示已完结作品。")

    feishu = sub.add_parser("feishu-push", aliases=["push-feishu"], help="推送爬榜日报到飞书自定义机器人。")
    feishu.add_argument("--webhook", default=os.environ.get("FEISHU_WEBHOOK", ""))
    feishu.add_argument("--secret", default=os.environ.get("FEISHU_SECRET", ""))
    feishu.add_argument("--top", type=int, default=10)
    feishu.add_argument("--source-id", type=int, default=0)
    feishu.add_argument("--completed-only", action="store_true", help="只推送已完结作品。")
    feishu.add_argument("--dry-run", action="store_true", help="只打印消息内容，不发送。")
    feishu.add_argument("--timeout", type=int, default=20)

    email_push = sub.add_parser("email-push", aliases=["push-email"], help="推送爬榜日报到邮箱。")
    email_push.add_argument("--smtp-host", default=os.environ.get("SMTP_HOST", ""))
    email_push.add_argument("--smtp-port", type=int, default=safe_int(os.environ.get("SMTP_PORT"), 587))
    email_push.add_argument("--smtp-username", default=os.environ.get("SMTP_USERNAME", ""))
    email_push.add_argument("--smtp-password", default=os.environ.get("SMTP_PASSWORD", ""))
    email_push.add_argument("--from", dest="mail_from", default=os.environ.get("EMAIL_FROM", ""))
    email_push.add_argument("--to", dest="mail_to", default=os.environ.get("EMAIL_TO", ""))
    email_push.add_argument("--subject", default=os.environ.get("EMAIL_SUBJECT", ""))
    email_push.add_argument("--top", type=int, default=10)
    email_push.add_argument("--source-id", type=int, default=0)
    email_push.add_argument("--completed-only", action="store_true", help="只推送已完结作品。")
    email_push.add_argument("--dry-run", action="store_true", help="只打印邮件内容，不发送。")
    email_push.add_argument("--use-ssl", action="store_true", default=parse_bool(os.environ.get("SMTP_USE_SSL", ""), False))
    email_push.add_argument("--no-tls", action="store_true", default=not parse_bool(os.environ.get("SMTP_USE_TLS", ""), True))
    email_push.add_argument("--timeout", type=int, default=30)
    return parser


def gender_filter(value: str) -> set[int] | None:
    if value == "male":
        return {1}
    if value == "female":
        return {0, 2}
    return None


def rank_mold_filter(value: str) -> set[int] | None:
    if value == "read":
        return {2}
    if value == "new":
        return {1}
    return None


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db_path = Path(args.db).expanduser()
    command = args.command or "serve"

    if command == "snapshot":
        result = capture_rank(
            args.url,
            db_path,
            limit=args.limit,
            snapshot_date=args.snapshot_date or None,
            timeout=args.timeout,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if command == "snapshot-all":
        result = capture_all_ranks(
            args.url,
            db_path,
            limit=args.limit,
            snapshot_date=args.snapshot_date or None,
            timeout=args.timeout,
            include_gender=gender_filter(args.gender),
            include_rank_mold=rank_mold_filter(args.rank_mold),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if command == "discover":
        sources = discover_rank_sources(args.url, timeout=args.timeout)
        for source in sources:
            print(f"{source.display_name}\t{source.url}")
        print(f"共 {len(sources)} 个榜单")
        return 0
    if command == "serve":
        global SERVER_DB, SERVER_DEFAULT_URL
        SERVER_DB = db_path
        SERVER_DEFAULT_URL = args.url
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        print(f"Fanqie rank tracker: http://{args.host}:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return 0
    if command == "report":
        print_report(db_path, args.source_id or None, args.limit, creation_status="0" if args.completed_only else "")
        return 0
    if command == "tag":
        with connect_db(db_path) as conn:
            print(json.dumps(update_book_tag(conn, args.book_id, args.status_tag, args.note), ensure_ascii=False, indent=2))
        return 0
    if command == "export":
        output = Path(args.output).expanduser() if args.output else db_path.parent / "rank_export.csv"
        with connect_db(db_path) as conn:
            path = export_csv(
                conn,
                output,
                source_id=args.source_id or None,
                creation_status="0" if args.completed_only else "",
            )
        print(path)
        return 0
    if command == "static-report":
        result = write_static_report(
            db_path,
            output_dir=resolve_output_path(args.output),
            docs_dir=resolve_output_path(args.docs_output) if args.docs_output else None,
            top=args.top,
            source_id=args.source_id or None,
            completed_only=args.completed_only,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if command in {"feishu-push", "push-feishu"}:
        result = push_feishu_report(
            db_path,
            webhook=args.webhook,
            secret=args.secret,
            top=args.top,
            source_id=args.source_id or None,
            completed_only=args.completed_only,
            dry_run=args.dry_run,
            timeout=args.timeout,
        )
        if not args.dry_run:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if command in {"email-push", "push-email"}:
        result = push_email_report(
            db_path,
            smtp_host=args.smtp_host,
            smtp_port=args.smtp_port,
            smtp_username=args.smtp_username,
            smtp_password=args.smtp_password,
            mail_from=args.mail_from,
            mail_to=args.mail_to,
            subject=args.subject,
            top=args.top,
            source_id=args.source_id or None,
            completed_only=args.completed_only,
            dry_run=args.dry_run,
            use_ssl=args.use_ssl,
            use_tls=not args.no_tls,
            timeout=args.timeout,
        )
        if not args.dry_run:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
