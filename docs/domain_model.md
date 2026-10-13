# Minimal Domain Model

状态：逻辑模型设计，未创建数据库、迁移脚本或 Repository。

## 1. 边界与授权

Workspace 在 V1 中承担 tenant isolation boundary，不定义为永久 Tenant 等价物。User 可以拥有多个 Membership；未来可增加 Organization/Tenant 上层模型。

可信身份来自应用适配器，业务层始终校验 active Membership。角色仅 owner / admin / member。所有角色均只能通过本客服修改本人通知设置、查看本人支持工单；管理员角色不自动赋予本客服跨用户写权限。套餐信息查询只返回工作空间状态和功能集合，不返回支付或账单敏感数据。

跨 workspace、非本人及不可见对象访问在外部统一为 scope/permission 错误，不能通过错误差异枚举他人项目或工单。项目不可见时不返回他人项目细节；已知可见项目的权限不足可说明当前允许的访问级别。

## 2. 八类业务对象

| 对象 / 主键 | 最小字段 | 关系、唯一性与所有权 |
| --- | --- | --- |
| User / user_id | display_name、status(active/disabled) | 全局身份；名字不作主键；不存密码或凭证 |
| Workspace / workspace_id | name、status(active/suspended)、plan_code、version | V1 隔离根；plan_code 为只读业务状态，无计费实体 |
| Membership / membership_id | workspace_id、user_id、role、status(active/inactive)、version | FK Workspace/User；UNIQUE(workspace_id,user_id)；定义成员归属 |
| Project / project_id | workspace_id、name、status(active/archived)、version | 属于唯一 Workspace；提供 UNIQUE(workspace_id,project_id) 供复合引用 |
| ProjectAccess / access_id | workspace_id、project_id、user_id、access_level(viewer/editor)、version | 引用同 scope Project 和 Membership；UNIQUE(workspace_id,project_id,user_id)；无 grant 代表无显式访问，V1 无角色继承 |
| NotificationPreference / preference_id | workspace_id、user_id、notification_type、channel、enabled、version | 属于本人 Membership；UNIQUE(workspace_id,user_id,notification_type,channel)；不存在 project_id |
| SupportTicket / ticket_id | workspace_id、requester_user_id、issue_type、summary、context_snapshot、project_id(nullable)、status、created_at、version | 创建者所有；project 若有必须同 scope；context_snapshot 是用户确认的有限交接信息 |
| OperationReceipt / receipt_id | workspace_id、user_id、operation_id、pending_action_id、action_revision、action_type、payload_hash、status、result_snapshot、error_code、created_at | UNIQUE(workspace_id,user_id,operation_id)；仅两个 action_type；重复调用返回同一回执，参数哈希不一致则冲突 |

所有关键时间使用有时区时间，存储语义统一为 UTC，展示按明确时区转换。version 为用于并发校验的业务版本，不是模型置信度。

ProjectAccess 的“无显式访问”不等同可向任何人泄露项目存在；先做请求范围和对象可见性检查。这个简单访问模型是本项目 V1 规则，不声称覆盖其他协作平台。

## 3. NotificationPreference

- 用户在 Workspace 内的个人设置；notification_type 为 task_assigned / comment_mentioned，channel 为 email / in_app。
- 首版一个操作只改一个单元的 enabled，不支持项目覆盖、继承、批量更新或替他人修改。
- 未存储单元使用明确默认值 enabled=false、version=0；首次更新物化记录，唯一约束解决并发创建。
- 修改前读取有效值与版本；确认内容包括工作空间、本人、类型、渠道、旧值和新值。
- 保存设置不代表真实邮件已投递，系统不实现消息投递引擎。

## 4. SupportTicket 与 OperationReceipt

人工请求复用 create_support_ticket。创建后 SupportTicket.status 固定为 waiting_for_agent，OperationResult.status 为 succeeded。SupportTicket 后续可使用 in_progress / resolved / closed 作为可查询业务状态；V1 客服没有修改这些状态的 Tool，不新增第三个写操作。

工单创建存储已确认的 summary 与 context_snapshot。context_snapshot 不自动包含全量历史、秘密、其他 scope 内容或未经确认的模型推断。摘要正文和快照发生变化必须重新确认。

OperationReceipt 记录已完成的 succeeded / failed 操作尝试及结构化结果。超时或提交结果不明属于上层 pending_resolution，先按 operation_id 查询回执，再决定是否重试，不能伪造失败或成功回执。

成功业务写入与成功回执必须在同一事务提交；已知回滚失败可以单独记录失败回执。基础设施完全不可用时不能保证失败回执存在，应返回 pending_resolution 或明确的未执行错误并保留 request_id。不存在回执不自动证明远端/数据库操作未执行，需等待执行终止或可确认的回滚。

## 5. 业务状态与 Memory

| 信息 | 权威位置 | Memory 可保存什么 |
| --- | --- | --- |
| 成员归属、权限、套餐、偏好、工单状态 | Repository | 带 scope/observed_at/version 的快照，执行前重新查询 |
| 写操作结果 | OperationReceipt＋业务记录 | 回执引用，不能以模型总结替代 |
| 用户当前目标、已确认槽位、排查步骤 | TaskContext | 事实来源、任务与实体归属 |
| 待确认动作及确认内容 | PendingAction / Confirmation | 当前会话的状态结构，非默认数据库表 |
| 产品说明与排查规则 | 版本化知识文档 | 证据引用，不能代替实时状态 |

V1 未承诺进程重启后恢复待确认任务；重启后必须重建并确认，不执行旧内存草稿。已经提交的操作和工单通过持久化回执/业务查询恢复。

## 6. 不新增的模型

本阶段不增加完整 Tenant/Organization、Plan/Billing、审批链、任务管理、客服坐席、通知投递、通用工作流引擎或领域事件表。TaskContext、PendingAction、Confirmation 定义在对话契约中，不因有名字就建立数据库表。
