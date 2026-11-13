"""T02.6 跨模块只读结果 DTO (`results.py`) 单元测试。"""

from __future__ import annotations

import math

import pytest
from pydantic import TypeAdapter, ValidationError

import ddnovel.core.schemas.results as results_mod
from ddnovel.core.schemas._base import (
    EntityStatus,
    FtsScope,
    IssueLevel,
    SettingCategory,
    ThreadStatus,
)
from ddnovel.core.schemas.models import (
    CharacterState,
    FactionState,
    SettingDoc,
    TemporalRelation,
    Thread,
)
from ddnovel.core.schemas.results import (
    AuditIssue,
    AuditReport,
    CompactResult,
    DropResult,
    EntityBundle,
    GCReport,
    ImpactHit,
    ImpactReport,
    ImpactScope,
    MatchedEntity,
    PacketBuildResult,
    ParsedBlocks,
    PromoteResult,
    PruneReport,
    RadarHit,
    RadarReason,
    RadarReport,
    SearchHit,
    SlotName,
    SlotReport,
    SyncReport,
)


def _make_setting(
    entity_id: str = "char_linfan",
    name: str = "林凡",
    category: SettingCategory = SettingCategory.CHARACTER,
    file_path: str = "01_settings/characters/char_linfan.md",
) -> SettingDoc:
    return SettingDoc(
        id=entity_id,
        name=name,
        category=category,
        file_path=file_path,
        core_text="核心设定文本",
    )


def _make_thread(thread_id: str = "thread_gujing") -> Thread:
    return Thread(
        id=thread_id,
        title="残缺青铜古镜来历",
        status=ThreadStatus.ACTIVE,
        planted_at=87,
        last_progressed_at=141,
        expected_window=(140, 150),
        trigger_entities=["char_linfan", "item_gujing"],
        public_clue="镜缘温热",
    )


# ---------------------------------------------------------------------------
# 1. Frozen 只读不可变性、extra="forbid" 与可哈希性测试
# ---------------------------------------------------------------------------


def test_frozen_immutability_forbids_extra_and_hashability():
    matched = MatchedEntity(
        entity_id="char_linfan", alias="林凡", start=0, end=2, is_archived=False
    )
    hit = SearchHit(
        doc_id="doc_01",
        scope=FtsScope.SETTING,
        chapter_num=0,
        entity_id="char_linfan",
        excerpt="林凡",
        score=1.5,
    )
    impact_hit = ImpactHit(
        scope=ImpactScope.MANUSCRIPT,
        location="04_creation/manuscript/vol_01/ch_0010.md",
        chapter_num=10,
        matched_term="林凡",
        excerpt="林凡转身。",
    )
    audit_issue = AuditIssue(
        level=IssueLevel.WARNING,
        code="UNKNOWN_EXTRA",
        message="存在未知字段",
        location="03_state/world_state.yaml",
    )

    # 不可变
    with pytest.raises(ValidationError):
        matched.alias = "小凡"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        hit.score = 2.0  # type: ignore[misc]

    # extra="forbid" 拒绝未知字段
    with pytest.raises(ValidationError):
        MatchedEntity.model_validate(
            {
                "entity_id": "char_linfan",
                "alias": "林凡",
                "start": 0,
                "end": 2,
                "unknown_flag": True,
            }
        )
    with pytest.raises(ValidationError):
        ParsedBlocks.model_validate({"core": "hi", "extra_field": 123})

    # 纯标量字段 FrozenDTO 可哈希，可直接用于 set / dict key
    scalar_set = {matched, hit, impact_hit, audit_issue}
    assert len(scalar_set) == 4
    cache = {matched: "ok", hit: 1, impact_hit: 2, audit_issue: 3}
    assert cache[matched] == "ok"


# ---------------------------------------------------------------------------
# 2. 解析与别名嗅探 DTO (ParsedBlocks / MatchedEntity)
# ---------------------------------------------------------------------------


def test_parsed_blocks_and_matched_entity_validation():
    pb = ParsedBlocks(
        core="林凡核心人设",
        topics={"剑道感悟": "秋水剑意"},
        secret="上古剑宗遗孤",
        token_estimates={"core": 120, "topic:剑道感悟": 80, "secret": 45},
    )
    assert pb.token_estimates["core"] == 120

    for bad_tokens in (-1, True, False):
        with pytest.raises(ValidationError):
            ParsedBlocks.model_validate({"token_estimates": {"core": bad_tokens}})

    m_ok = MatchedEntity(entity_id="char_linfan", alias="小凡", start=5, end=7)
    assert m_ok.start == 5 and m_ok.end == 7

    for bad_span in ((5, 5), (6, 5), (0, 0)):
        with pytest.raises(ValidationError, match="end"):
            MatchedEntity(
                entity_id="char_linfan",
                alias="小凡",
                start=bad_span[0],
                end=bad_span[1],
            )


# ---------------------------------------------------------------------------
# 3. 索引与混合检索 DTO (SyncReport / SearchHit / EntityBundle)
# ---------------------------------------------------------------------------


def test_sync_report_posix_dedupe_and_search_hit_scores():
    sync = SyncReport(
        added=[
            r"01_settings\characters\char_linfan.md",
            "01_settings/characters/char_linfan.md",
        ],
        updated=[r"03_state\relations.yaml"],
        deleted=[r"01_settings\items\old_item.md"],
        elapsed_ms=18.5,
        rebuilt=False,
    )
    assert sync.added == ["01_settings/characters/char_linfan.md"]
    assert sync.updated == ["03_state/relations.yaml"]
    assert sync.deleted == ["01_settings/items/old_item.md"]

    for bad_ms in (-0.1, True, False, math.nan, math.inf):
        with pytest.raises(ValidationError):
            SyncReport(elapsed_ms=bad_ms)  # type: ignore[arg-type]

    # SearchHit 支持 chapter_num=0（设定）与负分（如 SQLite BM25 原始分）
    hit = SearchHit(
        doc_id="setting:char_linfan:core",
        scope=FtsScope.SETTING,
        chapter_num=0,
        entity_id="char_linfan",
        excerpt="林凡",
        score=-3.14159,
    )
    assert hit.chapter_num == 0
    assert hit.score == pytest.approx(-3.14159)

    for bad_score in (True, False, math.nan, math.inf, -math.inf):
        with pytest.raises(ValidationError):
            SearchHit(doc_id="d1", scope=FtsScope.FACT, score=bad_score)  # type: ignore[arg-type]


def test_entity_bundle_state_disambiguation_and_mismatch():
    char_setting = _make_setting(category=SettingCategory.CHARACTER)
    char_state = CharacterState(updated_at_ch=141, location="loc_beijing_gucheng")
    rel = TemporalRelation(
        source="char_linfan",
        target="fac_qingyunmen",
        relation="流亡弃徒",
        valid_from_ch=121,
    )
    char_bundle = EntityBundle(
        setting=char_setting, state=char_state, relations=[rel], is_archived=False
    )
    assert isinstance(char_bundle.state, CharacterState)

    # 最小化 FactionState 字典（仅 updated_at_ch）必须按 setting.category="faction"
    # 精准反序列化为 FactionState
    fac_setting = _make_setting(
        entity_id="fac_qingyunmen",
        name="青云门",
        category=SettingCategory.FACTION,
        file_path="01_settings/factions/fac_qingyunmen.md",
    )
    fac_bundle = EntityBundle.model_validate(
        {
            "setting": fac_setting.model_dump(mode="json"),
            "state": {"updated_at_ch": 140, "profile_ext": {"realm": "金丹宗门"}},
        },
        context={"genre_profile": "cultivation_rpg"},
    )
    assert isinstance(fac_bundle.state, FactionState)
    assert fac_bundle.state.status == EntityStatus.ACTIVE
    assert fac_bundle.state.profile_ext["realm"] == "金丹宗门"
    assert fac_bundle.state.profile_ext["techniques"] == []

    # context 中的无效扩展字段同样在 EntityBundle 内被拦截
    with pytest.raises(ValidationError):
        EntityBundle.model_validate(
            {
                "setting": fac_setting.model_dump(mode="json"),
                "state": {"updated_at_ch": 140, "profile_ext": {"realm": ["bad"]}},
            },
            context={"genre_profile": "cultivation_rpg"},
        )

    # 静态设定（如 geography）允许 state=None，但不可挂载状态截面
    geo_setting = _make_setting(
        entity_id="loc_zhuixinggu",
        name="坠星谷",
        category=SettingCategory.GEOGRAPHY,
        file_path="01_settings/geography/loc_zhuixinggu.md",
    )
    geo_ok = EntityBundle(setting=geo_setting, state=None)
    assert geo_ok.state is None

    with pytest.raises(ValidationError, match="不支持挂载"):
        EntityBundle(setting=geo_setting, state=char_state)

    # character 与 FactionState / faction 与 CharacterState 错配报错
    with pytest.raises(ValidationError, match="CharacterState"):
        EntityBundle(
            setting=char_setting,
            state=FactionState(updated_at_ch=10),
        )
    with pytest.raises(ValidationError, match="FactionState"):
        EntityBundle(
            setting=fac_setting,
            state=char_state,
        )


# ---------------------------------------------------------------------------
# 4. 业务报告 DTO (Radar / Impact / Packet)
# ---------------------------------------------------------------------------


def test_radar_impact_and_packet_dtos():
    th = _make_thread()
    radar_hit = RadarHit(
        thread=th,
        reasons=[RadarReason.WINDOW, "entity", RadarReason.WINDOW],  # type: ignore[list-item]
        detail="命中窗口 [140, 150]",
    )
    assert radar_hit.reasons == [RadarReason.WINDOW, RadarReason.ENTITY]

    with pytest.raises(ValidationError):
        RadarHit(thread=th, reasons=[])

    radar_report = RadarReport(
        target_chapter=142,
        planned_entities=["char_linfan", "item_gujing", "char_linfan"],
        hits=[radar_hit],
        total_active_threads=3,
    )
    assert radar_report.planned_entities == ["char_linfan", "item_gujing"]

    # ImpactReport 自动推导 is_safe_to_change，且尊重显式传入值
    safe_report = ImpactReport(
        target="item_gujing",
        entity_id="item_gujing",
        expanded_terms=[" 青铜古镜 ", "", "古镜", "青铜古镜"],
        hits=[],
    )
    assert safe_report.is_safe_to_change is True
    assert safe_report.expanded_terms == ["青铜古镜", "古镜"]

    unsafe_report = ImpactReport(
        target="char_zhaoqian",
        hits=[
            ImpactHit(
                scope=ImpactScope.FACT,
                location=r"03_state\history\vol_01_facts.jsonl",
                chapter_num=87,
                matched_term="赵乾",
                excerpt="赵乾于第87章死亡",
            )
        ],
    )
    assert unsafe_report.is_safe_to_change is False
    assert unsafe_report.hits[0].location == "03_state/history/vol_01_facts.jsonl"

    explicit_report = ImpactReport(target="item_gujing", hits=[], is_safe_to_change=False)
    assert explicit_report.is_safe_to_change is False

    with pytest.raises(ValidationError):
        ImpactReport.model_validate({"target": "item_gujing", "hits": "not-a-list"})

    # PacketBuildResult
    packet_res = PacketBuildResult(
        chapter=142,
        packet_path=r"04_creation\packets\ch_0142_packet.md",
        budget_total=10000,
        total_tokens=7450,
        slots=[
            SlotReport(name=SlotName.GUARDRAILS, budget=1200, used=900),
            SlotReport(
                name=SlotName.WORLD_AND_FACTS,
                budget=2200,
                used=2100,
                trimmed=True,
                dropped_items=[" 第12章铁剑事实 ", "", "第12章铁剑事实"],
            ),
        ],
        blocked_secrets=["char_suyao", "char_suyao"],
        blocked_threads=["thread_suyao_identity", "thread_suyao_identity"],
        warnings=["  已裁剪 1 条远期事实  ", ""],
    )
    assert packet_res.packet_path == "04_creation/packets/ch_0142_packet.md"
    assert packet_res.slots[1].dropped_items == ["第12章铁剑事实"]
    assert packet_res.blocked_secrets == ["char_suyao"]
    assert packet_res.blocked_threads == ["thread_suyao_identity"]
    assert packet_res.warnings == ["已裁剪 1 条远期事实"]

    with pytest.raises(ValidationError, match="重复槽位"):
        PacketBuildResult(
            chapter=142,
            packet_path="04_creation/packets/ch_0142_packet.md",
            total_tokens=1000,
            slots=[
                SlotReport(name=SlotName.GUARDRAILS, budget=1200, used=500),
                SlotReport(name=SlotName.GUARDRAILS, budget=1200, used=500),
            ],
        )


# ---------------------------------------------------------------------------
# 5. 生命周期与体检 DTO (Promote / GC / Drop / Prune / Compact / Audit)
# ---------------------------------------------------------------------------


def test_lifecycle_and_audit_dtos():
    promote = PromoteResult(
        chapter=142,
        draft_path=r"04_creation\drafts\ch_0142_draft.md",
        manuscript_path=r"04_creation\manuscript\vol_01\ch_0142.md",
        cleaned_paths=[
            r"04_creation\packets\ch_0136_packet.md",
            "04_creation/packets/ch_0136_packet.md",
        ],
    )
    assert promote.draft_path == "04_creation/drafts/ch_0142_draft.md"
    assert promote.manuscript_path == "04_creation/manuscript/vol_01/ch_0142.md"
    assert promote.cleaned_paths == ["04_creation/packets/ch_0136_packet.md"]

    gc = GCReport(
        current_chapter=142,
        archived_characters=["char_zhaoqian", "char_zhaoqian"],
        archived_factions=["fac_old_sect"],
        archived_relations_count=2,
        archived_threads=["thread_zhuixinggu_ambush"],
        modified_paths=[r"03_state\active\characters.yaml", r"03_state\archive\characters.yaml"],
        dry_run=True,
    )
    assert gc.archived_characters == ["char_zhaoqian"]
    assert gc.modified_paths == [
        "03_state/active/characters.yaml",
        "03_state/archive/characters.yaml",
    ]

    drop = DropResult(
        thread_id="thread_side_quest",
        removed_from=r"03_state\threads.yaml",
        prior_mentions=[
            ImpactHit(
                scope=ImpactScope.FACT,
                location=r"03_state\history\vol_01_facts.jsonl",
                chapter_num=12,
                excerpt="曾提及支线人物",
            )
        ],
    )
    assert drop.removed_from == "03_state/threads.yaml"

    prune = PruneReport(
        orphan_ids=["item_unused", "item_unused"],
        low_frequency_ids=["char_innkeeper"],
        deleted_paths=[r"01_settings\items\item_unused.md"],
        skipped_ids=["char_linfan"],
    )
    assert prune.orphan_ids == ["item_unused"]
    assert prune.deleted_paths == ["01_settings/items/item_unused.md"]

    compact = CompactResult(
        volume_id="vol_01",
        history_path=r"03_state\history\vol_01_facts.jsonl",
        original_count=100,
        compacted_count=18,
    )
    assert compact.history_path == "03_state/history/vol_01_facts.jsonl"

    with pytest.raises(ValidationError, match="compacted_count"):
        CompactResult(
            volume_id="vol_01",
            history_path="03_state/history/vol_01_facts.jsonl",
            original_count=10,
            compacted_count=11,
        )

    # AuditReport 自动推导 passed
    info_issue = AuditIssue(
        level=IssueLevel.INFO, code="I001", message="提示", location=r"03_state\world_state.yaml"
    )
    warn_issue = AuditIssue(level=IssueLevel.WARNING, code="W001", message="警告")
    err_issue = AuditIssue(level=IssueLevel.ERROR, code="E001", message="死人出场")

    assert info_issue.location == "03_state/world_state.yaml"
    assert AuditReport(chapter=142, issues=[info_issue, warn_issue]).passed is True
    assert AuditReport(chapter=142, issues=[warn_issue, err_issue]).passed is False
    assert (
        AuditReport.model_validate(
            {
                "chapter": 142,
                "issues": [{"level": "error", "code": "E002", "message": "缺少设定"}],
            }
        ).passed
        is False
    )
    # 显式严格模式传入 passed=False 时保留调用方值
    assert AuditReport(chapter=142, issues=[warn_issue], passed=False).passed is False


# ---------------------------------------------------------------------------
# 6. 全量 JSON 往返、JSON Schema 导出与 __all__ 完整性测试
# ---------------------------------------------------------------------------


def test_all_18_dtos_json_roundtrip():
    char_setting = _make_setting(category=SettingCategory.CHARACTER)
    fac_setting = _make_setting(
        entity_id="fac_qingyunmen",
        name="青云门",
        category=SettingCategory.FACTION,
        file_path="01_settings/factions/fac_qingyunmen.md",
    )
    char_state = CharacterState(updated_at_ch=141, location="loc_beijing_gucheng")
    fac_state = FactionState(updated_at_ch=140, leader="char_linfan")
    rel = TemporalRelation(
        source="char_linfan",
        target="fac_qingyunmen",
        relation="弃徒",
        valid_from_ch=121,
    )
    thread = _make_thread()
    impact_hit = ImpactHit(
        scope=ImpactScope.BLUEPRINT,
        location="02_structure/blueprints/vol_01/ch_0142.md",
        chapter_num=142,
        matched_term="古镜",
        excerpt="古镜共鸣",
    )
    radar_hit = RadarHit(thread=thread, reasons=[RadarReason.WINDOW], detail="到期")
    slot_report = SlotReport(name=SlotName.GUARDRAILS, budget=1200, used=800)
    audit_issue = AuditIssue(
        level=IssueLevel.ERROR,
        code="E_DEAD_CAST",
        message="已故角色出现在出场表",
        location="02_structure/blueprints/vol_01/ch_0142.md",
    )

    instances = [
        ParsedBlocks(core="core", topics={"t": "v"}, secret="sec", token_estimates={"core": 10}),
        MatchedEntity(entity_id="char_linfan", alias="林凡", start=0, end=2),
        SyncReport(added=["a.md"], updated=["b.md"], deleted=["c.md"], elapsed_ms=3.2, rebuilt=True),
        SearchHit(doc_id="d1", scope=FtsScope.SETTING, chapter_num=0, score=0.9),
        EntityBundle(setting=char_setting, state=char_state, relations=[rel]),
        EntityBundle(setting=fac_setting, state=fac_state, relations=[rel], is_archived=True),
        radar_hit,
        RadarReport(
            target_chapter=142,
            planned_entities=["char_linfan"],
            hits=[radar_hit],
            total_active_threads=2,
        ),
        impact_hit,
        ImpactReport(target="item_gujing", entity_id="item_gujing", hits=[impact_hit]),
        slot_report,
        PacketBuildResult(
            chapter=142,
            packet_path="04_creation/packets/ch_0142_packet.md",
            budget_total=10000,
            total_tokens=5000,
            slots=[slot_report],
            blocked_secrets=["char_suyao"],
            blocked_threads=["thread_suyao_identity"],
        ),
        PromoteResult(
            chapter=142,
            draft_path="04_creation/drafts/ch_0142_draft.md",
            manuscript_path="04_creation/manuscript/vol_01/ch_0142.md",
            cleaned_paths=["04_creation/packets/ch_0136_packet.md"],
        ),
        GCReport(
            current_chapter=142,
            archived_characters=["char_zhaoqian"],
            archived_relations_count=1,
            modified_paths=["03_state/active/characters.yaml"],
        ),
        DropResult(
            thread_id="thread_old",
            removed_from="03_state/threads.yaml",
            prior_mentions=[impact_hit],
        ),
        PruneReport(orphan_ids=["item_unused"], deleted_paths=["01_settings/items/item_unused.md"]),
        CompactResult(
            volume_id="vol_01",
            history_path="03_state/history/vol_01_facts.jsonl",
            original_count=100,
            compacted_count=18,
        ),
        audit_issue,
        AuditReport(chapter=142, issues=[audit_issue]),
    ]

    for obj in instances:
        reloaded = type(obj).model_validate_json(obj.model_dump_json())
        assert reloaded == obj


@pytest.mark.parametrize("symbol_name", results_mod.__all__)
def test_results_all_symbols_json_schema_exportable(symbol_name: str):
    sym = getattr(results_mod, symbol_name)
    schema = TypeAdapter(sym).json_schema(mode="serialization")
    assert isinstance(schema, dict)


def test_results_public_all_exports():
    expected = [
        "AuditIssue",
        "AuditReport",
        "CompactResult",
        "DropResult",
        "EntityBundle",
        "GCReport",
        "ImpactHit",
        "ImpactReport",
        "ImpactScope",
        "MatchedEntity",
        "PacketBuildResult",
        "ParsedBlocks",
        "PromoteResult",
        "PruneReport",
        "RadarHit",
        "RadarReason",
        "RadarReport",
        "SearchHit",
        "SlotName",
        "SlotReport",
        "SyncReport",
    ]
    assert sorted(results_mod.__all__) == sorted(expected)
    for sym in expected:
        assert hasattr(results_mod, sym)
