"""新书四维目录骨架与多宿主配置模板 (T05 initializer.py / T14 skills)。"""

from .initializer import (
    ANTIGRAVITY_PLUGIN_NAME,
    BOOK_SKELETON_DIRS,
    DEFAULT_ANTIGRAVITY_PLUGINS_DIR,
    SKILL_ROLES,
    GlobalPluginInstallResult,
    InitBookResult,
    ProjectInitializer,
    init_book,
    install_antigravity_plugin,
    resolve_global_mcp_command,
)

__all__ = [
    "ANTIGRAVITY_PLUGIN_NAME",
    "BOOK_SKELETON_DIRS",
    "DEFAULT_ANTIGRAVITY_PLUGINS_DIR",
    "GlobalPluginInstallResult",
    "InitBookResult",
    "ProjectInitializer",
    "SKILL_ROLES",
    "init_book",
    "install_antigravity_plugin",
    "resolve_global_mcp_command",
]
