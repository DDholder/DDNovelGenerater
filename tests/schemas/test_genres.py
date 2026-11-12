"""T02.3 题材扩展注册表与内置题材扩展模型单元测试。"""

from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import Field, ValidationError

from ddnovel.core.schemas.genres import (
    _GENRE_REGISTRY,
    Clue,
    CultivationRpgExt,
    Disguise,
    GeneralExt,
    GenreExtBase,
    MysteryIntrigueExt,
    get_genre,
    list_genres,
    register_genre,
    validate_ext,
)


@pytest.fixture
def clean_registry():
    """保证测试注册自定义题材后恢复原状，不污染其他测试用例。"""
    backup = dict(_GENRE_REGISTRY)
    try:
        yield
    finally:
        _GENRE_REGISTRY.clear()
        _GENRE_REGISTRY.update(backup)


# ---------------------------------------------------------------------------
# 1. 内置题材与注册表基础查询测试
# ---------------------------------------------------------------------------


def test_built_in_genres_registered_at_import():
    """导入时默认自动注册 3 个内置题材。"""
    registered = list_genres()
    assert registered == ["cultivation_rpg", "general", "mystery_intrigue"]
    assert get_genre("general") is GeneralExt
    assert get_genre("cultivation_rpg") is CultivationRpgExt
    assert get_genre("mystery_intrigue") is MysteryIntrigueExt


def test_get_genre_unknown_raises_key_error():
    """查询未注册题材时抛出 KeyError，并在异常信息中罗列当前可选题材。"""
    with pytest.raises(KeyError) as exc_info:
        get_genre("cyberpunk_scifi")
    msg = str(exc_info.value)
    assert "cyberpunk_scifi" in msg
    assert "cultivation_rpg" in msg
    assert "general" in msg
    assert "mystery_intrigue" in msg


# ---------------------------------------------------------------------------
# 2. 注册机制与异常分支测试
# ---------------------------------------------------------------------------


def test_duplicate_registration_raises_value_error():
    """重复注册已存在的题材标识时抛出 ValueError。"""
    with pytest.raises(ValueError, match="already registered"):
        @register_genre
        class AnotherGeneralExt(GenreExtBase):
            genre_name: ClassVar[str] = "general"


def test_register_genre_with_invalid_type_raises_type_error():
    """注册非类对象或未继承 GenreExtBase 的类时抛出 TypeError。"""
    with pytest.raises(TypeError, match="必须继承自 GenreExtBase"):
        register_genre(12345)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="必须继承自 GenreExtBase"):
        class NotAGenreExt:
            genre_name = "test"

        register_genre(NotAGenreExt)  # type: ignore[arg-type]


def test_register_genre_with_missing_or_empty_genre_name_raises_value_error():
    """未声明 genre_name 或为空字符串时抛出 ValueError。"""
    with pytest.raises(ValueError, match="genre_name"):
        class EmptyNameExt(GenreExtBase):
            pass

        register_genre(EmptyNameExt)

    with pytest.raises(ValueError, match="genre_name"):
        class BlankNameExt(GenreExtBase):
            genre_name: ClassVar[str] = "   "

        register_genre(BlankNameExt)


def test_register_genre_call_styles_and_restoration(clean_registry):
    """验证三种注册形式：函数调用、无参装饰器、带参装饰器均能正常工作。"""
    # 形式 1: 函数调用
    class SciFiExt(GenreExtBase):
        genre_name: ClassVar[str] = "scifi"
        display_name: ClassVar[str] = "科幻星际"
        tech_level: int = 1

    registered_scifi = register_genre(SciFiExt)
    assert registered_scifi is SciFiExt
    assert get_genre("scifi") is SciFiExt

    # 形式 2: 无参装饰器
    @register_genre
    class WesternExt(GenreExtBase):
        genre_name: ClassVar[str] = "western_fantasy"
        display_name: ClassVar[str] = "西幻中世纪"
        magic_system: str = "elements"

    assert get_genre("western_fantasy") is WesternExt

    # 形式 3: 带参装饰器
    @register_genre()
    class CyberpunkExt(GenreExtBase):
        genre_name: ClassVar[str] = "cyberpunk"
        display_name: ClassVar[str] = "赛博朋克"
        implants: list[str] = Field(default_factory=list)

    assert get_genre("cyberpunk") is CyberpunkExt

    # 验证排序列表包含新题材
    genres = list_genres()
    assert "scifi" in genres
    assert "western_fantasy" in genres
    assert "cyberpunk" in genres
    assert genres == sorted(genres)


# ---------------------------------------------------------------------------
# 3. 内置题材 1: GeneralExt 测试
# ---------------------------------------------------------------------------


def test_general_ext_defaults_and_extra_fields():
    """GeneralExt 默认值为空且允许任意自定义扩展字段。"""
    assert GeneralExt.genre_name == "general"
    assert GeneralExt.display_name == "通用都市/现实"

    # 空参数实例化
    obj = GeneralExt()
    assert obj.model_dump() == {}

    # 允许额外字段 (extra="allow") 并无损保留
    data = {
        "company": "晨星科技",
        "net_worth": 50000000,
        "social_rank": "亿万富豪",
    }
    custom_obj = GeneralExt.model_validate(data)
    assert custom_obj.model_extra["company"] == "晨星科技"
    assert custom_obj.model_extra["net_worth"] == 50000000
    dumped = custom_obj.model_dump()
    assert dumped == {
        "company": "晨星科技",
        "net_worth": 50000000,
        "social_rank": "亿万富豪",
    }


# ---------------------------------------------------------------------------
# 4. 内置题材 2: CultivationRpgExt 测试
# ---------------------------------------------------------------------------


def test_cultivation_rpg_ext_defaults_and_population():
    """CultivationRpgExt 默认结构与正确参数实例化测试。"""
    assert CultivationRpgExt.genre_name == "cultivation_rpg"
    assert CultivationRpgExt.display_name == "修仙/玄幻RPG"

    # 默认值
    default_obj = CultivationRpgExt()
    assert default_obj.realm == ""
    assert default_obj.techniques == []
    assert default_obj.key_items == []
    assert default_obj.trump_cards == []

    # 完整赋值与额外字段
    payload = {
        "realm": " 筑基中期 ",
        "techniques": ["青元剑诀", "罗烟步"],
        "key_items": ["掌天瓶", "玄铁飞剑"],
        "trump_cards": ["青竹蜂云剑大阵"],
        "spiritual_root": "四属性伪灵根",
    }
    obj = CultivationRpgExt.model_validate(payload)
    assert obj.realm == "筑基中期"
    assert obj.techniques == ["青元剑诀", "罗烟步"]
    assert obj.key_items == ["掌天瓶", "玄铁飞剑"]
    assert obj.trump_cards == ["青竹蜂云剑大阵"]
    assert obj.model_extra["spiritual_root"] == "四属性伪灵根"

    dumped = obj.model_dump()
    assert dumped["realm"] == "筑基中期"
    assert dumped["spiritual_root"] == "四属性伪灵根"


def test_cultivation_rpg_ext_invalid_type_raises_validation_error():
    """CultivationRpgExt 字段类型不匹配时抛出 ValidationError。"""
    # realm 传入列表或字典等非法类型
    with pytest.raises(ValidationError):
        CultivationRpgExt.model_validate({"realm": ["炼气期", "筑基期"]})

    # techniques 传入非 list 类型
    with pytest.raises(ValidationError):
        CultivationRpgExt.model_validate({"techniques": 12345})


# ---------------------------------------------------------------------------
# 5. 内置题材 3: MysteryIntrigueExt、Disguise 与 Clue 测试
# ---------------------------------------------------------------------------


def test_disguise_valid_and_invalid():
    """Disguise 结构与实体 ID 正则校验。"""
    # 合法伪装
    d = Disguise(
        identity="  仁济堂刘大夫  ",
        known_by=["char_linfan", "char_elder_chen"],
    )
    assert d.identity == "仁济堂刘大夫"
    assert d.known_by == ["char_linfan", "char_elder_chen"]

    # 非法已知实体 ID（不匹配 ENTITY_ID_PATTERN）
    with pytest.raises(ValidationError):
        Disguise(identity="掌柜", known_by=["Invalid_ID"])

    # identity 不能为空
    with pytest.raises(ValidationError):
        Disguise(identity="")


def test_clue_valid_and_invalid():
    """Clue 结构、章号约束与实体 ID 校验。"""
    # 合法线索
    c = Clue(
        id=" clue_bloody_jade ",
        content=" 现场遗留的染血双鱼玉佩 ",
        found_at_ch=12,
        points_to=["char_shadow_killer"],
    )
    assert c.id == "clue_bloody_jade"
    assert c.content == "现场遗留的染血双鱼玉佩"
    assert c.found_at_ch == 12
    assert c.points_to == ["char_shadow_killer"]

    # 章号必须 >= 1，不能为 0 或负数
    with pytest.raises(ValidationError):
        Clue(id="c1", content="证据", found_at_ch=0)

    with pytest.raises(ValidationError):
        Clue(id="c1", content="证据", found_at_ch=-5)

    # 章号禁止传入布尔值 (防止 True 被当作 1)
    with pytest.raises(ValidationError):
        Clue(id="c1", content="证据", found_at_ch=True)  # type: ignore[arg-type]

    # points_to 包含非法实体 ID
    with pytest.raises(ValidationError):
        Clue(id="c1", content="证据", found_at_ch=1, points_to=["BAD_ID!"])


def test_mystery_intrigue_ext_defaults_and_nested_validation():
    """MysteryIntrigueExt 默认值与嵌套 Disguise / Clue 校验。"""
    assert MysteryIntrigueExt.genre_name == "mystery_intrigue"
    assert MysteryIntrigueExt.display_name == "悬疑/谍战/谋略"

    # 默认值
    default_obj = MysteryIntrigueExt()
    assert default_obj.disguises == []
    assert default_obj.clues_held == []
    assert default_obj.suspicions == []

    # 嵌套数据字典反序列化
    raw_data = {
        "disguises": [
            {"identity": "当铺掌柜", "known_by": ["char_hero"]},
        ],
        "clues_held": [
            {
                "id": "clue_letter",
                "content": "半截密信",
                "found_at_ch": 5,
                "points_to": ["char_traitor"],
            }
        ],
        "suspicions": ["内阁首辅与黑龙会有资金往来"],
        "extra_note": "机密线索记录",
    }
    ext = MysteryIntrigueExt.model_validate(raw_data)
    assert len(ext.disguises) == 1
    assert ext.disguises[0].identity == "当铺掌柜"
    assert len(ext.clues_held) == 1
    assert ext.clues_held[0].id == "clue_letter"
    assert ext.clues_held[0].found_at_ch == 5
    assert ext.suspicions == ["内阁首辅与黑龙会有资金往来"]
    assert ext.model_extra["extra_note"] == "机密线索记录"

    dumped = ext.model_dump()
    assert dumped["disguises"][0]["identity"] == "当铺掌柜"
    assert dumped["clues_held"][0]["found_at_ch"] == 5
    assert dumped["extra_note"] == "机密线索记录"


def test_mystery_intrigue_ext_nested_validation_error():
    """嵌套非法 Disguise / Clue 数据时抛出 ValidationError。"""
    bad_data = {
        "clues_held": [
            {
                "id": "clue_bad",
                "content": "无效章号证据",
                "found_at_ch": 0,
            }
        ]
    }
    with pytest.raises(ValidationError):
        MysteryIntrigueExt.model_validate(bad_data)


# ---------------------------------------------------------------------------
# 6. validate_ext 快捷校验函数测试
# ---------------------------------------------------------------------------


def test_validate_ext_success_cases():
    """validate_ext 能根据题材名正确校验并返回规范字典。"""
    # general
    res_gen = validate_ext("general", {"job": "软件工程师", "salary": 20000})
    assert res_gen == {"job": "软件工程师", "salary": 20000}

    # cultivation_rpg
    res_cult = validate_ext(
        "cultivation_rpg",
        {
            "realm": "化神初期",
            "techniques": ["一气化三清"],
        },
    )
    assert res_cult["realm"] == "化神初期"
    assert res_cult["techniques"] == ["一气化三清"]
    assert res_cult["key_items"] == []
    assert res_cult["trump_cards"] == []

    # mystery_intrigue
    res_mys = validate_ext(
        "mystery_intrigue",
        {
            "suspicions": ["副官有嫌疑"],
        },
    )
    assert res_mys["suspicions"] == ["副官有嫌疑"]
    assert res_mys["disguises"] == []
    assert res_mys["clues_held"] == []


def test_validate_ext_unknown_genre_raises_key_error():
    """validate_ext 传入未注册题材标识时抛出 KeyError。"""
    with pytest.raises(KeyError, match="未找到题材扩展 'non_existent'"):
        validate_ext("non_existent", {})


def test_validate_ext_invalid_data_raises_validation_error():
    """validate_ext 传入非法数据时抛出 ValidationError。"""
    with pytest.raises(ValidationError):
        validate_ext("cultivation_rpg", {"realm": ["非法列表类型"]})


# ---------------------------------------------------------------------------
# 7. GenreExtBase 与公共导出测试
# ---------------------------------------------------------------------------


def test_genre_ext_base_attributes_and_registration_rejection():
    """GenreExtBase 基类默认属性为空，直接注册该基类应当抛出 ValueError。"""
    assert GenreExtBase.genre_name == ""
    assert GenreExtBase.display_name == ""

    # 直接注册未定义具体 genre_name 的抽象基类应被拒绝
    with pytest.raises(ValueError, match="genre_name"):
        register_genre(GenreExtBase)


def test_custom_genre_roundtrip_with_validate_ext(clean_registry):
    """自定义题材注册后，validate_ext 能正常校验并返回标准 dict。"""
    @register_genre
    class ApocalypseExt(GenreExtBase):
        genre_name: ClassVar[str] = "apocalypse"
        display_name: ClassVar[str] = "末世危机"

        radiation_level: int = Field(default=0, ge=0)
        shelter_id: str = ""

    payload = {"radiation_level": 4, "shelter_id": "shelter_07", "extra_supplies": 50}
    validated = validate_ext("apocalypse", payload)
    assert validated == {
        "radiation_level": 4,
        "shelter_id": "shelter_07",
        "extra_supplies": 50,
    }


def test_public_all_exports():
    """验证模块 __all__ 符号导出完整且符合规范。"""
    import ddnovel.core.schemas.genres as genres_mod

    expected = [
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
    assert sorted(genres_mod.__all__) == sorted(expected)
    for sym in genres_mod.__all__:
        assert hasattr(genres_mod, sym)

