"""T03 宽容 Markdown 解析器与渐进式切片器 (ProgressiveParser) 单元测试。"""

from __future__ import annotations

from pathlib import Path

from ddnovel.core import (
    ProgressiveParser,
    estimate_tokens,
    parse_blueprint_md,
    parse_setting_md,
    split_frontmatter,
    split_progressive_blocks,
)
from ddnovel.core.schemas._base import EntityStatus, SettingCategory, ThreadAction
from ddnovel.core.schemas.models import BlueprintDoc, SettingDoc
from ddnovel.core.schemas.results import ParsedBlocks

FIXTURES_BOOKS_DIR = Path(__file__).resolve().parent / "fixtures" / "books"


# ---------------------------------------------------------------------------
# 1. Frontmatter 切分与容错
# ---------------------------------------------------------------------------


def test_split_frontmatter_standard_and_variations():
    # 标准 frontmatter + 正文
    content = "---\nid: char_chenyu\nname: 陈屿\n---\n## [核心]\n核心文本"
    fm, body = split_frontmatter(content)
    assert fm == {"id": "char_chenyu", "name": "陈屿"}
    assert body == "## [核心]\n核心文本"

    # 无 frontmatter
    no_fm = "## [核心]\n纯文本设定"
    fm2, body2 = split_frontmatter(no_fm)
    assert fm2 == {}
    assert body2 == "## [核心]\n纯文本设定"

    # 仅 frontmatter，无正文
    only_fm = "---\nid: item_gujing\n---"
    fm3, body3 = split_frontmatter(only_fm)
    assert fm3 == {"id": "item_gujing"}
    assert body3 == ""

    # 含 UTF-8 BOM
    bom_content = "\ufeff---\nid: char_test\n---\n正文"
    fm4, body4 = split_frontmatter(bom_content)
    assert fm4 == {"id": "char_test"}
    assert body4 == "正文"

    # 损坏的 YAML 语法（不合法的冒号缩进），宽容处理不崩溃
    corrupted_yaml = "---\n[unbalanced bracket:\n  bad yaml: true\n---\n## 正文仍然可读"
    fm5, body5 = split_frontmatter(corrupted_yaml)
    assert fm5 == {}
    assert "## 正文仍然可读" in body5

    # Frontmatter 为非字典（如列表或标量）
    scalar_yaml = "---\n- item1\n- item2\n---\n正文"
    fm6, body6 = split_frontmatter(scalar_yaml)
    assert fm6 == {}
    assert body6 == "正文"


# ---------------------------------------------------------------------------
# 2. Token 估算测试
# ---------------------------------------------------------------------------


def test_estimate_tokens_empty_and_whitespace():
    assert estimate_tokens("") == 0
    assert estimate_tokens("   ") == 0
    assert estimate_tokens("\n\t\r\n") == 0
    assert ProgressiveParser.estimate_tokens("") == 0


def test_estimate_tokens_chinese_and_english_mixtures():
    # 纯中文
    cn_text = "三十二岁，晨星智造创始人兼首席架构师。"
    cn_tokens = estimate_tokens(cn_text)
    assert isinstance(cn_tokens, int)
    assert cn_tokens > 0
    assert 15 <= cn_tokens <= 40

    # 纯英文
    en_text = "Progressive parser designed for high performance and tolerant slicing."
    en_tokens = estimate_tokens(en_text)
    assert isinstance(en_tokens, int)
    assert en_tokens > 0
    assert 8 <= en_tokens <= 25

    # 中英数混排
    mixed_text = (
        "陈屿在白板上推导 sparse matrix 收敛边界，并在 v0.9 镜像封盘前留存 snapshot (耗时 48 小时)。"
    )
    mixed_tokens = estimate_tokens(mixed_text)
    assert isinstance(mixed_tokens, int)
    assert mixed_tokens > 0

    # 单调性：长文本 Token 数必大于短文本
    short_text = "你好世界"
    long_text = "你好世界" * 20
    assert estimate_tokens(long_text) > estimate_tokens(short_text)


# ---------------------------------------------------------------------------
# 3. 渐进式三级切片 (core / topic / secret)
# ---------------------------------------------------------------------------


def test_split_progressive_blocks_three_tiers_and_secret_isolation():
    md = """---
id: char_chenyu
name: "陈屿"
---

## [核心]
三十二岁，晨星智造首席架构师。技术上极度克制较真。

## [专题: 内核重构习惯]
每逢攻坚夜，用红蓝双色马克笔推导稀疏矩阵。

## [专题: 咖啡偏好]
只喝不加糖的浓美式。

## [机密]
已抵押唯一房产垫付团队上季度的服务器租金，瞒着全体员工。
"""
    blocks = split_progressive_blocks(md)
    assert isinstance(blocks, ParsedBlocks)

    # 1. 核心区块正确提取
    assert "三十二岁，晨星智造首席架构师" in blocks.core
    assert "稀疏矩阵" not in blocks.core
    assert "抵押唯一房产" not in blocks.core

    # 2. 专题区块正确提取为 dict
    assert len(blocks.topics) == 2
    assert "内核重构习惯" in blocks.topics
    assert "咖啡偏好" in blocks.topics
    assert "红蓝双色马克笔" in blocks.topics["内核重构习惯"]
    assert "浓美式" in blocks.topics["咖啡偏好"]

    # 3. 机密区块严格物理隔离
    assert blocks.secret is not None
    assert "抵押唯一房产垫付团队" in blocks.secret
    assert "抵押唯一房产" not in blocks.core
    for topic_content in blocks.topics.values():
        assert "抵押唯一房产" not in topic_content

    # 4. Token 估算字典生成
    assert "core" in blocks.token_estimates
    assert "topic:内核重构习惯" in blocks.token_estimates
    assert "topic:咖啡偏好" in blocks.token_estimates
    assert "secret" in blocks.token_estimates
    assert blocks.token_estimates["core"] > 0
    assert blocks.token_estimates["secret"] > 0


def test_split_progressive_blocks_chinese_brackets_and_variants():
    md = """
## 【核心】
青云门外门弟子林凡。

## 【专题：剑道感悟】
领悟秋水剑意第一重。

## 【机密】
身怀上古北冥古镜钥匙碎片。
"""
    blocks = ProgressiveParser.split_progressive_blocks(md)
    assert "林凡" in blocks.core
    assert "剑道感悟" in blocks.topics
    assert "秋水剑意" in blocks.topics["剑道感悟"]
    assert blocks.secret is not None
    assert "北冥古镜" in blocks.secret


def test_split_progressive_blocks_freeform_without_headers():
    freeform_text = "纯自由手写人设。这名角色是黑客，平时独来独往，擅长逆向工程。"
    blocks = split_progressive_blocks(freeform_text)
    assert blocks.core == freeform_text
    assert blocks.topics == {}
    assert blocks.secret is None
    assert blocks.token_estimates["core"] > 0
    assert "secret" not in blocks.token_estimates


def test_split_progressive_blocks_without_secret():
    md = """
## [核心]
常规设定，无任何机密。

## [专题: 历史]
很久以前的故事。
"""
    blocks = split_progressive_blocks(md)
    assert "常规设定" in blocks.core
    assert "历史" in blocks.topics
    assert blocks.secret is None
    assert "secret" not in blocks.token_estimates


# ---------------------------------------------------------------------------
# 4. SettingDoc 宽容解析测试 (parse_setting_md)
# ---------------------------------------------------------------------------


def test_parse_setting_md_from_fixture():
    fixture_path = (
        FIXTURES_BOOKS_DIR / "general" / "01_settings" / "characters" / "char_chenyu.md"
    )
    assert fixture_path.is_file()

    parser = ProgressiveParser(book_dir=FIXTURES_BOOKS_DIR / "general")
    doc = parser.parse_setting_md(fixture_path)

    assert isinstance(doc, SettingDoc)
    assert doc.id == "char_chenyu"
    assert doc.name == "陈屿"
    assert doc.category == SettingCategory.CHARACTER
    assert doc.status == EntityStatus.ACTIVE
    assert "老陈" in doc.aliases
    assert "陈工" in doc.aliases
    assert "主角" in doc.tags
    assert doc.file_path == "01_settings/characters/char_chenyu.md"
    assert "三十二岁" in doc.core_text
    assert "内核重构习惯" in doc.topics
    assert doc.secret_text is not None and "经营贷抵押" in doc.secret_text


def test_parse_setting_md_without_frontmatter_inferred():
    raw_md = """# 林凡
青云门外门弟子，性格沉稳内敛。

## [专题: 佩剑]
手持玄铁重剑。

## [机密]
真实身份为上古剑宗唯一遗孤。
"""
    # 模拟路径在 `01_settings/characters/char_linfan.md`
    fake_path = Path("01_settings/characters/char_linfan.md")
    doc = parse_setting_md(fake_path, content=raw_md)

    assert isinstance(doc, SettingDoc)
    assert doc.id == "char_linfan"
    assert doc.name == "林凡"
    assert doc.category == SettingCategory.CHARACTER
    assert doc.status == EntityStatus.ACTIVE
    assert "沉稳内敛" in doc.core_text
    assert "佩剑" in doc.topics
    assert doc.secret_text == "真实身份为上古剑宗唯一遗孤。"


def test_parse_setting_md_informal_chinese_filename_and_invalid_status():
    raw_md = """---
status: alive_and_kicking  # 非法状态枚举，应优雅回退为 active
aliases: "小黑, 极客"     # 字符串别名，自动切分为列表
tags: "黑客, 配角"        # 字符串标签，自动切分为列表
custom_score: 99           # 未知字段保留至 model_extra
---
## [核心]
无名黑客的速写描述。
"""
    fake_path = Path("01_settings/characters/黑客阿飞.md")
    doc = parse_setting_md(fake_path, content=raw_md)

    assert isinstance(doc, SettingDoc)
    assert doc.id.startswith("char_")
    assert doc.name == "黑客阿飞"
    assert doc.category == SettingCategory.CHARACTER
    assert doc.status == EntityStatus.ACTIVE
    assert "小黑" in doc.aliases and "极客" in doc.aliases
    assert "黑客" in doc.tags and "配角" in doc.tags
    assert doc.model_extra.get("custom_score") == 99


# ---------------------------------------------------------------------------
# 5. BlueprintDoc 宽容解析测试 (parse_blueprint_md)
# ---------------------------------------------------------------------------


def test_parse_blueprint_md_from_fixture():
    fixture_path = (
        FIXTURES_BOOKS_DIR
        / "general"
        / "02_structure"
        / "blueprints"
        / "vol_01"
        / "ch_0013.md"
    )
    assert fixture_path.is_file()

    parser = ProgressiveParser(book_dir=FIXTURES_BOOKS_DIR / "general")
    bp = parser.parse_blueprint_md(fixture_path)

    assert isinstance(bp, BlueprintDoc)
    assert bp.chapter == 13
    assert bp.volume_id == "vol_01"
    assert bp.arc_id == "vol_01_arc_01"
    assert bp.title == "封闭尽调前的四十八小时"
    assert bp.pov == "char_chenyu"
    assert "char_chenyu" in bp.cast.characters
    assert "char_tangzhi" in bp.cast.characters
    assert "loc_binhai_kejichang" in bp.cast.locations
    assert "fac_chenxing_tech" in bp.cast.factions
    assert "item_xinghe_prototype" in bp.cast.items
    assert len(bp.threads) == 2
    assert bp.threads[0].thread_id == "thread_solver_benchmark"
    assert bp.threads[0].action == ThreadAction.PROGRESS
    assert len(bp.beats) == 3
    assert bp.beats[0].index == 1
    assert bp.beats[0].title == "晨会桌上的补充备忘录"
    assert "本章创作提示" in bp.body


def test_parse_blueprint_md_freeform_with_body_beats():
    raw_md = """# 第14章 盲测算例突袭
POV: char_chenyu
出场角色: char_chenyu, char_tangzhi, loc_binhai_kejichang

### 节拍 1: 盲测设备进驻
东海重工技术代表提前两小时抵达园区实验室。

### 节拍 2: 算例突袭
资方代表临时更换极端工况算例，试图突破收敛边界。

## 创作注意
重点描写机房散热风扇与紧张气氛。
"""
    fake_path = Path("02_structure/blueprints/vol_01/ch_0014.md")
    bp = parse_blueprint_md(fake_path, content=raw_md)

    assert isinstance(bp, BlueprintDoc)
    assert bp.chapter == 14
    assert bp.volume_id == "vol_01"
    assert bp.title == "盲测算例突袭"
    assert bp.pov == "char_chenyu"
    assert "char_chenyu" in bp.cast.characters
    assert "char_tangzhi" in bp.cast.characters
    assert "loc_binhai_kejichang" in bp.cast.locations
    assert len(bp.beats) == 2
    assert bp.beats[0].index == 1
    assert bp.beats[0].title == "盲测设备进驻"
    assert "东海重工技术代表" in bp.beats[0].content
    assert bp.beats[1].index == 2
    assert bp.beats[1].title == "算例突袭"
    assert "创作注意" in bp.body


def test_parse_blueprint_md_duplicate_threads_and_arc_mismatch_resilience():
    # 模拟作者笔误：threads 中存在重复 ID，且 volume_id 与 arc_id 冲突
    raw_md = """---
chapter: 15
volume_id: vol_01
arc_id: vol_02_arc_01  # 与 vol_01 冲突，应容错修正
threads:
  - thread_id: thread_solver_benchmark
    action: progress
  - thread_id: thread_solver_benchmark
    action: resolve
beats:
  - index: 1
    title: 澄清
    content: 事实澄清。
---
正文备忘
"""
    bp = parse_blueprint_md("02_structure/blueprints/vol_02/ch_0015.md", content=raw_md)
    assert isinstance(bp, BlueprintDoc)
    assert bp.chapter == 15
    # 冲突被优雅修复，避免 BlueprintDoc 校验崩溃
    assert bp.volume_id == "vol_02"
    assert bp.arc_id == "vol_02_arc_01"
    # 重复 thread_id 去重保留第一项
    assert len(bp.threads) == 1
    assert bp.threads[0].thread_id == "thread_solver_benchmark"
    assert bp.threads[0].action == ThreadAction.PROGRESS


# ---------------------------------------------------------------------------
# 6. 全量样例库真源文件遍历解析测试
# ---------------------------------------------------------------------------


def test_parse_all_setting_fixtures_across_all_genres():
    """验证对三大题材书库中全部设定文档的真源解析均无异常。"""
    setting_files = sorted(FIXTURES_BOOKS_DIR.glob("*/01_settings/**/*.md"))
    assert len(setting_files) >= 24

    parser = ProgressiveParser()
    for s_file in setting_files:
        book_dir = s_file.parents[2]
        doc = parser.parse_setting_md(s_file, book_dir=book_dir)
        assert isinstance(doc, SettingDoc)
        assert doc.id.startswith(("char_", "loc_", "fac_", "world_", "rule_", "item_", "other_"))
        assert len(doc.name) >= 1
        assert doc.category in SettingCategory
        assert doc.status in (EntityStatus.ACTIVE, EntityStatus.RETIRED, EntityStatus.DECEASED)
        assert doc.file_path.startswith("01_settings/")


def test_parse_all_blueprint_fixtures_across_all_genres():
    """验证对三大题材书库中全部蓝图文档的真源解析均无异常。"""
    bp_files = sorted(FIXTURES_BOOKS_DIR.glob("*/02_structure/blueprints/**/*.md"))
    assert len(bp_files) >= 3

    parser = ProgressiveParser()
    for bp_file in bp_files:
        book_dir = bp_file.parents[3]
        bp = parser.parse_blueprint_md(bp_file, book_dir=book_dir)
        assert isinstance(bp, BlueprintDoc)
        assert bp.chapter >= 1
        assert bp.volume_id is not None
        assert len(bp.beats) >= 1
        for b in bp.beats:
            assert b.index >= 1


def test_token_estimation_edge_cases():
    """测试极端文本与特殊符号的 Token 估算稳定性。"""
    cases = [
        "1234567890",
        "!@#$%^&*()_+{}[]:\"<>?,./;'",
        "😀🚀🎉📚🔥",
        "```python\ndef foo():\n    return 42\n```",
        "A" * 1000,
        "字" * 1000,
    ]
    for text in cases:
        tokens = estimate_tokens(text)
        assert isinstance(tokens, int)
        assert tokens > 0


# ---------------------------------------------------------------------------
# 7. 严苛边界与对抗性测试 (Adversarial Edge Cases)
# ---------------------------------------------------------------------------


def test_secret_isolation_with_subtitles_and_separators():
    """验证包含副标题或分隔符的机密段落绝不泄露到 core 或 topics。"""
    md = """## [核心]
普通少年林凡。

## [机密: 真实身份]
上古神明转世，携带逆天至宝北冥古镜。

## 【机密：隐藏修为】
实为元婴后期大圆满。

## [绝密 - 终极底牌]
燃烧本源可唤醒诛仙剑灵。
"""
    blocks = split_progressive_blocks(md)
    assert "林凡" in blocks.core
    assert "上古神明" not in blocks.core
    assert "元婴后期" not in blocks.core
    assert "诛仙剑灵" not in blocks.core
    assert blocks.secret is not None
    assert "上古神明转世" in blocks.secret
    assert "元婴后期" in blocks.secret
    assert "诛仙剑灵" in blocks.secret
    assert blocks.topics == {}


def test_setting_body_starting_with_horizontal_rule_not_erased():
    """验证正文起始处包含 Markdown 分割线 (---) 时不会被误判并抹除正文。"""
    md = """---
id: char_test
name: 测试角色
---

---
## [核心]
第一段核心内容。

## [机密]
绝密底牌。
---
"""
    doc = parse_setting_md("01_settings/characters/char_test.md", content=md)
    assert "第一段核心内容" in doc.core_text
    assert doc.secret_text is not None and "绝密底牌" in doc.secret_text


def test_infer_name_ignores_structural_headings():
    """验证实体名称推断不会错误提取 '# [核心设定]' 或 '# 核心' 作为名称。"""
    md = """# [核心设定]
青云门外门弟子林凡。
"""
    doc = parse_setting_md("01_settings/characters/char_linfan.md", content=md)
    assert doc.name == "linfan"
    assert doc.id == "char_linfan"
    assert "青云门外门弟子林凡" in doc.core_text


def test_blueprint_headings_with_digits_not_falsely_matched_as_beats():
    """验证包含数字的常规标题（如 '## 2024年度大事件' 或 '## 3 大注意点'）不会被误解析为节拍。"""
    md = """# 第10章 总结
POV: char_chenyu

## 2024年度大事件
这是年度回顾正文。

## 3 大研发禁忌
研发注意点说明。

### 节拍 1: 晨会
清晨七点。
"""
    bp = parse_blueprint_md("ch_0010.md", content=md)
    assert len(bp.beats) == 1
    assert bp.beats[0].index == 1
    assert bp.beats[0].title == "晨会"
    assert "2024年度大事件" in bp.body
    assert "3 大研发禁忌" in bp.body


def test_blueprint_beats_multiline_and_indented_sub_items():
    """验证列表节拍下的多行缩进内容及子列表被完整捕获进 content，且正文不残留孤儿行。"""
    md = """## 节拍
1. **晨会桌上的备忘录**:
   清晨七点，唐栀逐条核对资方条款。
   - 对方要求开放底层库。
   - 陈屿明确予以拒绝。
2. **物理封盘**:
   完成离线工程盘封存。

## 创作注意
注意描写晨光与机房风扇声。
"""
    bp = parse_blueprint_md("ch_0011.md", content=md)
    assert len(bp.beats) == 2
    assert bp.beats[0].index == 1
    assert bp.beats[0].title == "晨会桌上的备忘录"
    assert "清晨七点" in bp.beats[0].content
    assert "陈屿明确予以拒绝" in bp.beats[0].content
    assert bp.beats[1].index == 2
    assert bp.beats[1].title == "物理封盘"
    assert "完成离线工程盘封存" in bp.beats[1].content
    # 正文不应包含已提取节拍的孤儿行
    assert "清晨七点" not in bp.body
    assert "底层库" not in bp.body
    assert "创作注意" in bp.body


def test_blueprint_beats_standalone_list_without_header():
    """验证无 '## 节拍' 标题时直接书写的有序编号列表亦能正确解析为节拍。"""
    md = """# 第12章 突袭
POV: char_chenyu

1. **盲测设备进驻**: 东海重工提前抵达实验室。
2. **算例突袭**: 临时更换极端工况算例。
"""
    bp = parse_blueprint_md("ch_0012.md", content=md)
    assert len(bp.beats) == 2
    assert bp.beats[0].title == "盲测设备进驻"
    assert "东海重工" in bp.beats[0].content
    assert bp.beats[1].title == "算例突袭"
    assert "极端工况" in bp.beats[1].content


def test_topic_headings_without_colons_and_chinese_brackets():
    """验证无冒号的专题标题（如 '## [专题] 剑道' 或 '## 【专题】剑道'）能正确提取为专题切片。"""
    md = """## [核心]
林凡基本人设。

## [专题] 剑道感悟
领悟秋水剑意。

## 【专题】身法步法
踏雪无痕。

## [Topic] Alchemy
Refining spirit pill.
"""
    blocks = split_progressive_blocks(md)
    assert len(blocks.topics) == 3
    assert "剑道感悟" in blocks.topics
    assert "身法步法" in blocks.topics
    assert "Alchemy" in blocks.topics
    assert "秋水剑意" in blocks.topics["剑道感悟"]
    assert "踏雪无痕" in blocks.topics["身法步法"]
    assert "Refining spirit pill" in blocks.topics["Alchemy"]
    assert "秋水剑意" not in blocks.core


def test_freeform_blueprint_complete_metadata_extraction():
    """验证纯手写无 Frontmatter 蓝图正文中各类元数据（出场、地点、伏笔、红线、机密、专题）的全面解析。"""
    md = """# 第13章 封闭尽调前的四十八小时
本卷: vol_01
单元: vol_01_arc_01
POV: char_chenyu
出场人物: char_chenyu, char_tangzhi
涉及地点: loc_binhai_kejichang
涉及伏笔: thread_solver_benchmark (推进), thread_audit (埋设)
解锁机密: char_chenyu
按需专题: loc_binhai_kejichang: C座通宵实验室

## 禁止事项
- 严禁让唐栀提前知晓陈屿已抵押房产
- 严禁出现脱离常识的代码桥段

## 分场节拍
### 节拍一: 晨会
清晨七点讨论对赌条款。
### 节拍二: 封盘
完成星河v0.9离线封盘。

## 本章创作提示
节奏紧凑干练。
"""
    bp = parse_blueprint_md("ch_0013.md", content=md)
    assert bp.chapter == 13
    assert bp.volume_id == "vol_01"
    assert bp.arc_id == "vol_01_arc_01"
    assert bp.title == "封闭尽调前的四十八小时"
    assert bp.pov == "char_chenyu"
    assert "char_chenyu" in bp.cast.characters
    assert "char_tangzhi" in bp.cast.characters
    assert "loc_binhai_kejichang" in bp.cast.locations
    assert len(bp.threads) == 2
    assert bp.threads[0].thread_id == "thread_solver_benchmark"
    assert bp.threads[0].action == ThreadAction.PROGRESS
    assert bp.threads[1].thread_id == "thread_audit"
    assert bp.threads[1].action == ThreadAction.PLANT
    assert "char_chenyu" in bp.unlock_secrets
    assert len(bp.topics) == 1
    assert bp.topics[0].entity_id == "loc_binhai_kejichang"
    assert bp.topics[0].topic == "C座通宵实验室"
    assert len(bp.forbidden) == 2
    assert "抵押房产" in bp.forbidden[0]
    assert len(bp.beats) == 2
    assert bp.beats[0].index == 1
    assert bp.beats[0].title == "晨会"
    assert bp.beats[1].index == 2
    assert bp.beats[1].title == "封盘"
    assert "本章创作提示" in bp.body
    # 结构性元数据行不应残留在 body
    assert "涉及地点" not in bp.body
    assert "出场人物" not in bp.body


def test_setting_chinese_punctuation_aliases_and_tags():
    """验证设定 frontmatter 中使用顿号、全半角分号与逗号混合的别名与标签能正确切分。"""
    md = """---
aliases: "老陈、陈工; 架构师，屿哥"
tags: "修真、剑道；主角, 逆袭"
---
## [核心]
测试。
"""
    doc = parse_setting_md("01_settings/characters/char_chenyu.md", content=md)
    assert "老陈" in doc.aliases
    assert "陈工" in doc.aliases
    assert "架构师" in doc.aliases
    assert "屿哥" in doc.aliases
    assert "修真" in doc.tags
    assert "剑道" in doc.tags
    assert "主角" in doc.tags
    assert "逆袭" in doc.tags


# ---------------------------------------------------------------------------
# 8. R2 深度对抗性边界压力测试 (Adversarial Round 2 Tests)
# ---------------------------------------------------------------------------


def test_adversarial_headings_with_subtitles_and_modifiers():
    """验证各种修饰词、冒号、副标题及括号外副标题的切片标题均被准确捕获，绝不泄露到 core。"""
    md = """## [核心设定: 基础人设]
普通少年林凡，为人低调隐忍。

## [核心] 进阶特性
遇险时轻叩剑柄三次。

## [专题设定: 武器]
手持玄铁重剑。

## 【专题设定：佩剑】
剑身铭刻青云古印。

## 专题设定: 身法
踏雪无痕。

## [机密设定: 真实身份]
上古神明转世，体内藏有神格碎片。

## 【机密设定：隐藏修为】
元婴后期大圆满。

## [机密] 真实身世
北冥仙宗宗主独子。

## 【机密】前世因果
曾一剑斩碎幽冥忘川。

## [机密档案]
仙宗太上长老秘密密令。

## [绝密资料: 底牌]
本源真火可焚尽天劫。
"""
    blocks = split_progressive_blocks(md)
    # 核心区块
    assert "林凡" in blocks.core
    assert "轻叩剑柄三次" in blocks.core
    assert "神明转世" not in blocks.core
    assert "元婴后期" not in blocks.core
    assert "独子" not in blocks.core
    assert "本源真火" not in blocks.core

    # 专题区块
    assert "武器" in blocks.topics
    assert "佩剑" in blocks.topics
    assert "身法" in blocks.topics
    assert "玄铁重剑" in blocks.topics["武器"]
    assert "青云古印" in blocks.topics["佩剑"]
    assert "踏雪无痕" in blocks.topics["身法"]

    # 机密区块物理隔离
    assert blocks.secret is not None
    assert "上古神明转世" in blocks.secret
    assert "元婴后期大圆满" in blocks.secret
    assert "北冥仙宗宗主独子" in blocks.secret
    assert "斩碎幽冥忘川" in blocks.secret
    assert "仙宗太上长老" in blocks.secret
    assert "本源真火" in blocks.secret


def test_adversarial_code_fence_heading_isolation():
    """验证处于代码块内部的切片标题行绝不会破坏外部区块状态，更不会使代码内容误泄露至 secret。"""
    md = """## [核心]
这里是核心配置说明：

```markdown
## [机密]
这是示例代码里的假机密说明
## [专题: 假专题]
这也是示例代码
```

核心正文第二段，依然属于核心设定。

## [机密]
这是真正的机密底牌。
"""
    blocks = split_progressive_blocks(md)
    assert "这里是核心配置说明" in blocks.core
    assert "这是示例代码里的假机密说明" in blocks.core
    assert "核心正文第二段" in blocks.core
    assert blocks.secret is not None
    assert "这是真正的机密底牌" in blocks.secret
    assert "假机密说明" not in blocks.secret
    assert "假专题" not in blocks.topics


def test_adversarial_blueprint_frontmatter_mappings_and_dict_formats():
    """验证蓝图 Frontmatter 中使用单键字典列表及直接字典格式的 threads、topics、beats 均能宽容加载。"""
    md = """---
chapter: 1
threads:
  - thread_solver_benchmark: progress
  - thread_audit: plant
topics:
  - loc_binhai_kejichang: C座通宵实验室
  - item_xinghe_prototype: 核心求解器
beats:
  - 晨会: 清晨七点讨论对赌条款。
  - 封盘: 完成星河v0.9离线封盘。
---
本章提示
"""
    bp = parse_blueprint_md("ch_0001.md", content=md)
    assert len(bp.threads) == 2
    assert bp.threads[0].thread_id == "thread_solver_benchmark"
    assert bp.threads[0].action == ThreadAction.PROGRESS
    assert bp.threads[1].thread_id == "thread_audit"
    assert bp.threads[1].action == ThreadAction.PLANT

    assert len(bp.topics) == 2
    assert bp.topics[0].entity_id == "loc_binhai_kejichang"
    assert bp.topics[0].topic == "C座通宵实验室"
    assert bp.topics[1].entity_id == "item_xinghe_prototype"
    assert bp.topics[1].topic == "核心求解器"

    assert len(bp.beats) == 2
    assert bp.beats[0].index == 1
    assert bp.beats[0].title == "晨会"
    assert "清晨七点" in bp.beats[0].content
    assert bp.beats[1].index == 2
    assert bp.beats[1].title == "封盘"
    assert "离线封盘" in bp.beats[1].content


def test_adversarial_blueprint_cast_parenthesized_ids_and_cross_prefixes():
    """验证正文或 Frontmatter 中包含括号名称（如 '陈屿 (char_chenyu)'）与跨前缀实体能被准确分类。"""
    md = """# 第15章 突围
POV: 陈屿 (char_chenyu)
出场人物: 陈屿 (char_chenyu), 唐栀 (char_tangzhi), 滨海科技厂 (loc_binhai_kejichang)
涉及势力: 远创资本 (fac_yuanchuang_cap)
涉及物品: 星河原型盘 (item_xinghe_prototype)

### 节拍 1: 突围
测试正文。
"""
    bp = parse_blueprint_md("ch_0015.md", content=md)
    assert bp.pov == "char_chenyu"
    assert "char_chenyu" in bp.cast.characters
    assert "char_tangzhi" in bp.cast.characters
    assert "loc_binhai_kejichang" in bp.cast.locations
    assert "fac_yuanchuang_cap" in bp.cast.factions
    assert "item_xinghe_prototype" in bp.cast.items


def test_adversarial_blueprint_body_multiline_list_sections():
    """验证正文中以 Markdown 大段列表书写的各类元数据均被结构化捕获并从 body 中剥离。"""
    md = """# 第16章 终局
POV: char_chenyu

## 出场人物
- 陈屿 (char_chenyu)
- 唐栀 (char_tangzhi)

## 涉及地点
- 滨海科技厂 (loc_binhai_kejichang)

## 涉及伏笔
- thread_solver_benchmark (推进)
- thread_audit (回收)

## 解锁机密
- char_chenyu

## 按需专题
- loc_binhai_kejichang: C座通宵实验室

## 禁止事项
- 严禁剧透陈屿抵押房产
- 严禁出现荒谬代码

## 分场节拍
### 节拍 1: 晨会
清晨七点。
### 节拍 2: 封盘
离线封盘。

## 创作注意
注意机房风扇声。
"""
    bp = parse_blueprint_md("ch_0016.md", content=md)
    assert "char_chenyu" in bp.cast.characters
    assert "char_tangzhi" in bp.cast.characters
    assert "loc_binhai_kejichang" in bp.cast.locations
    assert len(bp.threads) == 2
    assert bp.threads[0].thread_id == "thread_solver_benchmark"
    assert bp.threads[1].thread_id == "thread_audit"
    assert bp.threads[1].action == ThreadAction.RESOLVE
    assert "char_chenyu" in bp.unlock_secrets
    assert len(bp.topics) == 1
    assert bp.topics[0].entity_id == "loc_binhai_kejichang"
    assert len(bp.forbidden) == 2
    assert len(bp.beats) == 2
    assert bp.beats[0].title == "晨会"
    assert bp.beats[1].title == "封盘"

    # 验证元数据与节拍标题均已从 body 剥离，不残留孤儿标题
    assert "## 出场人物" not in bp.body
    assert "## 涉及地点" not in bp.body
    assert "## 涉及伏笔" not in bp.body
    assert "## 分场节拍" not in bp.body
    assert "## 创作注意" in bp.body


def test_adversarial_blueprint_list_beats_chinese_circled_and_roman():
    """验证列表节拍全面支持汉字数字（一、）、带圈数字（①）、括号数字与罗马数字。"""
    # 汉字数字节拍列表
    md_cn = """## 节拍
一、晨会: 清晨七点讨论条款。
二、封盘: 完成离线封盘。
三、突围: 東海重工提前发函。
"""
    bp_cn = parse_blueprint_md("ch_0017.md", content=md_cn)
    assert len(bp_cn.beats) == 3
    assert bp_cn.beats[0].index == 1
    assert bp_cn.beats[0].title == "晨会"
    assert bp_cn.beats[1].index == 2
    assert bp_cn.beats[1].title == "封盘"
    assert bp_cn.beats[2].index == 3
    assert bp_cn.beats[2].title == "突围"

    # 带圈数字节拍列表
    md_circled = """## 节拍清单
① 晨会: 清晨七点。
② 封盘: 离线封盘。
"""
    bp_c = parse_blueprint_md("ch_0018.md", content=md_circled)
    assert len(bp_c.beats) == 2
    assert bp_c.beats[0].index == 1
    assert bp_c.beats[0].title == "晨会"
    assert bp_c.beats[1].index == 2
    assert bp_c.beats[1].title == "封盘"

    # 括号数字与罗马数字
    md_roman = """## 节拍
(1) 晨会: 清晨七点。
(2) 封盘: 离线封盘。
"""
    bp_r = parse_blueprint_md("ch_0019.md", content=md_roman)
    assert len(bp_r.beats) == 2
    assert bp_r.beats[0].index == 1
    assert bp_r.beats[1].index == 2


def test_adversarial_setting_chinese_status_and_categories():
    """验证设定状态与分类支持中文声明（如 '阵亡' 映射 DECEASED，'退隐' 映射 RETIRED）。"""
    # 阵亡角色
    md_dead = """---
status: 阵亡
category: 角色
---
## [核心]
前代掌门，已在落霞峰一战中身陨。
"""
    doc_dead = parse_setting_md("01_settings/人物/char_master.md", content=md_dead)
    assert doc_dead.status == EntityStatus.DECEASED
    assert doc_dead.category == SettingCategory.CHARACTER

    # 退隐角色
    md_retired = """---
status: 退隐
category: 人物
---
## [核心]
隐居落霞峰后山。
"""
    doc_retired = parse_setting_md("01_settings/characters/char_elder.md", content=md_retired)
    assert doc_retired.status == EntityStatus.RETIRED
    assert doc_retired.category == SettingCategory.CHARACTER

    # 中文地理目录与分类
    md_geo = """---
category: 地理
---
## [核心]
北境孤城。
"""
    doc_geo = parse_setting_md("01_settings/地理/loc_gucheng.md", content=md_geo)
    assert doc_geo.category == SettingCategory.GEOGRAPHY


def test_adversarial_blueprint_chinese_chapter_numbers_and_title_dashes():
    """验证蓝图章号推断支持汉字章号（第十二章）且标题提取不会被多余破折号或回数污染。"""
    md1 = """# 第十二章 - 算例突袭
POV: char_chenyu

## 节拍
1. 晨会: 内容。
"""
    bp1 = parse_blueprint_md("blueprint_without_digits.md", content=md1)
    assert bp1.chapter == 12
    assert bp1.title == "算例突袭"

    md2 = """# 第142回：暗星鬼市与残镜微鸣
POV: char_linfan

## 节拍
1. 晨会: 内容。
"""
    bp2 = parse_blueprint_md("blueprint_no_digits.md", content=md2)
    assert bp2.chapter == 142
    assert bp2.title == "暗星鬼市与残镜微鸣"


# ---------------------------------------------------------------------------
# 9. R3 深度对抗性破坏测试 (Adversarial Round 3 Tests)
# ---------------------------------------------------------------------------


def test_adversarial_r3_secret_heading_leak_prevention_brackets_and_spaces():
    """验证全角方括号、圆括号、无冒号空格分隔及修饰词机密段落绝不泄露到 core。"""
    md = """## ［核心］
基础人设说明。

## ［机密］
全角方括号机密内容。

## （机密）
全角圆括号机密内容。

## (机密)
半角圆括号机密内容。

## 机密 真实身世
空格分隔机密副标题。

## 绝密 真实身份
绝密底牌说明。

## 秘密 隐藏底牌
秘密底牌说明。

## ［机密］真实身世
全角方括号外带副标题。

## （机密）身世之谜
全角圆括号外带副标题。

## 机密设定 真实身份
机密设定修饰词带空格。

## 绝密底牌 诛仙残卷
绝密底牌修饰词带空格。
"""
    blocks = split_progressive_blocks(md)
    assert "基础人设说明" in blocks.core
    assert "全角方括号机密内容" not in blocks.core
    assert "全角圆括号机密内容" not in blocks.core
    assert "半角圆括号机密内容" not in blocks.core
    assert "空格分隔机密副标题" not in blocks.core
    assert "绝密底牌说明" not in blocks.core
    assert "秘密底牌说明" not in blocks.core
    assert "全角方括号外带副标题" not in blocks.core
    assert "全角圆括号外带副标题" not in blocks.core
    assert "机密设定修饰词带空格" not in blocks.core
    assert "绝密底牌修饰词带空格" not in blocks.core

    assert blocks.secret is not None
    assert "全角方括号机密内容" in blocks.secret
    assert "全角圆括号机密内容" in blocks.secret
    assert "半角圆括号机密内容" in blocks.secret
    assert "空格分隔机密副标题" in blocks.secret
    assert "绝密底牌说明" in blocks.secret
    assert "秘密底牌说明" in blocks.secret
    assert "全角方括号外带副标题" in blocks.secret
    assert "全角圆括号外带副标题" in blocks.secret
    assert "机密设定修饰词带空格" in blocks.secret
    assert "绝密底牌修饰词带空格" in blocks.secret


def test_adversarial_r3_core_and_topic_variations_brackets_and_spaces():
    """验证核心与专题标题的全角方括号、圆括号及空格副标题均正确归类。"""
    md = """## ［核心］
全角核心正文第一段。

## （核心）基础速写
圆括号核心正文第二段。

## 核心 基础设定
无括号空格核心正文第三段。

## ［专题: 武器］
全角方括号武器说明。

## （专题: 佩剑）
圆括号佩剑说明。

## 专题 功法秘籍
无冒号空格专题说明。

## 专题设定 身法步法
带修饰词空格专题说明。
"""
    blocks = split_progressive_blocks(md)
    assert "全角核心正文第一段" in blocks.core
    assert "圆括号核心正文第二段" in blocks.core
    assert "无括号空格核心正文第三段" in blocks.core

    assert "武器" in blocks.topics
    assert "佩剑" in blocks.topics
    assert "功法秘籍" in blocks.topics
    assert "身法步法" in blocks.topics
    assert "全角方括号武器说明" in blocks.topics["武器"]
    assert "圆括号佩剑说明" in blocks.topics["佩剑"]
    assert "无冒号空格专题说明" in blocks.topics["功法秘籍"]
    assert "带修饰词空格专题说明" in blocks.topics["身法步法"]
    assert blocks.secret is None


def test_adversarial_r3_blueprint_frontmatter_parenthesized_ids_and_chinese():
    """验证蓝图 Frontmatter 中包含括号实体 ID 及纯中文名称均被完整保留与解析。"""
    md = """---
chapter: 1
cast:
  characters:
    - 陈屿 (char_chenyu)
    - 唐栀
  locations:
    - 滨海科技厂 (loc_binhai_kejichang)
    - 孤山观测站
  factions:
    - 晨星科技 (fac_chenxing_tech)
  items:
    - 星河原型盘 (item_xinghe_prototype)
unlock_secrets:
  - 陈屿 (char_chenyu)
topics:
  - 滨海科技厂 (loc_binhai_kejichang): C座通宵实验室
threads:
  - 对赌协议 (thread_solver_benchmark): progress
  - 历史审计: plant
---
本章正文备忘
"""
    bp = parse_blueprint_md("ch_0001.md", content=md)
    # 括号 ID 成功提取
    assert "char_chenyu" in bp.cast.characters
    assert "loc_binhai_kejichang" in bp.cast.locations
    assert "fac_chenxing_tech" in bp.cast.factions
    assert "item_xinghe_prototype" in bp.cast.items
    # 中文名称未被抹除，已合成合法 ID
    assert len(bp.cast.characters) == 2
    assert len(bp.cast.locations) == 2
    # 机密与专题
    assert "char_chenyu" in bp.unlock_secrets
    assert len(bp.topics) == 1
    assert bp.topics[0].entity_id == "loc_binhai_kejichang"
    assert bp.topics[0].topic == "C座通宵实验室"
    # 伏笔
    assert len(bp.threads) == 2
    assert bp.threads[0].thread_id == "thread_solver_benchmark"
    assert bp.threads[0].action == ThreadAction.PROGRESS
    assert bp.threads[1].thread_id.startswith("thread_")
    assert bp.threads[1].action == ThreadAction.PLANT


def test_adversarial_r3_beat_headings_chinese_numerals_scenes_and_lists():
    """验证场景、第一场、汉字及带圈数字节拍标题均能准确解析。"""
    md = """## 章节正文
### 场景 1: 晨会桌上的备忘录
清晨七点核对条款。

### 第一场: 物理封盘
完成离线封盘。

### 一、东海重工突袭
资方代表提前发函。

### ① 极限对赌
双方签订补充协议。
"""
    bp = parse_blueprint_md("ch_0002.md", content=md)
    assert len(bp.beats) == 4
    assert bp.beats[0].index == 1
    assert bp.beats[0].title == "晨会桌上的备忘录"
    assert "清晨七点" in bp.beats[0].content

    assert bp.beats[1].index == 1 or bp.beats[1].index == 2
    assert bp.beats[1].title == "物理封盘"
    assert "离线封盘" in bp.beats[1].content

    assert bp.beats[2].title == "东海重工突袭"
    assert bp.beats[3].title == "极限对赌"


def test_adversarial_r3_unnumbered_bullet_beats_under_header():
    """验证 ## 节拍 下书写的无序号无序列表项能正确被识别为节拍并从正文剥离。"""
    md = """# 第3章 谈判
## 节拍
- 晨会: 清晨七点讨论对赌条款。
- 封盘: 完成星河v0.9离线封盘。

## 创作注意
注意机房风扇噪音与紧张感。
"""
    bp = parse_blueprint_md("ch_0003.md", content=md)
    assert len(bp.beats) == 2
    assert bp.beats[0].index == 1
    assert bp.beats[0].title == "晨会"
    assert "对赌条款" in bp.beats[0].content
    assert bp.beats[1].index == 2
    assert bp.beats[1].title == "封盘"
    assert "离线封盘" in bp.beats[1].content

    assert "晨会" not in bp.body
    assert "封盘" not in bp.body
    assert "创作注意" in bp.body


def test_adversarial_r3_list_beats_first_line_title_multiline():
    """验证无冒号列表节拍的首行短标题被正确提取为 title，而非错误当成 content。"""
    md = """## 节拍
1. 晨会桌上的补充备忘录
   清晨七点，唐栀逐条核对资方送来的补充条款。
2. 物理工程封盘
   完成星河v0.9离线工程盘物理封存。
"""
    bp = parse_blueprint_md("ch_0004.md", content=md)
    assert len(bp.beats) == 2
    assert bp.beats[0].title == "晨会桌上的补充备忘录"
    assert "逐条核对" in bp.beats[0].content
    assert bp.beats[1].title == "物理工程封盘"
    assert "物理封存" in bp.beats[1].content


def test_adversarial_r3_empty_frontmatter_not_polluting_body():
    """验证空 Frontmatter (--- \n ---) 不会残留分隔线并污染正文。"""
    md = """---
---
## [核心]
林凡基本人设说明。
"""
    doc = parse_setting_md("char_linfan.md", content=md)
    assert "---" not in doc.core_text
    assert doc.core_text == "林凡基本人设说明。"


def test_adversarial_r3_chinese_chapter_and_volume_in_filename_and_path():
    """验证蓝图文件名中的中文章号（第十二章.md）与目录中的中文卷号（第一卷）自动推断。"""
    md = """# 算例突袭
POV: char_chenyu

## 节拍
1. 晨会: 内容。
"""
    bp = parse_blueprint_md("02_structure/blueprints/第一卷/第十二章.md", content=md)
    assert bp.chapter == 12
    assert bp.volume_id == "vol_01"
    assert bp.title == "算例突袭"


def test_adversarial_r3_category_english_synonyms_and_chinese_folders():
    """验证英文复数/同义词及中文子目录能够准确映射设定分类并合成正确前缀 ID。"""
    # 英文复数 category: characters
    md1 = """---
category: characters
---
## [核心]
角色描述。
"""
    doc1 = parse_setting_md("linfan.md", content=md1)
    assert doc1.category == SettingCategory.CHARACTER
    assert doc1.id.startswith("char_")

    # 英文同义词 category: location
    md2 = """---
category: location
---
## [核心]
地点描述。
"""
    doc2 = parse_setting_md("binhai.md", content=md2)
    assert doc2.category == SettingCategory.GEOGRAPHY
    assert doc2.id.startswith("loc_")

    # 中文子目录 01_settings/主角/陈屿.md
    md3 = "## [核心]\n首席架构师。"
    doc3 = parse_setting_md("01_settings/主角/陈屿.md", content=md3)
    assert doc3.category == SettingCategory.CHARACTER
    assert doc3.id.startswith("char_")

    # 英文状态 status: dead 映射至 DECEASED 而非复活
    md4 = """---
status: dead
---
## [核心]
已故角色。
"""
    doc4 = parse_setting_md("01_settings/characters/char_dead.md", content=md4)
    assert doc4.status == EntityStatus.DECEASED


def test_parse_freeform_id_and_name_filenames_without_frontmatter_or_h1(tmp_path: Path) -> None:
    doc1 = parse_setting_md(
        tmp_path / "01_设定集" / "人物" / "char_0001-林凡.md",
        content="## [核心]\n外门少年。\n",
    )
    assert doc1.id == "char_0001"
    assert doc1.name == "林凡"

    doc2 = parse_setting_md(
        tmp_path / "01_设定集" / "人物" / "ch-0001-苏瑶.md",
        content="## [核心]\n药庐师妹。\n",
    )
    assert doc2.id == "ch_0001"
    assert doc2.name == "苏瑶"

    bp = parse_blueprint_md(
        tmp_path / "02_结构" / "章节蓝图" / "第01卷" / "第0002章-深夜来电.md",
        content="### 节拍 1：接听电话\n电话那头传来杂音。\n",
    )
    assert bp.chapter == 2
    assert bp.title == "深夜来电"


def test_two_layer_strips_html_comments_and_author_note_sections_respecting_fences(
    tmp_path: Path,
) -> None:
    """验证设定卡中的代码块外 HTML 注释与 [作者备忘]/[随笔] 区块会被自动剥离丢弃。"""
    from ddnovel.core.parser import parse_setting_md, split_progressive_blocks

    raw_body = """## [核心]
表面冷静的帝国战术指挥官。<!-- 内部随笔：记得第5章写她喝黑咖啡 -->

```markdown
<!-- code-fence comment must stay -->
```

## [作者备忘]
这段临时脑洞台词不应该进核心切片。

## [专题: 战术素养]
擅长铁道线伏击与补给线切断。<!-- 专题注释应剥离 -->

## [随笔]
深夜草稿随笔碎片，仅供作者参考。

## [机密]
实际持有旧纪元主控密钥。<!-- 机密注释应剥离 -->
"""
    blocks = split_progressive_blocks(raw_body)
    assert "内部随笔" not in blocks.core
    assert "这段临时脑洞" not in blocks.core
    assert "深夜草稿随笔" not in blocks.core
    assert "<!-- code-fence comment must stay -->" in blocks.core
    assert "专题注释应剥离" not in blocks.topics["战术素养"]
    assert blocks.secret is not None
    assert "机密注释应剥离" not in blocks.secret

    doc = parse_setting_md(
        tmp_path / "01_设定集" / "人物" / "char_0001-林风.md",
        content=f"---\nid: char_0001\nname: 林风\ncategory: character\n---\n{raw_body}",
        book_dir=tmp_path,
    )
    assert "这段临时脑洞" not in doc.core_text
    assert "深夜草稿随笔" not in doc.core_text
    assert "作者备忘" not in doc.topics
    assert "随笔" not in doc.topics


def test_lint_model_setting_doc_flags_concrete_leaks_and_passes_clean_cards(
    tmp_path: Path,
) -> None:
    """验证模型侧设定卡抽象层检测能命中具象台词/场景/动作清单/过长膨胀，且纯净卡零告警。"""
    from ddnovel.core.parser import lint_model_setting_doc, parse_setting_md

    clean_doc = parse_setting_md(
        tmp_path / "01_设定集" / "人物" / "char_clean-沈砚.md",
        content=(
            "---\n"
            "id: char_clean\n"
            "name: 沈砚\n"
            "category: character\n"
            "---\n"
            "## [核心]\n"
            "- 外貌特征：黑发短裁，右眉骨有一道旧疤，着深灰风衣。\n"
            "- 性格机制：防御型理性主义者，习惯在情绪波动前先收集信息并评估退路，"
            "面对信任之人时语言克制但会以实际资源让渡表达庇护。\n"
            "- 能力边界：擅长近距脉冲刃对抗，但心智过载超过三十秒会引发视觉短暂盲区。\n\n"
            "## [机密]\n"
            "幼年曾接受过帝国零号实验室的神经重置。\n"
        ),
        book_dir=tmp_path,
    )
    assert lint_model_setting_doc(clean_doc) == []

    leaky_doc = parse_setting_md(
        tmp_path / "01_设定集" / "人物" / "char_leaky-苏玥.md",
        content=(
            "---\n"
            "id: char_leaky\n"
            "name: 苏玥\n"
            "category: character\n"
            "---\n"
            "## [核心]\n"
            "常见小动作清单：每次紧张时必先撩耳边碎发再轻咳两声。\n"
            "台词例句：“哼，别以为我是在担心你！你要是敢死在外面，我可不会去收尸！”\n"
            "有一次她曾独自站在天台雨夜里对妹妹说“过来吧”。\n"
        ),
        book_dir=tmp_path,
    )
    warnings = lint_model_setting_doc(leaky_doc)
    assert len(warnings) >= 3

    secret_leaky_doc = parse_setting_md(
        tmp_path / "01_设定集" / "人物" / "char_secret_leak-赫尔.md",
        content=(
            "---\n"
            "id: char_secret_leak\n"
            "name: 赫尔\n"
            "category: character\n"
            "---\n"
            "## [核心]\n"
            "无实体的专属伴生AI。\n\n"
            "## [机密]\n"
            "- 独白样本：“主人，我也好想触碰您冰凉的手背，闻一闻您身上的味道啊……”\n"
        ),
        book_dir=tmp_path,
    )
    secret_warnings = lint_model_setting_doc(secret_leaky_doc)
    assert any("[机密]" in w and "独白样本" in w for w in secret_warnings)
    assert any("[机密]" in w and "对白" in w for w in secret_warnings)

    bloated_text = "这是一条非常冗长的未蒸馏设定描述段落，用来模拟把千字原稿直接塞进核心区的膨胀现象。" * 45
    bloated_doc = parse_setting_md(
        tmp_path / "01_设定集" / "人物" / "char_bloat-陈沉.md",
        content=f"---\nid: char_bloat\nname: 陈沉\ncategory: character\n---\n## [核心]\n{bloated_text}\n",
        book_dir=tmp_path,
    )
    bloat_warnings = lint_model_setting_doc(bloated_doc)
    assert any("Token" in w or "tokens" in w.lower() for w in bloat_warnings)




