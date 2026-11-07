# 工作约定

## 阶段与范围

当前已完成设计、通知修改、Task Context、离线商家售后闭环、可选 Milvus Lite 检索、3A HTTP 接口、3B 可选模型适配和 3C/3D 本地验收与发布准备。默认本机单进程、合成数据、离线规则；真实生成式模型端到端、云端 Milvus、生产认证和完整 Conversation Context 未验收或未实现。最终状态以 docs/release_validation.md 为准；不能把本地结果包装成生产效果。所有者已确认自有代码的公开授权并选择 MIT（2026 liharlan07-sketch）；Git 使用公开署名和经 GitHub 设置核实的 noreply 邮箱。首次发布已单独授权，后续 commit/push 仍须明确授权。

云端仅为可选部署方向。保留 Lite 为默认演示，未来单独确认客户端注入、客服独立 collection 和权限范围；禁止沿用人才项目数据集合。现有索引不为迁移提前改动。

- source project / migration source：Lingyu（lingyu_chat）与 SmartReading（smart_reading）仅为原始迁移来源，只读，不整体复制、不修改或删除；不作为最终业务模块名。
- 固定顺序：Trusted Identity Context → Load Memory → Intent/Slots/Confidence → Validation/Entity Resolution/Missing Slots → Deterministic Router → Branch Execution → Unified Result → Update Memory。
- Knowledge 使用文档证据；Query 使用业务服务；Action 以真实持久化结果为准。
- 两个主要写操作仅为 update_notification_preferences、create_support_ticket。人工支持请求复用后者。
- Workspace 在 V1 中承担 tenant isolation boundary，不等同于永久 Tenant 定义。
- 通知设置只属于用户在 Workspace 内的个人设置，不增加项目覆盖。
- 最多一个 pending write；第二个写请求必须明确选择继续原操作或取消原操作后切换。Query 插入后恢复须重新验证确认。
- TaskContext、PendingAction、Confirmation 为对话状态结构，不默认对应数据库表。
- Task Context 使用进程内 Store，按 user/workspace/session 隔离；收集参数时保存 Slots，准备完成后只保留 PendingAction 引用，活跃状态从原 Workflow 投影。TaskContextManager 与 Workflow 配对共享生命周期，不另建权威执行状态。
- Query 不修改任务；显式 resume 重新校验后仍须确认。pending_resolution 不可取消、覆盖或 cleanup；取消/完成/已知失败后新任务不继承旧 Slots。跨重启恢复未实现。
- 确认绑定具体对象、字段、值和版本。工单摘要在确认前生成并展示，确认后不得重新生成不同内容再提交。
- OperationResult.status=succeeded 与 SupportTicket.status=waiting_for_agent 分开表达。
- 幂等绑定规范化 pending action / operation identity，不以用户文本相同为依据。

## 模块命名与职责

- 正式模块名使用职责型名称，不使用来源工程品牌名。README 核心架构、模块职责、对外介绍、简历与面试主叙事统一使用以下名称。
- **Conversation Intelligence**：Request Understanding、Intent Recognition、Slot Extraction、Confidence、Conversation Context、Task Context。
- **Knowledge Support**：Query Rewrite、Retrieval、Filtering / Dedup、Rerank、Evidence、Citation、No-evidence / fallback。
- 对外架构为 `User Request → Conversation Intelligence → Deterministic Router → Knowledge / Query / Action`，其中 `Knowledge → Knowledge Support`。该概览不取消既有可信身份、参数校验、授权、确认与执行前重新校验要求。
- Conversation Intelligence 不负责授权或业务写入；Knowledge Support 不维护独立业务会话、不决定权限、不修改业务数据。Router 保持确定性分支选择。
- 原始工程名称仅允许出现在 migration notes、source / origin explanation 或 development history 中，并标明 `source project / migration source`；不能用于最终模块名。
- 上述为待接入模块的正式命名，不代表已经实现。实际迁移时再考虑 conversation/（intent、slots、context、routing）与 knowledge/（indexing、retrieval、rerank、evidence）目录；目录归属不改变职责和信任边界。不为命名提前创建空目录、重命名既有业务类/函数或引入依赖。

## 变更与沟通

使用中文，先说明结论；改动前说明范围和原因。最小必要变更，保留易读业务规则。现有有效调研可复用；当技术路线或需求实质变化时，先检索相关 GitHub 项目、库或 Skills，说明维护、许可证、兼容性与引入成本，不为复用引入不必要依赖。

复杂开发先给简版方案并确认；已确认方案继续执行，不重复询问已决定事项。仅对影响范围或行为的未决事项集中提问，可查证事实自行查证。

未经明确授权，不安装或新增依赖、不创建虚拟环境、不执行测试/构建/服务启动/付费调用、不 git init/commit/push/创建 PR。不得读取或输出真实凭证。配置示例只放空值或说明。

每阶段执行与范围相称的静态检查，交付说明具体修改位置、检查结果及未验证事项。有代码改动时打开 Codex review 供检查；非 Git 目录如 review 不可用，说明限制并打开源文件，不为查看 diff 初始化 Git。

## 本地运行环境

- 使用 uv 和项目独立 `.venv`，Python 固定为 3.14 系列；版本约束见 `.python-version` 和 `pyproject.toml`。
- 保留 `uv.lock`，不提交 `.venv/`。当前不安装项目包，不新增构建后端，测试命令为 `PYTHONPATH=src uv run --locked python -B -m unittest discover -s tests -v`。
- 新增依赖仍需授权；环境统一不代表已验证未来 LLM/RAG 依赖的兼容性。

## 用户的代码所有权

采用用户于 2026-10-13 确认的“Vertical Slice 优先 + 里程碑复盘”。在已授权范围内持续实现、验证，不因每个新字段或工程术语暂停教学；不扩展为完整后端课程。用户重点掌握 Intent/Slots/Router、notification_update、确认与幂等、Task Context Memory、RAG no-evidence/fallback 和评测标准。

改变 Phase 1 契约、业务范围、依赖、数据模型核心边界、Confirmation/Idempotency/Memory 语义或需要明显替代方案时，先说明问题和建议，必要时确认；普通文件拆分、helper 和字段命名自行处理。

每个 Slice 以链路运行、自动化测试通过、真实数据库状态/回执可核对和失败不虚报成功为完成标准。完成后按下方 Ownership Evidence 的八项结构交接；其中保留 8–15 行核心调用链、3–5 个代码入口，以及最多 2–3 个重要知识点。等待用户复盘后再进入下一 Slice。

本地版本管理采用已授权的里程碑提交，阶段完成且验证通过后形成清晰 commit，不逐文件提交半成品。远程创建、remote 配置和 push 必须另行明确授权。

## Ownership Evidence

自 2026-10-15 起，从下一开发阶段开始，在完成 Vertical Slice、核心模块或重要重构时同步记录真实项目 ownership 与实现证据。先解决业务问题，再记录事实，不改变开发优先级，不为简历增加功能或基础设施。已完成的 notification_update 不因本规范重写代码；如后续复盘，只依据既有实现、测试和真实失败记录补充证据。

### 里程碑交接的八项结构

1. **Business Problem**：本次实际解决的业务问题，以及真实项目协作 SaaS 场景为什么需要它。
2. **Architecture Decision**：实际考虑过的可选方案、采用当前方案的理由。沿用既有确认设计时明确写“沿用既有设计”，不能编造本轮方案比较或重新决策过程。
3. **Core Ownership**：按下方 A/B/C/D 区分代码来源、用户参与及已确认的掌握程度，不将助手产出转述为用户亲手实现。
4. **Implementation Evidence**：核心调用链、关键类/函数、数据或状态流转，以及真正改变业务状态的位置；给出可核对的文件/函数入口。
5. **Testing / Evaluation Evidence**：实际运行了什么、谁执行、环境与数据来源、实际检查的数据库/回执/副作用或指标、结果和证据等级。能提供时记录命令、测试名称、日期与对应版本；不得只写“测试通过”。
6. **Real Bug / Failure Case**：有价值的失败测试或场景、现象、根因、定位过程、修复方法、是否补充回归测试。区分开发中实际发现的问题与主动注入的预期故障；不记录普通拼写或机械调整充数。
7. **Trade-off**：实际取舍、采用最小可行范围的原因、延后能力及原因、当前方案获得与牺牲的能力。未讨论或未验证的替代方案不得伪装成已进行比较。
8. **Current Boundary**：分别列出已经实现、已经设计但未实现、下一步扩展、明确不做；下一步建议不代表已获实施授权。

某项本轮没有真实内容时直接写“本轮无”，不为填满格式而虚构。证据记录用于后续 README、简历和面试复盘，不在开发阶段自行夸大包装。

### Core Ownership 的事实边界

- **A**：用户亲手写过的核心代码。需要实际依据，不因用户提出需求、确认方案或拥有仓库就归入 A。
- **B**：用户能够完整解释、修改和 debug 的代码。未确认掌握情况时标注“待确认”，不根据阅读过交接摘要自动推定。
- **C**：Codex 生成，用户参与设计、review、运行测试和验证的代码。逐项记录实际参与环节；例如“用户确认设计，Codex 执行测试，用户 review/独立验证尚待确认”，不能笼统声称全部参与。
- **D**：仅了解用途，不应包装成用户核心实现的代码。若了解程度也未确认，应如实标注；A/B/C/D 用于区分事实维度，不是自动晋级标签。
- 禁止把 C/D 写成用户亲手从零实现。用户重点负责业务理解、架构判断、核心设计、模块边界、状态/接口决策、关键流程 ownership、review 与验证；Codex 可按已确认设计协助样板、CRUD、wiring、测试、重复工程代码和文档，责任分工不等于这些活动已经发生。
- Router、Confirmation、Revalidation、Idempotency、Task Context、RAG fallback、Evaluation correctness 的设计与代码入口必须清晰，便于后续用户重点掌握。

### 结果证据等级

| 等级 | 可以记录的内容 | 必须注明的边界 |
| --- | --- | --- |
| Level 1：真实运行结果 | 实际数据库状态变化、实际接口执行结果、测试执行结果 | 标明本地/真实外部环境、合成/实际业务数据及执行者；本地运行不等于生产效果 |
| Level 2：离线 Evaluation | 固定评测集上实际跑出的 Accuracy、Recall、F1 等 | 记录评测集范围、定义和真实结果；未跑不报数字 |
| Level 3：结构 / 能力验证 | 状态机路径、失败分支、流程与数据状态符合预期的验证 | 给出具体验证依据；不当成业务收益或生产效果 |
| Level 4：设计目标 | 未实现或尚未验证的能力、目标及下一步 | 只能作为目标，不能写成实际结果 |

所有效果和验证结论标注适用等级与范围。可将一次测试的执行结果标为 Level 1，将其支持的有限能力结论标为 Level 3；不要混淆两者。禁止将 Level 4 升格为实际结果，禁止虚构准确率、提升比例、并发量、QPS、用户数、客户规模、上线或使用效果。

本地 SQLite、合成数据、模拟 Workspace、设计文档、本地测试和未接入真实系统的内部接口，必须如实标注；不能描述成企业生产系统、真实客户上线、商业效果、生产级并发能力或已接通真实客服平台。README 同样遵守。

### 值得用户掌握的点

每次交接最多选 2–3 个真正重要的工程概念：P0 为必须掌握的项目核心/面试高价值能力，P1 为理解即可、不要求从零实现，P2 为知道用途即可。每个 P0 按“业务问题 → 旧方案为何不够 → 工程方案 → 当前实现 → 失败案例 → 如实的面试表达”解释。文件名、字段名、内部 Phase 名及助手临时抽象不作为重点知识。

保持当前三类分支、正式职责型模块名称及两个主要写操作不变。不为 evidence 增加 Redis、微服务、消息队列、通用 Workflow Engine、复杂 DDD、分布式事务、Kubernetes 或无关基础设施；先满足真实闭环、可验证、可解释、失败处理和清晰边界。

## Code comments and documentation

### 当前学习阶段

- 优先级：理解正确 → 能够解释 → 能够修改/debug → 注释语言美观。源码保持工程风格，不写成教程或学习笔记。
- 代码标识符、函数名、变量名、类名、文件名和数据库字段继续使用英文。
- 关键业务规则与安全边界的 comment/docstring 优先用简洁中文解释业务含义和设计原因，并保留必要英文术语。包括 Workflow/State Machine、PendingAction、Confirmation、Revalidation、Idempotency、Transaction、OperationReceipt、Permission/Authorization、Failure Case、Retry/Reconciliation 和状态失效。
- 注释解释 WHY：为什么不能直接执行、为什么状态不能覆盖、为什么旧确认必须失效，以及接口保证和失败边界。不逐行描述 WHAT，不解释简单 Python 语法，不翻译变量名，不写中英双语重复段落。
- 优先通过清晰命名和小而聚焦的函数表达逻辑；只在注释确有价值时添加。必要时可用简短的 `Key concepts:` 列出英文术语，不强制套模板或罗列长术语表。
- 示例：

  ```python
  """处理可信调用方触发的显式确认事件。

  自然语言中的“确认”不能直接作为执行授权；确认必须绑定当前待执行操作、
  版本与参数，避免“确认 A、执行 B”。

  Key concepts: explicit confirmation / trusted caller / stale confirmation
  """
  ```

### 应用范围与最小改动

- 从现在起应用于新增代码、本来需要修改的代码，以及用户明确指定且确实影响理解的关键 docstring/comment。
- 当前学习重点为 `src/saas_support_agent/workflows/notification_update.py`、`src/saas_support_agent/services/notification_service.py`、`src/saas_support_agent/core/models.py`，以及 prepare、revise、confirm、execute、reconcile 等核心函数。这是后续局部优化的优先范围，不是立即批量修改授权。
- 用户指定函数时，只修改对应函数附近必要的注释。不得为统一语言扫描全仓库、批量翻译、改格式、改函数或变量名、修改业务逻辑，或顺手修改其他文件。
- 长篇学习说明放在项目文档或 review/交接说明中，不放进实现文件。
- 当前 README 和学习文档以中文为主；Git commit 保持简短、规范的英文。中文注释不代表最终公开版本必须保留原样。

### Publication Cleanup（公开前单独开展）

- 当前不提前执行。准备正式公开 GitHub 仓库时，再单独确认并开展 Publication Cleanup。
- 届时按公开受众合理统一 README、architecture 和 public docs 的英文或中英表达；代码注释以简洁英文为主，保留有价值的业务规则与设计原因。
- 清理明显教学式注释和个人学习痕迹，检查 secret、`.env`、本地路径和培训来源痕迹，统一必要的命名与文档风格。敏感检查不得输出凭证，不改变既有敏感文件读取约束。
- 清理展示形式不改变真实来源、贡献归属、测试结果和实现边界；不借此虚构原创、生产经历或业务效果，也不代表已授权 commit/push。

## 对外真实性

文档、代码和接口说明按正常企业 SaaS 系统定位表述，明确当前实现阶段、示例数据和真实集成边界。不虚构客户、上线规模、团队人数或未测指标。不称人工待受理任务为已接通客服。

## 明确不做

不添加权限修改、套餐升级、退款支付、完整项目管理/计费系统、Text-to-SQL、ChatBI、Multi-Agent、Supervisor、Redis、微服务、Kubernetes 或自主规划。BM25/MMR/Hybrid/RRF 与额外基础设施须有实际评测或需求依据，并单独确认。
