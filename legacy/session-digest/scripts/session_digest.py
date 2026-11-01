#!/usr/bin/env python3
"""
session-digest — distill coding-agent session transcripts for the next agent.

Supports Codex / Claude Code / Grok Build / Pi real on-disk formats.
Balances dialogue vs tool noise; redacts secrets; portable across machines
via HOME and optional CODEX_HOME / CLAUDE_HOME / GROK_HOME / PI_HOME.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

# Value-shaped secrets (matched as whole tokens — never bare keywords like "token").
_SECRET_VALUE_RES = [
    re.compile(r"(?i)\bsk-[a-z0-9_\-]{10,}\b"),
    re.compile(r"(?i)\bxai-[a-z0-9_\-]{10,}\b"),
    re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),  # GitHub
    re.compile(r"(?i)\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),  # Slack
    re.compile(r"(?i)\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"(?i)\bAIza[0-9A-Za-z_\-]{20,}\b"),  # Google API
    re.compile(
        r"\beyJ[a-zA-Z0-9_\-]{20,}\.[a-zA-Z0-9_\-]{10,}\.[a-zA-Z0-9_\-]{10,}\b"
    ),  # JWT
]
# key=value / "key": "value" — run BEFORE bare value patterns only.
# Note: "authorization" / "bearer" are handled by _SECRET_BEARER_RE so simple
# assign does not turn "Authorization: Bearer <tok>" into double redaction.
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|token|secret|password|passwd)\b"
    r"\s*[=:]\s*)([^\s,;\"']+)"
)
_SECRET_JSON_RE = re.compile(
    r'(?i)("(?:api[_-]?key|token|secret|password|passwd|authorization|bearer)"\s*:\s*)'
    r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\s,}\]]+)'
)
_SECRET_BEARER_RE = re.compile(
    r"(?i)(\b(?:authorization\s*[=:]\s*)?bearer\s+)([^\s,;\"']+)"
)
_SECRET_AUTH_HEADER_RE = re.compile(
    r"(?i)(\bauthorization\s*[=:]\s*)(?!bearer\b)([^\s,;\"']+)"
)

HOME = Path.home()

# Parse-time tool result budget (render may clip further via --max-tool-result-chars).
TOOL_RESULT_STORE_CHARS = 12000


def _env_path(name: str, *parts: str) -> Path:
    raw = os.environ.get(name)
    if raw:
        return Path(raw).expanduser()
    return HOME.joinpath(*parts)


CODEX_HOME = _env_path("CODEX_HOME", ".codex")
CLAUDE_HOME = _env_path("CLAUDE_HOME", ".claude")
GROK_HOME = _env_path("GROK_HOME", ".grok")
PI_HOME = _env_path("PI_HOME", ".pi")


# --------------------------------------------------------------------------- utils


def _norm_path_str(p: Path | str) -> str:
    return str(p).replace("\\", "/")


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.expanduser().resolve().relative_to(root.expanduser().resolve())
        return True
    except (ValueError, OSError):
        return False


def _parse_rfc3339(value: str) -> Optional[datetime]:
    """Normalize Codex RFC3339 timestamps for strict chronological cutoffs."""
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def redact(text: str) -> str:
    """Redact secret *values* without mangling benign words like tokenizer/max_tokens."""
    if not text:
        return text
    # 1) assignment / JSON first (preserve key names, hide values)
    text = _SECRET_BEARER_RE.sub(r"\1[REDACTED]", text)
    text = _SECRET_AUTH_HEADER_RE.sub(r"\1[REDACTED]", text)
    text = _SECRET_ASSIGN_RE.sub(r"\1[REDACTED]", text)
    text = _SECRET_JSON_RE.sub(r"\1[REDACTED]", text)
    # 2) known value shapes
    for rx in _SECRET_VALUE_RES:
        text = rx.sub("[REDACTED]", text)
    return text


# System / framework wrappers that are not the user's real ask.
_INJECTION_BLOCK_RES = [
    re.compile(r"<recommended_plugins>[\s\S]*?</recommended_plugins>", re.I),
    re.compile(r"<environment_context>[\s\S]*?</environment_context>", re.I),
    re.compile(r"<user_info>[\s\S]*?</user_info>", re.I),
    re.compile(r"<system-reminder>[\s\S]*?</system-reminder>", re.I),
    re.compile(r"<agent_skills>[\s\S]*?</agent_skills>", re.I),
    re.compile(r"<available_skills>[\s\S]*?</available_skills>", re.I),
    re.compile(
        r"#\s*AGENTS\.md\s+instructions\s*\n+\s*<INSTRUCTIONS>[\s\S]*?</INSTRUCTIONS>",
        re.I,
    ),
    re.compile(r"<INSTRUCTIONS>[\s\S]*?</INSTRUCTIONS>", re.I),
]


def strip_injection(text: str) -> str:
    """Remove CLI framework wrappers; keep the real user/assistant content."""
    if not text:
        return ""
    # Grok / some agents wrap the real ask
    m = re.search(r"<user_query>\s*([\s\S]*?)\s*</user_query>", text, re.I)
    if m and m.group(1).strip():
        return m.group(1).strip()
    out = text
    for rx in _INJECTION_BLOCK_RES:
        out = rx.sub("", out)
    # Drop leading "From: path/AGENTS.md" style system dumps that remain after partial strip
    out = re.sub(
        r"(?m)^## From: .*(?:\n(?!## |# |$).*)*",
        "",
        out,
    )
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def is_noise_user_text(text: str) -> bool:
    """True when a 'user' row is only framework injection / empty after strip."""
    cleaned = strip_injection(text)
    if not cleaned:
        return True
    # very short residual after stripping large system dumps
    if len(text) > 800 and len(cleaned) < 40:
        return True
    noise_starts = (
        "<recommended_plugins>",
        "<environment_context>",
        "<user_info>",
        "<system-reminder>",
        "# AGENTS.md instructions",
        "## Available Skills",
        "This session is being continued from a previous conversation",
    )
    head = cleaned.lstrip()[:80]
    # Keep continuation summaries — they carry task state — but flag pure skill dumps
    if cleaned.lstrip().startswith("## Available Skills"):
        return True
    if any(text.lstrip().startswith(s) for s in noise_starts[:6]) and len(cleaned) < 60:
        return True
    return False


def clip(text: str, limit: int) -> str:
    text = text.replace("\r\n", "\n")
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 20)].rstrip() + "\n…[truncated]…"


def as_dict(obj: Any) -> Optional[dict]:
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, str):
        s = obj.strip()
        if not s:
            return None
        try:
            v = json.loads(s)
            return v if isinstance(v, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def text_from_content(content: Any, *, include_thinking: bool = False) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype in ("text", "input_text", "output_text", "summary_text"):
                    parts.append(str(block.get("text") or block.get("content") or ""))
                elif btype in ("thinking", "redacted_thinking"):
                    if include_thinking:
                        parts.append(
                            str(block.get("thinking") or block.get("text") or "")
                        )
                elif "text" in block and btype not in ("tool_use", "tool_result", "functionCall"):
                    parts.append(str(block.get("text") or ""))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        return text_from_content(
            content.get("text") or content.get("content") or content.get("thinking"),
            include_thinking=include_thinking,
        )
    return str(content)


def text_from_thinking_payload(payload: Any) -> str:
    """Extract readable reasoning from Codex/Claude/Grok thinking/reasoning shapes.

    Prefers plaintext summary_text / thinking fields. Encrypted-only blobs yield empty.
    """
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, list):
        return text_from_content(payload, include_thinking=True).strip()
    if not isinstance(payload, dict):
        return str(payload).strip()

    # Common field order: explicit summary → thinking body → content blocks
    for key in ("summary", "thinking", "text", "content", "reasoning"):
        if key not in payload or payload[key] in (None, "", []):
            continue
        got = text_from_thinking_payload(payload[key])
        if got:
            return got
    return ""


_DECISION_LINE_RE = re.compile(
    r"(?im)^(?:#{1,6}\s*)?(?:"
    r"(?:decision|decided|conclusion|therefore|so\s+i\s+(?:will|am|should)|"
    r"i(?:'ll| will| should| need to| plan to| chose| choose| instead)|"
    r"plan|next(?:\s+step)?s?|because|risk|trade-?off|instead of)\b|"
    r"(?:选择|不选|结论|下一步|因为|所以|计划|决定)"  # CJK: no word-boundary after
    r").*$"
)
_BULLET_LINE_RE = re.compile(r"(?m)^(?:[-*]|\d+[.)])\s+\S.*$")


def summarize_thinking(text: str, mode: str, limit: int) -> str:
    """Compress reasoning while keeping decision trail when mode=summary.

    Modes:
      drop    → empty
      summary → decision-oriented lines, else head+tail (default)
      keep    → clipped full text (limit usually larger)
    """
    if mode == "drop" or not text:
        return ""
    text = redact(text.replace("\r\n", "\n").strip())
    if not text:
        return ""
    if mode == "keep":
        return clip(text, limit if limit > 0 else len(text))

    # summary mode
    if limit <= 0:
        limit = 1000
    if len(text) <= limit:
        return text

    hits: List[str] = []
    seen = set()
    for rx in (_DECISION_LINE_RE, _BULLET_LINE_RE):
        for m in rx.finditer(text):
            line = " ".join(m.group(0).split())
            if len(line) < 8:
                continue
            key = line[:160]
            if key in seen:
                continue
            seen.add(key)
            hits.append(line)
    if hits:
        joined = "\n".join(hits)
        # Only trust extraction if it captured enough signal
        if len(joined) >= min(120, limit // 4):
            return clip(joined, limit)

    # Fallback: head (setup) + tail (final decision) — better than pure head for CoT
    head_n = max(200, (limit * 2) // 3)
    tail_n = max(120, limit - head_n - 40)
    if head_n + tail_n + 40 >= len(text):
        return clip(text, limit)
    return (
        text[:head_n].rstrip()
        + "\n…[thinking middle omitted]…\n"
        + text[-tail_n:].lstrip()
    )


def summarize_tool_args(args: Any, limit: int = 180) -> str:
    if args is None:
        return ""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return clip(redact(args), limit)
    if isinstance(args, dict):
        # Prefer path-like / command-like fields
        prefer = (
            "file_path", "path", "target_file", "command", "pattern",
            "query", "glob", "cwd", "old_string", "new_string", "url",
        )
        bits = []
        for k in prefer:
            if k in args and args[k] not in (None, ""):
                bits.append(f"{k}={clip(redact(str(args[k])), 80)}")
        if not bits:
            s = json.dumps(args, ensure_ascii=False)
            return clip(redact(s), limit)
        return clip("; ".join(bits), limit)
    return clip(redact(str(args)), limit)


def extract_tool_output(out: Any) -> Tuple[str, Optional[bool]]:
    """Normalize tool output text and optional success flag.

    Handles Codex JSON-string outputs, Wall-time wrappers, exit_code metadata,
    list-shaped Claude tool_result contents, and plain strings.
    """
    ok: Optional[bool] = None
    if out is None:
        return "", None

    # JSON string → dict
    if isinstance(out, str):
        parsed = as_dict(out)
        if parsed is not None:
            out = parsed
        else:
            text = out
            m = re.search(r"(?is)\bOutput:\s*(.*)$", text)
            if m and m.group(1).strip():
                text = m.group(1).strip()
            low = text.lstrip().lower()
            if low.startswith("exit: 1") or low.startswith("exit code: 1"):
                ok = False
            elif low.startswith("exit: 0") or low.startswith("exit code: 0"):
                ok = True
            return text, ok

    if isinstance(out, dict):
        meta = out.get("metadata") if isinstance(out.get("metadata"), dict) else {}
        if "exit_code" in meta:
            try:
                ok = int(meta["exit_code"]) == 0
            except (TypeError, ValueError):
                pass
        elif "exit_code" in out:
            try:
                ok = int(out["exit_code"]) == 0
            except (TypeError, ValueError):
                pass
        if out.get("is_error") is True or out.get("isError") is True:
            ok = False
        text_val = out.get("output")
        if text_val is None:
            text_val = out.get("content")
        if text_val is None:
            text_val = out.get("result")
        if text_val is None:
            try:
                return json.dumps(out, ensure_ascii=False), ok
            except TypeError:
                return str(out), ok
        if isinstance(text_val, (list, dict)):
            return text_from_content(text_val), ok
        return str(text_val), ok

    if isinstance(out, list):
        return text_from_content(out), ok

    return str(out), ok


def store_tool_result(result: Any, limit: int = TOOL_RESULT_STORE_CHARS) -> Tuple[str, Optional[bool]]:
    """Store a longer tool result at parse time; render clips further."""
    text, ok = extract_tool_output(result)
    text = redact(text.replace("\r\n", "\n"))
    if not text:
        return "", ok
    # Heuristic ok if still unknown
    if ok is None:
        low = text.lstrip().lower()
        if low.startswith("exit: 1") or low.startswith("exit code: 1"):
            ok = False
        elif low.startswith("exit: 0") or low.startswith("exit code: 0"):
            ok = True
    return clip(text, limit), ok


def summarize_tool_result(result: Any, limit: int) -> str:
    """Legacy short summary helper (display-oriented)."""
    text, _ = store_tool_result(result, limit=max(limit, 1))
    if not text:
        return ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    if limit <= 500:
        max_lines = 6
    else:
        max_lines = max(6, min(200, limit // 40))
    head = lines[0]
    if len(lines) == 1:
        return clip(head, limit)
    body = "\n".join(lines[1:max_lines])
    return clip(head + "\n" + body, limit)


@dataclass
class Event:
    kind: str  # user | assistant | tool | thinking | meta | system
    text: str = ""
    tool_name: str = ""
    tool_args: str = ""
    tool_result: str = ""
    ok: Optional[bool] = None
    ts: str = ""
    raw_type: str = ""
    call_id: str = ""


@dataclass
class Transcript:
    source: str
    format: str
    session_id: str = ""
    cwd: str = ""
    events: List[Event] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionMatch:
    path: Path
    kind: str  # title-exact | title-substring | path
    title: str = ""
    format: str = ""
    session_id: str = ""


@dataclass(frozen=True)
class CodexSessionMeta:
    session_id: str = ""
    forked_from_id: str = ""
    created_at: str = ""


# --------------------------------------------------------------------------- detect / find


def detect_format(path: Path) -> str:
    path = path.expanduser().resolve()
    if path.is_dir():
        if (path / "chat_history.jsonl").exists() or (path / "events.jsonl").exists():
            return "grok"
        # scan first jsonl
        for p in sorted(path.rglob("*.jsonl"))[:5]:
            f = detect_format(p)
            if f != "unknown":
                return f
        return "unknown"
    name = path.name
    if name.startswith("rollout-") or "codex" in str(path).lower():
        # still sniff
        pass
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(30):
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(o, dict):
                    continue
                t = o.get("type")
                if t in ("response_item", "event_msg", "session_meta", "turn_context"):
                    return "codex"
                if t in ("user", "assistant") and ("sessionId" in o or "message" in o) and "cwd" in o:
                    return "claude"
                if t in ("user", "assistant", "tool_result", "reasoning", "system") and (
                    "tool_calls" in o or "tool_call_id" in o or t == "reasoning"
                ):
                    # grok chat_history
                    return "grok"
                if t in ("session", "message", "model_change", "agent_start", "turn_start"):
                    # pi runtime stream or stored session
                    if "version" in o and t == "session":
                        return "pi"
                    if t == "message" and "message" in o:
                        return "pi"
                    if t in ("agent_start", "turn_start", "message_start"):
                        return "pi"
                if t in ("phase_changed", "tool_started", "turn_started"):
                    return "grok-events"
    except OSError:
        return "unknown"
    # path heuristics
    s = _norm_path_str(path)
    if ".codex/sessions" in s or name.startswith("rollout-"):
        return "codex"
    if ".claude/projects" in s or "/claude/projects/" in s:
        return "claude"
    if ".grok/sessions" in s or "/grok/sessions/" in s:
        return "grok"
    if "/.pi/" in s or "/pi/agent/sessions" in s:
        return "pi"
    return "unknown"


def iter_candidate_roots() -> List[Tuple[str, Path]]:
    return [
        ("codex", CODEX_HOME / "sessions"),
        ("claude", CLAUDE_HOME / "projects"),
        ("grok", GROK_HOME / "sessions"),
        ("pi", PI_HOME / "agent" / "sessions"),
    ]


def _claude_session_title(path: Path) -> str:
    """Return the latest Claude custom title without scanning a large JSONL forward."""
    marker = b'"customTitle"'
    block_size = 64 * 1024
    carry = b""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            while pos > 0:
                size = min(block_size, pos)
                pos -= size
                f.seek(pos)
                parts = (f.read(size) + carry).splitlines()
                if pos > 0 and parts:
                    carry = parts.pop(0)
                else:
                    carry = b""
                for raw in reversed(parts):
                    if marker not in raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    if record.get("type") != "custom-title":
                        continue
                    title = record.get("customTitle")
                    return title.strip() if isinstance(title, str) else ""
    except OSError:
        return ""
    return ""


_UUID_RE = re.compile(
    r"(?i)(?<![0-9a-f])"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?![0-9a-f])"
)


def _codex_session_titles() -> Dict[str, str]:
    """Return the latest indexed title for each logical Codex thread."""
    index = CODEX_HOME / "session_index.jsonl"
    latest: Dict[str, Tuple[str, int, str]] = {}
    try:
        with index.open("r", encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                session_id = str(record.get("id") or "").strip()
                title = str(record.get("thread_name") or "").strip()
                if not session_id or not title:
                    continue
                rank = (str(record.get("updated_at") or ""), line_no, title)
                if session_id not in latest or rank[:2] >= latest[session_id][:2]:
                    latest[session_id] = rank
    except OSError:
        return {}
    return {session_id: item[2] for session_id, item in latest.items()}


def _codex_session_meta(path: Path) -> CodexSessionMeta:
    """Read logical-thread and fork metadata from a Codex rollout."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(30):
                line = f.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if isinstance(payload, dict):
                    return CodexSessionMeta(
                        session_id=str(
                            payload.get("session_id") or payload.get("id") or ""
                        ),
                        forked_from_id=str(payload.get("forked_from_id") or ""),
                        created_at=str(
                            payload.get("timestamp") or record.get("timestamp") or ""
                        ),
                    )
    except OSError:
        pass
    return CodexSessionMeta()


def _codex_session_id(path: Path) -> str:
    """Read the logical thread id; fall back to the first UUID in the filename."""
    session_id = _codex_session_meta(path).session_id
    if session_id:
        return session_id
    match = _UUID_RE.search(path.name)
    return match.group(1) if match else ""


def _codex_session_paths(session_id: str, anchor: Path) -> List[Path]:
    """Find available rollout fragments for one logical Codex thread."""
    if not session_id:
        return []
    candidates: Dict[str, Path] = {}
    if anchor.is_file() and _codex_session_id(anchor) == session_id:
        candidates[str(anchor.resolve())] = anchor

    roots = [CODEX_HOME / "sessions"]
    codex_root = roots[0]
    if not _is_under(anchor, codex_root):
        roots.append(anchor.parent)
    for root in roots:
        if not root.exists():
            continue
        try:
            paths = root.rglob(f"*{session_id}*.jsonl")
            for path in paths:
                if not path.is_file() or not _is_primary_session_file(path):
                    continue
                if _codex_session_id(path) != session_id:
                    continue
                candidates[str(path.resolve())] = path
        except OSError:
            continue

    def rank(path: Path) -> Tuple[datetime, str]:
        created_at = _parse_rfc3339(_codex_session_meta(path).created_at)
        return (created_at or datetime.max.replace(tzinfo=timezone.utc), str(path))

    return sorted(candidates.values(), key=rank)


def _grok_session_titles() -> Dict[str, str]:
    """Return indexed Grok titles keyed by session id."""
    index = GROK_HOME / "sessions" / "session_search.sqlite"
    if not index.exists():
        return {}
    base_uri = index.resolve().as_uri()
    for options in ("?mode=ro", "?mode=ro&immutable=1"):
        try:
            with sqlite3.connect(base_uri + options, uri=True) as db:
                rows = db.execute(
                    "SELECT session_id, title FROM session_docs WHERE title <> ''"
                )
                return {
                    str(session_id): re.sub(
                        r"(?:<\|eos\|>\s*)+$", "", str(title)
                    ).strip()
                    for session_id, title in rows
                    if session_id and title
                }
        except sqlite3.Error:
            continue
    return {}


def find_session_matches(
    query: str, limit: int = 20, format_hint: str = "auto"
) -> List[SessionMatch]:
    """Locate primary transcripts by path/id fragment or current session title."""
    q = query.strip().casefold()
    if not q:
        return []
    hits: List[Tuple[SessionMatch, int, float]] = []
    codex_titles = (
        _codex_session_titles() if format_hint in ("auto", "codex") else {}
    )
    grok_titles = (
        _grok_session_titles() if format_hint in ("auto", "grok") else {}
    )
    for fmt, root in iter_candidate_roots():
        if format_hint != "auto" and fmt != format_hint:
            continue
        if not root.exists():
            continue
        for p in root.rglob("*.jsonl"):
            if not p.is_file():
                continue
            if not _is_primary_session_file(p):
                continue
            path_text = str(p).casefold()
            path_match = (
                q in path_text
                or q in p.name.casefold()
                or q in p.parent.name.casefold()
            )
            session_id = ""
            if fmt == "codex":
                session_id = _codex_session_id(p)
                title = codex_titles.get(session_id, "")
            elif fmt == "claude":
                session_id = p.stem
                title = _claude_session_title(p)
            elif fmt == "grok":
                session_id = p.parent.name
                title = grok_titles.get(session_id, "")
            else:
                title = ""
            folded_title = title.casefold()
            if title and folded_title == q:
                match = SessionMatch(p, "title-exact", title, fmt, session_id)
                match_rank = 3
            elif path_match:
                match = SessionMatch(p, "path", format=fmt, session_id=session_id)
                match_rank = 2
            elif title and q in folded_title:
                match = SessionMatch(p, "title-substring", title, fmt, session_id)
                match_rank = 1
            else:
                continue
            primary_rank = (
                1
                if p.name.startswith("rollout-") or p.name == "chat_history.jsonl"
                else 0
            )
            try:
                mtime = p.stat().st_mtime
            except OSError:
                mtime = 0.0
            hits.append((match, match_rank * 10 + primary_rank, mtime))

    uniq: Dict[str, Tuple[SessionMatch, int, float]] = {}
    for hit in hits:
        key = str(hit[0].path)
        if key not in uniq or hit[1:] > uniq[key][1:]:
            uniq[key] = hit
    ranked = list(uniq.values())
    ranked.sort(key=lambda item: (item[1], item[2]), reverse=True)
    results: List[SessionMatch] = []
    seen_logical_titles = set()
    for match, _, _ in ranked:
        if match.kind.startswith("title") and match.session_id:
            logical_key = (match.format, match.session_id, match.kind)
            if logical_key in seen_logical_titles:
                continue
            seen_logical_titles.add(logical_key)
        results.append(match)
        if len(results) >= limit:
            break
    return results


def find_sessions(
    query: str, limit: int = 20, format_hint: str = "auto"
) -> List[Path]:
    """Backward-compatible path-only session lookup."""
    return [m.path for m in find_session_matches(query, limit, format_hint)]


# --------------------------------------------------------------------------- parsers


def _parse_codex_file(path: Path, before_ts: str = "") -> Transcript:
    tr = Transcript(source=str(path), format="codex")
    cutoff = _parse_rfc3339(before_ts) if before_ts else None
    pending: Dict[str, Event] = {}
    type_counts: Counter = Counter()
    # Prefer clean event_msg user lines when present; fall back to response_item users.
    event_users: List[Event] = []
    response_users: List[Event] = []
    other_events: List[Event] = []

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = o.get("payload")
            if isinstance(payload, str):
                payload = as_dict(payload) or {}
            if not isinstance(payload, dict):
                continue
            ts = str(o.get("timestamp") or "")
            ot = o.get("type")
            if ot == "session_meta":
                tr.session_id = str(
                    payload.get("session_id") or payload.get("id") or tr.session_id
                )
                continue
            if before_ts:
                event_time = _parse_rfc3339(ts)
                if cutoff is None or event_time is None or event_time >= cutoff:
                    continue
            if ot == "turn_context":
                tr.cwd = str(payload.get("cwd") or tr.cwd)
                continue
            if ot == "event_msg":
                et = payload.get("type")
                if et == "user_message":
                    text = strip_injection(
                        text_from_content(payload.get("message") or payload.get("content"))
                    )
                    if text and not is_noise_user_text(text):
                        event_users.append(
                            Event(
                                "user",
                                text=text,
                                ts=ts,
                                raw_type=et,
                                call_id=str(
                                    payload.get("turn_id") or payload.get("id") or ""
                                ),
                            )
                        )
                # skip agent_message here — response_item assistant is more complete
                continue
            if ot != "response_item":
                continue
            pt = payload.get("type")
            item_id = str(payload.get("id") or "")
            type_counts[str(pt)] += 1
            if pt == "message":
                role = payload.get("role")
                text = text_from_content(payload.get("content"))
                if not text:
                    continue
                if role == "user":
                    cleaned = strip_injection(text)
                    if cleaned and not is_noise_user_text(text):
                        metadata = payload.get("internal_chat_message_metadata_passthrough")
                        if not isinstance(metadata, dict):
                            metadata = {}
                        response_users.append(
                            Event(
                                "user",
                                text=cleaned,
                                ts=ts,
                                raw_type=pt,
                                call_id=str(metadata.get("turn_id") or item_id),
                            )
                        )
                elif role == "assistant":
                    cleaned = strip_injection(text)
                    if cleaned:
                        other_events.append(
                            Event(
                                "assistant",
                                text=cleaned,
                                ts=ts,
                                raw_type=pt,
                                call_id=item_id,
                            )
                        )
                elif role == "developer":
                    continue
            elif pt in ("function_call", "custom_tool_call"):
                name = payload.get("name") or payload.get("tool_name") or "tool"
                ns = payload.get("namespace") or ""
                full = f"{ns}.{name}" if ns else str(name)
                call_id = str(payload.get("call_id") or payload.get("id") or "")
                ev = Event(
                    "tool",
                    tool_name=full,
                    tool_args=summarize_tool_args(payload.get("arguments")),
                    ts=ts,
                    raw_type=pt,
                    call_id=call_id,
                )
                if call_id:
                    pending[call_id] = ev
                other_events.append(ev)
            elif pt in ("function_call_output", "custom_tool_call_output"):
                call_id = str(payload.get("call_id") or "")
                out = payload.get("output")
                summary, ok = store_tool_result(out, TOOL_RESULT_STORE_CHARS)
                # Prefer structured exit_code when present on dict/string-json
                if ok is None and isinstance(out, dict):
                    meta = out.get("metadata") if isinstance(out.get("metadata"), dict) else {}
                    if "exit_code" in meta:
                        try:
                            ok = int(meta["exit_code"]) == 0
                        except (TypeError, ValueError):
                            pass
                ev = pending.get(call_id)
                if ev is not None:
                    ev.tool_result = summary
                    if ok is not None:
                        ev.ok = ok
                    elif summary:
                        low = summary.lower()[:120]
                        ev.ok = not (
                            "traceback" in low
                            or low.startswith("exit: 1")
                            or low.startswith("error:")
                        )
                else:
                    other_events.append(
                        Event(
                            "tool",
                            tool_name="(result)",
                            tool_result=summary,
                            ok=ok,
                            ts=ts,
                            raw_type=pt,
                        )
                    )
            elif pt == "agent_message":
                # Inter-agent / subagent traffic — not the main assistant conclusion
                am_text = strip_injection(
                    text_from_content(payload.get("text") or payload.get("content"))
                )
                if am_text:
                    other_events.append(
                        Event(
                            "meta",
                            text=am_text,
                            ts=ts,
                            raw_type="agent_message",
                            call_id=item_id,
                        )
                    )
            elif pt in ("reasoning", "thinking"):
                # Codex stores short summary_text; full body often encrypted only
                text = text_from_thinking_payload(payload)
                if text:
                    other_events.append(
                        Event(
                            "thinking",
                            text=text,
                            ts=ts,
                            raw_type=pt,
                            call_id=item_id,
                        )
                    )

    preferred_users = event_users if event_users else response_users
    if not preferred_users:
        tr.events = other_events
    elif any(e.ts for e in preferred_users) and any(e.ts for e in other_events):
        indexed = list(enumerate(preferred_users + other_events))
        indexed.sort(key=lambda iv: (iv[1].ts or "\uffff", iv[0]))
        tr.events = [e for _, e in indexed]
    else:
        tr.events = _interleave_users(preferred_users, other_events)

    tr.stats = {
        "response_item_types": dict(type_counts),
        "user_source": "event_msg" if event_users else "response_item",
    }
    return tr


def _event_key(event: Event) -> Tuple[Any, ...]:
    if event.call_id:
        return (event.kind, event.call_id)
    return (
        event.kind,
        event.text,
        event.tool_name,
        event.tool_args,
        event.tool_result,
        event.ok,
        event.ts,
        event.raw_type,
    )


def _merge_codex_fragments(
    session_id: str, paths: List[Path], before_ts: str = ""
) -> Transcript:
    tr = Transcript(
        source=str(paths[-1]) if paths else "",
        format="codex",
        session_id=session_id,
    )
    events: List[Event] = []
    type_counts: Counter = Counter()
    user_sources = set()
    for path in paths:
        part = _parse_codex_file(path, before_ts=before_ts)
        events.extend(part.events)
        if part.cwd:
            tr.cwd = part.cwd
        type_counts.update(part.stats.get("response_item_types") or {})
        if part.stats.get("user_source"):
            user_sources.add(str(part.stats["user_source"]))

    indexed = list(enumerate(events))
    if any(event.ts for event in events):
        indexed.sort(key=lambda item: (item[1].ts or "\uffff", item[0]))
    seen = set()
    for _, event in indexed:
        key = _event_key(event)
        if key in seen:
            continue
        seen.add(key)
        tr.events.append(event)
    tr.stats = {
        "response_item_types": dict(type_counts),
        "user_source": ",".join(sorted(user_sources)),
    }
    return tr


def _parse_codex_session(
    session_id: str,
    anchor: Path,
    before_ts: str = "",
    visited: Optional[set[str]] = None,
) -> Transcript:
    visited = set(visited or ())
    visited.add(session_id)
    paths = _codex_session_paths(session_id, anchor)
    if not paths:
        paths = [anchor]
    tr = _merge_codex_fragments(session_id, paths, before_ts=before_ts)

    fork_meta = next(
        (
            meta
            for meta in (_codex_session_meta(path) for path in paths)
            if meta.forked_from_id
        ),
        CodexSessionMeta(),
    )
    lineage = [session_id]
    missing: List[str] = []
    unresolved: List[str] = []
    cutoffs: Dict[str, str] = {}
    parent_id = fork_meta.forked_from_id
    if parent_id:
        if parent_id in visited or _parse_rfc3339(fork_meta.created_at) is None:
            unresolved.append(parent_id)
        else:
            parent_paths = _codex_session_paths(parent_id, anchor)
            if not parent_paths:
                missing.append(parent_id)
            else:
                cutoffs[parent_id] = fork_meta.created_at
                parent = _parse_codex_session(
                    parent_id,
                    parent_paths[-1],
                    before_ts=fork_meta.created_at,
                    visited=visited,
                )
                tr.events = parent.events + tr.events
                tr.cwd = tr.cwd or parent.cwd
                lineage = list(parent.stats.get("lineage_session_ids") or [parent_id])
                lineage.append(session_id)
                missing.extend(parent.stats.get("lineage_missing_parent_ids") or [])
                unresolved.extend(parent.stats.get("lineage_unresolved_parent_ids") or [])
                cutoffs.update(parent.stats.get("lineage_cutoffs") or {})

    tr.stats.update(
        {
            "lineage_session_ids": lineage,
            "lineage_missing_parent_ids": missing,
            "lineage_unresolved_parent_ids": unresolved,
            "lineage_cutoffs": cutoffs,
            "lineage_complete": not missing and not unresolved,
        }
    )
    return tr


def parse_codex(path: Path) -> Transcript:
    """Parse a Codex thread plus each available ancestor up to its fork point."""
    session_id = _codex_session_id(path)
    if not session_id:
        return _parse_codex_file(path)
    tr = _parse_codex_session(session_id, path)
    tr.source = str(path)
    tr.session_id = session_id
    return tr


def _interleave_users(users: List[Event], rest: List[Event]) -> List[Event]:
    """Place each user message before successive assistant turns when timestamps are absent."""
    if not users:
        return list(rest)
    if not rest:
        return list(users)
    out: List[Event] = []
    ui = 0
    i = 0
    while i < len(rest) and rest[i].kind == "tool":
        out.append(rest[i])
        i += 1
    while i < len(rest):
        if rest[i].kind == "assistant":
            if ui < len(users):
                out.append(users[ui])
                ui += 1
            out.append(rest[i])
            i += 1
            while i < len(rest) and rest[i].kind == "tool":
                out.append(rest[i])
                i += 1
        else:
            out.append(rest[i])
            i += 1
    while ui < len(users):
        out.append(users[ui])
        ui += 1
    return out


def parse_claude(path: Path) -> Transcript:
    tr = Transcript(source=str(path), format="claude")
    pending_tools: List[Event] = []  # FIFO for tool_use → tool_result pairing
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            tr.session_id = str(o.get("sessionId") or tr.session_id)
            tr.cwd = str(o.get("cwd") or tr.cwd)
            ts = str(o.get("timestamp") or "")
            if t not in ("user", "assistant"):
                continue
            msg = o.get("message")
            if isinstance(msg, str):
                msg = as_dict(msg) or {}
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                cleaned = strip_injection(content)
                if t == "user" and is_noise_user_text(content):
                    continue
                if cleaned:
                    tr.events.append(Event(t, text=cleaned, ts=ts, raw_type=t))
                continue
            if not isinstance(content, list):
                continue
            texts: List[str] = []
            thinking_parts: List[str] = []

            def _flush_texts() -> None:
                nonlocal texts
                if not texts:
                    return
                role = "user" if t == "user" else "assistant"
                cleaned = strip_injection("\n".join(texts))
                if cleaned and not (role == "user" and is_noise_user_text(cleaned)):
                    tr.events.append(Event(role, text=cleaned, ts=ts))
                texts = []

            def _flush_thinking() -> None:
                nonlocal thinking_parts
                if not thinking_parts:
                    return
                body = "\n".join(p for p in thinking_parts if p.strip()).strip()
                if body:
                    tr.events.append(
                        Event("thinking", text=body, ts=ts, raw_type="thinking")
                    )
                thinking_parts = []

            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    _flush_thinking()
                    texts.append(str(block.get("text") or ""))
                elif btype in ("thinking", "redacted_thinking"):
                    # Flush assistant text before thinking only if we already had text;
                    # otherwise collect thinking first (common Claude order).
                    th = str(block.get("thinking") or block.get("text") or "").strip()
                    if th:
                        thinking_parts.append(th)
                elif btype == "tool_use":
                    _flush_thinking()
                    _flush_texts()
                    name = str(block.get("name") or "tool")
                    call_id = str(block.get("id") or "")
                    ev = Event(
                        "tool",
                        tool_name=name,
                        tool_args=summarize_tool_args(block.get("input")),
                        ts=ts,
                        raw_type="tool_use",
                        call_id=call_id,
                    )
                    tr.events.append(ev)
                    pending_tools.append(ev)
                elif btype == "tool_result":
                    _flush_thinking()
                    _flush_texts()
                    summary, ok_store = store_tool_result(
                        block.get("content") or block.get("output"),
                        TOOL_RESULT_STORE_CHARS,
                    )
                    ok = not bool(block.get("is_error"))
                    if block.get("is_error") is None and ok_store is not None:
                        ok = ok_store
                    tid = str(block.get("tool_use_id") or block.get("tool_useId") or "")
                    attached = False
                    if tid:
                        for pev in pending_tools:
                            if pev.call_id == tid and not pev.tool_result:
                                pev.tool_result = summary
                                pev.ok = ok
                                attached = True
                                break
                    if not attached and pending_tools:
                        for pev in pending_tools:
                            if not pev.tool_result:
                                pev.tool_result = summary
                                pev.ok = ok
                                attached = True
                                break
                    if not attached:
                        tr.events.append(
                            Event(
                                "tool",
                                tool_name="(result)",
                                tool_result=summary,
                                ok=ok,
                                ts=ts,
                                raw_type="tool_result",
                            )
                        )
            _flush_thinking()
            if texts:
                role = "user" if t == "user" else "assistant"
                cleaned = strip_injection("\n".join(texts))
                if role == "user" and is_noise_user_text("\n".join(texts)):
                    continue
                if cleaned:
                    tr.events.append(Event(role, text=cleaned, ts=ts))
    return tr


def parse_grok_chat(path: Path) -> Transcript:
    tr = Transcript(source=str(path), format="grok")
    # Layout: sessions/<urlenc-cwd>/<sessionId>/chat_history.jsonl
    parent = path.parent.name
    if re.match(r"^[0-9a-f-]{20,}$", parent, re.I):
        tr.session_id = parent
        try:
            from urllib.parse import unquote

            tr.cwd = unquote(path.parent.parent.name)
        except Exception:
            pass
    pending: Dict[str, Event] = {}
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            if t == "system":
                continue
            if t == "user":
                raw = text_from_content(o.get("content"))
                text = strip_injection(raw)
                # keep long continuation summaries; drop pure system/skill dumps
                if not text or is_noise_user_text(raw):
                    # exception: condensed handoff summaries are useful
                    if raw.lstrip().startswith(
                        "This session is being continued from a previous conversation"
                    ):
                        tr.events.append(
                            Event(
                                "user",
                                text=clip(strip_injection(raw) or raw, 4000),
                                raw_type="continuation",
                            )
                        )
                    continue
                tr.events.append(Event("user", text=text))
            elif t == "assistant":
                text = strip_injection(text_from_content(o.get("content")))
                if text:
                    tr.events.append(Event("assistant", text=text))
                for tc in o.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    tid = str(tc.get("id") or "")
                    name = str(tc.get("name") or "tool")
                    ev = Event(
                        "tool",
                        tool_name=name,
                        tool_args=summarize_tool_args(tc.get("arguments")),
                    )
                    tr.events.append(ev)
                    if tid:
                        pending[tid] = ev
            elif t == "tool_result":
                tid = str(o.get("tool_call_id") or "")
                summary, ok = store_tool_result(o.get("content"), TOOL_RESULT_STORE_CHARS)
                if ok is None and summary:
                    low = summary.strip().lower()
                    if low.startswith("exit: 1") or low.startswith("exit code: 1"):
                        ok = False
                    elif low.startswith("exit: 0") or low.startswith("exit code: 0"):
                        ok = True
                ev = pending.get(tid)
                if ev is not None:
                    ev.tool_result = summary
                    if ok is not None:
                        ev.ok = ok
                else:
                    tr.events.append(
                        Event(
                            "tool",
                            tool_name="(result)",
                            tool_result=summary,
                            ok=ok,
                        )
                    )
            elif t in ("reasoning", "thinking"):
                text = text_from_thinking_payload(o)
                if text:
                    tr.events.append(
                        Event("thinking", text=text, raw_type=str(t))
                    )
    return tr


def parse_pi(path: Path) -> Transcript:
    tr = Transcript(source=str(path), format="pi")
    pending: Dict[str, Event] = {}
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            ts = str(o.get("timestamp") or "")
            if t == "session":
                tr.session_id = str(o.get("id") or tr.session_id)
                tr.cwd = str(o.get("cwd") or tr.cwd)
                continue
            # runtime stream forms
            if t == "message_end":
                msg = o.get("message") or {}
                role = msg.get("role")
                text_body = text_from_content(msg.get("content"))
                if role in ("user", "assistant") and text_body:
                    tr.events.append(Event(role, text=text_body, ts=ts, raw_type=t))
                continue
            if t == "message":
                msg = o.get("message")
                if isinstance(msg, str):
                    msg = as_dict(msg) or {}
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                # Standalone tool results (Pi v3 stores these as separate messages)
                if role in ("toolResult", "tool_result", "tool"):
                    tid = str(
                        msg.get("toolCallId")
                        or msg.get("tool_call_id")
                        or msg.get("id")
                        or ""
                    )
                    summary, ok_store = store_tool_result(
                        msg.get("content") or msg.get("output"),
                        TOOL_RESULT_STORE_CHARS,
                    )
                    ok = ok_store
                    if msg.get("isError") is True or msg.get("is_error") is True:
                        ok = False
                    elif msg.get("isError") is False or msg.get("is_error") is False:
                        ok = True
                    name = str(msg.get("toolName") or msg.get("name") or "(result)")
                    ev = pending.get(tid) if tid else None
                    if ev is not None:
                        ev.tool_result = summary
                        ev.ok = ok
                    else:
                        tr.events.append(
                            Event(
                                "tool",
                                tool_name=name if name != "(result)" else "(result)",
                                tool_result=summary,
                                ok=ok,
                                ts=ts,
                                raw_type="toolResult",
                                call_id=tid,
                            )
                        )
                    continue
                content = msg.get("content")
                texts: List[str] = []
                thinking_parts: List[str] = []
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            texts.append(str(block.get("text") or ""))
                        elif btype in ("thinking", "redacted_thinking", "reasoning"):
                            th = text_from_thinking_payload(block)
                            if th:
                                thinking_parts.append(th)
                        elif btype in ("tool_use", "toolCall", "functionCall"):
                            if thinking_parts:
                                tr.events.append(
                                    Event(
                                        "thinking",
                                        text="\n".join(thinking_parts),
                                        ts=ts,
                                        raw_type="thinking",
                                    )
                                )
                                thinking_parts = []
                            if texts and role in ("user", "assistant"):
                                tr.events.append(
                                    Event(role, text="\n".join(texts), ts=ts)
                                )
                                texts = []
                            call_id = str(
                                block.get("id")
                                or block.get("toolCallId")
                                or block.get("tool_call_id")
                                or ""
                            )
                            tev = Event(
                                "tool",
                                tool_name=str(block.get("name") or "tool"),
                                tool_args=summarize_tool_args(
                                    block.get("input") or block.get("arguments")
                                ),
                                ts=ts,
                                call_id=call_id,
                                raw_type=str(btype),
                            )
                            tr.events.append(tev)
                            if call_id:
                                pending[call_id] = tev
                        elif btype == "tool_result":
                            summary, ok = store_tool_result(
                                block.get("content"), TOOL_RESULT_STORE_CHARS
                            )
                            tid = str(
                                block.get("toolCallId")
                                or block.get("tool_call_id")
                                or block.get("tool_use_id")
                                or ""
                            )
                            if tid and tid in pending:
                                pending[tid].tool_result = summary
                                pending[tid].ok = ok
                            else:
                                tr.events.append(
                                    Event(
                                        "tool",
                                        tool_name="(result)",
                                        tool_result=summary,
                                        ok=ok,
                                        ts=ts,
                                    )
                                )
                else:
                    texts.append(text_from_content(content))
                if thinking_parts:
                    tr.events.append(
                        Event(
                            "thinking",
                            text="\n".join(thinking_parts),
                            ts=ts,
                            raw_type="thinking",
                        )
                    )
                if texts and role in ("user", "assistant"):
                    tr.events.append(Event(role, text="\n".join(texts), ts=ts))
    return tr



def load_transcript(path: Path, fmt: str = "auto") -> Transcript:
    path = path.expanduser()
    if path.is_dir():
        # Grok session directory
        chat = path / "chat_history.jsonl"
        if chat.exists():
            tr = parse_grok_chat(chat)
            tr.source = str(path)
            tr.session_id = tr.session_id or path.name
            return tr
        # Prefer primary session transcripts; newest mtime (not largest — avoids subagents)
        jsonls = [p for p in path.rglob("*.jsonl") if _is_primary_session_file(p)]
        if not jsonls:
            jsonls = [
                p
                for p in path.rglob("*.jsonl")
                if p.name not in _SKIP_SESSION_NAMES
                and "subagent" not in _norm_path_str(p).lower()
            ]
        if not jsonls:
            raise FileNotFoundError(f"no jsonl under directory: {path}")
        path = max(jsonls, key=lambda p: p.stat().st_mtime)
        print(f"[session-digest] directory → {path}", file=sys.stderr)
    if fmt == "auto":
        fmt = detect_format(path)
    if fmt == "codex":
        return parse_codex(path)
    if fmt == "claude":
        return parse_claude(path)
    if fmt in ("grok", "grok-events"):
        # if events file, try sibling chat_history
        if path.name == "events.jsonl":
            sibling = path.parent / "chat_history.jsonl"
            if sibling.exists():
                return parse_grok_chat(sibling)
        return parse_grok_chat(path)
    if fmt == "pi":
        return parse_pi(path)
    raise ValueError(f"unsupported or undetected format for {path} (got {fmt})")


# --------------------------------------------------------------------------- render


def merge_tool_events(events: List[Event]) -> List[Event]:
    """Attach bare (result) events to open tool calls (FIFO) when possible."""
    out: List[Event] = []
    open_tools: List[Event] = []
    for ev in events:
        if ev.kind == "tool" and ev.tool_name != "(result)":
            out.append(ev)
            if not ev.tool_result:
                open_tools.append(ev)
            continue
        if ev.kind == "tool" and ev.tool_name == "(result)":
            attached = False
            while open_tools:
                target = open_tools.pop(0)
                if not target.tool_result:
                    target.tool_result = ev.tool_result
                    target.ok = ev.ok
                    attached = True
                    break
            if not attached:
                out.append(ev)
            continue
        out.append(ev)
        # dialogue resets open-tool window only lightly — keep queue across user tool_result rows
    return out


def _thinking_limit(thinking_mode: str, max_thinking_chars: int) -> int:
    if thinking_mode == "keep":
        return max(max_thinking_chars * 3, max_thinking_chars)
    return max_thinking_chars


def _render_thinking_block(
    lines: List[str],
    text: str,
    thinking_mode: str,
    max_thinking_chars: int,
) -> None:
    body = summarize_thinking(
        text, thinking_mode, _thinking_limit(thinking_mode, max_thinking_chars)
    )
    if not body:
        return
    label = {
        "summary": "### Thinking (summary)",
        "keep": "### Thinking",
        "drop": "",
    }.get(thinking_mode, "### Thinking (summary)")
    if not label:
        return
    lines.append(label)
    lines.append("")
    lines.append(body)
    lines.append("")


def _append_tools_section(
    lines: List[str],
    tools: List[Event],
    mode: str,
    max_tools_per_turn: int,
    max_tool_result_chars: int,
) -> None:
    """Same cap/status/fencing for assistant-adjacent and orphan tool bursts."""
    if mode == "dialogue" or not tools:
        return
    real = [
        te
        for te in tools
        if not (te.tool_name == "(result)" and not te.tool_result)
    ]
    if not real:
        return
    lines.append("### Tools")
    lines.append("")
    if mode == "tools":
        cap = max(max_tools_per_turn * 3, max_tools_per_turn)
        res_lim = max_tool_result_chars * 3
    else:
        cap = max_tools_per_turn
        res_lim = max_tool_result_chars
    shown = 0
    for te in real:
        shown += 1
        if shown > cap:
            lines.append(f"- … +{len(real) - shown + 1} more tool calls omitted")
            break
        status = ""
        if te.ok is True:
            status = " ok"
        elif te.ok is False:
            status = " ERR"
        line = f"- `{te.tool_name}`{status}"
        if te.tool_args:
            line += f" — {te.tool_args}"
        lines.append(line)
        if te.tool_result and mode in ("balanced", "tools"):
            res = clip(te.tool_result, res_lim)
            if res:
                lines.append("  ```")
                lines.append("  " + res.replace("\n", "\n  "))
                lines.append("  ```")
    lines.append("")


def render_markdown(
    tr: Transcript,
    mode: str = "balanced",
    max_message_chars: int = 3500,
    max_tool_result_chars: int = 350,
    max_tools_per_turn: int = 12,
    thinking_mode: str = "summary",
    max_thinking_chars: int = 1000,
    handoff_full: bool = False,
    handoff_max_decisions: int = 12,
    handoff_max_thinking: int = 15,
    handoff_max_tools: int = 40,
) -> str:
    events = merge_tool_events(tr.events)
    user_n = sum(1 for e in events if e.kind == "user")
    asst_n = sum(1 for e in events if e.kind == "assistant")
    tool_n = sum(1 for e in events if e.kind == "tool" and e.tool_name != "(result)")
    think_n = sum(1 for e in events if e.kind == "thinking")
    out_lines: List[str] = []
    out_lines.append("# Session Digest")
    out_lines.append("")
    out_lines.append(f"- **format**: `{tr.format}`")
    out_lines.append(f"- **source**: `{tr.source}`")
    if tr.session_id:
        out_lines.append(f"- **session_id**: `{tr.session_id}`")
    if tr.cwd:
        out_lines.append(f"- **cwd**: `{tr.cwd}`")
    lineage = tr.stats.get("lineage_session_ids") or []
    if len(lineage) > 1:
        out_lines.append(f"- **lineage**: `{' -> '.join(lineage)}`")
    missing_lineage = list(tr.stats.get("lineage_missing_parent_ids") or [])
    unresolved_lineage = list(tr.stats.get("lineage_unresolved_parent_ids") or [])
    if missing_lineage or unresolved_lineage:
        details = []
        if missing_lineage:
            details.append("missing=" + ",".join(missing_lineage))
        if unresolved_lineage:
            details.append("unresolved=" + ",".join(unresolved_lineage))
        out_lines.append(f"- **lineage_status**: `partial ({'; '.join(details)})`")
    out_lines.append(
        f"- **counts**: users={user_n}, assistants={asst_n}, tools={tool_n}, "
        f"thinking_blocks={think_n}, mode={mode}, thinking_tier={thinking_mode}"
    )
    out_lines.append("")
    out_lines.append(
        "_Distilled for the next agent: dialogue kept; decision reasoning "
        f"({thinking_mode}); tool payloads compressed; secret values redacted._"
    )
    out_lines.append("")

    if mode == "handoff":
        out_lines.append("## Goal / current ask")
        last_user = next((e for e in reversed(events) if e.kind == "user"), None)
        first_user = next((e for e in events if e.kind == "user"), None)
        if first_user:
            out_lines.append(clip(redact(first_user.text), max_message_chars))
        out_lines.append("")
        if last_user and last_user is not first_user:
            out_lines.append("## Latest user message")
            out_lines.append(clip(redact(last_user.text), max_message_chars))
            out_lines.append("")

        asst_events = [e for e in events if e.kind == "assistant" and e.text.strip()]
        if len(asst_events) > handoff_max_decisions:
            asst_events = asst_events[-handoff_max_decisions:]
            out_lines.append(
                f"## Decisions & outcomes (last {handoff_max_decisions} assistant turns)"
            )
        else:
            out_lines.append("## Decisions & outcomes (from assistant turns)")
        for e in asst_events:
            t = clip(redact(e.text), min(800, max_message_chars))
            if t.strip():
                out_lines.append(f"- {t.replace(chr(10), ' / ')}")
        if not asst_events:
            out_lines.append("- (no assistant conclusion found)")
        out_lines.append("")

        if thinking_mode != "drop":
            out_lines.append("## Decision reasoning (thinking)")
            th_events = [e for e in events if e.kind == "thinking" and e.text.strip()]
            if len(th_events) > handoff_max_thinking:
                th_events = th_events[-handoff_max_thinking:]
            any_th = False
            for e in th_events:
                body = summarize_thinking(
                    e.text,
                    thinking_mode,
                    min(600, _thinking_limit(thinking_mode, max_thinking_chars)),
                )
                if not body.strip():
                    continue
                any_th = True
                out_lines.append(f"- {body.replace(chr(10), ' / ')}")
            if not any_th:
                out_lines.append("- (no recoverable thinking/reasoning in this session)")
            out_lines.append("")

        out_lines.append("## Key tool actions")
        tool_events = [
            e for e in events if e.kind == "tool" and e.tool_name != "(result)"
        ]
        if len(tool_events) > handoff_max_tools:
            omitted = len(tool_events) - handoff_max_tools
            tool_events = tool_events[-handoff_max_tools:]
            out_lines.append(f"- … ({omitted} earlier tool calls omitted)")
        for e in tool_events:
            bit = f"- `{e.tool_name}`"
            if e.ok is True:
                bit += " ok"
            elif e.ok is False:
                bit += " ERR"
            if e.tool_args:
                bit += f" · {e.tool_args}"
            if e.tool_result:
                bit += f" → {clip(e.tool_result, 160).replace(chr(10), ' / ')}"
            out_lines.append(bit)
        if not tool_events:
            out_lines.append("- (no tools)")
        out_lines.append("")

        sub = [e for e in events if e.kind == "meta" and e.raw_type == "agent_message"]
        if sub:
            out_lines.append("## Subagent notes (truncated)")
            for e in sub[-5:]:
                out_lines.append(f"- {clip(redact(e.text), 240).replace(chr(10), ' / ')}")
            out_lines.append("")

        out_lines.append("## Open / next")
        last_asst = next((e for e in reversed(events) if e.kind == "assistant"), None)
        if last_asst and last_asst.text.strip():
            out_lines.append(clip(redact(last_asst.text), min(1500, max_message_chars)))
        else:
            out_lines.append("- (no assistant conclusion found)")
        out_lines.append("")

        if handoff_full:
            out_lines.append("## Full balanced transcript")
            out_lines.append("")
            mode = "balanced"
        else:
            out_lines.append(
                "_Handoff is compact by default. Pass `--handoff-full` to append the balanced transcript._"
            )
            out_lines.append("")
            return "\n".join(out_lines).rstrip() + "\n"

    i = 0
    turn = 0
    while i < len(events):
        e = events[i]
        if e.kind == "meta":
            if mode == "tools" and e.raw_type == "agent_message" and e.text.strip():
                out_lines.append(f"_subagent: {clip(redact(e.text), 200)}_")
                out_lines.append("")
            i += 1
            continue
        if e.kind == "thinking":
            chunks: List[str] = []
            while i < len(events) and events[i].kind == "thinking":
                if events[i].text.strip():
                    chunks.append(events[i].text.strip())
                i += 1
            if thinking_mode != "drop" and chunks:
                _render_thinking_block(
                    out_lines,
                    "\n\n".join(chunks),
                    thinking_mode,
                    max_thinking_chars,
                )
            continue
        if e.kind == "user":
            turn += 1
            out_lines.append(f"## Turn {turn} · User")
            out_lines.append("")
            out_lines.append(clip(redact(e.text), max_message_chars))
            out_lines.append("")
            i += 1
            continue
        if e.kind == "assistant":
            out_lines.append("### Assistant")
            out_lines.append("")
            out_lines.append(clip(redact(e.text), max_message_chars))
            out_lines.append("")
            i += 1
            tools: List[Event] = []
            while i < len(events) and events[i].kind == "tool":
                tools.append(events[i])
                i += 1
            _append_tools_section(
                out_lines, tools, mode, max_tools_per_turn, max_tool_result_chars
            )
            continue
        if e.kind == "tool":
            tools = []
            while i < len(events) and events[i].kind == "tool":
                tools.append(events[i])
                i += 1
            _append_tools_section(
                out_lines, tools, mode, max_tools_per_turn, max_tool_result_chars
            )
            continue
        i += 1

    return "\n".join(out_lines).rstrip() + "\n"



# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Distill Codex/Claude/Grok/Pi session transcripts for the next agent."
    )
    p.add_argument(
        "target",
        nargs="?",
        default="",
        help="Session file, directory, id fragment, or exact indexed title",
    )
    p.add_argument(
        "--format",
        default="auto",
        choices=["auto", "codex", "claude", "grok", "pi"],
        help="Force transcript format (default: auto-detect)",
    )
    p.add_argument(
        "--cli",
        choices=["codex", "claude", "grok", "pi"],
        default="",
        help="Limit lookup and parsing to one CLI session store",
    )
    p.add_argument(
        "--name",
        default="",
        help="Resolve one exact session title; requires --cli",
    )
    p.add_argument(
        "--mode",
        default="balanced",
        choices=["balanced", "dialogue", "tools", "handoff"],
        help="balanced=dialogue+tool summaries; dialogue=messages only; "
        "tools=more tool detail; handoff=compact structured brief "
        "(add --handoff-full to append balanced body)",
    )
    p.add_argument(
        "--handoff-full",
        action="store_true",
        help="With --mode handoff, also append the full balanced transcript",
    )
    p.add_argument(
        "--thinking",
        default="summary",
        choices=["drop", "summary", "keep"],
        dest="thinking_mode",
        help="How to retain model reasoning/thinking: "
        "drop=omit; summary=decision-oriented compress (default); "
        "keep=retain clipped full text. Encrypted-only blobs stay empty.",
    )
    p.add_argument("--max-message-chars", type=int, default=3500)
    p.add_argument("--max-tool-result-chars", type=int, default=350)
    p.add_argument(
        "--max-thinking-chars",
        type=int,
        default=1000,
        help="Char budget per thinking block in summary mode "
        "(keep uses 3× this; default 1000)",
    )
    p.add_argument("--max-tools-per-turn", type=int, default=12)
    p.add_argument(
        "--find",
        default="",
        help="Search session stores under HOME for id/path fragment or indexed title",
    )
    p.add_argument(
        "--list-recent",
        type=int,
        default=0,
        metavar="N",
        help="List N recent session files from known roots",
    )
    p.add_argument(
        "-o",
        "--out",
        default="-",
        help="Output path (default: stdout)",
    )
    p.add_argument(
        "--json-stats",
        action="store_true",
        help="Print parse stats as JSON to stderr",
    )
    return p


_PRIMARY_SESSION_NAMES = frozenset({"chat_history.jsonl"})
_SKIP_SESSION_NAMES = frozenset(
    {
        "events.jsonl",
        "updates.jsonl",
        "prompt_history.jsonl",
        "rewind_points.jsonl",
        "memtrace.jsonl",
    }
)


def _is_primary_session_file(p: Path) -> bool:
    """Prefer human-facing session transcripts over runtime side channels."""
    name = p.name
    if name in _SKIP_SESSION_NAMES:
        return False
    nlow = _norm_path_str(p).lower()
    if "subagent" in nlow:
        return False
    if name in _PRIMARY_SESSION_NAMES:
        return True
    if name.startswith("rollout-") and name.endswith(".jsonl"):
        return True
    if name.endswith(".jsonl") and _is_under(p, CLAUDE_HOME / "projects"):
        return True
    if name.endswith(".jsonl") and _is_under(p, PI_HOME / "agent" / "sessions"):
        return True
    if name.endswith(".jsonl") and (
        "/.claude/projects/" in nlow or "/claude/projects/" in nlow
    ):
        return True
    if name.endswith(".jsonl") and (
        "/.pi/agent/sessions/" in nlow
        or "/pi/agent/sessions/" in nlow
        or "/agent/sessions/" in nlow
    ):
        return True
    return False



def list_recent(n: int) -> List[Path]:
    found: List[Tuple[float, Path]] = []
    for _, root in iter_candidate_roots():
        if not root.exists():
            continue
        for p in root.rglob("*.jsonl"):
            if not _is_primary_session_file(p):
                continue
            try:
                found.append((p.stat().st_mtime, p))
            except OSError:
                continue
    found.sort(reverse=True)
    return [p for _, p in found[:n]]


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cli and args.format != "auto" and args.cli != args.format:
        print("error: --cli and --format select different stores", file=sys.stderr)
        return 2
    format_hint = args.cli or args.format
    if args.name and not args.cli:
        print("error: --name requires an explicit --cli", file=sys.stderr)
        return 2
    if args.name and (args.target or args.find or args.list_recent):
        print(
            "error: --name cannot be combined with target, --find, or --list-recent",
            file=sys.stderr,
        )
        return 2
    if args.list_recent:
        for p in list_recent(args.list_recent):
            try:
                sz = p.stat().st_size
            except OSError:
                sz = -1
            print(f"{sz:10d}  {p}")
        return 0
    if args.find:
        matches = find_sessions(args.find, format_hint=format_hint)
        if not matches:
            print(f"No sessions matched: {args.find}", file=sys.stderr)
            return 1
        for p in matches:
            print(p)
        return 0
    lookup = args.name or args.target
    if not lookup:
        print(
            "error: provide a session path/id/name, --cli ... --name ..., "
            "or use --find / --list-recent",
            file=sys.stderr,
        )
        return 2

    target: Optional[Path] = None
    if not args.name:
        target = Path(lookup).expanduser()
    if args.name or target is None or not target.exists():
        matches = find_session_matches(lookup, limit=20, format_hint=format_hint)
        if not matches:
            if args.name:
                print(
                    f'error: no exact {args.cli} session title matched: "{lookup}"',
                    file=sys.stderr,
                )
            else:
                print(
                    f"error: path not found and no session id/name match: {lookup}",
                    file=sys.stderr,
                )
            return 1
        exact_titles = [m for m in matches if m.kind == "title-exact"]
        path_matches = [m for m in matches if m.kind == "path"]
        title_substrings = [m for m in matches if m.kind == "title-substring"]
        if len(exact_titles) > 1:
            print(f'error: ambiguous exact session title: "{lookup}"', file=sys.stderr)
            for match in exact_titles:
                detail = f"{match.format}"
                if match.session_id:
                    detail += f" {match.session_id}"
                print(f"  [{detail}] {match.path}", file=sys.stderr)
            return 1
        if exact_titles:
            selected = exact_titles[0]
        elif args.name:
            print(
                f'error: no exact {args.cli} session title matched: "{lookup}"',
                file=sys.stderr,
            )
            return 1
        elif path_matches:
            selected = path_matches[0]
        else:
            print(
                f'error: session title lookup requires the full exact title: "{lookup}"',
                file=sys.stderr,
            )
            for match in title_substrings:
                detail = f"{match.format}"
                if match.session_id:
                    detail += f" {match.session_id}"
                print(f'  [{detail}] "{match.title}"  {match.path}', file=sys.stderr)
            return 1
        target = selected.path
        if selected.title:
            print(
                f'[session-digest] resolved {selected.format} title '
                f'"{selected.title}" ({selected.session_id}) → {target}',
                file=sys.stderr,
            )
        else:
            print(f"[session-digest] resolved → {target}", file=sys.stderr)

    try:
        tr = load_transcript(target, format_hint)
    except Exception as e:
        print(f"error: failed to parse: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if not tr.events:
        print(
            "[session-digest] warning: 0 events parsed — check --format / file shape",
            file=sys.stderr,
        )

    md = render_markdown(
        tr,
        mode=args.mode,
        max_message_chars=args.max_message_chars,
        max_tool_result_chars=args.max_tool_result_chars,
        max_tools_per_turn=args.max_tools_per_turn,
        thinking_mode=args.thinking_mode,
        max_thinking_chars=args.max_thinking_chars,
        handoff_full=bool(getattr(args, "handoff_full", False)),
    )
    if args.json_stats:
        stats = {
            "format": tr.format,
            "source": tr.source,
            "session_id": tr.session_id,
            "cwd": tr.cwd,
            "events": len(tr.events),
            "users": sum(1 for e in tr.events if e.kind == "user"),
            "assistants": sum(1 for e in tr.events if e.kind == "assistant"),
            "tools": sum(1 for e in tr.events if e.kind == "tool"),
            "thinking": sum(1 for e in tr.events if e.kind == "thinking"),
            "thinking_mode": args.thinking_mode,
            "parser_stats": tr.stats,
        }
        print(json.dumps(stats, ensure_ascii=False, indent=2), file=sys.stderr)

    if args.out == "-" or args.out == "":
        try:
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            except Exception:
                pass
            sys.stdout.write(md)
        except BrokenPipeError:
            # e.g. piped to `head`
            try:
                sys.stdout.close()
            except Exception:
                pass
            return 0
    else:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"wrote {out} ({len(md)} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
