#!/usr/bin/env python3
"""Collect child documents from a Feishu Wiki tree with rendered-page scrolling."""

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
DEFAULT_PROFILE = Path(".browser_profiles/feishu").resolve()


def clean_text(value: str) -> str:
    return (
        value.replace("\u200b", "")
        .replace("\u00a0", " ")
        .replace("\r\n", "\n")
        .strip()
    )


def slugify(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|#]+", "_", value)
    value = re.sub(r"\s+", " ", value).strip("._ ")
    return value[:90] or "untitled"


def is_feishu_wiki_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.netloc.endswith("feishu.cn") and parsed.path.startswith("/wiki/")


def wait_for_tree(page, timeout_ms: int) -> None:
    print("Waiting for Feishu Wiki tree. Log in or request access in the opened browser if needed.", flush=True)
    page.wait_for_function(
        """() => document.querySelectorAll('.workspace-tree-view-node-wrapper.workspace-tree-node').length > 0""",
        timeout=timeout_ms,
    )


def read_tree(page) -> list[dict[str, Any]]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('.workspace-tree-view-node-wrapper.workspace-tree-node'))
            .map((el, i) => ({ index: i, title: el.textContent.trim().replace(/\\s+/g, ' ') }))
            .filter(item => item.title)"""
    )


def scroll_to_top(page) -> None:
    page.evaluate(
        """() => {
            const scroller = document.querySelector('.bear-web-x-container.docx-in-wiki')
                || document.querySelector('.bear-web-x-container')
                || document.scrollingElement;
            if (scroller) scroller.scrollTop = 0;
        }"""
    )
    page.wait_for_timeout(500)


def read_visible_doc_blocks(page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
            const clean = (value) => String(value || '').replace(/\\u200b/g, '').replace(/\\u00a0/g, ' ').trim();
            const scroller = document.querySelector('.bear-web-x-container.docx-in-wiki')
                || document.querySelector('.bear-web-x-container')
                || document.scrollingElement;
            const blocks = Array.from(document.querySelectorAll(
                '.page-main-item.editor h1.page-block-content, .page-main-item.editor .block.docx-text-block'
            )).map((el, domIndex) => {
                const text = clean(el.innerText || el.textContent || '');
                return {
                    domIndex,
                    text,
                    blockId: el.getAttribute('data-block-id') || '',
                    recordId: el.getAttribute('data-record-id') || '',
                    isTitle: el.tagName === 'H1'
                };
            });
            const title = clean(
                document.querySelector('.page-main-item.editor h1.page-block-content')?.innerText
                || document.title.replace(/ - 飞书云文档$/, '')
            );
            return {
                url: location.href,
                page_title: document.title,
                doc_title: title,
                scrollTop: scroller?.scrollTop || 0,
                scrollHeight: scroller?.scrollHeight || 0,
                clientHeight: scroller?.clientHeight || 0,
                blocks
            };
        }"""
    )


def scroll_down(page, amount: int) -> None:
    page.evaluate(
        """(amount) => {
            const scroller = document.querySelector('.bear-web-x-container.docx-in-wiki')
                || document.querySelector('.bear-web-x-container')
                || document.scrollingElement;
            if (scroller) scroller.scrollTop += amount;
        }""",
        amount,
    )
    page.wait_for_timeout(500)


def collect_current_doc(page, scroll_step: int, max_steps: int) -> dict[str, Any]:
    scroll_to_top(page)
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    stagnant = 0
    last_top = -1.0
    last_state: dict[str, Any] = {}

    for step in range(max_steps):
        state = read_visible_doc_blocks(page)
        last_state = state
        for block in state["blocks"]:
            text = clean_text(block.get("text", ""))
            if not text:
                continue
            key = block.get("recordId") or (f"title:{text}" if block.get("isTitle") else f"block:{block.get('blockId')}:{text}")
            if key in seen:
                continue
            seen.add(key)
            block["text"] = text
            ordered.append(block)

        scroll_top = float(state.get("scrollTop") or 0)
        scroll_height = float(state.get("scrollHeight") or 0)
        client_height = float(state.get("clientHeight") or 0)
        if scroll_height and scroll_top + client_height >= scroll_height - 24:
            break
        if abs(scroll_top - last_top) < 2:
            stagnant += 1
        else:
            stagnant = 0
        if stagnant >= 4 and step > 4:
            break
        last_top = scroll_top
        scroll_down(page, scroll_step)

    title = clean_text(last_state.get("doc_title") or "")
    if not title:
        title = next((item["text"] for item in ordered if item.get("isTitle")), "")
    lines = [item["text"] for item in ordered if not item.get("isTitle") or item["text"] != title]
    content = re.sub(r"\n{4,}", "\n\n", "\n\n".join(lines)).strip()
    return {
        "title": title,
        "url": last_state.get("url") or page.url,
        "page_title": last_state.get("page_title") or page.title(),
        "content": content,
        "content_length": len(content),
        "block_count": len(ordered),
        "scroll": {
            "scrollTop": last_state.get("scrollTop", 0),
            "scrollHeight": last_state.get("scrollHeight", 0),
            "clientHeight": last_state.get("clientHeight", 0),
        },
        "collected_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def write_outputs(output_dir: Path, source_url: str, started_at: str, tree: list[dict[str, Any]], docs: list[dict[str, Any]]) -> None:
    docs_dir = output_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    for position, doc in enumerate(docs, start=1):
        filename = f"{position:03d}_{slugify(doc.get('title', ''))}.md"
        doc["file"] = str(docs_dir / filename)
        md = "\n".join(
            [
                f"# {doc.get('title', '')}",
                "",
                f"- Source URL: {doc.get('url', '')}",
                f"- Captured at: {doc.get('collected_at', started_at)}",
                f"- Content length: {doc.get('content_length', 0)}",
                f"- Block count: {doc.get('block_count', 0)}",
                "",
                doc.get("content", ""),
                "",
            ]
        )
        (docs_dir / filename).write_text(md, encoding="utf-8")

    catalog = [
        {
            "index": i + 1,
            "tree_index": doc.get("tree_index"),
            "title": doc.get("title"),
            "url": doc.get("url", ""),
            "content_length": doc.get("content_length", 0),
            "block_count": doc.get("block_count", 0),
            "file": doc.get("file", ""),
        }
        for i, doc in enumerate(docs)
    ]

    combined = [
        "# 飞书 Wiki 小说采集",
        "",
        f"- Source URL: {source_url}",
        f"- Captured at: {started_at}",
        f"- Root title: {tree[0]['title'] if tree else ''}",
        f"- Child docs captured: {len(docs)}",
        "",
    ]
    for i, doc in enumerate(docs, start=1):
        combined.extend([f"## {i:03d} {doc.get('title', '')}", "", f"- Source URL: {doc.get('url', '')}", "", doc.get("content", ""), ""])

    warnings = [
        {"tree_index": doc.get("tree_index"), "title": doc.get("title"), "warning": "empty_content"}
        for doc in docs
        if not doc.get("content_length")
    ]
    summary = {
        "source": "feishu wiki browser page",
        "source_url": source_url,
        "root_title": tree[0]["title"] if tree else "",
        "captured_at": started_at,
        "output_base": str(output_dir),
        "tree_count": len(tree),
        "child_doc_count_in_tree": max(len(tree) - 1, 0),
        "captured_doc_count": len(docs),
        "total_characters": sum(doc.get("content_length", 0) for doc in docs),
        "min_content_length": min([doc.get("content_length", 0) for doc in docs], default=0),
        "max_content_length": max([doc.get("content_length", 0) for doc in docs], default=0),
        "warnings": warnings,
        "files": {
            "combined_markdown": str(output_dir / "feishu_wiki_clean.md"),
            "docs_json": str(output_dir / "docs.json"),
            "catalog_json": str(output_dir / "catalog.json"),
            "tree_json": str(output_dir / "tree.json"),
            "summary": str(output_dir / "run_summary.json"),
            "docs_dir": str(docs_dir),
        },
        "catalog": catalog,
    }

    (output_dir / "feishu_wiki_clean.md").write_text("\n".join(combined).strip() + "\n", encoding="utf-8")
    (output_dir / "docs.json").write_text(json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "tree.json").write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect Feishu Wiki child docs with browser rendering.")
    parser.add_argument("url", help="Feishu Wiki URL.")
    parser.add_argument("--output", default="feishu_wiki_crawl", help="Output directory.")
    parser.add_argument("--chrome", default=DEFAULT_CHROME, help="Chrome executable path.")
    parser.add_argument("--user-data-dir", default=str(DEFAULT_PROFILE), help="Persistent browser profile directory.")
    parser.add_argument("--login-timeout", type=int, default=300, help="Seconds to wait for wiki access.")
    parser.add_argument("--start", type=int, default=1, help="First child document order to capture.")
    parser.add_argument("--end", type=int, default=0, help="Last child document order to capture; 0 means no upper bound.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum child docs to capture after filtering; 0 means all.")
    parser.add_argument("--step", type=int, default=650, help="Scroll step for rendered document collection.")
    parser.add_argument("--max-steps", type=int, default=100, help="Maximum scroll samples per document.")
    parser.add_argument("--catalog-only", action="store_true", help="Only write the visible wiki tree/catalog.")
    parser.add_argument("--headless", action="store_true", help="Run browser headlessly. Use only when the profile already has access.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not is_feishu_wiki_url(args.url):
        raise SystemExit("URL must be a my.feishu.cn/wiki/... page.")

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
            wait_for_tree(page, args.login_timeout * 1000)
        except PlaywrightTimeoutError:
            context.close()
            raise SystemExit("Timed out waiting for Feishu Wiki tree. Log in or request access in the opened browser, then retry.")

        tree = read_tree(page)
        print(f"Wiki tree nodes: {len(tree)}", flush=True)
        (output_dir / "tree.json").write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")

        child_tree_indexes = [item["index"] for item in tree if item["index"] > 0]
        child_tree_indexes = child_tree_indexes[args.start - 1 :]
        if args.end:
            child_tree_indexes = child_tree_indexes[: max(args.end - args.start + 1, 0)]
        if args.limit:
            child_tree_indexes = child_tree_indexes[: args.limit]

        if args.catalog_only:
            write_outputs(output_dir, args.url, started_at, tree, [])
            context.close()
            print(f"Done: {output_dir}", flush=True)
            return 0 if tree else 2

        docs: list[dict[str, Any]] = []
        for order, tree_index in enumerate(child_tree_indexes, start=1):
            title = tree[tree_index]["title"] if tree_index < len(tree) else f"文档 {tree_index}"
            print(f"[{order}/{len(child_tree_indexes)}] Capture: {title}", flush=True)
            locator = page.locator(".workspace-tree-view-node-wrapper.workspace-tree-node").nth(tree_index)
            locator.scroll_into_view_if_needed(timeout=10000)
            locator.click(timeout=15000)
            page.wait_for_timeout(2500)
            doc = collect_current_doc(page, args.step, args.max_steps)
            doc["tree_index"] = tree_index
            if not doc.get("title"):
                doc["title"] = title
            docs.append(doc)
            write_outputs(output_dir, args.url, started_at, tree, docs)

        context.close()

    write_outputs(output_dir, args.url, started_at, tree, docs)
    print(f"Done: {output_dir}", flush=True)
    return 0 if docs else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
