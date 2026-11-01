# Council operations

[SKILL.md](../SKILL.md) owns invocation, member selection and authorization rules. This reference explains execution details; use `scripts/council.py --help` for current flags and defaults.

## Execution and communication

`council.py` submits one independent CLI process per selected member through a `ThreadPoolExecutor`. Every member receives the same composed prompt. It collects events concurrently and returns results in requested member order after all processes finish. Members do not automatically exchange answers, inherit the caller's context or subscribe to its progress. They use their own CLI tools/configuration and the shared `--cd` filesystem.

| Member | Transport | Read-only mode | Write mode | Resume |
| --- | --- | --- | --- | --- |
| codex | `app-server --listen stdio://`; JSON-RPC over stdin/stdout | `sandbox=read-only` | `sandbox=workspace-write` | `thread/resume` |
| gemini | `--prompt … -o stream-json`; CLI event stream | `--approval-mode default` plus a deny-all/read-search-only policy | `--approval-mode auto_edit` | `--resume` |
| claude | `-p --output-format stream-json --verbose --permission-prompts none` | `--permission-mode auto`, read-only built-in tool allowlist and no MCP | `--permission-mode auto`; explicit `acceptEdits` alternative | `--resume` |
| grok | `-p --output-format streaming-json --no-plan`; CLI event stream | `--permission-mode auto` with read/search-only tools | `--permission-mode acceptEdits` | `--resume` |
| pi | `-p --mode json --no-extensions`; CLI event stream | `--tools read,grep,find,ls --no-mcp` | Explicit built-in write tools | `--session` |
| opencode | `run --format json --dir <cd>`; JSONL events | `--agent plan` plus `OPENCODE_CONFIG_CONTENT` denying edit/bash | `--agent build`, no deny override | `-s` |
| dsh | `--profile headless --json`; JSONL events; cwd is `--cd` | `DSH_PERMISSION_MODE=read-only` | `DSH_PERMISSION_MODE=workspace-write` | `--session-id` |

Permission modes and tool allowlists reduce available actions; file/region scope is a prompt constraint, not a per-file filesystem sandbox. Native planning modes may still write their own plan/session metadata. They are not equivalent to an OS read-only sandbox. Review actual changes before accepting delegated implementation.

Grok read-only calls expose only `read_file,grep,list_dir,web_search,web_fetch`; shell, editing, delegation and MCP bridge tools are excluded. Both read-only and authorized write calls pass `--no-plan` and `--no-subagents`.

Claude defaults to `auto` for both read-only and authorized scoped writes. Read-only calls provide only `Read,Glob,Grep,WebFetch,WebSearch`, add `--strict-mcp-config` without an MCP configuration, and deny `EnterPlanMode` and `ExitPlanMode`; the prompt also carries the no-write constraint. `auto` reviews actions through a classifier, while `acceptEdits` auto-approves edits and selected filesystem commands but may deny test or network commands. `--permission-prompts none` denies unresolved approvals instead of waiting/retrying. It requires Claude Code 2.1.259+. `--claude-permission-mode plan` remains an explicit read-only choice only. For an explicit Haiku request in that plan mode, council passes an invocation-local `--settings` allowlist containing exactly that requested model. Claude documents that excluding all Sonnet models prevents plan-mode Haiku upgrades; this keeps an explicitly requested plan mode without persisting a global model policy. `availableModels` lists merge across non-managed settings scopes and a managed list overrides them, so pre-existing configuration can still widen or replace this guard; the receipt remains the outcome check. If auto is unavailable, inspect `effective_permission_mode` and `permission_denials` when returned; do not silently switch models, fall back to plan, or bypass permissions. `--claude-permission-mode acceptEdits` is an explicit write-mode alternative, not a recovery fallback.

Gemini read-only calls use `--approval-mode default` with [gemini-read-only.toml](gemini-read-only.toml). That headless policy denies every tool except built-in file read/search and web-fetch/search tools, which prevents edits, shell use, MCP tools, delegation, and plan transitions. Existing higher-precedence user/admin policies can still override it. Gemini `auto_edit` allows edits without granting blanket shell execution; required test commands can still be denied. Pi `--approve` trusts project resources; it is not a tool-approval mode and council does not add it automatically. Saved/configured project trust remains the CLI's responsibility. Pi read-only calls disable MCP so MCP tools cannot bypass the built-in read-tool allowlist.

OpenCode read-only removes edit/write/bash tools through the plan agent and the deny config. A turn completes on `step_finish` with `part.reason=stop`; `sessionID` is the resume id. `type=error` or a non-zero exit fails the turn. dsh completes on `type=final`. The answer is `final.text`, or accumulated assistant `text` events when that is empty. `turn_end` with `reason.kind=error` fails and reports `error.message` and `error.code`. dsh has no model flag. Neither member is pinned to Surge; both inherit the process proxy. Council never sets dsh to `danger-full-access`.

Codex performs `initialize`/`initialized`, thread start/resume, `turn/start`, event collection and shutdown. Interactive server requests are refused because council is headless (`approvalPolicy=never`, also explicit in the exec backend). Initialization or thread start/resume failure triggers the existing `codex exec --json` fallback; failures after turn start are returned without replaying the task. `--codex-backend exec` explicitly selects the fallback.

## Delegate implementation and continue

Supply a compact task contract, not the entire caller transcript:

```text
Goal: fix the parser's handling of escaped quotes.
State: lexer changes are complete; parser tests still fail on escaped delimiters.
Own: src/parser.py, tests/test_parser.py; preserve existing edits.
Check: run the parser tests; report changes, results and blockers.
```

Put write boundaries in `--write-scope`; the script appends the mode constraint. It creates no separate authorization file, but the scope is part of the member prompt and may be retained in that CLI's transcript. Write flags must be present on each authorized implementation call, including resumes; the earlier session is not an authorization source.

Each invocation executes one turn per member. Continue one member at a time, with no overlapping calls to the same session. The caller owns scheduling and synthesis; council is the bridge and the named session ledger.

```bash
/opt/anaconda3/envs/pytorch/bin/python /Users/wzn/.ai/skills/council/scripts/council.py \
  --with opencode --session parser-review --cd "/absolute/project" \
  --PROMPT "Parser tests now pass. Review whether escaped backslashes change the quote state."
```

`--session` takes a short name, not a raw id. The ledger is `/Users/wzn/.ai/skills/council/state/sessions.json` (`COUNCIL_SESSIONS_PATH` overrides it). A successful turn stores `(name → member, absolute cwd, SESSION_ID)`. The next call with the same name, member and cwd injects that id. Several open conversations use different names, different members, or both. If the name is bound to another member or cwd, pick another name or pass `--session-forget NAME` first. `--session-list` prints the ledger as JSON. Neither command runs a member. Raw `--SESSION_ID` still works and does not read or write the ledger unless `--session` is also set. When both are set, the explicit id is used for this turn; after success the returned `SESSION_ID` overwrites the stored id. The ledger stores no prompts or replies.

```bash
/opt/anaconda3/envs/pytorch/bin/python /Users/wzn/.ai/skills/council/scripts/council.py \
  --with claude --SESSION_ID "<claude id>" --cd "/absolute/project" \
  --PROMPT "Parser tests now pass. Review whether escaped backslashes change the quote state."
```

The caller drives additional rounds: send new evidence, relay another member's findings or answer a blocking question. There is no automatic debate loop or persistent caller-to-member message channel. A fresh independent review needs the relevant files and decisions; a same-member follow-up should reuse its session and send the delta.

Several members cannot share `--SESSION_ID` or `--model`. For different prompts, model choices or ownership scopes, launch separate calls concurrently. Avoid overlapping writers and editing files while another member reviews them. Create an isolated checkout only when the task requires it; council does not create one.

## Results and visibility

The final stdout object contains:

```json
{
  "success": true,
  "all_succeeded": true,
  "failed_members": [],
  "results": {
    "codex": {
      "success": true,
      "SESSION_ID": "...",
      "agent_messages": "..."
    }
  },
  "permission": {"mode": "read-only", "allow_write": false, "write_scope": null},
  "concurrency": {"mode": "threadpool-process-fanout", "members": ["codex"], "max_workers": 1}
}
```

`permission` reports requested backend modes; Claude can additionally report its effective mode in the member result. Its `receipt` records the requested model and effort, initialization model when emitted, observed non-synthetic assistant models, and terminal usage/cost metadata when emitted. `effective_effort` remains null unless Claude explicitly reports it. If an explicit standard family alias (`haiku`, `sonnet`, `opus`) disagrees with an observed family, or an explicit full `claude-*` ID disagrees exactly, Claude keeps its answer/session but reports `success: false`; council never retries or substitutes a model. Member fields are conditional: errors may have partial replies and resumable session IDs. Top-level `success` remains `any(member.success)` for compatibility; `all_succeeded` and `failed_members` distinguish full from partial fanout completion. None establishes task correctness. Inspect every selected result and any required checks; retain successful work if another member fails.

CLI completion requires the backend's terminal event and no terminal error, plus a clean process exit. Codex app-server requires `turn.status=completed`; `interrupted` fails. Terminal events are Codex exec `turn.completed`/`turn.failed`, Gemini `result`, Claude `result`, Grok `end`/`error`, Pi `agent_settled`, OpenCode `step_finish` with `part.reason=stop` or `type=error`, and dsh `final` or `type=error`. Pi's earlier `agent_end` can precede retries and must not stop collection. A session ID or partial assistant message alone does not prove completion.

Print-mode CLIs get two seconds to exit naturally after the terminal event. A lingering process is terminated, then killed after a bounded wait if necessary; forced shutdown is reported as failure. On POSIX, cleanup targets the invocation's own process group so a descendant holding stdout cannot keep the fanout alive. This is a shutdown grace period, not a deadline for an active model turn.

By default, only extracted replies, session ids, errors and execution metadata reach the caller. `--return-all-messages` adds `results.<member>.all_messages`: the collected backend JSON events, including tool details/outputs or reasoning fields when the backend emits them. Codex app-server events include responses and notifications. This is not access to hidden reasoning, un-emitted state, a resumed session's full history or a separate dump of all stderr. Event payloads may contain source code and sensitive output; request them for a concrete diagnostic need, not every call.

The caller does not receive live model text through council stdout. Lifecycle and 15-second heartbeats are on stderr; final JSON arrives after all members complete. Growing event counts show activity, not correctness. A zero-event warning after 60 seconds is a diagnostic signal, not proof that the process failed.

## Conditional flags and recovery

- `--model`: one member only; omission keeps its CLI-configured model. dsh rejects `--model` because `dsh --profile headless` has no model flag. Effort flags select reasoning effort, not model identity. With Claude, an omitted `--claude-effort` selects `max` for explicit `haiku` or `claude-haiku-*`, `medium` for omitted/default/Opus selections, and retains `max` for other explicit model choices. An explicit Claude effort passes through unchanged. This is council's CLI-local request default; runtime receipts still determine whether a usable model response was actually emitted. Do not infer the actual model or effective effort from the request, and do not downgrade a requested setting after an error.
- `--image`: Codex only; repeat the flag or comma-separate local paths.
- `--proxy`: override all selected members; `none` means direct. Otherwise `COUNCIL_PROXY` overrides member defaults. Codex/Gemini pin local Surge (`http://127.0.0.1:6152`); other members inherit the environment. Change the route only to resolve a demonstrated connection problem.
- CLI discovery uses PATH and supported CLI authentication/configuration. Never put credentials in the prompt.
- On failure, inspect the member result and council's stderr first. The caller's process handle tracks council, not individual members. Incomplete failures retain a bounded non-JSON diagnostic tail, so argument/authentication errors are visible. Use native diagnostics when events are insufficient, and `--help` for invalid arguments. Do not replay a turn that may have applied changes; inspect the diff and continue its returned session where available.
- A member cannot wait for a live UI choice. For a question, use existing authorization/context to resolve ordinary implementation details; ask the user only when the answer changes scope, correctness or authorization. If unavailable, preserve partial results and report the blocker.
