ANet Config — macOS / Linux

首次安装：
  sh install.sh
  ~/.local/bin/anet-config setup

原生发行包包含独立 Python 环境，不需要自行安装 Python。
通用 Python 发行包需要 Python 3.11 或更新版本，安装程序会检测系统和 Homebrew 中的 Python。
首次安装节点使用 OpenSSH ssh-keygen
验证 ANet 官方发布签名，再校验压缩包和程序的 SHA-256。
已有节点会复用其程序和身份，不会降级或重新创建身份。
有原生维护更新的节点会优先使用当前版本，并检查路径、文件哈希和程序版本。
检查失败时会停止并显示原因；不会悄悄改回旧程序。
确需指定程序时，可用全局参数 --anet-binary 指定自己已确认的路径。

setup 会提示输入门户地址、账户、密码、工作目录和要接入的软件。
默认门户为 https://124.222.173.36:8888。
密码仅用于这次登录，不写入配置文件或命令参数。
ANet 登录与模型软件登录相互独立；按诊断结果完成所选软件的登录。

常用命令：
  anet-config status
  anet-config login --username 用户名
  anet-config agents connect codex
  anet-config agents connect claude_code
  anet-config agents connect workbuddy
  anet-config agents connect deepseek_harness
  anet-config workspace ~/ANetWork
  anet-config doctor
  anet-config service start

让本机接收协作（可选）：
  anet-config receiver prepare --harness codex
  anet-config receiver enable --harness codex --username 用户名
  anet-config receiver status

也可选择 claude_code 或 deepseek_harness。开启时输入门户密码核验账户归属，
并运行一次简短模型自检；status 显示实际执行服务是否就绪。
每个节点选择一个接收执行器。已存在的执行器和访问规则不会被静默替换。
WorkBuddy 的 MCP 工具连接已经支持；作为接收执行器还需要独立授权和可靠完成回执，当前不能开启。
通用 Python 包的接收功能还需要所选软件的 SDK；原生包包含 Claude 和 DeepSeek 的执行组件。

全局参数放在子命令前，例如：
  anet-config --data-dir ~/.anet --json status

配置完成后，直接在 Codex、Claude Code、WorkBuddy 或 DeepSeek Harness
中使用 ANet。本软件管理连接、配置和服务；任务可从智能体软件或门户对话发起。

升级：下载新版本并再次运行 sh install.sh；节点数据不在软件目录。
卸载：删除 ~/.local/bin/anet-config 和 ~/.local/lib/anet-config；
这会保留 ~/.anet 中的身份、连接和记录，也保留智能体软件中的 MCP 配置。
