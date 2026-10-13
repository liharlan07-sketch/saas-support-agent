# 两个 Action State Machine

状态：行为设计，未实现任何状态机、服务调用或数据库写入。

## 1. 共用约束

两个主要写操作为 update_notification_preferences、create_support_ticket。Intent notification_update 映射到前者；support_ticket_create 与 human_handoff 映射到后者。不存在单独的人工转接写接口。

确认必须绑定 user_id、workspace_id、action_type、目标、规范化 payload、pending_action_id、revision、payload_hash、operation_id 与有效期。用户看到的摘要必须完整覆盖实际副作用。摘要不能隐藏额外写入。

同一版本重试复用 operation_id；改变参数、对象或工单提交内容后生成新版本及 operation_id。服务收到相同 operation_id 但不同 payload_hash 必须拒绝，不能覆盖已有结果。

## 2. notification_update

```text
understand → fill_slots → validate → build_pending_action
→ pending_confirmation → confirmed → revalidate → execute
→ succeeded / failed

执行开始前：用户取消 → cancelled
参数/对象/摘要改变：旧确认失效 → validate → 新版本 → pending_confirmation
相关状态变化或确认过期：重新校验/展示 → pending_confirmation
```

这些名字首先描述处理阶段；不要求每一阶段写入数据库。持久化事实仅由业务记录和 OperationReceipt 表达。

| 阶段 | 条件与动作 | 不能发生的事 |
| --- | --- | --- |
| understand / fill_slots | 理解类型、渠道、新值，解析 workspace | 不能因用户问“能不能开启”直接操作 |
| validate | 检查可信用户、active workspace/member、本人范围、白名单；读取旧值与 version | 不让 LLM 判断授权，不接收 project override |
| build_pending_action | 冻结 workspace、本人、字段、新值、期望版本，生成操作标识与期限 | 不继承旧任务确认 |
| pending_confirmation | 展示旧值→新值及范围，等待用户显式确认 | Query 或沉默不等于确认 |
| confirmed / revalidate | 验证确认绑定、期限、当前 scope、权限和业务版本 | 不因高 confidence 跳过复核 |
| execute | 在事务内进行版本条件更新，并写成功回执；提交后才返回成功 | 不先回复成功后再尝试保存 |

相关状态变化：成员被停用则 denied；偏好版本变化则重新查询并展示，旧确认不能继续使用；不相关字段变化不必误作同一偏好冲突。已经是目标值且确认前后版本未变可返回 succeeded、changed=false 的回执，不重复修改。若版本已变，即使新值碰巧一致，仍先解释变化并重新确认。

## 3. create_support_ticket

```text
collect_context → build_summary → user_review
→ pending_confirmation → confirmed → revalidate
→ create_ticket（业务记录＋回执同事务提交）
→ OperationResult.status=succeeded
   data.ticket.status=waiting_for_agent
```

- 收集 issue_type、description、必要 scope 和可选项目/步骤。
- build_summary 生成可编辑问题摘要与有限 context_snapshot，user_review 展示实际将提交的全部内容。
- 用户修正摘要或交接内容后，重新生成 pending 版本并展示。确认之后禁止用 LLM 重新生成另一份摘要再提交。
- revalidate 验证成员归属、项目引用可用、payload_hash、确认期限及幂等标识。
- 创建和回执原子提交，返回 ticket_id / receipt_id / waiting_for_agent，不称“已接通人工”。
- 已有本人的同问题待受理工单时可提供查询，避免自动重复创建；明确要求另建记录则建立新 pending 并确认。不能按描述文本相同强行合并不同请求。

## 4. 多任务与确认失效

| 事件 | 规则 |
| --- | --- |
| 第二个写请求到来 | 原 pending 不变；先让用户选继续原操作或取消原操作并进入新操作；未选不覆盖 |
| 选择切换 | 旧 pending cancelled，清除其确认；新请求重新校验和展示 |
| 修改 notification type/channel/enabled | 原确认失效，revision 增加，新的 operation_id |
| workspace/project/目标改变 | 原 scope 事实不能直接复用；检查归属，重建待确认内容 |
| 插入同 scope Query | 暂存原任务，查询结束重验期限、payload、权限与目标版本；恢复不等于执行 |
| Query 跨 scope | 查询与原任务状态隔离；恢复原 scope 需显式恢复并重新确认 |
| 查询工单后返回设置任务 | 不用 ticket_id 覆盖设置对象；恢复前重验 pending confirmation |
| 已确认后发生修改 | execute 前修改使旧确认失效；执行已开始则不能再改变执行载荷 |
| 重复确认成功操作 | 当前身份授权校验后按 operation_id 读取原回执，不再次写入 |

## 5. 并发、取消、异常

1. 同会话状态推进串行化；多个会话更新同一偏好靠数据库条件更新/唯一约束保证，不能只依赖会话锁。
2. 执行前，事务内或同等一致性边界再次保证授权和目标版本；检查后到写入之间不能留下未防护的状态竞争。
3. execute 前取消只清理对话状态，不创建业务记录。execute 已开始时不能宣称成功撤销；先核对执行结果，返回实际状态。V1 不提供自动补偿或撤销写操作。
4. 技术重试必须等待前次执行结果确定并复用同一 operation_id。相同 ID 已成功返回原回执；已记录失败返回已知失败。用户明确重新尝试失败业务操作时重新建立并确认一个新操作。
5. 数据库超时或提交结果未知返回 pending_resolution；按原 operation_id 查询结果，不能立即生成新 ID 重做。首版使用单数据库事务，不承诺分布式 exactly-once。
6. 确认过期返回 needs_confirmation，参数缺失返回 needs_clarification，无权限返回 denied。取消返回 cancelled，已知执行失败返回 failed。所有写操作成功必须有真实提交依据。
7. 日志只记录 request_id、operation_id、阶段和错误类别等必要字段，不记录密钥或未脱敏的完整上下文。

## 6. 状态机核心不变量

- 未确认、不完整、过期、越权或跨 scope 请求都不能写入。
- 用户确认的 payload 必须等于执行 payload。
- 一个 operation identity 最多对应一个已提交业务副作用。
- 成功回执与实际业务结果一致。
- 工单 waiting_for_agent 与操作 succeeded 同时成立，但意义不同。
- Memory 不能作为授权来源或执行成功证明。
