# Cross-CLI delegation design

Reviewed 2026-10-08. This is a maintenance reference, not extra instructions for every invocation. Installed versions and fetched upstream documentation were checked separately; a supported flag does not prove authentication, model availability or task completion.

## Scope and name

The bridge dispatches independent CLI processes. It does not create native child agents with inherited context, shared tools or a live message channel. One member can own an implementation task; multiple members receiving the same prompt provide independent opinions. The caller owns synthesis, additional rounds and acceptance.

`cli-delegate` is the clearest umbrella name for this capability. `council` describes the multi-member advice workflow but undersells single-member implementation. `cli-subagents` suggests native parent/child semantics that the bridge does not provide; `agent-bridge` is less specific about the task.

Keep `council`, `council.py` and `COUNCIL_PROXY` for compatibility in this revision. A future rename must migrate the skill folder/frontmatter, linked installations, shared prompt references and examples together; retain a compatibility entrypoint if old callers still use it. Do not create duplicate automatically discovered skills merely to advertise an alias.

## Current adapter contract

| CLI checked locally | Invocation and terminal contract | Primary reference |
| --- | --- | --- |
| Codex 0.161.0 | app-server stdio JSON-RPC; `turn/completed` must contain `status=completed`; exec fallback uses JSONL | [App Server](https://developers.openai.com/codex/app-server), locally generated protocol schemas |
| Claude Code 2.1.296 | print + stream-json + verbose; `auto` by default, explicit read-only plan; `result` subtype and error status decide completion | [CLI reference](https://code.claude.com/docs/en/cli-reference), [permission modes](https://code.claude.com/docs/en/permission-modes), [headless](https://code.claude.com/docs/en/headless) |
| Gemini CLI 0.63.0 | prompt + stream-json; default mode plus deny-all/read-search-only policy for read-only, auto_edit for writes; final `result.status` and nested error | [Headless](https://geminicli.com/docs/cli/headless/), [plan mode](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/plan-mode.md), [policy engine](https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/policy-engine.md) |
| Grok Build 1.0.50 | print + streaming-json; auto plus read/search-only tools for read-only, acceptEdits for writes, `--no-plan` for both; `end` or `error`; only `end_turn` is clean completion | [Headless scripting](https://docs.x.ai/build/cli/headless-scripting), [source guide](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md) |
| Pi 1.1.0 | print + JSON; wait for `agent_settled`, inspect assistant stop reason; read-only tools must also exclude MCP | [CLI](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/cli.md), [JSON mode](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/json.md), [RPC event semantics](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md) |
| OpenCode 1.19.4 | `run --format json --dir`; done on `step_finish` `part.reason=stop`; resume `-s`; model `-m provider/model` | local `opencode run --help`; read-only probe removed edit/write/bash when `OPENCODE_CONFIG_CONTENT` denies `edit` and `bash` |
| DeepSeek Harness (dsh) 0.2.1-rc.3 | `--profile headless --json`; done on `final`; resume `--session-id`; no model flag | local `dsh --profile headless --help`; profile sandbox-policy reads `DSH_PERMISSION_MODE` (unset defaults to `danger-full-access`) |

Claude's `auto` is a classifier-backed approval mode, not permission bypass or a filesystem sandbox. Council uses it by default for read-only and authorized scoped writes. Read-only calls restrict Claude to `Read,Glob,Grep,WebFetch,WebSearch`, exclude configured MCP servers with `--strict-mcp-config`, and deny `EnterPlanMode`/`ExitPlanMode`; the injected no-write prompt remains a second constraint. `--permission-prompts none` is the explicit headless policy and requires 2.1.259+. A denied tool may coexist with a successful model turn; preserve permission denials and check the requested artifact or test result. Do not silently downgrade to `acceptEdits`, fall back to `plan`, change a requested model, or enable bypass when auto is unavailable. `plan` is allowed only when explicitly selected for a read-only call.

Pi's `--approve` concerns project resource trust, not permission to edit files. Trust in project settings, extensions and MCP configuration is separate from the caller authorizing a specific change. Planning modes may write their own plan/session files; none of these modes promises that the CLI process itself performs zero filesystem writes.

Gemini's noninteractive default mode treats `ask_user` as deny. The bundled [read-only policy](gemini-read-only.toml) explicitly allows only built-in read/search and web-fetch/search tools and denies all other tools, including MCP, shell, edits, delegation and plan transitions. Pi retries can emit an assistant error followed by a successful assistant response; decide failure from the latest assistant outcome and final `agent_settled.aborted`, while retaining diagnostic events.

Checked 2026-10-31 against local `opencode run --help` (1.19.4) and `dsh --profile headless --help` (0.2.1-rc.3). OpenCode read-only denies `edit` and `bash` through `OPENCODE_CONFIG_CONTENT`; a local probe showed the edit, write and bash tools removed. Writes use `--agent build` without that override. dsh's sandbox-policy defaults to `danger-full-access` when `DSH_PERMISSION_MODE` is unset, and approval is `ask` otherwise, so council forces `read-only` or `workspace-write` over any ambient value. Both inherit the process proxy. OpenCode resolves from `PATH`, then `~/.nvm/versions/node/v24.16.0/bin`. Event completion is in [MANUAL.md](MANUAL.md).

## Alternatives inspected

| Approach | Mechanism worth keeping | Decision for this bridge |
| --- | --- | --- |
| Native structured subprocess | CLI-specific structured events preserve exact model, permissions and resumable session behavior | Keep as the default; update small adapters against local help and upstream event contracts |
| [ACP](https://agentclientprotocol.com/protocol/overview) / [acpx](https://github.com/openclaw/acpx) | Common initialize/session/prompt/update/cancel/permission protocol. acpx [session identity](https://github.com/openclaw/acpx/blob/main/docs/sessions.md) includes agent command, absolute cwd and name; its [JSON output](https://github.com/openclaw/acpx/blob/main/docs/output-formats.md) exposes stop reasons | Strong candidate if additional backends or live steering justify it. acpx describes itself as pre-1.0; adapters and permission policies are not interchangeable with OS isolation. No new dependency now |
| [Codex MCP + Agents SDK](https://developers.openai.com/cookbook/examples/codex/codex_mcp_agents_sdk/building_consistent_workflows_codex_cli_agents_sdk) | Managed stdio server lifecycle, `codex`/`codex-reply`, structured handoffs and tracing | Useful when a model-driven SDK workflow is already required. Adds an orchestration layer/API dependency here without replacing all CLI adapters |
| [Pi RPC](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md) | Correlated commands, steering/follow-up, cancel and a fully settled event after retries | Reuse the settled semantics now; switch transport only if active-turn control is required |
| [multi-model-team dispatcher skill](https://github.com/WatchAndyTW/multi-model-team/blob/main/agents/codex.md) | Call-file input, explicit backend, status sidecar and waiting without duplicate dispatch | An inspected relay pattern, not an endorsed dependency. Consider prompt-file input when long prompts become a demonstrated need; do not adopt fallback/relaunch after possible writes |
| [Claude Squad](https://github.com/smtg-ai/claude-squad) | Persistent tmux sessions with separate branches/worktrees and reviewable diffs | Useful for interactive parallel writers. Keep checkout isolation owned by the caller; no default tmux or automatic worktree creation |

[AgentAPI](https://github.com/coder/agentapi) was also inspected: its current README declares it deprecated/unmaintained, and its terminal-emulator/screen-diff transport is a reason to prefer structured protocols. Do not introduce it as a dependency.

## Design rules retained or added

1. Dispatch a compact task contract: goal, essential state, owned paths, checks and expected output. Each CLI loads its own configuration; parent tools and authority are not inherited.
2. Retain `(member, absolute cwd, SESSION_ID)` and send only the delta on a continuation. Serialize turns for one session. Fresh independent review uses a fresh session.
3. Separate transport completion from acceptance. Require the final backend event, its status and process outcome; retain partial text/session/error on failure. Report fanout partial success explicitly. A heartbeat, a session ID and fluent output do not prove completion.
4. Let print-mode processes exit after their terminal event; bound shutdown and expose a forced termination as failure. Do not cut off Pi at an event that can precede automatic recovery.
5. Once a turn may have executed, transport loss leaves the filesystem outcome unknown. Inspect artifacts and continue the session when possible; never replay automatically. Codex fallback is limited to pre-turn setup.
6. Permissions, allowed tools, project trust and path scope are separate controls. `--write-scope` documents authorized ownership but is not an OS sandbox. Choose isolation when the actual task requires it.
7. Keep native outputs available for diagnostics, but return extracted answers by default. Bound non-JSON startup diagnostics so invalid options and authentication failures are visible. Additional output caps, persistent queues or a prompt-file interface remain optional changes driven by a concrete need.
8. Preserve Claude request and receipt evidence separately. `--claude-effort` is auto-selected only when omitted: `max` for explicit Haiku, `medium` for omitted/default/Opus, and `max` for other explicit model choices; a request does not prove the CLI used that model or effort. Council defaults Claude to `auto`; an explicitly requested read-only `plan` call with Haiku receives an invocation-local `availableModels` allowlist of that exact input to prevent plan-mode Haiku upgrades. No persistent configuration is changed. Non-managed `availableModels` scopes merge and managed configuration can override them, so this is a local guard rather than universal enforcement. Record main-turn init, assistant and result evidence separately from `subagent_models`; only the main turn participates in alias/full-ID comparison and mismatch failure.

### Session identity

Named ledger keys are `(name → member, cwd, SESSION_ID)` in `state/sessions.json`. Council still runs one turn at a time per session.

## Maintenance checks

When an adapter changes, inspect that installed CLI's help/version, check its official terminal/resume/permission contract, and update the relevant adapter tests. Use a temporary workspace for a small real request when claiming current usability; report authentication, provider or quota failures separately from code regressions. A successful mock suite or `--help` alone is not an end-to-end result. Preserve user-selected models and effort; never substitute a successful backend for an unavailable requested one.

The 2026-10-08 temporary-workspace probe verified file reading through Codex and Grok, and exact-session continuation through Codex. Claude's initialization reported the requested `auto` mode, but its session limit blocked the write probe. Gemini waited for browser authentication and was stopped after 208 seconds without a protocol event; Pi failed OAuth refresh. These three backends remain unverified end to end under the current account state. No accounts, dependencies, configured models or authentication settings were changed.

On 2026-10-12, Claude Code 2.1.296 through council with `--model haiku` and omitted effort returned `receipt.requested_effort=max`, `configured_model=claude-haiku-5-5`, and read-only `permission=plan`. A session limit produced `actual_models=[]`, `model_match=null`, `effective_effort=null`, and `success=false`. This validates the prior explicit-plan request/receipt path only; successful Haiku inference remains unverified.
