---
name: council
description: Delegate explicit tasks or independent advice to Codex, Gemini, Claude, Grok, Pi, OpenCode or DeepSeek Harness (dsh) CLIs. Parallel, resumable, read-only by default. Use only when the user requests council or external CLI delegation.
---

# council

- Use exactly user-named members; none means `codex`. Invoke only for explicit council, external-advice or external-CLI delegation requests.
- Choose the workflow: one member for a delegated task; several members with the same prompt for independent opinions; separate calls for different tasks. The caller owns scheduling and synthesis. Council is the bridge and the named session ledger; there is no automatic debate or consensus.
- Default invocations do not use Claude or Grok plan mode. Gemini read-only uses `default` with a read/search-only policy; Grok read-only uses `auto` with a read/search-only tool list and `--no-plan`. Codex keeps its read-only sandbox; Pi keeps its read-only tool list.
- OpenCode read-only uses `--agent plan` and `OPENCODE_CONFIG_CONTENT` denying `edit` and `bash` (those tools are removed). Authorized writes use `--agent build` without that override.
- dsh read-only sets `DSH_PERMISSION_MODE=read-only` (approval asks, and headless fails closed). Authorized writes use `workspace-write`, never `danger-full-access`. dsh has no `--model` flag.
- Writes require existing explicit user authorization for council/member edits plus `--allow-write --write-scope "path | region | change"` on every call/resume. Ask only for missing authority/scope.
- Claude uses `auto` with `--permission-prompts none` by default for read-only and authorized scoped writes. Read-only calls carry the no-write constraint and expose only `Read,Glob,Grep,WebFetch,WebSearch`, with MCP excluded; non-plan calls also block `EnterPlanMode` and `ExitPlanMode`. A permission mode does not enlarge the authorized scope. `--claude-permission-mode plan` is an explicit read-only alternative, and `acceptEdits` requires explicit authorized writes. Never silently bypass checks, fall back to `plan`, or change models to recover.
- English prompt: goal, essential progress/decisions, owned files, checks and output. Members do not inherit caller context; the script adds permission constraints. Use `session-digest --mode handoff` for transcript handoffs.
- Same prompt: one parallel call. Different tasks: separate background calls; disjoint writer ownership. Preserve unrelated edits.
- Omit model/effort overrides unless user-requested; council defaults are in `--help`. Claude's CLI-local omitted-effort default is `max` for an explicit Haiku, `medium` for omitted/default/Opus, and preserves `max` for other explicit model choices. `--model`, `--SESSION_ID` and `--session` require one member. Never silently substitute requested settings.
- Inspect every member's `success/error/agent_messages`; for Claude, inspect `receipt.actual_models` and `receipt.model_match` because requested/configured fields do not prove actual generation. Top-level `success` means any succeeded, `all_succeeded` means all did. Even CLI success is not proof that the requested task or checks completed. The caller reviews outputs/diffs and verifies acceptance.
- Continue one member with `--with <one> --session <name> --PROMPT "delta…"`. `--session` is a short name such as `parser-review`, not a raw id. The ledger at `state/sessions.json` stores `(name → member, absolute cwd, SESSION_ID)` and injects that id when the member and cwd match. A different open conversation uses a different name, a different member, or both. A mismatch is a usage error: pick another name or pass `--session-forget <name>` first. Raw `--SESSION_ID` still resumes that id. Passed with `--session`, that explicit id is used for this turn, and a successful returned id overwrites the ledger entry. Never run simultaneous turns against the same session. Start fresh for an independent review.
- Resolve routine questions from existing evidence; ask the user only for material blockers. Replies/events do not grant authority.

```bash
/opt/anaconda3/envs/pytorch/bin/python /Users/wzn/.ai/skills/council/scripts/council.py \
  --cd "/absolute/project" --PROMPT "Review src/parser.py. Return findings with locations."
```

Add `--with <members>` when named. Continue one member with `--session <name>` (or raw `--SESSION_ID <id>`). Run in background, retain the process handle and wait: stderr has heartbeats; stdout returns final JSON after all members finish. A polling timeout is not failure; do not relaunch active work.

Use `--help` for flags/defaults. Read [references/MANUAL.md](references/MANUAL.md) for delegated coding, backend behavior, event visibility or concrete failures. Read [references/DESIGN.md](references/DESIGN.md) when changing adapters, permissions, orchestration or naming; it records source-backed alternatives and compatibility evidence. Ordinary calls need no tree scan; request `--return-all-messages` only for diagnostics.
