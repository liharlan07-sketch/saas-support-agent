# Task Context Memory — Ownership Handoff

完成日期：2026-10-17。开发测试阶段，本地合成数据与文件型 SQLite；没有外部客户、生产流量或模型效果指标。代码和测试由 Codex 实现并运行，等待用户 review。未提交 Git。

## 1. Business Problem

用户办理通知设置时可能缺少参数、修改目标、临时查询当前设置或取消。系统需要知道“当前在办什么”，避免把旧参数带进新任务，也不能让新写请求覆盖已经等待确认的操作。

本轮在原 notification_update 上增加最小任务协调层：收集缺参、关联草稿、显式恢复、确定性关闭/失效，保留已有确认、事务与幂等机制。

## 2. Architecture Decision

沿用既有设计：一个 pending write、有限只读查询、TrustedIdentity、确认绑定具体操作版本、业务结果以数据库和回执为准。本轮没有重设这些契约。

实现选择：TaskContextManager 在原 Workflow 上协调任务；TaskContextStore 使用进程内字典和锁。考虑过直接在 TaskContext 保存全量 payload/status，但这会形成第二份权威状态，因此只在补参阶段保存参数，prepare 后清除副本、保存 pending_action_id；展示时投影原 PendingAction。没有引入新的框架或依赖。

Conversation History 只能记录说过什么，不能证明授权、当前任务或操作成功。Task Context 使用程序规则决定生命周期；自然语言输入适配层尚未实现，不能直接让 LLM 输出成为执行事实。

GitHub 调研只作职责分离与存储边界参考，没有复制或引入依赖：[BotBuilder state sample](https://github.com/microsoft/BotBuilder-Samples/tree/main/samples/python/45.state-management)、[LangGraph InMemorySaver](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint/langgraph/checkpoint/memory/__init__.py)。本项目不依赖它们的版本、许可证或运行兼容性，不将调研描述为已集成这些框架。

```text
可信身份 + 结构化 Slots
→ start：检查单写限制与成员资格
→ TaskRecord：仅保留当前任务已收集参数
→ update_slots：补齐同一任务
→ 原 prepare：创建 PendingAction，Store 转为仅引用
→ 可插入只读 Query，任务不被覆盖
→ 显式 resume：重验；必要时 revise、重新展示
→ 对当前版本显式 confirm
→ 原 execute：事务内校验、更新设置并保存回执
→ 投影终态；新任务不继承旧 Slots
```

## 3. Core Ownership

- A（用户亲手编写）：本轮无可确认记录。
- B（用户能完整解释、修改、debug）：待 Ownership Review，不能预先认定。
- C（Codex 生成、用户参与）：用户明确业务边界和生命周期要求，Codex 按要求设计细节、实现并执行测试；用户代码 review 与独立验证尚待完成。
- D（仅了解用途）：了解程度尚待确认，不作为用户核心实现陈述。

应重点掌握 `_project` 的权威来源、`update_slots` 如何复用 revise、`resume` 如何保持确认边界，以及 cleanup 为什么不能清除结果未知的任务。

## 4. Code Evidence

以下路径均相对仓库根目录，最多五个入口：

1. `src/saas_support_agent/core/task_context.py`：TaskRecord / TaskContext / TaskContextStore，区分实际存储与只读视图。
2. `src/saas_support_agent/workflows/task_context.py`：TaskContextManager，负责 start/update_slots/cancel/resume/cleanup；不执行数据库写入。
3. `src/saas_support_agent/workflows/notification_update.py`：新增 inspect_pending/has_pending_write/resume；沿用原 prepare/revise/confirm/resolve。resume 刷新不执行。
4. `src/saas_support_agent/services/notification_service.py`：新增 authorize 用于缺参阶段；原 execute/reconcile 保持不变，真正业务写入仍由原事务完成。
5. `tests/test_task_context.py`：29 项任务测试，结合原测试验证中间状态、业务数据及回执。

## 5. P0 Concepts

### P0：Task Context vs Conversation History（任务上下文与对话历史）

业务问题：用户只说“打开邮件通知”，系统需要保留 channel/enabled，同时继续追问 notification_type。旧方案只有完整草稿，聊天文本也不能作为可靠业务参数存储。

工程方案：结构化记录当前任务与缺参；准备后只关联草稿，防止记录与草稿各改一份。

当前实现：TaskRecord.collected → prepare → pending_action_id；TaskContext 的 slots 和 status 从当前权威对象投影。它类似 Agent 的结构化 State，不是聊天消息数组，也不是业务数据库。

Failure Case：若 TaskContext 改为评论通知，但原 PendingAction 仍是任务指派，确认时可能执行旧目标。当前有效修改必须通过原 revise。

如实面试表达：“我参与定义任务与草稿的职责边界，使用结构化上下文收集参数；准备完成后以草稿为权威来源，已有本地测试验证。”亲手实现和掌握程度须按第 3 节实际更新。

### P0：State Invalidation（状态失效）

业务问题：上一任务成功或取消后，下一请求缺少通知类型，不能静默继承上次的类型。只保留一份聊天历史或长期字典不足以判断哪些值仍有效。

工程方案：以 scope + 新 task_id 隔离任务事件，修正走原 revision 机制；终态不输出可继承 Slots。恢复时重验权限、目标与期限。

当前实现：`_require` 拒绝旧 task_id，`_project` 不给终态提供 Slots，`resume` 校验当前业务状态，`cleanup` 仅处理终态。这对应 State/Memory 生命周期，不是让 LLM 判断任务完成。

Failure Case：Query 期间权限被撤销，直接恢复原确认会误执行。当前 resume 拒绝，原 confirm 仍有事务内重新授权。结果未知时不能当成普通失败清理；只能核对回执。

如实面试表达：“任务生命周期由确定性规则控制；取消、完成与结果未知区别处理，已用本地测试验证旧确认不能执行及未知结果关联不会被 cleanup 删除。”

## 6. Real Bugs / Failure Cases

实际发现：首轮 69 项测试中的失效任务清理用例失败。现象是任务关联已失效，cleanup 却返回 needs_clarification。根因是复用的 `_require` 一律拒绝 invalidated，误把“不能恢复执行”扩展成“不能清理”。修复仅允许 cleanup 通过该检查，仍检查 scope/task_id 和 active。原失败用例保留为回归测试，新增撤权失效清理时错误 scope/task_id 拒绝的覆盖。

主动故障注入：COMMIT 已完成后抛出异常并撤销权限，产生 pending_resolution；测试验证 cleanup/cancel/revise/第二写均不能丢掉关联，恢复权限后只读回原回执，数据库仍为 1 条偏好、1 条回执。数据库不可用、回执写入失败也有注入验证；这些不是生产事故。

## 7. Testing Evidence

Level 1：Codex 在 uv 0.12.17、CPython 3.14.7 独立环境实际运行全部 unittest，71 项通过（原 Workflow 28、原 SQLite 事务 14、新任务测试 29）。原两个测试文件未修改，数据为临时 SQLite 合成数据。完整命令、版本边界和分组断言见 evaluation_spec.md 第 10 节。

Level 3：证明这些测试覆盖范围内的补参、修正、scope 隔离、终态不继承、Query/Resume、结果待核对保护和实例内并发 start 守卫。不是自然语言理解准确率或生产可靠性证明。

Level 2 本轮无；自然语言指代、LLM 与 RAG 评测仍为 Level 4 后续目标。未测性能、真实外部系统、强杀/断电、多进程或分布式恢复。

## 8. Trade-off

采用进程内 Store 和一对 Manager/Workflow 使权威状态清晰、改动集中，无需 Redis、LangGraph 或通用调度器；代价是实例级锁串行化、没有多 worker 共享、重启即丢失任务、没有自动空闲回收。

同用户同会话最多一个写任务，跨 Workspace 也受限；这是沿用原 Workflow 的更严格边界。不同 user/session 可以独立持有任务，读取按三元 scope 隔离。

Query 只复用当前通知查询，不为例子新增套餐模型；同步查询无需持久化 suspended_query 或任务栈。TaskContext 不保存查询结果，也不缓存权限结论。

TaskRecord 的 terminal_status 只记录已经关闭的任务；活跃 pending 状态始终投影原 Workflow。OperationResult.failed 可能只是本次读取失败，不等于业务任务失败；临时读故障保留待确认任务，已知执行失败才关闭。

## 9. Current Boundary

| 事件 | 当前规则 |
| --- | --- |
| 新任务/缺参 | 新 task_id，从本次明确 Slots 开始，不继承上次参数 |
| 同任务补参 | 保留 task_id，完整后 prepare，再移交参数权威来源 |
| 合法草稿修正 | 原 revise 更新版本，旧确认失效 |
| 非法或不完整的草稿修正 | 整体拒绝，原草稿不变；返回错误，不声称修改生效 |
| Query 成功或失败 | 暂时保留任务，不自动恢复或确认 |
| 显式 resume | 重新校验；过期/相关状态改变刷新草稿；仍待显式确认 |
| 取消/成功/已知执行失败 | 关闭任务，对外 Slots 清空，可 cleanup 或新建 |
| 关联丢失/补参时恢复发现撤权 | invalidated，不可恢复，可清理 |
| pending_resolution | 保留关联，不取消/不清理/不覆盖，仅核对结果 |
| 第二写请求 | 保留原任务，明确提示继续或先取消，不自动切换 |
| 重启 | 内存任务和草稿丢失，业务数据/回执保留 |

已实现：上表结构化规则；没有自动理解“刚才那个”“取消”等自然语言。可信调用方必须将事件明确传给正确入口。

已设计未实现：Intent/Slots/Router、History/Summary/Facts、Knowledge Support、工单及生产身份认证。下一步扩展须单独授权。

实例约束：复用同一个 Manager/Workflow 配对，任务流不要绕过 Manager 另行推进；不支持运行中独立替换或多个 Manager 共享同一 Workflow 来实现并发任务。get_context/inspect_pending 是可信内部只读接口，快照不是授权结果；面向客户端的认证/API 尚未实现。

重启边界：待确认和结果未知的任务没有持久化恢复索引。持有原 operation_id 可通过原 receipt 查询服务核对已提交结果；没有该关联时不得猜测是否成功或自动重放写操作，本轮没有实现恢复协调器。模拟重启测试通过重建实例进行。

明确不做：新业务写操作、LLM/RAG/Milvus、HTTP/UI、Redis、LangGraph、Multi-Agent、通用 Workflow Engine；不更改已完成的事务和幂等核心。

## 10. Ownership Review Order

1. 读 core/task_context.py：说明 TaskRecord 存什么、TaskContext 展示什么，何时清除参数副本。
2. 读 TaskContextManager.start/update_slots/_project：跟一次“只给邮件渠道 → 补类型和开关”的数据流。
3. 读 NotificationUpdateWorkflow.resume，再看 Manager.cancel/cleanup：区分确认过期、任务终态与结果待核对。
4. 读测试中 revise、Query 恢复、pending_resolution 和失效清理四组用例，核对数据库与回执断言。

三个复盘问题：为什么准备后不能在 Store 再维护一份 payload？为什么查询失败不能直接关闭任务？为什么 invalidated 可以清理而 pending_resolution 不可以？

一个小任务：在现有测试中增加“取消 collecting 任务后，再给旧 task_id 调用 resume 不能恢复”的断言，运行对应测试并解释新任务为什么不应受影响。此项尚未由用户执行。
