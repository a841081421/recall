# Recall — 第二大脑

> **Everything 找文件名，Recall 找记忆。本机、即时、可问答的第二大脑。**

状态：**v0.1（CLI 心脏，零 AI）** ｜ 平台：Windows（理论跨平台）｜ Python 3.12+

指定几个文件夹，后台增量索引进本地 SQLite（FTS5 全文），一个命令把"我明明见过这个东西"变成一条带文件名、行号和高亮片段的命中结果。文件永远不出你的机器。

## 快速上手

```bash
uv sync                              # 安装依赖
uv run recall config add D:/Notes    # 登记要索引的文件夹（可多次添加）
uv run recall index                  # 首次建库
uv run recall search 项目计划         # 全文检索
uv run recall watch                  # 常驻监视，改动秒级入库（Ctrl+C 退出）
uv run recall open 1                 # 打开上次搜索的第 1 条结果
uv run recall stats                  # 库概况
```

配置默认放在 `~/.recall/config.yaml`（当前目录有 `recall.yaml` 时优先用当前的），模板见 [config.example.yaml](config.example.yaml)。

## 命令

| 命令 | 作用 |
|---|---|
| `recall config add/remove <目录>` · `config list` | 管理索引目录（yaml） |
| `recall index` | 建库/追赶扫描，未变化文件自动跳过 |
| `recall index --reset` | 清库全量重建 |
| `recall index --rebuild-fts` | 只重建全文索引（换分词器后用，不重抽文件） |
| `recall search <词>` | 检索：文件名 + 行号 + 高亮片段，`-n` 调数量 |
| `recall watch` | 常驻监视（启动先追赶扫描，周期重扫兜底） |
| `recall stats` | 文件/块数/文本量/库大小/分目录统计 |
| `recall open <N>` | 用系统默认程序打开上次搜索的第 N 条 |

## 设计纪律（四条，抄 Everything 三条，加一条自己的）

| 纪律 | 落地 |
|---|---|
| 一个输入框 | 搜索、筛选、（v2 的）提问共用同一入口 |
| 永远活着 | watchdog 监视 + 2 秒防抖 + 30 分钟周期重扫兜底，不搞全量重扫 |
| 零等待 | 查询走本地 SQLite FTS5，无网络依赖 |
| 本地优先 | 文件不出机器；数据库就在 `~/.recall/` |

## v0.1 验收数字（实测，1500 个中文文件：md/txt/py/pdf/docx，约 3.5MB 文本）

| 验收口径 | 目标 | jieba | trigram |
|---|---|---|---|
| 建库速度 | 千级文档分钟级 | **16.3s**（92 文件/秒） | **2.5s**（604 文件/秒） |
| 搜索延迟（p95） | < 100ms | **5.9ms** | **4.7ms** |
| 改动可搜到 | < 10s | **1.2s** | **1.2s** |

复现：`uv run python scripts/bench.py --files 1500`。

### 中文分词定案（思路文档里"第一周要定"的坑）

两种方案都实现了，配置项 `tokenizer` 一键切换（切换后自动重算重建，无需重抽文件）：

- **默认 `jieba`**：入库与查询同用 `jieba.cut_for_search`，两字短词（会议/合同）原生命中、按词查准好。代价：建库慢 6 倍（1500 文件 16 秒，可接受）、每次进程冷启动加载词库约 0.5~1s（常驻 `watch` 只付一次，单次 `search` 命令能感知）。
- **备选 `trigram`**：FTS5 原生 3-gram 子串匹配，建库极快、查询零预处理。代价：少于 3 字的查询自动降级为 LIKE 子串扫描（能命中但无相关性排序），索引略大（实测只大 15%）。

结论：中文为主、经常搜两字词 → 用默认 jieba；追求极简与极速、习惯搜长词片段 → 切 trigram。

## 支持格式

txt / md / 代码（30+ 扩展名）/ pdf / docx。编码自动识别（UTF-8 / UTF-16 / GB18030）。PDF 与 docx 用 PyMuPDF 抽文本（docx 失败时 python-docx 兜底，含表格）。

保护上限（可在 yaml 调）：单文件默认 20MB 不索引；单文件抽取文本超 2MB 截断。

## 架构

```
src/recall/
  config.py    配置（yaml：目录列表、分词器、上限、排除规则）
  extract.py   文本抽取（txt/md/代码 → pdf/docx）
  chunking.py  按行聚块（约 600 字符/块，带起始行号）
  tokenize.py  分词（jieba cut_for_search / trigram 直通）
  db.py        SQLite：files + chunks + FTS5 外部内容表；增量删改同步
  indexer.py   单文件对齐（mtime/size 快路径 → hash → 重抽重切）+ 追赶扫描
  searcher.py  MATCH 检索 + bm25 排序 + 自制高亮片段；短词 LIKE 兜底
  watcher.py   watchdog 事件 → 防抖队列 → 单线程入库；周期重扫
  cli.py       index / watch / search / stats / config / open
```

数据库（思路文档草图 + 行号）：

```sql
files(id, path UNIQUE, mtime_ns, size, hash, kind, n_chunks, indexed_at)
chunks(id, file_id→files ON DELETE CASCADE, ordinal, start_line, text, tokens)
chunks_fts  -- FTS5 虚表挂 chunks.tokens（外部内容表），分词器 jieba→unicode61 / trigram
```

增量策略：watchdog 事件 → 防抖 2 秒 → `(mtime, size)` 没变直接跳过，变了先比 hash，hash 变了才重抽重建该文件的块；删除文件级联清块和索引。FTS5 外部内容表的增删全部手工同步并保持原子（单文件单事务）。

## Non-goals（写死，防膨胀）

- 不做聊天式主界面：AI 是搜索的升级，不是主体（v2 的"问"模式是搜索框旁的一个字，不是聊天窗口）
- 不做云同步、多用户、手机端（第一年内）
- 不做插件系统；格式支持按真实需求加

## 路线图

- [x] **v0.1 心脏（CLI，零 AI）**——本仓库当前状态
- [ ] **v0.2 皮肤**——全局热键（Alt+Space）唤起小窗，输入即出结果，回车跳转
- [ ] **v1 语义层**——sqlite-vec + EmbeddingGemma 2 端侧向量，FTS 分数 × 向量分数混合排序
- [ ] **v2 问模式**——检索片段 → OpenAI 兼容接口综合成带引用（文件+行号）的回答
- 未来池（只许进池，不许开工）：OCR 截图搜索、浏览器历史、微信/QQ 记录、闲时记忆整理、多机同步

## 已知限制

- Windows 长路径（>260 字符）依赖系统开启 LongPathsEnabled
- 扫描版 PDF（纯图片）抽不出文本，OCR 在未来池
- 词料级联目录很深时首次建库受磁盘速度限制（watch 模式无感）
