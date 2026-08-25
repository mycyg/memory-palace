"""L1 索引层：索引文件的构建与读取，全部由 L0 派生、可随时删除重建。

索引写入一律走临时文件 ＋ os.replace（SPEC §5.4），因此不存在中间态：整理被新
会话打断时旧索引仍然可用。

v0.2 增量：salience.json（显著性后验）、prefetch.json（预取候选）、
granularity.json（合并组与粗事件分段）三个派生文件由 consolidate 写、本模块读。
三者缺失时全部消费方退回 v0.1 行为，工作集产物逐字节不变。

分级遗忘（SPEC §3.19）：archive-index.md 既是归档索引，也是「哪些事件已冷却」
的唯一清单——rebuild_all 读它把冷事件排除在全部索引之外，因此不需要给重建函数
加排除集合参数，冷却结果对已有调用方完全透明。它同样是派生层：整份删掉只会让冷
事件回到热层，下一次深整理按判据重新冷却。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from .paths import MemoryPaths, atomic_write
from .schema import Event, id_to_datetime
from .store import Store

# 拉丁词元与 CJK 连续段；CJK 段切成字符 bigram
_TOKEN_RE = re.compile("[a-z0-9]+|[\\u3400-\\u4dbf\\u4e00-\\u9fff\\u3040-\\u30ff]+")
# lessons.md 行解析
_LESSON_RE = re.compile(r"^- \[([^\]]+)\] \((candidate|promoted|retired|adopted)\) (.*)$")
# global-lessons.md 行解析：跨项目 lesson 无来源事件 id
_GLOBAL_LESSON_RE = re.compile(r"^- \((candidate|promoted|retired)\) (.*)$")
# lesson 状态取值（adopted：建议文本已出现在项目 CLAUDE.md 中，SPEC §3.15）
LESSON_STATES: tuple[str, ...] = ("candidate", "promoted", "retired", "adopted")

PROJECT_INDEX_HEADER = "| id | kind | status | intent |"
PROJECT_INDEX_SEPARATOR = "|---|---|---|---|"
INTENT_COLUMN_CHARS = 80

_CLOSED_STATUSES: tuple[str, ...] = ("done", "abandoned", "superseded")

# ---- v0.2 派生文件与用户级目录 ----

SALIENCE_FILE = "salience.json"
PREFETCH_FILE = "prefetch.json"
GRANULARITY_FILE = "granularity.json"
CLAUDE_MD_SUGGESTIONS_FILE = "claude-md-suggestions.md"

# ---- 分级遗忘（SPEC §3.19）----

ARCHIVE_INDEX_TITLE = "# Archive index"
# 归档索引的一行：`id | epoch | intent`
_ARCHIVE_ROW_RE = re.compile(r"^(?P<id>\S+)\s*\|\s*(?P<epoch>\S+)\s*\|\s*(?P<intent>.*)$")
_EPOCH_RE = re.compile(r"^\d{4}-Q[1-4]$")

# 用户级（跨项目）目录，SPEC §3.18；不存在即全部行为静默跳过，本模块永不创建它
GLOBAL_DIR_ENV = "EVENTMEM_GLOBAL_DIR"
GLOBAL_LESSONS_FILE = "global-lessons.md"
GLOBAL_STATE_FILE = "global-lesson-state.json"
# 用户级 lesson 的独立注入预算（SPEC §3.18），不占 working_set_tokens
GLOBAL_LESSON_TOKENS = 200
# 预取区占工作集预算的份额上限：1/3（SPEC §3.12）
PREFETCH_BUDGET_DIVISOR = 3

OPEN_TITLE = "## Open events"
LIKELY_TITLE = "## Likely next"
OUTCOMES_TITLE = "## Recent outcomes"
LESSONS_TITLE = "## Lessons (promoted)"
GLOBAL_LESSONS_TITLE = "## Lessons (global)"


@dataclass
class Budget:
    """注入与浮现的硬预算（DESIGN §5）。"""

    working_set_tokens: int = 1500
    surface_k: int = 3
    stale_days: int = 14


@dataclass(frozen=True)
class ProjectRow:
    """project.md 的一行：全量单行索引的最小单位。

    合并组行的 id 单元格形如 `<首id>+2`；解析时 id 取首成员，group_size 记成员数。
    """

    id: str
    kind: str
    status: str
    intent: str
    group_size: int = 1


@dataclass(frozen=True)
class ArchiveRow:
    """archive-index.md 的一行：冷事件的最后一条可检索痕迹（SPEC §3.19）。"""

    id: str
    epoch: str
    intent: str

    @property
    def line(self) -> str:
        """单行形态；intent 压成单行并去掉竖线，保证这一行永远可被解析回来。"""
        return f"{self.id} | {self.epoch} | {_one_line(self.intent, INTENT_COLUMN_CHARS)}"


@dataclass(frozen=True)
class Segment:
    """粗事件的一个虚拟分段（只存在于索引层，SPEC §3.16）。"""

    label: str
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class MergedGroup:
    """一组被合并显示的连续事件；成员事件本身在 L0 里毫发无损。"""

    ids: tuple[str, ...]
    summary: str
    anchors_union: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        """组的稳定标识：首成员 id。"""
        return self.ids[0] if self.ids else ""

    @property
    def label_id(self) -> str:
        """组行方括号里的标识：`<首id>+n`。"""
        return f"{self.ids[0]}+{len(self.ids) - 1}" if self.ids else ""

    @property
    def line(self) -> str:
        """组行正文（不含列表符号）：`[<首id>+n] 组概括`。"""
        return f"[{self.label_id}] {_one_line(self.summary)}"


@dataclass
class Granularity:
    """granularity.json 的内存形态；缺文件时为空，一切消费方退回逐事件行为。"""

    merged: tuple[MergedGroup, ...] = ()
    coarse: Mapping[str, tuple[Segment, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._by_member = {mid: group for group in self.merged for mid in group.ids}

    def is_empty(self) -> bool:
        return not self.merged and not self.coarse

    def group_of(self, event_id: str) -> MergedGroup | None:
        """事件所属的合并组；不属于任何组返回 None。"""
        return self._by_member.get(event_id)

    def segment_label(self, event_id: str, file_anchor: str) -> str | None:
        """粗事件里覆盖该文件锚点的分段标题；无对应分段返回 None。"""
        for segment in self.coarse.get(event_id, ()):
            if file_anchor in segment.files:
                return segment.label
        return None


def estimate_tokens(text: str) -> int:
    """token 粗估：字符数除以 3，中文场景够用。"""
    return len(text) // 3


def tokenize(text: str) -> list[str]:
    """分词：按非字母数字切出拉丁词元，中文按字符 bigram。"""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text.lower()):
        chunk = match.group(0)
        if chunk[0].isascii():
            tokens.append(chunk)
        elif len(chunk) == 1:
            tokens.append(chunk)
        else:
            tokens.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return tokens


def intent_tokens(text: str) -> list[str]:
    """intent 倒排用的词元：长度 ≥2，去重保序。"""
    out: list[str] = []
    seen: set[str] = set()
    for token in tokenize(text):
        if len(token) >= 2 and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def anchor_key(kind: str, cue: str) -> str:
    """拼倒排索引的 key：file/error/intent 三类。"""
    return f"{kind}:{cue}"


def rebuild_all(store: Store, paths: MemoryPaths, budget: Budget, now: datetime) -> None:
    """全量重建索引文件；同输入下产物逐字节一致，可无限重跑。

    salience/prefetch/granularity 三个派生文件由整理写，这里只读不写——重建不会
    因为它们缺失而失败，只是退回 v0.1 的排序与逐事件展示。

    archive-index.md 里的冷事件在这里被排除在全部索引之外（SPEC §3.19）：它们的
    L0 文件仍在，只是不再进倒排／project.md／BM25 语料／工作集。
    """
    paths.ensure()
    archived = archived_ids(paths)
    events = [e for e in store.iter_events() if e.id not in archived]
    states = load_lesson_states(paths)  # 先读旧状态，晋升/退休结果在重建中保留
    granularity = load_granularity(paths)
    salience = salience_scores(paths)
    _write_project_index(paths, events, granularity)
    _write_anchor_map(paths, events)
    _write_lessons(paths, events, states)
    _write_working_set(paths, events, budget, now, states, granularity, salience)


def load_anchor_map(paths: MemoryPaths) -> dict[str, list[str]]:
    """读锚点倒排；文件缺失或损坏返回空字典。"""
    try:
        raw = paths.anchors.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, ids in loaded.items():
        if isinstance(ids, list):
            out[str(key)] = [str(i) for i in ids]
    return out


# ---- v0.2 派生文件：路径、读取、用户级目录 ----


def salience_file(paths: MemoryPaths) -> Path:
    """显著性后验文件（consolidate.deep 写）。"""
    return paths.index_dir / SALIENCE_FILE


def prefetch_file(paths: MemoryPaths) -> Path:
    """预取候选文件（两级整理都写）。"""
    return paths.index_dir / PREFETCH_FILE


def granularity_file(paths: MemoryPaths) -> Path:
    """粒度视图文件：合并组与粗事件分段（consolidate.deep 写）。"""
    return paths.index_dir / GRANULARITY_FILE


def claude_md_suggestions_file(paths: MemoryPaths) -> Path:
    """CLAUDE.md 晋升建议文件（consolidate.deep 写，永不自动改用户的 CLAUDE.md）。"""
    return paths.index_dir / CLAUDE_MD_SUGGESTIONS_FILE


# ---- 分级遗忘：归档索引与纪元（SPEC §3.19）----


def epoch_of(event_id: str) -> str:
    """事件 id 时间戳所属的季度纪元 `YYYY-Qn`；无法解析时间戳返回 `unknown`。"""
    moment = id_to_datetime(event_id)
    if moment is None:
        return "unknown"
    return f"{moment.year}-Q{(moment.month - 1) // 3 + 1}"


def is_epoch(text: str) -> bool:
    """字符串是否是一个纪元标识（`YYYY-Qn`）。"""
    return bool(_EPOCH_RE.match(text.strip()))


def epoch_end(epoch: str) -> datetime | None:
    """纪元的末日（含）；不是合法纪元返回 None。purge --before 用它判定整季是否已过期。"""
    if not is_epoch(epoch):
        return None
    year, quarter = int(epoch[:4]), int(epoch[-1])
    end_month = quarter * 3
    if end_month == 12:
        return datetime(year, 12, 31)
    return datetime(year, end_month + 1, 1) - timedelta(days=1)


def load_archive_index(paths: MemoryPaths) -> dict[str, ArchiveRow]:
    """读 archive-index.md；文件缺失或损坏返回空字典（冷事件回到热层，可重新冷却）。"""
    try:
        raw = paths.archive_index.read_text(encoding="utf-8")
    except OSError:
        return {}
    rows: dict[str, ArchiveRow] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ARCHIVE_ROW_RE.match(stripped)
        if not match:
            continue
        rows[match.group("id")] = ArchiveRow(
            id=match.group("id"),
            epoch=match.group("epoch"),
            intent=match.group("intent").strip(),
        )
    return rows


def archived_ids(paths: MemoryPaths) -> set[str]:
    """已冷却（含已冻结）的事件 id 集合。"""
    return set(load_archive_index(paths))


def write_archive_index(paths: MemoryPaths, rows: Mapping[str, ArchiveRow]) -> None:
    """整份重写归档索引，按 id 升序；空表也写出（保留标题行，语义是「一个都没有」）。"""
    lines = [ARCHIVE_INDEX_TITLE, ""]
    lines.extend(rows[event_id].line for event_id in sorted(rows))
    atomic_write(paths.archive_index, "\n".join(lines) + "\n")


def append_archive_rows(paths: MemoryPaths, rows: Iterable[ArchiveRow]) -> int:
    """并入新的归档行；已在表内的 id 不重复写，返回新增行数（重跑幂等）。"""
    existing = load_archive_index(paths)
    added = 0
    for row in rows:
        if row.id in existing:
            continue
        existing[row.id] = row
        added += 1
    if added:
        write_archive_index(paths, existing)
    return added


def remove_archive_rows(paths: MemoryPaths, ids: Iterable[str]) -> int:
    """从归档索引移除若干 id（thaw／purge 用），返回移除行数。"""
    existing = load_archive_index(paths)
    targets = [event_id for event_id in set(ids) if event_id in existing]
    for event_id in targets:
        del existing[event_id]
    if targets:
        write_archive_index(paths, existing)
    return len(targets)


def global_dir() -> Path | None:
    """用户级 lesson 目录；不存在返回 None（SPEC §3.18：静默跳过），本模块不创建它。"""
    override = (os.environ.get(GLOBAL_DIR_ENV) or "").strip()
    target = Path(override).expanduser() if override else Path.home() / ".claude" / "eventmem"
    try:
        return target if target.is_dir() else None
    except OSError:
        return None


def _read_json(path: Path) -> Any:
    """读 JSON；文件缺失或损坏一律返回 None，不让派生文件的问题炸掉调用方。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def load_salience(paths: MemoryPaths) -> dict[str, dict[str, Any]]:
    """读显著性后验的完整记录；文件缺失或损坏返回空字典。"""
    loaded = _read_json(salience_file(paths))
    if not isinstance(loaded, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for event_id, record in loaded.items():
        if isinstance(record, dict):
            out[str(event_id)] = record
    return out


def salience_scores(paths: MemoryPaths) -> dict[str, float]:
    """只取 {event_id: score}；缺文件返回空字典，消费方据此退回 v0.1 排序。"""
    scores: dict[str, float] = {}
    for event_id, record in load_salience(paths).items():
        try:
            scores[event_id] = float(record.get("score"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return scores


def load_prefetch(paths: MemoryPaths) -> dict[str, Any]:
    """读预取候选：{"generated", "items": [...], "anchors": [...]}；缺文件返回空结构。"""
    loaded = _read_json(prefetch_file(paths))
    if not isinstance(loaded, dict):
        return {"items": [], "anchors": []}
    items = loaded.get("items")
    anchors = loaded.get("anchors")
    return {
        "generated": str(loaded.get("generated") or ""),
        "items": [i for i in items if isinstance(i, dict)] if isinstance(items, list) else [],
        "anchors": [str(a) for a in anchors] if isinstance(anchors, list) else [],
    }


def load_granularity(paths: MemoryPaths) -> Granularity:
    """读粒度视图；缺文件返回空 Granularity，一切展示退回逐事件。"""
    loaded = _read_json(granularity_file(paths))
    if not isinstance(loaded, dict):
        return Granularity()

    merged: list[MergedGroup] = []
    raw_merged = loaded.get("merged")
    for group in raw_merged if isinstance(raw_merged, list) else []:
        if not isinstance(group, dict):
            continue
        ids = tuple(str(i) for i in group.get("ids", []) if str(i).strip())
        summary = str(group.get("summary") or "").strip()
        if len(ids) < 2 or not summary:
            continue  # 单成员或无概括的组没有展示价值
        union = tuple(str(a) for a in group.get("anchors_union", []) if str(a).strip())
        merged.append(MergedGroup(ids=ids, summary=summary, anchors_union=union))

    coarse: dict[str, tuple[Segment, ...]] = {}
    raw_coarse = loaded.get("coarse")
    for entry in raw_coarse if isinstance(raw_coarse, list) else []:
        if not isinstance(entry, dict):
            continue
        event_id = str(entry.get("id") or "").strip()
        raw_segments = entry.get("segments")
        if not event_id or not isinstance(raw_segments, list):
            continue
        segments: list[Segment] = []
        for item in raw_segments:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            if not label:
                continue
            files = tuple(str(f) for f in item.get("files", []) if str(f).strip())
            segments.append(Segment(label=label, files=files))
        if segments:
            coarse[event_id] = tuple(segments)

    return Granularity(merged=tuple(merged), coarse=coarse)


def load_config(paths: MemoryPaths) -> dict[str, Any]:
    """读 config.yml；缺失或损坏返回空字典（缺省即默认值，SPEC §3.10）。"""
    try:
        raw = paths.config.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = yaml.safe_load(raw)
    except Exception:  # noqa: BLE001 —— 配置损坏不阻断索引重建
        return {}
    return data if isinstance(data, dict) else {}


def global_lessons_enabled(paths: MemoryPaths) -> bool:
    """config.yml 的 global_lessons 开关，缺省为开（SPEC §3.18）。"""
    value = load_config(paths).get("global_lessons", True)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "0", "no", "off")


def load_global_lessons() -> list[str]:
    """读用户级 global-lessons.md 里 promoted 的条目；目录或文件不存在返回空列表。"""
    directory = global_dir()
    if directory is None:
        return []
    try:
        raw = (directory / GLOBAL_LESSONS_FILE).read_text(encoding="utf-8")
    except OSError:
        return []
    texts: list[str] = []
    for line in raw.splitlines():
        match = _GLOBAL_LESSON_RE.match(line.strip())
        if match and match.group(1) == "promoted" and match.group(2).strip():
            texts.append(match.group(2).strip())
    return texts


def load_lesson_states(paths: MemoryPaths) -> dict[str, str]:
    """读 lessons.md 里每个来源事件的 lesson 状态；文件缺失返回空字典。"""
    try:
        raw = paths.lessons.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    states: dict[str, str] = {}
    for line in raw.splitlines():
        match = _LESSON_RE.match(line.strip())
        if match:
            states[match.group(1)] = match.group(2)
    return states


def load_project_rows(paths: MemoryPaths) -> list[ProjectRow]:
    """读 project.md 的数据行；文件缺失返回空列表。"""
    try:
        raw = paths.project_index.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    rows: list[ProjectRow] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 4:
            continue
        if cells[0] in ("id", "") or set(cells[0]) <= {"-", ":"}:
            continue  # 表头与分隔行
        event_id, group_size = _split_group_id(cells[0])
        rows.append(
            ProjectRow(
                id=event_id,
                kind=cells[1],
                status=cells[2],
                intent=cells[3],
                group_size=group_size,
            )
        )
    return rows


def _split_group_id(cell: str) -> tuple[str, int]:
    """把 `<首id>+n` 拆成 (首id, 成员数)；普通 id 返回 (id, 1)。事件 id 不含 `+`，无歧义。"""
    head, plus, tail = cell.partition("+")
    if plus and tail.isdigit():
        return head, int(tail) + 1
    return cell, 1


def append_to_project_index(paths: MemoryPaths, e: Event) -> None:
    """向 project.md 追加一行；文件不存在时先补表头。避免每写一个事件就全量重建。"""
    paths.ensure()
    path = paths.project_index
    need_header = not path.is_file() or path.stat().st_size == 0
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        if need_header:
            fh.write(f"{PROJECT_INDEX_HEADER}\n{PROJECT_INDEX_SEPARATOR}\n")
        fh.write(_project_row(e) + "\n")


# ---- 各索引文件的写出 ----


def _write_project_index(
    paths: MemoryPaths,
    events: Sequence[Event],
    granularity: Granularity | None = None,
) -> None:
    """写全量单行索引；合并组的成员行折叠成一行组行（SPEC §3.16）。"""
    view = granularity or Granularity()
    lines = [PROJECT_INDEX_HEADER, PROJECT_INDEX_SEPARATOR]
    emitted: set[str] = set()
    for e in events:
        group = view.group_of(e.id)
        if group is None:
            lines.append(_project_row(e))
            continue
        if group.key in emitted:
            continue  # 组内后续成员并入已写出的组行
        emitted.add(group.key)
        lines.append(_group_project_row(group, e))
    atomic_write(paths.project_index, "\n".join(lines) + "\n")


def _project_row(e: Event) -> str:
    """一个事件的 project.md 行；intent 压成单行并截断到 80 字符。"""
    intent = _one_line(e.intent, INTENT_COLUMN_CHARS)
    return f"| {e.id} | {e.kind} | {e.status} | {intent} |"


def _group_project_row(group: MergedGroup, first: Event) -> str:
    """合并组的 project.md 行：id 单元格为 `<首id>+n`，kind/status 取首成员。"""
    summary = _one_line(group.summary, INTENT_COLUMN_CHARS)
    return f"| {group.label_id} | {first.kind} | {first.status} | {summary} |"


def _write_anchor_map(paths: MemoryPaths, events: Sequence[Event]) -> None:
    """写锚点倒排：file / error / intent 三类 key。"""
    mapping: dict[str, set[str]] = {}
    for e in events:
        for key in iter_anchor_keys(e, paths):
            mapping.setdefault(key, set()).add(e.id)
    ordered = {key: sorted(mapping[key]) for key in sorted(mapping)}
    atomic_write(paths.anchors, json.dumps(ordered, ensure_ascii=False, indent=2) + "\n")


def _write_lessons(paths: MemoryPaths, events: Sequence[Event], states: dict[str, str]) -> None:
    """写 lesson 表；沿用旧文件里的晋升/退休状态，新 lesson 默认 candidate。"""
    lines = ["# Lessons", ""]
    for e in events:
        if not e.lesson:
            continue
        state = states.get(e.id, "candidate")
        if state not in LESSON_STATES:
            state = "candidate"
        lines.append(f"- [{e.id}] ({state}) {_one_line(e.lesson)}")
    atomic_write(paths.lessons, "\n".join(lines) + "\n")


def _write_working_set(
    paths: MemoryPaths,
    events: Sequence[Event],
    budget: Budget,
    now: datetime,
    states: dict[str, str],
    granularity: Granularity | None = None,
    salience: Mapping[str, float] | None = None,
) -> None:
    """写工作集：open 全量 → 预取 → promoted lessons → 最近 outcome，填到预算为止。

    两个 v0.2 新区（Likely next / Lessons (global)）只在有内容时才渲染标题并记账，
    因此 prefetch.json 缺失、用户级目录不存在时，产物与 v0.1 逐字节一致。
    """
    view = granularity or Granularity()
    scores = salience or {}
    header = f"# Memory working set (generated {now.isoformat(timespec='seconds')})"

    prefetch_lines, prefetch_ids = _prefetch_lines(paths, budget)
    titles: list[str] = [OPEN_TITLE]
    if prefetch_lines:
        titles.append(LIKELY_TITLE)
    titles.extend((OUTCOMES_TITLE, LESSONS_TITLE))
    buckets: dict[str, list[str]] = {t: [] for t in titles}

    # 结构性开销先记账：标题行永远渲染，保证结构稳定
    used = len(header) + 1
    for title in titles:
        used += len(title) + 2

    stale_before = now - timedelta(days=budget.stale_days)
    candidates: list[tuple[str, str]] = []
    for e in reversed([x for x in events if x.status == "open"]):  # open 全量，新的在前
        candidates.append((OPEN_TITLE, _open_line(e, stale_before)))
    candidates.extend((LIKELY_TITLE, line) for line in prefetch_lines)
    for e in events:
        if e.lesson and states.get(e.id) == "promoted":
            candidates.append((LESSONS_TITLE, f"- {_one_line(e.lesson)} [{e.id}]"))
    candidates.extend(
        (OUTCOMES_TITLE, line) for line in _outcome_lines(events, view, scores, prefetch_ids)
    )

    for title, line in candidates:
        cost = len(line) + 1  # 行本身 ＋ 换行
        if (used + cost) // 3 > budget.working_set_tokens:  # 公式同 estimate_tokens
            break  # 严格优先级：第一条装不下即停止填充
        used += cost
        buckets[title].append(line)

    out: list[str] = [header]
    for title in titles:
        out.append("")
        out.append(title)
        out.extend(buckets[title])
    global_lines = _global_lesson_lines(paths)
    if global_lines:  # 独立小节、独立 200 token 预算（SPEC §3.18），不占工作集预算
        out.append("")
        out.append(GLOBAL_LESSONS_TITLE)
        out.extend(global_lines)
    atomic_write(paths.working_set, "\n".join(out) + "\n")


def _prefetch_lines(paths: MemoryPaths, budget: Budget) -> tuple[list[str], set[str]]:
    """预取区的行与其覆盖的事件 id；总量封顶在工作集预算的 1/3（SPEC §3.12）。"""
    items = load_prefetch(paths)["items"]
    if not items:
        return [], set()
    share = max(0, budget.working_set_tokens // PREFETCH_BUDGET_DIVISOR)
    lines: list[str] = []
    ids: set[str] = set()
    used = 0
    for item in items:
        event_id = str(item.get("event_id") or "").strip()
        text = str(item.get("text") or "").strip()
        if not event_id or not text or event_id in ids:
            continue
        line = f"- [{event_id}] {_one_line(text)}"
        anchor = str(item.get("anchor") or "").strip()
        if anchor:
            line += f" — 关联: {anchor}"
        cost = len(line) + 1
        if (used + cost) // 3 > share:
            break
        used += cost
        lines.append(line)
        ids.add(event_id)
    return lines, ids


def _outcome_lines(
    events: Sequence[Event],
    granularity: Granularity,
    salience: Mapping[str, float],
    skip_ids: set[str],
) -> list[str]:
    """Recent outcomes 的行：salience 降序，无 salience 时保持 v0.1 的新近度降序。"""
    ordered = [e for e in reversed(events) if e.status in _CLOSED_STATUSES and e.outcome]
    if salience:  # 稳定排序：同分者保持新近度次序
        ordered.sort(key=lambda e: _event_score(e.id, granularity, salience), reverse=True)

    lines: list[str] = []
    emitted: set[str] = set()
    for e in ordered:
        group = granularity.group_of(e.id)
        if group is None:
            if e.id in skip_ids:
                continue  # 已在预取区出现过，不重复占预算
            lines.append(f"- [{e.id}] {_one_line(e.outcome or '')}")
            continue
        if group.key in emitted or any(mid in skip_ids for mid in group.ids):
            continue
        emitted.add(group.key)
        lines.append(f"- {group.line}")
    return lines


def _event_score(event_id: str, granularity: Granularity, salience: Mapping[str, float]) -> float:
    """事件（或其所在合并组）的显著性；组取成员最大值。"""
    group = granularity.group_of(event_id)
    if group is None:
        return salience.get(event_id, 0.0)
    return max((salience.get(mid, 0.0) for mid in group.ids), default=0.0)


def _global_lesson_lines(paths: MemoryPaths) -> list[str]:
    """用户级 promoted lessons 的行，独立 200 token 上限；目录不存在或开关关闭返回空。"""
    if not global_lessons_enabled(paths):
        return []
    lines: list[str] = []
    used = 0
    for text in load_global_lessons():
        line = f"- {_one_line(text)}"
        cost = len(line) + 1
        if (used + cost) // 3 > GLOBAL_LESSON_TOKENS:
            break
        used += cost
        lines.append(line)
    return lines


def _open_line(e: Event, stale_before: datetime) -> str:
    """open 事件在工作集里的一行；超期事件行尾标 (stale)。"""
    line = f"- [{e.id}] ({e.kind}) {_one_line(e.intent)}"
    anchor = _latest_anchor(e)
    if anchor:
        line += f" — {anchor}"
    created = id_to_datetime(e.id)
    if created is not None and created < stale_before:
        line += " (stale)"
    return line


def _latest_anchor(e: Event) -> str:
    """取事件最近的一个锚点，按 commit → file → test → dialog 的优先级。"""
    for label, values in (
        ("commit", e.anchors.commits),
        ("file", e.anchors.files),
        ("test", e.anchors.tests),
        ("dialog", e.anchors.dialog),
    ):
        if values:
            return f"{label} {values[-1]}"
    return ""


def _one_line(text: str, limit: int = 0) -> str:
    """压成单行：换行与连续空白折成单空格，竖线换成斜杠，按需截断。"""
    flat = " ".join(text.split()).replace("|", "/")
    if limit and len(flat) > limit:
        return flat[: limit - 1] + "…"
    return flat


def iter_anchor_keys(e: Event, paths: MemoryPaths) -> Iterable[str]:
    """列出一个事件应进入倒排的全部 key，供增量更新与测试复用。"""
    for f in e.anchors.files:
        yield anchor_key("file", paths.relative(f))
    for sig in e.anchors.error_sigs:
        yield anchor_key("error", sig)
    for token in intent_tokens(e.intent):
        yield anchor_key("intent", token)
