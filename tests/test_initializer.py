"""T05 新书四维目录与多宿主配置渲染器单元测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest
import yaml
from pydantic import ValidationError

from ddnovel import __version__
from ddnovel.core import AliasMatcher, ProgressiveParser
from ddnovel.core.schemas import (
    ArcDoc,
    BlueprintDoc,
    CharacterStateFile,
    FactionStateFile,
    HostKind,
    NovelConfig,
    RelationsFile,
    StyleGuide,
    ThreadsFile,
    VolumeDoc,
    WorldState,
)
from ddnovel.core.schemas.genres import _GENRE_REGISTRY, GenreExtBase, register_genre
from ddnovel.templates import (
    ANTIGRAVITY_PLUGIN_NAME,
    BOOK_SKELETON_DIRS,
    SKILL_ROLES,
    GlobalPluginInstallResult,
    InitBookResult,
    ProjectInitializer,
    init_book,
    install_antigravity_plugin,
    resolve_global_mcp_command,
)
from ddnovel.templates.initializer import ProjectInitializer as DirectProjectInitializer


@pytest.fixture
def clean_genre_registry():
    """保证测试注册自定义题材后恢复全局题材注册表。"""
    backup = dict(_GENRE_REGISTRY)
    try:
        yield
    finally:
        _GENRE_REGISTRY.clear()
        _GENRE_REGISTRY.update(backup)


def _split_frontmatter(raw_md: str) -> tuple[dict, str]:
    text = raw_md.replace("\r\n", "\n")
    if text.startswith("---\n"):
        end_idx = text.find("\n---\n", 4)
        if end_idx != -1:
            parsed = yaml.safe_load(text[4:end_idx])
            return (parsed if isinstance(parsed, dict) else {}), text[end_idx + 5 :].strip()
    return {}, text.strip()


# ---------------------------------------------------------------------------
# 1. 三题材四维目录初始化 + Schema / T03 / T04 全链路联动校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("genre", ["general", "cultivation_rpg", "mystery_intrigue"])
def test_init_book_all_genres_schema_parser_and_matcher_integration(tmp_path: Path, genre: str):
    book_dir = tmp_path / f"book_{genre}"
    result = init_book(
        book_dir,
        title=f"测试作品_{genre}",
        author="测试作者",
        genre_profile=genre,
    )

    assert ProjectInitializer is DirectProjectInitializer
    assert isinstance(result, InitBookResult)
    assert result.book_dir == book_dir.resolve()
    assert result.config_path == book_dir.resolve() / "小说配置.yaml"
    assert result.host_files == ()

    # 1. 完整四维目录树与 .gitignore 检查
    for rel_dir in BOOK_SKELETON_DIRS:
        assert (book_dir / rel_dir).is_dir(), f"缺少骨架目录: {rel_dir}"
    gitignore_text = (book_dir / ".gitignore").read_text(encoding="utf-8")
    assert ".ddnovel/" in gitignore_text

    # 2. novel.yaml 配置加载校验
    cfg = NovelConfig.load(book_dir)
    assert cfg == result.config
    assert cfg.genre_profile == genre
    assert cfg.current_chapter == 0
    assert cfg.chapter_id_width == 4
    assert cfg.hosts == []

    # 3. 设定维 01_设定集/ 使用 ProgressiveParser (T03) 解析与三层切片校验
    parser = ProgressiveParser(book_dir=book_dir)
    setting_files = sorted((book_dir / "01_设定集").rglob("*.md"))
    assert len(setting_files) == 5

    settings = [parser.parse_setting_md(path) for path in setting_files]
    setting_ids = {doc.id for doc in settings}
    assert setting_ids == {
        "world_overview",
        "loc_starter_stage",
        "fac_main_org",
        "char_protagonist",
        "item_key_token",
    }
    for doc in settings:
        assert doc.core_text.strip()
        assert len(doc.topics) >= 1
        assert doc.secret_text is not None and doc.secret_text.strip()
        # 机密切片严格物理隔离，不可混入 core_text / topics
        assert doc.secret_text not in doc.core_text
        for topic_val in doc.topics.values():
            assert doc.secret_text not in topic_val

    # 4. 结构维 02_结构/ 校验与 T04 AliasMatcher 联动嗅探
    expected_structure_names = {
        "general": (
            "第01卷-破局立锚.md",
            "第01卷_第01单元-暗夜启程.md",
            "第0001章-深夜来电与异常校验码.md",
        ),
        "cultivation_rpg": (
            "第01卷-微末起青萍.md",
            "第01卷_第01单元-药庐微光.md",
            "第0001章-寒溪采露与铁牌生温.md",
        ),
        "mystery_intrigue": (
            "第01卷-迷雾钟声.md",
            "第01卷_第01单元-三点十七分.md",
            "第0001章-失物招领箱里的停表.md",
        ),
    }
    vol_filename, arc_filename, bp_filename = expected_structure_names[genre]

    assert (book_dir / "02_结构" / "总纲.md").is_file()
    vol_fm, vol_body = _split_frontmatter(
        (book_dir / "02_结构" / "分卷" / vol_filename).read_text(encoding="utf-8")
    )
    vol = VolumeDoc.model_validate({**vol_fm, "body": vol_body})
    assert vol.id == "vol_01"

    arc_fm, arc_body = _split_frontmatter(
        (book_dir / "02_结构" / "单元" / arc_filename).read_text(encoding="utf-8")
    )
    arc = ArcDoc.model_validate({**arc_fm, "body": arc_body})
    assert arc.id == "vol_01_arc_01"
    assert arc.volume_id == "vol_01"

    bp_path = book_dir / "02_结构" / "章节蓝图" / "第01卷" / bp_filename
    assert bp_path.is_file()
    bp = parser.parse_blueprint_md(bp_path)
    assert isinstance(bp, BlueprintDoc)
    assert bp.chapter == 1
    assert bp.pov == "char_protagonist"
    assert len(bp.beats) == 3
    assert set(bp.all_entity_ids()) <= setting_ids

    # 用 T04 AliasMatcher 扫描首章蓝图文本，确保主角名与别名可被嗅探
    matcher = AliasMatcher.from_settings(settings)
    bp_hits = matcher.scan_entity_ids(bp_path.read_text(encoding="utf-8"))
    assert "char_protagonist" in bp_hits

    # 5. 状态维 03_状态/ 全部 YAML 校验（含题材扩展槽 context 校验）及跨引用闭合
    ctx = {"genre_profile": genre}
    state_dir = book_dir / "03_状态"

    world_state = WorldState.model_validate(
        yaml.safe_load((state_dir / "世界状态.yaml").read_text(encoding="utf-8"))
    )
    assert world_state.current_chapter == 0

    active_chars = CharacterStateFile.model_validate(
        yaml.safe_load((state_dir / "当前" / "人物状态.yaml").read_text(encoding="utf-8")),
        context=ctx,
    )
    assert "char_protagonist" in active_chars.root
    assert active_chars.root["char_protagonist"].location in setting_ids

    active_facs = FactionStateFile.model_validate(
        yaml.safe_load((state_dir / "当前" / "势力状态.yaml").read_text(encoding="utf-8")),
        context=ctx,
    )
    assert "fac_main_org" in active_facs.root

    rels = RelationsFile.model_validate(
        yaml.safe_load((state_dir / "人物关系.yaml").read_text(encoding="utf-8"))
    )
    assert len(rels.relations) == 1
    for edge in rels.relations:
        assert edge.source in setting_ids
        assert edge.target in setting_ids

    threads = ThreadsFile.model_validate(
        yaml.safe_load((state_dir / "伏笔台账.yaml").read_text(encoding="utf-8"))
    )
    active_thread_ids = {t.id for t in threads.threads}
    assert "thread_opening_mystery" in active_thread_ids
    assert {ref.thread_id for ref in bp.threads} <= active_thread_ids

    # 冷库空容器校验
    arch_chars = CharacterStateFile.model_validate(
        yaml.safe_load((state_dir / "归档" / "人物状态.yaml").read_text(encoding="utf-8")),
        context=ctx,
    )
    assert arch_chars.root == {}
    arch_facs = FactionStateFile.model_validate(
        yaml.safe_load((state_dir / "归档" / "势力状态.yaml").read_text(encoding="utf-8")),
        context=ctx,
    )
    assert arch_facs.root == {}
    arch_rels = RelationsFile.model_validate(
        yaml.safe_load((state_dir / "归档" / "人物关系.yaml").read_text(encoding="utf-8"))
    )
    assert arch_rels.relations == []
    arch_threads = ThreadsFile.model_validate(
        yaml.safe_load((state_dir / "归档" / "伏笔台账.yaml").read_text(encoding="utf-8"))
    )
    assert arch_threads.threads == []
    assert (state_dir / "历史" / "第01卷_事实.jsonl").is_file()

    # 6. 创作维 04_创作/文风指南.md 校验
    sg_raw = (book_dir / "04_创作" / "文风指南.md").read_text(encoding="utf-8")
    sg_fm, _ = _split_frontmatter(sg_raw)
    sg = StyleGuide.model_validate({**sg_fm, "raw_markdown": sg_raw})
    assert len(sg.pov_rules) >= 2
    assert len(sg.banned_words) >= 3


# ---------------------------------------------------------------------------
# 2. 多宿主配置生成、合并保留与 Skills 目录同步
# ---------------------------------------------------------------------------


def test_init_book_renders_all_three_host_configs_and_skills_dirs(tmp_path: Path):
    book_dir = tmp_path / "multi_host_book"
    result = init_book(
        book_dir,
        title="三宿主协同书库",
        hosts=["opencode", HostKind.DSH, "claude", "opencode"],
    )

    assert result.config.hosts == [HostKind.OPENCODE, HostKind.DSH, HostKind.CLAUDE]

    # 1. opencode.json & .opencode/skills/
    opencode_path = book_dir / "opencode.json"
    assert opencode_path.is_file()
    opencode_data = json.loads(opencode_path.read_text(encoding="utf-8"))
    assert opencode_data["$schema"] == "https://opencode.ai/config.json"
    dd_opencode = opencode_data["mcp"]["ddnovel"]
    assert dd_opencode["type"] == "local"
    assert dd_opencode["command"] == ["ddnovel-mcp"]
    assert dd_opencode["environment"] == {"DDNOVEL_BOOK_DIR": book_dir.resolve().as_posix()}
    assert dd_opencode["enabled"] is True

    # 2. .dsh/cordis.mcp.patch.yml & .dsh/skills/
    dsh_path = book_dir / ".dsh" / "cordis.mcp.patch.yml"
    assert dsh_path.is_file()
    dsh_data = yaml.safe_load(dsh_path.read_text(encoding="utf-8"))
    assert isinstance(dsh_data, list)
    insert_entry = dsh_data[0]["insert"][0]
    assert insert_entry["id"] == "mcp-ddnovel"
    assert insert_entry["name"] == "@deepseek-ai/dsh-mcp-client"
    assert insert_entry["config"]["serverName"] == "ddnovel"
    assert insert_entry["config"]["transport"] == "stdio"
    assert insert_entry["config"]["command"] == "ddnovel-mcp"
    assert insert_entry["config"]["args"] == []
    assert insert_entry["config"]["cwd"] == book_dir.resolve().as_posix()
    assert insert_entry["config"]["env"] == {"DDNOVEL_BOOK_DIR": book_dir.resolve().as_posix()}

    # 3. .mcp.json & .claude/skills/
    claude_path = book_dir / ".mcp.json"
    assert claude_path.is_file()
    claude_data = json.loads(claude_path.read_text(encoding="utf-8"))
    dd_claude = claude_data["mcpServers"]["ddnovel"]
    assert dd_claude["type"] == "stdio"
    assert dd_claude["command"] == "ddnovel-mcp"
    assert dd_claude["args"] == []
    assert dd_claude["env"] == {"DDNOVEL_BOOK_DIR": book_dir.resolve().as_posix()}

    # 4. 三宿主四角色 Skills 目录全部落盘
    for host_skill_root in (
        book_dir / ".opencode" / "skills",
        book_dir / ".dsh" / "skills",
        book_dir / ".claude" / "skills",
    ):
        for role in SKILL_ROLES:
            assert (host_skill_root / role).is_dir()


def test_host_config_merging_custom_command_and_skills_sync(tmp_path: Path):
    # 准备模拟 T14 skills 源目录
    mock_skills = tmp_path / "mock_skills"
    for role in SKILL_ROLES:
        role_dir = mock_skills / role
        role_dir.mkdir(parents=True)
        (role_dir / "SKILL.md").write_text(f"# {role}\n", encoding="utf-8")
        (role_dir / "references").mkdir()
        (role_dir / "references" / "guide.md").write_text("ref\n", encoding="utf-8")

    book_dir = tmp_path / "existing_book"
    init = ProjectInitializer(
        skills_source_dir=mock_skills,
        mcp_command=["uv", "run", "--directory", "/engine/repo", "ddnovel-mcp"],
    )
    init.init_book(book_dir, title="增量挂载测试", hosts="opencode")

    # 在已有配置中放入其他 MCP 服务，验证增量渲染不会破坏已有服务并正确更新 novel.yaml
    (book_dir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "other-tool": {"type": "stdio", "command": "other-mcp", "args": []}
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (book_dir / "opencode.json").write_text(
        json.dumps(
            {
                "theme": "dark",
                "mcp": {
                    "existing-srv": {"type": "local", "command": ["echo"], "enabled": True}
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    written = init.render_host_configs(book_dir, ["opencode", "claude", "dsh"])
    assert len(written) > 0

    opencode_data = json.loads((book_dir / "opencode.json").read_text(encoding="utf-8"))
    assert opencode_data["theme"] == "dark"
    assert "existing-srv" in opencode_data["mcp"]
    assert opencode_data["mcp"]["ddnovel"]["command"] == [
        "uv",
        "run",
        "--directory",
        "/engine/repo",
        "ddnovel-mcp",
    ]

    claude_data = json.loads((book_dir / ".mcp.json").read_text(encoding="utf-8"))
    assert "other-tool" in claude_data["mcpServers"]
    assert claude_data["mcpServers"]["ddnovel"]["command"] == "uv"
    assert claude_data["mcpServers"]["ddnovel"]["args"] == [
        "run",
        "--directory",
        "/engine/repo",
        "ddnovel-mcp",
    ]

    dsh_data = yaml.safe_load((book_dir / ".dsh" / "cordis.mcp.patch.yml").read_text(encoding="utf-8"))
    cfg_block = dsh_data[0]["insert"][0]["config"]
    assert cfg_block["command"] == "uv"
    assert cfg_block["args"] == ["run", "--directory", "/engine/repo", "ddnovel-mcp"]

    # 验证 skills 模板同步
    for host_root in (
        book_dir / ".opencode" / "skills",
        book_dir / ".dsh" / "skills",
        book_dir / ".claude" / "skills",
    ):
        for role in SKILL_ROLES:
            assert (host_root / role / "SKILL.md").read_text(encoding="utf-8") == f"# {role}\n"
            assert (host_root / role / "references" / "guide.md").read_text(encoding="utf-8") == "ref\n"

    # novel.yaml 应自动合并写入新增宿主且保持去重保序
    reloaded = NovelConfig.load(book_dir)
    assert reloaded.hosts == [HostKind.OPENCODE, HostKind.CLAUDE, HostKind.DSH]

    # overwrite=False 时保留已有 ddnovel 配置不改写
    (book_dir / "opencode.json").write_text(
        json.dumps({"mcp": {"ddnovel": {"type": "local", "command": ["custom"]}}}),
        encoding="utf-8",
    )
    init.render_host_configs(book_dir, ["opencode"], overwrite=False, update_novel_yaml=False)
    preserved = json.loads((book_dir / "opencode.json").read_text(encoding="utf-8"))
    assert preserved["mcp"]["ddnovel"]["command"] == ["custom"]


def test_init_book_and_render_host_configs_antigravity_workspace_bundle(tmp_path: Path):
    """验证形态 B：单书工作区 .agents/plugins/ddnovel/ 插件结构、MCP 合并与 Subagent/Rules 契约。"""
    book_dir = tmp_path / "ag_novel"
    res = init_book(book_dir, title="星海问道", hosts=["antigravity"])
    assert res.config.hosts == [HostKind.ANTIGRAVITY]

    plugin_dir = book_dir / ".agents" / "plugins" / ANTIGRAVITY_PLUGIN_NAME
    manifest_path = plugin_dir / "plugin.json"
    mcp_path = plugin_dir / "mcp_config.json"
    agent_path = plugin_dir / "agents" / "dd-chapter-writer.md"
    rules_path = plugin_dir / "rules" / "AGENTS.md"
    readme_path = plugin_dir / "README.md"

    for required in (manifest_path, mcp_path, agent_path, rules_path, readme_path):
        assert required.is_file()
        assert required in res.host_files

    # 1. plugin.json 严格仅含合法 Antigravity 清单字段
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["name"] == "ddnovel"
    assert manifest["version"] == __version__
    assert isinstance(manifest["displayName"], str) and manifest["displayName"]
    assert isinstance(manifest["description"], str) and manifest["description"]
    assert isinstance(manifest["suggestedPrompts"], list) and len(manifest["suggestedPrompts"]) == 3
    assert set(manifest.keys()) <= {
        "name",
        "displayName",
        "version",
        "description",
        "suggestedPrompts",
        "logo",
        "author",
        "homepage",
        "repository",
        "disabled",
    }

    # 2. mcp_config.json 绑定 DDNOVEL_BOOK_DIR 与 cwd，不含 Claude 专属 "type" 字段
    mcp_data = json.loads(mcp_path.read_text(encoding="utf-8"))
    dd_srv = mcp_data["mcpServers"]["ddnovel"]
    assert "type" not in dd_srv
    assert dd_srv["command"] == "ddnovel-mcp"
    assert dd_srv["args"] == []
    assert dd_srv["cwd"] == book_dir.resolve().as_posix()
    assert dd_srv["env"] == {"DDNOVEL_BOOK_DIR": book_dir.resolve().as_posix()}

    # 3. 隔离执笔 Subagent Frontmatter 校验
    agent_raw = agent_path.read_text(encoding="utf-8")
    _, fm_yaml, _ = agent_raw.split("---", 2)
    agent_fm = yaml.safe_load(fm_yaml)
    assert agent_fm["name"] == "dd-chapter-writer"
    assert agent_fm["subagent"] is True
    assert agent_fm["mainAgent"] is False
    assert agent_fm["tools"] == ["read_file", "write_file", "edit_file"]

    # 4. rules/AGENTS.md 不含 YAML frontmatter，包含子代理委派与固定书库路径
    rules_raw = rules_path.read_text(encoding="utf-8")
    assert not rules_raw.lstrip().startswith("---")
    assert "dd-chapter-writer" in rules_raw
    assert book_dir.resolve().as_posix() in rules_raw

    # 5. 验证已有同级 MCP 服务保留与 overwrite=False 保护，以及四宿主协同渲染
    mcp_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "other-srv": {"command": "other", "args": []},
                    "ddnovel": {"command": "custom-mcp", "args": ["--keep"]},
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    custom_init = ProjectInitializer(mcp_command=["uv", "run", "ddnovel-mcp"])
    custom_init.render_host_configs(book_dir, ["antigravity"], overwrite=False, update_novel_yaml=False)
    preserved_mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert preserved_mcp["mcpServers"]["ddnovel"]["command"] == "custom-mcp"
    assert "other-srv" in preserved_mcp["mcpServers"]

    # overwrite=True 时保留 other-srv 并更新 ddnovel + 合并四宿主
    custom_init.render_host_configs(
        book_dir,
        ["opencode", "dsh", "claude", "antigravity"],
        overwrite=True,
        update_novel_yaml=True,
    )
    updated_mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert "other-srv" in updated_mcp["mcpServers"]
    assert updated_mcp["mcpServers"]["ddnovel"]["command"] == "uv"
    assert updated_mcp["mcpServers"]["ddnovel"]["args"] == ["run", "ddnovel-mcp"]
    assert NovelConfig.load(book_dir).hosts == [
        HostKind.ANTIGRAVITY,
        HostKind.OPENCODE,
        HostKind.DSH,
        HostKind.CLAUDE,
    ]


def test_install_global_antigravity_plugin_mode_a(tmp_path: Path):
    """验证形态 A：全局插件安装省略 DDNOVEL_BOOK_DIR 与 cwd，支持自定义与自动命令解析。"""
    default_resolved = resolve_global_mcp_command()
    assert len(default_resolved) >= 1 and all(isinstance(part, str) and part for part in default_resolved)

    plugin_dir = tmp_path / "global_plugins" / "ddnovel"
    res = install_antigravity_plugin(plugin_dir=plugin_dir)
    assert isinstance(res, GlobalPluginInstallResult)
    assert res.host == HostKind.ANTIGRAVITY
    assert res.plugin_dir == plugin_dir.resolve()
    assert res.mcp_command == default_resolved

    mcp_data = json.loads((plugin_dir / "mcp_config.json").read_text(encoding="utf-8"))
    dd_srv = mcp_data["mcpServers"]["ddnovel"]
    assert dd_srv["command"] == default_resolved[0]
    assert dd_srv["args"] == list(default_resolved[1:])
    assert "env" not in dd_srv
    assert "cwd" not in dd_srv

    rules_raw = (plugin_dir / "rules" / "AGENTS.md").read_text(encoding="utf-8")
    assert not rules_raw.lstrip().startswith("---")
    assert "book_dir" in rules_raw
    assert "dd-chapter-writer" in rules_raw

    for role in SKILL_ROLES:
        assert (plugin_dir / "skills" / role / "SKILL.md").is_file()

    # overwrite=False 保护已有自定义规则与 mcp_config
    (plugin_dir / "rules" / "AGENTS.md").write_text("# 自定义全局规则\n", encoding="utf-8")
    custom_res = install_antigravity_plugin(
        plugin_dir=plugin_dir,
        mcp_command=["uv", "run", "--directory", "/repo", "ddnovel-mcp"],
        overwrite=False,
    )
    assert custom_res.mcp_command == ("uv", "run", "--directory", "/repo", "ddnovel-mcp")
    assert (plugin_dir / "rules" / "AGENTS.md").read_text(encoding="utf-8") == "# 自定义全局规则\n"
    assert json.loads((plugin_dir / "mcp_config.json").read_text(encoding="utf-8"))["mcpServers"][
        "ddnovel"
    ]["command"] == default_resolved[0]

    # overwrite=True 更新为显式 mcp_command
    install_antigravity_plugin(
        plugin_dir=plugin_dir,
        mcp_command=["uv", "run", "--directory", "/repo", "ddnovel-mcp"],
        overwrite=True,
    )
    updated_srv = json.loads((plugin_dir / "mcp_config.json").read_text(encoding="utf-8"))["mcpServers"][
        "ddnovel"
    ]
    assert updated_srv["command"] == "uv"
    assert updated_srv["args"] == ["run", "--directory", "/repo", "ddnovel-mcp"]

    # 非 antigravity 宿主禁止走全局插件安装
    with pytest.raises(ValueError, match="antigravity"):
        ProjectInitializer().install_global_plugin("claude", plugin_dir=plugin_dir)


# ---------------------------------------------------------------------------
# 3. 无样例纯净骨架、特殊字符书名、边界与异常防护
# ---------------------------------------------------------------------------


def test_init_book_without_samples_and_special_title(tmp_path: Path):
    book_dir = tmp_path / "书库 目录" / "blank_book"
    res = init_book(
        book_dir,
        title='破晓: "零号"档案 [卷一]',
        chapter_id_width=3,
        include_samples=False,
    )

    assert res.config.title == '破晓: "零号"档案 [卷一]'
    assert res.config.chapter_id_width == 3

    # 无样例模式下 01_设定集 与 章节蓝图 下为空目录，但所有状态容器仍为合法空结构
    assert list((book_dir / "01_设定集").rglob("*.md")) == []
    assert list((book_dir / "02_结构" / "章节蓝图").rglob("*.md")) == []

    state_dir = book_dir / "03_状态"
    ws = WorldState.model_validate(
        yaml.safe_load((state_dir / "世界状态.yaml").read_text(encoding="utf-8"))
    )
    assert ws.current_chapter == 0
    assert CharacterStateFile.model_validate(
        yaml.safe_load((state_dir / "当前" / "人物状态.yaml").read_text(encoding="utf-8"))
    ).root == {}
    assert FactionStateFile.model_validate(
        yaml.safe_load((state_dir / "当前" / "势力状态.yaml").read_text(encoding="utf-8"))
    ).root == {}
    assert RelationsFile.model_validate(
        yaml.safe_load((state_dir / "人物关系.yaml").read_text(encoding="utf-8"))
    ).relations == []
    assert ThreadsFile.model_validate(
        yaml.safe_load((state_dir / "伏笔台账.yaml").read_text(encoding="utf-8"))
    ).threads == []

    sg_raw = (book_dir / "04_创作" / "文风指南.md").read_text(encoding="utf-8")
    sg_fm, _ = _split_frontmatter(sg_raw)
    sg = StyleGuide.model_validate({**sg_fm, "raw_markdown": sg_raw})
    assert sg.pov_rules

    # title=None 时默认使用目录名，且 chapter_id_width=3 生成 第001章-深夜来电与异常校验码.md
    auto_dir = tmp_path / "山海剑途"
    init_book(auto_dir, chapter_id_width=3, include_samples=True)
    assert NovelConfig.load(auto_dir).title == "山海剑途"
    assert (auto_dir / "02_结构" / "章节蓝图" / "第01卷" / "第001章-深夜来电与异常校验码.md").is_file()


def test_overwrite_protection_and_invalid_inputs_leave_no_dirty_dir(tmp_path: Path):
    book_dir = tmp_path / "protected_book"
    init_book(book_dir, title="原书名")

    marker = book_dir / "01_设定集" / "人物" / "char_protagonist-陆沉.md"
    marker.write_text("author custom content", encoding="utf-8")

    # 默认防误覆盖
    with pytest.raises(FileExistsError):
        init_book(book_dir, title="误覆盖")
    assert marker.read_text(encoding="utf-8") == "author custom content"
    assert NovelConfig.load(book_dir).title == "原书名"

    # force=True 允许覆盖重建
    init_book(book_dir, title="确认重建", force=True)
    assert NovelConfig.load(book_dir).title == "确认重建"
    assert "author custom content" not in marker.read_text(encoding="utf-8")

    # 非法题材与非法宿主必须在创建目录前报错，不得遗留半成品目录
    bad_genre_dir = tmp_path / "bad_genre_book"
    with pytest.raises(ValidationError):
        init_book(bad_genre_dir, genre_profile="nonexistent_genre")
    assert not bad_genre_dir.exists()

    bad_host_dir = tmp_path / "bad_host_book"
    with pytest.raises(ValueError, match="不支持的宿主类型"):
        init_book(bad_host_dir, hosts=["unknown_host"])
    assert not bad_host_dir.exists()

    # 空 mcp_command 防护
    with pytest.raises(ValueError, match="mcp_command"):
        ProjectInitializer(mcp_command=[])
    with pytest.raises(ValueError, match="mcp_command"):
        ProjectInitializer(mcp_command="   ")


def test_custom_registered_genre_falls_back_gracefully(tmp_path: Path, clean_genre_registry):
    @register_genre
    class CyberpunkExt(GenreExtBase):
        genre_name: ClassVar[str] = "cyberpunk_noir"
        display_name: ClassVar[str] = "赛博朋克黑色电影"
        chrome_level: int = 0

    book_dir = tmp_path / "cyber_book"
    res = init_book(book_dir, title="霓虹雨", genre_profile="cyberpunk_noir")
    assert res.config.genre_profile == "cyberpunk_noir"

    chars = CharacterStateFile.model_validate(
        yaml.safe_load(
            (book_dir / "03_状态" / "当前" / "人物状态.yaml").read_text(encoding="utf-8")
        ),
        context={"genre_profile": "cyberpunk_noir"},
    )
    assert chars.root["char_protagonist"].profile_ext == {"chrome_level": 0}


def test_init_book_creates_author_notes_layer_and_host_isolation_rules(tmp_path: Path):
    from ddnovel.core.paths import ZH_AUTHOR_NOTES_DIR, ZH_AUTHOR_NOTES_README_FILENAME
    from ddnovel.templates import ANTIGRAVITY_PLUGIN_NAME

    sample_dir = tmp_path / "sample_two_layer"
    init_book(
        sample_dir,
        title="双层设定初始化测试",
        include_samples=True,
        hosts=["antigravity"],
    )
    sample_readme = sample_dir / ZH_AUTHOR_NOTES_DIR / ZH_AUTHOR_NOTES_README_FILENAME
    assert sample_readme.is_file()
    readme_text = sample_readme.read_text(encoding="utf-8")
    assert "00_作者设定" in readme_text and "01_设定集" in readme_text

    plugin_dir = sample_dir / ".agents" / "plugins" / ANTIGRAVITY_PLUGIN_NAME
    writer_agent = (plugin_dir / "agents" / "dd-chapter-writer.md").read_text(encoding="utf-8")
    rules_md = (plugin_dir / "rules" / "AGENTS.md").read_text(encoding="utf-8")
    assert "00_作者设定/" in writer_agent
    assert "三留三剥离" in rules_md
    assert "00_作者设定/" in rules_md

    minimal_dir = tmp_path / "minimal_two_layer"
    init_book(minimal_dir, title="极简双层测试", include_samples=False)
    minimal_readme = minimal_dir / ZH_AUTHOR_NOTES_DIR / ZH_AUTHOR_NOTES_README_FILENAME
    assert minimal_readme.is_file()
