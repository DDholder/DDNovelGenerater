# DDNovelGenerater

与具体小说书库解耦、一引擎驱动多本书的长篇小说创作工程底座：Python CLI (`ddnovel`) + MCP Server (`ddnovel-mcp`) + 多角色 Skills。

## 快速开始

```bash
uv sync
uv run ddnovel version
uv run pytest
```

可选向量检索插件：`uv sync --extra vector`

## 文档

- [需求拆解](docs/01-requirements-breakdown.md)
- [系统架构设计](docs/02-system-architecture-design.md)
- [开发路线与 DAG](docs/03-development-roadmap-and-dag.md)
- [架构蓝图](docs/architecture-blueprint.md)
- [T02 Schema 契约设计与开发流程](docs/T02-schema-contract-design.md)
