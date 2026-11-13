# 02. DDNovelGenerater 系统架构与详细设计方案

> **关联文档**：
> - [01-requirements-breakdown.md](file:///D:/work/AI/proj/DDNovelGenerater/docs/01-requirements-breakdown.md)（需求全景拆解说明书）
> - [03-development-roadmap-and-dag.md](file:///D:/work/AI/proj/DDNovelGenerater/docs/03-development-roadmap-and-dag.md)（开发步骤、前后依赖与串并联流程图）

---

## 1. 全景系统分层架构框架图

系统采用 **六层解耦架构**，自顶向下依次为：**人机与宿主交互层 -> 角色认知编排层（Skills）-> 双端协议接入层（CLI & MCP）-> 核心业务领域层 -> 解析与派生索引底座层 -> 文账分离唯一真源层（SSoT）**。

```mermaid
flowchart TB
    subgraph L6["Layer 6: 作者与 AI 宿主环境层 (Host Runtimes)"]
        Author["小说作者 (编辑器手改 / 终端指令 / 通篇审稿)"]
        Hosts["AI 软件宿主 (opencode / dsh / Claude Code)"]
    end

    subgraph L5["Layer 5: 四角色认知编排层 (Agent Skills)"]
        SK1["dd-setting-architect (设定师 Skill)"]
        SK2["dd-structure-planner (结构规划师 Skill)"]
        SK3["dd-chapter-writer (单包独立执笔人 Skill)"]
        SK4["dd-state-curator (场记与审校员 Skill)"]
    end

    subgraph L4["Layer 4: 双端协议接入层 (Dual Interfaces)"]
        CLI["Typer + Rich CLI (`ddnovel`)"]
        MCP["FastMCP Stdio Server (`ddnovel-mcp`)"]
    end

    subgraph L3["Layer 3: 核心业务领域引擎层 (Domain Engines)"]
        Init["ProjectInitializer (多书骨架与宿主配置生成)"]
        Radar["ForeshadowRadar (规划期三类伏笔巡检)"]
        Impact["ImpactAnalyzer (中途改设定五维吃书扫描)"]
        Packet["PacketBuilder (U型注意力单章写作包编译器)"]
        Life["LifecycleManager (定稿入库 / 冷库冻结 / 硬剪枝 / 卷浓缩 / 体检)"]
    end

    subgraph L2["Layer 2: 解析、匹配与派生检索层 (Index & Retrieval Core)"]
        Parser["ProgressiveParser (宽容 Frontmatter + 渐进区块切片 + Token估算)"]
        AC["AliasMatcher (Aho-Corasick 实体全名/外号自动机)"]
        Idx["SqliteIndexer (哈希增量同步 / 零污全量重建 / FTS5 / 双时态SQL)"]
        Vec["VectorPlugin (可选插拔 Embedding 混合 RRF 重排)"]
        DB[(".ddnovel/index.sqlite (派生缓存库)")]
    end

    subgraph L1["Layer 1: 本地文账分离唯一真源层 (Git-Tracked SSoT Files)"]
        F_Cfg["novel.yaml (全局配置与题材Profile)"]
        F_Set["01_settings/ (自由MD + 核心/专题/机密渐进切片)"]
        F_Str["02_structure/ (总纲 -> 分卷 -> 单元 -> 章节节拍蓝图)"]
        F_Sta["03_state/ (截面热状态YAML + 双时态关系 + 伏笔池 + 原子事实JSONL + archive冷库)"]
        F_Cre["04_creation/ (文风红线 + packets写作包 + drafts整章草稿 + manuscript正文)"]
    end

    Author <--> Hosts
    Author <--> CLI
    Author <--> L1
    Hosts --> SK1 & SK2 & SK3 & SK4
    SK1 & SK2 & SK4 --> MCP
    SK3 -.->|"严格上下文隔离: 仅读单章写作包输出整章草稿"| F_Cre

    CLI & MCP --> Init & Radar & Impact & Packet & Life
    Radar & Impact & Packet & Life --> Idx & AC & Parser
    Idx <--> Vec
    Idx <--> DB
    Parser & Init & Life <--> L1
    Idx <-- "SHA-256 增量读取同步" --- L1
```

---

## 2. 核心模块结构与类依赖图（Class Diagram）

以下类图明确了 `src/ddnovel/` 各核心类的职责边界、核心方法与组合依赖关系，作为编码实现的直接接口契约：

```mermaid
classDiagram
    class NovelConfig {
        +int schema_version = 1
        +str title
        +str author
        +str genre_profile
        +int current_chapter
        +int chapter_id_width = 4
        +TokenBudgetConfig budget
        +LifecycleConfig lifecycle
        +EmbeddingConfig embedding
        +list~HostKind~ hosts
        +load(book_dir) NovelConfig
        +dump(book_dir) Path
    }

    class ProgressiveParser {
        +parse_setting_md(path) SettingDoc
        +parse_blueprint_md(path) BlueprintDoc
        +estimate_tokens(text) int
        +split_progressive_blocks(markdown) ParsedBlocks
    }

    class AliasMatcher {
        -dict trie
        +build(entity_aliases_map) void
        +scan_text(text, include_archived) list~MatchedEntity~
    }

    class SqliteIndexer {
        -Path db_path
        +sync(force_rebuild) SyncReport
        +search_fts(query, scope, top_k) list~SearchHit~
        +get_active_relations(entity_ids, at_chapter) list~TemporalRelation~
        +get_entity_bundle(entity_id) EntityBundle
        +get_recent_summaries(before_ch, count) list~ChapterFactRecord~
    }

    class VectorPlugin {
        +is_enabled() bool
        +embed_texts(texts) list~bytes~
        +hybrid_rerank(query, fts_hits, top_k) list~SearchHit~
    }

    class ForeshadowRadar {
        +scan(target_chapter, planned_entities) RadarReport
    }

    class ImpactAnalyzer {
        +scan_impact(target_id_or_keyword) ImpactReport
    }

    class PacketBuilder {
        +build(chapter_num, custom_budget) PacketBuildResult
        -compile_slot_1_guardrails(bp, pov_state) str
        -compile_slot_2_world_and_facts(bp, matched_ids) str
        -compile_slot_3_entities_and_relations(bp, matched_ids) str
        -compile_slot_4_continuity(ch_num) str
        -compile_slot_5_beats_and_threads(bp) str
    }

    class LifecycleManager {
        +promote_chapter(chapter_num) PromoteResult
        +clean_ttl_artifacts(current_ch) list~Path~
        +cold_archive_gc(current_ch, dry_run) GCReport
        +drop_thread(thread_id) DropResult
        +prune_settings(target_ids, apply) PruneReport
        +compact_volume_facts(vol_id, milestones) CompactResult
        +audit_chapter(chapter_num) AuditReport
    }

    SqliteIndexer --> NovelConfig : reads
    SqliteIndexer --> ProgressiveParser : uses
    SqliteIndexer --> VectorPlugin : optional
    PacketBuilder --> SqliteIndexer : queries
    PacketBuilder --> AliasMatcher : scans blueprint
    PacketBuilder --> ProgressiveParser : estimates tokens
    ForeshadowRadar --> SqliteIndexer : queries threads
    ImpactAnalyzer --> SqliteIndexer : queries 5 scopes
    ImpactAnalyzer --> AliasMatcher : expands aliases
    LifecycleManager --> SqliteIndexer : triggers sync
    LifecycleManager --> ImpactAnalyzer : pre-check before drop/prune
```

---

## 3. 派生索引数据库设计（ER 图）

`.ddnovel/index.sqlite` 完全由本地文本真源派生，不存独占业务数据，表关系如下：

```mermaid
erDiagram
    FILE_HASHES {
        text rel_path PK "相对小说根目录路径"
        text sha256 "文件内容哈希"
        text category "setting / blueprint / state / fact / manuscript"
        real synced_at "同步时间戳"
    }

    ENTITIES {
        text id PK "如 char_linfan / loc_beijing"
        text name "主名称"
        text category "world / rule / geography / faction / character / item / other"
        text status "active / deceased / retired"
        int is_archived "0=热池参与嗅探, 1=冷库冻结"
        int updated_at_ch "最后更新章号"
        text file_path "源设定文件路径"
        text core_text "默认入包核心切片"
        text secret_text "默认物理隔离的机密切片"
        text state_yaml_json "当前动态截面序列化"
    }

    ENTITY_ALIASES {
        text alias PK "名称或外号简称"
        text entity_id FK "指向 ENTITIES.id"
        int is_archived "冗余标记加速热池过滤"
    }

    SETTING_TOPICS {
        int id PK
        text entity_id FK "指向 ENTITIES.id"
        text topic_name "专题名 如 剑冢禁制"
        text content "按需装载切片正文"
    }

    RELATIONS {
        int id PK
        text source_id FK "起点实体ID"
        text target_id FK "终点实体ID"
        text relation "关系描述"
        int valid_from_ch "生效起始章号"
        int invalid_at_ch "失效章号 (左闭右开, NULL代表当前仍有效)"
    }

    THREADS {
        text id PK "如 thread_042"
        text title "伏笔简述"
        text category "悬线分类"
        text status "planted / dormant / active / resolved"
        int planted_at "埋设章号"
        int last_progressed_at "上次推进章号"
        int win_start "预计回收起始章"
        int win_end "预计回收结束章"
        text trigger_entities_json "关联触发实体ID数组"
        text public_clue "表面已知线索"
        text truth_secret "幕后真相(非回收期隔离)"
    }

    CHAPTER_FACTS {
        int ch PK "章号"
        text volume_id "所属卷ID"
        text arc_id "所属单元ID (JSONL别名 arc)"
        text in_world_time "书内时间坐标 (JSONL别名 time)"
        text summary "单章百字摘要"
        text facts_json "3~5条原子事实数组"
        text entities_json "本章涉及实体ID数组"
    }

    FTS_DOCS {
        text doc_id "文档切片唯一键"
        text scope "setting / fact / summary / manuscript / blueprint"
        int chapter_num "关联章号(设定为0)"
        text entity_id "关联实体ID"
        text tokenized_text "CJK双模索引文本"
        text raw_excerpt "原始文本片段"
    }

    EMBEDDINGS {
        text doc_id PK "与 FTS_DOCS 对应(可选启用)"
        text content_hash "防重复请求哈希"
        int dim "向量维度"
        blob vec_f32 "紧凑 float32 字节流"
    }

    ENTITIES ||--o{ ENTITY_ALIASES : "拥有多个称呼"
    ENTITIES ||--o{ SETTING_TOPICS : "包含专题切片"
    ENTITIES ||--o{ RELATIONS : "作为起点或终点"
    FTS_DOCS ||--o| EMBEDDINGS : "可选向量化"
```

---

## 4. 单章独立写作包“U 型注意力预算槽位”框架图

大语言模型对长文本存在显著的 **首尾高注意、中部注意衰减（Lost in the Middle）** 现象。`PacketBuilder` 生成 `ch_XXXX_packet.md` 时严格按照以下五槽位结构排版与预算裁剪：

```mermaid
flowchart TD
    subgraph Packet["ch_XXXX_packet.md (默认总预算 ~10,000 Tokens, 作者可直接打开增删微调)"]
        direction TB
        Slot1["[槽位 1 - 顶部强注意区 ~1,200T] 文风与叙事铁律 + POV 视角角色认知禁区 (unaware_of 防剧透红线)"]
        Slot2["[槽位 2 - 中前背景区 ~2,200T] 分卷/单元宏观一句话坐标 + 本场地理/规则切片 + 召回的历史原子事实锚点"]
        Slot3["[槽位 3 - 中部实体截面区 ~2,500T] 仅限本章出场角色的[核心速写] + [当前热状态截面] + [当前章有效关系边]"]
        Slot4["[槽位 4 - 中后文气衔接区 ~2,000T] 最近 3 章百字摘要 + 上一章定稿正文末尾 800~1,200 字原文"]
        Slot5["[槽位 5 - 尾部最高注意指令区 ~1,800T] 本章分场节拍蓝图 (Scenes/Beats) + 指定伏笔动作 + 本章禁止事项"]

        Slot1 --> Slot2 --> Slot3 --> Slot4 --> Slot5
    end

    Filter1["物理隔离拦截器 1: 所有设定[机密]区块默认拦截 (除非 unlock_secrets 解锁)"] -.->|"过滤"| Slot2 & Slot3
    Filter2["物理隔离拦截器 2: 未被本章蓝图点名的休眠伏笔 100% 拦截不入包"] -.->|"过滤"| Slot5
    Filter3["物理隔离拦截器 3: 冷库(archive)退场龙套与失效旧关系边自动过滤"] -.->|"过滤"| Slot3
```

---

## 5. 核心业务流程时序图（Sequence Diagrams）

### 5.1 单章创作、通篇审稿、定稿与状态沉淀闭环

```mermaid
sequenceDiagram
    actor Author as 作者
    participant Planner as 规划师 Skill
    participant Engine as DDNovel Engine (CLI/MCP)
    participant Writer as 执笔人 Skill (隔离上下文)
    participant Curator as 场记审校 Skill

    Author->>Engine: 1. 请求规划第 N 章 (运行 radar 伏笔雷达)
    Engine-->>Author: 返回窗口到期 / 久未推进 / 关联人物的三类候选伏笔
    Author->>Planner: 2. 共创编写章节蓝图 ch_N.md (确定出场人物、节拍Beats、勾选伏笔)
    Author->>Engine: 3. 执行 packet build N
    Engine->>Engine: 嗅探别名 + 查双时态关系 + 认知防剧透过滤 + U型排版
    Engine-->>Author: 一步直出 04_creation/packets/ch_N_packet.md (附各段Token统计)
    Author->>Author: 4. 打开 ch_N_packet.md 自由删减冗余或补充细节并保存

    rect rgb(235, 245, 255)
        Note over Author,Writer: 完全上下文隔离区：执笔模型不读百章旧文件，只看 ch_N_packet.md
        Author->>Writer: 5. 指令生成第 N 章草稿
        Writer->>Writer: 按蓝图分场节拍一气呵成写完整章草稿
        Writer-->>Author: 输出完整草稿 04_creation/drafts/ch_N_draft.md
    end

    Author->>Author: 6. 一次性通篇阅读并修改打磨整章草稿
    Author->>Curator: 7. 定稿结算 (audit + promote + 状态/事实提取)
    Curator->>Engine: 执行 audit N & chapter promote N
    Engine->>Engine: 归档至 manuscript/ 并自动 TTL 清理超期旧写作包与旧草稿
    Curator-->>Author: 协助提炼单章摘要+3~5条原子事实、更新 active 状态截面(新临时配角可反哺01_settings)
    Author->>Engine: 8. 确认状态文件并执行 index sync (+ git tag)
```

### 5.2 创作中途动态增删改设定与防吃书扫描时序图

```mermaid
sequenceDiagram
    actor Author as 作者
    participant Architect as 设定师 Skill
    participant Engine as DDNovel Engine (Impact + Indexer)
    participant Files as 四维真源文件库

    alt 情况 A: 剧情推进导致书内世界巨变 (如第150章城池毁灭/宗门覆灭)
        Author->>Architect: 提出城池毁灭变化
        Architect->>Files: 保留 01_settings/ 原始设定不删，覆写 03_state/ 动态状态与关系失效边
    else 情况 B: 作者中途调整/重构/删除早期设定 (Retcon 或砍支线)
        Author->>Engine: 1. 执行 setting impact <设定ID或关键词>
        Engine->>Engine: 展开全部外号别名，跨[正文/历史事实/热状态/伏笔/后续蓝图]五维扫描
        Engine-->>Author: 2. 输出《防吃书影响面报告》(精确到章号与原句摘录)
        Author->>Files: 3. 修改/删除 01_settings/ 文件 (若有前文冲突顺手微调圆场)
        Author->>Engine: 4. 执行 index sync (若当前章已建包可重跑 packet build)
        Engine->>Engine: SHA-256 增量同步，自动级联清除已删词条与残留别名
    end
```

---

## 6. 状态演进与四级遗忘状态机图（State Diagrams）

### 6.1 悬线与事件伏笔生命周期（含主动弃坑硬删除）

```mermaid
stateDiagram-v2
    [*] --> planted : 首次埋设 (记入 threads.yaml)
    planted --> dormant : 暂不推进 (进入休眠，写作包零打扰)
    planted --> active : 近期持续铺垫推进

    dormant --> active : 雷达命中回收窗口或关联实体，蓝图勾选唤醒
    active --> dormant : 阶段性推进完毕，重新转入长线潜伏

    active --> resolved : 蓝图指定揭晓回收完成
    resolved --> ArchivedCold : 执行 state gc 移入 archive/ 冷库归档

    planted --> DroppedDeleted : 作者不想填了 -> 执行 thread drop 物理移除
    dormant --> DroppedDeleted : 作者砍掉支线 -> 执行 thread drop 物理移除
    active --> DroppedDeleted : 作者决定放弃 -> 查影响面后物理移除

    ArchivedCold --> [*]
    DroppedDeleted --> [*]
```

### 6.2 设定实体与历史记忆冷热瘦身生命周期

```mermaid
stateDiagram-v2
    [*] --> DraftOrphan : 新建设定卡 (尚未在正式正文出场)
    DraftOrphan --> PrunedDeleted : 长期未用废案 -> 执行 setting prune 物理删除
    DraftOrphan --> ActiveHot : 正式出场进入热池 (参与默认别名嗅探)

    ActiveHot --> PrunedDeleted : 仅出场1~2章纯工具人/小地点 -> 作者勾选 prune 销毁卡片
    ActiveHot --> HibernatedCold : 角色死亡 / 势力覆灭 / 超40章未登场且无绑定伏笔 (state gc)

    HibernatedCold --> ActiveHot : 数百章后回忆或返场 -> 蓝图显式点名重新激活
    HibernatedCold --> [*] : 常驻冷库 (退出默认嗅探与写作包，零注意力消耗)
    PrunedDeleted --> [*]
```
