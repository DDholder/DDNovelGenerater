"""T02.6 跨模块只读结果 DTO (`results.py`)：解析、嗅探、索引、雷达、影响面、写作包与生命周期报告。"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Self, TypeVar

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from ._base import (
    ChapterNum,
    EntityId,
    FrozenDTO,
    FtsScope,
    IssueLevel,
    SettingCategory,
    ThreadId,
    VolumeId,
)
from .models import (
    CharacterState,
    FactionState,
    SettingDoc,
    TemporalRelation,
    Thread,
)

_T = TypeVar("_T")


# ---------------------------------------------------------------------------
# 1. 辅助约束类型与列表/路径规范化工具
# ---------------------------------------------------------------------------


def _reject_bool_int(value: Any) -> Any:
    """拒绝将布尔值 True/False 静默当作整数 1/0。"""
    if isinstance(value, bool):
        raise ValueError("整数结果字段不能传入布尔值 True/False")
    return value


def _reject_bool_float(value: Any) -> Any:
    """拒绝将布尔值 True/False 静默当作浮点数 1.0/0.0。"""
    if isinstance(value, bool):
        raise ValueError("浮点结果字段不能传入布尔值 True/False")
    return value


def _to_posix_path(value: str) -> str:
    """将 Windows 反斜杠路径统一规范为 POSIX 斜杠格式。"""
    return value.replace("\\", "/")


def _dedupe_list(items: list[_T]) -> list[_T]:
    """对列表元素保持首次出现顺序去重。"""
    return list(dict.fromkeys(items))


def _clean_str_list(items: list[str]) -> list[str]:
    """清理字符串列表中的首尾空白与空串，并保持首次出现顺序去重。"""
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        val = item.strip()
        if not val or val in seen:
            continue
        seen.add(val)
        cleaned.append(val)
    return cleaned


NonNegativeInt = Annotated[int, Field(ge=0), BeforeValidator(_reject_bool_int)]
PositiveInt = Annotated[int, Field(ge=1), BeforeValidator(_reject_bool_int)]
NonNegativeFloat = Annotated[
    float, Field(ge=0.0, allow_inf_nan=False), BeforeValidator(_reject_bool_float)
]
FiniteFloat = Annotated[
    float, Field(allow_inf_nan=False), BeforeValidator(_reject_bool_float)
]
PosixPathStr = Annotated[str, AfterValidator(_to_posix_path)]
NonEmptyPosixPathStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
    AfterValidator(_to_posix_path),
]


# ---------------------------------------------------------------------------
# 2. 公共结果枚举
# ---------------------------------------------------------------------------


class RadarReason(StrEnum):
    """伏笔雷达告警触发原因。"""

    WINDOW = "window"
    STALE = "stale"
    ENTITY = "entity"


class ImpactScope(StrEnum):
    """防吃书影响面扫描命中的五维作用域。"""

    MANUSCRIPT = "manuscript"
    FACT = "fact"
    STATE = "state"
    THREAD = "thread"
    BLUEPRINT = "blueprint"


class SlotName(StrEnum):
    """单章写作包 U 型五槽位标准名称。"""

    GUARDRAILS = "guardrails"
    WORLD_AND_FACTS = "world_and_facts"
    ENTITIES = "entities"
    CONTINUITY = "continuity"
    BEATS = "beats"


# ---------------------------------------------------------------------------
# 3. 解析与别名嗅探层 DTO (T03 / T04)
# ---------------------------------------------------------------------------


class ParsedBlocks(FrozenDTO):
    """设定文档渐进式三级切片与 Token 估算结果（T03 产出）。"""

    core: str = ""
    topics: dict[str, str] = Field(default_factory=dict)
    secret: str | None = None
    token_estimates: dict[str, NonNegativeInt] = Field(default_factory=dict)


class MatchedEntity(FrozenDTO):
    """Aho-Corasick 实体别名自动机单次命中结果（T04 产出）。"""

    entity_id: EntityId
    alias: str = Field(..., min_length=1)
    start: NonNegativeInt
    end: PositiveInt
    is_archived: bool = False

    @model_validator(mode="after")
    def _validate_span(self) -> Self:
        if self.end <= self.start:
            raise ValueError(
                f"匹配区间 end ({self.end}) 必须严格大于 start ({self.start})"
            )
        return self


# ---------------------------------------------------------------------------
# 4. 索引与混合检索层 DTO (T06 / T07)
# ---------------------------------------------------------------------------


class SyncReport(FrozenDTO):
    """SQLite 派生索引增量同步或全量重建报告（T06 产出）。"""

    added: Annotated[list[NonEmptyPosixPathStr], AfterValidator(_dedupe_list)] = Field(
        default_factory=list
    )
    updated: Annotated[list[NonEmptyPosixPathStr], AfterValidator(_dedupe_list)] = Field(
        default_factory=list
    )
    deleted: Annotated[list[NonEmptyPosixPathStr], AfterValidator(_dedupe_list)] = Field(
        default_factory=list
    )
    elapsed_ms: NonNegativeFloat = 0.0
    rebuilt: bool = False


class SearchHit(FrozenDTO):
    """FTS5 / 向量混合检索单条命中结果（T06 / T07 产出）。"""

    doc_id: str = Field(..., min_length=1)
    scope: FtsScope
    chapter_num: NonNegativeInt = 0
    entity_id: EntityId | None = None
    excerpt: str = ""
    score: FiniteFloat = 0.0


class EntityBundle(FrozenDTO):
    """单个实体的设定切片、当前截面状态与有效关系聚合包（T06 产出）。"""

    setting: SettingDoc
    state: CharacterState | FactionState | None = None
    relations: list[TemporalRelation] = Field(default_factory=list)
    is_archived: bool = False

    @model_validator(mode="before")
    @classmethod
    def _disambiguate_state_by_category(cls, data: Any, info: ValidationInfo) -> Any:
        if not isinstance(data, dict):
            return data
        raw_state = data.get("state")
        if not isinstance(raw_state, dict):
            return data

        setting_val = data.get("setting")
        category: Any = None
        if isinstance(setting_val, SettingDoc):
            category = setting_val.category
        elif isinstance(setting_val, dict):
            category = setting_val.get("category")

        if category in (SettingCategory.FACTION, SettingCategory.FACTION.value):
            return {
                **data,
                "state": FactionState.model_validate(raw_state, context=info.context),
            }
        if category in (SettingCategory.CHARACTER, SettingCategory.CHARACTER.value):
            return {
                **data,
                "state": CharacterState.model_validate(raw_state, context=info.context),
            }
        return data

    @model_validator(mode="after")
    def _validate_state_matches_category(self) -> Self:
        if self.state is None:
            return self
        category = self.setting.category
        if category == SettingCategory.CHARACTER and not isinstance(
            self.state, CharacterState
        ):
            raise ValueError("角色设定 (character) 只能挂载 CharacterState 状态截面")
        if category == SettingCategory.FACTION and not isinstance(
            self.state, FactionState
        ):
            raise ValueError("势力设定 (faction) 只能挂载 FactionState 状态截面")
        if category not in {SettingCategory.CHARACTER, SettingCategory.FACTION}:
            raise ValueError(
                f"设定分类 '{category.value}' 不支持挂载动态状态截面，state 必须为 None"
            )
        return self


# ---------------------------------------------------------------------------
# 5. 核心业务引擎层 DTO (T08 / T09 / T10)
# ---------------------------------------------------------------------------


class RadarHit(FrozenDTO):
    """伏笔巡检雷达单条命中条目（T09 产出）。"""

    thread: Thread
    reasons: Annotated[list[RadarReason], AfterValidator(_dedupe_list)] = Field(
        ..., min_length=1
    )
    detail: str = ""


class RadarReport(FrozenDTO):
    """规划期悬线伏笔雷达完整巡检报告（T09 产出）。"""

    target_chapter: ChapterNum
    planned_entities: Annotated[list[EntityId], AfterValidator(_dedupe_list)] = Field(
        default_factory=list
    )
    hits: list[RadarHit] = Field(default_factory=list)
    total_active_threads: NonNegativeInt = 0


class ImpactHit(FrozenDTO):
    """设定影响面扫描单条命中记录（T10 产出）。"""

    scope: ImpactScope
    location: NonEmptyPosixPathStr
    chapter_num: ChapterNum | None = None
    matched_term: Annotated[
        str | None, AfterValidator(lambda v: None if v == "" else v)
    ] = None
    excerpt: str = ""


class ImpactReport(FrozenDTO):
    """中途改设定五维防吃书影响面扫描报告（T10 产出）。"""

    target: str = Field(..., min_length=1)
    entity_id: EntityId | None = None
    expanded_terms: Annotated[list[str], AfterValidator(_clean_str_list)] = Field(
        default_factory=list
    )
    hits: list[ImpactHit] = Field(default_factory=list)
    is_safe_to_change: bool = True

    @model_validator(mode="before")
    @classmethod
    def _default_is_safe_to_change(cls, data: Any) -> Any:
        if isinstance(data, dict) and "is_safe_to_change" not in data:
            raw_hits = data.get("hits")
            if raw_hits is None:
                return {**data, "is_safe_to_change": True}
            if isinstance(raw_hits, (list, tuple)):
                return {**data, "is_safe_to_change": len(raw_hits) == 0}
        return data


class SlotReport(FrozenDTO):
    """单章写作包单个槽位的 Token 预算与裁剪报告（T08 产出）。"""

    name: SlotName
    budget: NonNegativeInt
    used: NonNegativeInt
    trimmed: bool = False
    dropped_items: Annotated[list[str], AfterValidator(_clean_str_list)] = Field(
        default_factory=list
    )


class PacketBuildResult(FrozenDTO):
    """单章写作包一步直出编译结果（T08 产出）。"""

    chapter: ChapterNum
    packet_path: NonEmptyPosixPathStr
    budget_total: PositiveInt | None = None
    total_tokens: NonNegativeInt
    slots: list[SlotReport] = Field(default_factory=list)
    blocked_secrets: Annotated[list[EntityId], AfterValidator(_dedupe_list)] = Field(
        default_factory=list
    )
    blocked_threads: Annotated[list[ThreadId], AfterValidator(_dedupe_list)] = Field(
        default_factory=list
    )
    warnings: Annotated[list[str], AfterValidator(_clean_str_list)] = Field(
        default_factory=list
    )

    @field_validator("slots", mode="after")
    @classmethod
    def _validate_unique_slot_names(cls, slots: list[SlotReport]) -> list[SlotReport]:
        seen: set[SlotName] = set()
        for slot in slots:
            if slot.name in seen:
                raise ValueError(f"写作包 slots 中存在重复槽位名: '{slot.name.value}'")
            seen.add(slot.name)
        return slots


# ---------------------------------------------------------------------------
# 6. 生命周期管理与一致性体检 DTO (T11)
# ---------------------------------------------------------------------------


class PromoteResult(FrozenDTO):
    """单章定稿归档与超期过程产物清理结果（T11 产出）。"""

    chapter: ChapterNum
    draft_path: NonEmptyPosixPathStr
    manuscript_path: NonEmptyPosixPathStr
    cleaned_paths: Annotated[
        list[NonEmptyPosixPathStr], AfterValidator(_dedupe_list)
    ] = Field(default_factory=list)
    dry_run: bool = False


class GCReport(FrozenDTO):
    """退场实体、失效关系与已回收伏笔冷归档报告（T11 产出）。"""

    current_chapter: NonNegativeInt = 0
    archived_characters: Annotated[list[EntityId], AfterValidator(_dedupe_list)] = Field(
        default_factory=list
    )
    archived_factions: Annotated[list[EntityId], AfterValidator(_dedupe_list)] = Field(
        default_factory=list
    )
    archived_relations_count: NonNegativeInt = 0
    archived_threads: Annotated[list[ThreadId], AfterValidator(_dedupe_list)] = Field(
        default_factory=list
    )
    modified_paths: Annotated[
        list[NonEmptyPosixPathStr], AfterValidator(_dedupe_list)
    ] = Field(default_factory=list)
    dry_run: bool = False


class DropResult(FrozenDTO):
    """主动弃坑伏笔物理移除结果（T11 产出）。"""

    thread_id: ThreadId
    removed_from: NonEmptyPosixPathStr
    prior_mentions: list[ImpactHit] = Field(default_factory=list)
    dry_run: bool = False


class PruneReport(FrozenDTO):
    """孤儿废案与一次性龙套卡片物理剪枝报告（T11 产出）。"""

    orphan_ids: Annotated[list[EntityId], AfterValidator(_dedupe_list)] = Field(
        default_factory=list
    )
    low_frequency_ids: Annotated[list[EntityId], AfterValidator(_dedupe_list)] = Field(
        default_factory=list
    )
    deleted_paths: Annotated[
        list[NonEmptyPosixPathStr], AfterValidator(_dedupe_list)
    ] = Field(default_factory=list)
    skipped_ids: Annotated[list[EntityId], AfterValidator(_dedupe_list)] = Field(
        default_factory=list
    )
    dry_run: bool = False


class CompactResult(FrozenDTO):
    """远期完结卷原子事实蒸馏浓缩结果（T11 产出）。"""

    volume_id: VolumeId
    history_path: NonEmptyPosixPathStr
    original_count: NonNegativeInt
    compacted_count: NonNegativeInt
    dry_run: bool = False

    @model_validator(mode="after")
    def _validate_compacted_not_greater(self) -> Self:
        if self.compacted_count > self.original_count:
            raise ValueError(
                f"浓缩后条数 compacted_count ({self.compacted_count}) "
                f"不能大于原始条数 original_count ({self.original_count})"
            )
        return self


class AuditIssue(FrozenDTO):
    """一致性体检单条问题项（T11 产出）。"""

    level: IssueLevel
    code: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    location: PosixPathStr = ""


class AuditReport(FrozenDTO):
    """单章或全书静态一致性体检报告（T11 产出）。"""

    chapter: ChapterNum | None = None
    issues: list[AuditIssue] = Field(default_factory=list)
    passed: bool = True

    @model_validator(mode="before")
    @classmethod
    def _default_passed(cls, data: Any) -> Any:
        if isinstance(data, dict) and "passed" not in data:
            raw_issues = data.get("issues")
            if raw_issues is None:
                return {**data, "passed": True}
            if isinstance(raw_issues, (list, tuple)):
                has_error = any(
                    (
                        item.level == IssueLevel.ERROR
                        if isinstance(item, AuditIssue)
                        else isinstance(item, dict)
                        and item.get("level")
                        in (IssueLevel.ERROR, IssueLevel.ERROR.value)
                    )
                    for item in raw_issues
                )
                return {**data, "passed": not has_error}
        return data


__all__ = [
    "AuditIssue",
    "AuditReport",
    "CompactResult",
    "DropResult",
    "EntityBundle",
    "GCReport",
    "ImpactHit",
    "ImpactReport",
    "ImpactScope",
    "MatchedEntity",
    "PacketBuildResult",
    "ParsedBlocks",
    "PromoteResult",
    "PruneReport",
    "RadarHit",
    "RadarReason",
    "RadarReport",
    "SearchHit",
    "SlotName",
    "SlotReport",
    "SyncReport",
]
