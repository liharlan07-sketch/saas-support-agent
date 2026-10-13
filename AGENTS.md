# 工作约定

## 阶段与范围

当前仅完成 Phase 1 骨架与设计契约。没有业务功能实现；任何后续阶段均需用户明确授权。不得将文档中的目标能力描述为已实现。

- 原始参考工程 lingyu_chat / smart_reading 只读，不整体复制，不修改或删除。
- 固定顺序：Trusted Identity Context → Load Memory → Intent/Slots/Confidence → Validation/Entity Resolution/Missing Slots → Deterministic Router → Branch Execution → Unified Result → Update Memory。
- Knowledge 使用文档证据；Query 使用业务服务；Action 以真实持久化结果为准。
- 两个主要写操作仅为 update_notification_preferences、create_support_ticket。人工支持请求复用后者。
- Workspace 在 V1 中承担 tenant isolation boundary，不等同于永久 Tenant 定义。
- 通知设置只属于用户在 Workspace 内的个人设置，不增加项目覆盖。
- 最多一个 pending write；第二个写请求必须明确选择继续原操作或取消原操作后切换。Query 插入后恢复须重新验证确认。
- TaskContext、PendingAction、Confirmation 为对话状态结构，不默认对应数据库表。
- 确认绑定具体对象、字段、值和版本。工单摘要在确认前生成并展示，确认后不得重新生成不同内容再提交。
- OperationResult.status=succeeded 与 SupportTicket.status=waiting_for_agent 分开表达。
- 幂等绑定规范化 pending action / operation identity，不以用户文本相同为依据。

## 变更与沟通

使用中文，先说明结论；改动前说明范围和原因。最小必要变更，保留易读业务规则。现有有效调研可复用；当技术路线或需求实质变化时，先检索相关 GitHub 项目、库或 Skills，说明维护、许可证、兼容性与引入成本，不为复用引入不必要依赖。

复杂开发先给简版方案并确认；已确认方案继续执行，不重复询问已决定事项。仅对影响范围或行为的未决事项集中提问，可查证事实自行查证。

未经明确授权，不安装或新增依赖、不创建虚拟环境、不执行测试/构建/服务启动/付费调用、不 git init/commit/push/创建 PR。不得读取或输出真实凭证。配置示例只放空值或说明。

每阶段执行与范围相称的静态检查，交付说明具体修改位置、检查结果及未验证事项。有代码改动时打开 Codex review 供检查；非 Git 目录如 review 不可用，说明限制并打开源文件，不为查看 diff 初始化 Git。

## 用户的代码所有权

用户重点掌握 Intent/Slots/Router、notification_update、确认与幂等、Task Context Memory、RAG no-evidence/fallback 和评测标准。不要一次性实现整个系统。核心设计说明原因，分批交付；重复 CRUD、配置、UI 和测试骨架可由助手承担。

## 对外真实性

文档、代码和接口说明按正常企业 SaaS 系统定位表述，明确当前实现阶段、示例数据和真实集成边界。不虚构客户、上线规模、团队人数或未测指标。不称人工待受理任务为已接通客服。

## 明确不做

不添加权限修改、套餐升级、退款支付、完整项目管理/计费系统、Text-to-SQL、ChatBI、Multi-Agent、Supervisor、Redis、微服务、Kubernetes 或自主规划。BM25/MMR/Hybrid/RRF 与额外基础设施须有实际评测或需求依据，并单独确认。
