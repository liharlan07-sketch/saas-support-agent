# SaaS Support Agent

面向项目协作 SaaS 的智能客服 Agent，目标覆盖产品知识咨询、业务状态查询、个人通知设置办理及支持受理。当前已实现范围见下文。

## 当前状态

**已完成 Phase 1 设计，以及 `notification_update` 确定性业务办理链路。**

已实现：结构化请求、成员与参数校验、待确认操作、版本绑定确认、执行前重新校验、SQLite 持久化、操作回执、幂等核对，以及通知设置/回执的 Python 查询接口。使用 Python 标准库，无第三方依赖。

尚未实现：自然语言理解、Intent/Slots/Router 集成、完整任务 Memory、支持工单、LLM、RAG、HTTP API、UI、生产身份认证。当前通过 Python 调用和自动化测试运行，不代表完整 Agent 已上线。

## 目标职责边界

| 分支 | 职责 | 事实来源 |
| --- | --- | --- |
| Knowledge | 产品规则、权限说明、通知机制、套餐说明和排查知识 | 版本化知识文档，经 RAG 检索 |
| Query | 当前工作空间、套餐、本人项目访问、通知设置和工单状态 | Business Service / Repository |
| Action | 受控修改个人通知设置、创建支持工单 | 确定性业务规则、持久化结果与操作回执 |

静态知识不能替代实时业务查询。LLM 负责语义理解和解释，代码负责授权、校验与执行。只有业务函数返回的持久化结果才能证明操作成功。

## 目标架构（尚未全部接通）

```text
Trusted Identity Context
→ Load Memory
→ Intent / Slots / Confidence
→ Validation / Entity Resolution / Missing Slots
→ Deterministic Router
→ Branch Execution
→ Unified Result
→ Update Memory
```

三个核心方向是 Controlled Business Execution、Task Context Memory、Evidence-based RAG。Evaluation、Fallback 和用户／隔离／权限边界作为贯穿它们的保障，不增加独立大型平台。

## V1 设计范围（工单等能力仍待实现）

- Workspace 在 V1 中承担 tenant isolation boundary；不定义为 Workspace 永久等同 Tenant。未来可增加 Organization/Tenant 上层模型。
- NotificationPreference 仅为用户在 Workspace 内的个人设置，没有项目级覆盖或复杂继承。
- 仅允许 `update_notification_preferences` 与 `create_support_ticket` 两个主要写操作。
- 人工支持请求复用工单创建，工单初始状态为 `waiting_for_agent`。这表示生成并持久化人工待受理任务，不表示已经接通真实客服平台。
- 最多一个 pending write；允许有限 Query 插入，恢复时重新验证确认。第二个写请求不得静默覆盖原请求。
- 不包含权限修改、套餐升级、支付退款、完整项目管理或计费系统、Text-to-SQL、多代理、自主规划、Redis、微服务或 Kubernetes。

## 设计文档

- [Intent、Slots、Router 与上下文契约](docs/phase1_contracts.md)
- [最小领域模型](docs/domain_model.md)
- [两个写操作状态机](docs/workflows.md)
- [评测规格](docs/evaluation_spec.md)

文档同时包含 V1 目标与已实现子集，文首和新增的实现说明明确区分。测试身份、空间及设置都是本地合成数据；没有外部客户数据、商业上线或模型效果指标。

## 当前可运行链路

```text
可信身份 + NotificationUpdateRequest
→ prepare：参数、成员与当前设置校验
→ PendingAction：返回具体修改摘要，等待确认
→ confirm：绑定待确认操作 ID、revision、内容摘要
→ 事务内重新授权，先核对是否已有回执
→ 确认期限与当前业务版本校验
→ 修改设置 + 写入回执，原子提交
→ 返回真实结果
→ query_preference / query_receipt 独立读回
```

- 一个操作只改本人在一个 Workspace 中的一个通知单元，类型为 `task_assigned / comment_mentioned`，渠道为 `email / in_app`。
- `enabled` 必须是 Python 布尔值；不把字符串 `"false"` 或整数 `1` 当作布尔值。缺参数返回 `needs_clarification`。
- 确认默认有效期 10 分钟，可通过 Workflow 构造参数调整；时钟可注入测试。变更参数或需要重新确认时，生成新版本及新操作编号。
- 同一操作重复确认只核对回执；用户新发起的相同目标操作有新编号。目标已满足时可返回 `changed=false`，不重复增加设置版本。
- 设置与成功回执同一 SQLite 事务提交。已知失败返回失败；提交结果未知只核对，不自动重试写入。
- Workflow 实例内按用户/会话保存草稿，并串行处理状态变化。部署时需为该会话复用同一协调实例；当前不支持多进程共享草稿或重启恢复待确认任务。
- `TrustedIdentity` 由受信调用方提供，仅为内部接口；不能将任意客户端提交的用户 ID 当作认证结果。业务服务仍查数据库验证成员与所有权。

## 本地验证

需要 Python 3.12+，在项目根目录运行，无需安装依赖：

```bash
PYTHONPATH=src python3 -B -m unittest discover -s tests -v
```

只运行从准备、确认到独立连接读回的完整正常路径：

```bash
PYTHONPATH=src:tests python3 -B -m unittest test_notification_workflow.NotificationWorkflowTests.test_success_is_persisted_and_readable_from_new_connection -v
```

测试在临时目录创建真实文件型 SQLite，执行后自动清理。`Database.initialize()` 只建表，不自动生成用户数据。测试夹具中显式建立合成用户、空间和成员关系，没有依赖模型或远程服务。

2026-10-13 在 Python 3.12.8 下执行：42 项测试通过。覆盖授权与隔离、确认修正/过期、取消、重复及并发提交、更新/回滚、回执持久化和提交结果不明。异常注入用例出现 `stage=exception` 日志属于预期验证，不代表测试失败。详见评测文档的执行记录。

## 工程状态

Python 版本基线为 3.12 及以上，当前只验证了 Python 3.12.8。`pyproject.toml` 仅用于元数据，不包含构建后端、第三方依赖或服务启动命令；使用上述源目录测试命令运行。

当前已有 core、services、repositories、workflows 与 tests；memory、rag、data 和服务入口尚未建立。没有迁移或修改 Lingyu / SmartReading 原始实现。下一阶段须单独确认范围。
