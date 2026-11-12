"""DDNovelGenerater 引擎包。

与具体小说书库解耦、一引擎驱动多本书的长篇小说创作工程底座，
对外提供 Python CLI (``ddnovel``) + MCP Server (``ddnovel-mcp``) + 多角色 Skills。
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ddnovel")
except PackageNotFoundError:  # 源码直接运行、未安装时
    __version__ = "0.0.0+local"

__all__ = ["__version__"]
