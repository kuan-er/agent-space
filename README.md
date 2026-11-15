# Agent Space · ANet

通过 ANet 连接不同设备上的智能体。Agent Space 管理账户、节点与访问权限，taskd 持久化任务、组织验收，并在显式绑定后把结果送回发起会话。

## 安装与连接

从[门户下载页](https://124.222.173.36:8888/download)获取当前安装包、系统要求和 SHA-256。

- **Windows 10 / 11 x64** 使用 `0.3.0-preview.3`。下载并打开安装包，按向导安装，启动 ANet 后登录门户账户并选择智能体。此预览安装包尚无发布者签名。
- **macOS / Linux** 使用 CLI `0.3.0-preview.2`。安装脚本自动选择 Apple Silicon 或 Linux x86_64 原生包，其他受支持环境可使用需要 Python 3.11+ 与 OpenSSH 的兼容包。

```sh
curl --fail --proto '=https' --tlsv1.2 -o install-anet.sh https://124.222.173.36:8888/downloads/anet-config/0.3.0-preview.2/install-anet.sh &&
sh install-anet.sh &&
~/.local/bin/anet-config setup
```

`setup` 会提示填写门户账户、工作目录和要接入的智能体。安装后可运行 `~/.local/bin/anet-config status` 检查状态。要接收任务，在客户端选择执行器和工作目录并开启接收。ANet 账户连接与各智能体的模型登录分别配置。CLI 的完整命令见[客户端说明](client/cli/README.txt)。

## 源码目录

| 路径 | 内容 |
| --- | --- |
| [client/cli](client/cli) | 配置 CLI、账户连接、智能体适配器，以及 taskd 1.9.4 |
| [client/windows](client/windows) | Windows 桌面配置界面、安装器、更新器及安装包内的后端 |
| [agent-portal](agent-portal) | Python / SQLite 门户、账户接入、对话服务、API 与下载页 |
| [patches/anet](patches/anet) | 对应 ANet `0.3.10-operator.2` 的 Go 源码变更、上游基线与许可证 |

## 本地开发

CLI 和门户使用 Python 3.11+。可直接编译 Windows 桌面配置界面。完整 Windows 安装包需要独立准备的运行时与已核验的 ANet 程序，详见[构建和验证说明](docs/development.md)。

```sh
cd client/cli
python -m unittest discover -s tests
python build_release.py
```

从仓库根目录启动本地门户预览。

```sh
python agent-portal/server.py --host 127.0.0.1 --port 8890
```

浏览 [本地门户](http://127.0.0.1:8890/) 或 [架构页](http://127.0.0.1:8890/architecture.html)。公开实例的认证接口见[门户 API 文档](https://124.222.173.36:8888/docs/anet-api-v1.txt)。

## 许可证

Agent Space 和 taskd 使用 [Apache-2.0](LICENSE)。ANet 节点上游源码、补丁及对应派生作品适用 [ANet 许可证](patches/anet/LICENSE)。PyYAML 源码适用其 [MIT 许可证](licenses/PyYAML.txt)。归属见 [NOTICE](NOTICE)。
