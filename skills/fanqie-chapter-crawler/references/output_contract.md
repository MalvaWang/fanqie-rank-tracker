# Output Contract

The crawler writes one run directory per job.

## Top-Level Files

- `catalog.json`: ordered, de-duplicated chapter list. Each item has `index`, `title`, and `url`.
- `book_ocr_clean.md`: combined Markdown text from all generated chapter files.
- `book_ocr_index.json`: metadata for each processed chapter, including screenshot paths and OCR stats.
- `book_ocr_summary.json`: quality summary for downstream tools.

## Per-Chapter Files

Each chapter directory is named like `001_第1章_标题`.

- `screenshots/page_*.png`: rendered viewport screenshots used for OCR.
- `ocr_pages.json`: OCR lines grouped by screenshot page.
- `chapter_raw.txt`: OCR lines after basic UI filtering and de-duplication.
- `chapter_clean.md`: human-readable chapter Markdown.
- `metadata.json`: source URL, screenshot count, OCR stats, and block/truncation flags.

## Summary Fields

`book_ocr_summary.json` contains:

- `chapters`: number of chapter links in the catalog/run.
- `complete_or_visible_chapters`: count of chapters without a detected download/login block.
- `blocked_or_truncated_chapters`: chapter numbers where the rendered page showed a download/login block or only a visible preview.
- `ocr_error_chapters`: chapter numbers whose screenshot step completed but OCR failed.
- `total_clean_chars`: total characters in generated `chapter_clean.md` files.

Downstream screenplay skills should only treat chapters as complete when their chapter number is absent from `blocked_or_truncated_chapters`.
