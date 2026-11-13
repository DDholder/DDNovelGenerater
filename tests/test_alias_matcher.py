"""T04 Aho-Corasick 实体别名自动机单元测试。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from ddnovel.core import AliasMatcher, ProgressiveParser
from ddnovel.core.alias_matcher import AliasMatcher as DirectAliasMatcher
from ddnovel.core.schemas import (
    EntityStatus,
    MatchedEntity,
    SettingCategory,
    SettingDoc,
)

FIXTURES_BOOKS_DIR = Path(__file__).resolve().parent / "fixtures" / "books"


def _assert_hit_invariants(text: str, hits: list[MatchedEntity]) -> None:
    """验证扫描结果满足有序、互不重叠及原串切片对齐三大不变量。"""
    for idx, hit in enumerate(hits):
        assert isinstance(hit, MatchedEntity)
        assert text[hit.start : hit.end] == hit.alias
        assert hit.end > hit.start
        if idx > 0:
            prev = hits[idx - 1]
            assert prev.end <= hit.start
            assert (prev.start, prev.end) < (hit.start, hit.end)


# ---------------------------------------------------------------------------
# 1. 基础多模式匹配与导出契约
# ---------------------------------------------------------------------------


def test_top_level_export_and_basic_chinese_english_matching():
    assert AliasMatcher is DirectAliasMatcher

    matcher = AliasMatcher()
    assert matcher.trie == {}
    assert matcher.entity_count == 0
    assert matcher.alias_count == 0

    matcher.build(
        {
            "char_linfan": ["林凡", "林师兄", "青云弃徒"],
            "loc_zhuixinggu": ["坠星谷"],
            "item_gujing": ["残缺青铜古镜", "青铜古镜"],
            "char_he": ["he"],
            "char_she": ["she"],
            "char_his": ["his"],
            "char_hers": ["hers"],
        }
    )

    assert isinstance(matcher.trie, dict)
    assert "林" in matcher.trie
    assert matcher.entity_count == 7
    assert matcher.alias_count == 10

    zh_text = "林师兄在坠星谷祭出青铜古镜，昔日青云弃徒林凡如今重掌残缺青铜古镜。"
    zh_hits = matcher.scan_text(zh_text)
    _assert_hit_invariants(zh_text, zh_hits)
    assert [(h.entity_id, h.alias) for h in zh_hits] == [
        ("char_linfan", "林师兄"),
        ("loc_zhuixinggu", "坠星谷"),
        ("item_gujing", "青铜古镜"),
        ("char_linfan", "青云弃徒"),
        ("char_linfan", "林凡"),
        ("item_gujing", "残缺青铜古镜"),
    ]

    # MatchedEntity 为 FrozenDTO，不可篡改
    with pytest.raises(ValidationError):
        zh_hits[0].alias = "改名"  # type: ignore[misc]

    # 经典英文 "ushers" 测试集：hers(4) [2,6) 长度大于 she(3) [1,4) 与 he(2) [1,3)，最长优先命中 "hers"
    en_hits = matcher.scan_text("ushers")
    _assert_hit_invariants("ushers", en_hits)
    assert [(h.entity_id, h.alias, h.start, h.end) for h in en_hits] == [
        ("char_hers", "hers", 2, 6),
    ]


# ---------------------------------------------------------------------------
# 2. 重叠匹配最长优先 & 深层 Failure Link 传播
# ---------------------------------------------------------------------------


def test_overlap_longest_match_priority_variants():
    matcher = AliasMatcher(
        {
            "fac_qingyun_part": ["青云"],
            "fac_qingyunmen": ["青云门"],
            "fac_zhifatang": ["青云门执法堂"],
            "char_zhaoqian": ["赵乾"],
            "char_zhaoqian_blood": ["血手赵乾"],
            "fac_yaoting": ["妖庭"],
            "char_saintess": ["北境妖庭圣女"],
            "loc_donghai": ["东海"],
            "item_trident": ["海神三叉戟"],
            "char_ab": ["AB"],
            "char_bc": ["BC"],
            "char_haha": ["哈哈"],
        }
    )

    # 前缀重叠：青云 < 青云门 < 青云门执法堂
    t1 = "前方就是青云门执法堂大门。"
    h1 = matcher.scan_text(t1)
    _assert_hit_invariants(t1, h1)
    assert [(h.entity_id, h.alias) for h in h1] == [("fac_zhifatang", "青云门执法堂")]

    # 后缀重叠：血手赵乾 (4) vs 赵乾 (2)
    t2 = "远处走来血手赵乾。"
    h2 = matcher.scan_text(t2)
    _assert_hit_invariants(t2, h2)
    assert [(h.entity_id, h.alias, h.start, h.end) for h in h2] == [
        ("char_zhaoqian_blood", "血手赵乾", 4, 8),
    ]

    # 内部包含重叠：北境妖庭圣女 (6) 包含 妖庭 (2)
    t3 = "同行者乃北境妖庭圣女。"
    h3 = matcher.scan_text(t3)
    _assert_hit_invariants(t3, h3)
    assert [(h.entity_id, h.alias) for h in h3] == [("char_saintess", "北境妖庭圣女")]

    # 跨边界不等长部分重叠：东海 [2,4) vs 海神三叉戟 [3,8) -> 最长优先选 海神三叉戟
    t4 = "祭出东海神三叉戟！"
    h4 = matcher.scan_text(t4)
    _assert_hit_invariants(t4, h4)
    assert [(h.entity_id, h.alias, h.start, h.end) for h in h4] == [
        ("item_trident", "海神三叉戟", 3, 8),
    ]

    # 等长重叠左优先：AB [0,2) vs BC [1,3)
    h5 = matcher.scan_text("ABC")
    _assert_hit_invariants("ABC", h5)
    assert [(h.entity_id, h.alias, h.start, h.end) for h in h5] == [
        ("char_ab", "AB", 0, 2),
    ]

    # 周期自重叠：哈哈 in 哈哈哈 / 哈哈哈哈
    h6 = matcher.scan_text("哈哈哈")
    _assert_hit_invariants("哈哈哈", h6)
    assert [(h.start, h.end) for h in h6] == [(0, 2)]

    h7 = matcher.scan_text("哈哈哈哈")
    _assert_hit_invariants("哈哈哈哈", h7)
    assert [(h.start, h.end) for h in h7] == [(0, 2), (2, 4)]


def test_adjacent_non_overlapping_and_deep_failure_links():
    # 紧邻无重叠边界（end == next.start）应全部保留
    adj_matcher = AliasMatcher(
        {
            "fac_qingyunmen": ["青云门"],
            "fac_zhifatang": ["执法堂"],
        }
    )
    adj_text = "青云门执法堂"
    adj_hits = adj_matcher.scan_text(adj_text)
    _assert_hit_invariants(adj_text, adj_hits)
    assert [(h.entity_id, h.alias, h.start, h.end) for h in adj_hits] == [
        ("fac_qingyunmen", "青云门", 0, 3),
        ("fac_zhifatang", "执法堂", 3, 6),
    ]

    # 深层 failure link 传递：
    # "北境妖庭圣女" 的前缀 "北境妖庭圣" 的 fail 链指向 "境妖庭圣"（无自身 output），
    # 再往上或通过中间节点继承 "妖庭" 的 output。
    deep_matcher = AliasMatcher(
        {
            "char_saintess": ["北境妖庭圣女"],
            "loc_palace": ["境妖庭圣殿"],
            "fac_yaoting": ["妖庭"],
        }
    )
    deep_text = "北境妖庭圣"
    deep_hits = deep_matcher.scan_text(deep_text)
    _assert_hit_invariants(deep_text, deep_hits)
    assert [(h.entity_id, h.alias, h.start, h.end) for h in deep_hits] == [
        ("fac_yaoting", "妖庭", 2, 4),
    ]


# ---------------------------------------------------------------------------
# 3. 冷库实体自动跳过与按需唤醒
# ---------------------------------------------------------------------------


def test_archived_entities_skipped_by_default_and_wakeup():
    matcher = AliasMatcher(
        {
            "char_linfan": ["林凡", "青云门剑圣"],
            "char_zhaoqian": ["赵乾", "血手赵乾"],
            "fac_qingyun_old": (["青云门老祖"], True),
            "fac_qingyun": ["青云门"],
            "char_old_saint": (["剑圣"], True),
            "char_new_saint": (["剑圣"], False),
        },
        archived_ids=["char_zhaoqian", "char_not_in_map"],
    )

    assert matcher.is_archived("char_zhaoqian") is True
    assert matcher.is_archived("fac_qingyun_old") is True
    assert matcher.is_archived("char_linfan") is False
    assert matcher.is_archived("char_not_in_map") is False

    text = "林凡想起血手赵乾，以及青云门老祖与剑圣的旧事。"

    # 默认 include_archived=False：跳过赵乾、青云门老祖（不遮蔽活跃的“青云门”）、旧剑圣
    hot_hits = matcher.scan_text(text)
    _assert_hit_invariants(text, hot_hits)
    assert [(h.entity_id, h.alias, h.is_archived) for h in hot_hits] == [
        ("char_linfan", "林凡", False),
        ("fac_qingyun", "青云门", False),
        ("char_new_saint", "剑圣", False),
    ]

    # 位置参数 include_archived=True 与关键字参数行为一致：唤醒冷库且最长优先选“青云门老祖”
    pos_all = matcher.scan_text(text, True)
    kw_all = matcher.scan_text(text, include_archived=True)
    assert pos_all == kw_all
    _assert_hit_invariants(text, pos_all)
    assert [(h.entity_id, h.alias, h.is_archived) for h in pos_all] == [
        ("char_linfan", "林凡", False),
        ("char_zhaoqian", "血手赵乾", True),
        ("fac_qingyun_old", "青云门老祖", True),
        ("char_new_saint", "剑圣", False),
    ]


# ---------------------------------------------------------------------------
# 4. 多输入形态、辅助查询、零残留重建与异常边界
# ---------------------------------------------------------------------------


def test_multiple_input_forms_and_helper_methods():
    doc_active = SettingDoc(
        id="char_linfan",
        name="林凡",
        aliases=["林师兄"],
        category=SettingCategory.CHARACTER,
        status=EntityStatus.ACTIVE,
        file_path="01_settings/characters/char_linfan.md",
    )
    doc_dead = SettingDoc(
        id="char_zhaoqian",
        name="赵乾",
        aliases=["赵少主"],
        category=SettingCategory.CHARACTER,
        status=EntityStatus.DECEASED,
        file_path="01_settings/characters/char_zhaoqian.md",
    )

    # 形态 1：Mapping 混排（str / tuple / dict / SettingDoc）
    m1 = AliasMatcher(
        {
            "char_linfan": doc_active,
            "loc_gucheng": "北境古城",  # 单字符串不可拆成单字！
            "fac_qingyun": {"name": "青云门", "aliases": ["青云宗"], "is_archived": False},
            "char_zhaoqian": (["赵乾", "赵少主"], True),
            "item_tuple_aliases": ("青铜古镜", "古镜"),  # 二元纯字符串元组不能误判为 (names, bool)
        }
    )
    assert m1.get_aliases("loc_gucheng") == ["北境古城"]
    assert m1.get_aliases("item_tuple_aliases") == ["青铜古镜", "古镜"]
    assert m1.get_aliases("char_missing") == []

    # 校验 get_aliases 返回浅拷贝，修改外部列表不影响内部状态
    aliases_copy = m1.get_aliases("char_linfan")
    aliases_copy.append("篡改名")
    assert m1.get_aliases("char_linfan") == ["林凡", "林师兄"]

    ids_hot = m1.scan_entity_ids("林师兄在北境古城望见青云宗的青铜古镜，想到赵少主。林凡叹息。")
    assert ids_hot == ["char_linfan", "loc_gucheng", "fac_qingyun", "item_tuple_aliases"]

    ids_all = m1.scan_entity_ids(
        "林师兄与赵少主在北境古城。",
        include_archived=True,
    )
    assert ids_all == ["char_linfan", "char_zhaoqian", "loc_gucheng"]

    assert m1.resolve_alias(" 林师兄 ") == "char_linfan"
    assert m1.resolve_alias("赵少主", include_archived=False) is None
    assert m1.resolve_alias("赵少主", include_archived=True) == "char_zhaoqian"
    assert m1.resolve_alias("   ") is None
    assert m1.resolve_alias("不存在") is None

    # 形态 2：from_settings(archive_non_active=True / False)
    m_settings_auto = AliasMatcher.from_settings([doc_active, doc_dead])
    assert m_settings_auto.is_archived("char_zhaoqian") is True
    assert m_settings_auto.scan_entity_ids("林凡与赵乾") == ["char_linfan"]

    m_settings_keep = AliasMatcher.from_settings([doc_active, doc_dead], archive_non_active=False)
    assert m_settings_keep.is_archived("char_zhaoqian") is False
    assert m_settings_keep.scan_entity_ids("林凡与赵乾") == ["char_linfan", "char_zhaoqian"]

    # 形态 3：from_alias_rows（对应 ER 图 ENTITY_ALIASES 表）
    m_rows = AliasMatcher.from_alias_rows(
        [
            ("林凡", "char_linfan", 0),
            ("林师兄", "char_linfan", False),
            ("赵乾", "char_zhaoqian", 1),
        ]
    )
    assert m_rows.is_archived("char_linfan") is False
    assert m_rows.is_archived("char_zhaoqian") is True
    assert m_rows.scan_entity_ids("林师兄回忆赵乾") == ["char_linfan"]
    assert m_rows.scan_entity_ids("林师兄回忆赵乾", include_archived=True) == [
        "char_linfan",
        "char_zhaoqian",
    ]


def test_rebuild_clears_previous_state_and_edge_cases():
    matcher = AliasMatcher({"char_zhaoqian": ["血手赵乾", "赵乾"]})
    assert matcher.scan_entity_ids("血手赵乾") == ["char_zhaoqian"]

    # 再次 build 不应保留任何旧词条或旧归档集合
    matcher.build({"char_linfan": ["  林凡  ", "", "   ", "林凡"]})
    assert matcher.entity_count == 1
    assert matcher.alias_count == 1
    assert matcher.get_aliases("char_zhaoqian") == []
    assert matcher.scan_text("血手赵乾") == []

    # 首尾空白已 strip，区间精确匹配 "林凡" 长度 2
    hits = matcher.scan_text("  林凡 ")
    assert [(h.entity_id, h.alias, h.start, h.end) for h in hits] == [
        ("char_linfan", "林凡", 2, 4),
    ]

    # 单字别名在句首、Emoji 与多行文本
    matcher.build({"item_jian": ["剑"]})
    multiline = "剑\n🔥剑气纵横"
    m_hits = matcher.scan_text(multiline)
    _assert_hit_invariants(multiline, m_hits)
    assert [(h.start, h.end) for h in m_hits] == [(0, 1), (3, 4)]

    # 空文本与空自动机
    assert matcher.scan_text("") == []
    matcher.build({})
    assert matcher.trie == {}
    assert matcher.scan_text("林凡") == []


def test_validation_and_type_errors():
    with pytest.raises(ValidationError):
        AliasMatcher({"Invalid_Id": ["林凡"]})

    with pytest.raises(TypeError):
        AliasMatcher(["not_a_setting_doc"])  # type: ignore[list-item]

    with pytest.raises(TypeError):
        AliasMatcher({"char_linfan": [123]})

    matcher = AliasMatcher({"char_linfan": ["林凡"]})
    with pytest.raises(TypeError):
        matcher.scan_text(123)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        matcher.resolve_alias(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5. 三套真实题材 Fixture 书库端到端联动测试
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("genre", "archived_char_id", "archived_name", "active_expected_id"),
    [
        ("cultivation_rpg", "char_zhaoqian", "赵乾", "char_linfan"),
        ("general", "char_hancheng", "韩诚", "char_chenyu"),
        ("mystery_intrigue", "char_zhoubo", "周伯", "char_shenmo"),
    ],
)
def test_fixture_books_end_to_end(
    genre: str,
    archived_char_id: str,
    archived_name: str,
    active_expected_id: str,
):
    book_dir = FIXTURES_BOOKS_DIR / genre
    parser = ProgressiveParser(book_dir=book_dir)

    settings_dir = book_dir / "01_settings"
    settings = [
        parser.parse_setting_md(path)
        for path in sorted(settings_dir.rglob("*.md"))
    ]

    archive_chars_path = book_dir / "03_state" / "archive" / "characters.yaml"
    archive_data = yaml.safe_load(archive_chars_path.read_text(encoding="utf-8")) or {}
    archived_ids = set(archive_data.keys())
    assert archived_char_id in archived_ids

    matcher = AliasMatcher.from_settings(settings, archived_ids=archived_ids)
    assert matcher.is_archived(archived_char_id) is True
    assert matcher.is_archived(active_expected_id) is False

    # 扫描章节蓝图正文：确认活跃实体稳定检出
    bp_files = sorted((book_dir / "02_structure" / "blueprints").rglob("ch_*.md"))
    assert bp_files
    bp_text = bp_files[0].read_text(encoding="utf-8")
    bp_hits = matcher.scan_text(bp_text)
    _assert_hit_invariants(bp_text, bp_hits)
    assert active_expected_id in {h.entity_id for h in bp_hits}

    # 验证默认跳过冷库角色，唤醒后可检出冷库角色
    active_name = matcher.get_aliases(active_expected_id)[0]
    probe_text = f"{active_name}回想起旧日故人{archived_name}。"
    hot_ids = matcher.scan_entity_ids(probe_text)
    assert active_expected_id in hot_ids
    assert archived_char_id not in hot_ids

    all_hits = matcher.scan_text(probe_text, include_archived=True)
    _assert_hit_invariants(probe_text, all_hits)
    assert {h.entity_id for h in all_hits} >= {active_expected_id, archived_char_id}
    archived_hit = next(h for h in all_hits if h.entity_id == archived_char_id)
    assert archived_hit.is_archived is True


# ---------------------------------------------------------------------------
# 6. 百章规模性能基准测试 (NFR-1)
# ---------------------------------------------------------------------------


def test_hundred_chapter_scale_performance():
    entities: dict[str, list[str]] = {}
    for i in range(500):
        entities[f"char_npc_{i:03d}"] = [
            f"青云弟子甲{i:03d}",
            f"外号剑客{i:03d}",
            f"别名阿{i:03d}",
        ]

    # 构造 50,000 字模拟长文本（含高频实体提及）
    chunk = "今日青云弟子甲001与外号剑客123在广场切磋，旁观的别名阿499点头称赞。其余路人甲乙丙丁各自散去。"
    long_text = (chunk * ((50_000 // len(chunk)) + 1))[:50_000]

    t0 = time.perf_counter()
    matcher = AliasMatcher(entities)
    hits = matcher.scan_text(long_text)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert matcher.entity_count == 500
    assert matcher.alias_count == 1500
    assert len(hits) > 1000
    assert elapsed_ms < 200.0, f"500 实体建树+5万字扫描耗时 {elapsed_ms:.2f} ms 超过 200 ms 预算"
