//! 知识抽取层
//!
//! - document.rs: 文档解析 (PDF/DOCX/TXT/MD)
//! - chunker.rs:  文本分块策略 (500字+50字重叠)
//! - entity.rs:   LLM 实体抽取
//! - relation.rs: LLM 关系抽取
//! - indexer.rs:  摄取编排 (解析→分块→嵌入→存储)

pub mod document;
pub mod chunker;
pub mod entity;
pub mod relation;
pub mod indexer;
