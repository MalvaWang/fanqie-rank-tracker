# Web Knowledge Agent

一个面向“短剧编剧学习资料收集”的轻量网页采集 agent。

它可以：

- 输入一个网页/合集 URL
- 提取页面下的文章链接和标题
- 只保留标题包含指定关键词的文章
- 逐篇读取正文
- 生成单篇摘要、要点和一份合并知识库

当前实现优先适配微信公众号合集页，同时保留通用 HTML 链接提取能力。基础版本只使用 Python 标准库。

## 快速开始

```bash
python3 web_knowledge_agent.py \
  "https://mp.weixin.qq.com/mp/appmsgalbum?__biz=MzE5MTQxODM4NA==&action=getalbum&album_id=4231581681670422537#wechat_redirect" \
  --title-contains "短剧编剧第一课"
```

输出会生成在 `knowledge_base/` 下，每次运行一个独立目录，包含：

- `articles.json`：结构化文章数据
- `knowledge_base.md`：合并后的学习知识库
- `articles/*.md`：每篇文章的摘要和正文摘录

本仓库还包含两份从测试合集初步学习出的编剧 agent 资料：

- `screenwriting_agent_playbook.md`：短剧编剧 agent 工作手册，包含小说改编模式、创作流程、剧本格式、主线/节奏/台词/落地性规则。
- `prompts/short_drama_screenwriter_agent.md`：可直接作为短剧编剧/小说改编 agent 起点的系统提示词草案。
- `novel_quick_read_agent.py`：处理已授权小说文本的快速阅读/改编拆解命令行工具。
- `novel_quick_read_app.html`：本地浏览器页面，可粘贴或导入已授权文本并生成快读报告。

## 小说快速阅读 Agent

如果你已经合法取得小说文本，可以用本地页面或命令行生成“快读 + 改编抓手”。

打开页面：

```bash
open novel_quick_read_app.html
```

命令行处理本地文本：

```bash
python3 novel_quick_read_agent.py path/to/novel.txt \
  --source-url "已获授权文本" \
  --output novel_reading_reports
```

输出会生成：

- `quick_read_report.md`：章节速读、人物、关键词、改编抓手。
- `chapters.json`：结构化章节分析数据。

说明：小说快速阅读工具只处理你粘贴、导入或本地保存的授权文本，不负责登录、抓取或绕过平台阅读限制。

## 常用参数

```bash
python3 web_knowledge_agent.py URL --title-contains 关键词 --output knowledge_base --max-articles 3
```

- `--title-contains`：标题必须包含的文字，可重复传入多次。
- `--title-like`：标题通配匹配，可重复传入多次，例如 `--title-like "第*章"`。
- `--max-articles`：最多读取多少篇，调试时可先设为 1 或 2。
- `--output`：知识库输出目录。
- `--timeout`：单次请求超时时间，默认 20 秒。

番茄小说 reader 页示例：

```bash
python3 web_knowledge_agent.py \
  "https://fanqienovel.com/reader/7639976610401092120" \
  --title-like "第*章" \
  --max-articles 1 \
  --output fanqie_kb
```

注意：部分小说站会使用自定义字体混淆正文。agent 会标记 `text_obfuscated: true` 并在 Markdown 中跳过乱码正文摘录；原始抓取文本保留在 `articles.json`，供后续授权导出、浏览器渲染或字体解码流程使用。

### 番茄小说 OCR 兜底采集

如果 reader 正文被字体混淆，但浏览器渲染后肉眼可读，可以用截图 + OCR 兜底：

```bash
python3 fanqie_ocr_crawler.py \
  "https://fanqienovel.com/page/7639975242047179800" \
  --output fanqie_book_ocr/run_demo
```

输出包含：

- `catalog.json`：去重后的章节目录。
- `*/screenshots/*.png`：每章滚动截图。
- `*/chapter_clean.md`：每章 OCR 清洗文本。
- `book_ocr_clean.md`：合并后的 OCR 文本。
- `book_ocr_summary.json`：章节数量、疑似拦截/不完整章节等质检信息。

说明：如果网页端显示“下载 APP / 登录后畅读全文”等拦截层，OCR 只能拿到当前网页实际可见内容，不能替代授权文本导出或登录后的完整正文。

### 三站统一本地页面

启动页面服务：

```bash
python3 fanqie_ocr_web_app.py --port 8787
```

打开 `http://127.0.0.1:8787`，输入 URL 后会自动识别站点并启动对应采集器：

- `fanqienovel.com`：番茄书籍页，走目录提取 + 截图 OCR。
- `shortdramas.com`：ShortDramas IP 详情页，走登录态浏览器 DOM 采集。
- `my.feishu.cn/wiki/...`：飞书 Wiki，走目录树 + 滚动正文采集。

ShortDramas 和飞书会使用项目下的持久浏览器 profile（`.browser_profiles/`）。如果页面需要登录，启动任务后会弹出浏览器窗口，在窗口里登录或授权后采集器会继续等待并抓取。

对应命令行脚本也可以单独使用：

```bash
python3 shortdramas_crawler.py \
  "https://www.shortdramas.com/page/ip-detail/7644663929213305406/2" \
  --output shortdramas_kb/run_demo

python3 feishu_wiki_crawler.py \
  "https://my.feishu.cn/wiki/QQYmwfUK7iWcLTkNNUtcKi1Cnwh" \
  --output feishu_wiki_crawl/run_demo
```

### Skill 入口

项目内 Skill 已固定在 `skills/fanqie-chapter-crawler/`。命令行调用：

```bash
python3 skills/fanqie-chapter-crawler/scripts/run.py \
  "https://fanqienovel.com/page/7639975242047179800"
```

如果把 Skill 复制到其他目录，设置 `FANQIE_CRAWLER_ROOT` 指向本项目根目录即可复用同一套采集脚本。

## 番茄榜单爬榜观察 Agent

用于每天保存番茄榜单快照，找出排名爬升最快的书，并给每本书打人工状态：

- `待观察`
- `已选择`
- `不要`

抓取一次榜单并写入本地 SQLite：

```bash
python3 fanqie_rank_agent.py snapshot \
  "https://fanqienovel.com/rank/1_2_1141" \
  --limit 100
```

发现并抓取排行榜菜单里的全部榜单源：

```bash
python3 fanqie_rank_agent.py snapshot-all \
  "https://fanqienovel.com/rank/1_2_1141" \
  --limit 100
```

默认数据库位置：

```text
fanqie_rank_tracker/rank_tracker.sqlite3
```

启动本地观察页面：

```bash
python3 fanqie_rank_agent.py serve --port 8791
```

打开 `http://127.0.0.1:8791`，可以抓取当天榜单、按爬升速度排序、搜索、导出 CSV，并把书标为“待观察 / 已选择 / 不要”。
页面支持多个榜单源切换，也可以按“连载中 / 已完结”筛选。

命令行查看最快爬榜：

```bash
python3 fanqie_rank_agent.py report --limit 30
```

推送飞书日报：

```bash
FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/..." \
python3 fanqie_rank_agent.py feishu-push --top 10
```

如果飞书机器人开启了签名校验，同时设置：

```bash
FEISHU_SECRET="你的机器人签名密钥"
```

### GitHub Actions 定时运行

已提供 `.github/workflows/fanqie-rank-daily.yml`，默认每天北京时间 16:10 运行：

1. 抓取全部番茄榜单源。
2. 更新并提交 `fanqie_rank_tracker/rank_tracker.sqlite3`。
3. 推送爬榜最快 Top 10 到飞书。

需要在 GitHub 仓库的 `Settings -> Secrets and variables -> Actions -> New repository secret` 中添加：

- `FEISHU_WEBHOOK`：飞书自定义机器人 Webhook。
- `FEISHU_SECRET`：可选，只有机器人开启签名校验时需要。

项目内 Skill 入口：

```bash
python3 skills/fanqie-rank-tracker/scripts/run.py report --limit 30
```

## 说明

这个 agent 的摘要是本地启发式摘要，会优先抽取含“题材、结构、节奏、悬念、情绪、台词、过稿、市场、用户”等关键词的句子。之后可以接入大模型，把 `summarize_article()` 替换成 LLM 摘要器。
