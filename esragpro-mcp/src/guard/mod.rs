//! 幻觉防护层
//!
//! - prompt.rs:    Layer 1 — 安全提示词生成
//! - factcheck.rs: Layer 2 — 事实核查引擎 (声明→文档验证)
//! - rejection.rs: Layer 3 — 置信度评估 + 拒绝机制

pub mod prompt;
pub mod factcheck;
pub mod rejection;
