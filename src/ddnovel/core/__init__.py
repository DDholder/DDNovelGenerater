"""核心领域层：解析、匹配、索引与四大业务引擎 (T03 ~ T11)。

CLI 与 MCP 仅做薄封装转发，所有业务逻辑必须留在本包内。
"""

from .alias_matcher import AliasMatcher
from .parser import (
    ProgressiveParser,
    estimate_tokens,
    parse_blueprint_md,
    parse_setting_md,
    split_frontmatter,
    split_progressive_blocks,
)

__all__ = [
    "AliasMatcher",
    "ProgressiveParser",
    "estimate_tokens",
    "parse_blueprint_md",
    "parse_setting_md",
    "split_frontmatter",
    "split_progressive_blocks",
]
