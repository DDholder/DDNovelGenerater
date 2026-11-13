"""四维真源领域模型 (models.py)。

本模块定义与书库四维真源文件 1:1 对应的 Pydantic 领域模型：
- 设定维 (`01_settings/`)：`SettingFrontmatter`、`SettingDoc`
- 结构维 (`02_structure/`)：`VolumeDoc`、`ArcDoc`、`BlueprintCast`、
  `ThreadRef`、`TopicRef`、`Beat`、`BlueprintDoc`
- 创作维 (`04_creation/`)：`StyleGuide`
- 状态维 (`03_state/`)：`EpistemicBounds`、`CharacterState`、`CharacterStateFile`、
  `FactionState`、`FactionStateFile`、`WorldState`、`TemporalRelation`、
  `RelationsFile`、`Thread`、`ThreadsFile`、`ChapterFactRecord`
"""

from typing import Annotated, Any, Self, TypeVar

from pydantic import (
    BeforeValidator,
    Field,
    RootModel,
    ValidationInfo,
    field_validator,
    model_validator,
)

from ._base import (
    ArcId,
    ChapterNum,
    ChapterWindow,
    EntityId,
    EntityStatus,
    LenientModel,
    SettingCategory,
    ThreadAction,
    ThreadId,
    ThreadStatus,
    ValidatedModel,
    VolumeId,
)
from .genres import validate_ext

_T = TypeVar("_T")


def _dedupe_list(items: list[_T]) -> list[_T]:
    """对列表元素保持首次出现顺序去重。"""
    return list(dict.fromkeys(items))


def _clean_str_list(items: list[str], *, exclude: str | None = None) -> list[str]:
    """清理字符串列表中的空白项，按需剔除指定值，并保持原有顺序去重。"""
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        val = item.strip()
        if not val or (exclude is not None and val == exclude) or val in seen:
            continue
        seen.add(val)
        cleaned.append(val)
    return cleaned


# ---------------------------------------------------------------------------
# 1. 设定维 (01_settings/)
# ---------------------------------------------------------------------------


class SettingFrontmatter(LenientModel):
    """设定文档 YAML Frontmatter 宽容模型（L0）。

    全部字段可选，允许空字典加载及保留未知自定义字段，缺失核心元信息由 T03 解析器按路径推断回填。
    """

    id: EntityId | None = None
    name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    category: SettingCategory | None = None
    status: EntityStatus = EntityStatus.ACTIVE
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _clean_metadata_lists(self) -> Self:
        self.aliases = _clean_str_list(self.aliases)
        self.tags = _clean_str_list(self.tags)
        return self


class SettingDoc(ValidatedModel):
    """设定文档完整解析模型（L1），包含核心设定、按需专题与机密底牌三级切片。"""

    id: EntityId
    name: str = Field(..., min_length=1)
    category: SettingCategory
    aliases: list[str] = Field(default_factory=list)
    status: EntityStatus = EntityStatus.ACTIVE
    tags: list[str] = Field(default_factory=list)
    file_path: str = Field(..., min_length=1)
    core_text: str = ""
    topics: dict[str, str] = Field(default_factory=dict)
    secret_text: str | None = None

    @model_validator(mode="after")
    def _normalize_aliases_and_tags(self) -> Self:
        self.aliases = _clean_str_list(self.aliases, exclude=self.name)
        self.tags = _clean_str_list(self.tags)
        return self

    def all_names(self) -> list[str]:
        """返回实体的标准名称与全部去重别名（供 T04 别名匹配自动机使用）。"""
        return [self.name, *self.aliases]


# ---------------------------------------------------------------------------
# 2. 结构维 (02_structure/)
# ---------------------------------------------------------------------------


class VolumeDoc(LenientModel):
    """分卷纲要文档模型（`02_structure/volumes/vol_XX.md`）。"""

    id: VolumeId
    title: str = ""
    chapter_range: ChapterWindow | None = None
    core_conflict: str = ""
    folded_summary: str | None = None
    body: str = ""


class ArcDoc(LenientModel):
    """剧情单元文档模型（`02_structure/arcs/vol_XX_arc_YY.md`）。"""

    id: ArcId
    volume_id: VolumeId | None = None
    title: str = ""
    chapter_range: ChapterWindow | None = None
    folded_summary: str | None = None
    body: str = ""

    @model_validator(mode="after")
    def _infer_or_validate_volume_id(self) -> Self:
        if self.volume_id is None:
            self.volume_id = self.id.split("_arc_", maxsplit=1)[0]
        elif not self.id.startswith(f"{self.volume_id}_arc_"):
            raise ValueError(
                f"单元 ID '{self.id}' 的分卷前缀与显式指定的 volume_id '{self.volume_id}' 不一致"
            )
        return self


class BlueprintCast(LenientModel):
    """章节蓝图登场与涉及实体清单。"""

    characters: list[EntityId] = Field(default_factory=list)
    locations: list[EntityId] = Field(default_factory=list)
    factions: list[EntityId] = Field(default_factory=list)
    items: list[EntityId] = Field(default_factory=list)

    @model_validator(mode="after")
    def _dedupe_cast_lists(self) -> Self:
        self.characters = _dedupe_list(self.characters)
        self.locations = _dedupe_list(self.locations)
        self.factions = _dedupe_list(self.factions)
        self.items = _dedupe_list(self.items)
        return self


class ThreadRef(LenientModel):
    """章节蓝图中对指定伏笔/悬线的动作引用。"""

    thread_id: ThreadId
    action: ThreadAction


class TopicRef(LenientModel):
    """章节蓝图中按需挂载的实体专题切片引用。"""

    entity_id: EntityId
    topic: str = Field(..., min_length=1)


class Beat(LenientModel):
    """章节蓝图分场节拍。"""

    index: int = Field(..., ge=1)
    title: str = ""
    content: str = ""

    @field_validator("index", mode="before")
    @classmethod
    def _reject_bool_index(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("分场节拍序号 index 必须为正整数，不能为布尔值")
        return value


class BlueprintDoc(LenientModel):
    """单章写作蓝图文档模型（`02_structure/blueprints/vol_XX/ch_XXXX.md`）。"""

    chapter: ChapterNum
    volume_id: VolumeId | None = None
    arc_id: ArcId | None = None
    title: str | None = None
    pov: EntityId | None = None
    cast: BlueprintCast = Field(default_factory=BlueprintCast)
    threads: list[ThreadRef] = Field(default_factory=list)
    unlock_secrets: list[EntityId] = Field(default_factory=list)
    topics: list[TopicRef] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    beats: list[Beat] = Field(default_factory=list)
    body: str = ""

    @field_validator("threads", mode="after")
    @classmethod
    def _validate_unique_threads(cls, threads: list[ThreadRef]) -> list[ThreadRef]:
        seen: set[str] = set()
        for ref in threads:
            if ref.thread_id in seen:
                raise ValueError(f"章节蓝图 threads 中存在重复的 thread_id: '{ref.thread_id}'")
            seen.add(ref.thread_id)
        return threads

    @model_validator(mode="after")
    def _normalize_and_validate_blueprint(self) -> Self:
        if (
            self.volume_id is not None
            and self.arc_id is not None
            and not self.arc_id.startswith(f"{self.volume_id}_arc_")
        ):
            raise ValueError(
                f"章节蓝图 arc_id '{self.arc_id}' 的分卷前缀与 volume_id '{self.volume_id}' 不一致"
            )

        self.unlock_secrets = _dedupe_list(self.unlock_secrets)
        self.forbidden = _clean_str_list(self.forbidden)

        if self.pov is not None and self.pov not in self.cast.characters:
            self.cast = self.cast.model_copy(
                update={"characters": [*self.cast.characters, self.pov]}
            )
        return self

    def all_entity_ids(self) -> list[EntityId]:
        """返回去重保序的全部关联实体 ID。

        汇总顺序：`pov` -> `cast.characters` -> `cast.locations` -> `cast.factions`
        -> `cast.items` -> `unlock_secrets` -> `topics` 中的 `entity_id`。
        """
        collected: list[EntityId] = []
        if self.pov is not None:
            collected.append(self.pov)
        collected.extend(self.cast.characters)
        collected.extend(self.cast.locations)
        collected.extend(self.cast.factions)
        collected.extend(self.cast.items)
        collected.extend(self.unlock_secrets)
        collected.extend(ref.entity_id for ref in self.topics)
        return _dedupe_list(collected)


# ---------------------------------------------------------------------------
# 3. 创作维 (04_creation/)
# ---------------------------------------------------------------------------


class StyleGuide(ValidatedModel):
    """文风与红线指南模型（`04_creation/style_guide.md`）。"""

    tone: str = ""
    pov_rules: list[str] = Field(default_factory=list)
    banned_words: list[str] = Field(default_factory=list)
    raw_markdown: str = ""

    @model_validator(mode="after")
    def _clean_rule_lists(self) -> Self:
        self.pov_rules = _clean_str_list(self.pov_rules)
        self.banned_words = _clean_str_list(self.banned_words)
        return self


# ---------------------------------------------------------------------------
# 4. 状态维 (03_state/)
# ---------------------------------------------------------------------------


def _reject_bool_non_negative_int(value: Any) -> Any:
    """防止布尔值 True/False 被静默当作章号 1/0。"""
    if isinstance(value, bool):
        raise ValueError("章号必须为非负整数，不能为布尔值")
    return value


_NonNegativeChapterInt = Annotated[
    int, Field(ge=0), BeforeValidator(_reject_bool_non_negative_int)
]


def _validate_profile_ext_with_context(
    value: dict[str, Any], info: ValidationInfo
) -> dict[str, Any]:
    """按 ValidationInfo.context['genre_profile'] 校验并归一化题材扩展槽。"""
    context = info.context
    genre_profile = context.get("genre_profile") if isinstance(context, dict) else None
    if genre_profile is None or genre_profile == "":
        return dict(value)
    if not isinstance(genre_profile, str):
        raise ValueError("context['genre_profile'] 必须为字符串")
    try:
        return validate_ext(genre_profile, value)
    except KeyError as exc:
        raise ValueError(str(exc)) from exc


class EpistemicBounds(ValidatedModel):
    """角色认知边界三元组（防全知视角剧透红线）。"""

    knows: list[str] = Field(default_factory=list)
    unaware_of: list[str] = Field(default_factory=list)
    misbelieves: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _clean_bound_lists(self) -> Self:
        self.knows = _clean_str_list(self.knows)
        self.unaware_of = _clean_str_list(self.unaware_of)
        self.misbelieves = _clean_str_list(self.misbelieves)
        return self


class CharacterState(ValidatedModel):
    """单个角色的当前动态状态截面（`03_state/{active,archive}/characters.yaml` 值模型）。"""

    updated_at_ch: ChapterNum
    location: EntityId | None = None
    physical_mental: str = ""
    current_goals: list[str] = Field(default_factory=list)
    epistemic_bounds: EpistemicBounds = Field(default_factory=EpistemicBounds)
    profile_ext: dict[str, Any] = Field(default_factory=dict)

    @field_validator("profile_ext", mode="after")
    @classmethod
    def _validate_profile_ext(
        cls, value: dict[str, Any], info: ValidationInfo
    ) -> dict[str, Any]:
        return _validate_profile_ext_with_context(value, info)

    @model_validator(mode="after")
    def _clean_goals(self) -> Self:
        self.current_goals = _clean_str_list(self.current_goals)
        return self


class CharacterStateFile(RootModel[dict[EntityId, CharacterState]]):
    """角色状态截面映射文件容器（`03_state/{active,archive}/characters.yaml`）。"""

    root: dict[EntityId, CharacterState] = Field(default_factory=dict)


class FactionState(ValidatedModel):
    """单个势力/组织的当前动态状态截面（`03_state/{active,archive}/factions.yaml` 值模型）。"""

    updated_at_ch: ChapterNum
    status: EntityStatus = EntityStatus.ACTIVE
    leader: EntityId | None = None
    territory: list[EntityId] = Field(default_factory=list)
    current_goals: list[str] = Field(default_factory=list)
    profile_ext: dict[str, Any] = Field(default_factory=dict)

    @field_validator("profile_ext", mode="after")
    @classmethod
    def _validate_profile_ext(
        cls, value: dict[str, Any], info: ValidationInfo
    ) -> dict[str, Any]:
        return _validate_profile_ext_with_context(value, info)

    @model_validator(mode="after")
    def _normalize_faction_lists(self) -> Self:
        self.territory = _dedupe_list(self.territory)
        self.current_goals = _clean_str_list(self.current_goals)
        return self


class FactionStateFile(RootModel[dict[EntityId, FactionState]]):
    """势力状态截面映射文件容器（`03_state/{active,archive}/factions.yaml`）。"""

    root: dict[EntityId, FactionState] = Field(default_factory=dict)


class WorldState(ValidatedModel):
    """全局世界日历与大势截面模型（`03_state/world_state.yaml`）。"""

    current_chapter: _NonNegativeChapterInt = 0
    in_world_time: str = ""
    macro_situation: str = ""


class TemporalRelation(ValidatedModel):
    """双时态实体关系边（左闭右开生效区间 ``[valid_from_ch, invalid_at_ch)``）。"""

    source: EntityId
    target: EntityId
    relation: str = Field(..., min_length=1)
    valid_from_ch: ChapterNum
    invalid_at_ch: ChapterNum | None = None

    @model_validator(mode="after")
    def _validate_relation_endpoints_and_window(self) -> Self:
        if self.source == self.target:
            raise ValueError(f"关系边起点 source 与终点 target 不能为同一实体: '{self.source}'")
        if self.invalid_at_ch is not None and self.invalid_at_ch <= self.valid_from_ch:
            raise ValueError(
                f"关系边失效章号 invalid_at_ch ({self.invalid_at_ch}) "
                f"必须严格大于生效起始章号 valid_from_ch ({self.valid_from_ch})"
            )
        return self

    def is_valid_at(self, ch: int) -> bool:
        """判断当前关系边在指定章号 ``ch`` 是否仍处于有效状态。"""
        if isinstance(ch, bool) or not isinstance(ch, int) or ch < 1:
            raise ValueError(f"章号 ch 必须为 >= 1 的整数，收到: {ch!r}")
        return self.valid_from_ch <= ch and (
            self.invalid_at_ch is None or ch < self.invalid_at_ch
        )


class RelationsFile(ValidatedModel):
    """双时态实体关系文件容器（`03_state/relations.yaml` 及 `archive/relations.yaml`）。"""

    relations: list[TemporalRelation] = Field(default_factory=list)


class Thread(ValidatedModel):
    """悬线与事件伏笔台账条目（`03_state/threads.yaml` 列表项）。"""

    id: ThreadId
    title: str = Field(..., min_length=1)
    category: str | None = None
    status: ThreadStatus = ThreadStatus.PLANTED
    planted_at: ChapterNum
    last_progressed_at: ChapterNum
    expected_window: ChapterWindow | None = None
    trigger_entities: list[EntityId] = Field(default_factory=list)
    public_clue: str = ""
    truth_secret: str | None = None

    @model_validator(mode="after")
    def _validate_and_normalize_thread(self) -> Self:
        if self.category == "":
            self.category = None
        if self.last_progressed_at < self.planted_at:
            raise ValueError(
                f"伏笔 '{self.id}' 的 last_progressed_at ({self.last_progressed_at}) "
                f"不能早于 planted_at ({self.planted_at})"
            )
        self.trigger_entities = _dedupe_list(self.trigger_entities)
        return self


class ThreadsFile(ValidatedModel):
    """悬线与事件伏笔台账文件容器（`03_state/threads.yaml` 及 `archive/threads.yaml`）。"""

    threads: list[Thread] = Field(default_factory=list)

    @field_validator("threads", mode="after")
    @classmethod
    def _validate_unique_thread_ids(cls, threads: list[Thread]) -> list[Thread]:
        seen: set[str] = set()
        for item in threads:
            if item.id in seen:
                raise ValueError(f"伏笔台账 threads 中存在重复的 thread_id: '{item.id}'")
            seen.add(item.id)
        return threads


class ChapterFactRecord(ValidatedModel):
    """单章历史摘要与原子事实锚点（`03_state/history/vol_XX_facts.jsonl` 每行记录）。"""

    ch: ChapterNum
    volume_id: VolumeId | None = None
    arc_id: ArcId | None = Field(default=None, alias="arc")
    in_world_time: str | None = Field(default=None, alias="time")
    summary: str = Field(..., min_length=1)
    facts: list[str]
    entities: list[EntityId] = Field(default_factory=list)

    @field_validator("facts", mode="after")
    @classmethod
    def _validate_and_clean_facts(cls, facts: list[str]) -> list[str]:
        if len(facts) > 8:
            raise ValueError(f"单章原子事实 facts 数量上限为 8 条，实际收到 {len(facts)} 条")
        cleaned = _clean_str_list(facts)
        if len(cleaned) < 1:
            raise ValueError("单章原子事实 facts 至少需要 1 条非空事实")
        return cleaned

    @model_validator(mode="after")
    def _validate_and_normalize_record(self) -> Self:
        if (
            self.volume_id is not None
            and self.arc_id is not None
            and not self.arc_id.startswith(f"{self.volume_id}_arc_")
        ):
            raise ValueError(
                f"历史事实记录 arc_id '{self.arc_id}' 的分卷前缀与 volume_id '{self.volume_id}' 不一致"
            )
        if self.in_world_time == "":
            self.in_world_time = None
        self.entities = _dedupe_list(self.entities)
        return self


__all__ = [
    "ArcDoc",
    "Beat",
    "BlueprintCast",
    "BlueprintDoc",
    "ChapterFactRecord",
    "CharacterState",
    "CharacterStateFile",
    "EpistemicBounds",
    "FactionState",
    "FactionStateFile",
    "RelationsFile",
    "SettingDoc",
    "SettingFrontmatter",
    "StyleGuide",
    "TemporalRelation",
    "Thread",
    "ThreadRef",
    "ThreadsFile",
    "TopicRef",
    "VolumeDoc",
    "WorldState",
]
