# esRAGPro 技术路线

> 本文档记录 esRAGPro 项目的技术选型、架构决策和实施路径。
>
> 创建日期：2026-03-17
> 作者：技术架构组

---

## 1. 技术目标

| 目标 | 指标 | 优先级 |
|------|------|--------|
| 检索准确率 | 95%+ | P0 - 必须达成 |
| 部署模式 | 本地私有化优先 | P0 |
| 知识源支持 | 企业文档 + 业务数据库 | P0 |
| 准确率追求 | 100%（持续迭代） | P1 |

---

## 2. 核心技术架构

### 2.1 技术栈选型

```
┌─────────────────────────────────────────────────────────────┐
│                    esRAGPro 技术栈                           │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  【检索引擎】                                                │
│  ├─ 主选：Elasticsearch 8.12+ / 9.0                        │
│  ├─ 方案：混合检索（BM25 + 向量 + RRF）                      │
│  └─ 备选：ES + Milvus 双写                                  │
│                                                             │
│  【Embedding 模型】                                          │
│  ├─ 主选：BGE-M3（中文友好，开源可私有部署）                  │
│  ├─ 备选：M3E-large、text-embedding-3-small                 │
│  └─ 云端：OpenAI Embedding（可选）                          │
│                                                             │
│  【LLM】                                                     │
│  ├─ 主选：支持客户自带模型（DeepSeek、Qwen、GLM 等）         │
│  └─ 云端：OpenAI GPT-4 / Claude（可选）                     │
│                                                             │
│  【Reranker】（增值服务）                                     │
│  ├─ 主选：BGE-Reranker-v2-m3                                │
│  └─ 云端：Cohere Rerank（可选）                             │
│                                                             │
│  【文档解析】                                                 │
│  ├─ PDF：PyMuPDF / pdfplumber                               │
│  ├─ Word：python-docx                                       │
│  ├─ Markdown：原生支持                                       │
│  └─ 表格/图片：后续迭代                                      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 核心检索流程

```
┌─────────┐    ┌──────────────┐    ┌──────────────────┐
│ Query   │───▶│ Query        │───▶│ Elasticsearch     │
│ Input   │    │ Processing   │    │ ┌──────────────┐ │
└─────────┘    └──────────────┘    │ │ BM25 Index   │ │
                                   │ └──────┬───────┘ │
┌─────────┐    ┌──────────────┐    │        │ RRF      │
│ Query   │───▶│ Embedding    │───▶│ ┌──────▼───────┐ │
│ Input   │    │ Model        │    │ │ Vector Index │ │
└─────────┘    └──────────────┘    │ └──────────────┘ │
                                   └────────┬─────────┘
                                            │
                                   ┌────────▼─────────┐
                                   │ Reranker (可选)   │
                                   └────────┬─────────┘
                                            │
                                   ┌────────▼─────────┐
                                   │ Context Assembly │
                                   └────────┬─────────┘
                                            │
                                   ┌────────▼─────────┐
                                   │ LLM Generation   │
                                   └──────────────────┘
```

---

## 3. ES 组件分析与 Rust 重写规划

> **核心决策：** 先用 ES 完整系统验证效果，逐步用 Rust 重写核心组件，最终实现自主可控的高性能检索引擎。

### 3.1 ES 核心组件分析

我们需要 ES 的哪些组件？

```
┌─────────────────────────────────────────────────────────────┐
│                 Elasticsearch 组件架构                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │                    我们需要的组件                     │   │
│  ├─────────────────────────────────────────────────────┤   │
│  │                                                     │   │
│  │  【P0 - 核心必须】                                   │   │
│  │  ├─ BM25 检索引擎（Lucene 倒排索引）                │   │
│  │  ├─ HNSW 向量索引                                   │   │
│  │  ├─ RRF 混合融合算法                                │   │
│  │  └─ 中文分词器（IK）                                │   │
│  │                                                     │   │
│  │  【P1 - 重要但可替代】                               │   │
│  │  ├─ 索引存储（Lucene Segment）                     │   │
│  │  ├─ 查询解析器                                     │   │
│  │  └─ 缓存层                                         │   │
│  │                                                     │   │
│  │  【P2 - 暂不需要重写】                               │   │
│  │  ├─ 集群协调（Zen Discovery）                      │   │
│  │  ├─ 分片路由                                       │   │
│  │  └─ REST API 层                                    │   │
│  │                                                     │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 Rust 重写规划

```
┌─────────────────────────────────────────────────────────────┐
│                    Rust 重写路线图                           │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Phase 1: ES 完整依赖（当前 - 6个月）                        │
│  ─────────────────────────────────────                      │
│  • 使用 ES 8.12/9.0 完整系统                                │
│  • 验证混合检索效果                                         │
│  • 积累生产经验和性能数据                                   │
│  • 识别真正的性能瓶颈                                       │
│                                                             │
│  Phase 2: 核心组件 Rust 化（6-18个月）                       │
│  ─────────────────────────────────────                      │
│  • 重写 RRF 融合算法（最简单，价值明显）                     │
│  • 重写 HNSW 向量索引（性能关键）                           │
│  • 重写 BM25 检索核心（复杂但核心）                         │
│  • 实现自定义中文分词器                                     │
│                                                             │
│  Phase 3: 自研检索引擎（18个月+）                            │
│  ─────────────────────────────────────                      │
│  • 完全替换 ES，实现自主可控                                │
│  • 针对 RAG 场景深度优化                                    │
│  • 极致性能优化                                             │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 3.3 Rust 重写优先级排序

| 优先级 | 组件 | 难度 | 价值 | 建议时机 |
|--------|------|------|------|---------|
| 🥇 **P0** | RRF 融合算法 | ⭐ 简单 | 🔥 高 | Phase 2 早期 |
| 🥈 **P1** | HNSW 向量索引 | ⭐⭐⭐ 中等 | 🔥🔥 极高 | Phase 2 中期 |
| 🥉 **P2** | 中文分词器 | ⭐⭐ 中等 | 🔥 高 | Phase 2 中期 |
| **P3** | BM25 核心 | ⭐⭐⭐⭐ 困难 | 🔥🔥 极高 | Phase 2 晚期 |
| **P4** | 索引存储 | ⭐⭐⭐⭐⭐ 极难 | 🔥 中 | Phase 3 |

### 3.4 Rust 技术选型

| 组件 | Rust 方案 | 参考项目 |
|------|----------|---------|
| 向量索引 | 自研 HNSW 或使用 `usearch` | usearch, hnswlib-rs |
| BM25 | 基于 Tantivy 改造 | Tantivy (Rust 搜索引擎) |
| 中文分词 | 基于 Jieba Rust 版 | rust-jieba |
| RRF 算法 | 自行实现（代码量小）| - |

### 3.5 Rust 重写参考项目

```
推荐参考的开源项目：
├─ Tantivy：Rust 全文搜索引擎（类似 Lucene）
├─ Quickwit：Rust 分布式日志搜索引擎
├─ usearch：Rust HNSW 向量索引
├─ lance：Rust 向量数据库
└─ Meilisearch：Rust 搜索引擎（有 BM25 实现）
```

---

## 4. ES 版本路线图

### 4.1 方案对比

| 方案 | ES 版本 | 混合检索方式 | 成熟度 | 性能 |
|------|--------|-------------|--------|------|
| **方案 A** | ES 9.0 | 原生 RRF | 新，风险高 | 最优 |
| **方案 B** | ES 8.12+ | 应用层 RRF | 成熟稳定 | 良好 |
| **方案 C** | ES + Milvus | 双写融合 | 架构复杂 | 向量最优 |

### 4.2 实施路径（已确定：ES 8.12 → 升级到 ES 9.0）

```
┌──────────────────────────────────────────────────────────┐
│ Phase 1: 快速验证（Week 1-2）                              │
│ ─────────────────────────────────────────                 │
│ • 使用 ES 8.12 搭建 PoC 环境                              │
│ • 导入测试数据集                                          │
│ • 实现 BM25 + kNN 混合检索（应用层 RRF）                   │
│ • 对比纯向量 vs 混合检索效果                               │
│ • 输出评测报告                                            │
│                                                          │
│ 产出：验证混合检索是否达到目标                            │
├──────────────────────────────────────────────────────────┤
│ Phase 2: 升级 ES 9.0（Week 3-4）                           │
│ ─────────────────────────────────────────                 │
│ • 搭建 ES 9.0 环境                                        │
│ • 对比 ES 9.0 vs 8.12 性能和准确率                        │
│ • 使用原生 RRF 替代应用层实现                              │
│ • 评估稳定性风险                                          │
│ • 确定生产使用 ES 9.0                                     │
│                                                          │
│ 产出：ES 9.0 作为生产版本                                 │
├──────────────────────────────────────────────────────────┤
│ Phase 3: 产品化（Week 5-8）                                │
│ ─────────────────────────────────────────                 │
│ • 完善文档解析模块                                         │
│ • 实现 Embedding 服务                                     │
│ • 开发管理后台                                            │
│ • 打包部署脚本                                            │
│                                                          │
│ 产出：可交付的 MVP 产品                                    │
├──────────────────────────────────────────────────────────┤
│ Phase 4: Rust 组件化（长期规划）                            │
│ ─────────────────────────────────────────                 │
│ • Step 1: Rust 重写 RRF 算法                              │
│ • Step 2: Rust 重写 HNSW 向量索引                         │
│ • Step 3: Rust 重写中文分词器                             │
│ • Step 4: Rust 重写 BM25 核心                             │
│ • Step 5: 完全自研检索引擎                                │
│                                                          │
│ 产出：自主可控的高性能检索引擎                             │
└──────────────────────────────────────────────────────────┘
```

### 4.3 ES 9.0 升级要点

```
ES 8.12 → ES 9.0 升级检查清单：
├─ [ ] 数据迁移兼容性测试
├─ [ ] 索引 Mapping 兼容性
├─ [ ] RRF 原生 API 测试
├─ [ ] 性能基准对比
├─ [ ] 稳定性压力测试
└─ [ ] 回滚方案准备
```

---

## 4. 降级方案

### 4.1 ES 版本降级策略

```python
# 伪代码示意
class HybridSearch:
    def __init__(self, es_version):
        self.es_version = es_version
        self.use_native_rrf = es_version >= "9.0"

    def search(self, query, k=10):
        if self.use_native_rrf:
            # ES 9.0 原生 RRF
            return self._native_hybrid_search(query, k)
        else:
            # ES 8.x 应用层 RRF
            return self._application_rrf(query, k)

    def _native_hybrid_search(self, query, k):
        # ES 9.0 单查询混合
        return es.search({
            "query": {"match": {"content": query}},
            "knn": {
                "field": "embedding",
                "query_vector": embed(query),
                "k": k
            },
            "rank": {"rrf": {}}  # 原生 RRF
        })

    def _application_rrf(self, query, k):
        # 应用层 RRF 融合
        bm25_results = es.search({"query": {"match": {"content": query}}}, size=k*2)
        vector_results = es.search({
            "knn": {"field": "embedding", "query_vector": embed(query), "k": k*2}
        }, size=k*2)
        return self._rrf_merge(bm25_results, vector_results, k)
```

### 4.2 向量库降级策略

如果 ES 向量检索性能不足，可降级到 Milvus/Qdrant：

```
架构变化：
ES (BM25 + 元数据) + Milvus (向量) → 应用层融合

降级触发条件：
• 单索引向量数 > 1000 万
• 向量检索延迟 > 100ms
• 内存占用 > 可用内存 80%
```

---

## 5. RRF 参数调优

### 5.1 RRF 公式

```
RRF_score(d) = Σ 1 / (k + rank_i(d))

其中：
• d: 文档
• rank_i(d): 文档 d 在第 i 个检索结果中的排名
• k: 平滑参数（通常 60）
```

### 5.2 调优参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| k | 60 | RRF 平滑参数 |
| bm25_weight | 0.5 | BM25 权重（应用层融合时）|
| vector_weight | 0.5 | 向量权重（应用层融合时）|
| top_k | 10 | 返回文档数 |
| num_candidates | 100 | 向量召回候选数 |

### 5.3 调优策略

```
调优流程：
1. 准备验证数据集
2. 网格搜索最优参数组合
   - k: [40, 50, 60, 70, 80]
   - bm25_weight: [0.3, 0.4, 0.5, 0.6, 0.7]
3. 以 Recall@10 和 NDCG@10 为指标
4. 选择最优参数组合
```

---

## 6. 索引设计

### 6.1 文档索引 Mapping

```json
{
  "mappings": {
    "properties": {
      "doc_id": {"type": "keyword"},
      "title": {
        "type": "text",
        "analyzer": "ik_max_word",
        "search_analyzer": "ik_smart"
      },
      "content": {
        "type": "text",
        "analyzer": "ik_max_word",
        "search_analyzer": "ik_smart"
      },
      "content_embedding": {
        "type": "dense_vector",
        "dims": 1024,
        "index": true,
        "similarity": "cosine"
      },
      "doc_type": {"type": "keyword"},
      "source": {"type": "keyword"},
      "created_at": {"type": "date"},
      "metadata": {
        "properties": {
          "author": {"type": "keyword"},
          "department": {"type": "keyword"},
          "tags": {"type": "keyword"}
        }
      }
    }
  }
}
```

### 6.2 分片策略

| 数据规模 | 分片数 | 副本数 |
|---------|--------|--------|
| < 10万文档 | 1 | 1 |
| 10-100万文档 | 3 | 1 |
| 100-1000万文档 | 5 | 2 |
| > 1000万文档 | 10+ | 2 |

---

## 7. 风险与缓解

| 风险 | 级别 | 缓解措施 |
|------|------|---------|
| ES 9.0 不稳定 | 高 | 先用 8.12 验证，保留降级方案 |
| 中文效果不佳 | 中 | 测试多种 Embedding 模型，选择最优 |
| 文档解析坑深 | 中 | Phase 1 只支持纯文本，逐步扩展 |
| 内存占用过高 | 中 | 监控内存，必要时降级到外部向量库 |

---

## 4. 技术决策记录

| 日期 | 决策 | 原因 | 决策者 |
|------|------|------|--------|
| 2026-03-17 | 选择 ES 混合检索作为核心方案 | 准确率提升潜力大，技术成熟度适中 | 架构组 |
| 2026-03-17 | Phase 1 使用 ES 8.12 | 文档和案例成熟，降低风险 | 架构组 |
| 2026-03-17 | Embedding 主选 BGE-M3 | 中文效果好，支持私有部署 | 架构组 |
| 2026-03-17 | 确定升级到 ES 9.0 | 原生 RRF 性能最优 | 创始人 |
| 2026-03-17 | 长期用 Rust 重写核心组件 | 自主可控、极致性能 | 创始人 |
| 2026-03-17 | Rust 重写优先级：RRF > HNSW > 分词 > BM25 | 由简到繁，逐步替换 | 架构组 |
| 2026-07-11 | **MCP Gateway V1 完成** | Rust 实现薄 Gateway，WP0-WP9 全部完成 | 架构组 |

---

## 9. Rust 重写详细规划

### 9.1 RRF 算法 Rust 实现（Phase 2 早期）

```rust
// RRF (Reciprocal Rank Fusion) 核心算法
// 代码量小，适合作为第一个 Rust 重写组件

pub struct RRFMerger {
    k: f64,  // 平滑参数，默认 60
}

impl RRFMerger {
    pub fn new(k: f64) -> Self {
        Self { k }
    }

    /// 计算 RRF 分数
    /// score(d) = Σ 1 / (k + rank_i(d))
    pub fn merge(&self, results: Vec<Vec<SearchResult>>) -> Vec<MergedResult> {
        let mut scores: HashMap<DocId, f64> = HashMap::new();

        for ranking in results {
            for (rank, result) in ranking.iter().enumerate() {
                let rrf_score = 1.0 / (self.k + (rank + 1) as f64);
                *scores.entry(result.doc_id.clone()).or_insert(0.0) += rrf_score;
            }
        }

        // 按分数排序返回
        let mut merged: Vec<_> = scores.into_iter()
            .map(|(doc_id, score)| MergedResult { doc_id, score })
            .collect();
        merged.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap());
        merged
    }
}
```

**重写收益：**
- 代码量小，风险可控
- 可独立测试验证
- 性能提升约 10-20%

### 9.2 HNSW 向量索引 Rust 实现（Phase 2 中期）

**方案选择：**
1. 自研：完全从头实现 HNSW
2. 封装：基于 `usearch` 或 `hnswlib-rs` 封装
3. 改造：基于 Tantivy 扩展向量能力

**推荐方案：** 先封装 `usearch`，验证后再自研

```rust
// 基于 usearch 的向量索引封装
use usearch::Index;

pub struct VectorIndex {
    index: Index,
    dimensions: usize,
}

impl VectorIndex {
    pub fn new(dimensions: usize, max_elements: usize) -> Self {
        let index = Index::new(dimensions, usearch::ScalarKind::F32)
            .expect("Failed to create index");
        index.reserve(max_elements).expect("Failed to reserve");
        Self { index, dimensions }
    }

    pub fn add(&mut self, id: u64, vector: &[f32]) {
        self.index.add(id, vector).expect("Failed to add vector");
    }

    pub fn search(&self, query: &[f32], k: usize) -> Vec<(u64, f32)> {
        self.index.search(query, k)
            .expect("Failed to search")
            .into_iter()
            .map(|r| (r.key, r.distance))
            .collect()
    }
}
```

**重写收益：**
- 内存占用降低 30-50%
- 检索延迟降低 20-40%
- 支持更灵活的优化

### 9.3 参考项目链接

| 项目 | 语言 | 说明 | GitHub |
|------|------|------|--------|
| Tantivy | Rust | 全文搜索引擎 | github.com/quickwit-oss/tantivy |
| usearch | Rust | HNSW 向量索引 | github.com/unum-cloud/usearch |
| lance | Rust | 向量数据库 | github.com/lancedb/lance |
| Quickwit | Rust | 分布式搜索 | github.com/quickwit-oss/quickwit |
| Meilisearch | Rust | 搜索引擎 | github.com/meilisearch/meilisearch |

---

## 变更记录

| 日期 | 变更内容 |
|------|---------|
| 2026-03-17 | 初始化技术路线文档 |
| 2026-03-17 | 新增 ES 组件分析与 Rust 重写规划 |
| 2026-03-17 | 确定 ES 8.12 → ES 9.0 升级路径 |
| 2026-03-17 | 新增 Rust 重写优先级和参考项目 |
| 2026-03-19 | **新增知识图谱模块（Neo4j）** |
| 2026-07-11 | **MCP Gateway V1 完成（WP0-WP9）** |

---

## 11. 当前项目状态（2026-07-11 更新）

### 已完成的工作包

| WP | 名称 | 状态 |
|---|------|------|
| WP0 | 基线审计、ADR 与版本控制 | ✅ |
| WP1 | 修复 esRAGPro 主链 | ✅ |
| WP2 | 身份、租户、ACL 与版本 | ✅ |
| WP3 | 异步摄取、稳定 ID 与投影一致性 | ✅ |
| WP4 | 可信 Claim 图谱 | ✅ |
| WP5 | 混合检索、按需图检索、重排与证据防护 | ✅ |
| WP6 | Rust 薄 MCP Gateway 与 stdio | ✅ |
| WP7 | Streamable HTTP、OAuth 与企业边界 | ✅ |
| WP7A | 独立管理 MCP Profile | ✅ |
| WP8 | 评测、可观测性、发布与灾备门禁 | ✅ |
| WP9 | 可选 Local Edge | ✅ |

### Rust 重写进展

MCP Gateway 已完成（esragpro-mcp）：
- 6 只读 Tools + 4 管理 Tools
- stdio + HTTP (Streamable) transport
- OAuth/JWT 认证
- 64 tests passed
- 6.99 MB release binary

### 下一步计划

1. 完成准确率评测（使用真实数据集）
2. 客户验证和 PoC
3. 考虑 Rust 重写检索核心组件

---

## 10. 知识图谱模块

### 10.1 技术选型

| 组件 | 选型 | 说明 |
|------|------|------|
| 图数据库 | Neo4j 5.15 Community | 成熟稳定，Cypher 易学 |
| 实体识别 | HanLP + 规则 + LLM | 三重保障 |
| 关系抽取 | 规则模式 + LLM | 准确率优先 |

### 10.2 核心功能

```
知识图谱模块：
├── 实体识别服务 (entity_extractor.py)
│   ├── 人物、组织、法律、案例识别
│   ├── 规则 + 词典 + HanLP 三重模式
│   └── 法律领域定制词典
│
├── 关系抽取服务 (relation_extractor.py)
│   ├── 预定义关系模式匹配
│   ├── 实体共现关系发现
│   └── LLM 增强抽取（可选）
│
├── 图谱服务 (knowledge_graph.py)
│   ├── 实体/关系的 CRUD
│   ├── 子图查询
│   ├── 多跳查询
│   └── 事实验证
│
└── GraphRAG 融合服务 (graph_rag.py)
    ├── 文档索引时构建图谱
    ├── 查询时融合检索
    └── 答案验证
```

### 10.3 图谱 Schema（律所场景）

```cypher
// 实体类型
(:Person)          // 人物
(:Organization)    // 组织
(:Law)             // 法律
(:Case)            // 案例
(:Document)        // 文档
(:Concept)         // 概念

// 关系类型
(:Person)-[:WORKS_FOR]->(:Organization)
(:Case)-[:CITES]->(:Law)
(:Concept)-[:DEFINED_BY]->(:Law)
(:Document)-[:ABOUT]->(:Concept)
```

### 10.4 预期效果

| 指标 | 当前 (v0.2) | 加入图谱后 (v0.3) |
|------|-------------|-------------------|
| 幻觉率 | 3-5% | **1-3%** |
| 回答完整性 | 70% | **90%** |
| 多跳问答 | ❌ | ✅ |
| 关系推理 | ❌ | ✅ |

### 10.5 API 端点

```
GET  /api/v1/graph/health          # 健康检查
GET  /api/v1/graph/stats           # 统计信息
GET  /api/v1/graph/entities        # 搜索实体
GET  /api/v1/graph/entities/{name} # 获取实体
POST /api/v1/graph/subgraph        # 子图查询
POST /api/v1/graph/multi-hop       # 多跳查询
POST /api/v1/graph/search          # 融合搜索
POST /api/v1/graph/ask             # 图谱增强问答
```