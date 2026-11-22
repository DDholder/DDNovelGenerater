"""只读 Web UI 数据快照采集器（零索引写入、严格隔离 00_作者设定/）。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ddnovel.core.parser import ProgressiveParser
from ddnovel.core.paths import (
    is_legacy_book,
    list_facts_files,
    resolve_active_characters_file,
    resolve_active_factions_file,
    resolve_archive_characters_file,
    resolve_archive_factions_file,
    resolve_archive_relations_file,
    resolve_archive_threads_file,
    resolve_arcs_dir,
    resolve_blueprints_dir,
    resolve_config_path,
    resolve_manuscript_dir,
    resolve_relations_file,
    resolve_settings_root,
    resolve_state_root,
    resolve_structure_root,
    resolve_threads_file,
    resolve_volumes_dir,
    resolve_world_state_file,
)
from ddnovel.core.radar import ForeshadowRadar
from ddnovel.core.schemas import (
    ID_PREFIX_CATEGORY,
    ChapterFactRecord,
    CharacterState,
    CharacterStateFile,
    FactionState,
    FactionStateFile,
    NovelConfig,
    RelationsFile,
    SettingCategory,
    TemporalRelation,
    Thread,
    ThreadsFile,
    ThreadStatus,
    WorldState,
)

_ENTITY_ID_RE = re.compile(r"^[a-z][a-z0-9]*_[a-z0-9_]+$")


def _rel_posix(path: Path, root: Path) -> str:
    """将路径规范化为相对书库根目录的 POSIX 字符串。"""
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _infer_category_from_id(entity_id: str) -> str:
    """根据实体 ID 前缀推断默认分类。"""
    for prefix, cat in ID_PREFIX_CATEGORY.items():
        if entity_id.startswith(prefix):
            return cat.value
    return SettingCategory.OTHER.value


def _make_entity_ref(entity_id: str, entities: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """将实体 ID 解析为带名称、分类与建卡状态的前端引用对象。"""
    entry = entities.get(entity_id)
    if entry is not None:
        return {
            "id": entity_id,
            "name": entry["name"],
            "category": entry["category"],
            "has_setting": True,
        }
    return {
        "id": entity_id,
        "name": entity_id,
        "category": _infer_category_from_id(entity_id),
        "has_setting": False,
    }


def _load_yaml_dict(path: Path, root: Path, warnings: list[str]) -> dict[str, Any] | None:
    """安全加载 YAML 顶层字典；文件不存在返回 None，解析异常写入告警。"""
    if not path.is_file():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, yaml.YAMLError) as exc:
        warnings.append(f"YAML 读取失败 ({_rel_posix(path, root)}): {exc}")
        return None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        warnings.append(f"YAML 顶层结构非字典 ({_rel_posix(path, root)}): 实际为 {type(raw).__name__}")
        return None
    return raw


def _load_character_states(
    path: Path,
    *,
    root: Path,
    is_archived: bool,
    warnings: list[str],
) -> dict[str, tuple[CharacterState, bool]]:
    """加载活跃或归档人物状态文件，整表校验失败时降级为逐键容错解析。"""
    raw = _load_yaml_dict(path, root, warnings)
    if not raw:
        return {}
    rel = _rel_posix(path, root)
    try:
        parsed = CharacterStateFile.model_validate(raw)
        return {eid: (state, is_archived) for eid, state in parsed.root.items()}
    except ValidationError as exc:
        warnings.append(f"人物状态表整表校验失败，已切换逐条容错解析 ({rel}): {exc.errors()[0]['msg']}")

    result: dict[str, tuple[CharacterState, bool]] = {}
    for eid, item in raw.items():
        if not isinstance(eid, str) or not _ENTITY_ID_RE.fullmatch(eid.strip()):
            warnings.append(f"跳过非法角色 ID '{eid}' ({rel})")
            continue
        try:
            state = CharacterState.model_validate(item)
            result[eid.strip()] = (state, is_archived)
        except ValidationError as item_exc:
            warnings.append(f"角色状态 '{eid}' 校验失败 ({rel}): {item_exc.errors()[0]['msg']}")
    return result


def _load_faction_states(
    path: Path,
    *,
    root: Path,
    is_archived: bool,
    warnings: list[str],
) -> dict[str, tuple[FactionState, bool]]:
    """加载活跃或归档势力状态文件，整表校验失败时降级为逐键容错解析。"""
    raw = _load_yaml_dict(path, root, warnings)
    if not raw:
        return {}
    rel = _rel_posix(path, root)
    try:
        parsed = FactionStateFile.model_validate(raw)
        return {eid: (state, is_archived) for eid, state in parsed.root.items()}
    except ValidationError as exc:
        warnings.append(f"势力状态表整表校验失败，已切换逐条容错解析 ({rel}): {exc.errors()[0]['msg']}")

    result: dict[str, tuple[FactionState, bool]] = {}
    for eid, item in raw.items():
        if not isinstance(eid, str) or not _ENTITY_ID_RE.fullmatch(eid.strip()):
            warnings.append(f"跳过非法势力 ID '{eid}' ({rel})")
            continue
        try:
            state = FactionState.model_validate(item)
            result[eid.strip()] = (state, is_archived)
        except ValidationError as item_exc:
            warnings.append(f"势力状态 '{eid}' 校验失败 ({rel}): {item_exc.errors()[0]['msg']}")
    return result


def _load_relations(
    path: Path,
    *,
    root: Path,
    is_archived_file: bool,
    warnings: list[str],
) -> list[tuple[TemporalRelation, bool]]:
    """加载关系网文件，整表校验失败时降级为逐条容错解析。"""
    raw = _load_yaml_dict(path, root, warnings)
    if not raw:
        return []
    rel = _rel_posix(path, root)
    try:
        parsed = RelationsFile.model_validate(raw)
        return [(item, is_archived_file) for item in parsed.relations]
    except ValidationError as exc:
        warnings.append(f"关系网整表校验失败，已切换逐条容错解析 ({rel}): {exc.errors()[0]['msg']}")

    raw_items = raw.get("relations")
    if not isinstance(raw_items, list):
        return []
    result: list[tuple[TemporalRelation, bool]] = []
    for idx, item in enumerate(raw_items):
        try:
            result.append((TemporalRelation.model_validate(item), is_archived_file))
        except ValidationError as item_exc:
            warnings.append(f"关系第 {idx + 1} 条校验失败 ({rel}): {item_exc.errors()[0]['msg']}")
    return result


def _load_threads(
    path: Path,
    *,
    root: Path,
    is_archived: bool,
    warnings: list[str],
) -> list[tuple[Thread, bool]]:
    """加载伏笔台账文件，整表校验失败时降级为逐条容错解析。"""
    raw = _load_yaml_dict(path, root, warnings)
    if not raw:
        return []
    rel = _rel_posix(path, root)
    try:
        parsed = ThreadsFile.model_validate(raw)
        return [(item, is_archived) for item in parsed.threads]
    except ValidationError as exc:
        warnings.append(f"伏笔台账整表校验失败，已切换逐条容错解析 ({rel}): {exc.errors()[0]['msg']}")

    raw_items = raw.get("threads")
    if not isinstance(raw_items, list):
        return []
    result: list[tuple[Thread, bool]] = []
    for idx, item in enumerate(raw_items):
        try:
            result.append((Thread.model_validate(item), is_archived))
        except ValidationError as item_exc:
            warnings.append(f"伏笔第 {idx + 1} 条校验失败 ({rel}): {item_exc.errors()[0]['msg']}")
    return result


def _collect_planned_entities(
    root: Path,
    parser: ProgressiveParser,
    target_chapter: int,
    warnings: list[str],
) -> set[str]:
    """在不触发索引写入的前提下从目标章蓝图中提取计划出场实体集合。"""
    bp_dir = resolve_blueprints_dir(root)
    if not bp_dir.is_dir():
        return set()
    for md_path in sorted(bp_dir.rglob("*.md")):
        if not md_path.is_file() or md_path.name.lower() == "readme.md":
            continue
        try:
            bp = parser.parse_blueprint_md(md_path, book_dir=root)
        except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
            warnings.append(f"章节蓝图解析跳过 ({_rel_posix(md_path, root)}): {exc}")
            continue
        if bp.chapter == target_chapter:
            return set(bp.all_entity_ids())
    return set()


def _count_md_files(directory: Path) -> int:
    """统计指定目录及其子目录下的 Markdown 文档数（跳过 README.md）。"""
    if not directory.is_dir():
        return 0
    return sum(
        1 for p in directory.rglob("*.md") if p.is_file() and p.name.lower() != "readme.md"
    )


def build_dashboard_snapshot(book_dir: Path | str) -> dict[str, Any]:
    """只读采集目标 DDNovel 书库的完整状态、悬线、关系网与历史摘要快照。"""
    root = Path(book_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"书库目录不存在: {root}")

    has_book_marker = (
        resolve_config_path(root).is_file()
        or resolve_settings_root(root).is_dir()
        or resolve_state_root(root).is_dir()
        or resolve_structure_root(root).is_dir()
    )
    if not has_book_marker:
        raise FileNotFoundError(f"目录不是有效的 DDNovel 书库: {root}")

    warnings: list[str] = []

    # 1. 加载全局配置与世界状态
    cfg_path = resolve_config_path(root)
    if cfg_path.is_file():
        try:
            config = NovelConfig.load(root)
        except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
            warnings.append(f"书库配置解析失败，已使用默认值 ({_rel_posix(cfg_path, root)}): {exc}")
            config = NovelConfig(title=root.name or "未命名小说")
    else:
        config = NovelConfig(title=root.name or "未命名小说")

    ws_path = resolve_world_state_file(root)
    ws_raw = _load_yaml_dict(ws_path, root, warnings)
    if ws_raw:
        try:
            ws = WorldState.model_validate(ws_raw)
        except ValidationError as exc:
            warnings.append(
                f"世界状态校验失败，已回退默认 ({_rel_posix(ws_path, root)}): {exc.errors()[0]['msg']}"
            )
            ws = WorldState()
    else:
        ws = WorldState()

    effective_chapter = max(config.current_chapter, ws.current_chapter)
    target_chapter = max(1, effective_chapter + 1)

    # 2. 扫描静态设定卡（仅扫描 01_设定集/，绝不触碰 00_作者设定/）
    parser = ProgressiveParser(book_dir=root)
    entities: dict[str, dict[str, Any]] = {}
    settings_root = resolve_settings_root(root)
    if settings_root.is_dir():
        for md_path in sorted(settings_root.rglob("*.md")):
            if not md_path.is_file() or md_path.name.lower() == "readme.md":
                continue
            try:
                doc = parser.parse_setting_md(md_path, book_dir=root)
            except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
                warnings.append(f"设定卡解析跳过 ({_rel_posix(md_path, root)}): {exc}")
                continue
            entities[doc.id] = {
                "id": doc.id,
                "name": doc.name,
                "category": doc.category.value,
                "status": doc.status.value,
                "aliases": list(doc.aliases),
                "tags": list(doc.tags),
                "file_path": doc.file_path,
                "core_text": doc.core_text,
                "topics": dict(doc.topics),
                "secret_text": doc.secret_text,
                "extra_fields": dict(doc.model_extra or {}),
            }

    # 3. 合并人物状态（当前 + 归档 + 仅静态设定卡）
    char_state_map = _load_character_states(
        resolve_active_characters_file(root), root=root, is_archived=False, warnings=warnings
    )
    archived_char_map = _load_character_states(
        resolve_archive_characters_file(root), root=root, is_archived=True, warnings=warnings
    )
    for eid, item in archived_char_map.items():
        if eid not in char_state_map:
            char_state_map[eid] = item

    characters: list[dict[str, Any]] = []
    seen_chars: set[str] = set()
    for eid, (st, is_arch) in char_state_map.items():
        seen_chars.add(eid)
        ent = entities.get(eid)
        loc_ref = _make_entity_ref(st.location, entities) if st.location else None
        characters.append(
            {
                "id": eid,
                "name": ent["name"] if ent else eid,
                "aliases": ent["aliases"] if ent else [],
                "tags": ent["tags"] if ent else [],
                "setting_status": ent["status"] if ent else ("retired" if is_arch else "active"),
                "is_archived": is_arch,
                "has_state": True,
                "updated_at_ch": st.updated_at_ch,
                "location": st.location,
                "location_ref": loc_ref,
                "physical_mental": st.physical_mental,
                "current_goals": list(st.current_goals),
                "epistemic_bounds": {
                    "knows": list(st.epistemic_bounds.knows),
                    "unaware_of": list(st.epistemic_bounds.unaware_of),
                    "misbelieves": list(st.epistemic_bounds.misbelieves),
                },
                "profile_ext": dict(st.profile_ext),
                "extra_fields": dict(st.model_extra or {}),
                "core_text": ent["core_text"] if ent else "",
                "topics": ent["topics"] if ent else {},
                "secret_text": ent["secret_text"] if ent else None,
                "file_path": ent["file_path"] if ent else None,
            }
        )

    for eid, ent in entities.items():
        if ent["category"] == SettingCategory.CHARACTER.value and eid not in seen_chars:
            characters.append(
                {
                    "id": eid,
                    "name": ent["name"],
                    "aliases": ent["aliases"],
                    "tags": ent["tags"],
                    "setting_status": ent["status"],
                    "is_archived": ent["status"] in {"deceased", "retired"},
                    "has_state": False,
                    "updated_at_ch": None,
                    "location": None,
                    "location_ref": None,
                    "physical_mental": "",
                    "current_goals": [],
                    "epistemic_bounds": {"knows": [], "unaware_of": [], "misbelieves": []},
                    "profile_ext": {},
                    "extra_fields": {},
                    "core_text": ent["core_text"],
                    "topics": ent["topics"],
                    "secret_text": ent["secret_text"],
                    "file_path": ent["file_path"],
                }
            )
    characters.sort(key=lambda c: (c["is_archived"], not c["has_state"], c["id"]))

    # 4. 合并势力状态（当前 + 归档 + 仅静态设定卡）
    fac_state_map = _load_faction_states(
        resolve_active_factions_file(root), root=root, is_archived=False, warnings=warnings
    )
    archived_fac_map = _load_faction_states(
        resolve_archive_factions_file(root), root=root, is_archived=True, warnings=warnings
    )
    for eid, item in archived_fac_map.items():
        if eid not in fac_state_map:
            fac_state_map[eid] = item

    factions: list[dict[str, Any]] = []
    seen_factions: set[str] = set()
    for eid, (st, is_arch) in fac_state_map.items():
        seen_factions.add(eid)
        ent = entities.get(eid)
        factions.append(
            {
                "id": eid,
                "name": ent["name"] if ent else eid,
                "aliases": ent["aliases"] if ent else [],
                "tags": ent["tags"] if ent else [],
                "status": st.status.value,
                "is_archived": is_arch,
                "has_state": True,
                "updated_at_ch": st.updated_at_ch,
                "leader": st.leader,
                "leader_ref": _make_entity_ref(st.leader, entities) if st.leader else None,
                "territory": list(st.territory),
                "territory_refs": [_make_entity_ref(t, entities) for t in st.territory],
                "current_goals": list(st.current_goals),
                "profile_ext": dict(st.profile_ext),
                "extra_fields": dict(st.model_extra or {}),
                "core_text": ent["core_text"] if ent else "",
                "topics": ent["topics"] if ent else {},
                "secret_text": ent["secret_text"] if ent else None,
                "file_path": ent["file_path"] if ent else None,
            }
        )

    for eid, ent in entities.items():
        if ent["category"] == SettingCategory.FACTION.value and eid not in seen_factions:
            factions.append(
                {
                    "id": eid,
                    "name": ent["name"],
                    "aliases": ent["aliases"],
                    "tags": ent["tags"],
                    "status": ent["status"],
                    "is_archived": ent["status"] in {"deceased", "retired"},
                    "has_state": False,
                    "updated_at_ch": None,
                    "leader": None,
                    "leader_ref": None,
                    "territory": [],
                    "territory_refs": [],
                    "current_goals": [],
                    "profile_ext": {},
                    "extra_fields": {},
                    "core_text": ent["core_text"],
                    "topics": ent["topics"],
                    "secret_text": ent["secret_text"],
                    "file_path": ent["file_path"],
                }
            )
    factions.sort(key=lambda f: (f["is_archived"], not f["has_state"], f["id"]))

    # 5. 加载双时态关系网
    raw_rels = [
        *_load_relations(
            resolve_relations_file(root), root=root, is_archived_file=False, warnings=warnings
        ),
        *_load_relations(
            resolve_archive_relations_file(root), root=root, is_archived_file=True, warnings=warnings
        ),
    ]
    check_ch = max(1, effective_chapter)
    relations: list[dict[str, Any]] = []
    for idx, (rel, is_arch_file) in enumerate(raw_rels, start=1):
        is_valid_now = (not is_arch_file) and rel.is_valid_at(check_ch)
        relations.append(
            {
                "id": f"rel_{idx}",
                "source": rel.source,
                "source_ref": _make_entity_ref(rel.source, entities),
                "target": rel.target,
                "target_ref": _make_entity_ref(rel.target, entities),
                "relation": rel.relation,
                "valid_from_ch": rel.valid_from_ch,
                "invalid_at_ch": rel.invalid_at_ch,
                "is_archived_file": is_arch_file,
                "is_currently_valid": is_valid_now,
                "extra_fields": dict(rel.model_extra or {}),
            }
        )

    # 6. 加载伏笔悬线与计算下一章雷达预警
    planned_set = _collect_planned_entities(root, parser, target_chapter, warnings)
    stale_threshold = config.lifecycle.stale_thread_chapters
    raw_threads = [
        *_load_threads(resolve_threads_file(root), root=root, is_archived=False, warnings=warnings),
        *_load_threads(
            resolve_archive_threads_file(root), root=root, is_archived=True, warnings=warnings
        ),
    ]
    threads: list[dict[str, Any]] = []
    for thread, is_arch in raw_threads:
        radar_reasons: list[str] = []
        radar_detail = ""
        if (
            not is_arch
            and thread.status != ThreadStatus.RESOLVED
            and thread.planted_at <= target_chapter
        ):
            hit = ForeshadowRadar._evaluate_thread(
                thread,
                target_chapter=target_chapter,
                planned_set=planned_set,
                stale_threshold=stale_threshold,
            )
            if hit is not None:
                radar_reasons = [r.value for r in hit.reasons]
                radar_detail = hit.detail

        exp_win = (
            [thread.expected_window.start, thread.expected_window.end]
            if thread.expected_window is not None
            else None
        )
        threads.append(
            {
                "id": thread.id,
                "title": thread.title,
                "category": thread.category,
                "status": thread.status.value,
                "is_archived": is_arch,
                "planted_at": thread.planted_at,
                "last_progressed_at": thread.last_progressed_at,
                "gap_chapters": max(0, effective_chapter - thread.last_progressed_at),
                "expected_window": exp_win,
                "trigger_entities": list(thread.trigger_entities),
                "trigger_entity_refs": [
                    _make_entity_ref(eid, entities) for eid in thread.trigger_entities
                ],
                "public_clue": thread.public_clue,
                "truth_secret": thread.truth_secret,
                "extra_fields": dict(thread.model_extra or {}),
                "radar_reasons": radar_reasons,
                "radar_detail": radar_detail,
            }
        )

    # 7. 加载章节历史摘要与原子事实
    history: list[dict[str, Any]] = []
    for facts_path in list_facts_files(root):
        rel_facts = _rel_posix(facts_path, root)
        try:
            lines = facts_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            warnings.append(f"历史事实文件读取失败 ({rel_facts}): {exc}")
            continue
        for line_no, raw_line in enumerate(lines, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
                rec = ChapterFactRecord.model_validate(payload)
            except (json.JSONDecodeError, ValidationError) as exc:
                warnings.append(f"历史事实第 {line_no} 行解析跳过 ({rel_facts}): {exc}")
                continue
            history.append(
                {
                    "ch": rec.ch,
                    "volume_id": rec.volume_id,
                    "arc_id": rec.arc_id,
                    "in_world_time": rec.in_world_time,
                    "summary": rec.summary,
                    "facts": list(rec.facts),
                    "entities": list(rec.entities),
                    "entity_refs": [_make_entity_ref(eid, entities) for eid in rec.entities],
                    "source_file": rel_facts,
                }
            )
    history.sort(key=lambda item: -item["ch"])

    # 8. 汇总统计信息
    active_characters_count = sum(1 for c in characters if not c["is_archived"] and c["has_state"])
    active_factions_count = sum(1 for f in factions if not f["is_archived"] and f["has_state"])
    valid_relations_count = sum(1 for r in relations if r["is_currently_valid"])
    open_threads_count = sum(
        1 for t in threads if not t["is_archived"] and t["status"] != ThreadStatus.RESOLVED.value
    )
    radar_alert_count = sum(1 for t in threads if len(t["radar_reasons"]) > 0)

    stats = {
        "effective_chapter": effective_chapter,
        "next_chapter": target_chapter,
        "entities_total": len(entities),
        "characters_total": len(characters),
        "characters_active": active_characters_count,
        "factions_total": len(factions),
        "factions_active": active_factions_count,
        "relations_total": len(relations),
        "relations_valid": valid_relations_count,
        "threads_total": len(threads),
        "threads_open": open_threads_count,
        "radar_alerts": radar_alert_count,
        "history_chapters": len(history),
        "volumes_count": _count_md_files(resolve_volumes_dir(root)),
        "arcs_count": _count_md_files(resolve_arcs_dir(root)),
        "blueprints_count": _count_md_files(resolve_blueprints_dir(root)),
        "manuscripts_count": _count_md_files(resolve_manuscript_dir(root)),
    }

    return {
        "book": {
            "title": config.title,
            "author": config.author,
            "genre_profile": config.genre_profile,
            "current_chapter": config.current_chapter,
            "effective_chapter": effective_chapter,
            "next_chapter": target_chapter,
            "chapter_id_width": config.chapter_id_width,
            "stale_thread_chapters": stale_threshold,
            "legacy_layout": is_legacy_book(root),
            "book_dir": root.as_posix(),
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
        "world_state": {
            "current_chapter": ws.current_chapter,
            "in_world_time": ws.in_world_time,
            "macro_situation": ws.macro_situation,
            "extra_fields": dict(ws.model_extra or {}),
        },
        "stats": stats,
        "entities": entities,
        "characters": characters,
        "factions": factions,
        "relations": relations,
        "threads": threads,
        "history": history,
        "warnings": warnings,
    }


__all__ = ["build_dashboard_snapshot"]
