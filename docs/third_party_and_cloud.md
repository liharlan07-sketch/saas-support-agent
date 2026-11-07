# 第三方来源与可选云端部署核对

2026-11-07：审查 uv.lock 的 52 项第三方包。50 项依据本机已安装元数据/许可证，另 2 项为未安装的 Windows 条件依赖。完整清单见 [依赖许可证记录](dependency_licenses_2026-11-07.json)。此核对不等于法律意见、漏洞扫描或重分发授权证明。

| 直接依赖 | 版本 | 许可证 |
|---|---|---|
| FastAPI | 0.144.0 | MIT |
| Uvicorn | 0.54.0 | BSD-3-Clause |
| HTTPX | 0.28.1 | BSD-3-Clause |
| PyMilvus | 3.0.2 | Apache-2.0 |
| Milvus Lite | 3.2.2 | Apache-2.0 |
| Jieba | 0.42.1 | MIT |
| FastEmbed | 0.9.1 | Apache-2.0 |
| ONNX Runtime | 1.26.0 | MIT |
| pypdf | 6.9.0 | BSD-3-Clause |

间接依赖包括 MPL-2.0 等许可，不能统称 MIT。py-rust-stemmers 的元数据为空，但安装包内 LICENSE 已核实为 MIT。Windows 条件依赖 colorama 0.4.6 的 [官方元数据](https://pypi.org/project/colorama/0.4.6/) 为 BSD-3-Clause；win32-setctime 项目标为 MIT，但此机未逐文件核对锁定的 1.2.1 构件，也未做 Windows 验收。

## 模型

- Embedding：[Qdrant/bge-small-zh-v1.5](https://huggingface.co/Qdrant/bge-small-zh-v1.5) 是 BAAI 模型的 ONNX 转换，模型卡 MIT；固定快照 `46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59`。
- Rerank：[BAAI/bge-reranker-v2-m3](https://huggingface.co/BAAI/bge-reranker-v2-m3) 模型卡 Apache-2.0。实际使用 [onnx-community 转换](https://huggingface.co/onnx-community/bge-reranker-v2-m3-ONNX) 快照 `90213ffc6a8e6f051a6331269a0f5526cdd896f6` 的 INT8 文件。
- 转换页面说明上游来源，但未显示独立许可证标记；固定快照在线文件页本轮无法读取。不能声称已审完转换产物的独立许可。仓库不提交权重，未来若打包分发权重，应先补齐许可核对。
- 权重 SHA-256 在 `knowledge/models.py` 固定。生成式模型厂商和模型尚未选定。

## 自有源码及发布边界

所有者已明确确认拟发布自有代码的授权权利，并采用根目录 [MIT LICENSE](../LICENSE)，版权署名为 2026 liharlan07-sketch。该许可不替代第三方包、模型、文件各自的许可证；下列来源和重分发边界继续保留。

能力来源参考 Lingyu / SmartReading（migration source），不附带原项目源码或权重，不宣称第三方算法与组件为原创。仓库仅分发源码和合成文档；安装包、容器或权重若未来另行分发，需要保留实际包含的 LICENSE/NOTICE，不能以此清单替代。

## 云端只读核对

用户使用云服务器自行部署的 Milvus。Multi-Agent 项目历史真实 SDK 验收记录的服务端版本为 **2.6.17**，状态 passed；这是旧报告，本轮没有连接云端，因此不能确认当前实例仍是该版本。

[官方 PyMilvus 兼容说明](https://milvus.io/api-reference/pymilvus/v3.0.x/About.md#compatibility) 指出协议向后兼容，较新 SDK 可连接较旧服务端；推荐验证组合为 2.6.x / PyMilvus 2.6.17，以及 3.0.x / PyMilvus 3.0.2。客服当前 SDK 3.0.2 对旧记录中的 2.6.17 有兼容基础，但中文分词、BM25、过滤、索引及一致性行为尚未在这个组合上验收。

最小可选方案（未实施）：保留 Lite 默认；在连接工厂注入远端客户端；复用人才项目的连接、集合结构检查和双路统一过滤模式；客服使用独立 collection、现有 512 维 Embedding、Workspace/店铺权限和政策有效期。不得共用人才集合或复制其业务实体。未来先另行授权只读能力核对，再授权独立测试集合的写入验证；不自动升级云实例或降级 SDK。

本轮没有读取真实凭证、创建云集合、上传数据或修改本地索引。
