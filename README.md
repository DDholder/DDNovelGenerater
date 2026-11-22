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

### DDNovelReader 伴读作品包支持（`.ddbook` / `.ddpatch`）

支持直接适配 [DDNovelReader](../DDNovelReader)（复用其 `ddnovel-reader` MCP 与 `tools/ddnovel_tool.py` 严格校验器）：

- **单章伴读设计与视觉资源需求提案**（列出所需角色立绘/表情差分、微徽章头像、场景背景、典藏剧情 CG 与已有/缺失状态）：
  ```bash
  uv run ddnovel reader plan 1 -d <book_dir> --save
  ```
- **导出当前全书并封装 `.ddbook`**（自动合并定稿/草稿、防剧透时空百科、已有插画并生成缺失占位图）：
  ```bash
  uv run ddnovel reader export -d <book_dir> --reader-dir D:/work/AI/proj/DDNovelReader
  ```
- **单独导出/增量更新一章**（保留其他章节，并支持生成 `.ddpatch` 增量补丁）：
  ```bash
  uv run ddnovel reader export -d <book_dir> -c 1 --no-pack
  uv run ddnovel reader export -d <book_dir> -c 2 --patch-from ./old.ddbook
  ```
- **过程图片与媒体资产项目内归档**（归档至 `04_创作/插画/` 对应分类子目录并维护 `资产清单.yaml`，自动同步入包）：
  ```bash
  uv run ddnovel reader archive ./gen.png -d <book_dir> -C sprite -e char_lin --expression alert
  uv run ddnovel reader archive -d <book_dir> --sync-from-source
  ```
- **一键为 AI 宿主同时接入 `ddnovel` 与 `ddnovel-reader` 双 MCP**：
  ```bash
  uv run ddnovel host install antigravity --reader-dir D:/work/AI/proj/DDNovelReader
  uv run ddnovel host install codex --reader-dir D:/work/AI/proj/DDNovelReader
  ```

可选向量检索插件：`uv sync --extra vector`

## 文档

- [需求拆解](docs/01-requirements-breakdown.md)
- [系统架构设计](docs/02-system-architecture-design.md)
- [开发路线与 DAG](docs/03-development-roadmap-and-dag.md)
- [架构蓝图](docs/architecture-blueprint.md)
- [T02 Schema 契约设计与开发流程](docs/T02-schema-contract-design.md)
