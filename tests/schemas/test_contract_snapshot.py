"""T02.9-B 公共数据契约 JSON Schema 快照冻结测试 (`snapshots/schema_v1.json`)。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from ddnovel.core.schemas import SCHEMA_VERSION, export_json_schemas

SNAPSHOT_PATH = Path(__file__).resolve().parent / "snapshots" / "schema_v1.json"


def _normalize_json(obj: Any) -> Any:
    return json.loads(json.dumps(obj, ensure_ascii=False, sort_keys=True))


def _find_schema_diffs(
    expected_models: dict[str, Any], actual_models: dict[str, Any]
) -> list[str]:
    """对比快照与当前导出模型的差异，返回可读差异列表。"""
    diffs: list[str] = []
    expected_names = set(expected_models)
    actual_names = set(actual_models)

    missing = sorted(expected_names - actual_names)
    added = sorted(actual_names - expected_names)
    if missing:
        diffs.append(f"缺失已冻结模型: {missing}")
    if added:
        diffs.append(f"存在未入快照的新增模型: {added}")

    for name in sorted(expected_names & actual_names):
        if expected_models[name] != actual_models[name]:
            diffs.append(f"模型 '{name}' 的 JSON Schema 与冻结快照不一致")

    return diffs


def test_schema_snapshot_file_exists_and_matches():
    assert SNAPSHOT_PATH.is_file(), (
        f"缺少契约快照文件: {SNAPSHOT_PATH}，请生成并提交 Gate 1 快照"
    )
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert snapshot.get("schema_version") == SCHEMA_VERSION == 1

    expected_models = snapshot.get("models")
    assert isinstance(expected_models, dict)

    actual_models = _normalize_json(export_json_schemas())
    diffs = _find_schema_diffs(expected_models, actual_models)
    assert not diffs, (
        "Schema 契约与冻结快照 schema_v1.json 不一致（若确需变更请走 Gate 1 评审并更新快照）:\n"
        + "\n".join(diffs)
    )


def test_snapshot_comparison_detects_field_drift():
    baseline = _normalize_json(export_json_schemas())

    # 1. 模拟删除字段
    drifted_removed = copy.deepcopy(baseline)
    drifted_removed["NovelConfig"]["properties"].pop("title")
    diffs_removed = _find_schema_diffs(baseline, drifted_removed)
    assert any("NovelConfig" in d for d in diffs_removed)

    # 2. 模拟新增未评审字段
    drifted_added_field = copy.deepcopy(baseline)
    drifted_added_field["Thread"]["properties"]["unexpected_col"] = {"type": "string"}
    diffs_added_field = _find_schema_diffs(baseline, drifted_added_field)
    assert any("Thread" in d for d in diffs_added_field)

    # 3. 模拟修改字段类型
    drifted_type = copy.deepcopy(baseline)
    drifted_type["WorldState"]["properties"]["current_chapter"] = {"type": "string"}
    diffs_type = _find_schema_diffs(baseline, drifted_type)
    assert any("WorldState" in d for d in diffs_type)

    # 4. 模拟增删模型
    drifted_models = copy.deepcopy(baseline)
    drifted_models.pop("AuditReport")
    drifted_models["UnreviewedDTO"] = {"type": "object"}
    diffs_models = _find_schema_diffs(baseline, drifted_models)
    assert any("AuditReport" in d for d in diffs_models)
    assert any("UnreviewedDTO" in d for d in diffs_models)
