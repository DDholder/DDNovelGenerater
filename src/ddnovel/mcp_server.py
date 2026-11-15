"""``ddnovel-mcp`` FastMCP Stdio 工具服务封装 (Task T13)。

设计原则：
1. 薄封装转发：所有核心业务逻辑均留在 ``ddnovel.core``，本模块仅负责参数归一化、
   ``book_dir`` 解析、调用四大业务引擎/索引中枢，以及格式化输出适合大模型消费的结构化文本；
2. 书库路径解析优先级：工具显式参数 ``book_dir`` > ``create_server(book_dir=...)`` 默认值 >
   环境变量 ``DDNOVEL_BOOK_DIR`` > 当前工作目录 ``Path.cwd()``；
3. 完整覆盖架构蓝图第 5 节（CLI 与 MCP 工具矩阵）全部 10 大非初始化功能域共 20 个 MCP Tool。
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import yaml
from pydantic import TypeAdapter, ValidationError

from ddnovel import __version__
from ddnovel.core import (
    ForeshadowRadar,
    ImpactAnalyzer,
    LifecycleManager,
    PacketBuilder,
    ProgressiveParser,
    SqliteIndexer,
    estimate_tokens,
    lint_model_setting_doc,
)
from ddnovel.core.parser import (
    CATEGORY_PREFIX_MAP,
    CATEGORY_SYNONYMS,
    STATUS_MAP,
    THREAD_ACTION_MAP,
)
from ddnovel.core.paths import (
    find_existing_blueprint_file,
    find_existing_setting_file,
    resolve_active_characters_file,
    resolve_active_factions_file,
    resolve_arc_file,
    resolve_archive_characters_file,
    resolve_archive_factions_file,
    resolve_blueprint_file,
    resolve_config_path,
    resolve_draft_file,
    resolve_facts_file,
    resolve_packet_file,
    resolve_relations_file,
    resolve_setting_file,
    resolve_settings_root,
    resolve_threads_file,
    resolve_volume_file,
    resolve_world_state_file,
)
from ddnovel.core.schemas import (
    ARC_ID_PATTERN,
    ENTITY_ID_PATTERN,
    THREAD_ID_PATTERN,
    VOLUME_ID_PATTERN,
    ArcDoc,
    Beat,
    BlueprintCast,
    BlueprintDoc,
    ChapterFactRecord,
    ChapterWindow,
    CharacterState,
    CharacterStateFile,
    EntityId,
    EntityStatus,
    FactionState,
    FactionStateFile,
    FtsScope,
    NovelConfig,
    RelationsFile,
    SettingCategory,
    SettingDoc,
    TemporalRelation,
    Thread,
    ThreadAction,
    ThreadId,
    ThreadRef,
    ThreadsFile,
    TopicRef,
    VolumeDoc,
    VolumeId,
    WorldState,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

BOOK_DIR_ENV_VAR: Final[str] = "DDNOVEL_BOOK_DIR"

MCP_TOOL_NAMES: Final[tuple[str, ...]] = (
    "novel_upsert_setting",
    "novel_list_settings",
    "novel_setting_impact",
    "novel_upsert_volume",
    "novel_upsert_arc",
    "novel_upsert_blueprint",
    "novel_foreshadow_radar",
    "novel_build_packet",
    "novel_read_packet",
    "novel_save_draft",
    "novel_promote_chapter",
    "novel_record_chapter_outcome",
    "novel_sync_index",
    "novel_search",
    "novel_inspect_entity",
    "novel_cold_archive",
    "novel_prune_unused",
    "novel_drop_thread",
    "novel_compact_history",
    "novel_audit_consistency",
)

_CATEGORY_DIR_MAP: Final[dict[SettingCategory, str]] = {
    SettingCategory.WORLD: "world",
    SettingCategory.RULE: "world",
    SettingCategory.GEOGRAPHY: "geography",
    SettingCategory.FACTION: "factions",
    SettingCategory.CHARACTER: "characters",
    SettingCategory.ITEM: "items",
    SettingCategory.OTHER: "other",
}

_ENTITY_ID_ADAPTER: Final[TypeAdapter[str]] = TypeAdapter(EntityId)
_VOLUME_ID_ADAPTER: Final[TypeAdapter[str]] = TypeAdapter(VolumeId)
_THREAD_ID_ADAPTER: Final[TypeAdapter[str]] = TypeAdapter(ThreadId)
_ENTITY_ID_RE: Final[re.Pattern[str]] = re.compile(ENTITY_ID_PATTERN)
_VOLUME_ID_RE: Final[re.Pattern[str]] = re.compile(VOLUME_ID_PATTERN)
_ARC_ID_RE: Final[re.Pattern[str]] = re.compile(ARC_ID_PATTERN)
_THREAD_ID_RE: Final[re.Pattern[str]] = re.compile(THREAD_ID_PATTERN)


# ---------------------------------------------------------------------------
# 1. 书库路径解析与 YAML / JSON 格式化辅助
# ---------------------------------------------------------------------------


def resolve_book_dir(
    book_dir: Path | str | None = None,
    default_book_dir: Path | str | None = None,
) -> Path:
    """按优先级解析目标小说书库根目录路径。

    优先级：
    1. 工具调用显式传入的 ``book_dir``
    2. ``create_server(book_dir=...)`` 绑定的默认目录
    3. 环境变量 ``DDNOVEL_BOOK_DIR``
    4. 当前工作目录 ``Path.cwd()``
    """
    if book_dir is not None and str(book_dir).strip():
        return Path(str(book_dir).strip()).expanduser().resolve()
    if default_book_dir is not None and str(default_book_dir).strip():
        return Path(str(default_book_dir).strip()).expanduser().resolve()
    env_val = os.environ.get(BOOK_DIR_ENV_VAR, "").strip()
    if env_val:
        return Path(env_val).expanduser().resolve()
    return Path.cwd().resolve()


def _load_config_or_default(book_dir: Path) -> NovelConfig:
    """加载书库 ``小说配置.yaml``（兼容 ``novel.yaml``）；若文件尚不存在则返回默认配置。"""
    cfg_path = resolve_config_path(book_dir)
    if cfg_path.is_file():
        return NovelConfig.load(book_dir)
    return NovelConfig(title=book_dir.name or "未命名小说")


def _dump_yaml(data: Any) -> str:
    """将结构化对象序列化为保持键序与中文可读性的 YAML 文本。"""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _render_frontmatter_md(frontmatter: Mapping[str, Any], body: str) -> str:
    """渲染含 YAML Frontmatter 与 Markdown 正文的文档文本。"""
    fm_yaml = _dump_yaml(dict(frontmatter)).rstrip("\n")
    clean_body = body.strip("\n")
    if clean_body:
        return f"---\n{fm_yaml}\n---\n\n{clean_body}\n"
    return f"---\n{fm_yaml}\n---\n"


def _format_response(payload: Mapping[str, Any]) -> str:
    """将结果字典格式化为兼具结构化字段与中文摘要的 UTF-8 JSON 文本。"""
    return json.dumps(dict(payload), ensure_ascii=False, indent=2)


def _format_error(exc: Exception, *, tool_name: str) -> str:
    """将领域层已知异常封装为结构化错误响应文本，避免宿主调用链崩溃。"""
    err_type = type(exc).__name__
    msg = str(exc)
    return _format_response(
        {
            "ok": False,
            "tool": tool_name,
            "error_type": err_type,
            "error": msg,
            "summary": f"❌ [{tool_name}] 执行失败 ({err_type}): {msg}",
        }
    )


def _normalize_category(raw_category: str | SettingCategory | None) -> SettingCategory:
    """将中英文分类名归一化为 ``SettingCategory`` 枚举。"""
    if raw_category is None:
        return SettingCategory.CHARACTER
    if isinstance(raw_category, SettingCategory):
        return raw_category
    cleaned = str(raw_category).strip().lower()
    if not cleaned:
        return SettingCategory.CHARACTER
    if cleaned in CATEGORY_SYNONYMS:
        return CATEGORY_SYNONYMS[cleaned]
    return SettingCategory(cleaned)


def _normalize_status(raw_status: str | EntityStatus | None) -> EntityStatus:
    """将中英文状态名归一化为 ``EntityStatus`` 枚举。"""
    if raw_status is None:
        return EntityStatus.ACTIVE
    if isinstance(raw_status, EntityStatus):
        return raw_status
    cleaned = str(raw_status).strip().lower()
    if not cleaned:
        return EntityStatus.ACTIVE
    if cleaned in STATUS_MAP:
        return STATUS_MAP[cleaned]
    return EntityStatus(cleaned)


def _normalize_entity_id_for_category(raw_id: str, category: SettingCategory) -> str:
    """校验或补全实体 ID 前缀，确保符合 ``ENTITY_ID_PATTERN``。"""
    cleaned = str(raw_id).strip().lower()
    if not cleaned:
        raise ValueError("entity_id 不能为空字符串")
    if _ENTITY_ID_RE.fullmatch(cleaned):
        return _ENTITY_ID_ADAPTER.validate_python(cleaned)
    slug = re.sub(r"[^a-z0-9_]+", "_", cleaned).strip("_")
    if not slug:
        raise ValueError(f"entity_id {raw_id!r} 无法规范化为合法的 ASCII 实体标识符")
    prefix = CATEGORY_PREFIX_MAP.get(category, "other_")
    candidate = slug if slug.startswith(prefix) else f"{prefix}{slug}"
    return _ENTITY_ID_ADAPTER.validate_python(candidate)


def _find_existing_setting_path(book_dir: Path, entity_id: str) -> Path | None:
    """若 ``01_设定集/`` 下已存在相同 ``entity_id`` 的设定文件，返回其路径以便原位更新。"""
    direct = find_existing_setting_file(book_dir, entity_id)
    if direct is not None:
        return direct
    settings_root = resolve_settings_root(book_dir)
    if not settings_root.is_dir():
        return None
    parser = ProgressiveParser(book_dir=book_dir)
    for md_path in sorted(settings_root.rglob("*.md")):
        if not md_path.is_file():
            continue
        try:
            doc = parser.parse_setting_md(md_path, book_dir=book_dir)
            if doc.id == entity_id:
                return md_path
        except Exception:
            continue
    return None


def _build_setting_markdown(
    *,
    entity_id: str,
    name: str,
    category: SettingCategory,
    status: EntityStatus,
    aliases: list[str],
    tags: list[str],
    core_text: str,
    topics: Mapping[str, str] | None,
    secret_text: str | None,
) -> str:
    """组装带三级渐进切片（``[核心]`` / ``[专题]`` / ``[机密]``）的设定 Markdown。"""
    frontmatter: dict[str, Any] = {
        "id": entity_id,
        "name": name,
        "aliases": aliases,
        "category": category.value,
        "status": status.value,
        "tags": tags,
    }
    sections: list[str] = [f"# {name}", "", "## [核心]", core_text.strip() or f"{name}的核心设定。"]
    if topics:
        for topic_name, topic_body in topics.items():
            clean_tname = str(topic_name).strip()
            clean_tbody = str(topic_body).strip()
            if clean_tname and clean_tbody:
                sections.extend(["", f"## [专题: {clean_tname}]", clean_tbody])
    if secret_text is not None and str(secret_text).strip():
        sections.extend(["", "## [机密]", str(secret_text).strip()])
    return _render_frontmatter_md(frontmatter, "\n".join(sections))


def _coerce_chapter_window(raw_range: Sequence[int] | None) -> ChapterWindow | None:
    """将 ``[start, end]`` 列表转换为 ``ChapterWindow`` 二元组。"""
    if raw_range is None:
        return None
    if len(raw_range) != 2:
        raise ValueError(f"chapter_range 必须为包含 [起始章, 结束章] 的二元整数列表，收到: {raw_range!r}")
    return TypeAdapter(ChapterWindow).validate_python((raw_range[0], raw_range[1]))


def _infer_volume_id_for_chapter(book_dir: Path, chapter: int) -> str:
    """根据已有分卷规划或正文中推断指定章号所属的 ``volume_id``。"""
    with LifecycleManager(book_dir, auto_sync=False) as mgr:
        return mgr._infer_volume_id(chapter)


def _lookup_entity_id(indexer: SqliteIndexer, id_or_name: str) -> str:
    """支持通过实体 ID、标准名称或外号别名反查规范 ``entity_id``。"""
    cleaned = id_or_name.strip()
    if not cleaned:
        raise ValueError("entity_id 不能为空字符串")
    row = indexer.conn.execute(
        "SELECT id FROM entities WHERE id = ?",
        (cleaned,),
    ).fetchone()
    if row is not None:
        return str(row[0])

    for sql in (
        "SELECT id FROM entities WHERE name = ? AND is_archived = 0 ORDER BY id ASC LIMIT 1",
        (
            "SELECT entity_id FROM entity_aliases "
            "WHERE alias = ? AND is_archived = 0 ORDER BY entity_id ASC LIMIT 1"
        ),
        "SELECT id FROM entities WHERE name = ? ORDER BY id ASC LIMIT 1",
        "SELECT entity_id FROM entity_aliases WHERE alias = ? ORDER BY entity_id ASC LIMIT 1",
    ):
        found = indexer.conn.execute(sql, (cleaned,)).fetchone()
        if found is not None:
            return str(found[0])
    return cleaned


# ---------------------------------------------------------------------------
# 2. FastMCP 服务工厂与 20 个领域工具注册
# ---------------------------------------------------------------------------


def create_server(book_dir: Path | str | None = None) -> FastMCP:
    """创建并注册 ``ddnovel`` FastMCP 工具服务实例。

    延迟导入 ``FastMCP``，避免 ``import ddnovel`` 时加载重量级依赖。
    """
    from fastmcp import FastMCP

    default_book_dir = (
        Path(book_dir).expanduser().resolve()
        if book_dir is not None and str(book_dir).strip()
        else None
    )
    mcp = FastMCP("ddnovel")

    # ------------------------------------------------------------------
    # 功能域 2: 设定维护 (novel_upsert_setting / novel_list_settings)
    # ------------------------------------------------------------------

    @mcp.tool(name="novel_upsert_setting")
    async def novel_upsert_setting(
        entity_id: str,
        name: str,
        category: str = "character",
        core_text: str = "",
        topics: dict[str, str] | None = None,
        secret_text: str | None = None,
        aliases: list[str] | None = None,
        status: str = "active",
        tags: list[str] | None = None,
        sync_index: bool = True,
        book_dir: str | None = None,
    ) -> str:
        """新建或更新模型侧静态设定卡片（01_设定集/<分类>/<id>-<对象名>.md），
        支持 [核心]/[专题]/[机密] 三级渐进切片并自动热同步索引。

        注意：本工具面向模型创作层（01_设定集/），写入前应确保已完成“三留三剥离”抽象提炼，
        勿混入作者随笔、备忘、具象示范场景、台词例句或动作指令清单（此类内容应留在 00_作者设定/）。
        """
        try:
            root = resolve_book_dir(book_dir, default_book_dir)
            cat_enum = _normalize_category(category)
            status_enum = _normalize_status(status)
            norm_id = _normalize_entity_id_for_category(entity_id, cat_enum)
            clean_name = name.strip()
            if not clean_name:
                raise ValueError("设定名称 name 不能为空字符串")

            existing_path = _find_existing_setting_path(root, norm_id)
            target_path = resolve_setting_file(
                root,
                norm_id,
                cat_enum,
                name=clean_name,
                for_write=True,
            )

            md_text = _build_setting_markdown(
                entity_id=norm_id,
                name=clean_name,
                category=cat_enum,
                status=status_enum,
                aliases=list(aliases or []),
                tags=list(tags or []),
                core_text=core_text,
                topics=topics,
                secret_text=secret_text,
            )
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(md_text, encoding="utf-8")
            if (
                existing_path is not None
                and existing_path.is_file()
                and not existing_path.samefile(target_path)
            ):
                existing_path.unlink(missing_ok=True)

            parser = ProgressiveParser(book_dir=root)
            parsed_doc: SettingDoc = parser.parse_setting_md(target_path, book_dir=root)
            abstraction_warnings = lint_model_setting_doc(parsed_doc)

            sync_report_dict: dict[str, Any] | None = None
            if sync_index:
                with SqliteIndexer(root) as indexer:
                    sync_report_dict = indexer.sync().model_dump(mode="json")

            rel_path = target_path.resolve().relative_to(root).as_posix()
            summary = (
                f"✅ 已保存设定卡 `{parsed_doc.id}` ({parsed_doc.name}, "
                f"分类={parsed_doc.category.value}) -> `{rel_path}`"
            )
            if abstraction_warnings:
                summary += (
                    f"（⚠️ 检测到 {len(abstraction_warnings)} 项具象台词/场景/动作泄露风险，"
                    "请移入 00_作者设定/ 并提炼为抽象特征）"
                )
            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_upsert_setting",
                    "action": "updated" if existing_path is not None else "created",
                    "file_path": rel_path,
                    "setting": parsed_doc.model_dump(mode="json"),
                    "abstraction_warnings": abstraction_warnings,
                    "sync_report": sync_report_dict,
                    "summary": summary,
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
            return _format_error(exc, tool_name="novel_upsert_setting")

    @mcp.tool(name="novel_list_settings")
    async def novel_list_settings(
        category: str | None = None,
        include_archived: bool = False,
        include_secret: bool = False,
        book_dir: str | None = None,
    ) -> str:
        """列出书库中的设定卡片清单（支持按分类与冷库归档状态过滤，默认物理隐藏 [机密] 文本）。"""
        try:
            root = resolve_book_dir(book_dir, default_book_dir)
            cat_filter = _normalize_category(category) if category is not None and category.strip() else None

            with SqliteIndexer(root) as indexer:
                indexer.sync()
                docs = indexer.list_entities(
                    category=cat_filter,
                    include_archived=include_archived,
                )

            items: list[dict[str, Any]] = []
            for doc in docs:
                dumped = doc.model_dump(mode="json")
                dumped["has_secret"] = bool(doc.secret_text and doc.secret_text.strip())
                dumped["topic_names"] = list(doc.topics.keys())
                if not include_secret:
                    dumped["secret_text"] = None
                items.append(dumped)

            cat_label = cat_filter.value if cat_filter is not None else "all"
            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_list_settings",
                    "category": cat_label,
                    "include_archived": include_archived,
                    "count": len(items),
                    "items": items,
                    "summary": (
                        f"📋 共检索到 {len(items)} 条设定记录 "
                        f"(分类={cat_label}, 包含归档={include_archived})"
                    ),
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
            return _format_error(exc, tool_name="novel_list_settings")

    # ------------------------------------------------------------------
    # 功能域 3: 设定影响面 (novel_setting_impact)
    # ------------------------------------------------------------------

    @mcp.tool(name="novel_setting_impact")
    async def novel_setting_impact(
        target_id_or_keyword: str,
        after_chapter: int | None = None,
        include_archived_state: bool = False,
        book_dir: str | None = None,
    ) -> str:
        """中途改/删设定防吃书五维影响面扫描（扫描已定稿正文、历史事实、活跃状态、未完结伏笔与后续蓝图）。"""
        try:
            root = resolve_book_dir(book_dir, default_book_dir)
            with ImpactAnalyzer(root) as analyzer:
                report = analyzer.scan_impact(
                    target_id_or_keyword,
                    after_chapter=after_chapter,
                    include_archived_state=include_archived_state,
                )
            dumped = report.model_dump(mode="json")
            scope_counts: dict[str, int] = {}
            for hit in report.hits:
                scope_counts[hit.scope.value] = scope_counts.get(hit.scope.value, 0) + 1

            safety_label = (
                "安全可直接修改/删除"
                if report.is_safe_to_change
                else f"存在 {len(report.hits)} 处关联引用需注意圆场"
            )
            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_setting_impact",
                    **dumped,
                    "hit_count": len(report.hits),
                    "scope_counts": scope_counts,
                    "summary": (
                        f"🔍 设定影响面扫描 `{report.target}` "
                        f"(展开词条: {', '.join(report.expanded_terms)})：{safety_label}"
                    ),
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
            return _format_error(exc, tool_name="novel_setting_impact")

    # ------------------------------------------------------------------
    # 功能域 4: 结构与蓝图 (novel_upsert_volume / novel_upsert_arc / novel_upsert_blueprint)
    # ------------------------------------------------------------------

    @mcp.tool(name="novel_upsert_volume")
    async def novel_upsert_volume(
        volume_id: str,
        title: str = "",
        chapter_range: list[int] | None = None,
        core_conflict: str = "",
        folded_summary: str | None = None,
        body: str = "",
        sync_index: bool = True,
        book_dir: str | None = None,
    ) -> str:
        """创建或更新分卷规划文档（02_结构/分卷/第XX卷-<卷名>.md，支持完结卷级折叠大事记）。

        粒度约束：分卷规划只到剧情单元（Arc）划分与定位，严禁在分卷正文中越级设计具体章节内容；具体章节排布归属单元设计（novel_upsert_arc）。
        """
        try:
            root = resolve_book_dir(book_dir, default_book_dir)
            vid = volume_id.strip().lower()
            if not _VOLUME_ID_RE.fullmatch(vid):
                raise ValueError(f"volume_id 必须符合 'vol_XX' 格式（如 'vol_01'），收到: {volume_id!r}")

            existing_vol = resolve_volume_file(root, vid, for_write=False)
            win = _coerce_chapter_window(chapter_range)
            vol_doc = VolumeDoc(
                id=vid,
                title=title.strip(),
                chapter_range=win,
                core_conflict=core_conflict.strip(),
                folded_summary=folded_summary.strip() if folded_summary and folded_summary.strip() else None,
                body=body.strip(),
            )
            frontmatter: dict[str, Any] = {
                "id": vol_doc.id,
                "title": vol_doc.title,
                "chapter_range": list(vol_doc.chapter_range) if vol_doc.chapter_range else None,
                "core_conflict": vol_doc.core_conflict,
                "folded_summary": vol_doc.folded_summary,
            }
            vol_path = resolve_volume_file(
                root,
                vol_doc.id,
                title=vol_doc.title or "",
                for_write=True,
            )
            default_body = (
                vol_doc.body or f"# {vol_doc.title or vol_doc.id}\n\n{vol_doc.core_conflict}".strip()
            )
            vol_path.parent.mkdir(parents=True, exist_ok=True)
            vol_path.write_text(_render_frontmatter_md(frontmatter, default_body), encoding="utf-8")
            if existing_vol.is_file() and not existing_vol.samefile(vol_path):
                existing_vol.unlink(missing_ok=True)

            if sync_index:
                with SqliteIndexer(root) as indexer:
                    indexer.sync()

            rel_path = vol_path.resolve().relative_to(root).as_posix()
            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_upsert_volume",
                    "file_path": rel_path,
                    "volume": vol_doc.model_dump(mode="json"),
                    "summary": f"✅ 已保存分卷规划 `{vol_doc.id}` ({vol_doc.title}) -> `{rel_path}`",
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
            return _format_error(exc, tool_name="novel_upsert_volume")

    @mcp.tool(name="novel_upsert_arc")
    async def novel_upsert_arc(
        arc_id: str,
        volume_id: str | None = None,
        title: str = "",
        chapter_range: list[int] | None = None,
        folded_summary: str | None = None,
        body: str = "",
        sync_index: bool = True,
        book_dir: str | None = None,
    ) -> str:
        """创建或更新剧情单元文档（02_结构/单元/第XX卷_第YY单元-<单元名>.md，含起承转合、章节内容排布与单元折叠摘要）。"""
        try:
            root = resolve_book_dir(book_dir, default_book_dir)
            aid = arc_id.strip().lower()
            if not _ARC_ID_RE.fullmatch(aid):
                raise ValueError(
                    f"arc_id 必须符合 'vol_XX_arc_YY' 格式（如 'vol_01_arc_01'），收到: {arc_id!r}"
                )
            existing_arc = resolve_arc_file(root, aid, for_write=False)
            vid = volume_id.strip().lower() if volume_id and volume_id.strip() else None
            win = _coerce_chapter_window(chapter_range)
            arc_doc = ArcDoc(
                id=aid,
                volume_id=vid,
                title=title.strip(),
                chapter_range=win,
                folded_summary=folded_summary.strip() if folded_summary and folded_summary.strip() else None,
                body=body.strip(),
            )
            frontmatter: dict[str, Any] = {
                "id": arc_doc.id,
                "volume_id": arc_doc.volume_id,
                "title": arc_doc.title,
                "chapter_range": list(arc_doc.chapter_range) if arc_doc.chapter_range else None,
                "folded_summary": arc_doc.folded_summary,
            }
            arc_path = resolve_arc_file(
                root,
                arc_doc.id,
                title=arc_doc.title or "",
                for_write=True,
            )
            default_body = arc_doc.body or f"# {arc_doc.title or arc_doc.id}".strip()
            arc_path.parent.mkdir(parents=True, exist_ok=True)
            arc_path.write_text(_render_frontmatter_md(frontmatter, default_body), encoding="utf-8")
            if existing_arc.is_file() and not existing_arc.samefile(arc_path):
                existing_arc.unlink(missing_ok=True)

            if sync_index:
                with SqliteIndexer(root) as indexer:
                    indexer.sync()

            rel_path = arc_path.resolve().relative_to(root).as_posix()
            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_upsert_arc",
                    "file_path": rel_path,
                    "arc": arc_doc.model_dump(mode="json"),
                    "summary": f"✅ 已保存单元规划 `{arc_doc.id}` ({arc_doc.title}) -> `{rel_path}`",
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
            return _format_error(exc, tool_name="novel_upsert_arc")

    @mcp.tool(name="novel_upsert_blueprint")
    async def novel_upsert_blueprint(
        chapter: int,
        volume_id: str | None = None,
        arc_id: str | None = None,
        title: str | None = None,
        pov: str | None = None,
        characters: list[str] | None = None,
        locations: list[str] | None = None,
        factions: list[str] | None = None,
        items: list[str] | None = None,
        threads: list[dict[str, str]] | None = None,
        unlock_secrets: list[str] | None = None,
        topics: list[dict[str, str]] | None = None,
        forbidden: list[str] | None = None,
        beats: list[dict[str, Any]] | None = None,
        body: str = "",
        sync_index: bool = True,
        book_dir: str | None = None,
    ) -> str:
        """创建或更新单章分场节拍蓝图（02_结构/章节蓝图/第XX卷/第XXXX章-<章节标题>.md）。"""
        try:
            if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 1:
                raise ValueError(f"chapter 必须为 >= 1 的整数，收到: {chapter!r}")

            root = resolve_book_dir(book_dir, default_book_dir)
            cfg = _load_config_or_default(root)
            existing_bp = find_existing_blueprint_file(root, chapter, width=cfg.chapter_id_width)

            resolved_vol = (
                volume_id.strip().lower()
                if volume_id and volume_id.strip()
                else (
                    arc_id.strip().lower().split("_arc_", maxsplit=1)[0]
                    if arc_id and "_arc_" in arc_id
                    else _infer_volume_id_for_chapter(root, chapter)
                )
            )
            resolved_arc = arc_id.strip().lower() if arc_id and arc_id.strip() else None

            thread_refs: list[ThreadRef] = []
            for raw_thr in threads or []:
                tid = str(raw_thr.get("thread_id") or raw_thr.get("id") or "").strip()
                raw_act = str(raw_thr.get("action") or "progress").strip().lower()
                act = THREAD_ACTION_MAP.get(raw_act) or ThreadAction(raw_act)
                thread_refs.append(ThreadRef(thread_id=tid, action=act))

            topic_refs: list[TopicRef] = []
            for raw_top in topics or []:
                eid = str(raw_top.get("entity_id") or raw_top.get("id") or "").strip()
                tname = str(raw_top.get("topic") or raw_top.get("topic_name") or "").strip()
                topic_refs.append(TopicRef(entity_id=eid, topic=tname))

            beat_objs: list[Beat] = []
            for idx, raw_beat in enumerate(beats or [], start=1):
                if isinstance(raw_beat, Mapping):
                    b_idx = int(raw_beat.get("index", idx))
                    b_title = str(raw_beat.get("title") or "").strip()
                    b_content = str(raw_beat.get("content") or "").strip()
                    beat_objs.append(Beat(index=b_idx, title=b_title, content=b_content))
                else:
                    beat_objs.append(Beat(index=idx, title=f"场景 {idx}", content=str(raw_beat).strip()))

            bp_doc = BlueprintDoc(
                chapter=chapter,
                volume_id=resolved_vol,
                arc_id=resolved_arc,
                title=title.strip() if title and title.strip() else f"第{chapter}章",
                pov=pov.strip() if pov and pov.strip() else None,
                cast=BlueprintCast(
                    characters=list(characters or []),
                    locations=list(locations or []),
                    factions=list(factions or []),
                    items=list(items or []),
                ),
                threads=thread_refs,
                unlock_secrets=list(unlock_secrets or []),
                topics=topic_refs,
                forbidden=list(forbidden or []),
                beats=beat_objs,
                body=body.strip(),
            )

            frontmatter: dict[str, Any] = {
                "chapter": bp_doc.chapter,
                "volume_id": bp_doc.volume_id,
                "arc_id": bp_doc.arc_id,
                "title": bp_doc.title,
                "pov": bp_doc.pov,
                "cast": bp_doc.cast.model_dump(mode="json"),
                "threads": [t.model_dump(mode="json") for t in bp_doc.threads],
                "unlock_secrets": list(bp_doc.unlock_secrets),
                "topics": [t.model_dump(mode="json") for t in bp_doc.topics],
                "forbidden": list(bp_doc.forbidden),
                "beats": [b.model_dump(mode="json") for b in bp_doc.beats],
            }

            bp_path = resolve_blueprint_file(
                root,
                resolved_vol,
                chapter,
                width=cfg.chapter_id_width,
                title=bp_doc.title or "",
                for_write=True,
            )
            bp_path.parent.mkdir(parents=True, exist_ok=True)
            bp_body = bp_doc.body or f"# 第 {chapter} 章：{bp_doc.title}"
            bp_path.write_text(_render_frontmatter_md(frontmatter, bp_body), encoding="utf-8")
            if (
                existing_bp is not None
                and existing_bp.is_file()
                and not existing_bp.samefile(bp_path)
            ):
                existing_bp.unlink(missing_ok=True)

            parsed_bp = ProgressiveParser(book_dir=root).parse_blueprint_md(bp_path, book_dir=root)

            if sync_index:
                with SqliteIndexer(root) as indexer:
                    indexer.sync()

            rel_path = bp_path.resolve().relative_to(root).as_posix()
            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_upsert_blueprint",
                    "file_path": rel_path,
                    "blueprint": parsed_bp.model_dump(mode="json"),
                    "summary": (
                        f"✅ 已保存第 {chapter} 章蓝图 `{ parsed_bp.title }` "
                        f"(POV={parsed_bp.pov or '无'}, 节拍数={len(parsed_bp.beats)}) -> `{rel_path}`"
                    ),
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
            return _format_error(exc, tool_name="novel_upsert_blueprint")

    # ------------------------------------------------------------------
    # 功能域 5: 伏笔雷达 (novel_foreshadow_radar)
    # ------------------------------------------------------------------

    @mcp.tool(name="novel_foreshadow_radar")
    async def novel_foreshadow_radar(
        target_chapter: int | None = None,
        planned_entities: list[str] | None = None,
        stale_threshold: int | None = None,
        include_blueprint_entities: bool = True,
        book_dir: str | None = None,
    ) -> str:
        """运行规划期悬线伏笔三类雷达巡检（窗口到期 / 超期断线 / 出场实体联动，忽略冷库与已填伏笔）。"""
        try:
            root = resolve_book_dir(book_dir, default_book_dir)
            with ForeshadowRadar(root) as radar:
                report = radar.scan(
                    target_chapter=target_chapter,
                    planned_entities=planned_entities,
                    stale_threshold=stale_threshold,
                    include_blueprint_entities=include_blueprint_entities,
                )
            dumped = report.model_dump(mode="json")
            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_foreshadow_radar",
                    **dumped,
                    "hit_count": len(report.hits),
                    "summary": (
                        f"📡 第 {report.target_chapter} 章伏笔雷达巡检完成："
                        f"在 {report.total_active_threads} 条活跃伏笔中命中 {len(report.hits)} 条候选伏笔"
                    ),
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError, TypeError) as exc:
            return _format_error(exc, tool_name="novel_foreshadow_radar")

    # ------------------------------------------------------------------
    # 功能域 6: 写作包输出 (novel_build_packet / novel_read_packet)
    # ------------------------------------------------------------------

    @mcp.tool(name="novel_build_packet")
    async def novel_build_packet(
        chapter: int,
        budget: int | None = None,
        include_content: bool = False,
        book_dir: str | None = None,
    ) -> str:
        """一步直出编译单章独立写作包（04_创作/写作包/第XXXX章_写作包.md，执行三重物理隔离与预算裁剪）。"""
        try:
            root = resolve_book_dir(book_dir, default_book_dir)
            with PacketBuilder(root) as builder:
                build_result = builder.build(chapter_num=chapter, custom_budget=budget)

            dumped = build_result.model_dump(mode="json")
            if include_content:
                pkt_abs = root / build_result.packet_path
                dumped["content"] = pkt_abs.read_text(encoding="utf-8") if pkt_abs.is_file() else ""

            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_build_packet",
                    **dumped,
                    "summary": (
                        f"📦 第 {build_result.chapter} 章写作包已生成 -> `{build_result.packet_path}` "
                        f"(总 Token: {build_result.total_tokens}/{build_result.budget_total}, "
                        f"隔离机密实体: {len(build_result.blocked_secrets)}, "
                        f"屏蔽休眠伏笔: {len(build_result.blocked_threads)})"
                    ),
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError, TypeError) as exc:
            return _format_error(exc, tool_name="novel_build_packet")

    @mcp.tool(name="novel_read_packet")
    async def novel_read_packet(
        chapter: int,
        auto_build: bool = True,
        book_dir: str | None = None,
    ) -> str:
        """读取指定章节已编译的单章独立写作包全文（供 dd-chapter-writer 在上下文隔离模式下唯一读取）。"""
        try:
            if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 1:
                raise ValueError(f"chapter 必须为 >= 1 的整数，收到: {chapter!r}")

            root = resolve_book_dir(book_dir, default_book_dir)
            cfg = _load_config_or_default(root)
            packet_abs = resolve_packet_file(root, chapter, width=cfg.chapter_id_width)
            packet_rel = packet_abs.relative_to(root).as_posix()

            built_on_demand = False
            if not packet_abs.is_file() and auto_build:
                with PacketBuilder(root) as builder:
                    res = builder.build(chapter_num=chapter)
                    packet_rel = res.packet_path
                    packet_abs = root / packet_rel
                    built_on_demand = True

            if not packet_abs.is_file():
                raise FileNotFoundError(
                    f"未找到第 {chapter} 章写作包文件: {packet_rel} "
                    "（请先编写章节蓝图并调用 novel_build_packet）"
                )

            content = packet_abs.read_text(encoding="utf-8")
            tokens = estimate_tokens(content)
            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_read_packet",
                    "chapter": chapter,
                    "packet_path": packet_rel,
                    "built_on_demand": built_on_demand,
                    "token_estimate": tokens,
                    "content": content,
                    "summary": (
                        f"📖 已读取第 {chapter} 章独立写作包 `{packet_rel}` "
                        f"(约 {tokens} Tokens)"
                    ),
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
            return _format_error(exc, tool_name="novel_read_packet")

    # ------------------------------------------------------------------
    # 功能域 7: 草稿创作 (novel_save_draft)
    # ------------------------------------------------------------------

    @mcp.tool(name="novel_save_draft")
    async def novel_save_draft(
        chapter: int,
        content: str,
        book_dir: str | None = None,
    ) -> str:
        """保存整章草稿正文至 04_创作/草稿/第XXXX章_草稿.md，供作者一次性通篇审阅修改。"""
        try:
            if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 1:
                raise ValueError(f"chapter 必须为 >= 1 的整数，收到: {chapter!r}")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("草稿正文 content 不能为空字符串")

            root = resolve_book_dir(book_dir, default_book_dir)
            cfg = _load_config_or_default(root)
            draft_abs = resolve_draft_file(
                root, chapter, width=cfg.chapter_id_width, for_write=True
            )
            draft_rel = draft_abs.relative_to(root).as_posix()

            clean_content = content.strip() + "\n"
            draft_abs.parent.mkdir(parents=True, exist_ok=True)
            draft_abs.write_text(clean_content, encoding="utf-8")

            char_count = len(clean_content.strip())
            tokens = estimate_tokens(clean_content)
            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_save_draft",
                    "chapter": chapter,
                    "draft_path": draft_rel,
                    "char_count": char_count,
                    "token_estimate": tokens,
                    "summary": (
                        f"✍️ 已保存第 {chapter} 章草稿 -> `{draft_rel}` "
                        f"(共 {char_count} 字符, 约 {tokens} Tokens)"
                    ),
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
            return _format_error(exc, tool_name="novel_save_draft")

    # ------------------------------------------------------------------
    # 功能域 8: 定稿与结算 (novel_promote_chapter / novel_record_chapter_outcome)
    # ------------------------------------------------------------------

    @mcp.tool(name="novel_promote_chapter")
    async def novel_promote_chapter(
        chapter: int,
        volume_id: str | None = None,
        run_audit: bool = False,
        dry_run: bool = False,
        book_dir: str | None = None,
    ) -> str:
        """将审校通过的章节草稿正式定稿归档至 04_创作/定稿/，并自动触发超期旧写作包/旧草稿 TTL 清理。"""
        try:
            root = resolve_book_dir(book_dir, default_book_dir)
            with LifecycleManager(root) as mgr:
                result = mgr.promote_chapter(
                    chapter_num=chapter,
                    volume_id=volume_id,
                    dry_run=dry_run,
                    run_audit=run_audit,
                )
            dumped = result.model_dump(mode="json")
            mode_tag = "[预览模式]" if dry_run else "[已落盘]"
            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_promote_chapter",
                    **dumped,
                    "summary": (
                        f"🏛️ {mode_tag} 第 {result.chapter} 章已定稿归档：`{result.draft_path}` -> "
                        f"`{result.manuscript_path}` (TTL 清理过程文件: {len(result.cleaned_paths)} 个)"
                    ),
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
            return _format_error(exc, tool_name="novel_promote_chapter")

    @mcp.tool(name="novel_record_chapter_outcome")
    async def novel_record_chapter_outcome(
        chapter: int,
        summary: str,
        facts: list[str],
        entities: list[str] | None = None,
        volume_id: str | None = None,
        arc_id: str | None = None,
        in_world_time: str | None = None,
        macro_situation: str | None = None,
        character_updates: dict[str, dict[str, Any]] | None = None,
        faction_updates: dict[str, dict[str, Any]] | None = None,
        relation_updates: list[dict[str, Any]] | None = None,
        thread_updates: list[dict[str, Any]] | None = None,
        sync_index: bool = True,
        book_dir: str | None = None,
    ) -> str:
        """沉淀单章百字摘要与原子事实（03_状态/历史/第XX卷_事实.jsonl），并覆写更新活跃状态截面、关系网与伏笔台账。"""
        try:
            if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 1:
                raise ValueError(f"chapter 必须为 >= 1 的整数，收到: {chapter!r}")

            root = resolve_book_dir(book_dir, default_book_dir)
            cfg = _load_config_or_default(root)
            genre_ctx = {"genre_profile": cfg.genre_profile}

            resolved_vol = (
                volume_id.strip().lower()
                if volume_id and volume_id.strip()
                else (
                    arc_id.strip().lower().split("_arc_", maxsplit=1)[0]
                    if arc_id and "_arc_" in arc_id
                    else _infer_volume_id_for_chapter(root, chapter)
                )
            )
            resolved_arc = arc_id.strip().lower() if arc_id and arc_id.strip() else None

            fact_record = ChapterFactRecord(
                ch=chapter,
                volume_id=resolved_vol,
                arc=resolved_arc,
                time=in_world_time.strip() if in_world_time and in_world_time.strip() else None,
                summary=summary.strip(),
                facts=list(facts),
                entities=list(entities or []),
            )

            updated_files: list[str] = []

            # 1) 写入/更新 03_状态/历史/<第XX卷>_事实.jsonl（按章号幂等覆写并保序）
            history_path = resolve_facts_file(root, resolved_vol, for_write=True)
            history_path.parent.mkdir(parents=True, exist_ok=True)
            existing_records: dict[int, ChapterFactRecord] = {}
            if history_path.is_file():
                for raw_line in history_path.read_text(encoding="utf-8").splitlines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    rec = ChapterFactRecord.model_validate_json(line)
                    existing_records[rec.ch] = rec
            existing_records[chapter] = fact_record
            sorted_lines = [
                existing_records[ch_key].model_dump_json(by_alias=True, exclude_none=True)
                for ch_key in sorted(existing_records.keys())
            ]
            history_path.write_text("\n".join(sorted_lines) + "\n", encoding="utf-8")
            history_rel = history_path.resolve().relative_to(root).as_posix()
            updated_files.append(history_rel)

            # 2) 更新 03_状态/世界状态.yaml 与 小说配置.yaml 当前章水位
            ws_path = resolve_world_state_file(root, for_write=True)
            raw_ws = yaml.safe_load(ws_path.read_text(encoding="utf-8")) if ws_path.is_file() else {}
            ws_obj = WorldState.model_validate(raw_ws if isinstance(raw_ws, dict) else {})
            new_ws = ws_obj.model_copy(
                update={
                    "current_chapter": max(ws_obj.current_chapter, chapter),
                    "in_world_time": (
                        in_world_time.strip()
                        if in_world_time is not None and in_world_time.strip()
                        else ws_obj.in_world_time
                    ),
                    "macro_situation": (
                        macro_situation.strip()
                        if macro_situation is not None and macro_situation.strip()
                        else ws_obj.macro_situation
                    ),
                }
            )
            ws_path.parent.mkdir(parents=True, exist_ok=True)
            ws_path.write_text(
                _dump_yaml(new_ws.model_dump(mode="json", by_alias=True, exclude_none=True)),
                encoding="utf-8",
            )
            updated_files.append(ws_path.resolve().relative_to(root).as_posix())

            cfg_path = resolve_config_path(root, for_write=True)
            if cfg_path.is_file() and chapter > cfg.current_chapter:
                cfg = cfg.model_copy(update={"current_chapter": chapter})
                cfg.dump(root)
                updated_files.append(cfg_path.name)

            # 3) 按需覆写角色状态截面 03_状态/当前/人物状态.yaml（含冷库唤醒）
            if character_updates:
                chars_path = resolve_active_characters_file(root, for_write=True)
                raw_chars = (
                    yaml.safe_load(chars_path.read_text(encoding="utf-8"))
                    if chars_path.is_file()
                    else {}
                )
                chars_dict: dict[str, Any] = dict(raw_chars) if isinstance(raw_chars, dict) else {}

                arch_chars_path = resolve_archive_characters_file(root, for_write=True)
                raw_arch_chars = (
                    yaml.safe_load(arch_chars_path.read_text(encoding="utf-8"))
                    if arch_chars_path.is_file()
                    else {}
                )
                arch_chars_dict: dict[str, Any] = (
                    dict(raw_arch_chars) if isinstance(raw_arch_chars, dict) else {}
                )
                arch_chars_modified = False

                for cid, patch in character_updates.items():
                    prev = chars_dict.get(cid)
                    if prev is None and cid in arch_chars_dict:
                        prev = arch_chars_dict[cid]
                    if cid in arch_chars_dict:
                        del arch_chars_dict[cid]
                        arch_chars_modified = True
                    merged = {**(prev if isinstance(prev, dict) else {}), **dict(patch)}
                    merged.setdefault("updated_at_ch", chapter)
                    c_state = CharacterState.model_validate(merged, context=genre_ctx)
                    chars_dict[cid] = c_state.model_dump(mode="json", by_alias=True, exclude_none=True)
                validated_chars = CharacterStateFile.model_validate(chars_dict, context=genre_ctx)
                chars_path.parent.mkdir(parents=True, exist_ok=True)
                chars_path.write_text(
                    _dump_yaml(validated_chars.model_dump(mode="json", by_alias=True, exclude_none=True)),
                    encoding="utf-8",
                )
                updated_files.append(chars_path.resolve().relative_to(root).as_posix())

                if arch_chars_modified:
                    validated_arch_chars = CharacterStateFile.model_validate(
                        arch_chars_dict, context=genre_ctx
                    )
                    arch_chars_path.write_text(
                        _dump_yaml(
                            validated_arch_chars.model_dump(
                                mode="json", by_alias=True, exclude_none=True
                            )
                        ),
                        encoding="utf-8",
                    )
                    updated_files.append(arch_chars_path.resolve().relative_to(root).as_posix())

            # 4) 按需覆写势力状态截面 03_状态/当前/势力状态.yaml（含冷库唤醒）
            if faction_updates:
                facs_path = resolve_active_factions_file(root, for_write=True)
                raw_facs = (
                    yaml.safe_load(facs_path.read_text(encoding="utf-8"))
                    if facs_path.is_file()
                    else {}
                )
                facs_dict: dict[str, Any] = dict(raw_facs) if isinstance(raw_facs, dict) else {}

                arch_facs_path = resolve_archive_factions_file(root, for_write=True)
                raw_arch_facs = (
                    yaml.safe_load(arch_facs_path.read_text(encoding="utf-8"))
                    if arch_facs_path.is_file()
                    else {}
                )
                arch_facs_dict: dict[str, Any] = (
                    dict(raw_arch_facs) if isinstance(raw_arch_facs, dict) else {}
                )
                arch_facs_modified = False

                for fid, patch in faction_updates.items():
                    prev = facs_dict.get(fid)
                    if prev is None and fid in arch_facs_dict:
                        prev = arch_facs_dict[fid]
                    if fid in arch_facs_dict:
                        del arch_facs_dict[fid]
                        arch_facs_modified = True
                    merged = {**(prev if isinstance(prev, dict) else {}), **dict(patch)}
                    merged.setdefault("updated_at_ch", chapter)
                    f_state = FactionState.model_validate(merged, context=genre_ctx)
                    facs_dict[fid] = f_state.model_dump(mode="json", by_alias=True, exclude_none=True)
                validated_facs = FactionStateFile.model_validate(facs_dict, context=genre_ctx)
                facs_path.parent.mkdir(parents=True, exist_ok=True)
                facs_path.write_text(
                    _dump_yaml(validated_facs.model_dump(mode="json", by_alias=True, exclude_none=True)),
                    encoding="utf-8",
                )
                updated_files.append(facs_path.resolve().relative_to(root).as_posix())

                if arch_facs_modified:
                    validated_arch_facs = FactionStateFile.model_validate(
                        arch_facs_dict, context=genre_ctx
                    )
                    arch_facs_path.write_text(
                        _dump_yaml(
                            validated_arch_facs.model_dump(
                                mode="json", by_alias=True, exclude_none=True
                            )
                        ),
                        encoding="utf-8",
                    )
                    updated_files.append(arch_facs_path.resolve().relative_to(root).as_posix())

            # 5) 按需更新双时态关系边 03_状态/人物关系.yaml
            if relation_updates:
                rel_path = resolve_relations_file(root, for_write=True)
                raw_rels = (
                    yaml.safe_load(rel_path.read_text(encoding="utf-8"))
                    if rel_path.is_file()
                    else {}
                )
                rel_file = RelationsFile.model_validate(
                    raw_rels if isinstance(raw_rels, dict) else {"relations": []}
                )
                rel_list: list[TemporalRelation] = list(rel_file.relations)
                for upd in relation_updates:
                    src = str(upd["source"]).strip()
                    tgt = str(upd["target"]).strip()
                    rel_desc = str(upd["relation"]).strip()
                    v_from = int(upd.get("valid_from_ch", chapter))
                    inv_at = upd.get("invalid_at_ch")
                    inv_at_int = int(inv_at) if inv_at is not None else None

                    # 若存在同向未失效旧边且新边起始章大于旧边，自动关闭旧边
                    if inv_at_int is None:
                        for i, existing_rel in enumerate(rel_list):
                            if (
                                existing_rel.source == src
                                and existing_rel.target == tgt
                                and existing_rel.invalid_at_ch is None
                                and existing_rel.valid_from_ch < v_from
                            ):
                                rel_list[i] = existing_rel.model_copy(
                                    update={"invalid_at_ch": v_from}
                                )
                    rel_list.append(
                        TemporalRelation(
                            source=src,
                            target=tgt,
                            relation=rel_desc,
                            valid_from_ch=v_from,
                            invalid_at_ch=inv_at_int,
                        )
                    )
                new_rel_file = RelationsFile(relations=rel_list)
                rel_path.parent.mkdir(parents=True, exist_ok=True)
                rel_path.write_text(
                    _dump_yaml(new_rel_file.model_dump(mode="json", by_alias=True, exclude_none=True)),
                    encoding="utf-8",
                )
                updated_files.append(rel_path.resolve().relative_to(root).as_posix())

            # 6) 按需更新悬线与事件伏笔台账 03_状态/伏笔台账.yaml
            if thread_updates:
                thr_path = resolve_threads_file(root, for_write=True)
                raw_thrs = (
                    yaml.safe_load(thr_path.read_text(encoding="utf-8"))
                    if thr_path.is_file()
                    else {}
                )
                thr_file = ThreadsFile.model_validate(
                    raw_thrs if isinstance(raw_thrs, dict) else {"threads": []}
                )
                by_id: dict[str, Thread] = {t.id: t for t in thr_file.threads}
                for upd in thread_updates:
                    tid = str(upd.get("id") or upd.get("thread_id") or "").strip()
                    if not tid:
                        raise ValueError("thread_updates 中的条目必须包含 'id' 或 'thread_id'")
                    if tid in by_id:
                        base_dict = by_id[tid].model_dump(mode="json")
                        merged_thr = {**base_dict, **dict(upd), "id": tid}
                        merged_thr.pop("thread_id", None)
                        if "last_progressed_at" not in upd:
                            merged_thr["last_progressed_at"] = max(
                                int(base_dict["last_progressed_at"]), chapter
                            )
                        by_id[tid] = Thread.model_validate(merged_thr)
                    else:
                        new_thr = {**dict(upd), "id": tid}
                        new_thr.pop("thread_id", None)
                        new_thr.setdefault("planted_at", chapter)
                        new_thr.setdefault("last_progressed_at", chapter)
                        by_id[tid] = Thread.model_validate(new_thr)
                new_thr_file = ThreadsFile(threads=list(by_id.values()))
                thr_path.parent.mkdir(parents=True, exist_ok=True)
                thr_path.write_text(
                    _dump_yaml(new_thr_file.model_dump(mode="json", by_alias=True, exclude_none=True)),
                    encoding="utf-8",
                )
                updated_files.append(thr_path.resolve().relative_to(root).as_posix())

            sync_report_dict: dict[str, Any] | None = None
            if sync_index:
                with SqliteIndexer(root) as indexer:
                    sync_report_dict = indexer.sync().model_dump(mode="json")

            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_record_chapter_outcome",
                    "chapter": chapter,
                    "history_path": history_rel,
                    "fact_record": fact_record.model_dump(mode="json", by_alias=True),
                    "updated_files": updated_files,
                    "sync_report": sync_report_dict,
                    "summary": (
                        f"📝 已结算第 {chapter} 章状态与事实台账 -> `{history_rel}` "
                        f"(提取原子事实 {len(fact_record.facts)} 条, 更新文件 {len(updated_files)} 个)"
                    ),
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
            return _format_error(exc, tool_name="novel_record_chapter_outcome")

    # ------------------------------------------------------------------
    # 功能域 9: 索引与检索 (novel_sync_index / novel_search / novel_inspect_entity)
    # ------------------------------------------------------------------

    @mcp.tool(name="novel_sync_index")
    async def novel_sync_index(
        force_rebuild: bool = False,
        book_dir: str | None = None,
    ) -> str:
        """增量同步或零污染全量重建 SQLite 派生索引库（.ddnovel/index.sqlite）。"""
        try:
            root = resolve_book_dir(book_dir, default_book_dir)
            with SqliteIndexer(root) as indexer:
                report = indexer.sync(force_rebuild=force_rebuild)
            dumped = report.model_dump(mode="json")
            mode_str = "零污染全量重建" if force_rebuild else "SHA-256 增量同步"
            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_sync_index",
                    **dumped,
                    "summary": (
                        f"🔄 索引{mode_str}完成 (新增={len(report.added)}, "
                        f"更新={len(report.updated)}, 删除={len(report.deleted)}, "
                        f"耗时={report.elapsed_ms:.1f}ms)"
                    ),
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
            return _format_error(exc, tool_name="novel_sync_index")

    @mcp.tool(name="novel_search")
    async def novel_search(
        query: str,
        scope: str | None = None,
        top_k: int = 10,
        hybrid: bool = False,
        auto_sync: bool = True,
        book_dir: str | None = None,
    ) -> str:
        """基于 SQLite FTS5 CJK 双模分词（及可选向量 RRF 重排）跨设定、事实、摘要、正文与蓝图检索。"""
        try:
            if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
                raise ValueError(f"top_k 必须为 >= 1 的正整数，收到: {top_k!r}")

            root = resolve_book_dir(book_dir, default_book_dir)
            scope_enum = FtsScope(scope.strip().lower()) if scope and scope.strip() else None

            with SqliteIndexer(root) as indexer:
                if auto_sync:
                    indexer.sync()
                hits = indexer.search_fts(
                    query,
                    scope=scope_enum,
                    top_k=top_k,
                    hybrid=hybrid,
                )

            hit_dicts = [h.model_dump(mode="json") for h in hits]
            scope_label = scope_enum.value if scope_enum is not None else "all"
            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_search",
                    "query": query,
                    "scope": scope_label,
                    "top_k": top_k,
                    "hybrid": hybrid,
                    "count": len(hit_dicts),
                    "hits": hit_dicts,
                    "summary": (
                        f"🔎 检索 `{query}` (作用域={scope_label}) 命中 {len(hit_dicts)} 条结果"
                    ),
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
            return _format_error(exc, tool_name="novel_search")

    @mcp.tool(name="novel_inspect_entity")
    async def novel_inspect_entity(
        entity_id: str,
        at_chapter: int | None = None,
        include_secret: bool = False,
        auto_sync: bool = True,
        book_dir: str | None = None,
    ) -> str:
        """聚合查阅指定实体的设定切片、当前活跃状态截面与在指定章号生效的双时态关系边。"""
        try:
            root = resolve_book_dir(book_dir, default_book_dir)
            with SqliteIndexer(root) as indexer:
                if auto_sync:
                    indexer.sync()
                resolved_id = _lookup_entity_id(indexer, entity_id)
                bundle = indexer.get_entity_bundle(resolved_id, at_chapter=at_chapter)

            dumped = bundle.model_dump(mode="json")
            has_secret = bool(bundle.setting.secret_text and bundle.setting.secret_text.strip())
            dumped["has_secret"] = has_secret
            if not include_secret and isinstance(dumped.get("setting"), dict):
                dumped["setting"]["secret_text"] = None

            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_inspect_entity",
                    "entity_id": bundle.setting.id,
                    "at_chapter": at_chapter,
                    **dumped,
                    "summary": (
                        f"👤 实体检视 `{bundle.setting.id}` ({bundle.setting.name}, "
                        f"分类={bundle.setting.category.value}, 状态={bundle.setting.status.value}, "
                        f"冷库归档={bundle.is_archived}, 有效关系边={len(bundle.relations)} 条)"
                    ),
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
            return _format_error(exc, tool_name="novel_inspect_entity")

    # ------------------------------------------------------------------
    # 功能域 10: 遗忘与瘦身 (cold_archive / prune_unused / drop_thread / compact_history)
    # ------------------------------------------------------------------

    @mcp.tool(name="novel_cold_archive")
    async def novel_cold_archive(
        current_chapter: int | None = None,
        cold_after_chapters: int | None = None,
        dry_run: bool = False,
        book_dir: str | None = None,
    ) -> str:
        """执行退场/死亡/久未登场实体、失效关系边与已完结伏笔的冷库休眠冻结（03_状态/归档/）。"""
        try:
            root = resolve_book_dir(book_dir, default_book_dir)
            with LifecycleManager(root) as mgr:
                report = mgr.cold_archive_gc(
                    current_ch=current_chapter,
                    dry_run=dry_run,
                    cold_after_chapters=cold_after_chapters,
                )
            dumped = report.model_dump(mode="json")
            mode_tag = "[预览模式]" if dry_run else "[已归档]"
            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_cold_archive",
                    **dumped,
                    "summary": (
                        f"🧊 {mode_tag} 冷库归档 GC (基准第 {report.current_chapter} 章)："
                        f"归档角色 {len(report.archived_characters)} 个, "
                        f"归档势力 {len(report.archived_factions)} 个, "
                        f"归档失效关系 {report.archived_relations_count} 条, "
                        f"归档已填伏笔 {len(report.archived_threads)} 条"
                    ),
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
            return _format_error(exc, tool_name="novel_cold_archive")

    @mcp.tool(name="novel_prune_unused")
    async def novel_prune_unused(
        target_ids: list[str] | None = None,
        apply: bool = False,
        current_chapter: int | None = None,
        max_appearances: int = 2,
        min_age_chapters: int | None = None,
        force: bool = False,
        book_dir: str | None = None,
    ) -> str:
        """扫描并按需物理清理零引用废案孤儿设定与远期一次性龙套卡片（默认 apply=False 为安全预览）。"""
        try:
            root = resolve_book_dir(book_dir, default_book_dir)
            with LifecycleManager(root) as mgr:
                report = mgr.prune_settings(
                    target_ids=target_ids,
                    apply=apply,
                    current_ch=current_chapter,
                    max_appearances=max_appearances,
                    min_age_chapters=min_age_chapters,
                    force=force,
                )
            dumped = report.model_dump(mode="json")
            mode_tag = "[已物理删除]" if apply else "[安全预览]"
            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_prune_unused",
                    **dumped,
                    "summary": (
                        f"✂️ {mode_tag} 冗余设定剪枝扫描：检出零引用孤儿设定 {len(report.orphan_ids)} 个, "
                        f"远期低频龙套设定 {len(report.low_frequency_ids)} 个, "
                        f"物理删除文件 {len(report.deleted_paths)} 个"
                    ),
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
            return _format_error(exc, tool_name="novel_prune_unused")

    @mcp.tool(name="novel_drop_thread")
    async def novel_drop_thread(
        thread_id: str,
        dry_run: bool = False,
        book_dir: str | None = None,
    ) -> str:
        """主动弃坑移除不想继续写的伏笔/支线，并附带前文提及影响面报告。"""
        try:
            root = resolve_book_dir(book_dir, default_book_dir)
            with LifecycleManager(root) as mgr:
                result = mgr.drop_thread(thread_id, dry_run=dry_run)
            dumped = result.model_dump(mode="json")
            mode_tag = "[预览模式]" if dry_run else "[已移除]"
            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_drop_thread",
                    **dumped,
                    "prior_mention_count": len(result.prior_mentions),
                    "summary": (
                        f"🗑️ {mode_tag} 弃坑伏笔 `{result.thread_id}` 从 `{result.removed_from}` 移除 "
                        f"(检出前文/蓝图提及 {len(result.prior_mentions)} 处)"
                    ),
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
            return _format_error(exc, tool_name="novel_drop_thread")

    @mcp.tool(name="novel_compact_history")
    async def novel_compact_history(
        volume_id: str,
        milestones: list[dict[str, Any]] | None = None,
        dry_run: bool = False,
        book_dir: str | None = None,
    ) -> str:
        """远期完结卷原子事实蒸馏浓缩（将老卷百章细碎事实合并浓缩为 15~20 条核心里程碑事实）。"""
        try:
            root = resolve_book_dir(book_dir, default_book_dir)
            with LifecycleManager(root) as mgr:
                result = mgr.compact_volume_facts(
                    vol_id=volume_id,
                    milestones=milestones,
                    dry_run=dry_run,
                )
            dumped = result.model_dump(mode="json")
            mode_tag = "[预览模式]" if dry_run else "[已浓缩]"
            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_compact_history",
                    **dumped,
                    "summary": (
                        f"📚 {mode_tag} 分卷 `{result.volume_id}` 历史事实蒸馏浓缩："
                        f"{result.original_count} 条 -> {result.compacted_count} 条 (`{result.history_path}`)"
                    ),
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
            return _format_error(exc, tool_name="novel_compact_history")

    # ------------------------------------------------------------------
    # 功能域 11: 一致性体检 (novel_audit_consistency)
    # ------------------------------------------------------------------

    @mcp.tool(name="novel_audit_consistency")
    async def novel_audit_consistency(
        chapter: int | None = None,
        book_dir: str | None = None,
    ) -> str:
        """执行单章或全书静态一致性体检（校验缺失设定/死人出场/冷库未唤醒出场/禁用词红线/扩展槽 Schema）。"""
        try:
            root = resolve_book_dir(book_dir, default_book_dir)
            with LifecycleManager(root) as mgr:
                report = mgr.audit_chapter(chapter_num=chapter)
            dumped = report.model_dump(mode="json")
            error_count = sum(1 for iss in report.issues if iss.level.value == "error")
            warn_count = sum(1 for iss in report.issues if iss.level.value == "warning")
            target_label = f"第 {chapter} 章" if chapter is not None else "全书"
            status_emoji = "✅ 通过" if report.passed else "❌ 未通过"
            return _format_response(
                {
                    "ok": True,
                    "tool": "novel_audit_consistency",
                    **dumped,
                    "error_count": error_count,
                    "warning_count": warn_count,
                    "summary": (
                        f"🩺 {target_label}一致性体检 {status_emoji} "
                        f"(错误: {error_count}, 警告: {warn_count})"
                    ),
                }
            )
        except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
            return _format_error(exc, tool_name="novel_audit_consistency")

    return mcp


def main(argv: Sequence[str] | None = None) -> None:
    """``ddnovel-mcp`` 命令行入口：启动 Stdio MCP Server。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if any(flag in args for flag in ("-h", "--help")):
        print(
            "Usage: ddnovel-mcp [OPTIONS]\n\n"
            "DDNovelGenerater FastMCP Stdio 工具服务端"
            "（供 OpenCode / Claude Code / DSH / Antigravity 等宿主连接）。\n\n"
            "选项:\n"
            "  -h, --help     显示本帮助信息并退出\n"
            "  -V, --version  显示版本号并退出\n\n"
            "环境变量:\n"
            "  DDNOVEL_BOOK_DIR  默认小说书库根目录路径（未指定时取当前工作目录 '.'）\n\n"
            "注意: 不带参数直接运行时将进入 Stdio JSON-RPC 监听模式等待 AI 宿主连接。"
        )
        return
    if any(flag in args for flag in ("-V", "--version")):
        print(f"ddnovel-mcp {__version__}")
        return
    create_server().run()


if __name__ == "__main__":
    main()


__all__ = [
    "BOOK_DIR_ENV_VAR",
    "MCP_TOOL_NAMES",
    "create_server",
    "main",
    "resolve_book_dir",
]
