# 构建与验证

## 客户端

在 `client/cli` 下运行 `python build_release.py`，生成可解压后运行 `sh install.sh` 的兼容包。构建把各 Python 模块及其 SHA-256 写入包内清单，同目录的 `bootstrap/` 保存线上 CLI 安装脚本和发布目录数据。仓库构建出的包保留源码内容并包含许可证，归档字节和门户已有发布的 SHA-256 会因重新打包而不同。

Windows 桌面配置界面使用 .NET Framework WPF。在 Windows 上从仓库根目录运行以下命令，生成 `client/windows/desktop/out/ANetDesktop.exe`。

```powershell
powershell -NoProfile -File client/windows/desktop/build.ps1
```

安装器入口为 `client/windows/installer/build.py`。使用 `--help` 查看参数。构建需要 Windows x64、Python 3.11+、上一步的桌面程序、经签名核验的 Codex 运行组件、固定版本的 CPython 3.14.10 嵌入包、离线 wheel（或已验证的 `--runtime-template`）、以及 `--node-binary` 和对应的 `--node-release`。离线文件及预编译程序由发布方单独准备。后端清单固定了 `client/windows/installer/backend` 中的源码字节。Windows 安装器的本地回归从该目录运行 `python -m unittest discover -s tests`。

## 门户

本地预览使用独立 SQLite 数据目录。默认目录为 `agent-portal/.data`，首次管理员入口在该目录里的 `admin-setup-link.txt`。自托管时设置 `PORTAL_DATA`、`PORTAL_URL`、`ANET_HUB_URL`、`ANET_HUB_LOCAL` 和 `PORTAL_HUB_HELPER`。Hub 使用按上游许可部署的自有实例，或连接已授权的实例。

账户接入与对话服务在同一主机上运行，分别用 `agent-portal/account/account_sidecar.py` 和 `agent-portal/chat/chat_server.py` 启动。两者以 `--portal-code` 指向门户目录，共用上述门户环境变量和数据目录。部署使用 HTTPS 反向代理，把 `/api/client/` 转发到回环端口 8891，把 `/api/chat/` 和 `/api/v1/` 转发到回环端口 8892。完整对话还需要部署者配置有权限的 ANet 节点。

下载页读取 `/api/desktop-release` 和 `/downloads/anet-config/0.3.0-preview.2/release.json`。自托管下载时，应将 Windows 安装包放入 `PORTAL_DESKTOP_DOWNLOADS` 指向的目录，并提供与实际文件匹配的 `desktop-release.json`。CLI 发布目录由反向代理提供文件；自托管 CLI 的引导脚本和发布校验值应指向该部署的 HTTPS 下载地址。直接安装当前公版时，从[线上下载页](https://124.222.173.36:8888/download)获取。

在 `agent-portal` 下运行 `python -m unittest discover -s tests`。门户运行及文件权限回归以 POSIX 环境为主，Windows 专有的桌面及安装器在 Windows 验证。可用 `node --check static/app.js` 和 `node --check static/download.js` 检查前端脚本。

## ANet Go 节点

[ANet 补丁说明](../patches/anet/README.md)给出上游 commit、当前开发 commit 和应用方法。用该上游版本指定的 Go 工具链在独立仓库应用补丁。
