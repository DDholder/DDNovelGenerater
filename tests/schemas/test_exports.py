"""T02.8 契约统一出口 (`schemas/__init__.py`) 与 `export_json_schemas()` 单元测试。"""

from __future__ import annotations

import json

from pydantic import BaseModel

import ddnovel.core.schemas as schemas
from ddnovel.core.schemas import (
    SCHEMA_VERSION,
    AuditReport,
    BlueprintDoc,
    ChapterFactRecord,
    CharacterState,
    NovelConfig,
    PacketBuildResult,
    SettingDoc,
    TemporalRelation,
    Thread,
    _base,
    config,
    export_json_schemas,
    genres,
    models,
    results,
)


def test_schema_version():
    assert type(SCHEMA_VERSION) is int
    assert SCHEMA_VERSION == 1


def test_reexports_complete_and_no_collisions():
    submodules = (_base, config, genres, models, results)
    symbol_sources: dict[str, object] = {}

    for mod in submodules:
        for name in mod.__all__:
            assert name not in symbol_sources, f"子模块之间存在重名导出符号: {name}"
            symbol_sources[name] = getattr(mod, name)

    expected_all = set(symbol_sources) | {"SCHEMA_VERSION", "export_json_schemas"}
    assert len(expected_all) == 85
    assert set(schemas.__all__) == expected_all
    assert len(schemas.__all__) == len(expected_all)

    for name in schemas.__all__:
        exported_obj = getattr(schemas, name)
        if name in symbol_sources:
            assert exported_obj is symbol_sources[name]


def test_representative_top_level_imports():
    assert NovelConfig is config.NovelConfig
    assert SettingDoc is models.SettingDoc
    assert BlueprintDoc is models.BlueprintDoc
    assert CharacterState is models.CharacterState
    assert TemporalRelation is models.TemporalRelation
    assert Thread is models.Thread
    assert ChapterFactRecord is models.ChapterFactRecord
    assert PacketBuildResult is results.PacketBuildResult
    assert AuditReport is results.AuditReport
    assert callable(export_json_schemas)


def test_export_json_schemas_validity_and_isolation():
    base_classes = {
        _base.LenientModel,
        _base.ValidatedModel,
        _base.StrictModel,
        _base.FrozenDTO,
        genres.GenreExtBase,
    }
    expected_model_names: list[str] = []
    for mod in (_base, config, genres, models, results):
        for name in mod.__all__:
            obj = getattr(mod, name)
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseModel)
                and obj not in base_classes
            ):
                expected_model_names.append(name)
    expected_model_names.sort()

    assert len(expected_model_names) == 49

    exported = export_json_schemas()
    assert list(exported.keys()) == expected_model_names

    for name, schema in exported.items():
        assert isinstance(schema, dict), f"{name} 导出 Schema 不是字典"
        assert schema.get("type") == "object", f"{name} 导出 Schema 缺少 type='object'"

    # 可完整 JSON 序列化与反序列化
    assert json.loads(json.dumps(exported, ensure_ascii=False)) == exported

    # 返回结果互相独立，外部修改不影响后续导出
    original_title = exported["NovelConfig"].get("title")
    exported["NovelConfig"]["title"] = "mutated"
    exported_again = export_json_schemas()
    assert exported_again["NovelConfig"].get("title") == original_title
