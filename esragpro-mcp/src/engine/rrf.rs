//! RRF (Reciprocal Rank Fusion) 融合算法
//!
//! RRF_score(d) = Σ weight_i / (k + rank_i(d))
//! 将多路搜索结果融合为统一排序。
