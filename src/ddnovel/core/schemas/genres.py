"""T02.3 题材扩展注册表与内置题材扩展模型。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, TypeVar, overload

from pydantic import Field

from ._base import ChapterNum, EntityId, ValidatedModel

T = TypeVar("T", bound="GenreExtBase")

_GENRE_REGISTRY: dict[str, type[GenreExtBase]] = {}


class GenreExtBase(ValidatedModel):
    """题材扩展模型抽象基类。"""

    genre_name: ClassVar[str] = ""
    display_name: ClassVar[str] = ""


@overload
def register_genre(cls_or_func: type[T]) -> type[T]: ...


@overload
def register_genre(cls_or_func: None = None) -> Callable[[type[T]], type[T]]: ...


def register_genre(
    cls_or_func: type[T] | None = None,
) -> type[T] | Callable[[type[T]], type[T]]:
    """题材扩展类注册装饰器/函数。

    支持三种使用形式：
    1. 函数调用：`register_genre(MyGenreExt)`
    2. 无参装饰器：`@register_genre`
    3. 带参装饰器：`@register_genre()`
    """

    def _decorator(cls: type[T]) -> type[T]:
        if not isinstance(cls, type) or not issubclass(cls, GenreExtBase):
            raise TypeError(f"注册的题材扩展类必须继承自 GenreExtBase，收到: {cls!r}")
        name = getattr(cls, "genre_name", "")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"题材扩展类 {cls.__name__} 必须定义非空字符串属性 'genre_name'"
            )
        clean_name = name.strip()
        if clean_name in _GENRE_REGISTRY:
            existing = _GENRE_REGISTRY[clean_name].__name__
            raise ValueError(
                f"Genre '{clean_name}' is already registered by class {existing}. 禁止重复注册"
            )
        _GENRE_REGISTRY[clean_name] = cls
        return cls

    if cls_or_func is not None:
        return _decorator(cls_or_func)
    return _decorator


def get_genre(name: str) -> type[GenreExtBase]:
    """根据题材标识获取对应的扩展模型类。

    Raises:
        KeyError: 当指定题材标识未注册时抛出，提示包含所有已注册题材。
    """
    if name not in _GENRE_REGISTRY:
        available = list_genres()
        raise KeyError(f"未找到题材扩展 '{name}'，已注册题材: {available}")
    return _GENRE_REGISTRY[name]


def list_genres() -> list[str]:
    """返回所有已注册题材标识列表（字母升序排列）。"""
    return sorted(_GENRE_REGISTRY.keys())


def validate_ext(genre_name: str, data: dict[str, Any]) -> dict[str, Any]:
    """根据指定题材扩展模型校验原始字典并输出标准字典。

    Raises:
        KeyError: 当指定题材标识未注册时抛出。
        pydantic.ValidationError: 当数据不符合题材模型校验规则时抛出。
    """
    cls = get_genre(genre_name)
    instance = cls.model_validate(data)
    return instance.model_dump()


# ---------------------------------------------------------------------------
# 内置题材 1: general (通用都市/现实，完全放开 extra)
# ---------------------------------------------------------------------------


@register_genre
class GeneralExt(GenreExtBase):
    """通用都市/现实等无特定限制题材扩展模型。"""

    genre_name: ClassVar[str] = "general"
    display_name: ClassVar[str] = "通用都市/现实"


# ---------------------------------------------------------------------------
# 内置题材 2: cultivation_rpg (修仙/玄幻RPG)
# ---------------------------------------------------------------------------


@register_genre
class CultivationRpgExt(GenreExtBase):
    """修仙/玄幻RPG 题材扩展模型。"""

    genre_name: ClassVar[str] = "cultivation_rpg"
    display_name: ClassVar[str] = "修仙/玄幻RPG"

    realm: str = Field(default="", description="当前修为境界，如'筑基后期'")
    techniques: list[str] = Field(default_factory=list, description="掌握功法/神通列表")
    key_items: list[str] = Field(default_factory=list, description="关键法宝/道具")
    trump_cards: list[str] = Field(default_factory=list, description="保命底牌/大招")


# ---------------------------------------------------------------------------
# 内置题材 3: mystery_intrigue (悬疑/谍战/谋略)
# ---------------------------------------------------------------------------


class Disguise(ValidatedModel):
    """悬疑/谍战伪装马甲。"""

    identity: str = Field(..., min_length=1, description="伪装身份名称")
    known_by: list[EntityId] = Field(default_factory=list, description="已知晓该伪装的实体 ID 列表")


class Clue(ValidatedModel):
    """悬疑/谍战线索证据。"""

    id: str = Field(..., min_length=1, description="线索标识")
    content: str = Field(..., min_length=1, description="线索内容描述")
    found_at_ch: ChapterNum = Field(..., description="发现线索的章号")
    points_to: list[EntityId] = Field(default_factory=list, description="指向的嫌疑实体 ID 列表")


@register_genre
class MysteryIntrigueExt(GenreExtBase):
    """悬疑/谍战/谋略 题材扩展模型。"""

    genre_name: ClassVar[str] = "mystery_intrigue"
    display_name: ClassVar[str] = "悬疑/谍战/谋略"

    disguises: list[Disguise] = Field(default_factory=list, description="当前马甲/伪装身份列表")
    clues_held: list[Clue] = Field(default_factory=list, description="当前掌握的线索证据")
    suspicions: list[str] = Field(default_factory=list, description="当前怀疑推论列表")


__all__ = [
    "Clue",
    "CultivationRpgExt",
    "Disguise",
    "GeneralExt",
    "GenreExtBase",
    "MysteryIntrigueExt",
    "get_genre",
    "list_genres",
    "register_genre",
    "validate_ext",
]
