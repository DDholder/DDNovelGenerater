"""中途改设定五维防吃书影响面扫描器 (Task T10)。

展开目标实体的标准名称与全部外号/别名（或直接以自由关键词/伏笔 ID 检索），
在【已定稿正文、历史事实台账、活跃状态截面、未完结伏笔、后续未定稿蓝图】五大维度执行精确扫描，
借助全书 Aho-Corasick 自动机实现最长优先重叠消歧，准确区分“可安全直接修改”与“需圆前文”。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Self

import yaml

from .alias_matcher import AliasMatcher
from .indexer import SqliteIndexer
from .parser import ProgressiveParser
from .paths import (
    list_facts_files,
    resolve_active_characters_file,
    resolve_active_factions_file,
    resolve_archive_characters_file,
    resolve_archive_factions_file,
    resolve_archive_relations_file,
    resolve_blueprints_dir,
    resolve_config_path,
    resolve_manuscript_dir,
    resolve_relations_file,
    resolve_state_root,
    resolve_threads_file,
    resolve_world_state_file,
)
from .schemas import (
    ChapterFactRecord,
    CharacterStateFile,
    FactionStateFile,
    ImpactHit,
    ImpactReport,
    ImpactScope,
    MatchedEntity,
    NovelConfig,
    RelationsFile,
    ThreadsFile,
    ThreadStatus,
    WorldState,
)

_CHAPTER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:ch_?(\d+)|第\s*(\d+)\s*[章回节])",
    re.IGNORECASE,
)
_CHAPTER_TITLE_RE: Final[re.Pattern[str]] = re.compile(r"第\s*(\d+)\s*章")
_ASCII_ID_CHAR_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_]")

_OTHER_QUERY_ENTITY_ID: Final[str] = "other_impact_query"

_SCOPE_ORDER: Final[dict[ImpactScope, int]] = {
    ImpactScope.MANUSCRIPT: 0,
    ImpactScope.FACT: 1,
    ImpactScope.STATE: 2,
    ImpactScope.THREAD: 3,
    ImpactScope.BLUEPRINT: 4,
}


def _is_id_boundary_ok(text: str, start: int, end: int) -> bool:
    """检查 ASCII 标识符边界，避免短 ID 误命中更长标识符的前/后缀。"""
    if start > 0 and _ASCII_ID_CHAR_RE.match(text[start - 1]):
        return False
    if end < len(text) and _ASCII_ID_CHAR_RE.match(text[end]):
        return False
    return True


def _dedupe_non_empty(items: list[str]) -> list[str]:
    """去重保序并过滤空白字符串。"""
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def _extract_context_excerpt(
    text: str,
    start: int,
    end: int,
    *,
    radius: int = 60,
    total_hits: int = 1,
) -> str:
    """围绕命中区间截取高信噪比上下文摘录。"""
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    snippet = re.sub(r"\s+", " ", text[left:right]).strip()
    if left > 0:
        snippet = f"...{snippet}"
    if right < len(text):
        snippet = f"{snippet}..."
    if total_hits > 1:
        snippet = f"{snippet}（共 {total_hits} 处命中）"
    return snippet


def _extract_chapter_num(path: Path, content: str) -> int | None:
    """从文件名或正文标题提取正整章号（无合法章号时返回 None，严禁返回 0）。"""
    m_stem = _CHAPTER_RE.search(path.stem)
    if m_stem:
        ch = int(m_stem.group(1) or m_stem.group(2))
        return ch if ch >= 1 else None
    m_title = _CHAPTER_TITLE_RE.search(content)
    if m_title:
        ch = int(m_title.group(1))
        return ch if ch >= 1 else None
    return None


def _iter_profile_strings(value: Any, prefix: str = "") -> Iterator[tuple[str, str]]:
    """递归提取题材扩展槽 `profile_ext` 中的所有非空字符串条目及其路径。"""
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            yield (prefix or "value", cleaned)
        return
    if isinstance(value, dict):
        for k, v in value.items():
            sub_prefix = f"{prefix}.{k}" if prefix else str(k)
            yield from _iter_profile_strings(v, sub_prefix)
        return
    if isinstance(value, (list, tuple)):
        for idx, item in enumerate(value):
            sub_prefix = f"{prefix}[{idx}]" if prefix else f"[{idx}]"
            yield from _iter_profile_strings(item, sub_prefix)


def _load_yaml_data(path: Path) -> Any:
    """安全加载 YAML 文件内容，文件不存在或为空时返回 None。"""
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return None
    return yaml.safe_load(raw)


def _sort_hits(hits: list[ImpactHit]) -> list[ImpactHit]:
    """按五维作用域、章号与相对路径执行确定性排序。"""
    return sorted(
        hits,
        key=lambda h: (
            _SCOPE_ORDER[h.scope],
            0 if h.chapter_num is not None else 1,
            h.chapter_num if h.chapter_num is not None else 0,
            h.location,
            h.matched_term or "",
            h.excerpt,
        ),
    )


@dataclass(frozen=True, slots=True)
class _QuerySpec:
    """单次影响面扫描的规范化检索目标与自动机句柄。"""

    target: str
    entity_id: str | None
    expanded_terms: list[str]
    matcher: AliasMatcher
    match_entity_id: str
    literal_eid: str | None = None

    def matches_structured_id(self, value: str | None) -> bool:
        """判断结构化 ID 字段是否命中检索目标。"""
        if value is None:
            return False
        if self.entity_id is not None and value == self.entity_id:
            return True
        return value == self.target

    def find_text_hits(self, text: str) -> list[MatchedEntity]:
        """使用自动机扫描文本并过滤当前目标，同时补充字面量 entity_id 检出。"""
        if not text:
            return []

        raw_hits = self.matcher.scan_text(text, include_archived=True)
        filtered: list[MatchedEntity] = []
        for hit in raw_hits:
            if hit.entity_id != self.match_entity_id:
                continue
            if (
                self.literal_eid is not None
                and hit.alias == self.literal_eid
                and _ASCII_ID_CHAR_RE.match(hit.alias[0])
                and not _is_id_boundary_ok(text, hit.start, hit.end)
            ):
                continue
            filtered.append(hit)

        # 若外部注入了 matcher 且未包含字面量 entity_id，补充边界安全扫描
        if (
            self.literal_eid is not None
            and self.literal_eid not in self.matcher.get_aliases(self.match_entity_id)
            and self.literal_eid in text
        ):
            occupied = [(h.start, h.end) for h in filtered]
            pattern = re.compile(
                rf"(?<![A-Za-z0-9_]){re.escape(self.literal_eid)}(?![A-Za-z0-9_])"
            )
            for m in pattern.finditer(text):
                s, e = m.start(), m.end()
                if any(not (e <= os or s >= oe) for os, oe in occupied):
                    continue
                filtered.append(
                    MatchedEntity(
                        entity_id=self.match_entity_id,
                        alias=self.literal_eid,
                        start=s,
                        end=e,
                        is_archived=False,
                    )
                )
            filtered.sort(key=lambda h: (h.start, h.end))

        return filtered


class ImpactAnalyzer:
    """中途改设定五维防吃书影响面扫描器（FR-04）。"""

    def __init__(
        self,
        book_dir: Path | str,
        *,
        indexer: SqliteIndexer | None = None,
        matcher: AliasMatcher | None = None,
        parser: ProgressiveParser | None = None,
        config: NovelConfig | None = None,
        auto_sync: bool = True,
        enable_corpus_cache: bool = False,
    ) -> None:
        self._book_dir: Path = Path(book_dir).resolve()
        self._external_indexer: SqliteIndexer | None = indexer
        self._external_matcher: AliasMatcher | None = matcher
        self._auto_sync: bool = auto_sync
        self._enable_corpus_cache: bool = enable_corpus_cache
        self._owned_indexer: SqliteIndexer | None = None
        self._cached_full_matcher: AliasMatcher | None = None
        self._cached_entity_meta: dict[str, tuple[str, str]] | None = None
        self._cached_entity_terms: dict[str, list[str]] = {}
        self._cached_manuscript_hits: dict[str, list[ImpactHit]] | None = None
        self._cached_facts: list[tuple[str, list[ChapterFactRecord]]] | None = None
        self._cached_yaml_models: dict[tuple[Path, str], Any] = {}
        self._cached_blueprints: list[tuple[str, Any]] | None = None

        if config is not None:
            self._config = config
        elif indexer is not None:
            self._config = indexer.config
        else:
            cfg_path = resolve_config_path(self._book_dir)
            if cfg_path.is_file():
                self._config = NovelConfig.load(self._book_dir)
            else:
                self._config = NovelConfig(title=self._book_dir.name or "未命名小说")

        self._parser: ProgressiveParser = (
            parser if parser is not None else ProgressiveParser(book_dir=self._book_dir)
        )

    @property
    def book_dir(self) -> Path:
        """返回解析后的小说书库根目录。"""
        return self._book_dir

    @property
    def config(self) -> NovelConfig:
        """返回当前使用的小说配置对象。"""
        return self._config

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """幂等释放内部创建的 SQLite 连接（绝不关闭外部注入的 indexer）。"""
        if self._owned_indexer is not None:
            try:
                self._owned_indexer.close()
            finally:
                self._owned_indexer = None

    @contextmanager
    def _use_indexer(self) -> Iterator[SqliteIndexer]:
        """提供生命周期安全的 SqliteIndexer 会话上下文，确保自建连接在 finally 中关闭。"""
        if self._external_indexer is not None:
            yield self._external_indexer
            return

        if self._owned_indexer is not None:
            yield self._owned_indexer
            return

        temp_indexer = SqliteIndexer(
            self._book_dir,
            config=self._config,
            parser=self._parser,
        )
        self._owned_indexer = temp_indexer
        try:
            yield temp_indexer
        finally:
            self._owned_indexer = None
            temp_indexer.close()

    # ------------------------------------------------------------------
    # 公共扫描入口
    # ------------------------------------------------------------------

    def clear_cache(self) -> None:
        """清空批量扫描语料缓存。"""
        self._cached_full_matcher = None
        self._cached_entity_meta = None
        self._cached_entity_terms.clear()
        self._cached_manuscript_hits = None
        self._cached_facts = None
        self._cached_yaml_models.clear()
        self._cached_blueprints = None

    def scan_impact(
        self,
        target_id_or_keyword: str,
        *,
        after_chapter: int | None = None,
        include_archived_state: bool = False,
        sync_index: bool | None = None,
    ) -> ImpactReport:
        """跨五大维度扫描修改/删除指定设定或关键词的防吃书影响面。"""
        if not isinstance(target_id_or_keyword, str):
            raise ValueError(
                f"target_id_or_keyword 必须为非空字符串，实际得到 {type(target_id_or_keyword).__name__}"
            )
        target = target_id_or_keyword.strip()
        if not target:
            raise ValueError("target_id_or_keyword 不能为空白字符串")

        if after_chapter is not None:
            if isinstance(after_chapter, bool) or not isinstance(after_chapter, int) or after_chapter < 0:
                raise ValueError(f"after_chapter 必须为 >= 0 的整数，实际得到: {after_chapter!r}")

        should_sync = self._auto_sync if sync_index is None else sync_index

        with self._use_indexer() as active_indexer:
            if should_sync:
                active_indexer.sync()
                if self._enable_corpus_cache:
                    self.clear_cache()
            query = self._resolve_query(target, indexer=active_indexer)

        cutoff = after_chapter if after_chapter is not None else self._config.current_chapter

        hits: list[ImpactHit] = []
        hits.extend(self._scan_manuscripts(query))
        hits.extend(self._scan_facts(query))
        hits.extend(
            self._scan_state(
                query,
                after_chapter=after_chapter,
                include_archived_state=include_archived_state,
            )
        )
        hits.extend(self._scan_threads(query))
        hits.extend(self._scan_blueprints(query, cutoff=cutoff))

        sorted_hits = _sort_hits(hits)
        return ImpactReport(
            target=target,
            entity_id=query.entity_id,
            expanded_terms=query.expanded_terms,
            hits=sorted_hits,
            is_safe_to_change=(len(sorted_hits) == 0),
        )

    # ------------------------------------------------------------------
    # 目标解析与外号别名展开
    # ------------------------------------------------------------------

    def _resolve_query(self, target: str, *, indexer: SqliteIndexer) -> _QuerySpec:
        """解析目标为已知实体或自由关键词/伏笔 ID，并构造全书消歧自动机。"""
        conn = indexer.conn

        entity_row = conn.execute(
            "SELECT id, name, file_path FROM entities WHERE id = ?",
            (target,),
        ).fetchone()
        if entity_row is None:
            entity_row = conn.execute(
                """
                SELECT ea.entity_id, e.name, e.file_path
                FROM entity_aliases AS ea
                JOIN entities AS e ON e.id = ea.entity_id
                WHERE ea.alias = ?
                ORDER BY ea.is_archived ASC, ea.entity_id ASC
                LIMIT 1
                """,
                (target,),
            ).fetchone()

        if entity_row is not None:
            eid = str(entity_row[0])
            name = str(entity_row[1])
            file_path = str(entity_row[2] or "")
            if self._enable_corpus_cache and self._external_matcher is None:
                expanded_terms = self._cached_entity_terms.get(eid)
                if expanded_terms is None:
                    expanded_terms = self._collect_entity_terms(
                        eid=eid,
                        name=name,
                        file_path=file_path,
                        indexer=indexer,
                    )
                    self._cached_entity_terms[eid] = expanded_terms
                if self._cached_full_matcher is None:
                    self._cached_full_matcher = self._build_full_entity_matcher(indexer=indexer)
                cached_aliases = set(self._cached_full_matcher.get_aliases(eid))
                if all(t in cached_aliases for t in expanded_terms) and eid in cached_aliases:
                    matcher = self._cached_full_matcher
                else:
                    matcher = self._build_full_entity_matcher(
                        indexer=indexer,
                        extra_terms=(eid, expanded_terms),
                    )
            else:
                expanded_terms = self._collect_entity_terms(
                    eid=eid,
                    name=name,
                    file_path=file_path,
                    indexer=indexer,
                )
                matcher = (
                    self._external_matcher
                    if self._external_matcher is not None
                    else self._build_full_entity_matcher(
                        indexer=indexer,
                        extra_terms=(eid, expanded_terms),
                    )
                )
            return _QuerySpec(
                target=target,
                entity_id=eid,
                expanded_terms=expanded_terms,
                matcher=matcher,
                match_entity_id=eid,
                literal_eid=eid,
            )

        # 若未命中 SQLite 且外部注入了 matcher，兜底尝试从 matcher 反查
        if self._external_matcher is not None:
            matcher_eid: str | None = None
            if self._external_matcher.get_aliases(target):
                matcher_eid = target
            else:
                matcher_eid = self._external_matcher.resolve_alias(target, include_archived=True)
            if matcher_eid is not None:
                terms = _dedupe_non_empty(self._external_matcher.get_aliases(matcher_eid))
                return _QuerySpec(
                    target=target,
                    entity_id=matcher_eid,
                    expanded_terms=terms or [matcher_eid],
                    matcher=self._external_matcher,
                    match_entity_id=matcher_eid,
                    literal_eid=matcher_eid,
                )

        # 未映射到实体：检查是否为 threads.id，否则作为自由关键词
        thread_title = self._lookup_thread_title(target, indexer=indexer)
        raw_terms = [target]
        if thread_title:
            raw_terms.append(thread_title)
        expanded_terms = _dedupe_non_empty(raw_terms)

        temp_matcher = AliasMatcher(
            {_OTHER_QUERY_ENTITY_ID: expanded_terms},
            archive_non_active=False,
        )
        return _QuerySpec(
            target=target,
            entity_id=None,
            expanded_terms=expanded_terms,
            matcher=temp_matcher,
            match_entity_id=_OTHER_QUERY_ENTITY_ID,
            literal_eid=None,
        )

    def _collect_entity_terms(
        self,
        *,
        eid: str,
        name: str,
        file_path: str,
        indexer: SqliteIndexer,
    ) -> list[str]:
        """收集实体 `[name, *aliases]`，保持设置文档原有顺序并去重。"""
        terms: list[str] = []

        if file_path and file_path.endswith(".md"):
            abs_setting = self._book_dir / file_path
            if abs_setting.is_file():
                doc = self._parser.parse_setting_md(abs_setting, book_dir=self._book_dir)
                terms.extend(doc.all_names())

        if name:
            terms.append(name)

        alias_rows = indexer.conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias ASC",
            (eid,),
        ).fetchall()
        for (alias,) in alias_rows:
            terms.append(str(alias))

        if self._external_matcher is not None:
            terms.extend(self._external_matcher.get_aliases(eid))

        cleaned = _dedupe_non_empty(terms)
        return cleaned if cleaned else [eid]

    @staticmethod
    def _build_full_entity_matcher(
        *,
        indexer: SqliteIndexer,
        extra_terms: tuple[str, list[str]] | None = None,
    ) -> AliasMatcher:
        """构建包含全书全部实体（含冷库与字面量 ID）的消歧自动机。"""
        conn = indexer.conn
        aliases_by_entity: dict[str, list[str]] = {}
        archived_ids: set[str] = set()

        entity_rows = conn.execute(
            "SELECT id, name, is_archived FROM entities ORDER BY is_archived ASC, id ASC"
        ).fetchall()
        for raw_eid, raw_name, is_arch_int in entity_rows:
            eid = str(raw_eid)
            bucket = aliases_by_entity.setdefault(eid, [])
            if raw_name:
                bucket.append(str(raw_name))
            bucket.append(eid)
            if bool(is_arch_int):
                archived_ids.add(eid)

        alias_rows = conn.execute(
            """
            SELECT alias, entity_id, is_archived
            FROM entity_aliases
            ORDER BY is_archived ASC, entity_id ASC, alias ASC
            """
        ).fetchall()
        for raw_alias, raw_eid, is_arch_int in alias_rows:
            eid = str(raw_eid)
            aliases_by_entity.setdefault(eid, []).append(str(raw_alias))
            if bool(is_arch_int):
                archived_ids.add(eid)

        if extra_terms is not None:
            target_eid, terms = extra_terms
            aliases_by_entity.setdefault(target_eid, []).extend(terms)
            aliases_by_entity[target_eid].append(target_eid)

        return AliasMatcher(
            aliases_by_entity,
            archived_ids=archived_ids,
            archive_non_active=False,
        )

    def _lookup_thread_title(self, target: str, *, indexer: SqliteIndexer) -> str | None:
        """尝试将 target 作为 thread_id 反查伏笔标题。"""
        row = indexer.conn.execute(
            "SELECT title FROM threads WHERE id = ?",
            (target,),
        ).fetchone()
        if row is not None and row[0]:
            return str(row[0]).strip()

        threads_path = resolve_threads_file(self._book_dir)
        data = _load_yaml_data(threads_path)
        if data:
            threads_file = ThreadsFile.model_validate(data)
            for th in threads_file.threads:
                if th.id == target:
                    return th.title.strip()
        return None

    # ------------------------------------------------------------------
    # 五维作用域扫描
    # ------------------------------------------------------------------

    def _build_cached_manuscript_hits(self, matcher: AliasMatcher) -> dict[str, list[ImpactHit]]:
        """在启用语料缓存时一次性扫描全部已定稿正文并按实体 ID 聚合命中。"""
        hits_by_eid: dict[str, list[ImpactHit]] = {}
        manuscript_dir = resolve_manuscript_dir(self._book_dir)
        if not manuscript_dir.is_dir():
            return hits_by_eid

        for path in sorted(manuscript_dir.rglob("*.md")):
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8")
            raw_hits = matcher.scan_text(content, include_archived=True)
            if not raw_hits:
                continue

            grouped: dict[str, list[MatchedEntity]] = {}
            for hit in raw_hits:
                if (
                    hit.alias == hit.entity_id
                    and _ASCII_ID_CHAR_RE.match(hit.alias[0])
                    and not _is_id_boundary_ok(content, hit.start, hit.end)
                ):
                    continue
                grouped.setdefault(hit.entity_id, []).append(hit)

            if not grouped:
                continue

            rel_path = path.relative_to(self._book_dir).as_posix()
            ch_num = _extract_chapter_num(path, content)
            for eid, text_hits in grouped.items():
                first = text_hits[0]
                excerpt = _extract_context_excerpt(
                    content,
                    first.start,
                    first.end,
                    radius=60,
                    total_hits=len(text_hits),
                )
                hits_by_eid.setdefault(eid, []).append(
                    ImpactHit(
                        scope=ImpactScope.MANUSCRIPT,
                        location=rel_path,
                        chapter_num=ch_num,
                        matched_term=first.alias,
                        excerpt=excerpt,
                    )
                )
        return hits_by_eid

    def _scan_manuscripts(self, query: _QuerySpec) -> list[ImpactHit]:
        """维度 1：扫描 04_creation/manuscript/**/*.md 全文，每章汇总 1 条命中。"""
        if (
            self._enable_corpus_cache
            and self._cached_full_matcher is not None
            and query.matcher is self._cached_full_matcher
            and query.entity_id is not None
            and query.literal_eid == query.match_entity_id
        ):
            if self._cached_manuscript_hits is None:
                self._cached_manuscript_hits = self._build_cached_manuscript_hits(
                    self._cached_full_matcher
                )
            return list(self._cached_manuscript_hits.get(query.match_entity_id, ()))

        manuscript_dir = resolve_manuscript_dir(self._book_dir)
        if not manuscript_dir.is_dir():
            return []

        hits: list[ImpactHit] = []
        for path in sorted(manuscript_dir.rglob("*.md")):
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8")
            text_hits = query.find_text_hits(content)
            if not text_hits:
                continue

            first = text_hits[0]
            rel_path = path.relative_to(self._book_dir).as_posix()
            ch_num = _extract_chapter_num(path, content)
            excerpt = _extract_context_excerpt(
                content,
                first.start,
                first.end,
                radius=60,
                total_hits=len(text_hits),
            )
            hits.append(
                ImpactHit(
                    scope=ImpactScope.MANUSCRIPT,
                    location=rel_path,
                    chapter_num=ch_num,
                    matched_term=first.alias,
                    excerpt=excerpt,
                )
            )
        return hits

    def _load_fact_records(self) -> list[tuple[str, list[ChapterFactRecord]]]:
        """加载 03_state/history/*_facts.jsonl，在开启语料缓存时复用已解析记录。"""
        if self._enable_corpus_cache and self._cached_facts is not None:
            return self._cached_facts

        fact_files = list_facts_files(self._book_dir)
        if not fact_files:
            if self._enable_corpus_cache:
                self._cached_facts = []
            return []

        parsed: list[tuple[str, list[ChapterFactRecord]]] = []
        for path in fact_files:
            if not path.is_file():
                continue
            rel_path = path.relative_to(self._book_dir).as_posix()
            records = [
                ChapterFactRecord.model_validate_json(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            parsed.append((rel_path, records))

        if self._enable_corpus_cache:
            self._cached_facts = parsed
        return parsed

    def _scan_facts(self, query: _QuerySpec) -> list[ImpactHit]:
        """维度 2：扫描 03_state/history/*_facts.jsonl 历史事实台账。"""
        hits: list[ImpactHit] = []
        for rel_path, records in self._load_fact_records():
            for record in records:
                structured_term: str | None = None
                for eid_ref in record.entities:
                    if query.matches_structured_id(eid_ref):
                        structured_term = eid_ref
                        break

                summary_hits = query.find_text_hits(record.summary)
                matched_facts: list[str] = []
                first_fact_hit: MatchedEntity | None = None
                for fact in record.facts:
                    f_hits = query.find_text_hits(fact)
                    if f_hits:
                        matched_facts.append(fact)
                        if first_fact_hit is None:
                            first_fact_hit = f_hits[0]

                if structured_term is None and not summary_hits and first_fact_hit is None:
                    continue

                if first_fact_hit is not None:
                    matched_term = first_fact_hit.alias
                elif summary_hits:
                    matched_term = summary_hits[0].alias
                else:
                    matched_term = structured_term

                if matched_facts:
                    excerpt = "; ".join(matched_facts)
                else:
                    excerpt = record.summary

                hits.append(
                    ImpactHit(
                        scope=ImpactScope.FACT,
                        location=rel_path,
                        chapter_num=record.ch,
                        matched_term=matched_term,
                        excerpt=excerpt,
                    )
                )
        return hits

    def _scan_state(
        self,
        query: _QuerySpec,
        *,
        after_chapter: int | None,
        include_archived_state: bool,
    ) -> list[ImpactHit]:
        """维度 3：扫描活跃热池状态截面（含可选冷库状态）。"""
        state_dir = resolve_state_root(self._book_dir)
        if not state_dir.is_dir():
            return []

        hits: list[ImpactHit] = []
        char_paths = [resolve_active_characters_file(self._book_dir)]
        fac_paths = [resolve_active_factions_file(self._book_dir)]
        if include_archived_state:
            char_paths.append(resolve_archive_characters_file(self._book_dir))
            fac_paths.append(resolve_archive_factions_file(self._book_dir))

        for char_path in char_paths:
            hits.extend(self._scan_character_state_file(char_path, query))

        for fac_path in fac_paths:
            hits.extend(self._scan_faction_state_file(fac_path, query))

        hits.extend(self._scan_world_state_file(resolve_world_state_file(self._book_dir), query))
        hits.extend(
            self._scan_relations_file(
                resolve_relations_file(self._book_dir),
                query,
                after_chapter=after_chapter,
                filter_active=True,
            )
        )
        if include_archived_state:
            hits.extend(
                self._scan_relations_file(
                    resolve_archive_relations_file(self._book_dir),
                    query,
                    after_chapter=after_chapter,
                    filter_active=False,
                )
            )

        return hits

    def _load_cached_yaml_model(self, path: Path, model_cls: type[Any]) -> Any:
        """按路径和模型类型加载 YAML，开启语料缓存时复用已校验模型。"""
        key = (path, model_cls.__name__)
        if self._enable_corpus_cache and key in self._cached_yaml_models:
            return self._cached_yaml_models[key]
        data = _load_yaml_data(path)
        model = model_cls.model_validate(data) if data else None
        if self._enable_corpus_cache:
            self._cached_yaml_models[key] = model
        return model

    def _scan_character_state_file(self, path: Path, query: _QuerySpec) -> list[ImpactHit]:
        cf = self._load_cached_yaml_model(path, CharacterStateFile)
        if cf is None:
            return []

        rel_path = path.relative_to(self._book_dir).as_posix()
        hits: list[ImpactHit] = []

        for char_id, c_state in cf.root.items():
            details: list[str] = []
            text_terms: list[str] = []
            struct_terms: list[str] = []

            if query.matches_structured_id(char_id):
                struct_terms.append(char_id)
                details.append(f"角色键 {char_id}")

            if query.matches_structured_id(c_state.location):
                assert c_state.location is not None
                struct_terms.append(c_state.location)
                details.append(f"location={c_state.location}")

            self._collect_field_text_hits(
                query, c_state.physical_mental, "physical_mental", text_terms, details
            )
            for goal in c_state.current_goals:
                self._collect_field_text_hits(query, goal, "current_goals", text_terms, details)

            bounds = c_state.epistemic_bounds
            for field_name, items in (
                ("epistemic_bounds.knows", bounds.knows),
                ("epistemic_bounds.unaware_of", bounds.unaware_of),
                ("epistemic_bounds.misbelieves", bounds.misbelieves),
            ):
                for item in items:
                    self._collect_field_text_hits(query, item, field_name, text_terms, details)

            self._collect_profile_ext_hits(query, c_state.profile_ext, struct_terms, text_terms, details)

            if details:
                matched_term = text_terms[0] if text_terms else struct_terms[0]
                excerpt = f"[{char_id}] " + "; ".join(_dedupe_non_empty(details))
                hits.append(
                    ImpactHit(
                        scope=ImpactScope.STATE,
                        location=rel_path,
                        chapter_num=c_state.updated_at_ch,
                        matched_term=matched_term,
                        excerpt=excerpt,
                    )
                )
        return hits

    def _scan_faction_state_file(self, path: Path, query: _QuerySpec) -> list[ImpactHit]:
        ff = self._load_cached_yaml_model(path, FactionStateFile)
        if ff is None:
            return []

        rel_path = path.relative_to(self._book_dir).as_posix()
        hits: list[ImpactHit] = []

        for fac_id, f_state in ff.root.items():
            details: list[str] = []
            text_terms: list[str] = []
            struct_terms: list[str] = []

            if query.matches_structured_id(fac_id):
                struct_terms.append(fac_id)
                details.append(f"势力键 {fac_id}")

            if query.matches_structured_id(f_state.leader):
                assert f_state.leader is not None
                struct_terms.append(f_state.leader)
                details.append(f"leader={f_state.leader}")

            for loc_id in f_state.territory:
                if query.matches_structured_id(loc_id):
                    struct_terms.append(loc_id)
                    details.append(f"territory={loc_id}")

            for goal in f_state.current_goals:
                self._collect_field_text_hits(query, goal, "current_goals", text_terms, details)

            self._collect_profile_ext_hits(query, f_state.profile_ext, struct_terms, text_terms, details)

            if details:
                matched_term = text_terms[0] if text_terms else struct_terms[0]
                excerpt = f"[{fac_id}] " + "; ".join(_dedupe_non_empty(details))
                hits.append(
                    ImpactHit(
                        scope=ImpactScope.STATE,
                        location=rel_path,
                        chapter_num=f_state.updated_at_ch,
                        matched_term=matched_term,
                        excerpt=excerpt,
                    )
                )
        return hits

    def _scan_world_state_file(self, path: Path, query: _QuerySpec) -> list[ImpactHit]:
        ws = self._load_cached_yaml_model(path, WorldState)
        if ws is None:
            return []

        rel_path = path.relative_to(self._book_dir).as_posix()
        details: list[str] = []
        text_terms: list[str] = []

        self._collect_field_text_hits(query, ws.in_world_time, "in_world_time", text_terms, details)
        self._collect_field_text_hits(query, ws.macro_situation, "macro_situation", text_terms, details)

        if not details:
            return []

        ch_num = ws.current_chapter if ws.current_chapter >= 1 else None
        return [
            ImpactHit(
                scope=ImpactScope.STATE,
                location=rel_path,
                chapter_num=ch_num,
                matched_term=text_terms[0],
                excerpt="; ".join(_dedupe_non_empty(details)),
            )
        ]

    def _scan_relations_file(
        self,
        path: Path,
        query: _QuerySpec,
        *,
        after_chapter: int | None,
        filter_active: bool,
    ) -> list[ImpactHit]:
        rel_file = self._load_cached_yaml_model(path, RelationsFile)
        if rel_file is None:
            return []

        rel_path = path.relative_to(self._book_dir).as_posix()
        hits: list[ImpactHit] = []

        for rel in rel_file.relations:
            if filter_active:
                if after_chapter is not None and after_chapter >= 1:
                    if not rel.is_valid_at(after_chapter):
                        continue
                elif rel.invalid_at_ch is not None:
                    continue

            struct_term: str | None = None
            if query.matches_structured_id(rel.source):
                struct_term = rel.source
            elif query.matches_structured_id(rel.target):
                struct_term = rel.target

            text_hits = query.find_text_hits(rel.relation)
            if struct_term is None and not text_hits:
                continue

            matched_term = text_hits[0].alias if text_hits else struct_term
            excerpt = f"{rel.source} -> {rel.target}: {rel.relation}"
            hits.append(
                ImpactHit(
                    scope=ImpactScope.STATE,
                    location=rel_path,
                    chapter_num=rel.valid_from_ch,
                    matched_term=matched_term,
                    excerpt=excerpt,
                )
            )
        return hits

    def _scan_threads(self, query: _QuerySpec) -> list[ImpactHit]:
        """维度 4：扫描 03_state/threads.yaml 中未完结伏笔（不含 archive/threads.yaml）。"""
        threads_path = resolve_threads_file(self._book_dir)
        threads_file = self._load_cached_yaml_model(threads_path, ThreadsFile)
        if threads_file is None:
            return []

        rel_path = threads_path.relative_to(self._book_dir).as_posix()
        hits: list[ImpactHit] = []

        for thread in threads_file.threads:
            if thread.status == ThreadStatus.RESOLVED:
                continue

            struct_term: str | None = None
            if query.matches_structured_id(thread.id):
                struct_term = thread.id
            else:
                for eid_ref in thread.trigger_entities:
                    if query.matches_structured_id(eid_ref):
                        struct_term = eid_ref
                        break

            text_terms: list[str] = []
            extra_snippets: list[str] = []
            for label, val in (
                ("title", thread.title),
                ("public_clue", thread.public_clue),
                ("truth_secret", thread.truth_secret or ""),
            ):
                t_hits = query.find_text_hits(val)
                if t_hits:
                    text_terms.append(t_hits[0].alias)
                    if label != "title":
                        snippet = _extract_context_excerpt(
                            val, t_hits[0].start, t_hits[0].end, radius=40
                        )
                        extra_snippets.append(f"{label}: {snippet}")

            if struct_term is None and not text_terms:
                continue

            matched_term = text_terms[0] if text_terms else struct_term
            excerpt = f"[{thread.id}] {thread.title}"
            if extra_snippets:
                excerpt = f"{excerpt} | {'; '.join(extra_snippets)}"

            hits.append(
                ImpactHit(
                    scope=ImpactScope.THREAD,
                    location=rel_path,
                    chapter_num=thread.last_progressed_at,
                    matched_term=matched_term,
                    excerpt=excerpt,
                )
            )
        return hits

    def _load_blueprints(self) -> list[tuple[str, Any]]:
        """加载 02_structure/blueprints/**/*.md，在开启语料缓存时复用解析结果。"""
        if self._enable_corpus_cache and self._cached_blueprints is not None:
            return self._cached_blueprints

        bp_dir = resolve_blueprints_dir(self._book_dir)
        if not bp_dir.is_dir():
            if self._enable_corpus_cache:
                self._cached_blueprints = []
            return []

        parsed: list[tuple[str, Any]] = []
        for path in sorted(bp_dir.rglob("*.md")):
            if not path.is_file():
                continue
            rel_path = path.relative_to(self._book_dir).as_posix()
            bp = self._parser.parse_blueprint_md(path, book_dir=self._book_dir)
            parsed.append((rel_path, bp))

        if self._enable_corpus_cache:
            self._cached_blueprints = parsed
        return parsed

    def _scan_blueprints(self, query: _QuerySpec, *, cutoff: int) -> list[ImpactHit]:
        """维度 5：扫描 02_structure/blueprints/**/*.md 中 chapter > cutoff 的后续蓝图。"""
        hits: list[ImpactHit] = []
        for rel_path, bp in self._load_blueprints():
            if bp.chapter <= cutoff:
                continue

            struct_terms: list[str] = []
            struct_fields: list[str] = []
            text_terms: list[str] = []
            text_details: list[str] = []

            if query.matches_structured_id(bp.pov):
                assert bp.pov is not None
                struct_terms.append(bp.pov)
                struct_fields.append(f"pov={bp.pov}")

            for cast_name, cast_list in (
                ("cast.characters", bp.cast.characters),
                ("cast.locations", bp.cast.locations),
                ("cast.factions", bp.cast.factions),
                ("cast.items", bp.cast.items),
                ("unlock_secrets", bp.unlock_secrets),
            ):
                for item_id in cast_list:
                    if query.matches_structured_id(item_id):
                        struct_terms.append(item_id)
                        struct_fields.append(f"{cast_name}={item_id}")

            for t_ref in bp.topics:
                if query.matches_structured_id(t_ref.entity_id):
                    struct_terms.append(t_ref.entity_id)
                    struct_fields.append(f"topics={t_ref.entity_id}:{t_ref.topic}")
                self._collect_field_text_hits(query, t_ref.topic, "topics", text_terms, text_details)

            for th_ref in bp.threads:
                if query.matches_structured_id(th_ref.thread_id):
                    struct_terms.append(th_ref.thread_id)
                    struct_fields.append(f"threads={th_ref.thread_id}")

            if bp.title:
                self._collect_field_text_hits(query, bp.title, "title", text_terms, text_details)

            for beat in bp.beats:
                self._collect_field_text_hits(
                    query, beat.title, f"beat#{beat.index}.title", text_terms, text_details
                )
                self._collect_field_text_hits(
                    query, beat.content, f"beat#{beat.index}", text_terms, text_details
                )

            for forb in bp.forbidden:
                self._collect_field_text_hits(query, forb, "forbidden", text_terms, text_details)

            self._collect_field_text_hits(query, bp.body, "body", text_terms, text_details)

            if not struct_fields and not text_details:
                continue

            matched_term = text_terms[0] if text_terms else struct_terms[0]
            all_parts = _dedupe_non_empty([*text_details, *struct_fields])
            hits.append(
                ImpactHit(
                    scope=ImpactScope.BLUEPRINT,
                    location=rel_path,
                    chapter_num=bp.chapter,
                    matched_term=matched_term,
                    excerpt="; ".join(all_parts),
                )
            )
        return hits

    # ------------------------------------------------------------------
    # 字段匹配辅助工具
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_field_text_hits(
        query: _QuerySpec,
        text: str,
        field_label: str,
        text_terms: list[str],
        details: list[str],
    ) -> None:
        if not text:
            return
        t_hits = query.find_text_hits(text)
        if not t_hits:
            return
        first = t_hits[0]
        text_terms.append(first.alias)
        snippet = _extract_context_excerpt(
            text,
            first.start,
            first.end,
            radius=50,
            total_hits=len(t_hits),
        )
        details.append(f"{field_label}: {snippet}")

    @staticmethod
    def _collect_profile_ext_hits(
        query: _QuerySpec,
        profile_ext: dict[str, Any],
        struct_terms: list[str],
        text_terms: list[str],
        details: list[str],
    ) -> None:
        for key_path, val_str in _iter_profile_strings(profile_ext):
            if query.matches_structured_id(val_str):
                struct_terms.append(val_str)
                details.append(f"profile_ext.{key_path}: {val_str}")
                continue
            t_hits = query.find_text_hits(val_str)
            if t_hits:
                first = t_hits[0]
                text_terms.append(first.alias)
                snippet = _extract_context_excerpt(
                    val_str,
                    first.start,
                    first.end,
                    radius=50,
                    total_hits=len(t_hits),
                )
                details.append(f"profile_ext.{key_path}: {snippet}")


def scan_setting_impact(
    book_dir: Path | str,
    target_id_or_keyword: str,
    **kwargs: Any,
) -> ImpactReport:
    """一步执行设定或关键词的五维防吃书影响面扫描。"""
    init_keys = ("indexer", "matcher", "parser", "config", "auto_sync", "enable_corpus_cache")
    init_kwargs = {k: kwargs.pop(k) for k in init_keys if k in kwargs}
    with ImpactAnalyzer(book_dir, **init_kwargs) as analyzer:
        return analyzer.scan_impact(target_id_or_keyword, **kwargs)


__all__ = [
    "ImpactAnalyzer",
    "scan_setting_impact",
]
