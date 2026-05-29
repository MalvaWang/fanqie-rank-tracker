#!/usr/bin/env python3
"""Crawl article links from a page, filter by title, and build a knowledge base."""

from __future__ import annotations

import argparse
import ast
import dataclasses
import datetime as dt
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, List, Optional


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36 "
    "MicroMessenger"
)

SUMMARY_KEYWORDS = [
    "短剧",
    "编剧",
    "新人",
    "题材",
    "IP",
    "开场",
    "主线",
    "结构",
    "节奏",
    "卡点",
    "悬念",
    "钩子",
    "情绪",
    "爆点",
    "台词",
    "过稿",
    "落地",
    "市场",
    "用户",
    "平台",
    "榜单",
    "转型",
    "建议",
    "关键",
    "核心",
    "方法",
    "技巧",
]


@dataclasses.dataclass
class ArticleLink:
    title: str
    url: str


@dataclasses.dataclass
class Article:
    title: str
    url: str
    description: str
    text: str
    summary: List[str]
    key_terms: List[str]
    text_obfuscated: bool = False
    warnings: List[str] = dataclasses.field(default_factory=list)


class LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: List[ArticleLink] = []
        self._current_href: Optional[str] = None
        self._current_text: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        attr = dict(attrs)
        data_link = attr.get("data-link")
        data_title = attr.get("data-title")
        if data_link and data_title:
            self.links.append(ArticleLink(clean_text(data_title), html.unescape(data_link)))
            return
        if tag == "a" and attr.get("href"):
            self._current_href = attr["href"]
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current_href:
            title = clean_text("".join(self._current_text))
            if title:
                self.links.append(ArticleLink(title, html.unescape(self._current_href)))
            self._current_href = None
            self._current_text = []


class TextExtractor(HTMLParser):
    BLOCK_TAGS = {"p", "br", "section", "div", "h1", "h2", "h3", "h4", "li", "blockquote"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        if tag in {"br", "p", "section", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        value = clean_text(data)
        if value:
            self.parts.append(value)

    def text(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def fetch(url: str, timeout: int = 20) -> str:
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


def clean_text(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def absolute_url(base_url: str, link: str) -> str:
    link = html.unescape(link).strip()
    if link.startswith("//"):
        return "https:" + link
    return urllib.parse.urljoin(base_url, link)


def unique_links(links: Iterable[ArticleLink]) -> List[ArticleLink]:
    seen = set()
    result = []
    for item in links:
        normalized = normalize_url(item.url)
        key = (item.title, normalized)
        if key in seen:
            continue
        seen.add(key)
        result.append(ArticleLink(item.title, normalized))
    return result


def normalize_url(url: str) -> str:
    return html.unescape(url).replace("&amp;", "&")


def extract_links(page_html: str, base_url: str) -> List[ArticleLink]:
    parser = LinkExtractor()
    parser.feed(page_html)

    links = parser.links
    links.extend(extract_wechat_album_links(page_html))
    normalized = []
    for item in links:
        normalized.append(ArticleLink(clean_text(item.title), absolute_url(base_url, item.url)))
    return unique_links(normalized)


def extract_wechat_album_links(page_html: str) -> List[ArticleLink]:
    links = []
    pattern = re.compile(
        r'data-link="(?P<link>[^"]+)"\s+data-title="(?P<title>[^"]+)"',
        re.S,
    )
    for match in pattern.finditer(page_html):
        links.append(ArticleLink(clean_text(match.group("title")), html.unescape(match.group("link"))))
    return links


def title_matches(title: str, required_fragments: List[str], wildcard_patterns: Optional[List[str]] = None) -> bool:
    contains_ok = all(fragment in title for fragment in required_fragments)
    wildcard_ok = all(wildcard_search(pattern, title) for pattern in (wildcard_patterns or []))
    return contains_ok and wildcard_ok


def wildcard_search(pattern: str, value: str) -> bool:
    regex = ".*?".join(re.escape(part) for part in pattern.split("*"))
    return re.search(regex, value) is not None


def parse_meta_content(page_html: str, key: str) -> str:
    patterns = [
        rf'<meta\s+property="{re.escape(key)}"\s+content="([^"]*)"',
        rf'<meta\s+name="{re.escape(key)}"\s+content="([^"]*)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, page_html, flags=re.I)
        if match:
            return clean_text(match.group(1))
    return ""


def extract_article_title(page_html: str, fallback: str) -> str:
    title = extract_fanqie_reader_title(page_html)
    if title:
        return title
    for key in ("og:title", "twitter:title"):
        title = parse_meta_content(page_html, key)
        if title:
            return title
    match = re.search(r"var\s+msg_title\s*=\s*'((?:\\.|[^'])*)'", page_html)
    if match:
        return clean_text(decode_js_string(match.group(1)))
    match = re.search(r"<title[^>]*>(.*?)</title>", page_html, flags=re.I | re.S)
    if match:
        return clean_text(match.group(1))
    return fallback


def extract_fanqie_reader_title(page_html: str) -> str:
    match = re.search(
        r'<h1[^>]+class="[^"]*\bmuye-reader-title\b[^"]*"[^>]*>(.*?)</h1>',
        page_html,
        flags=re.I | re.S,
    )
    if match:
        return clean_text(strip_tags(match.group(1)))
    return ""


def extract_description(page_html: str) -> str:
    for key in ("description", "og:description", "twitter:description"):
        description = parse_meta_content(page_html, key)
        if description:
            return description
    match = re.search(r"var\s+msg_desc\s*=\s*htmlDecode\(\"([^\"]*)\"\)", page_html)
    if match:
        return clean_text(match.group(1))
    return ""


def decode_js_string(raw: str) -> str:
    try:
        return ast.literal_eval("'" + raw.replace("'", "\\'") + "'")
    except Exception:
        return bytes(raw, "utf-8").decode("unicode_escape", errors="replace")


def extract_article_text(page_html: str) -> str:
    article_html = (
        extract_wechat_content_noencode(page_html)
        or extract_fanqie_reader_content(page_html)
        or extract_js_content_block(page_html)
    )
    if not article_html:
        article_html = page_html
    parser = TextExtractor()
    parser.feed(article_html)
    return parser.text()


def strip_tags(value: str) -> str:
    parser = TextExtractor()
    parser.feed(value)
    return parser.text()


def decode_json_string(raw: str) -> str:
    try:
        return json.loads('"' + raw + '"')
    except json.JSONDecodeError:
        return raw


def extract_fanqie_reader_content(page_html: str) -> str:
    state_match = re.search(
        r'"chapterData"\s*:\s*\{.*?"content"\s*:\s*"((?:\\.|[^"\\])*)"',
        page_html,
        re.S,
    )
    if state_match:
        return html.unescape(decode_json_string(state_match.group(1)))

    marker = re.search(
        r'<div[^>]+class="[^"]*\bmuye-reader-content\b[^"]*\bnoselect\b[^"]*"[^>]*>',
        page_html,
        flags=re.I,
    )
    if not marker:
        return ""
    start = marker.start()
    tail = page_html.find('<div class="muye-reader-btns"', marker.end())
    if tail < 0:
        tail = page_html.find("<script", marker.end())
    if tail < 0:
        tail = min(len(page_html), marker.end() + 200000)
    return page_html[start:tail]


def extract_wechat_content_noencode(page_html: str) -> str:
    match = re.search(r"content_noencode:\s*'((?:\\.|[^'])*)'", page_html, flags=re.S)
    if not match:
        return ""
    decoded = decode_js_string(match.group(1))
    return html.unescape(decoded)


def extract_js_content_block(page_html: str) -> str:
    marker = 'id="js_content"'
    marker_pos = page_html.find(marker)
    if marker_pos < 0:
        marker = "id='js_content'"
        marker_pos = page_html.find(marker)
    if marker_pos < 0:
        return ""

    start = page_html.rfind("<div", 0, marker_pos)
    if start < 0:
        return ""

    tail = page_html.find('<script', marker_pos)
    if tail < 0:
        tail = page_html.find('<div class="rich_media_tool', marker_pos)
    if tail < 0:
        tail = min(len(page_html), marker_pos + 200000)
    return page_html[start:tail]


def split_sentences(text: str) -> List[str]:
    raw_sentences = re.split(r"(?<=[。！？!?])\s*", text)
    result = []
    for sentence in raw_sentences:
        sentence = clean_text(sentence)
        if 18 <= len(sentence) <= 220:
            result.append(sentence)
    return result


def is_probably_obfuscated_text(text: str) -> bool:
    if not text:
        return False
    private_use_chars = sum(1 for char in text if 0xE000 <= ord(char) <= 0xF8FF)
    chinese_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    meaningful = private_use_chars + chinese_chars
    return private_use_chars >= 50 and meaningful > 0 and private_use_chars / meaningful > 0.25


def obfuscated_text_warning() -> str:
    return "正文疑似使用站点字体混淆，原始文本包含大量私用区字符；请通过授权导出、浏览器渲染或字体解码后再用于小说改编分析。"


def summarize_article(title: str, description: str, text: str, max_points: int = 8) -> List[str]:
    sentences = split_sentences(text)
    scored = []
    for index, sentence in enumerate(sentences):
        score = sum(1 for keyword in SUMMARY_KEYWORDS if keyword in sentence)
        if "。" in sentence or "！" in sentence or "？" in sentence:
            score += 0.2
        score -= index * 0.002
        if score > 0:
            scored.append((score, index, sentence))

    scored.sort(key=lambda item: (-item[0], item[1]))
    selected = sorted(scored[:max_points], key=lambda item: item[1])
    points = [item[2] for item in selected]
    if description and description not in points:
        points.insert(0, description)
    if not points:
        points = sentences[:max_points]
    return points[:max_points]


def extract_key_terms(text: str, limit: int = 16) -> List[str]:
    counts = []
    for keyword in SUMMARY_KEYWORDS:
        count = text.count(keyword)
        if count:
            counts.append((count, keyword))
    counts.sort(reverse=True)
    return [keyword for _, keyword in counts[:limit]]


def slugify(title: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|#]+", "_", title)
    value = re.sub(r"\s+", "_", value).strip("._ ")
    if len(value) > 80:
        digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:8]
        value = value[:70] + "_" + digest
    return value or "article"


def crawl(
    url: str,
    title_fragments: List[str],
    wildcard_patterns: List[str],
    max_articles: int,
    timeout: int,
) -> List[Article]:
    print(f"[1/3] Fetching index: {url}")
    page_html = fetch(url, timeout=timeout)
    links = extract_links(page_html, url)
    page_title = extract_article_title(page_html, "")
    if page_title:
        links.insert(0, ArticleLink(page_title, normalize_url(url)))
        links = unique_links(links)
    matches = [link for link in links if title_matches(link.title, title_fragments, wildcard_patterns)]
    if max_articles > 0:
        matches = matches[:max_articles]

    print(f"[2/3] Found {len(links)} links, matched {len(matches)} article(s).")
    articles = []
    for index, link in enumerate(matches, start=1):
        print(f"      Reading {index}/{len(matches)}: {link.title}")
        try:
            article_html = page_html if normalize_url(link.url) == normalize_url(url) else fetch(link.url, timeout=timeout)
            title = extract_article_title(article_html, link.title)
            description = extract_description(article_html)
            text = extract_article_text(article_html)
            text_obfuscated = is_probably_obfuscated_text(text)
            warnings = [obfuscated_text_warning()] if text_obfuscated else []
            articles.append(
                Article(
                    title=title,
                    url=link.url,
                    description=description,
                    text=text,
                    summary=warnings or summarize_article(title, description, text),
                    key_terms=extract_key_terms(text),
                    text_obfuscated=text_obfuscated,
                    warnings=warnings,
                )
            )
            time.sleep(0.8)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"      Warning: failed to read {link.url}: {exc}", file=sys.stderr)
    return articles


def write_outputs(articles: List[Article], output_root: Path, source_url: str, title_filters: List[str]) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = output_root / f"run_{stamp}"
    articles_dir = run_dir / "articles"
    articles_dir.mkdir(parents=True, exist_ok=True)

    data = [dataclasses.asdict(article) for article in articles]
    (run_dir / "articles.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    for article in articles:
        path = articles_dir / f"{slugify(article.title)}.md"
        path.write_text(render_article_markdown(article), encoding="utf-8")

    (run_dir / "knowledge_base.md").write_text(
        render_knowledge_base(articles, source_url, title_filters),
        encoding="utf-8",
    )
    return run_dir


def render_article_markdown(article: Article) -> str:
    lines = [
        f"# {article.title}",
        "",
        f"- URL: {article.url}",
        f"- 字数估计: {len(article.text)}",
        f"- 关键词: {', '.join(article.key_terms) if article.key_terms else '无'}",
        f"- 正文混淆: {'是' if article.text_obfuscated else '否'}",
        "",
        "## 摘要",
        "",
    ]
    if article.warnings:
        lines.extend([f"- {warning}" for warning in article.warnings])
        lines.append("")
    lines.extend([f"- {point}" for point in article.summary])
    if article.text_obfuscated:
        lines.extend(["", "## 正文摘录", "", "（正文疑似字体混淆，已跳过 Markdown 摘录；原始抓取文本保留在 articles.json 供后续解码流程使用。）"])
    else:
        lines.extend(["", "## 正文摘录", "", article.text[:12000]])
        if len(article.text) > 12000:
            lines.append("\n（正文较长，此处截断；完整文本见 articles.json）")
    lines.append("")
    return "\n".join(lines)


def render_knowledge_base(articles: List[Article], source_url: str, title_filters: List[str]) -> str:
    lines = [
        "# 短剧编剧学习知识库",
        "",
        f"- 来源: {source_url}",
        f"- 标题过滤: {' / '.join(title_filters)}",
        f"- 收录文章数: {len(articles)}",
        f"- 生成时间: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 总览",
        "",
    ]

    all_terms = {}
    for article in articles:
        for term in article.key_terms:
            all_terms[term] = all_terms.get(term, 0) + 1
    hot_terms = sorted(all_terms.items(), key=lambda item: (-item[1], item[0]))
    if hot_terms:
        lines.append("高频学习主题：" + "、".join(term for term, _ in hot_terms[:20]))
        lines.append("")

    for article in articles:
        lines.extend(
            [
                f"## {article.title}",
                "",
                f"原文：{article.url}",
                "",
                "核心要点：",
                "",
            ]
        )
        lines.extend([f"- {point}" for point in article.summary])
        lines.append("")
    return "\n".join(lines)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crawl and summarize web articles into a local knowledge base.")
    parser.add_argument("url", help="Index, album, or article-list URL to crawl.")
    parser.add_argument(
        "--title-contains",
        action="append",
        default=[],
        help="Required title fragment. Can be used multiple times.",
    )
    parser.add_argument(
        "--title-like",
        action="append",
        default=[],
        help='Required title wildcard pattern, e.g. "第*章". Can be used multiple times.',
    )
    parser.add_argument("--output", default="knowledge_base", help="Output directory.")
    parser.add_argument("--max-articles", type=int, default=0, help="Limit article count; 0 means no limit.")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout in seconds.")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    title_fragments = args.title_contains or []
    wildcard_patterns = args.title_like or []
    if not title_fragments and not wildcard_patterns:
        title_fragments = [""]
    title_filters = [f"contains:{item}" for item in title_fragments if item]
    title_filters.extend(f"like:{item}" for item in wildcard_patterns)
    if not title_filters:
        title_filters = ["all"]
    articles = crawl(args.url, title_fragments, wildcard_patterns, args.max_articles, args.timeout)
    output_dir = write_outputs(articles, Path(args.output), args.url, title_filters)
    print(f"[3/3] Knowledge base written to: {output_dir}")
    return 0 if articles else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
