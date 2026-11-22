"""T13 FastMCP Stdio 工具服务完整单元测试套件 (`tests/test_mcp_server.py`)。"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

from ddnovel.mcp_server import (
    BOOK_DIR_ENV_VAR,
    MCP_TOOL_NAMES,
    create_server,
    resolve_book_dir,
)
from ddnovel.templates.initializer import init_book

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    socket.socketpair = lambda *_a, **_kw: (  # type: ignore[assignment]
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM),
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM),
    )


def _extract_payload(tool_result: Any) -> dict[str, Any]:
    """从 FastMCP ToolResult 中提取文本并反序列化为 JSON 字典。"""
    assert tool_result.content, "ToolResult.content 不能为空"
    first_block = tool_result.content[0]
    text = getattr(first_block, "text", None)
    assert isinstance(text, str) and text.strip(), "首个 ContentBlock 必须包含非空文本"
    data = json.loads(text)
    assert isinstance(data, dict), "MCP 工具返回文本必须为合法 JSON 对象结构"
    return data


# ---------------------------------------------------------------------------
# 1. 服务注册、JSON Schema 合法性与路径优先级测试
# ---------------------------------------------------------------------------


def test_mcp_server_registration_and_json_schemas() -> None:
    """验证全部 20 个 MCP Tool 成功注册、具备中文描述且 JSON Schema 合法。"""

    async def _check() -> None:
        server = create_server()
        assert server.name == "ddnovel"

        tools = await server.list_tools(run_middleware=False)
        tool_map = {t.name: t for t in tools}

        assert len(MCP_TOOL_NAMES) >= 11
        for expected_name in MCP_TOOL_NAMES:
            assert expected_name in tool_map, f"缺失 MCP 工具: {expected_name}"
            tool = tool_map[expected_name]

            # 校验中文 docstring 描述
            assert tool.description and len(tool.description.strip()) >= 10

            # 校验内部参数 Schema 与导出的 MCP 协议 inputSchema
            schema = tool.parameters
            assert isinstance(schema, dict)
            assert schema.get("type") == "object"
            props = schema.get("properties")
            assert isinstance(props, dict) and "book_dir" in props

            mcp_tool = tool.to_mcp_tool()
            assert mcp_tool.name == expected_name
            assert isinstance(mcp_tool.input_schema, dict)
            assert mcp_tool.input_schema.get("type") == "object"

    asyncio.run(_check())


def test_resolve_book_dir_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 book_dir 解析优先级：显式参数 > create_server 默认值 > 环境变量 > cwd。"""
    explicit_dir = tmp_path / "explicit_book"
    default_dir = tmp_path / "default_book"
    env_dir = tmp_path / "env_book"
    explicit_dir.mkdir()
    default_dir.mkdir()
    env_dir.mkdir()

    monkeypatch.setenv(BOOK_DIR_ENV_VAR, str(env_dir))

    # 1. 显式参数最高优先
    assert resolve_book_dir(str(explicit_dir), default_dir) == explicit_dir.resolve()
    # 2. create_server 默认目录次之
    assert resolve_book_dir(None, default_dir) == default_dir.resolve()
    # 3. 环境变量再次之
    assert resolve_book_dir(None, None) == env_dir.resolve()
    # 4. 清除环境变量后回退到当前工作目录
    monkeypatch.delenv(BOOK_DIR_ENV_VAR, raising=False)
    assert resolve_book_dir(None, None) == Path.cwd().resolve()


# ---------------------------------------------------------------------------
# 2. 设定维护、防吃书影响面、结构蓝图与伏笔雷达异步调用测试
# ---------------------------------------------------------------------------


def test_mcp_settings_structure_radar_and_packet_workflow(tmp_path: Path) -> None:
    """端到端异步测试设定维护、影响面扫描、分卷/单元/蓝图维护、伏笔雷达与写作包编译读取。"""
    book_dir = tmp_path / "cultivation_book"
    init_book(
        book_dir,
        title="问道青云",
        author="测试作者",
        genre_profile="cultivation_rpg",
        include_samples=True,
    )

    async def _run_workflow() -> None:
        server = create_server(book_dir=book_dir)

        # 1. novel_upsert_setting: 新增一个带核心/专题/机密三切片的角色设定
        upsert_res = _extract_payload(
            await server.call_tool(
                "novel_upsert_setting",
                {
                    "entity_id": "char_suyao",
                    "name": "苏瑶",
                    "category": "character",
                    "aliases": ["苏师妹", "白璃圣女"],
                    "tags": ["女主", "妖庭"],
                    "core_text": "表面是青云宗外门药庐弟子，性情温婉但擅长毒术。",
                    "topics": {"药庐丹方": "苏瑶掌握失传的九转清心丹炼制手法。"},
                    "secret_text": "真实身份为北境妖庭潜伏在南域的圣女“白璃”。",
                },
                run_middleware=False,
            )
        )
        assert upsert_res["ok"] is True
        assert upsert_res["setting"]["id"] == "char_suyao"
        assert upsert_res["setting"]["name"] == "苏瑶"
        assert "药庐丹方" in upsert_res["setting"]["topics"]

        # 2. novel_list_settings: 默认屏蔽 secret_text，开启 include_secret=True 时返回
        list_masked = _extract_payload(
            await server.call_tool(
                "novel_list_settings",
                {"category": "character", "include_archived": False, "include_secret": False},
                run_middleware=False,
            )
        )
        assert list_masked["ok"] is True
        suyao_entry = next(x for x in list_masked["items"] if x["id"] == "char_suyao")
        assert suyao_entry["has_secret"] is True
        assert suyao_entry["secret_text"] is None

        list_unmasked = _extract_payload(
            await server.call_tool(
                "novel_list_settings",
                {"category": "character", "include_secret": True},
                run_middleware=False,
            )
        )
        suyao_unmasked = next(x for x in list_unmasked["items"] if x["id"] == "char_suyao")
        assert "白璃" in (suyao_unmasked["secret_text"] or "")

        # 3. novel_setting_impact: 扫描新建孤儿设定（安全）与样例主角（存在引用）
        impact_suyao = _extract_payload(
            await server.call_tool(
                "novel_setting_impact",
                {"target_id_or_keyword": "char_suyao"},
                run_middleware=False,
            )
        )
        assert impact_suyao["ok"] is True
        assert impact_suyao["is_safe_to_change"] is True
        assert impact_suyao["hit_count"] == 0

        impact_protag = _extract_payload(
            await server.call_tool(
                "novel_setting_impact",
                {"target_id_or_keyword": "char_protagonist"},
                run_middleware=False,
            )
        )
        assert impact_protag["ok"] is True
        assert impact_protag["is_safe_to_change"] is False
        assert impact_protag["hit_count"] >= 1

        # 4. novel_upsert_volume & novel_upsert_arc & novel_upsert_blueprint
        vol_res = _extract_payload(
            await server.call_tool(
                "novel_upsert_volume",
                {
                    "volume_id": "vol_01",
                    "title": "第一卷：微末潜龙",
                    "chapter_range": [1, 50],
                    "core_conflict": "顾青在外门立足并查清玄铁令牌秘密",
                    "body": "本卷聚焦外门大比与坠星谷秘境。",
                },
                run_middleware=False,
            )
        )
        assert vol_res["ok"] is True
        assert vol_res["volume"]["id"] == "vol_01"

        arc_res = _extract_payload(
            await server.call_tool(
                "novel_upsert_arc",
                {
                    "arc_id": "vol_01_arc_01",
                    "title": "风起外门",
                    "chapter_range": [1, 15],
                    "body": "起：令牌异动；承：苏瑶赠药；转：执事刁难；合：反击晋升。",
                },
                run_middleware=False,
            )
        )
        assert arc_res["ok"] is True
        assert arc_res["arc"]["volume_id"] == "vol_01"

        bp_res = _extract_payload(
            await server.call_tool(
                "novel_upsert_blueprint",
                {
                    "chapter": 1,
                    "volume_id": "vol_01",
                    "arc_id": "vol_01_arc_01",
                    "title": "寒夜古令与药庐偶遇",
                    "pov": "char_protagonist",
                    "characters": ["char_protagonist", "char_suyao"],
                    "topics": [{"entity_id": "char_suyao", "topic": "药庐丹方"}],
                    "forbidden": ["严禁提前暴露苏瑶的妖庭圣女身份"],
                    "beats": [
                        {"index": 1, "title": "寒夜识海异动", "content": "顾青在杂役房擦拭令牌。"},
                        {"index": 2, "title": "药庐求药", "content": "顾青前往药庐遇到苏瑶请教丹方。"},
                    ],
                },
                run_middleware=False,
            )
        )
        assert bp_res["ok"] is True
        assert bp_res["blueprint"]["chapter"] == 1
        assert len(bp_res["blueprint"]["beats"]) == 2

        # 5. novel_foreshadow_radar: 扫描第 1 章候选伏笔
        radar_res = _extract_payload(
            await server.call_tool(
                "novel_foreshadow_radar",
                {"target_chapter": 1, "planned_entities": ["char_protagonist"]},
                run_middleware=False,
            )
        )
        assert radar_res["ok"] is True
        assert radar_res["target_chapter"] == 1
        assert radar_res["total_active_threads"] >= 1

        # 6. novel_build_packet & novel_read_packet: 编译并读取单章写作包，验证机密物理隔离
        pkt_build = _extract_payload(
            await server.call_tool(
                "novel_build_packet",
                {"chapter": 1, "budget": 9000},
                run_middleware=False,
            )
        )
        assert pkt_build["ok"] is True
        assert pkt_build["chapter"] == 1
        assert "char_suyao" in pkt_build["blocked_secrets"]
        assert len(pkt_build["slots"]) == 5

        pkt_read = _extract_payload(
            await server.call_tool(
                "novel_read_packet",
                {"chapter": 1},
                run_middleware=False,
            )
        )
        assert pkt_read["ok"] is True
        pkt_md = pkt_read["content"]
        assert "九转清心丹" in pkt_md
        # 机密文本与机密代号必须被物理隔离
        assert "潜伏在南域的圣女" not in pkt_md
        assert "白璃圣女" not in pkt_md

    asyncio.run(_run_workflow())


# ---------------------------------------------------------------------------
# 3. 草稿保存、审校体检、定稿入库、状态结算、检索与四级瘦身全链路测试
# ---------------------------------------------------------------------------


def test_mcp_draft_promote_state_search_and_lifecycle(tmp_path: Path) -> None:
    """验证草稿保存、一致性体检、章节定稿、状态沉淀、FTS5 检索、实体检视与四级遗忘瘦身工具。"""
    book_dir = tmp_path / "lifecycle_book"
    init_book(
        book_dir,
        title="星河长明",
        author="测试作者",
        genre_profile="cultivation_rpg",
        include_samples=True,
    )

    async def _run_full_cycle() -> None:
        server = create_server(book_dir=book_dir)

        # 1. novel_save_draft: 保存第 1 章草稿
        draft_res = _extract_payload(
            await server.call_tool(
                "novel_save_draft",
                {
                    "chapter": 1,
                    "content": (
                        "# 第 1 章：寒夜古镜\n\n"
                        "夜风凛冽，林凡在青云宗外门静室凝视掌心斑驳的青铜古镜。"
                    ),
                },
                run_middleware=False,
            )
        )
        assert draft_res["ok"] is True
        assert draft_res["draft_path"] == "04_创作/草稿/第0001章_草稿.md"
        assert draft_res["char_count"] > 10

        # 2. novel_audit_consistency: 对第 1 章执行静态一致性体检
        audit_res = _extract_payload(
            await server.call_tool(
                "novel_audit_consistency",
                {"chapter": 1},
                run_middleware=False,
            )
        )
        assert audit_res["ok"] is True
        assert audit_res["passed"] is True
        assert audit_res["error_count"] == 0

        # 3. novel_promote_chapter: 定稿归档第 1 章
        promote_res = _extract_payload(
            await server.call_tool(
                "novel_promote_chapter",
                {"chapter": 1, "run_audit": True, "dry_run": False},
                run_middleware=False,
            )
        )
        assert promote_res["ok"] is True
        assert promote_res["manuscript_path"] == "04_创作/定稿/第01卷/第0001章-寒夜古镜.md"
        assert (book_dir / promote_res["manuscript_path"]).is_file()

        # 4. novel_record_chapter_outcome: 沉淀第 1 章摘要、原子事实、角色状态、关系与新伏笔
        outcome_res = _extract_payload(
            await server.call_tool(
                "novel_record_chapter_outcome",
                {
                    "chapter": 1,
                    "volume_id": "vol_01",
                    "arc_id": "vol_01_arc_01",
                    "in_world_time": "天历元年冬月初一",
                    "macro_situation": "外门大比将近，暗流涌动",
                    "summary": "林凡在寒夜激发青铜古镜异象，修为精进至练气四层。",
                    "facts": [
                        "林凡于第1章寒夜首次唤醒残缺青铜古镜",
                        "林凡修为突破至练气四层",
                        "执事赵乾察觉杂役峰灵气波动",
                    ],
                    "entities": ["char_linfan"],
                    "character_updates": {
                        "char_linfan": {
                            "physical_mental": "灵力充沛，心神振奋",
                            "current_goals": ["备战外门大比", "探查古镜第二重禁制"],
                            "profile_ext": {"realm": "练气四层"},
                        }
                    },
                    "thread_updates": [
                        {
                            "id": "thread_abandon_candidate",
                            "title": "废弃矿洞支线",
                            "category": "side_quest",
                            "status": "planted",
                            "planted_at": 1,
                            "last_progressed_at": 1,
                            "public_clue": "杂役峰后山有一处废弃灵石矿洞",
                            "truth_secret": "矿洞深处仅有一具枯骨",
                        },
                        {
                            "id": "thread_resolved_demo",
                            "title": "入门令牌遗失之谜",
                            "category": "minor",
                            "status": "resolved",
                            "planted_at": 1,
                            "last_progressed_at": 1,
                            "public_clue": "令牌在溪边寻回",
                        },
                    ],
                },
                run_middleware=False,
            )
        )
        assert outcome_res["ok"] is True
        assert outcome_res["chapter"] == 1
        assert "03_状态/历史/第01卷_事实.jsonl" in outcome_res["updated_files"]

        # 5. novel_sync_index & novel_search & novel_inspect_entity
        sync_res = _extract_payload(
            await server.call_tool(
                "novel_sync_index",
                {"force_rebuild": True},
                run_middleware=False,
            )
        )
        assert sync_res["ok"] is True
        assert sync_res["rebuilt"] is True

        search_res = _extract_payload(
            await server.call_tool(
                "novel_search",
                {"query": "青铜古镜", "scope": "fact", "top_k": 5},
                run_middleware=False,
            )
        )
        assert search_res["ok"] is True
        assert search_res["count"] >= 1
        assert any("青铜古镜" in h["excerpt"] for h in search_res["hits"])

        inspect_res = _extract_payload(
            await server.call_tool(
                "novel_inspect_entity",
                {"entity_id": "char_linfan", "at_chapter": 1},
                run_middleware=False,
            )
        )
        assert inspect_res["ok"] is True
        assert inspect_res["entity_id"] == "char_linfan"
        assert inspect_res["state"]["profile_ext"]["realm"] == "练气四层"

        # 6. novel_drop_thread: 主动弃坑移除 thread_abandon_candidate
        drop_res = _extract_payload(
            await server.call_tool(
                "novel_drop_thread",
                {"thread_id": "thread_abandon_candidate", "dry_run": False},
                run_middleware=False,
            )
        )
        assert drop_res["ok"] is True
        assert drop_res["thread_id"] == "thread_abandon_candidate"

        # 7. novel_cold_archive: 将已完结伏笔 thread_resolved_demo 移入冷库
        gc_res = _extract_payload(
            await server.call_tool(
                "novel_cold_archive",
                {"current_chapter": 1, "dry_run": False},
                run_middleware=False,
            )
        )
        assert gc_res["ok"] is True
        assert "thread_resolved_demo" in gc_res["archived_threads"]

        # 8. novel_prune_unused: 新建一个零引用废案设定并扫描/物理清理
        await server.call_tool(
            "novel_upsert_setting",
            {
                "entity_id": "item_unused_sword",
                "name": "废案断剑",
                "category": "item",
                "core_text": "从未在正文或蓝图中引用过的废案道具。",
            },
            run_middleware=False,
        )
        prune_preview = _extract_payload(
            await server.call_tool(
                "novel_prune_unused",
                {"target_ids": ["item_unused_sword"], "apply": False},
                run_middleware=False,
            )
        )
        assert prune_preview["ok"] is True
        assert "item_unused_sword" in prune_preview["orphan_ids"]
        assert prune_preview["dry_run"] is True

        prune_apply = _extract_payload(
            await server.call_tool(
                "novel_prune_unused",
                {"target_ids": ["item_unused_sword"], "apply": True},
                run_middleware=False,
            )
        )
        assert prune_apply["ok"] is True
        assert len(prune_apply["deleted_paths"]) == 1

        # 9. novel_compact_history: 浓缩 vol_01 历史事实
        compact_res = _extract_payload(
            await server.call_tool(
                "novel_compact_history",
                {"volume_id": "vol_01", "dry_run": True},
                run_middleware=False,
            )
        )
        assert compact_res["ok"] is True
        assert compact_res["volume_id"] == "vol_01"
        assert compact_res["original_count"] == 1
        assert compact_res["compacted_count"] == 1

    asyncio.run(_run_full_cycle())


# ---------------------------------------------------------------------------
# 4. 异常与边界输入优雅降级测试
# ---------------------------------------------------------------------------


def test_mcp_error_handling_graceful_responses(tmp_path: Path) -> None:
    """验证各 MCP 工具在目标缺失或非法业务参数下返回结构化 ok=False 错误报告而不崩溃。"""
    book_dir = tmp_path / "err_book"
    init_book(book_dir, title="错误边界测试", include_samples=False)

    async def _run_errors() -> None:
        server = create_server(book_dir=book_dir)

        # 1. 查阅不存在的实体
        err_inspect = _extract_payload(
            await server.call_tool(
                "novel_inspect_entity",
                {"entity_id": "char_ghost_nobody"},
                run_middleware=False,
            )
        )
        assert err_inspect["ok"] is False
        assert err_inspect["error_type"] == "KeyError"

        # 2. 移除不存在的伏笔
        err_drop = _extract_payload(
            await server.call_tool(
                "novel_drop_thread",
                {"thread_id": "thread_nonexistent"},
                run_middleware=False,
            )
        )
        assert err_drop["ok"] is False
        assert err_drop["error_type"] == "KeyError"

        # 3. 定稿不存在草稿的章节
        err_promote = _extract_payload(
            await server.call_tool(
                "novel_promote_chapter",
                {"chapter": 99},
                run_middleware=False,
            )
        )
        assert err_promote["ok"] is False
        assert err_promote["error_type"] == "FileNotFoundError"

        # 4. 读取不存在蓝图且不存在写作包的章节
        err_packet = _extract_payload(
            await server.call_tool(
                "novel_read_packet",
                {"chapter": 88, "auto_build": False},
                run_middleware=False,
            )
        )
        assert err_packet["ok"] is False
        assert err_packet["error_type"] == "FileNotFoundError"

        # 5. 保存空草稿报错
        err_draft = _extract_payload(
            await server.call_tool(
                "novel_save_draft",
                {"chapter": 1, "content": "   "},
                run_middleware=False,
            )
        )
        assert err_draft["ok"] is False
        assert err_draft["error_type"] == "ValueError"

    asyncio.run(_run_errors())


def test_mcp_rename_setting_and_structure_cleans_stale_files(tmp_path: Path) -> None:
    book_dir = tmp_path / "mcp_rename_book"
    init_book(
        book_dir,
        title="重命名测试",
        genre_profile="cultivation_rpg",
        include_samples=True,
    )

    async def _run() -> None:
        server = create_server(book_dir=book_dir)

        s1 = _extract_payload(
            await server.call_tool(
                "novel_upsert_setting",
                {
                    "entity_id": "char_0001",
                    "name": "林凡",
                    "category": "character",
                    "core_text": "初始核心描述。",
                },
                run_middleware=False,
            )
        )
        assert s1["ok"] is True
        old_char = book_dir / "01_设定集" / "人物" / "char_0001-林凡.md"
        assert old_char.is_file()

        s2 = _extract_payload(
            await server.call_tool(
                "novel_upsert_setting",
                {
                    "entity_id": "char_0001",
                    "name": "林玄",
                    "category": "character",
                    "core_text": "改名后核心描述。",
                },
                run_middleware=False,
            )
        )
        assert s2["ok"] is True
        new_char = book_dir / "01_设定集" / "人物" / "char_0001-林玄.md"
        assert new_char.is_file()
        assert not old_char.exists()

        vol_res = _extract_payload(
            await server.call_tool(
                "novel_upsert_volume",
                {
                    "volume_id": "vol_01",
                    "title": "第一卷：潜龙出渊",
                    "chapter_range": [1, 50],
                    "core_conflict": "立足外门",
                    "body": "卷一正文",
                },
                run_middleware=False,
            )
        )
        assert vol_res["ok"] is True
        assert (book_dir / "02_结构" / "分卷" / "第01卷-潜龙出渊.md").is_file()
        assert not (book_dir / "02_结构" / "分卷" / "第01卷-微末起青萍.md").exists()

        bp_res = _extract_payload(
            await server.call_tool(
                "novel_upsert_blueprint",
                {
                    "chapter": 1,
                    "volume_id": "vol_01",
                    "arc_id": "vol_01_arc_01",
                    "title": "古令初鸣",
                    "pov": "char_0001",
                    "characters": ["char_0001"],
                    "beats": [{"index": 1, "title": "试剑", "content": "林玄试剑。"}],
                },
                run_middleware=False,
            )
        )
        assert bp_res["ok"] is True
        assert (book_dir / "02_结构" / "章节蓝图" / "第01卷" / "第0001章-古令初鸣.md").is_file()
        old_bp = (
            book_dir / "02_结构" / "章节蓝图" / "第01卷" / "第0001章-寒溪采露与铁牌生温.md"
        )
        assert not old_bp.exists()

        pkt_build = _extract_payload(
            await server.call_tool(
                "novel_build_packet",
                {"chapter": 1, "budget": 9000},
                run_middleware=False,
            )
        )
        assert pkt_build["ok"] is True

        pkt_read = _extract_payload(
            await server.call_tool(
                "novel_read_packet",
                {"chapter": 1},
                run_middleware=False,
            )
        )
        assert pkt_read["ok"] is True
        assert "改名后核心描述" in pkt_read["content"]
        assert "古令初鸣" in pkt_read["content"]

    asyncio.run(_run())


def test_mcp_upsert_setting_returns_abstraction_warnings(tmp_path: Path) -> None:
    book_dir = tmp_path / "mcp_abstraction_book"
    init_book(book_dir, title="设定抽象体检测试", include_samples=True)

    async def _run() -> None:
        server = create_server(book_dir=book_dir)

        clean_res = _extract_payload(
            await server.call_tool(
                "novel_upsert_setting",
                {
                    "entity_id": "char_0002",
                    "name": "陆离",
                    "category": "character",
                    "core_text": "外冷内热的侦探型人格，面对危险优先收集证据并保护同伴。",
                },
                run_middleware=False,
            )
        )
        assert clean_res["ok"] is True
        assert clean_res["abstraction_warnings"] == []

        leaky_res = _extract_payload(
            await server.call_tool(
                "novel_upsert_setting",
                {
                    "entity_id": "char_0003",
                    "name": "苏玥",
                    "category": "character",
                    "core_text": (
                        "常见小动作清单：每次冷场必定抚摸银镯。\n"
                        "口头禅：“哼，真是个笨蛋，这种事都要我提醒吗！”"
                    ),
                },
                run_middleware=False,
            )
        )
        assert leaky_res["ok"] is True
        assert len(leaky_res["abstraction_warnings"]) >= 1
        assert "abstraction" not in leaky_res["summary"].lower() or "泄露风险" in leaky_res["summary"]
        assert "泄露风险" in leaky_res["summary"]

        ms_dir = book_dir / "04_创作" / "定稿" / "第01卷"
        ms_dir.mkdir(parents=True, exist_ok=True)
        (ms_dir / "第0001章-起航.md").write_text("# 第 1 章 起航\n\n正文一。\n", encoding="utf-8")
        (ms_dir / "第0002章-风暴.md").write_text("# 第 2 章 风暴\n\n正文二。\n", encoding="utf-8")
        export_res = _extract_payload(
            await server.call_tool(
                "novel_export_txt",
                {},
                run_middleware=False,
            )
        )
        assert export_res["ok"] is True
        assert export_res["chapter_count"] == 2
        assert export_res["chapters"] == [1, 2]
        assert (book_dir / export_res["output_path"]).is_file()

    asyncio.run(_run())
