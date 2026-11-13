"""T02.5 状态维模型、文件容器与题材上下文校验 (models.py 下半部分) 单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError

import ddnovel.core.schemas.models as models_mod
from ddnovel.core.schemas._base import EntityStatus, ThreadStatus
from ddnovel.core.schemas.config import NovelConfig
from ddnovel.core.schemas.models import (
    ChapterFactRecord,
    CharacterState,
    CharacterStateFile,
    EpistemicBounds,
    FactionState,
    FactionStateFile,
    RelationsFile,
    TemporalRelation,
    Thread,
    ThreadsFile,
    WorldState,
)

FIXTURES_BOOKS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "books"


# ---------------------------------------------------------------------------
# 1. EpistemicBounds 测试
# ---------------------------------------------------------------------------


def test_epistemic_bounds_defaults_cleaning_and_extra():
    empty = EpistemicBounds()
    assert empty.knows == []
    assert empty.unaware_of == []
    assert empty.misbelieves == []
    assert empty.model_extra == {}

    bounds = EpistemicBounds.model_validate(
        {
            "knows": [" 线索A ", "", "   ", "线索B", "线索A"],
            "unaware_of": ["  幕后黑手身份  ", "  ", "幕后黑手身份"],
            "misbelieves": ["以为师尊仍在闭关", "", "以为师尊仍在闭关"],
            "custom_tag": "pov_critical",
        }
    )
    assert bounds.knows == ["线索A", "线索B"]
    assert bounds.unaware_of == ["幕后黑手身份"]
    assert bounds.misbelieves == ["以为师尊仍在闭关"]
    assert bounds.model_extra == {"custom_tag": "pov_critical"}


# ---------------------------------------------------------------------------
# 2. CharacterState & CharacterStateFile 测试
# ---------------------------------------------------------------------------


def test_character_state_minimal_and_invalid_fields():
    cs = CharacterState.model_validate(
        {
            "updated_at_ch": 1,
            "current_goals": [" 突破炼气 ", "", "   ", "突破炼气"],
        }
    )
    assert cs.updated_at_ch == 1
    assert cs.location is None
    assert cs.physical_mental == ""
    assert cs.current_goals == ["突破炼气"]
    assert cs.epistemic_bounds == EpistemicBounds()
    assert cs.profile_ext == {}

    for bad_ch in (0, -1, True, False, "abc"):
        with pytest.raises(ValidationError):
            CharacterState.model_validate({"updated_at_ch": bad_ch})

    with pytest.raises(ValidationError):
        CharacterState.model_validate({"updated_at_ch": 1, "location": "Invalid_Loc"})


def test_character_state_blueprint_snippet_exact_load():
    """与 architecture-blueprint.md §3.2 characters.yaml 示例片段逐字兼容。"""
    blueprint_yaml = """
char_linfan:
  updated_at_ch: 141
  location: loc_beijing_gucheng
  physical_mental: "左肩剑伤未愈，因刚得知师门变故而表面冷静实则高度戒备"
  current_goals:
    - "混入北境拍卖会查清古镜来历"
  epistemic_bounds:
    knows:
      - "赵乾死前曾提及血魂印"
    unaware_of:
      - "同行旅伴苏瑶实为北境妖庭圣女（严禁在林凡视角描写中看破）"
    misbelieves:
      - "以为师尊玄尘仍在闭死关"
  profile_ext:
    realm: "筑基后期"
    key_items: ["残缺青铜古镜", "三枚破障丹"]
    trump_cards: ["惊雷剑诀（每日限用一剑）"]
"""
    raw = yaml.safe_load(blueprint_yaml)
    loaded = CharacterStateFile.model_validate(
        raw, context={"genre_profile": "cultivation_rpg"}
    )
    linfan = loaded.root["char_linfan"]
    assert linfan.updated_at_ch == 141
    assert linfan.location == "loc_beijing_gucheng"
    assert linfan.epistemic_bounds.unaware_of == [
        "同行旅伴苏瑶实为北境妖庭圣女（严禁在林凡视角描写中看破）"
    ]
    assert linfan.profile_ext["realm"] == "筑基后期"
    assert linfan.profile_ext["techniques"] == []
    assert linfan.profile_ext["key_items"] == ["残缺青铜古镜", "三枚破障丹"]


def test_character_state_genre_context_validation():
    # 无 context 或空 context 时，profile_ext 原样保留不报错
    raw_arbitrary = {
        "updated_at_ch": 10,
        "profile_ext": {"realm": ["任意未校验结构"], "nested": {"x": 1}},
    }
    no_ctx = CharacterState.model_validate(raw_arbitrary)
    assert no_ctx.profile_ext == {"realm": ["任意未校验结构"], "nested": {"x": 1}}

    empty_ctx = CharacterState.model_validate(raw_arbitrary, context={})
    assert empty_ctx.profile_ext == {"realm": ["任意未校验结构"], "nested": {"x": 1}}

    # cultivation_rpg 校验与默认值归一化
    cult_ok = CharacterState.model_validate(
        {"updated_at_ch": 10, "profile_ext": {"realm": " 筑基中期 "}},
        context={"genre_profile": "cultivation_rpg"},
    )
    assert cult_ok.profile_ext == {
        "realm": "筑基中期",
        "techniques": [],
        "key_items": [],
        "trump_cards": [],
    }

    with pytest.raises(ValidationError) as exc_cult:
        CharacterState.model_validate(
            {"updated_at_ch": 10, "profile_ext": {"realm": ["bad"]}},
            context={"genre_profile": "cultivation_rpg"},
        )
    assert any(err["loc"] == ("profile_ext", "realm") for err in exc_cult.value.errors())

    # mystery_intrigue 嵌套校验 loc 精确传递
    with pytest.raises(ValidationError) as exc_mys:
        CharacterState.model_validate(
            {
                "updated_at_ch": 20,
                "profile_ext": {
                    "clues_held": [{"id": "c1", "content": "密信", "found_at_ch": 0}]
                },
            },
            context={"genre_profile": "mystery_intrigue"},
        )
    assert any(
        err["loc"] == ("profile_ext", "clues_held", 0, "found_at_ch")
        for err in exc_mys.value.errors()
    )

    # general 放行自定义扩展字段
    gen_ok = CharacterState.model_validate(
        {"updated_at_ch": 5, "profile_ext": {"equity_ratio": 0.52}},
        context={"genre_profile": "general"},
    )
    assert gen_ok.profile_ext == {"equity_ratio": 0.52}

    # 未注册题材与非字符串 context['genre_profile'] 报错
    with pytest.raises(ValidationError, match="cyberpunk"):
        CharacterState.model_validate(
            {"updated_at_ch": 1, "profile_ext": {}},
            context={"genre_profile": "cyberpunk"},
        )

    with pytest.raises(ValidationError, match="字符串"):
        CharacterState.model_validate(
            {"updated_at_ch": 1, "profile_ext": {}},
            context={"genre_profile": 123},
        )


def test_character_state_file_container_and_roundtrip():
    empty = CharacterStateFile()
    assert empty.root == {}
    assert empty.model_dump() == {}

    payload = {
        "char_linfan": {
            "updated_at_ch": 12,
            "location": "loc_beijing_gucheng",
            "profile_ext": {"realm": "筑基后期"},
        },
        "char_suyao": {
            "updated_at_ch": 12,
            "profile_ext": {"realm": "筑基圆满", "techniques": ["幻月敛息术"]},
        },
    }
    loaded = CharacterStateFile.model_validate(
        payload, context={"genre_profile": "cultivation_rpg"}
    )
    assert set(loaded.root.keys()) == {"char_linfan", "char_suyao"}
    assert loaded.root["char_suyao"].profile_ext["techniques"] == ["幻月敛息术"]

    # 容器向内透传 context 校验非法字段
    with pytest.raises(ValidationError) as exc_info:
        CharacterStateFile.model_validate(
            {"char_linfan": {"updated_at_ch": 12, "profile_ext": {"realm": ["bad"]}}},
            context={"genre_profile": "cultivation_rpg"},
        )
    assert any(
        err["loc"] == ("char_linfan", "profile_ext", "realm")
        for err in exc_info.value.errors()
    )

    # 非法 EntityId 键名拒绝
    with pytest.raises(ValidationError):
        CharacterStateFile.model_validate({"Char_LinFan": {"updated_at_ch": 1}})

    # YAML / JSON 往返一致
    yaml_text = yaml.safe_dump(loaded.model_dump(mode="json"), allow_unicode=True, sort_keys=False)
    from_yaml = CharacterStateFile.model_validate(
        yaml.safe_load(yaml_text), context={"genre_profile": "cultivation_rpg"}
    )
    assert from_yaml == loaded

    from_json = CharacterStateFile.model_validate_json(
        loaded.model_dump_json(), context={"genre_profile": "cultivation_rpg"}
    )
    assert from_json == loaded


# ---------------------------------------------------------------------------
# 3. FactionState & FactionStateFile 测试
# ---------------------------------------------------------------------------


def test_faction_state_and_file_validation_and_roundtrip():
    empty = FactionStateFile()
    assert empty.root == {}

    fac = FactionState.model_validate(
        {
            "updated_at_ch": 140,
            "status": "retired",
            "leader": "char_suyao",
            "territory": ["loc_zhuixinggu", "loc_beijing_gucheng", "loc_zhuixinggu"],
            "current_goals": [" 封锁消息 ", "", "搜捕弃徒", "封锁消息"],
            "profile_ext": {"realm": "金丹宗门"},
            "extra_note": "势力备注",
        },
        context={"genre_profile": "cultivation_rpg"},
    )
    assert fac.status == EntityStatus.RETIRED
    assert fac.territory == ["loc_zhuixinggu", "loc_beijing_gucheng"]
    assert fac.current_goals == ["封锁消息", "搜捕弃徒"]
    assert fac.profile_ext["realm"] == "金丹宗门"
    assert fac.model_extra == {"extra_note": "势力备注"}

    with pytest.raises(ValidationError):
        FactionState.model_validate({"updated_at_ch": 10, "status": "destroyed"})

    with pytest.raises(ValidationError):
        FactionState.model_validate(
            {"updated_at_ch": 10, "profile_ext": {"realm": 999}},
            context={"genre_profile": "cultivation_rpg"},
        )

    with pytest.raises(ValidationError):
        FactionStateFile.model_validate({"InvalidFaction": {"updated_at_ch": 10}})

    fac_file = FactionStateFile.model_validate(
        {"fac_qingyunmen": fac.model_dump(mode="json")},
        context={"genre_profile": "cultivation_rpg"},
    )
    reloaded = FactionStateFile.model_validate_json(
        fac_file.model_dump_json(), context={"genre_profile": "cultivation_rpg"}
    )
    assert reloaded == fac_file


# ---------------------------------------------------------------------------
# 4. WorldState 测试
# ---------------------------------------------------------------------------


def test_world_state_validation_and_extra():
    init_state = WorldState()
    assert init_state.current_chapter == 0
    assert init_state.in_world_time == ""
    assert init_state.macro_situation == ""

    ws = WorldState.model_validate(
        {
            "current_chapter": 141,
            "in_world_time": "  天历三年冬·腊月初八  ",
            "macro_situation": "北境极寒暴雪封山。",
            "weather": "暴雪",
        }
    )
    assert ws.current_chapter == 141
    assert ws.in_world_time == "天历三年冬·腊月初八"
    assert ws.model_extra == {"weather": "暴雪"}
    assert WorldState.model_validate_json(ws.model_dump_json()) == ws

    for bad_ch in (-1, True, False, "abc"):
        with pytest.raises(ValidationError):
            WorldState.model_validate({"current_chapter": bad_ch})


# ---------------------------------------------------------------------------
# 5. TemporalRelation & RelationsFile 测试
# ---------------------------------------------------------------------------


def test_temporal_relation_is_valid_at_and_boundaries():
    old_edge = TemporalRelation(
        source="char_linfan",
        target="fac_qingyunmen",
        relation="落霞峰内门真传弟子",
        valid_from_ch=1,
        invalid_at_ch=120,
    )
    assert old_edge.is_valid_at(1) is True
    assert old_edge.is_valid_at(119) is True
    assert old_edge.is_valid_at(120) is False
    assert old_edge.is_valid_at(121) is False

    new_edge = TemporalRelation(
        source="char_linfan",
        target="fac_qingyunmen",
        relation="流亡弃徒",
        valid_from_ch=121,
        invalid_at_ch=None,
    )
    assert new_edge.is_valid_at(120) is False
    assert new_edge.is_valid_at(121) is True
    assert new_edge.is_valid_at(999) is True

    for bad_ch in (0, -1, True, False):
        with pytest.raises(ValueError):
            old_edge.is_valid_at(bad_ch)  # type: ignore[arg-type]


def test_temporal_relation_rejects_invalid_edges_and_relations_file_allows_multi_period():
    with pytest.raises(ValidationError, match="同一实体"):
        TemporalRelation.model_validate(
            {
                "source": "char_linfan",
                "target": "char_linfan",
                "relation": "自言自语",
                "valid_from_ch": 1,
            }
        )

    with pytest.raises(ValidationError, match="invalid_at_ch"):
        TemporalRelation.model_validate(
            {
                "source": "char_linfan",
                "target": "char_suyao",
                "relation": "同门",
                "valid_from_ch": 10,
                "invalid_at_ch": 10,
            }
        )

    with pytest.raises(ValidationError, match="invalid_at_ch"):
        TemporalRelation.model_validate(
            {
                "source": "char_linfan",
                "target": "char_suyao",
                "relation": "同门",
                "valid_from_ch": 10,
                "invalid_at_ch": 9,
            }
        )

    with pytest.raises(ValidationError):
        TemporalRelation.model_validate(
            {
                "source": "char_linfan",
                "target": "char_suyao",
                "relation": "   ",
                "valid_from_ch": 1,
            }
        )

    # RelationsFile 允许同一对实体在不同章节区间有多条演进边
    rel_file = RelationsFile.model_validate(
        {
            "relations": [
                {
                    "source": "char_linfan",
                    "target": "fac_qingyunmen",
                    "relation": "真传弟子",
                    "valid_from_ch": 1,
                    "invalid_at_ch": 120,
                },
                {
                    "source": "char_linfan",
                    "target": "fac_qingyunmen",
                    "relation": "反目死敌",
                    "valid_from_ch": 121,
                    "invalid_at_ch": None,
                },
            ]
        }
    )
    assert len(rel_file.relations) == 2
    assert rel_file.relations[0].is_valid_at(119) is True
    assert rel_file.relations[1].is_valid_at(119) is False
    assert rel_file.relations[1].is_valid_at(121) is True


# ---------------------------------------------------------------------------
# 6. Thread & ThreadsFile 测试
# ---------------------------------------------------------------------------


def test_thread_statuses_validation_and_threads_file_uniqueness():
    for status in (
        ThreadStatus.PLANTED,
        ThreadStatus.DORMANT,
        ThreadStatus.ACTIVE,
        ThreadStatus.RESOLVED,
    ):
        t = Thread.model_validate(
            {
                "id": f"thread_{status.value}",
                "title": "伏笔测试",
                "category": "",
                "status": status.value,
                "planted_at": 10,
                "last_progressed_at": 10,
                "expected_window": [15, 25],
                "trigger_entities": ["char_linfan", "item_gujing", "char_linfan"],
                "public_clue": "表面线索",
                "truth_secret": None,
            }
        )
        assert t.status == status
        assert t.category is None
        assert t.trigger_entities == ["char_linfan", "item_gujing"]

    # last_progressed_at < planted_at 报错
    with pytest.raises(ValidationError, match="last_progressed_at"):
        Thread.model_validate(
            {
                "id": "thread_bad_progress",
                "title": "时间倒流伏笔",
                "planted_at": 20,
                "last_progressed_at": 19,
            }
        )

    # expected_window 起始大于结束报错
    with pytest.raises(ValidationError):
        Thread.model_validate(
            {
                "id": "thread_bad_window",
                "title": "窗口倒置",
                "planted_at": 1,
                "last_progressed_at": 1,
                "expected_window": [30, 20],
            }
        )

    # 主动弃坑为物理移除，不存在 dropped 状态
    with pytest.raises(ValidationError):
        Thread.model_validate(
            {
                "id": "thread_dropped",
                "title": "废弃伏笔",
                "status": "dropped",
                "planted_at": 1,
                "last_progressed_at": 2,
            }
        )

    # ThreadsFile 不允许重复 thread.id
    with pytest.raises(ValidationError, match="thread_dup"):
        ThreadsFile.model_validate(
            {
                "threads": [
                    {
                        "id": "thread_dup",
                        "title": "伏笔一",
                        "planted_at": 1,
                        "last_progressed_at": 1,
                    },
                    {
                        "id": "thread_dup",
                        "title": "伏笔二",
                        "planted_at": 2,
                        "last_progressed_at": 3,
                    },
                ]
            }
        )


# ---------------------------------------------------------------------------
# 7. ChapterFactRecord 测试
# ---------------------------------------------------------------------------


def test_chapter_fact_record_blueprint_jsonl_exact_roundtrip():
    """与 architecture-blueprint.md §3.2 第 87 章 JSONL 样例逐字兼容且无多余键。"""
    raw_line = (
        '{"ch": 87, "arc": "vol_01_arc_03", "time": "天历三年秋", '
        '"summary": "林凡在坠星谷设伏反杀赵乾，夺得古镜但身中血魂印。", '
        '"facts": ["赵乾于第87章在坠星谷死亡", "林凡获得残缺青铜古镜", '
        '"赵家老祖通过血魂印感知到凶手在南域"], '
        '"entities": ["char_linfan", "char_zhaoqian", "loc_zhuixinggu", "item_gujing"]}'
    )
    raw_dict = json.loads(raw_line)
    record = ChapterFactRecord.model_validate(raw_dict)

    assert record.ch == 87
    assert record.volume_id is None
    assert record.arc_id == "vol_01_arc_03"
    assert record.in_world_time == "天历三年秋"
    assert record.model_extra == {}

    dumped = record.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert dumped == raw_dict
    assert "volume_id" not in dumped

    # 支持通过字段名 arc_id / in_world_time 构造
    by_field_name = ChapterFactRecord.model_validate(
        {
            "ch": 87,
            "arc_id": "vol_01_arc_03",
            "in_world_time": "天历三年秋",
            "summary": raw_dict["summary"],
            "facts": raw_dict["facts"],
            "entities": [*raw_dict["entities"], "char_linfan"],
        }
    )
    assert by_field_name.arc_id == "vol_01_arc_03"
    assert by_field_name.in_world_time == "天历三年秋"
    assert by_field_name.entities == raw_dict["entities"]
    assert by_field_name.model_extra == {}


def test_chapter_fact_record_constraints_and_prefix_check():
    # 1 条与 8 条均通过
    one = ChapterFactRecord.model_validate(
        {"ch": 1, "summary": "开篇", "facts": [" 事实1 "]}
    )
    assert one.facts == ["事实1"]

    eight = ChapterFactRecord.model_validate(
        {"ch": 2, "summary": "八条上限", "facts": [f"事实{i}" for i in range(1, 9)]}
    )
    assert len(eight.facts) == 8

    # 0 条或纯空白报错
    for bad_facts in ([], ["  ", ""]):
        with pytest.raises(ValidationError):
            ChapterFactRecord.model_validate(
                {"ch": 3, "summary": "空事实", "facts": bad_facts}
            )

    # 超过 8 条（含不同字符串和重复字符串）均报错
    with pytest.raises(ValidationError):
        ChapterFactRecord.model_validate(
            {"ch": 4, "summary": "超限", "facts": [f"事实{i}" for i in range(1, 10)]}
        )
    with pytest.raises(ValidationError):
        ChapterFactRecord.model_validate(
            {"ch": 5, "summary": "重复超限", "facts": ["同一条事实"] * 9}
        )

    # 空 summary 报错
    with pytest.raises(ValidationError):
        ChapterFactRecord.model_validate(
            {"ch": 6, "summary": "   ", "facts": ["有效事实"]}
        )

    # volume_id 与 arc_id 前缀不一致（含边界 vol_01 vs vol_010_arc_01）报错
    for bad_arc in ("vol_02_arc_01", "vol_010_arc_01"):
        with pytest.raises(ValidationError, match="volume_id"):
            ChapterFactRecord.model_validate(
                {
                    "ch": 7,
                    "volume_id": "vol_01",
                    "arc_id": bad_arc,
                    "summary": "前缀冲突",
                    "facts": ["有效事实"],
                }
            )


# ---------------------------------------------------------------------------
# 8. 三题材 03_state/ Fixtures 冒烟校验 + JSON Schema 导出 + __all__ 完整性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("genre_dir", ["general", "cultivation_rpg", "mystery_intrigue"])
def test_fixtures_state_files_validate_and_roundtrip(genre_dir: str):
    book_dir = FIXTURES_BOOKS_DIR / genre_dir
    cfg = NovelConfig.load(book_dir)
    ctx = {"genre_profile": cfg.genre_profile}
    state_dir = book_dir / "03_state"

    # world_state.yaml
    world_raw = yaml.safe_load((state_dir / "world_state.yaml").read_text(encoding="utf-8"))
    world = WorldState.model_validate(world_raw)
    world_dumped = yaml.safe_load(
        yaml.safe_dump(world.model_dump(mode="json", exclude_none=True), allow_unicode=True)
    )
    assert WorldState.model_validate(world_dumped) == world

    # active/ & archive/ characters.yaml
    for sub in ("active", "archive"):
        chars_raw = yaml.safe_load(
            (state_dir / sub / "characters.yaml").read_text(encoding="utf-8")
        )
        chars = CharacterStateFile.model_validate(chars_raw, context=ctx)
        assert len(chars.root) >= 1
        chars_dumped = yaml.safe_load(
            yaml.safe_dump(chars.model_dump(mode="json"), allow_unicode=True, sort_keys=False)
        )
        assert CharacterStateFile.model_validate(chars_dumped, context=ctx) == chars

    # active/factions.yaml
    facs_raw = yaml.safe_load(
        (state_dir / "active" / "factions.yaml").read_text(encoding="utf-8")
    )
    facs = FactionStateFile.model_validate(facs_raw, context=ctx)
    assert len(facs.root) >= 1
    facs_dumped = yaml.safe_load(
        yaml.safe_dump(facs.model_dump(mode="json"), allow_unicode=True, sort_keys=False)
    )
    assert FactionStateFile.model_validate(facs_dumped, context=ctx) == facs

    # relations.yaml & archive/relations.yaml
    for rel_path in (state_dir / "relations.yaml", state_dir / "archive" / "relations.yaml"):
        rel_raw = yaml.safe_load(rel_path.read_text(encoding="utf-8"))
        rels = RelationsFile.model_validate(rel_raw)
        assert len(rels.relations) >= 1
        rels_dumped = yaml.safe_load(
            yaml.safe_dump(rels.model_dump(mode="json"), allow_unicode=True, sort_keys=False)
        )
        assert RelationsFile.model_validate(rels_dumped) == rels

    # threads.yaml & archive/threads.yaml
    for th_path in (state_dir / "threads.yaml", state_dir / "archive" / "threads.yaml"):
        th_raw = yaml.safe_load(th_path.read_text(encoding="utf-8"))
        threads = ThreadsFile.model_validate(th_raw)
        assert len(threads.threads) >= 1
        th_dumped = yaml.safe_load(
            yaml.safe_dump(threads.model_dump(mode="json"), allow_unicode=True, sort_keys=False)
        )
        assert ThreadsFile.model_validate(th_dumped) == threads

    # history/vol_01_facts.jsonl
    fact_lines = [
        line
        for line in (state_dir / "history" / "vol_01_facts.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert len(fact_lines) >= 1
    for line in fact_lines:
        raw_obj = json.loads(line)
        rec = ChapterFactRecord.model_validate(raw_obj)
        dumped_obj = rec.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert dumped_obj == raw_obj
        assert ChapterFactRecord.model_validate(dumped_obj) == rec


@pytest.mark.parametrize(
    "model_cls",
    [
        EpistemicBounds,
        CharacterState,
        CharacterStateFile,
        FactionState,
        FactionStateFile,
        WorldState,
        TemporalRelation,
        RelationsFile,
        Thread,
        ThreadsFile,
        ChapterFactRecord,
    ],
)
def test_state_models_json_schema_exportable(model_cls):
    schema = TypeAdapter(model_cls).json_schema(mode="serialization")
    assert schema["type"] == "object"


def test_models_public_all_exports():
    expected = [
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
    assert sorted(models_mod.__all__) == sorted(expected)
    for sym in expected:
        assert hasattr(models_mod, sym)
