"""T15 百章演进、中途改设定、弃坑瘦身与多宿主 E2E 自动化测试套件 (`tests/test_e2e_hundred_chapters.py`)。"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from ddnovel.cli import app
from ddnovel.core.indexer import SqliteIndexer
from ddnovel.core.packet_builder import PacketBuilder
from ddnovel.core.parser import split_frontmatter
from ddnovel.core.schemas import (
    ChapterFactRecord,
    EmbeddingConfig,
    NovelConfig,
    SlotName,
    format_chapter,
)
from ddnovel.mcp_server import create_server
from ddnovel.templates.initializer import SKILL_ROLES, init_book

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    socket.socketpair = lambda *_a, **_kw: (  # type: ignore[assignment]
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM),
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM),
    )

cli_runner = CliRunner()


# ---------------------------------------------------------------------------
# 测试辅助工具函数
# ---------------------------------------------------------------------------


def _extract_mcp_payload(tool_result: Any) -> dict[str, Any]:
    """从 FastMCP ToolResult 中提取并反序列化 JSON 字典。"""
    assert tool_result.content, "MCP ToolResult.content 不能为空"
    text = getattr(tool_result.content[0], "text", None)
    assert isinstance(text, str) and text.strip(), "MCP 返回首个 ContentBlock 必须包含非空文本"
    payload = json.loads(text)
    assert isinstance(payload, dict), "MCP 返回内容必须为 JSON 对象"
    return payload


def _write_md(path: Path, frontmatter: dict[str, Any], body: str) -> None:
    """以标准 YAML Frontmatter + Markdown 正文落盘文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_yaml = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).rstrip("\n")
    text = f"---\n{fm_yaml}\n---\n\n{body.strip()}\n"
    path.write_text(text, encoding="utf-8")


def _write_yaml(path: Path, data: Any) -> None:
    """将结构化字典写入 YAML 文件（UTF-8，不破坏中文键名）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _read_yaml(path: Path) -> Any:
    """读取并解析 YAML 文件。"""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# E2E 测试 1: 105 章双卷演化 -> 改设定扫雷 -> 四层瘦身 -> 创作闭环 -> 冷库唤醒
# ---------------------------------------------------------------------------


def test_e2e_hundred_chapters_full_lifecycle_evolution(tmp_path: Path) -> None:
    """端到端覆盖百章演化、多宿主、五维防吃书、四层瘦身、U型包隔离、审计门禁与冷库唤醒。"""
    book_dir = tmp_path / "cangqiong_chronicle"

    # ==================================================================
    # Phase 1: 多宿主 CLI 初始化 & 四角色 SKILL.md 契约校验
    # ==================================================================
    init_res = cli_runner.invoke(
        app,
        [
            "init",
            str(book_dir),
            "--title",
            "苍穹剑纪",
            "--author",
            "测试作者",
            "--genre",
            "cultivation_rpg",
            "--host",
            "opencode,dsh",
            "--host",
            "claude",
            "--no-samples",
        ],
    )
    assert init_res.exit_code == 0, init_res.output

    # 校验三宿主 MCP 配置生成
    opencode_cfg = json.loads((book_dir / "opencode.json").read_text(encoding="utf-8"))
    assert "ddnovel" in json.dumps(opencode_cfg, ensure_ascii=False)

    dsh_cfg = _read_yaml(book_dir / ".dsh" / "cordis.mcp.patch.yml")
    assert "ddnovel" in json.dumps(dsh_cfg, ensure_ascii=False)

    claude_cfg = json.loads((book_dir / ".mcp.json").read_text(encoding="utf-8"))
    assert "ddnovel" in claude_cfg["mcpServers"]

    # 校验三宿主 4 个标准化 SKILL.md 均就位，且执笔角色启用上下文隔离
    for skill_root in (
        book_dir / ".opencode" / "skills",
        book_dir / ".dsh" / "skills",
        book_dir / ".claude" / "skills",
    ):
        for role in SKILL_ROLES:
            skill_file = skill_root / role / "SKILL.md"
            assert skill_file.is_file(), f"缺少宿主技能文件: {skill_file}"
            if role == "dd-chapter-writer":
                fm, body = split_frontmatter(skill_file.read_text(encoding="utf-8"))
                assert isinstance(fm.get("metadata"), dict)
                assert fm["metadata"].get("context_isolation") is True
                assert "写作包" in body and "机密" in body

    # ==================================================================
    # Phase 2: 构造 105 章双卷真源世界（含自由排版无 Frontmatter 设定与双时态演化）
    # ==================================================================
    # 1) 文风红线
    _write_md(
        book_dir / "04_creation" / "style_guide.md",
        {
            "tone": "古典仙侠冷峻凝练",
            "pov_rules": ["严守第三人称限制视角，禁止全知剧透"],
            "banned_words": ["一键暴走"],
        },
        "行文忌轻浮网游用语，突出剑意与古拙道韵。",
    )

    # 2) 核心角色与重名消歧角色
    _write_md(
        book_dir / "01_settings" / "characters" / "char_linfan.md",
        {
            "id": "char_linfan",
            "name": "林凡",
            "aliases": ["林师兄", "青云剑首"],
            "category": "character",
            "status": "active",
            "tags": ["主角", "剑修"],
        },
        (
            "## [核心]\n"
            "青云剑宗首席剑修弟子，性格坚毅果决。\n\n"
            "## [专题：剑道神通]\n"
            "修成太虚剑意第三重，剑出如九天星瀑。\n\n"
            "## [专题：身世渊源]\n"
            "幼年出身南荒小村，由师尊苏玄衣带回山门。\n\n"
            "## [机密]\n"
            "体内封印着荒古圣体血脉逆转天命，尚未对外显露。\n"
        ),
    )
    _write_md(
        book_dir / "01_settings" / "characters" / "char_linfanchen.md",
        {
            "id": "char_linfanchen",
            "name": "林凡尘",
            "aliases": [],
            "category": "character",
            "status": "active",
        },
        "## [核心]\n青云外门游历散修，善阵法与灵符推演，与林凡非同一人。\n",
    )
    _write_md(
        book_dir / "01_settings" / "characters" / "char_shenxiao.md",
        {
            "id": "char_shenxiao",
            "name": "沈霄",
            "aliases": ["沈师弟", "血衣叛徒"],
            "category": "character",
            "status": "active",
        },
        (
            "## [核心]\n"
            "昔日青云内门奇才，后叛离山门入血煞殿，近日在归墟与林凡暂歇干戈。\n\n"
            "## [机密]\n"
            "沈霄当年叛出宗门实为奉暗诏追查魔道渗入。\n"
        ),
    )
    _write_md(
        book_dir / "01_settings" / "characters" / "char_mentor_su.md",
        {
            "id": "char_mentor_su",
            "name": "苏玄衣",
            "aliases": ["苏长老", "玄衣剑尊"],
            "category": "character",
            "status": "active",
        },
        "## [核心]\n青云剑宗太上长老，林凡恩师，第四十章宣布入后山死关参悟化神之境。\n",
    )
    _write_md(
        book_dir / "01_settings" / "characters" / "char_protected_spy.md",
        {
            "id": "char_protected_spy",
            "name": "影无踪",
            "aliases": [],
            "category": "character",
            "status": "active",
        },
        "## [核心]\n行踪诡秘的情报暗桩，牵涉宗门密报失窃疑案。\n",
    )

    # 25 名第一卷阶段性人物（01..05 deceased, 06..10 retired, 11..25 active 但长期未出场）
    for idx in range(1, 26):
        eid = f"char_v1_npc_{idx:02d}"
        if idx == 1:
            npc_name = "赵铁柱"
            npc_status = "deceased"
        elif idx == 15:
            npc_name = "韩青锋"
            npc_status = "active"
        elif idx <= 5:
            npc_name = f"古碑甲{idx:02d}"
            npc_status = "deceased"
        elif idx <= 10:
            npc_name = f"隐退乙{idx:02d}"
            npc_status = "retired"
        else:
            npc_name = f"旧部丙{idx:02d}"
            npc_status = "active"

        _write_md(
            book_dir / "01_settings" / "characters" / f"{eid}.md",
            {
                "id": eid,
                "name": npc_name,
                "category": "character",
                "status": npc_status,
            },
            f"## [核心]\n第一卷登场人物{npc_name}（编号 {idx:02d}）。\n",
        )

    # 3 个早期一次性龙套
    cameo_specs = [
        ("char_cameo_01", "店小二阿福"),
        ("char_cameo_02", "渡口船夫老周"),
        ("char_cameo_03", "山道货郎老王"),
    ]
    for cid, cname in cameo_specs:
        _write_md(
            book_dir / "01_settings" / "characters" / f"{cid}.md",
            {"id": cid, "name": cname, "category": "character", "status": "active"},
            f"## [核心]\n早年驿道偶遇的凡俗人物{cname}。\n",
        )

    # 3 个全书零引用废案孤儿 + 1 个物理删除级联验证道具
    _write_md(
        book_dir / "01_settings" / "characters" / "char_orphan_draft.md",
        {"id": "char_orphan_draft", "name": "废案路人甲", "category": "character", "status": "active"},
        "## [核心]\n开篇构思阶段废弃未用的人物卡。\n",
    )
    _write_md(
        book_dir / "01_settings" / "geography" / "loc_orphan_island.md",
        {"id": "loc_orphan_island", "name": "废案孤岛", "category": "geography", "status": "active"},
        "## [核心]\n废弃构想的无名海外孤岛。\n",
    )
    _write_md(
        book_dir / "01_settings" / "items" / "item_orphan_bell.md",
        {"id": "item_orphan_bell", "name": "废案残铃", "category": "item", "status": "active"},
        "## [核心]\n从未在正文与蓝图中出现的废案法器。\n",
    )
    _write_md(
        book_dir / "01_settings" / "items" / "item_temp_talisman.md",
        {"id": "item_temp_talisman", "name": "破界符", "category": "item", "status": "active"},
        "## [核心]\n作者决定直接删除的临时符箓设定，用于检验索引零残留。\n",
    )

    # 地理 / 势力 / 法则 / 无 Frontmatter 纯手写 Markdown 设定
    _write_md(
        book_dir / "01_settings" / "geography" / "loc_abyss.md",
        {"id": "loc_abyss", "name": "归墟深渊", "category": "geography", "status": "active"},
        "## [核心]\n极北尽头的万丈裂谷，青铜巨门镇封太古黑印。\n",
    )
    _write_md(
        book_dir / "01_settings" / "geography" / "loc_sect.md",
        {"id": "loc_sect", "name": "青云山门", "category": "geography", "status": "active"},
        "## [核心]\n东域正道剑修圣地，七峰耸峙云海之上。\n",
    )
    _write_md(
        book_dir / "01_settings" / "factions" / "fac_qingyun.md",
        {"id": "fac_qingyun", "name": "青云剑宗", "category": "faction", "status": "active"},
        "## [核心]\n传承万年的东域剑修大宗。\n",
    )
    _write_md(
        book_dir / "01_settings" / "rules" / "rule_heavens.md",
        {"id": "rule_heavens", "name": "九霄天道压制法则", "category": "rule", "status": "active"},
        "## [核心]\n归墟百里之内元神神识受天道余威压制。\n",
    )
    freeform_path = book_dir / "01_settings" / "items" / "自在残剑谱.md"
    freeform_path.parent.mkdir(parents=True, exist_ok=True)
    freeform_path.write_text(
        "# 自在残剑谱\n\n"
        "## [核心]\n"
        "手写无 Frontmatter 的古剑谱设定文档，记载太古流派残招。\n\n"
        "## [机密]\n"
        "剑谱末页隐含破界斩天七式口诀。\n",
        encoding="utf-8",
    )

    # 3) 分卷与单元纲要（vol_01: 1..60, vol_02: 61..120）
    _write_md(
        book_dir / "02_structure" / "volumes" / "vol_01.md",
        {
            "id": "vol_01",
            "title": "第一卷 青云潜龙",
            "chapter_range": [1, 60],
            "core_conflict": "宗门大比与血煞潜伏之乱",
            "folded_summary": "林凡入青云拜师苏玄衣，历经外门试炼、血煞叛变，最终守住山门。",
        },
        "第一卷完结纲要。\n",
    )
    _write_md(
        book_dir / "02_structure" / "volumes" / "vol_02.md",
        {
            "id": "vol_02",
            "title": "第二卷 归墟问剑",
            "chapter_range": [61, 120],
            "core_conflict": "北境归墟黑印异动与正邪合围",
        },
        "第二卷连载中纲要。\n",
    )
    _write_md(
        book_dir / "02_structure" / "arcs" / "vol_01_arc_01.md",
        {"id": "vol_01_arc_01", "volume_id": "vol_01", "title": "潜龙出渊", "chapter_range": [1, 60]},
        "第一卷主单元。\n",
    )
    _write_md(
        book_dir / "02_structure" / "arcs" / "vol_02_arc_01.md",
        {"id": "vol_02_arc_01", "volume_id": "vol_02", "title": "归墟封魔", "chapter_range": [61, 120]},
        "第二卷主单元。\n",
    )

    # 4) 更新 novel.yaml & world_state & characters & factions & relations & threads
    cfg = NovelConfig.load(book_dir).model_copy(update={"current_chapter": 105})
    cfg.dump(book_dir)

    _write_yaml(
        book_dir / "03_state" / "world_state.yaml",
        {
            "current_chapter": 105,
            "in_world_time": "元和九十三年寒露",
            "macro_situation": "归墟深渊黑印异动加剧，正邪诸宗皆派高手北上探查。",
        },
    )

    active_chars: dict[str, Any] = {
        "char_linfan": {
            "updated_at_ch": 105,
            "location": "loc_abyss",
            "physical_mental": "灵力凝实，剑心通明",
            "current_goals": ["查明归墟黑印震颤根源"],
            "epistemic_bounds": {
                "knows": ["沈霄曾叛离青云剑宗"],
                "unaware_of": ["宗门长老会暗中与血煞殿密约"],
                "misbelieves": ["黑印只是寻常太古禁制"],
            },
            "profile_ext": {
                "realm": "化神初期",
                "techniques": ["太虚剑意"],
                "key_items": ["青云古佩"],
            },
        },
        "char_shenxiao": {
            "updated_at_ch": 105,
            "location": "loc_abyss",
            "physical_mental": "旧伤初愈，戒备深沉",
            "current_goals": ["借林凡剑意共破黑印外阵"],
            "profile_ext": {"realm": "元婴巅峰", "techniques": ["血河煞剑"]},
        },
        "char_linfanchen": {
            "updated_at_ch": 102,
            "location": "loc_sect",
            "physical_mental": "驻守外门阵堂",
            "current_goals": ["修补护山大阵"],
        },
        "char_mentor_su": {
            "updated_at_ch": 40,
            "location": "loc_sect",
            "physical_mental": "入后山死关冲击化神瓶颈",
            "current_goals": ["参悟化神剑道"],
            "profile_ext": {
                "realm": "元婴后期",
                "key_items": ["玄衣古剑"],
            },
        },
        "char_protected_spy": {
            "updated_at_ch": 35,
            "location": "loc_sect",
            "physical_mental": "隐匿宗门暗线之中",
            "current_goals": ["潜伏待机"],
        },
    }
    for idx in range(1, 26):
        eid = f"char_v1_npc_{idx:02d}"
        active_chars[eid] = {
            "updated_at_ch": 25 + idx,  # 26..50，均小于 105 - 40 = 65
            "location": "loc_sect",
            "physical_mental": "第一卷旧人状态",
            "current_goals": ["第一卷往事"],
        }
    _write_yaml(book_dir / "03_state" / "active" / "characters.yaml", active_chars)

    _write_yaml(
        book_dir / "03_state" / "active" / "factions.yaml",
        {
            "fac_qingyun": {
                "updated_at_ch": 105,
                "status": "active",
                "leader": "char_linfan",
                "territory": ["loc_sect"],
                "current_goals": ["镇守归墟北疆门户"],
            }
        },
    )

    _write_yaml(
        book_dir / "03_state" / "relations.yaml",
        {
            "relations": [
                {
                    "source": "char_linfan",
                    "target": "char_shenxiao",
                    "relation": "同门挚友",
                    "valid_from_ch": 1,
                    "invalid_at_ch": 50,
                },
                {
                    "source": "char_linfan",
                    "target": "char_shenxiao",
                    "relation": "叛宗死敌",
                    "valid_from_ch": 50,
                    "invalid_at_ch": 90,
                },
                {
                    "source": "char_linfan",
                    "target": "char_shenxiao",
                    "relation": "归墟暂结血盟",
                    "valid_from_ch": 90,
                    "invalid_at_ch": None,
                },
                {
                    "source": "char_shenxiao",
                    "target": "fac_qingyun",
                    "relation": "戴罪协防",
                    "valid_from_ch": 90,
                    "invalid_at_ch": None,
                },
            ]
        },
    )

    _write_yaml(
        book_dir / "03_state" / "threads.yaml",
        {
            "threads": [
                {
                    "id": "thread_ancient_seal",
                    "title": "归墟黑印镇封之谜",
                    "category": "主线核心",
                    "status": "active",
                    "planted_at": 10,
                    "last_progressed_at": 98,
                    "expected_window": [100, 110],
                    "trigger_entities": ["char_linfan", "loc_abyss", "char_shenxiao"],
                    "public_clue": "归墟黑印每逢朔月便渗出太古剑鸣。",
                    "truth_secret": "黑印下镇压初代祖师尸骸",
                },
                {
                    "id": "thread_stagnant_jade",
                    "title": "破碎魂玉北指异象",
                    "category": "支线悬念",
                    "status": "planted",
                    "planted_at": 20,
                    "last_progressed_at": 60,
                    "public_clue": "破碎魂玉逢夜微光指向极北。",
                    "truth_secret": "魂玉内封存妖皇一缕残魂",
                },
                {
                    "id": "thread_shadow_mole",
                    "title": "宗门暗桩密报疑云",
                    "category": "潜伏暗线",
                    "status": "active",
                    "planted_at": 30,
                    "last_progressed_at": 95,
                    "expected_window": [115, 130],
                    "trigger_entities": ["char_protected_spy"],
                    "public_clue": "护山大阵灵枢图曾经被人临摹。",
                    "truth_secret": "影无踪掌握高层密信副本",
                },
                {
                    "id": "thread_abandoned_sidequest",
                    "title": "南疆古墓残图",
                    "category": "废案支线",
                    "status": "planted",
                    "planted_at": 15,
                    "last_progressed_at": 25,
                    "public_clue": "南疆古墓残图隐约标出一处灵矿。",
                },
            ]
        },
    )

    # 5) 构造 105 章定稿正文 + 两卷历史事实 JSONL
    vol1_fact_lines: list[str] = []
    vol2_fact_lines: list[str] = []

    for ch in range(1, 106):
        vol_id = "vol_01" if ch <= 60 else "vol_02"
        arc_id = "vol_01_arc_01" if ch <= 60 else "vol_02_arc_01"
        ms_path = book_dir / "04_creation" / "manuscript" / vol_id / f"{format_chapter(ch)}.md"
        ms_path.parent.mkdir(parents=True, exist_ok=True)

        if ch == 55:
            # 刻意仅出现长名"林凡尘"，绝不出现独立"林凡"及其别名，用于验证最长匹配前缀消歧
            prose = (
                f"# 第{ch}章 阵堂夜巡\n\n"
                "夜雾笼罩外门阵堂，林凡尘手持朱砂阵笔校勘山南灵脉阵图，独自巡检至子时方歇。\n"
            )
            entities = ["char_linfanchen", "loc_sect"]
            facts = [f"第{ch}章林凡尘独自校勘外门灵脉阵图"]
        elif ch == 3:
            prose = (
                f"# 第{ch}章 山下茶肆\n\n"
                "林凡途经山下驿站，与店小二阿福、渡口船夫老周、山道货郎老王简短问路后径直奔赴山门。\n"
            )
            entities = ["char_linfan", "char_cameo_01", "char_cameo_02", "char_cameo_03"]
            facts = [f"第{ch}章林凡在驿站向店小二阿福与船夫老周等人打听山门路径"]
        elif ch == 4:
            prose = (
                f"# 第{ch}章 渡口风雪\n\n"
                "风雪漫渡口，店小二阿福、渡口船夫老周、山道货郎老王匆匆收摊避雪。\n"
            )
            entities = ["char_cameo_01", "char_cameo_02", "char_cameo_03"]
            facts = [f"第{ch}章雪夜渡口店小二阿福、老周与老王各自归家"]
        elif ch in (18, 22, 28, 34):
            prose = (
                f"# 第{ch}章 边关战事\n\n"
                "林凡与沈霄率赵铁柱、韩青锋及同门旧部坚守北麓防线，浴血迎敌。\n"
            )
            entities = ["char_linfan", "char_shenxiao", "char_v1_npc_01", "char_v1_npc_15", "fac_qingyun"]
            facts = [f"第{ch}章赵铁柱随林凡、沈霄守御青云外防，后于战事中力竭殉道"]
        elif ch in (15, 25):
            prose = (
                f"# 第{ch}章 旧摊残卷\n\n"
                "林凡与沈霄在古市角落瞥见一角南疆古墓残图（thread_abandoned_sidequest），却未深究。\n"
            )
            entities = ["char_linfan", "char_shenxiao"]
            facts = [f"第{ch}章曾出现南疆古墓残图线索"]
        elif ch in (1, 10, 30, 50, 60):
            prose = (
                f"# 第{ch}章 卷中转折\n\n"
                "青云剑宗钟声震荡，林凡与沈霄及同门经历大变，命运至此分流。\n"
            )
            entities = ["char_linfan", "char_shenxiao", "fac_qingyun", "loc_sect"]
            facts = [
                f"第{ch}章青云剑宗重大转折事件发生",
                f"第{ch}章林凡与沈霄在宗门格局中确立新定位",
                f"第{ch}章归墟北疆异象隐现端倪",
            ]
        elif ch == 105:
            prose = (
                "# 第105章 巨门初颤\n\n"
                "寒风呼啸过万丈裂谷，林凡收剑驻步，沈霄侧身立于石崖。\n"
                "归墟深渊的青铜巨门缓缓震颤，沈霄握紧断刃望向黑雾深处。\n"
            )
            entities = ["char_linfan", "char_shenxiao", "loc_abyss"]
            facts = ["第105章林凡与沈霄会师归墟深渊，青铜巨门震颤异响"]
        else:
            prose = (
                f"# 第{ch}章 问道修行\n\n"
                f"第{ch}章记录林凡与血衣叛徒沈霄在青云及归墟一带的修行交锋与因果推进。\n"
            )
            entities = ["char_linfan", "char_shenxiao", "fac_qingyun"]
            facts = [f"第{ch}章林凡与沈霄围绕宗门与归墟事务推进一步"]

        ms_path.write_text(prose, encoding="utf-8")

        fact_rec = ChapterFactRecord(
            ch=ch,
            volume_id=vol_id,
            arc=arc_id,
            summary=f"第{ch}章摘要：围绕主线推进的关键经历。",
            facts=facts,
            entities=entities,
        ).model_dump_json(by_alias=True, exclude_none=True)
        if ch <= 60:
            vol1_fact_lines.append(fact_rec)
        else:
            vol2_fact_lines.append(fact_rec)

    (book_dir / "03_state" / "history" / "vol_01_facts.jsonl").write_text(
        "\n".join(vol1_fact_lines) + "\n", encoding="utf-8"
    )
    (book_dir / "03_state" / "history" / "vol_02_facts.jsonl").write_text(
        "\n".join(vol2_fact_lines) + "\n", encoding="utf-8"
    )

    # 6) 构造第 106~108 章写作蓝图
    _write_md(
        book_dir / "02_structure" / "blueprints" / "vol_02" / "ch_0106.md",
        {
            "chapter": 106,
            "volume_id": "vol_02",
            "arc_id": "vol_02_arc_01",
            "title": "深渊探印",
            "pov": "char_linfan",
            "cast": {
                "characters": ["char_linfan", "char_shenxiao"],
                "locations": ["loc_abyss"],
                "factions": ["fac_qingyun"],
            },
            "threads": [{"thread_id": "thread_ancient_seal", "action": "progress"}],
            "topics": [{"entity_id": "char_linfan", "topic": "剑道神通"}],
            "unlock_secrets": [],
            "forbidden": ["禁止提前揭晓黑印底部的真相"],
        },
        (
            "### 节拍 1: 深渊探印\n"
            "林凡借太虚剑意试探黑印边缘禁制，沈霄从旁策应警戒。\n\n"
            "### 节拍 2: 联手破禁\n"
            "两人暂息前嫌，合力斩断三道太古锁链，引出黑印更深层共鸣。\n"
        ),
    )
    _write_md(
        book_dir / "02_structure" / "blueprints" / "vol_02" / "ch_0107.md",
        {
            "chapter": 107,
            "volume_id": "vol_02",
            "arc_id": "vol_02_arc_01",
            "title": "圣体破封",
            "pov": "char_linfan",
            "cast": {
                "characters": ["char_linfan", "char_shenxiao"],
                "locations": ["loc_abyss"],
            },
            "threads": [{"thread_id": "thread_ancient_seal", "action": "resolve"}],
            "unlock_secrets": ["char_linfan"],
            "forbidden": ["严守第三人称限制视角"],
        },
        "### 节拍 1: 圣体觉醒破封\n林凡引动体内血脉，彻底破开归墟黑印，查清祖师当年留下的真容。\n",
    )
    _write_md(
        book_dir / "02_structure" / "blueprints" / "vol_02" / "ch_0108.md",
        {
            "chapter": 108,
            "volume_id": "vol_02",
            "arc_id": "vol_02_arc_01",
            "title": "剑尊破关",
            "pov": "char_linfan",
            "cast": {
                "characters": ["char_linfan", "char_mentor_su", "char_v1_npc_15"],
                "locations": ["loc_abyss"],
            },
        },
        "### 节拍 1: 师尊破关驰援\n苏玄衣率韩青锋千里驰援归墟，师徒并肩稳住局势。\n",
    )

    # 7) 预置超期过程产物（TTL=5，第 106 章定稿时应被自动清理）
    expired_pkt = book_dir / "04_creation" / "packets" / "ch_0080_packet.md"
    expired_draft = book_dir / "04_creation" / "drafts" / "ch_0080_draft.md"
    expired_pkt.parent.mkdir(parents=True, exist_ok=True)
    expired_draft.parent.mkdir(parents=True, exist_ok=True)
    expired_pkt.write_text("# 第80章旧写作包\n", encoding="utf-8")
    expired_draft.write_text("# 第80章旧草稿\n", encoding="utf-8")

    # 8) CLI 全库同步 + 验证无 Frontmatter 设定入库 & 双时态点查
    sync_cli = cli_runner.invoke(app, ["index", "sync", "-d", str(book_dir)])
    assert sync_cli.exit_code == 0, sync_cli.output

    with SqliteIndexer(book_dir) as idx:
        all_items = idx.list_entities(category="item")
        freeform_doc = next((d for d in all_items if d.name == "自在残剑谱"), None)
        assert freeform_doc is not None, "无 Frontmatter 的纯手写设定应能正常入库"
        assert freeform_doc.secret_text and "破界斩天七式" in freeform_doc.secret_text

        b30 = idx.get_entity_bundle("char_linfan", at_chapter=30)
        b70 = idx.get_entity_bundle("char_linfan", at_chapter=70)
        b105 = idx.get_entity_bundle("char_linfan", at_chapter=105)
        assert [r.relation for r in b30.relations] == ["同门挚友"]
        assert [r.relation for r in b70.relations] == ["叛宗死敌"]
        assert [r.relation for r in b105.relations] == ["归墟暂结血盟"]

    # ==================================================================
    # Phase 3: 中途改设定五维影响面扫描 + 删卡级联同步（FastMCP + CLI）
    # ==================================================================
    impact_cli = cli_runner.invoke(
        app, ["setting", "impact", "char_shenxiao", "--after-chapter", "105", "-d", str(book_dir)]
    )
    assert impact_cli.exit_code == 0, impact_cli.output

    # 物理删卡级联测试
    (book_dir / "01_settings" / "items" / "item_temp_talisman.md").unlink()
    sync_del_cli = cli_runner.invoke(app, ["index", "sync", "-d", str(book_dir)])
    assert sync_del_cli.exit_code == 0, sync_del_cli.output
    with SqliteIndexer(book_dir) as idx:
        assert idx.get_entity("item_temp_talisman") is None
        assert idx.search_fts("破界符") == []

    # ==================================================================
    # Phase 4: 四层遗忘与上下文瘦身 GC（CLI 命令闭环）
    # ==================================================================
    # 1) 状态热转冷归档
    gc_cli = cli_runner.invoke(
        app, ["state", "gc", "--chapter", "105", "--apply", "-d", str(book_dir)]
    )
    assert gc_cli.exit_code == 0, gc_cli.output

    active_after_gc = _read_yaml(book_dir / "03_state" / "active" / "characters.yaml")
    archive_after_gc = _read_yaml(book_dir / "03_state" / "archive" / "characters.yaml")
    assert "char_mentor_su" not in active_after_gc
    assert "char_mentor_su" in archive_after_gc
    for idx_num in range(1, 26):
        eid = f"char_v1_npc_{idx_num:02d}"
        assert eid not in active_after_gc
        assert eid in archive_after_gc
    # 未完结伏笔保护期内的实体保留在活跃热池
    assert "char_protected_spy" in active_after_gc
    assert "char_protected_spy" not in archive_after_gc

    active_rels = _read_yaml(book_dir / "03_state" / "relations.yaml")["relations"]
    archived_rels = _read_yaml(book_dir / "03_state" / "archive" / "relations.yaml")["relations"]
    assert len(archived_rels) == 2
    assert {r["relation"] for r in active_rels} == {"归墟暂结血盟", "戴罪协防"}

    # 2) 主动弃坑物理移除 (先 dry-run 再物理移除)
    drop_dry = cli_runner.invoke(
        app, ["thread", "drop", "thread_abandoned_sidequest", "--dry-run", "-d", str(book_dir)]
    )
    assert drop_dry.exit_code == 0, drop_dry.output
    assert any(
        t["id"] == "thread_abandoned_sidequest"
        for t in _read_yaml(book_dir / "03_state" / "threads.yaml")["threads"]
    )

    drop_cli = cli_runner.invoke(
        app, ["thread", "drop", "thread_abandoned_sidequest", "-d", str(book_dir)]
    )
    assert drop_cli.exit_code == 0, drop_cli.output
    assert all(
        t["id"] != "thread_abandoned_sidequest"
        for t in _read_yaml(book_dir / "03_state" / "threads.yaml")["threads"]
    )

    # 3) 废案孤儿与远期龙套物理剪枝 (先 dry-run 再 apply)
    prune_dry = cli_runner.invoke(
        app, ["setting", "prune", "--current-chapter", "105", "-d", str(book_dir)]
    )
    assert prune_dry.exit_code == 0, prune_dry.output
    assert (book_dir / "01_settings" / "characters" / "char_orphan_draft.md").is_file()

    prune_apply = cli_runner.invoke(
        app, ["setting", "prune", "--apply", "--current-chapter", "105", "-d", str(book_dir)]
    )
    assert prune_apply.exit_code == 0, prune_apply.output
    assert not (book_dir / "01_settings" / "characters" / "char_orphan_draft.md").exists()
    assert not (book_dir / "01_settings" / "geography" / "loc_orphan_island.md").exists()
    assert not (book_dir / "01_settings" / "items" / "item_orphan_bell.md").exists()
    for cid, _ in cameo_specs:
        assert not (book_dir / "01_settings" / "characters" / f"{cid}.md").exists()

    # 4) 远期完结卷事实蒸馏浓缩
    compact_cli = cli_runner.invoke(app, ["history", "compact", "vol_01", "-d", str(book_dir)])
    assert compact_cli.exit_code == 0, compact_cli.output
    compacted_lines = [
        ln
        for ln in (book_dir / "03_state" / "history" / "vol_01_facts.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if ln.strip()
    ]
    assert 15 <= len(compacted_lines) <= 20
    for ln in compacted_lines:
        ChapterFactRecord.model_validate_json(ln)

    # ==================================================================
    # Phase 5 & 6 & 7: FastMCP 异步双通道创作闭环 + 门禁拦截 + 解锁 + 冷库唤醒
    # ==================================================================
    async def _run_mcp_loop() -> None:
        server = create_server(book_dir=book_dir)

        # -- 改设定五维全量影响面扫描 & 前缀消歧校验 --
        shenxiao_impact = _extract_mcp_payload(
            await server.call_tool(
                "novel_setting_impact",
                {"target_id_or_keyword": "char_shenxiao"},
                run_middleware=False,
            )
        )
        assert shenxiao_impact["ok"] is True
        assert shenxiao_impact["is_safe_to_change"] is False
        assert {"manuscript", "fact", "state", "thread", "blueprint"} <= set(
            shenxiao_impact["scope_counts"].keys()
        )

        linfan_impact = _extract_mcp_payload(
            await server.call_tool(
                "novel_setting_impact",
                {"target_id_or_keyword": "char_linfan"},
                run_middleware=False,
            )
        )
        assert linfan_impact["ok"] is True
        assert all(hit.get("chapter_num") != 55 for hit in linfan_impact["hits"])

        # -- 第 106 章伏笔雷达（CLI + MCP 双通道） --
        radar_cli = cli_runner.invoke(app, ["radar", "--chapter", "106", "-d", str(book_dir)])
        assert radar_cli.exit_code == 0, radar_cli.output

        radar_mcp = _extract_mcp_payload(
            await server.call_tool(
                "novel_foreshadow_radar",
                {"target_chapter": 106},
                run_middleware=False,
            )
        )
        assert radar_mcp["ok"] is True
        radar_by_id = {h["thread"]["id"]: h for h in radar_mcp["hits"]}
        assert "thread_ancient_seal" in radar_by_id
        assert {"window", "entity"} <= set(radar_by_id["thread_ancient_seal"]["reasons"])
        assert "thread_stagnant_jade" in radar_by_id
        assert "stale" in radar_by_id["thread_stagnant_jade"]["reasons"]
        assert "thread_abandoned_sidequest" not in radar_by_id

        # -- 第 106 章：紧缩预算裁剪验证（保护首尾槽位，仅裁剪中部） --
        tight_pkt = _extract_mcp_payload(
            await server.call_tool(
                "novel_build_packet",
                {"chapter": 106, "budget": 900, "include_content": True},
                run_middleware=False,
            )
        )
        assert tight_pkt["ok"] is True
        slot_by_name = {s["name"]: s for s in tight_pkt["slots"]}
        assert slot_by_name[SlotName.GUARDRAILS.value]["trimmed"] is False
        assert slot_by_name[SlotName.BEATS.value]["trimmed"] is False
        assert any(
            slot_by_name[s]["trimmed"] is True
            for s in (
                SlotName.WORLD_AND_FACTS.value,
                SlotName.ENTITIES.value,
                SlotName.CONTINUITY.value,
            )
        )

        # -- 第 106 章：标准预算编译（CLI + MCP）与 U 型位置 + 三重物理隔离校验 --
        pkt_cli = cli_runner.invoke(app, ["packet", "build", "106", "-d", str(book_dir)])
        assert pkt_cli.exit_code == 0, pkt_cli.output

        pkt_106 = _extract_mcp_payload(
            await server.call_tool(
                "novel_build_packet",
                {"chapter": 106, "include_content": True},
                run_middleware=False,
            )
        )
        assert pkt_106["ok"] is True
        content_106 = pkt_106["content"]
        assert (book_dir / "04_creation" / "packets" / "ch_0106_packet.md").is_file()

        # 校验 U 型五槽位严格先后顺序
        slot_headers = [
            "槽位 1 · 顶部强注意区",
            "槽位 2 · 中前背景区",
            "槽位 3 · 中部实体截面区",
            "槽位 4 · 中后文气衔接区",
            "槽位 5 · 尾部最高注意指令区",
        ]
        positions = [content_106.index(h) for h in slot_headers]
        assert positions == sorted(positions)

        # 校验按需专题精准挂载（请求剑道神通，未请求身世渊源）
        assert "太虚剑意第三重" in content_106
        assert "幼年出身南荒小村" not in content_106

        # 校验上章末尾文气衔接
        assert "沈霄握紧断刃望向黑雾深处" in content_106

        # 三重物理隔离硬断言
        assert "char_linfan" in pkt_106["blocked_secrets"]
        assert "荒古圣体血脉逆转天命" not in content_106
        assert "黑印下镇压初代祖师尸骸" not in content_106
        assert "南疆古墓残图" not in content_106
        assert "苏玄衣" not in content_106
        assert "赵铁柱" not in content_106

        # -- 第 106 章：注入违规草稿触发三级审计门禁拦截 --
        bad_draft = _extract_mcp_payload(
            await server.call_tool(
                "novel_save_draft",
                {
                    "chapter": 106,
                    "content": (
                        "# 第一百零六章 深渊探印\n\n"
                        "林凡剑光大作，突然一键暴走劈开迷雾。\n"
                        "早已身亡的赵铁柱竟然站在断崖边，而闭关多年的苏玄衣也悄然现身。\n"
                    ),
                },
                run_middleware=False,
            )
        )
        assert bad_draft["ok"] is True

        audit_bad_cli = cli_runner.invoke(app, ["audit", "106", "-d", str(book_dir)])
        assert audit_bad_cli.exit_code == 1
        promote_bad_cli = cli_runner.invoke(
            app, ["chapter", "promote", "106", "--volume-id", "vol_02", "-d", str(book_dir)]
        )
        assert promote_bad_cli.exit_code == 1

        audit_bad_mcp = _extract_mcp_payload(
            await server.call_tool(
                "novel_audit_consistency",
                {"chapter": 106},
                run_middleware=False,
            )
        )
        assert audit_bad_mcp["ok"] is True
        assert audit_bad_mcp["passed"] is False
        issue_codes = {iss["code"] for iss in audit_bad_mcp["issues"]}
        assert {
            "DECEASED_ENTITY_APPEARANCE",
            "ARCHIVED_ENTITY_UNAWAKENED",
            "BANNED_WORD_HIT",
        } <= issue_codes

        # -- 修复为合规草稿 -> 审计通过 -> 定稿入库（触发 TTL 清理 ch_0080）-> 状态结算 --
        good_draft = _extract_mcp_payload(
            await server.call_tool(
                "novel_save_draft",
                {
                    "chapter": 106,
                    "content": (
                        "# 第一百零六章 深渊探印\n\n"
                        "林凡催动太虚剑意，剑锋点在黑印边缘的玄奥符纹之上。\n"
                        "沈霄挥动断刃封住侧翼魔风，两人合力斩断三道太古锁链，青铜巨门应声微启。\n"
                    ),
                },
                run_middleware=False,
            )
        )
        assert good_draft["ok"] is True

        assert cli_runner.invoke(app, ["audit", "106", "-d", str(book_dir)]).exit_code == 0
        promote_ok_cli = cli_runner.invoke(
            app, ["chapter", "promote", "106", "--volume-id", "vol_02", "-d", str(book_dir)]
        )
        assert promote_ok_cli.exit_code == 0, promote_ok_cli.output
        assert (book_dir / "04_creation" / "manuscript" / "vol_02" / "ch_0106.md").is_file()
        assert not expired_pkt.exists(), "TTL=5 应自动清理第 80 章过期写作包"
        assert not expired_draft.exists(), "TTL=5 应自动清理第 80 章过期草稿"

        outcome_106 = _extract_mcp_payload(
            await server.call_tool(
                "novel_record_chapter_outcome",
                {
                    "chapter": 106,
                    "volume_id": "vol_02",
                    "summary": "林凡与沈霄在归墟合力斩断三道锁链，黑印松动。",
                    "facts": ["林凡与沈霄联合试探并斩断归墟黑印三道太古锁链"],
                    "entities": ["char_linfan", "char_shenxiao", "loc_abyss"],
                    "thread_updates": [
                        {
                            "id": "thread_ancient_seal",
                            "status": "active",
                            "last_progressed_at": 106,
                        }
                    ],
                },
                run_middleware=False,
            )
        )
        assert outcome_106["ok"] is True

        # -- 第 107 章：显式 unlock_secrets 解锁机密 + 伏笔回收 --
        pkt_107 = _extract_mcp_payload(
            await server.call_tool(
                "novel_build_packet",
                {"chapter": 107, "include_content": True},
                run_middleware=False,
            )
        )
        assert pkt_107["ok"] is True
        content_107 = pkt_107["content"]
        assert "荒古圣体血脉逆转天命" in content_107
        assert "黑印下镇压初代祖师尸骸" in content_107

        _extract_mcp_payload(
            await server.call_tool(
                "novel_save_draft",
                {
                    "chapter": 107,
                    "content": (
                        "# 第一百零七章 圣体破封\n\n"
                        "林凡体内金色血脉轰然觉醒，一剑劈开黑印核心，终于看清深渊下沉睡的初代祖师遗骨。\n"
                        "沈霄收刀伫立，眼中闪过一丝释然。\n"
                    ),
                },
                run_middleware=False,
            )
        )
        audit_107 = _extract_mcp_payload(
            await server.call_tool(
                "novel_audit_consistency",
                {"chapter": 107},
                run_middleware=False,
            )
        )
        assert audit_107["passed"] is True

        promote_107 = _extract_mcp_payload(
            await server.call_tool(
                "novel_promote_chapter",
                {"chapter": 107, "volume_id": "vol_02", "run_audit": True},
                run_middleware=False,
            )
        )
        assert promote_107["ok"] is True

        outcome_107 = _extract_mcp_payload(
            await server.call_tool(
                "novel_record_chapter_outcome",
                {
                    "chapter": 107,
                    "volume_id": "vol_02",
                    "summary": "林凡觉醒荒古圣体破开归墟黑印，太古封魔隐秘尘埃落定。",
                    "facts": ["林凡觉醒荒古圣体血脉并彻底破开归墟黑印"],
                    "entities": ["char_linfan", "char_shenxiao", "loc_abyss"],
                    "thread_updates": [
                        {
                            "id": "thread_ancient_seal",
                            "status": "resolved",
                            "last_progressed_at": 107,
                        }
                    ],
                },
                run_middleware=False,
            )
        )
        assert outcome_107["ok"] is True

        # -- 第 108 章：冷库角色（阔别 68 章）唤醒回归双通道闭环 --
        before_wake = _extract_mcp_payload(
            await server.call_tool(
                "novel_inspect_entity",
                {"entity_id": "char_mentor_su"},
                run_middleware=False,
            )
        )
        assert before_wake["is_archived"] is True

        # 唤醒前审计第 108 章蓝图，应给出 ARCHIVED_ENTITY_AWAKENED 提示警告
        pre_wake_audit = _extract_mcp_payload(
            await server.call_tool(
                "novel_audit_consistency",
                {"chapter": 108},
                run_middleware=False,
            )
        )
        assert pre_wake_audit["passed"] is True
        assert any(i["code"] == "ARCHIVED_ENTITY_AWAKENED" for i in pre_wake_audit["issues"])

        # 通道 A: FastMCP 唤醒 char_mentor_su
        wake_mcp = _extract_mcp_payload(
            await server.call_tool(
                "novel_record_chapter_outcome",
                {
                    "chapter": 108,
                    "volume_id": "vol_02",
                    "summary": "苏玄衣破关出山，携旧部韩青锋千里驰援归墟深渊。",
                    "facts": [
                        "苏玄衣破关出山，境界稳固在元婴后期",
                        "韩青锋率青云旧部护法并随苏玄衣赶赴归墟",
                        "林凡于归墟北境门户列阵接应师徒汇合",
                    ],
                    "entities": ["char_linfan", "char_mentor_su", "char_v1_npc_15", "loc_abyss"],
                    "character_updates": {
                        "char_mentor_su": {
                            "location": "loc_abyss",
                            "physical_mental": "破关复出，剑气冲霄",
                        }
                    },
                },
                run_middleware=False,
            )
        )
        assert wake_mcp["ok"] is True

        # 通道 B: CLI 唤醒 char_v1_npc_15
        wake_cli = cli_runner.invoke(
            app,
            [
                "state",
                "update",
                "--chapter",
                "108",
                "--char-state",
                json.dumps(
                    {
                        "char_v1_npc_15": {
                            "location": "loc_abyss",
                            "physical_mental": "率众驰援",
                        }
                    },
                    ensure_ascii=False,
                ),
                "-d",
                str(book_dir),
            ],
        )
        assert wake_cli.exit_code == 0, wake_cli.output

        # 校验冷库 YAML 已移除、热池已回归且扩展字段无损保留
        act_chars = _read_yaml(book_dir / "03_state" / "active" / "characters.yaml")
        arch_chars = _read_yaml(book_dir / "03_state" / "archive" / "characters.yaml")
        assert "char_mentor_su" in act_chars and "char_mentor_su" not in arch_chars
        assert "char_v1_npc_15" in act_chars and "char_v1_npc_15" not in arch_chars
        assert act_chars["char_mentor_su"]["profile_ext"]["key_items"] == ["玄衣古剑"]
        assert act_chars["char_mentor_su"]["profile_ext"]["realm"] == "元婴后期"

        # 增量同步 + 全量重建后均保持热池活跃状态 is_archived=False
        for mode in ("sync", "rebuild"):
            assert cli_runner.invoke(app, ["index", mode, "-d", str(book_dir)]).exit_code == 0
            su_insp = _extract_mcp_payload(
                await server.call_tool(
                    "novel_inspect_entity",
                    {"entity_id": "char_mentor_su", "auto_sync": False},
                    run_middleware=False,
                )
            )
            npc15_insp = _extract_mcp_payload(
                await server.call_tool(
                    "novel_inspect_entity",
                    {"entity_id": "char_v1_npc_15", "auto_sync": False},
                    run_middleware=False,
                )
            )
            assert su_insp["is_archived"] is False
            assert npc15_insp["is_archived"] is False

        # 编译第 108 章写作包 & 保存草稿 & 零警告零错误通过审计并定稿
        pkt_108 = _extract_mcp_payload(
            await server.call_tool(
                "novel_build_packet",
                {"chapter": 108, "include_content": True},
                run_middleware=False,
            )
        )
        assert pkt_108["ok"] is True
        assert "破关复出，剑气冲霄" in pkt_108["content"]
        assert "玄衣古剑" in pkt_108["content"]

        _extract_mcp_payload(
            await server.call_tool(
                "novel_save_draft",
                {
                    "chapter": 108,
                    "content": (
                        "# 第一百零八章 剑尊破关\n\n"
                        "九霄风雷骤散，苏玄衣执玄衣古剑踏空而至，身后韩青锋率青云弟子结阵护法。\n"
                        "林凡长揖行礼，师徒共守归墟北境门户。\n"
                    ),
                },
                run_middleware=False,
            )
        )
        final_audit_108 = _extract_mcp_payload(
            await server.call_tool(
                "novel_audit_consistency",
                {"chapter": 108},
                run_middleware=False,
            )
        )
        assert final_audit_108["passed"] is True
        assert final_audit_108["error_count"] == 0
        assert final_audit_108["warning_count"] == 0

        promote_108 = _extract_mcp_payload(
            await server.call_tool(
                "novel_promote_chapter",
                {"chapter": 108, "volume_id": "vol_02", "run_audit": True},
                run_middleware=False,
            )
        )
        assert promote_108["ok"] is True
        assert (book_dir / "04_creation" / "manuscript" / "vol_02" / "ch_0108.md").is_file()

    asyncio.run(_run_mcp_loop())
    shutil.rmtree(book_dir)


# ---------------------------------------------------------------------------
# E2E 测试 2: 多书库热切换隔离、多题材零耦合、离线向量 RRF 与 Windows 句柄释放
# ---------------------------------------------------------------------------


def test_e2e_multi_book_decoupling_and_vector_hybrid_rrf(tmp_path: Path) -> None:
    """验证共享 FastMCP 服务的多书库物理隔离、多题材扩展校验与离线向量 RRF 检索。"""
    book_general = tmp_path / "book_general"
    book_mystery = tmp_path / "book_mystery"

    init_book(book_general, title="都市风云录", genre_profile="general", include_samples=False)
    init_book(book_mystery, title="雾都迷局", genre_profile="mystery_intrigue", include_samples=False)

    # general 题材支持任意自由扩展字段
    _write_md(
        book_general / "01_settings" / "characters" / "char_ceo.md",
        {"id": "char_ceo", "name": "周明远", "category": "character", "status": "active"},
        "## [核心]\n星海科技董事长，专注芯片供应链并购。\n",
    )
    _write_yaml(
        book_general / "03_state" / "active" / "characters.yaml",
        {
            "char_ceo": {
                "updated_at_ch": 1,
                "physical_mental": "精力充沛",
                "profile_ext": {"company": "星海科技", "credit_score": 820},
            }
        },
    )

    # mystery_intrigue 题材开启离线 DeterministicHashEmbedder + Disguise/Clue 强校验
    mystery_cfg = NovelConfig.load(book_mystery).model_copy(
        update={
            "current_chapter": 12,
            "embedding": EmbeddingConfig(
                enabled=True,
                provider="hash",
                model="hash-v1",
                dim=64,
                rrf_k=60,
            ),
        }
    )
    mystery_cfg.dump(book_mystery)

    _write_md(
        book_mystery / "01_settings" / "characters" / "char_detective.md",
        {
            "id": "char_detective",
            "name": "陆沉",
            "aliases": ["钟表匠"],
            "category": "character",
            "status": "active",
        },
        "## [核心]\n隐居雾都钟表行的前刑侦顾问，擅从微物线索推演不在场证明。\n",
    )
    _write_yaml(
        book_mystery / "03_state" / "active" / "characters.yaml",
        {
            "char_detective": {
                "updated_at_ch": 12,
                "physical_mental": "夜间勘验略有疲惫",
                "profile_ext": {
                    "disguises": [{"identity": "老街钟表匠", "known_by": ["char_detective"]}],
                    "clues_held": [
                        {
                            "id": "clue_brass_key",
                            "content": "码头仓库遗落的黄铜钥匙刻有三点星记",
                            "found_at_ch": 12,
                            "points_to": [],
                        }
                    ],
                    "suspicions": ["港务局账簿曾遭夜间篡改"],
                },
            }
        },
    )
    (book_mystery / "03_state" / "history" / "vol_01_facts.jsonl").write_text(
        ChapterFactRecord(
            ch=12,
            volume_id="vol_01",
            summary="陆沉在码头仓库提取到三点星记黄铜钥匙。",
            facts=["陆沉发现刻有三点星记的黄铜钥匙并怀疑港务局账簿被篡改"],
            entities=["char_detective"],
        ).model_dump_json(by_alias=True, exclude_none=True)
        + "\n",
        encoding="utf-8",
    )

    async def _run_multi_book() -> None:
        # 共享同一个未绑定默认书库的 Server 实例
        server = create_server()

        for bdir in (book_general, book_mystery):
            sync_res = _extract_mcp_payload(
                await server.call_tool(
                    "novel_sync_index",
                    {"force_rebuild": True, "book_dir": str(bdir)},
                    run_middleware=False,
                )
            )
            assert sync_res["ok"] is True

        # general 书库与 mystery 书库均通过严格体检
        for bdir in (book_general, book_mystery):
            aud = _extract_mcp_payload(
                await server.call_tool(
                    "novel_audit_consistency",
                    {"book_dir": str(bdir)},
                    run_middleware=False,
                )
            )
            assert aud["passed"] is True

        # mystery 书库执行 hybrid=True (FTS5 + DeterministicHashEmbedder RRF)
        hybrid_hit = _extract_mcp_payload(
            await server.call_tool(
                "novel_search",
                {
                    "query": "黄铜钥匙 钟表匠",
                    "hybrid": True,
                    "top_k": 5,
                    "book_dir": str(book_mystery),
                },
                run_middleware=False,
            )
        )
        assert hybrid_hit["ok"] is True
        assert hybrid_hit["hybrid"] is True
        assert hybrid_hit["count"] >= 1
        assert any(
            "char_detective" in (h.get("entity_id") or "") or "钥匙" in h["excerpt"]
            for h in hybrid_hit["hits"]
        )

        # 验证两本书检索零串味
        cross_general = _extract_mcp_payload(
            await server.call_tool(
                "novel_search",
                {"query": "黄铜钥匙", "hybrid": False, "book_dir": str(book_general)},
                run_middleware=False,
            )
        )
        assert cross_general["count"] == 0

        cross_mystery = _extract_mcp_payload(
            await server.call_tool(
                "novel_search",
                {"query": "星海科技", "hybrid": True, "book_dir": str(book_mystery)},
                run_middleware=False,
            )
        )
        # FTS5 不命中星海科技（RRF 纯向量余弦也不会出现星海科技原文）
        assert all("星海科技" not in h["excerpt"] for h in cross_mystery["hits"])

    asyncio.run(_run_multi_book())

    # 校验 SQLite embeddings 表已落盘且连接关闭后无 Windows 文件锁残留
    with SqliteIndexer(book_mystery) as idx:
        emb_count = idx.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        assert int(emb_count) >= 1

    shutil.rmtree(book_general)
    shutil.rmtree(book_mystery)


# ---------------------------------------------------------------------------
# E2E 测试 3: 1,000 章（>=300万汉字 + 5000事实 + 500设定）三大 NFR 性能 SLA 红线验收
# ---------------------------------------------------------------------------


def test_e2e_thousand_chapter_nfr_performance_sla(tmp_path: Path) -> None:
    """压测千章千万字节级别大书库：重建 < 3000ms、单章增量 < 200ms、写作包编译 < 300ms。"""
    book_dir = tmp_path / "thousand_chapters_sla"
    init_book(
        book_dir,
        title="千章性能压测录",
        author="性能基准测试",
        genre_profile="cultivation_rpg",
        include_samples=False,
    )

    cfg = NovelConfig.load(book_dir).model_copy(update={"current_chapter": 1000})
    cfg.dump(book_dir)

    _write_yaml(
        book_dir / "03_state" / "world_state.yaml",
        {
            "current_chapter": 1000,
            "in_world_time": "九劫历千年",
            "macro_situation": "十域归一，灵潮奔涌，太虚神殿现世。",
        },
    )
    _write_md(
        book_dir / "04_creation" / "style_guide.md",
        {"tone": "宏大古典", "pov_rules": ["第三人称主角限知视角"], "banned_words": ["出戏词汇"]},
        "行文流畅大气，契合长篇修仙史诗质感。\n",
    )

    # 1) 500 个设定卡片（覆盖五大分类目录，含核心/专题/机密切片）
    cat_dirs = [
        ("characters", "character", "char_bench"),
        ("factions", "faction", "fac_bench"),
        ("geography", "geography", "loc_bench"),
        ("items", "item", "item_bench"),
        ("rules", "rule", "rule_bench"),
    ]
    for idx_cat, (sub_dir, cat_name, id_prefix) in enumerate(cat_dirs):
        target_dir = book_dir / "01_settings" / sub_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        for j in range(100):
            eid = f"{id_prefix}_{j:03d}"
            title = (
                "顾长青"
                if (idx_cat == 0 and j == 0)
                else ("苏晚晴" if (idx_cat == 0 and j == 1) else (
                    "太虚神殿" if (idx_cat == 2 and j == 0) else f"基准设定{idx_cat}_{j:03d}"
                ))
            )
            fm_yaml = (
                f"---\n"
                f"id: {eid}\n"
                f"name: {title}\n"
                f"category: {cat_name}\n"
                f"status: active\n"
                f"---\n\n"
                f"## [核心]\n"
                f"{title}的核心背景描述与行为纲领条目。\n\n"
                f"## [专题：详情]\n"
                f"{title}在长期剧情演化中的补充专题档案。\n\n"
                f"## [机密]\n"
                f"{title}尚未向读者揭晓的底层隐秘真相。\n"
            )
            (target_dir / f"{eid}.md").write_text(fm_yaml, encoding="utf-8")

    # 2) 状态截面、关系、伏笔与分卷大纲
    active_chars: dict[str, Any] = {}
    for j in range(30):
        active_chars[f"char_bench_{j:03d}"] = {
            "updated_at_ch": 1000 if j < 5 else 985,
            "location": "loc_bench_000",
            "physical_mental": "状态稳固，战意高昂",
            "current_goals": ["探索太虚神殿核心区域"],
            "profile_ext": {"realm": "渡劫初期", "techniques": ["长青剑诀"]},
        }
    _write_yaml(book_dir / "03_state" / "active" / "characters.yaml", active_chars)

    archive_chars: dict[str, Any] = {}
    for j in range(30, 100):
        archive_chars[f"char_bench_{j:03d}"] = {
            "updated_at_ch": 200,
            "location": "loc_bench_000",
            "physical_mental": "已退场",
            "current_goals": [],
        }
    _write_yaml(book_dir / "03_state" / "archive" / "characters.yaml", archive_chars)

    _write_yaml(
        book_dir / "03_state" / "active" / "factions.yaml",
        {
            "fac_bench_000": {
                "updated_at_ch": 1000,
                "status": "active",
                "leader": "char_bench_000",
                "territory": ["loc_bench_000"],
                "current_goals": ["统御北境灵脉"],
            }
        },
    )
    _write_yaml(
        book_dir / "03_state" / "relations.yaml",
        {
            "relations": [
                {
                    "source": "char_bench_000",
                    "target": "char_bench_001",
                    "relation": "生死道侣",
                    "valid_from_ch": 1,
                    "invalid_at_ch": None,
                },
                {
                    "source": "char_bench_000",
                    "target": "fac_bench_000",
                    "relation": "宗门支柱",
                    "valid_from_ch": 100,
                    "invalid_at_ch": None,
                },
            ]
        },
    )
    _write_yaml(
        book_dir / "03_state" / "threads.yaml",
        {
            "threads": [
                {
                    "id": "thread_taixu_gate",
                    "title": "太虚神殿开殿密匙",
                    "category": "主线核心",
                    "status": "active",
                    "planted_at": 120,
                    "last_progressed_at": 995,
                    "expected_window": [1000, 1010],
                    "trigger_entities": ["char_bench_000", "loc_bench_000"],
                    "public_clue": "九枚古符汇聚方能开启神殿外门。",
                    "truth_secret": "神殿实为飞升古阵中枢",
                }
            ]
        },
    )

    _write_md(
        book_dir / "02_structure" / "volumes" / "vol_10.md",
        {
            "id": "vol_10",
            "title": "第十卷 飞升问鼎",
            "chapter_range": [901, 1020],
            "core_conflict": "太虚神殿终局之战",
        },
        "第十卷纲要。\n",
    )
    _write_md(
        book_dir / "02_structure" / "arcs" / "vol_10_arc_01.md",
        {"id": "vol_10_arc_01", "volume_id": "vol_10", "title": "神殿登阶", "chapter_range": [950, 1020]},
        "当前核心单元。\n",
    )
    _write_md(
        book_dir / "02_structure" / "blueprints" / "vol_10" / "ch_1001.md",
        {
            "chapter": 1001,
            "volume_id": "vol_10",
            "arc_id": "vol_10_arc_01",
            "title": "登临太虚殿",
            "pov": "char_bench_000",
            "cast": {
                "characters": ["char_bench_000", "char_bench_001"],
                "locations": ["loc_bench_000"],
                "factions": ["fac_bench_000"],
            },
            "threads": [{"thread_id": "thread_taixu_gate", "action": "progress"}],
            "topics": [{"entity_id": "char_bench_000", "topic": "详情"}],
            "forbidden": ["禁止提前揭晓飞升古阵中枢真相"],
        },
        "### 节拍 1: 殿前集符\n顾长青与苏晚晴汇合诸强，准备以古符开启太虚神殿外门。\n",
    )

    # 3) 10 卷共 1,000 章正文（总字数 >= 3,000,000 汉字） + 1,000 条记录 x 5 条事实 = 5,000 条原子事实
    para_block = (
        "暮色漫过太虚灵脉千峰，灵气如潮汐翻涌不息。顾长青御剑穿云而行，袖中青锋清鸣作响，"
        "远处神殿飞檐在苍茫罡风中若隐若现，诸多修士正自四海八荒奔赴北域汇聚风云。"
    )
    # 约 77 字 x 40 遍 ≈ 3,080 汉字 / 章 -> 1,000 章 > 3,080,000 汉字
    chapter_body_template = "\n".join([para_block] * 40)
    total_chars = 0

    for vol_idx in range(1, 11):
        vol_id = f"vol_{vol_idx:02d}"
        ms_dir = book_dir / "04_creation" / "manuscript" / vol_id
        ms_dir.mkdir(parents=True, exist_ok=True)
        fact_lines: list[str] = []

        start_ch = (vol_idx - 1) * 100 + 1
        end_ch = vol_idx * 100
        for ch in range(start_ch, end_ch + 1):
            text = (
                f"# 第{ch}章 灵潮奔涌\n\n"
                f"（第{ch}章卷内纪事：顾长青与苏晚晴立于太虚神殿石阶之下。）\n"
                f"{chapter_body_template}\n"
            )
            total_chars += len(text)
            (ms_dir / f"{format_chapter(ch)}.md").write_text(text, encoding="utf-8")

            fact_obj = {
                "ch": ch,
                "volume_id": vol_id,
                "summary": f"第{ch}章摘要：顾长青在太虚灵域推进修行并搜集古符线索。",
                "facts": [
                    f"第{ch}章事实一：顾长青推演剑阵",
                    f"第{ch}章事实二：苏晚晴探明灵脉",
                    f"第{ch}章事实三：太虚神殿微光异动",
                    f"第{ch}章事实四：北域群雄暗中汇聚",
                    f"第{ch}章事实五：灵潮浓度显著抬升",
                ],
                "entities": ["char_bench_000", "char_bench_001", "loc_bench_000"],
            }
            fact_lines.append(json.dumps(fact_obj, ensure_ascii=False))

        (book_dir / "03_state" / "history" / f"{vol_id}_facts.jsonl").write_text(
            "\n".join(fact_lines) + "\n", encoding="utf-8"
        )

    assert total_chars >= 3_000_000, f"语料规模不足 300 万字符: {total_chars}"

    # 跨越 50ms racy-git 保护窗口，确保所有初始文件的 mtime_ns 均落在重建基准之前
    time.sleep(0.08)

    with SqliteIndexer(book_dir) as indexer:
        # SLA 1: 零污染全量索引重建 < 3,000 ms
        rebuild_rep = indexer.rebuild()
        assert rebuild_rep.rebuilt is True
        print(f"\n[NFR 1000ch] rebuild={rebuild_rep.elapsed_ms:.1f}ms (SLA < 3000.0ms)")
        assert rebuild_rep.elapsed_ms < 3000.0, (
            f"千章全量重建耗时超标: {rebuild_rep.elapsed_ms:.1f}ms >= 3000.0ms"
        )

        # SLA 2: 单文件改动增量热同步 < 200 ms
        time.sleep(0.08)
        ch1000_rel = "04_creation/manuscript/vol_10/ch_1000.md"
        ch1000_path = book_dir / ch1000_rel
        ch1000_path.write_text(
            ch1000_path.read_text(encoding="utf-8")
            + "\n第1000章结尾补录：青铜石阶尽头忽然亮起九枚太古符印的共鸣光柱。\n",
            encoding="utf-8",
        )
        sync_rep = indexer.sync()
        print(f"[NFR 1000ch] single_sync={sync_rep.elapsed_ms:.1f}ms (SLA < 200.0ms)")
        assert sync_rep.updated == [ch1000_rel]
        assert sync_rep.elapsed_ms < 200.0, (
            f"千章单章增量同步耗时超标: {sync_rep.elapsed_ms:.1f}ms >= 200.0ms"
        )

        # SLA 3: 单章独立写作包编译 < 300 ms
        builder = PacketBuilder(book_dir, indexer=indexer, auto_sync=False)
        t0 = time.perf_counter()
        pkt_res = builder.build(1001)
        packet_ms = (time.perf_counter() - t0) * 1000.0
        print(f"[NFR 1000ch] packet_build={packet_ms:.1f}ms (SLA < 300.0ms)")
        assert (book_dir / pkt_res.packet_path).is_file()
        assert packet_ms < 300.0, (
            f"千章单章写作包编译耗时超标: {packet_ms:.1f}ms >= 300.0ms"
        )

    shutil.rmtree(book_dir)
