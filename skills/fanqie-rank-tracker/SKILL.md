---
name: fanqie-rank-tracker
description: Track Fanqie novel ranking pages in a local SQLite database. Use when the user asks to monitor a fanqienovel.com/rank URL, save daily ranking snapshots, find fastest-climbing books, view rank/read-count trends, export ranking candidates, or tag books as 待观察、已选择、不要.
---

# Fanqie Rank Tracker

Use the project root script as the single source of truth:

```bash
python3 fanqie_rank_agent.py snapshot "https://fanqienovel.com/rank/1_2_1141" --limit 100
python3 fanqie_rank_agent.py snapshot-all "https://fanqienovel.com/rank/1_2_1141" --limit 100
python3 fanqie_rank_agent.py report --limit 30
python3 fanqie_rank_agent.py email-push --top 10
python3 fanqie_rank_agent.py serve --port 8791
```

Default database:

```text
fanqie_rank_tracker/rank_tracker.sqlite3
```

## Workflow

1. For a new daily capture, run `snapshot` with the target rank URL and limit. Same-day captures replace that day's snapshot for the same source; different days accumulate history.
2. To cover every menu category, run `snapshot-all`. It discovers all `/rank/{gender}_{rankMold}_{category}` links from the ranking menu before capturing.
3. For fastest-climbing books, run `report` or query the local page. Ranking speed uses previous stored snapshots when available, otherwise Fanqie's `rankPosDiff` field for the current list.
4. For human curation, use the local page or `tag BOOK_ID 待观察|已选择|不要 --note ...`.
5. For email notifications, run `email-push`. Use `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `EMAIL_TO`, and optional mail settings; never hard-code them.
6. To let the user review interactively, start `serve` and give them `http://127.0.0.1:8791`.

## Notes

- The scraper stores `bookId`, current rank, rank delta, read count, word count, latest chapter, book URL, and raw item JSON.
- The local page can switch between multiple ranking sources and filter `creationStatus`: `0` means 已完结, `1` means 连载中.
- Fanqie rank text may use a custom obfuscation font. The agent stores the page font CSS and the local UI applies it for display.
- Do not re-analyze novel正文 in this skill. This skill only tracks榜单 candidates and manual selection status.
