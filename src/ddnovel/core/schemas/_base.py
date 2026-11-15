"""T02.1 公共基础层：宽严分级基类、ID/章号约束类型、公共枚举与命名工具。"""

from enum import StrEnum
from typing import Annotated, Any, Final, NamedTuple

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

# ---------------------------------------------------------------------------
# 1. 宽严分级基类 (L0 宽容 / L1 校验 / L2 严格 / L2 只读)
# ---------------------------------------------------------------------------


class LenientModel(BaseModel):
    """L0 宽容基类：用于作者手写的设定/分卷/单元/蓝图 Frontmatter。

    允许未知字段并自动去除字符串首尾空白，缺失字段由解析器推断回填。
    """

    model_config = ConfigDict(
        extra="allow",
        str_strip_whitespace=True,
        populate_by_name=True,
    )


class ValidatedModel(BaseModel):
    """L1 校验基类：用于 03_state 结构化状态文件、题材扩展槽与文风指南。

    核心字段强类型校验；未知字段保留在 model_extra 以便无损写回并供 audit 提示。
    """

    model_config = ConfigDict(
        extra="allow",
        str_strip_whitespace=True,
        populate_by_name=True,
    )


class StrictModel(BaseModel):
    """L2 严格基类：用于 novel.yaml 全局配置，拼写错误或未知字段立即报错。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        populate_by_name=True,
    )


class FrozenDTO(BaseModel):
    """L2 只读基类：用于跨模块传递的结果 DTO，不可篡改、严禁未知字段。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        populate_by_name=True,
    )


# ---------------------------------------------------------------------------
# 2. 正则常量与 ID / 章号 / 窗口约束类型
# ---------------------------------------------------------------------------

ENTITY_ID_PATTERN: Final[str] = r"^[a-z][a-z0-9]*_[a-z0-9_]+$"
VOLUME_ID_PATTERN: Final[str] = r"^vol_\d{2,}$"
ARC_ID_PATTERN: Final[str] = r"^vol_\d{2,}_arc_\d{2,}$"
THREAD_ID_PATTERN: Final[str] = r"^thread_[a-z0-9_]+$"


def _reject_bool_int(value: Any) -> Any:
    """防止 YAML/Python 中的布尔值 True/False 被静默当作章号 1/0。"""
    if isinstance(value, bool):
        raise ValueError("章号必须为正整数，不能为布尔值")
    return value


ChapterNum = Annotated[int, Field(ge=1), BeforeValidator(_reject_bool_int)]

EntityId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=ENTITY_ID_PATTERN),
]
VolumeId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=VOLUME_ID_PATTERN),
]
ArcId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=ARC_ID_PATTERN),
]
ThreadId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=THREAD_ID_PATTERN),
]


class _ChapterWindowTuple(NamedTuple):
    """章节区间二元组 (start, end)，同时支持下标解包与 .start / .end 属性访问。"""

    start: ChapterNum
    end: ChapterNum


def _validate_chapter_window(window: _ChapterWindowTuple) -> _ChapterWindowTuple:
    if window.start > window.end:
        raise ValueError(f"章节区间起始章 ({window.start}) 不能大于结束章 ({window.end})")
    return window


ChapterWindow = Annotated[_ChapterWindowTuple, AfterValidator(_validate_chapter_window)]


# ---------------------------------------------------------------------------
# 3. 公共枚举与前缀推荐分类映射
# ---------------------------------------------------------------------------


class SettingCategory(StrEnum):
    """设定实体分类。"""

    WORLD = "world"
    RULE = "rule"
    GEOGRAPHY = "geography"
    FACTION = "faction"
    CHARACTER = "character"
    ITEM = "item"
    OTHER = "other"


class EntityStatus(StrEnum):
    """设定实体/势力生命周期状态（势力覆灭统一归入 retired）。"""

    ACTIVE = "active"
    DECEASED = "deceased"
    RETIRED = "retired"


class ThreadStatus(StrEnum):
    """悬线与事件伏笔四态枚举（主动弃坑为物理移除，不设 dropped 状态）。"""

    PLANTED = "planted"
    DORMANT = "dormant"
    ACTIVE = "active"
    RESOLVED = "resolved"


class ThreadAction(StrEnum):
    """章节蓝图中对指定伏笔执行的动作。"""

    PLANT = "plant"
    PROGRESS = "progress"
    RESOLVE = "resolve"


class FtsScope(StrEnum):
    """FTS5 全文检索切片作用域。"""

    SETTING = "setting"
    FACT = "fact"
    SUMMARY = "summary"
    MANUSCRIPT = "manuscript"
    BLUEPRINT = "blueprint"


class HostKind(StrEnum):
    """支持的 AI 软件宿主类型。"""

    OPENCODE = "opencode"
    DSH = "dsh"
    CLAUDE = "claude"
    ANTIGRAVITY = "antigravity"


class IssueLevel(StrEnum):
    """一致性体检（audit）问题严重级别。"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


ID_PREFIX_CATEGORY: Final[dict[str, SettingCategory]] = {
    "char_": SettingCategory.CHARACTER,
    "loc_": SettingCategory.GEOGRAPHY,
    "fac_": SettingCategory.FACTION,
    "world_": SettingCategory.WORLD,
    "rule_": SettingCategory.RULE,
    "item_": SettingCategory.ITEM,
}


# ---------------------------------------------------------------------------
# 4. 命名格式化工具函数
# ---------------------------------------------------------------------------


def format_chapter(n: int, width: int = 4) -> str:
    """将正整章号格式化为统一文件名前缀（如 ``format_chapter(42) -> "ch_0042"``）。"""
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError(f"章号 n 必须为 >= 1 的整数，收到: {n!r}")
    if isinstance(width, bool) or not isinstance(width, int) or width < 1:
        raise ValueError(f"宽度 width 必须为 >= 1 的整数，收到: {width!r}")
    return f"ch_{n:0{width}d}"


__all__ = [
    "ARC_ID_PATTERN",
    "ENTITY_ID_PATTERN",
    "ID_PREFIX_CATEGORY",
    "THREAD_ID_PATTERN",
    "VOLUME_ID_PATTERN",
    "ArcId",
    "ChapterNum",
    "ChapterWindow",
    "EntityId",
    "EntityStatus",
    "FrozenDTO",
    "FtsScope",
    "HostKind",
    "IssueLevel",
    "LenientModel",
    "SettingCategory",
    "StrictModel",
    "ThreadAction",
    "ThreadId",
    "ThreadStatus",
    "ValidatedModel",
    "VolumeId",
    "format_chapter",
]
