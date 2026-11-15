"""T08 单章写作包一步直出编译器 (PacketBuilder) 全量单元与集成测试。"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from ddnovel.core import (
    AliasMatcher,
    PacketBuilder,
    SqliteIndexer,
    build_packet,
    estimate_tokens,
)
from ddnovel.core.schemas import (
    Beat,
    BlueprintCast,
    BlueprintDoc,
    PacketBuildResult,
    SlotBudget,
    SlotName,
    ThreadAction,
    ThreadRef,
    TokenBudgetConfig,
)
from ddnovel.templates import ProjectInitializer

FIXTURES_BOOKS_DIR = Path(__file__).resolve().parent / "fixtures" / "books"


def _copy_fixture_book(genre_dir: str, tmp_path: Path) -> Path:
    dest = tmp_path / genre_dir
    shutil.copytree(FIXTURES_BOOKS_DIR / genre_dir, dest)
    return dest


# ---------------------------------------------------------------------------
# 1. 三题材样书端到端 U 型五槽位直出编译测试
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    (
        "genre_dir",
        "chapter_num",
        "expected_packet_rel",
        "expected_pov_unaware",
        "expected_topic_kw",
        "expected_rel_kw",
    ),
    [
        (
            "cultivation_rpg",
            142,
            "04_creation/packets/ch_0142_packet.md",
            "同行旅伴苏瑶实为北境妖庭圣女",
            "暗星地下拍卖场",
            "流亡弃徒",
        ),
        (
            "general",
            13,
            "04_creation/packets/ch_0013_packet.md",
            "韩诚退股前曾私下与远创资本签署过随售优先谅解函",
            "C座通宵实验室",
            "创始人兼首席架构师",
        ),
        (
            "mystery_intrigue",
            25,
            "04_creation/packets/ch_0025_packet.md",
            "顾晚晴便是上级派来接替周伯的特派联络员夜莺",
            "圣玛丽诊所掩护",
            "雾港海关缉私处查验科科长",
        ),
    ],
)
def test_build_packet_three_genres_e2e_u_shape(
    genre_dir: str,
    chapter_num: int,
    expected_packet_rel: str,
    expected_pov_unaware: str,
    expected_topic_kw: str,
    expected_rel_kw: str,
    tmp_path: Path,
) -> None:
    book_dir = _copy_fixture_book(genre_dir, tmp_path)

    result = build_packet(book_dir, chapter_num)
    assert isinstance(result, PacketBuildResult)
    assert result.chapter == chapter_num
    assert result.packet_path == expected_packet_rel

    packet_file = book_dir / expected_packet_rel
    assert packet_file.is_file()
    content = packet_file.read_text(encoding="utf-8")

    # 1. 严格五槽位 U 型顺序
    assert [s.name for s in result.slots] == [
        SlotName.GUARDRAILS,
        SlotName.WORLD_AND_FACTS,
        SlotName.ENTITIES,
        SlotName.CONTINUITY,
        SlotName.BEATS,
    ]
    slot_positions = [
        content.index("槽位 1"),
        content.index("槽位 2"),
        content.index("槽位 3"),
        content.index("槽位 4"),
        content.index("槽位 5"),
    ]
    assert slot_positions == sorted(slot_positions)

    # 2. Token 统计精度一致
    assert result.total_tokens == estimate_tokens(content)
    assert result.total_tokens > 0
    assert result.total_tokens <= result.budget_total

    # 3. 槽位 1 包含 POV 认知边界与文风红线
    slot2_start = content.index("槽位 2")
    slot1_text = content[:slot2_start]
    assert expected_pov_unaware in slot1_text
    assert "禁用套话红线" in slot1_text

    # 4. 槽位 2 包含按需请求的专题且未请求的无关专题不混入
    slot3_start = content.index("槽位 3")
    slot2_text = content[slot2_start:slot3_start]
    assert expected_topic_kw in slot2_text or expected_topic_kw in content

    # 5. 槽位 3 包含当前章有效双时态关系，不包含已失效旧边
    slot4_start = content.index("槽位 4")
    slot3_text = content[slot3_start:slot4_start]
    if genre_dir == "cultivation_rpg":
        assert expected_rel_kw in slot3_text
        assert "恪守门规尊师重道" not in content
        assert "嫉恨其剑道天资并欲杀人夺宝" not in content
        # 未请求的青云门专题 [专题: 剑冢禁制] 不应混入
        assert "九重惊雷剑意禁制" not in content
    elif genre_dir == "mystery_intrigue":
        assert expected_rel_kw in slot3_text or "缉私处" in slot3_text
    else:
        assert "远创资本" in slot3_text

    # 6. 槽位 4 包含近期摘要与上一章定稿末尾片段
    slot5_start = content.index("槽位 5")
    slot4_text = content[slot4_start:slot5_start]
    assert "摘要" in slot4_text
    assert "末尾正文衔接" in slot4_text

    # 7. 槽位 5 包含分场节拍与禁止事项
    slot5_text = content[slot5_start:]
    assert "Scene 1" in slot5_text
    assert "本章禁止事项" in slot5_text


# ---------------------------------------------------------------------------
# 2. [机密] 物理隔离与按需解锁单测
# ---------------------------------------------------------------------------


def test_secret_isolation_and_selective_unlock_cultivation(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("cultivation_rpg", tmp_path)

    with PacketBuilder(book_dir) as builder:
        # 默认 unlock_secrets=[]：所有 ## [机密] 100% 物理屏蔽
        res_locked = builder.build(142)
        locked_text = (book_dir / res_locked.packet_path).read_text(encoding="utf-8")

        secret_phrases = [
            "半滴玄冥真水",
            "白璃",
            "白璃圣女",
            "老妖皇密令",
            "互市密约",
            "冰渊秘境钥器半身",
            "剑冢底层水牢",
            "斩断飞升接引天梯",
            "九尾天狐本命精血护符",
            "镇族青铜古镜半身",
        ]
        for phrase in secret_phrases:
            assert phrase not in locked_text, f"机密词 '{phrase}' 不应出现在未解锁写作包中"

        assert "char_suyao" in res_locked.blocked_secrets
        assert "char_linfan" in res_locked.blocked_secrets
        assert "item_gujing" in res_locked.blocked_secrets
        assert "loc_beijing_gucheng" in res_locked.blocked_secrets

        # 显式解锁 char_suyao 的机密
        bp_path = book_dir / "02_structure" / "blueprints" / "vol_01" / "ch_0142.md"
        bp = builder._parser.parse_blueprint_md(bp_path, book_dir=book_dir)
        bp_unlocked = bp.model_copy(update={"unlock_secrets": ["char_suyao"]})

        res_unlocked = builder.build(142, blueprint=bp_unlocked, sync_index=False)
        unlocked_text = (book_dir / res_unlocked.packet_path).read_text(encoding="utf-8")

        assert "char_suyao" not in res_unlocked.blocked_secrets
        assert "白璃" in unlocked_text
        assert "老妖皇密令" in unlocked_text
        assert "[本章解锁机密]" in unlocked_text
        # 其余未解锁实体机密依然保持 100% 物理隔离
        assert "char_linfan" in res_unlocked.blocked_secrets
        assert "半滴玄冥真水" not in unlocked_text
        assert "剑冢底层水牢" not in unlocked_text


def test_secret_isolation_general_and_mystery(tmp_path: Path) -> None:
    # 1. general 书库机密隔离
    gen_dir = _copy_fixture_book("general", tmp_path)
    res_gen = build_packet(gen_dir, 13)
    gen_text = (gen_dir / res_gen.packet_path).read_text(encoding="utf-8")
    assert len(res_gen.blocked_secrets) >= 3
    # 读入所有设定卡的 secret_text，逐一核验无整段或核心短语泄漏
    with SqliteIndexer(gen_dir) as idx:
        idx.sync()
        sql = "SELECT id, secret_text FROM entities WHERE secret_text IS NOT NULL"
        for row in idx.conn.execute(sql).fetchall():
            sec = str(row[1]).strip()
            if sec:
                assert sec not in gen_text

    # 2. mystery_intrigue 书库机密与秘密马甲隔离
    mys_dir = _copy_fixture_book("mystery_intrigue", tmp_path)
    res_mys = build_packet(mys_dir, 25)
    mys_text = (mys_dir / res_mys.packet_path).read_text(encoding="utf-8")
    # 顾晚晴与罗振的 ## [机密] 与秘密马甲不得出现在 Slot 2~5
    assert "最终口令互认" not in mys_text
    assert "九箱特批军用盘尼西林秘密转运清单" not in mys_text
    assert "地下交通网特派重组联络员" not in mys_text
    assert "海外账户把柄" not in mys_text
    assert "黑潮特务机关潜伏内线" not in mys_text
    # “夜莺”仅允许作为 POV 未知红线([红线])出现在 Slot 1，绝不出现在 Slot 2~5 实体设定或状态中
    slot2_to_5 = mys_text[mys_text.index("槽位 2") :]
    assert "夜莺" not in slot2_to_5
    # 公开马甲应当保留
    assert "圣玛丽诊所外科主治医师顾晚晴" in mys_text
    assert "海关缉私处实权副处长" in mys_text


# ---------------------------------------------------------------------------
# 3. 伏笔物理隔离与 truth_secret 屏蔽单测
# ---------------------------------------------------------------------------


def test_thread_isolation_and_truth_secret_masking(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("cultivation_rpg", tmp_path)

    with PacketBuilder(book_dir) as builder:
        res = builder.build(142)
        text = (book_dir / res.packet_path).read_text(encoding="utf-8")

        # 1. 未勾选的休眠伏笔 100% 不入包，且记入 blocked_threads
        assert "thread_suyao_identity" in res.blocked_threads
        assert "thread_suyao_identity" not in text
        assert "同行医女苏瑶的真实跟脚" not in text

        # 2. 已归档完结伏笔 100% 不入包，且不记入 blocked_threads
        assert "thread_zhuixinggu_ambush" not in res.blocked_threads
        assert "thread_zhuixinggu_ambush" not in text

        # 3. 已勾选但动作为 progress 的伏笔：public_clue 入包，truth_secret 物理隔离
        assert "thread_gujing_origin" in text
        assert "二十八宿星纹" in text
        assert "thread_xuehunyin_curse" in text
        assert "怨气化印" in text
        assert "寻血罗盘" not in text
        assert "催动惊雷剑诀便会暴露精确方位" not in text

        # 4. 当动作改为 resolve 时，truth_secret 正确揭晓入包
        bp_path = book_dir / "02_structure" / "blueprints" / "vol_01" / "ch_0142.md"
        bp = builder._parser.parse_blueprint_md(bp_path, book_dir=book_dir)
        bp_resolve = bp.model_copy(
            update={
                "threads": [
                    ThreadRef(thread_id="thread_xuehunyin_curse", action=ThreadAction.RESOLVE),
                ]
            }
        )
        res_resolve = builder.build(142, blueprint=bp_resolve, sync_index=False)
        resolve_text = (book_dir / res_resolve.packet_path).read_text(encoding="utf-8")
        assert "寻血罗盘" in resolve_text
        assert "本章揭晓真相底牌" in resolve_text
        # thread_gujing_origin 此次未勾选，则也应进入 blocked_threads
        assert "thread_gujing_origin" in res_resolve.blocked_threads
        assert "thread_gujing_origin" not in resolve_text


# ---------------------------------------------------------------------------
# 4. 别名自动嗅探、冷库过滤与防时空穿越单测
# ---------------------------------------------------------------------------


def test_alias_sniffing_cold_archive_skip_and_no_future_leak(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("cultivation_rpg", tmp_path)

    # 在历史台账追加一条第 141 章(过去)与第 150 章(未来)的事实，测试严格 ch < 142 过滤
    hist_path = book_dir / "03_state" / "history" / "vol_01_facts.jsonl"
    with hist_path.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ch": 150,
                    "arc": "vol_01_arc_03",
                    "time": "天历四年春",
                    "summary": "未来第150章的未发生剧情摘要。",
                    "facts": ["林凡在第150章未来剧情中飞升北冥冰渊"],
                    "entities": ["char_linfan", "loc_beijing_gucheng"],
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    # 构造一个最小 cast 蓝图，但在节拍正文中通过外号提及“北冥边城”（活跃）和“血手赵乾”（冷库已死）
    bp = BlueprintDoc(
        chapter=142,
        volume_id="vol_01",
        arc_id="vol_01_arc_03",
        title="暗流试探",
        pov="char_linfan",
        cast=BlueprintCast(characters=["char_linfan"]),
        beats=[
            Beat(
                index=1,
                title="旧怨浮现",
                content="林凡站在北冥边城街角，想起当年坠星谷斩杀血手赵乾的旧事，掏出二品破障丹服下。",
            )
        ],
    )

    with PacketBuilder(book_dir) as builder:
        res = builder.build(142, blueprint=bp)
        text = (book_dir / res.packet_path).read_text(encoding="utf-8")

        # 活跃实体通过别名自动嗅探入包（loc_beijing_gucheng、item_pozhangdan）
        assert "loc_beijing_gucheng" in text
        assert "item_pozhangdan" in text

        # 冷库/已死实体 char_zhaoqian 仅在正文中被外号“血手赵乾”提及，未写入 cast -> 100% 跳过
        assert "char_zhaoqian" not in text

        # 严格禁止召回未来章 (ch >= 142) 事实
        assert "未来第150章" not in text
        assert "飞升北冥冰渊" not in text

        # 若在 cast 中显式点名冷库实体 char_zhaoqian（回忆返场），则按需加载并产生 warning
        bp_recall = bp.model_copy(
            update={
                "cast": BlueprintCast(characters=["char_linfan", "char_zhaoqian"]),
            }
        )
        res_recall = builder.build(142, blueprint=bp_recall, sync_index=False)
        recall_text = (book_dir / res_recall.packet_path).read_text(encoding="utf-8")
        assert "char_zhaoqian" in recall_text
        assert any("char_zhaoqian" in w and "冷库" in w for w in res_recall.warnings)


# ---------------------------------------------------------------------------
# 5. Token 预算超限裁剪与首尾保护铁律单测
# ---------------------------------------------------------------------------


def test_budget_trimming_priority_and_protected_ends(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("cultivation_rpg", tmp_path)

    # 写入多条远期历史事实（第 10~35 章）确保 Slot 2 事实较多，便于检验优先裁剪远期事实
    hist_path = book_dir / "03_state" / "history" / "vol_01_facts.jsonl"
    existing_lines = hist_path.read_text(encoding="utf-8").splitlines()
    extra_records = [
        json.dumps(
            {
                "ch": ch,
                "arc": "vol_01_arc_01",
                "time": "天历二年",
                "summary": f"第{ch}章林凡在落霞峰日常练剑与采购灵石的过渡事件记录。",
                "facts": [f"第{ch}章林凡在青云门外门坊市购入铁剑与低阶聚气散用于常规修行"],
                "entities": ["char_linfan", "fac_qingyunmen"],
            },
            ensure_ascii=False,
        )
        for ch in range(10, 26)
    ]
    hist_path.write_text("\n".join([*extra_records, *existing_lines]) + "\n", encoding="utf-8")

    with PacketBuilder(book_dir) as builder:
        full_res = builder.build(142)
        assert full_res.total_tokens > 3500

        # Case A: 收紧预算到 3500 Tokens -> 自动裁剪中间槽位（按 trim_order 优先裁减）
        tight_res = builder.build(142, custom_budget=3500, sync_index=False)
        tight_text = (book_dir / tight_res.packet_path).read_text(encoding="utf-8")

        assert tight_res.budget_total == 3500
        assert tight_res.total_tokens <= 3500
        assert tight_res.total_tokens == estimate_tokens(tight_text)

        slot_map = {s.name: s for s in tight_res.slots}
        assert slot_map[SlotName.GUARDRAILS].trimmed is False
        assert slot_map[SlotName.BEATS].trimmed is False
        assert any(
            slot_map[s].trimmed for s in (SlotName.WORLD_AND_FACTS, SlotName.CONTINUITY, SlotName.ENTITIES)
        )
        assert any(
            slot_map[s].dropped_items
            for s in (SlotName.WORLD_AND_FACTS, SlotName.CONTINUITY, SlotName.ENTITIES)
        )

        # Case B: 自定义 trim_order（优先裁剪 entities）
        custom_cfg = TokenBudgetConfig(
            total=3500,
            slots=SlotBudget(
                guardrails=700,
                world_and_facts=800,
                entities=600,
                continuity=600,
                beats=800,
            ),
            trim_order=["entities", "world_and_facts", "continuity"],
        )
        custom_res = builder.build(142, custom_budget=custom_cfg, sync_index=False)
        custom_slots = {s.name: s for s in custom_res.slots}
        assert custom_slots[SlotName.ENTITIES].trimmed is True
        assert custom_slots[SlotName.GUARDRAILS].trimmed is False
        assert custom_slots[SlotName.BEATS].trimmed is False
        assert custom_res.total_tokens <= 3500

        # Case C: 极端微小预算（如 80 Tokens）-> 中间三槽裁空，但首尾受保护槽位 100% 完整保留
        extreme_res = builder.build(142, custom_budget=80, sync_index=False)
        extreme_text = (book_dir / extreme_res.packet_path).read_text(encoding="utf-8")
        extreme_slots = {s.name: s for s in extreme_res.slots}

        assert extreme_slots[SlotName.GUARDRAILS].trimmed is False
        assert extreme_slots[SlotName.GUARDRAILS].dropped_items == []
        assert extreme_slots[SlotName.BEATS].trimmed is False
        assert extreme_slots[SlotName.BEATS].dropped_items == []
        assert "同行旅伴苏瑶实为北境妖庭圣女" in extreme_text
        assert "Scene 1" in extreme_text
        assert "城内严禁描写御剑升空斗法" in extreme_text
        assert any("受保护首尾槽位" in w for w in extreme_res.warnings)


# ---------------------------------------------------------------------------
# 6. 边界条件、连接生命周期与百章性能测试
# ---------------------------------------------------------------------------


def test_first_chapter_and_missing_optional_files(tmp_path: Path) -> None:
    book_dir = tmp_path / "fresh_book"
    ProjectInitializer().init_book(book_dir, title="开篇测试", genre_profile="general")

    # 删除可选文件验证优雅降级
    (book_dir / "04_创作" / "文风指南.md").unlink()
    (book_dir / "03_状态" / "世界状态.yaml").unlink()

    res = build_packet(book_dir, 1)
    text = (book_dir / res.packet_path).read_text(encoding="utf-8")
    assert res.chapter == 1
    assert "第 1 章开篇" in text
    assert any("style_guide.md" in w for w in res.warnings)
    assert any("world_state.yaml" in w for w in res.warnings)


def test_validation_errors_and_external_indexer_lifecycle(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("general", tmp_path)

    with SqliteIndexer(book_dir) as ext_idx:
        ext_idx.sync()
        ext_matcher = AliasMatcher()
        ext_matcher.build({"char_chenyu": ["陈屿"]})

        builder = PacketBuilder(
            book_dir,
            indexer=ext_idx,
            matcher=ext_matcher,
            auto_sync=False,
        )

        # 非法参数校验
        with pytest.raises(ValueError):
            builder.build(0)
        with pytest.raises(ValueError):
            builder.build(True)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            builder.build(13, custom_budget=0)
        with pytest.raises(TypeError):
            builder.build(13, custom_budget=True)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            builder.build(13, custom_budget="invalid")  # type: ignore[arg-type]
        with pytest.raises(FileNotFoundError):
            builder.build(999)

        # 正常编译，且不关闭外部注入的 indexer
        res = builder.build(13)
        assert res.chapter == 13
        builder.close()
        # 外部 indexer 仍可正常执行查询
        assert ext_idx.get_entity("char_chenyu") is not None


def test_slot_compile_methods_direct_and_cleanup(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("cultivation_rpg", tmp_path)
    with SqliteIndexer(book_dir) as idx:
        idx.sync()
        with PacketBuilder(book_dir, indexer=idx, auto_sync=False) as builder:
            bp = builder._parser.parse_blueprint_md(
                book_dir / "02_structure" / "blueprints" / "vol_01" / "ch_0142.md",
                book_dir=book_dir,
            )
            pov_bundle = idx.get_entity_bundle("char_linfan", at_chapter=142)
            s1 = builder._compile_slot_1_guardrails(bp, pov_bundle.state)  # type: ignore[arg-type]
            s2 = builder._compile_slot_2_world_and_facts(bp, ["loc_beijing_gucheng"])
            s3 = builder._compile_slot_3_entities_and_relations(bp, ["char_suyao"])
            s4 = builder._compile_slot_4_continuity(142)
            s5 = builder._compile_slot_5_beats_and_threads(bp)

            assert "POV 视角" in s1
            assert "暗星地下拍卖场" in s2
            assert "苏瑶" in s3
            assert "第141章" in s4
            assert "Scene 1" in s5

    # 验证 Windows 下临时目录可被干净删除（无句柄泄漏）
    shutil.rmtree(book_dir)
    assert not book_dir.exists()


def test_hundred_chapters_packet_build_performance(tmp_path: Path) -> None:
    book_dir = _copy_fixture_book("cultivation_rpg", tmp_path)

    # 扩展至 120 章历史原子事实与设定体量，验证单章编译性能 < 300ms (NFR-1)
    hist_path = book_dir / "03_state" / "history" / "vol_01_facts.jsonl"
    lines = [
        json.dumps(
            {
                "ch": ch,
                "arc": "vol_01_arc_03" if ch >= 81 else "vol_01_arc_01",
                "time": f"天历{(ch // 40) + 1}年",
                "summary": f"第{ch}章林凡在九州历练并与多方修士周旋推进主线剧情。",
                "facts": [
                    f"第{ch}章林凡在北境孤城坊市打探青铜古镜线索",
                    f"第{ch}章青云门执法堂加大南域通缉巡查力度",
                    f"第{ch}章苏瑶暗中调配凝神散压制血魂印波动",
                ],
                "entities": ["char_linfan", "char_suyao", "loc_beijing_gucheng", "item_gujing"],
            },
            ensure_ascii=False,
        )
        for ch in range(1, 142)
    ]
    hist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 同步设定实体与事实入派生库
    with SqliteIndexer(book_dir) as idx:
        idx.sync()
        with PacketBuilder(book_dir, indexer=idx, auto_sync=False) as builder:
            t0 = time.perf_counter()
            res = builder.build(142)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert res.chapter == 142
    assert (book_dir / res.packet_path).is_file()
    assert elapsed_ms < 300.0, f"百章规模单章编译耗时过高: {elapsed_ms:.1f} ms"


def test_packet_builder_isolates_author_notes_strips_memos_and_warns_concrete_leaks(
    tmp_path: Path,
) -> None:
    """验证单章写作包绝不漏入 00_作者设定/ 与 [作者备忘]，且在槽位 1 声明反照抄红线并上报具象泄漏告警。"""
    book_dir = _copy_fixture_book("cultivation_rpg", tmp_path)

    # 1) 在两类作者层目录中写入独占词元与同名实体随笔
    for folder in ("00_作者设定", "00_author_notes"):
        note = book_dir / folder / "suyao_memo.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(
            f"---\nid: char_suyao\nname: 苏瑶\n---\n作者层独占密文_{folder}_天台台词草稿\n",
            encoding="utf-8",
        )

    # 2) 在模型层 char_suyao.md 中混入 [作者备忘]、HTML 注释与一句具象台词
    suyao_path = book_dir / "01_settings" / "characters" / "char_suyao.md"
    orig_text = suyao_path.read_text(encoding="utf-8")
    suyao_path.write_text(
        orig_text.rstrip()
        + "\n<!-- 设定注释隔离文本 -->\n"
        + "\n## [作者备忘]\n备忘块独占词元绝不能进写作包。\n"
        + '\n## [专题: 应急反应]\n口头禅：“哼，谁要你管我生死！”\n',
        encoding="utf-8",
    )

    # 同步修改第142章蓝图挂载该专题以便检验 topics 也触发具象告警与备忘剥离
    bp_path = book_dir / "02_structure" / "blueprints" / "vol_01" / "ch_0142.md"
    bp_text = bp_path.read_text(encoding="utf-8").replace(
        "topics: []",
        "topics:\n  - entity_id: char_suyao\n    topic: 应急反应",
    )
    bp_path.write_text(bp_text, encoding="utf-8")

    with SqliteIndexer(book_dir) as idx:
        idx.rebuild()
        with PacketBuilder(book_dir, indexer=idx, auto_sync=False) as builder:
            res = builder.build(142)

    pkt_text = (book_dir / res.packet_path).read_text(encoding="utf-8")
    assert "作者层独占密文_00_作者设定_天台台词草稿" not in pkt_text
    assert "作者层独占密文_00_author_notes_天台台词草稿" not in pkt_text
    assert "设定注释隔离文本" not in pkt_text
    assert "备忘块独占词元绝不能进写作包" not in pkt_text
    assert "严禁照抄打卡" in pkt_text and "规则边界" in pkt_text
    assert any("char_suyao" in w for w in res.warnings)
