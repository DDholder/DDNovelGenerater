"""T02.1 公共基础层 (_base.py) 单元测试。"""

import pytest
import yaml
from pydantic import Field, TypeAdapter, ValidationError

from ddnovel.core.schemas._base import (
    ID_PREFIX_CATEGORY,
    ArcId,
    ChapterNum,
    ChapterWindow,
    EntityId,
    EntityStatus,
    FrozenDTO,
    FtsScope,
    HostKind,
    IssueLevel,
    LenientModel,
    SettingCategory,
    StrictModel,
    ThreadAction,
    ThreadId,
    ThreadStatus,
    ValidatedModel,
    VolumeId,
    format_chapter,
)

# ---------------------------------------------------------------------------
# 1. 宽严分级基类行为测试
# ---------------------------------------------------------------------------


class _SampleLenient(LenientModel):
    name: str | None = Field(default=None, alias="title")


class _SampleValidated(ValidatedModel):
    ch: ChapterNum
    note: str = ""


class _SampleStrict(StrictModel):
    total: int = 10000
    label: str = "default"


class _SampleFrozen(FrozenDTO):
    doc_id: str
    window: ChapterWindow


def test_lenient_model_allows_extra_and_strips_whitespace():
    obj = _SampleLenient.model_validate({"title": "  林凡  ", "custom_tag": "主角", "draft_ver": 2})
    assert obj.name == "林凡"
    assert obj.model_extra == {"custom_tag": "主角", "draft_ver": 2}

    obj_by_field = _SampleLenient.model_validate({"name": " 苏瑶 "})
    assert obj_by_field.name == "苏瑶"


def test_validated_model_preserves_extra_for_roundtrip_and_validates_core():
    obj = _SampleValidated.model_validate({"ch": 12, "note": "  受伤  ", "author_memo": "待补伏笔"})
    assert obj.ch == 12
    assert obj.note == "受伤"
    assert obj.model_extra == {"author_memo": "待补伏笔"}
    dumped = obj.model_dump()
    assert dumped["author_memo"] == "待补伏笔"

    with pytest.raises(ValidationError):
        _SampleValidated.model_validate({"ch": 0})


def test_strict_model_forbids_extra_and_validates_assignment():
    with pytest.raises(ValidationError):
        _SampleStrict.model_validate({"total": 8000, "budgte": 100})

    cfg = _SampleStrict(total=9000, label="  custom  ")
    assert cfg.label == "custom"

    with pytest.raises(ValidationError):
        cfg.total = "not_an_int"  # type: ignore[assignment]


def test_frozen_dto_is_immutable_forbids_extra_and_is_hashable():
    dto = _SampleFrozen(doc_id="  doc_01  ", window=(10, 20))  # type: ignore[arg-type]
    assert dto.doc_id == "doc_01"
    assert hash(dto) is not None
    assert {dto: "cached"}[dto] == "cached"

    with pytest.raises(ValidationError):
        dto.doc_id = "doc_02"  # type: ignore[misc]

    with pytest.raises(ValidationError):
        _SampleFrozen.model_validate({"doc_id": "doc_01", "window": [10, 20], "extra_key": 1})


# ---------------------------------------------------------------------------
# 2. 章号与章节窗口约束测试
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (1, 1),
        (142, 142),
        ("42", 42),
    ],
)
def test_chapter_num_valid(raw, expected):
    assert TypeAdapter(ChapterNum).validate_python(raw) == expected


@pytest.mark.parametrize("invalid", [0, -1, -99, True, False, 1.5, "ch_01", ""])
def test_chapter_num_invalid(invalid):
    with pytest.raises(ValidationError):
        TypeAdapter(ChapterNum).validate_python(invalid)


def test_chapter_window_valid_and_dual_access():
    adapter = TypeAdapter(ChapterWindow)
    win = adapter.validate_python([140, 155])

    assert win == (140, 155)
    assert win[0] == 140
    assert win[1] == 155
    assert win.start == 140
    assert win.end == 155
    start, end = win
    assert (start, end) == (140, 155)

    # 单章闭合窗口 start == end 允许
    same = adapter.validate_python((87, 87))
    assert (same.start, same.end) == (87, 87)

    # JSON / YAML 序列化兼容
    json_val = adapter.dump_python(win, mode="json")
    assert json_val == [140, 155]
    reloaded = yaml.safe_load(yaml.safe_dump({"expected_window": json_val}))
    assert adapter.validate_python(reloaded["expected_window"]) == (140, 155)


@pytest.mark.parametrize(
    "invalid_window",
    [
        [155, 140],  # start > end
        (10, 9),
        [0, 10],  # start < 1
        [-1, 5],
        [140],  # 长度不足
        [140, 155, 160],  # 长度超出
        [True, 10],  # 布尔值拒绝
    ],
)
def test_chapter_window_invalid(invalid_window):
    with pytest.raises(ValidationError):
        TypeAdapter(ChapterWindow).validate_python(invalid_window)


# ---------------------------------------------------------------------------
# 3. ID 正则约束正反例测试
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "valid_id",
    [
        "char_linfan",
        "loc_zhuixinggu",
        "loc_beijing_gucheng",
        "fac_qingyunmen",
        "world_tianli",
        "rule_jindan",
        "item_gujing",
        "c1_guard_02",
        "  char_linfan  ",
    ],
)
def test_entity_id_valid(valid_id):
    result = TypeAdapter(EntityId).validate_python(valid_id)
    assert result == valid_id.strip()


@pytest.mark.parametrize(
    "invalid_id",
    [
        "Char_LinFan",  # 大写拒绝
        "CHAR_LINFAN",
        "linfan",  # 缺少前缀下划线
        "_linfan",  # 下划线开头
        "1char_linfan",  # 数字开头
        "char_",  # 下划线后为空
        "char-linfan",  # 连字符非法
        "char_林凡",  # 非 ASCII 小写字母数字
        "",
    ],
)
def test_entity_id_invalid(invalid_id):
    with pytest.raises(ValidationError):
        TypeAdapter(EntityId).validate_python(invalid_id)


@pytest.mark.parametrize("valid_vol", ["vol_01", "vol_12", "vol_100", " vol_02 "])
def test_volume_id_valid(valid_vol):
    assert TypeAdapter(VolumeId).validate_python(valid_vol) == valid_vol.strip()


@pytest.mark.parametrize("invalid_vol", ["vol_1", "VOL_01", "volume_01", "vol_ab", "vol_01_arc_01", ""])
def test_volume_id_invalid(invalid_vol):
    with pytest.raises(ValidationError):
        TypeAdapter(VolumeId).validate_python(invalid_vol)


@pytest.mark.parametrize(
    "valid_arc",
    ["vol_01_arc_01", "vol_01_arc_03", "vol_100_arc_120", " vol_02_arc_05 "],
)
def test_arc_id_valid(valid_arc):
    assert TypeAdapter(ArcId).validate_python(valid_arc) == valid_arc.strip()


@pytest.mark.parametrize(
    "invalid_arc",
    ["vol_1_arc_01", "vol_01_arc_1", "arc_03", "vol_01", "VOL_01_ARC_03", ""],
)
def test_arc_id_invalid(invalid_arc):
    with pytest.raises(ValidationError):
        TypeAdapter(ArcId).validate_python(invalid_arc)


@pytest.mark.parametrize(
    "valid_thread",
    ["thread_042", "thread_1", "thread_ancient_mirror_01", " thread_042 "],
)
def test_thread_id_valid(valid_thread):
    assert TypeAdapter(ThreadId).validate_python(valid_thread) == valid_thread.strip()


@pytest.mark.parametrize("invalid_thread", ["Thread_042", "thread_", "th_042", "thread-042", ""])
def test_thread_id_invalid(invalid_thread):
    with pytest.raises(ValidationError):
        TypeAdapter(ThreadId).validate_python(invalid_thread)


# ---------------------------------------------------------------------------
# 4. 公共枚举、前缀映射、format_chapter 与 JSON Schema 测试
# ---------------------------------------------------------------------------


def test_public_enums_and_prefix_mapping():
    assert set(SettingCategory) == {"world", "rule", "geography", "faction", "character", "item", "other"}
    assert set(EntityStatus) == {"active", "deceased", "retired"}
    assert set(ThreadStatus) == {"planted", "dormant", "active", "resolved"}
    assert "dropped" not in set(ThreadStatus)
    assert set(ThreadAction) == {"plant", "progress", "resolve"}
    assert set(FtsScope) == {"setting", "fact", "summary", "manuscript", "blueprint"}
    assert set(HostKind) == {"opencode", "dsh", "claude", "antigravity"}
    assert set(IssueLevel) == {"info", "warning", "error"}

    assert ID_PREFIX_CATEGORY == {
        "char_": SettingCategory.CHARACTER,
        "loc_": SettingCategory.GEOGRAPHY,
        "fac_": SettingCategory.FACTION,
        "world_": SettingCategory.WORLD,
        "rule_": SettingCategory.RULE,
        "item_": SettingCategory.ITEM,
    }


def test_format_chapter():
    assert format_chapter(1) == "ch_0001"
    assert format_chapter(42) == "ch_0042"
    assert format_chapter(142) == "ch_0142"
    assert format_chapter(10000, width=4) == "ch_10000"
    assert format_chapter(7, width=3) == "ch_007"

    for bad_n in (0, -5, True, False):
        with pytest.raises(ValueError):
            format_chapter(bad_n)  # type: ignore[arg-type]

    for bad_width in (0, -1, True, False):
        with pytest.raises(ValueError):
            format_chapter(1, width=bad_width)  # type: ignore[arg-type]


def test_json_schema_serialization_compatibility():
    ch_schema = TypeAdapter(ChapterNum).json_schema(mode="serialization")
    assert ch_schema == {"minimum": 1, "type": "integer"}

    win_schema = TypeAdapter(ChapterWindow).json_schema(mode="serialization")
    assert win_schema["type"] == "array"
    assert win_schema["minItems"] == 2
    assert win_schema["maxItems"] == 2

    ent_schema = TypeAdapter(EntityId).json_schema(mode="serialization")
    assert ent_schema["type"] == "string"
    assert "pattern" in ent_schema
