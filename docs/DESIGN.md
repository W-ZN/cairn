# cairn 设计文档（草案 v2，原 council）

日期：2026-11-01　作者：Youkai　状态：待 Oliver 确认（第 10 节）

> 命名：cairn 是登山路上堆起来的石头路标。它是给后来人的标记（接力、续跑），也是沿途累积下来的记录（会话簿、任务账本、历史摘要）。这个名字由 Oliver 和 dsh 讨论后选定，2026-11-01。

## 1. 背景与目标

council 现在是一个单文件脚本：`~/.ai/skills/council/scripts/council.py`，共 1588 行。它支持 7 个成员：codex、gemini、claude、grok、pi、opencode、dsh。现有能力有：
- 讨论和交叉评审；
- 默认只读；
- 命名会话簿：`--session`、`--session-list`、`--session-forget`，67 个测试通过，尚未 commit。

本期目标是取代官方 `openai/codex-plugin-cc`（v1.0.6）在 Claude Code 生态里的位置，同时不绑定 Codex。官方插件 7/8 以后就没有维护，已知问题有：
- broker 进程泄漏；
- SessionStart hook 会重复往 `$CLAUDE_ENV_FILE` 里追加内容；
- rescue 卡住；
- 后台任务没跑完就报完成。

本期提供四种能力：

| 能力 | 说明 |
|---|---|
| 讨论/评审 | 保留现有行为不变 |
| 后台任务 | 用 start/status/result/cancel 管理在后台跑的 CLI |
| 续跑 | 按名字接着同一个成员的 session 继续 |
| 接力 | 主 agent 写好交接文件，再开一个新 session 接手，对标 `/codex:transfer` |
| 历史与摘要 | 读取任意 CLI 的会话历史，生成去掉工具噪声的摘要或交接文件（合并原 session-digest 的功能） |

原则如下：
- 调度和综合由主 agent 负责，cairn 只做桥梁和账本，不做自动辩论，也不求共识。
- 通用机制只写一份。每个 CLI 只写一层薄适配。

## 2. 形态

```
~/.ai/tools/cairn/              # 独立 Python 包，仓库 W-ZN/cairn，用 uv tool install -e 安装
  pyproject.toml
  cairn/
    cli.py                      # 子命令入口，全部支持 --json
    core/
      state.py                  # 原子写和文件锁
      jobs.py                   # 后台任务生命周期
      sessions.py               # 命名会话簿
      handoff.py                # 交接文件的模板和校验
      perms.py                  # 权限模型
      runner.py                 # 前台运行（从现有的 run_stream/_collect 迁过来）
      transcript.py             # 统一的 Event/Transcript 模型、脱敏和裁剪
      digest.py                 # 摘要渲染：balanced/dialogue/tools/handoff 四种模式
    adapters/
      base.py                   # Adapter 协议和能力标记
      codex.py gemini.py claude.py grok.py pi.py opencode.py dsh.py
  tests/
~/.ai/skills/cairn/             # skill：/cairn 的薄封装
  SKILL.md                      # 只负责拼参数、调用 cairn CLI、渲染 JSON
  （旧的 council 和 session-digest 目录保留一个过渡期，只做转发）
~/.local/state/cairn/         # 运行状态，不放在 skill 目录里
  sessions.json
  jobs/<id>/
  handoffs/
```

## 3. 子命令

| 命令 | 作用 |
|---|---|
| `cairn ask --with a,b --cd DIR --prompt …` | 讨论：一个或多个成员前台运行（现有行为） |
| `cairn review --with … --cd DIR --prompt …` | 交叉评审：只派给只读确实生效的成员（见 6.2） |
| `cairn job start --with X --cd DIR --prompt … [--session N]` | 启动后台任务，返回 job id |
| `cairn job status [ID]` / `job list` | 查看状态 |
| `cairn job result ID` | 读取结果 |
| `cairn job cancel ID` | 按进程组取消 |
| `cairn job prune` | 清理过期的任务 |
| `cairn session list` / `session forget N` | 管理会话簿 |
| `cairn transfer --with X --handoff FILE [--background] [--session N]` | 接力 |
| `cairn history find|recent` | 在各个 CLI 的本地记录里查找会话（按 id 片段、标题或路径） |
| `cairn history show <会话名或 id> [--mode balanced|dialogue|tools|handoff]` | 只读取出某个会话的历史并生成摘要 |
| `cairn digest <会话> --mode handoff -o FILE` | 生成交接文件，可以直接交给 `transfer` |

通用参数有 `--session NAME`、`--allow-write --write-scope "…"`、`--timeout`、`--json`。所有子命令在加 `--json` 时输出稳定的 JSON 结构，供 skill 渲染。

## 4. 通用层

### 4.1 状态文件
- 写入用同目录的临时文件加 `os.replace`，保证原子性；同时用 `fcntl.flock` 加排他锁。
- 状态机：`queued → running → (succeeded | failed | cancelled | timed_out | likely_dead)`。一旦进入结束状态，就不允许再写回 running。

### 4.2 后台任务
任务目录是 `jobs/<id>/`，里面有：
- `meta.json`：member、cwd、mode、write_scope、session 名、SESSION_ID、pid、pgid、进程启动时间、超时、各个时间戳、状态；
- `stdout.log`、`stderr.log`；
- `result.md`：由适配层从输出里提取的最终回答；
- `exit_code`：由包装进程在子进程退出后写入。

启动方式：
- `cairn job start` 先 fork 出一个包装进程，包装进程用 `setsid` 成为新进程组的组长，然后拉起成员 CLI。
- 成员 CLI 的 stdin 一律接 `</dev/null`，因为好几个 CLI 在没有 TTY 时会挂死。
- 设硬超时，默认 60 分钟。超时后杀掉整个进程组，状态记为 timed_out。

完成判断要同时看两件事：
- 有 `exit_code` 才算结束；
- 如果进程已经不在了，却没有 `exit_code`，就标为 `likely_dead`，不报完成。

取消：
- 先核对 pgid 和进程启动时间，和 meta 一致才动手。核对不上的一律不杀，避免误杀被复用的 pid。
- 核对通过后对进程组发 SIGTERM，等 5 秒，还在就发 SIGKILL。

上限和清理：
- 同时运行的任务默认最多 4 个，超出就拒绝启动。
- 已结束的任务保留 7 天，`prune` 和每次 `job start` 都会顺带清理。

### 4.3 会话簿
- 文件是 `sessions.json`，结构为 name 对应 `{member, cwd, SESSION_ID, updated_at}`。这部分沿用已经实现的逻辑。
- 同一个名字如果 member 或 cwd 冲突，按用法错误处理。
- 前台和后台共用会话簿。成功跑完并拿到 SESSION_ID 才回写；失败不写。
- 同一个会话名同一时间只允许跑一轮，用锁和 running 状态保证。

### 4.4 交接文件
文件放在 `handoffs/<ts>-<slug>.md`，格式如下：

```markdown
---
from: claude            # 交出方
to: codex               # 接手方
cwd: /path/to/repo
created: 2026-11-01T14:00:00+08:00
scope: read-only | "path | region | change"
---
## 目标
## 进展
## 关键文件
## 已定决策
## 下一步
## 约束
```

- 交接文件由主 agent 按模板来写。cairn 只检查格式（YAML 头和 6 节是否齐全）并负责投递。
- 不用 hook，也不读、不 hash Claude 的 transcript。

### 4.5 权限
- 统一由 `perms.py` 计算 mode：read-only 或 write。
- 默认只读，写入必须显式带 `--allow-write --write-scope`。前台、后台、接力都适用同一条规则。
- 每个成员的具体权限参数由它的适配层给出，沿用现有的映射：
  - codex 用 sandbox；
  - opencode 用 plan 或 build 加禁用规则；
  - pi 用工具白名单；
  - dsh 用 read-only 或 workspace-write，永不用 danger-full-access。

## 5. 适配层协议

```python
class Adapter(Protocol):
    name: str
    readonly_enforced: bool   # 只读是否经过验证、确实能挡住写入
    supports_resume: bool
    native_import: bool       # 是否支持原生导入交接文件
    needs_tty: bool

    def headless_cmd(self, prompt, cwd, mode, opts) -> Cmd   # argv、env、是否走代理
    def resume_args(self, session_id) -> list[str]
    def parse_session_id(self, output) -> str | None
    def deliver_handoff(self, path) -> Delivery              # 原生导入，或拼进首轮 prompt
    # 可选（历史与摘要）
    def locate_sessions(self, query) -> list[SessionRef]      # 在本地记录里按 id、标题或路径查找会话
    def parse_transcript(self, ref) -> Transcript             # 转成统一的 Event 模型
```

各成员的预期取值如下（实现时逐个实测确认）：

| 成员 | 无界面运行 | 续跑 | readonly_enforced |
|---|---|---|---|
| codex | 前台走 app-server（现状）；后台走 `codex exec --json` | `codex exec resume <id>` | true |
| opencode | `opencode run`，加 plan 和禁用 edit/bash | `-s <id>` | true |
| pi | 用 `--tools read,grep,find,ls` | 用 pi 自己的续接参数（待实测） | true |
| dsh | 用 `DSH_PERMISSION_MODE` | `--session-id <id>` | **false**（被 dsh-purge 绕过） |
| claude、grok、gemini | 沿用现有方式 | 沿用现有方式 | true |

新增一个 CLI 时，只要写一个 `adapters/<cli>.py` 并注册，后台、续跑、接力就都能直接用。另外预留一个 acpx 适配层的接口位，用来覆盖长尾 CLI，第一期不接。

## 6. 关键行为

### 6.1 续跑
`--session N` 会去会话簿里查 SESSION_ID，查到就调用 `resume_args`，查不到就新开一个。显式传入的 `--SESSION_ID` 优先，跑完后仍然回写。

### 6.2 评审只派给只读确实生效的成员
`review` 遇到 readonly_enforced=false 的成员（目前只有 dsh）会拒绝，除非显式加 `--allow-unenforced`。

### 6.3 历史与摘要（合并 session-digest）
- 原来的 `session_digest.py`（2212 行）本身是确定性的解析和渲染，不调用模型，可以直接拆开迁移：
  - 各个 CLI 的定位和解析（codex 的 rollout 和 fork 链、claude 的 projects、grok 的 chat_history 和 sqlite 标题、pi 的 sessions）搬进各自适配层的 `locate_sessions` 和 `parse_transcript`；
  - 脱敏、裁剪、thinking 摘要和四种渲染模式搬进 `core/transcript.py` 和 `core/digest.py`。
- `--session NAME` 可以直接作为历史的定位方式，因为会话簿里已经有 member 和 SESSION_ID，所以「派出去的任务」和「回头看历史」走的是同一条路由。
- 主 agent 的会话（比如 Claude）也能被 digest，生成交接文件。
- 只读：任何情况下都不写、不移动、不删除各个 CLI 的历史文件。
- 默认对密钥类的值脱敏。
- 原 session-digest 的测试全部迁过来，作为回归测试。

### 6.4 接力
1. 交接文件有两种来源：主 agent 按模板自己写，或者用 `cairn digest <会话> --mode handoff` 生成后再由主 agent 补充。
2. 运行 `cairn transfer --with X --handoff F --session N`。
3. cairn 校验文件，再调用 `deliver_handoff`，前台或后台都可以。
4. 拿到的 SESSION_ID 记到 N 下面，之后用 `--session N` 继续。

## 7. 和官方插件已知问题的对照

| 官方问题 | 本设计的处理 |
|---|---|
| broker 进程泄漏 | 没有常驻 broker；每个任务一个进程组，带超时，cancel 时杀整个组 |
| SessionStart hook 重复追加 | 不装任何 hook |
| rescue 卡住 | stdin 接 /dev/null，加硬超时，状态可查也可取消 |
| 后台任务误报完成 | 必须有 exit_code 才算完成；进程消失但没有 exit_code 就记为 likely_dead |

## 8. 测试与实施阶段

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | 全量备份到 `~/.ai-backups/council-20261101-ext/`，用真实 CLI 测一遍会话簿 | opencode 续跑时 SESSION_ID 一致，且记得上下文 |
| P1 | 搭好包骨架，把 7 个适配层迁过来，并留兼容入口 | 原有 67 个测试全过，旧命令行为不变 |
| P2 | 实现 jobs、取消、上限和清理 | 新增并发、意外死亡、超时、取消时归属核对的测试；用 opencode 和 pi 实际跑一个后台任务 |
| P3 | 实现续跑和接力，合并 session-digest | 每个 CLI 实测续跑和交接；原 session-digest 测试全过 |
| P4 | 写 skill 薄封装和文档，完成改名（skill 目录、magpie 软链接、各处引用；提示词里那三行要你单独确认） | `/cairn` 的各个子命令都能跑通 |

开发在 W-ZN/cairn 仓库的分支上进行，阶段性成果发 PR，合并前由你确认；本机 `~/.ai` 里的改动仍然不擅自 commit。

## 9. 改名影响面
- `~/.ai/skills/council`、`session-digest` 目录，以及 `~/.config/magpie/library/skills/` 下的软链接
- research-reading 和 session-digest 里的引用
- 系统提示词：`~/.ai/prompts/AGENTS.md`、`~/.ai/prompts/claude/rules/skills.md`、`~/.codex/AGENTS.md` 里各一行（要你单独确认）

## 10. 待你确认

1. 后台任务默认只读，显式加 `--allow-write --write-scope` 才写。（建议：同意）
2. 源码放在 `~/.ai/tools/cairn`，状态放在 `~/.local/state/cairn`。
3. `review` 默认拒绝 dsh，加 `--allow-unenforced` 才允许。
4. 默认超时 60 分钟、并发上限 4 个、已结束任务保留 7 天。
