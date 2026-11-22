"""四级可控遗忘与瘦身生命周期管理器及一致性体检器 (Task T11)。

本模块提供 ``LifecycleManager``，负责长篇创作全周期的七大核心维护操作：
1. ``promote_chapter``: 审校定稿入库并自动触发过程产物 TTL 清理；
2. ``clean_ttl_artifacts``: 清理超出最近保留窗口的历史写作包与草稿；
3. ``cold_archive_gc``: 退场/死亡/超期实体、失效关系边与已填伏笔冷库归档；
4. ``drop_thread``: 主动弃坑伏笔的影响面预检与物理移除；
5. ``prune_settings``: 孤儿废案与远期一次性龙套设定卡片物理剪枝；
6. ``compact_volume_facts``: 完结老卷原子事实蒸馏浓缩为里程碑记录；
7. ``audit_chapter``: 单章或全书静态一致性体检（引用/死人/冷库/红线/扩展槽）。
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Self

import yaml
from pydantic import TypeAdapter, ValidationError

from .alias_matcher import AliasMatcher
from .indexer import SqliteIndexer
from .parser import ProgressiveParser, lint_model_setting_doc, split_frontmatter
from .paths import (
    extract_clean_title,
    find_existing_manuscript_files,
    format_chapter_stem,
    infer_volume_id_from_stem,
    is_legacy_book,
    list_facts_files,
    resolve_active_characters_file,
    resolve_active_factions_file,
    resolve_archive_characters_file,
    resolve_archive_factions_file,
    resolve_archive_relations_file,
    resolve_archive_threads_file,
    resolve_blueprints_dir,
    resolve_config_path,
    resolve_drafts_dir,
    resolve_facts_file,
    resolve_manuscript_dir,
    resolve_manuscript_file,
    resolve_packets_dir,
    resolve_relations_file,
    resolve_settings_root,
    resolve_style_guide_file,
    resolve_threads_file,
    resolve_volumes_dir,
    resolve_world_state_file,
    sanitize_filename_label,
)
from .schemas import (
    VOLUME_ID_PATTERN,
    AuditIssue,
    AuditReport,
    BlueprintDoc,
    ChapterFactRecord,
    CharacterState,
    CharacterStateFile,
    CompactResult,
    DropResult,
    EntityId,
    EntityStatus,
    FactionState,
    FactionStateFile,
    GCReport,
    ImpactHit,
    ImpactReport,
    ImpactScope,
    IssueLevel,
    NovelConfig,
    PromoteResult,
    PruneReport,
    RelationsFile,
    SettingDoc,
    StyleGuide,
    TemporalRelation,
    Thread,
    ThreadAction,
    ThreadId,
    ThreadsFile,
    ThreadStatus,
    VolumeDoc,
    VolumeId,
    WorldState,
    format_chapter,
    get_genre,
)

_ENTITY_ID_ADAPTER: Final[TypeAdapter[str]] = TypeAdapter(EntityId)
_THREAD_ID_ADAPTER: Final[TypeAdapter[str]] = TypeAdapter(ThreadId)
_VOLUME_ID_ADAPTER: Final[TypeAdapter[str]] = TypeAdapter(VolumeId)

_VOLUME_DIR_RE: Final[re.Pattern[str]] = re.compile(VOLUME_ID_PATTERN)
_CH_FILE_RE: Final[re.Pattern[str]] = re.compile(r"(?:ch_?|第)0*(\d+)", re.IGNORECASE)
_CH_CN_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^\s*#\s*第\s*0*(\d+)\s*章", re.MULTILINE)

_FLASHBACK_MARKERS: Final[tuple[str, ...]] = (
    "回忆",
    "生前",
    "幻境",
    "遗体",
    "旧事",
    "往事",
    "梦境",
    "闪回",
    "头七",
    "祭奠",
    "坟前",
    "灵位",
    "牌位",
    "墓碑",
    "遗言",
)


def _validate_non_bool_int(value: Any, *, name: str, min_value: int) -> int:
    """校验非布尔整数参数并确保不小于指定下界。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < min_value:
        raise ValueError(f"{name} 必须为 >= {min_value} 的整数，收到: {value!r}")
    return value


def _dedupe_strs(items: Sequence[str]) -> list[str]:
    """清理字符串序列中的空白与空串并保持首次出现顺序去重。"""
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        cleaned = str(item).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def _dump_yaml_file(path: Path, payload: Any) -> None:
    """将结构化字典以作者友好的 UTF-8 YAML 格式落盘。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml_text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    path.write_text(yaml_text, encoding="utf-8")


def _extract_chapter_from_path(path: Path, content: str | None = None) -> int | None:
    """从文件名（如 ch_0012_draft.md / ch_12.md）或正文首行标题提取章号。"""
    m = _CH_FILE_RE.search(path.stem)
    if m:
        ch = int(m.group(1))
        if ch >= 1:
            return ch
    if content is not None:
        m_title = _CH_CN_HEADING_RE.search(content)
        if m_title:
            ch = int(m_title.group(1))
            if ch >= 1:
                return ch
    return None


def _make_excerpt(text: str, start: int, end: int, *, radius: int = 60) -> str:
    """截取命中位置前后上下文作为紧凑单行摘录。"""
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    snippet = re.sub(r"\s+", " ", text[left:right]).strip()
    if left > 0:
        snippet = f"...{snippet}"
    if right < len(text):
        snippet = f"{snippet}..."
    return snippet


@dataclass(frozen=True)
class ExportTxtResult:
    """将所有定稿正文合并导出为单个 TXT 文件的结果。"""

    output_path: str
    chapter_count: int
    chapters: list[int]
    source_paths: list[str]
    total_chars: int
    dry_run: bool = False


def _format_chapter_for_txt(
    body: str,
    *,
    strip_markdown: bool,
) -> str:
    """规范化单章正文换行并按需剥离 Markdown 标题前缀。"""
    text = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    if strip_markdown and text:
        text = re.sub(r"^[ \t]*#{1,6}[ \t]+", "", text, flags=re.MULTILINE)
    return text.strip()


class LifecycleManager:
    """四级可控遗忘与瘦身生命周期管理器及一致性体检器。"""

    def __init__(
        self,
        book_dir: Path | str,
        *,
        indexer: SqliteIndexer | None = None,
        impact_analyzer: Any | None = None,
        parser: ProgressiveParser | None = None,
        config: NovelConfig | None = None,
        auto_sync: bool = True,
    ) -> None:
        self._book_dir: Path = Path(book_dir).resolve()
        self._external_indexer: SqliteIndexer | None = indexer
        self._impact_analyzer: Any | None = impact_analyzer
        self._auto_sync: bool = auto_sync
        self._owned_indexer: SqliteIndexer | None = None

        if config is not None:
            self._config = config
        elif indexer is not None:
            self._config = indexer.config
        else:
            cfg_path = resolve_config_path(self._book_dir)
            if cfg_path.is_file():
                self._config = NovelConfig.load(self._book_dir)
            else:
                self._config = NovelConfig(title=self._book_dir.name or "未命名小说")

        self._parser: ProgressiveParser = (
            parser if parser is not None else ProgressiveParser(book_dir=self._book_dir)
        )

    @property
    def book_dir(self) -> Path:
        """返回解析后的小说书库根目录。"""
        return self._book_dir

    @property
    def config(self) -> NovelConfig:
        """返回当前使用的小说配置对象。"""
        return self._config

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """幂等释放内部创建的 SQLite 连接（绝不关闭外部注入的 indexer）。"""
        if self._owned_indexer is not None:
            try:
                self._owned_indexer.close()
            finally:
                self._owned_indexer = None

    @contextmanager
    def _use_indexer(self) -> Any:
        """提供生命周期安全的 SqliteIndexer 会话上下文。"""
        if self._external_indexer is not None:
            yield self._external_indexer
            return

        if self._owned_indexer is not None:
            yield self._owned_indexer
            return

        temp_indexer = SqliteIndexer(
            self._book_dir,
            config=self._config,
            parser=self._parser,
        )
        self._owned_indexer = temp_indexer
        try:
            yield temp_indexer
        finally:
            self._owned_indexer = None
            temp_indexer.close()

    def _rel_posix(self, path: Path) -> str:
        """将路径格式化为相对书库根目录的 POSIX 字符串。"""
        try:
            return path.resolve().relative_to(self._book_dir).as_posix()
        except ValueError:
            return path.as_posix()

    def _should_sync(self, sync_index: bool | None) -> bool:
        return self._auto_sync if sync_index is None else sync_index

    # ------------------------------------------------------------------
    # 方法 1: promote_chapter (定稿归档 + 进度更新 + TTL 清理)
    # ------------------------------------------------------------------

    def promote_chapter(
        self,
        chapter_num: int,
        *,
        volume_id: str | None = None,
        dry_run: bool = False,
        run_audit: bool = False,
        sync_index: bool | None = None,
    ) -> PromoteResult:
        """将审校通过的草稿归档为正式定稿，并自动触发超期过程文件 TTL 清理。"""
        ch = _validate_non_bool_int(chapter_num, name="chapter_num", min_value=1)
        if volume_id is not None:
            volume_id = _VOLUME_ID_ADAPTER.validate_python(volume_id)

        draft_abs = self._locate_draft_file(ch)
        draft_rel = self._rel_posix(draft_abs)
        draft_text = draft_abs.read_text(encoding="utf-8")

        if run_audit:
            report = self.audit_chapter(ch, sync_index=sync_index)
            if not report.passed:
                err_msgs = "; ".join(
                    f"[{iss.code}] {iss.message}"
                    for iss in report.issues
                    if iss.level == IssueLevel.ERROR
                )
                raise ValueError(f"第 {ch} 章定稿前一致性体检未通过: {err_msgs}")

        resolved_vol = volume_id if volume_id is not None else self._infer_volume_id(ch)
        legacy_mode = is_legacy_book(self._book_dir)
        chapter_title = "" if legacy_mode else self._resolve_chapter_title(ch, draft_text)
        manuscript_abs = resolve_manuscript_file(
            self._book_dir,
            resolved_vol,
            ch,
            width=self._config.chapter_id_width,
            title=chapter_title,
            for_write=True,
        )
        manuscript_rel = self._rel_posix(manuscript_abs)

        effective_cur = max(ch, self._config.current_chapter)

        if dry_run:
            cleaned_abs = self.clean_ttl_artifacts(effective_cur, dry_run=True)
            return PromoteResult(
                chapter=ch,
                draft_path=draft_rel,
                manuscript_path=manuscript_rel,
                cleaned_paths=[self._rel_posix(p) for p in cleaned_abs],
                dry_run=True,
            )

        manuscript_abs.parent.mkdir(parents=True, exist_ok=True)
        manuscript_abs.write_text(draft_text, encoding="utf-8")
        for stale in find_existing_manuscript_files(self._book_dir, ch, vol_id=resolved_vol):
            if stale.resolve() != manuscript_abs.resolve():
                stale.unlink(missing_ok=True)

        cfg_path = resolve_config_path(self._book_dir, for_write=True)
        if ch > self._config.current_chapter:
            self._config = self._config.model_copy(update={"current_chapter": ch})
            if cfg_path.is_file():
                self._config.dump(self._book_dir)

        ws_path = resolve_world_state_file(self._book_dir, for_write=True)
        if ws_path.is_file():
            raw_ws = yaml.safe_load(ws_path.read_text(encoding="utf-8"))
            if isinstance(raw_ws, dict):
                ws = WorldState.model_validate(raw_ws)
                if ch > ws.current_chapter:
                    ws_updated = ws.model_copy(update={"current_chapter": ch})
                    _dump_yaml_file(
                        ws_path,
                        ws_updated.model_dump(mode="json", by_alias=True, exclude_none=True),
                    )

        cleaned_abs = self.clean_ttl_artifacts(effective_cur, dry_run=False)

        if self._should_sync(sync_index):
            with self._use_indexer() as active_indexer:
                active_indexer.sync()

        return PromoteResult(
            chapter=ch,
            draft_path=draft_rel,
            manuscript_path=manuscript_rel,
            cleaned_paths=[self._rel_posix(p) for p in cleaned_abs],
            dry_run=False,
        )

    def _resolve_chapter_title(self, chapter_num: int, draft_text: str) -> str:
        """按草稿 Frontmatter -> 正文标题 -> 蓝图标题优先级提取定稿干净副标题。"""
        fm_draft, draft_body = split_frontmatter(draft_text)
        raw_fm_title = fm_draft.get("title")
        if raw_fm_title:
            cleaned = extract_clean_title(str(raw_fm_title), "chapter")
            if cleaned:
                return cleaned

        m_h1 = re.search(r"^\s*#\s+([^#\r\n]+)", draft_body, re.MULTILINE)
        if m_h1:
            cleaned = extract_clean_title(m_h1.group(1), "chapter")
            if cleaned:
                return cleaned
        else:
            m_h2 = re.search(
                r"^\s*##\s+(第\s*[零〇一二三四五六七八九十百千万两\d]+\s*[章回节][^\r\n]*)$",
                draft_body,
                re.MULTILINE,
            )
            if m_h2:
                cleaned = extract_clean_title(m_h2.group(1), "chapter")
                if cleaned:
                    return cleaned

        bp_hit = self._locate_blueprint_for_chapter(chapter_num)
        if bp_hit is not None:
            bp_doc, _ = bp_hit
            if bp_doc.title:
                cleaned = extract_clean_title(bp_doc.title, "chapter")
                if cleaned:
                    return cleaned

        return ""

    def _locate_draft_file(self, chapter_num: int) -> Path:
        drafts_dir = resolve_drafts_dir(self._book_dir)
        if not drafts_dir.is_dir():
            raise FileNotFoundError(f"未找到第 {chapter_num} 章草稿文件（目录不存在: {drafts_dir}）")

        ch_prefix = format_chapter(chapter_num, width=self._config.chapter_id_width)
        zh_prefix = format_chapter_stem(chapter_num, width=self._config.chapter_id_width, legacy=False)
        candidates = [
            drafts_dir / f"{zh_prefix}_草稿.md",
            drafts_dir / f"第{chapter_num}章_草稿.md",
            drafts_dir / f"{ch_prefix}_draft.md",
            drafts_dir / f"ch_{chapter_num}_draft.md",
        ]
        for cand in candidates:
            if cand.is_file():
                return cand

        ch_re = re.compile(rf"^(?:ch_?0*{chapter_num}_draft|第0*{chapter_num}章_草稿)$", re.IGNORECASE)
        for p in sorted(drafts_dir.glob("*.md")):
            if p.is_file() and ch_re.match(p.stem):
                return p

        raise FileNotFoundError(
            f"未找到第 {chapter_num} 章草稿文件: {drafts_dir / f'{zh_prefix}_草稿.md'}"
        )

    def _locate_blueprint_for_chapter(self, chapter_num: int) -> tuple[BlueprintDoc, Path] | None:
        bp_root = resolve_blueprints_dir(self._book_dir)
        if not bp_root.is_dir():
            return None

        all_files = sorted(p for p in bp_root.rglob("*.md") if p.is_file())
        if not all_files:
            return None

        expected_stem = format_chapter(chapter_num, width=self._config.chapter_id_width)
        expected_zh_stem = format_chapter_stem(
            chapter_num, width=self._config.chapter_id_width, legacy=False
        )
        ch_stem_re = re.compile(rf"^(?:ch_?|第)0*{chapter_num}(?:$|[^0-9])", re.IGNORECASE)

        prioritized: list[Path] = []
        fallback: list[Path] = []
        for path in all_files:
            if path.stem in (expected_stem, expected_zh_stem) or ch_stem_re.match(path.stem):
                prioritized.append(path)
            else:
                fallback.append(path)

        for path in (*prioritized, *fallback):
            parsed = self._parser.parse_blueprint_md(path, book_dir=self._book_dir)
            if parsed.chapter == chapter_num:
                return parsed, path
        return None

    def _infer_volume_id(self, chapter_num: int) -> str:
        # 1. 对应章节蓝图 volume_id 或父目录 vol_XX
        bp_hit = self._locate_blueprint_for_chapter(chapter_num)
        if bp_hit is not None:
            bp, bp_path = bp_hit
            if bp.volume_id is not None:
                return bp.volume_id
            for part in reversed(bp_path.parts):
                inferred_part = infer_volume_id_from_stem(part)
                if _VOLUME_DIR_RE.match(inferred_part):
                    return inferred_part

        # 2. 章节范围覆盖该章的分卷文件
        vol_dir = resolve_volumes_dir(self._book_dir)
        if vol_dir.is_dir():
            for path in sorted(vol_dir.glob("*.md")):
                if not path.is_file():
                    continue
                fm, body = split_frontmatter(path.read_text(encoding="utf-8"))
                vid = fm.get("id") or infer_volume_id_from_stem(path.stem)
                try:
                    doc = VolumeDoc.model_validate({**fm, "id": vid, "body": body})
                except ValidationError:
                    continue
                if doc.chapter_range is not None:
                    if doc.chapter_range.start <= chapter_num <= doc.chapter_range.end:
                        return doc.id

        # 3. 现有最大定稿分卷目录
        ms_root = resolve_manuscript_dir(self._book_dir)
        if ms_root.is_dir():
            vol_candidates: list[tuple[int, str]] = []
            for d in ms_root.iterdir():
                if not d.is_dir():
                    continue
                if _VOLUME_DIR_RE.match(d.name):
                    vol_candidates.append((int(d.name.split("_")[1]), d.name))
                else:
                    m_zh = re.match(r"^第(\d+)卷$", d.name)
                    if m_zh:
                        num = int(m_zh.group(1))
                        vol_candidates.append((num, f"vol_{num:02d}"))
            if vol_candidates:
                return max(vol_candidates, key=lambda item: (item[0], item[1]))[1]

        # 4. 默认 vol_01
        return "vol_01"

    # ------------------------------------------------------------------
    # 方法 2: clean_ttl_artifacts (TTL 过程产物自动清理)
    # ------------------------------------------------------------------

    def clean_ttl_artifacts(
        self,
        current_ch: int | None = None,
        *,
        keep_chapters: int | None = None,
        dry_run: bool = False,
    ) -> list[Path]:
        """清理超出保留章数窗口的历史写作包与草稿文件，返回待删/已删路径列表。"""
        cur = (
            self._config.current_chapter
            if current_ch is None
            else _validate_non_bool_int(current_ch, name="current_ch", min_value=0)
        )
        keep = (
            self._config.lifecycle.ttl_keep_chapters
            if keep_chapters is None
            else _validate_non_bool_int(keep_chapters, name="keep_chapters", min_value=0)
        )

        cutoff = cur - keep
        if cutoff < 1:
            return []

        cleaned: list[Path] = []
        for sub_dir in (
            resolve_packets_dir(self._book_dir),
            resolve_drafts_dir(self._book_dir),
        ):
            if not sub_dir.is_dir():
                continue
            for path in sorted(sub_dir.rglob("*.md")):
                if not path.is_file():
                    continue
                file_ch = _extract_chapter_from_path(path)
                if file_ch is not None and 1 <= file_ch <= cutoff:
                    if not dry_run:
                        path.unlink()
                    cleaned.append(path)

        return sorted(cleaned, key=lambda p: p.as_posix())

    # ------------------------------------------------------------------
    # 方法 3: cold_archive_gc (一级/二级冷归档垃圾回收)
    # ------------------------------------------------------------------

    def cold_archive_gc(
        self,
        current_ch: int | None = None,
        dry_run: bool = False,
        *,
        cold_after_chapters: int | None = None,
        sync_index: bool | None = None,
    ) -> GCReport:
        """将死亡/退场/超期冷实体、失效关系与已填完伏笔移入 03_state/archive/ 冷库。"""
        cur_ch = (
            self._config.current_chapter
            if current_ch is None
            else _validate_non_bool_int(current_ch, name="current_ch", min_value=0)
        )
        cold_threshold = (
            self._config.lifecycle.cold_after_chapters
            if cold_after_chapters is None
            else _validate_non_bool_int(cold_after_chapters, name="cold_after_chapters", min_value=1)
        )

        settings_by_id = {eid: doc for eid, (doc, _) in self._load_all_settings().items()}
        max_fact_ch = self._collect_max_fact_chapter_by_entity()

        # 收集未完结伏笔绑定的实体集合
        threads_path = resolve_threads_file(self._book_dir, for_write=True)
        active_threads_file = self._load_threads_file(threads_path)
        protected_entities: set[str] = set()
        for thr in active_threads_file.threads:
            if thr.status != ThreadStatus.RESOLVED:
                protected_entities.update(thr.trigger_entities)

        modified_paths: list[str] = []

        # 1) 角色热池 -> 冷库
        archived_characters: list[str] = []
        active_chars_path = resolve_active_characters_file(self._book_dir, for_write=True)
        archive_chars_path = resolve_archive_characters_file(self._book_dir, for_write=True)
        active_chars = self._load_character_state_file(active_chars_path)
        if active_chars.root:
            kept_chars: dict[str, CharacterState] = {}
            moved_chars: dict[str, CharacterState] = {}
            for eid, c_state in active_chars.root.items():
                s_doc = settings_by_id.get(eid)
                is_dead_or_retired = s_doc is not None and s_doc.status in (
                    EntityStatus.DECEASED,
                    EntityStatus.RETIRED,
                )
                last_seen = max(c_state.updated_at_ch, max_fact_ch.get(eid, 0))
                is_cold = (cur_ch - last_seen) > cold_threshold and eid not in protected_entities
                if is_dead_or_retired or is_cold:
                    moved_chars[eid] = c_state
                    archived_characters.append(eid)
                else:
                    kept_chars[eid] = c_state

            if moved_chars:
                modified_paths.extend(
                    [self._rel_posix(active_chars_path), self._rel_posix(archive_chars_path)]
                )
                if not dry_run:
                    existing_arch = self._load_character_state_file(archive_chars_path)
                    merged_arch = {**existing_arch.root, **moved_chars}
                    _dump_yaml_file(
                        active_chars_path,
                        CharacterStateFile(root=kept_chars).model_dump(
                            mode="json", by_alias=True, exclude_none=True
                        ),
                    )
                    _dump_yaml_file(
                        archive_chars_path,
                        CharacterStateFile(root=merged_arch).model_dump(
                            mode="json", by_alias=True, exclude_none=True
                        ),
                    )

        # 2) 势力热池 -> 冷库
        archived_factions: list[str] = []
        active_facs_path = resolve_active_factions_file(self._book_dir, for_write=True)
        archive_facs_path = resolve_archive_factions_file(self._book_dir, for_write=True)
        active_facs = self._load_faction_state_file(active_facs_path)
        if active_facs.root:
            kept_facs: dict[str, FactionState] = {}
            moved_facs: dict[str, FactionState] = {}
            for eid, f_state in active_facs.root.items():
                s_doc = settings_by_id.get(eid)
                is_dead_or_retired = f_state.status in (
                    EntityStatus.DECEASED,
                    EntityStatus.RETIRED,
                ) or (
                    s_doc is not None
                    and s_doc.status in (EntityStatus.DECEASED, EntityStatus.RETIRED)
                )
                last_seen = max(f_state.updated_at_ch, max_fact_ch.get(eid, 0))
                is_cold = (cur_ch - last_seen) > cold_threshold and eid not in protected_entities
                if is_dead_or_retired or is_cold:
                    moved_facs[eid] = f_state
                    archived_factions.append(eid)
                else:
                    kept_facs[eid] = f_state

            if moved_facs:
                modified_paths.extend(
                    [self._rel_posix(active_facs_path), self._rel_posix(archive_facs_path)]
                )
                if not dry_run:
                    existing_arch = self._load_faction_state_file(archive_facs_path)
                    merged_arch = {**existing_arch.root, **moved_facs}
                    _dump_yaml_file(
                        active_facs_path,
                        FactionStateFile(root=kept_facs).model_dump(
                            mode="json", by_alias=True, exclude_none=True
                        ),
                    )
                    _dump_yaml_file(
                        archive_facs_path,
                        FactionStateFile(root=merged_arch).model_dump(
                            mode="json", by_alias=True, exclude_none=True
                        ),
                    )

        # 3) 双时态关系边 -> archive/relations.yaml
        relations_path = resolve_relations_file(self._book_dir, for_write=True)
        archive_relations_path = resolve_archive_relations_file(self._book_dir, for_write=True)
        relations_file = self._load_relations_file(relations_path)
        archived_relations_count = 0
        if relations_file.relations:
            kept_rels: list[TemporalRelation] = []
            moved_rels: list[TemporalRelation] = []
            for rel in relations_file.relations:
                if rel.invalid_at_ch is not None and rel.invalid_at_ch <= cur_ch:
                    moved_rels.append(rel)
                else:
                    kept_rels.append(rel)

            if moved_rels:
                archived_relations_count = len(moved_rels)
                modified_paths.extend(
                    [self._rel_posix(relations_path), self._rel_posix(archive_relations_path)]
                )
                if not dry_run:
                    existing_arch_rels = self._load_relations_file(archive_relations_path)
                    merged_rels = [*existing_arch_rels.relations, *moved_rels]
                    _dump_yaml_file(
                        relations_path,
                        relations_file.model_copy(update={"relations": kept_rels}).model_dump(
                            mode="json", by_alias=True, exclude_none=True
                        ),
                    )
                    _dump_yaml_file(
                        archive_relations_path,
                        existing_arch_rels.model_copy(update={"relations": merged_rels}).model_dump(
                            mode="json", by_alias=True, exclude_none=True
                        ),
                    )

        # 4) 已完结伏笔 -> archive/threads.yaml
        archive_threads_path = resolve_archive_threads_file(self._book_dir, for_write=True)
        archived_threads: list[str] = []
        if active_threads_file.threads:
            kept_threads: list[Thread] = []
            moved_threads: list[Thread] = []
            for thr in active_threads_file.threads:
                if thr.status == ThreadStatus.RESOLVED:
                    moved_threads.append(thr)
                    archived_threads.append(thr.id)
                else:
                    kept_threads.append(thr)

            if moved_threads:
                modified_paths.extend(
                    [self._rel_posix(threads_path), self._rel_posix(archive_threads_path)]
                )
                if not dry_run:
                    existing_arch_threads = self._load_threads_file(archive_threads_path)
                    by_id = {t.id: t for t in existing_arch_threads.threads}
                    for t in moved_threads:
                        by_id[t.id] = t
                    _dump_yaml_file(
                        threads_path,
                        active_threads_file.model_copy(update={"threads": kept_threads}).model_dump(
                            mode="json", by_alias=True, exclude_none=True
                        ),
                    )
                    _dump_yaml_file(
                        archive_threads_path,
                        existing_arch_threads.model_copy(
                            update={"threads": list(by_id.values())}
                        ).model_dump(mode="json", by_alias=True, exclude_none=True),
                    )

        if not dry_run and modified_paths and self._should_sync(sync_index):
            with self._use_indexer() as active_indexer:
                active_indexer.sync()

        return GCReport(
            current_chapter=cur_ch,
            archived_characters=archived_characters,
            archived_factions=archived_factions,
            archived_relations_count=archived_relations_count,
            archived_threads=archived_threads,
            modified_paths=modified_paths,
            dry_run=dry_run,
        )

    # ------------------------------------------------------------------
    # 方法 4: drop_thread (主动弃坑伏笔硬删除)
    # ------------------------------------------------------------------

    def drop_thread(
        self,
        thread_id: str,
        *,
        dry_run: bool = False,
        sync_index: bool | None = None,
    ) -> DropResult:
        """检查前文/后文提及并从伏笔台账物理移除指定弃坑伏笔。"""
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError(f"thread_id 必须为非空合法 ThreadId 字符串，收到: {thread_id!r}")
        tid = _THREAD_ID_ADAPTER.validate_python(thread_id.strip())

        active_threads_path = resolve_threads_file(self._book_dir, for_write=True)
        archive_threads_path = resolve_archive_threads_file(self._book_dir, for_write=True)

        target_path: Path | None = None
        threads_file: ThreadsFile | None = None
        found_thread: Thread | None = None

        for cand_path in (active_threads_path, archive_threads_path):
            loaded = self._load_threads_file(cand_path)
            for item in loaded.threads:
                if item.id == tid:
                    target_path = cand_path
                    threads_file = loaded
                    found_thread = item
                    break
            if found_thread is not None:
                break

        if found_thread is None or target_path is None or threads_file is None:
            raise KeyError(f"未在伏笔台账或冷库中找到伏笔 '{tid}'")

        removed_from = self._rel_posix(target_path)

        with self._use_indexer() as active_indexer:
            if self._should_sync(sync_index):
                active_indexer.sync()
            report = self._run_impact_scan(
                tid,
                active_indexer=active_indexer,
                extra_terms=[found_thread.title],
            )

        other_threads = [t for t in threads_file.threads if t.id != tid]
        other_mentions_self = any(
            tid in t.title
            or tid in t.public_clue
            or tid in (t.truth_secret or "")
            or found_thread.title in t.title
            or found_thread.title in t.public_clue
            or found_thread.title in (t.truth_secret or "")
            for t in other_threads
        )

        prior_mentions: list[ImpactHit] = []
        for hit in report.hits:
            if hit.scope == ImpactScope.THREAD and hit.location == removed_from:
                if not other_mentions_self:
                    continue
                other_hit_in_excerpt = any(
                    ot.id in hit.excerpt or ot.title in hit.excerpt for ot in other_threads
                )
                if not other_hit_in_excerpt and (
                    tid in hit.excerpt or found_thread.title in hit.excerpt
                ):
                    continue
            prior_mentions.append(hit)

        if not dry_run:
            remaining = [t for t in threads_file.threads if t.id != tid]
            _dump_yaml_file(
                target_path,
                threads_file.model_copy(update={"threads": remaining}).model_dump(
                    mode="json", by_alias=True, exclude_none=True
                ),
            )
            if self._should_sync(sync_index):
                with self._use_indexer() as active_indexer:
                    active_indexer.sync()

        return DropResult(
            thread_id=tid,
            removed_from=removed_from,
            prior_mentions=prior_mentions,
            dry_run=dry_run,
        )

    # ------------------------------------------------------------------
    # 方法 5: prune_settings (废案孤儿与远期龙套设定物理剪枝)
    # ------------------------------------------------------------------

    def prune_settings(
        self,
        target_ids: Sequence[str] | None = None,
        apply: bool = False,
        *,
        current_ch: int | None = None,
        max_appearances: int = 2,
        min_age_chapters: int | None = None,
        force: bool = False,
        sync_index: bool | None = None,
    ) -> PruneReport:
        """扫描零引用废案孤儿与远期一次性龙套卡片，并按授权执行物理删除与索引级联清理。"""
        dry_run = not apply
        cur_ch = (
            self._config.current_chapter
            if current_ch is None
            else _validate_non_bool_int(current_ch, name="current_ch", min_value=0)
        )
        max_app = _validate_non_bool_int(max_appearances, name="max_appearances", min_value=0)
        age_threshold = (
            self._config.lifecycle.cold_after_chapters
            if min_age_chapters is None
            else _validate_non_bool_int(min_age_chapters, name="min_age_chapters", min_value=0)
        )

        validated_targets: list[str] | None = None
        if target_ids is not None:
            validated_targets = []
            seen_t: set[str] = set()
            for raw_tid in target_ids:
                if not isinstance(raw_tid, str) or not raw_tid.strip():
                    raise ValueError(f"target_ids 包含无效实体 ID: {raw_tid!r}")
                valid_eid = _ENTITY_ID_ADAPTER.validate_python(raw_tid.strip())
                if valid_eid not in seen_t:
                    seen_t.add(valid_eid)
                    validated_targets.append(valid_eid)

        settings_map = self._load_all_settings()

        orphan_ids: list[str] = []
        low_frequency_ids: list[str] = []

        with self._use_indexer() as active_indexer:
            if self._should_sync(sync_index):
                active_indexer.sync()

            cached_analyzer: Any | None = None
            if self._impact_analyzer is None:
                try:
                    from .impact import ImpactAnalyzer

                    cached_analyzer = ImpactAnalyzer(
                        self._book_dir,
                        indexer=active_indexer,
                        parser=self._parser,
                        config=self._config,
                        auto_sync=False,
                        enable_corpus_cache=True,
                    )
                except ImportError:
                    cached_analyzer = None

            for eid in sorted(settings_map.keys()):
                if cached_analyzer is not None:
                    report = self._invoke_external_impact_analyzer(
                        cached_analyzer,
                        eid,
                        after_chapter=cur_ch,
                    )
                else:
                    report = self._run_impact_scan(
                        eid,
                        active_indexer=active_indexer,
                        after_chapter=cur_ch,
                    )
                active_hits = [
                    h
                    for h in report.hits
                    if h.scope in (ImpactScope.STATE, ImpactScope.THREAD)
                    or (
                        h.scope == ImpactScope.BLUEPRINT
                        and (h.chapter_num is None or h.chapter_num > cur_ch)
                    )
                ]
                history_hits = [
                    h
                    for h in report.hits
                    if h.scope in (ImpactScope.MANUSCRIPT, ImpactScope.FACT)
                ]

                if not active_hits and not history_hits:
                    orphan_ids.append(eid)
                elif not active_hits and history_hits:
                    appeared_chapters = {
                        h.chapter_num for h in history_hits if h.chapter_num is not None
                    }
                    if (
                        appeared_chapters
                        and all(h.chapter_num is not None for h in history_hits)
                        and 1 <= len(appeared_chapters) <= max_app
                        and (cur_ch - max(appeared_chapters)) >= age_threshold
                    ):
                        low_frequency_ids.append(eid)

        prunable_set = set(orphan_ids) | set(low_frequency_ids)
        deleted_paths: list[str] = []
        skipped_ids: list[str] = []

        if validated_targets is None:
            if apply:
                for eid in [*orphan_ids, *low_frequency_ids]:
                    _, file_path = settings_map[eid]
                    if file_path.is_file():
                        file_path.unlink()
                        deleted_paths.append(self._rel_posix(file_path))
        else:
            for eid in validated_targets:
                entry = settings_map.get(eid)
                is_eligible = (eid in prunable_set) or (force and entry is not None)
                if not is_eligible or entry is None:
                    skipped_ids.append(eid)
                    continue
                if apply:
                    _, file_path = entry
                    if file_path.is_file():
                        file_path.unlink()
                        deleted_paths.append(self._rel_posix(file_path))

        if apply and deleted_paths and self._should_sync(sync_index):
            with self._use_indexer() as active_indexer:
                active_indexer.sync()

        return PruneReport(
            orphan_ids=orphan_ids,
            low_frequency_ids=low_frequency_ids,
            deleted_paths=deleted_paths,
            skipped_ids=skipped_ids,
            dry_run=dry_run,
        )

    # ------------------------------------------------------------------
    # 方法 6: compact_volume_facts (远期完结卷原子事实蒸馏浓缩)
    # ------------------------------------------------------------------

    def compact_volume_facts(
        self,
        vol_id: str,
        milestones: Sequence[ChapterFactRecord | Mapping[str, Any]] | None = None,
        *,
        dry_run: bool = False,
        sync_index: bool | None = None,
    ) -> CompactResult:
        """将已完结分卷的细碎逐章事实浓缩为高密度卷级里程碑事实。"""
        if not isinstance(vol_id, str) or not vol_id.strip():
            raise ValueError(f"vol_id 必须为非空合法 VolumeId 字符串，收到: {vol_id!r}")
        validated_vol_id = _VOLUME_ID_ADAPTER.validate_python(vol_id.strip())

        history_path = resolve_facts_file(self._book_dir, validated_vol_id, for_write=True)
        if not history_path.is_file():
            raise FileNotFoundError(f"未找到分卷历史事实台账: {history_path}")

        records: list[ChapterFactRecord] = []
        for raw_line in history_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            records.append(ChapterFactRecord.model_validate_json(line))

        original_count = len(records)

        if milestones is not None:
            compacted = [
                item if isinstance(item, ChapterFactRecord) else ChapterFactRecord.model_validate(item)
                for item in milestones
            ]
            compacted.sort(key=lambda r: r.ch)
            if len(compacted) > original_count:
                raise ValueError(
                    f"传入里程碑条数 ({len(compacted)}) 不能大于原始记录条数 ({original_count})"
                )
        else:
            max_m = self._config.lifecycle.compact_max_milestones
            sorted_records = sorted(records, key=lambda r: r.ch)
            if original_count <= max_m:
                compacted = sorted_records
            else:
                compacted = self._select_milestone_records(sorted_records, target_count=max_m)

        if not dry_run:
            lines = [
                rec.model_dump_json(by_alias=True, exclude_none=True)
                for rec in compacted
            ]
            text = ("\n".join(lines) + "\n") if lines else ""
            history_path.write_text(text, encoding="utf-8")
            if self._should_sync(sync_index):
                with self._use_indexer() as active_indexer:
                    active_indexer.sync()

        return CompactResult(
            volume_id=validated_vol_id,
            history_path=self._rel_posix(history_path),
            original_count=original_count,
            compacted_count=len(compacted),
            dry_run=dry_run,
        )

    def _select_milestone_records(
        self,
        records: list[ChapterFactRecord],
        *,
        target_count: int,
    ) -> list[ChapterFactRecord]:
        """按首尾章、单元边界、伏笔与多实体密度均匀选取确定性里程碑事实。"""
        n = len(records)
        if n <= target_count:
            return list(records)
        if target_count <= 0:
            return []
        if target_count == 1:
            return [records[0]]

        thread_entities: set[str] = set()
        thread_chapters: set[int] = set()
        for t_path in (
            resolve_threads_file(self._book_dir),
            resolve_archive_threads_file(self._book_dir),
        ):
            t_file = self._load_threads_file(t_path)
            for thr in t_file.threads:
                thread_entities.update(thr.trigger_entities)
                thread_chapters.add(thr.planted_at)
                thread_chapters.add(thr.last_progressed_at)

        scores: list[float] = []
        for idx, rec in enumerate(records):
            score = len(rec.entities) * 1.0 + len(rec.facts) * 0.5
            score += len(set(rec.entities) & thread_entities) * 2.0
            if rec.ch in thread_chapters:
                score += 10.0
            if idx == 0 or idx == n - 1:
                score += 100.0
            elif rec.arc_id and (
                rec.arc_id != records[idx - 1].arc_id or rec.arc_id != records[idx + 1].arc_id
            ):
                score += 5.0
            scores.append(score)

        # 首先锁定首尾章与单元起止边界（不超 target_count 时全部纳入）
        selected: set[int] = {0, n - 1}
        arc_boundaries: list[int] = []
        for idx in range(1, n - 1):
            rec = records[idx]
            if rec.arc_id and (
                rec.arc_id != records[idx - 1].arc_id or rec.arc_id != records[idx + 1].arc_id
            ):
                arc_boundaries.append(idx)

        if len(selected) + len(arc_boundaries) <= target_count:
            selected.update(arc_boundaries)
        else:
            need = target_count - len(selected)
            step = len(arc_boundaries) / need
            for k in range(need):
                selected.add(arc_boundaries[int(k * step)])

        # 剩余名额按均匀分段桶采样，每桶内择优取分值最高者
        remaining_slots = target_count - len(selected)
        if remaining_slots > 0:
            for b in range(remaining_slots):
                b_start = int(b * n / remaining_slots)
                b_end = max(b_start + 1, int((b + 1) * n / remaining_slots))
                candidates = [i for i in range(b_start, min(b_end, n)) if i not in selected]
                if candidates:
                    best = max(candidates, key=lambda i: (scores[i], -i))
                    selected.add(best)

        # 若分桶因整段已被选满导致仍不足，按全局最高分补齐
        if len(selected) < target_count:
            rest = [i for i in range(n) if i not in selected]
            rest.sort(key=lambda i: (-scores[i], i))
            for i in rest[: target_count - len(selected)]:
                selected.add(i)

        return [records[i] for i in sorted(selected)]

    # ------------------------------------------------------------------
    # 方法 7: audit_chapter (单章 / 全书静态一致性体检)
    # ------------------------------------------------------------------

    def audit_chapter(
        self,
        chapter_num: int | None = None,
        *,
        sync_index: bool | None = None,
    ) -> AuditReport:
        """执行单章或全书静态一致性体检，任何 ERROR 均会令 passed=False。"""
        if chapter_num is not None:
            _validate_non_bool_int(chapter_num, name="chapter_num", min_value=1)

        if self._should_sync(sync_index):
            with self._use_indexer() as active_indexer:
                active_indexer.sync()

        issues: list[AuditIssue] = []

        settings_map = self._load_all_settings()
        settings_by_id = {eid: doc for eid, (doc, _) in settings_map.items()}

        # 校验并加载状态文件
        active_threads_by_id, archived_threads_by_id = self._audit_state_and_threads(issues)
        archived_state_ids = self._collect_archived_state_ids()

        deceased_ids: set[str] = set()
        archived_ids: set[str] = set(archived_state_ids)
        for eid, s_doc in settings_by_id.items():
            if s_doc.status == EntityStatus.DECEASED:
                deceased_ids.add(eid)
                archived_ids.add(eid)
            elif s_doc.status == EntityStatus.RETIRED:
                archived_ids.add(eid)

        # 校验事实 JSONL
        self._audit_history_facts(chapter_num, issues)

        # 构建涵盖全部设定实体的自动机
        matcher = AliasMatcher(
            list(settings_by_id.values()),
            archived_ids=archived_ids,
            archive_non_active=True,
        )

        # 加载文风红线禁用词
        style_guide = self._load_style_guide(issues)
        banned_words = style_guide.banned_words if style_guide is not None else []

        # 收集所有蓝图并建立按章映射
        all_blueprints = self._load_blueprints_with_paths()
        blueprints_by_ch: dict[int, tuple[BlueprintDoc, Path]] = {
            bp.chapter: (bp, path) for bp, path in all_blueprints
        }

        target_blueprints = (
            [(bp, path) for bp, path in all_blueprints if bp.chapter == chapter_num]
            if chapter_num is not None
            else all_blueprints
        )

        warned_awakened: set[tuple[int, str]] = set()

        # 1) 蓝图引用完整性 & 死人误入蓝图检查
        for bp, bp_path in target_blueprints:
            bp_rel = self._rel_posix(bp_path)
            for eid in bp.all_entity_ids():
                if eid not in settings_by_id:
                    issues.append(
                        AuditIssue(
                            level=IssueLevel.ERROR,
                            code="MISSING_SETTING",
                            message=(
                                f"第 {bp.chapter} 章蓝图引用的实体 '{eid}' "
                                "在 01_settings/ 中不存在设定卡"
                            ),
                            location=bp_rel,
                        )
                    )

            for t_ref in bp.topics:
                s_doc = settings_by_id.get(t_ref.entity_id)
                if s_doc is not None and t_ref.topic not in s_doc.topics:
                    issues.append(
                        AuditIssue(
                            level=IssueLevel.WARNING,
                            code="MISSING_TOPIC",
                            message=(
                                f"第 {bp.chapter} 章蓝图请求的专题 "
                                f"'{t_ref.entity_id}:{t_ref.topic}' 在设定文档中不存在"
                            ),
                            location=bp_rel,
                        )
                    )

            for thr_ref in bp.threads:
                if thr_ref.action not in (ThreadAction.PROGRESS, ThreadAction.RESOLVE):
                    continue
                tid = thr_ref.thread_id
                if tid in archived_threads_by_id:
                    issues.append(
                        AuditIssue(
                            level=IssueLevel.ERROR,
                            code="ARCHIVED_THREAD_REF",
                            message=(
                                f"第 {bp.chapter} 章蓝图对冷库已归档伏笔 '{tid}' "
                                f"执行 '{thr_ref.action.value}'"
                            ),
                            location=bp_rel,
                        )
                    )
                elif tid not in active_threads_by_id:
                    issues.append(
                        AuditIssue(
                            level=IssueLevel.ERROR,
                            code="MISSING_THREAD",
                            message=(
                                f"第 {bp.chapter} 章蓝图引用的伏笔 '{tid}' "
                                "不存在于 03_state/threads.yaml"
                            ),
                            location=bp_rel,
                        )
                    )
                elif active_threads_by_id[tid].status == ThreadStatus.RESOLVED:
                    issues.append(
                        AuditIssue(
                            level=IssueLevel.ERROR,
                            code="RESOLVED_THREAD_REF",
                            message=(
                                f"第 {bp.chapter} 章蓝图对已完结(resolved)伏笔 '{tid}' "
                                f"执行 '{thr_ref.action.value}'"
                            ),
                            location=bp_rel,
                        )
                    )

            bp_text = self._blueprint_text(bp)
            bp_has_flashback = any(marker in bp_text for marker in _FLASHBACK_MARKERS)

            bp_char_ids: list[str] = []
            if bp.pov is not None:
                bp_char_ids.append(bp.pov)
            bp_char_ids.extend(bp.cast.characters)
            for cid in dict.fromkeys(bp_char_ids):
                if cid in deceased_ids and not bp_has_flashback:
                    issues.append(
                        AuditIssue(
                            level=IssueLevel.ERROR,
                            code="DECEASED_ENTITY_IN_BLUEPRINT",
                            message=(
                                f"第 {bp.chapter} 章蓝图将已故角色 '{cid}' 列入 POV/出场阵容，"
                                "且未标注回忆/生前/幻境/遗体/旧事等闪回标记"
                            ),
                            location=bp_rel,
                        )
                    )

            for eid in bp.all_entity_ids():
                if eid in archived_ids and eid not in deceased_ids:
                    warned_awakened.add((bp.chapter, eid))
                    issues.append(
                        AuditIssue(
                            level=IssueLevel.WARNING,
                            code="ARCHIVED_ENTITY_AWAKENED",
                            message=(
                                f"第 {bp.chapter} 章蓝图显式点名唤醒冷库实体 '{eid}'，"
                                "若需长期回归请恢复其热状态截面"
                            ),
                            location=bp_rel,
                        )
                    )

        # 2) 草稿与定稿正文检查（死人/冷库未唤醒误出场 + 禁用词红线）
        prose_files = self._collect_prose_files(chapter_num)
        for prose_ch, prose_path in prose_files:
            prose_rel = self._rel_posix(prose_path)
            raw_text = prose_path.read_text(encoding="utf-8")
            _, body = split_frontmatter(raw_text)

            # 禁用词红线
            for word in banned_words:
                if word and word in body:
                    issues.append(
                        AuditIssue(
                            level=IssueLevel.ERROR,
                            code="BANNED_WORD_HIT",
                            message=f"第 {prose_ch} 章正文命中禁用词 '{word}'",
                            location=prose_rel,
                        )
                    )

            bp_pair = blueprints_by_ch.get(prose_ch) if prose_ch is not None else None
            bp_obj = bp_pair[0] if bp_pair is not None else None
            bp_explicit = set(bp_obj.all_entity_ids()) if bp_obj is not None else set()
            bp_text = self._blueprint_text(bp_obj) if bp_obj is not None else ""
            has_flashback = any(
                marker in body or marker in bp_text for marker in _FLASHBACK_MARKERS
            )

            hits = matcher.scan_text(body, include_archived=True)
            seen_eids: set[str] = set()
            for hit in hits:
                eid = hit.entity_id
                if eid in seen_eids:
                    continue
                seen_eids.add(eid)

                if eid in deceased_ids:
                    if not has_flashback:
                        issues.append(
                            AuditIssue(
                                level=IssueLevel.ERROR,
                                code="DECEASED_ENTITY_APPEARANCE",
                                message=(
                                    f"第 {prose_ch} 章正文中已故角色 '{eid}'（命中 '{hit.alias}'）"
                                    "在无回忆/生前/幻境/遗体/旧事标记的情况下出场"
                                ),
                                location=prose_rel,
                            )
                        )
                    continue

                if eid in archived_ids:
                    if eid not in bp_explicit:
                        issues.append(
                            AuditIssue(
                                level=IssueLevel.ERROR,
                                code="ARCHIVED_ENTITY_UNAWAKENED",
                                message=(
                                    f"第 {prose_ch} 章正文命中冷库归档实体 '{eid}'（'{hit.alias}'），"
                                    "但未被本章蓝图显式点名唤醒"
                                ),
                                location=prose_rel,
                            )
                        )
                    elif prose_ch is not None and (prose_ch, eid) not in warned_awakened:
                        warned_awakened.add((prose_ch, eid))
                        issues.append(
                            AuditIssue(
                                level=IssueLevel.WARNING,
                                code="ARCHIVED_ENTITY_AWAKENED",
                                message=(
                                    f"第 {prose_ch} 章正文出现已被蓝图唤醒的冷库实体 '{eid}'，"
                                    "可考虑恢复其热状态"
                                ),
                                location=prose_rel,
                            )
                        )

        # 3) 模型层设定卡 (01_设定集/) 具象污染检查（防台词/具体场景/预设动作直出到模型）
        if chapter_num is None:
            lint_targets = [
                (doc, s_path)
                for eid, (doc, s_path) in sorted(settings_map.items())
                if doc.status == EntityStatus.ACTIVE and eid not in archived_ids
            ]
        else:
            seen_target_ids: set[str] = set()
            lint_targets = []
            for bp, _ in target_blueprints:
                for eid in bp.all_entity_ids():
                    if eid in seen_target_ids or eid not in settings_map:
                        continue
                    seen_target_ids.add(eid)
                    lint_targets.append(settings_map[eid])

        for doc, s_path in lint_targets:
            s_rel = self._rel_posix(s_path)
            for leak_warn in lint_model_setting_doc(doc):
                issues.append(
                    AuditIssue(
                        level=IssueLevel.WARNING,
                        code="SETTING_CONCRETE_LEAK",
                        message=f"设定卡 '{doc.id}' ({doc.name}) 混入作者层具象内容: {leak_warn}",
                        location=s_rel,
                    )
                )

        return AuditReport(chapter=chapter_num, issues=issues)

    # ------------------------------------------------------------------
    # 状态与文风辅助校验 (audit 专用)
    # ------------------------------------------------------------------

    def _audit_state_and_threads(
        self,
        issues: list[AuditIssue],
    ) -> tuple[dict[str, Thread], dict[str, Thread]]:
        genre = self._config.genre_profile
        ctx = {"genre_profile": genre}

        # 1. active/characters.yaml
        chars_path = resolve_active_characters_file(self._book_dir)
        if chars_path.is_file():
            chars_rel = self._rel_posix(chars_path)
            try:
                raw_chars = yaml.safe_load(chars_path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                issues.append(
                    AuditIssue(
                        level=IssueLevel.ERROR,
                        code="YAML_PARSE_ERROR",
                        message=f"解析角色状态文件失败: {exc}",
                        location=chars_rel,
                    )
                )
                raw_chars = None

            if raw_chars is not None and not isinstance(raw_chars, dict):
                issues.append(
                    AuditIssue(
                        level=IssueLevel.ERROR,
                        code="INVALID_CHARACTER_STATE",
                        message="active/characters.yaml 顶层必须为实体映射字典",
                        location=chars_rel,
                    )
                )
            elif isinstance(raw_chars, dict):
                for raw_eid, raw_state in raw_chars.items():
                    try:
                        eid = _ENTITY_ID_ADAPTER.validate_python(raw_eid)
                        c_state = CharacterState.model_validate(raw_state, context=ctx)
                    except ValidationError as exc:
                        issues.append(
                            AuditIssue(
                                level=IssueLevel.ERROR,
                                code="INVALID_CHARACTER_STATE",
                                message=f"角色 '{raw_eid}' 状态或扩展槽校验失败: {exc}",
                                location=chars_rel,
                            )
                        )
                        continue

                    self._warn_model_extra(
                        c_state, label=f"角色 '{eid}' 状态", location=chars_rel, issues=issues
                    )
                    self._warn_model_extra(
                        c_state.epistemic_bounds,
                        label=f"角色 '{eid}' epistemic_bounds",
                        location=chars_rel,
                        issues=issues,
                    )
                    self._warn_profile_ext_extra(
                        c_state.profile_ext,
                        label=f"角色 '{eid}' profile_ext",
                        location=chars_rel,
                        issues=issues,
                    )

        # 2. active/factions.yaml
        facs_path = resolve_active_factions_file(self._book_dir)
        if facs_path.is_file():
            facs_rel = self._rel_posix(facs_path)
            try:
                raw_facs = yaml.safe_load(facs_path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                issues.append(
                    AuditIssue(
                        level=IssueLevel.ERROR,
                        code="YAML_PARSE_ERROR",
                        message=f"解析势力状态文件失败: {exc}",
                        location=facs_rel,
                    )
                )
                raw_facs = None

            if raw_facs is not None and not isinstance(raw_facs, dict):
                issues.append(
                    AuditIssue(
                        level=IssueLevel.ERROR,
                        code="INVALID_FACTION_STATE",
                        message="active/factions.yaml 顶层必须为实体映射字典",
                        location=facs_rel,
                    )
                )
            elif isinstance(raw_facs, dict):
                for raw_eid, raw_state in raw_facs.items():
                    try:
                        eid = _ENTITY_ID_ADAPTER.validate_python(raw_eid)
                        f_state = FactionState.model_validate(raw_state, context=ctx)
                    except ValidationError as exc:
                        issues.append(
                            AuditIssue(
                                level=IssueLevel.ERROR,
                                code="INVALID_FACTION_STATE",
                                message=f"势力 '{raw_eid}' 状态或扩展槽校验失败: {exc}",
                                location=facs_rel,
                            )
                        )
                        continue

                    self._warn_model_extra(
                        f_state, label=f"势力 '{eid}' 状态", location=facs_rel, issues=issues
                    )
                    self._warn_profile_ext_extra(
                        f_state.profile_ext,
                        label=f"势力 '{eid}' profile_ext",
                        location=facs_rel,
                        issues=issues,
                    )

        # 3. world_state.yaml
        ws_path = resolve_world_state_file(self._book_dir)
        if ws_path.is_file():
            ws_rel = self._rel_posix(ws_path)
            try:
                raw_ws = yaml.safe_load(ws_path.read_text(encoding="utf-8"))
                if raw_ws is not None:
                    ws = WorldState.model_validate(raw_ws)
                    self._warn_model_extra(ws, label="world_state", location=ws_rel, issues=issues)
            except (yaml.YAMLError, ValidationError) as exc:
                issues.append(
                    AuditIssue(
                        level=IssueLevel.ERROR,
                        code="INVALID_WORLD_STATE",
                        message=f"world_state.yaml 校验失败: {exc}",
                        location=ws_rel,
                    )
                )

        # 4. relations.yaml
        rel_path = resolve_relations_file(self._book_dir)
        if rel_path.is_file():
            rel_posix = self._rel_posix(rel_path)
            try:
                raw_rel = yaml.safe_load(rel_path.read_text(encoding="utf-8"))
                if raw_rel is not None:
                    rel_file = RelationsFile.model_validate(raw_rel)
                    self._warn_model_extra(
                        rel_file, label="relations.yaml", location=rel_posix, issues=issues
                    )
                    for idx, rel in enumerate(rel_file.relations):
                        self._warn_model_extra(
                            rel,
                            label=f"relations[{idx}] ({rel.source}->{rel.target})",
                            location=rel_posix,
                            issues=issues,
                        )
            except (yaml.YAMLError, ValidationError) as exc:
                issues.append(
                    AuditIssue(
                        level=IssueLevel.ERROR,
                        code="INVALID_RELATIONS_FILE",
                        message=f"relations.yaml 校验失败: {exc}",
                        location=rel_posix,
                    )
                )

        # 5. threads.yaml & archive/threads.yaml
        active_threads: dict[str, Thread] = {}
        archived_threads: dict[str, Thread] = {}

        thr_path = resolve_threads_file(self._book_dir)
        if thr_path.is_file():
            thr_rel = self._rel_posix(thr_path)
            try:
                raw_thr = yaml.safe_load(thr_path.read_text(encoding="utf-8"))
                if raw_thr is not None:
                    thr_file = ThreadsFile.model_validate(raw_thr)
                    self._warn_model_extra(thr_file, label="threads.yaml", location=thr_rel, issues=issues)
                    for t in thr_file.threads:
                        active_threads[t.id] = t
                        self._warn_model_extra(t, label=f"伏笔 '{t.id}'", location=thr_rel, issues=issues)
            except (yaml.YAMLError, ValidationError) as exc:
                issues.append(
                    AuditIssue(
                        level=IssueLevel.ERROR,
                        code="INVALID_THREADS_FILE",
                        message=f"threads.yaml 校验失败: {exc}",
                        location=thr_rel,
                    )
                )

        arch_thr_path = resolve_archive_threads_file(self._book_dir)
        if arch_thr_path.is_file():
            arch_rel = self._rel_posix(arch_thr_path)
            try:
                raw_arch = yaml.safe_load(arch_thr_path.read_text(encoding="utf-8"))
                if raw_arch is not None:
                    arch_file = ThreadsFile.model_validate(raw_arch)
                    for t in arch_file.threads:
                        archived_threads[t.id] = t
            except (yaml.YAMLError, ValidationError) as exc:
                issues.append(
                    AuditIssue(
                        level=IssueLevel.ERROR,
                        code="INVALID_THREADS_FILE",
                        message=f"archive/threads.yaml 校验失败: {exc}",
                        location=arch_rel,
                    )
                )

        return active_threads, archived_threads

    def _audit_history_facts(self, chapter_num: int | None, issues: list[AuditIssue]) -> None:
        for path in list_facts_files(self._book_dir):
            if not path.is_file():
                continue
            rel = self._rel_posix(path)
            for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = ChapterFactRecord.model_validate_json(line)
                except ValidationError as exc:
                    issues.append(
                        AuditIssue(
                            level=IssueLevel.ERROR,
                            code="INVALID_FACT_RECORD",
                            message=f"{rel}:{line_no} 原子事实记录校验失败: {exc}",
                            location=rel,
                        )
                    )
                    continue

                if chapter_num is not None and rec.ch != chapter_num:
                    continue

                self._warn_model_extra(
                    rec,
                    label=f"第 {rec.ch} 章事实记录",
                    location=rel,
                    issues=issues,
                )
                if not (3 <= len(rec.facts) <= 5):
                    issues.append(
                        AuditIssue(
                            level=IssueLevel.WARNING,
                            code="FACT_COUNT_OUT_OF_RANGE",
                            message=(
                                f"第 {rec.ch} 章原子事实条数为 {len(rec.facts)} 条，"
                                "推荐保持在 3~5 条区间内"
                            ),
                            location=rel,
                        )
                    )

    @staticmethod
    def _warn_model_extra(
        model: Any,
        *,
        label: str,
        location: str,
        issues: list[AuditIssue],
    ) -> None:
        extra = getattr(model, "model_extra", None)
        if extra:
            keys = ", ".join(sorted(str(k) for k in extra.keys()))
            issues.append(
                AuditIssue(
                    level=IssueLevel.WARNING,
                    code="UNKNOWN_STATE_FIELD",
                    message=f"{label} 存在未定义扩展字段 ({keys})，请核对是否拼写错误",
                    location=location,
                )
            )

    def _warn_profile_ext_extra(
        self,
        profile_ext: dict[str, Any],
        *,
        label: str,
        location: str,
        issues: list[AuditIssue],
    ) -> None:
        if not profile_ext or self._config.genre_profile == "general":
            return
        try:
            genre_cls = get_genre(self._config.genre_profile)
            inst = genre_cls.model_validate(profile_ext)
        except (KeyError, ValidationError):
            return
        self._warn_model_extra(inst, label=label, location=location, issues=issues)

    def _load_style_guide(self, issues: list[AuditIssue]) -> StyleGuide | None:
        sg_path = resolve_style_guide_file(self._book_dir)
        if not sg_path.is_file():
            return None
        sg_rel = self._rel_posix(sg_path)
        raw_text = sg_path.read_text(encoding="utf-8")
        fm_dict, body = split_frontmatter(raw_text)

        raw_pov = fm_dict.get("pov_rules") or []
        if isinstance(raw_pov, str):
            pov_rules = [raw_pov]
        elif isinstance(raw_pov, list):
            pov_rules = [str(x) for x in raw_pov if str(x).strip()]
        else:
            pov_rules = []

        raw_banned = fm_dict.get("banned_words") or []
        if isinstance(raw_banned, str):
            banned_words = [raw_banned]
        elif isinstance(raw_banned, list):
            banned_words = [str(x) for x in raw_banned if str(x).strip()]
        else:
            banned_words = []

        reserved = {"tone", "pov_rules", "banned_words", "raw_markdown"}
        extra_fields = {k: v for k, v in fm_dict.items() if k not in reserved}

        sg = StyleGuide(
            tone=str(fm_dict.get("tone") or "").strip(),
            pov_rules=pov_rules,
            banned_words=banned_words,
            raw_markdown=body.strip(),
            **extra_fields,
        )
        self._warn_model_extra(sg, label="style_guide.md", location=sg_rel, issues=issues)
        return sg

    # ------------------------------------------------------------------
    # 与 T10 ImpactAnalyzer 解耦对接 & 内置五维影响面扫描回退实现
    # ------------------------------------------------------------------

    def _run_impact_scan(
        self,
        target_id_or_keyword: str,
        *,
        active_indexer: SqliteIndexer,
        after_chapter: int | None = None,
        extra_terms: Sequence[str] = (),
    ) -> ImpactReport:
        """优先调用注入或动态导入的 ImpactAnalyzer；尚未合入时自动回退至内置实现。"""
        if self._impact_analyzer is not None:
            return self._invoke_external_impact_analyzer(
                self._impact_analyzer,
                target_id_or_keyword,
                after_chapter=after_chapter,
            )

        try:
            from .impact import ImpactAnalyzer

            analyzer = ImpactAnalyzer(
                self._book_dir,
                indexer=active_indexer,
                parser=self._parser,
                config=self._config,
                auto_sync=False,
            )
            return self._invoke_external_impact_analyzer(
                analyzer,
                target_id_or_keyword,
                after_chapter=after_chapter,
            )
        except ImportError:
            return self._builtin_scan_impact(
                target_id_or_keyword,
                active_indexer=active_indexer,
                after_chapter=after_chapter,
                extra_terms=extra_terms,
            )

    @staticmethod
    def _invoke_external_impact_analyzer(
        analyzer: Any,
        target_id_or_keyword: str,
        *,
        after_chapter: int | None = None,
    ) -> ImpactReport:
        scan_fn = analyzer.scan_impact
        kwargs: dict[str, Any] = {}
        try:
            sig = inspect.signature(scan_fn)
            params = sig.parameters
            accepts_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            if "sync_index" in params or accepts_varkw:
                kwargs["sync_index"] = False
            if after_chapter is not None and ("after_chapter" in params or accepts_varkw):
                kwargs["after_chapter"] = after_chapter
        except (TypeError, ValueError):
            kwargs = {"sync_index": False}

        return scan_fn(target_id_or_keyword, **kwargs)

    def _builtin_scan_impact(
        self,
        target_id_or_keyword: str,
        *,
        active_indexer: SqliteIndexer,
        after_chapter: int | None = None,
        extra_terms: Sequence[str] = (),
    ) -> ImpactReport:
        """内置五维影响面扫描回退实现（与 T10 ImpactReport / ImpactHit 完全同构）。"""
        target = str(target_id_or_keyword).strip()
        if isinstance(target_id_or_keyword, bool) or not target:
            raise ValueError(f"target_id_or_keyword 不能为空: {target_id_or_keyword!r}")

        conn = active_indexer.conn
        ent_row = conn.execute("SELECT id, name FROM entities WHERE id = ?", (target,)).fetchone()
        if ent_row is None:
            alias_row = conn.execute(
                """
                SELECT e.id, e.name
                FROM entity_aliases AS a
                JOIN entities AS e ON e.id = a.entity_id
                WHERE a.alias = ?
                ORDER BY a.is_archived ASC, e.id ASC
                LIMIT 1
                """,
                (target,),
            ).fetchone()
            ent_row = alias_row

        resolved_eid: str | None = str(ent_row[0]) if ent_row is not None else None
        expanded_terms: list[str]
        matcher: AliasMatcher

        if resolved_eid is not None:
            ent_name = str(ent_row[1])
            alias_rows = conn.execute(
                "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias ASC",
                (resolved_eid,),
            ).fetchall()
            aliases = [str(r[0]) for r in alias_rows]
            expanded_terms = _dedupe_strs([ent_name, *aliases, *extra_terms])
            if not expanded_terms:
                expanded_terms = [resolved_eid]

            all_map: dict[str, list[str]] = {}
            for e_id, e_name in conn.execute("SELECT id, name FROM entities").fetchall():
                all_map[str(e_id)] = _dedupe_strs([str(e_id), str(e_name)])
            for a_text, e_id in conn.execute("SELECT alias, entity_id FROM entity_aliases").fetchall():
                all_map.setdefault(str(e_id), []).append(str(a_text))
            all_map[resolved_eid] = _dedupe_strs(
                [resolved_eid, *expanded_terms, *all_map.get(resolved_eid, [])]
            )
            matcher = AliasMatcher(all_map, archive_non_active=False)
            match_key = resolved_eid
        else:
            thr_title: str | None = None
            thr_row = conn.execute("SELECT title FROM threads WHERE id = ?", (target,)).fetchone()
            if thr_row is not None and thr_row[0]:
                thr_title = str(thr_row[0])
            else:
                for t_path in (
                    self._book_dir / "03_state" / "threads.yaml",
                    self._book_dir / "03_state" / "archive" / "threads.yaml",
                ):
                    for t in self._load_threads_file(t_path).threads:
                        if t.id == target:
                            thr_title = t.title
                            break
                    if thr_title:
                        break
            expanded_terms = _dedupe_strs(
                [target, *( [thr_title] if thr_title else [] ), *extra_terms]
            )
            match_key = "other_impact_query"
            matcher = AliasMatcher({match_key: expanded_terms}, archive_non_active=False)

        def _find_text_hits(text: str) -> list[tuple[str, int, int]]:
            if not text:
                return []
            return [
                (m.alias, m.start, m.end)
                for m in matcher.scan_text(text, include_archived=True)
                if m.entity_id == match_key
            ]

        hits: list[ImpactHit] = []

        # 1) MANUSCRIPT
        ms_root = resolve_manuscript_dir(self._book_dir)
        if ms_root.is_dir():
            for path in sorted(ms_root.rglob("*.md")):
                if not path.is_file():
                    continue
                content = path.read_text(encoding="utf-8")
                matches = _find_text_hits(content)
                if not matches:
                    continue
                ch_num = _extract_chapter_from_path(path, content)
                term, s, e = matches[0]
                excerpt = _make_excerpt(content, s, e)
                if len(matches) > 1:
                    excerpt = f"{excerpt}（共 {len(matches)} 处命中）"
                hits.append(
                    ImpactHit(
                        scope=ImpactScope.MANUSCRIPT,
                        location=self._rel_posix(path),
                        chapter_num=ch_num,
                        matched_term=term,
                        excerpt=excerpt,
                    )
                )

        # 2) FACT
        history_dir = self._book_dir / "03_state" / "history"
        if history_dir.is_dir():
            for path in sorted(history_dir.glob("*_facts.jsonl")):
                if not path.is_file():
                    continue
                rel = self._rel_posix(path)
                for raw_line in path.read_text(encoding="utf-8").splitlines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        rec = ChapterFactRecord.model_validate_json(line)
                    except ValidationError:
                        continue
                    id_matched = (
                        (resolved_eid is not None and resolved_eid in rec.entities)
                        or target in rec.entities
                    )
                    text_blob = "\n".join([rec.summary, *rec.facts])
                    text_matches = _find_text_hits(text_blob)
                    if id_matched or text_matches:
                        matched_term = (
                            text_matches[0][0]
                            if text_matches
                            else (resolved_eid or target)
                        )
                        excerpt = (
                            _make_excerpt(text_blob, text_matches[0][1], text_matches[0][2])
                            if text_matches
                            else rec.summary
                        )
                        hits.append(
                            ImpactHit(
                                scope=ImpactScope.FACT,
                                location=rel,
                                chapter_num=rec.ch,
                                matched_term=matched_term,
                                excerpt=excerpt,
                            )
                        )

        # 3) STATE (默认仅扫描活跃热状态)
        chars_path = resolve_active_characters_file(self._book_dir)
        if chars_path.is_file():
            chars_rel = self._rel_posix(chars_path)
            chars_file = self._load_character_state_file(chars_path)
            for cid, c_state in chars_file.root.items():
                id_hit = resolved_eid is not None and (
                    cid == resolved_eid or c_state.location == resolved_eid
                )
                state_text = json.dumps(
                    c_state.model_dump(mode="json", exclude_none=True),
                    ensure_ascii=False,
                )
                text_matches = _find_text_hits(state_text)
                if id_hit or text_matches:
                    hits.append(
                        ImpactHit(
                            scope=ImpactScope.STATE,
                            location=chars_rel,
                            chapter_num=c_state.updated_at_ch,
                            matched_term=text_matches[0][0] if text_matches else resolved_eid,
                            excerpt=f"{cid}: {c_state.physical_mental or c_state.location or cid}",
                        )
                    )

        facs_path = resolve_active_factions_file(self._book_dir)
        if facs_path.is_file():
            facs_rel = self._rel_posix(facs_path)
            facs_file = self._load_faction_state_file(facs_path)
            for fid, f_state in facs_file.root.items():
                id_hit = resolved_eid is not None and (
                    fid == resolved_eid
                    or f_state.leader == resolved_eid
                    or resolved_eid in f_state.territory
                )
                state_text = json.dumps(
                    f_state.model_dump(mode="json", exclude_none=True),
                    ensure_ascii=False,
                )
                text_matches = _find_text_hits(state_text)
                if id_hit or text_matches:
                    hits.append(
                        ImpactHit(
                            scope=ImpactScope.STATE,
                            location=facs_rel,
                            chapter_num=f_state.updated_at_ch,
                            matched_term=text_matches[0][0] if text_matches else resolved_eid,
                            excerpt=f"{fid}: {'; '.join(f_state.current_goals) or fid}",
                        )
                    )

        ws_path = resolve_world_state_file(self._book_dir)
        if ws_path.is_file():
            raw_ws = yaml.safe_load(ws_path.read_text(encoding="utf-8"))
            if isinstance(raw_ws, dict):
                ws = WorldState.model_validate(raw_ws)
                ws_text = f"{ws.in_world_time}\n{ws.macro_situation}"
                text_matches = _find_text_hits(ws_text)
                if text_matches:
                    hits.append(
                        ImpactHit(
                            scope=ImpactScope.STATE,
                            location=self._rel_posix(ws_path),
                            chapter_num=ws.current_chapter if ws.current_chapter >= 1 else None,
                            matched_term=text_matches[0][0],
                            excerpt=_make_excerpt(ws_text, text_matches[0][1], text_matches[0][2]),
                        )
                    )

        rel_path = resolve_relations_file(self._book_dir)
        if rel_path.is_file():
            rel_posix = self._rel_posix(rel_path)
            rel_file = self._load_relations_file(rel_path)
            for rel in rel_file.relations:
                if after_chapter is not None and after_chapter >= 1:
                    is_active = rel.is_valid_at(after_chapter)
                else:
                    is_active = rel.invalid_at_ch is None
                if not is_active:
                    continue
                id_hit = resolved_eid is not None and resolved_eid in (rel.source, rel.target)
                text_matches = _find_text_hits(rel.relation)
                if id_hit or text_matches:
                    hits.append(
                        ImpactHit(
                            scope=ImpactScope.STATE,
                            location=rel_posix,
                            chapter_num=rel.valid_from_ch,
                            matched_term=text_matches[0][0] if text_matches else resolved_eid,
                            excerpt=f"{rel.source} -> {rel.target}: {rel.relation}",
                        )
                    )

        # 4) THREAD (未完结伏笔)
        thr_path = resolve_threads_file(self._book_dir)
        if thr_path.is_file():
            thr_posix = self._rel_posix(thr_path)
            thr_file = self._load_threads_file(thr_path)
            for thr in thr_file.threads:
                if thr.status == ThreadStatus.RESOLVED:
                    continue
                id_hit = (
                    thr.id == target
                    or (resolved_eid is not None and resolved_eid in thr.trigger_entities)
                    or target in thr.trigger_entities
                )
                thr_text = "\n".join([thr.title, thr.public_clue, thr.truth_secret or ""])
                text_matches = _find_text_hits(thr_text)
                if id_hit or text_matches:
                    hits.append(
                        ImpactHit(
                            scope=ImpactScope.THREAD,
                            location=thr_posix,
                            chapter_num=thr.last_progressed_at,
                            matched_term=text_matches[0][0] if text_matches else (resolved_eid or target),
                            excerpt=f"{thr.id}: {thr.title}",
                        )
                    )

        # 5) BLUEPRINT (后续未定稿蓝图)
        cutoff = after_chapter if after_chapter is not None else self._config.current_chapter
        for bp, bp_path in self._load_blueprints_with_paths():
            if bp.chapter <= cutoff:
                continue
            bp_entities = set(bp.all_entity_ids())
            id_hit = (
                (resolved_eid is not None and resolved_eid in bp_entities)
                or target in bp_entities
                or any(ref.thread_id == target for ref in bp.threads)
            )
            bp_text = self._blueprint_text(bp)
            text_matches = _find_text_hits(bp_text)
            if id_hit or text_matches:
                hits.append(
                    ImpactHit(
                        scope=ImpactScope.BLUEPRINT,
                        location=self._rel_posix(bp_path),
                        chapter_num=bp.chapter,
                        matched_term=text_matches[0][0] if text_matches else (resolved_eid or target),
                        excerpt=(
                            _make_excerpt(bp_text, text_matches[0][1], text_matches[0][2])
                            if text_matches
                            else f"第 {bp.chapter} 章蓝图 {bp.title or ''}".strip()
                        ),
                    )
                )

        return ImpactReport(
            target=target,
            entity_id=resolved_eid,
            expanded_terms=expanded_terms,
            hits=hits,
            is_safe_to_change=(len(hits) == 0),
        )

    # ------------------------------------------------------------------
    # 真源文件读取辅助
    # ------------------------------------------------------------------

    def _load_all_settings(self) -> dict[str, tuple[SettingDoc, Path]]:
        results: dict[str, tuple[SettingDoc, Path]] = {}
        settings_dir = resolve_settings_root(self._book_dir)
        if not settings_dir.is_dir():
            return results
        for path in sorted(settings_dir.rglob("*.md")):
            if not path.is_file():
                continue
            doc = self._parser.parse_setting_md(path, book_dir=self._book_dir)
            results[doc.id] = (doc, path)
        return results

    def _load_blueprints_with_paths(self) -> list[tuple[BlueprintDoc, Path]]:
        bp_root = resolve_blueprints_dir(self._book_dir)
        if not bp_root.is_dir():
            return []
        results: list[tuple[BlueprintDoc, Path]] = []
        for path in sorted(bp_root.rglob("*.md")):
            if not path.is_file():
                continue
            bp = self._parser.parse_blueprint_md(path, book_dir=self._book_dir)
            results.append((bp, path))
        results.sort(key=lambda pair: (pair[0].chapter, pair[1].as_posix()))
        return results

    def _collect_prose_files(self, chapter_num: int | None) -> list[tuple[int | None, Path]]:
        collected: list[tuple[int | None, Path]] = []
        for sub_dir in (
            resolve_drafts_dir(self._book_dir),
            resolve_manuscript_dir(self._book_dir),
        ):
            if not sub_dir.is_dir():
                continue
            for path in sorted(sub_dir.rglob("*.md")):
                if not path.is_file():
                    continue
                content = path.read_text(encoding="utf-8")
                file_ch = _extract_chapter_from_path(path, content)
                if chapter_num is not None and file_ch != chapter_num:
                    continue
                collected.append((file_ch, path))
        return collected

    def _collect_archived_state_ids(self) -> set[str]:
        archived: set[str] = set()
        active_chars = self._load_character_state_file(
            resolve_active_characters_file(self._book_dir)
        )
        arch_chars = self._load_character_state_file(
            resolve_archive_characters_file(self._book_dir)
        )
        archived.update(set(arch_chars.root.keys()) - set(active_chars.root.keys()))

        active_facs = self._load_faction_state_file(
            resolve_active_factions_file(self._book_dir)
        )
        awake_fids = {
            fid for fid, f_state in active_facs.root.items() if f_state.status == EntityStatus.ACTIVE
        }
        arch_facs = self._load_faction_state_file(
            resolve_archive_factions_file(self._book_dir)
        )
        archived.update(set(arch_facs.root.keys()) - awake_fids)
        for fid, f_state in active_facs.root.items():
            if f_state.status in (EntityStatus.DECEASED, EntityStatus.RETIRED):
                archived.add(fid)
        return archived

    def _collect_max_fact_chapter_by_entity(self) -> dict[str, int]:
        max_ch: dict[str, int] = {}
        for path in list_facts_files(self._book_dir):
            if not path.is_file():
                continue
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = ChapterFactRecord.model_validate_json(line)
                except ValidationError:
                    continue
                for eid in rec.entities:
                    if rec.ch > max_ch.get(eid, 0):
                        max_ch[eid] = rec.ch
        return max_ch

    @staticmethod
    def _blueprint_text(bp: BlueprintDoc) -> str:
        parts: list[str] = []
        if bp.title:
            parts.append(bp.title)
        for beat in bp.beats:
            if beat.title:
                parts.append(beat.title)
            if beat.content:
                parts.append(beat.content)
        parts.extend(bp.forbidden)
        if bp.body:
            parts.append(bp.body)
        return "\n".join(parts)

    def export_manuscript_txt(
        self,
        *,
        output_path: Path | str | None = None,
        volume_id: str | None = None,
        start_chapter: int | None = None,
        end_chapter: int | None = None,
        include_book_title: bool = True,
        strip_markdown: bool = True,
        dry_run: bool = False,
    ) -> ExportTxtResult:
        """将定稿目录中的所有正文按章序合并打包为单个 UTF-8 TXT 文件。"""
        if volume_id is not None:
            volume_id = _VOLUME_ID_ADAPTER.validate_python(volume_id)
        if start_chapter is not None:
            start_chapter = _validate_non_bool_int(start_chapter, name="start_chapter", min_value=1)
        if end_chapter is not None:
            end_chapter = _validate_non_bool_int(end_chapter, name="end_chapter", min_value=1)
        if start_chapter is not None and end_chapter is not None and start_chapter > end_chapter:
            raise ValueError(
                f"start_chapter ({start_chapter}) 不能大于 end_chapter ({end_chapter})"
            )

        manuscript_dir = resolve_manuscript_dir(self._book_dir)
        if not manuscript_dir.is_dir():
            raise FileNotFoundError(f"未找到定稿目录: {manuscript_dir}")

        candidates: list[tuple[tuple[int, int, str], int | None, Path, str]] = []
        for md_path in sorted(manuscript_dir.rglob("*.md")):
            if not md_path.is_file():
                continue
            if md_path.name.lower() == "readme.md":
                continue
            if volume_id is not None:
                if md_path.parent == manuscript_dir:
                    continue
                if infer_volume_id_from_stem(md_path.parent.name) != volume_id:
                    continue

            raw_text = md_path.read_text(encoding="utf-8", errors="replace")
            fm_dict, body = split_frontmatter(raw_text)

            ch_num: int | None = None
            raw_ch = fm_dict.get("chapter")
            if isinstance(raw_ch, int) and not isinstance(raw_ch, bool) and raw_ch >= 1:
                ch_num = raw_ch
            elif isinstance(raw_ch, str) and raw_ch.strip().isdigit() and int(raw_ch.strip()) >= 1:
                ch_num = int(raw_ch.strip())
            if ch_num is None:
                ch_num = _extract_chapter_from_path(md_path, body)

            if start_chapter is not None and (ch_num is None or ch_num < start_chapter):
                continue
            if end_chapter is not None and (ch_num is None or ch_num > end_chapter):
                continue

            formatted_body = _format_chapter_for_txt(body, strip_markdown=strip_markdown)
            if not formatted_body:
                continue

            rel_posix = self._rel_posix(md_path)
            sort_key = (0, ch_num, rel_posix) if ch_num is not None else (1, 0, rel_posix)
            candidates.append((sort_key, ch_num, md_path, formatted_body))

        if not candidates:
            raise FileNotFoundError(f"在定稿目录 {manuscript_dir} 中未找到符合条件的定稿章节")

        candidates.sort(key=lambda item: item[0])

        chunks: list[str] = []
        if include_book_title:
            book_title = (self._config.title or "").strip()
            if book_title:
                author = str(getattr(self._config, "author", "") or "").strip()
                header = f"《{book_title}》\n作者：{author}" if author else f"《{book_title}》"
                chunks.append(header)

        chunks.extend(item[3] for item in candidates)
        merged_text = "\n\n\n".join(chunks) + "\n"

        if output_path is None:
            safe_title = sanitize_filename_label(self._config.title or "") or "manuscript_export"
            suffix = f"_{volume_id}" if volume_id is not None else ""
            out_path = manuscript_dir.parent / f"{safe_title}{suffix}.txt"
        else:
            raw_out = Path(output_path)
            out_path = raw_out if raw_out.is_absolute() else (self._book_dir / raw_out)

        if not dry_run:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(merged_text, encoding="utf-8")

        return ExportTxtResult(
            output_path=self._rel_posix(out_path),
            chapter_count=len(candidates),
            chapters=[item[1] for item in candidates if item[1] is not None],
            source_paths=[self._rel_posix(item[2]) for item in candidates],
            total_chars=len(merged_text),
            dry_run=dry_run,
        )

    @staticmethod
    def _load_character_state_file(path: Path) -> CharacterStateFile:
        if not path.is_file():
            return CharacterStateFile(root={})
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not raw:
            return CharacterStateFile(root={})
        return CharacterStateFile.model_validate(raw)

    @staticmethod
    def _load_faction_state_file(path: Path) -> FactionStateFile:
        if not path.is_file():
            return FactionStateFile(root={})
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not raw:
            return FactionStateFile(root={})
        return FactionStateFile.model_validate(raw)

    @staticmethod
    def _load_relations_file(path: Path) -> RelationsFile:
        if not path.is_file():
            return RelationsFile(relations=[])
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not raw:
            return RelationsFile(relations=[])
        return RelationsFile.model_validate(raw)

    @staticmethod
    def _load_threads_file(path: Path) -> ThreadsFile:
        if not path.is_file():
            return ThreadsFile(threads=[])
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not raw:
            return ThreadsFile(threads=[])
        return ThreadsFile.model_validate(raw)


__all__ = ["ExportTxtResult", "LifecycleManager"]
