#!/usr/bin/env python3
"""Small local web UI for novel chapter crawlers."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
import subprocess
import sys
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parent
APP_HTML = ROOT / "fanqie_ocr_app.html"
FANQIE_CRAWLER = ROOT / "fanqie_ocr_crawler.py"
SHORTDRAMAS_CRAWLER = ROOT / "shortdramas_crawler.py"
FEISHU_CRAWLER = ROOT / "feishu_wiki_crawler.py"
DEFAULT_OUTPUTS = {
    "fanqie": ROOT / "fanqie_book_ocr",
    "shortdramas": ROOT / "shortdramas_kb",
    "feishu": ROOT / "feishu_wiki_crawl",
}
JOBS: dict[str, dict[str, Any]] = {}


def json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def is_fanqie_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.netloc.endswith("fanqienovel.com")


def detect_site(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("请输入 http 或 https URL。")
    host = parsed.netloc.lower()
    if host.endswith("fanqienovel.com"):
        return "fanqie"
    if host.endswith("shortdramas.com"):
        return "shortdramas"
    if host.endswith("feishu.cn") and parsed.path.startswith("/wiki/"):
        return "feishu"
    raise ValueError("当前页面支持 fanqienovel.com、shortdramas.com、my.feishu.cn/wiki 三类 URL。")


def make_output_dir(root: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return root / f"run_{stamp}_{uuid.uuid4().hex[:6]}"


def start_job(payload: dict[str, Any]) -> dict[str, Any]:
    url = str(payload.get("url", "")).strip()
    site = detect_site(url)

    output_root = Path(str(payload.get("output_root") or DEFAULT_OUTPUTS[site])).expanduser()
    output_dir = make_output_dir(output_root)
    start = safe_int(payload.get("start"), 1)
    end = safe_int(payload.get("end"), 0)
    limit = safe_int(payload.get("limit"), 0)
    step = safe_int(payload.get("step"), 620)
    login_timeout = safe_int(payload.get("login_timeout"), 300)

    if site == "fanqie":
        cmd = [sys.executable, str(FANQIE_CRAWLER), url, "--output", str(output_dir)]
    elif site == "shortdramas":
        cmd = [sys.executable, str(SHORTDRAMAS_CRAWLER), url, "--output", str(output_dir)]
    elif site == "feishu":
        cmd = [sys.executable, str(FEISHU_CRAWLER), url, "--output", str(output_dir)]
    else:  # pragma: no cover - detect_site prevents this.
        raise ValueError(f"Unsupported site: {site}")

    if start > 1:
        cmd.extend(["--start", str(start)])
    if end > 0:
        cmd.extend(["--end", str(end)])
    if limit > 0:
        cmd.extend(["--limit", str(limit)])
    if site in {"fanqie", "feishu"} and step > 0:
        cmd.extend(["--step", str(step)])
    if site in {"shortdramas", "feishu"} and login_timeout > 0:
        cmd.extend(["--login-timeout", str(login_timeout)])
    if site == "fanqie" and payload.get("skip_existing"):
        cmd.append("--skip-existing")
    if site == "fanqie" and payload.get("screenshots_only"):
        cmd.append("--screenshots-only")
    if payload.get("catalog_only"):
        cmd.append("--catalog-only")
    if site in {"shortdramas", "feishu"} and not payload.get("headed", True):
        cmd.append("--headless")

    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "status": "running",
        "site": site,
        "url": url,
        "output_dir": str(output_dir),
        "command": cmd,
        "log": [],
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
        "returncode": None,
    }
    JOBS[job_id] = job

    thread = threading.Thread(target=run_job, args=(job,), daemon=True)
    thread.start()
    return snapshot_job(job)


def run_job(job: dict[str, Any]) -> None:
    output_dir = Path(job["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        process = subprocess.Popen(
            job["command"],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        assert process.stdout is not None
        for line in process.stdout:
            job["log"].append(line.rstrip())
            job["log"] = job["log"][-400:]
        process.wait()
        job["returncode"] = process.returncode
        job["status"] = "done" if process.returncode == 0 else "failed"
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        job["log"].append(f"ERROR: {exc}")
    finally:
        job["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
        enrich_job(job)


def enrich_job(job: dict[str, Any]) -> None:
    output_dir = Path(job["output_dir"])
    site = job.get("site", "fanqie")
    if site == "shortdramas":
        paths = {
            "book": output_dir / "book_preview_clean.md",
            "summary": output_dir / "run_summary.json",
            "catalog": output_dir / "catalog.json",
            "chapters": output_dir / "chapters.json",
        }
    elif site == "feishu":
        paths = {
            "book": output_dir / "feishu_wiki_clean.md",
            "summary": output_dir / "run_summary.json",
            "catalog": output_dir / "catalog.json",
            "docs": output_dir / "docs.json",
            "tree": output_dir / "tree.json",
        }
    else:
        paths = {
            "book": output_dir / "book_ocr_clean.md",
            "summary": output_dir / "book_ocr_summary.json",
            "catalog": output_dir / "catalog.json",
            "index": output_dir / "book_ocr_index.json",
        }
    job["files"] = {key: str(path) for key, path in paths.items() if path.exists()}
    summary_path = paths["summary"]
    if summary_path.exists():
        try:
            job["summary"] = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            job["summary"] = None


def snapshot_job(job: dict[str, Any]) -> dict[str, Any]:
    enrich_job(job)
    data = {key: value for key, value in job.items() if key != "command"}
    data["links"] = {}
    for key, path in data.get("files", {}).items():
        try:
            rel = Path(path).resolve().relative_to(Path(data["output_dir"]).resolve())
        except ValueError:
            continue
        data["links"][key] = f"/outputs/{data['id']}/{rel.as_posix()}"
    return data


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_file(APP_HTML)
        elif parsed.path == "/api/health":
            self.send_json({"ok": True, "jobs": len(JOBS)})
        elif parsed.path.startswith("/api/jobs/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            job = JOBS.get(job_id)
            if not job:
                self.send_json({"error": "job not found"}, HTTPStatus.NOT_FOUND)
            else:
                self.send_json(snapshot_job(job))
        elif parsed.path.startswith("/outputs/"):
            self.send_output_file(parsed.path)
        else:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/start":
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            job = start_job(payload)
            self.send_json(job, HTTPStatus.CREATED)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def send_output_file(self, path: str) -> None:
        parts = path.split("/", 3)
        if len(parts) < 4:
            self.send_json({"error": "missing output path"}, HTTPStatus.BAD_REQUEST)
            return
        job = JOBS.get(parts[2])
        if not job:
            self.send_json({"error": "job not found"}, HTTPStatus.NOT_FOUND)
            return
        base = Path(job["output_dir"]).resolve()
        target = (base / unquote(parts[3])).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            self.send_json({"error": "invalid path"}, HTTPStatus.BAD_REQUEST)
            return
        if not target.exists() or not target.is_file():
            self.send_json({"error": "file not found"}, HTTPStatus.NOT_FOUND)
            return
        self.send_file(target)

    def send_file(self, path: Path) -> None:
        content = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.suffix in {".md", ".txt"}:
            ctype = "text/plain; charset=utf-8"
        elif path.suffix == ".html":
            ctype = "text/html; charset=utf-8"
        elif path.suffix == ".json":
            ctype = "application/json; charset=utf-8"
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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the local multi-site chapter crawler page.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Chapter crawler page: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
