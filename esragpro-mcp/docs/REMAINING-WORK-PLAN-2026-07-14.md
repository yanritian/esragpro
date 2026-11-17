# esRAGPro 剩余整改与交付计划

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务执行本计划。每个步骤用复选框跟踪；上一工作包未 `VERIFIED`，不得进入下一工作包。

**Goal:** 在不扩张 MCP V1 权限边界、不开启未经批准的 Memory Profile 的前提下，把 MCP Gateway 和企业级 Agent Memory 整改到可审计、可回归、可发布候选状态。

**Architecture:** 先完成 MCP R4A/R4B 的传输安全和领域合同，再在知识核心旁维护独立 Memory bounded context；PostgreSQL/对象存储保存权威记录，ES/Neo4j 只作可重建投影，Context Orchestrator 在授权和 Token Budget 下组合 Knowledge EvidencePacket 与 MemoryContextPack。

**Tech Stack:** Python/FastAPI/SQLAlchemy/Alembic/PostgreSQL/Redis/Elasticsearch/Neo4j/MinIO、Rust 2021/rmcp/Tokio/Axum、pytest/Ruff/Cargo。

> 本文件只描述当前仍需完成的工作，不把历史台账中的“complete”当作真实验收证据。执行者必须先以当前源码、当前提交、当前测试和真实进程重新取证。

**范围：** MCP R4A/R4B、Agent Memory M1–M8、发布门禁和交付文档。

**明确不做：** M9（MCP V1.1 Memory Profile）未授权；不连接生产数据库；不部署生产；不把 Memory Tool 加入 MCP V1 的 6/9/10 Tool 集合。

## 一、执行前必须确认的事实

1. 唯一 Git 根目录是 `D:\dingsun\esRAGPro`，MCP 源码是 `D:\dingsun\esRAGPro\esragpro-mcp`。仓库外同名目录不能作为源码或状态源。
2. `.omx/` 和用户已有改动属于用户数据，不得修改、删除、暂存或用清理命令掩盖。
3. `esragpro-mcp/docs/EXECUTION-STATUS.md` 是追加式历史台账；任何旧的 `complete` 必须由当前提交和当前命令重新证明。
4. 开始时保存：分支、HEAD、`git status --short --untracked-files=all`、目标文件 diff、Python/Rust/Node/Docker 版本和测试基线。
5. 若当前工作树有未说明的生产代码改动，先标记 `BLOCKED` 并在状态报告中列出，不得覆盖。

## 二、冻结合同与发布前提

以下约束不能通过修改实现来绕过：

- MCP 生产协议只接受 `2025-11-25`；stdio 永远 Reader；HTTP 只允许 Reader/Operator/Admin 完整档位，Tool 数量只能为 6/9/10。
- MCP V1 固定 10 个 Tool、1 个静态 Resource、8 个 Resource Template、4 个 Prompt；禁止加入 `memory.*`。
- Principal 必须来自受控 stdio 配置或已验证 OAuth，不得由 Tool 参数构造；`authn.method` 的 OAuth 序列化值必须是 `oauth`。
- HTTP 会话必须在 SDK SessionManager 之外统一校验 GET/POST/DELETE，并绑定 tenant、issuer、subject、client、policy、authorization revision/fingerprint、Profile、协议版本和 bearer token 的不可逆指纹。
- Token 指纹使用带 `key_id`/版本的 HMAC-SHA-256 绑定摘要；禁止保存或记录原 token、身份明文或内部授权细节。
- Knowledge 是事实源；Memory 是独立 bounded context。ES/Neo4j 只能是可重建投影，不能成为 Memory 真源；LLM 只能生成 Candidate Draft，不能直接创建 active MemoryItem。
- 所有业务状态、时间戳、分页游标、分数、方向和 Job 状态必须来自权威领域 API；未知枚举、缺字段、额外字段、超限响应必须 fail closed。
- 生产启用只有 `production_release_eligible=YES` 且 `production_deployed=NO`；在用户另行批准前不得部署或启用。

## 三、工作包顺序与完成定义

依赖关系：

```text
R4A → R4B → M1 → M2 → M3 → M4 → M5 → M6 → M7 → M8
                         ↘ M6 real-service E2E
```

### 总体追踪清单

- [ ] R4A：传输、Principal、生命周期、JWKS、限流和错误合同 `VERIFIED`
- [ ] R4B：10 Tool、8 Resource、4 Prompt、共享 SessionStore `VERIFIED`
- [ ] M1：Memory 合同、身份映射、迁移 `VERIFIED`
- [ ] M2：观察、同意、DLP、幂等 outbox `VERIFIED`
- [ ] M3：Candidate Draft、审查、冲突、晋级 `VERIFIED`
- [ ] M4：分层检索、授权、Token Budget、Context Bundle `VERIFIED`
- [ ] M5：保留、衰减、legal hold、删除、checkpoint/DLQ `VERIFIED`
- [ ] M6：真实 PostgreSQL/Redis/ES/Neo4j/MinIO E2E `VERIFIED`
- [ ] M7：配置、健康检查、指标、CI、发布候选 `VERIFIED`
- [ ] M8：sealed holdout、独立 evaluator、签名报告、发布门禁 `VERIFIED`
- [ ] M9：保持 `NOT_STARTED / NOT_GRANTED`，不得实现或批准

每个工作包都必须完成：

1. 写一个可复现问题的失败测试（红灯）。
2. 实现最小完整修复（绿灯）。
3. 运行相邻模块回归、静态检查、秘密扫描和安全审查。
4. 独立规格审查，再做独立代码质量/安全审查；Critical/Important 必须修复并复审。
5. 只提交本工作包路径：一个 implementation commit，再一个只含 `EXECUTION-STATUS.md` 的 status commit。
6. 在 implementation commit 上重新运行门禁；status 必须记录命令、退出码、提交 hash 和未完成项。
7. 只有状态为 `VERIFIED` 才能进入下一个依赖包；`PARTIAL`、`BLOCKED`、旧日志和“测试数量增加”都不能放行。

### R4A：MCP 传输、Principal、生命周期和错误边界

**目标：** 修复协议版本漂移、跨 HTTP 方法会话接管、Principal 不完整和 HTTP 边界缺口。

**主要文件：**

- `esragpro-mcp/src/http.rs`、`src/server.rs`、`src/oauth.rs`、`src/schema/principal.rs`、`src/error.rs`
- `esragpro-mcp/tests/` 下新增协议、会话、OAuth、Origin/Host、CORS、限流测试
- `esragpro-mcp/scripts/` 下新增独立 HTTP 多进程 smoke
- `esragpro-mcp/docs/contracts/principal-context-v1.schema.json`（只允许纠正与冻结合同不一致的实现，不得放宽）

**必须交付：**

1. initialize 和后续 GET/POST/DELETE 都严格锁定 `MCP-Protocol-Version: 2025-11-25`；未知版本、重复 initialize、initialized 前业务请求都有稳定错误。
2. 在 rmcp 传输入口外实现 SessionSecurityRegistry；所有方法在进入 SessionManager 前校验绑定，DELETE 后原子删除绑定。
3. 共享摘要使用 HMAC-SHA-256 canonical binding digest，包含 `key_id`、算法版本、协议版本和全部绑定字段；日志和状态存储中不得出现原 token。
4. OAuth/JWKS 使用租户隔离的原子快照替换、singleflight、unknown-kid 负缓存/限流、过期和硬过期、`kty/alg/use/key_ops/kid` 严格校验、响应大小/JSON 深度/Content-Type 限制、禁止重定向。
5. 非 loopback 的 issuer/JWKS/public origin 强制 HTTPS；规范化 Host、默认端口、IPv6 和 HTTP/2 `:authority`；CORS 显式暴露 `Mcp-Session-Id`。
6. 限流使用租户维度原子 acquire 和 RAII lease，SSE/长响应直到 body 完成才释放；配置完整的 profile/feature 组合矩阵。
7. Principal 的 subject/client 长度、UUID、access partition、OAuth method、UUIDv7 request id、W3C trace id 全部做运行时校验；local-stdio 只能读取受控启动配置。
8. 错误 envelope 固定包含 `request_id`、机器码、`retryable`，必要时有 `retry_after_ms`；禁止通过字符串包含关系分类或泄露后端细节。

**R4A 门禁：**

```powershell
Set-Location D:\dingsun\esRAGPro\esragpro-mcp
cargo fmt --check
cargo metadata --locked --format-version 1
cargo clippy --locked --all-targets --all-features -- -D warnings
cargo test --locked --all-targets
cargo test --locked --all-targets --all-features
cargo build --locked --release
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\stdio-contract-smoke.ps1 -Binary .\target\release\esragpro-mcp.exe
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\http-session-security-smoke.ps1 -Binary .\target\release\esragpro-mcp.exe
```

必须有真实进程负例：跨 subject、同 Principal 换 token、换 Profile、换协议、GET/POST/DELETE 混用、重启/换实例、过期 JWKS、unknown kid、Host/Origin 绕过。

### R4B：MCP 领域合同、共享会话和真实下游边界

**目标：** 让 10 Tool、8 Resource、4 Prompt 的声明、运行时校验和真实输出完全同源。

**主要文件：**

- `esragpro-mcp/docs/contracts/mcp-v1.schema.json` 及新增独立 Tool/Resource/Prompt schema
- `esragpro-mcp/src/handlers.rs`、`src/backend/http.rs`、`src/schema/`
- 新增 `src/contracts/`、`src/session_store/`（或同等职责的小模块）
- `esragpro-mcp/tests/contract_*`, `tests/resource_*`, `tests/prompt_*`, `tests/downstream_*`

**必须交付：**

1. 修复泛型 ToolInput/ToolOutput、Profile scope 数组结构、Claim/EntityRef/TypedLiteral 和缺失的独立 schema；记录旧/新 hash，保持外部名称和 6/9/10 不变。
2. 每个 Tool 使用运行时 schema 校验；拒绝未知字段、空 allowlist、越界分页、非法 cursor、非法 reason/source_ref、无效时间和不支持的枚举。
3. Tool 输出只能映射权威领域 DTO；不再填充 `0`、`false`、`Utc::now()`、默认 Pending、固定 Reliable 或伪造 score/latency/graph_used。
4. Resource 使用严格 typed DTO，拒绝未知字段，只返回冻结白名单；下游 JSON 响应上限 4 MiB，Resource inline 内容上限 256 KiB，超限返回稳定可重试错误。
5. Job Resource 完整呈现 target/approval/execution 状态；不可逆管理操作在取消点前后幂等恢复到 Job，不得报告虚假成功。
6. Prompt 校验并真实渲染所有声明参数；不得静默丢弃 `valid_at`、`language` 等参数，不自动读取 Resource 或调用 Tool。
7. 共享 SessionStore 选用已有 Redis 7（若仓库事实不支持则先写 ADR 再实现）：定义 key prefix、schema/version、TTL、CAS、序列化、HMAC binding digest、清理和故障 fail-closed；测试重启、换实例、DELETE 和并发接管。
8. capabilities、tools/list、tools/call、resources/list/read、prompts/list/get 全部从同一 Registry/合同派生。

**R4B 门禁：** R4A 全部门禁 + 10 Tool × 4 profile、8 Resource、4 Prompt 的 schema/运行时/负例矩阵，以及真实下游超限、额外字段、未知枚举、授权越界和取消恢复测试。R4A/R4B 均 VERIFIED 后才允许 M1。

### M1：Memory bounded context、身份映射与合同

**主要文件：** `backend/app/memory_core/{contracts.py,models.py,repository.py}`, `backend/app/agent_runtime/{models.py,contracts.py}`, `docs/contracts/agent-memory-v1/*.schema.json`、Alembic migration、合同测试。

**必须交付：**

- 明确外部 `(tenant_id, issuer, subject_id)` 到内部 `principal_id` 的映射；禁止混用 `owner_subject_id` 与内部 UUID。
- 增加 `AgentAuthorizationContext`/`MemoryAccessContext`，不扩张 MCP V1 Principal Scope；Session/Task/Memory 权限只在后端内部表达。
- 建立 `MemoryObservation`、`ExtractedCandidateDraft`、`MemoryItem`、`EvidencePacket`、`MemoryContextPack`、`AgentContextBundle` 的严格 schema；canonical enum 只有一个生成源，并同步 Python/JSON/DB。
- 迁移必须可升级、可回滚、幂等，带 tenant/owner/retention/consent/legal_hold/checkpoint 字段和索引。

### M2：观察摄取、同意、DLP 与幂等事件

**主要文件：** `backend/app/memory_core/events.py`, `candidates.py`, `dlp.py`, `policy.py`, outbox migration/API/tests。

**必须交付：**

- 观察事件不可变、带来源、版本、请求/会话/任务关联和幂等键；原文与派生字段分级保存。
- Secret/DLP 扫描在持久化前执行；默认拒绝 token、密码、私钥、Cookie、连接串和高敏 PII。M2 不得先于 DLP。
- consent、retention、tenant policy、legal hold 进入同一授权判定；异步 outbox 有重试、checkpoint、DLQ 和人工恢复记录。
- DEBUG 不能接受任意 Bearer 或固定测试 Principal；生产身份必须来自真实验证链，测试身份只在显式 test fixture 中存在。

### M3：候选审查、冲突与晋级

**主要文件：** `backend/app/memory_core/review.py`, `conflicts.py`, `projection.py`, `backend/app/api/memory_candidates.py`。

**必须交付：**

- Provider 输出只生成 `ExtractedCandidateDraft`，由 policy、DLP、consent、证据和人工/规则审查后才可晋级 `MemoryItem`。
- 冲突采用确定性版本/来源/置信度策略；覆盖、合并、拒绝、待审都有审计事件。
- active MemoryItem 必须有 provenance、evidence refs、owner、visibility、retention、policy revision；不能由 LLM 单步写入。

### M4：智能检索与上下文编排

**主要文件：** `backend/app/memory_core/retrieval.py`, `backend/app/context_orchestrator/{contracts.py,providers.py,reducers.py,token_budget.py,service.py}`。

**必须交付：**

- Working/episodic/semantic/procedural 四类记忆与 Knowledge EvidencePacket 分层检索、授权过滤和 token budget。
- 融合顺序、去重、时间衰减、来源优先级和冲突提示可解释、可测量；任何记忆都不能越 tenant/subject/policy 边界。
- 输出 `MemoryContextPack` 和 `AgentContextBundle`，不把内部数据库字段、授权理由或敏感原文直接暴露给 LLM。
- 对话记忆与企业事实分离，知识回答必须仍以证据包为准。

### M5：生命周期、保留、删除与恢复

**主要文件：** `backend/app/memory_core/lifecycle.py`, retention/legal-hold migration/API/tests、checkpoint/DLQ worker。

**必须交付：**

- 明确 active/expired/archived/deleted 状态、衰减和保留策略；legal hold 阻止删除并留下审计。
- 删除必须覆盖原始观察、候选、active item、派生索引、对象存储和缓存；幂等、可重试、有 checkpoint 和 DLQ。
- 软删/硬删、租户级清理、主体级删除、撤回 consent、恢复失败均有真实集成测试，不能只用全局 mock。

### M6：真实服务投影与端到端验证

**主要文件：** ES/Neo4j/S3(或 MinIO) projection adapters、`docker-compose` 测试 override、`tests/e2e/`。

**必须交付：**

- PostgreSQL/对象存储作为真源；ES/Neo4j/缓存为可重建投影；MinIO 的 put/get/delete 必须是真实实现。
- 端到端测试使用独立数据库、独立 bucket、独立 Redis namespace，不受 `backend/tests/conftest.py` 的全局 mock 污染。
- 覆盖摄取→候选→审查→晋级→检索→删除→投影重建→权限隔离全链路。

### M7：运行、监控和发布候选

**主要文件：** `docker-compose.memory-test.yml`、配置、健康检查、指标/日志、CI 工作流、运维文档。

**必须交付：**

- feature flag 默认关闭；密钥来自环境/secret store，不进入仓库；迁移、worker、API、投影可独立健康检查。
- 指标包含摄取、DLP 拒绝、候选晋级、检索命中、删除延迟、DLQ、越权拒绝、p50/p95/p99 和成本/Token。
- CI 运行 lint、unit、contract、real-service E2E、迁移升级/回滚和安全扫描；部署动作只生成候选包，不执行生产部署。

### M8：封闭评测与发布门禁

**主要文件：** `eval/agent_memory/`, sealed holdout 数据清单、独立 evaluator、签名报告、release checklist。

**必须交付：**

- sealed holdout 与开发集物理隔离；评测器不能由被测服务自身生成或修改答案。
- 覆盖事实、时间、冲突、多跳、无答案、越权、提示注入、删除和跨租户泄漏；报告 recall/precision、citation、ACL leakage、删除完成率、延迟和成本。
- 报告包含代码/合同/数据集 hash、运行环境、阈值、失败样本和签名；任一 P0/P1 或阈值失败都阻止 release。
- 最终只允许输出：`production_release_eligible=YES|NO`、`production_deployed=NO`、`production_enablement=NO`。

## 四、最终总门禁

### Python

```powershell
Set-Location D:\dingsun\esRAGPro\backend
uv sync --all-extras
uv run ruff check app tests
uv run python -m pytest -q
uv run python -m pytest tests/contract tests/security tests/e2e -q
```

### MCP Rust

```powershell
Set-Location D:\dingsun\esRAGPro\esragpro-mcp
cargo fmt --check
cargo metadata --locked --format-version 1
cargo clippy --locked --all-targets --all-features -- -D warnings
cargo test --locked --all-targets
cargo test --locked --all-targets --all-features
cargo build --locked --release
```

### 必须人工阅读的证据

- `git diff --check` 无输出。
- secret/DLP 扫描无真实秘密；失败用例不会被删掉或改成非严格 xfail。
- 迁移升级、回滚、重试、DLQ、legal hold、跨租户越权和真实 HTTP 多进程测试有日志与退出码。
- 独立规格审查和独立安全审查都为 PASS；否则状态是 `REMEDIATION_REQUIRED`。
- Git 工作树只允许保留用户原有的 `.omx/` 等明确改动；本工作包路径提交后为空。

## 五、交付物清单

1. 本计划及每个工作包的 implementation/status commit。
2. `EXECUTION-STATUS.md` 最新状态摘要（不能覆盖历史记录）。
3. R4A/R4B 的协议、会话、合同、真实进程测试报告。
4. Agent Memory V1 合同、ADR、迁移、API、策略、DLP、生命周期、E2E 和评测报告。
5. Claude Code/Qwen3.7 Plus 执行日志：每个工作包的红灯、绿灯、审查、修复、提交 hash、命令和退出码。
6. 发布结论三元组：`production_release_eligible`、`production_deployed`、`production_enablement`。
