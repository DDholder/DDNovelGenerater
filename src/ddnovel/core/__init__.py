"""核心领域层：解析、匹配、索引与四大业务引擎 (T03 ~ T11)。

CLI 与 MCP 仅做薄封装转发，所有业务逻辑必须留在本包内。
"""

from .alias_matcher import AliasMatcher
from .impact import ImpactAnalyzer, scan_setting_impact
from .indexer import SqliteIndexer, build_fts_query, tokenize_cjk
from .lifecycle import LifecycleManager
from .packet_builder import PacketBuilder, build_packet
from .parser import (
    ProgressiveParser,
    estimate_tokens,
    lint_model_setting_doc,
    parse_blueprint_md,
    parse_setting_md,
    split_frontmatter,
    split_progressive_blocks,
)
from .radar import ForeshadowRadar, scan_foreshadow_radar
from .reader_export import (
    DDNOVEL_READER_DIR_ENV_VAR,
    ArchivedAssetResult,
    ChapterCastProfile,
    ChapterResourcePlan,
    ReaderExporter,
    ReaderExportResult,
    ReaderResourceRequirement,
    ReaderValidationResult,
    SyncArchiveResult,
    build_reader_patch,
    discover_reader_dir,
    is_placeholder_png,
    pack_reader_book,
    resolve_reader_mcp_command,
    resolve_reader_mcp_script,
    resolve_reader_tool_script,
    validate_reader_target,
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
    "DDNOVEL_READER_DIR_ENV_VAR",
    "AliasMatcher",
    "ArchivedAssetResult",
    "ChapterCastProfile",
    "ChapterResourcePlan",
    "DeterministicHashEmbedder",
    "EmbeddingDimensionError",
    "EmbeddingError",
    "ForeshadowRadar",
    "ImpactAnalyzer",
    "LifecycleManager",
    "PacketBuilder",
    "ProgressiveParser",
    "ReaderExportResult",
    "ReaderExporter",
    "ReaderResourceRequirement",
    "ReaderValidationResult",
    "SqliteIndexer",
    "SyncArchiveResult",
    "VectorDocument",
    "VectorPlugin",
    "VectorSyncStats",
    "build_fts_query",
    "build_packet",
    "build_reader_patch",
    "cosine_similarity",
    "discover_reader_dir",
    "estimate_tokens",
    "is_placeholder_png",
    "lint_model_setting_doc",
    "pack_f32",
    "pack_reader_book",
    "parse_blueprint_md",
    "parse_setting_md",
    "resolve_reader_mcp_command",
    "resolve_reader_mcp_script",
    "resolve_reader_tool_script",
    "rrf_fuse",
    "scan_foreshadow_radar",
    "scan_setting_impact",
    "split_frontmatter",
    "split_progressive_blocks",
    "tokenize_cjk",
    "unpack_f32",
    "validate_reader_target",
]
