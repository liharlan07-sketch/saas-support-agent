# 工作约定

## 阶段与范围

当前已完成 Phase 1 设计与 notification_update 确定性业务办理链路；自然语言、完整任务 Memory、工单与 RAG 尚未实现。任何后续阶段均需用户明确授权，不得将文档中的目标能力描述为已实现。

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

采用用户于 2026-10-13 确认的“Vertical Slice 优先 + 里程碑复盘”。在已授权范围内持续实现、验证，不因每个新字段或工程术语暂停教学；不扩展为完整后端课程。用户重点掌握 Intent/Slots/Router、notification_update、确认与幂等、Task Context Memory、RAG no-evidence/fallback 和评测标准。

改变 Phase 1 契约、业务范围、依赖、数据模型核心边界、Confirmation/Idempotency/Memory 语义或需要明显替代方案时，先说明问题和建议，必要时确认；普通文件拆分、helper 和字段命名自行处理。

每个 Slice 以链路运行、自动化测试通过、真实数据库状态/回执可核对和失败不虚报成功为完成标准。完成后提供项目所有权交接：业务问题、8–15 行调用链、最多 2–3 个 P0/P1/P2 知识点、每个 P0 的问题/原因/方案/实现/失败例子/面试表达、3–5 个代码入口、测试证据与剩余边界。等待用户复盘后再进入下一 Slice。

本地版本管理采用已授权的里程碑提交，阶段完成且验证通过后形成清晰 commit，不逐文件提交半成品。远程创建、remote 配置和 push 必须另行明确授权。

## Code comments and documentation

- Code identifiers, function names, variable names, class names, file names, and database field names must use English.
- Comments and docstrings may use Chinese or English. Prefer concise Chinese for non-obvious business rules, design rationale, and failure cases when it improves clarity. Short technical notes may use either language; do not duplicate the same explanation in both languages.
- README and project learning documents should primarily use Chinese for the intended audience. Git commit messages should remain concise, conventional English, such as `feat: ...` or `docs: ...`.
- Prefer clear naming and small, focused functions over excessive comments. Do not add comments when naming or structure can make the code clearer.
- Comments should explain why a design or business rule exists, rather than restating what the code does.
- Add comments only where they provide real value, especially around business rules, tenant and permission boundaries, confirmation validity, revalidation, idempotency, state invalidation, fallback behavior, retry safety, and non-obvious failure handling.
- Public or core service interfaces may use concise Chinese or English docstrings to describe their contract, important guarantees, and side effects.
- Avoid line-by-line narration. For example, avoid:

  ```python
  # Get user.
  user = get_user()
  ```

- Prefer explanations of the reason for a safeguard, such as:

  ```python
  # 执行写操作前重新校验当前状态，避免使用已经失效的用户确认。
  ```

- Do not add large tutorial-style explanations inside production code. Learning explanations belong in project documentation or review notes, not implementation files.

## 对外真实性

文档、代码和接口说明按正常企业 SaaS 系统定位表述，明确当前实现阶段、示例数据和真实集成边界。不虚构客户、上线规模、团队人数或未测指标。不称人工待受理任务为已接通客服。

## 明确不做

不添加权限修改、套餐升级、退款支付、完整项目管理/计费系统、Text-to-SQL、ChatBI、Multi-Agent、Supervisor、Redis、微服务、Kubernetes 或自主规划。BM25/MMR/Hybrid/RRF 与额外基础设施须有实际评测或需求依据，并单独确认。
