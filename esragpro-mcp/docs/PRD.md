---
workflowType: prd
workflow: edit
documentVersion: 1.0
documentStatus: planning-baseline
lastEdited: 2026-07-10
stepsCompleted:
  - discovery
  - review
  - edit
decisionReview: docs/DECISION-REVIEW.md
---

# esRAGPro 企业可信知识核心与 MCP Gateway 产品需求文档

## 1. 执行摘要

esRAGPro 面向需要私有知识问答、证据追溯、权限隔离和知识治理的企业。产品采用“一个知识核心、两种交付形态”的路线：先交付 Enterprise Edition，再在核心领域契约稳定后交付 Local Edge Edition。

Enterprise Edition 负责文档版本、权限、摄取任务、候选事实、审核记录和审计记录等权威数据。PostgreSQL 与对象存储构成系统记录；Elasticsearch 与 Neo4j 是可重建的查询投影。任何索引或图谱损坏均不得改变权威记录，也不得阻止从权威记录重建投影。

MCP Gateway 是知识核心的安全协议适配层。它向 Claude Code、Qwen、Cursor 等智能体暴露受控的 Tools、Resources 和 Prompts，统一执行身份传递、授权、限流、审计、内容裁剪和结构化输出；它不拥有另一套文档、向量或图谱事实，不承担企业数据真源职责。

产品不承诺未经评测的“幻觉率”或通用性能数字。发布结论只来自冻结数据集、固定容量档位和可复现测试报告。

## 2. 产品决策与边界

### 2.1 已确认决策

| 决策 | 产品约束 |
|---|---|
| 一个知识核心 | Web、REST API、MCP 和未来 Local Edge 共享领域对象、证据契约、权限语义与评测口径 |
| Enterprise 先行 | 多租户、ACL、审计和可信数据生命周期先稳定；Local Edge 不与 Enterprise 并行重写 |
| 权威记录与投影分离 | PostgreSQL/对象存储保存权威记录；Elasticsearch/Neo4j 只保存可重建投影 |
| MCP 是安全适配层 | MCP 不直接操作底层存储，不绕过领域服务，不保存独立事实副本 |
| 证据优先 | 检索、图谱路径、声明验证和答案均返回可解析的证据标识与来源版本 |
| 图谱按需增强 | BM25、向量及条件触发的图检索并行召回；融合后重排，不固定串行执行“五层检索” |
| 图谱不是 LLM 输出缓存 | LLM 只产生候选实体与候选事实；校验、消歧、审核和发布后才能进入已发布图谱 |

### 2.2 两种交付形态

| 形态 | 目标用户 | 数据与部署 | MCP 传输 | 优先级 |
|---|---|---|---|---|
| Enterprise Edition | 企业知识工作者、IT、安全、知识管理员、审计人员 | PostgreSQL、对象存储、Elasticsearch、Neo4j；支持高可用与多租户 | 远程 Streamable HTTP；受控本地运维可用 stdio | 首要交付 |
| Local Edge Edition | 单人、隔离网、现场设备和离线工作站 | 嵌入式存储；复用同一领域契约、MCP Schema 与评测集 | stdio | Enterprise 核心契约稳定后启动 |

Local Edge 可采用 Rust 单二进制，但其存储实现不得反向定义 Enterprise 的领域模型。两个版本不承诺物理存储兼容，只承诺经过版本管理的领域契约和导入导出格式兼容。

## 3. 产品目标与非目标

### 3.1 产品目标

| 编号 | 目标 | 验收方向 |
|---|---|---|
| G-01 | 智能体在权限范围内获得可追溯的企业知识 | 每条证据可解析到原文、文档版本和位置 |
| G-02 | 企业知识在摄取、检索、图谱和删除全生命周期保持权限一致 | 权限过滤发生在召回前，跨租户泄漏测试为零 |
| G-03 | 复杂关系问题可按需使用可信图谱 | 图谱事实具备来源、状态、版本和有效时间；路径不被当作结论 |
| G-04 | MCP 客户端获得完整且稳定的协议体验 | Tools、Resources、Prompts、分页、取消、错误和审计均通过兼容测试 |
| G-05 | 搜索与回答质量可重复评估 | 固定数据集、基线、阈值、测试配置和报告可复现 |
| G-06 | 索引与图谱可从权威记录恢复 | 重建演练不丢失文档版本、ACL、审核结果或证据关系 |

### 3.2 非目标

- Enterprise V1 不以“零外部服务”或单二进制作为目标。
- MCP Gateway 不建立独立的 LanceDB、SQLite 或其他企业知识数据面。
- 不把模型生成内容、推理路径或相似度分数自动认定为企业事实。
- 不向默认智能体工具集开放上传本地路径、直接删除文档、清空图谱、修改 ACL 或运行全库评测。
- 不在 PRD 中锁死具体模型、重排器或融合算法；实现可替换，但必须满足数据契约与质量门禁。
- 不把医疗、法律、金融专业结论作为通用版产品能力；进入相应领域前必须增加专门合规、风险和人工升级策略。
- 不在 Enterprise GA 前并行开发 Local Edge 的第二套领域逻辑。
- 不使用没有冻结评测集、测试方法和报告支撑的“幻觉率”“快若干倍”等营销结论。

## 4. 用户、用例与用户旅程

### 4.1 用户与核心用例

| 用户 | 核心任务 | 成功条件 |
|---|---|---|
| 知识工作者 | 让智能体查询制度、产品、项目或技术资料 | 答案说明可靠状态，引用可打开且只包含其有权查看的内容 |
| 智能体开发者 | 在 Claude Code 或 Qwen 中调用企业知识 | Schema 稳定、错误可处理、结果结构化、无需理解底层存储 |
| 知识管理员 | 上传和版本化文档，处理失败任务，维护知识范围 | 原文件、版本、元数据和任务状态完整；重复摄取幂等 |
| 知识审核员 | 审核候选实体、关系、冲突和失效事实 | 每个决策有证据、操作者、时间和原因；可撤销或替代 |
| 安全管理员 | 管理租户、知识库、角色、策略和模型出站范围 | 最小权限可验证，敏感操作有审批与审计 |
| 审计人员 | 复核答案、来源、权限决策和数据变更 | 通过请求标识串联检索、答案、证据、策略和审计事件 |
| 平台运维 | 监控服务、处理积压、重建投影和执行恢复 | 不修改权威记录即可恢复 ES/Neo4j，恢复结果通过一致性校验 |

### 4.2 关键用户旅程

1. **证据问答**：知识工作者在智能体中提问；Gateway 验证身份与范围；知识核心自动路由检索；返回答案、声明级引用、可靠状态和警告；用户打开证据 Resource 查看原文。
2. **声明核验**：用户提交一条具体声明；系统同时检索支持与反驳证据；返回 `supported`、`contradicted` 或 `insufficient`，并说明采用的证据版本。
3. **文档摄取与发布**：管理员通过管理面上传文件；系统保存原件与校验和、建立版本并异步解析；ES 投影可用后文档进入可检索状态；候选图谱事实继续进入审核队列。
4. **图谱审核**：审核员查看候选事实、实体别名、冲突、来源片段和有效时间；批准、拒绝、合并或标记替代；只有已发布事实进入默认图检索。
5. **权限变更与删除**：管理员修改授权或发起删除；权威记录先产生版本化策略或删除标记；投影异步传播；传播期间查询采用更严格的权威策略，禁止旧索引扩大访问。
6. **投影恢复**：运维从 PostgreSQL、对象存储和已发布事实重建 ES/Neo4j；校验数量、校验和、ACL 和证据解析率后切换流量。

## 5. 功能范围与需求

### 5.1 权威数据与摄取

| 编号 | 需求 |
|---|---|
| FR-DATA-01 | 每份文档具有不可复用的文档标识；每次内容变化创建新版本，保留来源、校验和、解析器版本、创建者和时间。 |
| FR-DATA-02 | 原始文件与派生文本可追溯；证据记录包含文档版本、页码或字符范围、块标识和内容校验和。 |
| FR-DATA-03 | 摄取以异步任务执行，支持幂等键、状态、进度、重试、取消、错误分类和逐文件结果。 |
| FR-DATA-04 | 支持 PDF、DOCX、TXT、Markdown；扫描件 OCR 作为显式能力标记，失败不得伪装成空文档成功。 |
| FR-DATA-05 | PostgreSQL 保存文档元数据、版本、ACL、任务、候选事实、审核和审计；对象存储保存原件及需要长期保留的派生物。 |
| FR-DATA-06 | ES 与 Neo4j 投影包含权威记录版本和投影版本；系统可检测滞后、重复、孤儿和校验和不一致。 |
| FR-DATA-07 | 权限收紧和删除采用拒绝优先策略；旧投影未更新时不得继续暴露已撤销内容。 |

### 5.2 检索、重排与证据包

统一证据包类型名固定为 `EvidencePacket`；不得另建近似命名、搜索结果 DTO 或图结果 DTO 作为不兼容的外部合同。

| 编号 | 需求 |
|---|---|
| FR-RET-01 | 查询路由依据问题类型、过滤条件、租户策略和可用性自动选择 BM25、向量和图检索；调用者可请求策略级别，但不能绕过安全策略。 |
| FR-RET-02 | BM25 与向量召回并行执行；图检索仅在实体、关系、时序、依赖或多跳意图明确时触发。 |
| FR-RET-03 | 多路结果经可配置融合与重排生成统一排名；响应记录实际路由、模型/索引版本和降级原因。 |
| FR-RET-04 | 租户、知识库、文档、标签、时间和用户 ACL 在召回查询中预过滤，不允许先召回后在应用层裁剪。 |
| FR-RET-05 | 每个结果返回 `evidence_id`、引用文本、来源标题、文档版本、位置、得分分解和可访问 Resource URI。 |
| FR-RET-06 | 无命中、索引滞后、图不可用、重排超时和部分结果使用不同的结构化状态，不以空成功掩盖故障。 |
| FR-RET-07 | 支持游标分页和稳定排序；同一查询快照的后续页不得因实时写入而重复或跳过结果。 |

### 5.3 可信知识图谱

| 编号 | 需求 |
|---|---|
| FR-KG-01 | 模型抽取只创建 `ClaimCandidate`，其状态固定为 `extracted`、`validated`、`review_required`、`approved` 或 `rejected`；候选获批后才创建或修订权威 `Claim`。 |
| FR-KG-02 | 候选事实包含主语、关系、宾语、证据、来源版本、抽取器版本、置信信息和创建时间；缺失证据不得发布。 |
| FR-KG-03 | 实体支持规范名、别名、类型、本体版本和消歧记录；合并实体必须可审计且可回滚。 |
| FR-KG-04 | 事实支持有效起止时间、记录时间和替代链；冲突事实可并存但必须标记冲突范围与处理状态。 |
| FR-KG-05 | 权威 `Claim` 的发布状态固定为 `active`、`superseded`、`revoked` 或 `expired`；默认图检索只使用调用者有权访问、状态为 `active` 且在有效期内的 Claim。 |
| FR-KG-06 | 图路径的每条边均链接证据；系统将路径标记为“检索到的关系链”，不得将路径本身标记为已证明结论。 |
| FR-KG-07 | 支持实体查询、邻居扩展、受限多跳路径、声明核验、冲突发现和投影重建。 |

### 5.4 回答与可信状态

| 编号 | 需求 |
|---|---|
| FR-ANS-01 | 回答生成只接收经过授权的证据包；检索内容被视为不可信数据，不得作为工具调用或系统指令执行。 |
| FR-ANS-02 | 系统把回答拆为可核验声明，并将每条声明映射到支持、反驳或不足的证据集合。 |
| FR-ANS-03 | 回答状态统一为 `reliable`、`partial`、`uncertain`、`refused`；状态由可版本化规则产生并返回判定依据。 |
| FR-ANS-04 | 证据不足或冲突未解决时，系统缩小回答范围、显示警告或拒绝，不用模型流畅度替代证据。 |
| FR-ANS-05 | 所有引用在响应时校验权限和可解析性；无法解析的引用不得出现在最终答案。 |

### 5.5 管理与运维

| 编号 | 需求 |
|---|---|
| FR-OPS-01 | 管理面支持知识库、文档、版本、摄取任务、候选事实、审核队列和投影状态管理。 |
| FR-OPS-02 | 删除、清空、批量权限变更和投影切换采用显式确认或审批；操作记录影响范围与执行结果。 |
| FR-OPS-03 | 提供健康、就绪、依赖、队列积压、投影滞后、错误率、配额和审计导出能力。 |
| FR-OPS-04 | 评测作为离线或 CI 工作流运行；评测数据、提示词、模型、索引和结果均版本化，不在默认智能体工具集中开放。 |
| FR-OPS-05 | 支持按租户和知识库重建 ES/Neo4j，重建完成前不覆盖可用投影；切换失败可回退。 |
| FR-OPS-06 | 管理摄取的源文件通过受认证 upload-session 创建、流式上传、hash/size 校验和 finalize 签发一次性 `source_ref`；跨主体、跨知识库、过期和重放均拒绝。 |
| FR-OPS-07 | 重建与删除请求具备申请、独立审批、拒绝、乐观并发、outbox 执行、失败重试和最终审计闭环；申请人不得审批自己的请求。 |

## 6. MCP 产品契约

### 6.1 能力分层

| 配置档 | 使用者 | 默认能力 |
|---|---|---|
| `knowledge-reader` | 普通知识工作者及其智能体 | 证据检索、证据回答、实体查询、图扩展、路径查询、声明核验、只读 Resources 和 Prompts |
| `knowledge-operator` | 受控运维智能体 | 仅远程 profile；在 reader 基础上增加创建摄取任务、取消任务和请求投影重建；默认关闭，按租户启用 |
| `knowledge-admin` | 管理员 | 仅远程 profile；管理面仍以 Web/REST 为主；MCP 只允许发起删除审批请求，不提供直接清空、越权授权或不可逆删除 |

Profile 采用完整 Scope bundle，而非逐 Tool 裁剪：reader 需要四个只读 Scope，operator 再需要三个运维 Scope，admin 再需要删除请求 Scope。配置上限、租户策略和 token 共同决定最高完整档；缺少任一管理 Scope 时整体降到下一档，reader bundle 不完整则拒绝远程初始化。因此发布合同只有 6/9/10 三种 Tool 数，每次调用仍重新鉴权。

### 6.2 默认只读 Tools

所有 Tool 使用严格输入 Schema、`outputSchema` 和 `structuredContent`；文本内容只作为兼容回退。服务端从认证上下文取得租户和主体，禁止客户端通过参数冒充。

| Tool | 关键输入 | 结构化输出 | Scope |
|---|---|---|---|
| `kb.search_evidence` | `query`、`knowledge_base_ids`，可选 `filters/top_k/cursor/valid_at/graph_policy/budget_class` | `query_id`、`evidence[]`、实际路由、降级、警告、下一游标 | `knowledge:read` |
| `kb.answer_with_evidence` | `question`、`knowledge_base_ids`，可选 `filters/language/citation_style/answer_detail/valid_at/graph_policy/budget_class` | `answer`、`status`、`claims[]`、`citations[]`、`evidence[]`、警告、`request_id` | `knowledge:answer` |
| `graph.lookup_entity` | `knowledge_base_id`、`name_or_identifier`，可选 `entity_types/limit/cursor` | 规范实体、别名、类型、本体版本、可见事实摘要、下一游标 | `graph:read` |
| `graph.expand` | `knowledge_base_id`、`entity_id`、`direction`，可选 `predicates/max_depth/valid_at/limit/cursor` | 节点、边、每条边的证据 URI、截断信息、下一游标 | `graph:read` |
| `graph.find_paths` | `knowledge_base_id`、`from_entity_id`、`to_entity_id`，可选 `predicates/max_hops/valid_at/limit/cursor` | 排名路径、边级证据、冲突、“非结论”标记和下一游标 | `graph:read` |
| `graph.verify_claim` | `knowledge_base_id`、结构化 `claim`，可选 `valid_at/evidence_ids` | `supported`/`contradicted`/`insufficient`、支持与反驳证据、规则版本 | `graph:verify` |

### 6.3 受控运维 Tools

| Tool | 行为约束 | Scope | outputSchema 必需字段 |
|---|---|---|---|
| `admin.create_ingest_job` | 输入 `knowledge_base_id/source_ref/idempotency_key` 和可选 `ingest_profile`；source_ref 已独立授权且不是路径/URL | `ingest:create` | `job_id/job_kind="ingest"/state/revision/resource_uri/idempotency_replayed/accepted_at` |
| `admin.cancel_ingest_job` | 输入 `knowledge_base_id/job_id/reason/idempotency_key`；只取消可取消状态任务，保留审计与中间结果 | `ingest:cancel` | `job_id/job_kind="ingest"/state/revision/resource_uri/idempotency_replayed/updated_at` |
| `admin.request_projection_rebuild` | 输入 `knowledge_base_id/projections/reason/idempotency_key`；只发起受限重建审批请求 | `projection:rebuild` | `job_id/job_kind="projection_rebuild"/state="approval_pending"/revision/resource_uri/idempotency_replayed/created_at` |
| `admin.request_document_deletion` | 输入 `knowledge_base_id/document_id/expected_revision/reason/idempotency_key`；只创建删除审批请求 | `document:delete:request` | `job_id/job_kind="document_deletion"/state="approval_pending"/revision/resource_uri/idempotency_replayed/created_at` |

WP0 必须在 `mcp-v1.schema.json` 中一次性冻结 reader/operator/admin 三种 profile 和全部 10 个 Tool；后续工作包只实现，不得原地扩写已签字 V1 合同。`source_ref` 必须由 Core 受认证 REST 的上传会话 create/content/finalize 流程签发，绑定 tenant/subject/KB、内容 hash、大小和 TTL；创建 Job 与单次消费 ref 在同一事务完成。重建/删除批准不经 MCP：不同审批主体在 Core Web/REST 以 `management:approve`、`expected_revision` 和理由作出不可改写 decision，批准与 execution outbox 原子提交。重建经新 generation 对账后切换；删除批准立即建立撤权屏障（revocation barrier），再异步收敛投影、缓存和 blob。

四个管理 Tool 都返回 `job_id/job_kind/state/revision/resource_uri/idempotency_replayed` 及对应时间字段；`resource_uri` 必须是 `esrag://kb/{kb_id}/jobs/{job_id}`。审批请求是 Job 子类型，批准、拒绝和执行沿同一 Job revision/state 推进，客户端用同一 Resource 轮询，不引入第二套 ManagementRequest URI。

### 6.4 Resources 与 Resource Templates

| 类型与 URI | 内容 | 权限与限制 |
|---|---|---|
| 静态 Resource：`esrag://server/capabilities` | 协议版本、Schema 版本、可用配置档和限制 | 已认证主体可读，不泄露内部拓扑或密钥 |
| `esrag://kb/{kb_id}/documents/{doc_id}/versions/{version_id}` | 文档版本元数据及获准返回的原文范围 | 读取时再次鉴权；大内容分页或返回受时效下载引用 |
| `esrag://kb/{kb_id}/evidence/{evidence_id}` | 引用文本、位置、校验和、来源版本和上下文窗口 | 只返回当前仍有权限的证据 |
| `esrag://kb/{kb_id}/entities/{entity_id}` | 实体、本体类型、别名和可见事实 | 隐藏无权事实，不通过节点存在性泄漏信息 |
| `esrag://kb/{kb_id}/claims/{claim_id}` | 当前可见的权威 Claim、状态、时态、修订和证据 | 只读取 `active` 或为审计主体明确获准的历史状态 |
| `esrag://kb/{kb_id}/ontology/{schema_version}` | 实体、关系、约束和字段定义 | 按知识库策略开放 |
| `esrag://kb/{kb_id}/jobs/{job_id}` | 摄取、投影重建或文档删除的申请、审批、执行、进度、错误和结果摘要 | 仅任务发起者、审批者、运维或管理员按策略可读 |
| `esrag://kb/{kb_id}/catalog` | 当前主体可见的文档目录 | 使用签名游标分页；统计和条目均基于授权后集合 |
| `esrag://kb/{kb_id}/stats` | 当前主体可见范围内的知识库统计 | 不返回全租户总量或可形成存在性侧信道的数据 |

V1 能力清单固定为 **1 个静态 Resource + 8 个 Resource Templates**。实现和测试必须分别验证 `resources/list`、`resources/templates/list` 与 `resources/read`，不得把静态 Resource 计入模板数量。

`get_document`、`list_documents` 和 `stats` 不再作为宽泛 Tool；文档与证据通过 Resources 读取，列表与统计通过受权管理 API 或专用资源视图提供。

### 6.5 Prompts

| Prompt | 参数 | 约束 |
|---|---|---|
| `answer-with-citations` | `question`、`knowledge_base_ids`，可选 `language/citation_style/answer_detail/valid_at` | 指示客户端先调用只读 Tool 检索证据，再生成声明级引用；无证据时拒绝 |
| `compare-evidence` | `topic`、`knowledge_base_ids`、结构化 `left_ref/right_ref`，可选 `valid_at/language` | 区分共同点、差异、冲突与缺失证据 |
| `trace-requirement` | `requirement`、`knowledge_base_ids`，可选 `target_artifact/valid_at/language` | 输出从原文到事实、答案或实现引用的追踪链 |
| `verify-claim` | `knowledge_base_id`、结构化 `claim`，可选 `valid_at/language` | 指示客户端同时寻找支持与反驳材料，不把无反驳等同于已证实 |

Prompts 是可发现、可版本化的消息模板；`prompts/get` 不读取 evidence、不自动执行 Tool，也不包含密钥、租户身份或绕过授权的指令。客户端按模板发起的每个 Tool/Resource 调用仍独立鉴权；调用 Prompt 不授予额外 Scope。

### 6.6 协议完整性要求

| 编号 | 要求 |
|---|---|
| MCP-01 | 实现协议初始化与版本协商，并完整支持 `tools/list`、`resources/list`、资源模板发现、`prompts/list` 和相应读取/调用。 |
| MCP-02 | Enterprise 远程连接使用 Streamable HTTP；Local Edge 使用 stdio。旧式独立 SSE 不作为目标传输。 |
| MCP-03 | stdio 模式的标准输出只承载 MCP 帧，日志写入标准错误或结构化日志出口。 |
| MCP-04 | 所有列表支持游标分页；长操作支持取消、超时、进度或任务 Resource；重试语义和幂等性明确。 |
| MCP-05 | 错误至少区分认证、授权、参数、资源不存在、冲突、限流、依赖不可用、超时和内部错误，并返回 `request_id`。 |
| MCP-06 | Tool 返回大小受限；超限内容改为 Resource Link，不把整篇文档塞入 Tool 响应。 |
| MCP-07 | 客户端断开不会让不可控写操作继续；已进入不可取消阶段时返回真实状态和任务 URI。 |
| MCP-08 | 协议兼容测试覆盖声明支持的 Claude Code、Qwen 调用环境及 MCP Inspector；每个发布物附能力清单。 |
| MCP-09 | `initialize` 只声明已经实现并通过合同测试的能力；V1 对 resources subscription/listChanged、tools/prompts listChanged、logging、completion、sampling、elicitation、roots 和实验性 Tasks 的取舍以 `MCP-V1-CAPABILITY-MATRIX.md` 为准，未声明即不得返回伪成功。 |
| MCP-10 | 规划日正式基线为 MCP 2025-11-25；WP0 在实施日复核最新正式版与客户端矩阵。候选版不用于生产；破坏性升级必须有 ADR、版本化 fixture 与兼容/回滚证据。 |

## 7. 企业安全与治理需求

| 编号 | 需求 |
|---|---|
| SEC-01 | 远程 MCP 使用企业身份联合或受管服务身份；服务端校验签发者、受众、有效期和 Scope，不信任客户端提交的主体字段。 |
| SEC-02 | 授权至少覆盖租户、知识库、文档和操作；拒绝优先，权限在检索、图扩展、Resource 读取和引用解析各阶段执行。 |
| SEC-03 | 任一缓存、ES 查询、Neo4j 查询和重排输入均携带安全边界；缓存键包含租户、主体权限版本和知识范围。 |
| SEC-04 | 文档内容、OCR 文本、图谱字段和检索片段均视为不可信输入；其中的提示词或工具指令不得改变系统策略。 |
| SEC-05 | 上传执行类型、大小、压缩炸弹、恶意文件和路径校验；远程 MCP 不接受任意服务端路径。 |
| SEC-06 | 密钥、令牌、个人信息和受限内容在日志、追踪、错误和模型请求中按策略裁剪；模型出站受租户数据驻留策略控制。 |
| SEC-07 | 数据在传输和静态存储中加密；密钥轮换不要求重建领域标识，备份与导出沿用同等保护。 |
| SEC-08 | 删除、保留、法律保全和版本替代在权威记录中执行，并可证明已传播到缓存与投影。 |
| SEC-09 | 每次 Tool、Resource、Prompt 和管理操作记录主体、租户、知识范围、策略版本、结果、延迟与请求标识；审计内容不记录不必要的原文。 |
| SEC-10 | 高风险管理操作需要显式确认或审批；默认 MCP 配置档无破坏性权限。 |
| SEC-11 | 限流和配额至少按租户、主体、Tool 和成本维度执行，不能因单一租户耗尽共享资源。 |
| SEC-12 | 发布前执行跨租户、越权 Resource URI、缓存污染、提示词注入、SSRF、路径穿越和审计绕过测试。 |
| SEC-13 | OAuth Protected Resource Metadata 公布服务端支持 Scope 的固定并集，不按租户或 token 动态生成；metadata 不授予权限，Tool 发现和调用都必须重新授权。 |

## 8. SLO、评测与质量门禁

以下数值是目标发布门槛，不代表当前代码已经达到。每次报告必须附硬件、语料规模、并发、模型、索引版本、预热条件和测量区间；缺少这些信息的性能结论无效。

### 8.1 Enterprise GA 服务目标

| 指标 | 目标 | 测量边界 |
|---|---|---|
| 只读 MCP 可用性 | 月度不低于 99.9% | 排除已公告维护；由外部探针测量初始化、发现和一次只读调用 |
| `kb.search_evidence` 延迟 | p95 不高于 2 秒，p99 不高于 5 秒 | 在已签署容量档位内，含授权、检索和重排，不含客户端网络 |
| 图只读 Tool 延迟 | p95 不高于 2 秒 | 最大深度 2、响应上限生效、在已签署容量档位内 |
| Gateway 额外开销 | p95 不高于 300 毫秒 | 不含外部 LLM 和知识核心处理时间 |
| 摄取任务成功率 | 有效且受支持文件不低于 99% | 按文件计；恶意、损坏、加密且未供密钥的文件单独统计 |
| 投影新鲜度 | 95% 的已发布变更在 5 分钟内可检索 | 同时报告最坏积压与失败重试 |
| 恢复目标 | RPO 不高于 15 分钟，RTO 不高于 4 小时 | 只针对权威记录；通过恢复与投影重建演练验证 |

### 8.2 检索与可信质量门禁

| 门禁 | 发布条件 |
|---|---|
| 数据集有效性 | 每个主要领域至少 500 条人工审定检索问题、200 条多跳问题、200 条无答案或冲突问题；训练调参集与最终验收集隔离。 |
| 检索增益 | Recall@20 相对同数据集 BM25 基线提升至少 5 个百分点；任何关键权限或业务分组下降不得超过 2 个百分点。 |
| 图谱价值 | 多跳问题的正确证据命中率相对非图方案提升至少 5 个百分点；简单事实集不得下降超过 2 个百分点。 |
| 引用完整性 | 最终答案中 100% 的 `evidence_id` 可解析、权限有效且校验和匹配；不可解析引用为发布阻断项。 |
| 声明支撑 | 人工验收集中的声明级证据支撑精确率不低于 95%；每个不达标声明保留错误类别。 |
| 拒绝质量 | 无答案与冲突集的拒绝 F1 不低于 0.85；同时报告误拒和漏拒，不用单一“幻觉率”替代。 |
| 图谱发布质量 | 已发布事实 100% 具有证据与审核记录；抽样实体消歧准确率不低于 95%。 |
| ACL 隔离 | 跨租户、跨知识库、撤权后访问和缓存复用测试泄漏数为 0。 |

### 8.3 发布门禁

| 门禁 | 必须提供的证据 |
|---|---|
| 契约门禁 | 领域 Schema、MCP Schema 和迁移兼容测试通过，无未声明破坏性变更 |
| 代码门禁 | 构建、单元、集成、端到端和静态检查通过；关键路径有失败与降级测试 |
| 协议门禁 | stdio/Streamable HTTP 按支持矩阵完成初始化、发现、调用、分页、取消、错误和断线测试 |
| 安全门禁 | 无未处置的严重或高危问题；ACL 与提示词注入对抗集全部通过 |
| 数据门禁 | 权威记录、ES、Neo4j、缓存的创建、更新、撤权、删除和重建一致性测试通过 |
| 质量门禁 | 冻结验收集达到 8.2 全部阈值，并保留可复现报告 |
| 运维门禁 | 监控、告警、备份恢复、投影重建、回滚和审计导出演练通过 |

## 9. 版本路线

| 版本阶段 | 产品范围 | 退出条件 |
|---|---|---|
| V0 基线修复与契约冻结 | 修复现有无法编译或空实现路径；定义领域对象、证据包、权限语义和 MCP Schema | 基线测试可运行；未实现能力不再被文档描述为可用 |
| V0.4 Enterprise 权威数据面 | 文档版本、对象存储、ACL、摄取任务、删除传播和投影事件 | 权威记录与 ES 重建演练通过；权限收紧无暴露窗口 |
| V0.5 可信检索与回答 | 并行混合召回、重排、证据包、声明级引用、拒绝与离线评测 | 检索、引用、拒绝和 ACL 门禁通过 |
| V0.6 可信图谱 | 候选事实、实体消歧、审核、时态、冲突、Neo4j 投影和图检索 | 已发布图谱全部可追溯；图谱价值门禁通过 |
| V0.7 MCP Gateway | 默认只读 Tools、Resources、Prompts、stdio、Streamable HTTP、OAuth/OIDC、限流和审计 | MCP 协议、安全与客户端兼容门禁通过 |
| V1.0 Enterprise GA | 高可用、备份恢复、可观测性、容量档位、运维手册与升级回滚 | 8.3 全部门禁通过并完成受控试点验收 |
| V2 Local Edge | Rust 单二进制、嵌入式投影、离线模型和 stdio；复用 V1 契约与评测集 | 不分叉领域语义；离线安装、迁移、资源上限和安全测试通过 |

任何阶段未满足退出条件时，不以版本号、演示路径或文档勾选替代验收证据。Local Edge 的立项入口是 V1 领域契约稳定及离线需求被真实试点验证，而不是预设日期。

## 10. 成功标准

| 维度 | 成功标准 |
|---|---|
| 产品边界 | Web、REST 与 MCP 调用同一知识核心；不存在需要双写维护的第二套企业事实库 |
| 用户价值 | 受控试点用户能完成证据问答、声明核验和来源追踪，关键任务无需访问底层 ES/Neo4j |
| 企业安全 | 所有调用可归属到主体与租户；默认智能体无写权限；跨边界泄漏测试为零 |
| 数据可信 | 文档、证据、候选事实、审核和发布事实形成可审计链；投影可以从权威记录重建 |
| MCP 完整性 | 已声明 Tools、Resources、Prompts 及协议行为全部通过支持矩阵，不以“十个工具名称存在”代替功能完整 |
| 检索与回答 | 达到冻结数据集上的检索、图谱、引用、支撑和拒绝门禁；报告披露失败案例与适用范围 |
| 运维能力 | 容量、监控、告警、恢复、回滚、审计和投影重建均完成演练 |
| 路线纪律 | Enterprise GA 前不维护 Local Edge 的独立领域实现；V2 复用 V1 契约与评测资产 |

## 11. 需求追踪

| 用户旅程 | 主要需求 | 对应门禁 |
|---|---|---|
| 证据问答 | FR-RET-01 至 FR-RET-07、FR-ANS-01 至 FR-ANS-05、MCP-01 至 MCP-10 | 检索增益、引用完整性、协议门禁 |
| 声明核验 | FR-KG-04 至 FR-KG-07、FR-ANS-02 至 FR-ANS-04 | 声明支撑、拒绝质量、图谱价值 |
| 文档摄取与发布 | FR-DATA-01 至 FR-DATA-07、FR-OPS-01 | 摄取成功率、投影新鲜度、数据门禁 |
| 图谱审核 | FR-KG-01 至 FR-KG-06、FR-OPS-01 至 FR-OPS-02 | 图谱发布质量、审计与安全门禁 |
| 权限变更与删除 | FR-DATA-07、FR-RET-04、SEC-01 至 SEC-12 | ACL 隔离、删除传播、安全门禁 |
| 投影恢复 | FR-DATA-05 至 FR-DATA-07、FR-OPS-03 至 FR-OPS-05 | 恢复目标、数据门禁、运维门禁 |

## 12. 当前状态声明

本文定义目标产品和发布标准，不表示现有 `esragpro-mcp` Rust 骨架或现有 esRAGPro 后端已经实现这些能力。现状、证据与取舍记录见 `docs/DECISION-REVIEW.md`；实施必须以可运行测试和门禁报告更新状态。
