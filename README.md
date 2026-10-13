# SaaS Support Agent

面向项目协作 SaaS 的智能客服 Agent，为企业用户提供产品知识咨询、工作空间与项目状态查询、个人通知设置办理及支持受理。

## 当前状态

**当前仅完成 Phase 1 架构与设计契约，业务功能尚未实现。**

本仓库目前包含项目元数据、最小 Python 包标识和设计文档。尚无运行入口、模型调用、数据库、业务接口、RAG 接入或可执行评测。不能据此声称系统已经可用或指标已经验证。

## 职责边界

| 分支 | 职责 | 事实来源 |
| --- | --- | --- |
| Knowledge | 产品规则、权限说明、通知机制、套餐说明和排查知识 | 版本化知识文档，经 RAG 检索 |
| Query | 当前工作空间、套餐、本人项目访问、通知设置和工单状态 | Business Service / Repository |
| Action | 受控修改个人通知设置、创建支持工单 | 确定性业务规则、持久化结果与操作回执 |

静态知识不能替代实时业务查询。LLM 负责语义理解和解释，代码负责授权、校验与执行。只有业务函数返回的持久化结果才能证明操作成功。

## 架构

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

## V1 范围

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

文档描述的是待实现行为，不是运行验证报告。后续使用的本地示例数据需明确标注；外部集成、部署状态与效果指标仅按实际完成情况披露。不声称真实客户、生产规模或商业上线。

## 工程状态

Python 版本基线为 3.12 及以上，尚未验证更多版本兼容性。`pyproject.toml` 目前仅用于元数据，不包含构建后端、第三方依赖或启动命令；本阶段不承诺安装或运行流程。

后续按需建立 core、memory、services、repositories、workflows、rag、tests 与 data 目录，本阶段没有创建空模块。下一阶段须单独确认范围。
