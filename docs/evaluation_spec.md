# Evaluation Spec

状态：V1 评测规格，notification_update 和最小 Task Context 已有标准库自动化测试，执行记录见第 8–10 节。Intent、RAG、完整 Multi-turn 指标仍未评测；前文样例不自动等于已通过测试。

## 1. 评测组织

四层：Intent/Routing、RAG、Multi-turn、Business Workflow。Evaluation 是三个核心能力的验收体系，不扩展成独立平台。

数据分开发集与独立保留集；阈值、Prompt 和规则在开发集调整，保留集不用于调参。记录模型、Prompt、代码、知识文档、索引/切分版本和采样参数。对模型非确定性记录重复运行策略、波动与失败样本，不只挑成功输出。

首版分离离线确定性检查和真实模型在线评测；在线调用、测试执行与费用须经用户授权。阈值及数值验收目标待基线完成后另行确定，当前不编造准确率或性能数据。租户隔离、未确认不写、取消不写等逻辑不变量必须满足，不能以平均准确率掩盖违例。

## 2. 样本结构

公共字段：case_id、category、description、trusted_identity_fixture、initial_business_state、initial_task_context、turns、expected_intents、expected_slots、expected_route/status、expected_calls、expected_final_state、expected_receipt、forbidden_effects。

RAG 追加：original_question、history、knowledge_scope/version、answerable、gold_evidence_units、required_claims、allowed_alternative_evidence。证据单元标到稳定文档/章节/页码或文本范围，切分变化后重新映射，不直接比较失效 chunk ID。

事实标签区分用户明确、用户确认、工具观测与模型推断。标注歧义允许多个可接受澄清表达，但对业务副作用要求确定。

## 3. Intent / Routing

| 指标 | 定义与判定 |
| --- | --- |
| Intent Accuracy | 单意图样本正确标签数 / 单意图样本数；按类别报告并提供混淆记录 |
| Slot Extraction Accuracy | 归一化后 gold 槽位值精确匹配数 / gold 槽位数；同时报告额外槽位/编造槽位与整组 exact match，缺失不算正确 |
| Routing Accuracy | 实际分支与预期分支/澄清出口一致的样本比例；分别记录识别正确但路由错误的情况 |
| low-confidence handling | 模糊请求的澄清覆盖、清晰请求的误拦截，以及未澄清导致的错误路由；标明分母 |
| unsupported handling | 超范围请求正确拒绝/解释比例，及支持范围内被误判超范围的比例 |

多意图单列小型场景集：检查意图集合、执行顺序、是否遗漏和是否违规产生多个 pending write，不混入单标签指标。

confidence 是模型自报分数，confidence=0.9 不代表 90% 正确率。未来在开发集比较不同阈值的 wrong routing 与 false blocking，再固定策略评估保留集；类型校验、授权与确认不因分数高而跳过。

## 4. RAG

| 指标 | 定义与首版实现方式 |
| --- | --- |
| Recall@K | 对 answerable 且有 gold 证据的问题，Top-K 覆盖的必要证据单元 / gold 必要证据单元，逐题后宏平均；等价证据组命中任一成员视为命中该组 |
| Evidence Hit Rate | 最终生成上下文含至少一个有效 gold 支持证据的问题数 / 有答案问题数；不等同多证据完整覆盖 |
| No-evidence behavior | 无答案集正确不作无依据回答的比例；另报有答案集误拒答比例；空召回和有召回但不相关都覆盖 |
| Query Rewrite A/B | 同模型、索引、K、重排设置下比较原问题检索与改写检索；记录召回、实体保留、用户约束丢失和短指代；最终生成始终保留 original_question |
| Citation existence | 回答中的引用标识是否存在于当前返回 evidence；自动校验，不能引用其他请求的证据 |
| Evidence support correctness | 人工逐项检查可验证结论是否获引用证据支持；记录 unsupported/contradicted/missing citation；引用存在不证明结论正确 |

无答案包含：文档没有相关信息、版本/适用范围不符、知识不足以解释实时状态。数据库实时状态问题应进入 Query，不能让知识库填空。重排失败时检查显式降级和有效召回保留；召回本身失败不能伪装普通 no-evidence，需保留技术错误原因。

建议按访问规则、通知说明、套餐说明、故障排查分层收集样本，不因常见类样本多就掩盖少数类失败。首版不添加 BM25/MMR/Hybrid/RRF；只有基线暴露具体召回问题后才比较候选方案。

## 5. Multi-turn 场景目录

| case_id | 对话与条件 | 必须观察到的状态/行为 |
| --- | --- | --- |
| MT-01 | “刚才那个项目”且唯一候选 | 同 workspace 解析到正确项目；有多个候选则澄清 |
| MT-02 | 将 comment_mentioned 修正为 task_assigned | 只改变当前任务类型；旧确认作废 |
| MT-03 | workspace A 切到 B | A 的项目、查询结论和待确认对象不能用于 B；重新验证成员关系 |
| MT-04 | 项目甲切到项目乙 | 旧访问结论失效，不继承甲的权限结论 |
| MT-05 | 展示通知变更后修改 enabled | 新 revision/operation identity；旧确认不可执行 |
| MT-06 | 待确认设置时插入工单查询后返回 | 工单 Query 不覆盖设置对象，恢复前重验 pending；不自动提交 |
| MT-07 | 同一次操作连续确认 | 同 operation_id 返回同回执，只有一次副作用 |
| MT-08 | 已有 pending write 又要求创建工单 | 提示继续原操作或取消后切换；选择前不覆盖也不建单 |
| MT-09 | 修改工单摘要后确认 | 实际保存内容等于最后展示确认内容；不能确认后重新生成 |
| MT-10 | Query 插入期间偏好版本/成员状态变化 | 恢复时重新确认或拒绝，不使用旧确认执行 |

采用场景通过率和失败类型统计，同时断言中间 TaskContext、PendingAction 与 Confirmation。检查结果不能只依赖最终自然语言回答。

## 6. Business Workflow 验收

| case_id / 情况 | 数据库、回执、调用与返回结构的断言 |
| --- | --- |
| BW-01 success | 通知设置更新可独立读回；回执记录实际 old/new、changed、对象；工单和回执对应，返回 waiting_for_agent |
| BW-02 missing slot | 不调用写服务、不改变业务数据；needs_clarification 指出必要缺槽 |
| BW-03 unauthorized | 不产生副作用；denied，且不泄露他人对象是否存在 |
| BW-04 cancel | execute 前取消无业务写入；清除有效确认；已开始执行时不得虚报已撤销 |
| BW-05 duplicate submit | 同 ID 并发/重复调用只提交一次；同 ID 不同 payload 拒绝 |
| BW-06 stale confirmation | 参数、对象、摘要、期限或目标版本变化时旧确认失效；不写入 |
| BW-07 execution error | 已知回滚无部分写入；提交结果不明返回 pending_resolution 并按原 operation_id 核对；不得换 ID 盲目重试 |
| BW-08 tenant isolation | 同用户不同 workspace、不同用户同 session_id、伪造项目/工单引用均受边界检查 |
| BW-09 no-op | 状态未变化且已为目标值：changed=false，回执真实，未重复修改 |
| BW-10 atomic receipt | 在写业务数据/写回执/提交边界注入故障，成功数据与成功回执不能分离提交 |

办理成功率分母仅包含参数完整、授权有效、已确认且预期可执行的请求；取消、拒绝、澄清单独评价是否正确处理，不计作办理失败。异常注入案例单独报告，不与正常成功率混淆。

四种证据必须联合检查：database state、operation receipt、tool/service call result、returned structured result。成功话术不能替代数据库与调用断言。

## 7. 后续落地顺序

先建立可读样本与离线业务/路由/Memory 断言，再授权少量在线识别和 RAG 评测；最后执行包含咨询、查询、确认、写入和支持受理的端到端场景。Phase 1 仅交付本规格；notification_update 的业务测试现已落地，其余评测执行器和样本由后续阶段新增。

## 8. notification_update 执行记录（2026-10-13）

环境：本地 Python 3.12.8；文件型 SQLite，测试在临时目录建立合成身份/业务数据，完成后清理。无 LLM、联网或第三方测试依赖。

命令：`PYTHONPATH=src python3 -B -m unittest discover -s tests -v`

结果：42 项测试全部通过。其中 `test_notification_workflow.py` 28 项，`test_sqlite_transactions.py` 14 项。测试通过数不等于模型效果指标或生产办理成功率。

| 验证组 | 已断言的事实 |
| --- | --- |
| 正常路径与持久化 | 确认前无写入，确认后新连接读到目标值和版本，回执编号与结果对应；重建 Workflow 后回执仍可查 |
| 授权与隔离 | 无成员、停用用户/空间、执行前撤销权限拒绝；同用户跨空间、同会话编号跨用户不串数据；不存在/不可见回执同类错误 |
| 参数与确认 | 缺参数、非法类型不执行；取消、修正、过期使旧确认不可执行；第二个写请求不覆盖当前草稿 |
| 业务重新校验 | 相关版本变化重新确认；其他通知单元变化不误阻塞；已有偏好更新为新值且版本递增 |
| 幂等与竞争 | 重复确认同回执、同偏好只改一次；两个连接同操作只提交一次；不同操作同旧版本不能覆盖；同 ID 不同内容拒绝 |
| 原子性与异常 | 新增或更新偏好后回执写入失败全部回滚；COMMIT 前失败不留成功记录；提交后回复丢失可读回原结果；核对受阻保持未知，不盲重试 |
| 无变化与历史结果 | 默认 false 再设 false 只写回执；相同目标新操作新回执、不增版本；历史回执不会被后续设置变化改写 |

实现首轮曾发现并修复两个状态问题：已知失败无回执被误转为结果未知；未执行草稿调用 resolve 被误转为结果未知。已加入对应回归用例。另对提交后丢失回复且权限被撤销的组合场景检查：不能把查询拒绝当作执行失败。

边界：这是单机 SQLite 自动化验证，不是生产认证、真实邮件投递、操作系统断电/进程强杀验证、性能压测或分布式保证。模拟的响应丢失通过提交后注入异常实现，不声称经过真实 HTTP 网络。完整 TaskContext、自然语言路由、RAG 和工单评测尚未运行。


## 9. uv 环境验证（2026-10-17）

- 目的：沿用已确认方案，将运行方式统一为 uv + Python 3.14，不修改通知办理流程。
- 环境：uv 0.12.17，uv 管理的 CPython 3.14.7，项目独立 `.venv`；无第三方依赖。
- 执行者：Codex。用户确认版本和环境方案；本轮用户对新环境的独立验证尚待确认。此前用户已提供 Homebrew Python 下 42 项测试通过的终端输出，两次执行不混为同一次。
- 实际命令：`PYTHONPATH=src uv --no-cache --offline run --locked python -B -m unittest discover -s tests -v`。`--no-cache --offline` 用于本次隔离验证，日常可使用 README 的命令。
- 结果：42 项通过（28 项 Workflow、14 项 SQLite 事务），测试报告耗时 0.332 秒；该数字仅为本次测试耗时，不是业务性能指标。证据等级 Level 1。
- 验证内容：沿用第 8 节断言，覆盖独立连接读回设置和回执、授权隔离、参数与确认失效、重复及竞争操作、写入回滚和结果不明核对。数据为临时文件型 SQLite 中的合成数据。
- 真实环境问题：首次复用 Homebrew Python 3.14.6 时，uv 的解释器探测失败。进一步检查发现 `pyexpat` 加载系统 `libexpat` 时缺少 `_XML_SetAllocTrackerActivationThreshold` 符号，导致 `plistlib` 无法导入、`platform.mac_ver()` 返回空。没有修改全局 Homebrew 或业务代码；改用 uv 管理的独立解释器后完成建环境和测试。未修复全局解释器，也未为外部解释器故障添加项目回归测试。
- 取舍：保留源目录运行方式和 `PYTHONPATH=src`，不新增构建后端、不安装项目包。`.python-version` 固定 3.14 系列，不固定补丁版；此处记录实际验证的补丁版本。
- 边界：仅验证当前业务代码；LLM/RAG/Milvus 依赖、编辑器实际运行配置和生产部署未验证。未提交 Git。


## 10. Task Context 执行记录（2026-10-17）

- 执行者：Codex；uv 0.12.17 / CPython 3.14.7 / 项目独立环境。基于 Git HEAD `7722ffa` 的未提交工作区增量，不将 HEAD 描述为本次完整实现版本。
- 实际命令：`PYTHONPATH=src uv --no-cache --offline run --locked python -B -m unittest discover -s tests -v`。
- 结果：71 项全部通过，原 Workflow 28 项、原 SQLite 事务 14 项、本轮 Task Context 29 项。原有两个测试文件及原执行/确认实现未修改。Level 1：本地测试运行，合成身份和业务数据、临时文件型 SQLite。
- Level 3：下表是测试支持的有限能力结论，不是生产成功率或自然语言 Memory 指标。Level 2 模型 Evaluation 本轮无；语言解析/指代消解仍是 Level 4 目标。

| 场景 | 实际检查 |
| --- | --- |
| 补参及修改 | 同任务补齐参数，不提前写数据库；prepare 后 Store 不保存参数副本；revise 更新版本，旧确认不写入 |
| 生命周期 | 取消/成功/已知失败后不继承旧 Slots；旧 task_id 事件不能改变新任务；非法修改整体拒绝 |
| Scope | user/workspace/session 隔离；无权限缺参任务不建立；同用户会话跨空间不能覆盖已有写任务 |
| Query/Resume | 查询成功/失败不覆盖草稿；显式恢复不执行；期限、成员版本、目标偏好变化重新展示，撤权拒绝 |
| 状态保留 | 临时数据库读失败保留草稿；结果未知时禁止 cleanup/cancel/revise/新写，恢复权限后核对原回执 |
| 真实数据库与回执 | 成功路径有对应偏好和成功回执，重复确认仍为 1 条偏好/1 条回执；回执写失败全部回滚 |
| 重启与并发 | 重建 Manager/Workflow 丢失未确认任务且不执行；已成功回执仍可查询；两个线程 start 仅一个建立任务 |

真实发现并修复：首轮 69 项有 1 项失败，`test_lost_workflow_invalidates_task_without_reconstructing_action` 中，失效任务 cleanup 错误返回 needs_clarification。根因是清理复用了拒绝失效任务的执行前置检查。修复为仅 cleanup 允许访问同 scope、同 task_id 的失效终态，活跃任务检查保持不变。保留原失败测试，并增加撤权后失效清理（含跨 scope/错误 ID 拒绝）与未确认任务重启测试，最终 71 项通过。

提交后响应丢失/撤权、数据库不可用及回执写入失败为主动故障注入，不是生产事故。重启通过重建实例模拟，并非进程强杀或断电实验。两个线程 start 仅证明本实例锁内守卫，不证明吞吐量、多 worker 或分布式一致性。
