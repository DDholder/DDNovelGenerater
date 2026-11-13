"""T07 可选插拔向量检索与 RRF 重排插件单元测试。"""

from __future__ import annotations

import io
import json
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from ddnovel.core import (
    DeterministicHashEmbedder,
    EmbeddingDimensionError,
    EmbeddingError,
    VectorDocument,
    VectorPlugin,
    VectorSyncStats,
    cosine_similarity,
    pack_f32,
    rrf_fuse,
    unpack_f32,
)
from ddnovel.core.schemas import EmbeddingConfig, FtsScope, NovelConfig, SearchHit
from ddnovel.core.vector_store import (
    list_embedding_providers,
    register_embedding_provider,
    try_load_sqlite_vec,
)

# ---------------------------------------------------------------------------
# 辅助构造器
# ---------------------------------------------------------------------------


def _make_hit(
    doc_id: str,
    *,
    scope: FtsScope = FtsScope.FACT,
    chapter_num: int = 1,
    entity_id: str | None = None,
    excerpt: str = "",
    score: float = 0.0,
) -> SearchHit:
    return SearchHit(
        doc_id=doc_id,
        scope=scope,
        chapter_num=chapter_num,
        entity_id=entity_id,
        excerpt=excerpt,
        score=score,
    )


def _create_fts_docs_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fts_docs (
            doc_id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            chapter_num INTEGER NOT NULL,
            entity_id TEXT,
            tokenized_text TEXT NOT NULL,
            raw_excerpt TEXT NOT NULL
        )
        """
    )


# ---------------------------------------------------------------------------
# 1. 关闭状态零外部调用保证
# ---------------------------------------------------------------------------


def test_disabled_plugin_zero_external_calls_and_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认关闭（enabled=False / None / NovelConfig）时零外部调用、零联网、透传截断 FTS 结果。"""
    call_count = 0

    def exploding_embedder(texts: Sequence[str]) -> list[list[float]]:
        nonlocal call_count
        call_count += 1
        raise AssertionError("关闭状态下严禁调用外部向量编码器")

    def exploding_urlopen(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("关闭状态下严禁发起任何网络请求")

    monkeypatch.setattr(urllib.request, "urlopen", exploding_urlopen)

    novel_cfg = NovelConfig(title="长生劫", author="青崖子")
    configs = [
        None,
        EmbeddingConfig(enabled=False),
        EmbeddingConfig(enabled=False, provider="openai", model="text-embedding-3-small", dim=4),
        novel_cfg,
    ]
    fts_hits = [
        _make_hit("fact_01", excerpt="古镜震颤", score=12.5),
        _make_hit("fact_02", excerpt="剑气破空", score=9.0),
        _make_hit("fact_03", excerpt="北境风雪", score=5.0),
    ]
    docs = [VectorDocument(doc_id="fact_01", text="青铜古镜在黑夜中微微震颤", scope=FtsScope.FACT)]

    for cfg in configs:
        plugin = VectorPlugin(cfg, embedder=exploding_embedder)
        assert plugin.is_enabled() is False
        assert plugin.embed_texts(["测试文本"]) == []
        assert plugin.index_documents(docs) == VectorSyncStats(embedded=0, skipped_cached=0, deleted=0)
        assert plugin.search_vector("青铜古镜", top_k=5) == []
        assert plugin.hybrid_rerank("青铜古镜", fts_hits, top_k=2) == fts_hits[:2]
        assert plugin.hybrid_rerank("青铜古镜", fts_hits, top_k=0) == []

    assert call_count == 0


# ---------------------------------------------------------------------------
# 2. float32 二进制编解码与余弦相似度
# ---------------------------------------------------------------------------


def test_pack_unpack_f32_roundtrip_and_byte_layout() -> None:
    """pack_f32 / unpack_f32 严格使用 4 字节小端序 float32 往返一致。"""
    vec = [1.0, -0.5, 0.25, 0.0]
    blob = pack_f32(vec, expected_dim=4)
    assert isinstance(blob, bytes)
    assert len(blob) == 16
    assert blob[:4] == b"\x00\x00\x80?"  # IEEE-754 little-endian 1.0f

    restored = unpack_f32(blob, expected_dim=4)
    assert restored == pytest.approx(tuple(vec), rel=1e-6, abs=1e-6)
    assert unpack_f32(memoryview(blob), expected_dim=4) == restored


@pytest.mark.parametrize(
    ("bad_vec", "expected_dim", "exc_cls"),
    [
        ([], None, EmbeddingDimensionError),
        ([1.0, 2.0], 4, EmbeddingDimensionError),
        ([1.0, float("nan")], 2, EmbeddingError),
        ([1.0, float("inf")], 2, EmbeddingError),
        ([1.0, float("-inf")], 2, EmbeddingError),
        ("1234", None, EmbeddingDimensionError),
    ],
)
def test_pack_f32_validation_errors(
    bad_vec: Any,
    expected_dim: int | None,
    exc_cls: type[Exception],
) -> None:
    with pytest.raises(exc_cls):
        pack_f32(bad_vec, expected_dim=expected_dim)


@pytest.mark.parametrize(
    ("bad_blob", "expected_dim"),
    [
        (b"", None),
        (b"\x00\x00\x00", None),  # 非 4 字节倍数
        (b"\x00" * 8, 4),  # 维度不匹配
    ],
)
def test_unpack_f32_validation_errors(bad_blob: bytes, expected_dim: int | None) -> None:
    with pytest.raises(EmbeddingDimensionError):
        unpack_f32(bad_blob, expected_dim=expected_dim)


def test_cosine_similarity_properties_and_blob_support() -> None:
    """覆盖同向、反向、正交、零向量及 bytes/sequence 混合入参。"""
    a = [3.0, 4.0, 0.0]
    b = [6.0, 8.0, 0.0]
    neg = [-3.0, -4.0, 0.0]
    orth = [0.0, 0.0, 5.0]
    zero = [0.0, 0.0, 0.0]

    assert cosine_similarity(a, b) == pytest.approx(1.0, abs=1e-6)
    assert cosine_similarity(pack_f32(a), pack_f32(neg)) == pytest.approx(-1.0, abs=1e-6)
    assert cosine_similarity(pack_f32(a), orth) == pytest.approx(0.0, abs=1e-6)
    assert cosine_similarity(zero, a) == 0.0
    assert cosine_similarity(pack_f32(zero), pack_f32(zero)) == 0.0

    with pytest.raises(EmbeddingDimensionError):
        cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0])


# ---------------------------------------------------------------------------
# 3. Mock Embedding 与 RRF 混合重排精确验证
# ---------------------------------------------------------------------------


def test_rrf_fuse_closed_form_scores_and_rank_promotion() -> None:
    """BM25 排第 1 的假阳性词面诱饵与向量排第 1 的同义候选融合后，准确提升真实语义结果。"""
    fts_hits = [
        _make_hit("doc_decoy", excerpt="关键词诱饵但语义偏离", score=15.0),  # rank 1
        _make_hit("doc_mid", excerpt="中等相关文本", score=10.0),  # rank 2
        _make_hit("doc_target", excerpt="核心同义答案", score=5.0),  # rank 3
    ]
    vec_hits = [
        _make_hit("doc_target", excerpt="核心同义答案", score=0.96),  # rank 1
        _make_hit("doc_mid", excerpt="中等相关文本", score=0.88),  # rank 2
        _make_hit("doc_semantic_only", excerpt="纯语义召回无字面重叠", score=0.72),  # rank 3
        _make_hit("doc_decoy", excerpt="关键词诱饵但语义偏离", score=0.05),  # rank 4
    ]

    k = 60
    fused = rrf_fuse(fts_hits, vec_hits, rrf_k=k, top_k=10)
    assert [hit.doc_id for hit in fused] == ["doc_target", "doc_mid", "doc_decoy", "doc_semantic_only"]

    expected_scores = {
        "doc_target": 1.0 / (k + 3) + 1.0 / (k + 1),
        "doc_mid": 1.0 / (k + 2) + 1.0 / (k + 2),
        "doc_decoy": 1.0 / (k + 1) + 1.0 / (k + 4),
        "doc_semantic_only": 1.0 / (k + 3),
    }
    for hit in fused:
        assert hit.score == pytest.approx(expected_scores[hit.doc_id], rel=1e-12, abs=1e-12)

    # 输入模型保持不可变
    assert fts_hits[0].score == 15.0
    assert vec_hits[0].score == 0.96


def test_rrf_fuse_symmetric_tie_break_by_higher_similarity() -> None:
    """两候选在双路刚好呈 (1,2) 与 (2,1) 对称位次时，按更高余弦相似度稳定破局。"""
    fts_hits = [
        _make_hit("doc_bm25_first", score=10.0),
        _make_hit("doc_sem_first", score=8.0),
    ]
    vec_hits = [
        _make_hit("doc_sem_first", score=0.95),
        _make_hit("doc_bm25_first", score=0.20),
    ]
    fused = rrf_fuse(fts_hits, vec_hits, rrf_k=10, top_k=2)
    assert [h.doc_id for h in fused] == ["doc_sem_first", "doc_bm25_first"]
    assert fused[0].score == pytest.approx(fused[1].score, rel=1e-12)


def test_hybrid_rerank_mock_embedding_single_batch_and_cache() -> None:
    """Mock Embedding 下，未预索引 FTS 命中与查询合并单批编码，并按 content_hash 缓存防重复调用。"""
    batches_seen: list[list[str]] = []

    def mock_embed(texts: Sequence[str]) -> list[list[float]]:
        batches_seen.append(list(texts))
        vecs: list[list[float]] = []
        for t in texts:
            if "古镜" in t or "青铜器物" in t or "照见前世" in t:
                vecs.append([0.99, 0.14, 0.0])
            elif "血脉" in t or "禁制" in t:
                vecs.append([0.70, 0.70, 0.10])
            elif "药铺" in t:
                vecs.append([0.30, 0.30, 0.90])
            else:
                vecs.append([0.0, 0.10, 0.99])
        return vecs

    cfg = EmbeddingConfig(enabled=True, provider="mock", model="mock-v1", dim=3, rrf_k=10)
    plugin = VectorPlugin(cfg, embedder=mock_embed)

    fts_hits = [
        _make_hit("d_noise", excerpt="第1章：客栈买酒喝茶闲聊", score=18.0),  # bm25=1, vec=4
        _make_hit("d_mid", excerpt="第2章：血脉禁制压制修为", score=11.0),  # bm25=2, vec=2
        _make_hit("d_target", excerpt="第3章：青铜器物照见前世因果", score=7.0),  # bm25=3, vec=1
        _make_hit("d_extra", excerpt="第4章：药铺采购草药", score=3.0),  # bm25=4, vec=3
    ]

    reranked = plugin.hybrid_rerank("古镜真实来历", fts_hits, top_k=4)
    # 首次调用合并为 1 批 [query, excerpt1, excerpt2, excerpt3, excerpt4]
    assert len(batches_seen) == 1
    assert batches_seen[0][0] == "古镜真实来历"
    assert len(batches_seen[0]) == 5
    assert [h.doc_id for h in reranked] == ["d_target", "d_mid", "d_noise", "d_extra"]

    # 再次使用相同候选文本与新查询重排：仅需编码新 query，命中 _text_vec_cache
    reranked_again = plugin.hybrid_rerank("古镜是否源自上古", fts_hits, top_k=4)
    assert len(batches_seen) == 2
    assert batches_seen[1] == ["古镜是否源自上古"]
    assert [h.doc_id for h in reranked_again] == ["d_target", "d_mid", "d_noise", "d_extra"]

    # _text_vec_cache 不得污染纯向量语料检索（未调用 index_documents 时应为空）
    assert plugin.search_vector("古镜", top_k=5) == []


# ---------------------------------------------------------------------------
# 4. SQLite embeddings 表持久化、增量缓存与 fts_docs 混合召回
# ---------------------------------------------------------------------------


def test_sqlite_persistence_incremental_sync_and_orphan_prune() -> None:
    """验证 SQLite embeddings 建表结构（对齐 ER 图）、content_hash 跳过复用、删改与孤儿清理。"""
    embed_calls: list[list[str]] = []
    hasher = DeterministicHashEmbedder(dim=16)

    def tracking_embed(texts: Sequence[str]) -> list[list[float]]:
        embed_calls.append(list(texts))
        return hasher.embed(texts)

    conn = sqlite3.connect(":memory:")
    try:
        _create_fts_docs_table(conn)
        cfg = EmbeddingConfig(enabled=True, provider="hash", model="hash-16", dim=16, rrf_k=60)
        plugin = VectorPlugin(cfg, embedder=tracking_embed, conn=conn)

        docs = [
            VectorDocument(
                doc_id="set_linfan",
                text="林凡：青云门外门弟子，左肩受剑伤，随身携带青铜古镜",
                scope=FtsScope.SETTING,
                chapter_num=0,
                entity_id="char_linfan",
            ),
            VectorDocument(
                doc_id="fact_87",
                text="赵乾在坠星谷设伏反杀失败身亡，血魂印爆发",
                scope=FtsScope.FACT,
                chapter_num=87,
                entity_id="char_zhaoqian",
            ),
        ]
        for doc in docs:
            conn.execute(
                """
                INSERT INTO fts_docs (doc_id, scope, chapter_num, entity_id, tokenized_text, raw_excerpt)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (doc.doc_id, doc.scope.value, doc.chapter_num, doc.entity_id, doc.text, doc.resolved_excerpt),
            )
        conn.commit()

        # 验证 ER 图规定的 4 个字段
        stats1 = plugin.index_documents(docs)
        assert stats1 == VectorSyncStats(embedded=2, skipped_cached=0, deleted=0)
        assert len(embed_calls) == 1
        cols = {row[1] for row in conn.execute("PRAGMA table_info(embeddings)").fetchall()}
        assert cols == {"doc_id", "content_hash", "dim", "vec_f32"}

        # 重复同步：content_hash 完全一致 -> 零 embed 调用
        stats2 = plugin.index_documents(docs)
        assert stats2 == VectorSyncStats(embedded=0, skipped_cached=2, deleted=0)
        assert len(embed_calls) == 1

        # 仅修改 1 篇文档 -> 仅重编码被修改的 1 篇
        updated_docs = [
            docs[0],
            VectorDocument(
                doc_id="fact_87",
                text="赵乾在坠星谷身亡，临死前激发血魂印标记林凡",
                scope=FtsScope.FACT,
                chapter_num=87,
                entity_id="char_zhaoqian",
            ),
        ]
        stats3 = plugin.index_documents(updated_docs)
        assert stats3 == VectorSyncStats(embedded=1, skipped_cached=1, deleted=0)
        assert embed_calls[-1] == ["赵乾在坠星谷身亡，临死前激发血魂印标记林凡"]

        # 模拟 fts_docs 中删除 fact_87 -> prune_orphan_embeddings 自动清掉孤儿向量
        conn.execute("DELETE FROM fts_docs WHERE doc_id = ?", ("fact_87",))
        assert plugin.prune_orphan_embeddings() == 1
        remaining = [r[0] for r in conn.execute("SELECT doc_id FROM embeddings").fetchall()]
        assert remaining == ["set_linfan"]

        # delete_stale=True 删除不再传入列表的旧向量
        stats4 = plugin.index_documents([], delete_stale=True)
        assert stats4 == VectorSyncStats(embedded=0, skipped_cached=0, deleted=1)
        assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    finally:
        conn.close()


def test_sqlite_hybrid_recall_surfaces_zero_bm25_semantic_hit(tmp_path: Path) -> None:
    """通过真实文件型 SQLite (fts_docs + embeddings)，新鲜 VectorPlugin 召回零字面重叠语义文档并 RRF 重排。"""
    db_path = tmp_path / "index.sqlite"

    def semantic_embed(texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            if any(kw in t for kw in ("太阴星", "月华", "寒渊冰魄", "极寒灵宝")):
                out.append([1.0, 0.0, 0.0, 0.0])
            elif "剑阵" in t:
                out.append([0.0, 1.0, 0.0, 0.0])
            else:
                out.append([0.1, 0.1, 0.9, 0.1])
        return out

    cfg = EmbeddingConfig(enabled=True, provider="mock", model="semantic-4", dim=4, rrf_k=20)
    writer_conn = sqlite3.connect(db_path)
    try:
        _create_fts_docs_table(writer_conn)
        writer_plugin = VectorPlugin(cfg, embedder=semantic_embed, conn=writer_conn)
        docs = [
            VectorDocument(
                doc_id="fact_12_decoy",
                text="第12章：主角打听寒月客栈剑阵消息",
                scope=FtsScope.FACT,
                chapter_num=12,
            ),
            VectorDocument(
                doc_id="fact_45_semantic",
                text="第45章：极寒灵宝于月华井底苏醒",
                scope=FtsScope.FACT,
                chapter_num=45,
                entity_id="item_bingpo",
                excerpt="极寒灵宝于月华井底苏醒（纯语义同义片段）",
            ),
            VectorDocument(
                doc_id="setting_rule_01",
                text="太阴星灵气潮汐规则",
                scope=FtsScope.SETTING,
                chapter_num=0,
            ),
        ]
        writer_conn.executemany(
            """
            INSERT INTO fts_docs (doc_id, scope, chapter_num, entity_id, tokenized_text, raw_excerpt)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (d.doc_id, d.scope.value, d.chapter_num, d.entity_id, d.text, d.resolved_excerpt)
                for d in docs
            ],
        )
        writer_plugin.index_documents(docs)
        # 插入一条维度不匹配的旧模型脏数据，验证检索自动跳过而不崩
        writer_conn.execute(
            "INSERT INTO embeddings (doc_id, content_hash, dim, vec_f32) VALUES (?, ?, ?, ?)",
            ("stale_dim_doc", "abc", 2, pack_f32([1.0, 0.0])),
        )
        writer_conn.commit()
    finally:
        writer_conn.close()

    # 新建另一只读/查询连接与全新 VectorPlugin 实例（内存缓存为空），直接从 SQLite 检索
    reader_conn = sqlite3.connect(db_path)
    try:
        reader_plugin = VectorPlugin(cfg, embedder=semantic_embed, conn=reader_conn)

        # 限定 scope=FACT，不召回 SETTING 文档，且能召回 BM25 未命中的 fact_45_semantic
        vec_only = reader_plugin.search_vector("太阴星寒渊冰魄", top_k=5, scope=FtsScope.FACT)
        assert [h.doc_id for h in vec_only] == ["fact_45_semantic", "fact_12_decoy"]
        assert vec_only[0].excerpt == "极寒灵宝于月华井底苏醒（纯语义同义片段）"
        assert vec_only[0].entity_id == "item_bingpo"

        # FTS 仅命中诱饵 doc，经过 hybrid_rerank 后语义文档应进入最终结果并与 BM25 共存融合
        fts_hits = [_make_hit("fact_12_decoy", scope=FtsScope.FACT, chapter_num=12, score=10.0)]
        hybrid = reader_plugin.hybrid_rerank(
            "太阴星寒渊冰魄",
            fts_hits,
            top_k=5,
            scope=FtsScope.FACT,
        )
        assert {h.doc_id for h in hybrid} == {"fact_12_decoy", "fact_45_semantic"}
        assert hybrid[0].doc_id == "fact_12_decoy"  # RRF: 1/21 + 1/22 > 1/21
        assert hybrid[1].doc_id == "fact_45_semantic"
    finally:
        reader_conn.close()


# ---------------------------------------------------------------------------
# 5. 内置 Provider、OpenAI/Ollama HTTP Mock 与环境变量密钥安全
# ---------------------------------------------------------------------------


def test_deterministic_hash_provider_end_to_end() -> None:
    """内置 hash provider 无需外部依赖与 API Key，稳定生成单位向量并支持语义匹配。"""
    cfg = EmbeddingConfig(enabled=True, provider="hash", model="blake2b-hash", dim=64, rrf_k=60)
    plugin = VectorPlugin(cfg)
    assert plugin.is_enabled() is True

    blobs = plugin.embed_texts(["林凡持有青铜古镜", "   ", "林凡手持青铜古镜"])
    assert len(blobs) == 3
    assert blobs[1] == b"\x00" * (64 * 4)

    sim_close = cosine_similarity(blobs[0], blobs[2])
    assert sim_close > 0.70

    # 与完全无关文本相似度应显著更低
    unrelated = plugin.embed_texts(["西方魔法学院炼金药剂配方"])[0]
    assert cosine_similarity(blobs[0], unrelated) < sim_close


def test_openai_and_ollama_http_providers_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock urlopen 验证 OpenAI 兼容接口（认证头/乱序 index 还原/batch 分片）与 Ollama 接口。"""
    captured_requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    class _FakeHttpResponse:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._data = json.dumps(payload).encode("utf-8")

        def read(self) -> bytes:
            return self._data

        def __enter__(self) -> _FakeHttpResponse:
            return self

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

    def fake_urlopen(req: urllib.request.Request, timeout: float = 30.0) -> _FakeHttpResponse:
        headers = {k.lower(): v for k, v in req.header_items()}
        body = json.loads((req.data or b"{}").decode("utf-8"))
        captured_requests.append((req.full_url, headers, body))
        if req.full_url.endswith("/embeddings"):
            # 故意倒序返回 index，验证客户端能按 index 复原顺序
            inputs = body["input"]
            data = [
                {"index": idx, "embedding": [float(idx + 1), 0.5, 0.25]}
                for idx in reversed(range(len(inputs)))
            ]
            return _FakeHttpResponse({"data": data})
        if req.full_url.endswith("/api/embed"):
            inputs = body["input"]
            return _FakeHttpResponse({"embeddings": [[0.2, float(i), 0.4] for i in range(len(inputs))]})
        raise AssertionError(f"未预期请求路径: {req.full_url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    # 1) 缺少环境变量时显式报错（不泄露/不硬编码密钥）
    monkeypatch.delenv("TEST_DDNOVEL_EMBED_KEY", raising=False)
    openai_cfg = EmbeddingConfig(
        enabled=True,
        provider="openai",
        model="text-embedding-3-small",
        dim=3,
        api_key_env="TEST_DDNOVEL_EMBED_KEY",
    )
    plugin_missing_key = VectorPlugin(openai_cfg)
    with pytest.raises(EmbeddingError, match="TEST_DDNOVEL_EMBED_KEY"):
        plugin_missing_key.embed_texts(["测试鉴权"])

    # 2) 设置环境变量 + batch_size=2 分片测试
    monkeypatch.setenv("TEST_DDNOVEL_EMBED_KEY", "sk-secret-env-token")
    plugin_openai = VectorPlugin(
        openai_cfg,
        base_url="https://mock.embed.local/v1",
        batch_size=2,
    )
    blobs = plugin_openai.embed_texts(["第一条", "第二条", "第三条"])
    assert len(blobs) == 3
    assert len(captured_requests) == 2
    url1, hdr1, body1 = captured_requests[0]
    assert url1 == "https://mock.embed.local/v1/embeddings"
    assert hdr1["authorization"] == "Bearer sk-secret-env-token"
    assert body1 == {"model": "text-embedding-3-small", "input": ["第一条", "第二条"], "dimensions": 3}
    assert unpack_f32(blobs[0]) == pytest.approx((1.0, 0.5, 0.25))
    assert unpack_f32(blobs[1]) == pytest.approx((2.0, 0.5, 0.25))
    assert unpack_f32(blobs[2]) == pytest.approx((1.0, 0.5, 0.25))

    # 3) Ollama 本地接口测试（无需强制 api_key_env）
    captured_requests.clear()
    ollama_cfg = EmbeddingConfig(enabled=True, provider="ollama", model="bge-m3", dim=3)
    plugin_ollama = VectorPlugin(ollama_cfg, base_url="http://127.0.0.1:11434")
    ollama_blobs = plugin_ollama.embed_texts(["北境古城"])
    assert len(ollama_blobs) == 1
    assert captured_requests[0][0] == "http://127.0.0.1:11434/api/embed"
    assert unpack_f32(ollama_blobs[0]) == pytest.approx((0.2, 0.0, 0.4))

    # 4) HTTP 错误包装为 EmbeddingError
    def http_fail(req: urllib.request.Request, timeout: float = 30.0) -> Any:
        raise urllib.error.HTTPError(
            req.full_url,
            429,
            "Too Many Requests",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"rate limit"),
        )

    monkeypatch.setattr(urllib.request, "urlopen", http_fail)
    with pytest.raises(EmbeddingError, match="HTTP 429"):
        plugin_openai.embed_texts(["触发限流"])


def test_provider_registry_and_sqlite_vec_fallback() -> None:
    """支持自定义 Provider 注册与可选 sqlite-vec 扩展安全降级探测。"""
    providers = list_embedding_providers()
    assert {"hash", "openai", "dashscope", "ollama"}.issubset(set(providers))

    class _ConstantBackend:
        def __init__(self, dim: int) -> None:
            self._dim = dim

        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            return [[0.5] * self._dim for _ in texts]

    register_embedding_provider(
        "test_custom_const",
        lambda cfg, base_url, timeout_s: _ConstantBackend(cfg.dim or 2),
        overwrite=True,
    )
    cfg = EmbeddingConfig(enabled=True, provider="test_custom_const", model="const-1", dim=3)
    plugin = VectorPlugin(cfg)
    blobs = plugin.embed_texts(["任意文本"])
    assert unpack_f32(blobs[0]) == pytest.approx((0.5, 0.5, 0.5))

    conn = sqlite3.connect(":memory:")
    try:
        assert isinstance(try_load_sqlite_vec(conn), bool)
    finally:
        conn.close()
