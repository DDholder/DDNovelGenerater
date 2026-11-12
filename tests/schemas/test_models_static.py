"""T02.4 设定维、结构维与创作维模型 (models.py 上半部分) 单元测试。"""

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError

from ddnovel.core.schemas._base import EntityStatus, SettingCategory, ThreadAction
from ddnovel.core.schemas.models import (
    ArcDoc,
    Beat,
    BlueprintCast,
    BlueprintDoc,
    SettingDoc,
    SettingFrontmatter,
    StyleGuide,
    ThreadRef,
    TopicRef,
    VolumeDoc,
)

# ---------------------------------------------------------------------------
# 1. 设定维 (SettingFrontmatter / SettingDoc)
# ---------------------------------------------------------------------------


def test_setting_frontmatter_empty_and_lenient_extra():
    empty = SettingFrontmatter.model_validate({})
    assert empty.id is None
    assert empty.name is None
    assert empty.aliases == []
    assert empty.category is None
    assert empty.status == EntityStatus.ACTIVE
    assert empty.tags == []
    assert empty.model_extra == {}

    fm = SettingFrontmatter.model_validate(
        {
            "id": "  char_linfan  ",
            "name": "  林凡  ",
            "aliases": [" 小凡 ", "", "   ", "青云弃徒", "小凡"],
            "category": "character",
            "status": "retired",
            "tags": [" 主角 ", "剑修", "", "主角", "  "],
            "custom_rank": "S",
            "author_memo": "前期隐忍",
        }
    )
    assert fm.id == "char_linfan"
    assert fm.name == "林凡"
    assert fm.aliases == ["小凡", "青云弃徒"]
    assert fm.category == SettingCategory.CHARACTER
    assert fm.status == EntityStatus.RETIRED
    assert fm.tags == ["主角", "剑修"]
    assert fm.model_extra == {"custom_rank": "S", "author_memo": "前期隐忍"}


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"id": "Char_LinFan"},  # ID 大写非法
        {"id": "linfan"},  # 缺少前缀下划线
        {"category": "magic_system"},  # 未知分类枚举
        {"status": "destroyed"},  # 未知状态枚举
    ],
)
def test_setting_frontmatter_invalid_fields(bad_payload):
    with pytest.raises(ValidationError):
        SettingFrontmatter.model_validate(bad_payload)


def test_setting_doc_aliases_dedupe_exclude_name_and_roundtrip():
    raw = {
        "id": "char_linfan",
        "name": "  林凡  ",
        "category": "character",
        "aliases": ["林凡", "  林凡  ", " 小凡 ", "", "   ", "青云弃徒", "小凡", "林师兄"],
        "status": "active",
        "tags": ["主角", " 剑修 ", "", "主角"],
        "file_path": "01_settings/characters/char_linfan.md",
        "core_text": "林凡，青云门外门弟子，性格沉稳。",
        "topics": {
            "剑道感悟": "领悟秋水剑意第一重。",
            "身世线索": "襁褓中携有一枚残缺古镜。",
        },
        "secret_text": "真实身份为上古剑宗遗孤。",
        "custom_note": "核心视角人物",
    }
    doc = SettingDoc.model_validate(raw)

    assert doc.name == "林凡"
    assert doc.aliases == ["小凡", "青云弃徒", "林师兄"]
    assert "林凡" not in doc.aliases
    assert doc.tags == ["主角", "剑修"]
    assert doc.all_names() == ["林凡", "小凡", "青云弃徒", "林师兄"]
    assert doc.core_text == "林凡，青云门外门弟子，性格沉稳。"
    assert doc.topics["剑道感悟"] == "领悟秋水剑意第一重。"
    assert doc.secret_text == "真实身份为上古剑宗遗孤。"
    assert doc.model_extra == {"custom_note": "核心视角人物"}

    # JSON 往返
    from_json = SettingDoc.model_validate_json(doc.model_dump_json())
    assert from_json == doc
    assert from_json.model_extra == {"custom_note": "核心视角人物"}

    # YAML 往返
    yaml_text = yaml.safe_dump(doc.model_dump(mode="json"), allow_unicode=True, sort_keys=False)
    from_yaml = SettingDoc.model_validate(yaml.safe_load(yaml_text))
    assert from_yaml == doc


@pytest.mark.parametrize(
    "invalid_doc",
    [
        {
            "id": "char_linfan",
            "name": "   ",  # strip 后为空串，违反 min_length=1
            "category": "character",
            "file_path": "01_settings/char_linfan.md",
        },
        {
            "id": "char_linfan",
            "name": "林凡",
            "category": "character",
            "file_path": "",  # 空路径违反 min_length=1
        },
    ],
)
def test_setting_doc_rejects_empty_required_strings(invalid_doc):
    with pytest.raises(ValidationError):
        SettingDoc.model_validate(invalid_doc)


# ---------------------------------------------------------------------------
# 2. 结构维：分卷与单元 (VolumeDoc / ArcDoc)
# ---------------------------------------------------------------------------


def test_volume_doc_valid_and_window_validation():
    vol = VolumeDoc.model_validate(
        {
            "id": "vol_01",
            "title": "  潜龙在渊  ",
            "chapter_range": [1, 60],
            "core_conflict": "外门大比与古镜觉醒",
            "folded_summary": None,
            "body": "## 卷纲正文",
            "theme": "成长",
        }
    )
    assert vol.id == "vol_01"
    assert vol.title == "潜龙在渊"
    assert vol.chapter_range == (1, 60)
    assert vol.chapter_range is not None
    assert (vol.chapter_range.start, vol.chapter_range.end) == (1, 60)
    assert vol.model_extra == {"theme": "成长"}

    with pytest.raises(ValidationError):
        VolumeDoc.model_validate({"id": "vol_01", "chapter_range": [60, 1]})

    with pytest.raises(ValidationError):
        VolumeDoc.model_validate({"id": "vol_1"})


def test_arc_doc_infers_and_validates_volume_id():
    # 未提供 volume_id 时自动从 id 推断
    arc_inferred = ArcDoc.model_validate(
        {
            "id": "vol_01_arc_03",
            "title": "坠星谷秘境",
            "chapter_range": [25, 40],
        }
    )
    assert arc_inferred.volume_id == "vol_01"
    assert arc_inferred.chapter_range == (25, 40)

    arc_three_digits = ArcDoc.model_validate({"id": "vol_102_arc_05"})
    assert arc_three_digits.volume_id == "vol_102"

    # 显式提供且一致
    arc_explicit = ArcDoc.model_validate({"id": "vol_02_arc_01", "volume_id": "vol_02"})
    assert arc_explicit.volume_id == "vol_02"

    # 显式提供但前缀冲突
    with pytest.raises(ValidationError, match="volume_id"):
        ArcDoc.model_validate({"id": "vol_01_arc_03", "volume_id": "vol_02"})

    # 边界：vol_01 不能误匹配 vol_010_arc_03
    with pytest.raises(ValidationError, match="volume_id"):
        ArcDoc.model_validate({"id": "vol_010_arc_03", "volume_id": "vol_01"})

    # 非法章节区间拒绝
    with pytest.raises(ValidationError):
        ArcDoc.model_validate({"id": "vol_01_arc_01", "chapter_range": [30, 20]})


# ---------------------------------------------------------------------------
# 3. 结构维：章节蓝图及子结构 (Beat / BlueprintCast / BlueprintDoc)
# ---------------------------------------------------------------------------


def test_beat_valid_and_rejects_bool_or_non_positive():
    beat = Beat.model_validate({"index": 1, "title": "  夜探藏经阁  ", "content": "林凡潜入二层。"})
    assert beat.index == 1
    assert beat.title == "夜探藏经阁"

    for bad_idx in (True, False, 0, -1, "abc"):
        with pytest.raises(ValidationError):
            Beat.model_validate({"index": bad_idx, "title": "节拍"})


def test_blueprint_cast_and_topic_ref():
    cast = BlueprintCast.model_validate(
        {
            "characters": ["char_linfan", "char_suyao", "char_linfan"],
            "locations": ["loc_zhuixinggu", "loc_zhuixinggu"],
            "factions": ["fac_qingyunmen", "fac_xuanyinzong", "fac_qingyunmen"],
            "items": ["item_gujing", "item_gujing"],
        }
    )
    assert cast.characters == ["char_linfan", "char_suyao"]
    assert cast.locations == ["loc_zhuixinggu"]
    assert cast.factions == ["fac_qingyunmen", "fac_xuanyinzong"]
    assert cast.items == ["item_gujing"]

    topic = TopicRef.model_validate({"entity_id": "loc_zhuixinggu", "topic": "  空间裂隙  "})
    assert topic.topic == "空间裂隙"

    with pytest.raises(ValidationError):
        TopicRef.model_validate({"entity_id": "loc_zhuixinggu", "topic": "   "})


def test_blueprint_doc_minimal_and_auto_fixes():
    minimal = BlueprintDoc.model_validate({"chapter": 1})
    assert minimal.chapter == 1
    assert minimal.pov is None
    assert minimal.cast == BlueprintCast()
    assert minimal.all_entity_ids() == []

    # 共享 cast 实例测试：自动补入 pov 不应污染外部传入的 BlueprintCast 实例
    external_cast = BlueprintCast(
        characters=["char_suyao"],
        locations=["loc_zhuixinggu"],
        factions=["fac_qingyunmen"],
        items=["item_gujing"],
    )
    bp = BlueprintDoc(
        chapter=42,
        volume_id="vol_01",
        arc_id="vol_01_arc_03",
        title="古镜苏醒",
        pov="char_linfan",
        cast=external_cast,
        threads=[
            ThreadRef(thread_id="thread_001", action=ThreadAction.PROGRESS),
            ThreadRef(thread_id="thread_042", action=ThreadAction.PLANT),
        ],
        unlock_secrets=["char_suyao", "item_ancient_seal", "char_suyao"],
        topics=[
            TopicRef(entity_id="loc_zhuixinggu", topic="禁制阵眼"),
            TopicRef(entity_id="rule_jindan", topic="灵气潮汐"),
        ],
        forbidden=["  禁止提前暴露幕后黑手  ", "", "   ", "禁止出现现代词汇", "禁止提前暴露幕后黑手"],
        beats=[
            Beat(index=1, title="入谷", content="林凡与苏瑶踏入谷口。"),
            Beat(index=2, title="破阵", content="古镜产生共鸣。"),
        ],
        body="本章重点刻画气氛压迫感。",
    )

    assert external_cast.characters == ["char_suyao"]
    assert bp.cast.characters == ["char_suyao", "char_linfan"]
    assert bp.unlock_secrets == ["char_suyao", "item_ancient_seal"]
    assert bp.forbidden == ["禁止提前暴露幕后黑手", "禁止出现现代词汇"]

    # 验证汇总顺序：pov -> cast(char/loc/fac/item) -> unlock_secrets -> topics.entity_id
    assert bp.all_entity_ids() == [
        "char_linfan",
        "char_suyao",
        "loc_zhuixinggu",
        "fac_qingyunmen",
        "item_gujing",
        "item_ancient_seal",
        "rule_jindan",
    ]

    # pov 已在 cast.characters 中时不重复追加
    bp_existing_pov = BlueprintDoc.model_validate(
        {
            "chapter": 43,
            "pov": "char_linfan",
            "cast": {"characters": ["char_linfan", "char_suyao"]},
        }
    )
    assert bp_existing_pov.cast.characters == ["char_linfan", "char_suyao"]


def test_blueprint_doc_rejects_duplicate_threads_and_mismatched_arc():
    # 同一 thread_id 不可在同一章蓝图重复出现
    with pytest.raises(ValidationError, match="thread_001"):
        BlueprintDoc.model_validate(
            {
                "chapter": 10,
                "threads": [
                    {"thread_id": "thread_001", "action": "plant"},
                    {"thread_id": "thread_001", "action": "progress"},
                ],
            }
        )

    # volume_id 与 arc_id 同时提供但前缀不一致
    with pytest.raises(ValidationError, match="volume_id"):
        BlueprintDoc.model_validate(
            {
                "chapter": 10,
                "volume_id": "vol_01",
                "arc_id": "vol_02_arc_01",
            }
        )

    with pytest.raises(ValidationError, match="volume_id"):
        BlueprintDoc.model_validate(
            {
                "chapter": 10,
                "volume_id": "vol_01",
                "arc_id": "vol_010_arc_01",
            }
        )

    # 仅给 arc_id、不给 volume_id 时允许加载
    only_arc = BlueprintDoc.model_validate({"chapter": 10, "arc_id": "vol_02_arc_01"})
    assert only_arc.volume_id is None
    assert only_arc.arc_id == "vol_02_arc_01"


# ---------------------------------------------------------------------------
# 4. 创作维：文风与红线指南 (StyleGuide) 及 JSON Schema 兼容性
# ---------------------------------------------------------------------------


def test_style_guide_dedupe_extra_preservation_and_roundtrip():
    guide = StyleGuide.model_validate(
        {
            "tone": "  冷峻克制，注重环境烘托与细节质感  ",
            "pov_rules": [
                " 严格第三人称有限视角 ",
                "",
                "禁止全知视角剧透未揭示信息",
                "严格第三人称有限视角",
                "   ",
            ],
            "banned_words": [" 恐怖如斯 ", "倒吸一口凉气", "", "恐怖如斯"],
            "raw_markdown": "# 文风指南\n\n保持叙事克制。",
            "dialogue_ratio": "35%",
            "custom_checklist": ["检查感官描写"],
        }
    )
    assert guide.tone == "冷峻克制，注重环境烘托与细节质感"
    assert guide.pov_rules == ["严格第三人称有限视角", "禁止全知视角剧透未揭示信息"]
    assert guide.banned_words == ["恐怖如斯", "倒吸一口凉气"]
    assert guide.model_extra == {"dialogue_ratio": "35%", "custom_checklist": ["检查感官描写"]}

    # YAML 与 JSON 往返无损保留 extra 字段
    dumped = guide.model_dump(mode="json")
    assert dumped["dialogue_ratio"] == "35%"
    reloaded_yaml = StyleGuide.model_validate(yaml.safe_load(yaml.safe_dump(dumped, allow_unicode=True)))
    assert reloaded_yaml == guide
    assert reloaded_yaml.model_extra == guide.model_extra

    reloaded_json = StyleGuide.model_validate_json(guide.model_dump_json())
    assert reloaded_json == guide


@pytest.mark.parametrize(
    "model_cls",
    [
        SettingFrontmatter,
        SettingDoc,
        VolumeDoc,
        ArcDoc,
        BlueprintCast,
        ThreadRef,
        TopicRef,
        Beat,
        BlueprintDoc,
        StyleGuide,
    ],
)
def test_static_models_json_schema_exportable(model_cls):
    schema = TypeAdapter(model_cls).json_schema(mode="serialization")
    assert schema["type"] == "object"
    assert "properties" in schema
