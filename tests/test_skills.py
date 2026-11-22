"""T14 多宿主四角色专项 SKILL.md 套件单元测试。

验证内容：
1. `src/ddnovel/templates/skills/<role>/SKILL.md`（及仓库根目录 `skills/<role>/SKILL.md`）四角色文件完整性；
2. YAML Frontmatter 结构与 `opencode` / `dsh` / `claude` 宿主元数据规范兼容性；
3. `ProjectInitializer().init_book(..., hosts=["opencode", "dsh", "claude"])` 自动同步四套 `SKILL.md`；
4. 四角色核心领域铁律（特别是 `dd-chapter-writer` 上下文隔离铁律）、四维路径与 CLI/MCP 工具矩阵对齐。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from ddnovel.core.schemas import HostKind
from ddnovel.templates import SKILL_ROLES, ProjectInitializer, init_book

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_SKILLS_DIR = REPO_ROOT / "src" / "ddnovel" / "templates" / "skills"
ROOT_SKILLS_DIR = REPO_ROOT / "skills"

EXPECTED_ROLES = (
    "dd-setting-architect",
    "dd-structure-planner",
    "dd-chapter-writer",
    "dd-state-curator",
    "dd-character-illustrator",
    "dd-scene-cg-illustrator",
    "dd-reader-packager",
)

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _parse_skill_markdown(path: Path) -> tuple[dict[str, Any], str]:
    """解析 SKILL.md 的 YAML Frontmatter 与 Markdown 正文。"""
    raw = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert raw.startswith("---\n"), f"{path} 必须以 YAML Frontmatter '---' 开头"
    end_idx = raw.find("\n---\n", 4)
    assert end_idx != -1, f"{path} 缺少闭合的 YAML Frontmatter 分隔符 '---'"
    fm_text = raw[4:end_idx]
    body = raw[end_idx + 5 :].strip()
    fm_data = yaml.safe_load(fm_text)
    assert isinstance(fm_data, dict), f"{path} Frontmatter 顶层必须为字典"
    return fm_data, body


# ---------------------------------------------------------------------------
# 1. 四角色 SKILL.md 文件存在性与 opencode / dsh / claude 元数据规范校验
# ---------------------------------------------------------------------------


def test_skill_roles_constant_matches_expected_four_roles():
    assert SKILL_ROLES == EXPECTED_ROLES
    assert TEMPLATES_SKILLS_DIR.is_dir()


@pytest.mark.parametrize("role", EXPECTED_ROLES)
def test_skill_file_exists_and_frontmatter_conforms_to_opencode_and_dsh_specs(role: str):
    skill_path = TEMPLATES_SKILLS_DIR / role / "SKILL.md"
    assert skill_path.is_file(), f"缺少内置技能模板文件: {skill_path}"

    # 若仓库根目录同时维护了 skills/<role>/SKILL.md，确保二者内容完全一致
    root_skill_path = ROOT_SKILLS_DIR / role / "SKILL.md"
    assert root_skill_path.is_file(), f"缺少根目录技能文件: {root_skill_path}"
    assert skill_path.read_text(encoding="utf-8") == root_skill_path.read_text(encoding="utf-8")

    fm, body = _parse_skill_markdown(skill_path)

    # 1. opencode & dsh 必填核心字段校验
    assert fm.get("name") == role, f"Frontmatter name 必须与目录名 {role!r} 严格一致"
    assert SKILL_NAME_RE.fullmatch(fm["name"]), f"技能名称不符合 kebab-case 规范: {fm['name']}"

    description = fm.get("description")
    assert isinstance(description, str) and 20 <= len(description.strip()) <= 1024

    version = fm.get("version")
    assert isinstance(version, str) and version.strip()

    compatibility = fm.get("compatibility")
    assert isinstance(compatibility, list)
    assert {"opencode", "dsh", "claude", "antigravity", "codex"} <= set(compatibility)

    triggers = fm.get("triggers")
    assert isinstance(triggers, list) and len(triggers) >= 3
    assert all(isinstance(t, str) and t.strip() for t in triggers)

    allowed_tools = fm.get("allowed-tools")
    assert isinstance(allowed_tools, list) and len(allowed_tools) >= 2
    assert all(
        isinstance(t, str) and t.startswith(("novel_", "ddbook_"))
        for t in allowed_tools
    )

    # 2. 扩展 metadata 结构校验
    meta = fm.get("metadata")
    assert isinstance(meta, dict)
    assert meta.get("hosts") == ["opencode", "dsh", "claude", "antigravity", "codex"]
    assert isinstance(meta.get("role"), str) and meta["role"]
    assert isinstance(meta.get("display_name"), str) and meta["display_name"]
    assert isinstance(meta.get("phase"), str) and meta["phase"]
    assert isinstance(meta.get("ssot_directories"), list) and len(meta["ssot_directories"]) >= 1
    assert isinstance(meta.get("cli_commands"), list) and len(meta["cli_commands"]) >= 2
    assert all(cmd.startswith("ddnovel ") for cmd in meta["cli_commands"])
    assert meta.get("mcp_tools") == allowed_tools
    assert isinstance(meta.get("context_isolation"), bool)

    # 3. Markdown 正文结构完整性校验
    assert len(body) >= 800, f"{role}/SKILL.md 正文内容过短，需包含完整作业规范"
    assert f"# `{role}`" in body
    assert "CLI" in body and "MCP" in body


# ---------------------------------------------------------------------------
# 2. ProjectInitializer 多宿主初始化与增量同步测试
# ---------------------------------------------------------------------------


def test_init_book_copies_all_four_skill_md_files_to_all_three_hosts(tmp_path: Path):
    book_dir = tmp_path / "novel_with_skills"
    result = init_book(
        book_dir,
        title="四角色技能同步测试",
        hosts=["opencode", "dsh", "claude", "antigravity"],
    )

    host_skill_roots = (
        book_dir / ".opencode" / "skills",
        book_dir / ".dsh" / "skills",
        book_dir / ".claude" / "skills",
        book_dir / ".agents" / "plugins" / "ddnovel" / "skills",
    )

    for host_root in host_skill_roots:
        for role in SKILL_ROLES:
            copied_skill = host_root / role / "SKILL.md"
            src_skill = TEMPLATES_SKILLS_DIR / role / "SKILL.md"
            assert copied_skill.is_file(), f"宿主技能文件未生成: {copied_skill}"
            assert copied_skill in result.host_files
            assert copied_skill.read_text(encoding="utf-8") == src_skill.read_text(encoding="utf-8")


def test_render_host_configs_respects_overwrite_flag_for_skills(tmp_path: Path):
    book_dir = tmp_path / "custom_skill_book"
    initializer = ProjectInitializer()
    initializer.init_book(
        book_dir,
        title="技能覆盖保护测试",
        hosts=[HostKind.OPENCODE, HostKind.ANTIGRAVITY],
    )

    for custom_path in (
        book_dir / ".opencode" / "skills" / "dd-chapter-writer" / "SKILL.md",
        book_dir / ".agents" / "plugins" / "ddnovel" / "skills" / "dd-chapter-writer" / "SKILL.md",
    ):
        custom_path.write_text("# 作者自定义执笔技能\n", encoding="utf-8")

    # overwrite=False 时保留作者在书库内自定义的 SKILL.md
    initializer.render_host_configs(
        book_dir,
        [HostKind.OPENCODE, HostKind.ANTIGRAVITY],
        overwrite=False,
    )
    for custom_path in (
        book_dir / ".opencode" / "skills" / "dd-chapter-writer" / "SKILL.md",
        book_dir / ".agents" / "plugins" / "ddnovel" / "skills" / "dd-chapter-writer" / "SKILL.md",
    ):
        assert custom_path.read_text(encoding="utf-8") == "# 作者自定义执笔技能\n"

    # overwrite=True 时恢复为引擎内置标准 SKILL.md
    initializer.render_host_configs(
        book_dir,
        [HostKind.OPENCODE, HostKind.ANTIGRAVITY],
        overwrite=True,
    )
    expected = (TEMPLATES_SKILLS_DIR / "dd-chapter-writer" / "SKILL.md").read_text(encoding="utf-8")
    for custom_path in (
        book_dir / ".opencode" / "skills" / "dd-chapter-writer" / "SKILL.md",
        book_dir / ".agents" / "plugins" / "ddnovel" / "skills" / "dd-chapter-writer" / "SKILL.md",
    ):
        assert custom_path.read_text(encoding="utf-8") == expected


# ---------------------------------------------------------------------------
# 3. 四角色核心领域约束、上下文隔离铁律与 CLI/MCP 工具矩阵校验
# ---------------------------------------------------------------------------


def test_setting_architect_skill_domain_requirements():
    fm, body = _parse_skill_markdown(TEMPLATES_SKILLS_DIR / "dd-setting-architect" / "SKILL.md")

    assert fm["metadata"]["context_isolation"] is False
    allowed = set(fm["allowed-tools"])
    assert {
        "novel_upsert_setting",
        "novel_list_settings",
        "novel_setting_impact",
        "novel_sync_index",
        "novel_search",
        "novel_inspect_entity",
        "novel_prune_unused",
    } <= allowed

    # 5 类设定目录 + 三级渐进式切片 + 演进 vs 改设定区分 + 五维影响面扫描
    for required_token in (
        "01_设定集/世界观",
        "01_设定集/地理",
        "01_设定集/势力",
        "01_设定集/人物",
        "01_设定集/器物",
        "## [核心]",
        "## [专题:",
        "## [机密]",
        "03_状态/",
        "novel_setting_impact",
        "ddnovel setting impact",
        "novel_sync_index",
        "ddnovel index sync",
        "manuscript",
        "fact",
        "state",
        "thread",
        "blueprint",
    ):
        assert required_token in body, f"dd-setting-architect 缺少关键规范内容: {required_token}"


def test_structure_planner_skill_domain_requirements():
    fm, body = _parse_skill_markdown(TEMPLATES_SKILLS_DIR / "dd-structure-planner" / "SKILL.md")

    assert fm["metadata"]["context_isolation"] is False
    allowed = set(fm["allowed-tools"])
    assert {
        "novel_foreshadow_radar",
        "novel_upsert_volume",
        "novel_upsert_arc",
        "novel_upsert_blueprint",
        "novel_build_packet",
        "novel_read_packet",
    } <= allowed

    # 三级结构文件 + 章节蓝图核心字段 + 三类伏笔雷达 + 五槽位单章写作包编译
    for required_token in (
        "02_结构/总纲.md",
        "02_结构/分卷/第XX卷-<卷名>.md",
        "02_结构/单元/第XX卷_第YY单元-<单元名>.md",
        "02_结构/章节蓝图/第XX卷/第XXXX章-<章节标题>.md",
        "pov",
        "cast",
        "threads",
        "unlock_secrets",
        "topics",
        "forbidden",
        "beats",
        "novel_foreshadow_radar",
        "ddnovel radar",
        "novel_build_packet",
        "ddnovel packet build",
        "guardrails",
        "world_and_facts",
        "entities",
        "continuity",
        "04_创作/写作包/第XXXX章_写作包.md",
    ):
        assert required_token in body, f"dd-structure-planner 缺少关键规范内容: {required_token}"


def test_chapter_writer_strict_context_isolation_iron_law():
    fm, body = _parse_skill_markdown(TEMPLATES_SKILLS_DIR / "dd-chapter-writer" / "SKILL.md")

    # 1. 元数据层必须开启严格上下文隔离，且白名单工具仅限读取写作包与保存草稿
    assert fm["metadata"]["context_isolation"] is True
    assert fm["allowed-tools"] == ["novel_read_packet", "novel_save_draft"]

    forbidden_tools = set(fm["metadata"].get("forbidden_mcp_tools", []))
    for leaked_tool in (
        "novel_search",
        "novel_foreshadow_radar",
        "novel_inspect_entity",
        "novel_list_settings",
        "novel_setting_impact",
    ):
        assert leaked_tool not in fm["allowed-tools"]
        assert leaked_tool in forbidden_tools

    forbidden_dirs = set(fm["metadata"].get("forbidden_directories", []))
    assert {"01_设定集", "02_结构", "03_状态", "04_创作/定稿"} <= forbidden_dirs

    # 2. 正文必须明确锁死“上下文隔离铁律”与唯一输入/输出路径
    for required_token in (
        "上下文隔离铁律",
        "04_创作/写作包/第XXXX章_写作包.md",
        "novel_read_packet",
        "04_创作/草稿/第XXXX章_草稿.md",
        "novel_save_draft",
        "ddnovel draft save",
        "01_设定集/",
        "02_结构/",
        "03_状态/",
        "04_创作/定稿/",
        "unaware_of",
        "## [机密]",
        "truth_secret",
    ):
        assert required_token in body, f"dd-chapter-writer 缺少上下文隔离关键约束: {required_token}"


def test_state_curator_skill_domain_requirements():
    fm, body = _parse_skill_markdown(TEMPLATES_SKILLS_DIR / "dd-state-curator" / "SKILL.md")

    assert fm["metadata"]["context_isolation"] is False
    allowed = set(fm["allowed-tools"])
    assert {
        "novel_audit_consistency",
        "novel_promote_chapter",
        "novel_record_chapter_outcome",
        "novel_upsert_setting",
        "novel_sync_index",
        "novel_inspect_entity",
        "novel_cold_archive",
        "novel_prune_unused",
        "novel_drop_thread",
        "novel_compact_history",
    } <= allowed

    for required_token in (
        "novel_audit_consistency",
        "ddnovel audit",
        "novel_promote_chapter",
        "ddnovel chapter promote",
        "第XX卷_事实.jsonl",
        "03_状态/世界状态.yaml",
        "03_状态/当前/人物状态.yaml",
        "03_状态/当前/势力状态.yaml",
        "03_状态/人物关系.yaml",
        "03_状态/伏笔台账.yaml",
        "novel_record_chapter_outcome",
        "ddnovel state update",
        "01_设定集/",
        "novel_cold_archive",
        "ddnovel state gc",
        "novel_prune_unused",
        "ddnovel setting prune",
        "novel_drop_thread",
        "ddnovel thread drop",
        "novel_compact_history",
        "ddnovel history compact",
    ):
        assert required_token in body, f"dd-state-curator 缺少关键规范内容: {required_token}"


def test_all_blueprint_mcp_tools_and_cli_domains_covered_across_skills():
    """验证架构蓝图第 5 节定义的全部 MCP 工具与核心 CLI 子命令在四角色技能中 100% 覆盖。"""
    all_allowed_tools: set[str] = set()
    all_bodies: list[str] = []

    for role in EXPECTED_ROLES:
        fm, body = _parse_skill_markdown(TEMPLATES_SKILLS_DIR / role / "SKILL.md")
        all_allowed_tools.update(fm["allowed-tools"])
        all_bodies.append(body)

    combined_text = "\n".join(all_bodies)

    expected_mcp_tools = {
        "novel_upsert_setting",
        "novel_list_settings",
        "novel_setting_impact",
        "novel_upsert_volume",
        "novel_upsert_arc",
        "novel_upsert_blueprint",
        "novel_foreshadow_radar",
        "novel_build_packet",
        "novel_read_packet",
        "novel_save_draft",
        "novel_promote_chapter",
        "novel_record_chapter_outcome",
        "novel_sync_index",
        "novel_search",
        "novel_inspect_entity",
        "novel_cold_archive",
        "novel_prune_unused",
        "novel_drop_thread",
        "novel_compact_history",
        "novel_audit_consistency",
    }
    assert expected_mcp_tools <= all_allowed_tools

    for cli_cmd in (
        "ddnovel setting upsert",
        "ddnovel setting list",
        "ddnovel setting show",
        "ddnovel setting impact",
        "ddnovel setting prune",
        "ddnovel structure volume",
        "ddnovel structure arc",
        "ddnovel structure blueprint",
        "ddnovel radar",
        "ddnovel packet build",
        "ddnovel packet show",
        "ddnovel draft save",
        "ddnovel draft show",
        "ddnovel chapter promote",
        "ddnovel state update",
        "ddnovel state inspect",
        "ddnovel state gc",
        "ddnovel index sync",
        "ddnovel index rebuild",
        "ddnovel search",
        "ddnovel thread drop",
        "ddnovel history compact",
        "ddnovel audit",
        "ddnovel setting lint",
    ):
        assert cli_cmd in combined_text, f"四角色 SKILL.md 未覆盖 CLI 命令: {cli_cmd}"


def test_two_layer_setting_isolation_contracts():
    """验证双层设定隔离（00_作者设定/ -> 01_设定集/）在四角色技能契约中的权限与铁律。"""
    arch_fm, arch_body = _parse_skill_markdown(
        TEMPLATES_SKILLS_DIR / "dd-setting-architect" / "SKILL.md"
    )
    assert "00_作者设定" in arch_fm["metadata"]["ssot_directories"]
    assert "ddnovel setting lint" in arch_fm["metadata"]["cli_commands"]
    assert "三留三剥离" in arch_body

    planner_fm, planner_body = _parse_skill_markdown(
        TEMPLATES_SKILLS_DIR / "dd-structure-planner" / "SKILL.md"
    )
    assert "00_作者设定" not in planner_fm["metadata"]["ssot_directories"]
    assert "00_作者设定" in planner_body

    writer_fm, writer_body = _parse_skill_markdown(
        TEMPLATES_SKILLS_DIR / "dd-chapter-writer" / "SKILL.md"
    )
    assert writer_fm["allowed-tools"] == ["novel_read_packet", "novel_save_draft"]
    assert "00_作者设定" in writer_fm["metadata"]["forbidden_directories"]
    assert "00_作者设定" in writer_body
    assert "打卡" in writer_body

    curator_fm, curator_body = _parse_skill_markdown(
        TEMPLATES_SKILLS_DIR / "dd-state-curator" / "SKILL.md"
    )
    assert "00_作者设定" not in curator_fm["metadata"]["ssot_directories"]
    assert "00_作者设定" in curator_body
    assert "SETTING_CONCRETE_LEAK" in curator_body
    assert "三留三剥离" in curator_body


def test_character_illustrator_skill_domain_requirements():
    fm, body = _parse_skill_markdown(TEMPLATES_SKILLS_DIR / "dd-character-illustrator" / "SKILL.md")

    assert fm["metadata"]["context_isolation"] is False
    allowed = set(fm["allowed-tools"])
    assert {"novel_list_settings", "novel_inspect_entity", "novel_search"} <= allowed

    for required_token in (
        "01_设定集/人物",
        "03_状态/当前/人物状态.yaml",
        "04_创作/插画/角色立绘/<实体ID>-<角色名>/",
        "柔润细腻的半厚涂数码绘画质感",
        "三分之二微侧身（3/4 view）",
        "plush full curvy thighs with soft rounded volume",
        "soft airbrushed seamless skin shading",
        "masterpiece, best quality",
        "image-generator",
    ):
        assert required_token in body, f"dd-character-illustrator 缺少关键规范内容: {required_token}"


def test_scene_cg_illustrator_skill_domain_requirements():
    fm, body = _parse_skill_markdown(TEMPLATES_SKILLS_DIR / "dd-scene-cg-illustrator" / "SKILL.md")

    assert fm["metadata"]["context_isolation"] is False
    allowed = set(fm["allowed-tools"])
    assert {"novel_list_settings", "novel_inspect_entity", "novel_search"} <= allowed

    for required_token in (
        "04_创作/定稿",
        "04_创作/草稿",
        "04_创作/插画/角色立绘/<实体ID>-<角色名>/",
        "04_创作/插画/场景CG/第XX卷/",
        "强制人工确认门禁",
        "ask_question",
        "柔润细腻的半厚涂数码绘画",
        "plush full curvy thighs with soft rounded volume",
        "soft airbrushed seamless skin shading",
        "使用参考图中的同一角色",
        "image-generator",
    ):
        assert required_token in body, f"dd-scene-cg-illustrator 缺少关键规范内容: {required_token}"


def test_reader_packager_skill_domain_requirements():
    fm, body = _parse_skill_markdown(TEMPLATES_SKILLS_DIR / "dd-reader-packager" / "SKILL.md")

    assert fm["metadata"]["context_isolation"] is False
    allowed = set(fm["allowed-tools"])
    assert {
        "novel_reader_plan_chapter",
        "novel_reader_export",
        "novel_reader_validate_pack",
        "ddbook_init_project",
        "ddbook_draft_chapter_from_text",
        "ddbook_upsert_chapter",
        "ddbook_sync_gallery",
        "ddbook_upsert_codex",
        "ddbook_generate_placeholder_assets",
        "ddbook_preview_stage",
        "ddbook_preview_codex",
        "ddbook_validate_target",
        "ddbook_pack_book",
        "ddbook_build_patch",
    } <= allowed

    for required_token in (
        "DDNovelReader",
        ".ddbook",
        ".ddpatch",
        "模式 A",
        "模式 B",
        "视觉资源需求清单",
        "强制人工确认门禁",
        "dd-character-illustrator",
        "dd-scene-cg-illustrator",
        "ddnovel reader plan",
        "ddnovel reader export",
        "ddnovel reader validate",
        "ddnovel reader pack",
        "unlock_gallery_id",
        "ZIP Store",
    ):
        assert required_token in body, f"dd-reader-packager 缺少关键规范内容: {required_token}"
