---
name: session-digest
description: >
  Distill Codex / Claude Code / Grok Build / Pi session transcripts into a
  compact, next-agent-ready Markdown brief. Use when the user provides a session
  id/name/path/file, asks to summarize/compact a coding-agent conversation, strip tool
  noise, prepare a handoff from another CLI session, or complains that feeding a
  raw session wastes tokens. Balances dialogue vs tool calls; redacts secret
  values (not bare keywords). Thinking retention default: summary.
  Triggers: session digest, compact session, summarize transcript, handoff from
  codex/claude/grok/pi session, 压缩会话, 去工具噪声, 会话摘要.
---

# session-digest

Turn raw multi-CLI session logs into a **balanced digest** the next agent can
read without drowning in tool payloads — while keeping intent and decision trail.

## When to use

- User points at a Codex / Claude / Grok / Pi **session id, indexed session name, jsonl path, or session directory**
- Cross-CLI handoff: "continue this Codex session in Claude/Grok/Pi" without pasting the whole JSONL
- Context is full of tool dumps; need dialogue + short tool evidence only

**Do not use** for ordinary repo Q&A with no session artifact, or as a substitute for live `resume` when the user wants the same interactive thread.

## Install layout (portable)

```text
session-digest/
  SKILL.md
  scripts/session_digest.py
```

Roots (override with env for other machines):

| Env | Default |
|-----|---------|
| `CODEX_HOME` | `~/.codex` |
| `CLAUDE_HOME` | `~/.claude` |
| `GROK_HOME` | `~/.grok` |
| `PI_HOME` | `~/.pi` |

## CLI

Prefer an absolute interpreter on machines where bare `python` is unreliable
(example: `/opt/anaconda3/envs/pytorch/bin/python`). Resolve the skill root from
this `SKILL.md` path (not interactive-shell `$0`):

```bash
# Prefer an absolute path to the skill (portable across shells)
SD="$HOME/.cc-switch/skills/session-digest"   # or wherever the skill is installed
# On Windows PowerShell: $SD = "$env:USERPROFILE\.cc-switch\skills\session-digest"
PY="${PYTHON:-python3}"

$PY "$SD/scripts/session_digest.py" --list-recent 15
$PY "$SD/scripts/session_digest.py" --find 019f9220
$PY "$SD/scripts/session_digest.py" --cli codex --name '梳理 PhysRoute 当前进度 (3)' --mode handoff
$PY "$SD/scripts/session_digest.py" 'Learn about the LeetCode With Labuladong' --format claude --mode balanced
$PY "$SD/scripts/session_digest.py" /path/to/rollout-....jsonl --mode balanced
$PY "$SD/scripts/session_digest.py" 019f8df7 --mode handoff -o digest.md
$PY "$SD/scripts/session_digest.py" 019f8df7 --mode handoff --handoff-full -o full.md
$PY "$SD/scripts/session_digest.py" ~/.grok/sessions/<cwd>/<id> --mode balanced
```

Exit codes: `0` success · `1` not found / parse failure · `2` bad usage.

### Modes

| Mode | Behavior |
|------|----------|
| `balanced` (default) | User/assistant text (clipped) + tool name/args + short result |
| `dialogue` | Messages only (no tools); thinking still follows `--thinking` |
| `tools` | Messages + longer tool results (3× budget) + higher tool cap |
| `handoff` | **Compact** structured brief only (goal / last decisions / key tools / next). Does **not** append the full transcript unless `--handoff-full` |

### Thinking retention (`--thinking`)

Dual goals: **compress** and **preserve decision trail**. Default is `summary`.

| Tier | Behavior |
|------|----------|
| `summary` (**default**) | Heuristic decision extract (plan/choose/because/next…); else head+tail. Budget: `--max-thinking-chars` (default 1000) |
| `keep` | Clipped full thinking (budget ≈ 3× `--max-thinking-chars`) |
| `drop` | Omit all thinking/reasoning |

Sources when present on disk:

- **Codex**: `response_item.type=reasoning` → `summary[].summary_text` (full body often encrypted-only → empty)
- **Claude**: `content[].type=thinking` → `thinking` field
- **Grok**: top-level `type=reasoning` → `summary[].summary_text`
- **Pi**: thinking blocks + standalone `role=toolResult` messages

Encrypted-only blobs cannot be recovered.

### Useful flags

- `--cli codex|claude|grok|pi` limits lookup and parsing to one CLI store
- `--name TITLE` performs exact title lookup and requires `--cli`; Codex, Claude, and Grok expose authoritative on-disk titles (Pi currently does not)
- `--format auto|codex|claude|grok|pi` forces parsing and remains compatible with positional path/id/title lookup
- `--thinking summary|keep|drop` (default `summary`)
- `--handoff-full` (only with `--mode handoff`)
- `--max-message-chars 3500`
- `--max-tool-result-chars 350` (honored at **render**; parse stores up to ~12k)
- `--max-thinking-chars 1000`
- `--max-tools-per-turn 12` (applies to **orphan** tool bursts too)
- `--json-stats` (stderr)
- `-o path` (default stdout, UTF-8)

## Agent workflow

1. Resolve the artifact: path, session directory, id fragment, or indexed title. For title lookup, prefer `--cli <source> --name '<full title>'`. Matching is exact after case folding; keep suffixes such as `(2)` or `(3)`. Duplicate current titles fail with logical session ids and candidate paths instead of selecting silently. Positional substring title lookup also fails and asks for the full title. Codex forks automatically include available ancestor history only up to each fork timestamp; later parent and sibling turns are excluded.
2. Run `session_digest.py` in **balanced** or **handoff** (not raw `cat` of jsonl).
3. Feed the Markdown digest to the next agent. Keep the original path as optional provenance.
4. If still huge: lower `--max-message-chars` / `--max-tool-result-chars` / `--max-tools-per-turn`, or use `--mode handoff` (compact). Use `--handoff-full` only when you explicitly need the full balanced body.

## Format notes (real layouts)

- **Codex**: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` — `response_item` / `event_msg`; tools as `function_call` + `function_call_output`. Current names come from the latest record per logical thread id in `~/.codex/session_index.jsonl`; historical rename rows are ignored. Name resolution selects the newest primary fragment, then parsing combines available fragments for that logical thread. Forks follow `session_meta.forked_from_id` recursively and apply the child rollout timestamp as an exclusive parent cutoff, so parent activity after the fork is never included. A deleted or missing ancestor degrades to a partial lineage instead of failing. Inter-agent `agent_message` → meta (not main assistant).
- **Claude Code**: `~/.claude/projects/<slug>/<sessionUuid>.jsonl` — `tool_use` / `tool_result`; session names come from the latest `custom-title.customTitle` record; subagent files under `subagents/` are excluded from discovery.
- **Grok Build**: `~/.grok/sessions/<urlenc-cwd>/<sessionId>/chat_history.jsonl` (prefer over `events.jsonl`). Names come from `~/.grok/sessions/session_search.sqlite`; trailing stored `<|eos|>` markers are normalized before exact matching. CWD is URL-decoded from the parent path.
- **Pi**: `~/.pi/agent/sessions/<cwd-slug>/*_<id>.jsonl` — `session` + `message` records; tool outcomes are `role: toolResult` with `toolCallId`.

## Balancing policy

Two constraints:

1. **Compress** tool noise and system walls so the next agent fits context.
2. **Retain** intent, outcomes, and **decision reasoning** so the next agent does not re-litigate abandoned paths blindly.

Concrete rules:

- Keep user asks and assistant conclusions.
- Keep thinking at `--thinking summary` by default (heuristic, not an LLM re-summary).
- Compress tools to: **name + key args + short result** (capped per turn, including orphan bursts).
- Drop/skip: developer/system walls, encrypted-only reasoning, full file dumps, MCP side channels (`events.jsonl`, etc.).
- Redact **secret values** (`token=…`, `sk-…`, `ghp_…`, JWT, …) without mangling benign words like `tokenizer` / `max_tokens`.

## Output contract

Markdown with:

1. Header metadata (format, source, session_id, cwd, counts, thinking tier, and Codex lineage status when applicable)
2. For `balanced`/`tools`/`dialogue`: turn-wise **User** / **Thinking** / **Assistant** / **Tools**
3. For `handoff`: Goal · Decisions (last N) · Thinking (last N) · Key tools (last N) · Open/next

Suitable as the sole context blob for a fresh CLI session or council member.

## Examples

```bash
# Recent Codex work → compact handoff for another agent
python scripts/session_digest.py --find rollout-2026-07-24 --mode handoff -o /tmp/codex-handoff.md

# Exact Codex task name; suffix is part of identity, so sibling forks are not selected
python scripts/session_digest.py --cli codex --name '梳理 PhysRoute 当前进度 (3)' --mode handoff

# This Grok session directory
python scripts/session_digest.py "$HOME/.grok/sessions/%2FUsers%2Fwzn/<id>" --mode balanced

# Claude project session by uuid fragment
python scripts/session_digest.py daafa25b --mode balanced --max-tool-result-chars 200

# Claude project session by the same custom title accepted by `claude --resume`
python scripts/session_digest.py 'Learn about the LeetCode With Labuladong' --format claude --mode balanced

# Max compression (no thinking)
python scripts/session_digest.py 019f8df7 --thinking drop --mode handoff

# Audit: keep more of the decision trail + full body
python scripts/session_digest.py 019f8df7 --thinking keep --mode handoff --handoff-full --max-thinking-chars 2000
```
