#!/usr/bin/env python3
"""Render Fanqie reader chapters, screenshot them, and OCR the visible text.

This is a fallback path for reader pages whose HTML text is font-obfuscated.
It captures rendered pages in Chrome, then uses macOS Vision OCR through ocrmac.
"""

from __future__ import annotations

import argparse
import dataclasses
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, List, Optional

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

try:
    from ocrmac import ocrmac
except ImportError:  # pragma: no cover - exercised by users without optional dep.
    ocrmac = None


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
UI_LINES = {
    "登录",
    "目录",
    "夜间",
    "字号",
    "下载",
    "加书架",
    "上一章",
    "下一章",
    "试试使用键盘左右键切章吧！",
}
UI_LINE_PATTERNS = [
    re.compile(r"试试使用键盘左右键切.吧"),
    re.compile(r"扫码下载.*APP.*读"),
    re.compile(r"番茄小说APP"),
    re.compile(r"会员登录后"),
    re.compile(r"网页畅读全文"),
    re.compile(r"^去下载$"),
]


@dataclasses.dataclass
class ChapterLink:
    index: int
    title: str
    url: str


class ReaderLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: List[tuple[str, str]] = []
        self._href: Optional[str] = None
        self._text_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        attr = dict(attrs)
        href = attr.get("href") or ""
        if tag == "a" and href.startswith("/reader/"):
            self._href = href
            self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            title = clean_text("".join(self._text_parts))
            self.links.append((title, self._href))
            self._href = None
            self._text_parts = []


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def fetch_html(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def chapter_number(title: str) -> Optional[int]:
    match = re.search(r"第\s*(\d+)\s*章", title)
    if match:
        return int(match.group(1))
    return None


def canonical_title(title: str) -> str:
    return re.sub(r"^最近更新：", "", title).strip()


def extract_catalog(url: str, timeout: int = 20) -> List[ChapterLink]:
    parser = ReaderLinkParser()
    parser.feed(fetch_html(url, timeout=timeout))
    by_url: dict[str, ChapterLink] = {}
    for title, href in parser.links:
        title = canonical_title(title)
        index = chapter_number(title)
        if index is None:
            continue
        full_url = urllib.parse.urljoin(url, href)
        existing = by_url.get(full_url)
        if existing and not title.startswith("最近更新"):
            existing.title = title
            existing.index = index
            continue
        by_url.setdefault(full_url, ChapterLink(index=index, title=title, url=full_url))
    return sorted(by_url.values(), key=lambda item: (item.index, item.url))


def slugify(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|#]+", "_", value)
    value = re.sub(r"\s+", "_", value).strip("._ ")
    return value[:90] or "chapter"


def screenshot_chapter(page, chapter: ChapterLink, output_dir: Path, step: int, wait_ms: int) -> dict:
    chapter_dir = output_dir / f"{chapter.index:03d}_{slugify(chapter.title)}"
    screenshot_dir = chapter_dir / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    page.goto(chapter.url, wait_until="load", timeout=30000)
    page.wait_for_selector(".muye-reader-title", timeout=15000)
    page.wait_for_timeout(wait_ms)
    browser_title = page.locator(".muye-reader-title").inner_text(timeout=5000).strip()
    scroll_info = page.evaluate(
        """() => {
            const el = document.querySelector('.muye-reader') || document.scrollingElement;
            return {scrollHeight: el.scrollHeight, clientHeight: el.clientHeight};
        }"""
    )
    max_scroll = max(0, int(scroll_info["scrollHeight"]) - int(scroll_info["clientHeight"]))
    positions = list(range(0, max_scroll + 1, step))
    if positions[-1] != max_scroll:
        positions.append(max_scroll)

    screenshots = []
    for shot_index, y in enumerate(positions, start=1):
        page.evaluate(
            """(scrollTop) => {
                const el = document.querySelector('.muye-reader') || document.scrollingElement;
                el.scrollTop = scrollTop;
            }""",
            y,
        )
        page.wait_for_timeout(wait_ms)
        path = screenshot_dir / f"page_{shot_index:03d}.png"
        page.screenshot(path=str(path), full_page=False)
        screenshots.append({"index": shot_index, "scroll_top": y, "path": str(path)})

    metadata = {
        "index": chapter.index,
        "title": browser_title or chapter.title,
        "catalog_title": chapter.title,
        "url": chapter.url,
        "scroll_height": scroll_info["scrollHeight"],
        "client_height": scroll_info["clientHeight"],
        "screenshots": screenshots,
    }
    (chapter_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def normalized_for_dedupe(value: str) -> str:
    return "".join(value.split())


def is_ui_line(line: str) -> bool:
    return line in UI_LINES or any(pattern.search(line) for pattern in UI_LINE_PATTERNS)


def ocr_screenshots(chapter_dir: Path) -> dict:
    if ocrmac is None:
        raise RuntimeError("ocrmac is not installed. Install it before running OCR.")

    metadata = json.loads((chapter_dir / "metadata.json").read_text(encoding="utf-8"))
    pages = []
    kept_lines: List[str] = []
    blocked_lines: List[str] = []
    recent: List[str] = []

    for screenshot in metadata["screenshots"]:
        rows = ocrmac.OCR(
            screenshot["path"],
            language_preference=["zh-Hans"],
            recognition_level="accurate",
        ).recognize()
        page_lines = []
        for text, confidence, bbox in rows:
            line = clean_text(str(text))
            if not line:
                continue
            x, y, _width, _height = bbox
            if x > 0.84:
                continue
            if y > 0.90 and screenshot["index"] != 1:
                continue
            if is_ui_line(line):
                blocked_lines.append(line)
                continue
            if len(line) <= 2 and confidence < 0.8:
                continue
            page_lines.append(line)

        pages.append({"page": screenshot["index"], "image": screenshot["path"], "lines": page_lines})
        for line in page_lines:
            key = normalized_for_dedupe(line)
            if not key or key in recent:
                continue
            kept_lines.append(line)
            recent.append(key)
            recent = recent[-80:]

    if kept_lines and chapter_number(kept_lines[0]) is None:
        kept_lines.insert(0, metadata["title"])

    raw_text = "\n".join(kept_lines).strip() + "\n"
    body = "\n".join(kept_lines[1:] if kept_lines and kept_lines[0] == metadata["title"] else kept_lines).strip()
    warning = ""
    if blocked_lines:
        warning = "> OCR 状态：网页端出现下载/登录拦截，本章仅包含当前可见片段，非完整正文。\n\n"
    clean_md = "# " + metadata["title"] + "\n\n" + warning + body + "\n"
    (chapter_dir / "ocr_pages.json").write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")
    (chapter_dir / "chapter_raw.txt").write_text(raw_text, encoding="utf-8")
    (chapter_dir / "chapter_clean.md").write_text(clean_md, encoding="utf-8")

    metadata["ocr"] = {
        "line_count": len(kept_lines),
        "char_count": len(clean_md),
        "raw_text": str(chapter_dir / "chapter_raw.txt"),
        "clean_markdown": str(chapter_dir / "chapter_clean.md"),
        "ocr_pages": str(chapter_dir / "ocr_pages.json"),
        "content_blocked": bool(blocked_lines),
        "blocked_line_count": len(blocked_lines),
    }
    (chapter_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def combine_chapters(metadata: Iterable[dict], output_dir: Path) -> None:
    ordered = sorted(metadata, key=lambda item: item["index"])
    book_parts = ["# OCR 小说正文", ""]
    index_rows = []
    for item in ordered:
        chapter_dir = output_dir / f"{item['index']:03d}_{slugify(item['title'])}"
        clean_path = chapter_dir / "chapter_clean.md"
        if clean_path.exists():
            book_parts.append(clean_path.read_text(encoding="utf-8").strip())
            book_parts.append("")
        index_rows.append(item)
    summary = {
        "chapters": len(ordered),
        "complete_or_visible_chapters": sum(
            1 for item in ordered if item.get("ocr") and not item.get("ocr", {}).get("content_blocked")
        ),
        "blocked_or_truncated_chapters": [
            item["index"] for item in ordered if item.get("ocr", {}).get("content_blocked")
        ],
        "ocr_error_chapters": [item["index"] for item in ordered if item.get("ocr_error")],
        "total_clean_chars": sum(item.get("ocr", {}).get("char_count", 0) for item in ordered),
    }
    (output_dir / "book_ocr_clean.md").write_text("\n".join(book_parts).strip() + "\n", encoding="utf-8")
    (output_dir / "book_ocr_index.json").write_text(json.dumps(index_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "book_ocr_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OCR Fanqie reader chapters from rendered screenshots.")
    parser.add_argument("url", help="Fanqie book page URL.")
    parser.add_argument("--output", default="fanqie_book_ocr", help="Output directory.")
    parser.add_argument("--chrome", default=DEFAULT_CHROME, help="Chrome executable path.")
    parser.add_argument("--timeout", type=int, default=20, help="Catalog fetch timeout.")
    parser.add_argument("--start", type=int, default=1, help="First chapter number to process.")
    parser.add_argument("--end", type=int, default=0, help="Last chapter number to process; 0 means no upper bound.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum chapters to process after filtering; 0 means all.")
    parser.add_argument("--step", type=int, default=620, help="Scroll step in pixels; lower values increase overlap.")
    parser.add_argument("--wait-ms", type=int, default=250, help="Wait after navigation/scroll before screenshot.")
    parser.add_argument("--skip-existing", action="store_true", help="Skip chapters that already have chapter_clean.md.")
    parser.add_argument("--screenshots-only", action="store_true", help="Do not run OCR after screenshot capture.")
    parser.add_argument("--catalog-only", action="store_true", help="Only extract and write the chapter catalog.")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    chapters = extract_catalog(args.url, timeout=args.timeout)
    chapters = [chapter for chapter in chapters if chapter.index >= args.start]
    if args.end:
        chapters = [chapter for chapter in chapters if chapter.index <= args.end]
    if args.limit:
        chapters = chapters[: args.limit]
    (output_dir / "catalog.json").write_text(
        json.dumps([dataclasses.asdict(chapter) for chapter in chapters], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Catalog chapters: {len(chapters)}")
    if args.catalog_only:
        summary = {
            "chapters": len(chapters),
            "mode": "catalog_only",
            "complete_or_visible_chapters": 0,
            "blocked_or_truncated_chapters": [],
            "ocr_error_chapters": [],
            "total_clean_chars": 0,
        }
        (output_dir / "book_ocr_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (output_dir / "book_ocr_index.json").write_text("[]\n", encoding="utf-8")
        print(f"Done: {output_dir}")
        return 0 if chapters else 2

    processed = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=args.chrome)
        page = browser.new_page(
            viewport={"width": 1280, "height": 720},
            device_scale_factor=1,
            user_agent=DEFAULT_USER_AGENT,
            locale="zh-CN",
        )
        for order, chapter in enumerate(chapters, start=1):
            chapter_dir = output_dir / f"{chapter.index:03d}_{slugify(chapter.title)}"
            if args.skip_existing and (chapter_dir / "chapter_clean.md").exists():
                print(f"[{order}/{len(chapters)}] Skip existing: {chapter.title}", flush=True)
                processed.append(json.loads((chapter_dir / "metadata.json").read_text(encoding="utf-8")))
                continue
            print(f"[{order}/{len(chapters)}] Screenshot: {chapter.title}", flush=True)
            try:
                metadata = screenshot_chapter(page, chapter, output_dir, args.step, args.wait_ms)
            except PlaywrightTimeoutError as exc:
                print(f"  Warning: screenshot failed for {chapter.url}: {exc}", file=sys.stderr, flush=True)
                continue
            if not args.screenshots_only:
                print(f"[{order}/{len(chapters)}] OCR: {chapter.title}", flush=True)
                try:
                    metadata = ocr_screenshots(chapter_dir)
                except Exception as exc:  # Keep later chapters moving if one OCR pass fails.
                    metadata["ocr_error"] = str(exc)
                    (chapter_dir / "metadata.json").write_text(
                        json.dumps(metadata, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    print(f"  Warning: OCR failed for {chapter.url}: {exc}", file=sys.stderr, flush=True)
            processed.append(metadata)
            time.sleep(0.3)
        browser.close()

    combine_chapters(processed, output_dir)
    print(f"Done: {output_dir}")
    return 0 if processed else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
