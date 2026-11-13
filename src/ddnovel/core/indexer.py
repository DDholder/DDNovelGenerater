"""SQLite 索引引擎与衍生层检索中心 (Task T06)。

设计原则：
1. SSoT 绝对权威：磁盘文件 (01_settings/, 02_structure/, 03_state/, 04_creation/) 为唯一真实源，
   SQLite 数据库 (.ddnovel/index.sqlite) 为纯衍生缓存；
2. 零污染重建 (Zero-Pollution Rebuild)：调用 rebuild() 或 sync(force_rebuild=True) 彻底重置库表
   并从磁盘重建，杜绝幽灵实体与脏残留；
3. 级联清理一致性 (Cascade Deletion)：文件变更或删除时，原子清理对应实体、别名、话题切片、关系、
   伏笔、单章事实与 FTS 切片；
4. CJK 双模检索 (Dual-Mode CJK Retrieval)：结合 jieba.cut_for_search 与单字/相邻双字 N-gram，
   保障单字、二字专有名词与复合词汇 100% 召回；
5. 软插件隔离 (Soft Vector Plugin Isolation)：向量检索插件通过依赖注入接入，关闭时保证零外部网络调用、
   零额外开销。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final, Self

import jieba
import yaml

from .alias_matcher import AliasMatcher
from .parser import ProgressiveParser
from .schemas import (
    ChapterFactRecord,
    ChapterWindow,
    CharacterState,
    CharacterStateFile,
    EntityBundle,
    EntityStatus,
    FactionState,
    FactionStateFile,
    FtsScope,
    NovelConfig,
    RelationsFile,
    SearchHit,
    SettingCategory,
    SettingDoc,
    SyncReport,
    TemporalRelation,
    Thread,
    ThreadsFile,
    ThreadStatus,
)
from .vector_store import VectorDocument, VectorPlugin

_CJK_RE: Final[re.Pattern[str]] = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_]+")
_PUNCTUATION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[\s\W_]+$")
_RESERVED_OPERATORS: Final[frozenset[str]] = frozenset({"AND", "OR", "NOT", "NEAR"})
_CHAPTER_RE: Final[re.Pattern[str]] = re.compile(r"ch_?(\d+)", re.IGNORECASE)
_CHAPTER_TITLE_RE: Final[re.Pattern[str]] = re.compile(r"第\s*(\d+)\s*章")

# SQLite DDLs for all 8 relational tables + 1 virtual table + 3 triggers
_SCHEMA_DDL: Final[str] = """
-- 1. FILE_HASHES (增量同步文件哈希账本)
CREATE TABLE IF NOT EXISTS file_hashes (
    rel_path TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    category TEXT NOT NULL,
    synced_at REAL NOT NULL,
    mtime_ns INTEGER,
    file_size INTEGER
);
CREATE INDEX IF NOT EXISTS idx_file_hashes_category ON file_hashes(category);

-- 2. ENTITIES (实体主表)
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    is_archived INTEGER NOT NULL DEFAULT 0,
    updated_at_ch INTEGER NOT NULL DEFAULT 0,
    file_path TEXT NOT NULL,
    core_text TEXT NOT NULL DEFAULT '',
    secret_text TEXT,
    state_yaml_json TEXT,
    state_file TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_entities_category ON entities(category);
CREATE INDEX IF NOT EXISTS idx_entities_is_archived ON entities(is_archived);
CREATE INDEX IF NOT EXISTS idx_entities_file_path ON entities(file_path);
CREATE INDEX IF NOT EXISTS idx_entities_state_file ON entities(state_file);

-- 3. ENTITY_ALIASES (实体别名与曾用名)
CREATE TABLE IF NOT EXISTS entity_aliases (
    alias TEXT NOT NULL,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    is_archived INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (alias, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_entity_aliases_eid ON entity_aliases(entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_aliases_archived ON entity_aliases(is_archived);

-- 4. SETTING_TOPICS (渐进式专题切片)
CREATE TABLE IF NOT EXISTS setting_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    topic_name TEXT NOT NULL,
    content TEXT NOT NULL,
    UNIQUE(entity_id, topic_name)
);
CREATE INDEX IF NOT EXISTS idx_setting_topics_eid ON setting_topics(entity_id);

-- 5. RELATIONS (动态双时态关系边)
CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    valid_from_ch INTEGER NOT NULL,
    invalid_at_ch INTEGER,
    source_file TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(source_id);
CREATE INDEX IF NOT EXISTS idx_relations_target ON relations(target_id);
CREATE INDEX IF NOT EXISTS idx_relations_validity ON relations(valid_from_ch, invalid_at_ch);
CREATE INDEX IF NOT EXISTS idx_relations_source_file ON relations(source_file);

-- 6. THREADS (伏笔悬线台账)
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'other',
    status TEXT NOT NULL,
    planted_at INTEGER NOT NULL,
    last_progressed_at INTEGER NOT NULL,
    win_start INTEGER,
    win_end INTEGER,
    trigger_entities_json TEXT NOT NULL DEFAULT '[]',
    public_clue TEXT NOT NULL DEFAULT '',
    truth_secret TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_threads_status ON threads(status);
CREATE INDEX IF NOT EXISTS idx_threads_source_file ON threads(source_file);

-- 7. CHAPTER_FACTS (单章摘要与原子事实)
CREATE TABLE IF NOT EXISTS chapter_facts (
    ch INTEGER PRIMARY KEY,
    volume_id TEXT NOT NULL DEFAULT '',
    arc_id TEXT NOT NULL DEFAULT '',
    in_world_time TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    facts_json TEXT NOT NULL DEFAULT '[]',
    entities_json TEXT NOT NULL DEFAULT '[]',
    source_file TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chapter_facts_vol ON chapter_facts(volume_id);
CREATE INDEX IF NOT EXISTS idx_chapter_facts_arc ON chapter_facts(arc_id);
CREATE INDEX IF NOT EXISTS idx_chapter_facts_source_file ON chapter_facts(source_file);

-- 8. FTS_DOCS (FTS5 外部内容关系存储)
CREATE TABLE IF NOT EXISTS fts_docs (
    doc_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    chapter_num INTEGER NOT NULL DEFAULT 0,
    entity_id TEXT REFERENCES entities(id) ON DELETE CASCADE,
    tokenized_text TEXT NOT NULL,
    raw_excerpt TEXT NOT NULL,
    source_file TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fts_docs_scope ON fts_docs(scope);
CREATE INDEX IF NOT EXISTS idx_fts_docs_ch ON fts_docs(chapter_num);
CREATE INDEX IF NOT EXISTS idx_fts_docs_entity ON fts_docs(entity_id);
CREATE INDEX IF NOT EXISTS idx_fts_docs_source_file ON fts_docs(source_file);

-- 9. FTS_DOCS_IDX (FTS5 虚拟检索表)
CREATE VIRTUAL TABLE IF NOT EXISTS fts_docs_idx USING fts5(
    tokenized_text,
    content='fts_docs',
    content_rowid='rowid',
    tokenize="unicode61 tokenchars '_'"
);

-- 10. 自动同步触发器
CREATE TRIGGER IF NOT EXISTS entities_ad AFTER DELETE ON entities BEGIN
    DELETE FROM fts_docs WHERE entity_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS fts_docs_ai AFTER INSERT ON fts_docs BEGIN
    INSERT INTO fts_docs_idx(rowid, tokenized_text) VALUES (new.rowid, new.tokenized_text);
END;

CREATE TRIGGER IF NOT EXISTS fts_docs_ad AFTER DELETE ON fts_docs BEGIN
    INSERT INTO fts_docs_idx(fts_docs_idx, rowid, tokenized_text)
        VALUES ('delete', old.rowid, old.tokenized_text);
END;

CREATE TRIGGER IF NOT EXISTS fts_docs_au AFTER UPDATE ON fts_docs BEGIN
    INSERT INTO fts_docs_idx(fts_docs_idx, rowid, tokenized_text)
        VALUES ('delete', old.rowid, old.tokenized_text);
    INSERT INTO fts_docs_idx(rowid, tokenized_text) VALUES (new.rowid, new.tokenized_text);
END;
"""


def tokenize_cjk(text: str) -> str:
    """CJK 双模分词器：提取英数标识符、jieba 词元、单字流 (Unigram) 与双字流 (Bigram)。

    保证二字特定人名与单字在 SQLite FTS5 虚拟表中具备 100% 召回率。
    """
    if not text:
        return ""
    raw_text = text.strip()
    if not raw_text:
        return ""

    word_tokens = [w for w in _WORD_RE.findall(raw_text) if w.strip("_")]
    subword_tokens = [
        part for w in word_tokens if "_" in w for part in w.split("_") if part
    ]

    cjk_seqs = _CJK_RE.findall(raw_text)
    if cjk_seqs:
        unique_cjk_seqs = list(dict.fromkeys(cjk_seqs))
        jieba_tokens = [
            t for t in jieba.cut_for_search(" ".join(unique_cjk_seqs)) if t and t != " "
        ]
        unigrams: list[str] = []
        bigrams: list[str] = []
        for seq in unique_cjk_seqs:
            unigrams.extend(seq)
            seq_len = len(seq)
            if seq_len >= 2:
                bigrams.extend(seq[i : i + 2] for i in range(seq_len - 1))
        all_tokens = [*word_tokens, *subword_tokens, *jieba_tokens, *bigrams, *unigrams]
    else:
        all_tokens = [*word_tokens, *subword_tokens]

    if not all_tokens:
        return ""
    return " ".join(dict.fromkeys(all_tokens))


def build_fts_query(raw_query: str) -> str:
    """清洗转义 FTS 查询词，保证 0 语法崩溃风险。

    1. 提取英数标识符与 CJK 片段，天然过滤括号、冒号、通配符等非法字符；
    2. CJK 片段经 jieba 细分，英文字词原样保留；
    3. 剔除 FTS5 保留布尔算子 (AND, OR, NOT, NEAR)；
    4. 对有效词元包裹双引号，生成安全的双引号词元查询；
    5. 若清洗后无有效词元，返回空字符串 ""。
    """
    q = raw_query.strip()
    if not q:
        return ""

    segments = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", q)
    raw_terms: list[str] = []
    for seg in segments:
        if _CJK_RE.match(seg):
            for t in jieba.cut_for_search(seg):
                t_clean = t.strip()
                if t_clean:
                    raw_terms.append(t_clean)
        else:
            raw_terms.append(seg)

    seen: set[str] = set()
    safe_terms: list[str] = []
    for t in raw_terms:
        if t.upper() in _RESERVED_OPERATORS:
            continue
        if t not in seen:
            seen.add(t)
            escaped = t.replace('"', '""')
            safe_terms.append(f'"{escaped}"')

    return " ".join(safe_terms)


prepare_fts_query = build_fts_query


class SqliteIndexer:
    """SQLite 索引引擎与衍生层检索中枢。"""

    def __init__(
        self,
        book_dir: Path | str,
        *,
        db_path: Path | str | None = None,
        config: NovelConfig | None = None,
        vector_plugin: VectorPlugin | None = None,
        parser: ProgressiveParser | None = None,
    ) -> None:
        self._book_dir = Path(book_dir).resolve()

        if db_path is None:
            self._db_path = self._book_dir / ".ddnovel" / "index.sqlite"
        elif str(db_path) == ":memory:":
            self._db_path = Path(":memory:")
        else:
            self._db_path = Path(db_path).resolve()

        if str(self._db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

        if config is not None:
            self._config = config
        else:
            cfg_file = self._book_dir / "novel.yaml"
            if cfg_file.is_file():
                self._config = NovelConfig.model_validate(
                    yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
                )
            else:
                self._config = NovelConfig()

        self._parser = parser if parser is not None else ProgressiveParser(book_dir=self._book_dir)

        self._conn: sqlite3.Connection | None = sqlite3.connect(str(self._db_path))
        self._conn.execute("PRAGMA foreign_keys = ON;")
        if str(self._db_path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA synchronous = NORMAL;")

        self._init_db()

        if vector_plugin is not None:
            self._vector_plugin: VectorPlugin | None = vector_plugin
        else:
            self._vector_plugin = VectorPlugin(
                config=self._config.embedding,
                conn=self._conn,
            )

    @property
    def db_path(self) -> Path:
        """返回 SQLite 数据库路径。"""
        return self._db_path

    @property
    def conn(self) -> sqlite3.Connection:
        """返回当前的 SQLite 连接实例。"""
        if self._conn is None:
            raise RuntimeError("SqliteIndexer 连接已关闭")
        return self._conn

    @property
    def config(self) -> NovelConfig:
        """返回当前小说配置。"""
        return self._config

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """幂等关闭 SQLite 连接并释放文件锁句柄。"""
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def _ensure_connected(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteIndexer 连接已关闭")
        return self._conn

    def _init_db(self) -> None:
        """初始化全部表结构、索引与 FTS5 虚拟表及触发器。"""
        conn = self._ensure_connected()
        cursor = conn.execute("PRAGMA table_info(entities)")
        cols = {row[1] for row in cursor.fetchall()}
        if cols and "state_file" not in cols:
            conn.execute("ALTER TABLE entities ADD COLUMN state_file TEXT")
        fh_cursor = conn.execute("PRAGMA table_info(file_hashes)")
        fh_cols = {row[1] for row in fh_cursor.fetchall()}
        if fh_cols:
            if "mtime_ns" not in fh_cols:
                conn.execute("ALTER TABLE file_hashes ADD COLUMN mtime_ns INTEGER")
            if "file_size" not in fh_cols:
                conn.execute("ALTER TABLE file_hashes ADD COLUMN file_size INTEGER")
        conn.executescript(_SCHEMA_DDL)
        conn.commit()

    def sync(self, *, force_rebuild: bool = False) -> SyncReport:
        """执行增量哈希对比同步；若 force_rebuild=True 则执行零污染重建。"""
        conn = self._ensure_connected()
        t0 = time.perf_counter()

        disk_entries = self._scan_disk_files_with_stat()
        disk_files: dict[str, str] = {k: v[0] for k, v in disk_entries.items()}

        if force_rebuild:
            db_meta: dict[str, tuple[str, int | None, int | None, float]] = {}
            db_hashes: dict[str, str] = {}
        else:
            db_rows = conn.execute(
                "SELECT rel_path, sha256, mtime_ns, file_size, synced_at FROM file_hashes"
            ).fetchall()
            db_meta = {
                str(r[0]): (
                    str(r[1]),
                    int(r[2]) if r[2] is not None else None,
                    int(r[3]) if r[3] is not None else None,
                    float(r[4]),
                )
                for r in db_rows
            }
            db_hashes = {rp: vals[0] for rp, vals in db_meta.items()}

        # 计算磁盘文件的 SHA-256 哈希（带防竞态 stat 短路缓存）
        disk_hashes: dict[str, str] = {}
        racy_refreshed: list[tuple[float, int, int, str]] = []
        now = time.time()
        now_ns = int(now * 1_000_000_000)
        for rel_path, (_, st_mtime_ns, st_size) in disk_entries.items():
            meta = db_meta.get(rel_path)
            if (
                meta is not None
                and meta[1] is not None
                and meta[2] is not None
                and st_mtime_ns == meta[1]
                and st_size == meta[2]
                and (int(meta[3] * 1_000_000_000) - st_mtime_ns) > 50_000_000
            ):
                disk_hashes[rel_path] = meta[0]
            else:
                abs_p = self._book_dir / rel_path
                sha = hashlib.sha256(abs_p.read_bytes()).hexdigest()
                disk_hashes[rel_path] = sha
                if meta is not None and sha == meta[0] and (now_ns - st_mtime_ns) > 50_000_000:
                    racy_refreshed.append((now, st_mtime_ns, st_size, rel_path))

        with conn:
            if force_rebuild:
                conn.execute("DELETE FROM fts_docs")
                conn.execute("DELETE FROM file_hashes")
                conn.execute("DELETE FROM setting_topics")
                conn.execute("DELETE FROM entity_aliases")
                conn.execute("DELETE FROM entities")
                conn.execute("DELETE FROM relations")
                conn.execute("DELETE FROM threads")
                conn.execute("DELETE FROM chapter_facts")
                if self._vector_plugin is not None:
                    self._vector_plugin.clear(conn=conn)

            added: list[str] = sorted(p for p in disk_files if p not in db_hashes)
            updated: list[str] = sorted(
                p for p in disk_files if p in db_hashes and disk_hashes[p] != db_hashes[p]
            )
            deleted: list[str] = sorted(p for p in db_hashes if p not in disk_files)

            # 1. 级联清理删除的文件
            for p in deleted:
                self._cascade_delete_file(p)
                conn.execute("DELETE FROM file_hashes WHERE rel_path = ?", (p,))

            # 2. 级联清理所有已变更文件的旧派生记录 (Phase 1: Cascade Delete)
            for p in updated:
                self._cascade_delete_file(p)

            # 3. 统一摄入已变更文件的最新内容并更新哈希 (Phase 2: Ingest & Hash Update)
            updated_rows: list[tuple[str, float, int, int, str]] = []
            for p in updated:
                self._ingest_file(p, disk_files[p])
                _, st_mtime_ns, st_size = disk_entries[p]
                updated_rows.append((disk_hashes[p], now, st_mtime_ns, st_size, p))
            if updated_rows:
                conn.executemany(
                    """
                    UPDATE file_hashes
                    SET sha256 = ?, synced_at = ?, mtime_ns = ?, file_size = ?
                    WHERE rel_path = ?
                    """,
                    updated_rows,
                )

            # 4. 解析摄入新增的文件
            added_rows: list[tuple[str, str, str, float, int, int]] = []
            for p in added:
                self._ingest_file(p, disk_files[p])
                cat, st_mtime_ns, st_size = disk_entries[p]
                added_rows.append((p, disk_hashes[p], cat, now, st_mtime_ns, st_size))
            if added_rows:
                conn.executemany(
                    """
                    INSERT INTO file_hashes (rel_path, sha256, category, synced_at, mtime_ns, file_size)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    added_rows,
                )

            if racy_refreshed:
                conn.executemany(
                    """
                    UPDATE file_hashes
                    SET synced_at = ?, mtime_ns = ?, file_size = ?
                    WHERE rel_path = ?
                    """,
                    racy_refreshed,
                )

        # 5. 同步向量检索插件
        if self._vector_plugin is not None and self._vector_plugin.is_enabled():
            self._sync_vector_plugin()

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return SyncReport(
            added=added,
            updated=updated,
            deleted=deleted,
            elapsed_ms=elapsed_ms,
            rebuilt=force_rebuild,
        )

    def rebuild(self) -> SyncReport:
        """零污染全量重建数据库缓存。"""
        return self.sync(force_rebuild=True)

    @staticmethod
    def _scandir_recursive(
        root: Path,
        *,
        extensions: tuple[str, ...],
    ) -> list[tuple[Path, os.stat_result]]:
        """使用 os.scandir 递归收集指定后缀的文件与 stat 结果，减少 Win32 stat 系统调用。"""
        results: list[tuple[Path, os.stat_result]] = []
        stack: list[Path] = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            dirs: list[Path] = []
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    dirs.append(Path(entry.path))
                elif entry.is_file() and entry.name.endswith(extensions):
                    results.append((Path(entry.path), entry.stat()))
            stack.extend(reversed(dirs))
        return results

    def _scan_disk_files_with_stat(self) -> dict[str, tuple[str, int, int]]:
        """使用 os.scandir 扫描磁盘有效源文件，返回 {相对 POSIX 路径: (分类, mtime_ns, size)}。"""
        results: dict[str, tuple[str, int, int]] = {}
        book_dir = self._book_dir

        # 1. 01_settings/
        settings_dir = book_dir / "01_settings"
        if settings_dir.is_dir():
            for p, st in self._scandir_recursive(settings_dir, extensions=(".md",)):
                rel = p.relative_to(book_dir).as_posix()
                results[rel] = ("setting", st.st_mtime_ns, st.st_size)

        # 2. 02_structure/
        structure_dir = book_dir / "02_structure"
        if structure_dir.is_dir():
            for p, st in self._scandir_recursive(structure_dir, extensions=(".md",)):
                rel = p.relative_to(book_dir).as_posix()
                cat = "blueprint" if "blueprints" in p.relative_to(structure_dir).parts else "structure"
                results[rel] = (cat, st.st_mtime_ns, st.st_size)

        # 3. 03_state/
        state_dir = book_dir / "03_state"
        if state_dir.is_dir():
            for p, st in self._scandir_recursive(state_dir, extensions=(".yaml", ".yml")):
                rel = p.relative_to(book_dir).as_posix()
                results[rel] = ("state", st.st_mtime_ns, st.st_size)
            history_dir = state_dir / "history"
            if history_dir.is_dir():
                try:
                    with os.scandir(history_dir) as it:
                        h_entries = sorted(it, key=lambda e: e.name)
                except OSError:
                    h_entries = []
                for entry in h_entries:
                    if entry.is_file() and entry.name.endswith("_facts.jsonl"):
                        st = entry.stat()
                        rel = Path(entry.path).relative_to(book_dir).as_posix()
                        results[rel] = ("fact", st.st_mtime_ns, st.st_size)

        # 4. 04_creation/manuscript/
        manuscript_dir = book_dir / "04_creation" / "manuscript"
        if manuscript_dir.is_dir():
            for p, st in self._scandir_recursive(manuscript_dir, extensions=(".md",)):
                rel = p.relative_to(book_dir).as_posix()
                results[rel] = ("manuscript", st.st_mtime_ns, st.st_size)

        return results

    def _scan_disk_files(self) -> dict[str, str]:
        """扫描磁盘有效源文件，返回相对 POSIX 路径到所属分类的映射字典。"""
        return {k: v[0] for k, v in self._scan_disk_files_with_stat().items()}

    def _cascade_delete_file(self, rel_path: str) -> None:
        """原子级联删除源文件对应的所有派生表记录。"""
        conn = self._ensure_connected()
        posix_path = Path(rel_path).as_posix()
        is_state_yaml = (
            "03_state" in Path(rel_path).parts
            and posix_path.endswith((".yaml", ".yml"))
        )
        if is_state_yaml:
            conn.execute(
                """
                DELETE FROM entities
                WHERE state_file = ?
                  AND (file_path = ? OR file_path = '' OR file_path IS NULL)
                """,
                (rel_path, rel_path),
            )
            conn.execute(
                """
                UPDATE entity_aliases
                SET is_archived = (
                    SELECT CASE WHEN status IN ('deceased', 'retired') THEN 1 ELSE 0 END
                    FROM entities
                    WHERE entities.id = entity_aliases.entity_id
                )
                WHERE entity_id IN (SELECT id FROM entities WHERE state_file = ?)
                """,
                (rel_path,),
            )
            conn.execute(
                """
                UPDATE entities
                SET state_yaml_json = NULL,
                    state_file = NULL,
                    updated_at_ch = 0,
                    is_archived = CASE
                        WHEN status IN ('deceased', 'retired') THEN 1
                        ELSE 0
                    END
                WHERE state_file = ?
                """,
                (rel_path,),
            )

        is_setting = (
            "01_settings" in Path(rel_path).parts
            and posix_path.endswith((".md", ".markdown"))
        )
        if is_setting:
            conn.execute(
                """
                DELETE FROM setting_topics
                WHERE entity_id IN (SELECT id FROM entities WHERE file_path = ?)
                """,
                (rel_path,),
            )
            conn.execute(
                """
                DELETE FROM entity_aliases
                WHERE entity_id IN (SELECT id FROM entities WHERE file_path = ?)
                """,
                (rel_path,),
            )
            conn.execute(
                """
                UPDATE entities
                SET file_path = state_file,
                    name = id,
                    core_text = '',
                    secret_text = NULL,
                    tags_json = '[]',
                    is_archived = CASE WHEN state_file LIKE '%archive%' THEN 1 ELSE 0 END
                WHERE file_path = ? AND state_file IS NOT NULL AND state_file != ''
                """,
                (rel_path,),
            )
            conn.execute(
                """
                DELETE FROM entities
                WHERE file_path = ? AND (state_file IS NULL OR state_file = '')
                """,
                (rel_path,),
            )
        elif not is_state_yaml:
            conn.execute("DELETE FROM entities WHERE file_path = ?", (rel_path,))

        conn.execute("DELETE FROM relations WHERE source_file = ?", (rel_path,))
        conn.execute("DELETE FROM threads WHERE source_file = ?", (rel_path,))
        conn.execute("DELETE FROM chapter_facts WHERE source_file = ?", (rel_path,))
        conn.execute("DELETE FROM fts_docs WHERE source_file = ?", (rel_path,))

    def _ingest_file(self, rel_path: str, category: str) -> None:
        """按文件类型路由分派解析与落库。"""
        if category == "setting":
            self._ingest_setting(rel_path)
        elif category == "blueprint":
            self._ingest_blueprint(rel_path)
        elif category == "state":
            self._ingest_state_yaml(rel_path)
        elif category == "fact":
            self._ingest_chapter_facts(rel_path)
        elif category == "manuscript":
            self._ingest_manuscript(rel_path)

    def _ingest_setting(self, rel_path: str) -> None:
        """解析并入库 01_settings/ 设定 Markdown 文件。"""
        conn = self._ensure_connected()
        abs_p = self._book_dir / rel_path
        doc = self._parser.parse_setting_md(abs_p, book_dir=self._book_dir)

        is_archived = 1 if doc.status in (EntityStatus.DECEASED, EntityStatus.RETIRED) else 0
        tags_json = json.dumps(doc.tags or [], ensure_ascii=False)

        conn.execute(
            """
            INSERT INTO entities (
                id, name, category, status, is_archived,
                updated_at_ch, file_path, core_text, secret_text, tags_json
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                category = excluded.category,
                status = excluded.status,
                file_path = excluded.file_path,
                core_text = excluded.core_text,
                secret_text = excluded.secret_text,
                tags_json = excluded.tags_json,
                is_archived = CASE
                    WHEN excluded.is_archived = 1 THEN 1
                    WHEN entities.state_file LIKE '%archive%' THEN 1
                    ELSE 0
                END
            """,
            (
                doc.id,
                doc.name,
                doc.category.value,
                doc.status.value,
                is_archived,
                rel_path,
                doc.core_text or "",
                doc.secret_text,
                tags_json,
            ),
        )

        ent_arch_row = conn.execute(
            "SELECT is_archived FROM entities WHERE id = ?", (doc.id,)
        ).fetchone()
        alias_archived = ent_arch_row[0] if ent_arch_row else is_archived

        all_aliases = [doc.name, *doc.aliases]
        for a in all_aliases:
            clean_a = a.strip()
            if clean_a:
                conn.execute(
                    """
                    INSERT INTO entity_aliases (alias, entity_id, is_archived)
                    VALUES (?, ?, ?)
                    ON CONFLICT(alias, entity_id) DO UPDATE SET is_archived = excluded.is_archived
                    """,
                    (clean_a, doc.id, alias_archived),
                )

        for topic_name, content in doc.topics.items():
            clean_tn = topic_name.strip()
            clean_content = content.strip()
            if clean_tn:
                conn.execute(
                    """
                    INSERT INTO setting_topics (entity_id, topic_name, content)
                    VALUES (?, ?, ?)
                    ON CONFLICT(entity_id, topic_name) DO UPDATE SET content = excluded.content
                    """,
                    (doc.id, clean_tn, clean_content),
                )
                topic_doc_id = f"setting:{doc.id}:topic:{clean_tn}"
                conn.execute(
                    """
                    INSERT OR REPLACE INTO fts_docs (
                        doc_id, scope, chapter_num, entity_id, tokenized_text, raw_excerpt, source_file
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        topic_doc_id,
                        FtsScope.SETTING.value,
                        0,
                        doc.id,
                        tokenize_cjk(f"{clean_tn} {clean_content}"),
                        clean_content,
                        rel_path,
                    ),
                )

        core_doc_id = f"setting:{doc.id}:core"
        alias_str = " ".join(doc.aliases) if doc.aliases else ""
        core_text_to_tokenize = f"{doc.name} {alias_str} {doc.core_text or ''}".strip()
        conn.execute(
            """
            INSERT OR REPLACE INTO fts_docs (
                doc_id, scope, chapter_num, entity_id, tokenized_text, raw_excerpt, source_file
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                core_doc_id,
                FtsScope.SETTING.value,
                0,
                doc.id,
                tokenize_cjk(core_text_to_tokenize),
                doc.core_text or "",
                rel_path,
            ),
        )

    def _ingest_blueprint(self, rel_path: str) -> None:
        """解析并入库 02_structure/blueprints/ 分章细纲。"""
        conn = self._ensure_connected()
        abs_p = self._book_dir / rel_path
        bp = self._parser.parse_blueprint_md(abs_p, book_dir=self._book_dir)

        beats_text = "\n".join(b.content for b in bp.beats)
        combined_text = f"{bp.title}\n{beats_text}\n{bp.body or ''}"
        beats_excerpt = "; ".join(b.content for b in bp.beats[:2])
        excerpt = f"{bp.title}: {beats_excerpt}" if beats_excerpt else bp.title

        conn.execute(
            """
            INSERT OR REPLACE INTO fts_docs (
                doc_id, scope, chapter_num, entity_id, tokenized_text, raw_excerpt, source_file
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"blueprint:ch_{bp.chapter:04d}",
                FtsScope.BLUEPRINT.value,
                bp.chapter,
                None,
                tokenize_cjk(combined_text),
                excerpt,
                rel_path,
            ),
        )

    def _ingest_state_yaml(self, rel_path: str) -> None:
        """解析并入库 03_state/ 状态 YAML 文件。"""
        conn = self._ensure_connected()
        abs_p = self._book_dir / rel_path
        content = abs_p.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        if not data:
            return

        posix_path = Path(rel_path).as_posix()
        is_archive = "archive" in Path(rel_path).parts

        if posix_path.endswith("relations.yaml"):
            rel_file = RelationsFile.model_validate(data)
            for r in rel_file.relations:
                conn.execute(
                    """
                    INSERT INTO relations (
                        source_id, target_id, relation, valid_from_ch, invalid_at_ch, source_file
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (r.source, r.target, r.relation, r.valid_from_ch, r.invalid_at_ch, rel_path),
                )

        elif posix_path.endswith("threads.yaml"):
            thr_file = ThreadsFile.model_validate(data)
            for t in thr_file.threads:
                win_start = t.expected_window.start if t.expected_window else None
                win_end = t.expected_window.end if t.expected_window else None
                entities_json = json.dumps(t.trigger_entities, ensure_ascii=False)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO threads (
                        id, title, category, status, planted_at, last_progressed_at,
                        win_start, win_end, trigger_entities_json, public_clue, truth_secret, source_file
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        t.id,
                        t.title,
                        t.category or "other",
                        t.status.value,
                        t.planted_at,
                        t.last_progressed_at,
                        win_start,
                        win_end,
                        entities_json,
                        t.public_clue,
                        t.truth_secret or "",
                        rel_path,
                    ),
                )

        elif posix_path.endswith("characters.yaml"):
            cf = CharacterStateFile.model_validate(data)
            archived_flag = 1 if is_archive else 0
            for eid, c_state in cf.root.items():
                if is_archive:
                    existing = conn.execute(
                        "SELECT state_file FROM entities WHERE id = ?", (eid,)
                    ).fetchone()
                    if (
                        existing is not None
                        and existing[0]
                        and str(existing[0]).startswith("03_state/active/")
                    ):
                        continue
                state_json = c_state.model_dump_json()
                conn.execute(
                    """
                    INSERT INTO entities (
                        id, name, category, status, is_archived,
                        updated_at_ch, file_path, core_text, state_yaml_json, state_file
                    ) VALUES (?, ?, 'character', 'active', ?, ?, ?, '', ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        state_yaml_json = excluded.state_yaml_json,
                        state_file = excluded.state_file,
                        updated_at_ch = excluded.updated_at_ch,
                        file_path = CASE
                            WHEN entities.file_path = ''
                                OR entities.file_path IS NULL
                                OR entities.file_path LIKE '03_state/%' THEN excluded.file_path
                            ELSE entities.file_path
                        END,
                        is_archived = CASE
                            WHEN excluded.is_archived = 1 THEN 1
                            WHEN entities.status IN ('deceased', 'retired') THEN 1
                            ELSE 0
                        END
                    """,
                    (eid, eid, archived_flag, c_state.updated_at_ch, rel_path, state_json, rel_path),
                )
                conn.execute(
                    """
                    UPDATE entity_aliases
                    SET is_archived = (
                        SELECT is_archived FROM entities WHERE entities.id = entity_aliases.entity_id
                    )
                    WHERE entity_id = ?
                    """,
                    (eid,),
                )

        elif posix_path.endswith("factions.yaml"):
            ff = FactionStateFile.model_validate(data)
            for eid, f_state in ff.root.items():
                if is_archive:
                    existing = conn.execute(
                        "SELECT state_file FROM entities WHERE id = ?", (eid,)
                    ).fetchone()
                    if (
                        existing is not None
                        and existing[0]
                        and str(existing[0]).startswith("03_state/active/")
                    ):
                        continue
                archived_flag = (
                    1
                    if (is_archive or f_state.status in (EntityStatus.DECEASED, EntityStatus.RETIRED))
                    else 0
                )
                state_json = f_state.model_dump_json()
                conn.execute(
                    """
                    INSERT INTO entities (
                        id, name, category, status, is_archived,
                        updated_at_ch, file_path, core_text, state_yaml_json, state_file
                    ) VALUES (?, ?, 'faction', 'active', ?, ?, ?, '', ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        state_yaml_json = excluded.state_yaml_json,
                        state_file = excluded.state_file,
                        updated_at_ch = excluded.updated_at_ch,
                        file_path = CASE
                            WHEN entities.file_path = ''
                                OR entities.file_path IS NULL
                                OR entities.file_path LIKE '03_state/%' THEN excluded.file_path
                            ELSE entities.file_path
                        END,
                        is_archived = CASE
                            WHEN excluded.is_archived = 1 THEN 1
                            WHEN entities.status IN ('deceased', 'retired') THEN 1
                            ELSE 0
                        END
                    """,
                    (eid, eid, archived_flag, f_state.updated_at_ch, rel_path, state_json, rel_path),
                )
                conn.execute(
                    """
                    UPDATE entity_aliases
                    SET is_archived = (
                        SELECT is_archived FROM entities WHERE entities.id = entity_aliases.entity_id
                    )
                    WHERE entity_id = ?
                    """,
                    (eid,),
                )

    def _ingest_chapter_facts(self, rel_path: str) -> None:
        """解析并入库 03_state/history/ 单章事实与摘要。"""
        conn = self._ensure_connected()
        abs_p = self._book_dir / rel_path
        lines = abs_p.read_text(encoding="utf-8").splitlines()
        chapter_fact_rows: list[tuple[int, str, str, str, str, str, str, str]] = []
        fts_doc_rows: list[tuple[str, str, int, None, str, str, str]] = []
        for line in lines:
            if not line.strip():
                continue
            r = ChapterFactRecord.model_validate_json(line)
            facts_json = json.dumps(r.facts, ensure_ascii=False)
            entities_json = json.dumps(r.entities, ensure_ascii=False)
            chapter_fact_rows.append(
                (
                    r.ch,
                    r.volume_id or "",
                    r.arc_id or "",
                    r.in_world_time or "",
                    r.summary,
                    facts_json,
                    entities_json,
                    rel_path,
                )
            )
            fts_doc_rows.append(
                (
                    f"fact:ch_{r.ch:04d}:summary",
                    FtsScope.SUMMARY.value,
                    r.ch,
                    None,
                    tokenize_cjk(r.summary),
                    r.summary,
                    rel_path,
                )
            )
            for idx, fact in enumerate(r.facts):
                fts_doc_rows.append(
                    (
                        f"fact:ch_{r.ch:04d}:fact_{idx + 1}",
                        FtsScope.FACT.value,
                        r.ch,
                        None,
                        tokenize_cjk(fact),
                        fact,
                        rel_path,
                    )
                )

        if chapter_fact_rows:
            conn.executemany(
                """
                INSERT OR REPLACE INTO chapter_facts (
                    ch, volume_id, arc_id, in_world_time, summary, facts_json, entities_json, source_file
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                chapter_fact_rows,
            )
        if fts_doc_rows:
            conn.executemany(
                """
                INSERT OR REPLACE INTO fts_docs (
                    doc_id, scope, chapter_num, entity_id, tokenized_text, raw_excerpt, source_file
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                fts_doc_rows,
            )

    def _ingest_manuscript(self, rel_path: str) -> None:
        """解析并入库 04_creation/manuscript/ 章节正文切片。"""
        conn = self._ensure_connected()
        abs_p = self._book_dir / rel_path
        content = abs_p.read_text(encoding="utf-8")
        ch_num = 0
        m_stem = _CHAPTER_RE.search(abs_p.stem)
        if m_stem:
            ch_num = int(m_stem.group(1))
        else:
            m_title = _CHAPTER_TITLE_RE.search(content)
            if m_title:
                ch_num = int(m_title.group(1))

        conn.execute(
            """
            INSERT OR REPLACE INTO fts_docs (
                doc_id, scope, chapter_num, entity_id, tokenized_text, raw_excerpt, source_file
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"manuscript:ch_{ch_num:04d}",
                FtsScope.MANUSCRIPT.value,
                ch_num,
                None,
                tokenize_cjk(content),
                content[:200],
                rel_path,
            ),
        )

    def _sync_vector_plugin(self) -> None:
        """执行可插拔向量检索插件的增量索引与孤儿向量清理。"""
        conn = self._ensure_connected()
        if self._vector_plugin is None or not self._vector_plugin.is_enabled():
            return
        rows = conn.execute(
            "SELECT doc_id, scope, chapter_num, entity_id, raw_excerpt FROM fts_docs"
        ).fetchall()
        docs = [
            VectorDocument(
                doc_id=str(r[0]),
                text=str(r[4]),
                scope=FtsScope(r[1]),
                chapter_num=int(r[2]),
                entity_id=str(r[3]) if r[3] is not None else None,
                excerpt=str(r[4]),
            )
            for r in rows
        ]
        self._vector_plugin.index_documents(docs, conn=conn)
        self._vector_plugin.prune_orphan_embeddings(conn=conn)

    def search_fts(
        self,
        query: str,
        *,
        scope: FtsScope | str | None = None,
        top_k: int = 20,
        hybrid: bool = False,
    ) -> list[SearchHit]:
        """在 FTS5 虚拟表中检索文本切片，返回规范化 SearchHit 列表。"""
        if top_k <= 0 or not query or not query.strip():
            return []

        fts_q = build_fts_query(query)
        if not fts_q:
            return []

        conn = self._ensure_connected()
        scope_val: FtsScope | None = None
        if scope is not None:
            scope_val = FtsScope(str(scope.value if isinstance(scope, FtsScope) else scope))

        sql = """
            SELECT d.doc_id, d.scope, d.chapter_num, d.entity_id, d.raw_excerpt,
                   -bm25(fts_docs_idx) AS score
            FROM fts_docs_idx
            JOIN fts_docs AS d ON d.rowid = fts_docs_idx.rowid
            WHERE fts_docs_idx MATCH ?
        """
        params: list[Any] = [fts_q]
        if scope_val is not None:
            sql += " AND d.scope = ?"
            params.append(scope_val.value)

        sql += " ORDER BY score DESC LIMIT ?"
        params.append(top_k)

        cursor = conn.execute(sql, params)
        hits: list[SearchHit] = []
        for row in cursor.fetchall():
            hits.append(
                SearchHit(
                    doc_id=str(row[0]),
                    scope=FtsScope(row[1]),
                    chapter_num=int(row[2]),
                    entity_id=str(row[3]) if row[3] else None,
                    excerpt=str(row[4]),
                    score=float(row[5]),
                )
            )

        if hybrid and self._vector_plugin is not None and self._vector_plugin.is_enabled():
            return self._vector_plugin.hybrid_rerank(
                query=query,
                fts_hits=hits,
                top_k=top_k,
                scope=scope_val,
                conn=conn,
            )

        return hits

    def get_active_relations(
        self,
        entity_ids: Sequence[str] | str | None = None,
        at_chapter: int = 1,
    ) -> list[TemporalRelation]:
        """查询指定实体在特定章号生效的动态双时态关系列表。

        严格遵循 [valid_from_ch, invalid_at_ch) 左闭右开区间进行判定。
        """
        if isinstance(at_chapter, bool) or not isinstance(at_chapter, int) or at_chapter < 1:
            raise ValueError(f"章号 at_chapter 必须为 >= 1 的整数，收到: {at_chapter!r}")

        conn = self._ensure_connected()
        normalized_eids: list[str] | None = None
        if entity_ids is not None:
            if isinstance(entity_ids, str):
                normalized_eids = [entity_ids.strip()]
            else:
                normalized_eids = [str(e).strip() for e in entity_ids if str(e).strip()]
            if not normalized_eids:
                return []

        if normalized_eids is not None:
            placeholders = ", ".join("?" * len(normalized_eids))
            sql = f"""
                SELECT source_id, target_id, relation, valid_from_ch, invalid_at_ch
                FROM relations
                WHERE valid_from_ch <= ?
                  AND (invalid_at_ch IS NULL OR invalid_at_ch > ?)
                  AND (source_id IN ({placeholders}) OR target_id IN ({placeholders}))
                ORDER BY valid_from_ch ASC, id ASC
            """
            params = [at_chapter, at_chapter, *normalized_eids, *normalized_eids]
        else:
            sql = """
                SELECT source_id, target_id, relation, valid_from_ch, invalid_at_ch
                FROM relations
                WHERE valid_from_ch <= ?
                  AND (invalid_at_ch IS NULL OR invalid_at_ch > ?)
                ORDER BY valid_from_ch ASC, id ASC
            """
            params = [at_chapter, at_chapter]

        cursor = conn.execute(sql, params)
        return [
            TemporalRelation(
                source=str(row[0]),
                target=str(row[1]),
                relation=str(row[2]),
                valid_from_ch=int(row[3]),
                invalid_at_ch=int(row[4]) if row[4] is not None else None,
            )
            for row in cursor.fetchall()
        ]

    def get_entity_bundle(
        self,
        entity_id: str,
        *,
        at_chapter: int | None = None,
    ) -> EntityBundle:
        """聚合获取指定实体的设定卡、动态截面状态与有效时态关系包。"""
        conn = self._ensure_connected()
        eid = str(entity_id).strip()
        row = conn.execute(
            """
            SELECT id, name, category, status, is_archived, updated_at_ch,
                   file_path, core_text, secret_text, state_yaml_json, tags_json
            FROM entities
            WHERE id = ?
            """,
            (eid,),
        ).fetchone()

        if row is None:
            raise KeyError(f"未找到实体 '{eid}' 的设定记录")

        (
            e_id,
            name,
            category_str,
            status_str,
            is_archived_int,
            _updated_at_ch,
            file_path,
            core_text,
            secret_text,
            state_yaml_json,
            tags_json,
        ) = row

        alias_rows = conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias ASC",
            (eid,),
        ).fetchall()
        aliases = [str(r[0]) for r in alias_rows if str(r[0]) != name]

        topic_rows = conn.execute(
            "SELECT topic_name, content FROM setting_topics WHERE entity_id = ? ORDER BY id ASC",
            (eid,),
        ).fetchall()
        topics = {str(r[0]): str(r[1]) for r in topic_rows}

        tags = json.loads(tags_json) if tags_json else []
        category = SettingCategory(category_str)
        status = EntityStatus(status_str)

        setting = SettingDoc(
            id=e_id,
            name=name,
            category=category,
            aliases=aliases,
            status=status,
            tags=tags,
            file_path=file_path,
            core_text=core_text or "",
            topics=topics,
            secret_text=secret_text,
        )

        state: CharacterState | FactionState | None = None
        if category == SettingCategory.CHARACTER and state_yaml_json:
            state = CharacterState.model_validate_json(state_yaml_json)
        elif category == SettingCategory.FACTION and state_yaml_json:
            state = FactionState.model_validate_json(state_yaml_json)

        if at_chapter is not None:
            relations = self.get_active_relations(entity_ids=[eid], at_chapter=at_chapter)
        else:
            rel_rows = conn.execute(
                """
                SELECT source_id, target_id, relation, valid_from_ch, invalid_at_ch
                FROM relations
                WHERE (source_id = ? OR target_id = ?) AND invalid_at_ch IS NULL
                ORDER BY valid_from_ch ASC, id ASC
                """,
                (eid, eid),
            ).fetchall()
            relations = [
                TemporalRelation(
                    source=str(r[0]),
                    target=str(r[1]),
                    relation=str(r[2]),
                    valid_from_ch=int(r[3]),
                    invalid_at_ch=None,
                )
                for r in rel_rows
            ]

        return EntityBundle(
            setting=setting,
            state=state,
            relations=relations,
            is_archived=bool(is_archived_int),
        )

    def get_entity(self, entity_id: str) -> SettingDoc | None:
        """查询指定实体的 SettingDoc 设定文档，不存在时返回 None。"""
        try:
            bundle = self.get_entity_bundle(entity_id)
            if (
                bundle.setting.file_path.endswith((".yaml", ".yml"))
                or "03_state" in Path(bundle.setting.file_path).parts
                or not bundle.setting.file_path
            ):
                return None
            return bundle.setting
        except KeyError:
            return None

    def list_entities(
        self,
        category: SettingCategory | str | None = None,
        *,
        include_archived: bool = True,
    ) -> list[SettingDoc]:
        """列出符合分类与归档状态条件的实体设定列表。"""
        conn = self._ensure_connected()
        conditions = []
        params: list[Any] = []

        if category is not None:
            cat_str = category.value if isinstance(category, SettingCategory) else str(category)
            conditions.append("category = ?")
            params.append(cat_str)

        if not include_archived:
            conditions.append("is_archived = 0")

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = conn.execute(
            f"SELECT id FROM entities {where_clause} ORDER BY id ASC",
            params,
        )
        results: list[SettingDoc] = []
        for (eid,) in cursor.fetchall():
            doc = self.get_entity(str(eid))
            if doc is not None:
                results.append(doc)
        return results

    def get_recent_summaries(
        self,
        before_ch: int,
        count: int = 3,
    ) -> list[ChapterFactRecord]:
        """回溯获取指定章号之前的最近 N 章摘要与原子事实（按章号正序排列）。"""
        if isinstance(before_ch, bool) or not isinstance(before_ch, int) or before_ch <= 1:
            raise ValueError(f"章号 before_ch 必须为 >= 2 的整数，收到: {before_ch!r}")
        if count <= 0:
            raise ValueError(f"count 必须为正整数，收到: {count}")

        conn = self._ensure_connected()
        cursor = conn.execute(
            """
            SELECT ch, volume_id, arc_id, in_world_time, summary, facts_json, entities_json
            FROM chapter_facts
            WHERE ch < ?
            ORDER BY ch DESC
            LIMIT ?
            """,
            (before_ch, count),
        )
        rows = cursor.fetchall()
        if not rows:
            return []

        records: list[ChapterFactRecord] = []
        for r in reversed(rows):
            records.append(
                ChapterFactRecord(
                    ch=int(r[0]),
                    volume_id=str(r[1]) if r[1] else None,
                    arc_id=str(r[2]) if r[2] else None,
                    in_world_time=str(r[3]) if r[3] else None,
                    summary=str(r[4]),
                    facts=json.loads(r[5]),
                    entities=json.loads(r[6]),
                )
            )
        return records

    def get_chapter_fact(self, ch: int) -> ChapterFactRecord | None:
        """查询指定章节的单章事实记录，不存在时返回 None。"""
        if isinstance(ch, bool) or not isinstance(ch, int) or ch < 1:
            raise ValueError(f"章号 ch 必须为 >= 1 的整数，收到: {ch!r}")

        conn = self._ensure_connected()
        row = conn.execute(
            """
            SELECT ch, volume_id, arc_id, in_world_time, summary, facts_json, entities_json
            FROM chapter_facts
            WHERE ch = ?
            """,
            (ch,),
        ).fetchone()

        if row is None:
            return None

        return ChapterFactRecord(
            ch=int(row[0]),
            volume_id=str(row[1]) if row[1] else None,
            arc_id=str(row[2]) if row[2] else None,
            in_world_time=str(row[3]) if row[3] else None,
            summary=str(row[4]),
            facts=json.loads(row[5]),
            entities=json.loads(row[6]),
        )

    def get_threads(
        self,
        *,
        status: ThreadStatus | str | None = None,
        at_chapter: int | None = None,
    ) -> list[Thread]:
        """查询伏笔悬线台账列表，支持按状态及章号过滤。"""
        conn = self._ensure_connected()
        conditions = []
        params: list[Any] = []

        if status is not None:
            stat_str = status.value if isinstance(status, ThreadStatus) else str(status)
            conditions.append("status = ?")
            params.append(stat_str)

        if at_chapter is not None:
            conditions.append("planted_at <= ?")
            params.append(at_chapter)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = conn.execute(
            f"""
            SELECT id, title, category, status, planted_at, last_progressed_at,
                   win_start, win_end, trigger_entities_json, public_clue, truth_secret
            FROM threads
            {where_clause}
            ORDER BY planted_at ASC, id ASC
            """,
            params,
        )
        threads: list[Thread] = []
        for r in cursor.fetchall():
            exp_window = None
            if r[6] is not None and r[7] is not None:
                exp_window = ChapterWindow(start=int(r[6]), end=int(r[7]))
            threads.append(
                Thread(
                    id=str(r[0]),
                    title=str(r[1]),
                    category=str(r[2]) if r[2] else "other",
                    status=ThreadStatus(r[3]),
                    planted_at=int(r[4]),
                    last_progressed_at=int(r[5]),
                    expected_window=exp_window,
                    trigger_entities=json.loads(r[8]),
                    public_clue=str(r[9]),
                    truth_secret=str(r[10]) if r[10] else None,
                )
            )
        return threads

    def get_alias_rows(self) -> list[tuple[str, str, int]]:
        """导出 (alias, entity_id, is_archived) 别名映射元组，供 AliasMatcher 初始化。"""
        conn = self._ensure_connected()
        cursor = conn.execute(
            "SELECT alias, entity_id, is_archived FROM entity_aliases ORDER BY alias ASC, entity_id ASC"
        )
        return [(str(r[0]), str(r[1]), int(r[2])) for r in cursor.fetchall()]

    def get_alias_matcher(self) -> AliasMatcher:
        """基于当前派生库构建并返回就绪的 AliasMatcher 自动机。"""
        return AliasMatcher.from_alias_rows(self.get_alias_rows())
