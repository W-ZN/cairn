"""
Council — explicit cross-CLI delegation and independent advice
(codex / gemini / claude / grok / pi / opencode / dsh).

Default is read-only. Write authority is opt-in only when the orchestrator
passes --allow-write with a non-empty --write-scope (file | lines | logic set
authorized by the user's plain-text request). Council creates no grant file;
scope is prompt-injected and may remain in the member CLI's transcript.

--session NAME records member, absolute cwd and SESSION_ID in the named
ledger. Prompts and replies are not stored.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable, Generator, List, Optional

SURGE_PROXY = "http://127.0.0.1:6152"
NO_WRITE_GUARD = (
    "Constraint: read-only. Do not modify files."
)
# Codex: workspace-write allows edits under cwd without unrestricted FS access.
CODEX_SANDBOX_READ = "read-only"
CODEX_SANDBOX_WRITE = "workspace-write"
# Claude: default to auto with a restricted tool surface for read-only work;
# plan remains an explicit read-only choice.
CLAUDE_PERM_DEFAULT = "auto"
CLAUDE_PERM_OVERRIDES = ("auto", "acceptEdits", "plan")
CLAUDE_TOOLS_READ = "Read,Glob,Grep,WebFetch,WebSearch"
CLAUDE_NO_PLAN_TOOLS = "EnterPlanMode,ExitPlanMode"
CLAUDE_FAMILY_ALIASES = ("haiku", "sonnet", "opus")
GEMINI_APPROVAL_READ = "default"
GEMINI_APPROVAL_WRITE = "auto_edit"
GROK_PERM_READ = "auto"
GROK_PERM_WRITE = "acceptEdits"
GROK_TOOLS_READ = "read_file,grep,list_dir,web_search,web_fetch"
# Pi has no plan mode; restrict tools for read-only.
PI_TOOLS_READ = "read,grep,find,ls"
PI_TOOLS_WRITE = "read,bash,edit,write,grep,find,ls"
# OpenCode plan agent plus this config removes edit/write/bash tools.
OPENCODE_AGENT_READ = "plan"
OPENCODE_AGENT_WRITE = "build"
OPENCODE_READ_ONLY_CONFIG = '{"permission":{"edit":"deny","bash":"deny"}}'
# dsh's profile defaults to danger-full-access when this variable is unset.
DSH_PERM_READ = "read-only"
DSH_PERM_WRITE = "workspace-write"
_OPENCODE_NVM_BIN = Path.home() / ".nvm/versions/node/v24.16.0/bin"

_START = time.time()
_STDERR_LOCK = threading.Lock()
MEMBER_STATUS: dict = {}


def _progress(message: str) -> None:
    """Lifecycle/heartbeat lines on stderr; stdout stays the pure JSON contract."""
    with _STDERR_LOCK:
        print(f"[council +{time.time() - _START:.0f}s] {message}",
              file=sys.stderr, flush=True)


def _write_enabled(args) -> bool:
    return bool(getattr(args, "allow_write", False))


def _codex_sandbox(args) -> str:
    return CODEX_SANDBOX_WRITE if _write_enabled(args) else CODEX_SANDBOX_READ


def _claude_permission_mode(args) -> str:
    return getattr(args, "claude_permission_mode", "") or CLAUDE_PERM_DEFAULT


def _claude_model_family(model: object) -> Optional[str]:
    """Return a standard Claude family only for known Claude model names."""
    if not isinstance(model, str):
        return None
    normalized = model.strip().lower()
    if normalized in CLAUDE_FAMILY_ALIASES:
        return normalized
    if not normalized.startswith("claude-"):
        return None
    match = re.search(r"(?:^|[-_])(haiku|sonnet|opus)(?:$|[-_.])", normalized)
    return match.group(1) if match else None


def _claude_effort(args) -> str:
    """Choose Claude's documented council default unless the caller specified one."""
    if effort := getattr(args, "claude_effort", None):
        return effort
    model = getattr(args, "model", "")
    normalized = model.strip().lower() if isinstance(model, str) else ""
    if _claude_model_family(model) == "haiku":
        return "max"
    if not normalized or _claude_model_family(model) == "opus":
        return "medium"
    if normalized in ("default", "opusplan", "opus[1m]", "opusplan[1m]"):
        return "medium"
    return "max"


def _claude_settings(args) -> Optional[str]:
    """Pin an explicitly requested Haiku while Claude explicitly runs in plan mode."""
    model = getattr(args, "model", "")
    if (_claude_permission_mode(args) == "plan"
            and isinstance(model, str) and _claude_model_family(model) == "haiku"):
        return json.dumps({"availableModels": [model]})
    return None


def _claude_model_match(requested_model: Optional[str], actual_models: list[str]) -> Optional[bool]:
    """Compare only model identities that Claude's event stream makes observable."""
    if not requested_model or not actual_models:
        return None
    requested_family = _claude_model_family(requested_model)
    if requested_model.strip().lower() in CLAUDE_FAMILY_ALIASES:
        actual_families = [_claude_model_family(model) for model in actual_models]
        if any(family not in (None, requested_family) for family in actual_families):
            return False
        if all(family == requested_family for family in actual_families):
            return True
        return None
    if requested_model.startswith("claude-"):
        return all(model == requested_model for model in actual_models)
    return None


def _claude_receipt(args, events: list[dict]) -> dict:
    """Summarize Claude's emitted model and terminal accounting metadata."""
    requested_model = args.model or None
    configured_model = None
    actual_models: list[str] = []
    subagent_models: list[str] = []
    effective_effort = None
    terminal_result = None
    for event in events:
        if event.get("parent_tool_use_id") is not None:
            if event.get("type") == "assistant":
                message = event.get("message")
                model = message.get("model") if isinstance(message, dict) else None
                if isinstance(model, str) and model != "<synthetic>" and model not in subagent_models:
                    subagent_models.append(model)
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            model = event.get("model")
            if isinstance(model, str):
                configured_model = model
        if event.get("type") == "assistant":
            message = event.get("message")
            model = message.get("model") if isinstance(message, dict) else None
            if isinstance(model, str) and model != "<synthetic>" and model not in actual_models:
                actual_models.append(model)
        for source in (event, event.get("message")):
            if not isinstance(source, dict):
                continue
            reported = (source.get("effective_effort") or source.get("effectiveEffort")
                        or source.get("effort"))
            if isinstance(reported, str):
                effective_effort = reported
        if event.get("type") == "result":
            terminal_result = event

    receipt = {
        "requested_model": requested_model,
        "requested_effort": _claude_effort(args),
        "configured_model": configured_model,
        "actual_models": actual_models,
        "subagent_models": subagent_models,
        "model_match": _claude_model_match(requested_model, actual_models),
        "effective_effort": effective_effort,
    }
    if terminal_result:
        for key in ("usage", "modelUsage", "total_cost_usd"):
            if key in terminal_result:
                receipt[key] = terminal_result[key]
    return receipt


def _attach_claude_receipt(args, result: dict) -> dict:
    receipt = _claude_receipt(args, result.get("_all_messages", []))
    result["receipt"] = receipt
    if receipt["model_match"] is False:
        result["success"] = False
        message = (
            "[claude model mismatch] requested "
            f"{receipt['requested_model']!r}, observed {receipt['actual_models']!r}"
        )
        result["error"] = f"{result['error']}\n\n{message}" if result.get("error") else message
    return result


def _grok_permission_mode(args) -> str:
    return GROK_PERM_WRITE if _write_enabled(args) else GROK_PERM_READ


def _gemini_approval_mode(args) -> str:
    return GEMINI_APPROVAL_WRITE if _write_enabled(args) else GEMINI_APPROVAL_READ


def _pi_tools_arg(args) -> Optional[str]:
    """Use a fixed tool allowlist for each council permission mode."""
    return PI_TOOLS_WRITE if _write_enabled(args) else PI_TOOLS_READ


def _opencode_agent(args) -> str:
    return OPENCODE_AGENT_WRITE if _write_enabled(args) else OPENCODE_AGENT_READ


def _dsh_permission_mode(args) -> str:
    return DSH_PERM_WRITE if _write_enabled(args) else DSH_PERM_READ


def _build_write_guard(write_scope: str) -> str:
    """Build the per-turn scope constraint without creating a grant file."""
    return (
        "Constraint: for this turn, modify only the change set below. "
        "Keep the diff minimal; preserve unrelated edits. Report changes and checks.\n"
        "Change set (path | region | change):\n"
        f"{write_scope.strip()}"
    )


def _compose_prompt(args, base_prompt: str) -> str:
    """Attach the mode-appropriate guard without creating a grant file."""
    prompt = base_prompt
    if _write_enabled(args):
        prompt = f"{prompt}\n\n{_build_write_guard(args.write_scope)}"
    elif not prompt.rstrip().endswith(NO_WRITE_GUARD):
        prompt = f"{prompt}\n\n{NO_WRITE_GUARD}"
    if os.name == "nt":
        prompt = windows_escape(prompt)
    return prompt


# ------------------------------------------------------------------ environment

def _get_windows_npm_paths() -> List[Path]:
    """Return candidate directories for npm global installs on Windows."""
    if os.name != "nt":
        return []
    paths: List[Path] = []
    env = os.environ
    if prefix := env.get("NPM_CONFIG_PREFIX") or env.get("npm_config_prefix"):
        paths.append(Path(prefix))
    if appdata := env.get("APPDATA"):
        paths.append(Path(appdata) / "npm")
    if localappdata := env.get("LOCALAPPDATA"):
        paths.append(Path(localappdata) / "npm")
    if programfiles := env.get("ProgramFiles"):
        paths.append(Path(programfiles) / "nodejs")
    return paths


def _augment_path_env(env: dict) -> None:
    """Prepend npm global directories to PATH if missing."""
    if os.name != "nt":
        return
    path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
    path_entries = [p for p in env.get(path_key, "").split(os.pathsep) if p]
    lower_set = {p.lower() for p in path_entries}
    for candidate in _get_windows_npm_paths():
        if candidate.is_dir() and str(candidate).lower() not in lower_set:
            path_entries.insert(0, str(candidate))
            lower_set.add(str(candidate).lower())
    env[path_key] = os.pathsep.join(path_entries)


def _resolve_executable(name: str, env: dict) -> str:
    """Resolve executable path, checking npm directories for .cmd/.bat on Windows."""
    if os.path.isabs(name) or os.sep in name or (os.altsep and os.altsep in name):
        return name
    path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
    path_val = env.get(path_key)
    win_exts = {".exe", ".cmd", ".bat", ".com"}
    if resolved := shutil.which(name, path=path_val):
        if os.name == "nt":
            suffix = Path(resolved).suffix.lower()
            if not suffix:
                resolved_dir = str(Path(resolved).parent)
                for ext in (".cmd", ".bat", ".exe", ".com"):
                    candidate = Path(resolved_dir) / f"{name}{ext}"
                    if candidate.is_file():
                        return str(candidate)
            elif suffix not in win_exts:
                return resolved
        return resolved
    if os.name == "nt":
        for base in _get_windows_npm_paths():
            for ext in (".cmd", ".bat", ".exe", ".com"):
                candidate = base / f"{name}{ext}"
                if candidate.is_file():
                    return str(candidate)
    if name == "opencode":
        fallback = _OPENCODE_NVM_BIN / name
        if fallback.is_file():
            return str(fallback)
    return name


_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                   "http_proxy", "https_proxy", "all_proxy")


def _apply_proxy(env: dict, proxy: Optional[str]) -> None:
    """Force the subprocess onto a specific proxy route.

    The host environment may inject its own HTTP(S)_PROXY. For codex that
    egress can be Cloudflare-blocked from the wrong region, while the user's
    system proxy (Surge) is not. Pinning the proxy makes the subprocess take
    the same network path as the interactive CLI. Empty proxy means inherit
    the environment untouched.
    """
    if not proxy:
        return
    if proxy.strip().lower() in ("none", "direct", "off"):
        for key in _PROXY_ENV_KEYS:
            env.pop(key, None)
        return
    for key in _PROXY_ENV_KEYS:
        env[key] = proxy


def windows_escape(prompt):
    """Windows style string escaping for newlines and special chars in prompt text."""
    result = prompt.replace('\n', '\\n')
    result = result.replace('\r', '\\r')
    result = result.replace('\t', '\\t')
    return result


def configure_windows_stdio() -> None:
    """Configure stdout/stderr to use UTF-8 encoding on Windows."""
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


# ------------------------------------------------------------------ stream core

def _stream_env(env_extra: Optional[dict], env_force: Optional[dict],
                proxy: Optional[str]) -> dict:
    """Build a child environment. Forced keys replace ambient values."""
    env = os.environ.copy()
    _augment_path_env(env)
    _apply_proxy(env, proxy)
    if env_extra:
        for key, value in env_extra.items():
            env.setdefault(key, value)
    if env_force:
        env.update(env_force)
    return env


def run_stream(cmd: List[str], *, cwd: Optional[str] = None,
               env_extra: Optional[dict] = None, env_force: Optional[dict] = None,
               proxy: Optional[str] = None, is_done: Callable[[str], bool],
               outcome: Optional[dict] = None) -> Generator[str, None, None]:
    """Execute a command and stream its output line-by-line.

    After a terminal event, let the CLI exit naturally so that a later error
    event and its exit status remain observable. A stuck process is terminated
    only after a bounded grace period.
    """
    env = _stream_env(env_extra, env_force, proxy)

    popen_cmd = cmd.copy()
    exe_path = _resolve_executable(cmd[0], env)
    popen_cmd[0] = exe_path

    # Windows .cmd/.bat files need cmd.exe wrapper (avoid shell=True for security)
    if os.name == "nt" and Path(exe_path).suffix.lower() in {".cmd", ".bat"}:
        # Escape shell metacharacters for cmd.exe
        def _cmd_quote(arg: str) -> str:
            if not arg:
                return '""'
            # For Windows batch files, % and ^ must be escaped before quoting
            arg = arg.replace('%', '%%')
            arg = arg.replace('^', '^^')
            if any(c in arg for c in '&|<>()^" \t'):
                # To safely escape " inside "...", close quote, escape ", reopen
                escaped = arg.replace('"', '"^""')
                return f'"{escaped}"'
            return arg
        cmdline = " ".join(_cmd_quote(a) for a in popen_cmd)
        comspec = env.get("COMSPEC", "cmd.exe")
        popen_cmd = f'"{comspec}" /d /s /c "{cmdline}"'

    process = subprocess.Popen(
        popen_cmd,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        encoding='utf-8',
        errors='replace',
        cwd=cwd,
        env=env,
        start_new_session=os.name != "nt",
    )

    output_queue: "queue.Queue[Optional[str]]" = queue.Queue()
    TERMINAL_EXIT_GRACE_SECONDS = 2.0
    terminal_seen = threading.Event()
    terminal_at: list[float] = []
    terminated_after_terminal = False

    def read_output() -> None:
        if process.stdout:
            for line in iter(process.stdout.readline, ""):
                stripped = line.strip()
                output_queue.put(stripped)
                if is_done(stripped):
                    terminal_at.append(time.monotonic())
                    terminal_seen.set()
            process.stdout.close()
        output_queue.put(None)

    thread = threading.Thread(target=read_output, daemon=True)
    thread.start()

    while True:
        if (terminal_seen.is_set() and terminal_at
                and time.monotonic() - terminal_at[0] >= TERMINAL_EXIT_GRACE_SECONDS):
            if process.poll() is None or thread.is_alive():
                terminated_after_terminal = True
            break
        try:
            line = output_queue.get(timeout=0.5)
            if line is None:
                if terminal_seen.is_set() and process.poll() is None and terminal_at:
                    remaining = max(0.0, TERMINAL_EXIT_GRACE_SECONDS - (
                        time.monotonic() - terminal_at[0]))
                    try:
                        process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        terminated_after_terminal = True
                break
            yield line
        except queue.Empty:
            if process.poll() is not None and not thread.is_alive():
                break

    def stop_process(force: bool = False) -> None:
        try:
            if os.name == "nt":
                process.kill() if force else process.terminate()
            else:
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass  # The process group exited between poll and signal.

    if terminated_after_terminal:
        stop_process()
    try:
        process.wait(timeout=2 if terminated_after_terminal else 5)
    except subprocess.TimeoutExpired:
        stop_process(force=True)
        process.wait()
    thread.join(timeout=0.5)
    if thread.is_alive() and terminated_after_terminal:
        stop_process(force=True)  # A descendant may still hold stdout open.
        thread.join(timeout=0.5)

    if outcome is not None:
        outcome["terminal_seen"] = terminal_seen.is_set()
        outcome["returncode"] = process.returncode
        outcome["terminated_after_terminal"] = terminated_after_terminal

    while not output_queue.empty():
        try:
            line = output_queue.get_nowait()
            if line is not None:
                yield line
        except queue.Empty:
            break


def _json_type_is(line: str, *types: str) -> bool:
    try:
        return json.loads(line).get("type") in types
    except (json.JSONDecodeError, AttributeError, TypeError):
        return False


def _collect(cmd: List[str], *, cwd: Optional[str], env_extra: Optional[dict],
             proxy: Optional[str], is_done: Callable[[str], bool],
             on_event: Callable[[dict, dict], None], tag: str,
             env_force: Optional[dict] = None) -> dict:
    """Stream a backend process and reduce its events into the unified contract."""
    state = {"all": [], "text": "", "fallback": "", "session": None,
             "err": "", "has_error": False, "diagnostic_tail": deque(maxlen=20),
             "effective_permission_mode": None, "permission_denials": None}
    process_outcome: dict = {}
    status = MEMBER_STATUS.setdefault(tag, {"events": 0, "done": False})
    _progress(f"{tag}: started — `{cmd[0]}`")
    try:
        for line in run_stream(cmd, cwd=cwd, env_extra=env_extra, env_force=env_force,
                               proxy=proxy, is_done=is_done, outcome=process_outcome):
            try:
                event = json.loads(line.strip())
            except json.JSONDecodeError:
                if line:
                    state["diagnostic_tail"].append(line[:1000])
                continue
            status["events"] += 1
            if status["events"] == 1:
                _progress(f"{tag}: first event received")
            state["all"].append(event)
            try:
                on_event(event, state)
            except Exception as error:
                state["err"] += f"\n\n[unexpected error] {error}. Line: {line!r}"
                state["has_error"] = True
    except FileNotFoundError as error:
        status["done"] = True
        _progress(f"{tag}: failed — executable not found")
        return {"success": False, "error": f"[{tag}] executable not found: {error}"}

    text = state["text"] or state["fallback"]
    terminal_seen = process_outcome.get("terminal_seen", False)
    returncode = process_outcome.get("returncode")
    if not terminal_seen:
        state["err"] += f"\n\n[{tag} error] stream ended without a terminal event"
    if returncode not in (None, 0):
        state["has_error"] = True
        state["err"] += f"\n\n[{tag} error] process exited with status {returncode}"
    if process_outcome.get("terminated_after_terminal"):
        state["err"] += f"\n\n[{tag} error] process did not exit within the terminal grace period"
        state["has_error"] = True
    success = terminal_seen and not state["has_error"] and returncode == 0
    status["done"] = True
    _progress(f"{tag}: done — success={success}, events={status['events']}, "
              f"reply={len(text)} chars")
    result = {"success": success}
    if state["session"]:
        result["SESSION_ID"] = state["session"]
    if text:
        result["agent_messages"] = text
    if state["err"]:
        result["error"] = state["err"].strip()
    if not success and state["diagnostic_tail"]:
        result["diagnostic_tail"] = list(state["diagnostic_tail"])
    if state["effective_permission_mode"]:
        result["effective_permission_mode"] = state["effective_permission_mode"]
    if state["permission_denials"]:
        result["permission_denials"] = state["permission_denials"]
    result["_all_messages"] = state["all"]
    return result


# ------------------------------------------------------------------ members

def _proxy_for(member: str, args) -> str:
    if args.proxy:
        return args.proxy
    if env_proxy := os.environ.get("COUNCIL_PROXY"):
        return env_proxy
    # codex/gemini: pin Surge because the inherited host proxy can break
    # Codex egress; gemini is pinned for parity. Other members inherit it.
    return SURGE_PROXY if member in ("codex", "gemini") else ""


def _run_codex_exec(args, prompt: str) -> dict:
    cmd = ["codex", "exec", "--sandbox", _codex_sandbox(args), "--cd", args.cd,
           "--json", "--skip-git-repo-check"]
    cmd.extend(["--config", f"model_reasoning_effort={json.dumps(args.codex_effort)}"])
    cmd.extend(["--config", f"approval_policy={json.dumps('never')}"])
    if args.image:
        cmd.extend(["--image", ",".join(args.image)])
    if args.model:
        cmd.extend(["--model", args.model])
    if args.SESSION_ID:
        cmd.extend(["resume", args.SESSION_ID])
    cmd += ["--", prompt]

    def on_event(event: dict, state: dict) -> None:
        item = event.get("item", {})
        if item.get("type") == "agent_message":
            state["text"] += item.get("text", "")
        elif item.get("type") == "message":
            state["fallback"] += item.get("text", "") or event.get("content", "")
        if event.get("thread_id") is not None:
            state["session"] = event.get("thread_id")
        etype = event.get("type", "")
        if etype == "turn.failed":
            state["has_error"] = True
            state["err"] += "\n\n[codex error] " + event.get("error", {}).get("message", "")
        if etype == "error":
            message = event.get("message", "")
            if not re.match(r"^Reconnecting\.\.\.\s+\d+/\d+$", message):
                state["has_error"] = True
                state["err"] += "\n\n[codex error] " + message

    return _collect(cmd, cwd=None, env_extra=None, proxy=_proxy_for("codex", args),
                    is_done=lambda l: _json_type_is(l, "turn.completed", "turn.failed"),
                    on_event=on_event, tag="codex")


def _codex_input(args, prompt: str) -> list[dict]:
    inputs = [{"type": "text", "text": prompt}]
    for group in args.image:
        inputs.extend({"type": "localImage", "path": path.strip()}
                      for path in group.split(",") if path.strip())
    return inputs


class _CodexAppServer:
    """Minimal synchronous JSON-RPC client for one Codex app-server turn."""

    def __init__(self, args) -> None:
        self.args = args
        self.process: Optional[subprocess.Popen[str]] = None
        self.next_id = 1
        self.pending = deque()
        self.events: list[dict] = []
        self.stderr_lines = deque(maxlen=100)
        self.stderr_thread: Optional[threading.Thread] = None
        self.status = MEMBER_STATUS.setdefault("codex", {"events": 0, "done": False})

    def start(self) -> None:
        env = os.environ.copy()
        _apply_proxy(env, _proxy_for("codex", self.args))
        cmd = [_resolve_executable("codex", env), "app-server", "--listen", "stdio://"]
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=self.args.cd,
            env=env,
            bufsize=1,
        )
        self.stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self.stderr_thread.start()
        self.request("initialize", {
            "clientInfo": {
                "name": "council",
                "title": "Council CLI Delegate",
                "version": "1",
            },
            "capabilities": {"experimentalApi": True},
        })
        self.notify("initialized")

    def _drain_stderr(self) -> None:
        if self.process is None or self.process.stderr is None:
            return
        for line in self.process.stderr:
            self.stderr_lines.append(line.rstrip())

    def _write(self, message: dict) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("codex app-server stdin is unavailable")
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _read(self) -> dict:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("codex app-server stdout is unavailable")
        while True:
            line = self.process.stdout.readline()
            if not line:
                details = "\n".join(self.stderr_lines)
                raise RuntimeError(f"codex app-server closed unexpectedly{': ' + details if details else ''}")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.events.append(message)
            self.status["events"] += 1
            if self.status["events"] == 1:
                _progress("codex: first event received — app-server initialized")
            if "id" in message and "method" in message:
                # Deny interactive server→client requests; council is
                # headless. Write mode still uses non-interactive approval.
                self._write({
                    "id": message["id"],
                    "error": {
                        "code": -32601,
                        "message": (
                            "Council denies interactive server requests "
                            "(headless; approvalPolicy=never)."
                        ),
                    },
                })
                continue
            return message

    def request(self, method: str, params: Optional[dict] = None) -> dict:
        request_id = self.next_id
        self.next_id += 1
        message = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)
        while True:
            response = self._read()
            if response.get("id") == request_id:
                if response.get("error") is not None:
                    raise RuntimeError(f"{method}: {response['error']}")
                result = response.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError(f"{method}: expected an object result")
                return result
            if response.get("method"):
                self.pending.append(response)

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        message = {"method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def next_notification(self) -> dict:
        while True:
            message = self.pending.popleft() if self.pending else self._read()
            if message.get("method"):
                return message

    def close(self) -> None:
        if self.process is None:
            return
        process = self.process
        self.process = None
        if process.stdin:
            process.stdin.close()
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            process.kill()
            process.wait()
        if self.stderr_thread and self.stderr_thread.is_alive():
            self.stderr_thread.join(timeout=0.5)


def _codex_thread_params(args) -> dict:
    params = {
        "approvalPolicy": "never",
        "config": {"model_reasoning_effort": args.codex_effort},
        "cwd": args.cd,
        "sandbox": _codex_sandbox(args),
    }
    if args.model:
        params["model"] = args.model
    return params


def _codex_turn_result(server: _CodexAppServer, thread_id: str,
                       turn_id: str) -> dict:
    items = []
    completed_turn = None
    while completed_turn is None:
        event = server.next_notification()
        method = event.get("method")
        params = event.get("params") or {}
        if method == "item/completed" and params.get("turnId") == turn_id:
            item = params.get("item")
            if isinstance(item, dict):
                items.append(item)
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            if turn.get("id") == turn_id:
                completed_turn = turn

    if not items:
        items = [item for item in completed_turn.get("items", []) if isinstance(item, dict)]
    final = None
    fallback = None
    for item in reversed(items):
        if item.get("type") != "agentMessage":
            continue
        if item.get("phase") == "final_answer":
            final = item.get("text", "")
            break
        if item.get("phase") is None and fallback is None:
            fallback = item.get("text", "")
    text = final if final is not None else (fallback or "")
    error = completed_turn.get("error")
    status = completed_turn.get("status")
    success = status == "completed" and error is None
    result = {
        "success": success and (bool(text) or bool(thread_id)),
        "SESSION_ID": thread_id,
        "_all_messages": server.events,
    }
    if text:
        result["agent_messages"] = text
    if error is not None:
        result["error"] = f"[codex app-server error] {error}"
    elif not success:
        result["error"] = f"[codex app-server error] turn status: {status or 'missing'}"
    return result


def run_codex(args, prompt: str) -> dict:
    """Run Codex through app-server JSON-RPC, with exec as a startup fallback."""
    if args.codex_backend == "exec":
        return _run_codex_exec(args, prompt)

    status = MEMBER_STATUS.setdefault("codex", {"events": 0, "done": False})
    _progress("codex: started — `codex app-server` JSON-RPC")
    server = _CodexAppServer(args)
    try:
        server.start()
        thread_params = _codex_thread_params(args)
        if args.SESSION_ID:
            thread_params["threadId"] = args.SESSION_ID
            thread_result = server.request("thread/resume", thread_params)
        else:
            thread_result = server.request("thread/start", thread_params)
        thread_id = thread_result["thread"]["id"]
    except Exception as error:
        server.close()
        _progress(f"codex: app-server startup failed ({error}); falling back to `codex exec`")
        return _run_codex_exec(args, prompt)

    result = None
    try:
        turn_params = {
            "approvalPolicy": "never",
            "cwd": args.cd,
            "effort": args.codex_effort,
            "input": _codex_input(args, prompt),
            "threadId": thread_id,
        }
        if args.model:
            turn_params["model"] = args.model
        turn_result = server.request("turn/start", turn_params)
        turn_id = turn_result["turn"]["id"]
        result = _codex_turn_result(server, thread_id, turn_id)
        return result
    except Exception as error:
        result = {
            "success": False,
            "SESSION_ID": thread_id,
            "error": f"[codex app-server error] {type(error).__name__}: {error}",
            "_all_messages": server.events,
        }
        return result
    finally:
        server.close()
        status["done"] = True
        reply_len = len(result.get("agent_messages", "")) if result else 0
        _progress(f"codex: done — success={result.get('success', False) if result else False}, "
                  f"events={status['events']}, reply={reply_len} chars")


def run_gemini(args, prompt: str) -> dict:
    cmd = ["gemini", "--prompt", prompt, "-o", "stream-json",
           "--approval-mode", _gemini_approval_mode(args)]
    if not _write_enabled(args):
        cmd.extend(["--policy", str(Path(__file__).resolve().parent.parent / "references" / "gemini-read-only.toml")])
    if args.model:
        cmd.extend(["--model", args.model])
    if args.SESSION_ID:
        cmd.extend(["--resume", args.SESSION_ID])

    def on_event(event: dict, state: dict) -> None:
        if event.get("type") == "message" and event.get("role") == "assistant":
            content = event.get("content", "")
            if "The --prompt (-p) flag has been deprecated" not in content:
                state["text"] += content
        if event.get("session_id") is not None:
            state["session"] = event.get("session_id")
        if event.get("type") == "result" and event.get("status") == "error":
            state["has_error"] = True
            error = event.get("error") or {}
            message = error.get("message", "") if isinstance(error, dict) else str(error)
            state["err"] += "\n\n[gemini error] " + message

    # GEMINI_CLI_TRUST_WORKSPACE lets gemini run in any --cd directory
    # (mirrors codex's --skip-git-repo-check; otherwise exit 55).
    return _collect(cmd, cwd=args.cd,
                    env_extra={"GEMINI_CLI_TRUST_WORKSPACE": "true"},
                    proxy=_proxy_for("gemini", args),
                    is_done=lambda l: _json_type_is(l, "result"),
                    on_event=on_event, tag="gemini")


def run_claude(args, prompt: str) -> dict:
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
           "--permission-mode", _claude_permission_mode(args),
           "--permission-prompts", "none",
           "--effort", _claude_effort(args)]
    if not _write_enabled(args):
        cmd.extend(["--tools", CLAUDE_TOOLS_READ, "--strict-mcp-config"])
    if _claude_permission_mode(args) != "plan":
        cmd.extend(["--disallowedTools", CLAUDE_NO_PLAN_TOOLS])
    if settings := _claude_settings(args):
        cmd.extend(["--settings", settings])
    if args.model:
        cmd.extend(["--model", args.model])
    if args.SESSION_ID:
        cmd.extend(["--resume", args.SESSION_ID])
    cmd.extend(["--", prompt])

    def on_event(event: dict, state: dict) -> None:
        if event.get("session_id"):
            state["session"] = event["session_id"]
        permission_mode = event.get("permissionMode") or event.get("permission_mode")
        if isinstance(permission_mode, str):
            state["effective_permission_mode"] = permission_mode
        etype = event.get("type", "")
        if etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    state["fallback"] += block.get("text", "")
        elif etype == "result":
            if event.get("permission_denials"):
                state["permission_denials"] = event["permission_denials"]
            if event.get("is_error") or event.get("subtype") != "success":
                state["has_error"] = True
                state["err"] += "\n\n[claude error] " + str(
                    event.get("result") or event.get("subtype", ""))
            else:
                state["text"] = event.get("result") or state["text"]

    result = _collect(cmd, cwd=args.cd, env_extra=None,
                      proxy=_proxy_for("claude", args),
                      is_done=lambda l: _json_type_is(l, "result"),
                      on_event=on_event, tag="claude")
    return _attach_claude_receipt(args, result)


def run_grok(args, prompt: str) -> dict:
    """Grok Build headless: `grok -p` + streaming-json."""
    cmd = [
        "grok",
        "-p", prompt,
        "--output-format", "streaming-json",
        "--permission-mode", _grok_permission_mode(args),
        "--cwd", args.cd,
        "--no-subagents",
        "--no-plan",
    ]
    if not _write_enabled(args):
        cmd.extend(["--tools", GROK_TOOLS_READ])
    if args.model:
        cmd.extend(["--model", args.model])
    if getattr(args, "grok_effort", ""):
        cmd.extend(["--reasoning-effort", args.grok_effort])
    if args.SESSION_ID:
        cmd.extend(["--resume", args.SESSION_ID])

    def on_event(event: dict, state: dict) -> None:
        etype = event.get("type", "")
        if etype == "text":
            state["text"] += event.get("data", "") or ""
        elif etype == "end":
            if event.get("sessionId"):
                state["session"] = event["sessionId"]
            if event.get("stopReason") != "end_turn":
                state["has_error"] = True
                state["err"] += "\n\n[grok error] stop reason: " + str(event.get("stopReason"))
        elif etype == "error":
            state["has_error"] = True
            state["err"] += "\n\n[grok error] " + str(
                event.get("message") or event.get("data") or event)

    return _collect(
        cmd, cwd=args.cd, env_extra=None, proxy=_proxy_for("grok", args),
        is_done=lambda l: _json_type_is(l, "end", "error"),
        on_event=on_event, tag="grok",
    )


def run_pi(args, prompt: str) -> dict:
    """Pi coding agent headless: `pi -p --mode json`."""
    cmd = [
        "pi",
        "-p",
        "--mode", "json",
        "--no-extensions",
    ]
    if not _write_enabled(args):
        cmd.append("--no-mcp")
    if getattr(args, "pi_thinking", ""):
        cmd.extend(["--thinking", args.pi_thinking])
    tools = _pi_tools_arg(args)
    if tools:
        cmd.extend(["--tools", tools])
    if args.model:
        cmd.extend(["--model", args.model])
    if args.SESSION_ID:
        # Prefer exact session file/id resume over interactive --resume picker.
        cmd.extend(["--session", args.SESSION_ID])
    cmd.append(prompt)

    def on_event(event: dict, state: dict) -> None:
        etype = event.get("type", "")
        if etype == "session" and event.get("id"):
            state["session"] = event["id"]
        elif etype == "message_end":
            msg = event.get("message") or {}
            if msg.get("role") != "assistant":
                return
            texts = []
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", "") or "")
            chunk = "".join(texts)
            stop_reason = event.get("stopReason") or msg.get("stopReason")
            # A failed assistant attempt can be followed by a successful retry.
            # Only its latest outcome is authoritative once the run settles.
            state["pi_last_error"] = (
                event.get("errorMessage") or msg.get("errorMessage") or stop_reason
            ) if stop_reason in ("error", "aborted") else None
            if state["pi_last_error"]:
                state["pi_pending_failed_text"] = chunk
            else:
                state["pi_pending_failed_text"] = ""
                if chunk:
                    state["text"] += ("\n" if state["text"] else "") + chunk
        elif etype == "agent_settled":
            message = "run aborted" if event.get("aborted") else state.get("pi_last_error")
            if message:
                state["has_error"] = True
                state["err"] += "\n\n[pi error] " + str(message)
                if chunk := state.get("pi_pending_failed_text"):
                    state["text"] += ("\n" if state["text"] else "") + chunk
        elif etype == "error":
            state["has_error"] = True
            state["err"] += "\n\n[pi error] " + str(
                event.get("message") or event.get("error") or event)

    return _collect(
        cmd, cwd=args.cd, env_extra=None, proxy=_proxy_for("pi", args),
        is_done=lambda l: _json_type_is(l, "agent_settled"),
        on_event=on_event, tag="pi",
    )


def _opencode_done(line: str) -> bool:
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, AttributeError, TypeError):
        return False
    if not isinstance(event, dict):
        return False
    if event.get("type") == "error":
        return True
    if event.get("type") != "step_finish":
        return False
    part = event.get("part")
    return isinstance(part, dict) and part.get("reason") == "stop"


def _opencode_error_message(event: dict) -> str:
    message = event.get("message") or event.get("error") or ""
    if isinstance(message, dict):
        message = message.get("message") or message.get("error") or ""
    if not message and isinstance(event.get("part"), dict):
        part = event["part"]
        message = part.get("text") or part.get("message") or part.get("error") or ""
    return str(message or event)


def run_opencode(args, prompt: str) -> dict:
    """OpenCode headless: `opencode run --format json`."""
    cmd = ["opencode", "run", "--format", "json", "--dir", args.cd,
           "--agent", _opencode_agent(args)]
    if args.model:
        cmd.extend(["-m", args.model])
    if args.SESSION_ID:
        cmd.extend(["-s", args.SESSION_ID])
    cmd.extend(["--", prompt])
    env_force = None
    if not _write_enabled(args):
        env_force = {"OPENCODE_CONFIG_CONTENT": OPENCODE_READ_ONLY_CONFIG}

    def on_event(event: dict, state: dict) -> None:
        if event.get("sessionID"):
            state["session"] = event["sessionID"]
        etype = event.get("type", "")
        if etype == "text":
            part = event.get("part")
            if isinstance(part, dict) and part.get("text"):
                state["text"] += part["text"]
        elif etype == "error":
            state["has_error"] = True
            state["err"] += "\n\n[opencode error] " + _opencode_error_message(event)

    return _collect(
        cmd, cwd=args.cd, env_extra=None, env_force=env_force,
        proxy=_proxy_for("opencode", args), is_done=_opencode_done,
        on_event=on_event, tag="opencode",
    )


def _merge_dsh_text(notes: str, final: str) -> str:
    """Use final.text as the answer. Assistant text fills an empty final."""
    if final:
        return final
    return notes


def run_dsh(args, prompt: str) -> dict:
    """DeepSeek Harness headless: `dsh --profile headless --json`."""
    cmd = ["dsh", "--profile", "headless", "--json"]
    if args.SESSION_ID:
        cmd.extend(["--session-id", args.SESSION_ID])
    cmd.append(prompt)

    def on_event(event: dict, state: dict) -> None:
        etype = event.get("type", "")
        if etype == "session" and event.get("sessionId"):
            state["session"] = event["sessionId"]
        elif etype == "text" and event.get("text"):
            state["dsh_text"] = state.get("dsh_text", "") + event["text"]
        elif etype == "status" and event.get("phase") == "turn_end":
            reason = event.get("reason") or {}
            if isinstance(reason, dict) and reason.get("kind") == "error":
                error = reason.get("error") or {}
                if not isinstance(error, dict):
                    error = {"message": str(error)}
                message = str(error.get("message") or "")
                code = str(error.get("code") or "")
                detail = f"{message} ({code})" if message and code else (message or code)
                state["has_error"] = True
                state["err"] += "\n\n[dsh error] " + detail
        elif etype == "final":
            state["text"] = _merge_dsh_text(state.get("dsh_text", ""), event.get("text") or "")
        elif etype == "error":
            state["has_error"] = True
            state["err"] += "\n\n[dsh error] " + str(event.get("message") or event)

    return _collect(
        cmd, cwd=args.cd, env_extra=None,
        env_force={"DSH_PERMISSION_MODE": _dsh_permission_mode(args)},
        proxy=_proxy_for("dsh", args),
        is_done=lambda l: _json_type_is(l, "final", "error"),
        on_event=on_event, tag="dsh",
    )


RUNNERS = {
    "codex": run_codex,
    "gemini": run_gemini,
    "claude": run_claude,
    "grok": run_grok,
    "pi": run_pi,
    "opencode": run_opencode,
    "dsh": run_dsh,
}


# ------------------------------------------------------------------ entry

def _usage_error(message: str) -> None:
    print(json.dumps({"success": False, "error": message}, indent=2,
                     ensure_ascii=False))
    sys.exit(2)


_SESSION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _sessions_path() -> Path:
    override = os.environ.get("COUNCIL_SESSIONS_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".ai" / "skills" / "council" / "state" / "sessions.json"


def _absolute_cwd(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def _load_sessions() -> dict:
    path = _sessions_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        _usage_error(f"session ledger `{path}` is unreadable: {error}")
    if not isinstance(data, dict):
        _usage_error(f"session ledger `{path}` must be a JSON object")
    return data


def _save_sessions(ledger: dict) -> None:
    """Replace the ledger via a temp file in the same directory. Single-writer."""
    path = _sessions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(ledger, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _validate_session_name(name: str) -> str:
    cleaned = name.strip()
    if not _SESSION_NAME.fullmatch(cleaned):
        _usage_error(
            "--session name must be a short token such as parser-review "
            "(letters, digits, '.', '_' or '-')"
        )
    return cleaned


def _stored_session_id(name: str, member: str, cwd: str) -> str:
    """Return the stored id when member and cwd match. Empty if the name is new."""
    entry = _load_sessions().get(name)
    if entry is None:
        return ""
    if not isinstance(entry, dict):
        _usage_error(
            f"session `{name}` is malformed; pick another name or pass "
            f"--session-forget {name} first"
        )
    bound_cwd = entry.get("cwd")
    same_cwd = False
    if isinstance(bound_cwd, str) and bound_cwd:
        try:
            same_cwd = _absolute_cwd(bound_cwd) == cwd
        except OSError:
            same_cwd = False
    if entry.get("member") != member or not same_cwd:
        _usage_error(
            f"session `{name}` is bound to member={entry.get('member')} "
            f"cwd={entry.get('cwd')}; this call is member={member} cwd={cwd}. "
            f"Pick another name or pass --session-forget {name} first."
        )
    session_id = entry.get("SESSION_ID") or ""
    if not isinstance(session_id, str):
        _usage_error(
            f"session `{name}` has no string SESSION_ID; pick another name or "
            f"pass --session-forget {name} first"
        )
    return session_id


def _upsert_session(name: str, member: str, cwd: str, session_id: str) -> None:
    ledger = _load_sessions()
    ledger[name] = {
        "member": member,
        "cwd": cwd,
        "SESSION_ID": session_id,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _save_sessions(ledger)


def _forget_session(name: str) -> bool:
    path = _sessions_path()
    if not path.exists():
        return False
    ledger = _load_sessions()
    if name not in ledger:
        return False
    del ledger[name]
    _save_sessions(ledger)
    return True


def _record_successful_session(name: str, member: str, cwd: str, result: dict) -> None:
    if not name or not result.get("success"):
        return
    session_id = result.get("SESSION_ID") or ""
    if not isinstance(session_id, str) or not session_id:
        return
    _upsert_session(name, member, cwd, session_id)


def _finish_session_admin(args) -> None:
    """Print or delete ledger entries and return. Does not run a member."""
    listing = bool(getattr(args, "session_list", False))
    forget = (getattr(args, "session_forget", "") or "").strip()
    named = (getattr(args, "session", "") or "").strip()
    if listing and forget:
        _usage_error("pass only one of --session-list or --session-forget")
    if named and (listing or forget):
        _usage_error(
            "--session cannot be combined with --session-list or --session-forget"
        )
    if listing:
        print(json.dumps(_load_sessions(), indent=2, ensure_ascii=False))
        return
    if forget:
        _forget_session(forget)
        return
    _usage_error("expected --session-list or --session-forget")


def _wants_session_admin(argv: list[str]) -> bool:
    for token in argv:
        if token == "--session-list" or token.startswith("--session-list="):
            return True
        if token == "--session-forget" or token.startswith("--session-forget="):
            return True
    return False


def _run_session_admin(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--session", default="")
    parser.add_argument("--session-list", action="store_true")
    parser.add_argument("--session-forget", default="")
    args, _unknown = parser.parse_known_args(argv)
    _finish_session_admin(args)


def main():
    configure_windows_stdio()
    # --PROMPT/--cd stay required for a member run. List and forget exit first.
    if "-h" not in sys.argv[1:] and "--help" not in sys.argv[1:]:
        if _wants_session_admin(sys.argv[1:]):
            _run_session_admin(sys.argv[1:])
            return
    parser = argparse.ArgumentParser(
        description=(
            "Council — multi-model bridge (default read-only; optional "
            "scoped write when user plain-text authorizes)"
        ))
    parser.add_argument("--with", dest="members", default="codex",
                        help="Comma-separated members: codex,gemini,claude,grok,pi,opencode,dsh. Pass EXACTLY the members the user named — naming replaces the default, never extends it; codex is only the default when no member is named. Multiple members run in parallel.")
    parser.add_argument("--PROMPT", required=True,
                        help="Instruction sent to every selected member.")
    parser.add_argument("--cd", required=True,
                        help="Workspace root for the discussion.")
    parser.add_argument("--SESSION_ID", default="",
                        help="Resume a previous session. Requires exactly one member (sessions are per-member).")
    parser.add_argument(
        "--session", default="", metavar="NAME",
        help=(
            "Named session to resume or record. Requires exactly one member. "
            "Reuses the ledger SESSION_ID when member and cwd match. "
            "An explicit --SESSION_ID overrides the stored id for this turn."
        ),
    )
    parser.add_argument(
        "--session-list", action="store_true",
        help="Print the named-session ledger JSON and exit without running a member.",
    )
    parser.add_argument(
        "--session-forget", default="", metavar="NAME",
        help=(
            "Delete one named session and exit without running a member. "
            "A missing name is not an error."
        ),
    )
    parser.add_argument("--model", default="",
                        help="Model override. Requires exactly one member. Not supported for dsh. Strictly prohibited unless explicitly specified by the user.")
    parser.add_argument("--codex-effort", default="ultra",
                        help="Codex reasoning effort (default: ultra). Passed through app-server thread/turn parameters or exec --config fallback.")
    parser.add_argument("--codex-backend", default="app-server",
                        choices=["app-server", "exec"],
                        help="Codex invocation backend (default: app-server). exec preserves the legacy JSONL path.")
    parser.add_argument("--claude-effort", default=None,
                        help="Claude effort level. Defaults to max for haiku, otherwise medium. Passes through Claude's --effort option.")
    parser.add_argument(
        "--claude-permission-mode", choices=CLAUDE_PERM_OVERRIDES,
        default="",
        help=(
            "Claude permission override (auto, acceptEdits, or plan). Default: "
            "auto. acceptEdits requires --allow-write; plan is read-only only."
        ),
    )
    parser.add_argument("--grok-effort", default="high",
                        help="Grok reasoning effort (default: high). Passed as --reasoning-effort.")
    parser.add_argument("--pi-thinking", default="off",
                        help="Pi thinking level (default: off). Passed as --thinking.")
    parser.add_argument("--image", action="append", default=[],
                        help="Attach image files (codex only). Repeat the flag or separate paths with commas.")
    parser.add_argument("--proxy", default="",
                        help="Proxy override for all selected members ('none' for direct). Defaults: codex/gemini pin Surge http://127.0.0.1:6152; claude, grok, pi, opencode and dsh inherit the environment. Also settable via COUNCIL_PROXY env.")
    parser.add_argument("--return-all-messages", action="store_true",
                        help="Include each member's full app-server or CLI event stream.")
    parser.add_argument(
        "--allow-write", action="store_true", default=False,
        help=(
            "Enable scoped file writes for this invocation. Orchestrator may "
            "set this ONLY after the user plain-text authorized council to "
            "modify files. Requires --write-scope. Default: read-only."
        ),
    )
    parser.add_argument(
        "--write-scope", default="",
        help=(
            "Authorized change set for --allow-write (no separate grant file). "
            "Free text, preferred lines: `path | line-range-or-region | logic`. "
            "Required with --allow-write; forbidden without it. "
            "Included in the prompt; the member CLI may retain it."
        ),
    )

    args = parser.parse_args()

    members = list(dict.fromkeys(
        m.strip() for m in args.members.split(",") if m.strip()))
    if not members:
        _usage_error("--with selected no members")
    if unknown := [m for m in members if m not in RUNNERS]:
        _usage_error(
            f"unknown member(s): {', '.join(unknown)}; "
            f"valid: {', '.join(RUNNERS)}"
        )
    if len(members) > 1 and args.SESSION_ID:
        _usage_error("--SESSION_ID requires exactly one member (sessions are per-member)")
    session_name = ""
    session_cwd = ""
    if args.session:
        session_name = _validate_session_name(args.session)
        if len(members) != 1:
            _usage_error("--session requires exactly one member (sessions are per-member)")
    if args.session_list or args.session_forget:
        _finish_session_admin(args)
        return
    if len(members) > 1 and args.model:
        _usage_error("--model requires exactly one member (model ids are per-member)")
    if args.model and "dsh" in members:
        _usage_error(
            "--model is not supported for dsh; `dsh --profile headless` has no model flag"
        )
    if args.image and members != ["codex"]:
        _usage_error("--image is supported by codex only (use --with codex)")
    if args.claude_permission_mode and "claude" not in members:
        _usage_error("--claude-permission-mode requires --with including claude")
    if args.claude_permission_mode == "acceptEdits" and not args.allow_write:
        _usage_error("--claude-permission-mode acceptEdits requires --allow-write")
    if args.claude_permission_mode == "plan" and args.allow_write:
        _usage_error("--claude-permission-mode plan is incompatible with --allow-write")
    if not Path(args.cd).exists():
        _usage_error(f"workspace root `{Path(args.cd).absolute()}` does not exist")

    write_scope = (args.write_scope or "").strip()
    if args.allow_write and not write_scope:
        _usage_error(
            "--allow-write requires --write-scope with the authorized change "
            "set (file | lines/region | logic). Scope is not saved to disk."
        )
    if write_scope and not args.allow_write:
        _usage_error(
            "--write-scope was provided without --allow-write; refuse to "
            "imply write authority. Pass both flags after plain-text user "
            "authorization, or omit --write-scope for read-only."
        )
    if args.allow_write and len(write_scope) < 12:
        _usage_error(
            "--write-scope is too short; list concrete targets "
            "(path | lines/region | intended logic)."
        )
    args.write_scope = write_scope

    if session_name:
        session_cwd = _absolute_cwd(args.cd)
        stored = _stored_session_id(session_name, members[0], session_cwd)
        if not (args.SESSION_ID or "").strip() and stored:
            args.SESSION_ID = stored

    prompt = _compose_prompt(args, args.PROMPT)
    mode = "write-scoped" if args.allow_write else "read-only"
    _progress(
        f"permission mode={mode} "
        f"codex_sandbox={_codex_sandbox(args)} "
        f"gemini_approval={_gemini_approval_mode(args)} "
        f"claude_perm={_claude_permission_mode(args)} "
        f"grok_perm={_grok_permission_mode(args)} "
        f"pi_tools={_pi_tools_arg(args) or 'all'} "
        f"opencode_agent={_opencode_agent(args)} "
        f"dsh_perm={_dsh_permission_mode(args)}"
    )

    heartbeat_stop = threading.Event()

    def heartbeat() -> None:
        while not heartbeat_stop.wait(15):
            running = [m for m in members
                       if not MEMBER_STATUS.get(m, {}).get("done")]
            if not running:
                return
            parts = []
            for m in running:
                events = MEMBER_STATUS.get(m, {}).get("events", 0)
                stuck = (" — WARNING: no events yet, backend may be stuck "
                         "(check proxy/auth)"
                         if events == 0 and time.time() - _START > 60 else "")
                parts.append(f"{m}: running, {events} events{stuck}")
            _progress("; ".join(parts))

    # Member-level concurrency: each selected CLI is an independent subprocess
    # (cross-CLI "subagent"). Threads only orchestrate I/O wait; work happens
    # in separate processes (codex / gemini / claude). max_workers == N members
    # so all named members start immediately, not sequentially.
    _progress(f"dispatch parallel members={','.join(members)} "
              f"workers={len(members)}")
    threading.Thread(target=heartbeat, daemon=True).start()
    results: dict = {}
    with ThreadPoolExecutor(max_workers=len(members)) as pool:
        futures = {pool.submit(RUNNERS[m], args, prompt): m for m in members}
        for future in as_completed(futures):
            member = futures[future]
            try:
                results[member] = future.result()
            except Exception as error:
                # One member hard-crash must not drop sibling answers.
                MEMBER_STATUS.setdefault(member, {})["done"] = True
                _progress(f"{member}: crashed — {type(error).__name__}: {error}")
                results[member] = {
                    "success": False,
                    "error": f"[{member}] unhandled {type(error).__name__}: {error}",
                }
    heartbeat_stop.set()
    # Preserve caller-facing member order (dict.fromkeys order of --with).
    results = {m: results[m] for m in members if m in results}

    for member_result in results.values():
        if args.return_all_messages:
            if "_all_messages" in member_result:
                member_result["all_messages"] = member_result.pop("_all_messages")
        else:
            member_result.pop("_all_messages", None)

    failed_members = [member for member, result in results.items()
                      if not result.get("success")]
    if session_name:
        _record_successful_session(
            session_name, members[0], session_cwd, results.get(members[0], {}),
        )
    print(json.dumps({
        "success": any(r.get("success") for r in results.values()),
        "all_succeeded": not failed_members,
        "failed_members": failed_members,
        "results": results,
        "permission": {
            "mode": mode,
            "allow_write": bool(args.allow_write),
            # Scope is returned for the orchestrator audit trail only; council
            # never writes it to a file.
            "write_scope": write_scope if args.allow_write else None,
            "codex_sandbox": _codex_sandbox(args),
            "gemini_approval_mode": _gemini_approval_mode(args),
            "claude_permission_mode": _claude_permission_mode(args),
            "grok_permission_mode": _grok_permission_mode(args),
            "pi_tools": _pi_tools_arg(args) or "all",
            "opencode_agent": _opencode_agent(args),
            "dsh_permission_mode": _dsh_permission_mode(args),
        },
        "concurrency": {
            "mode": "threadpool-process-fanout",
            "members": members,
            "max_workers": len(members),
            "note": ("Members run as concurrent OS processes under a "
                     "ThreadPoolExecutor; not sequential CLI calls."),
        },
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
