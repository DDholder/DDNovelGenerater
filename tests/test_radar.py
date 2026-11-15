"""T09 规划期悬线伏笔三类雷达 (`ForeshadowRadar`) 全量单元与集成测试。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from ddnovel.core.indexer import SqliteIndexer
from ddnovel.core.radar import ForeshadowRadar, scan_foreshadow_radar
from ddnovel.core.schemas import (
    NovelConfig,
    RadarReason,
    RadarReport,
)
from ddnovel.templates import ProjectInitializer

FIXTURES_BOOKS_DIR = Path(__file__).resolve().parent / "fixtures" / "books"


def _copy_fixture_book(genre_dir: str, tmp_path: Path) -> Path:
    dest = tmp_path / genre_dir
    shutil.copytree(FIXTURES_BOOKS_DIR / genre_dir, dest)
    return dest


def _write_threads_yaml(path: Path, threads: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"threads": threads}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1. 三题材真实 fixtures 巡检与默认参数/蓝图自动提取
# ---------------------------------------------------------------------------


def test_cultivation_rpg_fixture_default_scan_and_dormant_guard(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("cultivation_rpg", tmp_path)

    report = scan_foreshadow_radar(book_dir)
    assert isinstance(report, RadarReport)
    # current_chapter=141 -> 默认扫描 142 章，自动提取 ch_0142.md 实体
    assert report.target_chapter == 142
    assert report.planned_entities == [
        "char_linfan",
        "char_suyao",
        "loc_beijing_gucheng",
        "fac_qingyunmen",
        "fac_beijing_yaoting",
        "item_gujing",
    ]
    # archive/threads.yaml 中的 resolved 伏笔被物理排除，仅剩 3 条有效候选
    assert report.total_active_threads == 3

    hit_map = {h.thread.id: h for h in report.hits}
    assert "thread_zhuixinggu_ambush" not in hit_map
    assert set(hit_map.keys()) == {
        "thread_xuehunyin_curse",
        "thread_gujing_origin",
        "thread_suyao_identity",
    }

    # 1) 确定性排序：3 原因 > 2 原因 > 1 原因
    assert [h.thread.id for h in report.hits] == [
        "thread_xuehunyin_curse",
        "thread_gujing_origin",
        "thread_suyao_identity",
    ]

    # 2) thread_xuehunyin_curse: 三类全命中，且原因顺序固定 [WINDOW, STALE, ENTITY]
    curse_hit = hit_map["thread_xuehunyin_curse"]
    assert curse_hit.reasons == [RadarReason.WINDOW, RadarReason.STALE, RadarReason.ENTITY]
    assert "[142, 155]" in curse_hit.detail
    assert "27 章" in curse_hit.detail
    assert "char_linfan" in curse_hit.detail
    assert "；" in curse_hit.detail

    # 3) thread_gujing_origin: WINDOW + ENTITY (gap=1 <= 20)
    gujing_hit = hit_map["thread_gujing_origin"]
    assert gujing_hit.reasons == [RadarReason.WINDOW, RadarReason.ENTITY]
    assert "[140, 150]" in gujing_hit.detail
    assert "char_linfan" in gujing_hit.detail
    assert "item_gujing" in gujing_hit.detail
    assert "loc_beijing_gucheng" in gujing_hit.detail

    # 4) thread_suyao_identity: dormant (gap=24 > 20, window=[148,165])，绝不误报 STALE，仅命中 ENTITY
    suyao_hit = hit_map["thread_suyao_identity"]
    assert suyao_hit.reasons == [RadarReason.ENTITY]
    assert "char_suyao" in suyao_hit.detail
    assert "fac_beijing_yaoting" in suyao_hit.detail


def test_cultivation_rpg_dormant_not_stale_without_planned_entities(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("cultivation_rpg", tmp_path)

    # 显式传空 planned_entities=[]：不混入蓝图实体，且 dormant 的 thread_suyao_identity 不应因 gap=24 告警
    report = scan_foreshadow_radar(book_dir, target_chapter=142, planned_entities=[])
    assert report.planned_entities == []
    assert report.total_active_threads == 3

    hit_map = {h.thread.id: h for h in report.hits}
    assert "thread_suyao_identity" not in hit_map
    assert hit_map["thread_xuehunyin_curse"].reasons == [RadarReason.WINDOW, RadarReason.STALE]
    assert hit_map["thread_gujing_origin"].reasons == [RadarReason.WINDOW]


@pytest.mark.parametrize(
    ("genre_dir", "expected_chapter", "expected_hits"),
    [
        (
            "general",
            13,
            {
                "thread_audit_clause": [RadarReason.WINDOW, RadarReason.ENTITY],
                "thread_solver_benchmark": [RadarReason.ENTITY],
                "thread_leaked_benchmark": [RadarReason.ENTITY],
            },
        ),
        (
            "mystery_intrigue",
            25,
            {
                "thread_watchmaker_will": [RadarReason.WINDOW, RadarReason.ENTITY],
                "thread_mole_identity": [RadarReason.ENTITY],
                "thread_cargo_manifest": [RadarReason.ENTITY],
            },
        ),
    ],
)
def test_general_and_mystery_fixtures_scan(
    genre_dir: str,
    expected_chapter: int,
    expected_hits: dict[str, list[RadarReason]],
    tmp_path: Path,
) -> None:
    book_dir = _copy_fixture_book(genre_dir, tmp_path)

    with ForeshadowRadar(book_dir) as radar:
        report = radar.scan()

    assert report.target_chapter == expected_chapter
    assert report.total_active_threads == 3
    hit_map = {h.thread.id: h.reasons for h in report.hits}
    assert hit_map == expected_hits


# ---------------------------------------------------------------------------
# 2. 三类原因单独与组合命中、阈值边界及 dormant 窗口过期告警
# ---------------------------------------------------------------------------


def test_three_reasons_combinations_boundaries_and_overdue_window(tmp_path: Path) -> None:
    book_dir = tmp_path / "radar_matrix"
    ProjectInitializer().init_book(book_dir, title="矩阵测试", genre_profile="general")

    threads = [
        # 1) 仅 WINDOW（边界 start == 30，gap == 20 等于阈值不触发 STALE）
        {
            "id": "thread_win_start",
            "title": "窗口起点边界",
            "status": "planted",
            "planted_at": 5,
            "last_progressed_at": 10,
            "expected_window": [30, 35],
            "trigger_entities": ["char_other"],
        },
        # 2) 仅 WINDOW（边界 end == 30）
        {
            "id": "thread_win_end",
            "title": "窗口终点边界",
            "status": "active",
            "planted_at": 6,
            "last_progressed_at": 25,
            "expected_window": [20, 30],
            "trigger_entities": [],
        },
        # 3) 仅 STALE（情况 A：active 且 gap = 21 > 20，无窗口）
        {
            "id": "thread_stale_gap_only",
            "title": "活跃悬线断线",
            "status": "active",
            "planted_at": 4,
            "last_progressed_at": 9,
            "trigger_entities": ["char_other"],
        },
        # 4) 仅 STALE（情况 B：dormant 且 gap = 2 <= 20，但超出预计窗口 [20, 29]）
        {
            "id": "thread_dormant_overdue",
            "title": "休眠伏笔窗口过期",
            "status": "dormant",
            "planted_at": 3,
            "last_progressed_at": 28,
            "expected_window": [20, 29],
            "trigger_entities": [],
        },
        # 5) 仅 ENTITY（dormant 且 gap = 28 > 20，未设窗口 -> 不触发 STALE）
        {
            "id": "thread_dormant_entity_only",
            "title": "长线休眠无窗口防误报",
            "status": "dormant",
            "planted_at": 2,
            "last_progressed_at": 2,
            "trigger_entities": ["char_hero"],
        },
        # 6) WINDOW + STALE（planted 且 gap = 21 > 20，无实体交集）
        {
            "id": "thread_win_and_stale",
            "title": "窗口与断线双命中",
            "status": "planted",
            "planted_at": 8,
            "last_progressed_at": 9,
            "expected_window": [28, 32],
            "trigger_entities": [],
        },
        # 7) STALE + ENTITY（情况 A + 情况 B 同时成立，且命中实体）
        {
            "id": "thread_stale_both_and_entity",
            "title": "断线超窗且命中实体",
            "status": "active",
            "planted_at": 1,
            "last_progressed_at": 5,
            "expected_window": [15, 25],
            "trigger_entities": ["char_hero", "item_key"],
        },
        # 8) 未命中任何条件（dormant + gap=25 + 未来窗口 [40, 50]）
        {
            "id": "thread_silent_dormant",
            "title": "安静潜伏",
            "status": "dormant",
            "planted_at": 5,
            "last_progressed_at": 5,
            "expected_window": [40, 50],
            "trigger_entities": ["char_other"],
        },
    ]
    _write_threads_yaml(book_dir / "03_状态" / "伏笔台账.yaml", threads)

    report = scan_foreshadow_radar(
        book_dir,
        target_chapter=30,
        planned_entities=["char_hero"],
        stale_threshold=20,
    )

    assert report.total_active_threads == 8
    hit_map = {h.thread.id: h for h in report.hits}
    assert "thread_silent_dormant" not in hit_map

    assert hit_map["thread_win_start"].reasons == [RadarReason.WINDOW]
    assert hit_map["thread_win_end"].reasons == [RadarReason.WINDOW]
    assert hit_map["thread_stale_gap_only"].reasons == [RadarReason.STALE]
    assert "21 章" in hit_map["thread_stale_gap_only"].detail

    assert hit_map["thread_dormant_overdue"].reasons == [RadarReason.STALE]
    assert "[20, 29]" in hit_map["thread_dormant_overdue"].detail

    assert hit_map["thread_dormant_entity_only"].reasons == [RadarReason.ENTITY]
    assert hit_map["thread_win_and_stale"].reasons == [RadarReason.WINDOW, RadarReason.STALE]

    both_hit = hit_map["thread_stale_both_and_entity"]
    assert both_hit.reasons == [RadarReason.STALE, RadarReason.ENTITY]
    assert "25 章" in both_hit.detail
    assert "[15, 25]" in both_hit.detail
    assert "char_hero" in both_hit.detail


# ---------------------------------------------------------------------------
# 3. 过滤规则（archive / resolved / future planted_at）与热同步更新
# ---------------------------------------------------------------------------


def test_exclusions_and_hot_sync_after_thread_delete_or_resolve(tmp_path: Path) -> None:
    book_dir = tmp_path / "filter_sync_book"
    ProjectInitializer().init_book(book_dir, title="过滤热同步测试", genre_profile="general")

    active_file = book_dir / "03_状态" / "伏笔台账.yaml"
    archive_file = book_dir / "03_状态" / "归档" / "伏笔台账.yaml"

    _write_threads_yaml(
        active_file,
        [
            {
                "id": "thread_active_one",
                "title": "活跃候选",
                "status": "active",
                "planted_at": 1,
                "last_progressed_at": 1,
                "expected_window": [10, 15],
                "trigger_entities": ["char_a"],
            },
            {
                "id": "thread_resolved_in_active",
                "title": "活跃表中已完结",
                "status": "resolved",
                "planted_at": 1,
                "last_progressed_at": 5,
                "expected_window": [10, 15],
                "trigger_entities": ["char_a"],
            },
            {
                "id": "thread_future",
                "title": "未来章节才埋设",
                "status": "planted",
                "planted_at": 15,
                "last_progressed_at": 15,
                "expected_window": [15, 20],
                "trigger_entities": ["char_a"],
            },
        ],
    )
    # 即使冷库里的状态未标 resolved，也必须因路径含 archive 被 100% 物理忽略
    _write_threads_yaml(
        archive_file,
        [
            {
                "id": "thread_archived_but_active",
                "title": "冷库冻结伏笔",
                "status": "active",
                "planted_at": 1,
                "last_progressed_at": 1,
                "expected_window": [10, 15],
                "trigger_entities": ["char_a"],
            },
        ],
    )

    with ForeshadowRadar(book_dir) as radar:
        r1 = radar.scan(target_chapter=12, planned_entities=["char_a"])
        assert r1.total_active_threads == 1
        assert [h.thread.id for h in r1.hits] == ["thread_active_one"]

        # 模拟修改：将 thread_active_one 改为 resolved 并新增一条 active 伏笔，删除旧一条
        _write_threads_yaml(
            active_file,
            [
                {
                    "id": "thread_active_two",
                    "title": "新埋设伏笔",
                    "status": "planted",
                    "planted_at": 10,
                    "last_progressed_at": 10,
                    "expected_window": [12, 18],
                    "trigger_entities": ["char_a"],
                },
            ],
        )

        # 热同步后旧伏笔不再告警，新伏笔立即生效
        r2 = radar.scan(target_chapter=12, planned_entities=["char_a"])
        assert r2.total_active_threads == 1
        assert [h.thread.id for h in r2.hits] == ["thread_active_two"]

        # 若物理清空 threads.yaml，热同步后全部归零
        _write_threads_yaml(active_file, [])
        r3 = radar.scan(target_chapter=12, planned_entities=["char_a"])
        assert r3.total_active_threads == 0
        assert r3.hits == []


# ---------------------------------------------------------------------------
# 4. planned_entities 名称/别名解析、蓝图控制与参数校验
# ---------------------------------------------------------------------------


def test_planned_entities_resolution_and_blueprint_toggle(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("cultivation_rpg", tmp_path)

    with ForeshadowRadar(book_dir) as radar:
        # 1) include_blueprint_entities=False 时不读取蓝图
        no_bp = radar.scan(target_chapter=142, include_blueprint_entities=False)
        assert no_bp.planned_entities == []

        # 2) planned_entities=None 且不存在对应章节蓝图时退化为 []
        missing_bp = radar.scan(target_chapter=200)
        assert missing_bp.planned_entities == []

        # 3) 显式传名称、别名、带空白项与重复 ID，以及归档实体的别名精确映射
        resolved_report = radar.scan(
            target_chapter=142,
            planned_entities=[
                "  林师兄 ",
                "",
                "   ",
                "char_linfan",
                "北冥古镜",
                "血手赵乾",
                "char_new_custom",
            ],
        )
        assert resolved_report.planned_entities == [
            "char_linfan",
            "item_gujing",
            "char_zhaoqian",
            "char_new_custom",
        ]
        # 显式传入时绝不混入蓝图实体 char_suyao
        assert "char_suyao" not in resolved_report.planned_entities

        # 4) 仅空白项列表应合法解析为空
        blank_only = radar.scan(target_chapter=142, planned_entities=["  ", "\t"])
        assert blank_only.planned_entities == []

        # 5) 无效非空字符串抛出 ValueError
        with pytest.raises(ValueError, match="无法将 planned_entities"):
            radar.scan(target_chapter=142, planned_entities=["不存在的角色名"])

        with pytest.raises(ValueError, match="无法将 planned_entities"):
            radar.scan(target_chapter=142, planned_entities=["invalid-id!"])

        # 6) 直接传入字符串或非字符串元素抛出 TypeError
        with pytest.raises(TypeError):
            radar.scan(target_chapter=142, planned_entities="char_linfan")
        with pytest.raises(TypeError):
            radar.scan(target_chapter=142, planned_entities=[123])  # type: ignore[list-item]


def test_invalid_parameter_validation(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("general", tmp_path)

    with ForeshadowRadar(book_dir) as radar:
        for bad_ch in (0, -3, True, False, "13", 1.5):
            with pytest.raises(ValueError):
                radar.scan(target_chapter=bad_ch)  # type: ignore[arg-type]

        for bad_thresh in (0, -1, True, False, "20", 2.5):
            with pytest.raises(ValueError):
                radar.scan(target_chapter=13, stale_threshold=bad_thresh)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5. 无 novel.yaml 兜底、外部 Indexer 生命周期与 Windows SQLite 锁释放
# ---------------------------------------------------------------------------


def test_missing_novel_yaml_fallback_and_windows_sqlite_cleanup(tmp_path: Path) -> None:
    bare_dir = tmp_path / "bare_novel_dir"
    bare_dir.mkdir(parents=True)

    # 无 novel.yaml 时必须使用 NovelConfig(title=...) 兜底，且单次 scan 后无连接句柄残留
    radar = ForeshadowRadar(bare_dir)
    assert radar.book_dir == bare_dir.resolve()
    assert isinstance(radar.config, NovelConfig)
    assert radar.config.title == "bare_novel_dir"

    report = radar.scan()
    assert report.target_chapter == 1
    assert report.planned_entities == []
    assert report.hits == []
    assert report.total_active_threads == 0

    # 即使未显式使用 with / close()，scan() 结束后也应已关闭临时连接，Windows 下可直接 rmtree
    shutil.rmtree(bare_dir)
    assert not bare_dir.exists()


def test_external_indexer_is_not_closed_by_radar(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("mystery_intrigue", tmp_path)

    with SqliteIndexer(book_dir) as ext_idx:
        ext_idx.sync()
        with ForeshadowRadar(book_dir, indexer=ext_idx, auto_sync=False) as radar:
            report = radar.scan()
            assert report.target_chapter == 25
            assert report.total_active_threads == 3

        # 外部注入的 indexer 必须保持可用，不被 ForeshadowRadar.close() 关闭
        assert ext_idx.get_entity("char_shenmo") is not None

    shutil.rmtree(book_dir)
    assert not book_dir.exists()
