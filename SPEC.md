# eventmem 实现规格（SPEC）

版本：v0.1。本文件是实现的契约：模块边界、接口签名、文件格式、协议、编码纪律。设计依据见 [DESIGN.md](DESIGN.md)，冲突时以 DESIGN.md 的设计意图为准、以本文件的接口为准。

## 1. 技术选型

- Python ≥ 3.10，src 布局，包名 `eventmem`。
- 运行依赖仅两个：`httpx`、`pyyaml`。dev 依赖：`pytest`。
- BM25 手写实现（约 30 行），不引第三方检索库。不用向量库（DESIGN §7.4）。
- 全量类型标注；数据结构用 `dataclass`；无模块级可变全局状态。
- 所有路径操作经由 `MemoryPaths`（见 §3.1），禁止在函数内拼 `.memory` 路径。

## 2. 目录布局

```
event-memory/
├── DESIGN.md / SPEC.md / README.md
├── pyproject.toml               # console_scripts: eventmem = eventmem.cli:main
├── .gitignore                   # .env / .memory/ / __pycache__ / *.egg-info / .pytest_cache
├── .env.example                 # 变量名与说明，不含真实 key
├── src/eventmem/
│   ├── __init__.py
│   ├── paths.py                 # MemoryPaths
│   ├── schema.py                # Event 模型与序列化
│   ├── store.py                 # L0 存储
│   ├── index.py                 # L1 索引构建与读取
│   ├── recall.py                # 联想浮现 + 兜底检索
│   ├── llm.py                   # Anthropic 兼容 client
│   ├── extract.py               # transcript → 事件（LLM）
│   ├── consolidate.py           # 轻/深整理
│   ├── cli.py                   # eventmem 命令行
│   └── hooks/
│       ├── __init__.py          # 公共：stdin 解析、输出协议、异常护栏
│       ├── session_start.py     # python3 -m eventmem.hooks.session_start
│       ├── post_tool_use.py
│       ├── pre_compact.py
│       └── session_end.py
├── examples/settings.json       # Claude Code hooks 配置示例
└── tests/
```

运行时数据（被管理项目内，不在本 repo）：

```
<project>/.memory/
├── events/<id>.md               # L0：一事件一文件
├── raw/                         # 预留，V0.1 不写（transcript 由宿主保存，事件只存指针）
├── index/
│   ├── working-set.md           # 注入文本本体
│   ├── project.md               # 全量单行索引
│   ├── anchors.json             # 锚点倒排
│   └── lessons.md               # lesson 表
├── log/eventmem.log             # 护栏日志
└── config.yml                   # 覆盖默认参数（可缺省）
```

## 3. 模块契约

### 3.1 paths.py

```python
@dataclass(frozen=True)
class MemoryPaths:
    root: Path                    # <project>/.memory
    @classmethod
    def for_project(cls, project_dir: Path) -> "MemoryPaths": ...
    # 属性：events_dir / index_dir / working_set / project_index / anchors / lessons / log / config
    def ensure(self) -> None      # mkdir -p 全部目录
```

### 3.2 schema.py

```python
Kind = Literal["decision", "build", "explore", "fix"]
Status = Literal["open", "done", "abandoned", "superseded"]

@dataclass
class Anchors:
    commits: list[str]; files: list[str]; tests: list[str]; dialog: list[str]
    error_sigs: list[str]         # 规范化错误签名（DESIGN §4.4 报错线索）

@dataclass
class Event:
    id: str                       # "YYYY-MM-DD_HHMMSS" ＋ 冲突时追加 "-2" 等
    parent: str | None
    kind: Kind
    status: Status
    superseded_by: str | None
    intent: str
    anchors: Anchors
    outcome: str | None
    lesson: str | None
    body: str                     # frontmatter 之下的行动序列摘要，可为空串

def to_markdown(e: Event) -> str
def from_markdown(text: str) -> Event      # 严格解析，缺 id/intent/kind/status 则 raise SchemaError
def new_id(now: datetime, existing: set[str]) -> str
```

frontmatter 字段名与 DESIGN §2.5 一致（`superseded_by`、`anchors.error_sigs` 为实现补充）。**序列化必须往返稳定**：`from_markdown(to_markdown(e)) == e`。

### 3.3 store.py —— L0，append-only

```python
class Store:
    def __init__(self, paths: MemoryPaths): ...
    def append(self, e: Event) -> str                  # 写 events/<id>.md，id 冲突自动追加后缀，返回最终 id
    def read(self, event_id: str) -> Event             # 不存在 raise EventNotFound
    def all_ids(self) -> list[str]                     # 升序
    def iter_events(self) -> Iterator[Event]
    # 生命周期操作（flush 阶段，非整理）：
    def close(self, event_id: str, status: Status, outcome: str) -> None
    def add_anchors(self, event_id: str, anchors: Anchors) -> None   # 集合并集，幂等
    # 整理专用（唯一允许整理写 L0 的字段，DESIGN §4.3）：
    def set_lesson(self, event_id: str, lesson: str) -> None
    def set_outcome(self, event_id: str, outcome: str) -> None   # 仅「已闭合且 outcome 为空」可写（轻整理补全通道）
    def mark_superseded(self, event_id: str, by: str) -> None
```

不可变纪律的代码表达：**不提供**修改 `intent`／`body` 的方法。`close` 仅当 `status == "open"` 时允许、目标状态仅 done/abandoned（superseded 一律经 `mark_superseded`，close 与它是两条不重叠的路径），重复 close 报 `AlreadyClosed`。`mark_superseded` 可作用于任意状态（事后推翻合法），改指已推翻事件到不同目标时报 `AlreadyClosed`。

### 3.4 index.py —— L1，全部可重建

```python
@dataclass
class Budget:
    working_set_tokens: int = 1500        # DESIGN §3 L1
    surface_k: int = 3                    # DESIGN §4.4 浮现上限
    stale_days: int = 14                  # DESIGN §4.2 闭合信号 4

def rebuild_all(store: Store, paths: MemoryPaths, budget: Budget, now: datetime) -> None
    # 依次重建四个索引文件；写临时文件后 os.replace 原子替换（DESIGN §4.3）
def load_anchor_map(paths: MemoryPaths) -> dict[str, list[str]]
    # {"file:src/a.py": [id...], "error:<sig>": [id...], "intent:<token>": [id...]}
def append_to_project_index(paths: MemoryPaths, e: Event) -> None   # 增量追加，避免每事件全量重建
```

**文件格式**：

- `project.md` 每行：`| id | kind | status | intent 单行（截 80 字符）|`，首行表头。
- `anchors.json`：上述倒排 dict，UTF-8，缩进 2。
- `working-set.md` 结构（注入文本，人可读）：

```markdown
# Memory working set (generated 2026-08-25T14:32:01)
## Open events
- [<id>] (kind) intent — 最近锚点
## Recent outcomes
- [<id>] outcome 单行
## Lessons (promoted)
- lesson 文本 [<id>]
```

按预算填充：open 事件全量优先 → promoted lessons → 最近闭合 outcome 行，达到 `working_set_tokens` 即截断（token 估算：`len(text) // 3`，中文场景的粗估，够用）。

- `lessons.md` 每行：`- [<id>] (candidate|promoted|retired) lesson 单行`。

### 3.5 recall.py

```python
@dataclass
class SurfaceHit:
    event_id: str; line: str      # line = "[id] outcome 或 intent 单行"

def surface(cue: str, kind: Literal["file", "error", "intent"],
            store: Store, paths: MemoryPaths, budget: Budget,
            seen: set[str]) -> list[SurfaceHit]
    # 查 anchor_map 精确命中 → 过滤 seen → 按 (状态权重: done>abandoned>open, 新近度) 排序 → 截 surface_k
    # 无命中返回 []。不调用 LLM。纯查表，目标毫秒级。
def search(query: str, store: Store, paths: MemoryPaths, top: int = 10) -> list[SurfaceHit]
    # 兜底检索：对 project.md 各行（intent+outcome 域）做 BM25，返回单行列表
def error_signature(stderr: str) -> str
    # 规范化：取首个非空错误行，去绝对路径、行号、十六进制地址、时间戳，压空白，截 120 字符
```

`seen` 集合由 hooks 层持久化在 `<project>/.memory/log/seen-<session_id>.txt`（同会话去重，DESIGN §4.4）。

### 3.6 llm.py

```python
@dataclass(frozen=True)
class LLMConfig:
    base_url: str      # env EVENTMEM_BASE_URL，默认 "https://api.anthropic.com"
    api_key: str       # env EVENTMEM_API_KEY；缺失 raise ConfigError（护栏层负责降级）
    model: str         # env EVENTMEM_MODEL，默认 "claude-haiku-4-5-20251001"
    timeout_s: float = 60.0
    @classmethod
    def from_env(cls) -> "LLMConfig"

class LLMClient:
    def __init__(self, cfg: LLMConfig): ...
    def complete(self, system: str, user: str, max_tokens: int = 2048) -> str
        # POST {base_url}/v1/messages，Anthropic messages 格式
        # headers: x-api-key, anthropic-version: 2023-06-01, content-type
        # 非 200：重试 1 次（指数退避 2s），仍失败 raise LLMError
    def complete_json(self, system: str, user: str, max_tokens: int = 4096) -> Any
        # complete 后解析 JSON；容忍 ```json 围栏；解析失败重试 1 次（提示词追加严格 JSON 指令）
```

兼容性验收：`EVENTMEM_BASE_URL=https://api.deepseek.com/anthropic` ＋ DeepSeek key ＋ `EVENTMEM_MODEL=deepseek-v4-flash` 必须可用（Anthropic 兼容端点）。**key 一律来自环境变量，任何文件不得出现真实 key。**

### 3.7 extract.py

```python
def extract_events(transcript_path: Path, store: Store, client: LLMClient,
                   session_id: str, now: datetime) -> list[str]   # 返回新事件 id
```

- 读 Claude Code transcript（jsonl）；只取上次抽取水位之后的行（水位存 `log/extract-watermark-<session_id>`，值为已处理行数）。
- 抽取前先做机械收集：TodoWrite 调用（声明式事件开闭）、Bash 的 git commit（锚点）、非零退出的错误签名、Read/Edit 的文件路径。机械收集不调 LLM。
- LLM 只负责剩余判断：将机械收集结果＋对话摘录交给 `complete_json`，产出事件列表（schema 同 Event 的 JSON 投影）。
- **prompt 纪律**（DESIGN §1.3 宁漏勿胀）：拿不准的不抽；一次会话产出事件数上限 20；lesson 字段默认留空（轻整理才补）；dialog 指针格式 `<session_id>#L<start>-L<end>`。
- LLM 失败时：机械收集的事件仍落盘，LLM 部分放弃（记日志）。

### 3.8 consolidate.py

```python
def light(store, paths, budget, client: LLMClient | None, now) -> None
    # 1) 无 outcome 的已闭合事件：有 client 则 LLM 补一句 outcome，无则用 intent 复制降级
    # 2) 规则闭合：todo 已 completed 但事件仍 open 的 → close(done)
    # 3) rebuild_all（含 working-set 重排 —— 即预取）
def deep(store, paths, budget, client: LLMClient, now) -> None
    # 前置：dirty_count(paths) >= config.deep_threshold（默认 30）
    # 1) stale 检查：open 超过 stale_days → 标注（写入 working-set 的 Open events 行尾 "(stale)"）
    # 2) lesson 蒸馏：对无 lesson 的 abandoned/fix 事件批量提蒸馏候选（LLM，门槛在 prompt：仅当教训可复用才写）
    # 3) 晋升/退休：同一 lesson 文本近似重复 ≥ 2 次 → promoted；promoted 连续 3 次深整理未被引用 → retired
    # 4) rebuild_all
def dirty_count(paths) -> int     # 上次深整理水位（log/deep-watermark）之后的新事件数
```

两级整理均**只写**：L1 全部文件、L0 的 `lesson` 字段、超时 stale 标注。不动 intent/body/outcome（outcome 补写仅限「已闭合但缺失」的降级补全，属 flush 收尾职责在 light 中代行）。

### 3.9 hooks/（护栏纪律最高优先）

**硬纪律：hook 进程永不非零退出、永不抛异常到顶层、永不阻塞超过 5 秒（LLM 类工作 fork 到后台）。** 公共护栏在 `hooks/__init__.py`：

```python
def run_hook(main: Callable[[dict], dict | None]) -> None
    # stdin 读 JSON → main(payload) → 有返回则 stdout 打 JSON → 任何异常：写 log，exit 0
def spawn_detached(argv: list[str]) -> None    # subprocess.Popen, start_new_session=True, 输出重定向到 log
```

各 hook 行为（payload 字段以官方文档为准，实现前 WebFetch 核对：https://docs.anthropic.com/en/docs/claude-code/hooks）：

| hook | 输入要点 | 行为 | 输出 |
|---|---|---|---|
| `session_start` | cwd | 读 `working-set.md` | `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "<工作集全文>"}}`；文件不存在则无输出 |
| `post_tool_use` | tool_name, tool_input, tool_response | TodoWrite → 事件开/闭落盘；Read/Edit/Write → `surface(file)`；Bash → commit 锚点、错误签名 → `surface(error)` | 命中时 `additionalContext` 注入 `Memory: [id] outcome...` 行（≤ surface_k），并记 seen |
| `pre_compact` | transcript_path | `spawn_detached(python3 -m eventmem.cli extract --transcript ...)` | 无 |
| `session_end` | transcript_path, session_id | `spawn_detached(eventmem extract --transcript … --session … --then-light)` —— 抽取（含 LLM 补漏层）→ 轻整理 →（脏量达标）深整理串成同一后台进程。不做同步机械 flush：那会抢先推水位使后台 LLM 层空转；spawn 失败时水位未动，下次照常补上（自愈）。无 transcript 信息时退回 `consolidate --light --deep-if-dirty` | 无 |

### 3.10 cli.py

```
eventmem status                    # 事件数、open 数、脏量、索引年龄
eventmem search <query> [--top N]
eventmem read <id>
eventmem trace <id>                # 打印 anchors.dialog 指针（V0.1 不解引用 transcript）
eventmem extract --transcript P --session S
eventmem consolidate --light | --deep | --deep-if-dirty
eventmem rebuild                   # rebuild_all
eventmem init                     # 在 cwd 创建 .memory/ 骨架与 config.yml 模板
```

`--project` 全局参数指定项目根（默认 cwd）。config.yml 可覆盖 Budget 与 deep_threshold；缺省即默认值。

## 4. 测试契约

- 单测（无网络）：schema 往返、store 生命周期与不可变纪律（close 后再 close 报错、无 intent 修改接口）、index 重建幂等＋原子替换、BM25 排序合理性、error_signature 规范化、surface 的 K 截断与 seen 去重、每个 hook 的 stdin→stdout（fixture JSON，mock store）。
- 集成（`@pytest.mark.llm`，需 env key，CI 默认跳过）：合成 transcript → extract → 事件落盘断言；light/deep 整理产出 working-set 断言。
- 端到端（无 LLM）：模拟一个会话的 hook 序列（start→todo→edit→bash 报错→end），断言 `.memory/` 结构完整、二次 session_start 注入包含上一会话的 open 事件。
- 全部测试在临时目录跑（`tmp_path`），不触碰真实项目。

## 3.11 显著性（salience）——v0.2 增量

设计依据 DESIGN §8.7：规则划界，自评开局，证据说了算。

**schema 扩展**（schema.py，向后兼容：旧事件文件缺字段时解析为默认值）：

```python
# Event 新增两个可选字段（frontmatter 同名）：
salience_prior: Literal["low", "medium", "high"] | None = None   # 闭合时的自评
salience_reason: str | None = None                                # 一句理由
prospective: bool = False                                         # 前瞻标记（§3.12）
```

**store 新增**：`set_salience_prior(event_id, prior, reason)` —— 仅已闭合且 prior 为空时写（整理补评通道，同 set_outcome 风格）。

**先验来源**：extract 的 LLM 层对闭合事件同时产出 prior＋reason（prompt 扩展）；机械闭合的事件由 light 整理经 LLM 补评（无 client 时按 kind 规则表默认：decision→high、explore(abandoned)→medium、fix→medium、build→low）。

**证据与后验**（consolidate.deep 重算，写 `index/salience.json`，纯派生可重建）：

```json
{"<event_id>": {"score": 0.0-1.0, "prior": "low|medium|high",
  "evidence": {"refs": 0, "hits": 0, "ignored": 0, "superseded_trigger": false},
  "updated": "<iso>"}}
```

- `refs`：被后续事件 anchors 交集或 parent 引用的次数
- `hits` / `ignored`：浮现采纳／无视（判定见 §3.13）
- 公式：`score = clamp_rules(0.35·prior_val + 0.25·min(refs,4)/4 + 0.30·hit_ratio − 0.10·ignored_decay + 0.20·superseded_trigger)`，prior_val ∈ {0.2, 0.5, 0.8}；clamp_rules：decision 下限 0.4，顺利 build 上限 0.25（有 evidence 抬升时失效；0.25 使钳位在零证据时真实可达——prior 项最大 0.28）。权重为模块级常量。
- **消费方**：recall.surface 排序键改为（salience 降序，cue 锚点重合数降序，新近度降序）——重合数是相关性信号，排在新近度前才可能生效（id 唯一，新近度无并列）；salience.json 缺失时退回（状态权重，重合数，新近度）。working-set 的 Recent outcomes 填充按 salience 排序。

## 3.12 预取（预测 pass）——v0.2 增量

设计依据 DESIGN §4.3 预测 pass。

- **前瞻标记捕获**：extract 的 LLM prompt 新增指令——对话中的将来时意图（「下次先做 X」「明天记得 Y」）抽为 `prospective: true`、`status: open`、`kind: build` 的事件，intent 前缀「下次：」。机械层不做（LLM 专属判断）。
- **规则级预取**（light 与 deep 共用）：对每个 open 事件，取其 `anchors.files` 查倒排，收集关联的已闭合事件，按 salience 排序。
- **模型级预取**（仅 deep，client 存在时）：把 open 事件列表＋最近 10 个事件的锚点摘要交给 LLM，产出 ≤3 条「下次入口」预测（JSON：一句话＋关联锚点），锚点白名单过滤后反查倒排。
- **working-set 新区**（index.py 渲染，插在 Open events 与 Recent outcomes 之间）：

```markdown
## Likely next
- [<id>] outcome 单行 — 关联: <锚点>
```

预算份额：整体 working_set_tokens 的 1/3 上限，填充顺序在 open 之后、lesson 之前。预取候选写 `index/prefetch.json`（派生），渲染时读。
- **预取命中记录**：session 结束时（extract 水位处理中）对比本会话实际出现的锚点与上次 prefetch.json，命中计数追加进 `log/prefetch-outcome.jsonl`（评估用）。

## 3.13 观测与评估埋点——v0.2 增量

设计依据 DESIGN §7.6（评估设计，随本增量补写）。全部为追加写 jsonl，不进上下文。

| 文件 | 写入方 | 记录 |
|---|---|---|
| `log/surfaced-<session>.jsonl` | hooks（Python 与 dsh TS 侧同格式） | `{ts, event_id, cue, cue_kind, chars}` 每次浮现 |
| `log/injected-<session>.jsonl` | session_start hook | `{ts, source: "working-set", chars}` |
| `log/prefetch-outcome.jsonl` | extract 收尾 | `{session, predicted: n, hit: m}` |

**采纳判定**（deep 整理离线执行，不在热路径）：浮现记录后，同会话 feed/transcript 中后续 10 次工具调用内出现该事件 `anchors.files` 之一的 Edit/Write，判为 hit；会话结束仍无则 ignored。判定结果累加进 salience.json 的 evidence。

**`eventmem stats`**（cli 新命令）：输出事件总数与 kind/status 分布、浮现次数与采纳率、注入字符量估算、**重复踩坑数**（同 error_sig 关联 ≥2 个 fix 事件的组数，直接扫 L0 得出）、预取命中率。`--json` 机器可读。

**`eventmem log`**（cli 新命令）：时间线视图——`<id> <kind> <status> <intent 单行>`，`--tree` 按 parent 缩进，`--since <days>`，`--kind` 过滤。纯读。

## 3.14 敏感信息清洗（scrubbing）——v0.2 增量

- 位置：extract 构造事件之后、store.append 之前（唯一入口）；error_signature 的输入同样先过。
- 规则（模块 `scrub.py`，`scrub(text) -> str`）：正则替换为 `<REDACTED:类型>`——`sk-[A-Za-z0-9_-]{16,}`、`AKIA[0-9A-Z]{16}`、`ghp_[A-Za-z0-9]{36}`、`xox[abp]-[A-Za-z0-9-]{10,}`、`Bearer\s+[A-Za-z0-9._-]{16,}`、`-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----`、`(password|passwd|secret|token|api_key)\s*[=:]\s*\S{8,}`（保留 key 名，值替换）。
- 作用域：intent / outcome / lesson / body / salience_reason / anchors.error_sigs / anchors.tests（后两项为 LLM 自由文本与 shell 命令原文，同样可能携带密钥）。配置 `config.yml: scrub: false` 可整体关闭。
- 幂等：`scrub(scrub(x)) == scrub(x)`。

## 3.15 CLAUDE.md 晋升建议——v0.2 增量

- deep 整理里，lesson 达到 promoted 状态时，生成／更新 `index/claude-md-suggestions.md`：每条一个区块——lesson 文本、来源事件 id 链、建议措辞（可直接粘贴进 CLAUDE.md 的一行）。
- 永不自动修改用户的 CLAUDE.md；`eventmem status` 在有未读建议时输出一行提示。
- 已被用户采纳（建议文本出现在项目 CLAUDE.md 中）的条目在下次 deep 时移除并在 lessons.md 标 `(adopted)`。

## 3.16 事件粒度自适应——v0.2 增量

L0 不可变，因此**合并与拆分都只发生在索引层**（视图操作，全派生可重建）：`index/granularity.json`

```json
{"merged": [{"ids": ["...", "..."], "summary": "一句概括", "anchors_union": [...]}],
 "coarse": [{"id": "...", "segments": [{"label": "分段一句话", "files": [...]}]}]}
```

- **合并检测**（deep，规则先筛＋LLM 概括）：同 parent、锚点交集非空、id 时间间隔 < 30 分钟的连续已闭合事件 ≥3 个 → 候选组；LLM 给组概括（一句）。working-set 与浮现以组行显示：`[<首id>+n] 组概括`；`memory_read` 组 id 时列出成员。
- **拆分检测**（deep）：单事件 `anchors.files` ≥ 8 或 dialog 区间跨度 > 400 行 → 粗事件；LLM 按锚点聚类产出 ≤4 个 segments（虚拟子条目，只存在于索引）。浮现命中粗事件的某个 file 锚点时，浮现行展示对应 segment 的 label 而非整体 outcome。
- 判定阈值为模块级常量；granularity.json 缺失时一切行为退回现状。

## 3.17 子 agent 的事件归属——v0.2 增量

- **Python 侧（Claude Code）**：extract 机械层识别 `Task`／`Agent` 工具调用 → 生成一个委托事件：kind 按任务描述推断（默认 build）、intent = 任务描述首行、outcome 从返回摘要截取、`anchors.dialog` 指主 transcript 区间、body 首行记 `委托: <subagent_type>`；返回文本中的文件路径与 commit hash 经白名单正则抽为锚点。
- **dsh 侧（TS feed）**：`tools/result` 中工具名命中配置的委托工具名单（默认 `["task", "subagent", "agent"]`）时，以同构形态写入 feed。
- 子 agent 内部的失败细节不进入主记忆（其 transcript 不解析），记为 DESIGN 已知限制：委托事件的质量取决于返回摘要的质量。

## 3.18 跨项目的用户级 lesson 晋升——v0.2 增量

- 存放：`~/.claude/eventmem/`（`global-lessons.md` ＋ `global-lesson-state.json`），与项目 `.memory/` 隔离。
- **可移植性判定**（deep，LLM）：项目内 lesson 达到 promoted 后，判定其是否不含项目特有信息（文件路径、项目名、内部术语）——通过者成为用户级候选。判定 prompt 偏保守：存疑即不候选。
- **用户级晋升**：候选在 ≥2 个不同项目中出现近似表述（8-gram Jaccard ≥ 0.5，跨项目扫描仅读 global 状态文件中的候选记录，不读其他项目的 `.memory/`）→ promoted。
- **注入**：session_start 在工作集之后追加用户级 promoted lessons，预算上限 200 token，独立小节 `## Lessons (global)`。
- 信息流动纪律：进入 global 的文本必须已过 scrub（§3.14）；`config.yml: global_lessons: false` 可整体关闭；global 目录不存在时全部行为静默跳过。

## 3.19 分级遗忘（archive）——v0.2 增量

原则：**压缩访问结构，不压缩信息。** L0 内容永不有损；「暴力」体现为逐级撤出索引与文件系统整包归档，活跃层规模与整理成本有界。

**四级生命周期**（深整理最后一个 pass 执行，全部阈值进 config.yml）：

| 级 | 判据（全部满足） | 动作 |
|---|---|---|
| hot | 默认 | 现状 |
| cold | `age > cold_days(90)` 且 evidence.refs == 0 且 evidence.hits == 0 且 salience < `salience_floor(0.2)` | 逐出全部索引（倒排／project.md／BM25 语料／granularity），`index/archive-index.md` 留单行 `id \| epoch \| intent` |
| frozen | cold 且 `age > frozen_days(365)` | 按季度分组：LLM 生成纪元摘要写 `archive/epoch-<YYYY-Qn>.md`（一段时代总结＋成员 id 清单＋各自 intent 单行）；成员事件文件打包 `archive/epoch-<YYYY-Qn>.tar.gz` 后从 `events/` 移除散文件 |
| purged | 永不自动 | 仅 `eventmem purge --before <date>` 手动命令，默认 `--dry-run`，需 `--yes` 才执行 |

**纪律**：

- 冻结打包原子性：写包 → 解包校验（数量与 id 逐一比对）→ 校验通过才删散文件；任何失败回滚并记日志。重跑幂等（已在包内的 id 跳过）。
- open／prospective／promoted-lesson 来源事件永不冷却；被活跃事件 superseded 链或 parent 引用的事件永不冻结（链目标必须可读）；读到指向 frozen 事件的链接时显示「已归档于 <epoch>」。
- `search` 默认只搜活跃层；`--all` 附带搜 archive-index 与纪元摘要（不解包）；`memory_read` 命中 frozen id 时提示 thaw。
- `eventmem thaw <epoch|id>`：解包回 `events/`，重入索引（下次 rebuild 自动收编），thaw 后重新计龄。
- `eventmem stats` 增列：各级事件数、归档包数与体积、活跃层占比。
- 与不可变原则的关系写明：tar 包是 L0 的一种存放形态而非删除；审计与幂等重跑对 frozen 层的成本变高（需 thaw），属刻意取舍。

## 5. 编码纪律汇总

1. hook 永不崩、永不慢（§3.9 护栏）。
2. key 只在环境变量；`.env` 在 .gitignore；`.env.example` 只有变量名。
3. 整理只写派生层与 lesson（§3.8）；store 不提供 intent/body 修改接口（§3.3）。
4. 索引写入一律临时文件 + `os.replace`。
5. hook 内不调 LLM；LLM 工作全部 `spawn_detached`。
6. 注释与 docstring 中文，简短；代码自解释优先。
