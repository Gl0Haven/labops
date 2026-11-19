# LabOps

[English](README.md)

LabOps 是一个在本机运行的 AI 编程助手管控台，管的是 Codex 和 Claude 在你自己的 Git 仓库里干活的全过程。每一处改动都要走完任务、轮次（各自独立的 worktree）、独立验证、审查和闸门，最后由你在终端里批准一次 fast-forward，才进入 `main`。围绕这条主线，它还跟踪训练进度、排 GPU 队列、把告警推到手机，并在 Claude 和 Codex 桌面端里已经打开的会话之间传递消息。

所有状态存在一个 SQLite 数据库里。每次提交的代码都由 Git 保留在 `refs/labops/*` 下；进程中途崩溃时，操作日志负责把 Git 和 SQLite 恢复到一致。

## 限制

- 只支持 Windows。LabOps 依赖 PowerShell 7（`pwsh`）、Windows 命名管道和 Windows 进程查询，没有在 macOS 或 Linux 上跑过。
- 单用户。在终端（TTY）里确认只能防误操作，防不了恶意行为：同一 Windows 用户下运行的任何程序都能读数据库和仓库。
- 给 Codex 发消息走的是 Codex 桌面端的私有 IPC（`\\.\pipe\codex-ipc`），在 Codex 桌面端 26.1111 上测过，桌面端升级后可能失效。给 Claude 发消息走官方文档化的[跨会话消息](https://code.claude.com/docs/en/cross-session-messaging)收件管道。
- 手机通知目前只内置一个发送器，调用 OpenClaw 命令行。接其他渠道需要自己写发送器，见[通知](#通知)。

## 安装

需要 Python 3.11 及以上，`PATH` 里有 Git；GPU 队列启动任务还需要 PowerShell 7。

```powershell
git clone <this-repo> labops
cd labops
python -m pip install -e ".[mcp]"   # 核心 CLI 没有第三方依赖；[mcp] 额外安装 MCP server 所需的包
labops init
```

状态目录默认是 `%USERPROFILE%\.labops`，用 `LABOPS_HOME` 可以改。所有命令都输出 JSON，出错时带 `error`、`message`、`detail` 三个字段。

不安装、直接在源码目录里用时，设 `$env:PYTHONPATH = "<labops>\src"`，把命令里的 `labops` 换成 `python -m labops.cli`。

## 任务流程

仓库只需登记一次。建任务时会冻结一份验收档案（要跑的命令、超时、改动允许落在哪些路径）和一份冻结文件策略。实现方按轮次干活，每一轮有自己的 worktree 和分支。

```powershell
labops repo add --repo-id toy --path C:\work\toy --trusted-for-exec
labops policy set --repo-id toy --file examples\policy.toy.json
labops task create --repo-id toy --task-id T20260101-demo --goal "Fix X" --profile examples\profile.toy.json
labops round open --task-id T20260101-demo --actor codex --role implement
labops wt create --round T20260101-demo/r1
# ……助手在 worktree 里改文件……
labops snapshot --round T20260101-demo/r1 --actor codex --message "Fix X" --files src\x.py
labops submit --round T20260101-demo/r1 --actor codex --commit <sha> --request-id r1-submit
labops verify --round T20260101-demo/r1          # 在另一个 worktree 里跑冻结的验收命令
labops review open --round T20260101-demo/r1 --reviewer opus
labops review submit --review <review-id> --actor opus --verdict PASS --request-id r1-review
labops gate check --task-id T20260101-demo
labops integrate propose --task-id T20260101-demo
labops integrate approve --integration <ig-id>   # 要求你输入 tree 前缀确认
```

闸门检查五件事：最新一轮已验证、已审查，没有阻断性的审查结论，改动没有超出档案允许的路径，冻结文件没被改动（除非你批准过对应的决策）。同一个角色既修又审的轮次，还要你执行 `labops approve self-review --task-id T`。任务开始后 `main` 又前进了的，由助手开一个修复轮次执行 `sync_main`，再重新提出合入。

文件格式见 `examples/profile.toy.json` 和 `examples/policy.toy.json`。`python scripts\smoke_m0a.py` 会在临时仓库里把整条流程跑一遍。

## 通过 MCP 接入助手

`labops.mcp_server` 是一个 stdio MCP server。每次工具调用都在一个短命的 worker 进程里执行，从 `src` 加载当前代码，所以改了实现不用重启客户端；改了工具名或参数，仍然要重启。

客户端能看到哪些工具，由 `LABOPS_ACTOR` 决定。它只记录“谁做的”，不做身份认证。

| 角色 | 对应客户端 | 能做什么 |
| --- | --- | --- |
| `codex` | Codex | 开实现轮次、快照、提交、同步 `main`、登记队列任务、认领 GPU 进程、给 Claude 会话发消息、给你发通知 |
| `opus` | Claude | `codex` 能做的（`message_claude` 除外），另外能开修复轮次、提交审查、提出合入、给任意桌面会话发消息、查看进度 |
| `secretary` | 聊天助手（例如 OpenClaw agent） | 查看进度和历史、记录排队意图和选择、把聊天里的话转发到桌面会话 |

没有任何 MCP 工具能批准或合入。批准和 `integrate approve` 只在 CLI 里提供，并且必须在 TTY 里执行。

Codex（`~/.codex/config.toml`）：

```toml
[mcp_servers.labops]
command = '<labops>\.venv\Scripts\python.exe'
args = ['-m', 'labops.mcp_server']
cwd = '<labops>'
env = { LABOPS_ACTOR = 'codex', LABOPS_HOME = '<home>\.labops', PYTHONPATH = '<labops>\src' }
tool_timeout_sec = 60
```

Claude Code（`claude mcp add` 或 `.mcp.json`）：

```json
{
  "mcpServers": {
    "labops": {
      "command": "<labops>\\.venv\\Scripts\\python.exe",
      "args": ["-m", "labops.mcp_server"],
      "cwd": "<labops>",
      "env": { "LABOPS_ACTOR": "opus", "LABOPS_HOME": "<home>\\.labops", "PYTHONPATH": "<labops>\\src" }
    }
  }
}
```

新会话里的助手先调 `next_task`，就能拿到该做的任务和下一步动作；`get_task_context` 返回任务说明和通用交接规矩。

Codex 还可以接两个可选 hook，把会话事件记进 spool，不保存消息内容：`Stop` 和 `SessionEnd` 都指向 `<labops>\labops.cmd hook ingest --client codex`。

## 通知

已提交、已验证、审查阻断、等待批准、训练失败等事件都会进入一张 outbox 表。`labops notify run` 扫描新事件、合并后发送。用 Windows 任务计划程序或 OpenClaw cron 每分钟跑一次即可。

- 紧急类事件下一次运行就发，同一次里有多条紧急事件时合成一条；其余类别攒到汇总窗口（默认 180 分钟）再一起发。用 `labops notify config --urgent-kinds a,b --digest-minutes N` 调整。
- `labops notify mute KIND` 屏蔽某一类；`repo set --alert-detail off` 让该仓库的告警只发首行。
- 执行 `labops notify config --llm-rewrite on` 并设置 `DEEPSEEK_API_KEY` 后，告警会交给 DeepSeek 改写成更短的文字。改写结果丢了任务号、SHA、数字或命令，就作废，改发模板原文。机密仓库从不改写。
- 设置了 `LABOPS_NTFY_TOPIC` 时，没能送达的告警会把首行转发到 [ntfy](https://ntfy.sh) 兜底。

发送器可以替换。发送器就是一个可调用对象 `sender(text) -> (status, error)`，`status` 取 `sent`、`failed`、`unknown`、`token_expired` 之一；带属性 `markdown_style = "wechat"` 的发送器会先把文本转成微信能渲染的 Markdown。用 `labops.notify.register_sender(name, factory)` 注册，用 `labops notify config --sender NAME`（存进数据库）或环境变量 `LABOPS_NOTIFY_SENDER` 选择。

内置的 `openclaw` 发送器执行 `openclaw message send --channel <channel> --account <account> --target <target>`。需要设置 `LABOPS_NOTIFY_ACCOUNT` 和 `LABOPS_NOTIFY_TARGET`；换成 OpenClaw 的其他渠道，就改 `LABOPS_NOTIFY_CHANNEL`（默认 `openclaw-weixin`）。目前只测过微信渠道。发送结果不明或无法解析时记为 `UNKNOWN`，不会自动重发。

`labops notify test-send` 用当前发送器发一条固定的测试消息。

## 训练进度与 GPU 队列

`notify run` 同时读取 background-run 的状态目录（默认 `%USERPROFILE%\.codex\background-state`，可用 `LABOPS_BG_STATE_ROOT` 指定），跟踪属于已登记仓库的训练：从日志尾部提取进度，发现失败，以及 60 分钟内日志和关注文件都没更新的卡住情况。不经这个启动器、直接起的进程，可以用 `labops runs adopt` 或 MCP 工具 `adopt_process` 登记。LabOps 同时记下 PID 和进程启动时间，PID 被系统复用时不会认错。

`labops repo set --progress-rule '<json>'` 可以从仓库里的 JSON 或文本文件提取进度，`labops repo progress-preview` 显示每条规则匹配到了什么。

GPU 队列把“想跑什么”和“怎么跑”分开：聊天助手只记录意图（`queue_intent`），由 Codex 或 Claude 登记仓库里已有的 `.ps1` 入口（`queue_register`）；入口的 SHA-256 在启动前会再核对一次。GPU 正忙时，LabOps 对每一项只问一次：等你手动调度，还是空闲后自动开跑。同一时刻最多只有一项处于 `LAUNCHING`，由部分唯一索引保证。

```powershell
labops queue config --entry-root C:\work --launcher C:\tools\Start-BackgroundRun.ps1
labops queue config            # 查看当前设置
```

不设 `--entry-root` 时，只要求入口位于所属仓库内。不设 `--launcher` 时，LabOps 不会自己启动任务；队列默认的 `handoff` 模式会通知助手，附上一条可直接复制的启动命令。启动器的调用方式是 `pwsh -File <launcher> -ScriptPath <entry> -WorkingDirectory <repo> -RunDirectory <run-dir>`。

## 给桌面会话发消息

LabOps 能把消息送进 Claude 或 Codex 桌面端里已经打开的会话，再读回回复，对话始终留在你平时用的窗口里。

| 工具 | 谁能用 | 作用 |
| --- | --- | --- |
| `desktop_list` | Claude、聊天助手；Codex（只能列 Claude 会话） | 列出候选的 Claude 会话或最近的 Codex 聊天 |
| `desktop_send` | Claude | 发给某个 Claude 会话或 Codex 聊天 |
| `desktop_status` | Claude、聊天助手 | 查询状态（`not_seen`、`running`、`completed`、`interrupted`、`session_gone`）和回复 |
| `desktop_dispatch` | 聊天助手 | 代你转发聊天里的一句话，带“转发”标记 |
| `message_claude` | Codex | 给某个 Claude 会话发消息，带“来自 Codex”标记 |
| `notify_user` | Codex、Claude | 往你手机上推一条简短回报 |

对应的 CLI 是 `labops desktop list|send|status`。

每条消息都带一个标记 `[labops:<request_id>]`。同一个 `request_id` 最多发一次；发送结果说不清时，请求记为 `UNKNOWN`，LabOps 去读 transcript 或 rollout 确认实际情况，不会重发。Codex 聊天必须在桌面端打开才能收消息。

从聊天转发出去的消息到了终态，`notify run` 会把回复推回手机。Codex 和 Claude 之间的往来也会作为 `agent_relay` 通知入队；配置了 DeepSeek 时先概括到 400 字以内。来自聊天或 Codex 的消息都带前缀，说明这不是你本人的直接指令；Claude 把它们当作同伴消息处理，不当作你的批准。

## 检索、历史与备份

- `labops search "QUERY"` 把任务号、审查号、决策号和 SHA 前缀的精确匹配，与 trigram、bigram 两路全文检索合在一起；中文按重叠二元组切分。先用 `labops index rebuild --all` 建索引。
- `labops history`、`labops events`、`labops context` 用来回答“这个任务里发生过什么”。
- `labops backup run --all` 把每个仓库的全部 ref（包括 `refs/labops/*`）打包并轮转旧包；`labops backup verify-restore` 把包克隆到临时目录，核对 ref 是否一致。
- `labops onboard` 用一条幂等命令完成仓库登记、配置、备份和建索引。

完整命令列表见 `labops --help` 和 `labops <command> --help`。

## 机密仓库

`labops repo set --repo-id ID --confidential on` 打开后，`secretary` 角色看不到该仓库的目标、审查意见、路径和完整 SHA；该仓库不参与 DeepSeek 改写；推到手机的桌面消息正文换成占位文字。关闭时需要在终端里输入完整的仓库 ID。

## 环境变量

| 变量 | 用途 |
| --- | --- |
| `LABOPS_HOME` | 状态目录（默认 `%USERPROFILE%\.labops`） |
| `LABOPS_ACTOR` | MCP 角色：`codex`、`opus` 或 `secretary` |
| `LABOPS_CODE_ROOT` | MCP worker 加载的源码目录（默认与 server 同目录） |
| `LABOPS_NOTIFY_SENDER` | 数据库里没有设置时使用的发送器（默认 `openclaw`） |
| `LABOPS_NOTIFY_ACCOUNT`、`LABOPS_NOTIFY_TARGET`、`LABOPS_NOTIFY_CHANNEL` | OpenClaw 发送器的账号、目标和渠道 |
| `LABOPS_NOTIFY_TIMEOUT` | 发送超时秒数（默认 180） |
| `LABOPS_NTFY_TOPIC` | ntfy 兜底 topic |
| `DEEPSEEK_API_KEY`、`LABOPS_DS_BASE_URL`、`LABOPS_DS_MODEL` | DeepSeek 改写与概括 |
| `LABOPS_BG_STATE_ROOT` | background-run 状态目录 |
| `LABOPS_CLAUDE_HOME`、`LABOPS_CODEX_HOME` | Claude 和 Codex 的数据目录（默认 `~/.claude`、`~/.codex`） |

## 安全要点

- 只在登记时加了 `--trusted-for-exec` 的仓库里跑验证，而且只跑档案里冻结的 argv，不经过 shell。
- `snapshot` 只提交列出的文件，要求它们与全部未提交改动完全一致，并拒绝模型权重、数据目录、大文件和常见密钥格式。
- 合入是一次 fast-forward，执行前核对 `main` 自候选生成以来没有变化。
- hook 事件只记录哈希、ID 和时间，不记录消息内容。

## 开发

```powershell
python -m unittest discover -s tests -t .
python scripts\smoke_wxdispatch.py --offline
```

测试全部使用临时仓库和假传输层，不碰真实的桌面会话、聊天账号，也不联网。启动 MCP server 的测试使用 `LABOPS_TEST_PYTHON` 指定的解释器（默认是当前解释器）。CI 在 Windows 上用 Python 3.11 和 3.13 跑全量测试。

## 许可证

MIT，见 [LICENSE](LICENSE)。
