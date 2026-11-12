# Agent Space 门户与 connector

独立的团队 Agent 管理门户，使用 Python 标准库、SQLite 和无构建静态前端。完整架构见 [`docs/architecture.md`](../docs/architecture.md)，部署见 [`docs/self-hosting.md`](../docs/self-hosting.md)。

```sh
python3 server.py
# 监听 http://127.0.0.1:8890
python3 -m unittest discover -s tests
node --check static/app.js
node tests/test_execution_ui.js
```

首次管理员入口保存在 `.data/admin-setup-link.txt`。运行数据和凭证保留在 `.data/` 或部署者设置的 `PORTAL_DATA`，不进入源码仓库。

## 模块

| 文件 | 作用 |
| --- | --- |
| `server.py` | 账号、角色、节点归属、可见范围、准入、能力与状态 API |
| `connector.py` | 本机接入、权限接口、心跳、诊断、执行事实白名单与签名更新 |
| `assistant.py` | 可选门户助手；派工申请与用户确认分开 |
| `pi_bridge.py`、`harness_bridge.py` | Pi、DeepSeek、WorkBuddy 与自定义命令桥接 |
| `workbuddy_auth.py` | 可选的官方 OAuth 授权代理，密钥由部署者在仓库外提供 |
| `hub-helper.py` | 本机 Hub 准入 helper |
| `backup.py` | SQLite 一致性备份 |
| `native-launcher/` | Windows 原生进程入口的 Go 源码 |
| `tools/publish_update.py` | 在部署者电脑上生成与签名更新通道 |

支持接入某种软件，不代表已经验证该软件的长任务或原生 Goals 能力。connector 心跳只报告实际观察到的能力；调用端、执行端、来源会话回传分别核对。

Apache-2.0，见仓库根目录 LICENSE。ANet 外部依赖与补丁使用单独许可证。
