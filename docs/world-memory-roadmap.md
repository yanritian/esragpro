# 动态世界记忆路线图

> 文档日期：2026-07-23
> 当前状态：**Phase 2-A 参考存储**（进程内实现，重启不持久）
> 本路线图描述未来规划，所有 P1-P9 阶段均处于 planned/target/prototype 状态，尚未实现。

## 当前基线 (Phase 2-A)

已完成：

- Memory Core 领域代码（`remember`、`query`、`create_context`）
- 进程内参考存储（内存字典）
- Python 定向测试 57 passed
- Rust MCP 协议适配（`cargo test` 140 passed，1 既有失败）
- 合同定义（`MemoryItem`、`EvidenceRef`、`ContextPack`）
- 无权限本地调用与企业远程调用的安全边界设计

**当前状态不是生产化**。参考存储不支持持久化、跨进程和重启恢复。

## 阶段总览

| 阶段 | 名称 | 目标 |
|---|---|---|
| **P0** | 边界冻结和基线 | 合同冻结、mock 标记、验收命令 |
| **P1** | 真实权威存储和投影 | PostgreSQL → Outbox → Redis → ES → Neo4j → MinIO |
| **P2** | 实时摄取和 Outbox Worker | 事务写入、重试、DLQ、回放 |
| **P3** | 混合检索和图增强 | Query Router、RRF、Neo4j 真连接、Claim 生命周期 |
| **P4** | MCP 跨进程和客户端兼容 | Rust MCP → Backend HTTP、stdio/HTTP 合同测试 |
| **P5** | 质量、性能和发布门禁 | holdout 数据集、可复现报告 |
| **P6** | 企业发布和无人机参考场景 | 企业多租户、无人机救援示范 |
| **P7** | 动态世界记忆核心对象 | Observation/State/Hypothesis/Fact/Evidence/ContextPack |
| **P8** | 边缘融合和双时间 | Edge-first 融合、双时间、可重放 |
| **P9** | 场景扩展（具身、自动驾驶） | 合同复用、场景特定安全控制器 |

## P0: 边界冻结和基线

**目标**: 冻结合同，标记 mock，建立验收基线

**交付物**:
- 合同冻结文档（MemoryItem、EvidenceRef、ContextPack）
- 所有 mock/stub 显式标记
- 验收命令清单（Python/Rust/Compose/MCP）
- ADR 0001-0004 遵守检查

**验收门禁**:
- 合同测试通过
- 目标范围和非目标写入 ADR/PRD
- 工作区变更可追踪

**建议原子提交**:
- `docs(adr): freeze memory plane contracts and mark mocks`

**当前状态**: ✅ 基本完成

## P1: 真实权威存储和投影

**目标**: 让记忆真实持久化，重启不丢失，跨进程可读

### 第一生产化链路

拆分为 P1-01 至 P1-09 的原子提交：

| 提交 | 目标 | 交付物 | 验收门禁 |
|---|---|---|---|
| **P1-01** | PostgreSQL Schema | 迁移脚本：memory_items、evidence_refs、context_packs、audit_log | `alembic upgrade head` 成功，表结构正确 |
| **P1-02** | SQLAlchemy Repository | `PostgresMemoryRepository` 实现 `MemoryRepository` 接口 | 单测通过，CRUD 正确 |
| **P1-03** | Outbox 表 | outbox 表 + 事务性写入辅助函数 | remember 写入在同一事务中插入 memory + outbox |
| **P1-04** | Outbox Worker | 轮询 worker，读取 outbox，投递到 ES/Neo4j/Redis | 写入 memory 后，投影最终一致 |
| **P1-05** | Redis 热状态 | session/hot state 缓存，TTL 管理 | 重启后 hot state 仍可从 PostgreSQL 重建 |
| **P1-06** | Elasticsearch 投影 | 文本/向量索引、ACL 分区、RRF 支持 | remember 写入后，ES 可检索 |
| **P1-07** | Neo4j 投影 | 实体/关系/时间边投影 | remember 写入后，Neo4j 可查询关系 |
| **P1-08** | MinIO 原始证据 | 原始数据存储、稳定 source reference | evidence_ref 写入后，MinIO 可读取原始数据 |
| **P1-09** | FastAPI + Rust MCP 端到端 | Backend HTTP 接通 Rust MCP | 一个进程的 remember，另一进程的 query 可读 |

**验收门禁**:
- 进程重启后数据仍存在
- 跨进程 MCP 与 Backend 能读到同一条记忆
- 投影失败可重试和回放
- 删除传播可审计

### 故障注入测试（P1 完成后执行）

| 测试 | 方法 | 期望 |
|---|---|---|
| 重启恢复 | kill -9 PostgreSQL → 重启 | 数据不丢失，outbox 继续投递 |
| Outbox 重放 | 删除 ES 索引 → 触发 replay | ES 从 PostgreSQL 重建 |
| 幂等性 | 同一事件重复投递 | 不产生重复记忆 |
| 断网重连 | 断开 Redis → 重连 | 热状态从 PostgreSQL 重建 |
| DLQ | 模拟 ES 写入失败 | 事件进入 DLQ，可手动重试 |

**建议原子提交**: 每个 P1-01 至 P1-09 是一个独立提交，一个行为边界一个提交。

## P2: 实时摄取和 Outbox Worker 增强

**目标**: 支持多种输入源，保证幂等和可观测

**交付物**:
- 输入适配器：对话、工具调用、企业文档、Webhook、设备遥测
- Worker 增强：checkpoint、retry、DLQ、replay、projection lag 指标
- DLP 和 consent 处理
- 稳定 ID 和幂等性保证

**验收门禁**:
- 同一事件重复投递不产生重复记忆
- Worker 崩溃可恢复
- 删除/撤销事件最终到达所有投影
- 新鲜度指标可观测

**建议原子提交**:
- `feat(ingest): add input adapters for conversation and webhook`
- `feat(worker): add checkpoint, retry and DLQ support`

## P3: 混合检索和图增强

**目标**: 智能路由、多路召回、Claim 生命周期

**交付物**:
- Query Router：根据意图选择热状态、历史、语义、关键词和图检索
- 并行召回 + RRF/重排 + token budget + 证据去重
- Neo4j 真连接（替换降级实现）
- Claim 生命周期：extracted → reviewed → approved → revoked → superseded

**验收门禁**:
- 检索结果可追踪到 source/chunk/version
- 图扩展有关系类型和时间边
- 冲突查询能返回冲突证据和状态
- 证据不足时返回拒答

**建议原子提交**:
- `feat(query): add query router with intent classification`
- `feat(graph): connect Neo4j projection for real`

## P4: MCP 跨进程和客户端兼容

**目标**: 真实客户端可跨进程读写记忆

**交付物**:
- Rust `memory.*` 通过 Backend HTTP 调用真实 Memory Plane
- stdio 合同测试（Codex/Claude/Pi/TUI）
- Streamable HTTP 合同测试（session、超时、取消）
- MCP 输入输出合同、错误码、大小限制、字段白名单冻结
- **注意**: OAuth、Principal、scope 等字段只属于企业远程 Gateway/adapter 合同；本地 MCP stdio 和无权限 Memory Core 不要求这些字段

**验收门禁**:
- 同一 remember 由 MCP 写入后，另一进程 query 可读
- 真实客户端至少完成一套 stdio 和一套 HTTP 合同测试
- 管理/破坏性工具默认关闭，采用审批和审计

**建议原子提交**:
- `feat(mcp): connect Rust memory tools to Backend HTTP`
- `test(mcp): add stdio and HTTP contract tests`

## P5: 质量、性能和发布门禁

**目标**: 建立可复现的评测和发布门禁

**交付物**:
- holdout 数据集和独立 evaluator
- 必测维度：Evidence coverage、Refusal correctness、Freshness、Hot query latency、Retrieval quality、Idempotency、Projection health
- 可复现报告模板

**验收门禁**:
- Evidence coverage: 所有 `trusted` 结果 100% 有稳定证据引用
- Refusal correctness: 应拒答问题 precision/recall ≥ 0.85
- Hot query latency: P99 ≤ 100ms（不含 LLM，**发布门禁目标，尚未实测通过**）
- Projection health: ES/Neo4j/Redis lag 可观测

**对外口径**: "0 幻觉"表述为"证据约束和无证据拒答"，不能表述为对任意 LLM 输出的数学保证。

**建议原子提交**:
- `docs(eval): add holdout dataset manifest and evaluator contract`

## P6: 企业发布和无人机参考场景

**目标**: 企业多租户发布，无人机救援示范

**交付物**:
- 企业模式：租户、权限、ACL 预过滤（**预过滤位于企业包装层和存储查询边界，不进入无权限 Memory Core 的领域语义**）
- Local Edge：本地 stdio、离线模型、有限缓存
- 无人机救援：Memory Plane 只存遥测、地图、历史救援记录、风险事实、无人机状态和证据
- 每个动作结果重新写回 Memory，形成可追溯闭环

**验收门禁**:
- 企业模式通过 ACL 注入对抗集
- 无人机救援完成至少一个端到端示范场景

### Isaac Sim 山洪救援雏形

**执行步骤**:

1. **环境搭建**: 安装 Isaac Sim，配置山洪场景（地形、水流、障碍物）
2. **无人机模型**: 导入无人机 URDF，配置传感器（RGB-D、LiDAR、GPS）
3. **ROS 2 集成**: 建立 ROS 2 节点，发布传感器数据到 Memory Plane
4. **记忆写入**: 无人机观察（位置、温度、视觉检测）写入 WorldObservation
5. **状态融合**: 边缘节点融合多无人机观察，生成 WorldState
6. **Agent 规划**: Agent Plane 读取 WorldState，规划搜索路径
7. **安全兜底**: 安全控制器独立运行，碰撞避免不经过记忆平面
8. **证据追溯**: 每个决策可追溯到具体观察和证据
9. **事后回放**: 使用 MCAP 格式记录完整事件流，支持事后分析

**参考**: [Isaac Sim](https://docs.nvidia.com/learning/physical-ai/getting-started-with-isaac-sim/latest/index.html)

**建议原子提交**:
- `docs(demo): add Isaac Sim mountain flood rescue scenario spec`

## P7: 动态世界记忆核心对象

**目标**: 实现六类世界记忆对象

**交付物**:
- WorldObservation、WorldState、Hypothesis、VerifiedFact、EvidenceRef、ContextPack
- 状态迁移规则（显式函数，禁止静默覆盖）
- 双时间（observed_at + ingested_at）
- 时间有效性（valid_from + valid_to）
- 冲突检测（supersedes + contradicts）

**验收门禁**:
- Observation、Hypothesis 和 Verified Fact 不得混存或互相静默覆盖
- 状态迁移必须显式，必须保留审计轨迹
- 每条记录必须携带双时间和证据引用

**建议原子提交**:
- `feat(world-memory): add WorldObservation and WorldState models`
- `feat(world-memory): add Hypothesis and VerifiedFact with state transitions`

## P8: 边缘融合和双时间

**目标**: Edge-first 融合，双时间，可重放

**交付物**:
- 边缘融合节点：初步状态融合、冲突检测
- 双时间处理：传感器时钟漂移 vs 系统处理延迟
- 可重放设计：相同输入 + 相同版本 = 相同输出
- 降级策略：断网时本地工作

**验收门禁**:
- 边缘节点可在断网时本地融合
- 双时间可正确区分时钟漂移和系统延迟
- 相同输入和版本下结果可重放

**建议原子提交**:
- `feat(edge): add edge fusion node with dual timestamp handling`

## P9: 场景扩展（具身、自动驾驶）

**目标**: 合同复用，场景特定安全控制器

**交付物**:
- 具身智能：多模态传感器数据融合（视觉、力觉、位置）
- 自动驾驶：驾驶场景、交通状态、决策历史
- 场景特定安全控制器：不复用，独立实现
- CARLA 集成参考

**验收门禁**:
- 具身和自动驾驶复用世界记忆合同
- 安全控制器独立实现，不与记忆平面共享代码
- 硬实时控制路径不经过 LLM、RAG、Elasticsearch 或 Neo4j

**参考**: [CARLA sensors](https://carla.readthedocs.io/en/latest/ref_sensors/)

**建议原子提交**:
- `docs(scenarios): add embodied AI and autonomous driving contract reuse spec`

## 阶段依赖

```
P0 → P1 → P2 → P3 → P4 → P5 → P6
                      ↘
                       P7 → P8 → P9
```

P0-P6 是主链路，优先完成企业记忆和基础能力。P7-P9 是动态世界记忆扩展，可在 P4 完成后并行启动。

## 当前最大风险

1. **历史文档误读**: "Phase 完成"容易被误读为生产化完成，应以实际适配器和端到端测试为准
2. **参考存储不持久**: 当前 MCP 记忆工具是进程内参考实现，重启数据丢失
3. **授权链并存**: 传统企业授权和无权限记忆合同并存，必须保持边界
4. **GraphRAG 不完整**: Neo4j 服务存在，但图查询路径仍有降级实现
5. **Phase 2-A 不等于发布**: 当前提交只代表参考实现，不能把本地验证状态当成已发布版本

## 参考链接

以下公开资料提供了相关技术参考，不得声称这些外部产品的结果已在 esRAGPro 中复现：

- [NVIDIA Cosmos](https://docs.nvidia.com/cosmos/latest/introduction.html)
- [Isaac Sim](https://docs.nvidia.com/learning/physical-ai/getting-started-with-isaac-sim/latest/index.html)
- [CARLA sensors](https://carla.readthedocs.io/en/latest/ref_sensors/)
- [MCAP](https://mcap.dev/guides)
- [ROS 2 QoS](https://docs.ros.org/en/humble/Concepts/Intermediate/About-Quality-of-Service-Settings.html)
