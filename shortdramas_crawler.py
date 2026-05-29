#!/usr/bin/env python3
"""Collect visible ShortDramas IP preview chapters with a logged-in browser."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEFAULT_PROFILE = Path(".browser_profiles/shortdramas").resolve()


def clean_text(value: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", value.replace("\r\n", "\n")).strip()


def slugify(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|#]+", "_", value)
    value = re.sub(r"\s+", " ", value).strip("._ ")
    return value[:90] or "untitled"


def is_shortdramas_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.netloc.endswith("shortdramas.com")


def parse_preview_ids(url: str) -> dict[str, str]:
    match = re.search(r"/ip-chapter-preview/([^/]+)/([^/]+)/([^/?#]+)", url)
    if not match:
        return {}
    return {"ip_id": match.group(1), "chapter_id": match.group(2), "adaptation_type": match.group(3)}


def wait_for_detail_page(page, timeout_ms: int) -> None:
    print("Waiting for ShortDramas detail page. Log in in the opened browser if needed.", flush=True)
    page.wait_for_function(
        """() => document.querySelectorAll('.ip-category-item').length > 0""",
        timeout=timeout_ms,
    )


def read_detail_catalog(page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
            const body = document.body.innerText || '';
            const matchTitle = body.match(/作品详情\\s*·\\s*[^\\n]+\\n([^\\n]+)/);
            return {
                work_title: matchTitle?.[1]?.trim() || '',
                url: location.href,
                chapters: Array.from(document.querySelectorAll('.ip-category-item')).map((el, i) => ({
                    index: i + 1,
                    title: el.textContent.trim().replace(/\\s+/g, ' ')
                }))
            };
        }"""
    )


def read_preview(page, expected_title: str, timeout_ms: int) -> dict[str, Any]:
    page.wait_for_function(
        """(expectedTitle) => {
            const title = document.querySelector('.preview-content-title')?.textContent?.trim() || '';
            const content = document.querySelector('.preview-content-script')?.innerText?.trim() || '';
            return location.href.includes('/ip-chapter-preview/') && title === expectedTitle && content.length > 0;
        }""",
        expected_title,
        timeout=timeout_ms,
    )
    data = page.evaluate(
        """() => {
            const text = (sel) => document.querySelector(sel)?.textContent?.trim() || '';
            const contentEl = document.querySelector('.preview-content-script');
            let workTitle = text('.serial-container-header-title') || '';
            try { workTitle = workTitle || new URL(location.href).searchParams.get('title') || ''; } catch {}
            return {
                url: location.href,
                page_title: document.title,
                work_title: workTitle,
                chapter_title: text('.preview-content-title'),
                content_text: contentEl?.innerText || '',
                fetched_at: new Date().toISOString()
            };
        }"""
    )
    data["content_text"] = clean_text(data.get("content_text", ""))
    data["content_length"] = len(data["content_text"])
    data.update(parse_preview_ids(data.get("url", "")))
    return data


def write_outputs(output_dir: Path, source_url: str, started_at: str, work_title: str, catalog: list[dict[str, Any]], chapters: list[dict[str, Any]]) -> None:
    chapters_dir = output_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)

    for chapter in chapters:
        filename = f"{chapter['index']:03d}_{slugify(chapter.get('chapter_title') or chapter.get('catalog_title'))}.md"
        md = "\n".join(
            [
                f"# {chapter.get('chapter_title') or chapter.get('catalog_title')}",
                "",
                f"- Source URL: {chapter.get('url', '')}",
                f"- Chapter ID: {chapter.get('chapter_id', '')}",
                f"- Captured at: {chapter.get('fetched_at', started_at)}",
                f"- Content length: {chapter.get('content_length', 0)}",
                f"- Warning: {chapter.get('warning', '')}",
                "",
                chapter.get("content_text", ""),
                "",
            ]
        )
        (chapters_dir / filename).write_text(md, encoding="utf-8")

    catalog_rows = [
        {
            "index": item.get("index"),
            "title": item.get("chapter_title") or item.get("catalog_title") or item.get("title"),
            "url": item.get("url", ""),
            "ip_id": item.get("ip_id", ""),
            "chapter_id": item.get("chapter_id", ""),
            "adaptation_type": item.get("adaptation_type", ""),
            "content_length": item.get("content_length", 0),
            "warning": item.get("warning", ""),
        }
        for item in chapters
    ]
    if not catalog_rows:
        catalog_rows = catalog

    combined = [
        f"# {work_title or 'ShortDramas IP'} - ShortDramas 登录态章节采集",
        "",
        f"- Source detail URL: {source_url}",
        f"- Captured at: {started_at}",
        f"- Chapters captured: {len(chapters)}",
        "",
    ]
    for chapter in chapters:
        combined.extend(
            [
                f"## {chapter['index']:03d} {chapter.get('chapter_title') or chapter.get('catalog_title')}",
                "",
                chapter.get("content_text", ""),
                "",
            ]
        )

    warnings = [
        {"index": item.get("index"), "title": item.get("chapter_title") or item.get("catalog_title"), "warning": item.get("warning")}
        for item in chapters
        if item.get("warning")
    ]
    summary = {
        "source": "shortdramas.com logged-in browser page",
        "source_detail_url": source_url,
        "work_title": work_title,
        "captured_at": started_at,
        "output_base": str(output_dir),
        "catalog_count_on_detail_page": len(catalog),
        "chapter_count": len(chapters),
        "completed_chapter_count": sum(1 for item in chapters if item.get("content_length", 0) > 0 and not item.get("warning")),
        "total_characters": sum(item.get("content_length", 0) for item in chapters),
        "min_content_length": min([item.get("content_length", 0) for item in chapters], default=0),
        "max_content_length": max([item.get("content_length", 0) for item in chapters], default=0),
        "warnings": warnings,
        "files": {
            "catalog": str(output_dir / "catalog.json"),
            "chapters_json": str(output_dir / "chapters.json"),
            "combined_markdown": str(output_dir / "book_preview_clean.md"),
            "summary": str(output_dir / "run_summary.json"),
            "chapters_dir": str(chapters_dir),
        },
        "catalog": catalog_rows,
    }

    (output_dir / "catalog.json").write_text(json.dumps(catalog_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "chapters.json").write_text(json.dumps(chapters, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "book_preview_clean.md").write_text("\n".join(combined).strip() + "\n", encoding="utf-8")
    (output_dir / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect ShortDramas preview chapters from a logged-in browser page.")
    parser.add_argument("url", help="ShortDramas IP detail URL.")
    parser.add_argument("--output", default="shortdramas_kb", help="Output directory.")
    parser.add_argument("--chrome", default=DEFAULT_CHROME, help="Chrome executable path.")
    parser.add_argument("--user-data-dir", default=str(DEFAULT_PROFILE), help="Persistent browser profile directory.")
    parser.add_argument("--login-timeout", type=int, default=300, help="Seconds to wait for login/detail page readiness.")
    parser.add_argument("--start", type=int, default=1, help="First visible chapter order to capture.")
    parser.add_argument("--end", type=int, default=0, help="Last visible chapter order to capture; 0 means no upper bound.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum chapters to capture after filtering; 0 means all.")
    parser.add_argument("--catalog-only", action="store_true", help="Only write the visible chapter catalog.")
    parser.add_argument("--headless", action="store_true", help="Run browser headlessly. Use only when the profile is already logged in.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not is_shortdramas_url(args.url):
        raise SystemExit("URL must be under shortdramas.com.")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = dt.datetime.now().isoformat(timespec="seconds")

    with sync_playwright() as playwright:
        profile_dir = Path(args.user_data_dir).expanduser()
        profile_dir.mkdir(parents=True, exist_ok=True)
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            executable_path=args.chrome,
            headless=args.headless,
            viewport={"width": 1400, "height": 1000},
            locale="zh-CN",
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
        try:
            wait_for_detail_page(page, args.login_timeout * 1000)
        except PlaywrightTimeoutError:
            context.close()
            raise SystemExit("Timed out waiting for ShortDramas chapters. Log in in the opened browser, then retry.")

        detail = read_detail_catalog(page)
        catalog = detail["chapters"]
        work_title = detail.get("work_title") or "ShortDramas IP"
        (output_dir / "catalog_from_detail.json").write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Catalog chapters: {len(catalog)}", flush=True)

        filtered = [item for item in catalog if item["index"] >= args.start]
        if args.end:
            filtered = [item for item in filtered if item["index"] <= args.end]
        if args.limit:
            filtered = filtered[: args.limit]

        if args.catalog_only:
            write_outputs(output_dir, args.url, started_at, work_title, catalog, [])
            context.close()
            print(f"Done: {output_dir}", flush=True)
            return 0

        chapters: list[dict[str, Any]] = []
        for order, entry in enumerate(filtered, start=1):
            print(f"[{order}/{len(filtered)}] Capture: {entry['title']}", flush=True)
            page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
            wait_for_detail_page(page, args.login_timeout * 1000)
            locator = page.locator(".ip-category-item").nth(entry["index"] - 1)
            locator.scroll_into_view_if_needed(timeout=10000)
            locator.click(timeout=15000)
            try:
                chapter = read_preview(page, entry["title"], 20000)
                chapter["warning"] = ""
            except PlaywrightTimeoutError:
                chapter = {
                    "index": entry["index"],
                    "catalog_title": entry["title"],
                    "chapter_title": entry["title"],
                    "url": page.url,
                    "content_text": "",
                    "content_length": 0,
                    "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "warning": "preview_not_ready_or_empty",
                }
            chapter["index"] = entry["index"]
            chapter["catalog_title"] = entry["title"]
            chapter["source_detail_url"] = args.url
            chapters.append(chapter)
            write_outputs(output_dir, args.url, started_at, work_title, catalog, chapters)

        context.close()

    write_outputs(output_dir, args.url, started_at, work_title, catalog, chapters)
    print(f"Done: {output_dir}", flush=True)
    return 0 if chapters else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
