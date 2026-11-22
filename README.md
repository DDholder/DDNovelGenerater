# DDNovelGenerater

与具体小说书库解耦、一引擎驱动多本书的长篇小说创作工程底座：Python CLI (`ddnovel`) + MCP Server (`ddnovel-mcp`) + 多角色 Skills。

## 快速开始

```bash
uv sync
uv run ddnovel version
uv run pytest
```

### Antigravity 集成

- **全局安装插件（一引擎驱动多本书，推荐）**：
  ```bash
  uv run ddnovel host install antigravity
  ```
- **单书工作区自包含配置（随书库 Git 共享）**：
  ```bash
  uv run ddnovel init <book_dir> -H antigravity
  # 或为已有书库增量同步
  uv run ddnovel host sync -d <book_dir> -H antigravity
  ```

### ChatGPT / Codex 桌面版集成

Codex Desktop、ChatGPT Desktop（Codex / Work 本地模式）与 Codex CLI 共享同一套本地配置标准（`config.toml` + `AGENTS.md` + `SKILL.md`），支持 `codex` 或别名 `chatgpt`：

- **全局安装（一引擎驱动多本书，写入 `~/.codex/`，推荐）**：
  ```bash
  uv run ddnovel host install codex
  # 或使用别名：
  uv run ddnovel host install chatgpt
  ```
- **单书工作区自包含配置（写入 `.codex/config.toml`、`AGENTS.md` 与 `.agents/skills/`）**：
  ```bash
  uv run ddnovel init <book_dir> -H codex
  # 或为已有书库增量同步（同样支持 -H chatgpt）：
  uv run ddnovel host sync -d <book_dir> -H chatgpt
  ```

可选向量检索插件：`uv sync --extra vector`

## 文档

- [需求拆解](docs/01-requirements-breakdown.md)
- [系统架构设计](docs/02-system-architecture-design.md)
- [开发路线与 DAG](docs/03-development-roadmap-and-dag.md)
- [架构蓝图](docs/architecture-blueprint.md)
- [T02 Schema 契约设计与开发流程](docs/T02-schema-contract-design.md)
