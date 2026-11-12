# 许可证与来源边界

## 本仓库实现

`taskd/`、`agent-portal/`、`examples/` 和本项目文档使用 Apache License 2.0，见根目录 `LICENSE` 与 `NOTICE`。这是从作者本地工作区提取的独立项目，不是 a2aproject 的官方样例或官方产品。

本次提取仅选择程序、静态资源、测试和文档；不包含父工作区的其他项目、部署归档、运行状态、客户数据或 Git 历史。公开副本将生产地址替换为示例地址，使用通用 Go 工具链路径，并更新了架构文档与可移植测试入口。

ANet 通过进程和协议接口作为外部依赖使用，主程序仓库不内嵌其完整实现。外部 Codex、Pi、DeepSeek Harness 和 WorkBuddy 产品的使用与授权由各自条款约束，本仓库不再分发这些产品或模型凭证。

## ANet 补丁

`patches/anet/` 独立适用该目录内完整的 `LICENSE`。这是 ANet 上游许可证，包含 Apache-2.0 正文及附加条件；不能将它标为标准 Apache-2.0，也不能用根目录许可证覆盖。

补丁的基准版本与本地实现版本在该目录的 README 中注明。补丁本身明确列出相对基准新增和修改的文件；再分发派生作品时须保留适用的版权、归属、许可与修改说明。

其中对多租户托管 Hub 和特定用户界面标识有额外条件。公开源代码不自动授予运行面向不同个人或组织的托管 Hub 的权利；按完整条款确认具体部署是否需要上游书面授权。

来源：

- [ANet 上游 LICENSE](https://github.com/ANetResearch/ANet/blob/main/LICENSE)
- [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)

本仓库随附的是提取时的 ANet 许可文本；发布其他版本时应核对对应版本，而不是仅依据上游 main 的最新内容。
