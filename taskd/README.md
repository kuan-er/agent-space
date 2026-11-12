# taskd：多 Agent 长任务交付

taskd 是运行在 ANet 节点旁边的任务守护进程。执行方优先让支持原生 Goals 的执行器在同一会话中持续推进，taskd 负责资源调度、状态与控制、独立验收和文件交付；不支持原生 Goals 时保留计划执行方式。发起方将结果送回原会话。设计文档：[`docs/architecture.md`](../docs/architecture.md)。

- 版本 1.9.0，协议 `taskd/1`。Python 3.9 及以上，只用标准库。
- 核心与执行器、发起端、操作系统、业务领域、传输方式都无关；具体实现都是插件（`taskd/builtin.py` 是插件目录）。
- 第一版插件：执行器 Codex、自定义命令；发起端 Codex；通用检查器 `files`、`value`、`dir_unchanged`、`llm_review`；Windows 检查器 `uia_property`、`uia_values`、`window_screenshot`；Windows 桌面探针；ANet 传输；macOS / Linux / Windows 登录自启。

## 工作方式

```
发起方会话 ──ANet──▶ 执行方 taskd ──▶ 原生 Goal / 轻量执行 / 显式计划
     ▲                    │  资源 · 状态与控制 · 预算 · 独立验收
     └── 发起方 taskd ◀────┘  进展用 working 状态，结果用 completed（带 report.json）
```

- **不经过 auto-reply。** 执行方 taskd 通过本节点的 `anet mcp` 读取发给本节点的任务，用 `reply_task` 回复：`working`（进展或状态）、`input-required`（提问、阻塞、计划确认）、`completed`（完成并签发回执）、`failed`、`rejected`、`canceled`。因此不受 auto-reply 的 180 秒超时和每个交互 30 条消息的限制。执行方节点必须关闭 auto-reply，否则两者会同时回答；`install` 会备份并关闭它，`uninstall` 恢复。
- **保活。** ANet 发起方 15 分钟收不到消息会判任务无响应。taskd 每 10 分钟发送带 `heartbeat=true` 的结构化 `working` 心跳，保留最近阶段、步骤摘要、产物和阻塞原因；发起端只更新时间，不覆盖有效进展。
- **完成由检查决定。** 原生 Goal 的完成声明仍须通过独立验收；计划模式使用 `REPORT_SCHEMA` 报告事实。taskd 自己计算文件哈希、运行检查器。无法写成程序的验收条件交给另开的只读审查会话，结论按“问题 + 证据内容”缓存，同一事实只判一次。没有审查模型时，这些项目在报告里列为“需发起方确认”；没有明确验收项的原生任务也会保留目标条件待审，不宣称全部业务验收通过。
- **会话。** 原生 Goal 始终使用同一持久化线程，运行中的 hint/answer 用 steer 送入当前轮次；独立验收失败的修复也回到该线程。计划模式同一阶段内续接，换阶段、重新规划时新开会话。默认安装为 Codex 配置独立的 `CODEX_HOME`，不混入机器主人自己的会话。
- **回到原会话。** 优先从来源会话的工作目录运行 `submit`，在发出消息前保存 `CODEX_THREAD_ID`、工作目录与提交意图；直接用 MCP 提交后必须显式 `track`。不再搜索聊天文本推断绑定。发起方通过正在服务原会话的 Codex app-server 读取实时忙闲状态，空闲才请求新一轮执行。持久化请求 ID、接收确认、执行轮次 ID 和最终回复；断线按结构化消息 ID 对账，不自动重发不确定请求。桌面通知、诊断输出和 CLI 退出码不能证明回传。
- **文件收到。** 默认下载到 `~/ANet/received/<节点标识>/<任务 ID>/`。同一附件清单使用稳定的 `receive-<清单摘要>/receipt.json`；每次复用前重新测量大小和 SHA-256，损坏缓存不会成为成功证明。内容保存在任务内 `objects/<SHA-256>`，收件目录用硬链接提供文件名（不支持时复制），相同内容不用反复导出。ANet 历史同名文件被编号时，使用 `pull` 的 CID/路径映射及本机哈希定位本条消息声明的版本；没有映射的旧传输仍严格按名称校验。全历史导出只放在本次临时目录，成功、失败都清除临时 payload，保留收据；不整理或删除旧版本产生的目录。确定性匹配失败不自动连试三次，相同清单的失败缓存 5 分钟后允许重新核对；网络异常仍可重试。失败显示“文件未收到”，完整性通过不等于业务正确。执行方随报告发送实际结果文件，文件名带路径摘要防止同名覆盖。
- **阶段与最终验收。** 计划用 `produces` / `requires` 描述阶段产物和依赖，`checks` 只放阶段检查，`final_checks` 标明最终验收责任。程序纠正把最终检查挂在早期阶段的计划，拒绝后向产物依赖并自动要求修订。最终检查只在各阶段结束后运行。阶段检查或有序里程碑通过会推进验收；有新证据支持的诊断发现经独立评审后也可重置停滞计数，但不代表验收通过。新文件和重复截图本身不能重置计数。
- **独立状态。** `show` 的 `requester_side.stages` 分开列出 `message`、`execution`、`acceptance`、`files`、`session`，分别保存时间与错误。`provider_passed` 明确是执行方自报；`receipt_verified` / `effect_status` 原样保留，缺失为 null。旧版文本匹配产生的交付标记保留为 `legacy_unverified`。
- **原会话服务要求。** Codex 0.167.0 的共享控制 socket 为 `$CODEX_HOME/app-server-control/app-server-control.sock`。也可配置 `requester.codex_endpoint` 为显式的本机 `ws://127.0.0.1:PORT`。Windows 等没有共享会话服务的环境标为 requester 未就绪，不回退到独立 `codex exec resume` 假装恢复了桌面会话。当前 API 没有原子 start-if-idle；若忙闲检查后与用户启动发生竞态，旧轮次确认不能判成功。

## 安装

在 ANet 节点所在的电脑上，以运行 ANet 的账户执行。`--data-dir` 默认是 `$ANET_DATA_DIR` 或 `~/.anet`。

### 发起方（例如 Mac）

```sh
python3 run_taskd.py install
```

只跟踪本机发出的任务，不接任务。安装后登录自启。

### 执行方（例如运行 EPS 的 Windows 虚拟机）

```powershell
python run_taskd.py install --accept-tasks --gui `
  --work-dir C:\Users\admin\EPS --read-path "C:\Program Files\<EPS 安装目录>"
python run_taskd.py login-executor     # 给执行器自己的 CODEX_HOME 登录一次 Codex
python run_taskd.py selftest
```

- `--gui`：任务要操作这个桌面上的图形程序。Codex 以完整权限运行（Codex 的 Windows 沙箱可能使用私有桌面），桌面阶段执行前检查是否处于已登录用户的交互桌面。同一桌面阶段含执行、截图与验收全程互斥，后台阶段可并行。后台默认不设并发上限，快速回复与长任务共用调度器；桌面容量固定为 1。可选用 `--concurrency N` 另设总执行上限。整台虚拟机应当作为隔离边界：不存放其他重要数据或凭证，定期快照，节点设为私有。
- `--read-path`：允许检查器读取的额外目录，例如用 `dir_unchanged` 确认安装目录没有被修改。
- 想沿用机器上已登录的 Codex，可以改用 `--share-codex-home`，代价是 taskd 的会话会出现在这个账户的 Codex 记录里。
- Windows 的登录启动项只运行一次，所以自启目标是 `supervise`，它在 `serve` 退出后重新拉起。taskd 也会守护 ANet 节点和 Agent Space connector。
- 无人值守需要 Windows 自动登录到这个账户，并关闭锁屏和休眠。

其他选项见 `python run_taskd.py install --help`（执行器为自定义命令时用 `--executor command --executor-command <程序>`）。

卸载：`python run_taskd.py uninstall`。会移除自启、停止进程、恢复 auto-reply 原设置，保留数据目录。

## 发起任务

### 按任务复杂度分流（1.9.0）

| 任务 | 入口 | 自动模型调用 |
|---|---|---|
| 能力、已有任务状态、最近事件 | `capabilities` / `status` / `logs` JSON 查询 | 0 |
| 已知的小部署、配置修改 | `taskd=run`，明确程序验收 | 执行 1 次，无规划、审查或重规划 |
| 已登记的成熟脚本 | `taskd=run` + `routine` | 成功 0 次；所有者启用异常修复后最多 1 次 |
| 有验收目标的持续工作 | `taskd=submit`，默认 `orchestration=auto` | 支持时使用原生 Goal；不增加 taskd 模型规划 |
| 需要显式阶段，或执行器没有原生适配 | `taskd=submit` + `orchestration=planned` | 原有规划、阶段、检查点与恢复 |

1.9.0 原生适配目前只有 Codex：启动时以不调用模型的 App Server 探针确认 Goal API。
自动分流失败会在 `routing_reason` 说明回退原因；执行已启动后出现断线或未知结果会阻塞对账，绝不换执行器重做。
强制要求原生执行可设 `orchestration=native`，能力不足则拒绝；发送前必须确认对端是 1.9.0 及以上且 `capabilities.native.available=true`，旧版本可能忽略新字段。

原生任务用同一个任务 ID 查询 `status` / `logs`，后者包含有界的命令、输出、Goal 和重试事件。
`pause` 暂停目标、中断轮次并清理托管后台终端；`resume` 续接原线程；`cancel` 确认停止后终结任务。
停止结果不确定时保留工作目录/桌面占用，重启服务后也不会丢失；再次取消只连接原线程完成清理，不启动新目标。
独立启动的守护服务仍需任务自身的停止检查。普通“继续”或修复指令可恢复阻塞任务，不要求阶段计划修订。

原生通道沿用所有者的模型、沙箱、执行器配置和登录环境，不自动提升权限。需要额外审批时暂停并说明原因。
默认使用私有 stdio App Server；任务状态和日志通过 taskd 查看。要在原生 Codex TUI 中接管，节点所有者可配置
`executor.native_endpoint` 为已有的回环 WebSocket App Server，并用 `executor.native_token_file` 指定本地令牌文件；
只有这个模式声明 `interactive_attach=true`。这些配置不接受远端请求覆盖。`executor.native_goals=false` 可关闭原生路由。

`max_steps` 在原生模式中统计观察到的执行轮次；`max_hours` 仍限制累计执行时间。
`step_timeout_min` 只用于旧的单次执行，不再截断原生 Goal 内部的容量重试等待；原生失败恢复和独立验收仍受总预算约束。
模型排队显示已观测的重试原因，不能据此承诺完成时间。验收轮次耗尽后等待用户指令，保留历史证据。

先查询对端 `capabilities.lightweight.action=run`。新动作 `run` 在旧节点上会被拒绝，
不会被忽略后悄悄走完整流程。普通文本和 `mode=quick` 仍是聊天快速回复，不代替带验收的部署。
`logs` 只读调用者自己的任务事件摘要，最多 50 条，不接受任意文件路径。

轻量提交示例（保存为 `run.json`，运行 `python3 run_taskd.py run --to <AID> --spec run.json`）：

```json
{
  "taskd": "run", "protocol": "taskd/1",
  "goal": "在任务目录内使用现有部署脚本安装并测试；保留配置备份与测试结果",
  "resource": "background",
  "acceptance": [
    {"id": "test", "type": "value", "file": "out/test-result.json", "path": "exit_code", "expect": 0},
    {"id": "backup", "type": "files", "pattern": "out/config-before.json"}
  ],
  "budget": {"max_steps": 2, "max_hours": 0.5, "step_timeout_min": 10}
}
```

轻量执行不把自然语言验收自动转换成模型审查，必须给出确定性检查（或使用登记脚本自带的检查）。
首次执行只发接收消息和最终短结果；长时间等待时仍发送资源等待说明和保活。
`report.json` 及实际产物由程序打包、计算哈希；不再要求模型额外整理多阶段报告。
验收不通过或执行中断会阻塞，不自动升级为长任务、重跑部署或反复修复报告格式。
原任务上的 hint/answer 可允许下一次有界模型执行，累计预算不重置。
`plan_approval=always` 仍在执行前显示程序生成的一次执行计划并等待确认。
GUI 工作显式使用 `resource=desktop`，继续持有共享桌面锁；后台不得操作 GUI。

节点所有者可在本机配置的 `routines` 登记脚本（重启服务后生效）：

```json
{
  "routines": {
    "deploy_app": {
      "argv": ["/absolute/path/python3", "/absolute/path/deploy_app.py"],
      "peers": ["<允许调用的 AID>"],
      "parameters": {"release": ["stable", "candidate"]},
      "resource": "background",
      "repair": false,
      "constraints": ["先备份配置，再验证包哈希，最后执行真实调用测试"],
      "acceptance": [
        {"id": "routine_test", "type": "value", "file": "out/test-result.json", "path": "exit_code", "expect": 0}
      ]
    }
  }
}
```

请求为 `{"taskd":"run","goal":"部署已登记程序","routine":"deploy_app","params":{"release":"stable"}}`。
脚本在任务工作目录执行，从 stdin 读取 `{"params":{...},"inputs_dir":...}`，退出码为 0 才进入验收。
所有参数都必须匹配登记的名称与有限取值，命令行固定；请求可追加程序验收，不能替换登记验收。
固定脚本无法理解新增的自然语言约束，因此只接受登记时已有的约束；新增限制需使用 direct 或另行登记脚本。
脚本必须自行落实备份、包校验、身份核对和实际调用测试；配置中的约束不会被转换成可执行代码。
只有所有者显式配置 `repair=true`，脚本失败或验收失败时才允许一次模型检查现场并修复。
中断或超时不自动重跑，也不自动调用模型；未知副作用必须先核对。任务接受后登记定义改变则拒绝继续。
能力查询只列出该对端可用的脚本名、参数、资源和修复开关，不暴露命令路径。
程序验收结果仍是执行节点的自报，不等于请求方已独立验收业务结果。

需要多步执行、检查点、失败恢复、验收检查或回传原会话时，使用 taskd JSON 提交；普通短问题默认走快速回复。
安装及守护进程启动时，会为已配置的 Codex requester 写入独立的 taskd 指导区块（保留原有说明并备份，位于 ANet 管理区块之外）。
`python3 run_taskd.py wire` 可以立即刷新；卸载只移除本节点的 taskd 区块。已开启的助手会话需要新会话才能自动加载新指导。

可以用新 ANet 任务查询 `{"taskd":"capabilities","protocol":"taskd/1"}`，消息放在 `send_message.text`。
1.1.1 起，查询不需要先创建工作任务，不调用模型、不下载附件，并遵守节点的对端准入限制。
旧版返回 `no_task` 时，使用门户能力声明或升级执行端；不要为查询能力创建占位工作任务。

把任务写成 JSON（`python run_taskd.py guide` 有完整说明），作为新 ANet 任务的正文发给对方 AID。在 Codex 会话里可以直接用 ANet MCP 的 `send_message`；也可以：

```sh
python3 run_taskd.py submit --to <AID> --spec task.json
```

```json
{
    "taskd": "submit",
    "protocol": "taskd/1",
    "goal": "在 EPS 中完成喷涂功能验证",
    "acceptance": [
        {
            "id": "gui_enable",
            "type": "uia_property",
            "window": "EPS*",
            "element": "paintSimulationItem",
            "property": "IsEnabled",
            "expect": true
        },
        {
            "id": "no_tamper",
            "type": "dir_unchanged",
            "path": "C:\\Program Files\\<EPS 安装目录>"
        },
        {
            "id": "gui_cases",
            "type": "files",
            "pattern": "results/case_*.csv",
            "count": 3,
            "newer_than": "task_start"
        },
        "最终中文报告说明每个 GUI 工况的结果"
    ],
    "constraints": ["不修改 EPS 安装目录", "只在 C:\\Users\\admin\\EPS 下工作"],
    "budget": { "max_steps": 40, "max_hours": 12, "step_timeout_min": 30 }
}
```

计划默认直接执行，只有需要超出约束的操作、付费资源、更改权限、预计超出预算，或者验收条件完全无法检查时，才暂停等待确认。

处理任务：`taskd show <id>`、`taskd answer <id> "..."`、`taskd hint <id> "..."`、`taskd approve <id>`、`taskd cancel <id>`、`taskd ask-status <id>`。

## 门户

connector 发现 `<ANet 目录>/taskd/status.json` 时，以 taskd 的自检结果作为“执行器就绪”，并在心跳中附上能力声明。门户保存后在智能体卡片上显示“支持长任务”，为这类目标生成按 taskd/1 委派的任务说明。旧版 connector 不认识 taskd：执行方节点关闭 auto-reply 后，门户会显示“需要检查运行软件”，直到 connector 更新。

## 自动更新

taskd 每 6 小时（首次在启动后 2–10 分钟随机）检查 Agent Space 的签名更新通道 `<门户>/downloads/agent-space-updates/`（门户地址取自同一数据目录的 `portal/connection.json`，也可在 `config.json` 里写 `update_portal`）。信任规则与 connector 的自动更新相同：

- 清单 `manifest.json` 的 Ed25519 签名必须能用内置公钥（与 connector 同一把，私钥只在运营者手里）验证；zip 的 SHA-256 必须与签名清单一致；清单序号不能倒退；通道必须是 HTTPS。
- zip 只能包含 `taskd-<版本>/` 下的 `taskd/**/*.py`、`run_taskd.py`、`README.md`；绝对路径、`..`、反斜杠、链接、多余文件、超限大小都会被拒绝，写入前再确认路径没有逃出暂存目录。
- 先在暂存目录里编译每个 `.py`，再运行新程序自己的 `run_taskd.py version`，输出必须等于清单里的版本，之后才替换 `<数据目录>/taskd/app`；旧程序留在 `updates/backup/<旧版本>/`。
- **只在空闲时替换**：有任务处于 `accepted / planning / running / verifying` 就推迟（约 10 分钟后重试），不会打断任务。等人确认或回答（`awaiting_approval / needs_user / blocked`）的任务是持久化的，不算忙。替换前先暂停接收新任务和调度，所以检查与替换之间不会有新任务开工。
- **重启**：守护进程干净停止后，按监督方式交接——Windows `supervise` 和 macOS launchd（KeepAlive）退出码 0 由它们重新拉起；systemd 用户服务退出码 75（`Restart=on-failure`）；没有监督者（例如 Linux 的 tmux 窗格）时 POSIX 进程用 `exec` 原地换成新程序，PID 不变；没有监督者的 Windows 启动一个脱离的新进程，新进程最多等 30 秒让旧进程释放锁。
- **新版必须自证**：新进程启动后，有执行器的节点自动重跑 `selftest`（会调用一次真实执行器），通过才确认；没有执行器的发起端节点运行满 60 秒确认。更新前就不就绪的节点不以自检结果判新版。自检不通过、或新版连续 3 次启动都没确认（第 4 次启动时），自动回滚到备份、屏蔽该版本、重启，并在旧程序上重跑自检；若旧程序的自检也失败，说明不是新版的问题，不屏蔽。代码变了自检指纹就会变，所以确认之前 `status.json` 的 `executor_ready` 一直是 false。
- 源码目录里直接运行的 taskd（不是 `<数据目录>/taskd/app`）拒绝自更新。

命令：`taskd update --check`（只报告 `current / available / blocked`）、`taskd update`（守护进程在运行时，请它马上检查；没运行时直接检查并替换）、`taskd auto-update [on|off]`（开关；关闭是 `<数据目录>/taskd/auto-update-off` 文件，默认开启）。`status.json` 带 `version`、`auto_update`、`update_state`（`current`、`available:x.y.z`、`updated:x.y.z`、`confirmed:x.y.z`、`rolled-back:x.y.z`、`blocked:x.y.z`、`busy:x.y.z`、`error:…`），connector 可据此上报。

发布一个新版本（在运营者的电脑上，私钥由部署者自行生成并保存在仓库外）：

```sh
python3 build.py                                     # 生成 dist/taskd-<版本>.zip（先改 taskd/service.py 的 VERSION）
python3 ../agent-portal/tools/publish_update.py build OUT --taskd dist/taskd-<版本>.zip
# OUT 里是 manifest.json、manifest.sig、connector-<版本>/、taskd-<版本>/；清单总是同时带当前 connector.py
```

上传顺序：先传 `connector-<版本>/` 和 `taskd-<版本>/` 两个目录，最后再替换 `manifest.json` 和 `manifest.sig`（先传成临时名，再连续 `mv`），这样节点看到新清单时文件已经就位；两个文件换到一半时节点验签会失败并忽略，下次再查。

## 文件

```
<ANet 目录>/taskd/
  app/                 安装时复制的程序
  config.json          配置（含 auto-reply 原设置备份）
  selftest.json        自检结果与配置指纹；程序或执行设置变化后失效
  status.json          守护进程状态（connector 读取）
  codex-home/          执行器专用的 CODEX_HOME
  state/tasks/<id>/    task.json（原子替换）、log.jsonl（只追加）、baselines.json、report.json
  state/tracked/       发起方任务、分项状态与会话交付回执（附件在 ~/ANet/received）
  taskd.log
  updates/             自动更新：state.json（已接受的最大序号、屏蔽的版本、最近结果）、backup/、failed/、pending.json（新版尚未自证）
```

## 验证范围

公开版本的验证方法和结果见 [`docs/verification.md`](../docs/verification.md)。
测试使用临时目录、模拟传输和执行器；不要求生产身份、模型登录或客户文件。
真实网络、模型执行、Windows 桌面和原会话回传需要在各自目标环境单独验收。

### Verification recovery and blocked deliveries (1.2.6)

Final verification rewinds to the earliest phase owning a failed check before it blocks. An explicit requester hint/answer on a verification-blocked task grants another bounded verification cycle while preserving cumulative steps, time, review and verification counters, original budgets, acceptance rules and earlier verified phases. Legacy 1.2.5 blocked tasks reconstruct their failed checks from the existing journal; they are not recreated or automatically resumed during an update.

A blocked reply, including a later status query, carries report.json, verification-failures.json and available authorized result files on the same input-required message. Reports retain raw observed/note results, identify partial delivery and failed attachments, and do not imply business acceptance. A single unreadable artifact does not suppress the diagnostic report.

### Content-addressed attachment aliases (1.2.7)

ANet can download identical content only once when it is announced under several names. The receiver verifies each declared name against the downloaded bytes and records `aliased_from` when another declared name has exactly the same measured size and SHA-256. A corrupt named file still fails; undeclared files and mismatching hashes cannot satisfy a missing attachment.

### 节点工作状态（1.3.0）

`status.json.work` 独立于在线心跳，统计本节点通过 ANet 接收的任务：正在执行、等待执行位置、等待用户、受阻。快速回复区分信号量前的排队与实际执行。未接入本机其他会话，所以空闲只表示没有 ANet 任务。

每个任务保留不含原始输出的活动时间线。执行器开始/结束、输出字节增长和检查动作更新活动时间；新的阶段检查通过、阶段推进或独立评审支持的诊断发现会更新进展时间。保活、重复检查和单纯新增文件不算进展。超过当前调用的预期时限，或达到已有的连续无进展步数阈值时，提示可能停滞，不据此宣称失败。记录持久化到 `activity/`；旧任务缺少记录时不从心跳或 `updated_at` 推断活动。

connector 0.8.0 将此信息传给门户。任务标题、阶段标题、问题及阻塞原因继续受原有内容同步开关控制；默认只同步编号、状态、数量和内容无关的事件。门户对 90 秒以上未更新的工作快照显示状态未知，其他成员默认只见统计。

### 逐任务来源绑定（1.3.1）

自动回传由正在运行的 requester 服务处理。`submit` 先用明确的会话 ID 和工作目录请求 `binding-check`，服务通过共享 app-server 的只读 `thread/read` 核验来源，再允许发送任务。发送后服务确认这是本机发出的任务并保存绑定；CLI 只有拿到该任务的 `binding.state=verified` 才返回自动回传可用。全局 `requester_ready=true` 不代替逐任务回执。

沙箱中的 CLI 不连接 app-server 套接字。优先通过节点私有目录的文件队列通信；如果沙箱也不能写该目录，则在来源项目的 `.taskd-requester/<节点标识>/inbox` 留下请求。守护进程从已加载会话的元数据发现项目队列，再独立核验请求中明确给出的会话和目录。回执始终写入节点私有目录，项目队列不能绑定另一个项目，不公开任意 app-server RPC 或本机命令执行。任务发送也由服务处理，沿用已有 ANet 身份、准入及预算；沙箱客户端不直接写入节点提交意图或调用 ANet 套接字。

`binding-check [--session ID] [--cwd DIR]` 不发送远端任务，也不唤醒会话。默认从 `CODEX_THREAD_ID` 和当前工作目录取来源。`track TASK_ID` 使用同一服务，绑定失败返回非零并明确 `auto_return=false`。提交前预检失败不发送；发送后的绑定失败保留任务 ID 和提交意图，输出 `do_not_resubmit=true`，后续只能对原 ID 补绑或查询。没有自动回传需求时，必须显式 `submit --no-callback`。

历史上已收到文件但缺少绑定的任务不会自动猜测来源或改绑到当前会话。仍保留原有会话忙闲检查、请求/确认/新轮次最终答复回执与不确定请求对账；绑定成功不等于结果已经回传。


### 资源调度与私有目录（1.4.0）

- 新任务默认在 `<work_dir>/taskd-work/long/<task_id>` 工作，快速回复在 `taskd-work/quick/<task_id>` 工作。计划、检查、审查、产物和交付全部使用任务自己的目录；共享源码目录仅作只读参考，先复制需要的输入再修改。已有任务保留原目录，共享同一目录的旧任务会串行，避免升级后混写。会话独立，但目录隔离不是操作系统沙箱。
- 提交可指定 `resource: "background" | "desktop" | "auto"`，默认 auto。计划中每个阶段声明 `resource: "background" | "desktop"`；纯文件处理、编译和 CLI 测试可使用后台阶段；需要 GUI 的开发循环可在同一桌面阶段完成修改源码、编译、复现和截图。旧 GUI 安装曾强制保存 concurrency=1，升级后迁移为后台无上限；新安装的 scheduler_version=2 尊重显式 concurrency=1。concurrency=0 表示无上限；capabilities.limits.concurrency 返回实际生效值（null 为无上限），scheduling.background_unlimited 明确说明该设置。
- 等待资源的线程不占执行名额；可运行的后台工作可越过等待桌面的任务。桌面锁覆盖同一阶段的多步执行与检查，阶段结束或阻塞、取消、异常时释放。已声明为后台的任务不能加入桌面阶段或桌面检查；未标注的旧桌面计划按独占处理。
- 这是执行器配合的资源协议。后台调用通过提示禁止桌面工具、GUI 启停和截图；自定义命令或拥有完整系统权限的执行器仍需遵守该约定。调度器不能阻止脱离任务进程运行的程序操作桌面，也不能替代操作系统级桌面隔离。
- `TASKD` 状态包和门户区分 `executing`、`waiting_slot`、`waiting_desktop`、`waiting_workspace`，附资源类型、队列位置与 `blocked_by` 占用任务 ID。`last_step` 保留最近摘要、产物、下一步和阻塞信息。心跳更新存活时间，不当作检查通过或阶段推进。
- 独立查询控制消息：新 ANet 查询 `{"taskd":"status","task_id":"<原任务ID>"}`，无需执行器、执行名额或桌面锁；只能查询原发起者自己的任务。也可在原任务上发送 `{"taskd":"status"}`。普通提示不会强制换执行会话，重规划或换阶段仍换会话。
- 私有节点不会出现在 ANet 公共目录。运行 `python3 run_taskd.py --data-dir ~/.anet agents --query EPS`，通过现有 Agent Space 节点连接读取本人和团队公开节点的实时目录，返回 AID、在线状态、有效并发、能力和工作状态。失败明确返回 unavailable，不把旧缓存当作在线。门户节点凭证只在 HTTPS 请求头中使用，拒绝重定向；不会把私有节点公开到 Hub。
- 更新仍只在节点空闲时安装，运行中的任务不会为了升级而被中断。旧执行阶段不会在运行中自动拆分；要释放桌面给后台任务，需要新计划按资源拆分。

### 原任务计划修订与预检（1.5.0）

`taskd revise-plan <任务 ID> --spec revision.json` 接收完整替换后缀：

```json
{"taskd":"revise_plan","request_id":"repair-configuration-1","base_revision":1,
 "from_phase":"p2","reason":"先修正配置，再进入桌面验证",
 "plan":{"phases":[
   {"id":"repair","title":"修正配置","done_when":"配置检查通过","resource":"background",
    "checks":[],"preflight":[],"produces":[],"requires":[],"final_checks":[]},
   {"id":"verify","title":"验证","done_when":"原验收条件通过","resource":"desktop",
    "checks":[],"preflight":[],"produces":[],"requires":[],"final_checks":[]}
 ],"drafted_checks":[],"approval_reasons":[],"notes":"保留原验收"}}
```

示例阶段、检查 ID 必须替换为 `show` 读到的实际计划。`base_revision` 是状态信封中的计划版本；升级前旧记录默认为 0。原 `from_phase` 之前只能保留已完成阶段；替换后缀必须保留旧检查和强制预检。相同 request_id 和正文不会重复应用，正文改变或版本过期会拒绝。每任务默认最多 3 次明确修订，独立于自动重规划额度；累计步骤、时间、总预算、目标、约束和最终验收不变。修订在当前步骤安全结束、租约释放后应用，相关检查失效重验，原文件与日志保留。普通“继续”不再唤醒已明确标为 stalled 的任务。完成或停止的任务不能复活。

阶段 `preflight` 引用确定性检查 ID，在每次执行器调用前运行；缺失、失败或无法检查都阻止执行。后台修复阶段不能依赖它自己尚未生成的预检产物。`eps_preflight` 是独立插件，参数为 config、dll、dll_sha256、project_folder、program、snapshot、parser_result 和可选 max_age_seconds。它测量配置/模块/程序文件，比较项目绑定，检查 XPL ipaint 外部变量声明，并要求新鲜的原生状态与解析记录。原生解析 JSON 必须包含 `source=eps_native_parser`、`utc`、`eps_pid`、`program_sha256`、`success`、`errors`，与快照 PID 和实际程序哈希匹配。没有实际解析证据时必须保留失败，不能用静态通过或自写成功 JSON 代替。该检查是文件证据预检，不证明当前 GUI 精确方案身份、控制器执行或沉积结果；已有观察模块尚不输出解析回执的环境需要接入该证据。

阻塞提供 code/detail/action/evidence，分类不自动授权执行建议动作。原会话回传合并相同状态和附件清单的重复消息，新文件仍独立接收与校验。门户显示计划版本、待应用修订和建议处理动作，沿用原内容同步开关。


### 附件存储与自动重排修复（1.5.1）

- 按附件清单建立稳定收据，按 SHA-256 保存内容对象；同一清单复用前重新核验，多个清单可以共享相同内容。只保留本条声明的附件，历史导出和失败 payload 在本次临时目录退出时清除。旧收件、旧收据和现有交付路径不迁移、不删除。
- 使用 ANet 本机 `pull` 返回的 CID/路径映射，解决历史 `report.json` 被编号后无法匹配的问题；必须同时匹配预期大小及本机 SHA-256，不能用文件名或 CID 自报代替测量。当前 ANet 仍会临时导出整个历史；此版本没有新增核心协议或按 CID 拉取接口。
- 同一任务的接收用跨进程文件锁串行化。确定性失败保留小收据并缓存 5 分钟，不立即重复全量导出；网络或磁盘异常允许重试。清理仅限本次新建 staging；进程被强制杀死留下的目录和旧对象不自动回收。
- 自动重排明确列出已有检查 ID 和阶段预检；程序先保留已完成阶段（含产物依赖），为原 ID 的剩余阶段补回原预检，再做严格计划校验。替换阶段 ID 时必须明确安排原有预检，程序不猜测位置；明确 `revise-plan` 的防丢检查保持不变。
- 计划失败反馈含具体未知/缺失 ID、原阶段归属和上次无效计划；重排额度不增加。因 `plan_failed` 停止时额外回传 `planning-diagnostics.json`，包含校验错误、已有计划和检查 ID，执行方报告仍属执行方自报。


### 资源声明、产物类型与可审计检查修订（1.6.0）

- 阶段新增 `requires_desktop` 布尔声明。实时原生实验、GUI、浏览器、截图和桌面状态观测应同时声明 `requires_desktop=true`、`resource=desktop`；离线文件分析与编译声明 false/background。程序拒绝后台阶段引用桌面检查或声明桌面需求，执行前再次核对实际桌面租约。后台执行器发现遗漏时报告 `blocker.code=resource_mismatch`；1.7.1 起，在任务约定与机器配置允许时自动为同一阶段重新调度桌面租约，否则交付结构化阻塞。报告需求不能授权当前后台步骤直接操作桌面。自由文本中的 GUI 语义仍依赖规划器准确声明，租约约束对拥有完全本机权限的执行器仍是合作约束。
- `files` 检查器 v2 增加 `kind=file|directory|any`，原检查省略 kind 时仍只接受文件。显式目录不使用文件大小判据，默认 `min_size=0`；目录存在不代表内容有效。`produces` 自动门槛对末尾 `/` 的路径检查 directory，其余检查 any；需要内容时使用显式 `kind=file`、目录内文件 glob 或清单。新检查标记 `origin=generated`；旧自动检查不会在升级时偷偷改写。
- `revise_plan` 可带 `check_updates` 数组，每项为 `id/type/params/reason`，修订同一个阶段检查 ID 的定义。仅允许 generated/drafted、scope=phase、无 covers 的检查；请求方原始验收、最终检查、保留的已完成阶段及历史基线保护检查不可修改。所有旧检查 ID 和 preflight 仍必须安排在替换计划里，目标、约束、预算和累积用量保持不变。
- 修订先在任务副本上验证，当前步骤结束并释放租约后再应用。日志 `check_replaced` 记录前后定义与修订请求 ID；旧计划和检查结果保留，替换后缀的新旧检查结果全部失效，不能沿用旧 pass。相同 request_id 幂等，改变正文或过期 base_revision 会拒绝。通过节点能力中的 `recovery.check_updates=1` 确认支持。
- 检查器异常、已测量确认的旧自动目录/文件错配直接归类 `checker_error`，不交给模型反复重排；单纯缺少业务文件仍是业务检查失败。同根因规划错误累计两次即 `planning_error` 阻塞，重试前核对步数预算；不同错误仍受原重排预算限制。1.7.1 起，系统阻塞的普通 hint/answer 可以触发有界恢复（见下节）；修改检查定义仍使用版本修订。尚无计划的 planning_error 也可用 `from_phase=initial` 提交首份有效计划。
- 阻塞回复同条发送 `report.json`、已存在的部分产物、`orchestration-diagnostics.json` 和 `delivery-manifest.json`（所列文件大小/SHA-256；不列清单自身）。标注 partial 与 business_acceptance=not_verified，检查和执行记录均为执行端自报，附件完整性不等于业务成功。

检查参数修订示例（必须与当前 show 的 ID、版本和完整替换后缀一起使用，不可直接套用）：

```json
{"check_updates":[{"id":"phase_4818c8fa0b0abc43b9d3","type":"files",
 "params":{"pattern":"work/p0-evidence/","kind":"directory"},
 "reason":"修复自动文件检查与实际目录产物的类型错配"}]}
```


1.6.1 收紧系统故障分类：只有旧版自动门槛对明确目录声明错误地要求 file，且本机实际测量为目录时，才归类 checker_error。正确的目录门槛遇到生产者生成的普通文件仍是业务输出失败，不自动建议修改检查器。


## 1.7.0 工作流提速

阶段是复用执行器上下文、桌面租约的一段相关工作。阶段内可用 `milestones` 声明有序的 `{id,title,checks:[检查 ID]}`；旧计划默认 `[]`，不会自动改写或恢复既有任务。

例如一个桌面阶段包含“副本与工程身份确认 → 程序加载确认 → 运行结束 → 原始结果导出”。每个里程碑使用已定义、确定性、阶段局部的检查，不允许自然语言审查或最终验收充当里程碑。证据应包含本任务工程、程序及操作身份，避免使用空的“成功标记”。针对瞬时界面状态，固定操作工具应保存可复核的持久证据。

日常步骤只检查下一待完成里程碑，核验后记录检查定义摘要和时间；同一定义在重规划后不会再次获得进展额度。检查定义改变后需重新核验。阶段声明完成时重跑全部阶段检查，最终验收仍单独运行，因此旧证据通过不代表后续仍有效。带确定性里程碑的阶段省去周期性“相关性”模型咨询；阶段与最终验收所需独立审查保留。

相邻的同窗口、同 `process_id`（可选）`uia_property` / `uia_values` 检查合并为一次 PowerShell 调用；同控件同属性只读一次，单次读取内复用控件定位。不同窗口、进程、其他检查器或截图不合并；下一批重新读取，不跨操作缓存通过结果。多个同名窗口无法唯一定位时停止核验，需指定准确标题或进程。该实现不是 UI Automation 原子快照，也没有声称已实现持久 PowerShell 会话或跨轮控件缓存。

`report.json` 的 `timing` 分别记录 `planning`、`execution`、`report_repair`、`review`、`checks`、`resource_wait` 的次数和墙钟时间。检查时间不包含独立审查；执行器时间仍混合模型思考、工具与等待，不能当成 EPS 运行时间或模型独立耗时。实际 EPS 运行和导出时长需由操作工具另行测量。门户显示调用目的、当前里程碑和累计耗时，仍遵守任务内容同步开关。

本版不改变模型、总预算、桌面互斥或已有任务计划，不自动重试可能已经生效的 EPS 操作。固定 EPS 加载/运行/导出脚本须针对已确认的接口、独立工程副本和授权动作范围实测后接入。


### 开发循环、诊断进展与普通消息恢复（1.7.1）

- 桌面租约表示互斥访问桌面，不限制文件和命令行工作。同一阶段可以完成“启动发现问题 → 修改源码 → 重编译 → GUI 再测试”；独立且不再依赖当前桌面状态的长时间离线工作仍适合后台阶段。
- 后台步骤报告缺少桌面资源时，若任务不是显式 background 且节点启用了桌面能力，taskd 保留阶段、检查和预算，仅更新资源声明与计划版本。当前步骤返回并释放后台租约后，调度器重新取得桌面锁及环境检查，才继续执行。不耗费重规划额度；不会放宽任务或机器的桌面权限。
- 新通过的验收检查之外，新诊断结论、排除的假设、验证的兼容方案也可以构成进展：必须有在任务目录内实际存在且内容未见过的证据，并经独立评审判为 supported。明确引用的编译/诊断日志可作为证据；只有新时间戳、重复症状、改名文件或无证据自述不能获得进展。记录 `diagnostic_progress` 包含证据哈希与评审理由，同时更新活动面板。诊断进展不通过任何验收门槛；无评审器、评审额度耗尽、unclear/unsupported 时不计入。
- stalled 的普通“继续/按这个方向修复”恢复当前阶段；planning_error 重新进入规划并获得一轮有界校验机会，保留累计错误与重规划次数。checker_error 先重跑当前阶段确定性检查；若仍是系统错误则重新阻塞，不浪费模型执行步骤。resource_mismatch 只能在原任务及节点允许桌面的情况下恢复。原先要求的计划审批仍有效。
- 所有恢复保留任务 ID、目标、约束、原始验收、完成阶段和累计步数/时间/评审/重规划预算。每条新补充消息最多触发一次恢复，重复消息 ID 幂等；没有新消息不会循环复活。升级本身不恢复旧阻塞或已取消任务。真正需要修改检查定义时仍使用可审计的 `revise_plan/check_updates`。

### Windows 状态读取并发修复（1.7.2）

- Windows 回归暴露了原子替换 task.json 时读取端短暂 PermissionError 的问题。读取现在与写入一样最多尝试 20 次、间隔 50 ms；持续权限错误仍抛出，文件不存在仍返回默认值，损坏 JSON 不隐藏。测试清理前会停止并等待工作线程，避免文件仍被占用。
