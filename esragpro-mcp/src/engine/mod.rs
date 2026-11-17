//! 检索引擎层
//!
//! - pipeline.rs:    五层检索流水线编排
//! - keyword.rs:     Layer 1 — jieba-rs 中文分词 + 关键词重写
//! - fulltext.rs:    Layer 2 — LanceDB Tantivy 全文搜索
//! - vector.rs:      Layer 3 — BGE-M3 ONNX 向量搜索
//! - graph_query.rs: Layer 4 — SQLite 图谱查询 + 邻居扩展
//! - reasoning.rs:   Layer 5 — 多跳推理路径排序
//! - rrf.rs:         RRF (Reciprocal Rank Fusion) 融合算法
//! - embedding.rs:   ONNX Embedding 推理封装

pub mod pipeline;
pub mod keyword;
pub mod fulltext;
pub mod vector;
pub mod graph_query;
pub mod reasoning;
pub mod rrf;
pub mod embedding;
