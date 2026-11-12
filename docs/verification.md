# 验证范围

公开版本的检查只使用临时目录与测试身份，不访问生产 Hub、模型账户、客户 EPS 工程或既有会话。

## 可以复现的检查

- taskd 完整 unittest：协议、计划与原生生命周期、预算、恢复、资源、程序验收、签名更新、附件完整性和显式会话绑定。
- 门户完整 unittest：账号权限、接入、迁移、connector、harness 协议模拟、诊断与内容过滤。
- JavaScript 语法和执行状态渲染断言。
- 本地往返：两个实际 taskd 守护进程，模拟网络、执行器和会话服务；真实写文件并检查哈希和回传记录。
- 发布文件和 Git 提交的密钥扫描；不发布扫描命中内容。

运行命令见根目录 README 和 `ci/github-actions.yml`。初次发布的本地结果见下表；当前 GitHub CLI 凭证没有 workflow scope，未启用 Actions。配置模板已附，具有相应权限的维护者可复制到 `.github/workflows/`。任何跳过项都会出现在测试输出中。

## 跳过与适用环境

macOS seatbelt 集成测试先尝试启动一个无副作用的 `sandbox-exec` 探针。只有主机明确拒绝该原语时跳过；原来的网络隔离和工作区绑定断言保留。其他操作系统跳过该 macOS 专属测试。模拟网络使用 POSIX 文件锁，Windows 跳过相应往返测试。

可选 Windows launcher 二进制不在源码仓库中，没有自行构建的文件时跳过该二进制集成检查。跨编译和本地参数检查不证明 Windows 实机行为。

## 需要另行验收

模拟结果不覆盖真实 ANet 网络部署、模型 API 可用性、所有 Codex 版本的 Goal API、Windows 交互桌面、各 harness 实机执行、生产自动更新以及真实原会话唤醒。EPS 检查器只是适配代码，不附带 EPS 软件、客户工程或业务验收结论。

ANet 补丁的基准可用 `git apply --check` 验证；本仓库的 Python 测试不等于 ANet Go 全量测试。

## 2026-11-12 初次发布检查

| 检查 | 本地结果 |
| --- | --- |
| taskd 完整回归（Python 3.14.6 / macOS） | 365 tests，OK |
| 门户完整回归 | 110 tests，OK，跳过 1 项可选 Windows 二进制检查 |
| 法律说明随更新包保留后的更新回归 | 41 tests，OK |
| 更新架构页后的门户回归 | 13 tests，OK |
| 脱敏测试字符串标注后的同步回归 | 5 tests，OK |
| JavaScript | 语法通过，12 项执行状态 UI 断言通过 |
| 安装与打包 | LICENSE / NOTICE 在安装目录和更新 zip 中保留 |
| ANet 补丁 | 对注明的基准执行 git apply --check 通过；未在本次重跑 Go 测试 |
| Gitleaks 8.30.1 | 对最终发布文件及 Git 历史扫描；虚构脱敏测试行有局部 allow 标记，不排除整个测试目录 |
| 浏览器 | 本地架构页桌面 1280px、手机 390px 无水平溢出；执行路径切换正确 |

Python 3.10 与 Linux 的结果尚未在本次验证；模板中的版本矩阵是可执行配置，不是已通过的结果。历史私有现场验收记录未随源码发布，不能用它们声称当前公开版本已完成所有实机测试。

初次发布重复运行整个五场景演示时，`test_blocked_task_delivers_partial_artifacts_and_failure_details` 曾发生一次等待超时；此前完整 365 项回归中的相同测试通过。本地端到端测试存在时序不稳定性，保留此边界，不声称每次重复运行都稳定通过。演示入口只选择完整成功交付场景；完整回归继续包含阻塞场景。
