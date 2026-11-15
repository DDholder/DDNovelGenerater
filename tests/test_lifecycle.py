"""T11 四级可控遗忘与瘦身生命周期管理器及一致性体检器 (LifecycleManager) 全量测试。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from ddnovel.core.indexer import SqliteIndexer
from ddnovel.core.lifecycle import LifecycleManager
from ddnovel.core.schemas import (
    AuditReport,
    ChapterFactRecord,
    CharacterStateFile,
    CompactResult,
    DropResult,
    GCReport,
    ImpactHit,
    ImpactReport,
    ImpactScope,
    IssueLevel,
    NovelConfig,
    PromoteResult,
    PruneReport,
    RelationsFile,
    ThreadsFile,
    WorldState,
    format_chapter,
)

FIXTURES_BOOKS_DIR = Path(__file__).resolve().parent / "fixtures" / "books"


def _copy_fixture_book(genre_dir: str, tmp_path: Path) -> Path:
    dest = tmp_path / genre_dir
    shutil.copytree(FIXTURES_BOOKS_DIR / genre_dir, dest)
    return dest


def _make_minimal_book(tmp_path: Path, *, current_chapter: int = 10, genre: str = "general") -> Path:
    book_dir = tmp_path / "mini_book"
    for rel in (
        "01_settings/characters",
        "01_settings/locations",
        "02_structure/blueprints/vol_01",
        "02_structure/volumes",
        "03_state/active",
        "03_state/archive",
        "03_state/history",
        "04_creation/packets",
        "04_creation/drafts",
        "04_creation/manuscript/vol_01",
    ):
        (book_dir / rel).mkdir(parents=True, exist_ok=True)
    cfg = NovelConfig(title="测试书库", genre_profile=genre, current_chapter=current_chapter)
    cfg.dump(book_dir)
    zh_cfg = book_dir / "小说配置.yaml"
    if zh_cfg.exists():
        zh_cfg.replace(book_dir / "novel.yaml")
    return book_dir


# ---------------------------------------------------------------------------
# 1. 构造、无 novel.yaml 回退与资源释放
# ---------------------------------------------------------------------------


def test_init_without_novel_yaml_and_windows_sqlite_cleanup(tmp_path: Path) -> None:
    empty_dir = tmp_path / "no_config_book"
    empty_dir.mkdir(parents=True)

    with LifecycleManager(empty_dir) as lm:
        assert lm.book_dir == empty_dir.resolve()
        assert lm.config.title == "no_config_book"
        assert lm.config.current_chapter == 0
        report = lm.audit_chapter()
        assert isinstance(report, AuditReport)
        assert report.passed is True

    # 确认临时连接已关闭，可删除整个临时书库目录（Windows 无文件锁残留）
    shutil.rmtree(empty_dir)
    assert not empty_dir.exists()


def test_external_indexer_is_not_closed_by_lifecycle_manager(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("cultivation_rpg", tmp_path)
    idx = SqliteIndexer(book_dir)
    try:
        with LifecycleManager(book_dir, indexer=idx) as lm:
            report = lm.audit_chapter(142)
            assert report.passed is True
        # 外部 indexer 仍可继续查询
        hits = idx.search_fts("青铜古镜")
        assert len(hits) >= 1
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# 2. promote_chapter & clean_ttl_artifacts
# ---------------------------------------------------------------------------


def test_promote_chapter_and_auto_ttl_cleanup(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("cultivation_rpg", tmp_path)
    drafts_dir = book_dir / "04_creation" / "drafts"
    packets_dir = book_dir / "04_creation" / "packets"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    packets_dir.mkdir(parents=True, exist_ok=True)

    # 制造历史过程文件（ttl_keep_chapters=5，升格第 142 章后，<= 137 的应被清理，138..142 保留）
    for ch in (135, 137, 138, 141, 142):
        stem = format_chapter(ch)
        (packets_dir / f"{stem}_packet.md").write_text(f"packet {ch}", encoding="utf-8")
        (drafts_dir / f"{stem}_draft.md").write_text(f"# 第{ch}章\n\n林凡踏入暗星拍卖场。", encoding="utf-8")

    with LifecycleManager(book_dir) as lm:
        # dry_run=True 不改动任何文件
        dry_res = lm.promote_chapter(142, dry_run=True, run_audit=True)
        assert isinstance(dry_res, PromoteResult)
        assert dry_res.dry_run is True
        assert dry_res.manuscript_path == "04_creation/manuscript/vol_01/ch_0142.md"
        assert set(dry_res.cleaned_paths) == {
            "04_creation/drafts/ch_0135_draft.md",
            "04_creation/drafts/ch_0137_draft.md",
            "04_creation/packets/ch_0135_packet.md",
            "04_creation/packets/ch_0137_packet.md",
        }
        assert not (book_dir / dry_res.manuscript_path).exists()
        assert (packets_dir / "ch_0135_packet.md").exists()
        assert NovelConfig.load(book_dir).current_chapter == 141

        # 正式定稿入库
        res = lm.promote_chapter(142, dry_run=False, run_audit=True)
        assert res.dry_run is False
        assert res.manuscript_path == "04_creation/manuscript/vol_01/ch_0142.md"
        assert (book_dir / res.manuscript_path).read_text(encoding="utf-8").startswith("# 第142章")
        assert NovelConfig.load(book_dir).current_chapter == 142

        ws = WorldState.model_validate(
            yaml.safe_load((book_dir / "03_state" / "world_state.yaml").read_text(encoding="utf-8"))
        )
        assert ws.current_chapter == 142
        assert ws.in_world_time == "天历三年冬·腊月初八"

        # TTL <= 137 已删除，138..142 仍保留
        assert not (packets_dir / "ch_0135_packet.md").exists()
        assert not (drafts_dir / "ch_0137_draft.md").exists()
        assert (packets_dir / "ch_0138_packet.md").exists()
        assert (drafts_dir / "ch_0142_draft.md").exists()


def test_promote_chapter_audit_failure_blocks_and_vol_inference(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("cultivation_rpg", tmp_path)
    drafts_dir = book_dir / "04_creation" / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    # 命中禁用词 "恐怖如斯"
    (drafts_dir / "ch_142_draft.md").write_text("# 第142章\n\n此剑气恐怖如斯！", encoding="utf-8")

    with LifecycleManager(book_dir) as lm:
        with pytest.raises(ValueError, match="一致性体检未通过"):
            lm.promote_chapter(142, run_audit=True)
        assert not (book_dir / "04_creation" / "manuscript" / "vol_01" / "ch_0142.md").exists()

        # 不跑 audit 时验证兼容 ch_142_draft.md 与卷号推断回退
        (book_dir / "02_structure" / "volumes" / "vol_02.md").write_text(
            "---\nid: vol_02\nchapter_range: [151, 220]\n---\n",
            encoding="utf-8",
        )
        (drafts_dir / "ch_160_draft.md").write_text("# 第160章\n\n新卷开篇。", encoding="utf-8")
        res = lm.promote_chapter(160, run_audit=False)
        assert res.manuscript_path == "04_creation/manuscript/vol_02/ch_0160.md"

        with pytest.raises(ValueError):
            lm.promote_chapter(True)  # type: ignore[arg-type]
        with pytest.raises(FileNotFoundError):
            lm.promote_chapter(999)


def test_clean_ttl_artifacts_standalone_validation(tmp_path: Path) -> None:
    book_dir = _make_minimal_book(tmp_path, current_chapter=10)
    packets_dir = book_dir / "04_creation" / "packets"
    for ch in range(1, 12):
        (packets_dir / f"ch_{ch:04d}_packet.md").write_text(f"p{ch}", encoding="utf-8")
    (packets_dir / "notes.md").write_text("unrelated", encoding="utf-8")

    with LifecycleManager(book_dir) as lm:
        with pytest.raises(ValueError):
            lm.clean_ttl_artifacts(current_ch=-1)
        with pytest.raises(ValueError):
            lm.clean_ttl_artifacts(keep_chapters=False)  # type: ignore[arg-type]

        cleaned = lm.clean_ttl_artifacts(current_ch=10, keep_chapters=3, dry_run=False)
        assert [p.name for p in cleaned] == [f"ch_{c:04d}_packet.md" for c in range(1, 8)]
        assert (packets_dir / "ch_0008_packet.md").exists()
        assert (packets_dir / "notes.md").exists()


# ---------------------------------------------------------------------------
# 3. cold_archive_gc (死亡/退场/超期冷归档 + 未完结伏笔绑定保护)
# ---------------------------------------------------------------------------


def test_cold_archive_gc_protects_bound_threads_and_archives_four_pools(tmp_path: Path) -> None:
    book_dir = _make_minimal_book(tmp_path, current_chapter=100)

    # 设定：死亡角色、超期未绑伏笔角色、超期但绑定未完结伏笔角色、退休势力、活跃势力
    chars_dir = book_dir / "01_settings" / "characters"
    (chars_dir / "char_dead.md").write_text(
        "---\nid: char_dead\nname: 死人甲\nstatus: deceased\n---\n", encoding="utf-8"
    )
    (chars_dir / "char_cold.md").write_text(
        "---\nid: char_cold\nname: 路人乙\nstatus: active\n---\n", encoding="utf-8"
    )
    (chars_dir / "char_bound.md").write_text(
        "---\nid: char_bound\nname: 潜伏丙\nstatus: active\n---\n", encoding="utf-8"
    )
    facs_dir = book_dir / "01_settings" / "factions"
    facs_dir.mkdir(parents=True, exist_ok=True)
    (facs_dir / "fac_fallen.md").write_text(
        "---\nid: fac_fallen\nname: 覆灭宗\nstatus: active\n---\n", encoding="utf-8"
    )

    (book_dir / "03_state" / "active" / "characters.yaml").write_text(
        yaml.safe_dump(
            {
                "char_dead": {"updated_at_ch": 95, "physical_mental": "已故", "author_tag": "保留extra"},
                "char_cold": {"updated_at_ch": 10, "physical_mental": "隐居"},
                "char_bound": {"updated_at_ch": 10, "physical_mental": "潜伏中"},
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (book_dir / "03_state" / "active" / "factions.yaml").write_text(
        yaml.safe_dump(
            {
                "fac_fallen": {"updated_at_ch": 90, "status": "retired"},
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (book_dir / "03_state" / "relations.yaml").write_text(
        yaml.safe_dump(
            {
                "relations": [
                    {
                        "source": "char_cold",
                        "target": "char_bound",
                        "relation": "旧日盟友",
                        "valid_from_ch": 1,
                        "invalid_at_ch": 50,
                    },
                    {
                        "source": "char_bound",
                        "target": "fac_fallen",
                        "relation": "暗中调查",
                        "valid_from_ch": 10,
                        "invalid_at_ch": None,
                    },
                ]
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (book_dir / "03_state" / "threads.yaml").write_text(
        yaml.safe_dump(
            {
                "threads": [
                    {
                        "id": "thread_keep_bound",
                        "title": "潜伏丙的真实身份",
                        "status": "dormant",
                        "planted_at": 5,
                        "last_progressed_at": 10,
                        "trigger_entities": ["char_bound"],
                    },
                    {
                        "id": "thread_done",
                        "title": "已填完伏笔",
                        "status": "resolved",
                        "planted_at": 1,
                        "last_progressed_at": 20,
                        "trigger_entities": ["char_cold"],
                    },
                ]
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    with LifecycleManager(book_dir) as lm:
        # dry_run=True
        dry_gc = lm.cold_archive_gc(current_ch=100, dry_run=True)
        assert isinstance(dry_gc, GCReport)
        assert dry_gc.dry_run is True
        assert set(dry_gc.archived_characters) == {"char_dead", "char_cold"}
        assert dry_gc.archived_factions == ["fac_fallen"]
        assert dry_gc.archived_relations_count == 1
        assert dry_gc.archived_threads == ["thread_done"]
        assert not (book_dir / "03_state" / "archive" / "characters.yaml").exists()

        # 正式归档
        gc_res = lm.cold_archive_gc(current_ch=100, dry_run=False)
        assert gc_res.dry_run is False
        assert set(gc_res.archived_characters) == {"char_dead", "char_cold"}
        assert "char_bound" not in gc_res.archived_characters

        # char_bound 仍保留在热池
        hot_chars = CharacterStateFile.model_validate(
            yaml.safe_load((book_dir / "03_state" / "active" / "characters.yaml").read_text(encoding="utf-8"))
        )
        assert list(hot_chars.root.keys()) == ["char_bound"]

        # archive/characters.yaml 保留 model_extra
        arch_chars_raw = yaml.safe_load(
            (book_dir / "03_state" / "archive" / "characters.yaml").read_text(encoding="utf-8")
        )
        assert arch_chars_raw["char_dead"]["author_tag"] == "保留extra"
        assert "char_cold" in arch_chars_raw

        # relations / threads 迁移准确
        hot_rels = RelationsFile.model_validate(
            yaml.safe_load((book_dir / "03_state" / "relations.yaml").read_text(encoding="utf-8"))
        )
        arch_rels = RelationsFile.model_validate(
            yaml.safe_load((book_dir / "03_state" / "archive" / "relations.yaml").read_text(encoding="utf-8"))
        )
        assert len(hot_rels.relations) == 1
        assert len(arch_rels.relations) == 1

        hot_thr = ThreadsFile.model_validate(
            yaml.safe_load((book_dir / "03_state" / "threads.yaml").read_text(encoding="utf-8"))
        )
        arch_thr = ThreadsFile.model_validate(
            yaml.safe_load((book_dir / "03_state" / "archive" / "threads.yaml").read_text(encoding="utf-8"))
        )
        assert [t.id for t in hot_thr.threads] == ["thread_keep_bound"]
        assert [t.id for t in arch_thr.threads] == ["thread_done"]


def test_cold_archive_gc_uses_max_fact_chapter_for_last_seen(tmp_path: Path) -> None:
    book_dir = _make_minimal_book(tmp_path, current_chapter=100)
    (book_dir / "01_settings" / "characters" / "char_recent_fact.md").write_text(
        "---\nid: char_recent_fact\nname: 近期提及角色\nstatus: active\n---\n",
        encoding="utf-8",
    )
    (book_dir / "03_state" / "active" / "characters.yaml").write_text(
        yaml.safe_dump({"char_recent_fact": {"updated_at_ch": 10}}, sort_keys=False),
        encoding="utf-8",
    )
    (book_dir / "03_state" / "history" / "vol_01_facts.jsonl").write_text(
        json.dumps(
            {
                "ch": 85,
                "summary": "提及近期提及角色。",
                "facts": ["近期提及角色传信而来"],
                "entities": ["char_recent_fact"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    with LifecycleManager(book_dir) as lm:
        gc_res = lm.cold_archive_gc(current_ch=100, cold_after_chapters=40)
        assert gc_res.archived_characters == []


# ---------------------------------------------------------------------------
# 4. drop_thread (内置回退 + 注入 ImpactAnalyzer 协同)
# ---------------------------------------------------------------------------


def test_drop_thread_builtin_fallback_and_physical_removal(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("cultivation_rpg", tmp_path)
    # 在前文定稿章节写入伏笔完整标题，以同时验证 MANUSCRIPT 与 BLUEPRINT 多维提及召回
    (book_dir / "04_creation" / "manuscript" / "vol_01" / "ch_0140.md").write_text(
        "# 第140章 余悸\n\n林凡内视经脉，深知气海边缘潜伏的赵家血魂印追踪禁制迟早会引来追兵。\n",
        encoding="utf-8",
    )

    with LifecycleManager(book_dir) as lm:
        with pytest.raises(ValueError):
            lm.drop_thread("invalid_thread")
        with pytest.raises(KeyError):
            lm.drop_thread("thread_nonexistent")

        # dry_run=True 检查前文与后续蓝图提及，排除 threads.yaml 自身条目
        dry_drop = lm.drop_thread("thread_xuehunyin_curse", dry_run=True)
        assert isinstance(dry_drop, DropResult)
        assert dry_drop.thread_id == "thread_xuehunyin_curse"
        assert dry_drop.removed_from == "03_state/threads.yaml"
        assert dry_drop.dry_run is True
        assert any(
            h.scope == ImpactScope.MANUSCRIPT and h.chapter_num == 140
            for h in dry_drop.prior_mentions
        )
        assert any(
            h.scope == ImpactScope.BLUEPRINT and h.chapter_num == 142
            for h in dry_drop.prior_mentions
        )
        assert not any(
            h.scope == ImpactScope.THREAD and h.location == "03_state/threads.yaml"
            for h in dry_drop.prior_mentions
        )

        # 正式移除并同步索引
        res = lm.drop_thread("thread_xuehunyin_curse", dry_run=False)
        assert res.dry_run is False
        thr_file = ThreadsFile.model_validate(
            yaml.safe_load((book_dir / "03_state" / "threads.yaml").read_text(encoding="utf-8"))
        )
        assert all(t.id != "thread_xuehunyin_curse" for t in thr_file.threads)

    with SqliteIndexer(book_dir) as idx:
        assert all(t.id != "thread_xuehunyin_curse" for t in idx.get_threads())


def test_drop_thread_and_prune_with_injected_impact_analyzer(tmp_path: Path) -> None:
    book_dir = _make_minimal_book(tmp_path, current_chapter=100)
    (book_dir / "01_settings" / "characters" / "char_mock_orphan.md").write_text(
        "---\nid: char_mock_orphan\nname: 孤儿甲\n---\n",
        encoding="utf-8",
    )
    (book_dir / "03_state" / "threads.yaml").write_text(
        yaml.safe_dump(
            {
                "threads": [
                    {
                        "id": "thread_mock_drop",
                        "title": "暗桩密信",
                        "status": "active",
                        "planted_at": 10,
                        "last_progressed_at": 20,
                    }
                ]
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    calls: list[str] = []

    class FakeImpactAnalyzer:
        def scan_impact(self, target_id_or_keyword: str, *, sync_index: bool | None = None) -> ImpactReport:
            calls.append(target_id_or_keyword)
            if target_id_or_keyword == "thread_mock_drop":
                return ImpactReport(
                    target=target_id_or_keyword,
                    expanded_terms=["thread_mock_drop", "暗桩密信"],
                    hits=[
                        ImpactHit(
                            scope=ImpactScope.THREAD,
                            location="03_state/threads.yaml",
                            chapter_num=20,
                            excerpt="thread_mock_drop: 暗桩密信",
                        ),
                        ImpactHit(
                            scope=ImpactScope.MANUSCRIPT,
                            location="04_creation/manuscript/vol_01/ch_0012.md",
                            chapter_num=12,
                            matched_term="暗桩密信",
                            excerpt="他在袖中摸到那封暗桩密信。",
                        ),
                    ],
                )
            return ImpactReport(target=target_id_or_keyword, entity_id=target_id_or_keyword, hits=[])

    with LifecycleManager(book_dir, impact_analyzer=FakeImpactAnalyzer()) as lm:
        drop_res = lm.drop_thread("thread_mock_drop", dry_run=True)
        assert len(drop_res.prior_mentions) == 1
        assert drop_res.prior_mentions[0].scope == ImpactScope.MANUSCRIPT

        prune_res = lm.prune_settings(apply=False)
        assert prune_res.orphan_ids == ["char_mock_orphan"]
        assert "thread_mock_drop" in calls
        assert "char_mock_orphan" in calls


# ---------------------------------------------------------------------------
# 5. prune_settings (孤儿废案 & 远期龙套剪枝 + 活跃实体保护)
# ---------------------------------------------------------------------------


def test_prune_settings_orphan_low_frequency_and_active_protection(tmp_path: Path) -> None:
    book_dir = _make_minimal_book(tmp_path, current_chapter=120)
    chars_dir = book_dir / "01_settings" / "characters"
    (chars_dir / "char_orphan.md").write_text(
        "---\nid: char_orphan\nname: 废案角色\n---\n", encoding="utf-8"
    )
    (chars_dir / "char_one_shot.md").write_text(
        "---\nid: char_one_shot\nname: 客栈掌柜\n---\n", encoding="utf-8"
    )
    (chars_dir / "char_active_hero.md").write_text(
        "---\nid: char_active_hero\nname: 主角林风\n---\n", encoding="utf-8"
    )
    (chars_dir / "char_recent_npc.md").write_text(
        "---\nid: char_recent_npc\nname: 新出场客卿\n---\n", encoding="utf-8"
    )

    # char_active_hero 在活跃热状态中
    (book_dir / "03_state" / "active" / "characters.yaml").write_text(
        yaml.safe_dump({"char_active_hero": {"updated_at_ch": 120}}, sort_keys=False),
        encoding="utf-8",
    )
    # char_one_shot 仅在第 5 章出场一次（距今 115 >= 40）
    (book_dir / "04_creation" / "manuscript" / "vol_01" / "ch_0005.md").write_text(
        "# 第5章\n\n客栈掌柜端上一壶热茶。",
        encoding="utf-8",
    )
    # char_recent_npc 在第 110 章出场一次（距今 10 < 40，不应剪枝）
    (book_dir / "04_creation" / "manuscript" / "vol_01" / "ch_0110.md").write_text(
        "# 第110章\n\n新出场客卿拱手施礼。",
        encoding="utf-8",
    )

    with LifecycleManager(book_dir) as lm:
        preview = lm.prune_settings(
            target_ids=["char_orphan", "char_active_hero", "char_recent_npc", "char_missing"],
            apply=False,
        )
        assert isinstance(preview, PruneReport)
        assert preview.dry_run is True
        assert preview.orphan_ids == ["char_orphan"]
        assert preview.low_frequency_ids == ["char_one_shot"]
        assert preview.deleted_paths == []
        assert preview.skipped_ids == ["char_active_hero", "char_recent_npc", "char_missing"]
        assert (chars_dir / "char_orphan.md").exists()

        # apply=True 指定 target_ids，仅安全删除 char_orphan 与 char_one_shot，保护活跃/近期实体
        applied = lm.prune_settings(
            target_ids=["char_orphan", "char_one_shot", "char_active_hero"],
            apply=True,
        )
        assert applied.dry_run is False
        assert applied.deleted_paths == [
            "01_settings/characters/char_orphan.md",
            "01_settings/characters/char_one_shot.md",
        ]
        assert applied.skipped_ids == ["char_active_hero"]
        assert not (chars_dir / "char_orphan.md").exists()
        assert not (chars_dir / "char_one_shot.md").exists()
        assert (chars_dir / "char_active_hero.md").exists()

        # 验证 SQLite 级联清理
        with SqliteIndexer(book_dir) as idx:
            assert idx.get_entity("char_orphan") is None
            assert idx.get_entity("char_one_shot") is None
            assert idx.get_entity("char_active_hero") is not None


# ---------------------------------------------------------------------------
# 6. compact_volume_facts (手写里程碑 & 启发式自动浓缩)
# ---------------------------------------------------------------------------


def test_compact_volume_facts_auto_and_explicit_milestones(tmp_path: Path) -> None:
    book_dir = _make_minimal_book(tmp_path, current_chapter=60)
    history_path = book_dir / "03_state" / "history" / "vol_01_facts.jsonl"

    records = [
        ChapterFactRecord(
            ch=ch,
            volume_id="vol_01",
            arc_id=f"vol_01_arc_{1 + (ch - 1) // 15:02d}",
            in_world_time=f"第{ch}日",
            summary=f"第{ch}章事件摘要。",
            facts=[f"第{ch}章事实1", f"第{ch}章事实2", f"第{ch}章事实3"],
            entities=["char_a"] if ch % 5 != 0 else ["char_a", "char_b"],
        )
        for ch in range(1, 51)
    ]
    history_path.write_text(
        "\n".join(r.model_dump_json(by_alias=True, exclude_none=True) for r in records) + "\n",
        encoding="utf-8",
    )

    with LifecycleManager(book_dir) as lm:
        with pytest.raises(ValueError):
            lm.compact_volume_facts("bad_vol")
        with pytest.raises(FileNotFoundError):
            lm.compact_volume_facts("vol_99")

        # 自动浓缩至 compact_max_milestones (20)
        dry_compact = lm.compact_volume_facts("vol_01", dry_run=True)
        assert isinstance(dry_compact, CompactResult)
        assert dry_compact.original_count == 50
        assert 15 <= dry_compact.compacted_count <= 20
        assert dry_compact.dry_run is True
        assert len(history_path.read_text(encoding="utf-8").splitlines()) == 50

        auto_compact = lm.compact_volume_facts("vol_01", dry_run=False)
        assert auto_compact.compacted_count == 20
        compacted_lines = history_path.read_text(encoding="utf-8").splitlines()
        assert len(compacted_lines) == 20
        parsed_chs = [ChapterFactRecord.model_validate_json(line).ch for line in compacted_lines]
        assert parsed_chs[0] == 1
        assert parsed_chs[-1] == 50
        assert parsed_chs == sorted(parsed_chs)

        # 显式传入 milestones (支持 Mapping dict，含别名 arc/time)
        explicit = [
            {
                "ch": 1,
                "arc": "vol_01_arc_01",
                "time": "初春",
                "summary": "开篇入宗。",
                "facts": ["初入宗门", "结识师尊", "立下目标"],
                "entities": ["char_a"],
            },
            {
                "ch": 50,
                "arc": "vol_01_arc_04",
                "time": "岁末",
                "summary": "卷末决战。",
                "facts": ["突破境界", "斩杀强敌", "启程北上"],
                "entities": ["char_a", "char_b"],
            },
        ]
        res_explicit = lm.compact_volume_facts("vol_01", milestones=explicit, dry_run=False)
        assert res_explicit.original_count == 20
        assert res_explicit.compacted_count == 2
        lines_after = history_path.read_text(encoding="utf-8").splitlines()
        assert len(lines_after) == 2
        assert '"arc":"vol_01_arc_01"' in lines_after[0].replace(" ", "")


# ---------------------------------------------------------------------------
# 7. audit_chapter (三套真实 fixture + 全部检查项正反例)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("genre_dir", ["cultivation_rpg", "general", "mystery_intrigue"])
def test_audit_passes_on_three_official_fixtures(genre_dir: str, tmp_path: Path) -> None:
    book_dir = _copy_fixture_book(genre_dir, tmp_path)
    with LifecycleManager(book_dir) as lm:
        report = lm.audit_chapter()
        assert report.passed is True
        assert [i for i in report.issues if i.level == IssueLevel.ERROR] == []


def test_audit_chapter_catches_all_error_and_warning_categories(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("cultivation_rpg", tmp_path)

    # 准备冷库非死亡角色用于测试未唤醒 vs 已唤醒
    (book_dir / "01_settings" / "characters" / "char_hermit.md").write_text(
        "---\nid: char_hermit\nname: 隐世长老\nstatus: retired\n---\n",
        encoding="utf-8",
    )

    # 1) 构造第 143 章蓝图：缺失设定、缺失专题、已完结/冷库伏笔、死人无闪回入 cast
    bp_143 = book_dir / "02_structure" / "blueprints" / "vol_01" / "ch_0143.md"
    bp_143.write_text(
        """---
chapter: 143
volume_id: vol_01
title: "违规测试章"
pov: char_linfan
cast:
  characters:
    - char_linfan
    - char_zhaoqian
    - char_ghost_missing
threads:
  - thread_id: thread_zhuixinggu_ambush
    action: progress
topics:
  - entity_id: char_linfan
    topic: "不存在的专题"
---
### 节拍 1: 正面冲突
林凡与赵乾正面交锋。
""",
        encoding="utf-8",
    )

    # 2) 构造第 143 章草稿：命中禁用词、冷库未唤醒实体、死人无闪回出场
    draft_143 = book_dir / "04_creation" / "drafts" / "ch_0143_draft.md"
    draft_143.parent.mkdir(parents=True, exist_ok=True)
    draft_143.write_text(
        "# 第143章\n\n林凡倒吸一口凉气，只见隐世长老与赵乾并肩走来。",
        encoding="utf-8",
    )

    # 3) 破坏 active/characters.yaml 的 profile_ext 与写入未知字段
    chars_yaml_path = book_dir / "03_state" / "active" / "characters.yaml"
    raw_chars = yaml.safe_load(chars_yaml_path.read_text(encoding="utf-8"))
    raw_chars["char_linfan"]["unknown_note"] = "额外未知字段"
    # 非法 list，CultivationRpgExt 要求 str
    raw_chars["char_suyao"]["profile_ext"]["realm"] = ["筑基圆满"]
    chars_yaml_path.write_text(
        yaml.safe_dump(raw_chars, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    # 4) 写入一条事实数仅 1 条（不在 3~5 区间）的历史事实
    facts_path = book_dir / "03_state" / "history" / "vol_01_facts.jsonl"
    facts_path.write_text(
        facts_path.read_text(encoding="utf-8")
        + json.dumps(
            {
                "ch": 143,
                "summary": "测试章摘要。",
                "facts": ["仅一条原子事实"],
                "entities": ["char_linfan"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    with LifecycleManager(book_dir) as lm:
        with pytest.raises(ValueError):
            lm.audit_chapter(0)
        with pytest.raises(ValueError):
            lm.audit_chapter(True)  # type: ignore[arg-type]

        report = lm.audit_chapter(143)
        assert report.passed is False
        codes = {(iss.level, iss.code) for iss in report.issues}
        assert (IssueLevel.ERROR, "MISSING_SETTING") in codes
        assert (IssueLevel.WARNING, "MISSING_TOPIC") in codes
        assert (IssueLevel.ERROR, "ARCHIVED_THREAD_REF") in codes
        assert (IssueLevel.ERROR, "DECEASED_ENTITY_IN_BLUEPRINT") in codes
        assert (IssueLevel.ERROR, "DECEASED_ENTITY_APPEARANCE") in codes
        assert (IssueLevel.ERROR, "ARCHIVED_ENTITY_UNAWAKENED") in codes
        assert (IssueLevel.ERROR, "BANNED_WORD_HIT") in codes
        assert (IssueLevel.ERROR, "INVALID_CHARACTER_STATE") in codes
        assert (IssueLevel.WARNING, "UNKNOWN_STATE_FIELD") in codes
        assert (IssueLevel.WARNING, "FACT_COUNT_OUT_OF_RANGE") in codes


def test_audit_allows_deceased_flashback_and_awakened_archived_warning(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("cultivation_rpg", tmp_path)
    (book_dir / "01_settings" / "characters" / "char_hermit.md").write_text(
        "---\nid: char_hermit\nname: 隐世长老\nstatus: retired\n---\n",
        encoding="utf-8",
    )
    (book_dir / "02_structure" / "blueprints" / "vol_01" / "ch_0144.md").write_text(
        """---
chapter: 144
volume_id: vol_01
title: "旧事回忆与长老归来"
pov: char_linfan
cast:
  characters:
    - char_linfan
    - char_zhaoqian
    - char_hermit
---
### 节拍 1: 回忆往事
林凡回忆起赵乾生前在坠星谷的狠辣手段，又向隐世长老请教古镜来历。
""",
        encoding="utf-8",
    )
    draft_144 = book_dir / "04_creation" / "drafts" / "ch_0144_draft.md"
    draft_144.parent.mkdir(parents=True, exist_ok=True)
    draft_144.write_text(
        "# 第144章\n\n林凡回忆起赵乾生前的旧事，随即向隐世长老行礼。",
        encoding="utf-8",
    )

    with LifecycleManager(book_dir) as lm:
        report = lm.audit_chapter(144)
        assert report.passed is True
        assert any(
            iss.code == "ARCHIVED_ENTITY_AWAKENED" and iss.level == IssueLevel.WARNING
            for iss in report.issues
        )


def test_promote_chapter_extracts_chinese_heading_and_fallback_and_cleans_stale(
    tmp_path: Path,
) -> None:
    from ddnovel.templates.initializer import init_book

    book_dir = tmp_path / "promote_title_book"
    init_book(
        book_dir,
        title="定稿命名测试",
        genre_profile="general",
        include_samples=True,
    )

    drafts_dir = book_dir / "04_创作" / "草稿"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    draft_106 = drafts_dir / "第0106章_草稿.md"
    draft_1 = drafts_dir / "第0001章_草稿.md"

    with LifecycleManager(book_dir) as lm:
        draft_106.write_text("# 第一百零六章 深渊探印\n\n陆沉推开石门。", encoding="utf-8")
        res1 = lm.promote_chapter(106)
        assert res1.manuscript_path == "04_创作/定稿/第01卷/第0106章-深渊探印.md"
        assert (book_dir / res1.manuscript_path).is_file()

        draft_106.write_text("# 第106章 终局回响\n\n陆沉转身离开。", encoding="utf-8")
        res2 = lm.promote_chapter(106)
        assert res2.manuscript_path == "04_创作/定稿/第01卷/第0106章-终局回响.md"
        assert (book_dir / res2.manuscript_path).is_file()
        assert not (book_dir / "04_创作" / "定稿" / "第01卷" / "第0106章-深渊探印.md").exists()

        draft_1.write_text("# 第1章\n\n深夜电话响起。", encoding="utf-8")
        res3 = lm.promote_chapter(1)
        assert res3.manuscript_path == "04_创作/定稿/第01卷/第0001章-深夜来电与异常校验码.md"
        assert (book_dir / res3.manuscript_path).is_file()


def test_audit_chapter_flags_setting_concrete_leak_warning(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("cultivation_rpg", tmp_path)

    with LifecycleManager(book_dir) as lm:
        clean_report = lm.audit_chapter(142)
        assert not any(i.code == "SETTING_CONCRETE_LEAK" for i in clean_report.issues)

    # 给第142章出场的苏瑶 [核心] 区块注入示范台词与动作清单泄露
    suyao_card = book_dir / "01_settings" / "characters" / "char_suyao.md"
    suyao_card.write_text(
        suyao_card.read_text(encoding="utf-8").replace(
            "## [核心]\n",
            "## [核心]\n常见小动作清单：每次紧张必咬拇指并说：“哼，别管我！我才没受伤！”\n",
            1,
        ),
        encoding="utf-8",
    )

    with LifecycleManager(book_dir) as lm:
        ch_report = lm.audit_chapter(142)
        assert ch_report.passed is True
        assert any(
            iss.level == IssueLevel.WARNING and iss.code == "SETTING_CONCRETE_LEAK"
            for iss in ch_report.issues
        )

        book_report = lm.audit_chapter(None)
        assert any(
            iss.level == IssueLevel.WARNING and iss.code == "SETTING_CONCRETE_LEAK"
            for iss in book_report.issues
        )
