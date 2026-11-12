"""``ddnovel-mcp`` FastMCP Stdio 服务入口 (占位骨架，工具集在 T13 注册)。"""

from __future__ import annotations


def create_server():
    """延迟导入 FastMCP，避免 ``import ddnovel`` 时加载重量级依赖。"""
    from fastmcp import FastMCP

    return FastMCP("ddnovel")


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()
