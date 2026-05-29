#!/usr/bin/env python3
"""Build quick-read and adaptation notes from authorized novel text."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Sequence


PLOT_KEYWORDS = [
    "重生",
    "穿越",
    "系统",
    "契约",
    "婚",
    "离婚",
    "复仇",
    "报仇",
    "真相",
    "秘密",
    "身份",
    "误会",
    "背叛",
    "陷害",
    "证据",
    "威胁",
    "交易",
    "选择",
    "决定",
    "救",
    "死",
    "杀",
    "逃",
    "回家",
    "公司",
    "家族",
    "母亲",
    "父亲",
    "孩子",
    "爱",
    "恨",
]

ADAPTATION_KEYWORDS = [
    "冲突",
    "秘密",
    "真相",
    "证据",
    "身份",
    "打脸",
    "复仇",
    "背叛",
    "陷害",
    "误会",
    "威胁",
    "离婚",
    "婚约",
    "重生",
    "穿越",
    "系统",
    "救",
    "死",
    "杀",
    "逃",
]

CHAPTER_PATTERN = re.compile(
    r"(?m)^\s*((?:第[零一二三四五六七八九十百千万\d]+[章节回幕集卷].*)|(?:Chapter\s+\d+.*))$",
    re.I,
)


@dataclasses.dataclass
class Chapter:
    title: str
    text: str
    word_count: int
    summary: List[str]
    highlights: List[str]
    characters: List[str]
    key_terms: List[str]
    adaptation_notes: List[str]


def clean_text(value: str) -> str:
    value = value.replace("\u3000", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s+", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def split_sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[。！？!?])\s*", text)
    sentences = []
    for part in parts:
        sentence = clean_text(part)
        if 12 <= len(sentence) <= 180:
            sentences.append(sentence)
    return sentences


def split_chapters(text: str) -> List[tuple[str, str]]:
    text = clean_text(text)
    matches = list(CHAPTER_PATTERN.finditer(text))
    if not matches:
        return [("全文片段", text)]

    chapters = []
    prologue = clean_text(text[: matches[0].start()])
    if prologue:
        chapters.append(("卷首内容", prologue))

    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        title = clean_text(match.group(1))
        body = clean_text(text[start:end])
        if body:
            chapters.append((title, body))
    return chapters


def score_sentence(sentence: str, index: int, keywords: Sequence[str]) -> float:
    score = sum(1.0 for keyword in keywords if keyword in sentence)
    if "“" in sentence or "：" in sentence or ":" in sentence:
        score += 0.25
    if any(mark in sentence for mark in ("！", "？", "!", "?")):
        score += 0.2
    return score - index * 0.003


def pick_sentences(text: str, keywords: Sequence[str], limit: int) -> List[str]:
    scored = []
    for index, sentence in enumerate(split_sentences(text)):
        score = score_sentence(sentence, index, keywords)
        if score > 0:
            scored.append((score, index, sentence))
    scored.sort(key=lambda item: (-item[0], item[1]))
    selected = sorted(scored[:limit], key=lambda item: item[1])
    if not selected:
        selected = [(0, index, sentence) for index, sentence in enumerate(split_sentences(text)[:limit])]
    return [sentence for _, _, sentence in selected]


def extract_terms(text: str, keywords: Sequence[str], limit: int = 12) -> List[str]:
    counts = [(text.count(keyword), keyword) for keyword in keywords if text.count(keyword)]
    counts.sort(key=lambda item: (-item[0], item[1]))
    return [keyword for _, keyword in counts[:limit]]


def extract_characters(text: str, limit: int = 12) -> List[str]:
    candidates: Dict[str, int] = {}
    patterns = [
        r"([\u4e00-\u9fa5]{2,4})(?:说|问|道|喊|叫|笑|哭|看|想|走|拿|推|抱|转身)",
        r"(?:叫|名叫|看到|拉住|拦住|望着)([\u4e00-\u9fa5]{2,4})",
        r"“[^”]{0,40}”([\u4e00-\u9fa5]{2,4})(?:说|问|道)",
    ]
    stopwords = {
        "这个",
        "那个",
        "自己",
        "他们",
        "她们",
        "我们",
        "你们",
        "男人",
        "女人",
        "众人",
        "所有",
        "没有",
        "只是",
        "已经",
        "突然",
        "终于",
        "什么",
        "怎么",
        "为什么",
    }
    for pattern in patterns:
        for name in re.findall(pattern, text):
            if name not in stopwords and not any(word in name for word in stopwords):
                candidates[name] = candidates.get(name, 0) + 1
    ranked = sorted(candidates.items(), key=lambda item: (-item[1], item[0]))
    return [name for name, _ in ranked[:limit]]


def adaptation_notes(text: str, limit: int = 5) -> List[str]:
    notes = []
    for sentence in pick_sentences(text, ADAPTATION_KEYWORDS, limit * 2):
        if any(keyword in sentence for keyword in ("身份", "真相", "证据", "秘密")):
            notes.append(f"信息钩子：{sentence}")
        elif any(keyword in sentence for keyword in ("背叛", "陷害", "误会", "威胁")):
            notes.append(f"冲突外化：{sentence}")
        elif any(keyword in sentence for keyword in ("复仇", "重生", "穿越", "系统")):
            notes.append(f"核心设定/爽点：{sentence}")
        else:
            notes.append(f"可视化场面：{sentence}")
        if len(notes) >= limit:
            break
    return notes


def analyze_chapter(title: str, text: str) -> Chapter:
    return Chapter(
        title=title,
        text=text,
        word_count=len(re.sub(r"\s+", "", text)),
        summary=pick_sentences(text, PLOT_KEYWORDS, 6),
        highlights=pick_sentences(text, ADAPTATION_KEYWORDS, 5),
        characters=extract_characters(text),
        key_terms=extract_terms(text, PLOT_KEYWORDS),
        adaptation_notes=adaptation_notes(text),
    )


def analyze_text(text: str, max_chapters: int = 0) -> List[Chapter]:
    raw_chapters = split_chapters(text)
    if max_chapters > 0:
        raw_chapters = raw_chapters[:max_chapters]
    return [analyze_chapter(title, body) for title, body in raw_chapters]


def aggregate_terms(chapters: Iterable[Chapter], attr: str, limit: int = 20) -> List[str]:
    counts: Dict[str, int] = {}
    for chapter in chapters:
        for value in getattr(chapter, attr):
            counts[value] = counts.get(value, 0) + 1
    return [value for value, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def render_markdown(chapters: List[Chapter], source: str) -> str:
    total_words = sum(chapter.word_count for chapter in chapters)
    hot_characters = aggregate_terms(chapters, "characters")
    hot_terms = aggregate_terms(chapters, "key_terms")
    lines = [
        "# 小说快速阅读报告",
        "",
        f"- 来源备注: {source or '未填写'}",
        f"- 分析章节数: {len(chapters)}",
        f"- 字数估计: {total_words}",
        f"- 生成时间: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 快速入口",
        "",
        f"- 高频人物: {'、'.join(hot_characters) if hot_characters else '待补充'}",
        f"- 高频剧情词: {'、'.join(hot_terms) if hot_terms else '待补充'}",
        "",
        "## 改编初筛",
        "",
        "- 保留：身份反差、强冲突、证据/秘密、复仇或情感兑现相关桥段。",
        "- 删减：重复解释、长心理活动、不推动主线的日常段落。",
        "- 合并：功能相近的配角、同类打脸或误会桥段。",
        "- 前置：能制造前三集留存的危机、秘密、背叛、强选择。",
        "- 外化：把心理变化改成动作、道具、对峙台词和可见选择。",
        "",
        "## 章节快读",
        "",
    ]
    for index, chapter in enumerate(chapters, start=1):
        lines.extend(
            [
                f"### {index}. {chapter.title}",
                "",
                f"- 字数估计: {chapter.word_count}",
                f"- 人物: {'、'.join(chapter.characters) if chapter.characters else '未识别'}",
                f"- 关键词: {'、'.join(chapter.key_terms) if chapter.key_terms else '无'}",
                "",
                "剧情速读：",
            ]
        )
        lines.extend([f"- {item}" for item in chapter.summary])
        lines.extend(["", "改编抓手："])
        lines.extend([f"- {item}" for item in chapter.adaptation_notes])
        lines.append("")
    return "\n".join(lines)


def slug(value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    safe = re.sub(r"[\\/:*?\"<>|#]+", "_", value).strip("._ ")[:50]
    return safe or f"novel_{digest}"


def write_outputs(chapters: List[Chapter], output_root: Path, source: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = output_root / f"novel_run_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    data = [dataclasses.asdict(chapter) for chapter in chapters]
    (run_dir / "chapters.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "quick_read_report.md").write_text(render_markdown(chapters, source), encoding="utf-8")
    return run_dir


def read_input(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze authorized novel text and build a quick-read adaptation report."
    )
    parser.add_argument("input", help="Text/Markdown file to analyze, or '-' for stdin.")
    parser.add_argument("--source-url", default="", help="Optional source note or URL for your own record.")
    parser.add_argument("--output", default="novel_reading_reports", help="Output directory.")
    parser.add_argument("--max-chapters", type=int, default=0, help="Analyze only the first N chapters; 0 means all.")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    text = read_input(args.input)
    if not clean_text(text):
        print("No input text found. Paste authorized text or pass a local file.", file=sys.stderr)
        return 2
    chapters = analyze_text(text, max_chapters=args.max_chapters)
    run_dir = write_outputs(chapters, Path(args.output), args.source_url)
    print(f"Quick-read report written to: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
