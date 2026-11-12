# 本地运行与自托管

## 本地预览

Python 3.10+，Node.js 用于 UI 测试。无需 pip 包。

```sh
python3 agent-portal/server.py --host 127.0.0.1 --port 8890
```

打开 `http://127.0.0.1:8890/architecture.html` 阅读架构。首次管理员链接在 `agent-portal/.data/admin-setup-link.txt`，只供部署者本人使用，不要提交进 Git。

## 真实部署配置

| 环境变量 | 用途 |
| --- | --- |
| `PORTAL_DATA` | 仓库外的私有数据目录，包含数据库与运行凭证 |
| `PORTAL_URL` | 用户实际访问的 HTTPS 门户地址 |
| `ANET_HUB_URL` | 自己获准使用的 Hub 地址，公开代码默认回环地址 |
| `ANET_HUB_LOCAL` | 门户所在主机可访问的 Hub 管理地址 |
| `PORTAL_HUB_HELPER` | 本机准入 helper 路径，见 `agent-portal/hub-helper.py` |
| `ANET_BIN` | 本机 ANet 可执行文件 |

Hub 的安装和许可遵循 ANet 上游文档。门户账号、网页可见性和节点准入是不同机制；只有门户启动并不等于节点可以收发任务。真实部署使用 HTTPS 反向代理，保持后台监听回环地址，并按部署需要配置服务账户、数据目录和备份。

connector 的接入凭证通过隐藏输入读取，模型登录在各执行设备本机完成。`setup` 复用现有接入检查点；不要为修复连接问题重新生成身份。执行授权在节点所有者的交互终端完成。

```sh
python3 agent-portal/connector.py setup --help
python3 taskd/run_taskd.py install --help
python3 taskd/run_taskd.py guide
```

taskd 的安装、能力查询、提交与控制命令见 [`taskd/README.md`](../taskd/README.md)。提交前核对目标能力；需要回到原会话时核对该任务的显式绑定。

## 更新信任与可选集成

源码目录中的 taskd 不执行自更新。安装版与 connector 的更新清单使用 Ed25519 签名，代码中保留的 `UPDATE_KEY` 是公开验证密钥，不是私钥。自托管独立更新通道时，使用 `agent-portal/tools/publish_update.py init` 在仓库外生成自己的签名密钥，将输出公钥同步配置到 connector 和 taskd，再构建和签名；不能使用本仓库公钥签发更新。

公开版本的 taskd 安装包包含 LICENSE 和 NOTICE。该包需要支持这两个法律说明文件的更新器；旧版安装如拒绝这类条目，应先由所有者通过本地安装升级，不向旧节点直接发布不兼容包。

Windows 原生 launcher 源码位于 `agent-portal/native-launcher/`。设置 `GO` 或使用 PATH 上的 Go 构建，按生成哈希更新 connector 中的固定 launcher 哈希，再提供下载。公开仓库不内含运营者预编译二进制。对应测试可通过 `AGENT_SPACE_NATIVE_TEST_BINARY` 指定自己构建的文件。

WorkBuddy OAuth 需要部署者自己的官方应用、HTTPS 回调、`WORKBUDDY_CLIENT_ID` 和仓库外权限为 0600 的 `WORKBUDDY_CLIENT_SECRET_FILE`。未配置时该功能明确不可用。DeepSeek、Pi 与 Codex 各自需要对应设备安装并登录；接入目录中的支持项不构成跨软件实机验收。
