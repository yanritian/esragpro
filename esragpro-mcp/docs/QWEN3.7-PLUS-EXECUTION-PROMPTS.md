# Claude Code + Qwen3.7 Plus 执行提示词包

> 版本：2026-07-10  
> 用途：让 Claude Code 以 Qwen3.7 Plus 为执行模型，按工作包实施 esRAGPro 企业知识核心与 MCP Gateway。  
> 原则：一次会话只执行一个工作包；上一工作包门禁未通过，不得进入下一工作包。

## 1. 使用方法

1. 第一次启动时粘贴“总控提示词”。
2. 总控提示词完成预检并确认当前工作包后，再粘贴对应的工作包提示词。
3. 每个工作包结束后，必须执行“交付审查提示词”。
4. 新会话或上下文压缩后，先粘贴“断点续跑提示词”，不得凭记忆继续。
5. `WP9 Local Edge` 是可选工作包，只有 WP0-WP8（含 WP7A）全部门禁通过且存在批准的 ADR 才能开始。

## 2. 固定路径与事实源

```text
企业知识核心代码：D:\dingsun\esRAGPro
MCP Gateway 代码：D:\dingsun\esragpro-mcp
参考材料（只读）：D:\dingsun\kg-skill-package-20260607-234344
```

执行时按以下优先级读取文档：

1. `D:\dingsun\esragpro-mcp\docs\DECISION-REVIEW.md`
2. `D:\dingsun\esragpro-mcp\docs\PRD.md`
3. `D:\dingsun\esragpro-mcp\docs\ARCHITECTURE.md`
4. `D:\dingsun\esragpro-mcp\docs\MCP-V1-CAPABILITY-MATRIX.md`
5. `D:\dingsun\esragpro-mcp\docs\RETRIEVAL-DESIGN.md`
6. `D:\dingsun\esragpro-mcp\docs\PLAN.md`

发生冲突时，先停止编码，指出冲突位置并提出最小修正文案；不得自行选择对实现最方便的解释。

## 3. 总控提示词

```text
你现在是 esRAGPro 的首席实现代理，通过 Claude Code 工作，模型为 Qwen3.7 Plus。

你的任务不是一次性重写整个系统，而是严格执行 D:\dingsun\esragpro-mcp\docs\PLAN.md 中的一个工作包。你必须使用简体中文汇报，代码、标识符和提交信息按项目原有语言风格。

【必须先读取】
1. D:\dingsun\esRAGPro\AGENTS.md
2. D:\dingsun\esRAGPro\CLAUDE.md
3. D:\dingsun\esragpro-mcp\docs\DECISION-REVIEW.md
4. D:\dingsun\esragpro-mcp\docs\PRD.md
5. D:\dingsun\esragpro-mcp\docs\ARCHITECTURE.md
6. D:\dingsun\esragpro-mcp\docs\MCP-V1-CAPABILITY-MATRIX.md
7. D:\dingsun\esragpro-mcp\docs\RETRIEVAL-DESIGN.md
8. D:\dingsun\esragpro-mcp\docs\PLAN.md

【不可改变的架构决策】
- MCP 是安全协议适配层，不是知识库、数据库或新的事实源。
- Enterprise Edition 中，PostgreSQL 与对象/文件存储保存文档、版本、租户、ACL、任务和审核记录；Elasticsearch 是可重建检索投影；Neo4j 是可重建且只含 `active` Claim 的图投影。
- Rust MCP Gateway 通过受控领域 API 调用 esRAGPro，禁止直连 ES、Neo4j、PostgreSQL。
- 默认 MCP 工具只读。摄取和图谱修改属于独立管理域，必须授权、幂等、审计和审批。
- 用户、tenant、knowledge base 和 ACL 上下文只能从可信认证上下文推导，禁止相信模型传入的 tenant_id、user_id 或角色。
- ES 查询和图查询必须在召回阶段执行 ACL 预过滤，禁止检索后再删除无权结果。
- LLM 抽取结果只能创建 `ClaimCandidate`。候选状态为 extracted/validated/review_required/approved/rejected；获批后才创建或修订 publication_state=active 的权威 Claim，后续状态为 superseded/revoked/expired。
- 普通事实查询默认走 BM25 + dense hybrid；图谱只对实体、关系、多跳、因果、时态或冲突问题按需启用；融合后必须重排。
- 未经固定语料和黄金集测量，禁止宣称“幻觉率 3-5%”“拦截率 90%”或固定延迟/吞吐。
- Local Edge 是后续可选后端，不得为了它在 Enterprise 默认构建中引入 LanceDB、SQLite 图存储、ONNX 或 CUDA。

【冻结的 MCP V1 合同】
- 默认只读 Tools 恰好 6 个：kb.search_evidence、kb.answer_with_evidence、graph.lookup_entity、graph.expand、graph.find_paths、graph.verify_claim。
- 对应 scopes：knowledge:read、knowledge:answer、graph:read、graph:verify。`kb.*` 输入使用 `knowledge_base_ids` 数组；`graph.*` 输入使用单个 `knowledge_base_id`。
- Tool 的其余字段名必须逐字采用 MCP-V1-CAPABILITY-MATRIX.md 4.1 与 mcp-v1.schema.json；禁止增加 query/question、depth/max_depth、as_of/valid_at 或拆分 claim 等兼容别名形成双合同。
- 静态 Resource 恰好 1 个：esrag://server/capabilities。
- Resource Templates 恰好 8 个：esrag://kb/{kb_id}/documents/{doc_id}/versions/{version_id}、esrag://kb/{kb_id}/evidence/{evidence_id}、esrag://kb/{kb_id}/entities/{entity_id}、esrag://kb/{kb_id}/claims/{claim_id}、esrag://kb/{kb_id}/ontology/{schema_version}、esrag://kb/{kb_id}/jobs/{job_id}、esrag://kb/{kb_id}/catalog、esrag://kb/{kb_id}/stats。
- Prompts 恰好 4 个：answer-with-citations、compare-evidence、trace-requirement、verify-claim。
- Prompt 参数逐字采用 MCP-V1-CAPABILITY-MATRIX.md 第 7 节和 mcp-v1.schema.json；prompts/get 只返回模板，不读取 evidence、不自动调用 Tool、不扩大 Scope。
- 管理 Tools 只存在于默认关闭的独立 profile：admin.create_ingest_job、admin.cancel_ingest_job、admin.request_projection_rebuild、admin.request_document_deletion；其 scopes 分别为 ingest:create、ingest:cancel、projection:rebuild、document:delete:request。
- Profile 只允许完整 Scope bundle：reader 4 个只读 Scope、operator 再加 3 个运维 Scope、admin 再加删除请求 Scope；缺管理 Scope 整体降档，reader bundle 不全则拒绝 initialize，禁止产生 5/7/8 Tool。
- WP0 的 `mcp-v1.schema.json` 必须一次性包含 reader/operator/admin 全合同和四个管理 Tool 的严格输出；全部管理结果映射 `esrag://kb/{kb_id}/jobs/{job_id}`。WP7A 只能实现，不能修改已签字 Schema。`source_ref` 上传闭环与 management job 审批执行闭环属于 WP0 冻结的 `domain-api-v1.schema.json`。

【执行纪律】
1. 先运行只读预检：工作树状态、项目结构、依赖版本、现有测试、可用命令。保留用户已有修改，不覆盖、不回滚、不删除。
2. 两个目录目前可能不是 Git 仓库。先运行 git rev-parse --is-inside-work-tree。若不是 Git 仓库，不得自行 git init；在报告中标为阻塞选择，等待操作者选择“初始化 Git / 移入已有仓库 / 无 Git 执行”。
3. 只执行操作者指定的一个 WP。未指定时只做 WP0，不得自动进入 WP1。
4. 严格 TDD：先写能说明行为的失败测试，运行并记录预期失败，再做最小实现，再运行相关测试和回归测试。
5. 任何代码变更都必须对应测试；不得用 mock 掩盖核心授权、过滤、删除传播或协议行为。
6. 禁止留下 TODO、TBD、pass、NotImplementedError、固定假数据、硬编码评测分数或“后续实现”占位。
7. 禁止输出或读取展示 .env、backend/config.local.py 中的密钥值。只允许检查变量名是否存在。发现疑似泄露时仅报告文件和变量名，隐藏值。
8. 禁止使用任意 Cypher、任意 ES DSL 或任意文件路径作为 MCP 工具参数。枚举和路径必须白名单化。
9. Windows 文件删除/移动必须使用同一 PowerShell 进程，解析并验证绝对目标仍位于工作区；没有明确必要性不得删除。
10. 软件库和 MCP 规范可能变化。涉及 rmcp、MCP Transport、OAuth 或 Elasticsearch API 时，只查官方文档；在 ADR 中锁定确切版本和 feature flags，不照抄旧文档中的 rmcp = "1" 或旧 HTTP+SSE。
11. 每次声称完成前必须给出新鲜的命令输出摘要。不能运行的测试必须说明原因，不能写“应该通过”。
12. 达不到当前 WP 门禁时停止，不得进入下一 WP，也不得通过降低断言、跳过测试或移除安全检查来制造绿色结果。
13. 将每次 EXECUTION_START、EXECUTION_RESULT、审查结论和阻塞报告追加写入 D:\dingsun\esragpro-mcp\docs\EXECUTION-STATUS.md；该文件是跨会话状态源，不得只把状态留在聊天记录中。
14. 每次执行生成唯一 run_id，并记录 PRD/ARCHITECTURE/MCP-V1-CAPABILITY-MATRIX/RETRIEVAL-DESIGN/PLAN 及 mcp-v1/domain-api-v1/principal-context-v1 三份 Schema 的 SHA-256、当前 commit 或 snapshot hash；文件 hash 变化后不得沿用旧门禁。
15. Git 模式提交命令必须纳入本 WP 新建的 scripts、Compose override、lockfile 和文档；提交后对本 WP 路径运行 `git status --short -- <scoped paths>` 并要求为空。不得以清理无关用户改动来制造空状态。

【每次开始时输出】
EXECUTION_START
- run_id: 唯一 UUID
- work_package: WP编号和名称
- repositories: 两个目录的 Git/非 Git 状态
- baseline_commands: 计划运行的命令
- files_in_scope: 本 WP 允许修改的文件
- files_out_of_scope: 明确不修改的区域
- assumptions: 最多 5 条，可验证
- blockers: 无则写 none
- contract_hashes: 五份权威设计文档 + 三份 Schema 的 SHA-256

【每次结束时输出】
EXECUTION_RESULT
- run_id: 与开始一致
- work_package: WP编号和名称
- status: complete | blocked | failed
- changed_files: 逐个文件说明职责
- tests_red: 先失败的测试和失败原因
- tests_green: 通过的测试、构建和检查
- security_checks: 本 WP 的负向授权/输入验证结果
- acceptance_gate: 每条门禁 pass/fail
- known_limits: 仅列真实限制，不得写占位工作
- commit: commit id；非 Git 模式写 not-available
- artifacts: delivery report、测试日志、审查报告的绝对路径与 SHA-256
- next_allowed_action: 只能是审查当前 WP，或门禁通过后开始下一个 WP

先把同样内容追加到 D:\dingsun\esragpro-mcp\docs\EXECUTION-STATUS.md，再在终端会话中输出。

现在只做预检，并告诉我当前应该执行哪个工作包。不要修改生产代码。
```

## 4. WP0：基线、ADR 与可复现环境

```text
执行 D:\dingsun\esragpro-mcp\docs\PLAN.md 的 WP0，且只执行 WP0。

目标：把当前两个 pre-alpha 目录变成可复现、可审计的实施起点，不修业务功能。

强制要求：
- 记录 esRAGPro 后端测试、前端构建、Docker Compose 配置检查以及 esragpro-mcp cargo check 的真实基线。
- cargo check 失败时区分：源码缺失、manifest/SDK 版本错误、依赖下载问题、平台工具链问题。
- 创建并批准 PLAN 要求的 6 份 ADR：事实源与投影、Enterprise/Edge 形态、身份与 ACL、评测/发布门禁、薄 MCP Gateway、MCP 正式规范/SDK/transport/auth。
- 生成并验证 3 份 JSON Schema：domain-api-v1、mcp-v1、principal-context-v1；人类文档中的字段名必须与 Schema 逐字一致。`mcp-v1` 必须在 WP0 一次性冻结 reader/operator/admin 三种完整 Scope bundle、全部 10 个 Tool（含四个管理 Tool 的严格输入/输出）、1 个静态 Resource、8 个 Resource Templates、4 个 Prompts 与 Scope；所有管理输出映射同一 Job Resource。`domain-api-v1` 必须预先冻结上传会话 create/content/finalize、source_ref 单次事务消费、management job decision、outbox 执行和完整状态机。后续 WP 只实现，不能原地扩写已签字 V1 Schema。
- ADR 中明确 Enterprise 默认不编译 LanceDB/SQLite/ONNX/CUDA；Local Edge 只能通过后续 feature/backend 实现。
- 生成或修正最小测试目录和命令入口，但不得修复 WP1 的业务错误。
- 使用经校验和固定版本的 uv 生成 backend/uv.lock，并用空缓存 `uv sync --frozen` 验证；生成 frontend/package-lock.json 并用 `npm ci` 验证；SDK spike 保存 Cargo.lock。任何 frozen install 失败都阻塞 WP0。
- 检查 backend/config.local.py 是否被忽略，只报告敏感变量名，绝不显示值。
- 若没有 Git，按总控提示词停止在版本控制选择门禁，不自行初始化。

WP0 完成门禁：
1. 每条基线命令有时间、环境、退出码和摘要。
2. 6 份 ADR 没有 TBD/TODO，决策、替代方案、后果和回退条件完整；stdio/HTTP SDK spike 真实编译并记录 Cargo.lock hash。
3. backend/uv.lock、frontend/package-lock.json、两个 spike Cargo.lock 与环境 manifest 的 hash 可复现。
4. mcp-v1 已覆盖 6/9/10 Tool 三种 profile，domain-api-v1 已覆盖 source_ref 与审批执行闭环；三份 Schema 均有签字 hash。任何后续 hash 变化必须发布新版本、重新签字并重跑受影响门禁，不能沿用旧 PASS。
5. PLAN 中 WP1 的输入文件和失败用例能从基线报告定位。
6. 没有业务生产代码改动。

完成后停止，输出 EXECUTION_RESULT，不进入 WP1。
```

## 5. WP1：恢复 esRAGPro 单租户主链

```text
执行 PLAN.md 的 WP1，且只执行 WP1。前提是 WP0 门禁全部通过。

目标：让“上传 -> 分块/嵌入 -> ES 索引 -> 混合检索 -> 有来源回答 -> 整篇删除”在单租户测试环境真实跑通。

本工作包必须用测试复现并修复：
- kb.upload 调用不存在的 index_document_with_graph。
- graph_rag 将 query_vector=None 交给 kNN。
- kb.search/raw 未导入 rag_service。
- 根文档 ID、version ID、chunk ID 混用，列表返回重复分块，删除不能稳定删除整篇。
- GraphRAG 未实现时必须显式降级到已验证的 hybrid retrieval，不得假装图查询成功。
- 数据库迁移、Dockerfile 构建顺序、安全 Compose 的变量拼写和 Neo4j 开关不闭合。
- 新建独立 docker-compose.wp1.test.yml：禁止固定 container_name/宿主端口，测试通过 runner 容器和 Compose DNS 执行；随机 project label + sentinel + finally 清理，不能碰当前栈。

约束：
- 本 WP 不实现多租户、MCP 或完整知识图谱。
- 不保留硬编码“3-5%”评测结果；若修改宣传数字，必须改为“尚未建立基线”。
- 健康检查必须区分 liveness/readiness，并反映真实依赖状态。
- 测试至少覆盖成功上传、无向量错误的搜索、来源字段、列表去重、整文删除和无 Neo4j 降级。

只有端到端测试和 Docker 构建/配置门禁通过才算完成。完成后停止，不进入 WP2。
```

## 6. WP2：身份、租户、知识库、版本与 ACL

```text
执行 PLAN.md 的 WP2，且只执行 WP2。前提是 WP1 门禁全部通过。

目标：建立企业数据边界，任何 repository、service 和 API 都不能在缺少 PrincipalContext 时访问业务数据。

不可妥协的安全属性：
- PrincipalContext 以 principal-context-v1.schema.json 为唯一结构：顶层 context_version/request_id/trace_id/tenant_id/subject_id/subject_type/client_id，以及嵌套 authn、authorization、可选 delegation；roles、scopes、group_ids、knowledge_base_ids、clearance、policy_version、authorization_revision、authorization_fingerprint、access_partition_ids 位于 authorization。所有字段来自认证/策略中间件，不能来自业务请求 JSON，access_partition_ids 只能由服务端 policy engine 派生。
- 建立 tenant、knowledge_base、membership、document、document_version、ACL/grant 的迁移和领域模型。
- 注册接口忽略或拒绝客户端提交的 admin 角色；管理员创建必须经过受保护的 bootstrap 或 admin 流程。
- ES chunk 文档和 Neo4j Claim/Entity 投影包含 tenant_id、knowledge_base_id、access_partition_ids、authorization_revision、version/status，并在查询内部强制预过滤。
- 默认拒绝：上下文缺失、租户不匹配、知识库无成员关系或文档无授权时返回统一的 403/404 策略，不泄露对象是否存在。
- 缓存键必须包含 tenant、principal/authorization fingerprint、kb、query、index/model version。
- 上线前只读盘点旧 users、上传文件/对象、ES 索引、根文档/chunk ID 与 hash；未知归属一律进入无 grant 的 legacy-quarantine，禁止猜测归属或原地给旧 ES 文档补公共权限。WP3 只能在新索引重建、对账和别名切换后启用新读取。
- 权限收紧/删除在 PG 事务中递增 authorization_revision 并写 revocation barrier；投影滞后期间所有召回把 barrier 编译为 must_not，缓存/游标绑定 revision。barrier 不可用时 fail closed，不能形成撤权泄漏窗口。

必须通过的负向测试：
1. tenant A 无法搜索、读取、列出、删除 tenant B 的任何文档或 chunk。
2. tenant A 无法从图扩展、缓存、错误消息、计数和 timing-friendly API 得知 tenant B 数据。
3. 普通用户无法创建管理员、合并实体或调用管理接口。
4. 修改请求体中的 tenant_id/user_id 不改变服务端 PrincipalContext。
5. 暂停投影后撤权，旧 ES 文档与旧缓存仍存在时也必须立即零命中。
6. 无法确认 tenant/KB/owner 的历史数据在人工确认前不可列出、读取或搜索。

任何跨租户泄露测试失败都必须将 WP2 标为 failed。完成后停止，不进入 WP3。
```

## 7. WP3：异步摄取、稳定 ID 与投影一致性

```text
执行 PLAN.md 的 WP3，且只执行 WP3。前提是 WP2 门禁全部通过。

目标：建立可重试、幂等、可观察的文档摄取和删除传播状态机。

要求：
- 原始文档先登记 document/document_version，再创建 ingestion_job；上传 API 不同步执行 OCR、embedding 和图抽取。
- 稳定 ID 必须由 tenant、kb、document、version、结构位置/内容哈希构成；相同版本重放不得生成重复 chunk。
- 结构化解析保留 page、section、heading breadcrumb、字符/页内偏移和 content hash；500 字+50 字只能作为对照基线，不能作为唯一策略。
- 使用 outbox 或等价可重放机制驱动 ES 与 Neo4j 投影；记录 attempt、next_retry_at、last_error_code，不在事务中假装跨三库原子提交。
- LLM 抽取输出进入 ClaimCandidate；状态机和获批后发布 active Claim 的事务边界必须遵循冻结合同，不直接写 Neo4j。
- 删除产生 tombstone，传播至对象存储、ES、Neo4j、缓存；失败可重放并有一致性巡检。
- 只从 WP2 已确认的 legacy inventory 回填 PG/blob，并重建全新的 ES generation；按数量/hash/ACL/隔离项对账后才原子切 alias。未知归属不迁出 quarantine，禁止原地给旧索引补公共权限。
- 撤权/删除的 revocation barrier 必须在 PG 提交后立即阻断旧 ES/Neo4j/缓存结果，直到各投影确认 required authorization revision。
- 模型、tokenizer、chunker、ontology、prompt 均版本化；更换 embedding 时使用新索引/别名迁移，不原地混写不同维度。

测试必须覆盖重复提交、进程中断后重放、部分投影失败、删除传播失败重试、旧版本不可见、撤权零窗口、历史重建对账失败禁止切换、alias 回滚/前滚和 quarantine 零可见。常驻 worker/Compose 必须在隔离 project 中启动并在 finally 安全清理。

完成后停止，不进入 WP4。
```

## 8. WP4：可信 Claim 图谱

```text
执行 PLAN.md 的 WP4，且只执行 WP4。前提是 WP3 门禁全部通过。

目标：将知识图谱实现为带来源、审核、时态和权限的派生事实视图，而不是 LLM 三元组堆积。

必须实现的概念边界：
- Mention：原文中的实体提及，绑定 evidence span。
- CanonicalEntity：租户/知识库范围内经过消歧的规范实体，具有 alias 和 merge/split 历史。
- ClaimCandidate：subject-predicate-object 加 qualifiers、polarity、valid_from/valid_to、extractor/schema version、evidence_ids，状态只允许 extracted、validated、review_required、approved、rejected。
- Claim：由 approved ClaimCandidate 发布的权威对象，包含 recorded_at、审核链和 publication_state；状态只允许 active、superseded、revoked、expired。只有 active、当前有效且调用者有权访问的 Claim 可进入回答证据。
- 同名不等于同一实体，Neo4j MERGE 必须使用稳定 canonical ID 和唯一约束，不按 name 全局合并。
- entity type、predicate 和查询模板必须来自 ontology 白名单；禁止把 LLM 字符串直接拼接为 Cypher label、relationship type 或查询片段。
- 图路径每一条边都必须回指可读取 evidence；路径中任一边无权、过期、冲突或未验证，整条路径不可用于回答。

测试必须覆盖同名异义、别名链接、人工审核、冲突 Claim、时态查询、supersede、跨租户隔离和 Cypher 注入输入。

完成后停止，不进入 WP5。
```

## 9. WP5：混合检索、按需图检索、重排与证据防护

```text
执行 PLAN.md 的 WP5，且只执行 WP5。前提是 WP4 门禁全部通过。

目标：实现可测量的 retrieval policy 和 EvidencePacket，不再使用固定五层串行流水线。

查询流程必须为：
1. 根据会话历史生成独立查询，并解析意图、实体、时态和缺失槽位。
2. 在同一 PrincipalContext 下并行执行 BM25 与 dense recall；只有关系、多跳、因果、冲突、时态问题才触发图检索。
3. 所有召回在存储层 ACL 预过滤。
4. 用 RRF 合并候选，再使用经基准验证的 cross-encoder/reranker 重排。
5. 输出 EvidencePacket：chunks、claims、paths、sources、score components、retrieval trace、warnings、evidence_strength。
6. kb.search_evidence 只返回证据；kb.answer_with_evidence 在服务器内完成生成、逐 Claim 引用核验和拒答，二者不得混为一个不透明接口。
7. 无命中、证据冲突、缺失关键槽位或引用无法验证时，必须拒答或请求澄清；不得用模型常识补齐。
8. verifier 超时/失败时，answer 模式拒答，evidence-only 模式只返回 EvidencePacket 和降级警告；禁止返回未经核验的 uncertain 生成答案。
9. 所有可超限集合使用 page_size/cursor/next_cursor/truncated；cursor 绑定授权 revision/fingerprint、请求 hash、投影水位和过期时间，撤权后立即失效。

要求建立分层、版本化且与最终 sealed holdout 分离的 dev 集：事实、流程、比较、多跳、时态、冲突、无答案、越权和提示词注入。reranker 必须与关闭重排对照，按预注册的质量增益、关键分组退化、p95 和成本门禁选择并锁模型/hash；无合格收益就保持关闭。至少报告 Recall@k、MRR、NDCG、rerank 增益、citation precision/recall、claim support、拒答准确率、ACL leakage 和 p50/p95。

未经本项目固定语料实测，不得采用任何文档中的通用目标值作为完成证据。

完成后停止，不进入 WP6。
```

## 10. WP6：Rust 薄 MCP Gateway 与 stdio

```text
执行 PLAN.md 的 WP6，且只执行 WP6。前提是 WP5 门禁全部通过。

目标：在 D:\dingsun\esragpro-mcp 实现只调用 esRAGPro 领域 API 的 Rust MCP Gateway，并通过 stdio 与 MCP Inspector/Claude Code 集成。

开始编码前：
- 根据官方 modelcontextprotocol/rust-sdk、WP0 锁定的正式 MCP 规范（规划基线为 2025-11-25）和 crates.io 做 SDK spike，写 ADR 并锁定确切版本与 feature flags；候选规范不能替代正式版。
- 删除或 feature-gate Enterprise 默认路径不需要的 LanceDB、SQLite、ONNX、CUDA、jieba 依赖；不得实现第二套检索引擎。
- 定义 KnowledgeBackend trait，但 WP6 只实现 EnterpriseHttpBackend。

只读 Tool：
- kb.search_evidence
- kb.answer_with_evidence
- graph.lookup_entity
- graph.expand
- graph.find_paths
- graph.verify_claim

静态 Resource：
- esrag://server/capabilities

Resource Templates：
- esrag://kb/{kb_id}/documents/{doc_id}/versions/{version_id}
- esrag://kb/{kb_id}/evidence/{evidence_id}
- esrag://kb/{kb_id}/entities/{entity_id}
- esrag://kb/{kb_id}/claims/{claim_id}
- esrag://kb/{kb_id}/ontology/{schema_version}
- esrag://kb/{kb_id}/jobs/{job_id}
- esrag://kb/{kb_id}/catalog
- esrag://kb/{kb_id}/stats

Prompt：
- answer-with-citations
- compare-evidence
- trace-requirement
- verify-claim

协议要求：
- 所有 tool 有严格 inputSchema、outputSchema、additionalProperties=false、长度/数量/深度上限。
- kb.* 使用 knowledge_base_ids 数组，graph.* 使用单个 knowledge_base_id；禁止同时接受 kb_id 别名造成双合同。
- 返回 structuredContent，同时为兼容客户端返回序列化文本；证据使用 resource_link。
- stdout 只能包含合法 JSON-RPC；tracing 和诊断只能写 stderr。
- 后端 401/403/404/409/429/超时映射为稳定、可测试且不泄露敏感信息的 Tool Error。
- 任何 tool 参数都不得包含 tenant_id、user_id、任意 URL、任意 ES DSL、任意 Cypher 或任意服务器文件路径。
- stdio 凭证从环境/操作系统安全存储获取，日志禁止打印 token、query 原文中的敏感字段和证据全文。
- initialize capability 必须逐项匹配 MCP-V1-CAPABILITY-MATRIX.md；标为否/不请求的 subscription、listChanged、logging、completion、sampling、elicitation、roots、实验性 Tasks 不得声明或伪成功。

测试必须覆盖 initialize/initialized（按锁定版本）、ping、capability、reader 的 6 Tools、1 static Resource、8 Resource Templates、4 Prompts、列表顺序/分页、每个 schema、structured output、resource read、prompt get、progress、错误映射、取消、超时、stdout 纯净、恶意参数和所有未声明能力的负向行为。

完成后停止，不进入 WP7。
```

## 11. WP7：Streamable HTTP、OAuth 与企业边界

```text
执行 PLAN.md 的 WP7，且只执行 WP7。前提是 WP6 门禁全部通过。

目标：增加符合当前 MCP 正式规范的远程 Streamable HTTP，不实现已废弃的旧 HTTP+SSE 作为默认传输。

安全要求：
- 单一 MCP endpoint 严格按 WP0 锁定的正式规范实现。目标为 2025-11-25 时支持 POST/GET、协议版本头、Mcp-Session-Id、SSE 重连、会话终止与取消，并用共享 SessionStore 避免粘滞内存；若 WP0 经 ADR 升级到后续已正式发布的 stateless lifecycle，则完全按新生命周期实现，禁止混用旧 session/initialized 语义。候选版不能直接作为生产目标。
- 验证 Origin，非法 Origin 返回 403；本地监听默认仅 127.0.0.1。
- 按 MCP Authorization 规范实现 OAuth/OIDC discovery、PKCE/资源指示器兼容、token audience 验证和最小 scope。
- 严禁 token passthrough。Gateway 接收的 MCP token 与调用后端使用的 token 必须是不同受众、可审计的委托/交换结果。
- 普通 bearer 的 jti 不是一次性请求 ID；基础档依靠 TLS、短 TTL、最小 scope，需要强防重放时采用经 ADR 验证的 DPoP 或 mTLS sender-constrained token。
- `/.well-known/oauth-protected-resource` 是未认证、可缓存、资源级 metadata，`scopes_supported` 始终固定公布服务端 V1 的八 Scope 并集：knowledge:read、knowledge:answer、graph:read、graph:verify、ingest:create、ingest:cancel、projection:rebuild、document:delete:request；不得按 tenant、token 或会话动态变化。metadata 只表示服务器能力，不授予权限；实际 profile 按服务端开关、租户策略与 token 满足的最高完整 Scope bundle 选择，调用仍逐次鉴权。
- profile 只能按完整 Scope bundle 选择最高的 reader/operator/admin 档；缺任一管理 Scope 整体降档，reader bundle 不全则 initialize 返回 `insufficient_scope`。不得逐 Tool 裁剪出 5/7/8 个 Tool；每次调用仍重新检查 Scope 与领域策略。
- 每次调用记录 request/trace id、principal、tenant、kb、tool、policy decision、耗时、结果码和证据数量；日志不得保存 token 或完整敏感证据。
- 加入速率限制、并发限制、结果大小限制、超时和断路策略。
- 远程 Inspector 只能通过 PLAN 指定的 `run_wp7_http_integration.ps1 -ConfirmIsolatedTest` 执行：随机隔离 Compose project/loopback 端口，启动并等待测试 IdP/JWKS、Core fixture、当前 MCP 和 TLS proxy，就绪后签发受限短 TTL token，运行锁定 Inspector；finally 仅清理匹配本次 project label/sentinel 的容器、网络、volume、token 和证书。禁止依赖操作者预先启动的 ambient service。

负向测试至少覆盖错误 audience、过期 token、错误 Origin、无 scope、跨租户、重放、超限、会话劫持尝试和后端 token 泄露检查；另断言 metadata 对不同 tenant/token 字节级一致，而默认关闭管理 profile 时仍只有 6 个 Tool 且绕过 list 调用管理 Tool 被拒绝。

完成后停止，不进入 WP7A。
```

## 11A. WP7A：独立管理 MCP Profile

```text
执行 PLAN.md 的 WP7A，且只执行 WP7A。前提是 WP7 门禁全部通过。

目标：实现默认关闭、仅远程启用、与 reader 分离的 operator/admin profile；不得把管理能力塞进默认 6 个只读 Tool。

合同冻结：WP7A 只能实现 WP0 已签字的 mcp-v1、domain-api-v1、principal-context-v1。开始和结束都校验三份 SHA-256 完全不变。若必须改合同，立即停止 WP7A，发布新 `$id`/版本的 Schema 与 ADR，重新签字并重跑 WP0 及受影响的 WP6/WP7 门禁；禁止覆盖 V1 Schema 后沿用旧 PASS。

精确 Tool 与 Scope：
- admin.create_ingest_job → ingest:create
- admin.cancel_ingest_job → ingest:cancel
- admin.request_projection_rebuild → projection:rebuild
- admin.request_document_deletion → document:delete:request

强制边界：
- 默认与 stdio profile 恰好 6 个 Tool；operator 恰好 9 个；admin 恰好 10 个。同一会话 profile 固定，变化需重新认证/initialize。采用完整 Scope bundle，缺任一管理 Scope 整体降到下一完整档，reader 四 Scope 不全则拒绝初始化；枚举测试并禁止 5/7/8 Tool。
- source_ref 必须来自受认证 Core REST 闭环：POST `/api/v1/management/upload-sessions` 创建绑定 tenant/subject/KB 的短期会话，PUT `.../{id}/content` 流式上传并计算 size/SHA256，POST `.../{id}/finalize` 校验后签发密码学随机 opaque source_ref。拒绝 URL、盘符、UNC、相对路径和对象键；测试过期、跨主体、跨 KB、重复 finalize 与恶意 MIME/size/hash。
- `admin.create_ingest_job` 必须在一个数据库事务中锁定并 CAS 单次消费 source_ref，同时创建 Job 和 outbox；回滚后 ref 仍可用。同一主体/Tool/目标/请求 hash/idempotency_key 的重试返回同一 Job，其他重放拒绝，不能出现“ref 已消费但无 Job”。
- create/cancel 只作用于授权范围 Job；rebuild/deletion 的 MCP 调用只创建 approval_pending 请求，不直接切换投影、写 tombstone 或物理删除。
- 四个 Tool 都返回严格结构化 Job 输出：`job_id/job_kind/state/revision/resource_uri/idempotency_replayed` 和对应时间字段；`resource_uri` 必须是 `esrag://kb/{kb_id}/jobs/{job_id}`。ManagementRequest 是 Job 子类型，客户端从申请、decision 到 executor 终态始终轮询同一 URI，不得暴露第二个 request/execution Resource ID。
- 审批仅由不同 audience 的 Core Web/REST `POST /api/v1/management/jobs/{job_id}/decision` 执行，请求体严格为 `{decision:"approve"|"reject", expected_revision, reason, idempotency_key}`，返回同一 Job 的结构化引用；要求 `management:approve`、当前 KB 权限、申请人与审批人分离、乐观并发和不可改写审计；不得新增 MCP decision Tool。
- approve 与唯一 `management.execution.requested.v1` outbox 同事务；reject 不产生执行事件。outbox executor 按 job_id+revision 幂等，覆盖重复、乱序、崩溃和重试。rebuild 仅在新 generation 对账通过后原子切 alias，失败保留旧 alias；deletion 批准事务立即写 deletion_pending+revocation barrier，随后异步清理投影/cache/blob 并置 deleted，执行失败不得重新暴露。
- Claim 审核、ACL 修改、清空和直接物理删除永远没有 MCP Tool。
- 每个写请求要求幂等键；并发、重放、expected_revision、职责分离、Scope 和审计必须在领域服务端验证，不能依赖 Tool annotations。
- Protected Resource Metadata 固定公布八个服务端支持 Scope 的并集，不按租户/token 动态变化；实际 tools/list 与 tools/call 都重新授权，绕过列表直接调用仍必须拒绝。

测试覆盖：6/9/10 发现计数及所有缺 Scope bundle 组合、冻结 Schema hash、四个 Tool 的 outputSchema、同一 Job Resource 全状态轮询、跨租户 KB/job/document、完整上传会话、恶意/过期/跨主体 source_ref、事务回滚与幂等重放、并发取消、申请人自批、approve/reject 竞态、审批前零破坏性副作用、批准后的重建切换/删除 barrier/outbox 故障恢复、法律保全、审计脱敏，以及 WP6/WP7 全量回归。

完成后停止，不进入 WP8。
```

## 12. WP8：评测、可观测性、发布与灾备门禁

```text
执行 PLAN.md 的 WP8，且只执行 WP8。前提是 WP7A 门禁全部通过。

目标：让所有质量、安全、性能和发布结论由可复现实验产生。

要求：
- 黄金集绑定 corpus_version、ontology_version、embedding_version、reranker_version、prompt_version 和代码版本。
- train/dev 与 sealed holdout 分离；仓库只存 holdout manifest 和 evaluator 公钥，不存问题、标签或期望答案。独立 CI evaluator 保管样本并返回绑定 artifact/schema/policy/dataset hash 的签名报告；执行者只能验签和读取汇总指标。manifest/公钥/evaluator 变化必须重新批准。
- 评测覆盖事实、流程、比较、多跳、时态、冲突、无答案、越权、提示词注入、恶意文档和删除后不可见。
- 分别报告解析覆盖率、chunk/引用定位、实体抽取 F1、实体链接准确率、Claim 审核准确率、Recall@k、MRR、NDCG、citation precision/recall、claim support、拒答准确率、ACL leakage、p50/p95/p99 和单次成本。
- LLM-as-judge 只能作为辅助；必须有确定性指标和人工抽样校准，记录 judge 模型与提示词版本。
- OpenTelemetry/等价 tracing 串联 MCP request、领域 API、ES、Neo4j、embedding/reranker/LLM 和 ingestion job。
- 发布包含依赖锁定、SBOM、漏洞扫描、配置示例、升级/回滚、备份恢复、索引重建和灾备演练记录。
- 恢复演练只能新建带随机前缀和测试 sentinel 的隔离 project/数据库/bucket；同时校验 host allowlist、目标为空和非生产凭据，并要求显式 ConfirmDestructiveTest。禁止清空当前开发/生产栈，任一 guard 失败立即停止。
- 必须交付可加载的 OTel/Prometheus/dashboard 配置、Core/MCP/frontend/容器 SBOM，以及 release binary 的 stdio/HTTP smoke 证据。
- release smoke 必须验证 reader/operator/admin 分别为 6/9/10 个 Tool，默认部署与 stdio 只有 reader；四个管理 Tool 的 Scope、审批与幂等负例继续全绿。
- README、销售材料和 API 响应中删除未经实测的 3-5% 幻觉率或固定性能数字；发布报告只引用本次可复现实验。

WP8 完成后生成 release-readiness 报告。任一 ACL leakage 非零、关键路径无审计或恢复演练失败，都不得发布。

完成后停止。WP9 仍需单独 ADR 批准。
```

## 13. WP9：可选 Local Edge

```text
仅当 WP0-WP8（含 WP7A）全部通过，并且操作者明确批准 Local Edge ADR 时，执行 PLAN.md 的 WP9。

目标：在不改变 Enterprise 契约的情况下增加单用户离线 KnowledgeBackend。

约束：
- Local Edge 是单用户、单机、stdio 产品，不得宣传为多租户企业版。
- 通过 Cargo feature 或独立 crate 隔离 embedded backend；Enterprise 默认二进制不得携带 LanceDB、SQLite 图、ONNX/CUDA。
- 先用相同黄金集对候选存储和模型做基准；没有可测收益不得锁定 LanceDB/SQLite/BGE-M3 ONNX。
- 使用相同 EvidencePacket、Claim、Resource URI 和 Tool Schema；若能力不支持，必须显式 capability/error，不能返回伪结果。
- 文件访问采用显式 allowlist，解析后的绝对路径必须位于批准的数据目录；禁止 MCP 参数触发任意路径遍历或递归删除。
- bundle 使用位于压缩 payload 外部的 RFC8785 canonical manifest + detached Ed25519 signature + payload.tar.zst；manifest 绑定压缩 payload 的文件名/大小/hash 和全部内部条目的 path/size/hash。先验签并校验未解压 payload hash/size，再允许流式解包；key_id/trust-store 支持轮换。
- importer 必须拒绝绝对/盘符/UNC/../反斜杠绕过、symlink、hardlink、reparse point、设备/FIFO/socket、重复/额外条目和文件数/大小/压缩比炸弹；只流式写入已验证 staging，全部 hash 通过后原子激活。
- 所有索引可由本地系统记录重建，embedding/model manifest 包含来源、许可证、哈希、维度和 tokenizer 版本。

完成后停止并分别报告 Enterprise 与 Edge 的契约一致性、质量差异、资源占用和已知限制。
```

## 14. 交付审查提示词

```text
你现在不是实现者，而是当前工作包的对抗性审查者。必须在新的隔离子 agent 或独立会话中运行，只读工作区；不得使用实现者未写入 artifact 的隐藏记忆，也不得修改代码。只审查刚完成的 WP，不实现下一个 WP。

先读取 DECISION-REVIEW.md、PRD.md、ARCHITECTURE.md、MCP-V1-CAPABILITY-MATRIX.md、RETRIEVAL-DESIGN.md、PLAN.md，再读取本 WP 的 diff、测试和 EXECUTION_RESULT。

按以下顺序审查：
1. 规格覆盖：逐条映射 PLAN 的任务和门禁到具体代码/测试。
2. 安全：认证上下文是否可信、ACL 是否预过滤、是否默认拒绝、是否存在跨租户/缓存/图路径泄露。
3. 数据一致性：稳定 ID、版本、outbox、删除传播、重放和投影重建是否闭环。
4. RAG 质量：混合召回、条件图检索、重排、证据包、引用核验、拒答是否可测。
5. MCP 合规：capability、schema、structuredContent、resource、prompt、transport、stdout、错误和取消是否符合锁定的官方规范版本。
6. 测试真实性：测试是否先红后绿，是否错误地 mock 掉核心逻辑，是否有跳过、弱断言、固定假数据或未运行命令。
7. 范围：是否越过当前 WP，是否复制 kg Flask/Zvec 脚手架，是否在 Enterprise 默认路径重造嵌入式数据面。

输出按严重度排序的 findings，每条必须包含文件、行号、影响、复现方式和最小修复建议。若无阻塞问题，明确写：APPROVED_FOR_NEXT_GATE。存在 P0/P1 时写：REJECTED，并禁止进入下一 WP。

把完整审查报告写入 D:\dingsun\esragpro-mcp\artifacts\wp<N>\review-report.md，将 run_id、结论和报告 SHA-256 追加到 D:\dingsun\esragpro-mcp\docs\EXECUTION-STATUS.md。若当前工具无法创建独立审查上下文，状态必须为 blocked，等待操作者新开审查会话；不能由实现者自行“切换角色”批准。
```

## 15. 断点续跑提示词

```text
这是一个续跑会话。不要依赖上一会话记忆，也不要立即编码。

重新读取：
- D:\dingsun\esragpro-mcp\docs\DECISION-REVIEW.md
- D:\dingsun\esragpro-mcp\docs\PRD.md
- D:\dingsun\esragpro-mcp\docs\ARCHITECTURE.md
- D:\dingsun\esragpro-mcp\docs\MCP-V1-CAPABILITY-MATRIX.md
- D:\dingsun\esragpro-mcp\docs\RETRIEVAL-DESIGN.md
- D:\dingsun\esragpro-mcp\docs\PLAN.md
- D:\dingsun\esragpro-mcp\docs\EXECUTION-STATUS.md
- 当前 WP 的最新审查报告和 git status/diff（如果有 Git）

然后输出 RESUME_STATE：
- last_completed_gate
- current_work_package
- approved_findings_fixed
- remaining_failed_tests
- uncommitted_files
- blockers
- exact_next_test

只有能用文件和测试输出证明状态后，才从 exact_next_test 继续。不得重复已经完成的工作，不得自动进入后续 WP。
```

## 16. 阻塞报告提示词

```text
当前工作包遇到阻塞。停止扩展实现，不要用临时假实现绕过。

输出 BLOCKER_REPORT：
- work_package
- blocking_command 与退出码
- 最小错误摘要（隐藏密钥和敏感内容）
- 已验证的根因证据
- 已尝试的安全方案
- 方案 A/B 的影响与风险
- 需要操作者做出的唯一决定
- 在等待期间仍可执行且不会改变架构的只读验证

不得把“测试失败”“工作量大”“依赖下载慢”本身当成永久阻塞；先完成可安全执行的诊断。
```

## 17. Qwen 执行红线

- 不得复制 `kg-skill-package` 的 Flask/Zvec 脚手架到 esRAGPro。
- 不得把 MCP Tool 数量当成功能完整度。
- 不得以 `py_compile`、页面能打开、空工具能注册或 mock 测试通过作为里程碑完成证据。
- 不得让模型控制 tenant、identity、ACL、Cypher、ES DSL、服务器路径或删除范围。
- 不得让未审核 Claim、过期 Claim、无来源路径或越权证据进入答案上下文。
- 不得在没有 reranker 基准的情况下把路径启发式评分称为重排模型。
- 不得把 `evidence_strength` 冒充校准后的概率 confidence。
- 不得在 stdio stdout 输出日志、欢迎语或进度文本。
- 不得因上下文不足而猜测；使用断点续跑提示词重新加载事实源。
- 不得在当前工作包未通过审查前开始下一工作包。
