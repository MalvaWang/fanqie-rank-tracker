---
name: fanqie-chapter-crawler
description: Collect Fanqie novel chapters from a fanqienovel.com book page URL. Use when the user asks to input a Fanqie URL, read all chapter links, crawl or OCR all chapters, produce chapter Markdown files, a combined book file, screenshots, catalog JSON, or a quality summary that flags blocked/truncated chapters.
---

# Fanqie Chapter Crawler

## Quick Start

Use the bundled launcher:

```bash
python3 skills/fanqie-chapter-crawler/scripts/run.py "https://fanqienovel.com/page/7639975242047179800"
```

The launcher calls the project crawler and writes a timestamped run under `fanqie_book_ocr/`.

## Workflow

1. Accept a `fanqienovel.com/page/...` book URL from the user.
2. Run the crawler with the URL. Default behavior:
   - Extract and de-duplicate `/reader/...` chapter links.
   - Render each reader page in local Chrome.
   - Scroll through the chapter, save screenshots, OCR visible text, and clean obvious UI text.
   - Generate per-chapter Markdown plus combined book output.
3. Inspect `book_ocr_summary.json` before telling the user the result is complete.
4. If `blocked_or_truncated_chapters` is non-empty, state that those chapters only contain currently visible page text and need an authorized export or logged-in readable page for full content.
5. Return links or absolute paths to:
   - `book_ocr_clean.md`
   - `book_ocr_summary.json`
   - `catalog.json`
   - the run directory

## Useful Commands

Catalog only:

```bash
python3 skills/fanqie-chapter-crawler/scripts/run.py URL --catalog-only
```

Limit a test run:

```bash
python3 skills/fanqie-chapter-crawler/scripts/run.py URL --start 1 --end 2
```

Resume without redoing existing chapter files:

```bash
python3 skills/fanqie-chapter-crawler/scripts/run.py URL --skip-existing --output fanqie_book_ocr/run_xxx
```

Start the local page:

```bash
python3 fanqie_ocr_web_app.py --port 8787
```

## Output Contract

Read `references/output_contract.md` when wiring this skill into another tool or when deciding whether a run is usable for downstream screenplay/adaptation analysis.

## Constraints

- The crawler only captures content visible in the browser. It must not claim to bypass platform restrictions.
- OCR may contain minor recognition errors; downstream novel-to-script analysis should treat the text as draft input unless proofread.
- For long books, start with `--catalog-only` or `--start/--end` before running all chapters.
