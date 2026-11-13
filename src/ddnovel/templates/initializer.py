"""新书四维目录骨架与多宿主配置渲染器 (T05)。

负责初始化与具体引擎仓库解耦的独立小说书库目录树（`novel.yaml` + 四维真源目录 +
`.ddnovel/` 派生缓存目录 + `.gitignore`），并按需渲染 `opencode`、`dsh`、`claude`
三类 AI 宿主的 MCP 配置与四角色 Skills 目录结构。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from ddnovel.core.schemas import (
    NOVEL_CONFIG_FILENAME,
    HostKind,
    NovelConfig,
    format_chapter,
)

SKILL_ROLES: Final[tuple[str, ...]] = (
    "dd-setting-architect",
    "dd-structure-planner",
    "dd-chapter-writer",
    "dd-state-curator",
)

BOOK_SKELETON_DIRS: Final[tuple[str, ...]] = (
    ".ddnovel",
    "01_settings/world",
    "01_settings/geography",
    "01_settings/factions",
    "01_settings/characters",
    "01_settings/items",
    "02_structure/volumes",
    "02_structure/arcs",
    "02_structure/blueprints/vol_01",
    "03_state/active",
    "03_state/history",
    "03_state/archive",
    "04_creation/packets",
    "04_creation/drafts",
    "04_creation/manuscript/vol_01",
)

_DEFAULT_TEMPLATES_DIR: Final[Path] = Path(__file__).resolve().parent


@dataclass(frozen=True, slots=True)
class InitBookResult:
    """新书初始化完成后的交付物摘要。"""

    book_dir: Path
    config_path: Path
    config: NovelConfig
    created_dirs: tuple[Path, ...]
    created_files: tuple[Path, ...]
    host_files: tuple[Path, ...]


def _dump_yaml(data: Any) -> str:
    """将字典/列表序列化为保留中文且字段顺序稳定的 YAML 字符串。"""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _render_frontmatter_md(frontmatter: dict[str, Any], body: str) -> str:
    """将 Frontmatter 字典安全序列化后与 Markdown 正文拼接。"""
    fm_yaml = _dump_yaml(frontmatter).rstrip("\n")
    clean_body = body.strip("\n")
    if clean_body:
        return f"---\n{fm_yaml}\n---\n\n{clean_body}\n"
    return f"---\n{fm_yaml}\n---\n"


class ProjectInitializer:
    """新书四维真源目录与多宿主配置初始化渲染器。"""

    def __init__(
        self,
        *,
        templates_dir: Path | str | None = None,
        skills_source_dir: Path | str | None = None,
        mcp_command: Sequence[str] | None = None,
    ) -> None:
        self.templates_dir: Path = (
            Path(templates_dir).resolve()
            if templates_dir is not None
            else _DEFAULT_TEMPLATES_DIR
        )
        self.skeleton_dir: Path = self.templates_dir / "book_skeleton"
        self.host_configs_dir: Path = self.templates_dir / "host_configs"
        self.skills_source_dir: Path = (
            Path(skills_source_dir).resolve()
            if skills_source_dir is not None
            else self.templates_dir / "skills"
        )
        self.mcp_command: tuple[str, ...] = self._normalize_mcp_command(mcp_command)

    @staticmethod
    def _normalize_mcp_command(mcp_command: Sequence[str] | None) -> tuple[str, ...]:
        if mcp_command is None:
            return ("ddnovel-mcp",)
        if isinstance(mcp_command, str):
            cleaned = mcp_command.strip()
            if not cleaned:
                raise ValueError("mcp_command 不能为空字符串")
            return (cleaned,)
        parts = tuple(str(part).strip() for part in mcp_command if str(part).strip())
        if not parts:
            raise ValueError("mcp_command 不能为空序列")
        return parts

    @staticmethod
    def _normalize_hosts(
        hosts: Iterable[HostKind | str] | HostKind | str | None,
    ) -> list[HostKind]:
        if hosts is None:
            return []
        raw_items: Iterable[HostKind | str]
        if isinstance(hosts, (str, HostKind)):
            raw_items = (hosts,)
        else:
            raw_items = hosts

        valid_values = [kind.value for kind in HostKind]
        normalized: list[HostKind] = []
        seen: set[HostKind] = set()
        for item in raw_items:
            if isinstance(item, HostKind):
                kind = item
            else:
                raw_str = str(item).strip().lower()
                try:
                    kind = HostKind(raw_str)
                except ValueError as exc:
                    raise ValueError(
                        f"不支持的宿主类型 {item!r}，可选值: {valid_values}"
                    ) from exc
            if kind not in seen:
                seen.add(kind)
                normalized.append(kind)
        return normalized

    def _read_skeleton_text(self, rel_name: str) -> str:
        return (self.skeleton_dir / rel_name).read_text(encoding="utf-8")

    def _load_genre_preset(self, genre_profile: str) -> dict[str, Any]:
        presets_path = self.skeleton_dir / "genre_presets.yaml"
        raw_presets = yaml.safe_load(presets_path.read_text(encoding="utf-8"))
        if not isinstance(raw_presets, dict):
            raise ValueError(f"题材预设文件格式异常: {presets_path}")

        if genre_profile in raw_presets:
            return dict(raw_presets[genre_profile])

        # 自定义第三方题材回退使用 general 骨架，并清空题材专属 profile_ext 避免校验冲突
        fallback = yaml.safe_load(_dump_yaml(raw_presets["general"]))
        active_chars = fallback.get("active_characters")
        if isinstance(active_chars, dict):
            for char_state in active_chars.values():
                if isinstance(char_state, dict):
                    char_state["profile_ext"] = {}
        return fallback

    def init_book(
        self,
        book_dir: Path | str,
        *,
        title: str | None = None,
        author: str = "",
        genre_profile: str = "general",
        hosts: Iterable[HostKind | str] | HostKind | str | None = None,
        chapter_id_width: int = 4,
        include_samples: bool = True,
        force: bool = False,
    ) -> InitBookResult:
        """在指定目录初始化完整四维小说书库及可选 AI 宿主配置。"""
        target_dir = Path(book_dir).resolve()
        existing_cfg_path = target_dir / NOVEL_CONFIG_FILENAME
        if existing_cfg_path.exists() and not force:
            raise FileExistsError(
                f"目标目录已存在 {NOVEL_CONFIG_FILENAME}: {existing_cfg_path}，"
                "如需覆盖请指定 force=True"
            )

        resolved_title = (
            title.strip()
            if title is not None and title.strip()
            else (target_dir.name.strip() or "未命名长篇小说")
        )
        host_list = self._normalize_hosts(hosts)

        # 在落盘任何目录与文件之前先通过配置 Schema 校验，防止产生半成品脏目录
        config = NovelConfig(
            title=resolved_title,
            author=author,
            genre_profile=genre_profile,
            current_chapter=0,
            chapter_id_width=chapter_id_width,
            hosts=host_list,
        )

        created_dirs: list[Path] = []
        target_dir.mkdir(parents=True, exist_ok=True)
        for rel_dir in BOOK_SKELETON_DIRS:
            dir_path = target_dir / rel_dir
            dir_path.mkdir(parents=True, exist_ok=True)
            created_dirs.append(dir_path)

        created_files: list[Path] = []

        def _write(rel_path: str, content: str) -> Path:
            file_path = target_dir / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            created_files.append(file_path)
            return file_path

        config_path = config.dump(target_dir)
        created_files.append(config_path)

        gitignore_path = target_dir / ".gitignore"
        if not gitignore_path.exists() or force:
            _write(".gitignore", self._read_skeleton_text("gitignore.tmpl"))

        # 冷库空容器与历史事实台账（无论是否生成样例均创建）
        _write("03_state/archive/characters.yaml", "{}\n")
        _write("03_state/archive/factions.yaml", "{}\n")
        _write("03_state/archive/relations.yaml", _dump_yaml({"relations": []}))
        _write("03_state/archive/threads.yaml", _dump_yaml({"threads": []}))
        _write("03_state/history/vol_01_facts.jsonl", "")

        if include_samples:
            self._write_sample_files(
                write_fn=_write,
                config=config,
            )
        else:
            self._write_minimal_empty_files(
                write_fn=_write,
                config=config,
            )

        host_files: list[Path] = []
        if host_list:
            host_files = self.render_host_configs(
                target_dir,
                host_list,
                overwrite=True,
                update_novel_yaml=False,
            )

        return InitBookResult(
            book_dir=target_dir,
            config_path=config_path,
            config=config,
            created_dirs=tuple(created_dirs),
            created_files=tuple(created_files),
            host_files=tuple(host_files),
        )

    def initialize(
        self,
        book_dir: Path | str,
        *,
        title: str | None = None,
        author: str = "",
        genre_profile: str = "general",
        hosts: Iterable[HostKind | str] | HostKind | str | None = None,
        chapter_id_width: int = 4,
        include_samples: bool = True,
        force: bool = False,
    ) -> InitBookResult:
        """``init_book`` 的语义别名方法。"""
        return self.init_book(
            book_dir,
            title=title,
            author=author,
            genre_profile=genre_profile,
            hosts=hosts,
            chapter_id_width=chapter_id_width,
            include_samples=include_samples,
            force=force,
        )

    def _write_sample_files(
        self,
        *,
        write_fn: Any,
        config: NovelConfig,
    ) -> None:
        preset = self._load_genre_preset(config.genre_profile)
        setting_tmpl = self._read_skeleton_text("setting_card.md.tmpl")
        outline_tmpl = self._read_skeleton_text("master_outline.md.tmpl")
        style_tmpl = self._read_skeleton_text("style_guide.md.tmpl")

        # 1. 01_settings/ 五类起始设定卡
        for card in preset["settings"]:
            fm = {
                "id": card["id"],
                "name": card["name"],
                "aliases": card["aliases"],
                "category": card["category"],
                "status": card["status"],
                "tags": card["tags"],
            }
            body = setting_tmpl.format(
                core_text=card["core_text"],
                topic_name=card["topic_name"],
                topic_text=card["topic_text"],
                secret_text=card["secret_text"],
            )
            write_fn(card["path"], _render_frontmatter_md(fm, body))

        # 2. 02_structure/ 总纲 -> 分卷 -> 单元 -> 首章蓝图
        vol = preset["volume"]
        write_fn(
            "02_structure/master_outline.md",
            outline_tmpl.format(
                title=config.title,
                premise=preset["premise"],
                vol_title=vol["title"],
                vol_conflict=vol["core_conflict"],
            ),
        )
        vol_fm = {
            "id": vol["id"],
            "title": vol["title"],
            "chapter_range": vol["chapter_range"],
            "core_conflict": vol["core_conflict"],
            "folded_summary": None,
        }
        write_fn("02_structure/volumes/vol_01.md", _render_frontmatter_md(vol_fm, vol["body"]))

        arc = preset["arc"]
        arc_fm = {
            "id": arc["id"],
            "volume_id": arc["volume_id"],
            "title": arc["title"],
            "chapter_range": arc["chapter_range"],
            "folded_summary": None,
        }
        write_fn("02_structure/arcs/vol_01_arc_01.md", _render_frontmatter_md(arc_fm, arc["body"]))

        bp = preset["blueprint"]
        bp_fm = {
            "chapter": bp["chapter"],
            "volume_id": bp["volume_id"],
            "arc_id": bp["arc_id"],
            "title": bp["title"],
            "pov": bp["pov"],
            "cast": bp["cast"],
            "threads": bp["threads"],
            "unlock_secrets": bp["unlock_secrets"],
            "topics": bp["topics"],
            "forbidden": bp["forbidden"],
            "beats": bp["beats"],
        }
        ch_filename = f"{format_chapter(1, width=config.chapter_id_width)}.md"
        write_fn(
            f"02_structure/blueprints/vol_01/{ch_filename}",
            _render_frontmatter_md(bp_fm, bp["body"]),
        )

        # 3. 03_state/ 状态维截面文件
        write_fn("03_state/world_state.yaml", _dump_yaml(preset["world_state"]))
        write_fn("03_state/active/characters.yaml", _dump_yaml(preset["active_characters"]))
        write_fn("03_state/active/factions.yaml", _dump_yaml(preset["active_factions"]))
        write_fn("03_state/relations.yaml", _dump_yaml({"relations": preset["relations"]}))
        write_fn("03_state/threads.yaml", _dump_yaml({"threads": preset["threads"]}))

        # 4. 04_creation/style_guide.md
        sg = preset["style_guide"]
        sg_fm = {
            "tone": sg["tone"],
            "pov_rules": sg["pov_rules"],
            "banned_words": sg["banned_words"],
        }
        sg_body = style_tmpl.format(style_note=sg["style_note"])
        write_fn("04_creation/style_guide.md", _render_frontmatter_md(sg_fm, sg_body))

    def _write_minimal_empty_files(
        self,
        *,
        write_fn: Any,
        config: NovelConfig,
    ) -> None:
        outline_tmpl = self._read_skeleton_text("master_outline.md.tmpl")
        style_tmpl = self._read_skeleton_text("style_guide.md.tmpl")

        write_fn(
            "02_structure/master_outline.md",
            outline_tmpl.format(
                title=config.title,
                premise="（待编写全书核心立意与主线冲突）",
                vol_title="第一卷（待定）",
                vol_conflict="（待编写第一卷核心冲突）",
            ),
        )
        write_fn(
            "03_state/world_state.yaml",
            _dump_yaml({"current_chapter": 0, "in_world_time": "", "macro_situation": ""}),
        )
        write_fn("03_state/active/characters.yaml", "{}\n")
        write_fn("03_state/active/factions.yaml", "{}\n")
        write_fn("03_state/relations.yaml", _dump_yaml({"relations": []}))
        write_fn("03_state/threads.yaml", _dump_yaml({"threads": []}))

        sg_fm = {
            "tone": "",
            "pov_rules": ["严格遵守单章 POV 角色认知边界，禁止未解锁剧透。"],
            "banned_words": ["震惊全场", "恐怖如斯"],
        }
        sg_body = style_tmpl.format(style_note="保持叙事节奏凝练，突出动作与细节。")
        write_fn("04_creation/style_guide.md", _render_frontmatter_md(sg_fm, sg_body))

    def render_host_configs(
        self,
        book_dir: Path | str,
        hosts: Iterable[HostKind | str] | HostKind | str,
        *,
        overwrite: bool = True,
        update_novel_yaml: bool = True,
    ) -> list[Path]:
        """为目标书库生成或增量更新指定 AI 宿主的 MCP 配置与 Skills 目录。"""
        target_dir = Path(book_dir).resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        host_list = self._normalize_hosts(hosts)
        book_dir_posix = target_dir.as_posix()

        written_files: list[Path] = []
        for host in host_list:
            if host == HostKind.OPENCODE:
                cfg_path = self._render_opencode_config(
                    target_dir,
                    book_dir_posix=book_dir_posix,
                    overwrite=overwrite,
                )
                if cfg_path is not None:
                    written_files.append(cfg_path)
                written_files.extend(
                    self._sync_host_skills(target_dir / ".opencode" / "skills", overwrite=overwrite)
                )
            elif host == HostKind.DSH:
                cfg_path = self._render_dsh_config(
                    target_dir,
                    book_dir_posix=book_dir_posix,
                    overwrite=overwrite,
                )
                if cfg_path is not None:
                    written_files.append(cfg_path)
                written_files.extend(
                    self._sync_host_skills(target_dir / ".dsh" / "skills", overwrite=overwrite)
                )
            elif host == HostKind.CLAUDE:
                cfg_path = self._render_claude_config(
                    target_dir,
                    book_dir_posix=book_dir_posix,
                    overwrite=overwrite,
                )
                if cfg_path is not None:
                    written_files.append(cfg_path)
                written_files.extend(
                    self._sync_host_skills(target_dir / ".claude" / "skills", overwrite=overwrite)
                )

        if update_novel_yaml and (target_dir / NOVEL_CONFIG_FILENAME).is_file():
            cfg = NovelConfig.load(target_dir)
            merged_hosts = list(dict.fromkeys([*cfg.hosts, *host_list]))
            if merged_hosts != cfg.hosts:
                updated_cfg = cfg.model_copy(update={"hosts": merged_hosts})
                updated_cfg.dump(target_dir)

        return written_files

    def _render_opencode_config(
        self,
        book_dir: Path,
        *,
        book_dir_posix: str,
        overwrite: bool,
    ) -> Path | None:
        target_path = book_dir / "opencode.json"
        template = json.loads((self.host_configs_dir / "opencode.json").read_text(encoding="utf-8"))

        if target_path.is_file():
            try:
                existing = json.loads(target_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = None
            if isinstance(existing, dict):
                mcp_block = existing.get("mcp")
                if not overwrite and isinstance(mcp_block, dict) and "ddnovel" in mcp_block:
                    return None
                data = existing
            else:
                if not overwrite:
                    return None
                data = template
        else:
            data = template

        data.setdefault("$schema", template.get("$schema", "https://opencode.ai/config.json"))
        mcp_section = data.get("mcp")
        if not isinstance(mcp_section, dict):
            mcp_section = {}
            data["mcp"] = mcp_section

        mcp_section["ddnovel"] = {
            "type": "local",
            "command": list(self.mcp_command),
            "environment": {"DDNOVEL_BOOK_DIR": book_dir_posix},
            "enabled": True,
        }
        target_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return target_path

    def _render_dsh_config(
        self,
        book_dir: Path,
        *,
        book_dir_posix: str,
        overwrite: bool,
    ) -> Path | None:
        dsh_dir = book_dir / ".dsh"
        dsh_dir.mkdir(parents=True, exist_ok=True)
        target_path = dsh_dir / "cordis.mcp.patch.yml"

        if target_path.exists() and not overwrite:
            return None

        raw_template = (self.host_configs_dir / "cordis.mcp.patch.yml").read_text(encoding="utf-8")
        patch_data = yaml.safe_load(raw_template)
        if not isinstance(patch_data, list):
            raise ValueError("host_configs/cordis.mcp.patch.yml 顶层必须为列表")

        cmd_exe = self.mcp_command[0]
        cmd_args = list(self.mcp_command[1:])
        for op in patch_data:
            if not isinstance(op, dict):
                continue
            inserts = op.get("insert")
            if not isinstance(inserts, list):
                continue
            for entry in inserts:
                if isinstance(entry, dict) and entry.get("id") == "mcp-ddnovel":
                    cfg = entry.setdefault("config", {})
                    cfg["command"] = cmd_exe
                    cfg["args"] = cmd_args
                    cfg["cwd"] = book_dir_posix
                    cfg["env"] = {"DDNOVEL_BOOK_DIR": book_dir_posix}

        header = "# DDNovelGenerater MCP patch for DeepSeek Harness (DSH / Cordis)\n"
        target_path.write_text(header + _dump_yaml(patch_data), encoding="utf-8")
        return target_path

    def _render_claude_config(
        self,
        book_dir: Path,
        *,
        book_dir_posix: str,
        overwrite: bool,
    ) -> Path | None:
        target_path = book_dir / ".mcp.json"
        template = json.loads((self.host_configs_dir / "claude.mcp.json").read_text(encoding="utf-8"))

        if target_path.is_file():
            try:
                existing = json.loads(target_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = None
            if isinstance(existing, dict):
                servers_block = existing.get("mcpServers")
                if not overwrite and isinstance(servers_block, dict) and "ddnovel" in servers_block:
                    return None
                data = existing
            else:
                if not overwrite:
                    return None
                data = template
        else:
            data = template

        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
            data["mcpServers"] = servers

        servers["ddnovel"] = {
            "type": "stdio",
            "command": self.mcp_command[0],
            "args": list(self.mcp_command[1:]),
            "env": {"DDNOVEL_BOOK_DIR": book_dir_posix},
        }
        target_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return target_path

    def _sync_host_skills(self, skills_root: Path, *, overwrite: bool) -> list[Path]:
        """创建四角色技能目录，若引擎内置技能模板存在则同步复制到宿主目录。"""
        skills_root.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        for role in SKILL_ROLES:
            dst_role_dir = skills_root / role
            dst_role_dir.mkdir(parents=True, exist_ok=True)

            src_role_dir = self.skills_source_dir / role
            if not src_role_dir.is_dir():
                continue

            for src_file in sorted(src_role_dir.rglob("*")):
                if not src_file.is_file():
                    continue
                rel_file = src_file.relative_to(src_role_dir)
                dst_file = dst_role_dir / rel_file
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                if not dst_file.exists() or overwrite:
                    shutil.copy2(src_file, dst_file)
                    copied.append(dst_file)
        return copied


def init_book(
    book_dir: Path | str,
    *,
    title: str | None = None,
    author: str = "",
    genre_profile: str = "general",
    hosts: Iterable[HostKind | str] | HostKind | str | None = None,
    chapter_id_width: int = 4,
    include_samples: bool = True,
    force: bool = False,
) -> InitBookResult:
    """快捷初始化新小说书库目录与宿主配置。"""
    return ProjectInitializer().init_book(
        book_dir,
        title=title,
        author=author,
        genre_profile=genre_profile,
        hosts=hosts,
        chapter_id_width=chapter_id_width,
        include_samples=include_samples,
        force=force,
    )


__all__ = [
    "BOOK_SKELETON_DIRS",
    "InitBookResult",
    "ProjectInitializer",
    "SKILL_ROLES",
    "init_book",
]
