# esRAGPro MCP V1 能力与支持矩阵

> 状态：目标合同，不代表当前 Rust 骨架已经实现  
> 规划基线：2026-07-10  
> 正式协议基线：[MCP 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)；实施时按 WP0 版本策略复核

## 1. “功能完整”的定义

V1 的完整性不是把 MCP 所有可选原语全部打开，而是：

1. 基础协议、声明的能力、错误与安全语义全部实现并通过合同测试。
2. 未实现的可选能力不在 initialize 中声明，不返回伪成功。
3. Tools、Resources、Prompts 共用同一领域合同、PrincipalContext、ACL 与审计链。
4. stdio 与 Streamable HTTP 在相同授权上下文下返回等价业务结果。
5. 每个发布物附本矩阵、锁定协议版本、客户端兼容结果与 Schema hash。

## 2. 协议与传输

| 能力 | V1 决策 | 验收要点 |
|---|---|---|
| JSON-RPC 与生命周期 | 必须 | initialize、版本协商、capability negotiation、initialized（若锁定版本要求）、ping、正常关闭与非法顺序 |
| stdio | 必须 | UTF-8、逐行消息、stdout 零非协议文本、日志仅 stderr |
| Streamable HTTP | Enterprise 必须 | 单一 `/mcp`、POST/GET、Origin/Host、HTTPS、协议版本头、内容类型、请求体上限 |
| 会话/无状态 | 按锁定正式版二选一 | 2025-11-25 使用共享 SessionStore；若未来正式版切为 stateless，禁止混用旧会话语义 |
| 取消 | 必须 | 显式取消终止可取消的下游 future；不可取消阶段返回真实 Job Resource |
| 超时 | 必须 | 客户端 deadline、服务端硬上限、稳定 TIMEOUT/partial/evidence-only 语义 |
| 进度 | 有条件支持 | 请求携带 progress token 时只报告阶段，不泄露未授权数量；长写操作返回 Job Resource |
| 游标分页 | 必须 | list/search/entity/path/catalog 使用签名不透明 cursor，绑定授权 revision、请求 hash、水位与过期时间 |
| 恢复与重投递 | HTTP 必须测试 | 只实现锁定正式版声明的 SSE/重连语义，不跨 stream 重放消息 |

规划日的 [2026-07-28 版本仍是候选版](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/)，不能直接成为生产目标。正式发布后，只有 Claude Code、Qwen 客户端和 Inspector 的兼容矩阵通过，才能通过 ADR 升级。

## 3. Server Capabilities

| Capability | V1 声明 | 说明 |
|---|---|---|
| `tools` | 是 | 默认 reader profile 固定 6 个只读 Tool；同一会话列表稳定 |
| `tools.listChanged` | 否 | Scope/profile 变化要求新建会话；未实现前不得发送通知 |
| `resources` | 是 | 1 个静态 Resource、8 个 Resource Templates，支持 list/templates/read |
| `resources.subscribe` | 否 | Job 状态通过有界轮询读取；持久订阅作为后续独立能力 |
| `resources.listChanged` | 否 | 授权或目录变化使 cursor 失效；未实现前不得宣称通知 |
| `prompts` | 是 | 固定 4 个 Prompt，参数有严格 Schema |
| `prompts.listChanged` | 否 | Prompt 版本随服务器版本发布，不做运行时热变更 |
| logging utility | 否 | 运维日志走 stderr/OTel；避免把敏感日志内容推给 MCP 客户端 |
| completion utility | 否 | 防止通过自动补全枚举无权实体、文档或知识库 |
| sampling | 不请求 | 回答由知识核心执行，不把模型调用委托给客户端 |
| elicitation | 不请求 | 缺少业务槽位时返回结构化 clarification，不弹出不可审计表单 |
| roots | 不请求 | 远程 Tool 不读取客户端或服务器任意路径 |
| experimental Tasks/Extensions | 否 | 只有在成为锁定正式扩展并通过独立 ADR 后启用 |

## 4. 默认只读 Tools

| Tool | 最小 Scope | 知识库字段 | 结果 |
|---|---|---|---|
| `kb.search_evidence` | `knowledge:read` | `knowledge_base_ids` | EvidencePacket、Resource Links、路由/降级、cursor |
| `kb.answer_with_evidence` | `knowledge:answer` | `knowledge_base_ids` | 逐声明核验答案、状态、引用与警告 |
| `graph.lookup_entity` | `graph:read` | `knowledge_base_id` | 规范实体与消歧候选 |
| `graph.expand` | `graph:read` | `knowledge_base_id` | 有界 active Claim 邻域、证据与 cursor |
| `graph.find_paths` | `graph:read` | `knowledge_base_id` | 有界时态路径、逐边证据与 cursor |
| `graph.verify_claim` | `graph:verify` | `knowledge_base_id` | supported/contradicted/insufficient 与证据 |

所有输入对象默认 `additionalProperties=false`，不得包含 tenant、subject、角色、任意 URL、任意路径、任意 ES DSL 或 Cypher。每个 Tool 同时提供 `inputSchema`、`outputSchema` 与 `structuredContent`。

外部 `evidence_id` 一律是稳定 UUIDv5；SHA-256 只用于 `content_hash`、`quote_sha256` 和完整性校验，不得把 hash 字符串作为第二种 evidence ID。

### 4.1 冻结输入字段

以下字段名与 `mcp-v1.schema.json` 必须逐字一致：

| Tool | 输入字段 |
|---|---|
| `kb.search_evidence` | `query`、`knowledge_base_ids`、可选 `filters`、`top_k`、`cursor`、`valid_at`、`graph_policy`、`budget_class` |
| `kb.answer_with_evidence` | `question`、`knowledge_base_ids`、可选 `filters`、`language`、`citation_style`、`answer_detail`、`valid_at`、`graph_policy`、`budget_class` |
| `graph.lookup_entity` | `knowledge_base_id`、`name_or_identifier`、可选 `entity_types`、`limit`、`cursor` |
| `graph.expand` | `knowledge_base_id`、`entity_id`、`direction`、可选 `predicates`、`max_depth`、`valid_at`、`limit`、`cursor` |
| `graph.find_paths` | `knowledge_base_id`、`from_entity_id`、`to_entity_id`、可选 `predicates`、`max_hops`、`valid_at`、`limit`、`cursor` |
| `graph.verify_claim` | `knowledge_base_id`、结构化 `claim`、可选 `valid_at`、`evidence_ids` |

`claim` 固定为 `subject`、`predicate`、`object`，以及可选 `qualifiers`、`polarity`；subject/object 使用 EntityRef 或带 datatype/unit 的 TypedLiteral，禁止把拆分后的 subject/predicate/object 作为另一套顶层参数。时间字段统一叫 `valid_at`，深度字段统一叫 `max_depth`/`max_hops`。

## 5. 独立管理 Profile

默认关闭、V1 仅允许在 Streamable HTTP 远程 profile 启用；stdio 固定 reader-only。它们不计入 reader profile 的 6 个 Tool：operator 为 reader + 前三个管理 Tool（9 个），admin 为 operator + 删除审批请求（10 个）。profile 在 initialize 前确定，同一会话不变化。WP0 的 `mcp-v1.schema.json` 必须一次性冻结三种 profile 和全部 10 个 Tool；WP7A 只能实现，不能原地修改已签字的 V1 Schema。

Profile 使用完整 Scope bundle，不做逐 Tool 裁剪：reader 要求 `knowledge:read + knowledge:answer + graph:read + graph:verify`；operator 还必须同时具备 `ingest:create + ingest:cancel + projection:rebuild`；admin 再要求 `document:delete:request`。服务端从“配置上限、租户策略上限、token 所含完整 bundle”选择最高完整档：管理 Scope 缺任一项就整体降到下一完整档；连 reader bundle 都不完整则 HTTP initialize 返回 `insufficient_scope`，不产生 5/7/8 等隐式第四档。`tools/call` 仍逐次校验 profile、Scope 与领域策略，防止绕过发现列表。

| Tool | Scope | 冻结输入 | 安全边界 |
|---|---|---|---|
| `admin.create_ingest_job` | `ingest:create` | `knowledge_base_id/source_ref/idempotency_key`，可选 `ingest_profile` | 只接收已授权 opaque source_ref |
| `admin.cancel_ingest_job` | `ingest:cancel` | `knowledge_base_id/job_id/reason/idempotency_key` | 只取消当前主体范围内的可取消任务 |
| `admin.request_projection_rebuild` | `projection:rebuild` | `knowledge_base_id/projections/reason/idempotency_key` | 只创建审批请求，不直接切生产投影 |
| `admin.request_document_deletion` | `document:delete:request` | `knowledge_base_id/document_id/expected_revision/reason/idempotency_key` | 只创建删除审批请求，不绕过保留策略 |

四个 Tool 的冻结结构化输出如下，且都含可轮询的 Job Resource URI：

| Tool | outputSchema 必需字段 |
|---|---|
| `admin.create_ingest_job` | `job_id`、`job_kind="ingest"`、`state`、`revision`、`resource_uri`、`idempotency_replayed`、`accepted_at` |
| `admin.cancel_ingest_job` | `job_id`、`job_kind="ingest"`、`state`、`revision`、`resource_uri`、`idempotency_replayed`、`updated_at` |
| `admin.request_projection_rebuild` | `job_id`、`job_kind="projection_rebuild"`、`state="approval_pending"`、`revision`、`resource_uri`、`idempotency_replayed`、`created_at` |
| `admin.request_document_deletion` | `job_id`、`job_kind="document_deletion"`、`state="approval_pending"`、`revision`、`resource_uri`、`idempotency_replayed`、`created_at` |

`resource_uri` 一律是 `esrag://kb/{kb_id}/jobs/{job_id}`。ManagementRequest 是 `Job` 的受治理子类型，外部只使用 `job_id`；审批、outbox 和 executor 更新同一 Job 的 revision/state，客户端从同一 Resource 轮询 `approval_pending -> rejected` 或 `approval_pending -> approved -> execution_queued -> executing -> succeeded|failed`。内部执行标识不得成为第二个外部资源 ID。

Claim 审核、ACL 修改、清空、任意评测和直接物理删除不通过 MCP 暴露。

`source_ref` 只能由 Core 受认证 REST 的 upload-session create → 流式 content → finalize 闭环签发，并绑定 tenant/subject/KB、内容 hash、大小和 TTL。`admin.create_ingest_job` 在同一事务中单次消费 ref、创建 Job 和 outbox；回滚不得消耗 ref，幂等重试返回同一 Job。

rebuild/deletion Tool 只创建 `approval_pending`。批准/拒绝使用不同 audience、要求 `management:approve` 的 Core Web/REST decision API，强制申请/审批职责分离和 `expected_revision`；批准与唯一 execution outbox 同事务。重建只在新 generation 对账通过后切 alias；删除批准立即建立 revocation barrier，再由幂等 executor 清理并最终置 `deleted`。MCP 不提供 decision Tool。

## 6. Resources

静态 Resource：

- `esrag://server/capabilities`

Resource Templates：

1. `esrag://kb/{kb_id}/documents/{doc_id}/versions/{version_id}`
2. `esrag://kb/{kb_id}/evidence/{evidence_id}`
3. `esrag://kb/{kb_id}/entities/{entity_id}`
4. `esrag://kb/{kb_id}/claims/{claim_id}`
5. `esrag://kb/{kb_id}/ontology/{schema_version}`
6. `esrag://kb/{kb_id}/jobs/{job_id}`
7. `esrag://kb/{kb_id}/catalog`
8. `esrag://kb/{kb_id}/stats`

`resources/list` 只列静态 Resource；`resources/templates/list` 返回 8 个模板。每次 `resources/read` 重新授权，URI 可猜测不等于可读。大内容分页或转为短期授权引用，不返回对象存储键和本地路径。

`jobs/{job_id}` 的 V1 JSON 内容固定为 `additionalProperties=false` 的 `JobResource`：必需 `schema_version/job_id/job_kind/state/revision/resource_uri/target/approval/execution/created_at/updated_at`。`job_kind` 为 `ingest|projection_rebuild|document_deletion`；`target` 必含 `knowledge_base_id`，按 kind 可含 `document_id` 或 `projections`；`approval` 必含 `required/state`，可含 `decided_at`；`execution` 必含 `state/attempt`，可含 `last_error_code/started_at/finished_at/projection_generation`。理由、主体标识、token、对象键与正文不通过该 Resource 暴露。审批和 executor 每次成功状态转换都递增 revision；URI 在整个生命周期不变。

## 7. Prompts

| Prompt | 冻结输入字段 | 约束 |
|---|---|---|
| `answer-with-citations` | `question`、`knowledge_base_ids`；可选 `language/citation_style/answer_detail/valid_at` | 指示客户端调用只读 Tool 获取 EvidencePacket，再逐声明引用 |
| `compare-evidence` | `topic`、`knowledge_base_ids`、结构化 `left_ref/right_ref`；可选 `valid_at/language` | 显式区分一致、冲突、时间和版本 |
| `trace-requirement` | `requirement`、`knowledge_base_ids`；可选 `target_artifact/valid_at/language` | 从原文要求追到证据、active Claim 或实现工件 |
| `verify-claim` | `knowledge_base_id`、结构化 `claim`；可选 `valid_at/language` | 指示客户端同时寻找支持与反驳证据；无反驳不等于已证实 |

`left_ref/right_ref` 固定为 `{label, document_id?, version_id?, entity_id?}`：document_id 与 entity_id 必须且只能出现一个，version_id 只能与 document_id 同时出现。Prompt 不接收 `evidence_ids` 作为另一套执行接口，不读取 Resource，也不自动调用 Tool；`prompts/get` 只返回消息模板。Prompt 不携带身份、密钥或隐藏授权；后续 Tool/Resource 调用独立鉴权，调用 Prompt 不扩大 Scope。

## 8. 结果与错误合同

- 业务状态：`ok|partial|no_match|refused`。no-match/refused 不是协议错误。
- ClaimAssessment：`supported|contradicted|insufficient`。
- 回答状态：`reliable|partial|uncertain|refused`；verifier 失败时不返回未经核验的生成答案。
- 系统错误：认证、授权、参数、未找到、冲突、限流、依赖不可用、超时、取消、内部错误。
- 每个错误带 `request_id`、`retryable` 和可选 `retry_after_ms`，不含 token、栈、SQL、对象键或未授权计数。
- Resource not found 的 JSON-RPC code 必须来自 WP0 锁定协议版本，不能跨版本照抄。

## 9. OAuth 与信任边界

- HTTP Gateway 是 OAuth Protected Resource；验证 issuer、audience、expiry、签名与最小 scope。
- Protected Resource Metadata 是未认证、可缓存的资源级文档，始终公布八个 V1 Scope 的固定并集：`knowledge:read`、`knowledge:answer`、`graph:read`、`graph:verify`、`ingest:create`、`ingest:cancel`、`projection:rebuild`、`document:delete:request`；不得按 tenant、token 或 profile 变化。metadata 不授予权限，实际 profile 按完整 Scope bundle 选择且 `tools/call` 逐次鉴权。Core Web/REST 的 `management:approve` 属于不同 audience，不在 `/mcp` 的此集合中。
- stdio 不运行 HTTP OAuth 流程，凭据来自受控启动器、受限文件或系统密钥链。
- 禁止 token passthrough。下游 Core API 使用不同 audience 的短期 exchange/OBO/签名委托凭据。
- 普通 bearer 的 jti 不是一次性请求标识；强重放防护使用经 ADR 验证的 DPoP 或 mTLS。
- PrincipalContext 和 access_partition_ids 只由服务端构造；所有召回先执行 ACL 与 revocation barrier。

## 10. 发布验收矩阵

| 维度 | 必须通过 |
|---|---|
| 生命周期 | 正常、重复、乱序、未知版本、ping、关闭 |
| 发现 | reader/operator/admin 分别为 6/9/10 Tools，缺 Scope bundle 降档/拒绝且绝无 5/7/8；1 static Resource、8 Templates、4 Prompts，顺序稳定 |
| 调用 | 每个 Tool 正常、边界、错误、取消、超时、分页 |
| 安全 | 固定八 Scope metadata 不授予权限；错 issuer/audience/scope、跨租户、越权 URI、cursor 失效、注入、SSRF/路径/Cypher/DSL |
| stdio | stdout 100% 合法 MCP 消息，stderr 日志不含秘密 |
| HTTP | Origin/Host、内容类型、协议头、锁定 lifecycle、重连/取消、限流、下游 token 隔离 |
| 兼容 | 锁定版 Claude Code、Qwen 调用环境、MCP Inspector |
| 声明真实性 | initialize 只声明已实现并通过测试的 capability |

任何一项未通过时，该 capability 不得在发布物中声明为可用。
