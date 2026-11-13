"""T12 Typer + Rich CLI 全指令套件 (`src/ddnovel/cli.py`) 单元与集成测试。"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from ddnovel import __version__
from ddnovel.cli import app

runner = CliRunner()


def _init_sample_book(book_dir: Path, *, genre: str = "cultivation_rpg") -> Path:
    res = runner.invoke(
        app,
        [
            "init",
            str(book_dir),
            "--title",
            "问剑长生",
            "--author",
            "测试作者",
            "--genre",
            genre,
            "--host",
            "opencode,dsh",
            "--host",
            "claude",
        ],
    )
    assert res.exit_code == 0, res.output
    return book_dir


# ---------------------------------------------------------------------------
# 1. 基础版本与 Group 1: 工程初始化 (init)
# ---------------------------------------------------------------------------


def test_cli_version_and_help() -> None:
    res = runner.invoke(app, ["version"])
    assert res.exit_code == 0
    assert __version__ in res.output

    help_res = runner.invoke(app, ["--help"])
    assert help_res.exit_code == 0
    for cmd_name in (
        "init",
        "setting",
        "structure",
        "radar",
        "packet",
        "draft",
        "chapter",
        "state",
        "index",
        "search",
        "thread",
        "history",
        "audit",
    ):
        assert cmd_name in help_res.output


def test_cli_init_command_options_and_duplicate_guard(tmp_path: Path) -> None:
    book_dir = tmp_path / "my_novel"
    res = _init_sample_book(book_dir, genre="cultivation_rpg")
    assert (res / "novel.yaml").is_file()
    assert (res / "opencode.json").is_file()
    assert (res / ".dsh" / "cordis.mcp.patch.yml").is_file()
    assert (res / ".mcp.json").is_file()

    # 重复初始化不加 --force 应优雅报错并返回退出码 1
    dup_res = runner.invoke(app, ["init", str(book_dir)])
    assert dup_res.exit_code == 1
    assert "执行失败" in dup_res.output

    # 使用 --force 与 --no-samples 覆盖初始化
    empty_book = tmp_path / "empty_novel"
    force_res = runner.invoke(
        app,
        ["init", str(empty_book), "--genre", "mystery_intrigue", "--no-samples", "--force"],
    )
    assert force_res.exit_code == 0, force_res.output
    assert (empty_book / "novel.yaml").is_file()
    assert list((empty_book / "01_settings" / "characters").glob("*.md")) == []


# ---------------------------------------------------------------------------
# 2. Group 2 & 3: 设定维护、五维影响面与废案剪枝 (setting)
# ---------------------------------------------------------------------------


def test_cli_setting_upsert_list_show_impact_and_prune(tmp_path: Path) -> None:
    book_dir = _init_sample_book(tmp_path / "book_setting")

    # 1) setting upsert 新建角色设定卡（含核心、专题、机密）
    upsert_res = runner.invoke(
        app,
        [
            "-d",
            str(book_dir),
            "setting",
            "upsert",
            "char_suyao",
            "--name",
            "苏瑶",
            "--category",
            "character",
            "--alias",
            "瑶儿,北境妖女",
            "--tag",
            "女主,妖庭",
            "--core",
            "容貌清丽的青衣女修，表面为南域散修。",
            "--topic",
            "寒冰灵根:苏瑶所修功法可冻结百丈灵气。",
            "--secret",
            "真实身份为北境妖庭圣女，潜入南域只为寻回妖帝遗骨。",
        ],
    )
    assert upsert_res.exit_code == 0, upsert_res.output
    assert "char_suyao" in upsert_res.output
    suyao_path = book_dir / "01_settings" / "characters" / "char_suyao.md"
    assert suyao_path.is_file()

    # 2) setting upsert 增量更新既有卡片（保留原有未传字段）
    update_res = runner.invoke(
        app,
        [
            "setting",
            "upsert",
            "char_suyao",
            "-d",
            str(book_dir),
            "--topic",
            "贴身法宝:玄冰玉簪，可挡金丹一击。",
        ],
    )
    assert update_res.exit_code == 0, update_res.output

    # 3) setting upsert --from-file 导入卡片
    ext_md = tmp_path / "item_orphan.md"
    ext_md.write_text(
        "---\nid: item_orphan_bell\nname: 废案铜铃\ncategory: item\n---\n\n## [核心]\n无人使用的废案铜铃。\n",
        encoding="utf-8",
    )
    from_file_res = runner.invoke(
        app,
        ["setting", "upsert", "--from-file", str(ext_md), "-d", str(book_dir)],
    )
    assert from_file_res.exit_code == 0, from_file_res.output
    assert (book_dir / "01_settings" / "items" / "item_orphan_bell.md").is_file()

    # 4) setting list 列表与分类过滤
    list_res = runner.invoke(
        app,
        ["setting", "list", "--category", "character", "-d", str(book_dir)],
    )
    assert list_res.exit_code == 0, list_res.output
    assert "char_suyao" in list_res.output
    assert "苏瑶" in list_res.output

    # 5) setting show 按 ID 与按外号查阅（含 --hide-secret）
    show_res = runner.invoke(app, ["setting", "show", "瑶儿", "-d", str(book_dir)])
    assert show_res.exit_code == 0, show_res.output
    assert "寒冰灵根" in show_res.output
    assert "北境妖庭圣女" in show_res.output

    hide_res = runner.invoke(
        app,
        ["setting", "show", "char_suyao", "--hide-secret", "-d", str(book_dir)],
    )
    assert hide_res.exit_code == 0, hide_res.output
    assert "已隐藏" in hide_res.output

    # 6) setting impact 扫描被引用实体与零引用孤儿实体
    impact_hit_res = runner.invoke(
        app,
        ["setting", "impact", "char_protagonist", "-d", str(book_dir)],
    )
    assert impact_hit_res.exit_code == 0, impact_hit_res.output
    assert "char_protagonist" in impact_hit_res.output
    assert "state" in impact_hit_res.output

    impact_safe_res = runner.invoke(
        app,
        ["setting", "impact", "item_orphan_bell", "-d", str(book_dir)],
    )
    assert impact_safe_res.exit_code == 0, impact_safe_res.output
    assert "无历史引用冲突" in impact_safe_res.output

    # 7) setting prune 预览与实际物理剪枝
    prune_dry = runner.invoke(app, ["setting", "prune", "-d", str(book_dir)])
    assert prune_dry.exit_code == 0, prune_dry.output
    assert "item_orphan_bell" in prune_dry.output
    assert (book_dir / "01_settings" / "items" / "item_orphan_bell.md").is_file()

    prune_apply = runner.invoke(
        app,
        ["setting", "prune", "item_orphan_bell", "--apply", "-d", str(book_dir)],
    )
    assert prune_apply.exit_code == 0, prune_apply.output
    assert not (book_dir / "01_settings" / "items" / "item_orphan_bell.md").exists()


# ---------------------------------------------------------------------------
# 3. Group 4, 5, 6, 7: 结构蓝图、伏笔雷达、写作包与草稿 (structure/radar/packet/draft)
# ---------------------------------------------------------------------------


def test_cli_structure_radar_packet_and_draft_workflow(tmp_path: Path, monkeypatch) -> None:
    book_dir = _init_sample_book(tmp_path / "book_flow")
    monkeypatch.setenv("DDNOVEL_BOOK_DIR", str(book_dir))

    # 1) structure volume 更新分卷（含折叠摘要）
    vol_res = runner.invoke(
        app,
        [
            "structure",
            "volume",
            "vol_01",
            "--title",
            "潜龙出渊",
            "--range",
            "1-50",
            "--conflict",
            "主角在宗门大比前突破筑基并查明寒潭异变真相",
            "--folded-summary",
            "第一卷折叠摘要：主角夺得古镜初入仙门。",
            "--body",
            "## 分卷大纲\n第一阶段至第三阶段推进计划。",
        ],
    )
    assert vol_res.exit_code == 0, vol_res.output
    assert "vol_01" in vol_res.output

    # 2) structure arc 更新单元
    arc_res = runner.invoke(
        app,
        [
            "structure",
            "arc",
            "vol_01_arc_01",
            "--title",
            "寒潭惊变",
            "--range",
            "1-15",
            "--folded-summary",
            "首单元摘要：山门试炼开启。",
            "--body",
            "## 单元节拍\n第1章至第15章冲突升级。",
        ],
    )
    assert arc_res.exit_code == 0, arc_res.output
    assert "vol_01_arc_01" in arc_res.output

    # 3) structure blueprint 创建第 2 章蓝图
    bp_res = runner.invoke(
        app,
        [
            "structure",
            "blueprint",
            "2",
            "--volume-id",
            "vol_01",
            "--arc-id",
            "vol_01_arc_01",
            "--title",
            "外门执事的试探",
            "--pov",
            "char_protagonist",
            "--character",
            "char_protagonist",
            "--location",
            "loc_starter_stage",
            "--faction",
            "fac_main_org",
            "--item",
            "item_key_token",
            "--thread",
            "thread_opening_mystery:progress",
            "--forbidden",
            "严禁直接暴露古镜器灵存在",
            "--beat",
            "晨钟集结:主角前往外门演武场点卯",
            "--beat",
            "暗中试探:执事借查验令牌试探主角灵力深浅",
            "--body",
            "注意保持悬疑压迫感。",
        ],
    )
    assert bp_res.exit_code == 0, bp_res.output
    assert (book_dir / "02_structure" / "blueprints" / "vol_01" / "ch_0002.md").is_file()

    # 4) radar 伏笔雷达巡检
    radar_res = runner.invoke(
        app,
        ["radar", "--chapter", "2", "--entity", "char_protagonist"],
    )
    assert radar_res.exit_code == 0, radar_res.output
    assert "thread_opening_mystery" in radar_res.output
    assert "窗口到期" in radar_res.output or "实体联动" in radar_res.output

    # 5) packet build & packet show / read
    pkt_build = runner.invoke(app, ["packet", "build", "1", "--budget", "6000"])
    assert pkt_build.exit_code == 0, pkt_build.output
    assert "guardrails" in pkt_build.output
    assert "ch_0001_packet.md" in pkt_build.output
    assert (book_dir / "04_creation" / "packets" / "ch_0001_packet.md").is_file()

    pkt_show = runner.invoke(app, ["packet", "show", "1"])
    assert pkt_show.exit_code == 0, pkt_show.output
    assert "槽位 1" in pkt_show.output

    pkt_read = runner.invoke(app, ["packet", "read", "1"])
    assert pkt_read.exit_code == 0, pkt_read.output

    # 6) draft save & draft show / read
    draft_save = runner.invoke(
        app,
        [
            "draft",
            "save",
            "1",
            "--content",
            "# 第1章 寒潭孤影\n\n夜色深沉，主角独立于崖边，紧握手中信物静静推演局势。",
        ],
    )
    assert draft_save.exit_code == 0, draft_save.output
    assert (book_dir / "04_creation" / "drafts" / "ch_0001_draft.md").is_file()

    draft_show = runner.invoke(app, ["draft", "show", "1"])
    assert draft_show.exit_code == 0, draft_show.output
    assert "寒潭孤影" in draft_show.output

    draft_file = tmp_path / "ch2_input.md"
    draft_file.write_text("# 第2章 演武点卯\n\n晨钟响彻山谷，众人齐聚演武场。", encoding="utf-8")
    draft_save_file = runner.invoke(
        app,
        ["draft", "save", "2", "--from-file", str(draft_file)],
    )
    assert draft_save_file.exit_code == 0, draft_save_file.output
    assert (book_dir / "04_creation" / "drafts" / "ch_0002_draft.md").is_file()


# ---------------------------------------------------------------------------
# 4. Group 8, 9, 10, 11: 定稿结算、索引检索、遗忘瘦身与体检
# ---------------------------------------------------------------------------


def test_cli_promote_state_index_search_thread_history_and_audit(tmp_path: Path) -> None:
    book_dir = _init_sample_book(tmp_path / "book_settlement", genre="general")

    # 保存第 1 章合规草稿并执行 audit
    runner.invoke(
        app,
        [
            "-d",
            str(book_dir),
            "draft",
            "save",
            "1",
            "--content",
            "# 第1章 深夜来电\n\n陆沉在滨海高新区创研园核对加密银色移动盘的最后一组日志。",
        ],
    )

    audit_ok = runner.invoke(app, ["audit", "1", "-d", str(book_dir)])
    assert audit_ok.exit_code == 0, audit_ok.output
    assert "PASSED" in audit_ok.output

    # chapter promote 第 1 章（dry-run 与正式入库）
    prom_dry = runner.invoke(app, ["chapter", "promote", "1", "--dry-run", "-d", str(book_dir)])
    assert prom_dry.exit_code == 0, prom_dry.output
    assert not (book_dir / "04_creation" / "manuscript" / "vol_01" / "ch_0001.md").exists()

    prom_ok = runner.invoke(app, ["chapter", "promote", "1", "-d", str(book_dir)])
    assert prom_ok.exit_code == 0, prom_ok.output
    assert (book_dir / "04_creation" / "manuscript" / "vol_01" / "ch_0001.md").is_file()

    # state update / record-outcome 沉淀第 1 章摘要与事实并更新角色、关系与伏笔
    state_up = runner.invoke(
        app,
        [
            "state",
            "update",
            "-d",
            str(book_dir),
            "--chapter",
            "1",
            "--volume-id",
            "vol_01",
            "--arc-id",
            "vol_01_arc_01",
            "--time",
            "第一日深夜",
            "--macro-situation",
            "项目组进入紧急排查状态",
            "--summary",
            "陆沉在创研园发现加密盘凌晨异常校验记录。",
            "--fact",
            "陆沉于第1章在创研园发现异常时间戳",
            "--fact",
            "启明项目组启动内部日志核查",
            "--fact",
            "加密银色移动盘触发只读预警",
            "--entity",
            "char_protagonist,loc_starter_stage,item_key_token",
            "--char-state",
            json.dumps(
                {
                    "char_protagonist": {
                        "updated_at_ch": 1,
                        "location": "loc_starter_stage",
                        "physical_mental": "专注警惕",
                        "current_goals": ["查明凌晨校验码来源"],
                        "epistemic_bounds": {
                            "knows": ["备份日志存在异常时间戳"],
                            "unaware_of": ["种子轮隐性回购协议"],
                            "misbelieves": [],
                        },
                    }
                },
                ensure_ascii=False,
            ),
            "--relation",
            "char_protagonist:fac_main_org:核心带头人:1",
        ],
    )
    assert state_up.exit_code == 0, state_up.output
    assert (book_dir / "03_state" / "history" / "vol_01_facts.jsonl").is_file()

    # state inspect 检视实体状态与有效关系
    inspect_res = runner.invoke(
        app,
        ["state", "inspect", "char_protagonist", "--chapter", "1", "-d", str(book_dir)],
    )
    assert inspect_res.exit_code == 0, inspect_res.output
    assert "查明凌晨校验码来源" in inspect_res.output
    assert "核心带头人" in inspect_res.output

    # index sync / rebuild & search
    sync_res = runner.invoke(app, ["index", "sync", "-d", str(book_dir)])
    assert sync_res.exit_code == 0, sync_res.output

    rebuild_res = runner.invoke(app, ["index", "rebuild", "-d", str(book_dir)])
    assert rebuild_res.exit_code == 0, rebuild_res.output
    assert "rebuilt=True" in rebuild_res.output

    search_res = runner.invoke(
        app,
        ["search", "创研园", "--scope", "fact", "--top-k", "5", "-d", str(book_dir)],
    )
    assert search_res.exit_code == 0, search_res.output
    assert "fact:ch_0001" in search_res.output

    # thread list & thread drop
    t_list = runner.invoke(app, ["thread", "list", "--status", "planted", "-d", str(book_dir)])
    assert t_list.exit_code == 0, t_list.output
    assert "thread_opening_mystery" in t_list.output

    # 新增一条待弃坑支线伏笔并通过 thread drop 移除
    runner.invoke(
        app,
        [
            "state",
            "record-outcome",
            "-d",
            str(book_dir),
            "--thread",
            json.dumps(
                {
                    "id": "thread_temp_sub",
                    "title": "临时支线悬念",
                    "status": "planted",
                    "planted_at": 1,
                    "last_progressed_at": 1,
                    "public_clue": "无关紧要的茶水间传闻",
                },
                ensure_ascii=False,
            ),
        ],
    )
    drop_res = runner.invoke(app, ["thread", "drop", "thread_temp_sub", "-d", str(book_dir)])
    assert drop_res.exit_code == 0, drop_res.output
    assert "已物理移除" in drop_res.output

    # state gc 冷归档（将已完结伏笔或超期实体归档）
    gc_res = runner.invoke(
        app,
        ["state", "gc", "--chapter", "100", "--dry-run", "-d", str(book_dir)],
    )
    assert gc_res.exit_code == 0, gc_res.output
    assert "预览模式" in gc_res.output

    gc_apply = runner.invoke(
        app,
        ["state", "gc", "--chapter", "100", "--apply", "-d", str(book_dir)],
    )
    assert gc_apply.exit_code == 0, gc_apply.output
    assert "已执行归档" in gc_apply.output

    # history compact 卷级事实蒸馏浓缩
    hist_path = book_dir / "03_state" / "history" / "vol_01_facts.jsonl"
    lines = []
    for ch in range(1, 26):
        lines.append(
            json.dumps(
                {
                    "ch": ch,
                    "volume_id": "vol_01",
                    "arc": "vol_01_arc_01",
                    "summary": f"第{ch}章日常推进摘要",
                    "facts": [f"第{ch}章事实A", f"第{ch}章事实B", f"第{ch}章事实C"],
                    "entities": ["char_protagonist"],
                },
                ensure_ascii=False,
            )
        )
    hist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    compact_res = runner.invoke(app, ["history", "compact", "vol_01", "-d", str(book_dir)])
    assert compact_res.exit_code == 0, compact_res.output
    assert "原始 25 章记录" in compact_res.output


# ---------------------------------------------------------------------------
# 5. 错误处理与一致性体检拦截验证
# ---------------------------------------------------------------------------


def test_cli_error_handling_and_audit_failure_exit_codes(tmp_path: Path) -> None:
    book_dir = _init_sample_book(tmp_path / "book_err", genre="general")

    # 1) 查阅不存在的设定或写作包返回退出码 1 与清晰中文提示
    missing_setting = runner.invoke(app, ["setting", "show", "char_nonexistent", "-d", str(book_dir)])
    assert missing_setting.exit_code == 1
    assert "未找到设定卡片" in missing_setting.output

    missing_pkt = runner.invoke(app, ["packet", "show", "99", "-d", str(book_dir)])
    assert missing_pkt.exit_code == 1
    assert "未找到第 99 章写作包文件" in missing_pkt.output

    # 2) 草稿包含禁用词 "震惊全场" 时，audit 与 chapter promote 均应拦截并返回退出码 1
    runner.invoke(
        app,
        [
            "draft",
            "save",
            "1",
            "--content",
            "# 第1章 违规草稿\n\n这一幕直接震惊全场，所有人都倒吸一口凉气！",
            "-d",
            str(book_dir),
        ],
    )
    audit_fail = runner.invoke(app, ["audit", "1", "-d", str(book_dir)])
    assert audit_fail.exit_code == 1
    assert "BANNED_WORD_HIT" in audit_fail.output

    promote_fail = runner.invoke(app, ["chapter", "promote", "1", "-d", str(book_dir)])
    assert promote_fail.exit_code == 1
    assert "体检未通过" in promote_fail.output

    # 3) 使用 --no-audit 可跳过体检强制归档
    promote_bypass = runner.invoke(
        app,
        ["chapter", "promote", "1", "--no-audit", "-d", str(book_dir)],
    )
    assert promote_bypass.exit_code == 0, promote_bypass.output

    # 4) state update 从 YAML 文件批量导入测试
    batch_file = tmp_path / "outcome.yaml"
    batch_file.write_text(
        yaml.safe_dump(
            {
                "ch": 1,
                "volume_id": "vol_01",
                "summary": "批量导入的第1章摘要",
                "facts": ["事实1", "事实2", "事实3"],
                "entities": ["char_protagonist"],
                "factions": {
                    "fac_main_org": {
                        "updated_at_ch": 1,
                        "status": "active",
                        "leader": "char_protagonist",
                        "territory": ["loc_starter_stage"],
                        "current_goals": ["完成交付"],
                    }
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    batch_res = runner.invoke(
        app,
        ["state", "update", "--from-file", str(batch_file), "-d", str(book_dir)],
    )
    assert batch_res.exit_code == 0, batch_res.output
