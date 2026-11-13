"""T10 中途改设定五维防吃书影响面扫描器 (ImpactAnalyzer) 单元与集成测试。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ddnovel.core.alias_matcher import AliasMatcher
from ddnovel.core.impact import ImpactAnalyzer, scan_setting_impact
from ddnovel.core.indexer import SqliteIndexer
from ddnovel.core.schemas import (
    ImpactReport,
    ImpactScope,
    NovelConfig,
)

FIXTURES_BOOKS_DIR = Path(__file__).resolve().parent / "fixtures" / "books"


def _copy_fixture_book(genre_dir: str, tmp_path: Path) -> Path:
    dest = tmp_path / genre_dir
    shutil.copytree(FIXTURES_BOOKS_DIR / genre_dir, dest)
    return dest


def _create_synthetic_book(book_dir: Path, *, current_chapter: int = 5) -> Path:
    """创建覆盖五维作用域、重叠消歧、冷库、已完结伏笔与历史蓝图的合成书库。"""
    (book_dir / "01_settings" / "factions").mkdir(parents=True, exist_ok=True)
    (book_dir / "01_settings" / "items").mkdir(parents=True, exist_ok=True)
    (book_dir / "01_settings" / "characters").mkdir(parents=True, exist_ok=True)
    (book_dir / "02_structure" / "blueprints" / "vol_01").mkdir(parents=True, exist_ok=True)
    (book_dir / "03_state" / "active").mkdir(parents=True, exist_ok=True)
    (book_dir / "03_state" / "archive").mkdir(parents=True, exist_ok=True)
    (book_dir / "03_state" / "history").mkdir(parents=True, exist_ok=True)
    (book_dir / "04_creation" / "manuscript" / "vol_01").mkdir(parents=True, exist_ok=True)

    NovelConfig(title="防吃书测试书", current_chapter=current_chapter).dump(book_dir)

    # 设定：青云 vs 青云门（重叠消歧），外号别名，孤儿废案，冷库角色
    (book_dir / "01_settings" / "factions" / "fac_qingyun.md").write_text(
        '---\nid: fac_qingyun\nname: "青云"\naliases:\n  - "青云驿"\n'
        "category: faction\n---\n\n## [核心]\n边境小驿。\n",
        encoding="utf-8",
    )
    (book_dir / "01_settings" / "factions" / "fac_qingyunmen.md").write_text(
        '---\nid: fac_qingyunmen\nname: "青云门"\naliases:\n  - "青云宗"\n'
        "category: faction\n---\n\n## [核心]\n修真大派。\n",
        encoding="utf-8",
    )
    (book_dir / "01_settings" / "items" / "item_gujing.md").write_text(
        '---\nid: item_gujing\nname: "青铜古镜"\naliases:\n  - "古镜"\n  - "北冥残镜"\n'
        "category: item\n---\n\n## [核心]\n上古残镜。\n",
        encoding="utf-8",
    )
    (book_dir / "01_settings" / "items" / "item_orphan.md").write_text(
        '---\nid: item_orphan\nname: "无名废铁"\naliases:\n  - "孤儿铁片"\n'
        "category: item\n---\n\n## [核心]\n从未引用过的废案。\n",
        encoding="utf-8",
    )
    (book_dir / "01_settings" / "characters" / "char_linfan.md").write_text(
        '---\nid: char_linfan\nname: "林凡"\naliases:\n  - "林师兄"\n'
        "category: character\n---\n\n## [核心]\n主角。\n",
        encoding="utf-8",
    )
    (book_dir / "01_settings" / "characters" / "char_archived.md").write_text(
        '---\nid: char_archived\nname: "旧朝遗老"\naliases:\n  - "老仆"\n'
        "category: character\nstatus: deceased\n---\n\n## [核心]\n已退场角色。\n",
        encoding="utf-8",
    )

    # 正文：ch_0005 在超过 300 字后的段落出现别名与字面量 ID，并包含“青云门”干扰项
    filler = "风雪漫天吹拂着荒原上的枯木与断碑，行人低头赶路不曾停歇。" * 10
    assert len(filler) > 250
    (book_dir / "04_creation" / "manuscript" / "vol_01" / "ch_0005.md").write_text(
        f"# 第5章 风雪夜归\n\n{filler}\n"
        "林师兄在青云门山脚停步，从怀中摸出那面古镜，又低声念出 item_gujing 与北冥残镜。\n",
        encoding="utf-8",
    )
    # 无法解析章号的序章正文：chapter_num 必须为 None 而非 0
    (book_dir / "04_creation" / "manuscript" / "vol_01" / "prologue.md").write_text(
        "序幕之中，青铜古镜悄然浮现。\n",
        encoding="utf-8",
    )
    # 仅含“青云门”而不含“青云”的正文，测试最长优先消歧不误报
    (book_dir / "04_creation" / "manuscript" / "vol_01" / "ch_0004.md").write_text(
        "# 第4章 山门旧事\n\n青云门弟子齐聚大殿，共商对策。\n",
        encoding="utf-8",
    )

    # 历史事实台账
    facts_lines = [
        json.dumps(
            {
                "ch": 3,
                "summary": "林凡首次见到古镜微光。",
                "facts": ["林凡在坊市拍下青铜古镜", "青云门外门弟子围观"],
                "entities": ["char_linfan", "item_gujing"],
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "ch": 4,
                "summary": "仅结构化关联实体记录。",
                "facts": ["夜探荒山无果"],
                "entities": ["item_gujing"],
            },
            ensure_ascii=False,
        ),
    ]
    (book_dir / "03_state" / "history" / "vol_01_facts.jsonl").write_text(
        "\n".join(facts_lines) + "\n",
        encoding="utf-8",
    )

    # 活跃状态与冷库状态
    (book_dir / "03_state" / "active" / "characters.yaml").write_text(
        """char_linfan:
  updated_at_ch: 5
  location: loc_beijing
  physical_mental: "警惕，左袖藏有北冥残镜"
  current_goals:
    - "寻找古镜另一半"
  epistemic_bounds:
    knows:
      - "青铜古镜能在月夜发烫"
    unaware_of:
      - "古镜实为镇族钥器"
    misbelieves:
      - "以为北冥残镜只是普通法器"
  profile_ext:
    key_items:
      - "青铜古镜"
""",
        encoding="utf-8",
    )
    (book_dir / "03_state" / "active" / "factions.yaml").write_text(
        """fac_qingyunmen:
  updated_at_ch: 5
  status: active
  leader: char_linfan
  territory:
    - fac_qingyun
  current_goals:
    - "追查古镜去向"
  profile_ext:
    treasury:
      - "item_gujing"
""",
        encoding="utf-8",
    )
    (book_dir / "03_state" / "world_state.yaml").write_text(
        """current_chapter: 5
in_world_time: "北冥历三年冬"
macro_situation: "各方势力正暗中争夺青铜古镜与青云门传承。"
""",
        encoding="utf-8",
    )
    (book_dir / "03_state" / "relations.yaml").write_text(
        """relations:
  - source: char_linfan
    target: fac_qingyunmen
    relation: "早期同门（已失效旧关系）"
    valid_from_ch: 1
    invalid_at_ch: 4
  - source: char_linfan
    target: fac_qingyunmen
    relation: "因争夺古镜而反目的叛徒"
    valid_from_ch: 4
    invalid_at_ch: null
""",
        encoding="utf-8",
    )
    (book_dir / "03_state" / "archive" / "characters.yaml").write_text(
        """char_archived:
  updated_at_ch: 2
  physical_mental: "已故，生前曾保管青铜古镜"
  current_goals: []
""",
        encoding="utf-8",
    )
    (book_dir / "03_state" / "archive" / "factions.yaml").write_text(
        """fac_old:
  updated_at_ch: 2
  status: retired
  current_goals:
    - "夺回古镜"
""",
        encoding="utf-8",
    )
    (book_dir / "03_state" / "archive" / "relations.yaml").write_text(
        """relations:
  - source: char_archived
    target: char_linfan
    relation: "曾托付古镜的故人"
    valid_from_ch: 1
    invalid_at_ch: 3
""",
        encoding="utf-8",
    )

    # 伏笔：未完结 vs 已完结 vs archive/threads.yaml
    (book_dir / "03_state" / "threads.yaml").write_text(
        """threads:
  - id: thread_mirror_secret
    title: "残镜星纹之谜"
    status: active
    planted_at: 2
    last_progressed_at: 5
    trigger_entities:
      - item_gujing
    public_clue: "古镜背面有残缺星图。"
    truth_secret: "集齐后可开启北冥秘境。"
  - id: thread_resolved_case
    title: "已完结的古镜旧案"
    status: resolved
    planted_at: 1
    last_progressed_at: 3
    trigger_entities:
      - item_gujing
    public_clue: "旧案已结，不应出现在影响面中。"
""",
        encoding="utf-8",
    )
    (book_dir / "03_state" / "archive" / "threads.yaml").write_text(
        """threads:
  - id: thread_cold_mirror
    title: "冷库归档的古镜支线"
    status: active
    planted_at: 1
    last_progressed_at: 2
    trigger_entities:
      - item_gujing
    public_clue: "已移入 archive/threads.yaml，不应误报。"
""",
        encoding="utf-8",
    )

    # 蓝图：已定稿历史蓝图 (<=5) 与后续蓝图 (>5)
    (book_dir / "02_structure" / "blueprints" / "vol_01" / "ch_0005.md").write_text(
        """---
chapter: 5
title: "已定稿历史蓝图"
pov: char_linfan
cast:
  items:
    - item_gujing
---
历史章节中的古镜描写，已定稿不应再误报。
""",
        encoding="utf-8",
    )
    (book_dir / "02_structure" / "blueprints" / "vol_01" / "ch_0006.md").write_text(
        """---
chapter: 6
title: "后续蓝图：古镜觉醒"
pov: char_linfan
cast:
  items:
    - item_gujing
threads:
  - thread_id: thread_mirror_secret
    action: progress
unlock_secrets:
  - char_linfan
topics:
  - entity_id: item_gujing
    topic: "星图共鸣"
forbidden:
  - "严禁提前毁坏北冥残镜"
beats:
  - index: 1
    title: "鬼市试镜"
    content: "林师兄在暗室催动青铜古镜。"
---
后续蓝图正文提及血魂印与惊雷剑诀。
""",
        encoding="utf-8",
    )

    return book_dir


# ---------------------------------------------------------------------------
# 1. 查询方式与五维作用域全覆盖测试
# ---------------------------------------------------------------------------


def test_five_scopes_full_coverage_by_id_name_and_alias(tmp_path: Path) -> None:
    book_dir = _create_synthetic_book(tmp_path / "book_full", current_chapter=5)

    # 通过实体 ID / 主名称 / 外号简称查询，展开词表与命中完全一致
    report_by_id = scan_setting_impact(book_dir, "item_gujing")
    report_by_name = scan_setting_impact(book_dir, "青铜古镜")
    report_by_alias = scan_setting_impact(book_dir, "北冥残镜")

    for report in (report_by_id, report_by_name, report_by_alias):
        assert isinstance(report, ImpactReport)
        assert report.entity_id == "item_gujing"
        assert report.expanded_terms == ["青铜古镜", "古镜", "北冥残镜"]
        assert report.is_safe_to_change is False
        assert {h.scope for h in report.hits} == {
            ImpactScope.MANUSCRIPT,
            ImpactScope.FACT,
            ImpactScope.STATE,
            ImpactScope.THREAD,
            ImpactScope.BLUEPRINT,
        }

    assert report_by_id.hits == report_by_name.hits == report_by_alias.hits

    # 验证 MANUSCRIPT 全文扫描（覆盖 > 200 字后段、多处命中汇总、无章号 None）
    ms_hits = [h for h in report_by_id.hits if h.scope == ImpactScope.MANUSCRIPT]
    assert len(ms_hits) == 2
    ch5_hit = next(h for h in ms_hits if h.chapter_num == 5)
    assert ch5_hit.location == "04_creation/manuscript/vol_01/ch_0005.md"
    assert "共 3 处命中" in ch5_hit.excerpt
    assert "古镜" in ch5_hit.excerpt

    prologue_hit = next(h for h in ms_hits if h.location.endswith("prologue.md"))
    assert prologue_hit.chapter_num is None

    # 验证 FACT 事实台账（文本命中 + 仅 entities 命中）
    fact_hits = [h for h in report_by_id.hits if h.scope == ImpactScope.FACT]
    assert [h.chapter_num for h in fact_hits] == [3, 4]
    assert "林凡在坊市拍下青铜古镜" in fact_hits[0].excerpt
    assert fact_hits[1].matched_term == "item_gujing"
    assert "仅结构化关联实体记录" in fact_hits[1].excerpt

    # 验证 STATE 活跃热池（角色 + 势力 + 世界状态 + 有效关系；排除 archive/ 与已失效关系）
    state_hits = [h for h in report_by_id.hits if h.scope == ImpactScope.STATE]
    state_locs = {h.location for h in state_hits}
    assert state_locs == {
        "03_state/active/characters.yaml",
        "03_state/active/factions.yaml",
        "03_state/world_state.yaml",
        "03_state/relations.yaml",
    }
    rel_hit = next(h for h in state_hits if h.location == "03_state/relations.yaml")
    assert rel_hit.chapter_num == 4
    assert "反目的叛徒" in rel_hit.excerpt

    # 验证 THREAD（仅未完结且非 archive）
    thread_hits = [h for h in report_by_id.hits if h.scope == ImpactScope.THREAD]
    assert len(thread_hits) == 1
    assert thread_hits[0].chapter_num == 5
    assert "thread_mirror_secret" in thread_hits[0].excerpt

    # 验证 BLUEPRINT（仅 chapter > current_chapter=5）
    bp_hits = [h for h in report_by_id.hits if h.scope == ImpactScope.BLUEPRINT]
    assert len(bp_hits) == 1
    assert bp_hits[0].chapter_num == 6
    assert bp_hits[0].location == "02_structure/blueprints/vol_01/ch_0006.md"


def test_keyword_and_thread_id_queries(tmp_path: Path) -> None:
    book_dir = _create_synthetic_book(tmp_path / "book_kw", current_chapter=5)

    kw_report = scan_setting_impact(book_dir, "惊雷剑诀")
    assert kw_report.entity_id is None
    assert kw_report.expanded_terms == ["惊雷剑诀"]
    assert kw_report.is_safe_to_change is False
    assert len(kw_report.hits) == 1
    assert kw_report.hits[0].scope == ImpactScope.BLUEPRINT
    assert kw_report.hits[0].chapter_num == 6

    thread_report = scan_setting_impact(book_dir, "thread_mirror_secret")
    assert thread_report.entity_id is None
    assert thread_report.expanded_terms == ["thread_mirror_secret", "残镜星纹之谜"]
    assert thread_report.is_safe_to_change is False
    assert {h.scope for h in thread_report.hits} == {
        ImpactScope.THREAD,
        ImpactScope.BLUEPRINT,
    }


def test_isolation_boundaries_archive_resolved_cutoff_and_orphan(tmp_path: Path) -> None:
    book_dir = _create_synthetic_book(tmp_path / "book_iso", current_chapter=5)

    # 1. 默认不包含 archive/*，开启 include_archived_state=True 后应检出冷库角色/势力/关系
    default_report = scan_setting_impact(book_dir, "item_gujing")
    assert not any("03_state/archive/" in h.location for h in default_report.hits)

    with_archive = scan_setting_impact(book_dir, "item_gujing", include_archived_state=True)
    archive_locs = {h.location for h in with_archive.hits if "03_state/archive/" in h.location}
    assert archive_locs == {
        "03_state/archive/characters.yaml",
        "03_state/archive/factions.yaml",
        "03_state/archive/relations.yaml",
    }

    # 2. 冷库角色自身按 ID 查询时，默认不扫冷库状态，开启后才命中
    arch_char_default = scan_setting_impact(book_dir, "char_archived")
    assert not any(h.scope == ImpactScope.STATE for h in arch_char_default.hits)

    arch_char_inc = scan_setting_impact(book_dir, "char_archived", include_archived_state=True)
    assert {
        h.location for h in arch_char_inc.hits if h.scope == ImpactScope.STATE
    } == {
        "03_state/archive/characters.yaml",
        "03_state/archive/relations.yaml",
    }

    # 3. after_chapter 覆盖蓝图 cutoff 与关系有效时间窗
    after_ch6 = scan_setting_impact(book_dir, "item_gujing", after_chapter=6)
    assert not any(h.scope == ImpactScope.BLUEPRINT for h in after_ch6.hits)

    after_ch2 = scan_setting_impact(book_dir, "char_linfan", after_chapter=2)
    rel_hits_ch2 = [h for h in after_ch2.hits if h.location == "03_state/relations.yaml"]
    assert len(rel_hits_ch2) == 1
    assert "早期同门" in rel_hits_ch2[0].excerpt

    # 4. 零引用孤儿设定 -> is_safe_to_change=True
    orphan_report = scan_setting_impact(book_dir, "item_orphan")
    assert orphan_report.entity_id == "item_orphan"
    assert orphan_report.expanded_terms == ["无名废铁", "孤儿铁片"]
    assert orphan_report.hits == []
    assert orphan_report.is_safe_to_change is True


def test_aho_corasick_overlap_disambiguation_and_state_only_entity(tmp_path: Path) -> None:
    book_dir = _create_synthetic_book(tmp_path / "book_ac", current_chapter=5)

    # ch_0004.md 与 ch_0005.md 都出现“青云门”，但均未出现独立“青云”或“青云驿”
    # 依赖全书 Aho-Corasick 最长优先消歧，“青云”绝不应误命中“青云门”
    short_report = scan_setting_impact(book_dir, "fac_qingyun")
    assert not any(h.scope == ImpactScope.MANUSCRIPT for h in short_report.hits)
    assert not any(h.location == "03_state/world_state.yaml" for h in short_report.hits)
    # 但在 factions.yaml 中 territory 结构化引用了 fac_qingyun，仍可精确检出
    assert any(
        h.scope == ImpactScope.STATE and h.location == "03_state/active/factions.yaml"
        for h in short_report.hits
    )

    # state-only 实体（无 01_settings/*.md 文件，仅存在于 characters.yaml）
    (book_dir / "03_state" / "active" / "characters.yaml").write_text(
        """char_state_only:
  updated_at_ch: 5
  physical_mental: "独行旅人"
""",
        encoding="utf-8",
    )
    state_only_report = scan_setting_impact(book_dir, "char_state_only")
    assert state_only_report.entity_id == "char_state_only"
    assert state_only_report.expanded_terms == ["char_state_only"]
    assert len(state_only_report.hits) == 1
    assert state_only_report.hits[0].scope == ImpactScope.STATE


# ---------------------------------------------------------------------------
# 2. 边界校验、生命周期、外部依赖注入与 Windows 句柄无泄漏测试
# ---------------------------------------------------------------------------


def test_validation_no_novel_yaml_and_handle_cleanup(tmp_path: Path) -> None:
    empty_book = tmp_path / "no_yaml_book"
    empty_book.mkdir()

    # 无 novel.yaml 时使用 NovelConfig(title=...) 兜底，绝不报错
    with ImpactAnalyzer(empty_book) as analyzer:
        assert analyzer.book_dir == empty_book.resolve()
        assert analyzer.config.title == "no_yaml_book"
        assert analyzer.config.current_chapter == 0

        report = analyzer.scan_impact("任意关键词")
        assert report.is_safe_to_change is True
        assert report.hits == []

        for bad_target in ("", "   ", True, False, 123, None):
            with pytest.raises(ValueError):
                analyzer.scan_impact(bad_target)  # type: ignore[arg-type]

        for bad_ch in (-1, True, False, "5"):
            with pytest.raises(ValueError):
                analyzer.scan_impact("关键词", after_chapter=bad_ch)  # type: ignore[arg-type]

        # after_chapter=0 合法
        assert analyzer.scan_impact("关键词", after_chapter=0).hits == []

    # Windows SQLite 文件锁已彻底释放，整个目录可直接删除
    shutil.rmtree(empty_book)
    assert not empty_book.exists()


def test_world_state_ch0_and_external_indexer_not_closed(tmp_path: Path) -> None:
    book_dir = _create_synthetic_book(tmp_path / "book_ext", current_chapter=0)
    (book_dir / "03_state" / "world_state.yaml").write_text(
        """current_chapter: 0
in_world_time: "太初元年"
macro_situation: "青铜古镜尚未现世。"
""",
        encoding="utf-8",
    )

    ext_indexer = SqliteIndexer(book_dir, db_path=":memory:")
    ext_indexer.sync()
    ext_matcher = AliasMatcher(
        {"item_gujing": ["青铜古镜", "古镜", "北冥残镜"]},
        archive_non_active=False,
    )

    try:
        with ImpactAnalyzer(
            book_dir,
            indexer=ext_indexer,
            matcher=ext_matcher,
            auto_sync=False,
        ) as analyzer:
            report = analyzer.scan_impact("item_gujing")
            ws_hit = next(
                h for h in report.hits if h.location == "03_state/world_state.yaml"
            )
            # current_chapter=0 时 chapter_num 必须为 None，绝不能传 0
            assert ws_hit.chapter_num is None

        # 外部注入的 indexer 在 ImpactAnalyzer 退出后绝不可被关闭
        assert ext_indexer.conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        ext_indexer.close()


# ---------------------------------------------------------------------------
# 3. 三套真实样书 Fixtures 端到端测试
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("genre_dir", "target", "expected_eid", "expected_scopes"),
    [
        (
            "cultivation_rpg",
            "苏姑娘",
            "char_suyao",
            {
                ImpactScope.MANUSCRIPT,
                ImpactScope.FACT,
                ImpactScope.STATE,
                ImpactScope.THREAD,
                ImpactScope.BLUEPRINT,
            },
        ),
        (
            "cultivation_rpg",
            "血魂印",
            None,
            {
                ImpactScope.MANUSCRIPT,
                ImpactScope.FACT,
                ImpactScope.STATE,
                ImpactScope.THREAD,
                ImpactScope.BLUEPRINT,
            },
        ),
        (
            "general",
            "char_chenyu",
            "char_chenyu",
            {
                ImpactScope.MANUSCRIPT,
                ImpactScope.FACT,
                ImpactScope.STATE,
                ImpactScope.THREAD,
                ImpactScope.BLUEPRINT,
            },
        ),
        (
            "mystery_intrigue",
            "沈默",
            "char_shenmo",
            {
                ImpactScope.MANUSCRIPT,
                ImpactScope.FACT,
                ImpactScope.STATE,
                ImpactScope.THREAD,
                ImpactScope.BLUEPRINT,
            },
        ),
        (
            "mystery_intrigue",
            "怀表密本",
            "item_yinbiao_miben",
            {
                ImpactScope.STATE,
                ImpactScope.THREAD,
                ImpactScope.BLUEPRINT,
            },
        ),
    ],
)
def test_three_fixture_books_e2e(
    genre_dir: str,
    target: str,
    expected_eid: str | None,
    expected_scopes: set[ImpactScope],
    tmp_path: Path,
) -> None:
    book_dir = _copy_fixture_book(genre_dir, tmp_path)

    with ImpactAnalyzer(book_dir) as analyzer:
        report = analyzer.scan_impact(target)

    assert report.entity_id == expected_eid
    assert report.is_safe_to_change is False
    assert {h.scope for h in report.hits} == expected_scopes

    # 确认释放句柄后可删除拷贝的临时书库（包含 .ddnovel/index.sqlite）
    shutil.rmtree(book_dir)
    assert not book_dir.exists()


def test_cultivation_fixture_archived_and_resolved_isolation(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("cultivation_rpg", tmp_path)

    # 赵乾已退场归档，默认不包含 archive/characters.yaml 与 archive/relations.yaml
    report_default = scan_setting_impact(book_dir, "char_zhaoqian")
    assert report_default.entity_id == "char_zhaoqian"
    assert not any("03_state/archive/" in h.location for h in report_default.hits)
    # 已完结归档的 thread_zhuixinggu_ambush 不应出现在 THREAD 命中中
    assert not any(
        h.scope == ImpactScope.THREAD and "thread_zhuixinggu_ambush" in h.excerpt
        for h in report_default.hits
    )

    report_with_archive = scan_setting_impact(
        book_dir, "char_zhaoqian", include_archived_state=True
    )
    assert any(
        h.location == "03_state/archive/characters.yaml" for h in report_with_archive.hits
    )
    assert any(
        h.location == "03_state/archive/relations.yaml" for h in report_with_archive.hits
    )
