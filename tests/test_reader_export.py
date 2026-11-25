"""DDNovelReader (.ddbook / .ddpatch) 适配桥接、单章资源规划、CLI/MCP 与宿主配置测试。"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import tomllib
import zipfile
from pathlib import Path

import yaml
from typer.testing import CliRunner

from ddnovel.cli import app
from ddnovel.core.reader_export import (
    ReaderExporter,
    _encode_png_rgba,
    _generate_placeholder_png,
    discover_reader_dir,
    is_placeholder_png,
    validate_reader_target,
)
from ddnovel.mcp_server import create_server
from ddnovel.templates import ProjectInitializer, init_book

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    socket.socketpair = lambda *_a, **_kw: (  # type: ignore[assignment]
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM),
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM),
    )

runner = CliRunner()


def _create_rich_novel_book(tmp_path: Path) -> Path:
    """创建一个包含双角色、双章定稿、防剧透机密设定、双时态关系与已有插画的小说测试书库。"""
    book_dir = tmp_path / "starport_novel"
    init_book(
        book_dir,
        title="星港夜航",
        author="测试作者",
        genre_profile="general",
        include_samples=False,
    )

    # 设定卡：主角、同伴、地点（均带不可泄露的 ## [机密]）
    (book_dir / "01_设定集" / "人物" / "char_lin-林岚.md").write_text(
        "---\n"
        "id: char_lin\n"
        "name: 林岚\n"
        "aliases: [引航员]\n"
        "category: character\n"
        "status: active\n"
        "tags: [主角]\n"
        "---\n\n"
        "## [核心]\n"
        "星港零点泊位的夜间引航员，性格冷静敏锐。\n\n"
        "## [机密]\n"
        "绝对不能泄露的剧透：林岚是旧时代星港 AI 的克隆体。\n",
        encoding="utf-8",
    )
    (book_dir / "01_设定集" / "人物" / "char_shen-沈砚.md").write_text(
        "---\n"
        "id: char_shen\n"
        "name: 沈砚\n"
        "aliases: [守秘人]\n"
        "category: character\n"
        "status: active\n"
        "tags: [同伴]\n"
        "---\n\n"
        "## [核心]\n"
        "常驻旧钟塔的守秘人，持有青铜星盘钥匙。\n\n"
        "## [机密]\n"
        "绝对不能泄露的剧透：沈砚曾亲手封锁零点泊位。\n",
        encoding="utf-8",
    )
    (book_dir / "01_设定集" / "地理" / "loc_berth-零点泊位.md").write_text(
        "---\n"
        "id: loc_berth\n"
        "name: 零点泊位\n"
        "aliases: [星港栈桥]\n"
        "category: geography\n"
        "status: active\n"
        "tags: [地点]\n"
        "---\n\n"
        "## [核心]\n"
        "海湾最外侧的古老泊位，雨夜常年回荡次声波。\n",
        encoding="utf-8",
    )

    # 分卷纲要与关系
    (book_dir / "02_结构" / "分卷" / "第01卷-静音航道.md").write_text(
        "---\n"
        "id: vol_01\n"
        "title: 第一卷：静音航道\n"
        "---\n\n"
        "第一卷主线。\n",
        encoding="utf-8",
    )
    (book_dir / "03_状态" / "人物关系.yaml").write_text(
        yaml.safe_dump(
            {
                "relations": [
                    {
                        "source": "char_lin",
                        "target": "char_shen",
                        "relation": "共鸣协约同伴",
                        "valid_from_ch": 1,
                    }
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # 第 1 章蓝图与定稿
    bp_dir = book_dir / "02_结构" / "章节蓝图" / "第01卷"
    bp_dir.mkdir(parents=True, exist_ok=True)
    (bp_dir / "第0001章-零点泊位.md").write_text(
        "---\n"
        "chapter: 1\n"
        "volume_id: vol_01\n"
        "title: 零点泊位\n"
        "pov: char_lin\n"
        "cast:\n"
        "  characters: [char_lin, char_shen]\n"
        "  locations: [loc_berth]\n"
        "beats:\n"
        "  - index: 1\n"
        "    title: 雨夜抵达泊位\n"
        "    content: 林岚在暴雨中抵达零点泊位。\n"
        "  - index: 2\n"
        "    title: 闸门破框重鸣\n"
        "    content: 与沈砚汇合并见证闸门共鸣开启。\n"
        "---\n",
        encoding="utf-8",
    )

    ms_dir = book_dir / "04_创作" / "定稿" / "第01卷"
    ms_dir.mkdir(parents=True, exist_ok=True)
    (ms_dir / "第0001章-零点泊位.md").write_text(
        "# 第1章 零点泊位\n\n"
        "暴雨敲打着零点泊位的钢梁，林岚站在星港栈桥尽头核对航道读数。\n\n"
        "林岚：「小心！气压读数正在异常飙升，第一道对接闸门快失控了！」\n\n"
        ":::system_hud[STARPORT // BERTH-00 | CHANNEL_OK]\n"
        "GATE_STATUS: SEALED\n"
        "PRESSURE: 101.3 kPa\n"
        ":::\n\n"
        "---\n\n"
        "沈砚微笑着走上栈桥：「别紧张，我带了备用调谐钥匙。」\n\n"
        "随着一阵剧烈共鸣轰鸣，闸门缓缓敞开，银白星云铺满整片海湾。\n",
        encoding="utf-8",
    )
    (ms_dir / "第0002章-回响深廊.md").write_text(
        "# 第2章 回响深廊\n\n"
        "浓雾漫过零点泊位内廊，林岚与沈砚并肩走向深处。\n\n"
        "林岚：「前面有古碑铭文。」\n\n"
        "沈砚沉声道：「看来十二年前的封印尚未失效。」\n",
        encoding="utf-8",
    )

    # 放入已有角色立绘与第 1 章场景 CG（验证真实素材自动复用 + 状态识别）
    lin_portrait_dir = book_dir / "04_创作" / "插画" / "角色立绘" / "char_lin-林岚"
    lin_portrait_dir.mkdir(parents=True, exist_ok=True)
    (lin_portrait_dir / "char_lin-林岚_立绘.png").write_bytes(
        _generate_placeholder_png("assets/sprites/char_lin_default_neutral.png", "#6CE5E8")
    )

    cg_dir = book_dir / "04_创作" / "插画" / "场景CG" / "第01卷"
    cg_dir.mkdir(parents=True, exist_ok=True)
    (cg_dir / "第0001章_S01_星港闸门开启.png").write_bytes(
        _generate_placeholder_png("assets/cgs/cg_ch_0001_01.png", "#6CE5E8")
    )

    return book_dir


def test_export_full_book_passes_strict_ddnovel_reader_validation(tmp_path: Path) -> None:
    """全书导出：验证防剧透百科、真实插画复用、零 null、ZIP Store 封装及 DDNovelReader strict 校验。"""
    book_dir = _create_rich_novel_book(tmp_path)
    exporter = ReaderExporter(book_dir)
    res = exporter.export_book(pack_ddbook=True, generate_placeholders=True, strict_validate=True)

    assert res.mode == "full"
    assert res.exported_chapters == [1, 2]
    assert res.ddbook_path is not None
    ddbook_file = Path(res.ddbook_path)
    assert ddbook_file.is_file()

    # 若本机存在 DDNovelReader，确认使用了真实 ddnovel_tool.py
    if discover_reader_dir() is not None:
        assert res.reader_tool_used == "ddnovel-tool-py"

    assert res.validation is not None
    assert res.validation["ok"] is True, f"校验失败: {res.validation}"
    assert res.validation["errors"] == 0
    assert res.validation["warnings"] == 0

    # 验证 ZIP Store (Method 0)、无 Data Descriptor (0x0008)、根目录平铺 manifest.json
    with zipfile.ZipFile(ddbook_file, "r") as zf:
        infos = zf.infolist()
        names = {info.filename for info in infos}
        assert "manifest.json" in names
        assert "chapters/ch_0001.json" in names
        assert "chapters/ch_0002.json" in names
        assert "gallery/index.json" in names
        assert "codex/entities.json" in names
        for info in infos:
            assert info.compress_type == zipfile.ZIP_STORED
            assert (info.flag_bits & 0x0008) == 0
            assert (info.flag_bits & 0x0001) == 0

        # 验证防剧透：## [机密] 绝对未泄漏进 codex/entities.json
        codex_text = zf.read("codex/entities.json").decode("utf-8")
        assert "绝对不能泄露的剧透" not in codex_text
        codex_obj = json.loads(codex_text)
        ent_ids = {e["id"] for e in codex_obj["entities"]}
        assert {"char_lin", "char_shen", "loc_berth"} <= ent_ids
        assert len(codex_obj["relations"]) == 1

        # 验证已有立绘与第 1 章场景 CG 被真实复用
        assert "assets/cgs/cg_ch_0001_01.png" in res.copied_assets
        assert any(p.startswith("assets/sprites/char_lin_") for p in res.copied_assets)


def test_plan_chapter_resources_proposes_sprites_cgs_backgrounds_and_statuses(tmp_path: Path) -> None:
    """单章设计资源规划：提出立绘/差分、微徽章、背景、CG 与封面需求并区分已有/待差分/缺失状态。"""
    book_dir = _create_rich_novel_book(tmp_path)
    exporter = ReaderExporter(book_dir)

    plan1 = exporter.plan_chapter_resources(1, save_plan=True)
    assert plan1.chapter == 1
    assert plan1.chapter_id == "ch_0001"
    assert plan1.saved_plan_path is not None
    assert Path(plan1.saved_plan_path).is_file()

    by_path = {r.asset_path: r for r in plan1.resources}
    categories = {r.category for r in plan1.resources}
    assert {"cover", "banner", "background", "sprite", "portrait", "cg"} <= categories

    # 林岚有基础常服立绘：neutral=ready，alert 差分=reuse_base；沈砚无立绘：missing
    assert by_path["assets/sprites/char_lin_default_neutral.png"].status == "ready"
    assert by_path["assets/sprites/char_lin_default_alert.png"].status == "reuse_base"
    assert by_path["assets/sprites/char_shen_default_smile.png"].status == "missing"

    # 第 1 章已有场景 CG
    assert by_path["assets/cgs/cg_ch_0001_01.png"].status == "ready"
    assert "视觉资源需求清单" in plan1.summary_markdown

    # 第 2 章尚无场景 CG，应主动提出推荐 CG 需求且状态为 missing
    plan2 = exporter.plan_chapter_resources(2)
    ch2_cgs = [r for r in plan2.resources if r.category == "cg"]
    assert len(ch2_cgs) >= 1
    assert all(r.status == "missing" for r in ch2_cgs)
    assert any(c["style"] == "stone_tablet" for c in plan2.stage_outline["diegetic_card_candidates"])


def test_export_single_chapter_incrementally_and_build_valid_ddpatch(tmp_path: Path) -> None:
    """单章增量导出 + 追更 .ddpatch：保留旧章并通过 DDNovelReader strict 校验。"""
    book_dir = _create_rich_novel_book(tmp_path)
    out_dir = tmp_path / "reader_out"
    exporter = ReaderExporter(book_dir)

    # 先仅导出第 1 章并打包 v1.ddbook
    res1 = exporter.export_chapter(1, output_dir=out_dir, pack_ddbook=True, strict_validate=True)
    assert res1.mode == "chapter"
    assert res1.exported_chapters == [1]
    assert res1.ddbook_path is not None
    v1_ddbook = Path(res1.ddbook_path)
    assert v1_ddbook.is_file()
    assert res1.validation is not None and res1.validation["ok"] is True

    # 再单独设计/增量导出第 2 章并对比 v1.ddbook 构建 .ddpatch
    res2 = exporter.export_chapter(
        2,
        output_dir=out_dir,
        pack_ddbook=False,
        patch_from=v1_ddbook,
        strict_validate=True,
    )
    assert res2.exported_chapters == [2]
    assert res2.ddpatch_path is not None
    patch_file = Path(res2.ddpatch_path)
    assert patch_file.is_file()

    # 确认第 1 章完整保留在 manifest.json 中，且新增了第 2 章
    manifest = json.loads((Path(res2.source_dir) / "manifest.json").read_text(encoding="utf-8"))
    ch_ids = [c["chapter_id"] for v in manifest["volumes"] for c in v["chapters"]]
    assert ch_ids == ["ch_0001", "ch_0002"]

    # 对 .ddpatch 执行严格校验
    patch_val = validate_reader_target(patch_file, strict=True, target_type="ddpatch")
    assert patch_val.ok is True, f".ddpatch 校验失败: {patch_val}"
    assert patch_val.errors == 0
    assert patch_val.warnings == 0


def test_cli_reader_commands_and_mcp_tools(tmp_path: Path) -> None:
    """端到端测试 ddnovel reader CLI 子命令与 novel_reader_* FastMCP 工具。"""
    book_dir = _create_rich_novel_book(tmp_path)

    # 1. CLI: reader plan 1 --json
    r_plan = runner.invoke(app, ["reader", "plan", "1", "-d", str(book_dir), "--json"])
    assert r_plan.exit_code == 0, r_plan.output
    plan_data = json.loads(r_plan.output)
    assert plan_data["chapter"] == 1
    assert plan_data["counts"]["total"] >= 5

    # 2. CLI: reader export (全书) --json
    r_exp = runner.invoke(app, ["reader", "export", "-d", str(book_dir), "--json"])
    assert r_exp.exit_code == 0, r_exp.output
    exp_data = json.loads(r_exp.output)
    assert exp_data["mode"] == "full"
    assert exp_data["validation"]["ok"] is True

    # 3. CLI: reader validate --strict
    r_val = runner.invoke(app, ["reader", "validate", exp_data["ddbook_path"], "--strict", "--json"])
    assert r_val.exit_code == 0, r_val.output
    val_data = json.loads(r_val.output)
    assert val_data["ok"] is True

    # 4. CLI: reader pack
    custom_out = tmp_path / "manual_pack.ddbook"
    r_pack = runner.invoke(
        app,
        ["reader", "pack", exp_data["source_dir"], "-o", str(custom_out), "--strict"],
    )
    assert r_pack.exit_code == 0, r_pack.output
    assert custom_out.is_file()

    # 5. FastMCP 工具调用测试
    async def _run_mcp() -> None:
        server = create_server(book_dir=book_dir)
        mcp_plan = await server.call_tool("novel_reader_plan_chapter", {"chapter": 1})
        plan_payload = json.loads(mcp_plan.content[0].text)
        assert plan_payload["ok"] is True
        assert plan_payload["chapter"] == 1
        assert len(plan_payload["resources"]) >= 5

        mcp_exp = await server.call_tool(
            "novel_reader_export",
            {"chapter": None, "pack_ddbook": True, "strict_validate": True},
        )
        exp_payload = json.loads(mcp_exp.content[0].text)
        assert exp_payload["ok"] is True
        assert exp_payload["validation"]["ok"] is True

        mcp_val = await server.call_tool(
            "novel_reader_validate_pack",
            {"source_dir": exp_payload["source_dir"], "pack": False, "strict": True},
        )
        val_payload = json.loads(mcp_val.content[0].text)
        assert val_payload["ok"] is True
        assert val_payload["validation"]["ok"] is True

    asyncio.run(_run_mcp())


def test_host_initializer_registers_ddnovel_reader_mcp_when_reader_dir_provided(tmp_path: Path) -> None:
    """提供 --reader-dir 时，自动将 ddnovel-reader MCP 写入多宿主配置文件。"""
    fake_reader = tmp_path / "DDNovelReader"
    mcp_script = fake_reader / "scripts" / "ddnovel-mcp.ts"
    mcp_script.parent.mkdir(parents=True)
    mcp_script.write_text("// mock mcp\n", encoding="utf-8")

    book_dir = tmp_path / "hosted_book"
    init_book(book_dir, title="双MCP联调书库", include_samples=False)

    initializer = ProjectInitializer(reader_dir=fake_reader)
    initializer.render_host_configs(
        book_dir,
        hosts=["antigravity", "codex", "claude", "opencode", "dsh"],
        overwrite=True,
    )

    ag_mcp = json.loads((book_dir / ".agents" / "plugins" / "ddnovel" / "mcp_config.json").read_text(encoding="utf-8"))
    assert ag_mcp["mcpServers"]["ddnovel-reader"]["command"] == "node"
    assert ag_mcp["mcpServers"]["ddnovel-reader"]["args"] == [mcp_script.resolve().as_posix()]

    codex_cfg = tomllib.loads((book_dir / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert codex_cfg["mcp_servers"]["ddnovel-reader"]["command"] == "node"
    assert codex_cfg["mcp_servers"]["ddnovel-reader"]["args"] == [mcp_script.resolve().as_posix()]

    claude_cfg = json.loads((book_dir / ".mcp.json").read_text(encoding="utf-8"))
    assert claude_cfg["mcpServers"]["ddnovel-reader"]["command"] == "node"

    opencode_cfg = json.loads((book_dir / "opencode.json").read_text(encoding="utf-8"))
    assert opencode_cfg["mcp"]["ddnovel-reader"]["command"] == ["node", mcp_script.resolve().as_posix()]

    dsh_cfg = yaml.safe_load((book_dir / ".dsh" / "cordis.mcp.patch.yml").read_text(encoding="utf-8"))
    inserts = [entry for op in dsh_cfg if isinstance(op, dict) for entry in op.get("insert", [])]
    assert any(e.get("id") == "mcp-ddnovel-reader" for e in inserts)


def _make_real_png_bytes(r: int = 80, g: int = 160, b: int = 220) -> bytes:
    """生成不含占位标记的真实测试 PNG 字节流。"""
    row = bytes([r, g, b, 255] * 8)
    return _encode_png_rgba(8, 8, [row] * 8, is_placeholder=False)


def test_archive_generated_assets_under_project_and_reverse_sync_skips_placeholders(
    tmp_path: Path,
) -> None:
    """验证过程中生成的图片归档在小说项目 04_创作/插画/ 下，且反向归档自动跳过占位图。"""
    book_dir = _create_rich_novel_book(tmp_path)
    exporter = ReaderExporter(book_dir)

    # 1. 先导出一次（生成占位图）
    exp1 = exporter.export_book(pack_ddbook=False, generate_placeholders=True, strict_validate=True)
    source_dir = Path(exp1.source_dir)
    cover_in_source = source_dir / "assets" / "cover.png"
    assert is_placeholder_png(cover_in_source) is True

    # 2. 模拟外部生图目录（如 $CODEX_HOME/generated_images/）产出真实立绘差分、头像、封面
    external_gen_dir = tmp_path / "external_generated_images"
    external_gen_dir.mkdir()
    gen_alert = external_gen_dir / "gen_lin_alert.png"
    gen_avatar = external_gen_dir / "gen_lin_avatar.png"
    gen_cover = external_gen_dir / "gen_cover.png"
    gen_alert.write_bytes(_make_real_png_bytes(220, 90, 90))
    gen_avatar.write_bytes(_make_real_png_bytes(90, 220, 180))
    gen_cover.write_bytes(_make_real_png_bytes(40, 80, 160))

    # 通过 archive_asset 归档至项目 04_创作/插画/ 并同步至 source/assets/
    arch_alert = exporter.archive_asset(
        gen_alert,
        category="sprite",
        entity_id="char_lin",
        outfit="常服",
        expression="alert",
    )
    assert arch_alert.archive_rel_path == "04_创作/插画/角色立绘/char_lin-林岚/char_lin-林岚_常服_alert.png"
    assert (book_dir / arch_alert.archive_rel_path).is_file()
    assert arch_alert.synced_to_source_path is not None
    assert not is_placeholder_png(Path(arch_alert.synced_to_source_path))

    arch_avatar = exporter.archive_asset(gen_avatar, category="portrait", entity_id="char_lin")
    assert arch_avatar.archive_rel_path == "04_创作/插画/角色头像/char_lin-林岚_头像.png"
    assert (book_dir / arch_avatar.archive_rel_path).is_file()

    arch_cover = exporter.archive_asset(gen_cover, category="cover")
    assert arch_cover.archive_rel_path == "04_创作/插画/封面与横幅/cover.png"
    assert (book_dir / arch_cover.archive_rel_path).is_file()
    assert not is_placeholder_png(cover_in_source)

    # 验证资产清单.yaml 已登记全部归档记录
    catalog_path = book_dir / "04_创作" / "插画" / "资产清单.yaml"
    assert catalog_path.is_file()
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    archived_rels = {item["archive_path"] for item in catalog["assets"]}
    assert {
        arch_alert.archive_rel_path,
        arch_avatar.archive_rel_path,
        arch_cover.archive_rel_path,
    } <= archived_rels

    # 3. 再次运行单章规划：已归档的 alert 差分、头像与封面均应识别为 ready，且每项都带 archive_target_path
    plan = exporter.plan_chapter_resources(1)
    by_path = {r.asset_path: r for r in plan.resources}
    assert all(r.archive_target_path and r.archive_target_path.startswith("04_创作/插画/") for r in plan.resources)
    assert by_path["assets/sprites/char_lin_default_alert.png"].status == "ready"
    assert by_path["assets/portraits/char_lin_badge.png"].status == "ready"
    assert by_path["assets/cover.png"].status == "ready"

    # 4. 模拟直接往 source/assets/bg/loc_berth.png 放入真实场景背景图，执行反向归档（跳过其余占位图）
    direct_bg = source_dir / "assets" / "bg" / "loc_berth.png"
    direct_bg.write_bytes(_make_real_png_bytes(30, 60, 90))
    sync_res = exporter.sync_assets_to_archive(source_dir=source_dir)
    assert any(item["reader_asset_path"] == "assets/bg/loc_berth.png" for item in sync_res.archived)
    assert (book_dir / "04_创作" / "插画" / "场景背景" / "loc_berth-零点泊位.png").is_file()
    assert len(sync_res.skipped_placeholders) >= 1
    assert "assets/banner.png" in sync_res.skipped_placeholders

    # 5. CLI & MCP 归档接口冒烟测试
    gen_banner = external_gen_dir / "gen_banner.png"
    gen_banner.write_bytes(_make_real_png_bytes(110, 120, 200))
    cli_res = runner.invoke(
        app,
        ["reader", "archive", str(gen_banner), "-d", str(book_dir), "-C", "banner", "--json"],
    )
    assert cli_res.exit_code == 0, cli_res.output
    cli_data = json.loads(cli_res.output)
    assert cli_data["archive_rel_path"] == "04_创作/插画/封面与横幅/banner.png"

    async def _run_mcp_archive() -> None:
        server = create_server(book_dir=book_dir)
        res = await server.call_tool(
            "novel_reader_archive_asset",
            {"sync_from_source": True},
        )
        payload = json.loads(res.content[0].text)
        assert payload["ok"] is True
        assert payload["action"] == "sync_from_source"

    asyncio.run(_run_mcp_archive())


def test_plan_chapter_proposes_rich_poses_outfits_expressions_and_appeal_cgs(
    tmp_path: Path,
) -> None:
    """验证单章规划主动提出多表情差分、动作差分、不同装扮以及魅力特写/情感/主角贴贴多类 CG。"""
    book_dir = _create_rich_novel_book(tmp_path)
    ms_dir = book_dir / "04_创作" / "定稿" / "第01卷"
    (ms_dir / "第0003章-月下礼服与贴贴.md").write_text(
        "# 第3章 月下礼服与贴贴\n\n"
        "月光洒满窗边，林岚换上一袭优雅的晚礼服，微风吹动发丝，她回眸望来，眼眸清亮动人。\n\n"
        "林岚脸红着轻轻俯身凑近，靠在肩头低声耳语：「别笑我……这身礼服是不是太显眼了？」\n\n"
        "沈砚温柔地牵起林岚的手，指尖相触，替她拂去发丝间沾染的水珠：「很美，比星海还耀眼。」\n\n"
        "林岚挑眉狡黠一笑，与他十指相扣并肩依偎在窗台前。\n",
        encoding="utf-8",
    )

    exporter = ReaderExporter(book_dir)
    plan = exporter.plan_chapter_resources(3)

    sprites = [r for r in plan.resources if r.category == "sprite"]
    cgs = [r for r in plan.resources if r.category == "cg"]

    # 验证检出了丰富表情差分（blush / tease / tender）、动作差分（pose）与礼服换装（formal）
    expressions = {r.expression for r in sprites}
    poses = {r.pose for r in sprites if r.pose}
    outfits = {r.outfit for r in sprites if r.outfit}
    assert {"blush", "tease", "tender"} <= expressions
    assert "lean_close" in poses or "turn_back" in poses
    assert "formal" in outfits

    # 验证提出了多维度高魅力 CG（主角贴贴 + 角色魅力特写 + 情感瞬间）
    cg_kinds = {r.cg_kind for r in cgs if r.cg_kind}
    assert {"intimacy", "charm_closeup", "emotion"} <= cg_kinds
    assert len(cgs) >= 3

    # 验证支持归档带装扮 + 动作差分的立绘
    tmp_img = tmp_path / "lin_formal_lean_blush.png"
    tmp_img.write_bytes(_make_real_png_bytes(250, 180, 200))
    arch = exporter.archive_asset(
        tmp_img,
        category="sprite",
        entity_id="char_lin",
        outfit="礼服",
        pose="lean_close",
        expression="blush",
    )
    assert arch.archive_rel_path == "04_创作/插画/角色立绘/char_lin-林岚/char_lin-林岚_礼服_lean_close_blush.png"
    assert arch.reader_asset_path == "assets/sprites/char_lin_formal_lean_close_blush.png"
    assert arch.pose == "lean_close"
