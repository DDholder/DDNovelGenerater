"""可选插拔向量检索与 RRF 混合重排插件 (T07)。

设计约束：
1. 默认关闭（``EmbeddingConfig.enabled = False``）时 **零外部调用**、**零 API Key 要求**、
   直接透传返回 BM25/FTS5 结果；
2. 零重依赖退化：可选依赖 ``numpy`` 与 ``sqlite-vec`` 未安装时，完全基于 Python 标准库
   （``struct`` / ``math`` / ``sqlite3`` / ``urllib``）执行 ``vec_f32`` 小端序 float32 编解码、
   余弦相似度计算、SQLite ``embeddings`` 表持久化与 RRF 重排；若检测到已安装 ``numpy`` /
   ``sqlite-vec`` 则自动加速；
3. 严禁明文 API Key 落盘：远程 HTTP Embedding 提供方仅在调用期通过 ``os.environ[api_key_env]``
   按需读取凭证。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import sqlite3
import struct
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable

from .schemas import EmbeddingConfig, FtsScope, NovelConfig, SearchHit

HAS_NUMPY: Final[bool] = importlib.util.find_spec("numpy") is not None
if HAS_NUMPY:  # pragma: no cover - 可选性能加速路径
    import numpy as np  # type: ignore[import-not-found]
else:
    np = None  # type: ignore[assignment]

_BYTES_PER_F32: Final[int] = 4
_ASCII_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9]+")

EmbedFn = Callable[[Sequence[str]], Sequence[Sequence[float]]]
ProviderFactory = Callable[[EmbeddingConfig, str | None, float], "EmbeddingBackend"]


class EmbeddingError(RuntimeError):
    """向量生成、Provider 鉴权、网络请求或数值校验失败时抛出的异常。"""


class EmbeddingDimensionError(EmbeddingError, ValueError):
    """向量维度与配置不匹配或字节流长度非法时抛出的异常。"""


@runtime_checkable
class EmbeddingBackend(Protocol):
    """可插拔向量编码后端协议。"""

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """将文本序列编码为等维度浮点向量列表。"""
        ...


@dataclass(frozen=True, slots=True)
class VectorDocument:
    """待向量化索引的文档切片元数据与正文。"""

    doc_id: str
    text: str
    scope: FtsScope = FtsScope.SETTING
    chapter_num: int = 0
    entity_id: str | None = None
    excerpt: str | None = None

    @property
    def resolved_excerpt(self) -> str:
        """返回用于检索展示的摘要文本（未单独指定时回退为正文）。"""
        return self.excerpt if self.excerpt is not None else self.text


@dataclass(frozen=True, slots=True)
class VectorSyncStats:
    """向量索引增量同步执行统计。"""

    embedded: int = 0
    skipped_cached: int = 0
    deleted: int = 0


# ---------------------------------------------------------------------------
# float32 二进制编解码、余弦相似度与 sqlite-vec 安全加载
# ---------------------------------------------------------------------------


def pack_f32(vector: Sequence[float], *, expected_dim: int | None = None) -> bytes:
    """将浮点序列编码为兼容 ``sqlite-vec`` 的小端序 ``float32`` 紧凑字节流（``vec_f32``）。"""
    if isinstance(vector, (str, bytes, bytearray, memoryview)):
        raise EmbeddingDimensionError("vector 必须为数值序列，不能为字符串或字节串")

    values = [float(v) for v in vector]
    dim = len(values)
    if dim == 0:
        raise EmbeddingDimensionError("Embedding 向量不能为空")
    if expected_dim is not None and dim != expected_dim:
        raise EmbeddingDimensionError(
            f"Embedding 维度不匹配：期望 {expected_dim}，实际得到 {dim}"
        )

    for idx, val in enumerate(values):
        if not math.isfinite(val):
            raise EmbeddingError(f"Embedding 向量包含非法非有限数值（下标 {idx}）: {val!r}")

    return struct.pack(f"<{dim}f", *values)


def unpack_f32(
    blob: bytes | bytearray | memoryview,
    *,
    expected_dim: int | None = None,
) -> tuple[float, ...]:
    """将 ``vec_f32`` 小端序 ``float32`` 字节流解码为浮点元组。"""
    raw = bytes(blob)
    byte_len = len(raw)
    if byte_len == 0 or byte_len % _BYTES_PER_F32 != 0:
        raise EmbeddingDimensionError(
            f"vec_f32 字节长度非法：{byte_len}（必须为大于 0 且 4 的整数倍）"
        )

    dim = byte_len // _BYTES_PER_F32
    if expected_dim is not None and dim != expected_dim:
        raise EmbeddingDimensionError(
            f"vec_f32 解码维度不匹配：期望 {expected_dim}，实际为 {dim}"
        )

    return struct.unpack(f"<{dim}f", raw)


def _cosine_similarity_py(vec_a: Sequence[float], vec_b: Sequence[float]) -> float:
    """纯 Python 标准库余弦相似度计算（无任何第三方依赖）。"""
    dim_a = len(vec_a)
    dim_b = len(vec_b)
    if dim_a == 0 or dim_b == 0:
        raise EmbeddingDimensionError("计算余弦相似度的向量不能为空")
    if dim_a != dim_b:
        raise EmbeddingDimensionError(f"向量维度不一致：{dim_a} != {dim_b}")

    dot = math.fsum(a * b for a, b in zip(vec_a, vec_b, strict=True))
    norm_a_sq = math.fsum(a * a for a in vec_a)
    norm_b_sq = math.fsum(b * b for b in vec_b)
    if norm_a_sq == 0.0 or norm_b_sq == 0.0:
        return 0.0

    denom = math.sqrt(norm_a_sq) * math.sqrt(norm_b_sq)
    if denom == 0.0:
        return 0.0

    sim = dot / denom
    return max(-1.0, min(1.0, sim))


def cosine_similarity(
    vec_a: bytes | bytearray | memoryview | Sequence[float],
    vec_b: bytes | bytearray | memoryview | Sequence[float],
) -> float:
    """计算两向量（浮点序列或 ``vec_f32`` 字节流）的余弦相似度，范围 ``[-1.0, 1.0]``。"""
    if HAS_NUMPY and np is not None:  # pragma: no cover - numpy 加速路径
        arr_a = _to_numpy_f64(vec_a)
        arr_b = _to_numpy_f64(vec_b)
        if arr_a.shape != arr_b.shape:
            raise EmbeddingDimensionError(
                f"向量维度不一致：{arr_a.shape[0]} != {arr_b.shape[0]}"
            )
        norm_a = float(np.linalg.norm(arr_a))
        norm_b = float(np.linalg.norm(arr_b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        sim = float(np.dot(arr_a, arr_b) / (norm_a * norm_b))
        return max(-1.0, min(1.0, sim))

    seq_a = unpack_f32(vec_a) if isinstance(vec_a, (bytes, bytearray, memoryview)) else vec_a
    seq_b = unpack_f32(vec_b) if isinstance(vec_b, (bytes, bytearray, memoryview)) else vec_b
    return _cosine_similarity_py(seq_a, seq_b)


def _to_numpy_f64(vec: bytes | bytearray | memoryview | Sequence[float]) -> Any:
    """校验并转换为一维 float64 numpy 数组。"""
    if isinstance(vec, (bytes, bytearray, memoryview)):
        raw = bytes(vec)
        if not raw or len(raw) % _BYTES_PER_F32 != 0:
            raise EmbeddingDimensionError(
                f"vec_f32 字节长度非法：{len(raw)}（必须为大于 0 且 4 的整数倍）"
            )
        return np.frombuffer(raw, dtype="<f4").astype(np.float64)

    arr = np.asarray(vec, dtype=np.float64)
    if arr.ndim != 1 or arr.size == 0:
        raise EmbeddingDimensionError("向量必须为非空一维序列")
    return arr


def try_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """尝试向 SQLite 连接加载可选 ``sqlite-vec`` 扩展，失败时安全返回 ``False``。"""
    try:
        import sqlite_vec  # type: ignore[import-not-found]
    except ImportError:
        return False

    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        return True
    except (AttributeError, sqlite3.Error):
        return False
    finally:
        try:
            conn.enable_load_extension(False)
        except (AttributeError, sqlite3.Error):
            pass


# ---------------------------------------------------------------------------
# RRF (Reciprocal Rank Fusion) 混合重排核心算法
# ---------------------------------------------------------------------------


def rrf_fuse(
    fts_hits: Sequence[SearchHit],
    vector_hits: Sequence[SearchHit],
    *,
    rrf_k: int = 60,
    top_k: int = 10,
) -> list[SearchHit]:
    """使用 Reciprocal Rank Fusion (RRF) 融合 BM25 与向量语义两路召回结果。

    公式：``score(d) = 1 / (k + rank_bm25(d)) + 1 / (k + rank_vec(d))``，
    仅出现在单一路径的文档仅累加该路径分项。RRF 分数相同时按更高余弦相似度、
    更前 BM25 排名与 ``doc_id`` 字典序确定性破局。
    """
    if rrf_k < 1:
        raise ValueError(f"rrf_k 必须为正整数，收到: {rrf_k}")
    if top_k <= 0:
        return []

    sorted_fts = sorted(
        enumerate(fts_hits),
        key=lambda pair: (-float(pair[1].score), pair[0]),
    )
    bm25_rank: dict[str, int] = {}
    fts_by_id: dict[str, SearchHit] = {}
    for _, hit in sorted_fts:
        if hit.doc_id not in bm25_rank:
            bm25_rank[hit.doc_id] = len(bm25_rank) + 1
            fts_by_id[hit.doc_id] = hit

    sorted_vec = sorted(
        enumerate(vector_hits),
        key=lambda pair: (-float(pair[1].score), pair[0]),
    )
    vec_rank: dict[str, int] = {}
    vec_sim: dict[str, float] = {}
    vec_by_id: dict[str, SearchHit] = {}
    for _, hit in sorted_vec:
        if hit.doc_id not in vec_rank:
            vec_rank[hit.doc_id] = len(vec_rank) + 1
            vec_sim[hit.doc_id] = float(hit.score)
            vec_by_id[hit.doc_id] = hit

    all_doc_ids = bm25_rank.keys() | vec_rank.keys()
    fused: list[tuple[tuple[float, float, int, str], SearchHit]] = []
    for doc_id in all_doc_ids:
        score = 0.0
        if doc_id in bm25_rank:
            score += 1.0 / (rrf_k + bm25_rank[doc_id])
        if doc_id in vec_rank:
            score += 1.0 / (rrf_k + vec_rank[doc_id])

        if doc_id in fts_by_id:
            base_hit = fts_by_id[doc_id]
            if not base_hit.excerpt and doc_id in vec_by_id and vec_by_id[doc_id].excerpt:
                base_hit = base_hit.model_copy(update={"excerpt": vec_by_id[doc_id].excerpt})
        else:
            base_hit = vec_by_id[doc_id]

        updated_hit = base_hit.model_copy(update={"score": score})
        sort_key = (
            -round(score, 12),
            -round(vec_sim.get(doc_id, float("-inf")), 9),
            bm25_rank.get(doc_id, sys.maxsize),
            doc_id,
        )
        fused.append((sort_key, updated_hit))

    fused.sort(key=lambda item: item[0])
    return [hit for _, hit in fused[:top_k]]


# ---------------------------------------------------------------------------
# 可插拔 Embedding Provider 注册表与内置后端
# ---------------------------------------------------------------------------


class DeterministicHashEmbedder:
    """离线确定性字符/词元特征哈希向量器（不发起任何网络请求，无需 API Key）。"""

    def __init__(self, dim: int) -> None:
        if dim < 1:
            raise EmbeddingDimensionError(f"dim 必须为正整数，收到: {dim}")
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """将文本列表确定性映射为单位 L2 范数向量。"""
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = self._tokenize(text)
        if not tokens:
            return vec

        for tok in tokens:
            digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=16).digest()
            bucket = int.from_bytes(digest[:8], "little") % self.dim
            sign = 1.0 if (digest[8] & 1) == 0 else -1.0
            vec[bucket] += sign

        norm = math.sqrt(math.fsum(v * v for v in vec))
        if norm == 0.0:
            return [0.0] * self.dim
        return [v / norm for v in vec]

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        chars = [ch.lower() for ch in text if not ch.isspace()]
        tokens: list[str] = [f"u:{ch}" for ch in chars]
        for idx in range(len(chars) - 1):
            tokens.append(f"b:{chars[idx]}{chars[idx + 1]}")
        for match in _ASCII_TOKEN_RE.finditer(text.lower()):
            tokens.append(f"w:{match.group(0)}")
        return tokens


class _CallableEmbedderAdapter:
    """将用户注入的函数或协议对象适配为标准 ``EmbeddingBackend``。"""

    def __init__(self, embedder: EmbedFn | EmbeddingBackend) -> None:
        self._embedder = embedder

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        if isinstance(self._embedder, EmbeddingBackend):
            return self._embedder.embed(texts)
        return self._embedder(texts)


class _OpenAICompatibleEmbedder:
    """OpenAI / DashScope / SiliconFlow 等兼容 ``/embeddings`` 接口的 HTTP 客户端。"""

    _DEFAULT_BASE_URLS: Final[dict[str, str]] = {
        "openai": "https://api.openai.com/v1",
        "openai_compatible": "https://api.openai.com/v1",
        "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "siliconflow": "https://api.siliconflow.cn/v1",
    }

    def __init__(
        self,
        config: EmbeddingConfig,
        base_url: str | None,
        timeout_s: float,
    ) -> None:
        self._config = config
        self._timeout_s = timeout_s
        provider_key = config.provider.strip().lower()
        env_url = os.environ.get("DDNOVEL_EMBEDDING_BASE_URL", "").strip()
        if not env_url and provider_key in {"openai", "openai_compatible"}:
            env_url = os.environ.get("OPENAI_BASE_URL", "").strip()
        default_url = self._DEFAULT_BASE_URLS.get(provider_key, "https://api.openai.com/v1")
        chosen_url = base_url.strip() if base_url else (env_url or default_url)
        self._endpoint = f"{chosen_url.rstrip('/')}/embeddings"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        env_name = self._config.api_key_env.strip()
        if not env_name:
            raise EmbeddingError(
                f"Provider '{self._config.provider}' 必须配置 embedding.api_key_env 环境变量名"
            )
        api_key = os.environ.get(env_name, "").strip()
        if not api_key:
            raise EmbeddingError(f"环境变量 '{env_name}' 未设置或为空，无法调用向量服务")

        payload_dict: dict[str, Any] = {
            "model": self._config.model,
            "input": list(texts),
        }
        provider_key = self._config.provider.strip().lower()
        if self._config.dim is not None and (
            provider_key == "dashscope"
            or (provider_key == "openai" and self._config.model.startswith("text-embedding-3"))
        ):
            payload_dict["dimensions"] = self._config.dim

        data = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        raw_resp = _post_json(req, timeout_s=self._timeout_s, provider=self._config.provider)
        items = raw_resp.get("data")
        if not isinstance(items, list):
            raise EmbeddingError(
                f"Provider '{self._config.provider}' 响应格式异常：缺少 data 列表"
            )

        sorted_items = sorted(
            enumerate(items),
            key=lambda pair: int(pair[1].get("index", pair[0])) if isinstance(pair[1], dict) else pair[0],
        )
        vectors: list[list[float]] = []
        for _, item in sorted_items:
            if not isinstance(item, dict) or not isinstance(item.get("embedding"), Sequence):
                raise EmbeddingError(
                    f"Provider '{self._config.provider}' 响应项缺少 embedding 字段"
                )
            vectors.append([float(v) for v in item["embedding"]])
        return vectors


class _OllamaEmbedder:
    """本地或远程 Ollama ``/api/embed`` 向量接口客户端。"""

    def __init__(
        self,
        config: EmbeddingConfig,
        base_url: str | None,
        timeout_s: float,
    ) -> None:
        self._config = config
        self._timeout_s = timeout_s
        env_url = (
            os.environ.get("DDNOVEL_EMBEDDING_BASE_URL", "").strip()
            or os.environ.get("OLLAMA_HOST", "").strip()
        )
        chosen_url = base_url.strip() if base_url else (env_url or "http://127.0.0.1:11434")
        self._endpoint = f"{chosen_url.rstrip('/')}/api/embed"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        headers: dict[str, str] = {"Content-Type": "application/json"}
        env_name = self._config.api_key_env.strip()
        if env_name:
            api_key = os.environ.get(env_name, "").strip()
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

        payload = json.dumps(
            {"model": self._config.model, "input": list(texts)},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(self._endpoint, data=payload, method="POST", headers=headers)
        raw_resp = _post_json(req, timeout_s=self._timeout_s, provider=self._config.provider)
        embeddings = raw_resp.get("embeddings")
        if not isinstance(embeddings, list):
            raise EmbeddingError("Ollama 响应格式异常：缺少 embeddings 列表")
        return [[float(v) for v in vec] for vec in embeddings]


def _post_json(req: urllib.request.Request, *, timeout_s: float, provider: str) -> dict[str, Any]:
    """执行 JSON POST 请求并统一封装 HTTP / 网络 / 解析错误。"""
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:300]
        except OSError:
            err_body = ""
        raise EmbeddingError(
            f"Provider '{provider}' 请求返回 HTTP {exc.code}: {err_body or exc.reason}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EmbeddingError(f"Provider '{provider}' 网络请求失败: {exc}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise EmbeddingError(f"Provider '{provider}' 返回了非法的 JSON 响应") from exc

    if not isinstance(parsed, dict):
        raise EmbeddingError(f"Provider '{provider}' 响应顶层必须为 JSON 对象")
    return parsed


_PROVIDER_REGISTRY: dict[str, ProviderFactory] = {}


def register_embedding_provider(
    name: str,
    factory: ProviderFactory,
    *,
    overwrite: bool = False,
) -> None:
    """注册自定义 Embedding Provider 工厂函数。"""
    normalized = name.strip().lower()
    if not normalized:
        raise ValueError("Provider 名称不能为空")
    if normalized in _PROVIDER_REGISTRY and not overwrite:
        raise ValueError(f"Provider '{normalized}' 已注册")
    _PROVIDER_REGISTRY[normalized] = factory


def list_embedding_providers() -> list[str]:
    """返回已注册的 Embedding Provider 名称列表（按字典序）。"""
    return sorted(_PROVIDER_REGISTRY)


def _build_hash_provider(
    config: EmbeddingConfig,
    base_url: str | None,
    timeout_s: float,
) -> EmbeddingBackend:
    del base_url, timeout_s
    if config.dim is None:
        raise EmbeddingDimensionError("启用 hash provider 时必须指定 embedding.dim")
    return DeterministicHashEmbedder(config.dim)


def _build_openai_provider(
    config: EmbeddingConfig,
    base_url: str | None,
    timeout_s: float,
) -> EmbeddingBackend:
    return _OpenAICompatibleEmbedder(config, base_url=base_url, timeout_s=timeout_s)


def _build_ollama_provider(
    config: EmbeddingConfig,
    base_url: str | None,
    timeout_s: float,
) -> EmbeddingBackend:
    return _OllamaEmbedder(config, base_url=base_url, timeout_s=timeout_s)


for _hash_name in ("hash", "deterministic", "mock"):
    register_embedding_provider(_hash_name, _build_hash_provider)
for _openai_name in ("openai", "openai_compatible", "dashscope", "siliconflow"):
    register_embedding_provider(_openai_name, _build_openai_provider)
register_embedding_provider("ollama", _build_ollama_provider)


# ---------------------------------------------------------------------------
# VectorPlugin 核心插件类
# ---------------------------------------------------------------------------


def _resolve_scope(scope: FtsScope | str | None) -> FtsScope | None:
    """将可选检索域参数规范化为 ``FtsScope`` 枚举。"""
    if scope is None:
        return None
    if isinstance(scope, FtsScope):
        return scope
    return FtsScope(str(scope))


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """判断 SQLite 连接中是否存在指定表或视图。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


class VectorPlugin:
    """可选插拔语义向量检索与 RRF 混合重排插件。"""

    def __init__(
        self,
        config: EmbeddingConfig | NovelConfig | None = None,
        *,
        embedder: EmbedFn | EmbeddingBackend | None = None,
        conn: sqlite3.Connection | None = None,
        base_url: str | None = None,
        batch_size: int = 64,
        timeout_s: float = 30.0,
    ) -> None:
        if batch_size < 1:
            raise ValueError(f"batch_size 必须为正整数，收到: {batch_size}")
        if timeout_s <= 0:
            raise ValueError(f"timeout_s 必须大于 0，收到: {timeout_s}")

        if isinstance(config, NovelConfig):
            resolved_cfg = config.embedding
        elif isinstance(config, EmbeddingConfig):
            resolved_cfg = config
        elif config is None:
            resolved_cfg = EmbeddingConfig(enabled=False)
        else:
            raise TypeError(
                f"config 必须为 EmbeddingConfig、NovelConfig 或 None，实际得到 {type(config).__name__}"
            )

        self._config: EmbeddingConfig = resolved_cfg
        self._explicit_embedder = _CallableEmbedderAdapter(embedder) if embedder is not None else None
        self._backend: EmbeddingBackend | None = self._explicit_embedder
        self._conn: sqlite3.Connection | None = conn
        self._base_url: str | None = base_url
        self._batch_size: int = batch_size
        self._timeout_s: float = timeout_s

        # 将“纯文本缓存”与“已索引语料库”严格分离，避免 rerank 缓存污染纯向量召回
        self._text_vec_cache: dict[str, bytes] = {}
        self._indexed_docs: dict[str, tuple[VectorDocument, str, bytes]] = {}

    @property
    def config(self) -> EmbeddingConfig:
        """返回只读向量配置对象。"""
        return self._config

    @property
    def dim(self) -> int | None:
        """返回配置的向量维度（关闭且未配置时为 ``None``）。"""
        return self._config.dim

    @property
    def rrf_k(self) -> int:
        """返回 RRF 融合常数 ``k``。"""
        return self._config.rrf_k

    def is_enabled(self) -> bool:
        """返回向量插件是否已启用（关闭时所有方法保证零外部调用）。"""
        return bool(self._config.enabled)

    def compute_content_hash(self, text: str) -> str:
        """基于 provider、model、dim 与文本内容计算缓存防重放 SHA-256 哈希。"""
        material = f"{self._config.provider}\x00{self._config.model}\x00{self._config.dim}\x00{text}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _get_backend(self) -> EmbeddingBackend:
        """延迟构造或返回当前激活的向量编码后端（仅在启用且确需编码时调用）。"""
        if self._backend is not None:
            return self._backend
        provider_key = self._config.provider.strip().lower()
        factory = _PROVIDER_REGISTRY.get(provider_key)
        if factory is None:
            raise EmbeddingError(
                f"未知的 Embedding Provider '{self._config.provider}'，"
                f"已注册提供方: {list_embedding_providers()}"
            )
        self._backend = factory(self._config, self._base_url, self._timeout_s)
        return self._backend

    def _resolve_conn(self, conn: sqlite3.Connection | None) -> sqlite3.Connection | None:
        return conn if conn is not None else self._conn

    def ensure_schema(self, conn: sqlite3.Connection | None = None) -> None:
        """在 SQLite 连接上确保 ``embeddings`` 表结构存在（对齐架构 ER 图）。"""
        target_conn = self._resolve_conn(conn)
        if target_conn is None:
            raise ValueError("ensure_schema 需要提供 SQLite 连接")
        if _table_exists(target_conn, "embeddings"):
            return
        target_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS embeddings (
                doc_id TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                dim INTEGER NOT NULL,
                vec_f32 BLOB NOT NULL
            )
            """
        )

    def embed_texts(self, texts: Sequence[str]) -> list[bytes]:
        """批量将文本编码为小端序 ``float32`` 字节流；插件关闭时零外部调用直接返回空列表。"""
        if not self.is_enabled() or not texts:
            return []

        expected_dim = self._config.dim
        if expected_dim is None:
            raise EmbeddingDimensionError("embedding.enabled=True 时必须配置正整数 dim")

        results: list[bytes | None] = [None] * len(texts)
        non_empty_indices: list[int] = []
        non_empty_texts: list[str] = []
        zero_blob = b"\x00" * (_BYTES_PER_F32 * expected_dim)

        for idx, text in enumerate(texts):
            if not text or not text.strip():
                results[idx] = zero_blob
            else:
                non_empty_indices.append(idx)
                non_empty_texts.append(text)

        if non_empty_texts:
            backend = self._get_backend()
            for offset in range(0, len(non_empty_texts), self._batch_size):
                chunk_texts = non_empty_texts[offset : offset + self._batch_size]
                chunk_indices = non_empty_indices[offset : offset + self._batch_size]
                raw_vectors = backend.embed(chunk_texts)
                if len(raw_vectors) != len(chunk_texts):
                    raise EmbeddingError(
                        f"Embedding 后端返回数量 ({len(raw_vectors)}) "
                        f"与输入批次大小 ({len(chunk_texts)}) 不一致"
                    )
                for target_idx, raw_vec in zip(chunk_indices, raw_vectors, strict=True):
                    results[target_idx] = pack_f32(raw_vec, expected_dim=expected_dim)

        return [blob for blob in results if blob is not None]

    def index_documents(
        self,
        docs: Sequence[VectorDocument],
        *,
        conn: sqlite3.Connection | None = None,
        delete_stale: bool = False,
    ) -> VectorSyncStats:
        """按 ``content_hash`` 增量编码并持久化文档向量；关闭时零调用立即返回。"""
        if not self.is_enabled():
            return VectorSyncStats()

        expected_dim = self._config.dim
        if expected_dim is None:
            raise EmbeddingDimensionError("embedding.enabled=True 时必须配置正整数 dim")

        deduped_docs: dict[str, VectorDocument] = {}
        for doc in docs:
            deduped_docs[doc.doc_id] = doc

        target_conn = self._resolve_conn(conn)
        if target_conn is not None:
            return self._index_to_sqlite(
                target_conn,
                deduped_docs,
                expected_dim=expected_dim,
                delete_stale=delete_stale,
            )
        return self._index_to_memory(
            deduped_docs,
            expected_dim=expected_dim,
            delete_stale=delete_stale,
        )

    def _index_to_memory(
        self,
        deduped_docs: dict[str, VectorDocument],
        *,
        expected_dim: int,
        delete_stale: bool,
    ) -> VectorSyncStats:
        expected_byte_len = expected_dim * _BYTES_PER_F32
        skipped = 0
        to_embed: list[tuple[VectorDocument, str]] = []

        for doc in deduped_docs.values():
            c_hash = self.compute_content_hash(doc.text)
            cached = self._indexed_docs.get(doc.doc_id)
            if cached is not None and cached[1] == c_hash and len(cached[2]) == expected_byte_len:
                self._indexed_docs[doc.doc_id] = (doc, c_hash, cached[2])
                skipped += 1
            else:
                to_embed.append((doc, c_hash))

        embedded_count = 0
        if to_embed:
            blobs = self.embed_texts([item[0].text for item in to_embed])
            for (doc, c_hash), blob in zip(to_embed, blobs, strict=True):
                self._indexed_docs[doc.doc_id] = (doc, c_hash, blob)
                self._text_vec_cache[c_hash] = blob
            embedded_count = len(to_embed)

        deleted_count = 0
        if delete_stale:
            stale_ids = [doc_id for doc_id in self._indexed_docs if doc_id not in deduped_docs]
            for doc_id in stale_ids:
                self._indexed_docs.pop(doc_id, None)
            deleted_count = len(stale_ids)

        return VectorSyncStats(
            embedded=embedded_count,
            skipped_cached=skipped,
            deleted=deleted_count,
        )

    def _index_to_sqlite(
        self,
        target_conn: sqlite3.Connection,
        deduped_docs: dict[str, VectorDocument],
        *,
        expected_dim: int,
        delete_stale: bool,
    ) -> VectorSyncStats:
        self.ensure_schema(target_conn)
        had_tx = target_conn.in_transaction

        existing_rows = {
            str(row[0]): (str(row[1]), int(row[2]))
            for row in target_conn.execute("SELECT doc_id, content_hash, dim FROM embeddings").fetchall()
        }

        skipped = 0
        to_embed: list[tuple[VectorDocument, str]] = []
        for doc in deduped_docs.values():
            c_hash = self.compute_content_hash(doc.text)
            prev = existing_rows.get(doc.doc_id)
            if prev is not None and prev == (c_hash, expected_dim):
                skipped += 1
                prev_mem = self._indexed_docs.get(doc.doc_id)
                if prev_mem is not None and prev_mem[1] == c_hash:
                    self._indexed_docs[doc.doc_id] = (doc, c_hash, prev_mem[2])
            else:
                to_embed.append((doc, c_hash))

        embedded_count = 0
        if to_embed:
            blobs = self.embed_texts([item[0].text for item in to_embed])
            upsert_rows: list[tuple[str, str, int, bytes]] = []
            for (doc, c_hash), blob in zip(to_embed, blobs, strict=True):
                upsert_rows.append((doc.doc_id, c_hash, expected_dim, blob))
                self._indexed_docs[doc.doc_id] = (doc, c_hash, blob)
                self._text_vec_cache[c_hash] = blob

            target_conn.executemany(
                """
                INSERT INTO embeddings (doc_id, content_hash, dim, vec_f32)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(doc_id) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    dim = excluded.dim,
                    vec_f32 = excluded.vec_f32
                """,
                upsert_rows,
            )
            embedded_count = len(to_embed)

        deleted_count = 0
        if delete_stale:
            stale_ids = [doc_id for doc_id in existing_rows if doc_id not in deduped_docs]
            if stale_ids:
                target_conn.executemany(
                    "DELETE FROM embeddings WHERE doc_id = ?",
                    [(doc_id,) for doc_id in stale_ids],
                )
                for doc_id in stale_ids:
                    self._indexed_docs.pop(doc_id, None)
                deleted_count = len(stale_ids)

        if not had_tx and target_conn.in_transaction:
            target_conn.commit()

        return VectorSyncStats(
            embedded=embedded_count,
            skipped_cached=skipped,
            deleted=deleted_count,
        )

    def delete_documents(
        self,
        doc_ids: Iterable[str],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """从内存与 SQLite ``embeddings`` 中移除指定 ``doc_id`` 的向量缓存。"""
        unique_ids = list(dict.fromkeys(str(doc_id) for doc_id in doc_ids if str(doc_id)))
        if not unique_ids:
            return 0

        mem_deleted = 0
        for doc_id in unique_ids:
            if self._indexed_docs.pop(doc_id, None) is not None:
                mem_deleted += 1

        target_conn = self._resolve_conn(conn)
        if target_conn is None or not _table_exists(target_conn, "embeddings"):
            return mem_deleted

        had_tx = target_conn.in_transaction
        cursor = target_conn.executemany(
            "DELETE FROM embeddings WHERE doc_id = ?",
            [(doc_id,) for doc_id in unique_ids],
        )
        deleted_rows = int(cursor.rowcount) if cursor.rowcount is not None and cursor.rowcount >= 0 else 0
        if not had_tx and target_conn.in_transaction:
            target_conn.commit()
        return max(mem_deleted, deleted_rows)

    def prune_orphan_embeddings(self, conn: sqlite3.Connection | None = None) -> int:
        """清理 SQLite 中已不在 ``fts_docs`` 内的孤儿向量记录。"""
        target_conn = self._resolve_conn(conn)
        if (
            target_conn is None
            or not _table_exists(target_conn, "embeddings")
            or not _table_exists(target_conn, "fts_docs")
        ):
            return 0

        had_tx = target_conn.in_transaction
        cursor = target_conn.execute(
            "DELETE FROM embeddings WHERE doc_id NOT IN (SELECT doc_id FROM fts_docs)"
        )
        deleted = int(cursor.rowcount) if cursor.rowcount is not None and cursor.rowcount >= 0 else 0
        if not had_tx and target_conn.in_transaction:
            target_conn.commit()
        return deleted

    def clear(self, *, conn: sqlite3.Connection | None = None) -> None:
        """清空内存向量缓存，并在提供连接时清空 ``embeddings`` 表。"""
        self._text_vec_cache.clear()
        self._indexed_docs.clear()
        target_conn = self._resolve_conn(conn)
        if target_conn is not None and _table_exists(target_conn, "embeddings"):
            had_tx = target_conn.in_transaction
            target_conn.execute("DELETE FROM embeddings")
            if not had_tx and target_conn.in_transaction:
                target_conn.commit()

    def _collect_corpus_candidates(
        self,
        *,
        scope: FtsScope | None,
        conn: sqlite3.Connection | None,
    ) -> list[tuple[SearchHit, bytes]]:
        """从 SQLite ``embeddings``（联合 ``fts_docs``）或内存索引中读取当前有效维度的候选向量。"""
        expected_dim = self._config.dim
        if expected_dim is None:
            return []
        expected_byte_len = expected_dim * _BYTES_PER_F32

        target_conn = self._resolve_conn(conn)
        if target_conn is not None:
            if not _table_exists(target_conn, "embeddings"):
                return []

            has_fts = _table_exists(target_conn, "fts_docs")
            if has_fts:
                rows = target_conn.execute(
                    """
                    SELECT e.doc_id, f.scope, f.chapter_num, f.entity_id, f.raw_excerpt, e.vec_f32
                    FROM embeddings AS e
                    LEFT JOIN fts_docs AS f ON f.doc_id = e.doc_id
                    WHERE e.dim = ?
                    """,
                    (expected_dim,),
                ).fetchall()
            else:
                rows = target_conn.execute(
                    "SELECT doc_id, NULL, NULL, NULL, NULL, vec_f32 FROM embeddings WHERE dim = ?",
                    (expected_dim,),
                ).fetchall()

            candidates: list[tuple[SearchHit, bytes]] = []
            seen_ids: set[str] = set()
            for row in rows:
                doc_id = str(row[0])
                if doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)

                blob = bytes(row[5])
                if len(blob) != expected_byte_len:
                    continue

                raw_scope = row[1]
                mem_entry = self._indexed_docs.get(doc_id)
                if raw_scope is not None:
                    row_scope = FtsScope(str(raw_scope))
                    chapter_num = int(row[2] or 0)
                    entity_id = str(row[3]) if row[3] is not None else None
                    excerpt = str(row[4] or "")
                elif mem_entry is not None:
                    doc = mem_entry[0]
                    row_scope = doc.scope
                    chapter_num = doc.chapter_num
                    entity_id = doc.entity_id
                    excerpt = doc.resolved_excerpt
                else:
                    row_scope = FtsScope.SETTING
                    chapter_num = 0
                    entity_id = None
                    excerpt = ""

                if scope is not None and row_scope != scope:
                    continue

                candidates.append(
                    (
                        SearchHit(
                            doc_id=doc_id,
                            scope=row_scope,
                            chapter_num=chapter_num,
                            entity_id=entity_id,
                            excerpt=excerpt,
                            score=0.0,
                        ),
                        blob,
                    )
                )
            return candidates

        mem_candidates: list[tuple[SearchHit, bytes]] = []
        for doc_id, (doc, _, blob) in self._indexed_docs.items():
            if len(blob) != expected_byte_len:
                continue
            if scope is not None and doc.scope != scope:
                continue
            mem_candidates.append(
                (
                    SearchHit(
                        doc_id=doc_id,
                        scope=doc.scope,
                        chapter_num=doc.chapter_num,
                        entity_id=doc.entity_id,
                        excerpt=doc.resolved_excerpt,
                        score=0.0,
                    ),
                    blob,
                )
            )
        return mem_candidates

    def _batch_cosine_scores(self, query_blob: bytes, doc_blobs: Sequence[bytes]) -> list[float]:
        """计算查询向量与一批文档向量的余弦相似度。"""
        if not doc_blobs:
            return []
        if HAS_NUMPY and np is not None:  # pragma: no cover - numpy 加速路径
            q_arr = np.frombuffer(query_blob, dtype="<f4").astype(np.float64)
            q_norm = float(np.linalg.norm(q_arr))
            if q_norm == 0.0:
                return [0.0] * len(doc_blobs)
            dim = int(q_arr.shape[0])
            joined = b"".join(doc_blobs)
            mat = np.frombuffer(joined, dtype="<f4").reshape(len(doc_blobs), dim).astype(np.float64)
            norms = np.linalg.norm(mat, axis=1) * q_norm
            dots = mat @ q_arr
            sims = np.zeros(len(doc_blobs), dtype=np.float64)
            valid = norms > 0.0
            sims[valid] = np.clip(dots[valid] / norms[valid], -1.0, 1.0)
            return [float(x) for x in sims]

        q_vec = unpack_f32(query_blob)
        return [_cosine_similarity_py(q_vec, unpack_f32(blob)) for blob in doc_blobs]

    def search_vector(
        self,
        query: str,
        *,
        top_k: int = 10,
        scope: FtsScope | str | None = None,
        conn: sqlite3.Connection | None = None,
        min_similarity: float | None = None,
    ) -> list[SearchHit]:
        """仅基于语义向量索引执行 Top-K 余弦相似度检索。"""
        if not self.is_enabled() or top_k <= 0 or not query or not query.strip():
            return []

        scope_val = _resolve_scope(scope)
        candidates = self._collect_corpus_candidates(scope=scope_val, conn=conn)
        if not candidates:
            return []

        query_blobs = self.embed_texts([query])
        if not query_blobs:
            return []
        query_blob = query_blobs[0]

        scores = self._batch_cosine_scores(query_blob, [blob for _, blob in candidates])
        scored_hits: list[SearchHit] = []
        for (hit, _), sim in zip(candidates, scores, strict=True):
            if min_similarity is not None and sim < min_similarity:
                continue
            scored_hits.append(hit.model_copy(update={"score": sim}))

        scored_hits.sort(key=lambda h: (-round(h.score, 9), h.doc_id))
        return scored_hits[:top_k]

    def hybrid_rerank(
        self,
        query: str,
        fts_hits: Sequence[SearchHit],
        top_k: int = 10,
        *,
        scope: FtsScope | str | None = None,
        conn: sqlite3.Connection | None = None,
        vector_hits: Sequence[SearchHit] | None = None,
        candidate_multiplier: int = 3,
        embed_missing: bool = True,
        min_similarity: float | None = None,
    ) -> list[SearchHit]:
        """执行 BM25/FTS5 + 向量语义 RRF 混合重排。

        当插件关闭或查询为空时 **零外部调用** 直接截断返回 ``fts_hits[:top_k]``。
        """
        scope_val = _resolve_scope(scope)
        effective_fts = (
            [hit for hit in fts_hits if hit.scope == scope_val]
            if scope_val is not None
            else list(fts_hits)
        )
        if top_k <= 0:
            return []
        if not self.is_enabled() or not query or not query.strip():
            return list(effective_fts[:top_k])

        if vector_hits is not None:
            effective_vec = (
                [hit for hit in vector_hits if hit.scope == scope_val]
                if scope_val is not None
                else list(vector_hits)
            )
            return rrf_fuse(
                effective_fts,
                effective_vec,
                rrf_k=self._config.rrf_k,
                top_k=top_k,
            )

        corpus_candidates = self._collect_corpus_candidates(scope=scope_val, conn=conn)
        corpus_by_id: dict[str, tuple[SearchHit, bytes]] = dict(
            (hit.doc_id, (hit, blob)) for hit, blob in corpus_candidates
        )

        deduped_fts: list[SearchHit] = []
        seen_fts_ids: set[str] = set()
        for hit in effective_fts:
            if hit.doc_id not in seen_fts_ids:
                seen_fts_ids.add(hit.doc_id)
                deduped_fts.append(hit)

        missing_hash_to_text: dict[str, str] = {}
        fts_resolved: list[tuple[SearchHit, str, bytes | None]] = []
        for hit in deduped_fts:
            if hit.doc_id in corpus_by_id:
                fts_resolved.append((hit, "", corpus_by_id[hit.doc_id][1]))
                continue
            c_hash = self.compute_content_hash(hit.excerpt)
            cached_blob = self._text_vec_cache.get(c_hash)
            if cached_blob is not None:
                fts_resolved.append((hit, c_hash, cached_blob))
            elif embed_missing and hit.excerpt.strip():
                missing_hash_to_text.setdefault(c_hash, hit.excerpt)
                fts_resolved.append((hit, c_hash, None))
            else:
                fts_resolved.append((hit, c_hash, None))

        has_any_vector_target = bool(corpus_candidates) or any(
            blob is not None or (c_hash in missing_hash_to_text)
            for _, c_hash, blob in fts_resolved
        )
        if not has_any_vector_target:
            return list(deduped_fts[:top_k])

        missing_hashes = list(missing_hash_to_text.keys())
        texts_to_embed = [query] + [missing_hash_to_text[h] for h in missing_hashes]
        encoded_blobs = self.embed_texts(texts_to_embed)
        if not encoded_blobs:
            return list(deduped_fts[:top_k])

        query_blob = encoded_blobs[0]
        for c_hash, blob in zip(missing_hashes, encoded_blobs[1:], strict=True):
            self._text_vec_cache[c_hash] = blob

        vector_pool_hits: list[SearchHit] = []
        vector_pool_blobs: list[bytes] = []
        in_fts_flags: list[bool] = []

        for hit, blob in corpus_candidates:
            vector_pool_hits.append(hit)
            vector_pool_blobs.append(blob)
            in_fts_flags.append(hit.doc_id in seen_fts_ids)

        for hit, c_hash, existing_blob in fts_resolved:
            if hit.doc_id in corpus_by_id:
                continue
            blob = existing_blob if existing_blob is not None else self._text_vec_cache.get(c_hash)
            if blob is None:
                continue
            vector_pool_hits.append(hit)
            vector_pool_blobs.append(blob)
            in_fts_flags.append(True)

        if not vector_pool_blobs:
            return list(deduped_fts[:top_k])

        sims = self._batch_cosine_scores(query_blob, vector_pool_blobs)
        computed_vector_hits: list[SearchHit] = []
        for hit, sim, is_fts in zip(vector_pool_hits, sims, in_fts_flags, strict=True):
            if min_similarity is not None and not is_fts and sim < min_similarity:
                continue
            computed_vector_hits.append(hit.model_copy(update={"score": sim}))

        computed_vector_hits.sort(key=lambda h: (-round(h.score, 9), h.doc_id))
        vec_limit = max(top_k * max(1, candidate_multiplier), len(deduped_fts), 20)

        return rrf_fuse(
            deduped_fts,
            computed_vector_hits[:vec_limit],
            rrf_k=self._config.rrf_k,
            top_k=top_k,
        )


__all__ = [
    "DeterministicHashEmbedder",
    "EmbedFn",
    "EmbeddingBackend",
    "EmbeddingDimensionError",
    "EmbeddingError",
    "HAS_NUMPY",
    "VectorDocument",
    "VectorPlugin",
    "VectorSyncStats",
    "cosine_similarity",
    "list_embedding_providers",
    "pack_f32",
    "register_embedding_provider",
    "rrf_fuse",
    "try_load_sqlite_vec",
    "unpack_f32",
]
