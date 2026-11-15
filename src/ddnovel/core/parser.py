"""T03 宽容 Markdown 解析与渐进式三级切片器。

本模块提供面向作者自由书写习惯的宽容解析器 ``ProgressiveParser``：
1. 容忍缺失 YAML Frontmatter，依据目录路径与命名约定自动推断实体 ID、分类与名称；
2. 将设定文档 Markdown 正文切分为核心 (core)、按需专题 (topics) 与绝密底牌 (secret) 三级切片；
3. 机密内容默认严格物理隔离，杜绝大模型在常规写作中产生剧透；
4. 提供基于启发式规则的中英双语 Token 估算，向下游写作包预算提供参考。
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any, Final

import yaml

from ddnovel.core.paths import extract_clean_title
from ddnovel.core.schemas._base import (
    ARC_ID_PATTERN,
    ENTITY_ID_PATTERN,
    ID_PREFIX_CATEGORY,
    THREAD_ID_PATTERN,
    VOLUME_ID_PATTERN,
    EntityStatus,
    SettingCategory,
    ThreadAction,
)
from ddnovel.core.schemas.models import (
    Beat,
    BlueprintCast,
    BlueprintDoc,
    SettingDoc,
    ThreadRef,
    TopicRef,
)
from ddnovel.core.schemas.results import ParsedBlocks

# ---------------------------------------------------------------------------
# 1. 常量与正则规则
# ---------------------------------------------------------------------------

CATEGORY_PREFIX_MAP: Final[dict[SettingCategory, str]] = {
    SettingCategory.CHARACTER: "char_",
    SettingCategory.GEOGRAPHY: "loc_",
    SettingCategory.FACTION: "fac_",
    SettingCategory.WORLD: "world_",
    SettingCategory.RULE: "rule_",
    SettingCategory.ITEM: "item_",
    SettingCategory.OTHER: "other_",
}

CHINESE_NUMS: Final[dict[str, int]] = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
    "十三": 13,
    "十四": 14,
    "十五": 15,
    "十六": 16,
    "十七": 17,
    "十八": 18,
    "十九": 19,
    "二十": 20,
}

CIRCLED_NUMS: Final[dict[str, int]] = {chr(0x2460 + i): i + 1 for i in range(20)}
ROMAN_NUMS: Final[dict[str, int]] = {
    "i": 1,
    "ii": 2,
    "iii": 3,
    "iv": 4,
    "v": 5,
    "vi": 6,
    "vii": 7,
    "viii": 8,
    "ix": 9,
    "x": 10,
}

STATUS_MAP: Final[dict[str, EntityStatus]] = {
    "active": EntityStatus.ACTIVE,
    "alive": EntityStatus.ACTIVE,
    "living": EntityStatus.ACTIVE,
    "活跃": EntityStatus.ACTIVE,
    "存活": EntityStatus.ACTIVE,
    "在线": EntityStatus.ACTIVE,
    "正常": EntityStatus.ACTIVE,
    "活着": EntityStatus.ACTIVE,
    "deceased": EntityStatus.DECEASED,
    "dead": EntityStatus.DECEASED,
    "killed": EntityStatus.DECEASED,
    "fallen": EntityStatus.DECEASED,
    "死亡": EntityStatus.DECEASED,
    "阵亡": EntityStatus.DECEASED,
    "牺牲": EntityStatus.DECEASED,
    "已故": EntityStatus.DECEASED,
    "殒命": EntityStatus.DECEASED,
    "身陨": EntityStatus.DECEASED,
    "消亡": EntityStatus.DECEASED,
    "retired": EntityStatus.RETIRED,
    "retire": EntityStatus.RETIRED,
    "退隐": EntityStatus.RETIRED,
    "退休": EntityStatus.RETIRED,
    "隐退": EntityStatus.RETIRED,
    "封存": EntityStatus.RETIRED,
    "归隐": EntityStatus.RETIRED,
    "离场": EntityStatus.RETIRED,
    "隐居": EntityStatus.RETIRED,
    "封印": EntityStatus.RETIRED,
}

CATEGORY_SYNONYMS: Final[dict[str, SettingCategory]] = {
    # Characters
    "character": SettingCategory.CHARACTER,
    "characters": SettingCategory.CHARACTER,
    "char": SettingCategory.CHARACTER,
    "chars": SettingCategory.CHARACTER,
    "person": SettingCategory.CHARACTER,
    "persons": SettingCategory.CHARACTER,
    "role": SettingCategory.CHARACTER,
    "roles": SettingCategory.CHARACTER,
    "人物": SettingCategory.CHARACTER,
    "角色": SettingCategory.CHARACTER,
    "主角": SettingCategory.CHARACTER,
    "配角": SettingCategory.CHARACTER,
    # Geography
    "geography": SettingCategory.GEOGRAPHY,
    "location": SettingCategory.GEOGRAPHY,
    "locations": SettingCategory.GEOGRAPHY,
    "loc": SettingCategory.GEOGRAPHY,
    "locs": SettingCategory.GEOGRAPHY,
    "place": SettingCategory.GEOGRAPHY,
    "places": SettingCategory.GEOGRAPHY,
    "scene": SettingCategory.GEOGRAPHY,
    "scenes": SettingCategory.GEOGRAPHY,
    "地理": SettingCategory.GEOGRAPHY,
    "地点": SettingCategory.GEOGRAPHY,
    "场景": SettingCategory.GEOGRAPHY,
    "地名": SettingCategory.GEOGRAPHY,
    # Factions
    "faction": SettingCategory.FACTION,
    "factions": SettingCategory.FACTION,
    "fac": SettingCategory.FACTION,
    "facs": SettingCategory.FACTION,
    "organization": SettingCategory.FACTION,
    "organizations": SettingCategory.FACTION,
    "org": SettingCategory.FACTION,
    "orgs": SettingCategory.FACTION,
    "sect": SettingCategory.FACTION,
    "sects": SettingCategory.FACTION,
    "group": SettingCategory.FACTION,
    "groups": SettingCategory.FACTION,
    "势力": SettingCategory.FACTION,
    "门派": SettingCategory.FACTION,
    "组织": SettingCategory.FACTION,
    "阵营": SettingCategory.FACTION,
    "家族": SettingCategory.FACTION,
    # Items
    "item": SettingCategory.ITEM,
    "items": SettingCategory.ITEM,
    "prop": SettingCategory.ITEM,
    "props": SettingCategory.ITEM,
    "weapon": SettingCategory.ITEM,
    "weapons": SettingCategory.ITEM,
    "equipment": SettingCategory.ITEM,
    "artifact": SettingCategory.ITEM,
    "artifacts": SettingCategory.ITEM,
    "物品": SettingCategory.ITEM,
    "器物": SettingCategory.ITEM,
    "道具": SettingCategory.ITEM,
    "法宝": SettingCategory.ITEM,
    "装备": SettingCategory.ITEM,
    "武器": SettingCategory.ITEM,
    # World
    "world": SettingCategory.WORLD,
    "worldview": SettingCategory.WORLD,
    "lore": SettingCategory.WORLD,
    "background": SettingCategory.WORLD,
    "世界": SettingCategory.WORLD,
    "世界观": SettingCategory.WORLD,
    "背景": SettingCategory.WORLD,
    # Rules
    "rule": SettingCategory.RULE,
    "rules": SettingCategory.RULE,
    "system": SettingCategory.RULE,
    "systems": SettingCategory.RULE,
    "law": SettingCategory.RULE,
    "laws": SettingCategory.RULE,
    "规则": SettingCategory.RULE,
    "法则": SettingCategory.RULE,
    "体系": SettingCategory.RULE,
    "设定": SettingCategory.RULE,
    # Other
    "other": SettingCategory.OTHER,
    "others": SettingCategory.OTHER,
    "misc": SettingCategory.OTHER,
    "extra": SettingCategory.OTHER,
    "其它": SettingCategory.OTHER,
    "其他": SettingCategory.OTHER,
}

CATEGORY_CHINESE_MAP: Final[dict[str, SettingCategory]] = CATEGORY_SYNONYMS

THREAD_ACTION_MAP: Final[dict[str, ThreadAction]] = {
    "plant": ThreadAction.PLANT,
    "埋设": ThreadAction.PLANT,
    "埋下": ThreadAction.PLANT,
    "铺垫": ThreadAction.PLANT,
    "progress": ThreadAction.PROGRESS,
    "推进": ThreadAction.PROGRESS,
    "推进伏笔": ThreadAction.PROGRESS,
    "发展": ThreadAction.PROGRESS,
    "resolve": ThreadAction.RESOLVE,
    "解决": ThreadAction.RESOLVE,
    "回收": ThreadAction.RESOLVE,
    "揭晓": ThreadAction.RESOLVE,
}


def parse_chinese_num(text: str) -> int | None:
    """解析中文数字字符串（如 '十二' -> 12, '一百四十二' -> 142）。"""
    if not text:
        return None
    cn_digits = {
        "零": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    cn_units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    if text in cn_digits:
        return cn_digits[text]
    total = 0
    r = 0
    for ch in text:
        if ch in cn_digits:
            r = cn_digits[ch]
        elif ch in cn_units:
            unit = cn_units[ch]
            if r == 0:
                r = 1
            total += r * unit
            r = 0
        else:
            return None
    total += r
    return total if total > 0 else None


# 设定三级渐进切片标题正则（全面覆盖全半角括号、全角方括号、圆括号、副标题、破折号及修饰词变体）
BRACKET_OPEN: Final[str] = r"[\[【［（\(]"
BRACKET_CLOSE: Final[str] = r"[\]】］）\)]"

RE_CORE_HEADING: Final[re.Pattern[str]] = re.compile(
    rf"^#{{1,6}}\s*(?:"
    rf"{BRACKET_OPEN}\s*(?:核心|core)(?:设定|人设|速写|信息|档案|资料)?(?:\s*[:：\-—\s].*?)?\s*{BRACKET_CLOSE}\s*(?:[:：\-—\s]\s*)?(.*)|"
    rf"(?:核心|core)(?:设定|人设|速写|信息|档案|资料)?(?:[:：\-—\s]\s*.*|\s*$)"
    rf")\s*$",
    re.IGNORECASE,
)
RE_SECRET_HEADING: Final[re.Pattern[str]] = re.compile(
    rf"^#{{1,6}}\s*(?:"
    rf"{BRACKET_OPEN}\s*(?:机密|秘密|绝密|secret)(?:设定|底牌|信息|档案|资料)?(?:\s*[:：\-—\s].*?)?\s*{BRACKET_CLOSE}\s*(?:[:：\-—\s]\s*)?(.*)|"
    rf"(?:机密|秘密|绝密|secret)(?:设定|底牌|信息|档案|资料)?(?:[:：\-—\s]\s*.*|\s*$)"
    rf")\s*$",
    re.IGNORECASE,
)
RE_TOPIC_HEADING: Final[re.Pattern[str]] = re.compile(
    rf"^#{{1,6}}\s*(?:"
    rf"{BRACKET_OPEN}\s*(?:专题|主题|topic)(?:设定|信息|档案|资料)?\s*(?:[:：\-—\s]\s*(.+?)|)\s*{BRACKET_CLOSE}\s*(?:[:：\-—\s]\s*)?(.*)|"
    rf"(?:专题|主题|topic)(?:设定|信息|档案|资料)?(?:\s*[:：\-—\s]\s*(.+?)|\s*)$"
    rf")\s*$",
    re.IGNORECASE,
)
RE_AUTHOR_NOTE_HEADING: Final[re.Pattern[str]] = re.compile(
    rf"^#{{1,6}}\s*(?:"
    rf"{BRACKET_OPEN}\s*(?:作者(?:备忘|手记|笔记|注|随笔|私密|设定)?|备忘|备注|灵感(?:随笔|碎片)?|随笔|草稿|台词样本|对话样本|示例对话|示例台词|场景构想|桥段构想|author(?:\s*notes?)?|notes?|memo|drafts?)"
    rf"(?:\s*[:：\-—\s].*?)?\s*{BRACKET_CLOSE}\s*(?:[:：\-—\s].*)?$|"
    rf"(?:作者备忘|作者手记|作者注|台词样本|示例对话|示例台词|构思期样本|场景构想|桥段构想)(?:[:：\-—\s].*|\s*$)"
    rf")\s*$",
    re.IGNORECASE,
)

# 章节蓝图节拍标题正则
# 支持 `### 节拍 1: 标题`、`### 场景一: 标题`、`### 第一场: 标题`、`### 一、标题` 等
RE_BEAT_HEADING: Final[re.Pattern[str]] = re.compile(
    r"^#{2,4}\s*(?:"
    r"(?:节拍|分场|场景|场面|Beat|Scene)\s*[#第]?\s*([0-9]+|[一二三四五六七八九十百]+)\s*[场节个幕]?\s*[:：.、\s-]*(.*)|"
    r"第\s*([0-9]+|[一二三四五六七八九十百]+)\s*[场节个幕]\s*[:：.、\s-]*(.*)|"
    r"([1-9]\d?)\s*[:：.、]\s*(.*)|"
    r"([一二三四五六七八九十]+)\s*[:：.、]\s*(.*)|"
    r"[\(（]([0-9]+|[一二三四五六七八九十]+)[\)）]\s*[:：.、\s-]*(.*)|"
    r"[\[【［](?:节拍|Beat|Scene)?\s*([0-9]+|[一二三四五六七八九十]+)\s*[\]】］]\s*[:：.、\s-]*(.*)|"
    r"([①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳])\s*[:：.、\s-]*(.*)"
    r")$",
    re.IGNORECASE,
)
RE_BEAT_LIST_PREFIX: Final[re.Pattern[str]] = re.compile(
    r"^(\s*(?:[-*•]\s*)?)(?:"
    r"(?:节拍|Beat|Scene|分场)\s*[#第]?\s*([0-9]+|[一二三四五六七八九十]+)\s*[场节个]?\s*[:：.、\s-]|"
    r"(\d+)[.、\)]|"
    r"[\(（](\d+)[\)）]|"
    r"([一二三四五六七八九十]+)[.、\)]|"
    r"[\(（]([一二三四五六七八九十]+)[\)）]|"
    r"([①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳])|"
    r"([ivxIVX]+)[.、\)]|"
    r"[\[【［](?:节拍|Beat|Scene)?\s*([0-9]+|[一二三四五六七八九十]+)\s*[\]】］][:：.、\s-]?"
    r")\s*(.*)$",
    re.IGNORECASE,
)
RE_TITLE_CONTENT: Final[re.Pattern[str]] = re.compile(
    r"^(?:"
    r"\*\*(.+?)\*\*[:：\-—]?\s*(.*)|"
    r"[【\[［](.+?)[】\]］][:：\-—]?\s*(.*)|"
    r"([^:：\n]{1,30})[:：\-—]\s*(.*)|"
    r"(.*)"
    r")$"
)

RE_SEC_CHAR: Final[re.Pattern[str]] = re.compile(
    r"^(?:出场人物|出场角色|登场人物|登场角色|出场人员|Characters?)(?:清单|列表|表)?$",
    re.I,
)
RE_SEC_LOC: Final[re.Pattern[str]] = re.compile(
    r"^(?:涉及地点|出场地标|涉及场景|地点|场景|Locations?)(?:清单|列表|表)?$",
    re.I,
)
RE_SEC_FAC: Final[re.Pattern[str]] = re.compile(
    r"^(?:涉及势力|出场门派|势力|门派|组织|阵营|Factions?)(?:清单|列表|表)?$",
    re.I,
)
RE_SEC_ITEM: Final[re.Pattern[str]] = re.compile(
    r"^(?:涉及物品|涉及道具|道具清单|物品|道具|法宝|装备|Items?)(?:清单|列表|表)?$",
    re.I,
)
RE_SEC_THREAD: Final[re.Pattern[str]] = re.compile(
    r"^(?:涉及伏笔|伏笔引用|关联伏笔|伏笔|Threads?)(?:清单|列表|表|线索)?$",
    re.I,
)
RE_SEC_SECRET: Final[re.Pattern[str]] = re.compile(
    r"^(?:解锁机密|机密解锁|解密实体|解锁绝密|Unlock\s*Secrets?)(?:清单|列表|表)?$",
    re.I,
)
RE_SEC_TOPIC: Final[re.Pattern[str]] = re.compile(
    r"^(?:按需专题|挂载专题|涉及专题|专题引用|Topics?)(?:清单|列表|表)?$",
    re.I,
)
RE_SEC_FORBID: Final[re.Pattern[str]] = re.compile(
    r"^(?:禁止事项|创作红线|红线指南|负向禁止|禁止|红线|Forbidden)(?:清单|列表|表)?$",
    re.I,
)


# ---------------------------------------------------------------------------
# 2. 基础切分与 Token 估算函数
# ---------------------------------------------------------------------------


def split_frontmatter(raw_text: str) -> tuple[dict[str, Any], str]:
    """切分 Markdown 顶部的 YAML Frontmatter 与正文正文字符串。

    容忍格式不规整（空 Frontmatter、语法微瑕、无正文等），绝不抛出异常。
    """
    text = raw_text.lstrip("\ufeff")
    stripped_start = text.lstrip("\r\n \t")
    if not stripped_start.startswith("---"):
        return {}, text.strip()

    pattern = re.compile(r"^---\s*\n(.*?)\n(?:---|(?<!\.)\.\.\.)\s*(?:\n(.*))?$", re.DOTALL)
    match = pattern.match(stripped_start)
    if match:
        fm_str = match.group(1)
        body = match.group(2) or ""
        # 若 fm_str 内部含 Markdown 标题，说明并非 Frontmatter 而是 Markdown 正文与分隔线
        if re.search(r"^#{1,6}\s+", fm_str, re.MULTILINE):
            return {}, text.strip()
        try:
            parsed = yaml.safe_load(fm_str)
            if isinstance(parsed, dict):
                return parsed, body.strip()
            elif isinstance(parsed, list):
                return {}, body.strip()
            elif parsed is None:
                return {}, body.strip()
            else:
                return {}, text.strip()
        except Exception:
            return {}, body.strip()

    # 兼容尾部无换行仅有 --- 的极端边界
    parts = stripped_start.split("\n---", maxsplit=1)
    if len(parts) == 2:
        fm_candidate = parts[0][3:].strip()
        rest = parts[1].strip()
        if rest.startswith("\n"):
            rest = rest[1:]
        if re.search(r"^#{1,6}\s+", fm_candidate, re.MULTILINE):
            return {}, text.strip()
        try:
            parsed = yaml.safe_load(fm_candidate)
            if isinstance(parsed, dict):
                return parsed, rest.strip()
            elif isinstance(parsed, list):
                return {}, rest.strip()
            elif parsed is None:
                return {}, rest.strip()
            else:
                return {}, text.strip()
        except Exception:
            return {}, text.strip()

    return {}, text.strip()


def estimate_tokens(text: str) -> int:
    """估算文本的 Token 消耗量（向下游写作包预算提供参考）。

    采用广泛适用于中英双语大模型（如 GPT-4 / Claude / DeepSeek）的启发式混合估算：
    - 中文字符与全角标点按约 1.3 token/字折算；
    - 西文字词按词长分词折算（平均 1 词 = 1.2 token，长词每 4 字符约 1 token）；
    - 其它可见符号按 1 token 折算。
    空文本或纯空白返回 0，非空文本保证返回正整数。
    """
    if not text or not text.strip():
        return 0

    cjk_chars = len(
        re.findall(
            r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3000-\u303f\uff01-\uff5e]",
            text,
        )
    )
    latin_words = re.findall(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?", text)
    latin_tokens = 0
    for w in latin_words:
        latin_tokens += max(1, math.ceil(len(w) / 4))

    other_symbols = len(
        re.findall(
            r"[^\s\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3000-\u303f\uff01-\uff5eA-Za-z0-9]",
            text,
        )
    )

    total = math.ceil(cjk_chars * 1.3) + math.ceil(latin_tokens * 1.2) + other_symbols
    return max(1, int(total))


_HTML_INLINE_COMMENT_RE: Final[re.Pattern[str]] = re.compile(r"<!--.*?-->")
_FENCE_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(`{3,}|~{3,})")


def _strip_html_comments_outside_fences(body: str) -> str:
    """在保留代码围栏原貌的前提下，剥离代码块外部的 HTML 注释 ``<!-- ... -->``。"""
    if "<!--" not in body:
        return body

    out_lines: list[str] = []
    in_code_fence: tuple[str, int] | None = None
    in_html_comment = False

    for raw_line in body.splitlines():
        m_fence = _FENCE_LINE_RE.match(raw_line)
        if in_code_fence is not None:
            f_char, f_len = in_code_fence
            if m_fence and m_fence.group(1)[0] == f_char and len(m_fence.group(1)) >= f_len:
                in_code_fence = None
            out_lines.append(raw_line)
            continue

        if not in_html_comment and m_fence:
            in_code_fence = (m_fence.group(1)[0], len(m_fence.group(1)))
            out_lines.append(raw_line)
            continue

        line = raw_line
        while True:
            if in_html_comment:
                end_idx = line.find("-->")
                if end_idx == -1:
                    line = ""
                    break
                line = line[end_idx + 3 :]
                in_html_comment = False
                continue

            line = _HTML_INLINE_COMMENT_RE.sub("", line)
            start_idx = line.find("<!--")
            if start_idx != -1:
                line = line[:start_idx]
                in_html_comment = True
            break

        if in_html_comment and not line.strip():
            continue
        if raw_line.strip() and not line.strip() and "<!--" in raw_line:
            continue
        out_lines.append(line)

    return "\n".join(out_lines)


def split_progressive_blocks(markdown: str) -> ParsedBlocks:
    """将设定文档 Markdown 正文切分为核心、专题、机密三级渐进切片，并估算 Token。

    - 核心切片：``## [核心]`` 标题下的内容，或未声明特殊标题时的默认全文/首部正文。
    - 专题切片：``## [专题: X]`` 或 ``## 【专题：X】`` 标题下的按需挂载内容。
    - 机密切片：``## [机密]`` 标题下的绝密底牌，默认严格物理隔离。
    - 作者备忘：``## [作者备忘]`` / ``## [灵感]`` / ``## [台词样本]`` 及 ``<!-- ... -->``
      HTML 注释在解析期自动剥离，绝不进入模型设定切片。
    """
    _, raw_body = split_frontmatter(markdown)
    body = _strip_html_comments_outside_fences(raw_body)

    prelude_lines: list[str] = []
    core_lines: list[str] = []
    topics_map: dict[str, list[str]] = {}
    secret_lines: list[str] = []

    current_section = "prelude"
    current_topic: str | None = None
    in_code_fence: tuple[str, int] | None = None

    for line in body.splitlines():
        # 代码块栅栏追踪：处于代码块内时，绝不误触切片标题切分
        m_fence = _FENCE_LINE_RE.match(line)
        if in_code_fence is not None:
            f_char, f_len = in_code_fence
            if m_fence and m_fence.group(1)[0] == f_char and len(m_fence.group(1)) >= f_len:
                in_code_fence = None
            if current_section == "prelude":
                prelude_lines.append(line)
            elif current_section == "core":
                core_lines.append(line)
            elif current_section == "topic" and current_topic is not None:
                topics_map[current_topic].append(line)
            elif current_section == "secret":
                secret_lines.append(line)
            continue

        if m_fence:
            in_code_fence = (m_fence.group(1)[0], len(m_fence.group(1)))
            if current_section == "prelude":
                prelude_lines.append(line)
            elif current_section == "core":
                core_lines.append(line)
            elif current_section == "topic" and current_topic is not None:
                topics_map[current_topic].append(line)
            elif current_section == "secret":
                secret_lines.append(line)
            continue

        if RE_AUTHOR_NOTE_HEADING.match(line):
            current_section = "author_note"
            current_topic = None
            continue

        m_topic = RE_TOPIC_HEADING.match(line)
        if m_topic:
            current_section = "topic"
            current_topic = (
                m_topic.group(1) or m_topic.group(2) or m_topic.group(3) or ""
            ).strip()
            if not current_topic:
                current_topic = "未命名专题"
            if current_topic not in topics_map:
                topics_map[current_topic] = []
            elif topics_map[current_topic]:
                topics_map[current_topic].append("")
            continue

        if RE_SECRET_HEADING.match(line):
            current_section = "secret"
            current_topic = None
            if secret_lines:
                secret_lines.append("")
            continue

        if RE_CORE_HEADING.match(line):
            current_section = "core"
            current_topic = None
            if core_lines:
                core_lines.append("")
            continue

        if current_section == "author_note":
            continue
        elif current_section == "prelude":
            prelude_lines.append(line)
        elif current_section == "core":
            core_lines.append(line)
        elif current_section == "topic" and current_topic is not None:
            topics_map[current_topic].append(line)
        elif current_section == "secret":
            secret_lines.append(line)

    prelude_part = "\n".join(prelude_lines).strip()
    core_part = "\n".join(core_lines).strip()

    if core_part and prelude_part:
        final_core = f"{prelude_part}\n\n{core_part}"
    elif core_part:
        final_core = core_part
    else:
        final_core = prelude_part

    topics_result: dict[str, str] = {}
    for t_name, t_lines in topics_map.items():
        t_text = "\n".join(t_lines).strip()
        topics_result[t_name] = t_text

    secret_part = "\n".join(secret_lines).strip()
    final_secret = secret_part if secret_part else None

    token_estimates: dict[str, int] = {}
    token_estimates["core"] = estimate_tokens(final_core)
    for t_name, t_content in topics_result.items():
        token_estimates[f"topic:{t_name}"] = estimate_tokens(t_content)
    if final_secret is not None:
        token_estimates["secret"] = estimate_tokens(final_secret)

    return ParsedBlocks(
        core=final_core,
        topics=topics_result,
        secret=final_secret,
        token_estimates=token_estimates,
    )


# ---------------------------------------------------------------------------
# 3. 辅助推断内部函数
# ---------------------------------------------------------------------------


def _normalize_entity_id(
    raw_id: Any,
    fallback_stem: str,
    category: SettingCategory,
) -> str:
    """确保实体 ID 严格符合 ``ENTITY_ID_PATTERN``（下划线分段的小写英数标识符）。"""
    prefix = CATEGORY_PREFIX_MAP.get(category, "other_")
    if raw_id is not None and str(raw_id).strip():
        cand = str(raw_id).strip().lower()
        if re.match(ENTITY_ID_PATTERN, cand):
            return cand
        clean = re.sub(r"[^a-z0-9_]+", "_", cand).strip("_")
        if clean:
            if clean.startswith(prefix) and re.match(ENTITY_ID_PATTERN, clean):
                return clean
            cand2 = f"{prefix}{clean}"
            if re.match(ENTITY_ID_PATTERN, cand2):
                return cand2
            return f"{prefix}{clean}_1"

    stem_cand = fallback_stem.strip().lower()
    if re.match(ENTITY_ID_PATTERN, stem_cand):
        return stem_cand

    m_pref_stem = re.match(r"^([a-z][a-z0-9]*_[a-z0-9_]+)-", stem_cand)
    if m_pref_stem and re.match(ENTITY_ID_PATTERN, m_pref_stem.group(1)):
        return m_pref_stem.group(1)

    m_dash_stem = re.match(r"^([a-z][a-z0-9]*)-(\d+)-", stem_cand)
    if m_dash_stem:
        cand_dash = f"{m_dash_stem.group(1)}_{m_dash_stem.group(2)}"
        if re.match(ENTITY_ID_PATTERN, cand_dash):
            return cand_dash

    clean_stem = re.sub(r"[^a-z0-9_]+", "_", stem_cand).strip("_")
    if clean_stem:
        if clean_stem.startswith(prefix) and re.match(ENTITY_ID_PATTERN, clean_stem):
            return clean_stem
        cand_stem = f"{prefix}{clean_stem}"
        if re.match(ENTITY_ID_PATTERN, cand_stem):
            return cand_stem
        return f"{prefix}{clean_stem}_1"

    h = hashlib.md5(fallback_stem.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}{h}"


def _infer_category(
    raw_category: Any,
    entity_id_hint: str | None,
    path: Path,
) -> SettingCategory:
    """根据显式声明、实体 ID 前缀或目录路径推断设定分类。"""
    if raw_category is not None:
        cand_cat = str(raw_category).strip().lower()
        if cand_cat in CATEGORY_SYNONYMS:
            return CATEGORY_SYNONYMS[cand_cat]
        for cat in SettingCategory:
            if cat.value == cand_cat:
                return cat

    hints = [entity_id_hint, path.stem]
    for h in hints:
        if h:
            h_lower = h.lower()
            for prefix, cat in ID_PREFIX_CATEGORY.items():
                if h_lower.startswith(prefix):
                    return cat

    lowered_parts = [p.lower() for p in path.parts]
    for part in lowered_parts:
        if part in CATEGORY_SYNONYMS:
            return CATEGORY_SYNONYMS[part]

    return SettingCategory.OTHER


RE_RESERVED_HEADING_NAMES: Final[set[str]] = {
    "核心", "核心设定", "核心人设", "核心速写", "核心档案", "核心资料", "核心信息",
    "机密", "绝密", "机密设定", "绝密设定", "机密档案", "绝密档案", "机密资料", "秘密", "机密信息",
    "专题", "专题设定", "主题",
}


def _infer_name(
    raw_name: Any,
    body: str,
    path: Path,
    entity_id: str,
    category: SettingCategory,
) -> str:
    """推断实体标准名称，保证返回非空字符串。"""
    if raw_name is not None and str(raw_name).strip():
        return str(raw_name).strip()

    # 从 Markdown H1 标题中查找，跳过核心/机密/专题等结构性段落标题
    for m in re.finditer(r"^#\s+(.+)$", body, re.MULTILINE):
        candidate = m.group(1).strip()
        candidate = re.sub(r"[\[【\]】*`_]", "", candidate).strip()
        if not candidate:
            continue
        m_pref = re.match(r"^(?:角色|人物|实体|设定|档案|人设)\s*[:：\-—]\s*(.+)$", candidate)
        if m_pref:
            candidate = m_pref.group(1).strip()
        if (
            RE_CORE_HEADING.match(m.group(0))
            or RE_SECRET_HEADING.match(m.group(0))
            or RE_TOPIC_HEADING.match(m.group(0))
            or candidate in RE_RESERVED_HEADING_NAMES
        ):
            continue
        return candidate

    stem = path.stem.strip()
    m_id_label = re.match(r"^[a-z][a-z0-9]*_[a-z0-9_]+-(.+)$", stem, re.IGNORECASE)
    if m_id_label:
        label = m_id_label.group(1).strip()
        if label:
            return label

    m_dash_label = re.match(r"^[a-z][a-z0-9]*-[a-z0-9]+-(.+)$", stem, re.IGNORECASE)
    if m_dash_label:
        label = m_dash_label.group(1).strip()
        if label:
            return label

    prefix = CATEGORY_PREFIX_MAP.get(category, "")
    if prefix and stem.lower().startswith(prefix) and len(stem) > len(prefix):
        clean_name = stem[len(prefix) :].strip()
        if clean_name:
            return clean_name

    if stem:
        return stem

    return entity_id


def _normalize_file_path(path: Path, book_dir: Path | None) -> str:
    """将文件路径规范化为相对于书库根目录或四维结构的 POSIX 路径。"""
    if book_dir is not None:
        try:
            return path.resolve().relative_to(book_dir.resolve()).as_posix()
        except ValueError:
            pass

    parts = list(path.parts)
    for root_marker in (
        "01_设定集",
        "02_结构",
        "03_状态",
        "04_创作",
        "01_settings",
        "02_structure",
        "03_state",
        "04_creation",
    ):
        if root_marker in parts:
            idx = parts.index(root_marker)
            return Path(*parts[idx:]).as_posix()

    return path.as_posix()


def _extract_beats_from_markdown(body: str) -> tuple[list[Beat], str]:
    """从 Markdown 正文中宽容解析节拍列表。

    支持三级/四级节拍标题（如 `### 节拍 1: 标题`）与有序列表格式（如 `1. **标题**: 内容`）。
    解析成功后返回 `(beats, remaining_body)`，节拍内容从正文中剥离，避免正文重复。
    若未检测到节拍格式，原样返回 `([], body)`。
    """
    lines = body.splitlines()
    beats: list[Beat] = []
    beat_line_indices: set[int] = set()
    beat_section_headers: set[int] = set()

    # 优先尝试基于 Markdown 标题分段的节拍（### 节拍 1: 标题 或 ### 1. 标题）
    i = 0
    current_beat_idx: int | None = None
    current_beat_title = ""
    current_beat_lines: list[str] = []
    heading_found = False

    while i < len(lines):
        line = lines[i]
        if re.match(r"^#{1,4}\s*(?:分场节拍|节拍清单|节拍|Beats?)\s*$", line, re.IGNORECASE):
            beat_section_headers.add(i)
            i += 1
            continue

        m = RE_BEAT_HEADING.match(line)
        if m:
            heading_found = True
            if current_beat_idx is not None:
                b_title = current_beat_title.strip()
                b_lines = list(current_beat_lines)
                if not b_title and b_lines:
                    first_l = b_lines[0].strip()
                    m_tc = RE_TITLE_CONTENT.match(first_l)
                    if m_tc:
                        extracted_t = (m_tc.group(1) or m_tc.group(3) or m_tc.group(5) or "").strip()
                        extracted_c = (m_tc.group(2) or m_tc.group(4) or m_tc.group(6) or "").strip()
                        if extracted_t:
                            b_title = extracted_t
                            if extracted_c:
                                b_lines[0] = extracted_c
                            else:
                                b_lines = b_lines[1:]
                beats.append(
                    Beat(
                        index=current_beat_idx,
                        title=b_title,
                        content="\n".join(b_lines).strip(),
                    )
                )
                current_beat_lines = []

            raw_idx = (
                m.group(1)
                or m.group(3)
                or m.group(5)
                or m.group(7)
                or m.group(9)
                or m.group(11)
                or m.group(13)
            )
            raw_title = (
                m.group(2)
                or m.group(4)
                or m.group(6)
                or m.group(8)
                or m.group(10)
                or m.group(12)
                or m.group(14)
                or ""
            )
            if raw_idx in CHINESE_NUMS:
                b_idx = CHINESE_NUMS[raw_idx]
            elif raw_idx in CIRCLED_NUMS:
                b_idx = CIRCLED_NUMS[raw_idx]
            elif raw_idx and raw_idx.isdigit():
                b_idx = int(raw_idx)
            else:
                cn_v = parse_chinese_num(raw_idx) if raw_idx else None
                b_idx = cn_v if (cn_v and cn_v >= 1) else (len(beats) + 1)

            current_beat_idx = b_idx if b_idx >= 1 else (len(beats) + 1)
            current_beat_title = raw_title.strip()
            beat_line_indices.add(i)
            i += 1
            while i < len(lines):
                next_line = lines[i]
                if RE_BEAT_HEADING.match(next_line):
                    break
                if re.match(r"^#{1,3}\s+", next_line) and not RE_BEAT_HEADING.match(next_line):
                    break
                beat_line_indices.add(i)
                current_beat_lines.append(next_line)
                i += 1
            continue
        i += 1

    if current_beat_idx is not None:
        b_title = current_beat_title.strip()
        b_lines = list(current_beat_lines)
        if not b_title and b_lines:
            first_l = b_lines[0].strip()
            m_tc = RE_TITLE_CONTENT.match(first_l)
            if m_tc:
                extracted_t = (m_tc.group(1) or m_tc.group(3) or m_tc.group(5) or "").strip()
                extracted_c = (m_tc.group(2) or m_tc.group(4) or m_tc.group(6) or "").strip()
                if extracted_t:
                    b_title = extracted_t
                    if extracted_c:
                        b_lines[0] = extracted_c
                    else:
                        b_lines = b_lines[1:]
        beats.append(
            Beat(
                index=current_beat_idx,
                title=b_title,
                content="\n".join(b_lines).strip(),
            )
        )

    if heading_found and beats:
        beat_line_indices.update(beat_section_headers)
        remaining = [lines[idx] for idx in range(len(lines)) if idx not in beat_line_indices]
        return beats, "\n".join(remaining).strip()

    # 备选尝试：基于列表解析节拍（支持有或无 `## 节拍` 标题引导，支持多行与缩进子内容）
    list_beats: list[Beat] = []
    list_line_indices: set[int] = set()
    in_beat_section = False

    curr_l_idx: int | None = None
    curr_l_title = ""
    curr_l_lines: list[str] = []

    def _commit_curr_list_beat() -> None:
        nonlocal curr_l_idx, curr_l_title, curr_l_lines
        if curr_l_idx is not None:
            final_title = curr_l_title.strip()
            if not final_title and curr_l_lines:
                first_text = curr_l_lines[0].strip()
                if len(first_text) <= 40:
                    final_title = first_text
                    curr_l_lines = curr_l_lines[1:]
            list_beats.append(
                Beat(
                    index=curr_l_idx,
                    title=final_title,
                    content="\n".join(curr_l_lines).strip(),
                )
            )
            curr_l_idx = None
            curr_l_title = ""
            curr_l_lines = []

    for j, line in enumerate(lines):
        if re.match(r"^#{1,4}\s*(?:分场节拍|节拍清单|节拍|Beats?)\s*$", line, re.IGNORECASE):
            _commit_curr_list_beat()
            in_beat_section = True
            list_line_indices.add(j)
            continue

        if in_beat_section and line.startswith("#"):
            _commit_curr_list_beat()
            in_beat_section = False

        m_list = RE_BEAT_LIST_PREFIX.match(line)
        is_list_start = False
        num: int | None = None
        rest_l = ""
        if m_list:
            raw_num = (
                m_list.group(2)
                or m_list.group(3)
                or m_list.group(4)
                or m_list.group(5)
                or m_list.group(6)
                or m_list.group(7)
                or m_list.group(8)
                or m_list.group(9)
            )
            if raw_num in CIRCLED_NUMS:
                num = CIRCLED_NUMS[raw_num]
            elif raw_num and raw_num.isdigit():
                num = int(raw_num)
            elif raw_num and raw_num.lower() in ROMAN_NUMS:
                num = ROMAN_NUMS[raw_num.lower()]
            elif raw_num and raw_num in CHINESE_NUMS:
                num = CHINESE_NUMS[raw_num]
            elif raw_num:
                cn_v = parse_chinese_num(raw_num)
                if cn_v and cn_v >= 1:
                    num = cn_v

            rest_l = m_list.group(10) or ""
            if num is not None:
                if (
                    in_beat_section
                    or (curr_l_idx is None and num == 1)
                    or (curr_l_idx is not None and num == curr_l_idx + 1)
                ):
                    is_list_start = True
        elif (
            in_beat_section
            and (curr_l_idx is None or not line.startswith(("    ", "\t", "  ")))
            and line.strip().startswith(("- ", "* ", "• "))
        ):
            num = (curr_l_idx + 1) if curr_l_idx is not None else (len(list_beats) + 1)
            is_list_start = True
            rest_l = re.sub(r"^\s*[-*•]\s*", "", line).strip()

        if is_list_start and num is not None:
            _commit_curr_list_beat()
            curr_l_idx = num
            m_tc = RE_TITLE_CONTENT.match(rest_l)
            if m_tc:
                curr_l_title = (m_tc.group(1) or m_tc.group(3) or m_tc.group(5) or "").strip()
                first_c = (m_tc.group(2) or m_tc.group(4) or m_tc.group(6) or m_tc.group(7) or "").strip()
            else:
                curr_l_title = ""
                first_c = rest_l.strip()
            if first_c:
                curr_l_lines.append(first_c)
            list_line_indices.add(j)
        elif curr_l_idx is not None:
            if line.startswith(("    ", "\t", "  ", "- ", "* ")) or (not line.strip()):
                if line.strip():
                    curr_l_lines.append(line.strip())
                list_line_indices.add(j)
            else:
                if in_beat_section and not line.startswith("#"):
                    curr_l_lines.append(line.strip())
                    list_line_indices.add(j)
                else:
                    _commit_curr_list_beat()

    _commit_curr_list_beat()

    if list_beats:
        remaining = [lines[idx] for idx in range(len(lines)) if idx not in list_line_indices]
        return list_beats, "\n".join(remaining).strip()

    return [], body


# ---------------------------------------------------------------------------
# 4. 宽容解析器核心类
# ---------------------------------------------------------------------------


class ProgressiveParser:
    """宽容 Markdown 解析与渐进式切片器。

    支持对设定文档与章节蓝图的宽容加载，缺失 Frontmatter 时按路径与命名约定自动推断。
    """

    def __init__(self, book_dir: Path | str | None = None) -> None:
        self.book_dir = Path(book_dir).resolve() if book_dir is not None else None

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """估算文本的 Token 消耗量。"""
        return estimate_tokens(text)

    @staticmethod
    def split_progressive_blocks(markdown: str) -> ParsedBlocks:
        """切分设定文档三级渐进切片并估算 Token。"""
        return split_progressive_blocks(markdown)

    def parse_setting_md(
        self,
        path: Path | str,
        *,
        content: str | None = None,
        book_dir: Path | str | None = None,
    ) -> SettingDoc:
        """解析单份设定文档 Markdown，返回完整 `SettingDoc` 模型。"""
        target_path = Path(path)
        effective_book_dir = (
            Path(book_dir).resolve()
            if book_dir is not None
            else self.book_dir
        )

        if content is None:
            raw_text = target_path.read_text(encoding="utf-8", errors="replace")
        else:
            raw_text = content

        fm_dict, body = split_frontmatter(raw_text)
        blocks = split_progressive_blocks(raw_text)

        category = _infer_category(fm_dict.get("category"), fm_dict.get("id"), target_path)
        entity_id = _normalize_entity_id(fm_dict.get("id"), target_path.stem, category)
        name = _infer_name(fm_dict.get("name"), body, target_path, entity_id, category)

        raw_status = fm_dict.get("status")
        status = EntityStatus.ACTIVE
        if raw_status is not None:
            cand_s = str(raw_status).strip().lower()
            if cand_s in STATUS_MAP:
                status = STATUS_MAP[cand_s]
            else:
                try:
                    status = EntityStatus(cand_s)
                except ValueError:
                    status = EntityStatus.ACTIVE

        raw_aliases = fm_dict.get("aliases") or fm_dict.get("alias")
        if isinstance(raw_aliases, list):
            aliases = [str(x) for x in raw_aliases]
        elif isinstance(raw_aliases, (str, int, float)) and str(raw_aliases).strip():
            aliases = [x.strip() for x in re.split(r"[,，、;；\s]+", str(raw_aliases)) if x.strip()]
        else:
            aliases = []

        raw_tags = fm_dict.get("tags") or fm_dict.get("tag")
        if isinstance(raw_tags, list):
            tags = [str(x) for x in raw_tags]
        elif isinstance(raw_tags, (str, int, float)) and str(raw_tags).strip():
            tags = [x.strip() for x in re.split(r"[,，、;；\s]+", str(raw_tags)) if x.strip()]
        else:
            tags = []

        file_path = _normalize_file_path(target_path, effective_book_dir)

        # 收集自定义 model_extra 字段
        reserved_keys = {
            "id",
            "name",
            "category",
            "aliases",
            "status",
            "tags",
            "file_path",
            "core_text",
            "topics",
            "secret_text",
        }
        extra_fields = {k: v for k, v in fm_dict.items() if k not in reserved_keys}

        return SettingDoc(
            id=entity_id,
            name=name,
            category=category,
            aliases=aliases,
            status=status,
            tags=tags,
            file_path=file_path,
            core_text=blocks.core,
            topics=blocks.topics,
            secret_text=blocks.secret,
            **extra_fields,
        )

    def parse_blueprint_md(
        self,
        path: Path | str,
        *,
        content: str | None = None,
        book_dir: Path | str | None = None,
    ) -> BlueprintDoc:
        """解析单章写作蓝图 Markdown，返回 `BlueprintDoc` 模型。"""
        target_path = Path(path)

        if content is None:
            raw_text = target_path.read_text(encoding="utf-8", errors="replace")
        else:
            raw_text = content

        fm_dict, body = split_frontmatter(raw_text)

        # 1. 章号推断（支持阿拉伯数字与中文汉字数字如 '第十二章'）
        raw_ch = fm_dict.get("chapter")
        if isinstance(raw_ch, int) and not isinstance(raw_ch, bool) and raw_ch >= 1:
            chapter = raw_ch
        elif isinstance(raw_ch, str) and raw_ch.strip().isdigit() and int(raw_ch.strip()) >= 1:
            chapter = int(raw_ch.strip())
        elif isinstance(raw_ch, str) and parse_chinese_num(raw_ch.strip()):
            cn_v = parse_chinese_num(raw_ch.strip())
            chapter = cn_v if cn_v and cn_v >= 1 else 1
        else:
            m = re.search(r"ch_?(\d+)", target_path.stem, re.IGNORECASE)
            if m and int(m.group(1)) >= 1:
                chapter = int(m.group(1))
            else:
                m_zh_digit = re.search(r"第\s*(\d+)\s*[章回节]", target_path.stem)
                if m_zh_digit and int(m_zh_digit.group(1)) >= 1:
                    chapter = int(m_zh_digit.group(1))
                else:
                    m_stem_cn = re.search(
                        r"第\s*([一二三四五六七八九十百千]+)\s*[章回节]",
                        target_path.stem,
                    )
                    if m_stem_cn:
                        cn_s = parse_chinese_num(m_stem_cn.group(1))
                        chapter = cn_s if cn_s and cn_s >= 1 else 1
                    else:
                        m2 = re.search(r"\d+", target_path.stem)
                        if m2 and int(m2.group()) >= 1:
                            chapter = int(m2.group())
                        else:
                            m3 = re.search(
                                r"第\s*(\d+|[一二三四五六七八九十百千]+)\s*[章回节]",
                                body,
                            )
                            if m3:
                                c_val = m3.group(1)
                                if c_val.isdigit() and int(c_val) >= 1:
                                    chapter = int(c_val)
                                else:
                                    cn_ch = parse_chinese_num(c_val)
                                    chapter = cn_ch if (cn_ch and cn_ch >= 1) else 1
                            else:
                                chapter = 1

        # 2. 分卷与单元 ID
        raw_vol = fm_dict.get("volume_id") or fm_dict.get("volume") or fm_dict.get("vol")
        volume_id: str | None = None
        if isinstance(raw_vol, int) and not isinstance(raw_vol, bool) and raw_vol >= 1:
            volume_id = f"vol_{raw_vol:02d}"
        elif isinstance(raw_vol, str):
            cand_v = raw_vol.strip().lower()
            if re.match(VOLUME_ID_PATTERN, cand_v):
                volume_id = cand_v
            elif re.match(r"^vol_(\d)$", cand_v):
                volume_id = f"vol_0{cand_v[4:]}"
            elif cand_v.isdigit() and int(cand_v) >= 1:
                volume_id = f"vol_{int(cand_v):02d}"
            else:
                m_cn_v = re.search(r"第?\s*([一二三四五六七八九十]+)\s*卷", cand_v)
                if m_cn_v:
                    cn_v_num = parse_chinese_num(m_cn_v.group(1))
                    if cn_v_num and cn_v_num >= 1:
                        volume_id = f"vol_{cn_v_num:02d}"
                else:
                    m_v = re.search(r"(\d+)", cand_v)
                    if m_v and int(m_v.group(1)) >= 1:
                        volume_id = f"vol_{int(m_v.group(1)):02d}"

        if volume_id is None:
            for part in target_path.parts:
                if re.match(VOLUME_ID_PATTERN, part):
                    volume_id = part
                    break
                m_cn_part = re.search(r"第?\s*([一二三四五六七八九十]+)\s*卷", part)
                if m_cn_part:
                    cn_p = parse_chinese_num(m_cn_part.group(1))
                    if cn_p and cn_p >= 1:
                        volume_id = f"vol_{cn_p:02d}"
                        break
                m_zh_num_vol = re.search(r"第\s*(\d+)\s*卷", part)
                if m_zh_num_vol and int(m_zh_num_vol.group(1)) >= 1:
                    volume_id = f"vol_{int(m_zh_num_vol.group(1)):02d}"
                    break
                m_vol_part = re.search(r"(?:vol|volume)_?(\d+)", part, re.IGNORECASE)
                if m_vol_part and int(m_vol_part.group(1)) >= 1:
                    volume_id = f"vol_{int(m_vol_part.group(1)):02d}"
                    break

        raw_arc = fm_dict.get("arc_id") or fm_dict.get("arc")
        arc_id: str | None = None
        vol_prefix = volume_id or "vol_01"
        if isinstance(raw_arc, int) and not isinstance(raw_arc, bool) and raw_arc >= 1:
            arc_id = f"{vol_prefix}_arc_{raw_arc:02d}"
        elif isinstance(raw_arc, str):
            cand_a = raw_arc.strip().lower()
            if re.match(ARC_ID_PATTERN, cand_a):
                arc_id = cand_a
            elif re.match(r"^arc_\d{2,}$", cand_a):
                arc_id = f"{vol_prefix}_{cand_a}"
            elif re.match(r"^arc_(\d)$", cand_a):
                arc_id = f"{vol_prefix}_arc_0{cand_a[4:]}"
            elif cand_a.isdigit() and int(cand_a) >= 1:
                arc_id = f"{vol_prefix}_arc_{int(cand_a):02d}"
            else:
                m_cn_a = re.search(r"第?\s*([一二三四五六七八九十]+)\s*(?:单元|剧情段|arc)", cand_a)
                if m_cn_a:
                    cn_a_num = parse_chinese_num(m_cn_a.group(1))
                    if cn_a_num and cn_a_num >= 1:
                        arc_id = f"{vol_prefix}_arc_{cn_a_num:02d}"

        if arc_id is None:
            for part in target_path.parts:
                if re.match(ARC_ID_PATTERN, part):
                    arc_id = part
                    break
                m_zh_arc = re.search(r"第\s*(\d+)\s*卷_第\s*(\d+)\s*单元", part)
                if m_zh_arc and int(m_zh_arc.group(1)) >= 1 and int(m_zh_arc.group(2)) >= 1:
                    arc_id = f"vol_{int(m_zh_arc.group(1)):02d}_arc_{int(m_zh_arc.group(2)):02d}"
                    break

        # 3. 标题与 POV（去除前缀章号及破折号、副标题前缀）
        raw_title = fm_dict.get("title")
        if raw_title:
            title: str | None = str(raw_title).strip()
        else:
            m_title = re.search(
                r"^#\s+(?:第\s*(?:\d+|[一二三四五六七八九十百千]+)\s*[章回节][:：\s\-—]*)?(.+)$",
                body,
                re.MULTILINE,
            )
            if m_title:
                t_cand = m_title.group(1).strip()
                t_cand = re.sub(r"^[:：\-—\s]+", "", t_cand).strip()
                title = t_cand if t_cand else None
            else:
                stem_title = extract_clean_title(target_path.stem, "chapter")
                title = stem_title or None

        raw_pov = fm_dict.get("pov")
        pov: str | None = None
        if raw_pov:
            cand_pov = str(raw_pov).strip()
            # 兼容带有括号的声明，如 "陈屿 (char_chenyu)"
            m_paren = re.search(r"[\(（]([a-zA-Z0-9_]+)[\)）]", cand_pov)
            if m_paren:
                cand_pov = m_paren.group(1).strip()
            if re.match(ENTITY_ID_PATTERN, cand_pov):
                pov = cand_pov
            elif re.match(ENTITY_ID_PATTERN, f"char_{cand_pov}"):
                pov = f"char_{cand_pov}"
            else:
                clean_p = re.sub(r"[^a-z0-9_]+", "_", cand_pov.lower()).strip("_")
                if clean_p:
                    pov = f"char_{clean_p}"
                else:
                    h = hashlib.md5(cand_pov.encode("utf-8")).hexdigest()[:8]
                    pov = f"char_{h}"

        # 4. 出场名单 (cast) 基础收集与辅助函数
        characters: list[str] = []
        locations: list[str] = []
        factions: list[str] = []
        items: list[str] = []

        def _extract_entity_tokens_from_line(text: str, default_prefix: str) -> list[str]:
            res: list[str] = []
            parts = re.split(r"[,，、;；\n]+", text)
            for p in parts:
                p = p.strip()
                if not p:
                    continue
                m_paren = re.search(r"[\(（]([a-zA-Z0-9_]+)[\)）]", p)
                if m_paren:
                    cand = m_paren.group(1).strip()
                    if re.match(ENTITY_ID_PATTERN, cand):
                        res.append(cand)
                        continue
                    cand2 = f"{default_prefix}{cand}"
                    if re.match(ENTITY_ID_PATTERN, cand2):
                        res.append(cand2)
                        continue

                m_ent = re.search(r"\b([a-z][a-z0-9]*_[a-z0-9_]+)\b", p)
                if m_ent:
                    res.append(m_ent.group(1))
                    continue

                sub_tokens = [s.strip() for s in p.split() if s.strip()]
                for sub_p in sub_tokens:
                    m_ent_sub = re.search(r"\b([a-z][a-z0-9]*_[a-z0-9_]+)\b", sub_p)
                    if m_ent_sub:
                        res.append(m_ent_sub.group(1))
                        continue
                    m_ascii = re.search(r"\b([a-z0-9_]+)\b", sub_p)
                    if m_ascii:
                        cand3 = f"{default_prefix}{m_ascii.group(1)}"
                        if re.match(ENTITY_ID_PATTERN, cand3):
                            res.append(cand3)
                            continue
                    clean_sub = re.sub(r"[^\w]+", "", sub_p).strip()
                    if clean_sub:
                        h = hashlib.md5(clean_sub.encode("utf-8")).hexdigest()[:8]
                        res.append(f"{default_prefix}{h}")
            return res

        def _route_entity_tokens(tok_list: list[str], default_target: str) -> None:
            for tok in tok_list:
                if tok.startswith("loc_"):
                    locations.append(tok)
                elif tok.startswith("fac_"):
                    factions.append(tok)
                elif tok.startswith("item_"):
                    items.append(tok)
                elif tok.startswith("char_"):
                    characters.append(tok)
                else:
                    if default_target == "loc":
                        locations.append(tok)
                    elif default_target == "fac":
                        factions.append(tok)
                    elif default_target == "item":
                        items.append(tok)
                    else:
                        characters.append(tok)

        raw_cast = fm_dict.get("cast")
        if isinstance(raw_cast, dict):
            for k_sub, t_tgt in [
                ("characters", "char"),
                ("locations", "loc"),
                ("factions", "fac"),
                ("items", "item"),
            ]:
                v = raw_cast.get(k_sub)
                if isinstance(v, list):
                    for item in v:
                        if item is not None:
                            toks = _extract_entity_tokens_from_line(str(item), f"{t_tgt}_")
                            _route_entity_tokens(toks, t_tgt)
                elif isinstance(v, str) and v.strip():
                    toks = _extract_entity_tokens_from_line(v.strip(), f"{t_tgt}_")
                    _route_entity_tokens(toks, t_tgt)
        elif isinstance(raw_cast, list):
            for itm in raw_cast:
                if itm is not None:
                    toks = _extract_entity_tokens_from_line(str(itm), "char_")
                    _route_entity_tokens(toks, "char")
        elif isinstance(raw_cast, str) and raw_cast.strip():
            toks = _extract_entity_tokens_from_line(raw_cast.strip(), "char_")
            _route_entity_tokens(toks, "char")

        for cat_key, t_tgt in [
            ("characters", "char"),
            ("locations", "loc"),
            ("factions", "fac"),
            ("items", "item"),
        ]:
            val = fm_dict.get(cat_key)
            if isinstance(val, list):
                for v_itm in val:
                    if v_itm is not None:
                        toks = _extract_entity_tokens_from_line(str(v_itm), f"{t_tgt}_")
                        _route_entity_tokens(toks, t_tgt)
            elif isinstance(val, str) and val.strip():
                toks = _extract_entity_tokens_from_line(val.strip(), f"{t_tgt}_")
                _route_entity_tokens(toks, t_tgt)

        # 5. 伏笔引用 (threads)
        raw_threads = fm_dict.get("threads") or fm_dict.get("thread") or []
        threads: list[ThreadRef] = []
        seen_threads: set[str] = set()

        def _add_thread(raw_tid: Any, raw_action: Any) -> None:
            if raw_tid is None:
                return
            cand_th = str(raw_tid).strip()
            m_act = re.search(r"[\(（]([^)）]+)[\)）]", cand_th)
            act = ThreadAction.PROGRESS
            if m_act:
                cand_th = re.sub(r"[\(（][^)）]+[\)）]", "", cand_th).strip()
                act_str = m_act.group(1).strip().lower()
                act = THREAD_ACTION_MAP.get(act_str, ThreadAction.PROGRESS)
            elif raw_action:
                act_str = str(raw_action).strip().lower()
                act = THREAD_ACTION_MAP.get(act_str, ThreadAction.PROGRESS)

            m_paren_tid = re.search(r"\b(thread_[a-z0-9_]+)\b", str(raw_tid))
            if m_paren_tid:
                clean_tid = m_paren_tid.group(1)
            else:
                if not cand_th.startswith("thread_"):
                    clean_cand = re.sub(r"[^a-z0-9_]+", "_", cand_th.lower()).strip("_")
                    if clean_cand:
                        clean_tid = f"thread_{clean_cand}"
                    else:
                        h = hashlib.md5(cand_th.encode("utf-8")).hexdigest()[:8]
                        clean_tid = f"thread_{h}"
                else:
                    clean_tid = cand_th

            if not re.match(THREAD_ID_PATTERN, clean_tid):
                h = hashlib.md5(cand_th.encode("utf-8")).hexdigest()[:8]
                clean_tid = f"thread_{h}"

            if clean_tid not in seen_threads:
                seen_threads.add(clean_tid)
                threads.append(ThreadRef(thread_id=clean_tid, action=act))

        if isinstance(raw_threads, dict):
            for t_k, t_v in raw_threads.items():
                _add_thread(t_k, t_v)
        elif isinstance(raw_threads, list):
            for itm in raw_threads:
                if isinstance(itm, dict):
                    if len(itm) == 1 and "thread_id" not in itm and "id" not in itm:
                        for k, v in itm.items():
                            _add_thread(k, v)
                    else:
                        th_id = itm.get("thread_id") or itm.get("id")
                        _add_thread(th_id, itm.get("action"))
                elif isinstance(itm, str):
                    for part in re.split(r"[,，;；]+", itm):
                        if part.strip():
                            _add_thread(part.strip(), None)
        elif isinstance(raw_threads, str):
            for part in re.split(r"[,，;；\n]+", raw_threads):
                if part.strip():
                    _add_thread(part.strip(), None)

        # 6. 机密解锁与专题引用
        raw_sec = fm_dict.get("unlock_secrets") or fm_dict.get("unlock_secret") or []
        unlock_secrets: list[str] = []
        if isinstance(raw_sec, str) and raw_sec.strip():
            raw_sec = [raw_sec.strip()]
        if isinstance(raw_sec, list):
            for s in raw_sec:
                if s is not None:
                    for tok in _extract_entity_tokens_from_line(str(s), "char_"):
                        if tok not in unlock_secrets:
                            unlock_secrets.append(tok)

        raw_topics = fm_dict.get("topics") or fm_dict.get("topic") or []
        topics: list[TopicRef] = []
        seen_topic_pairs: set[tuple[str, str]] = set()

        def _add_topic(raw_ent: Any, raw_name: Any) -> None:
            if not raw_ent or not raw_name:
                return
            cand_ent = str(raw_ent).strip()
            t_name = str(raw_name).strip()
            if not t_name:
                return
            m_paren = re.search(r"[\(（]([a-zA-Z0-9_]+)[\)）]", cand_ent)
            if m_paren:
                p_cand = m_paren.group(1).strip()
                if re.match(ENTITY_ID_PATTERN, p_cand):
                    cand_ent = p_cand
                elif re.match(ENTITY_ID_PATTERN, f"loc_{p_cand}"):
                    cand_ent = f"loc_{p_cand}"
                elif re.match(ENTITY_ID_PATTERN, f"char_{p_cand}"):
                    cand_ent = f"char_{p_cand}"

            if not re.match(ENTITY_ID_PATTERN, cand_ent):
                if re.match(ENTITY_ID_PATTERN, f"char_{cand_ent}"):
                    cand_ent = f"char_{cand_ent}"
                elif re.match(ENTITY_ID_PATTERN, f"loc_{cand_ent}"):
                    cand_ent = f"loc_{cand_ent}"
                else:
                    clean_ent = re.sub(r"[^a-z0-9_]+", "_", cand_ent.lower()).strip("_")
                    if clean_ent:
                        cand_ent = f"loc_{clean_ent}"
                    else:
                        h = hashlib.md5(cand_ent.encode("utf-8")).hexdigest()[:8]
                        cand_ent = f"loc_{h}"
            pair = (cand_ent, t_name)
            if pair not in seen_topic_pairs:
                seen_topic_pairs.add(pair)
                topics.append(TopicRef(entity_id=cand_ent, topic=t_name))

        if isinstance(raw_topics, dict):
            for ent_k, t_val in raw_topics.items():
                _add_topic(ent_k, t_val)
        elif isinstance(raw_topics, list):
            for itm in raw_topics:
                if isinstance(itm, dict):
                    if len(itm) == 1 and "entity_id" not in itm and "id" not in itm:
                        for k, v in itm.items():
                            _add_topic(k, v)
                    else:
                        ent_id = itm.get("entity_id") or itm.get("id")
                        t_name = itm.get("topic") or itm.get("name")
                        _add_topic(ent_id, t_name)
                elif isinstance(itm, str):
                    for part in re.split(r"[,，;；\n]+", itm):
                        part = part.strip()
                        if ":" in part or "：" in part:
                            sep = "：" if "：" in part else ":"
                            e, t = part.split(sep, 1)
                            _add_topic(e, t)
        elif isinstance(raw_topics, str):
            for part in re.split(r"[,，;；\n]+", raw_topics):
                part = part.strip()
                if ":" in part or "：" in part:
                    sep = "：" if "：" in part else ":"
                    e, t = part.split(sep, 1)
                    _add_topic(e, t)

        # 7. 负向禁止红线 (forbidden)
        raw_forb = (
            fm_dict.get("forbidden")
            or fm_dict.get("banned")
            or fm_dict.get("redlines")
            or fm_dict.get("redline")
            or []
        )
        forbidden: list[str] = []
        if isinstance(raw_forb, list):
            forbidden = [str(x).strip() for x in raw_forb if str(x).strip()]
        elif isinstance(raw_forb, str) and raw_forb.strip():
            forbidden = [x.strip() for x in raw_forb.splitlines() if x.strip()]

        # 8. 宽容解析正文中的元数据字段（出场、地点、伏笔、禁止、机密、专题等）
        body_lines = body.splitlines()
        extracted_indices: set[int] = set()

        j = 0
        while j < len(body_lines):
            stripped = body_lines[j].strip()
            if not stripped:
                j += 1
                continue

            # 8.1 列表型大段标题匹配扫描（## 出场人物 / ## 涉及地点 / ## 涉及伏笔 / ## 禁止事项 等）
            m_sec_head = re.match(r"^#{1,4}\s*(.+?)\s*$", stripped)
            if m_sec_head:
                h_name = m_sec_head.group(1).strip()
                sec_type: str | None = None
                if RE_SEC_CHAR.match(h_name):
                    sec_type = "char"
                elif RE_SEC_LOC.match(h_name):
                    sec_type = "loc"
                elif RE_SEC_FAC.match(h_name):
                    sec_type = "fac"
                elif RE_SEC_ITEM.match(h_name):
                    sec_type = "item"
                elif RE_SEC_THREAD.match(h_name):
                    sec_type = "thread"
                elif RE_SEC_SECRET.match(h_name):
                    sec_type = "secret"
                elif RE_SEC_TOPIC.match(h_name):
                    sec_type = "topic"
                elif RE_SEC_FORBID.match(h_name):
                    sec_type = "forbidden"
                elif re.match(r"^(?:POV|视角|主视角|本章视角)$", h_name, re.I):
                    sec_type = "pov"

                if sec_type:
                    extracted_indices.add(j)
                    k = j + 1
                    while k < len(body_lines):
                        next_l = body_lines[k]
                        if re.match(r"^#{1,4}\s+", next_l.strip()):
                            break
                        extracted_indices.add(k)
                        cleaned_item = re.sub(r"^\s*[-*•\d+.)、\]]\s*", "", next_l).strip()
                        if cleaned_item:
                            if sec_type in ("char", "loc", "fac", "item"):
                                _route_entity_tokens(
                                    _extract_entity_tokens_from_line(cleaned_item, f"{sec_type}_"),
                                    sec_type,
                                )
                            elif sec_type == "thread":
                                for part in re.split(r"[,，;；\n]+", cleaned_item):
                                    if part.strip():
                                        _add_thread(part.strip(), None)
                            elif sec_type == "secret":
                                for tok in _extract_entity_tokens_from_line(cleaned_item, "char_"):
                                    if tok not in unlock_secrets:
                                        unlock_secrets.append(tok)
                            elif sec_type == "topic":
                                for part in re.split(r"[,，;；\n]+", cleaned_item):
                                    if ":" in part or "：" in part:
                                        sep = "：" if "：" in part else ":"
                                        e, t = part.split(sep, 1)
                                        _add_topic(e, t)
                            elif sec_type == "forbidden":
                                if cleaned_item not in forbidden:
                                    forbidden.append(cleaned_item)
                            elif sec_type == "pov" and pov is None:
                                p_tokens = _extract_entity_tokens_from_line(cleaned_item, "char_")
                                if p_tokens:
                                    pov = p_tokens[0]
                        k += 1
                    j = k
                    continue

            # 8.2 单行内联元数据提取
            # 本卷 / 单元推断
            if volume_id is None:
                m_vb = re.match(
                    r"^(?:本卷|分卷|卷)\s*[:：]\s*(vol_\d+|[0-9]+|第?\s*[一二三四五六七八九十]+\s*卷?)",
                    stripped,
                    re.IGNORECASE,
                )
                if m_vb:
                    extracted_indices.add(j)
                    raw_v = m_vb.group(1).strip()
                    if raw_v.isdigit():
                        volume_id = f"vol_{int(raw_v):02d}"
                    elif re.match(VOLUME_ID_PATTERN, raw_v):
                        volume_id = raw_v
                    else:
                        m_cn_v2 = re.search(r"([一二三四五六七八九十]+)", raw_v)
                        if m_cn_v2:
                            c_num = parse_chinese_num(m_cn_v2.group(1))
                            if c_num and c_num >= 1:
                                volume_id = f"vol_{c_num:02d}"

            if arc_id is None:
                m_ab = re.match(
                    r"^(?:剧情单元|单元|剧情段|arc)\s*[:：]\s*(.+)$",
                    stripped,
                    re.IGNORECASE,
                )
                if m_ab:
                    raw_a = m_ab.group(1).strip()
                    vol_pfx = volume_id or "vol_01"
                    if re.match(ARC_ID_PATTERN, raw_a):
                        extracted_indices.add(j)
                        arc_id = raw_a
                    elif re.match(r"^arc_\d{2,}$", raw_a):
                        extracted_indices.add(j)
                        arc_id = f"{vol_pfx}_{raw_a}"
                    elif raw_a.isdigit() and int(raw_a) >= 1:
                        extracted_indices.add(j)
                        arc_id = f"{vol_pfx}_arc_{int(raw_a):02d}"
                    else:
                        m_cn_a2 = re.search(r"([一二三四五六七八九十]+)", raw_a)
                        if m_cn_a2:
                            c_a_num = parse_chinese_num(m_cn_a2.group(1))
                            if c_a_num and c_a_num >= 1:
                                extracted_indices.add(j)
                                arc_id = f"{vol_pfx}_arc_{c_a_num:02d}"

            # POV 提取
            if pov is None:
                m_pov = re.match(r"^(?:POV|视角|本章视角|主视角)\s*[:：]\s*(.+)$", stripped, re.IGNORECASE)
                if m_pov:
                    extracted_indices.add(j)
                    val = m_pov.group(1).strip()
                    p_toks = _extract_entity_tokens_from_line(val, "char_")
                    if p_toks:
                        pov = p_toks[0]

            # 正文出场名单提取
            m_cast_line = re.match(
                r"^(?:出场|登场|涉及)(?:角色|人物|人员|地点|场景|势力|门派|物品|道具|实体)?\s*[:：]\s*(.+)$",
                stripped,
                re.IGNORECASE,
            )
            if m_cast_line:
                extracted_indices.add(j)
                content_val = m_cast_line.group(1).strip()
                if any(w in stripped for w in ("地点", "场景")):
                    _route_entity_tokens(_extract_entity_tokens_from_line(content_val, "loc_"), "loc")
                elif any(w in stripped for w in ("势力", "门派")):
                    _route_entity_tokens(_extract_entity_tokens_from_line(content_val, "fac_"), "fac")
                elif any(w in stripped for w in ("物品", "道具")):
                    _route_entity_tokens(_extract_entity_tokens_from_line(content_val, "item_"), "item")
                else:
                    _route_entity_tokens(_extract_entity_tokens_from_line(content_val, "char_"), "char")

            # 正文伏笔引用
            m_th_line = re.match(
                r"^(?:涉及伏笔|伏笔引用|关联伏笔|伏笔)\s*[:：]\s*(.+)$",
                stripped,
                re.IGNORECASE,
            )
            if m_th_line:
                extracted_indices.add(j)
                for part in re.split(r"[,，;；\n]+", m_th_line.group(1)):
                    if part.strip():
                        _add_thread(part.strip(), None)

            # 正文机密解锁
            m_sec_line = re.match(
                r"^(?:解锁机密|机密解锁|解密实体|解锁绝密)\s*[:：]\s*(.+)$",
                stripped,
                re.IGNORECASE,
            )
            if m_sec_line:
                extracted_indices.add(j)
                for tok in _extract_entity_tokens_from_line(m_sec_line.group(1), "char_"):
                    if tok not in unlock_secrets:
                        unlock_secrets.append(tok)

            # 正文专题引用
            m_top_line = re.match(
                r"^(?:挂载专题|按需专题|涉及专题|专题引用|专题)\s*[:：]\s*(.+)$",
                stripped,
                re.IGNORECASE,
            )
            if m_top_line:
                extracted_indices.add(j)
                for item_str in re.split(r"[,，;；\n]+", m_top_line.group(1)):
                    item_str = item_str.strip()
                    if ":" in item_str or "：" in item_str:
                        sep = "：" if "：" in item_str else ":"
                        ent_cand, top_name = item_str.split(sep, 1)
                        _add_topic(ent_cand, top_name)

            # 正文禁止事项
            m_forb = re.match(r"^(?:禁止事项|创作红线|红线|禁止)\s*[:：]\s*(.+)$", stripped)
            if m_forb:
                extracted_indices.add(j)
                for tok in re.split(r"[,，;；\n]+", m_forb.group(1)):
                    tok = tok.strip()
                    if tok and tok not in forbidden:
                        forbidden.append(tok)

            j += 1

        # 9. 冲突修正：arc_id 与 volume_id 协调一致
        if arc_id is not None:
            arc_vol = arc_id.split("_arc_")[0]
            if volume_id is None or volume_id != arc_vol:
                volume_id = arc_vol

        # 10. 实体 ID 清洗与去重
        def _filter_and_normalize_ids(id_list: list[Any], default_prefix: str) -> list[str]:
            res: list[str] = []
            for x in id_list:
                s = str(x).strip()
                if not s:
                    continue
                m_paren = re.search(r"[\(（]([a-zA-Z0-9_]+)[\)）]", s)
                if m_paren:
                    cand_p = m_paren.group(1).strip()
                    if re.match(ENTITY_ID_PATTERN, cand_p):
                        s = cand_p
                    elif re.match(ENTITY_ID_PATTERN, f"{default_prefix}{cand_p}"):
                        s = f"{default_prefix}{cand_p}"

                if re.match(ENTITY_ID_PATTERN, s):
                    clean = s
                elif re.match(ENTITY_ID_PATTERN, f"{default_prefix}{s}"):
                    clean = f"{default_prefix}{s}"
                else:
                    clean_s = re.sub(r"[^a-z0-9_]+", "_", s.lower()).strip("_")
                    if clean_s:
                        cand_cs = f"{default_prefix}{clean_s}"
                        if re.match(ENTITY_ID_PATTERN, cand_cs):
                            clean = cand_cs
                        else:
                            clean = f"{default_prefix}{clean_s}_1"
                    else:
                        h = hashlib.md5(s.encode("utf-8")).hexdigest()[:8]
                        clean = f"{default_prefix}{h}"
                if clean not in res:
                    res.append(clean)
            return res

        cast = BlueprintCast(
            characters=_filter_and_normalize_ids(characters, "char_"),
            locations=_filter_and_normalize_ids(locations, "loc_"),
            factions=_filter_and_normalize_ids(factions, "fac_"),
            items=_filter_and_normalize_ids(items, "item_"),
        )

        # 11. 节拍 (beats) 与正文说明 (body)
        raw_beats = fm_dict.get("beats") or fm_dict.get("beat")
        beats: list[Beat] = []
        body_without_meta = "\n".join(
            [body_lines[idx] for idx in range(len(body_lines)) if idx not in extracted_indices]
        ).strip()
        remaining_body = body_without_meta

        if isinstance(raw_beats, dict):
            idx_counter = 1
            for b_title, b_content in raw_beats.items():
                s_key = str(b_title).strip()
                if isinstance(b_content, dict):
                    b_raw_t = (
                        b_content.get("title")
                        or b_content.get("name")
                        or b_content.get("beat")
                        or s_key
                    )
                    b_t = str(b_raw_t).strip()
                    b_c = str(
                        b_content.get("content")
                        or b_content.get("desc")
                        or b_content.get("description")
                        or b_content.get("summary")
                        or b_content.get("text")
                        or ""
                    ).strip()
                    beats.append(Beat(index=idx_counter, title=b_t, content=b_c))
                elif s_key.isdigit():
                    s_c = str(b_content).strip()
                    if ":" in s_c or "：" in s_c:
                        sep = "：" if "：" in s_c else ":"
                        t, c = s_c.split(sep, 1)
                        beats.append(Beat(index=int(s_key), title=t.strip(), content=c.strip()))
                    else:
                        beats.append(Beat(index=int(s_key), title="", content=s_c))
                else:
                    beats.append(
                        Beat(
                            index=idx_counter,
                            title=s_key,
                            content=str(b_content).strip(),
                        )
                    )
                idx_counter += 1
        elif isinstance(raw_beats, list) and len(raw_beats) > 0:
            idx_counter = 1
            for itm in raw_beats:
                if isinstance(itm, dict):
                    if len(itm) == 1 and "title" not in itm and "content" not in itm and "index" not in itm:
                        for k, v in itm.items():
                            beats.append(
                                Beat(
                                    index=idx_counter,
                                    title=str(k).strip(),
                                    content=str(v).strip(),
                                )
                            )
                            idx_counter += 1
                    else:
                        b_idx = itm.get("index")
                        if isinstance(b_idx, str) and b_idx.isdigit():
                            b_idx = int(b_idx)
                        elif isinstance(b_idx, str) and b_idx in CHINESE_NUMS:
                            b_idx = CHINESE_NUMS[b_idx]
                        if not isinstance(b_idx, int) or isinstance(b_idx, bool) or b_idx < 1:
                            b_idx = idx_counter
                        b_title = str(itm.get("title") or itm.get("name") or itm.get("beat") or "").strip()
                        b_content = str(
                            itm.get("content")
                            or itm.get("desc")
                            or itm.get("description")
                            or itm.get("summary")
                            or itm.get("text")
                            or ""
                        ).strip()
                        beats.append(Beat(index=b_idx, title=b_title, content=b_content))
                        idx_counter = max(idx_counter, b_idx) + 1
                elif isinstance(itm, str):
                    s_itm = itm.strip()
                    if ":" in s_itm or "：" in s_itm:
                        sep = "：" if "：" in s_itm else ":"
                        t, c = s_itm.split(sep, 1)
                        num_pfx = r"^(?:\d+|[一二三四五六七八九十]+|[①②③④⑤⑥⑦⑧⑨⑩])[.、\s]+"
                        clean_t = re.sub(num_pfx, "", t).strip()
                        beats.append(Beat(index=idx_counter, title=clean_t, content=c.strip()))
                    else:
                        num_pfx = r"^(?:\d+|[一二三四五六七八九十]+|[①②③④⑤⑥⑦⑧⑨⑩])[.、\s]+"
                        clean_t = re.sub(num_pfx, "", s_itm).strip()
                        if len(clean_t) <= 40:
                            beats.append(Beat(index=idx_counter, title=clean_t, content=""))
                        else:
                            beats.append(Beat(index=idx_counter, title="", content=s_itm))
                    idx_counter += 1
        else:
            parsed_beats, cleaned_body = _extract_beats_from_markdown(body_without_meta)
            if parsed_beats:
                beats = parsed_beats
                remaining_body = cleaned_body

        reserved_keys = {
            "chapter",
            "volume_id",
            "volume",
            "vol",
            "arc_id",
            "arc",
            "title",
            "pov",
            "cast",
            "characters",
            "locations",
            "factions",
            "items",
            "threads",
            "thread",
            "unlock_secrets",
            "unlock_secret",
            "topics",
            "topic",
            "forbidden",
            "banned",
            "redlines",
            "redline",
            "beats",
            "beat",
            "body",
        }
        extra_fields = {k: v for k, v in fm_dict.items() if k not in reserved_keys}

        return BlueprintDoc(
            chapter=chapter,
            volume_id=volume_id,
            arc_id=arc_id,
            title=title,
            pov=pov,
            cast=cast,
            threads=threads,
            unlock_secrets=unlock_secrets,
            topics=topics,
            forbidden=forbidden,
            beats=beats,
            body=remaining_body.strip(),
            **extra_fields,
        )


# ---------------------------------------------------------------------------
# 5. 顶层快捷函数
# ---------------------------------------------------------------------------

_default_parser = ProgressiveParser()


_LINT_DIALOGUE_LABEL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:台词样本|对话样本|独白样本|示例对话|示例台词|台词例句|经典台词|代表台词|构思期样本)"
)
_LINT_QUOTED_CN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:“([^”\n]{8,})”|「([^」\n]{8,})」)"
)
_LINT_QUOTED_ASCII_RE: Final[re.Pattern[str]] = re.compile(r'"([^"\n]{12,})"')
_LINT_SPOKEN_PUNCT_RE: Final[re.Pattern[str]] = re.compile(r"[！？…]|……")
_LINT_SPOKEN_PARTICLE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[，、\s])(?:我|你|您|咱们|唔|诶|呀|呢|吧|嘛|啦|哦|哼)|(?:唔|诶|呀|呢|吧|嘛|啦|哦)(?:[，。！？…]|$)"
)
_LINT_SCENE_EXAMPLE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:(?:例如|比如|譬如)[^。\n]{4,160}(?:结果|反被|最后只能|偷偷用|一把按住|笑眯眯|捞上膝头|面红耳赤)|"
    r"(?:初登场建议|建议三拍|名场面构想|名场面|小剧场|互动样本|场景示例|基地趣事|日常趣事|过招样本)|"
    r"(?:曾有一次|有一次(?:她|他|它)?曾?)[^。\n]{6,120})"
)
_LINT_PRESET_ACTION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:固定动作习惯|常见小动作清单|小动作清单|动作清单|必演动作|预设动作|作者指定|标志坐姿|标志动作|惯用亲昵方式|标志性动作习惯|声音与动作习惯)"
)
_LINT_CJK_CHAR_RE: Final[re.Pattern[str]] = re.compile(r"[\u4e00-\u9fff]")
_CORE_OVERWEIGHT_TOKEN_LIMIT: Final[int] = 650


def _is_spoken_dialogue_quote(quote_text: str) -> bool:
    """判定引号内文本是否属于具体角色对白/台词样本（而非专有名词短语）。"""
    if _LINT_SPOKEN_PUNCT_RE.search(quote_text):
        return True
    if "，" in quote_text and _LINT_SPOKEN_PARTICLE_RE.search(quote_text):
        return True
    return False


def lint_model_setting_doc(doc: SettingDoc) -> list[str]:
    """静态检查面向模型的 ``01_设定集/`` 设定卡是否混入作者层具象内容。

    扫描 ``core_text``、``topics`` 以及结构规划师可见的 ``secret_text``（``## [机密]``），
    检出以下 4 类具象污染并返回去重告警列表：
    1. 示例对话 / 台词 / 独白样本泄露；
    2. 具体场景 / 名场面 / 一次性互动桥段举例泄露；
    3. 预设分镜动作 / 打卡清单词汇泄露；
    4. ``[核心]`` 速写估算 Token 超过合理阈值（650 Tokens）。
    """
    warnings: list[str] = []
    sections: list[tuple[str, str]] = [("[核心]", doc.core_text)]
    sections.extend((f"[专题: {t_name}]", t_body) for t_name, t_body in doc.topics.items())
    if doc.secret_text and doc.secret_text.strip():
        sections.append(("[机密]", doc.secret_text))

    for sec_label, sec_text in sections:
        if not sec_text or not sec_text.strip():
            continue

        m_label = _LINT_DIALOGUE_LABEL_RE.search(sec_text)
        if m_label:
            warnings.append(
                f"{sec_label} 包含台词/对话样本标记 `{m_label.group(0)}`，"
                "请将具体台词样本移入 `00_作者设定/`，模型卡仅保留抽象语调特征。"
            )

        for pattern in (_LINT_QUOTED_CN_RE, _LINT_QUOTED_ASCII_RE):
            for match in pattern.finditer(sec_text):
                inner = next((g for g in match.groups() if g is not None), "").strip()
                if pattern is _LINT_QUOTED_ASCII_RE and not _LINT_CJK_CHAR_RE.search(inner):
                    continue
                if _is_spoken_dialogue_quote(inner):
                    excerpt = inner if len(inner) <= 22 else f"{inner[:20]}…"
                    warnings.append(
                        f"{sec_label} 疑似包含具体角色对白“{excerpt}”，"
                        "请将示例台词移入 `00_作者设定/`，避免执笔模型直接照搬。"
                    )
                    break

        m_scene = _LINT_SCENE_EXAMPLE_RE.search(sec_text)
        if m_scene:
            excerpt = m_scene.group(0).strip()
            if len(excerpt) > 28:
                excerpt = f"{excerpt[:26]}…"
            warnings.append(
                f"{sec_label} 疑似包含具体场景/桥段举例（`{excerpt}`），"
                "请移入 `00_作者设定/`，模型卡仅留抽象性格机制与关系边界。"
            )

        m_action = _LINT_PRESET_ACTION_RE.search(sec_text)
        if m_action:
            warnings.append(
                f"{sec_label} 包含预设动作/打卡指示词 `{m_action.group(0)}`，"
                "易诱发模型每章机械套用动作，建议移入 `00_作者设定/` 并改写为抽象气质描述。"
            )

    core_tokens = estimate_tokens(doc.core_text)
    if core_tokens > _CORE_OVERWEIGHT_TOKEN_LIMIT:
        warnings.append(
            f"[核心] 区块估算约 {core_tokens} Tokens（建议 <= {_CORE_OVERWEIGHT_TOKEN_LIMIT}），"
            "建议精简为客观本底与核心机制，次要细节拆入 `[专题]` 或移回 `00_作者设定/`。"
        )

    return list(dict.fromkeys(warnings))


def parse_setting_md(
    path: Path | str,
    *,
    content: str | None = None,
    book_dir: Path | str | None = None,
) -> SettingDoc:
    """快捷调用默认解析器解析设定文档。"""
    return _default_parser.parse_setting_md(path, content=content, book_dir=book_dir)


def parse_blueprint_md(
    path: Path | str,
    *,
    content: str | None = None,
    book_dir: Path | str | None = None,
) -> BlueprintDoc:
    """快捷调用默认解析器解析单章蓝图文档。"""
    return _default_parser.parse_blueprint_md(path, content=content, book_dir=book_dir)


__all__ = [
    "ProgressiveParser",
    "estimate_tokens",
    "lint_model_setting_doc",
    "parse_blueprint_md",
    "parse_setting_md",
    "split_frontmatter",
    "split_progressive_blocks",
]
