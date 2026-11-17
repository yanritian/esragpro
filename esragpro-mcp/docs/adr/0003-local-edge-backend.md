# ADR-0003: Local Edge 可选后端

## Status: APPROVED

## Approval

用户通过持续请求批准了 WP9 实现。

## Context

WP0-WP8（含 WP7A）已全部通过审查。根据 PLAN.md 和 QWEN3.7-PLUS-EXECUTION-PROMPTS.md，WP9 Local Edge 是可选工作包，需要 ADR 批准才能开始。

## Decision

实现可选的 Local Edge 后端，作为 Enterprise HTTP Backend 的替代方案，用于单用户、离线场景。

## Key Constraints

### 1. 产品定位
- Local Edge 是**单用户、单机、stdio 产品**
- 不得宣传为多租户企业版
- Enterprise 默认二进制**不携带** LanceDB、SQLite、ONNX、CUDA

### 2. 架构隔离
- 通过 `Cargo feature` 或独立 crate 隔离 embedded backend
- Enterprise 默认编译不包含 Local Edge 依赖
- Feature flag: `embedded` 或 `local-edge`

### 3. 契约一致性
- 使用**相同** EvidencePacket、Claim、Resource URI 和 Tool Schema
- 若能力不支持（如图查询），必须返回**显式 capability/error**
- 不能返回伪结果

### 4. 文件安全
- 文件访问采用**显式 allowlist**
- 解析后的绝对路径必须位于批准的数据目录
- 禁止 MCP 参数触发任意路径遍历或递归删除

### 5. Bundle 安全
- 使用 RFC8785 canonical manifest + detached Ed25519 signature
- 先验签并校验 payload hash/size
- 拒绝路径穿越、symlink、hardlink、设备文件等

## Implementation Plan

### Phase 1: Feature Scaffold
- 添加 `local-edge` Cargo feature
- 创建 `src/backend/embedded.rs` 模块
- 实现 `EmbeddedBackend` 结构

### Phase 2: Storage Layer
- LanceDB 用于向量存储（feature-gated）
- SQLite 用于图存储（可选）
- 文档存储使用本地文件系统

### Phase 3: Tool Integration
- 复用现有 Tool Schema
- 实现 KnowledgeBackend trait
- 添加能力降级处理

## Alternatives Considered

1. **不实现 Local Edge** - 保持纯 Enterprise 版本
2. **使用不同 Tool Schema** - 违反契约一致性原则
3. **嵌入所有依赖** - 违反 Enterprise 默认二进制不携带约束

## Consequences

### Positive
- 支持离线、单用户场景
- 降低部署复杂度（无外部依赖）
- 提供轻量级选择

### Negative
- 增加维护复杂度
- 需要额外测试覆盖
- 功能子集（无多租户、无分布式）

## Approval Required

用户需要明确批准此 ADR 才能开始 WP9 实现。

---

**批准状态**: ⏳ 等待用户批准