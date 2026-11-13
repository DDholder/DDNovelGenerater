"""tests/test_indexer.py

Comprehensive Unit & Integration Test Suite for SqliteIndexer (Task T06).
Covers database lifecycle, incremental sync, full rebuild, cascade cleanup,
CJK dual-mode FTS5 search, bi-temporal relations, entity bundle aggregation,
recent summaries, transaction rollback, and vector plugin soft integration.
"""

from __future__ import annotations

import shutil
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from ddnovel.core import (
    AliasMatcher,
    ProgressiveParser,
    SqliteIndexer,
    VectorPlugin,
)
from ddnovel.core.schemas import (
    ChapterFactRecord,
    CharacterState,
    EntityBundle,
    EntityStatus,
    FtsScope,
    SearchHit,
    SettingCategory,
    SyncReport,
    ThreadStatus,
)

FIXTURES_BOOKS_DIR = Path(__file__).resolve().parent / "fixtures" / "books"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cultivation_book(tmp_path: Path) -> Path:
    """Creates a temporary, fully isolated copy of the cultivation_rpg fixture book."""
    src = FIXTURES_BOOKS_DIR / "cultivation_rpg"
    dst = tmp_path / "cultivation_rpg"
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns(".ddnovel", "*.sqlite", "*.sqlite-wal", "*.sqlite-shm"),
    )
    return dst


@pytest.fixture
def empty_book(tmp_path: Path) -> Path:
    """Creates a minimal valid book directory containing only novel.yaml."""
    book_dir = tmp_path / "empty_book"
    book_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / "novel.yaml").write_text(
        "title: 空书测试\nauthor: 测试者\nschema_version: 1\n",
        encoding="utf-8",
    )
    return book_dir


@pytest.fixture
def indexer(cultivation_book: Path) -> Generator[SqliteIndexer, None, None]:
    """Provides an un-synced SqliteIndexer instance with guaranteed Windows cleanup."""
    idx = SqliteIndexer(cultivation_book)
    try:
        yield idx
    finally:
        idx.close()


@pytest.fixture
def synced_indexer(cultivation_book: Path) -> Generator[SqliteIndexer, None, None]:
    """Provides a pre-synced SqliteIndexer instance with guaranteed Windows cleanup."""
    idx = SqliteIndexer(cultivation_book)
    try:
        idx.sync()
        yield idx
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# 1. Database Lifecycle & Schema DDL Tests
# ---------------------------------------------------------------------------


def test_init_creates_database_and_directory(empty_book: Path) -> None:
    with SqliteIndexer(empty_book) as idx:
        assert (empty_book / ".ddnovel").is_dir()
        assert (empty_book / ".ddnovel" / "index.sqlite").is_file()
        assert idx.db_path == empty_book / ".ddnovel" / "index.sqlite"


def test_init_custom_db_path(tmp_path: Path, empty_book: Path) -> None:
    custom_path = tmp_path / "custom_dir" / "custom.sqlite"
    with SqliteIndexer(empty_book, db_path=custom_path) as idx:
        assert custom_path.is_file()
        assert idx.db_path == custom_path
        assert not (empty_book / ".ddnovel" / "index.sqlite").exists()


def test_database_ddl_and_triggers_exist(indexer: SqliteIndexer) -> None:
    conn = indexer.conn
    cursor = conn.cursor()

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}
    expected_tables = {
        "file_hashes",
        "entities",
        "entity_aliases",
        "setting_topics",
        "relations",
        "threads",
        "chapter_facts",
        "fts_docs",
        "fts_docs_idx",
    }
    assert expected_tables.issubset(tables)

    cursor.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    triggers = {row[0] for row in cursor.fetchall()}
    assert {"fts_docs_ai", "fts_docs_ad", "fts_docs_au"}.issubset(triggers)


def test_pragmas_foreign_keys_and_wal(indexer: SqliteIndexer) -> None:
    conn = indexer.conn
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert fk == 1
    assert journal.lower() in ("wal", "memory")


def test_context_manager_and_close(empty_book: Path) -> None:
    idx = SqliteIndexer(empty_book)
    with idx:
        assert idx.conn is not None
        assert idx.conn.execute("SELECT 1").fetchone()[0] == 1
    # After exiting context manager, connection is closed
    with pytest.raises(RuntimeError):
        _ = idx.conn


# ---------------------------------------------------------------------------
# 2. Full Sync & Idempotency Tests
# ---------------------------------------------------------------------------


def test_full_sync_on_real_fixture(indexer: SqliteIndexer) -> None:
    report = indexer.sync()
    assert isinstance(report, SyncReport)
    assert report.rebuilt is False
    assert report.elapsed_ms >= 0.0
    assert len(report.added) >= 15
    assert report.updated == []
    assert report.deleted == []

    added_set = set(report.added)
    assert "01_settings/characters/char_linfan.md" in added_set
    assert "01_settings/factions/fac_qingyunmen.md" in added_set
    assert "02_structure/blueprints/vol_01/ch_0142.md" in added_set
    assert "03_state/relations.yaml" in added_set
    assert "03_state/history/vol_01_facts.jsonl" in added_set


def test_sync_populates_entities_and_aliases(synced_indexer: SqliteIndexer) -> None:
    linfan = synced_indexer.get_entity("char_linfan")
    assert linfan is not None
    assert linfan.name == "林凡"
    assert linfan.category == SettingCategory.CHARACTER
    assert "林师兄" in linfan.aliases
    assert "青云弃徒" in linfan.aliases
    assert "惊雷剑诀" in linfan.topics

    zhaoqian = synced_indexer.get_entity("char_zhaoqian")
    assert zhaoqian is not None
    bundle = synced_indexer.get_entity_bundle("char_zhaoqian", at_chapter=87)
    assert bundle.is_archived is True


def test_sync_populates_relations_threads_facts(synced_indexer: SqliteIndexer) -> None:
    conn = synced_indexer.conn
    rel_count = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    assert rel_count >= 6

    thr_count = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    assert thr_count >= 4

    fact_count = conn.execute("SELECT COUNT(*) FROM chapter_facts").fetchone()[0]
    assert fact_count >= 3

    fts_count = conn.execute("SELECT COUNT(*) FROM fts_docs").fetchone()[0]
    assert fts_count >= 20


def test_sync_idempotency_zero_changes(synced_indexer: SqliteIndexer) -> None:
    report = synced_indexer.sync()
    assert report.added == []
    assert report.updated == []
    assert report.deleted == []
    assert report.rebuilt is False


# ---------------------------------------------------------------------------
# 3. Incremental Sync Tests
# ---------------------------------------------------------------------------


def test_incremental_sync_add_setting(
    cultivation_book: Path, synced_indexer: SqliteIndexer
) -> None:
    new_char = cultivation_book / "01_settings" / "characters" / "char_mojin.md"
    new_char.write_text(
        "---\n"
        "id: char_mojin\n"
        "name: 莫金\n"
        'aliases: ["金老怪"]\n'
        "category: character\n"
        "status: active\n"
        "---\n"
        "## [核心]\n"
        "魔道散修，精通血道遁术。\n",
        encoding="utf-8",
    )

    report = synced_indexer.sync()
    assert "01_settings/characters/char_mojin.md" in report.added
    assert report.updated == []
    assert report.deleted == []

    entity = synced_indexer.get_entity("char_mojin")
    assert entity is not None
    assert entity.name == "莫金"

    hits = synced_indexer.search_fts("金老怪")
    assert any(h.entity_id == "char_mojin" for h in hits)


def test_incremental_sync_update_setting(
    cultivation_book: Path, synced_indexer: SqliteIndexer
) -> None:
    target = cultivation_book / "01_settings" / "characters" / "char_linfan.md"
    content = target.read_text(encoding="utf-8")
    updated_content = content.replace("为人沉稳寡言", "为人沉稳寡言，剑心通明")
    target.write_text(updated_content, encoding="utf-8")

    report = synced_indexer.sync()
    assert "01_settings/characters/char_linfan.md" in report.updated
    assert report.added == []
    assert report.deleted == []

    hits = synced_indexer.search_fts("剑心通明")
    assert len(hits) > 0
    assert hits[0].entity_id == "char_linfan"


def test_incremental_sync_delete_setting(
    cultivation_book: Path, synced_indexer: SqliteIndexer
) -> None:
    target = cultivation_book / "01_settings" / "characters" / "char_suyao.md"
    target.unlink()

    report = synced_indexer.sync()
    assert "01_settings/characters/char_suyao.md" in report.deleted

    assert synced_indexer.get_entity("char_suyao") is None
    setting_hits = synced_indexer.search_fts("苏瑶", scope=FtsScope.SETTING)
    assert len(setting_hits) == 0


def test_incremental_sync_update_state_yaml(
    cultivation_book: Path, synced_indexer: SqliteIndexer
) -> None:
    rel_path = cultivation_book / "03_state" / "relations.yaml"
    content = rel_path.read_text(encoding="utf-8")
    new_entry = (
        "  - source: char_linfan\n"
        "    target: loc_zhuixinggu\n"
        '    relation: "探索遗迹"\n'
        "    valid_from_ch: 80\n"
        "    invalid_at_ch: 90\n"
    )
    rel_path.write_text(content + "\n" + new_entry, encoding="utf-8")

    report = synced_indexer.sync()
    assert "03_state/relations.yaml" in report.updated

    active_85 = synced_indexer.get_active_relations("char_linfan", at_chapter=85)
    assert any(r.target == "loc_zhuixinggu" and "探索遗迹" in r.relation for r in active_85)

    active_90 = synced_indexer.get_active_relations("char_linfan", at_chapter=90)
    assert not any(r.target == "loc_zhuixinggu" for r in active_90)


def test_incremental_sync_batch_changes(
    cultivation_book: Path, synced_indexer: SqliteIndexer
) -> None:
    # 1. Add 1 file
    new_char = cultivation_book / "01_settings" / "characters" / "char_batch_add.md"
    new_char.write_text(
        "---\nid: char_batch_add\nname: 批次新增\ncategory: character\n"
        "status: active\n---\n## [核心]\n内容\n",
        encoding="utf-8",
    )

    # 2. Modify 1 file
    mod_target = cultivation_book / "01_settings" / "characters" / "char_linfan.md"
    mod_content = mod_target.read_text(encoding="utf-8")
    mod_target.write_text(mod_content + "\n<!-- batch change -->\n", encoding="utf-8")

    # 3. Delete 1 file
    del_target = cultivation_book / "01_settings" / "items" / "item_pozhangdan.md"
    del_target.unlink()

    report = synced_indexer.sync()
    assert len(report.added) == 1
    assert "01_settings/characters/char_batch_add.md" in report.added
    assert len(report.updated) == 1
    assert "01_settings/characters/char_linfan.md" in report.updated
    assert len(report.deleted) == 1
    assert "01_settings/items/item_pozhangdan.md" in report.deleted


# ---------------------------------------------------------------------------
# 4. Zero-Pollution Rebuild Tests
# ---------------------------------------------------------------------------


def test_rebuild_method(synced_indexer: SqliteIndexer) -> None:
    report = synced_indexer.rebuild()
    assert report.rebuilt is True
    assert len(report.added) >= 15
    assert synced_indexer.get_entity("char_linfan") is not None


def test_sync_force_rebuild_flag(synced_indexer: SqliteIndexer) -> None:
    report = synced_indexer.sync(force_rebuild=True)
    assert report.rebuilt is True
    assert synced_indexer.get_entity("char_linfan") is not None


def test_rebuild_cleans_dirty_untracked_data(synced_indexer: SqliteIndexer) -> None:
    synced_indexer.conn.execute(
        "INSERT INTO entities(id, name, category, status, file_path) "
        "VALUES ('ghost_99', '幽灵老怪', 'character', 'active', 'ghost.md')"
    )
    synced_indexer.conn.commit()

    assert synced_indexer.get_entity("ghost_99") is not None

    synced_indexer.rebuild()
    assert synced_indexer.get_entity("ghost_99") is None


# ---------------------------------------------------------------------------
# 5. Cascade Deletion Tests
# ---------------------------------------------------------------------------


def test_cascade_delete_setting_file(
    cultivation_book: Path, synced_indexer: SqliteIndexer
) -> None:
    assert len(synced_indexer.search_fts("惊雷剑诀")) > 0

    target = cultivation_book / "01_settings" / "characters" / "char_linfan.md"
    target.unlink()

    synced_indexer.sync()

    assert synced_indexer.get_entity("char_linfan") is None
    cur = synced_indexer.conn.cursor()
    assert cur.execute("SELECT COUNT(*) FROM entity_aliases WHERE entity_id='char_linfan'").fetchone()[0] == 0
    assert cur.execute("SELECT COUNT(*) FROM setting_topics WHERE entity_id='char_linfan'").fetchone()[0] == 0
    assert cur.execute("SELECT COUNT(*) FROM fts_docs WHERE entity_id='char_linfan'").fetchone()[0] == 0
    assert len(synced_indexer.search_fts("惊雷剑诀")) == 0


def test_cascade_delete_facts_file(
    cultivation_book: Path, synced_indexer: SqliteIndexer
) -> None:
    facts_file = cultivation_book / "03_state" / "history" / "vol_01_facts.jsonl"
    facts_file.unlink()

    synced_indexer.sync()

    cur = synced_indexer.conn.cursor()
    assert cur.execute("SELECT COUNT(*) FROM chapter_facts").fetchone()[0] == 0
    assert len(synced_indexer.search_fts("半式惊雷剑意")) == 0


def test_cascade_delete_blueprint_file(
    cultivation_book: Path, synced_indexer: SqliteIndexer
) -> None:
    bp_file = cultivation_book / "02_structure" / "blueprints" / "vol_01" / "ch_0142.md"
    bp_file.unlink()

    synced_indexer.sync()

    cur = synced_indexer.conn.cursor()
    bp_hits = cur.execute(
        "SELECT COUNT(*) FROM fts_docs WHERE scope = 'blueprint' AND chapter_num = 142"
    ).fetchone()[0]
    assert bp_hits == 0


def test_cascade_delete_character_state_file_cleans_state_and_ghost_entities(
    empty_book: Path,
) -> None:
    """Deleting characters.yaml must cascade-delete pure state entities
    and clear state_yaml_json for setting entities.

    1. char_npc exists only in characters.yaml (no setting file) -> must be removed from entities.
    2. char_hero exists in setting file and characters.yaml -> must retain entity record
       but have state_yaml_json reset to NULL.
    """
    sett_dir = empty_book / "01_settings" / "characters"
    sett_dir.mkdir(parents=True, exist_ok=True)
    (sett_dir / "char_hero.md").write_text(
        "---\n"
        "id: char_hero\n"
        "name: 主角\n"
        "category: character\n"
        "status: active\n"
        "---\n"
        "# 核心\n"
        "主角设定\n",
        encoding="utf-8",
    )

    state_dir = empty_book / "03_state" / "active"
    state_dir.mkdir(parents=True, exist_ok=True)
    chars_file = state_dir / "characters.yaml"
    chars_file.write_text(
        yaml.dump(
            {
                "char_hero": {
                    "updated_at_ch": 10,
                    "location": "loc_town",
                    "physical_mental": "健康",
                    "current_goals": [],
                },
                "char_npc": {
                    "updated_at_ch": 10,
                    "location": "loc_shop",
                    "physical_mental": "健康",
                    "current_goals": [],
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    idx = SqliteIndexer(empty_book)
    try:
        idx.sync()
        conn = idx.conn

        # Initial presence verification
        assert conn.execute("SELECT COUNT(*) FROM entities WHERE id = 'char_hero'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM entities WHERE id = 'char_npc'").fetchone()[0] == 1
        assert idx.get_entity_bundle("char_hero").state is not None

        # Delete state file and sync
        chars_file.unlink()
        report = idx.sync()
        assert "03_state/active/characters.yaml" in report.deleted

        # Check 1: char_npc had no setting file, must be completely removed
        npc_count = conn.execute("SELECT COUNT(*) FROM entities WHERE id = 'char_npc'").fetchone()[0]
        # Check 2: char_hero retains setting record, but dynamic state must be reset to None
        hero_bundle = idx.get_entity_bundle("char_hero")
        hero_state = hero_bundle.state

        assert npc_count == 0, (
            "Ghost record detected: 'char_npc' still exists in entities after characters.yaml was deleted!"
        )
        assert hero_state is None, (
            f"Stale state detected: 'char_hero' still has state {hero_state} "
            "after characters.yaml was deleted!"
        )
    finally:
        idx.close()


def test_cascade_update_state_file_removes_omitted_entity(
    empty_book: Path,
) -> None:
    """Updating characters.yaml to omit an entity must cascade-delete the omitted pure-state entity.

    When characters.yaml is updated from [char_a, char_b] to [char_a]:
    char_b (which has no setting file) must be removed from entities with zero ghost records.
    """
    state_dir = empty_book / "03_state" / "active"
    state_dir.mkdir(parents=True, exist_ok=True)
    chars_file = state_dir / "characters.yaml"

    # Initially defines char_a and char_b
    chars_file.write_text(
        yaml.dump(
            {
                "char_a": {
                    "updated_at_ch": 10,
                    "location": "loc_a",
                    "physical_mental": "好",
                    "current_goals": [],
                },
                "char_b": {
                    "updated_at_ch": 10,
                    "location": "loc_b",
                    "physical_mental": "好",
                    "current_goals": [],
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    idx = SqliteIndexer(empty_book)
    try:
        idx.sync()
        conn = idx.conn
        assert conn.execute("SELECT COUNT(*) FROM entities WHERE id = 'char_b'").fetchone()[0] == 1

        # Update file: remove char_b, keep char_a
        chars_file.write_text(
            yaml.dump(
                {
                    "char_a": {
                        "updated_at_ch": 11,
                        "location": "loc_a_new",
                        "physical_mental": "好",
                        "current_goals": [],
                    }
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

        report = idx.sync()
        assert "03_state/active/characters.yaml" in report.updated

        b_count = conn.execute("SELECT COUNT(*) FROM entities WHERE id = 'char_b'").fetchone()[0]
        assert b_count == 0, (
            "Ghost entity detected: 'char_b' still exists in entities "
            "after being removed from characters.yaml!"
        )
    finally:
        idx.close()


def test_direct_entity_delete_foreign_key_and_fts_cleanliness(
    empty_book: Path,
) -> None:
    """Direct deletion of an entity from `entities` must cascade to entity_aliases,
    setting_topics, and fts_docs.

    Verifies that:
    1. Foreign keys cascade to entity_aliases and setting_topics.
    2. FTS store (fts_docs) has zero orphan rows for the deleted entity.
    3. FTS5 index (fts_docs_idx) no longer returns hits for content from the deleted entity.
    4. FTS5 integrity-check passes without corruption.
    """
    sett_dir = empty_book / "01_settings" / "characters"
    sett_dir.mkdir(parents=True, exist_ok=True)
    (sett_dir / "char_temp.md").write_text(
        "---\n"
        "id: char_temp\n"
        "name: 临时角色\n"
        "aliases: [小临, 临儿]\n"
        "category: character\n"
        "status: active\n"
        "---\n"
        "## [核心]\n"
        "临时角色描述\n"
        "## [专题: 绝技]\n"
        "临时神剑\n",
        encoding="utf-8",
    )

    idx = SqliteIndexer(empty_book)
    try:
        idx.sync()
        conn = idx.conn

        # Verify initial ingestion
        assert conn.execute("SELECT COUNT(*) FROM entities WHERE id = 'char_temp'").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM entity_aliases WHERE entity_id = 'char_temp'").fetchone()[0]
            == 3
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM setting_topics WHERE entity_id = 'char_temp'").fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM fts_docs WHERE entity_id = 'char_temp'").fetchone()[0] > 0
        assert len(idx.search_fts("临时神剑")) > 0

        # Execute direct administrative/relational deletion
        conn.execute("DELETE FROM entities WHERE id = 'char_temp'")
        conn.commit()

        # Check relational cascade
        ea_count = conn.execute(
            "SELECT COUNT(*) FROM entity_aliases WHERE entity_id = 'char_temp'"
        ).fetchone()[0]
        st_count = conn.execute(
            "SELECT COUNT(*) FROM setting_topics WHERE entity_id = 'char_temp'"
        ).fetchone()[0]
        assert ea_count == 0, "entity_aliases was not cascaded on direct entity delete!"
        assert st_count == 0, "setting_topics was not cascaded on direct entity delete!"

        # Check FTS cascade and search hit
        fts_count = conn.execute(
            "SELECT COUNT(*) FROM fts_docs WHERE entity_id = 'char_temp'"
        ).fetchone()[0]
        search_hits = idx.search_fts("临时神剑")

        assert fts_count == 0, (
            f"Ghost records in fts_docs: found {fts_count} rows for deleted entity 'char_temp'!"
        )
        assert len(search_hits) == 0, (
            f"Ghost search hit in FTS5: deleted entity still returned by search_fts: {search_hits}!"
        )

        # Verify FTS5 virtual table integrity
        conn.execute("INSERT INTO fts_docs_idx(fts_docs_idx) VALUES('integrity-check')")
    finally:
        idx.close()


def test_cascade_update_character_setting_preserves_dynamic_state(
    empty_book: Path,
) -> None:
    """Updating a character setting document must NOT wipe out its dynamic CharacterState.

    Verifies that when an entity has dual ownership across 01_settings/ and 03_state/,
    incremental sync preserves CharacterState and stays 100% synchronized with full rebuild.
    """
    sett_dir = empty_book / "01_settings" / "characters"
    sett_dir.mkdir(parents=True, exist_ok=True)
    sett_file = sett_dir / "char_hero.md"
    sett_file.write_text(
        "---\n"
        "id: char_hero\n"
        "name: 主角\n"
        "category: character\n"
        "status: active\n"
        "---\n"
        "# 核心\n"
        "主角初始设定\n",
        encoding="utf-8",
    )

    state_dir = empty_book / "03_state" / "active"
    state_dir.mkdir(parents=True, exist_ok=True)
    chars_file = state_dir / "characters.yaml"
    chars_file.write_text(
        yaml.dump(
            {
                "char_hero": {
                    "updated_at_ch": 10,
                    "location": "loc_town",
                    "physical_mental": "健康",
                    "current_goals": ["突破境界"],
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    with SqliteIndexer(empty_book) as idx:
        idx.sync()
        bundle_before = idx.get_entity_bundle("char_hero")
        assert bundle_before.state is not None, "Character state must be ingested initially"
        assert bundle_before.state.location == "loc_town"

        # Author updates setting markdown doc only
        sett_file.write_text(
            "---\n"
            "id: char_hero\n"
            "name: 主角\n"
            "category: character\n"
            "status: active\n"
            "---\n"
            "# 核心\n"
            "更新后的宏大设定\n",
            encoding="utf-8",
        )
        report = idx.sync()
        assert "01_settings/characters/char_hero.md" in report.updated

        bundle_after = idx.get_entity_bundle("char_hero")
        inc_state = bundle_after.state
        assert inc_state is not None, (
            "CRITICAL REGRESSION: Character dynamic state was wiped out after setting file update!"
        )
        assert inc_state.location == "loc_town"
        assert bundle_after.setting.core_text == "更新后的宏大设定"

        # Compare with clean rebuild
        idx.rebuild()
        rebuild_bundle = idx.get_entity_bundle("char_hero")
        assert rebuild_bundle.state == inc_state, (
            f"Sync/Rebuild desync: incremental={inc_state} vs rebuild={rebuild_bundle.state}"
        )


def test_cascade_update_faction_setting_preserves_faction_state(
    empty_book: Path,
) -> None:
    """Updating a faction setting document must NOT wipe out its dynamic FactionState."""
    sett_dir = empty_book / "01_settings" / "factions"
    sett_dir.mkdir(parents=True, exist_ok=True)
    sett_file = sett_dir / "fac_sect.md"
    sett_file.write_text(
        "---\n"
        "id: fac_sect\n"
        "name: 青云门\n"
        "category: faction\n"
        "status: active\n"
        "---\n"
        "# 核心\n"
        "名门正派\n",
        encoding="utf-8",
    )

    state_dir = empty_book / "03_state" / "active"
    state_dir.mkdir(parents=True, exist_ok=True)
    facs_file = state_dir / "factions.yaml"
    facs_file.write_text(
        yaml.dump(
            {
                "fac_sect": {
                    "updated_at_ch": 15,
                    "scale": "大型宗门",
                    "stability": "稳定",
                    "current_posture": "防御",
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    with SqliteIndexer(empty_book) as idx:
        idx.sync()
        bundle_before = idx.get_entity_bundle("fac_sect")
        assert bundle_before.state is not None
        assert bundle_before.state.scale == "大型宗门"

        # Author updates faction setting doc
        sett_file.write_text(
            "---\n"
            "id: fac_sect\n"
            "name: 青云门\n"
            "category: faction\n"
            "status: active\n"
            "---\n"
            "# 核心\n"
            "修正后的名门正派描述\n",
            encoding="utf-8",
        )
        report = idx.sync()
        assert "01_settings/factions/fac_sect.md" in report.updated

        bundle_after = idx.get_entity_bundle("fac_sect")
        assert bundle_after.state is not None, (
            "CRITICAL REGRESSION: Faction dynamic state was wiped out after faction setting update!"
        )
        assert bundle_after.state.scale == "大型宗门"
        assert bundle_after.setting.core_text == "修正后的名门正派描述"

        idx.rebuild()
        rebuild_bundle = idx.get_entity_bundle("fac_sect")
        assert rebuild_bundle.state == bundle_after.state


def test_cascade_delete_setting_file_dual_source_preserves_state(
    empty_book: Path,
) -> None:
    """When an entity exists in characters.yaml and its setting markdown is deleted,
    incremental sync must gracefully degrade the entity to a pure-state entity
    without desynchronizing from full rebuild.
    """
    sett_dir = empty_book / "01_settings" / "characters"
    sett_dir.mkdir(parents=True, exist_ok=True)
    sett_file = sett_dir / "char_hero.md"
    sett_file.write_text(
        "---\n"
        "id: char_hero\n"
        "name: 主角\n"
        "category: character\n"
        "status: active\n"
        "---\n"
        "## [核心]\n"
        "主角初始设定\n"
        "## [专题: 绝技]\n"
        "专属功法秘籍\n",
        encoding="utf-8",
    )

    state_dir = empty_book / "03_state" / "active"
    state_dir.mkdir(parents=True, exist_ok=True)
    chars_file = state_dir / "characters.yaml"
    chars_file.write_text(
        yaml.dump(
            {
                "char_hero": {
                    "updated_at_ch": 10,
                    "location": "loc_town",
                    "physical_mental": "健康",
                    "current_goals": ["求道修行"],
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    with SqliteIndexer(empty_book) as idx:
        idx.sync()
        conn = idx.conn

        # Verify initial presence in both setting tables and state
        assert conn.execute("SELECT COUNT(*) FROM entities WHERE id = 'char_hero'").fetchone()[0] == 1
        st_initial = conn.execute(
            "SELECT COUNT(*) FROM setting_topics WHERE entity_id = 'char_hero'"
        ).fetchone()[0]
        assert st_initial > 0
        assert idx.get_entity_bundle("char_hero").state is not None

        # Delete setting file
        sett_file.unlink()
        report = idx.sync()
        assert "01_settings/characters/char_hero.md" in report.deleted

        # Setting sub-records must be cleanly removed
        st_deleted = conn.execute(
            "SELECT COUNT(*) FROM setting_topics WHERE entity_id = 'char_hero'"
        ).fetchone()[0]
        ea_deleted = conn.execute(
            "SELECT COUNT(*) FROM entity_aliases WHERE entity_id = 'char_hero'"
        ).fetchone()[0]
        fts_deleted = conn.execute(
            "SELECT COUNT(*) FROM fts_docs WHERE source_file = '01_settings/characters/char_hero.md'"
        ).fetchone()[0]
        assert st_deleted == 0
        assert ea_deleted == 0
        assert fts_deleted == 0

        # Entity row must be preserved as pure-state entity because characters.yaml is active
        inc_exists = conn.execute("SELECT COUNT(*) FROM entities WHERE id = 'char_hero'").fetchone()[0] == 1
        assert inc_exists, "Entity row must be retained as pure-state entity when state file exists"
        assert idx.get_entity("char_hero") is None, "SettingDoc must be None for pure-state entity"
        inc_state = idx.get_entity_bundle("char_hero").state
        assert inc_state is not None
        assert inc_state.location == "loc_town"

        # Full rebuild must produce identical entity existence and state
        idx.rebuild()
        rebuild_exists = (
            conn.execute("SELECT COUNT(*) FROM entities WHERE id = 'char_hero'").fetchone()[0] == 1
        )
        rebuild_state = idx.get_entity_bundle("char_hero").state

        assert inc_exists == rebuild_exists, (
            f"CRITICAL DESYNC: Incremental exists={inc_exists} vs Rebuild exists={rebuild_exists}"
        )
        assert inc_state == rebuild_state, (
            f"State desync between sync and rebuild: {inc_state} != {rebuild_state}"
        )


# ---------------------------------------------------------------------------
# 6. FTS5 Chinese Search & Scope Filtering Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["剑", "镜", "丹"])
def test_fts5_single_char_search(synced_indexer: SqliteIndexer, query: str) -> None:
    hits = synced_indexer.search_fts(query)
    assert len(hits) > 0
    for hit in hits:
        assert isinstance(hit, SearchHit)
        assert hit.score >= 0.0


@pytest.mark.parametrize(
    "query, expected_id",
    [
        ("林凡", "char_linfan"),
        ("苏瑶", "char_suyao"),
        ("赵乾", "char_zhaoqian"),
        ("青云门", "fac_qingyunmen"),
    ],
)
def test_fts5_two_char_and_name_search(
    synced_indexer: SqliteIndexer, query: str, expected_id: str
) -> None:
    hits = synced_indexer.search_fts(query)
    assert len(hits) > 0
    hit_ids = {h.entity_id for h in hits if h.entity_id}
    assert expected_id in hit_ids


def test_fts5_multi_char_and_compound_search(synced_indexer: SqliteIndexer) -> None:
    topic_hits = synced_indexer.search_fts("惊雷剑诀")
    assert len(topic_hits) > 0
    assert any(h.entity_id == "char_linfan" for h in topic_hits)

    loc_hits = synced_indexer.search_fts("北境孤城")
    assert len(loc_hits) > 0

    fact_hits = synced_indexer.search_fts("半式惊雷剑意")
    assert len(fact_hits) > 0
    assert any(h.chapter_num == 38 for h in fact_hits)


def test_fts5_scope_filtering(synced_indexer: SqliteIndexer) -> None:
    setting_hits = synced_indexer.search_fts("林凡", scope=FtsScope.SETTING)
    assert len(setting_hits) > 0
    assert all(h.scope == FtsScope.SETTING for h in setting_hits)

    fact_hits = synced_indexer.search_fts("林凡", scope=FtsScope.FACT)
    assert len(fact_hits) > 0
    assert all(h.scope == FtsScope.FACT for h in fact_hits)


def test_fts5_top_k_parameter(synced_indexer: SqliteIndexer) -> None:
    hits_1 = synced_indexer.search_fts("林凡", top_k=1)
    assert len(hits_1) == 1

    hits_2 = synced_indexer.search_fts("林凡", top_k=2)
    assert len(hits_2) <= 2


def test_fts5_bm25_positive_scoring_and_ordering(synced_indexer: SqliteIndexer) -> None:
    hits = synced_indexer.search_fts("林凡")
    assert len(hits) >= 2
    for i in range(len(hits) - 1):
        assert hits[i].score >= hits[i + 1].score
        assert hits[i].score >= 0.0


def test_fts5_search_hit_structure(synced_indexer: SqliteIndexer) -> None:
    hits = synced_indexer.search_fts("林凡", top_k=1)
    assert len(hits) == 1
    hit = hits[0]
    assert isinstance(hit, SearchHit)
    assert len(hit.doc_id) > 0
    assert isinstance(hit.scope, FtsScope)
    assert hit.chapter_num >= 0
    assert isinstance(hit.excerpt, str)
    assert hit.score >= 0.0


# ---------------------------------------------------------------------------
# 7. FTS5 Query Sanitization & Robustness Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        '"',
        '林凡"',
        '"""林凡"""',
        "AND",
        "OR",
        "NOT",
        "NEAR",
        "AND OR NOT",
        "林凡 AND NOT",
        "*",
        "-",
        ":",
        "()",
        "[]",
        '林凡 (AND) NOT: "苏瑶*"',
    ],
)
def test_fts5_sanitization_no_crashes(synced_indexer: SqliteIndexer, query: str) -> None:
    hits = synced_indexer.search_fts(query)
    assert isinstance(hits, list)


@pytest.mark.parametrize("empty_query", ["", "   ", "\t\n", "???", "。。。"])
def test_fts5_empty_query_returns_empty_list(
    synced_indexer: SqliteIndexer, empty_query: str
) -> None:
    hits = synced_indexer.search_fts(empty_query)
    assert hits == []


# ---------------------------------------------------------------------------
# 8. Bi-temporal Relations Tests
# ---------------------------------------------------------------------------


def test_temporal_relations_left_closed_right_open(synced_indexer: SqliteIndexer) -> None:
    rel_ch1 = synced_indexer.get_active_relations("char_linfan", at_chapter=1)
    disciple_rels = [r for r in rel_ch1 if "真传弟子" in r.relation]
    assert len(disciple_rels) == 1

    rel_ch119 = synced_indexer.get_active_relations("char_linfan", at_chapter=119)
    assert any("真传弟子" in r.relation for r in rel_ch119)

    rel_ch120 = synced_indexer.get_active_relations("char_linfan", at_chapter=120)
    assert not any("真传弟子" in r.relation for r in rel_ch120)
    assert not any("流亡弃徒" in r.relation for r in rel_ch120)

    rel_ch121 = synced_indexer.get_active_relations("char_linfan", at_chapter=121)
    assert any("流亡弃徒" in r.relation for r in rel_ch121)


def test_temporal_relations_archived_enemy(synced_indexer: SqliteIndexer) -> None:
    assert len(synced_indexer.get_active_relations("char_zhaoqian", at_chapter=37)) == 0
    assert len(synced_indexer.get_active_relations("char_zhaoqian", at_chapter=38)) > 0
    assert len(synced_indexer.get_active_relations("char_zhaoqian", at_chapter=86)) > 0
    assert len(synced_indexer.get_active_relations("char_zhaoqian", at_chapter=87)) == 0


def test_temporal_relations_filter_variations(synced_indexer: SqliteIndexer) -> None:
    single = synced_indexer.get_active_relations("char_linfan", at_chapter=141)
    assert len(single) > 0
    assert all(r.source == "char_linfan" or r.target == "char_linfan" for r in single)

    multi = synced_indexer.get_active_relations(["char_linfan", "char_suyao"], at_chapter=141)
    assert len(multi) >= len(single)

    all_active = synced_indexer.get_active_relations(at_chapter=141)
    assert len(all_active) >= len(multi)

    empty_filter = synced_indexer.get_active_relations([], at_chapter=141)
    assert empty_filter == []


def test_temporal_relations_invalid_chapter_error(synced_indexer: SqliteIndexer) -> None:
    with pytest.raises(ValueError):
        synced_indexer.get_active_relations("char_linfan", at_chapter=0)
    with pytest.raises(ValueError):
        synced_indexer.get_active_relations("char_linfan", at_chapter=-5)


# ---------------------------------------------------------------------------
# 9. EntityBundle Aggregation Tests
# ---------------------------------------------------------------------------


def test_get_entity_bundle_active_character(synced_indexer: SqliteIndexer) -> None:
    bundle = synced_indexer.get_entity_bundle("char_linfan", at_chapter=141)
    assert isinstance(bundle, EntityBundle)
    assert bundle.setting.id == "char_linfan"
    assert bundle.setting.name == "林凡"
    assert bundle.is_archived is False
    assert isinstance(bundle.state, CharacterState)
    assert bundle.state.location == "loc_beijing_gucheng"
    assert len(bundle.relations) > 0


def test_get_entity_bundle_archived_character(synced_indexer: SqliteIndexer) -> None:
    bundle = synced_indexer.get_entity_bundle("char_zhaoqian", at_chapter=87)
    assert bundle.setting.id == "char_zhaoqian"
    assert bundle.is_archived is True
    assert isinstance(bundle.state, CharacterState)


def test_get_entity_bundle_static_setting(synced_indexer: SqliteIndexer) -> None:
    bundle = synced_indexer.get_entity_bundle("world_jiuzhou")
    assert bundle.setting.id == "world_jiuzhou"
    assert bundle.state is None
    assert bundle.is_archived is False


def test_get_entity_bundle_nonexistent_raises_keyerror(synced_indexer: SqliteIndexer) -> None:
    with pytest.raises(KeyError):
        synced_indexer.get_entity_bundle("char_nonexistent_entity")


# ---------------------------------------------------------------------------
# 10. Recent Summaries Tests
# ---------------------------------------------------------------------------


def test_get_recent_summaries_chronological(synced_indexer: SqliteIndexer) -> None:
    records = synced_indexer.get_recent_summaries(before_ch=142, count=3)
    assert len(records) == 3
    assert [r.ch for r in records] == [38, 87, 141]
    assert all(isinstance(r, ChapterFactRecord) for r in records)


def test_get_recent_summaries_count_limit(synced_indexer: SqliteIndexer) -> None:
    records = synced_indexer.get_recent_summaries(before_ch=142, count=1)
    assert len(records) == 1
    assert records[0].ch == 141


def test_get_recent_summaries_before_earliest(synced_indexer: SqliteIndexer) -> None:
    records = synced_indexer.get_recent_summaries(before_ch=38, count=3)
    assert records == []


def test_get_recent_summaries_validation(synced_indexer: SqliteIndexer) -> None:
    with pytest.raises(ValueError):
        synced_indexer.get_recent_summaries(before_ch=1, count=3)
    with pytest.raises(ValueError):
        synced_indexer.get_recent_summaries(before_ch=10, count=0)


def test_get_chapter_fact(synced_indexer: SqliteIndexer) -> None:
    f38 = synced_indexer.get_chapter_fact(38)
    assert f38 is not None
    assert f38.ch == 38
    assert "半式惊雷剑意" in f38.summary

    f999 = synced_indexer.get_chapter_fact(999)
    assert f999 is None

    with pytest.raises(ValueError):
        synced_indexer.get_chapter_fact(0)


# ---------------------------------------------------------------------------
# 11. Transaction Rollback Tests
# ---------------------------------------------------------------------------


def test_transaction_atomic_rollback_on_error(
    cultivation_book: Path, synced_indexer: SqliteIndexer, monkeypatch: pytest.MonkeyPatch
) -> None:
    new_char = cultivation_book / "01_settings" / "characters" / "char_broken.md"
    new_char.write_text(
        "---\nid: char_broken\nname: 崩坏\ncategory: character\nstatus: active\n---\n## [核心]\n内容\n",
        encoding="utf-8",
    )

    def broken_parse(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Simulated parse explosion")

    monkeypatch.setattr(ProgressiveParser, "parse_setting_md", broken_parse)

    with pytest.raises(RuntimeError):
        synced_indexer.sync()

    # Verify rollback: char_broken was not recorded
    assert synced_indexer.get_entity("char_broken") is None
    cur = synced_indexer.conn.cursor()
    assert (
        cur.execute(
            "SELECT COUNT(*) FROM file_hashes WHERE rel_path='01_settings/characters/char_broken.md'"
        ).fetchone()[0]
        == 0
    )


def test_recovery_after_failed_sync(
    cultivation_book: Path, synced_indexer: SqliteIndexer, monkeypatch: pytest.MonkeyPatch
) -> None:
    new_char = cultivation_book / "01_settings" / "characters" / "char_broken2.md"
    new_char.write_text(
        "---\nid: char_broken2\nname: 崩坏2\ncategory: character\nstatus: active\n---\n## [核心]\n内容\n",
        encoding="utf-8",
    )

    call_count = 0

    def conditionally_broken_parse(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Temporary error on first try")
        return orig_parse(*args, **kwargs)

    orig_parse = ProgressiveParser.parse_setting_md
    monkeypatch.setattr(ProgressiveParser, "parse_setting_md", conditionally_broken_parse)

    with pytest.raises(RuntimeError):
        synced_indexer.sync()

    # Second sync succeeds and ingests the file cleanly
    report = synced_indexer.sync()
    assert "01_settings/characters/char_broken2.md" in report.added
    assert synced_indexer.get_entity("char_broken2") is not None


# ---------------------------------------------------------------------------
# 12. Vector Plugin Soft Integration Tests
# ---------------------------------------------------------------------------


def test_vector_plugin_disabled_zero_impact(synced_indexer: SqliteIndexer) -> None:
    hits = synced_indexer.search_fts("林凡", hybrid=True)
    assert len(hits) > 0


def test_vector_plugin_enabled_hooks(cultivation_book: Path) -> None:
    mock_plugin = MagicMock(spec=VectorPlugin)
    mock_plugin.is_enabled.return_value = True

    with SqliteIndexer(cultivation_book, vector_plugin=mock_plugin) as idx:
        idx.sync()
        assert mock_plugin.index_documents.called
        assert mock_plugin.prune_orphan_embeddings.called

        idx.search_fts("林凡", hybrid=True)
        assert mock_plugin.hybrid_rerank.called


# ---------------------------------------------------------------------------
# 13. Auxiliary Queries & AliasMatcher Bridge Tests
# ---------------------------------------------------------------------------


def test_get_entity_and_list_entities(synced_indexer: SqliteIndexer) -> None:
    assert synced_indexer.get_entity("char_linfan") is not None
    assert synced_indexer.get_entity("ghost") is None

    chars = synced_indexer.list_entities(category=SettingCategory.CHARACTER)
    assert all(c.category == SettingCategory.CHARACTER for c in chars)

    active_chars = synced_indexer.list_entities(
        category=SettingCategory.CHARACTER, include_archived=False
    )
    assert not any(c.id == "char_zhaoqian" for c in active_chars)


def test_get_threads_filtering(synced_indexer: SqliteIndexer) -> None:
    active_threads = synced_indexer.get_threads(status=ThreadStatus.ACTIVE)
    assert len(active_threads) > 0
    assert all(t.status == ThreadStatus.ACTIVE for t in active_threads)

    ch100_threads = synced_indexer.get_threads(at_chapter=100)
    assert len(ch100_threads) > 0
    assert all(t.planted_at <= 100 for t in ch100_threads)


def test_get_alias_matcher_integration(synced_indexer: SqliteIndexer) -> None:
    matcher = synced_indexer.get_alias_matcher()
    assert isinstance(matcher, AliasMatcher)
    hits = matcher.scan_text("林师兄在青云门试剑台击败强敌")
    hit_ids = {h.entity_id for h in hits}
    assert "char_linfan" in hit_ids
    assert "fac_qingyunmen" in hit_ids


# ---------------------------------------------------------------------------
# 14. Windows Resource Cleanup Test
# ---------------------------------------------------------------------------


def test_windows_no_file_lock_leak(cultivation_book: Path) -> None:
    idx = SqliteIndexer(cultivation_book)
    idx.sync()
    idx.search_fts("林凡")
    db_file = idx.db_path
    idx.close()

    # On Windows, open handles raise PermissionError on unlink
    db_file.unlink()
    assert not db_file.exists()


# ---------------------------------------------------------------------------
# 15. Reverse Cascade & Cross-File Unarchive Migration Regression Tests
# ---------------------------------------------------------------------------


def test_reverse_cascade_delete_archive_state_resets_fields(empty_book: Path) -> None:
    sett_dir = empty_book / "01_settings" / "characters"
    sett_dir.mkdir(parents=True, exist_ok=True)
    (sett_dir / "char_hero.md").write_text(
        "---\n"
        "id: char_hero\n"
        "name: 主角\n"
        "aliases: [英雄]\n"
        "category: character\n"
        "status: active\n"
        "---\n"
        "# 核心\n"
        "主角设定\n",
        encoding="utf-8",
    )

    arc_dir = empty_book / "03_state" / "archive"
    arc_dir.mkdir(parents=True, exist_ok=True)
    arc_file = arc_dir / "characters.yaml"
    arc_file.write_text(
        yaml.dump(
            {
                "char_hero": {
                    "updated_at_ch": 20,
                    "location": "loc_town",
                    "physical_mental": "归隐",
                    "current_goals": [],
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    with SqliteIndexer(empty_book) as idx:
        idx.sync()
        assert idx.get_entity_bundle("char_hero").is_archived is True

        arc_file.unlink()
        idx.sync()

        bundle = idx.get_entity_bundle("char_hero")
        assert bundle.state is None
        assert bundle.is_archived is False

        row = idx.conn.execute(
            "SELECT updated_at_ch, is_archived FROM entities WHERE id = 'char_hero'"
        ).fetchone()
        assert tuple(row) == (0, 0)

        archived_alias_count = idx.conn.execute(
            "SELECT COUNT(*) FROM entity_aliases "
            "WHERE entity_id = 'char_hero' AND is_archived = 1"
        ).fetchone()[0]
        assert archived_alias_count == 0

        idx.rebuild()
        assert idx.get_entity_bundle("char_hero") == bundle


def test_cross_file_unarchive_migration_single_batch_sync(empty_book: Path) -> None:
    sett_dir = empty_book / "01_settings" / "characters"
    sett_dir.mkdir(parents=True, exist_ok=True)
    (sett_dir / "char_revive.md").write_text(
        "---\n"
        "id: char_revive\n"
        "name: 复出者\n"
        "category: character\n"
        "status: active\n"
        "---\n"
        "# 核心\n"
        "复出角色设定\n",
        encoding="utf-8",
    )
    (sett_dir / "char_dead.md").write_text(
        "---\n"
        "id: char_dead\n"
        "name: 陨落者\n"
        "category: character\n"
        "status: deceased\n"
        "---\n"
        "# 核心\n"
        "陨落角色设定\n",
        encoding="utf-8",
    )

    arc_dir = empty_book / "03_state" / "archive"
    act_dir = empty_book / "03_state" / "active"
    arc_dir.mkdir(parents=True, exist_ok=True)
    act_dir.mkdir(parents=True, exist_ok=True)
    arc_file = arc_dir / "characters.yaml"
    act_file = act_dir / "characters.yaml"

    arc_file.write_text(
        yaml.dump(
            {
                "char_revive": {
                    "updated_at_ch": 10,
                    "location": "loc_tomb",
                    "physical_mental": "沉寂",
                    "current_goals": [],
                },
                "char_dead": {
                    "updated_at_ch": 10,
                    "location": "loc_tomb",
                    "physical_mental": "沉寂",
                    "current_goals": [],
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    act_file.write_text(yaml.dump({}, allow_unicode=True), encoding="utf-8")

    with SqliteIndexer(empty_book) as idx:
        idx.sync()
        assert idx.get_entity_bundle("char_revive").is_archived is True
        assert idx.get_entity_bundle("char_dead").is_archived is True

        arc_file.write_text(yaml.dump({}, allow_unicode=True), encoding="utf-8")
        act_file.write_text(
            yaml.dump(
                {
                    "char_revive": {
                        "updated_at_ch": 25,
                        "location": "loc_sect",
                        "physical_mental": "苏醒",
                        "current_goals": [],
                    },
                    "char_dead": {
                        "updated_at_ch": 25,
                        "location": "loc_sect",
                        "physical_mental": "苏醒",
                        "current_goals": [],
                    },
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

        idx.sync()

        revive_bundle = idx.get_entity_bundle("char_revive")
        assert revive_bundle.is_archived is False
        assert revive_bundle.state is not None
        assert revive_bundle.state.updated_at_ch == 25

        dead_bundle = idx.get_entity_bundle("char_dead")
        assert dead_bundle.is_archived is True
        assert dead_bundle.setting.status == EntityStatus.DECEASED
        assert dead_bundle.state is not None
        assert dead_bundle.state.updated_at_ch == 25

        idx.rebuild()
        assert idx.get_entity_bundle("char_revive") == revive_bundle
        assert idx.get_entity_bundle("char_dead") == dead_bundle
