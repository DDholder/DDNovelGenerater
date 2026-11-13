"""核心领域层：解析、匹配、索引与四大业务引擎 (T03 ~ T11)。

CLI 与 MCP 仅做薄封装转发，所有业务逻辑必须留在本包内。
"""

from .alias_matcher import AliasMatcher
from .indexer import SqliteIndexer, build_fts_query, tokenize_cjk
from .packet_builder import PacketBuilder, build_packet
from .parser import (
    ProgressiveParser,
    estimate_tokens,
    parse_blueprint_md,
    parse_setting_md,
    split_frontmatter,
    split_progressive_blocks,
)
from .vector_store import (
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

__all__ = [
    "AliasMatcher",
    "DeterministicHashEmbedder",
    "EmbeddingDimensionError",
    "EmbeddingError",
    "PacketBuilder",
    "ProgressiveParser",
    "SqliteIndexer",
    "VectorDocument",
    "VectorPlugin",
    "VectorSyncStats",
    "build_fts_query",
    "build_packet",
    "cosine_similarity",
    "estimate_tokens",
    "pack_f32",
    "parse_blueprint_md",
    "parse_setting_md",
    "rrf_fuse",
    "split_frontmatter",
    "split_progressive_blocks",
    "tokenize_cjk",
    "unpack_f32",
]
