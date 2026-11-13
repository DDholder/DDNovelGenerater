"""T02.9-A 三题材样例书库 (`tests/fixtures/books/*`) 全量真源往返一致性与跨文件引用校验。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from ddnovel.core.schemas import (
    ID_PREFIX_CATEGORY,
    ArcDoc,
    BlueprintDoc,
    ChapterFactRecord,
    CharacterStateFile,
    FactionStateFile,
    NovelConfig,
    RelationsFile,
    SettingCategory,
    SettingDoc,
    SettingFrontmatter,
    StyleGuide,
    ThreadsFile,
    VolumeDoc,
    WorldState,
)

FIXTURES_BOOKS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "books"


def _split_frontmatter(raw_md: str) -> tuple[dict[str, Any], str]:
    """切分 Markdown 顶部的 YAML Frontmatter 与正文（测试内轻量辅助）。"""
    text = raw_md.replace("\r\n", "\n")
    if text.startswith("---\n"):
        end_idx = text.find("\n---\n", 4)
        if end_idx != -1:
            fm_raw = text[4:end_idx]
            body = text[end_idx + 5 :].strip()
            parsed = yaml.safe_load(fm_raw)
            return (parsed if isinstance(parsed, dict) else {}), body
        if text.endswith("\n---"):
            fm_raw = text[4:-4]
            parsed = yaml.safe_load(fm_raw)
            return (parsed if isinstance(parsed, dict) else {}), ""
    return {}, text.strip()


def _infer_category_from_id(entity_id: str) -> SettingCategory:
    for prefix, category in ID_PREFIX_CATEGORY.items():
        if entity_id.startswith(prefix):
            return category
    return SettingCategory.OTHER


def _yaml_roundtrip(model_cls: Any, instance: Any, *, context: dict[str, Any] | None = None) -> Any:
    dumped = instance.model_dump(mode="json", by_alias=True, exclude_none=True)
    yaml_text = yaml.safe_dump(dumped, allow_unicode=True, sort_keys=False)
    return model_cls.model_validate(yaml.safe_load(yaml_text), context=context)


@pytest.mark.parametrize("genre_dir", ["general", "cultivation_rpg", "mystery_intrigue"])
def test_fixture_book_full_roundtrip_and_cross_references(genre_dir: str, tmp_path: Path):
    book_dir = FIXTURES_BOOKS_DIR / genre_dir
    assert book_dir.is_dir()

    # 1. 全局配置 novel.yaml 往返
    cfg = NovelConfig.load(book_dir)
    assert cfg.genre_profile == genre_dir
    dumped_cfg_path = cfg.dump(tmp_path / genre_dir)
    assert dumped_cfg_path.is_file()
    reloaded_cfg = NovelConfig.load(tmp_path / genre_dir)
    assert reloaded_cfg == cfg

    ctx = {"genre_profile": cfg.genre_profile}

    # 2. 设定维 01_settings/**/*.md 往返与实体清单收集
    setting_files = sorted((book_dir / "01_settings").rglob("*.md"))
    assert len(setting_files) >= 8

    setting_ids: set[str] = set()
    for path in setting_files:
        fm_dict, body = _split_frontmatter(path.read_text(encoding="utf-8"))
        fm = SettingFrontmatter.model_validate(fm_dict)
        fm_reloaded = _yaml_roundtrip(SettingFrontmatter, fm)
        assert fm_reloaded == fm

        entity_id = fm.id or path.stem
        category = fm.category or _infer_category_from_id(entity_id)
        name = fm.name or path.stem

        doc = SettingDoc(
            id=entity_id,
            name=name,
            category=category,
            aliases=fm.aliases,
            status=fm.status,
            tags=fm.tags,
            file_path=path.relative_to(book_dir).as_posix(),
            core_text=body,
        )
        assert _yaml_roundtrip(SettingDoc, doc) == doc
        assert SettingDoc.model_validate_json(doc.model_dump_json()) == doc
        setting_ids.add(doc.id)

    # 3. 状态维 03_state/ 全量往返与引用收集
    state_dir = book_dir / "03_state"

    world_raw = yaml.safe_load((state_dir / "world_state.yaml").read_text(encoding="utf-8"))
    world = WorldState.model_validate(world_raw)
    assert _yaml_roundtrip(WorldState, world) == world
    assert WorldState.model_validate_json(world.model_dump_json()) == world

    for sub in ("active", "archive"):
        char_path = state_dir / sub / "characters.yaml"
        chars = CharacterStateFile.model_validate(
            yaml.safe_load(char_path.read_text(encoding="utf-8")), context=ctx
        )
        assert _yaml_roundtrip(CharacterStateFile, chars, context=ctx) == chars
        assert (
            CharacterStateFile.model_validate_json(chars.model_dump_json(), context=ctx) == chars
        )
        for char_id, char_state in chars.root.items():
            assert char_id in setting_ids
            if char_state.location is not None:
                assert char_state.location in setting_ids

        fac_path = state_dir / sub / "factions.yaml"
        if fac_path.is_file():
            facs = FactionStateFile.model_validate(
                yaml.safe_load(fac_path.read_text(encoding="utf-8")), context=ctx
            )
            assert _yaml_roundtrip(FactionStateFile, facs, context=ctx) == facs
            assert (
                FactionStateFile.model_validate_json(facs.model_dump_json(), context=ctx) == facs
            )
            for fac_id, fac_state in facs.root.items():
                assert fac_id in setting_ids
                if fac_state.leader is not None:
                    assert fac_state.leader in setting_ids
                assert set(fac_state.territory) <= setting_ids

    for rel_path in (state_dir / "relations.yaml", state_dir / "archive" / "relations.yaml"):
        rels = RelationsFile.model_validate(yaml.safe_load(rel_path.read_text(encoding="utf-8")))
        assert _yaml_roundtrip(RelationsFile, rels) == rels
        assert RelationsFile.model_validate_json(rels.model_dump_json()) == rels
        for edge in rels.relations:
            assert edge.source in setting_ids
            assert edge.target in setting_ids

    active_threads = ThreadsFile.model_validate(
        yaml.safe_load((state_dir / "threads.yaml").read_text(encoding="utf-8"))
    )
    assert _yaml_roundtrip(ThreadsFile, active_threads) == active_threads
    assert ThreadsFile.model_validate_json(active_threads.model_dump_json()) == active_threads
    active_thread_ids = {t.id for t in active_threads.threads}
    for th in active_threads.threads:
        assert set(th.trigger_entities) <= setting_ids

    archived_threads = ThreadsFile.model_validate(
        yaml.safe_load((state_dir / "archive" / "threads.yaml").read_text(encoding="utf-8"))
    )
    assert _yaml_roundtrip(ThreadsFile, archived_threads) == archived_threads
    assert (
        ThreadsFile.model_validate_json(archived_threads.model_dump_json()) == archived_threads
    )
    for th in archived_threads.threads:
        assert set(th.trigger_entities) <= setting_ids

    history_files = sorted((state_dir / "history").glob("*.jsonl"))
    assert len(history_files) >= 1
    for hist_path in history_files:
        lines = [ln for ln in hist_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) >= 1
        for line in lines:
            raw_obj = json.loads(line)
            rec = ChapterFactRecord.model_validate(raw_obj)
            dumped_obj = rec.model_dump(mode="json", by_alias=True, exclude_none=True)
            assert dumped_obj == raw_obj
            assert ChapterFactRecord.model_validate(dumped_obj) == rec
            assert set(rec.entities) <= setting_ids

    # 4. 结构维 02_structure/ 往返与引用自洽性
    structure_dir = book_dir / "02_structure"
    vol_files = sorted((structure_dir / "volumes").glob("vol_*.md"))
    assert len(vol_files) >= 1
    for vol_path in vol_files:
        fm_dict, body = _split_frontmatter(vol_path.read_text(encoding="utf-8"))
        vol = VolumeDoc.model_validate({**fm_dict, "body": body})
        assert _yaml_roundtrip(VolumeDoc, vol) == vol
        assert VolumeDoc.model_validate_json(vol.model_dump_json()) == vol

    arc_files = sorted((structure_dir / "arcs").glob("vol_*_arc_*.md"))
    assert len(arc_files) >= 1
    for arc_path in arc_files:
        fm_dict, body = _split_frontmatter(arc_path.read_text(encoding="utf-8"))
        arc = ArcDoc.model_validate({**fm_dict, "body": body})
        assert arc.volume_id is not None and arc.id.startswith(f"{arc.volume_id}_arc_")
        assert _yaml_roundtrip(ArcDoc, arc) == arc
        assert ArcDoc.model_validate_json(arc.model_dump_json()) == arc

    bp_files = sorted((structure_dir / "blueprints").rglob("ch_*.md"))
    assert len(bp_files) >= 1
    for bp_path in bp_files:
        fm_dict, body = _split_frontmatter(bp_path.read_text(encoding="utf-8"))
        bp = BlueprintDoc.model_validate({**fm_dict, "body": body})
        assert _yaml_roundtrip(BlueprintDoc, bp) == bp
        assert BlueprintDoc.model_validate_json(bp.model_dump_json()) == bp
        assert set(bp.all_entity_ids()) <= setting_ids
        assert {ref.thread_id for ref in bp.threads} <= active_thread_ids

    # 5. 创作维 04_creation/style_guide.md 往返
    sg_path = book_dir / "04_creation" / "style_guide.md"
    sg_raw = sg_path.read_text(encoding="utf-8")
    sg_fm, _ = _split_frontmatter(sg_raw)
    sg = StyleGuide.model_validate({**sg_fm, "raw_markdown": sg_raw})
    assert sg.pov_rules
    assert sg.banned_words
    assert _yaml_roundtrip(StyleGuide, sg) == sg
    assert StyleGuide.model_validate_json(sg.model_dump_json()) == sg
