# cairn

A trail marker for coding agents: let a main agent route work to other local coding-agent CLIs (Codex, OpenCode, Pi, Gemini, Claude Code, Grok, DeepSeek Harness…), run them as background jobs, continue named sessions, read and digest their histories, and hand tasks off between sessions.

The main agent owns scheduling and synthesis; cairn is the bridge and the ledger, not a debate engine.

## Status

Design stage. See [docs/DESIGN.md](docs/DESIGN.md) (Chinese).

`legacy/` holds the two tools cairn will replace, as they work today:

- `legacy/council/`: Claude Code skill + `scripts/council.py`, cross-CLI discussion/review with read-only defaults and a named session ledger.
- `legacy/session-digest/`: deterministic transcript digester for Codex / Claude Code / Grok / Pi sessions.

## Planned shape

- `cairn` CLI (installable with `uv tool install`), every subcommand with `--json`
- `core/`: jobs, sessions, handoff, permissions, transcript/digest
- `adapters/<cli>.py`: thin per-CLI adapters (headless command, resume args, session-id parsing, handoff delivery, optional history parsing)
- `/cairn` skill as a thin wrapper
