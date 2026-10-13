# Phase 1：Intent / Slots / Router Contract

状态：设计契约，尚未实现。本文与领域模型、状态机、评测规格共同构成首版基线。

## 1. 固定顺序与信任边界

Trusted Identity Context → Load Memory → Intent / Slots / Confidence → Validation / Entity Resolution / Missing Slots → Deterministic Router → Branch Execution → Unified Result → Update Memory。

可信上下文由受信适配器提供 user_id、active_workspace_id、session_id、request_id。V1 的开发身份适配器不等同生产认证；用户聊天文本或任意客户端字段不能赋予身份和权限。每个对象访问仍需业务服务验证成员关系、对象归属和可见范围。

Workspace 在 V1 中承担 tenant isolation boundary，未来可在其上增加 Organization/Tenant。会话至少按 user_id + session_id 隔离，任务事实按 workspace_id 和实体 scope 绑定。workspace 切换前必须验证成员身份，不跨 scope 自动继承事实。

## 2. 识别输出与对话动作

识别输出：intents（下表枚举列表）、slots（按意图解释）、confidence（模型自报有限浮点数）。校验对象类型、枚举、槽位类型和非有限数值；非法结构不得进入写操作。confidence 不是概率，阈值由未来开发集的 wrong routing / false blocking 权衡确定，当前不写任意数值。

confirm、cancel、correct、continue_original、switch_to_new 属于对话动作，不增加业务 Intent。自然语言确认只能解析为针对当前展示版本的确认信号，不能自行授予权限或构造确认凭据。优先使用明确的界面动作携带 pending_action_id / revision；自然语言确认只能绑定当前可见且无歧义的版本。

未知意图、超范围能力和无法解释的输出进入 unsupported / clarification，不自动落到可写分支。低置信且无法安全确定当前目标时，保留任务但不执行新动作，提出具体区分问题。合法、明确绑定的确认请求仍必须经过状态机校验。

## 3. 八个 Intent

下表 required slots 指进入对应分支所需的语义/已解析字段；user_id、授权、确认凭据不属于模型 slots。除明确说明外，workspace_id 可从已验证的活动工作空间继承。未提供且存在多个候选时先澄清。

| Intent / 含义 | Required | Optional | 最终 branch |
| --- | --- | --- | --- |
| product_knowledge：规则与使用咨询 | question（当前原问题） | topic、feature、product_version | Knowledge |
| workspace_status_query：工作空间与套餐状态 | workspace_id | requested_fields（白名单） | Query |
| project_access_query：本人项目访问状态 | workspace_id、project_id | 无 | Query |
| notification_query：本人个人通知设置 | workspace_id | notification_type、channel | Query |
| notification_update：修改本人通知设置 | workspace_id、notification_type、channel、enabled | 无 | Action → update_notification_preferences |
| support_ticket_create：创建支持请求 | workspace_id、issue_type、description | project_id、steps、observed_error | Action → create_support_ticket |
| support_ticket_query：查询本人支持工单 | workspace_id、ticket_id | 无 | Query |
| human_handoff：请求人工支持受理 | workspace_id、reason | project_id、当前问题细节、existing_ticket_id | Action → create_support_ticket（或已有关联工单的 Query） |

notification_type V1 为 task_assigned / comment_mentioned；channel V1 为 email / in_app。只修改一个类型与渠道组成的偏好单元，不做批量设置。枚举是本系统首版产品规则，不冒称第三方 SaaS 规则。

issue_type V1 为 access / notification / usage / other。human_handoff 的 reason 转为工单 description 输入；生成摘要并展示后才可提交。若 existing_ticket_id 指向当前用户的 waiting_for_agent 工单，查询并说明当前状态，不重复创建、不更新旧工单。不属于同一问题或已有工单结束时，用户可明确发起新请求。

## 4. 每个 Intent 的继承、归一化与校验

| Intent | 可从 Memory 继承 | 禁止继承/自动推断 | Normalization / validation | 缺槽行为 |
| --- | --- | --- | --- | --- |
| product_knowledge | 同任务明确功能、指代实体，作为检索上下文 | 不把旧问题替代当前问题；不把历史回答当产品证据 | 去首尾空白，保留原问题；版本由知识范围校验 | 只有指代且无唯一实体时澄清 |
| workspace_status_query | 已验证活动 workspace | 旧 workspace 的套餐或可用性结论 | 名称只作候选选择；解析为 ID 并重新验证成员关系 | 多 workspace 且无明确目标时选择 |
| project_access_query | 同 workspace 的唯一当前 project | 不跨 workspace 继承 project；不继承旧权限结论 | 名称解析有歧义时选择；服务做 scope/ownership 检查 | 询问项目；不可见/不存在使用统一外部错误 |
| notification_query | 当前 workspace，同任务明确的类型/渠道 | 不把过期偏好当当前配置 | 同义词映射到枚举；未指定类型/渠道则查询本人全部白名单偏好 | 仅缺 workspace 时询问 |
| notification_update | 当前 workspace；同一任务已经明确的类型/渠道/目标值 | 不从普通查询结果推断希望的新值；不复用其他任务的 enabled 或旧确认 | 开/关规范化为布尔，否定歧义时澄清；禁止 project_id 和其他用户目标；业务层校验白名单与归属 | 精确追问缺失类型、渠道或新值 |
| support_ticket_create | 同任务已确认症状、步骤、项目 | 不自动附全部历史；不复制其他 workspace 数据；不将模型猜测写成用户事实 | 规范 issue_type；保留描述原意；摘要/交接信息可编辑；project 必须同 scope 且可访问 | 收集可理解问题描述；步骤非强制 |
| support_ticket_query | 同 workspace、同任务最近的本人 ticket | 不跨用户/workspace 继承；不按任意工单号越权查询 | 解析 ID，检查 requester 与 workspace；V1 不提供他人工单列表 | 没有唯一工单时询问编号 |
| human_handoff | 同任务已确认问题和上下文 | 不静默发送未展示的历史或敏感内容；不假定人工已接通 | reason 必须明确；引用项目/工单需验证归属；创建内容冻结确认 | 询问需要交接的问题，随后展示摘要 |

所有分支共用低 confidence 规则：歧义未解除时不写入；确认主题、对象或动作后再识别/校验。所有分支共用 unsupported 规则：解释支持范围，必要时提供用户主动确认的支持受理选择，禁止隐式创建工单。

规范化 ID 保留原标识语义，不随意大小写折叠；对象名称不得当唯一主键；字符串布尔值需明确解析，不能用非空字符串真值规则。额外 slots 不进入业务参数，无法解释的冲突参数触发澄清。

## 5. Router 与多请求策略

Router 使用校验后的枚举和结构化参数确定分支。LLM 不选择任意函数、不生成 SQL、不绕过业务校验。知识规则与实时状态同时被问及时，按已声明顺序分别取得证据/状态，回答中区分来源。

- V1 对有限 Query → Action 组合使用显式顺序，无自主规划；用户的条件须由工具结果验证。
- 至多一个 pending write，且串行处理当前会话中的状态变更。
- 第二个写请求出现时先保留原 pending，不构造第二个可执行 pending。提示“继续原操作 / 取消原操作并进入新操作”。没有明确选择则不切换、不执行；仅暂存候选请求文本以供用户选择。
- 用户选择切换时，将旧 pending 标记 cancelled，再从新请求重新收集和校验，不继承旧确认。
- 只允许一个有限的临时 Query 插入，保留原任务快照；查询失败也应保留原任务。恢复前检查 identity、workspace、目标、参数版本、期限及相关业务版本。
- 参数或目标变化使旧确认失效；跨 workspace 的查询恢复原任务时也需显式恢复原 scope、重新验证归属和重新确认，不能静默写回旧空间。
- 同 scope、无变化的 Query 插入可保留已展示草稿，但恢复不是自动提交；必须等待用户明确确认。过期或不再适用时重新展示。
- 未支持的复杂复合请求明确列出可处理部分并让用户选择，不静默丢弃剩余意图。

## 6. Memory 状态契约（不默认对应数据库表）

TaskContext：task_id、owner_user_id、workspace_id、intent、entity_refs、confirmed_slots、fact_provenance、attempted_steps、query_snapshots、pending_action、可选 suspended_query。每个 fact 带来源（用户明确/用户确认/工具观测/模型推测）和 scope；推测不能直接成为写入参数。观察结果带 observed_at 和业务版本，执行前必须重新读取关键状态。

PendingAction：pending_action_id、revision、operation_id、owner_user_id、workspace_id、action_type、规范化 payload、canonical_payload_hash、expected_versions、已展示摘要、expires_at、workflow_state。

Confirmation：pending_action_id、revision、payload_hash、owner_user_id、workspace_id、confirmed_at、expires_at。凭据由可信应用生成/绑定，不能从聊天中接受伪造字段。有效期参数在实现阶段明确；必须可配置并进入测试，不在 .env.example 预造变量。

operation_id 标识一次规范化后的操作版本；同版本确认重试保持该 ID，用户改参数/摘要则 revision 增加并生成新 operation_id。不是文本相同就视作同一操作。执行事实以 Repository 为准，Memory 丢失不能通过模型猜测执行结果。

## 7. 统一结果与 RAG 接口

OperationResult：request_id、branch、status、message、data、evidence、missing_slots、pending_action、receipt、error_code。status 集合为 answered / needs_clarification / needs_confirmation / succeeded / failed / cancelled / unsupported / no_evidence / denied / pending_resolution。不同状态字段约束在后续类型实现时固化；没有 receipt 和提交证据不能返回写操作 succeeded。

SupportTicket.status=waiting_for_agent 放在 data 中，与 OperationResult.status=succeeded 分开。失败信息不泄露他人对象或内部凭证。

Lingyu → RAG：request_id、original_question、context_messages（限长投影）、knowledge_scope（应用确定的模块/版本）。RAG → Lingyu：status（answered/no_evidence/error）、answer、evidence、search_query、degraded_reason、可选 trace。生成使用原问题，检索使用 search_query；证据带稳定来源、版本、页码/片段标识。RAG 不维护第二套业务会话，不写业务数据，不决定权限。

## 8. 参考实现迁移登记（尚未迁移）

| 能力 | 分类 | 后续处理 |
| --- | --- | --- |
| 文件内容哈希 | 原代码可直接复用候选 | 仅作内容标识，检查接口后小范围迁移 |
| 请求响应、识别器、结构化输出 | 需要重构 | 统一八意图、槽位与错误契约；主链路只选一套识别方式 |
| History/Summary/Facts | 需要重构、需要修 bug | 隔离、最近窗口、任务归属与确认失效 |
| PDF/Embedding/Chroma | 需要适配、需要修 bug | 切分配置、凭证注入、索引版本与完成标记 |
| 改写/召回/LLM 重排 | 需要重构、需要修 bug | 原问题边界、短指代、空证据和失败降级 |
| Evidence/Diagnostics | 可保留结构思路 | 稳定 ID、序列化与统一状态 |
| 电商/论文角色 Prompt、直接全局字典访问 | 新工程不再使用 | 原工程保留，不删除 |
| Router/Business Service/Repository/Action/Evaluation | 新增实现 | 当前不存在完整业务闭环 |

迁移原则：阅读原实现 → 判断保留价值 → 明确接口 → 必要重构 → 小范围迁移 → 经授权验证。优先修低置信度 trace_id 缺失、用户隔离、密钥注入与导入冲突。BM25/MMR/Hybrid/RRF、Redis 和服务拆分不在 V1 范围。
