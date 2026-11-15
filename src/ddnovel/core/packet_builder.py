"""单章写作包一步直出编译器 (Task T08)。

按 U 型注意力预算曲线编译生成可人工增删微调的单章独立写作包
（``04_creation/packets/ch_XXXX_packet.md``），并严格执行三重物理隔离：
1. 设定 ``## [机密]`` 切片与敏感别名/非 POV 隐秘状态默认物理隔离；
2. 未被本章蓝图勾选的伏笔以及非 ``resolve`` 动作伏笔的 ``truth_secret`` 物理屏蔽；
3. 冷库归档与退场实体、失效旧关系边在自动嗅探中物理过滤。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Self

import yaml

from .alias_matcher import AliasMatcher
from .indexer import SqliteIndexer
from .parser import ProgressiveParser, estimate_tokens, lint_model_setting_doc, split_frontmatter
from .paths import (
    format_chapter_stem,
    infer_arc_id_from_stem,
    infer_volume_id_from_stem,
    is_archive_path,
    resolve_arc_file,
    resolve_arcs_dir,
    resolve_blueprints_dir,
    resolve_config_path,
    resolve_manuscript_dir,
    resolve_packet_file,
    resolve_style_guide_file,
    resolve_volume_file,
    resolve_volume_subdir,
    resolve_volumes_dir,
    resolve_world_state_file,
)
from .schemas import (
    ArcDoc,
    BlueprintDoc,
    CharacterState,
    EntityBundle,
    EntityStatus,
    FactionState,
    FtsScope,
    NovelConfig,
    PacketBuildResult,
    SettingCategory,
    SettingDoc,
    SlotBudget,
    SlotName,
    SlotReport,
    StyleGuide,
    Thread,
    ThreadAction,
    ThreadStatus,
    TokenBudgetConfig,
    VolumeDoc,
    WorldState,
    format_chapter,
)

_SLOT_TITLES: Final[dict[SlotName, str]] = {
    SlotName.GUARDRAILS: "槽位 1 · 顶部强注意区：文风红线与 POV 认知边界",
    SlotName.WORLD_AND_FACTS: "槽位 2 · 中前背景区：宏观坐标、环境法则与历史事实锚点",
    SlotName.ENTITIES: "槽位 3 · 中部实体截面区：出场角色/势力/道具与当前有效关系",
    SlotName.CONTINUITY: "槽位 4 · 中后文气衔接区：近期摘要与上章末尾原文",
    SlotName.BEATS: "槽位 5 · 尾部最高注意指令区：分场节拍、指定伏笔与禁止事项",
}

_WORLD_SLOT_CATEGORIES: Final[frozenset[SettingCategory]] = frozenset(
    {
        SettingCategory.WORLD,
        SettingCategory.RULE,
        SettingCategory.GEOGRAPHY,
    }
)

_ENTITY_SLOT_CATEGORIES: Final[frozenset[SettingCategory]] = frozenset(
    {
        SettingCategory.CHARACTER,
        SettingCategory.FACTION,
        SettingCategory.ITEM,
        SettingCategory.OTHER,
    }
)

_PROTECTED_SLOTS: Final[frozenset[SlotName]] = frozenset(
    {
        SlotName.GUARDRAILS,
        SlotName.BEATS,
    }
)

_QUOTED_TERM_RE: Final[re.Pattern[str]] = re.compile(r"[“\"「『]([^”\"」』]{2,32})[”\"」』]")
_CJK_RUN_RE: Final[re.Pattern[str]] = re.compile(r"[\u4e00-\u9fff]{2,}")
_SENTENCE_BOUNDARY_RE: Final[re.Pattern[str]] = re.compile(r"(?:\n\s*\n|\n|[。！？……]|[.!?]\s)")
_CHAPTER_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^\s*#\s+[^\n]*\n*")


@dataclass(slots=True)
class _SlotItem:
    """槽位内可独立裁剪的离散语义条目。"""

    group: str
    label: str
    content: str
    priority: int
    order: int
    compact_content: str | None = None
    compact_priority: int | None = None
    compact_label: str | None = None
    compacted: bool = False


@dataclass(slots=True)
class _ThreadRow:
    """索引库 threads 表的完整记录视图（包含 source_file）。"""

    thread: Thread
    source_file: str

    @property
    def is_archived_or_resolved(self) -> bool:
        return is_archive_path(self.source_file) or self.thread.status == ThreadStatus.RESOLVED


@dataclass(slots=True)
class _BuildContext:
    """单次写作包编译期间的共享状态容器。"""

    chapter_num: int
    blueprint: BlueprintDoc
    budget: TokenBudgetConfig
    indexer: SqliteIndexer
    matcher: AliasMatcher
    pov_state: CharacterState | None = None
    explicit_ids: list[str] = field(default_factory=list)
    explicit_id_set: set[str] = field(default_factory=set)
    sniffed_ids: list[str] = field(default_factory=list)
    bundles: dict[str, EntityBundle] = field(default_factory=dict)
    locked_secrets_by_entity: dict[str, str] = field(default_factory=dict)
    blocked_secret_ids: list[str] = field(default_factory=list)
    blocked_thread_ids: list[str] = field(default_factory=list)
    blocked_truth_secrets: list[str] = field(default_factory=list)
    public_names: set[str] = field(default_factory=set)
    thread_rows: dict[str, _ThreadRow] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    _sensitive_phrases: set[str] | None = None
    _sensitive_quoted: set[str] | None = None

    def add_warning(self, msg: str) -> None:
        cleaned = msg.strip()
        if cleaned and cleaned not in self.warnings:
            self.warnings.append(cleaned)

    def mark_blocked_secret(self, entity_id: str, secret_text: str) -> None:
        if entity_id not in self.blocked_secret_ids:
            self.blocked_secret_ids.append(entity_id)
        if entity_id not in self.locked_secrets_by_entity and secret_text.strip():
            self.locked_secrets_by_entity[entity_id] = secret_text.strip()
            self._sensitive_phrases = None
            self._sensitive_quoted = None

    def _ensure_sensitive_cache(self) -> tuple[set[str], set[str]]:
        if self._sensitive_phrases is not None and self._sensitive_quoted is not None:
            return self._sensitive_phrases, self._sensitive_quoted

        phrases: set[str] = set()
        quoted: set[str] = set()

        sources: list[str] = list(self.locked_secrets_by_entity.values())
        sources.extend(self.blocked_truth_secrets)
        if self.pov_state is not None:
            sources.extend(self.pov_state.epistemic_bounds.unaware_of)

        for text in sources:
            if not text:
                continue
            for m in _QUOTED_TERM_RE.finditer(text):
                term = m.group(1).strip()
                if len(term) >= 2 and term not in self.public_names:
                    quoted.add(term)
            for run in _CJK_RUN_RE.findall(text):
                if len(run) >= 4:
                    for i in range(len(run) - 3):
                        gram = run[i : i + 4]
                        if gram not in self.public_names:
                            phrases.add(gram)

        self._sensitive_phrases = phrases
        self._sensitive_quoted = quoted
        return phrases, quoted

    def is_sensitive_text(self, text: str) -> bool:
        """判断一段非公开文本是否命中被锁机密、未揭晓伏笔或 POV 未知红线。"""
        if not text:
            return False
        phrases, quoted = self._ensure_sensitive_cache()
        for q in quoted:
            if q in text:
                return True
        for p in phrases:
            if p in text:
                return True
        return False

    def is_secret_alias_or_tag(self, term: str, entity_id: str) -> bool:
        """判断实体别名/标签是否属于应隐藏的机密代号。"""
        cleaned = term.strip()
        if not cleaned or cleaned in self.public_names:
            return False
        secret_sources: list[str] = []
        own_secret = self.locked_secrets_by_entity.get(entity_id)
        if own_secret:
            secret_sources.append(own_secret)
        if self.pov_state is not None:
            secret_sources.extend(self.pov_state.epistemic_bounds.unaware_of)
        secret_sources.extend(self.blocked_truth_secrets)

        for src in secret_sources:
            if cleaned in src:
                return True
            if len(cleaned) >= 4:
                # 若长别名是由机密文本中的核心词组合而成（如“白璃圣女” <- “圣女“白璃””）
                chunks = [cleaned[i : i + 2] for i in range(0, len(cleaned) - 1, 2)]
                if chunks and all(chunk in src for chunk in chunks):
                    return True
        return False


class PacketBuilder:
    """单章独立写作包一步直出编译器（U 型五槽位排版 + 三重隔离 + 预算裁剪）。"""

    def __init__(
        self,
        book_dir: Path | str,
        *,
        indexer: SqliteIndexer | None = None,
        matcher: AliasMatcher | None = None,
        parser: ProgressiveParser | None = None,
        config: NovelConfig | None = None,
        auto_sync: bool = True,
    ) -> None:
        self._book_dir: Path = Path(book_dir).resolve()
        self._external_indexer: SqliteIndexer | None = indexer
        self._external_matcher: AliasMatcher | None = matcher
        self._auto_sync: bool = auto_sync
        self._owned_indexer: SqliteIndexer | None = None
        self._active_ctx: _BuildContext | None = None

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
        """提供线程/生命周期安全的 SqliteIndexer 会话上下文。"""
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

    # ------------------------------------------------------------------
    # 公共编译入口
    # ------------------------------------------------------------------

    def build(
        self,
        chapter_num: int,
        custom_budget: int | TokenBudgetConfig | None = None,
        *,
        blueprint: BlueprintDoc | None = None,
        sync_index: bool | None = None,
    ) -> PacketBuildResult:
        """一步编译指定章节的独立写作包 Markdown 文件并返回详细报告。"""
        self._validate_chapter_num(chapter_num)
        effective_budget = self._resolve_budget(custom_budget)

        bp = blueprint if blueprint is not None else self._locate_and_parse_blueprint(chapter_num)
        if bp.chapter != chapter_num:
            raise ValueError(
                f"蓝图章号 ({bp.chapter}) 与请求编译章号 ({chapter_num}) 不一致"
            )

        should_sync = self._auto_sync if sync_index is None else sync_index

        with self._use_indexer() as active_indexer:
            if should_sync:
                active_indexer.sync()

            matcher = (
                self._external_matcher
                if self._external_matcher is not None
                else active_indexer.get_alias_matcher()
            )
            ctx = self._prepare_build_context(
                chapter_num=chapter_num,
                blueprint=bp,
                budget=effective_budget,
                indexer=active_indexer,
                matcher=matcher,
            )

            self._active_ctx = ctx
            try:
                slot_items = self._build_all_slot_items(ctx)
            finally:
                self._active_ctx = None

        slot_budgets: dict[SlotName, int] = {
            SlotName.GUARDRAILS: effective_budget.slots.guardrails,
            SlotName.WORLD_AND_FACTS: effective_budget.slots.world_and_facts,
            SlotName.ENTITIES: effective_budget.slots.entities,
            SlotName.CONTINUITY: effective_budget.slots.continuity,
            SlotName.BEATS: effective_budget.slots.beats,
        }
        dropped_by_slot: dict[SlotName, list[str]] = {slot: [] for slot in SlotName}

        self._trim_slots_to_budget(
            slot_items=slot_items,
            slot_budgets=slot_budgets,
            effective_budget=effective_budget,
            dropped_by_slot=dropped_by_slot,
            ctx=ctx,
        )

        final_markdown, slot_reports, total_tokens = self._assemble_final_markdown(
            ctx=ctx,
            slot_items=slot_items,
            slot_budgets=slot_budgets,
            dropped_by_slot=dropped_by_slot,
        )

        packet_abs_path = resolve_packet_file(
            self._book_dir,
            chapter_num,
            width=self._config.chapter_id_width,
            for_write=True,
        )
        packet_rel_path = packet_abs_path.relative_to(self._book_dir).as_posix()
        packet_abs_path.parent.mkdir(parents=True, exist_ok=True)
        packet_abs_path.write_text(final_markdown, encoding="utf-8")

        return PacketBuildResult(
            chapter=chapter_num,
            packet_path=packet_rel_path,
            budget_total=effective_budget.total,
            total_tokens=total_tokens,
            slots=slot_reports,
            blocked_secrets=ctx.blocked_secret_ids,
            blocked_threads=ctx.blocked_thread_ids,
            warnings=ctx.warnings,
        )

    # ------------------------------------------------------------------
    # 对齐架构类图的 5 个槽位编译方法
    # ------------------------------------------------------------------

    def _compile_slot_1_guardrails(
        self,
        bp: BlueprintDoc,
        pov_state: CharacterState | None,
    ) -> str:
        """编译槽位 1：文风红线与 POV 认知边界。"""
        with self._ensure_slot_context(bp, pov_state=pov_state) as ctx:
            items = self._build_slot_1_items(ctx)
            return self._render_slot_body(SlotName.GUARDRAILS, items)

    def _compile_slot_2_world_and_facts(
        self,
        bp: BlueprintDoc,
        matched_ids: Sequence[str],
    ) -> str:
        """编译槽位 2：宏观坐标、环境/规则设定切片与历史原子事实。"""
        with self._ensure_slot_context(bp, matched_ids=matched_ids) as ctx:
            items = self._build_slot_2_items(ctx)
            return self._render_slot_body(SlotName.WORLD_AND_FACTS, items)

    def _compile_slot_3_entities_and_relations(
        self,
        bp: BlueprintDoc,
        matched_ids: Sequence[str],
    ) -> str:
        """编译槽位 3：出场实体核心设定、热状态截面与当前章有效关系边。"""
        with self._ensure_slot_context(bp, matched_ids=matched_ids) as ctx:
            items = self._build_slot_3_items(ctx)
            return self._render_slot_body(SlotName.ENTITIES, items)

    def _compile_slot_4_continuity(self, ch_num: int) -> str:
        """编译槽位 4：近期章节百字摘要与上章末尾正文衔接切片。"""
        dummy_bp = BlueprintDoc(chapter=ch_num)
        with self._ensure_slot_context(dummy_bp) as ctx:
            items = self._build_slot_4_items(ctx)
            return self._render_slot_body(SlotName.CONTINUITY, items)

    def _compile_slot_5_beats_and_threads(self, bp: BlueprintDoc) -> str:
        """编译槽位 5：本章分场节拍蓝图、指定伏笔动作与禁止事项。"""
        with self._ensure_slot_context(bp) as ctx:
            items = self._build_slot_5_items(ctx)
            return self._render_slot_body(SlotName.BEATS, items)

    @contextmanager
    def _ensure_slot_context(
        self,
        bp: BlueprintDoc,
        *,
        pov_state: CharacterState | None = None,
        matched_ids: Sequence[str] | None = None,
    ) -> Any:
        if self._active_ctx is not None and self._active_ctx.blueprint.chapter == bp.chapter:
            yield self._active_ctx
            return

        with self._use_indexer() as active_indexer:
            matcher = (
                self._external_matcher
                if self._external_matcher is not None
                else active_indexer.get_alias_matcher()
            )
            ctx = self._prepare_build_context(
                chapter_num=bp.chapter,
                blueprint=bp,
                budget=self._config.budget,
                indexer=active_indexer,
                matcher=matcher,
                matched_ids_override=matched_ids,
                pov_state_override=pov_state,
            )
            yield ctx

    # ------------------------------------------------------------------
    # 参数校验与蓝图定位
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_chapter_num(chapter_num: int) -> None:
        if isinstance(chapter_num, bool) or not isinstance(chapter_num, int) or chapter_num < 1:
            raise ValueError(f"章号 chapter_num 必须为 >= 1 的整数，收到: {chapter_num!r}")

    def _resolve_budget(
        self,
        custom_budget: int | TokenBudgetConfig | None,
    ) -> TokenBudgetConfig:
        if custom_budget is None:
            return self._config.budget
        if isinstance(custom_budget, TokenBudgetConfig):
            return custom_budget
        if isinstance(custom_budget, bool) or not isinstance(custom_budget, int):
            raise TypeError(
                "custom_budget 必须为正整数、TokenBudgetConfig 或 None，"
                f"实际收到: {type(custom_budget).__name__}"
            )
        if custom_budget < 1:
            raise ValueError(f"custom_budget 必须为 >= 1 的正整数，收到: {custom_budget}")

        base = self._config.budget
        denom = max(base.total, base.slots.total_allocated(), 1)
        scaled_slots = SlotBudget(
            guardrails=int(base.slots.guardrails * custom_budget // denom),
            world_and_facts=int(base.slots.world_and_facts * custom_budget // denom),
            entities=int(base.slots.entities * custom_budget // denom),
            continuity=int(base.slots.continuity * custom_budget // denom),
            beats=int(base.slots.beats * custom_budget // denom),
        )
        return base.model_copy(update={"total": custom_budget, "slots": scaled_slots})

    @staticmethod
    def _scandir_md_files(root: Path) -> list[Path]:
        """使用 os.scandir 递归收集目录下的全部 Markdown 文件（零额外 stat 系统调用）。"""
        found: list[Path] = []
        stack: list[Path] = [root]
        while stack:
            cur = stack.pop()
            with os.scandir(cur) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(".md"):
                        found.append(Path(entry.path))
        found.sort()
        return found

    def _locate_and_parse_blueprint(self, chapter_num: int) -> BlueprintDoc:
        bp_root = resolve_blueprints_dir(self._book_dir)
        if not bp_root.is_dir():
            raise FileNotFoundError(f"未找到第 {chapter_num} 章的章节蓝图文件（目录不存在: {bp_root}）")

        all_files = self._scandir_md_files(bp_root)
        if not all_files:
            raise FileNotFoundError(f"未找到第 {chapter_num} 章的章节蓝图文件")

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
                return parsed

        raise FileNotFoundError(f"未找到第 {chapter_num} 章的章节蓝图文件")

    # ------------------------------------------------------------------
    # 上下文准备与三重物理隔离前置收集
    # ------------------------------------------------------------------

    def _prepare_build_context(
        self,
        *,
        chapter_num: int,
        blueprint: BlueprintDoc,
        budget: TokenBudgetConfig,
        indexer: SqliteIndexer,
        matcher: AliasMatcher,
        matched_ids_override: Sequence[str] | None = None,
        pov_state_override: CharacterState | None = None,
    ) -> _BuildContext:
        ctx = _BuildContext(
            chapter_num=chapter_num,
            blueprint=blueprint,
            budget=budget,
            indexer=indexer,
            matcher=matcher,
        )

        # 收集库中全部公开实体名称与全书被锁机密，供后续防旁路剧透校验
        cursor = indexer.conn.execute("SELECT id, name, secret_text FROM entities")
        unlock_set = set(blueprint.unlock_secrets)
        for row_eid, row_name, row_secret in cursor.fetchall():
            eid_str = str(row_eid)
            name_str = str(row_name).strip()
            if name_str and name_str != eid_str:
                ctx.public_names.add(name_str)
            if row_secret and str(row_secret).strip() and eid_str not in unlock_set:
                ctx.locked_secrets_by_entity[eid_str] = str(row_secret).strip()

        # 读取全部伏笔台账（含 source_file 以区分活跃台账与 archive 冷库）
        ctx.thread_rows = self._load_thread_rows(indexer)
        selected_thread_actions = {ref.thread_id: ref.action for ref in blueprint.threads}
        for tid, trow in ctx.thread_rows.items():
            action = selected_thread_actions.get(tid)
            if action is None:
                if not trow.is_archived_or_resolved and tid not in ctx.blocked_thread_ids:
                    ctx.blocked_thread_ids.append(tid)
                if trow.thread.truth_secret:
                    ctx.blocked_truth_secrets.append(trow.thread.truth_secret)
            elif action != ThreadAction.RESOLVE and trow.thread.truth_secret:
                ctx.blocked_truth_secrets.append(trow.thread.truth_secret)

        # 收集显式实体 ID（包含 POV 当前所在位置）
        explicit_ids: list[str] = list(blueprint.all_entity_ids())
        if blueprint.pov is not None:
            try:
                pov_bundle = indexer.get_entity_bundle(blueprint.pov, at_chapter=chapter_num)
                ctx.bundles[blueprint.pov] = pov_bundle
                if isinstance(pov_bundle.state, CharacterState):
                    ctx.pov_state = (
                        pov_state_override if pov_state_override is not None else pov_bundle.state
                    )
                    if (
                        pov_bundle.state.location is not None
                        and pov_bundle.state.location not in explicit_ids
                    ):
                        explicit_ids.append(pov_bundle.state.location)
            except KeyError:
                ctx.add_warning(f"蓝图指定的 POV 角色 '{blueprint.pov}' 在索引库中不存在")
        elif pov_state_override is not None:
            ctx.pov_state = pov_state_override

        ctx.explicit_ids = list(dict.fromkeys(explicit_ids))
        ctx.explicit_id_set = set(ctx.explicit_ids)

        # 加载显式实体（含按需点名冷库实体的告警）
        for eid in ctx.explicit_ids:
            bundle = ctx.bundles.get(eid)
            if bundle is None:
                try:
                    bundle = indexer.get_entity_bundle(eid, at_chapter=chapter_num)
                    ctx.bundles[eid] = bundle
                except KeyError:
                    ctx.add_warning(f"蓝图引用的实体 '{eid}' 在索引库中不存在，已跳过")
                    continue
            if bundle.is_archived or bundle.setting.status in (
                EntityStatus.DECEASED,
                EntityStatus.RETIRED,
            ):
                ctx.add_warning(f"实体 '{eid}' 处于冷库/退场状态，因蓝图显式点名已按需载入")

        # 检查 unlock_secrets 与 topics 声明有效性
        for secret_eid in blueprint.unlock_secrets:
            bundle = ctx.bundles.get(secret_eid)
            if bundle is not None and not (bundle.setting.secret_text or "").strip():
                ctx.add_warning(f"蓝图解锁机密的实体 '{secret_eid}' 未定义 [机密] 切片")

        for topic_ref in blueprint.topics:
            bundle = ctx.bundles.get(topic_ref.entity_id)
            if bundle is not None and topic_ref.topic not in bundle.setting.topics:
                ctx.add_warning(
                    f"蓝图请求的专题 '{topic_ref.entity_id}:{topic_ref.topic}' 在设定文档中不存在"
                )

        # 使用 AliasMatcher 对蓝图文本进行自动嗅探（严格跳过冷库/退场实体）
        if matched_ids_override is not None:
            raw_sniffed = list(matched_ids_override)
        else:
            scan_text = self._blueprint_scan_text(blueprint)
            raw_sniffed = matcher.scan_entity_ids(scan_text, include_archived=False)

        for eid in raw_sniffed:
            if eid in ctx.explicit_id_set:
                continue
            try:
                bundle = indexer.get_entity_bundle(eid, at_chapter=chapter_num)
            except KeyError:
                continue
            if bundle.is_archived or bundle.setting.status in (
                EntityStatus.DECEASED,
                EntityStatus.RETIRED,
            ):
                continue
            ctx.bundles[eid] = bundle
            if eid not in ctx.sniffed_ids:
                ctx.sniffed_ids.append(eid)

        return ctx

    @staticmethod
    def _load_thread_rows(indexer: SqliteIndexer) -> dict[str, _ThreadRow]:
        rows: dict[str, _ThreadRow] = {}
        cursor = indexer.conn.execute(
            """
            SELECT id, title, category, status, planted_at, last_progressed_at,
                   win_start, win_end, trigger_entities_json, public_clue, truth_secret, source_file
            FROM threads
            ORDER BY planted_at ASC, id ASC
            """
        )
        from .schemas import ChapterWindow

        for r in cursor.fetchall():
            exp_win = (
                ChapterWindow(start=int(r[6]), end=int(r[7]))
                if r[6] is not None and r[7] is not None
                else None
            )
            thr = Thread(
                id=str(r[0]),
                title=str(r[1]),
                category=str(r[2]) if r[2] else "other",
                status=ThreadStatus(r[3]),
                planted_at=int(r[4]),
                last_progressed_at=int(r[5]),
                expected_window=exp_win,
                trigger_entities=json.loads(r[8]) if r[8] else [],
                public_clue=str(r[9] or ""),
                truth_secret=str(r[10]) if r[10] else None,
            )
            rows[thr.id] = _ThreadRow(thread=thr, source_file=str(r[11] or ""))
        return rows

    @staticmethod
    def _blueprint_scan_text(bp: BlueprintDoc) -> str:
        parts: list[str] = []
        if bp.title:
            parts.append(bp.title)
        for beat in bp.beats:
            if beat.title:
                parts.append(beat.title)
            if beat.content:
                parts.append(beat.content)
        if bp.body:
            parts.append(bp.body)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 五槽位内容构建
    # ------------------------------------------------------------------

    def _build_all_slot_items(self, ctx: _BuildContext) -> dict[SlotName, list[_SlotItem]]:
        return {
            SlotName.GUARDRAILS: self._build_slot_1_items(ctx),
            SlotName.WORLD_AND_FACTS: self._build_slot_2_items(ctx),
            SlotName.ENTITIES: self._build_slot_3_items(ctx),
            SlotName.CONTINUITY: self._build_slot_4_items(ctx),
            SlotName.BEATS: self._build_slot_5_items(ctx),
        }

    # -- 槽位 1: 文风红线 + POV 认知边界 --------------------------------

    def _build_slot_1_items(self, ctx: _BuildContext) -> list[_SlotItem]:
        items: list[_SlotItem] = []
        order = 0

        # 1.1 POV 视角与防剧透认知红线（置顶最高优先级）
        pov_lines: list[str] = ["### POV 视角与防穿帮认知红线"]
        bp = ctx.blueprint
        if bp.pov is not None:
            pov_bundle = ctx.bundles.get(bp.pov)
            pov_name = pov_bundle.setting.name if pov_bundle is not None else bp.pov
            pov_lines.append(f"- **本章叙事主视角 (POV)**：{pov_name} (`{bp.pov}`)")
            if ctx.pov_state is not None:
                eb = ctx.pov_state.epistemic_bounds
                if eb.unaware_of:
                    pov_lines.append("- **【绝对认知禁区 · 严禁看破 (`unaware_of`)】**：")
                    for entry in eb.unaware_of:
                        pov_lines.append(f"  - [红线] {entry}")
                if eb.misbelieves:
                    pov_lines.append("- **【当前主观误判 · 按误解视角描写 (`misbelieves`)】**：")
                    for entry in eb.misbelieves:
                        pov_lines.append(f"  - {entry}")
                if eb.knows:
                    pov_lines.append("- **【POV 当前已知情报 (`knows`)】**：")
                    for entry in eb.knows:
                        pov_lines.append(f"  - {entry}")
            else:
                pov_lines.append("- *（POV 角色暂无动态状态截面，须保持第三人称有限视角）*")
        else:
            pov_lines.append("- **本章叙事主视角 (POV)**：未指定单一角色（仍须严守蓝图禁止项与已隔离机密）")

        pov_lines.append(
            "- **【设定使用边界 · 严禁照抄打卡】**：背景与实体档案中的设定仅提供客观本底与抽象性格/规则边界，"
            "严禁将背景特征当作本章必演动作逐条打卡或照搬词句；具体场景、对话与动作均由尾部本章分场节拍自然驱动。"
        )

        items.append(
            _SlotItem(
                group="pov_guardrails",
                label="POV认知边界红线",
                content="\n".join(pov_lines),
                priority=100,
                order=order,
            )
        )
        order += 1

        # 1.2 文风指南 (04_creation/style_guide.md)
        sg = self._load_style_guide(ctx)
        if sg is not None:
            sg_lines: list[str] = ["### 本书文风指南与去 AI 腔红线"]
            if sg.tone:
                sg_lines.append(f"- **叙事基调**：{sg.tone}")
            if sg.pov_rules:
                sg_lines.append("- **视角铁律**：")
                for rule in sg.pov_rules:
                    sg_lines.append(f"  - {rule}")
            if sg.banned_words:
                sg_lines.append(f"- **禁用套话红线**：{'、'.join(sg.banned_words)}")
            if sg.raw_markdown.strip():
                sg_lines.append("")
                sg_lines.append(sg.raw_markdown.strip())
            items.append(
                _SlotItem(
                    group="style_guide",
                    label="文风与禁用词指南",
                    content="\n".join(sg_lines),
                    priority=100,
                    order=order,
                )
            )
            order += 1

        return items

    def _load_style_guide(self, ctx: _BuildContext) -> StyleGuide | None:
        sg_path = resolve_style_guide_file(self._book_dir)
        if not sg_path.is_file():
            ctx.add_warning("未找到文风指南文件 04_创作/文风指南.md (04_creation/style_guide.md)")
            return None

        raw_text = sg_path.read_text(encoding="utf-8")
        fm_dict, body = split_frontmatter(raw_text)
        tone = str(fm_dict.get("tone") or "").strip()

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

        return StyleGuide(
            tone=tone,
            pov_rules=pov_rules,
            banned_words=banned_words,
            raw_markdown=body.strip(),
        )

    # -- 槽位 2: 宏观坐标 + 地理/规则/世界设定 + 历史原子事实 -----------

    def _build_slot_2_items(self, ctx: _BuildContext) -> list[_SlotItem]:
        items: list[_SlotItem] = []
        order = 0
        bp = ctx.blueprint

        # 2.1 宏观世界状态与卷/单元坐标 (priority=75)
        macro_lines: list[str] = []
        world_state = self._load_world_state(ctx)
        if world_state is not None:
            if world_state.in_world_time:
                macro_lines.append(f"- **当前书内时间**：{world_state.in_world_time}")
            if world_state.macro_situation:
                macro_lines.append(f"- **当前天下大势**：{world_state.macro_situation}")

        vol_doc = self._load_volume_doc(bp.volume_id, ctx.chapter_num)
        if vol_doc is not None:
            vol_summary = vol_doc.folded_summary or vol_doc.core_conflict
            vol_title = f"《{vol_doc.title}》" if vol_doc.title else vol_doc.id
            if vol_summary:
                macro_lines.append(f"- **所属分卷 ({vol_doc.id} {vol_title})**：{vol_summary}")

        arc_doc = self._load_arc_doc(bp.arc_id, ctx.chapter_num)
        if arc_doc is not None:
            arc_summary = arc_doc.folded_summary or self._first_non_heading_line(arc_doc.body)
            arc_title = f"《{arc_doc.title}》" if arc_doc.title else arc_doc.id
            if arc_summary:
                macro_lines.append(f"- **所属单元 ({arc_doc.id} {arc_title})**：{arc_summary}")

        if macro_lines:
            items.append(
                _SlotItem(
                    group="macro_context",
                    label="宏观时空与卷/单元坐标",
                    content="### 宏观时空坐标\n" + "\n".join(macro_lines),
                    priority=75,
                    order=order,
                )
            )
            order += 1

        # 2.2 世界/规则/地理设定切片
        included_world_ids: set[str] = set()
        for eid in ctx.explicit_ids:
            bundle = ctx.bundles.get(eid)
            if bundle is None or bundle.setting.category not in _WORLD_SLOT_CATEGORIES:
                continue
            included_world_ids.add(eid)
            rendered = self._render_setting_block(bundle.setting, ctx=ctx)
            if rendered:
                items.append(
                    _SlotItem(
                        group="world_settings",
                        label=f"显式设定:{bundle.setting.name}({eid})",
                        content=rendered,
                        priority=90,
                        order=order,
                    )
                )
                order += 1

        for eid in ctx.sniffed_ids:
            if eid in included_world_ids:
                continue
            bundle = ctx.bundles.get(eid)
            if bundle is None or bundle.setting.category not in _WORLD_SLOT_CATEGORIES:
                continue
            included_world_ids.add(eid)
            rendered = self._render_setting_block(bundle.setting, ctx=ctx)
            if rendered:
                items.append(
                    _SlotItem(
                        group="world_settings",
                        label=f"嗅探设定:{bundle.setting.name}({eid})",
                        content=rendered,
                        priority=60,
                        order=order,
                    )
                )
                order += 1

        # 补充活跃的世界观与底层法则设定 (priority=45)
        for cat in (SettingCategory.WORLD, SettingCategory.RULE):
            for doc in ctx.indexer.list_entities(category=cat, include_archived=False):
                if doc.id in included_world_ids:
                    continue
                included_world_ids.add(doc.id)
                if doc.id not in ctx.bundles:
                    try:
                        ctx.bundles[doc.id] = ctx.indexer.get_entity_bundle(
                            doc.id, at_chapter=ctx.chapter_num
                        )
                    except KeyError:
                        pass
                rendered = self._render_setting_block(doc, ctx=ctx)
                if rendered:
                    items.append(
                        _SlotItem(
                            group="world_settings",
                            label=f"全局设定:{doc.name}({doc.id})",
                            content=rendered,
                            priority=45,
                            order=order,
                        )
                    )
                    order += 1

        # 2.3 历史原子事实锚点（严格 ch < chapter_num，按关联度与近度赋优先级 10..35）
        fact_items = self._recall_historical_facts(ctx, start_order=order)
        items.extend(fact_items)
        return items

    def _load_world_state(self, ctx: _BuildContext) -> WorldState | None:
        ws_path = resolve_world_state_file(self._book_dir)
        if not ws_path.is_file():
            ctx.add_warning("未找到全局世界状态文件 03_状态/世界状态.yaml (03_state/world_state.yaml)")
            return None
        data = yaml.safe_load(ws_path.read_text(encoding="utf-8"))
        if not data:
            return None
        return WorldState.model_validate(data)

    def _load_volume_doc(self, volume_id: str | None, chapter_num: int) -> VolumeDoc | None:
        vol_dir = resolve_volumes_dir(self._book_dir)
        if not vol_dir.is_dir():
            return None

        if volume_id:
            target = resolve_volume_file(self._book_dir, volume_id)
            if target.is_file():
                fm, body = split_frontmatter(target.read_text(encoding="utf-8"))
                return VolumeDoc.model_validate({**fm, "id": fm.get("id") or volume_id, "body": body})

        for path in sorted(vol_dir.glob("*.md")):
            fm, body = split_frontmatter(path.read_text(encoding="utf-8"))
            vid = fm.get("id") or infer_volume_id_from_stem(path.stem)
            try:
                doc = VolumeDoc.model_validate({**fm, "id": vid, "body": body})
            except Exception:
                continue
            if volume_id and doc.id == volume_id:
                return doc
            if doc.chapter_range is not None:
                if doc.chapter_range.start <= chapter_num <= doc.chapter_range.end:
                    return doc
        return None

    def _load_arc_doc(self, arc_id: str | None, chapter_num: int) -> ArcDoc | None:
        arc_dir = resolve_arcs_dir(self._book_dir)
        if not arc_dir.is_dir():
            return None

        if arc_id:
            target = resolve_arc_file(self._book_dir, arc_id)
            if target.is_file():
                fm, body = split_frontmatter(target.read_text(encoding="utf-8"))
                return ArcDoc.model_validate({**fm, "id": fm.get("id") or arc_id, "body": body})

        for path in sorted(arc_dir.glob("*.md")):
            fm, body = split_frontmatter(path.read_text(encoding="utf-8"))
            aid = fm.get("id") or infer_arc_id_from_stem(path.stem)
            try:
                doc = ArcDoc.model_validate({**fm, "id": aid, "body": body})
            except Exception:
                continue
            if arc_id and doc.id == arc_id:
                return doc
            if doc.chapter_range is not None:
                if doc.chapter_range.start <= chapter_num <= doc.chapter_range.end:
                    return doc
        return None

    @staticmethod
    def _first_non_heading_line(body: str) -> str:
        for line in body.splitlines():
            cleaned = line.strip().lstrip("-* ").strip()
            if cleaned and not cleaned.startswith("#"):
                return cleaned
        return ""

    def _recall_historical_facts(
        self,
        ctx: _BuildContext,
        *,
        start_order: int,
    ) -> list[_SlotItem]:
        """召回严格早于当前章号的历史原子事实锚点（兼容 FTS5 + 向量 RRF 与实体交集）。"""
        if ctx.chapter_num <= 1:
            return []

        bp = ctx.blueprint
        involved_eids = set(ctx.explicit_ids) | set(ctx.sniffed_ids)

        query_terms: list[str] = []
        if bp.title:
            query_terms.append(bp.title)
        for beat in bp.beats:
            if beat.title:
                query_terms.append(beat.title)
            if beat.content:
                query_terms.append(beat.content[:80])
        for eid in list(involved_eids)[:10]:
            b = ctx.bundles.get(eid)
            if b is not None:
                query_terms.append(b.setting.name)
        query_str = " ".join(query_terms).strip()

        fts_scores: dict[tuple[int, str], float] = {}
        if query_str:
            hits = ctx.indexer.search_fts(
                query_str,
                scope=FtsScope.FACT,
                top_k=24,
                hybrid=True,
            )
            for hit in hits:
                if 1 <= hit.chapter_num < ctx.chapter_num and hit.excerpt.strip():
                    key = (hit.chapter_num, hit.excerpt.strip())
                    prev = fts_scores.get(key, 0.0)
                    if hit.score > prev:
                        fts_scores[key] = hit.score

        # 补充：从 chapter_facts 表召回与当前出场实体有交集的历史原子事实
        rows = ctx.indexer.conn.execute(
            """
            SELECT ch, volume_id, arc_id, in_world_time, summary, facts_json, entities_json
            FROM chapter_facts
            WHERE ch < ?
            ORDER BY ch DESC
            """,
            (ctx.chapter_num,),
        ).fetchall()

        candidates: dict[tuple[int, str], tuple[int, str, float]] = {}
        for r in rows:
            ch = int(r[0])
            in_world_time = str(r[3] or "").strip()
            rec_facts: list[str] = json.loads(r[5]) if r[5] else []
            rec_entities: set[str] = set(json.loads(r[6]) if r[6] else [])
            overlap = len(rec_entities & involved_eids)

            for fact in rec_facts:
                clean_fact = str(fact).strip()
                if not clean_fact:
                    continue
                key = (ch, clean_fact)
                fts_score = fts_scores.get(key, 0.0)
                if fts_score <= 0.0 and overlap == 0:
                    continue
                recency = ch / max(ctx.chapter_num, 1)
                rank_score = (2.0 if fts_score > 0.0 else 0.0) + overlap * 1.5 + fts_score + recency
                candidates[key] = (ch, in_world_time, rank_score)

        # 将仅由 FTS 命中（例如未关联 entities_json）的历史事实也纳入候选
        for (ch, fact_text), fts_score in fts_scores.items():
            key = (ch, fact_text)
            if key not in candidates:
                recency = ch / max(ctx.chapter_num, 1)
                candidates[key] = (ch, "", 2.0 + fts_score + recency)

        if not candidates:
            return []

        # 按相关度从高到低排序，最多取前 12 条，并按时间正序渲染、低相关度赋予更低 priority 优先裁剪
        top_entries = sorted(
            candidates.items(),
            key=lambda kv: (-kv[1][2], -kv[0][0], kv[0][1]),
        )[:12]
        max_rank = max(v[2] for _, v in top_entries)
        min_rank = min(v[2] for _, v in top_entries)
        span = max(max_rank - min_rank, 1e-6)

        # 按章号正序呈现给执笔模型
        ordered_entries = sorted(top_entries, key=lambda kv: (kv[0][0], kv[0][1]))
        fact_items: list[_SlotItem] = []
        order = start_order
        for (ch, fact_text), (_, in_world_time, rank_score) in ordered_entries:
            norm = (rank_score - min_rank) / span
            prio = 10 + int(round(norm * 25))
            time_tag = f"（{in_world_time}）" if in_world_time else ""
            line = f"- [第{ch}章]{time_tag} {fact_text}"
            short_fact = fact_text[:18] + ("..." if len(fact_text) > 18 else "")
            fact_items.append(
                _SlotItem(
                    group="historical_facts",
                    label=f"第{ch}章历史事实:{short_fact}",
                    content=line,
                    priority=prio,
                    order=order,
                )
            )
            order += 1

        return fact_items

    # -- 槽位 3: 出场实体（人物/势力/道具）+ 热状态截面 + 双时态关系 ----

    def _build_slot_3_items(self, ctx: _BuildContext) -> list[_SlotItem]:
        items: list[_SlotItem] = []
        order = 0
        bp = ctx.blueprint

        ordered_eids: list[tuple[str, int]] = []
        seen_eids: set[str] = set()

        def _push_eid(eid: str, prio: int) -> None:
            if eid in seen_eids:
                return
            bundle = ctx.bundles.get(eid)
            if bundle is None or bundle.setting.category not in _ENTITY_SLOT_CATEGORIES:
                return
            seen_eids.add(eid)
            ordered_eids.append((eid, prio))

        if bp.pov is not None:
            _push_eid(bp.pov, 95)
        for cid in bp.cast.characters:
            _push_eid(cid, 85)
        for fid in bp.cast.factions:
            _push_eid(fid, 65)
        for iid in bp.cast.items:
            _push_eid(iid, 65)
        for eid in ctx.explicit_ids:
            _push_eid(eid, 65)
        for eid in ctx.sniffed_ids:
            _push_eid(eid, 50)

        for eid, prio in ordered_eids:
            bundle = ctx.bundles[eid]
            rendered = self._render_entity_entry(bundle, ctx=ctx)
            if rendered:
                items.append(
                    _SlotItem(
                        group="entities",
                        label=f"实体:{bundle.setting.name}({eid})",
                        content=rendered,
                        priority=prio,
                        order=order,
                    )
                )
                order += 1

        # 双时态关系边（严格按 chapter_num 过滤，并排除未点名冷库实体参与的关系）
        slot3_eids = [eid for eid, _ in ordered_eids]
        if slot3_eids:
            relations = ctx.indexer.get_active_relations(
                entity_ids=slot3_eids,
                at_chapter=ctx.chapter_num,
            )
            slot3_set = set(slot3_eids)
            all_packet_eids = set(ctx.explicit_ids) | set(ctx.sniffed_ids)

            for rel in relations:
                other = rel.target if rel.source in slot3_set else rel.source
                if not self._is_endpoint_allowed(other, ctx=ctx):
                    continue
                if ctx.is_sensitive_text(rel.relation):
                    continue

                both_in_scene = (rel.source in all_packet_eids) and (rel.target in all_packet_eids)
                rel_prio = 75 if both_in_scene else 40
                src_name = self._resolve_entity_name(rel.source, ctx)
                tgt_name = self._resolve_entity_name(rel.target, ctx)
                rel_line = (
                    f"- **{src_name}** (`{rel.source}`) → **{tgt_name}** (`{rel.target}`)："
                    f"{rel.relation} *（第{rel.valid_from_ch}章起生效）*"
                )
                items.append(
                    _SlotItem(
                        group="relations",
                        label=f"关系边:{rel.source}->{rel.target}",
                        content=rel_line,
                        priority=rel_prio,
                        order=order,
                    )
                )
                order += 1

        return items

    def _is_endpoint_allowed(self, eid: str, *, ctx: _BuildContext) -> bool:
        if eid in ctx.explicit_id_set:
            return True
        bundle = ctx.bundles.get(eid)
        if bundle is None:
            try:
                bundle = ctx.indexer.get_entity_bundle(eid, at_chapter=ctx.chapter_num)
                ctx.bundles[eid] = bundle
            except KeyError:
                return True
        if bundle.is_archived or bundle.setting.status in (
            EntityStatus.DECEASED,
            EntityStatus.RETIRED,
        ):
            return False
        return True

    @staticmethod
    def _resolve_entity_name(eid: str, ctx: _BuildContext) -> str:
        bundle = ctx.bundles.get(eid)
        if bundle is not None:
            return bundle.setting.name
        row = ctx.indexer.conn.execute("SELECT name FROM entities WHERE id = ?", (eid,)).fetchone()
        if row and row[0]:
            return str(row[0])
        return eid

    def _render_setting_block(self, setting: SettingDoc, *, ctx: _BuildContext) -> str:
        """渲染设定文档的核心切片、按需专题与解锁机密（默认严格拦截机密）。"""
        bp = ctx.blueprint
        is_unlocked = setting.id in bp.unlock_secrets

        for leak_warn in lint_model_setting_doc(setting):
            ctx.add_warning(f"设定卡 `{setting.id}` ({setting.name}) 存在具象泄露风险：{leak_warn}")

        if setting.secret_text and setting.secret_text.strip() and not is_unlocked:
            ctx.mark_blocked_secret(setting.id, setting.secret_text)

        safe_aliases = [
            a
            for a in setting.aliases
            if is_unlocked or not ctx.is_secret_alias_or_tag(a, setting.id)
        ]
        alias_suffix = f"（又名：{'、'.join(safe_aliases)}）" if safe_aliases else ""

        lines: list[str] = [f"#### {setting.name}{alias_suffix} (`{setting.id}`)"]
        if setting.core_text.strip():
            lines.append(setting.core_text.strip())

        # 按需加载专题：蓝图显式点名 OR 蓝图正文中命中专题名
        requested_topics = {
            t.topic for t in bp.topics if t.entity_id == setting.id
        }
        scan_text = self._blueprint_scan_text(bp)
        for topic_name, topic_content in setting.topics.items():
            if topic_name in requested_topics or (topic_name and topic_name in scan_text):
                if topic_content.strip():
                    lines.append(f"**[专题: {topic_name}]**\n{topic_content.strip()}")

        if is_unlocked and setting.secret_text and setting.secret_text.strip():
            lines.append(f"**[本章解锁机密]**\n{setting.secret_text.strip()}")

        return "\n".join(lines)

    def _render_entity_entry(self, bundle: EntityBundle, *, ctx: _BuildContext) -> str:
        """渲染 Slot 3 单个实体的设定切片与脱敏动态状态截面。"""
        setting_part = self._render_setting_block(bundle.setting, ctx=ctx)
        lines: list[str] = [setting_part]

        state = bundle.state
        if state is None:
            return "\n".join(lines)

        bp = ctx.blueprint
        eid = bundle.setting.id
        is_pov = bp.pov is not None and eid == bp.pov
        is_unlocked = eid in bp.unlock_secrets
        restrict_inner = bp.pov is not None and not is_pov and not is_unlocked

        state_lines: list[str] = []
        if isinstance(state, CharacterState):
            if state.location:
                loc_name = self._resolve_entity_name(state.location, ctx)
                state_lines.append(f"- **当前位置**：{loc_name} (`{state.location}`)")
            if state.physical_mental:
                state_lines.append(f"- **身心状态**：{state.physical_mental}")
            if not restrict_inner and state.current_goals:
                safe_goals = [
                    g for g in state.current_goals if is_unlocked or not ctx.is_sensitive_text(g)
                ]
                if safe_goals:
                    state_lines.append(f"- **当前目标**：{'；'.join(safe_goals)}")

            if bp.pov is None or is_unlocked:
                eb = state.epistemic_bounds
                safe_unaware = [u for u in eb.unaware_of if not ctx.is_sensitive_text(u)]
                safe_mis = [m for m in eb.misbelieves if not ctx.is_sensitive_text(m)]
                if safe_unaware:
                    state_lines.append(f"- **认知未知 (`unaware_of`)**：{'；'.join(safe_unaware)}")
                if safe_mis:
                    state_lines.append(f"- **当前误判 (`misbelieves`)**：{'；'.join(safe_mis)}")

            ext_lines = self._render_safe_profile_ext(
                state.profile_ext,
                pov_id=bp.pov,
                restrict_inner=restrict_inner,
                is_unlocked=is_unlocked,
                ctx=ctx,
            )
            state_lines.extend(ext_lines)

        elif isinstance(state, FactionState):
            state_lines.append(f"- **势力状态**：{state.status.value}")
            if state.leader:
                leader_hidden = False
                if restrict_inner and state.leader not in bp.unlock_secrets and state.leader != bp.pov:
                    leader_name = self._resolve_entity_name(state.leader, ctx)
                    leader_secret = ctx.locked_secrets_by_entity.get(state.leader, "")
                    if (
                        bundle.setting.name in leader_secret
                        or ctx.is_sensitive_text(f"{leader_name}{bundle.setting.name}")
                        or (
                            ctx.pov_state is not None
                            and any(
                                leader_name in u and bundle.setting.name in u
                                for u in ctx.pov_state.epistemic_bounds.unaware_of
                            )
                        )
                    ):
                        leader_hidden = True
                if not leader_hidden:
                    leader_name = self._resolve_entity_name(state.leader, ctx)
                    state_lines.append(f"- **首领/实权人物**：{leader_name} (`{state.leader}`)")

            if state.territory:
                terr_names = [f"{self._resolve_entity_name(t, ctx)}(`{t}`)" for t in state.territory]
                state_lines.append(f"- **势力范围**：{'、'.join(terr_names)}")
            if state.current_goals:
                safe_goals = [
                    g for g in state.current_goals if is_unlocked or not ctx.is_sensitive_text(g)
                ]
                if safe_goals:
                    state_lines.append(f"- **近期动向**：{'；'.join(safe_goals)}")

            ext_lines = self._render_safe_profile_ext(
                state.profile_ext,
                pov_id=bp.pov,
                restrict_inner=restrict_inner,
                is_unlocked=is_unlocked,
                ctx=ctx,
            )
            state_lines.extend(ext_lines)

        if state_lines:
            lines.append("**[当前动态状态截面]**")
            lines.extend(state_lines)

        return "\n".join(lines)

    @staticmethod
    def _render_safe_profile_ext(
        profile_ext: dict[str, Any],
        *,
        pov_id: str | None,
        restrict_inner: bool,
        is_unlocked: bool,
        ctx: _BuildContext,
    ) -> list[str]:
        if not profile_ext:
            return []

        lines: list[str] = []
        hidden_keys = {"trump_cards", "clues_held", "suspicions"}

        for key, value in profile_ext.items():
            if restrict_inner and key in hidden_keys:
                continue

            if key == "disguises" and isinstance(value, list):
                visible_identities: list[str] = []
                for entry in value:
                    if not isinstance(entry, dict):
                        continue
                    ident = str(entry.get("identity") or "").strip()
                    known_by = entry.get("known_by") or []
                    if not ident:
                        continue
                    if restrict_inner:
                        known_by_pov = pov_id is not None and pov_id in known_by
                        if not known_by_pov and ctx.is_sensitive_text(ident):
                            continue
                    visible_identities.append(ident)
                if visible_identities:
                    lines.append(f"- **表面身份/马甲**：{'；'.join(visible_identities)}")
                continue

            if isinstance(value, list):
                cleaned_items: list[str] = []
                for item in value:
                    if isinstance(item, dict):
                        s = json.dumps(item, ensure_ascii=False)
                    else:
                        s = str(item).strip()
                    if not s:
                        continue
                    if not is_unlocked and ctx.is_sensitive_text(s):
                        continue
                    cleaned_items.append(s)
                if cleaned_items:
                    lines.append(f"- **{key}**：{'、'.join(cleaned_items)}")
            elif value is not None and str(value).strip():
                s_val = str(value).strip()
                if not is_unlocked and ctx.is_sensitive_text(s_val):
                    continue
                lines.append(f"- **{key}**：{s_val}")

        return lines

    # -- 槽位 4: 近期百字摘要 + 上章末尾原文 -----------------------------

    def _build_slot_4_items(self, ctx: _BuildContext) -> list[_SlotItem]:
        items: list[_SlotItem] = []
        order = 0
        ch_num = ctx.chapter_num
        budget = ctx.budget

        if ch_num <= 1:
            items.append(
                _SlotItem(
                    group="continuity_notice",
                    label="首章无前序衔接说明",
                    content="本章为第 1 章开篇，无前序章节摘要与上一章末尾正文。",
                    priority=60,
                    order=order,
                )
            )
            return items

        # 4.1 近期 N 章百字摘要（按远近赋优先级，较早章优先裁剪）
        if budget.recent_summary_count >= 1:
            summaries = ctx.indexer.get_recent_summaries(
                before_ch=ch_num,
                count=budget.recent_summary_count,
            )
            total_sum = len(summaries)
            for idx, rec in enumerate(summaries):
                # 越靠近上一章优先级越高：最旧 ~20，最近 ~50
                distance_from_prev = (total_sum - 1) - idx
                prio = max(15, 50 - distance_from_prev * 15)
                time_str = f"（{rec.in_world_time}）" if rec.in_world_time else ""
                text = f"- **第{rec.ch}章摘要**{time_str}：{rec.summary}"
                items.append(
                    _SlotItem(
                        group="recent_summaries",
                        label=f"第{rec.ch}章摘要",
                        content=text,
                        priority=prio,
                        order=order,
                    )
                )
                order += 1

        # 4.2 上一章定稿末尾原文（默认 800~1200 字，支持先收缩至 min_chars 再彻底裁剪）
        prev_ch = ch_num - 1
        prev_text = self._load_previous_manuscript_text(prev_ch)
        if prev_text is None:
            ctx.add_warning(f"未找到第 {prev_ch} 章定稿正文，无法注入上章末尾衔接片段")
        else:
            max_tail = self._slice_tail_excerpt(
                prev_text,
                min_chars=budget.tail_excerpt_min_chars,
                max_chars=budget.tail_excerpt_max_chars,
            )
            min_tail = self._slice_tail_excerpt(
                prev_text,
                min_chars=min(budget.tail_excerpt_min_chars, len(prev_text)),
                max_chars=max(1, budget.tail_excerpt_min_chars),
            )
            full_block = f"### 上一章（第{prev_ch}章）末尾正文衔接\n{max_tail}"
            compact_block = (
                f"### 上一章（第{prev_ch}章）末尾正文衔接\n{min_tail}"
                if len(min_tail) < len(max_tail)
                else None
            )
            items.append(
                _SlotItem(
                    group="tail_excerpt",
                    label=f"第{prev_ch}章末尾正文",
                    content=full_block,
                    priority=60,
                    order=order,
                    compact_content=compact_block,
                    compact_priority=40 if compact_block is not None else None,
                    compact_label=f"第{prev_ch}章末尾正文(缩减至{budget.tail_excerpt_min_chars}字)"
                    if compact_block is not None
                    else None,
                )
            )
            order += 1

        return items

    def _load_previous_manuscript_text(self, prev_ch: int) -> str | None:
        ms_root = resolve_manuscript_dir(self._book_dir)
        if not ms_root.is_dir():
            return None

        expected_stem = format_chapter(prev_ch, width=self._config.chapter_id_width)
        expected_zh_stem = format_chapter_stem(
            prev_ch, width=self._config.chapter_id_width, legacy=False
        )
        direct_candidates: list[Path] = [
            ms_root / f"{expected_zh_stem}.md",
            ms_root / f"{expected_stem}.md",
        ]
        if self._active_ctx is not None and self._active_ctx.blueprint.volume_id:
            vid = self._active_ctx.blueprint.volume_id
            vol_sub = resolve_volume_subdir(ms_root, vid)
            direct_candidates = [
                vol_sub / f"{expected_zh_stem}.md",
                vol_sub / f"{expected_stem}.md",
                ms_root / vid / f"{expected_zh_stem}.md",
                ms_root / vid / f"{expected_stem}.md",
                *direct_candidates,
            ]
        for cand in direct_candidates:
            if cand.is_file():
                return self._clean_manuscript_body(cand.read_text(encoding="utf-8"))

        all_files = self._scandir_md_files(ms_root)
        if not all_files:
            return None

        ch_stem_re = re.compile(rf"^(?:ch_?|第)0*{prev_ch}(?:$|[^0-9])", re.IGNORECASE)

        for path in all_files:
            if path.stem in (expected_stem, expected_zh_stem) or ch_stem_re.match(path.stem):
                return self._clean_manuscript_body(path.read_text(encoding="utf-8"))

        title_re = re.compile(rf"^\s*#\s*第\s*0*{prev_ch}\s*章", re.MULTILINE)
        for path in all_files:
            raw = path.read_text(encoding="utf-8")
            if title_re.search(raw):
                return self._clean_manuscript_body(raw)

        return None

    @staticmethod
    def _clean_manuscript_body(raw_text: str) -> str:
        _, body = split_frontmatter(raw_text)
        cleaned = _CHAPTER_HEADING_RE.sub("", body, count=1).strip()
        return cleaned if cleaned else body.strip()

    @staticmethod
    def _slice_tail_excerpt(text: str, *, min_chars: int, max_chars: int) -> str:
        clean = text.strip()
        if not clean:
            return ""
        if len(clean) <= max_chars:
            return clean

        window = clean[-max_chars:]
        search_limit = max(0, max_chars - min_chars)
        if search_limit > 0:
            search_zone = window[:search_limit]
            m = _SENTENCE_BOUNDARY_RE.search(search_zone)
            if m is not None and len(window) - m.end() >= min_chars:
                trimmed = window[m.end() :].lstrip()
                if len(trimmed) >= min_chars:
                    return trimmed
        return window.lstrip()

    # -- 槽位 5: 本章蓝图节拍 + 指定伏笔 + 禁止事项（永不可裁） ---------

    def _build_slot_5_items(self, ctx: _BuildContext) -> list[_SlotItem]:
        items: list[_SlotItem] = []
        order = 0
        bp = ctx.blueprint

        # 5.1 章节标题与分场节拍细纲
        beat_lines: list[str] = []
        title_str = f"《{bp.title}》" if bp.title else ""
        beat_lines.append(f"### 第 {bp.chapter} 章分场节拍细纲 {title_str}".rstrip())
        if bp.beats:
            for beat in sorted(bp.beats, key=lambda b: b.index):
                b_title = f"：{beat.title}" if beat.title else ""
                beat_lines.append(f"{beat.index}. **Scene {beat.index}{b_title}**")
                if beat.content:
                    beat_lines.append(f"   {beat.content}")
        else:
            beat_lines.append("- *（未显式拆分节拍，请按下方蓝图创作提示推进本章）*")

        if bp.body.strip():
            beat_lines.append("")
            beat_lines.append(bp.body.strip())

        items.append(
            _SlotItem(
                group="beats",
                label="本章分场节拍蓝图",
                content="\n".join(beat_lines),
                priority=100,
                order=order,
            )
        )
        order += 1

        # 5.2 本章指定伏笔动作（仅装载 bp.threads，非 resolve 物理屏蔽 truth_secret）
        if bp.threads:
            thr_lines: list[str] = ["### 本章指定伏笔动作"]
            action_labels = {
                ThreadAction.PLANT: "新埋 (plant)",
                ThreadAction.PROGRESS: "推进 (progress)",
                ThreadAction.RESOLVE: "回收揭晓 (resolve)",
            }
            for ref in bp.threads:
                trow = ctx.thread_rows.get(ref.thread_id)
                act_text = action_labels[ref.action]
                if trow is None:
                    if ref.action != ThreadAction.PLANT:
                        ctx.add_warning(
                            f"蓝图指定的伏笔 '{ref.thread_id}' 未在状态台账中找到"
                        )
                    thr_lines.append(f"- **{ref.thread_id}**（{act_text}）：本章待处理的伏笔线索")
                    continue

                thr = trow.thread
                thr_lines.append(f"- **{thr.title}** (`{thr.id}`)｜动作：**{act_text}**")
                if thr.public_clue:
                    thr_lines.append(f"  - 表面线索：`{thr.public_clue}`")
                if ref.action == ThreadAction.RESOLVE and thr.truth_secret:
                    thr_lines.append(f"  - **[本章揭晓真相底牌]**：{thr.truth_secret}")

            items.append(
                _SlotItem(
                    group="selected_threads",
                    label="本章指定伏笔动作",
                    content="\n".join(thr_lines),
                    priority=100,
                    order=order,
                )
            )
            order += 1

        # 5.3 本章禁止事项与隔离写作指令
        tail_lines: list[str] = []
        if bp.forbidden:
            tail_lines.append("### 本章禁止事项 (Negative Constraints)")
            for item_text in bp.forbidden:
                tail_lines.append(f"- [禁止] {item_text}")
            tail_lines.append("")
        tail_lines.append(
            "> **隔离执笔指令**：请严格依据本写作包中的分场节拍、POV 认知红线与文风规范，"
            "一气呵成输出完整章节正文草稿；禁止自行脑补写作包之外的设定或剧透未解锁隐秘。"
        )
        items.append(
            _SlotItem(
                group="forbidden",
                label="本章禁止事项与执笔指令",
                content="\n".join(tail_lines),
                priority=100,
                order=order,
            )
        )
        return items

    # ------------------------------------------------------------------
    # 两阶段 Token 预算水印裁剪与渲染组装
    # ------------------------------------------------------------------

    @staticmethod
    def _effective_item_priority(item: _SlotItem) -> int:
        if (
            not item.compacted
            and item.compact_content is not None
            and item.compact_priority is not None
        ):
            return item.compact_priority
        return item.priority

    def _trim_slots_to_budget(
        self,
        *,
        slot_items: dict[SlotName, list[_SlotItem]],
        slot_budgets: dict[SlotName, int],
        effective_budget: TokenBudgetConfig,
        dropped_by_slot: dict[SlotName, list[str]],
        ctx: _BuildContext,
    ) -> None:
        trim_slots = [SlotName(name) for name in effective_budget.trim_order]

        # Phase 1: 单槽预算超出时优先裁剪槽位内低优先级条目
        for slot_name in trim_slots:
            budget_limit = slot_budgets[slot_name]
            while slot_items[slot_name]:
                section_md, _ = self._render_slot_section(
                    slot_name=slot_name,
                    items=slot_items[slot_name],
                    budget=budget_limit,
                )
                if estimate_tokens(section_md) <= budget_limit:
                    break
                if not self._pop_lowest_item(slot_items[slot_name], dropped_by_slot[slot_name]):
                    break

        # Phase 2: 总预算仍超出时按 trim_order 顺序继续裁剪中间三槽
        while True:
            full_md, _, total_tokens = self._assemble_final_markdown(
                ctx=ctx,
                slot_items=slot_items,
                slot_budgets=slot_budgets,
                dropped_by_slot=dropped_by_slot,
            )
            if total_tokens <= effective_budget.total:
                break

            popped = False
            for slot_name in trim_slots:
                if self._pop_lowest_item(slot_items[slot_name], dropped_by_slot[slot_name]):
                    popped = True
                    break

            if not popped:
                # 所有可裁槽位均已清空，仍超限则保留完整首尾受保护槽位并发出告警
                ctx.add_warning(
                    f"受保护首尾槽位 (guardrails + beats) 本身已达 {total_tokens} Tokens，"
                    f"超出总预算 {effective_budget.total} Tokens，已按安全铁律完整保留"
                )
                break

        for slot_name, dropped in dropped_by_slot.items():
            if dropped:
                ctx.add_warning(
                    f"槽位 {slot_name.value} 因预算受限已裁剪 {len(dropped)} 项低优先级内容"
                )

    @staticmethod
    def _pop_lowest_item(items: list[_SlotItem], dropped_labels: list[str]) -> bool:
        if not items:
            return False

        # 选择最低优先级条目；同优先级时优先淘汰靠后/较次要条目 (-order)
        best_idx = min(
            range(len(items)),
            key=lambda idx: (
                PacketBuilder._effective_item_priority(items[idx]),
                -items[idx].order,
            ),
        )
        target = items[best_idx]
        if (
            not target.compacted
            and target.compact_content is not None
            and target.compact_priority is not None
        ):
            target.content = target.compact_content
            target.compacted = True
            dropped_labels.append(target.compact_label or f"{target.label}(精简)")
            return True

        removed = items.pop(best_idx)
        dropped_labels.append(removed.label)
        return True

    def _render_slot_body(self, slot_name: SlotName, items: Sequence[_SlotItem]) -> str:
        if not items:
            if slot_name in _PROTECTED_SLOTS:
                return "*（本槽位暂无条目）*"
            return "*（受 Token 预算限制，本槽位可选背景已精简省略）*"

        ordered = sorted(items, key=lambda it: it.order)
        blocks: list[str] = []
        current_group: str | None = None

        for it in ordered:
            if it.group == "historical_facts" and current_group != "historical_facts":
                blocks.append("### 关联历史原子事实锚点")
            elif it.group == "world_settings" and current_group != "world_settings":
                blocks.append("### 世界观、地理与规则切片")
            elif it.group == "entities" and current_group != "entities":
                blocks.append("### 本章出场角色、势力与关键道具")
            elif it.group == "relations" and current_group != "relations":
                blocks.append("### 当前章有效双时态关系边")
            elif it.group == "recent_summaries" and current_group != "recent_summaries":
                blocks.append("### 近期前序章节百字摘要")

            blocks.append(it.content)
            current_group = it.group

        return "\n\n".join(b for b in blocks if b.strip())

    def _render_slot_section(
        self,
        *,
        slot_name: SlotName,
        items: Sequence[_SlotItem],
        budget: int,
    ) -> tuple[str, int]:
        title = _SLOT_TITLES[slot_name]
        body = self._render_slot_body(slot_name, items)

        used = estimate_tokens(body)
        section_md = ""
        for _ in range(3):
            header = f"## [{title}] (预算 ~{budget} Tokens | 估算 {used} Tokens)"
            section_md = f"{header}\n\n{body}"
            measured = estimate_tokens(section_md)
            if measured == used:
                break
            used = measured

        return section_md, used

    def _assemble_final_markdown(
        self,
        *,
        ctx: _BuildContext,
        slot_items: dict[SlotName, list[_SlotItem]],
        slot_budgets: dict[SlotName, int],
        dropped_by_slot: dict[SlotName, list[str]],
    ) -> tuple[str, list[SlotReport], int]:
        slot_order = (
            SlotName.GUARDRAILS,
            SlotName.WORLD_AND_FACTS,
            SlotName.ENTITIES,
            SlotName.CONTINUITY,
            SlotName.BEATS,
        )

        slot_sections: list[str] = []
        slot_reports: list[SlotReport] = []
        for s_name in slot_order:
            s_budget = slot_budgets[s_name]
            section_md, used_tokens = self._render_slot_section(
                slot_name=s_name,
                items=slot_items[s_name],
                budget=s_budget,
            )
            slot_sections.append(section_md)
            dropped = dropped_by_slot[s_name]
            slot_reports.append(
                SlotReport(
                    name=s_name,
                    budget=s_budget,
                    used=used_tokens,
                    trimmed=bool(dropped),
                    dropped_items=dropped,
                )
            )

        body_all = "\n\n---\n\n".join(slot_sections)
        bp = ctx.blueprint
        ch_title = f" · {bp.title}" if bp.title else ""
        slot_summary = " | ".join(f"{sr.name.value}={sr.used}T" for sr in slot_reports)
        isolation_note = (
            f"已隔离机密 {len(ctx.blocked_secret_ids)} 项 | "
            f"已屏蔽未勾选伏笔 {len(ctx.blocked_thread_ids)} 项"
        )

        total_tokens = estimate_tokens(body_all)
        final_md = ""
        for _ in range(3):
            doc_header = (
                f"# 第 {ctx.chapter_num} 章单章独立写作包{ch_title}\n\n"
                f"> **总预算**：{ctx.budget.total} Tokens | **当前估算**：{total_tokens} Tokens | "
                f"**分槽估算**：{slot_summary}\n"
                f"> **物理隔离状态**：{isolation_note}（作者可直接打开本文件增删微调后交由执笔模型使用）"
            )
            final_md = f"{doc_header}\n\n---\n\n{body_all}\n"
            measured = estimate_tokens(final_md)
            if measured == total_tokens:
                break
            total_tokens = measured

        return final_md, slot_reports, total_tokens


def build_packet(
    book_dir: Path | str,
    chapter_num: int,
    custom_budget: int | TokenBudgetConfig | None = None,
) -> PacketBuildResult:
    """一步编译指定章节的单章独立写作包。"""
    with PacketBuilder(book_dir) as builder:
        return builder.build(chapter_num, custom_budget=custom_budget)


__all__ = [
    "PacketBuilder",
    "build_packet",
]
