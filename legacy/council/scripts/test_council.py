import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import council


def _args(**overrides):
    values = {
        "allow_write": False, "write_scope": "", "claude_permission_mode": "",
        "claude_effort": None, "model": "", "SESSION_ID": "", "cd": "/tmp",
        "image": [], "codex_effort": "ultra", "proxy": "none",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class PromptConstraintTests(unittest.TestCase):
    def test_task_keywords_do_not_skip_read_only_constraint(self):
        args = _args()
        for task in ["Review parser boundaries", "Explain the read-only cache", "Prohibit duplicate tokens"]:
            with self.subTest(task=task):
                prompt = council._compose_prompt(args, task)
                self.assertTrue(prompt.startswith(task))
                self.assertTrue(prompt.endswith(council.NO_WRITE_GUARD))

    def test_composing_again_does_not_duplicate_constraint(self):
        args = _args()
        prompt = council._compose_prompt(args, "Review parser boundaries")
        self.assertEqual(council._compose_prompt(args, prompt), prompt)

    def test_write_mode_includes_authorized_scope_without_read_only_constraint(self):
        scope = "src/parser.py | tokenize | fix escaped quotes"
        args = _args(allow_write=True, write_scope=scope)
        prompt = council._compose_prompt(args, "Implement the parser fix")
        self.assertIn(scope, prompt)
        self.assertNotIn(council.NO_WRITE_GUARD, prompt)


class ClaudeCommandTests(unittest.TestCase):
    def _command_for(self, args):
        captured = {}

        def collect(cmd, **kwargs):
            captured["cmd"] = cmd
            return {"success": True}

        with patch.object(council, "_collect", side_effect=collect):
            council.run_claude(args, "Review the change")
        return captured["cmd"]

    def test_read_only_claude_defaults_to_auto_and_disables_prompts(self):
        cmd = self._command_for(_args())
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "auto")
        self.assertEqual(cmd[cmd.index("--permission-prompts") + 1], "none")
        self.assertEqual(cmd[cmd.index("--tools") + 1], council.CLAUDE_TOOLS_READ)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1], council.CLAUDE_NO_PLAN_TOOLS)
        self.assertNotIn("--resume", cmd)

    def test_claude_effort_defaults_preserve_explicit_sonnet_and_custom_models(self):
        expected = {
            "": "medium", "haiku": "max", "opus": "medium",
            "claude-opus-4-6": "medium", "default": "medium",
            "opusplan": "medium", "opus[1m]": "medium", "opusplan[1m]": "medium",
            "sonnet": "max", "my-custom-model": "max",
        }
        for model, effort in expected.items():
            with self.subTest(model=model):
                cmd = self._command_for(_args(model=model))
                self.assertEqual(cmd[cmd.index("--effort") + 1], effort)
                if model != "haiku":
                    self.assertNotIn("--settings", cmd)

    def test_claude_prompt_is_separated_from_variadic_tool_options(self):
        for allow_write in (False, True):
            with self.subTest(allow_write=allow_write):
                cmd = self._command_for(_args(allow_write=allow_write))
                self.assertEqual(cmd[-2:], ["--", "Review the change"])

    def test_default_haiku_does_not_add_plan_mode_allowlist(self):
        for model in ("haiku", "claude-haiku-5-5"):
            with self.subTest(model=model):
                cmd = self._command_for(_args(model=model))
                self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "auto")
                self.assertEqual(cmd[cmd.index("--effort") + 1], "max")
                self.assertNotIn("--settings", cmd)

    def test_explicit_read_only_plan_haiku_adds_local_allowlist(self):
        for model in ("haiku", "claude-haiku-5-5"):
            with self.subTest(model=model):
                cmd = self._command_for(_args(
                    model=model, claude_permission_mode="plan",
                ))
                self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "plan")
                settings = json.loads(cmd[cmd.index("--settings") + 1])
                self.assertEqual(settings, {"availableModels": [model]})
                self.assertNotIn("--disallowedTools", cmd)

    def test_authorized_haiku_write_does_not_add_the_plan_mode_allowlist(self):
        cmd = self._command_for(_args(
            model="haiku", allow_write=True,
            write_scope="scripts/council.py | Claude bridge | authorized edit",
        ))
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "auto")
        self.assertNotIn("--settings", cmd)
        self.assertNotIn("--tools", cmd)
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1], council.CLAUDE_NO_PLAN_TOOLS)

    def test_explicit_claude_effort_override_is_preserved(self):
        cmd = self._command_for(_args(model="haiku", claude_effort="low"))
        self.assertEqual(cmd[cmd.index("--effort") + 1], "low")

    def test_authorized_claude_write_defaults_to_auto_and_resumes(self):
        cmd = self._command_for(_args(
            allow_write=True,
            write_scope="scripts/council.py | Claude bridge | use auto mode",
            SESSION_ID="claude-session",
        ))
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "auto")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "claude-session")
        self.assertNotIn("--tools", cmd)
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1], council.CLAUDE_NO_PLAN_TOOLS)

    def test_explicit_claude_write_override_is_preserved(self):
        cmd = self._command_for(_args(
            allow_write=True,
            write_scope="scripts/council.py | Claude bridge | use legacy mode",
            claude_permission_mode="acceptEdits",
        ))
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")

    def test_claude_permission_override_requires_compatible_scope_and_claude_member(self):
        base = ["council.py", "--PROMPT", "x", "--cd", "/tmp"]
        for tail in [
            ["--with", "claude", "--claude-permission-mode", "acceptEdits"],
            ["--with", "codex", "--allow-write", "--write-scope", "src/x.py | x | edit", "--claude-permission-mode", "auto"],
            ["--with", "claude", "--allow-write", "--write-scope", "src/x.py | x | edit", "--claude-permission-mode", "plan"],
        ]:
            with self.subTest(tail=tail), patch.object(sys, "argv", base + tail):
                with self.assertRaises(SystemExit) as error:
                    council.main()
                self.assertEqual(error.exception.code, 2)

    def test_explicit_read_only_auto_and_plan_overrides_are_accepted(self):
        for mode in ("auto", "plan"):
            with self.subTest(mode=mode):
                output = io.StringIO()
                argv = [
                    "council.py", "--with", "claude", "--PROMPT", "Review",
                    "--cd", "/tmp", "--claude-permission-mode", mode,
                ]
                with patch.object(council, "RUNNERS", {"claude": lambda _args, _prompt: {"success": True}}), \
                     patch.object(sys, "argv", argv), patch("sys.stdout", output):
                    council.main()
                result = json.loads(output.getvalue())
                self.assertEqual(result["permission"]["claude_permission_mode"], mode)

    def test_claude_exposes_effective_mode_and_permission_denials(self):
        def collect(_cmd, **kwargs):
            state = {
                "session": None, "fallback": "", "text": "", "has_error": False,
                "err": "", "effective_permission_mode": None, "permission_denials": None,
            }
            kwargs["on_event"]({"type": "system", "permissionMode": "auto"}, state)
            kwargs["on_event"]({
                "type": "result", "subtype": "success", "result": "done",
                "permission_denials": [{"tool_name": "Edit"}],
            }, state)
            self.assertEqual(state["effective_permission_mode"], "auto")
            self.assertEqual(state["permission_denials"], [{"tool_name": "Edit"}])
            return {"success": True, "effective_permission_mode": state["effective_permission_mode"],
                    "permission_denials": state["permission_denials"]}

        with patch.object(council, "_collect", side_effect=collect):
            result = council.run_claude(_args(), "Review")
        self.assertEqual(result["effective_permission_mode"], "auto")
        self.assertEqual(result["permission_denials"], [{"tool_name": "Edit"}])

    def test_claude_receipt_fails_on_init_haiku_and_actual_sonnet(self):
        def collect(_cmd, **kwargs):
            state = {"session": None, "fallback": "", "text": "", "has_error": False,
                     "err": "", "effective_permission_mode": None, "permission_denials": None}
            kwargs["on_event"]({"type": "system", "subtype": "init", "model": "claude-haiku-5-5"}, state)
            kwargs["on_event"]({"type": "assistant", "message": {"model": "claude-sonnet-4-5", "content": []}}, state)
            kwargs["on_event"]({"type": "result", "subtype": "success", "result": "done"}, state)
            return {"success": True, "agent_messages": "done", "_all_messages": [
                {"type": "system", "subtype": "init", "model": "claude-haiku-5-5"},
                {"type": "assistant", "message": {"model": "claude-sonnet-4-5", "content": []}},
                {"type": "result", "subtype": "success", "result": "done"},
            ]}

        with patch.object(council, "_collect", side_effect=collect):
            result = council.run_claude(_args(model="haiku"), "Review")
        self.assertFalse(result["success"])
        self.assertEqual(result["receipt"]["configured_model"], "claude-haiku-5-5")
        self.assertEqual(result["receipt"]["actual_models"], ["claude-sonnet-4-5"])
        self.assertFalse(result["receipt"]["model_match"])
        self.assertIn("model mismatch", result["error"])

    def test_claude_receipt_accepts_matching_actual_family(self):
        result = council._attach_claude_receipt(_args(model="opus"), {"success": True, "_all_messages": [
            {"type": "assistant", "message": {"model": "claude-opus-4-6", "content": []}},
            {"type": "result", "subtype": "success", "result": "done"},
        ]})
        self.assertTrue(result["success"])
        self.assertTrue(result["receipt"]["model_match"])

    def test_claude_receipt_ignores_nested_subagent_models(self):
        result = council._attach_claude_receipt(_args(model="opus"), {"success": True, "_all_messages": [
            {"type": "assistant", "message": {"model": "claude-opus-4-6", "content": []}},
            {"type": "assistant", "parent_tool_use_id": "tool-1",
             "message": {"model": "claude-sonnet-4-5", "content": []}},
            {"type": "result", "subtype": "success", "result": "done"},
        ]})
        self.assertTrue(result["success"])
        self.assertEqual(result["receipt"]["actual_models"], ["claude-opus-4-6"])
        self.assertEqual(result["receipt"]["subagent_models"], ["claude-sonnet-4-5"])
        self.assertTrue(result["receipt"]["model_match"])

    def test_claude_synthetic_model_is_not_asserted_as_actual(self):
        result = council._attach_claude_receipt(_args(model="haiku"), {"success": False, "_all_messages": [
            {"type": "assistant", "message": {"model": "<synthetic>", "content": []}},
            {"type": "result", "subtype": "success", "is_error": True, "result": "quota exceeded"},
        ]})
        self.assertFalse(result["success"])
        self.assertEqual(result["receipt"]["actual_models"], [])
        self.assertIsNone(result["receipt"]["model_match"])

    def test_claude_receipt_uses_terminal_usage_without_fragment_accumulation(self):
        result = council._attach_claude_receipt(_args(), {"success": True, "_all_messages": [
            {"type": "assistant", "message": {"model": "claude-opus-4-6", "usage": {"input_tokens": 2}}},
            {"type": "result", "subtype": "success", "usage": {"input_tokens": 9},
             "modelUsage": {"claude-opus-4-6": {"outputTokens": 3}}, "total_cost_usd": 0.12},
        ]})
        receipt = result["receipt"]
        self.assertEqual(receipt["usage"], {"input_tokens": 9})
        self.assertEqual(receipt["modelUsage"], {"claude-opus-4-6": {"outputTokens": 3}})
        self.assertEqual(receipt["total_cost_usd"], 0.12)

    def test_claude_receipt_leaves_missing_metadata_unknown(self):
        result = council._attach_claude_receipt(_args(), {"success": True, "_all_messages": [
            {"type": "result", "subtype": "success", "result": "done"},
        ]})
        self.assertIsNone(result["receipt"]["configured_model"])
        self.assertIsNone(result["receipt"]["model_match"])
        self.assertIsNone(result["receipt"]["effective_effort"])
        self.assertNotIn("usage", result["receipt"])


class AdapterCommandTests(unittest.TestCase):
    def _command_for(self, runner, args):
        captured = {}

        def collect(cmd, **kwargs):
            captured["cmd"] = cmd
            captured.update(kwargs)
            return {"success": True}

        with patch.object(council, "_collect", side_effect=collect):
            runner(args, "Review")
        return captured

    def test_gemini_uses_non_plan_read_only_policy_and_result_terminal(self):
        read = self._command_for(council.run_gemini, _args())
        write = self._command_for(council.run_gemini, _args(allow_write=True))
        self.assertEqual(read["cmd"][read["cmd"].index("--approval-mode") + 1], "default")
        self.assertEqual(write["cmd"][write["cmd"].index("--approval-mode") + 1], "auto_edit")
        self.assertIn("--policy", read["cmd"])
        self.assertNotIn("--policy", write["cmd"])
        self.assertTrue(read["is_done"]('{"type":"result"}'))
        self.assertFalse(read["is_done"]('{"type":"turn.completed"}'))

    def test_gemini_terminal_error_marks_failure(self):
        def collect(_cmd, **kwargs):
            state = {"text": "", "session": None, "has_error": False, "err": ""}
            kwargs["on_event"]({"type": "result", "status": "error", "error": {"message": "denied"}}, state)
            self.assertTrue(state["has_error"])
            self.assertIn("denied", state["err"])
            return {"success": False}

        with patch.object(council, "_collect", side_effect=collect):
            council.run_gemini(_args(), "Review")

    def test_grok_uses_non_plan_defaults_and_read_only_tools(self):
        read = self._command_for(council.run_grok, _args())
        write = self._command_for(council.run_grok, _args(allow_write=True))
        self.assertEqual(read["cmd"][read["cmd"].index("--permission-mode") + 1], "auto")
        self.assertEqual(write["cmd"][write["cmd"].index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(read["cmd"][read["cmd"].index("--tools") + 1], council.GROK_TOOLS_READ)
        self.assertNotIn("--tools", write["cmd"])
        for result in (read, write):
            self.assertIn("--no-plan", result["cmd"])
            self.assertIn("--no-subagents", result["cmd"])

    def test_grok_requires_end_turn_stop_reason(self):
        def collect(_cmd, **kwargs):
            state = {"text": "", "session": None, "has_error": False, "err": ""}
            kwargs["on_event"]({"type": "end", "stopReason": "max_tokens"}, state)
            self.assertTrue(state["has_error"])
            self.assertIn("max_tokens", state["err"])
            return {"success": False}

        with patch.object(council, "_collect", side_effect=collect):
            council.run_grok(_args(grok_effort="high"), "Review")

    def test_pi_disables_mcp_and_uses_mode_specific_tools(self):
        read = self._command_for(council.run_pi, _args(pi_thinking="off"))
        write = self._command_for(council.run_pi, _args(allow_write=True, pi_thinking="off"))
        self.assertIn("--no-mcp", read["cmd"])
        self.assertNotIn("--no-mcp", write["cmd"])
        self.assertEqual(read["cmd"][read["cmd"].index("--tools") + 1], council.PI_TOOLS_READ)
        self.assertEqual(write["cmd"][write["cmd"].index("--tools") + 1], council.PI_TOOLS_WRITE)
        self.assertNotIn("--approve", write["cmd"])
        self.assertTrue(read["is_done"]('{"type":"agent_settled"}'))
        self.assertFalse(read["is_done"]('{"type":"agent_end"}'))

    def test_pi_message_end_error_marks_failure(self):
        def collect(_cmd, **kwargs):
            state = {"text": "", "session": None, "has_error": False, "err": ""}
            kwargs["on_event"]({
                "type": "message_end", "stopReason": "error", "errorMessage": "tool crashed",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "partial"}]},
            }, state)
            self.assertFalse(state["has_error"])
            kwargs["on_event"]({"type": "agent_settled"}, state)
            self.assertTrue(state["has_error"])
            self.assertEqual(state["text"], "partial")
            self.assertIn("tool crashed", state["err"])
            return {"success": False}

        with patch.object(council, "_collect", side_effect=collect):
            council.run_pi(_args(pi_thinking="off"), "Review")

    def test_pi_successful_retry_replaces_attempt_error(self):
        def collect(_cmd, **kwargs):
            state = {"text": "", "session": None, "has_error": False, "err": ""}
            for event in [
                {"type": "message_end", "message": {"role": "assistant", "stopReason": "error", "errorMessage": "overloaded", "content": [{"type": "text", "text": "stale partial"}]}},
                {"type": "agent_end", "willRetry": True},
                {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "done"}]}},
                {"type": "agent_settled"},
            ]:
                kwargs["on_event"](event, state)
            self.assertFalse(state["has_error"])
            self.assertEqual(state["text"], "done")
            return {"success": True}

        with patch.object(council, "_collect", side_effect=collect):
            council.run_pi(_args(pi_thinking="off"), "Review")

    def test_pi_aborted_settled_event_fails_without_assistant_error(self):
        def collect(_cmd, **kwargs):
            state = {"text": "", "session": None, "has_error": False, "err": ""}
            kwargs["on_event"]({"type": "agent_settled", "aborted": True}, state)
            self.assertTrue(state["has_error"])
            self.assertIn("aborted", state["err"])
            return {"success": False}

        with patch.object(council, "_collect", side_effect=collect):
            council.run_pi(_args(pi_thinking="off"), "Review")


class CollectionCompletionTests(unittest.TestCase):
    def _collect(self, lines, *, terminal, returncode=0, handler=None):
        def stream(*_args, outcome, **_kwargs):
            outcome.update({
                "terminal_seen": terminal, "returncode": returncode,
                "terminated_after_terminal": False,
            })
            yield from lines

        def on_event(event, state):
            if handler:
                handler(event, state)
            elif event.get("type") == "text":
                state["text"] += event.get("text", "")
            elif event.get("type") == "session":
                state["session"] = event["id"]
            elif event.get("type") == "error":
                state["has_error"] = True
                state["err"] += "backend failed"

        with patch.object(council, "run_stream", side_effect=stream):
            return council._collect(
                ["fake"], cwd=None, env_extra=None, proxy=None,
                is_done=lambda _line: False, on_event=on_event, tag="test",
            )

    def test_terminal_success_requires_clean_zero_exit(self):
        result = self._collect(['{"type":"text","text":"done"}'], terminal=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["agent_messages"], "done")

    def test_error_after_text_preserves_partial_reply_but_fails(self):
        result = self._collect([
            '{"type":"text","text":"partial"}',
            '{"type":"error","message":"late failure"}',
        ], terminal=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["agent_messages"], "partial")
        self.assertIn("backend failed", result["error"])

    def test_real_stream_drains_error_after_terminal_event(self):
        script = (
            "import json; "
            "print(json.dumps({'type': 'text', 'text': 'partial'})); "
            "print(json.dumps({'type': 'result'})); "
            "print(json.dumps({'type': 'error', 'message': 'late failure'}))"
        )

        def on_event(event, state):
            if event["type"] == "text":
                state["text"] += event["text"]
            elif event["type"] == "error":
                state["has_error"] = True
                state["err"] += event["message"]

        result = council._collect(
            [sys.executable, "-c", script], cwd=None, env_extra=None, proxy=None,
            is_done=lambda line: council._json_type_is(line, "result"),
            on_event=on_event, tag="real-stream",
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["agent_messages"], "partial")
        self.assertIn("late failure", result["error"])

    def test_session_without_terminal_is_not_a_success(self):
        result = self._collect(['{"type":"session","id":"resume-me"}'], terminal=False)
        self.assertFalse(result["success"])
        self.assertEqual(result["SESSION_ID"], "resume-me")
        self.assertIn("without a terminal event", result["error"])

    def test_incomplete_non_json_diagnostics_are_retained(self):
        result = self._collect(["authentication failed", "retry with --login"], terminal=False)
        self.assertFalse(result["success"])
        self.assertEqual(result["diagnostic_tail"], ["authentication failed", "retry with --login"])

    def test_nonzero_exit_fails_after_terminal_event(self):
        result = self._collect(['{"type":"text","text":"done"}'], terminal=True, returncode=7)
        self.assertFalse(result["success"])
        self.assertIn("status 7", result["error"])

    def test_real_stream_reports_nonzero_exit_after_terminal_event(self):
        script = "import json, sys; print(json.dumps({'type': 'result'})); sys.exit(7)"
        result = council._collect(
            [sys.executable, "-c", script], cwd=None, env_extra=None, proxy=None,
            is_done=lambda line: council._json_type_is(line, "result"),
            on_event=lambda _event, _state: None, tag="real-exit",
        )
        self.assertFalse(result["success"])
        self.assertIn("status 7", result["error"])

    def test_terminal_grace_terminates_a_cli_that_keeps_running(self):
        script = (
            "import json, sys, time; print(json.dumps({'type': 'result'})); "
            "sys.stdout.flush(); sys.stdout.close(); time.sleep(30)"
        )
        started = time.monotonic()
        result = council._collect(
            [sys.executable, "-c", script], cwd=None, env_extra=None, proxy=None,
            is_done=lambda line: council._json_type_is(line, "result"),
            on_event=lambda _event, _state: None, tag="terminal-grace",
        )
        self.assertLess(time.monotonic() - started, 4)
        self.assertFalse(result["success"])
        self.assertIn("terminal grace period", result["error"])

    @unittest.skipIf(os.name == "nt", "POSIX signal behavior")
    def test_terminal_grace_kills_a_process_ignoring_sigterm(self):
        script = (
            "import json, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print(json.dumps({'type': 'result'}), flush=True); time.sleep(30)"
        )
        started = time.monotonic()
        result = council._collect(
            [sys.executable, "-c", script], cwd=None, env_extra=None, proxy=None,
            is_done=lambda line: council._json_type_is(line, "result"),
            on_event=lambda _event, _state: None, tag="ignored-sigterm",
        )
        self.assertLess(time.monotonic() - started, 7)
        self.assertFalse(result["success"])
        self.assertIn("terminal grace period", result["error"])

    def test_claude_tool_event_does_not_override_a_clean_terminal_result(self):
        def claude_collect(_cmd, **kwargs):
            state = {"session": None, "fallback": "", "text": "", "has_error": False,
                     "err": "", "effective_permission_mode": None}
            kwargs["on_event"]({"type": "tool_result", "is_error": True}, state)
            kwargs["on_event"]({"type": "result", "subtype": "success", "result": "done"}, state)
            self.assertFalse(state["has_error"])
            self.assertEqual(state["text"], "done")
            return {"success": True}

        with patch.object(council, "_collect", side_effect=claude_collect):
            result = council.run_claude(_args(), "Review the change")
        self.assertTrue(result["success"])


class CodexAppServerCompletionTests(unittest.TestCase):
    def test_interrupted_turn_is_not_successful(self):
        server = SimpleNamespace(events=[])
        notifications = iter([{"method": "turn/completed", "params": {"turn": {
            "id": "turn-1", "status": "interrupted", "items": [],
        }}}])
        server.next_notification = lambda: next(notifications)
        result = council._codex_turn_result(server, "thread-1", "turn-1")
        self.assertFalse(result["success"])
        self.assertEqual(result["SESSION_ID"], "thread-1")
        self.assertIn("interrupted", result["error"])

    def test_exec_uses_noninteractive_approval_policy(self):
        args = _args()
        captured = {}

        def collect(cmd, **_kwargs):
            captured["cmd"] = cmd
            return {}

        with patch.object(council, "_collect", side_effect=collect):
            council._run_codex_exec(args, "Review")
        self.assertIn('approval_policy="never"', captured["cmd"])

    def test_exec_reconnect_is_not_terminal_or_a_failed_turn(self):
        def collect(_cmd, **kwargs):
            state = {"text": "", "fallback": "", "session": None, "has_error": False, "err": ""}
            event = {"type": "error", "message": "Reconnecting... 1/5"}
            self.assertFalse(kwargs["is_done"](json.dumps(event)))
            kwargs["on_event"](event, state)
            kwargs["on_event"]({"type": "turn.completed"}, state)
            self.assertFalse(state["has_error"])
            self.assertTrue(kwargs["is_done"]('{"type":"turn.completed"}'))
            self.assertTrue(kwargs["is_done"]('{"type":"turn.failed"}'))
            return {"success": True}

        with patch.object(council, "_collect", side_effect=collect):
            council._run_codex_exec(_args(), "Review")


class OpenCodeAndDshTests(unittest.TestCase):
    def _captured(self, runner, args):
        captured = {}

        def collect(cmd, **kwargs):
            captured["cmd"] = cmd
            captured.update(kwargs)
            return {"success": True}

        with patch.object(council, "_collect", side_effect=collect):
            runner(args, "Review")
        return captured

    def _apply(self, runner, lines):
        def collect(_cmd, **kwargs):
            state = {"text": "", "session": None, "has_error": False, "err": ""}
            for line in lines:
                kwargs["on_event"](json.loads(line), state)
            return state, kwargs["is_done"]

        with patch.object(council, "_collect", side_effect=collect):
            return runner(_args(), "Review")

    def test_opencode_parser_success_session_and_stop_line(self):
        lines = [
            '{"type":"step_start","sessionID":"ses_ok","part":{"type":"step-start"}}',
            '{"type":"text","sessionID":"ses_ok","part":{"type":"text","text":"hello"}}',
            '{"type":"step_finish","sessionID":"ses_ok","part":{"reason":"tool-calls"}}',
            '{"type":"text","sessionID":"ses_ok","part":{"type":"text","text":" world"}}',
            '{"type":"step_finish","sessionID":"ses_ok","part":{"reason":"stop"}}',
        ]
        state, is_done = self._apply(council.run_opencode, lines)
        self.assertFalse(is_done(lines[2]))
        self.assertTrue(is_done(lines[4]))
        self.assertEqual(state["session"], "ses_ok")
        self.assertEqual(state["text"], "hello world")
        self.assertFalse(state["has_error"])

    def test_opencode_error_event_is_terminal_and_captured(self):
        line = '{"type":"error","sessionID":"ses_bad","message":"provider failed"}'
        state, is_done = self._apply(council.run_opencode, [line])
        self.assertTrue(is_done(line))
        self.assertEqual(state["session"], "ses_bad")
        self.assertTrue(state["has_error"])
        self.assertIn("provider failed", state["err"])

    def test_opencode_read_only_and_write_commands(self):
        read = self._captured(council.run_opencode, _args(model="openai/gpt-5", SESSION_ID="ses_1"))
        write = self._captured(council.run_opencode, _args(allow_write=True, write_scope="a.txt | body | edit"))
        self.assertEqual(read["cmd"][:8], [
            "opencode", "run", "--format", "json", "--dir", "/tmp", "--agent", "plan",
        ])
        self.assertEqual(read["cmd"][read["cmd"].index("-m") + 1], "openai/gpt-5")
        self.assertEqual(read["cmd"][read["cmd"].index("-s") + 1], "ses_1")
        self.assertEqual(read["cmd"][-2:], ["--", "Review"])
        self.assertEqual(read["env_force"], {
            "OPENCODE_CONFIG_CONTENT": council.OPENCODE_READ_ONLY_CONFIG,
        })
        self.assertEqual(read["cwd"], "/tmp")
        self.assertEqual(write["cmd"][write["cmd"].index("--agent") + 1], "build")
        self.assertIsNone(write["env_force"])
        self.assertNotIn("-m", write["cmd"])
        self.assertNotIn("-s", write["cmd"])

    def test_dsh_parser_success_session_and_final_text(self):
        lines = [
            '{"type":"session","sessionId":"session-850b"}',
            '{"type":"text","text":"hello"}',
            '{"type":"status","phase":"turn_end","reason":{"kind":"completed"}}',
            '{"type":"final","text":"hello"}',
        ]
        state, is_done = self._apply(council.run_dsh, lines)
        self.assertFalse(is_done(lines[2]))
        self.assertTrue(is_done(lines[3]))
        self.assertEqual(state["session"], "session-850b")
        self.assertEqual(state["text"], "hello")
        self.assertFalse(state["has_error"])

    def test_dsh_empty_final_keeps_accumulated_assistant_text(self):
        lines = [
            '{"type":"session","sessionId":"session-notes"}',
            '{"type":"text","text":"hel"}',
            '{"type":"text","text":"lo"}',
            '{"type":"final","text":""}',
        ]
        state, is_done = self._apply(council.run_dsh, lines)
        self.assertTrue(is_done(lines[3]))
        self.assertEqual(state["session"], "session-notes")
        self.assertEqual(state["text"], "hello")

    def test_dsh_turn_end_error_reports_message_and_code(self):
        lines = [
            '{"type":"session","sessionId":"session-err"}',
            '{"type":"status","phase":"turn_end","turn":1,"reason":{"kind":"error","error":{"message":"llm-deepseek: no API key","code":"MISSING_CREDENTIAL"}}}',
            '{"type":"final","text":""}',
        ]
        state, is_done = self._apply(council.run_dsh, lines)
        self.assertFalse(is_done(lines[1]))
        self.assertTrue(is_done(lines[2]))
        self.assertTrue(state["has_error"])
        self.assertIn("llm-deepseek: no API key", state["err"])
        self.assertIn("MISSING_CREDENTIAL", state["err"])
        self.assertEqual(state["session"], "session-err")

    def test_dsh_error_event_is_terminal(self):
        line = '{"type":"error","message":"a task is required"}'
        state, is_done = self._apply(council.run_dsh, [line])
        self.assertTrue(is_done(line))
        self.assertTrue(state["has_error"])
        self.assertIn("a task is required", state["err"])

    def test_dsh_read_only_and_write_commands(self):
        read = self._captured(council.run_dsh, _args(SESSION_ID="session-1"))
        write = self._captured(council.run_dsh, _args(allow_write=True, write_scope="a.txt | body | edit"))
        self.assertEqual(read["cmd"], [
            "dsh", "--profile", "headless", "--json", "--session-id", "session-1", "Review",
        ])
        self.assertEqual(write["cmd"], ["dsh", "--profile", "headless", "--json", "Review"])
        self.assertEqual(read["env_force"], {"DSH_PERMISSION_MODE": "read-only"})
        self.assertEqual(write["env_force"], {"DSH_PERMISSION_MODE": "workspace-write"})
        self.assertEqual(read["cwd"], "/tmp")
        self.assertNotIn("--model", read["cmd"])
        self.assertNotIn("-m", write["cmd"])

    def test_dsh_rejects_model_override(self):
        output = io.StringIO()
        argv = [
            "council.py", "--with", "dsh", "--model", "deepseek-chat",
            "--PROMPT", "Review", "--cd", "/tmp",
        ]
        with patch.object(sys, "argv", argv), patch("sys.stdout", output):
            with self.assertRaises(SystemExit) as error:
                council.main()
        self.assertEqual(error.exception.code, 2)
        payload = json.loads(output.getvalue())
        self.assertIn("dsh", payload["error"])
        self.assertIn("model", payload["error"])

    def test_help_lists_opencode_and_dsh(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["council.py", "--help"]), patch("sys.stdout", output):
            with self.assertRaises(SystemExit) as error:
                council.main()
        self.assertEqual(error.exception.code, 0)
        text = output.getvalue()
        self.assertIn("opencode", text)
        self.assertIn("dsh", text)

    def test_new_members_inherit_proxy_and_report_read_only_modes(self):
        self.assertEqual(council._proxy_for("opencode", _args(proxy="")), "")
        self.assertEqual(council._proxy_for("dsh", _args(proxy="")), "")
        output = io.StringIO()
        argv = ["council.py", "--with", "opencode,dsh", "--PROMPT", "Review", "--cd", "/tmp"]
        runners = {
            "opencode": lambda *_args, _prompt: {"success": True},
            "dsh": lambda *_args, _prompt: {"success": True},
        }
        with patch.object(council, "RUNNERS", runners), patch.object(sys, "argv", argv), patch("sys.stdout", output):
            council.main()
        permission = json.loads(output.getvalue())["permission"]
        self.assertEqual(permission["mode"], "read-only")
        self.assertEqual(permission["opencode_agent"], "plan")
        self.assertEqual(permission["dsh_permission_mode"], "read-only")

    def test_forced_dsh_mode_replaces_ambient_danger_full_access(self):
        with patch.dict(os.environ, {"DSH_PERMISSION_MODE": "danger-full-access"}, clear=False):
            env = council._stream_env(None, {"DSH_PERMISSION_MODE": "read-only"}, None)
        self.assertEqual(env["DSH_PERMISSION_MODE"], "read-only")

    def test_opencode_resolves_nvm_bin_when_missing_from_path(self):
        fallback = council._OPENCODE_NVM_BIN / "opencode"
        with patch("council.shutil.which", return_value=None):
            resolved = council._resolve_executable("opencode", {"PATH": "/usr/bin"})
        if fallback.is_file():
            self.assertEqual(resolved, str(fallback))
        else:
            self.assertEqual(resolved, "opencode")


class FanoutResultTests(unittest.TestCase):
    def test_partial_fanout_keeps_legacy_success_and_reports_failures(self):
        runners = {
            "codex": lambda _args, _prompt: {"success": True},
            "claude": lambda _args, _prompt: {"success": False, "error": "failed"},
        }
        output = io.StringIO()
        argv = [
            "council.py", "--with", "codex,claude", "--PROMPT", "Review", "--cd", "/tmp",
        ]
        with patch.object(council, "RUNNERS", runners), patch.object(sys, "argv", argv), patch("sys.stdout", output):
            council.main()
        result = json.loads(output.getvalue())
        self.assertTrue(result["success"])
        self.assertFalse(result["all_succeeded"])
        self.assertEqual(result["failed_members"], ["claude"])


class NamedSessionLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger_path = Path(self.tmp.name) / "sessions.json"
        self.cwd = str(Path(self.tmp.name).resolve())
        self.env = patch.dict(
            os.environ, {"COUNCIL_SESSIONS_PATH": str(self.ledger_path)}, clear=False,
        )
        self.env.start()
        self.output = ""

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _main(self, argv, runners=None):
        output = io.StringIO()
        patches = [patch.object(sys, "argv", argv), patch("sys.stdout", output)]
        if runners is not None:
            patches.append(patch.object(council, "RUNNERS", runners))
        for item in patches:
            item.start()
        try:
            council.main()
        finally:
            self.output = output.getvalue()
            for item in reversed(patches):
                item.stop()
        return self.output

    def _guard_runners(self):
        called = []

        def fail(member):
            def run(_args, _prompt):
                called.append(member)
                raise AssertionError(f"{member} should not run")
            return run

        runners = {name: fail(name) for name in council.RUNNERS}
        return called, runners

    def test_default_path_ignores_an_unset_override(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COUNCIL_SESSIONS_PATH", None)
            self.assertEqual(
                council._sessions_path(),
                Path.home() / ".ai" / "skills" / "council" / "state" / "sessions.json",
            )

    def test_helpers_round_trip_without_storing_prompt_text(self):
        self.assertEqual(council._load_sessions(), {})
        self.assertEqual(council._stored_session_id("parser-review", "opencode", self.cwd), "")
        council._upsert_session("parser-review", "opencode", self.cwd, "ses_saved")
        council._upsert_session("other-review", "dsh", self.cwd, "session-2")
        self.assertEqual(
            council._stored_session_id("parser-review", "opencode", self.cwd), "ses_saved",
        )
        council._upsert_session("parser-review", "opencode", self.cwd, "ses_next")
        ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        entry = ledger["parser-review"]
        self.assertEqual(set(entry), {"member", "cwd", "SESSION_ID", "updated_at"})
        self.assertEqual(entry["member"], "opencode")
        self.assertEqual(entry["cwd"], self.cwd)
        self.assertEqual(entry["SESSION_ID"], "ses_next")
        self.assertRegex(entry["updated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
        self.assertEqual(ledger["other-review"]["SESSION_ID"], "session-2")
        self.assertFalse(list(Path(self.tmp.name).glob(".sessions.json.*.tmp")))
        self.assertFalse(council._forget_session("missing"))
        self.assertTrue(council._forget_session("parser-review"))
        self.assertNotIn("parser-review", council._load_sessions())

    def test_empty_ledger_does_not_inject_and_success_upserts(self):
        captured = {}

        def collect(cmd, **kwargs):
            captured.setdefault("cmds", []).append(list(cmd))
            return {
                "success": True,
                "SESSION_ID": "ses_new",
                "agent_messages": "do not store this reply or sk-test-secret",
            }

        argv = [
            "council.py", "--with", "opencode", "--session", "parser-review",
            "--cd", self.cwd, "--PROMPT", "Review the parser. sk-test-secret",
        ]
        with patch.object(council, "_collect", side_effect=collect):
            self._main(argv)
        cmd = captured["cmds"][0]
        self.assertNotIn("-s", cmd)
        ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        entry = ledger["parser-review"]
        self.assertEqual(entry["member"], "opencode")
        self.assertEqual(entry["cwd"], self.cwd)
        self.assertEqual(entry["SESSION_ID"], "ses_new")
        blob = self.ledger_path.read_text(encoding="utf-8")
        self.assertNotIn("sk-test-secret", blob)
        self.assertNotIn("do not store", blob)

        with patch.object(council, "_collect", side_effect=collect):
            self._main(argv)
        resumed = captured["cmds"][1]
        self.assertEqual(resumed[resumed.index("-s") + 1], "ses_new")

        council._upsert_session("dsh-review", "dsh", self.cwd, "session-9")
        dsh_argv = [
            "council.py", "--with", "dsh", "--session", "dsh-review",
            "--cd", self.cwd, "--PROMPT", "delta",
        ]
        with patch.object(council, "_collect", side_effect=collect):
            self._main(dsh_argv)
        dsh_cmd = captured["cmds"][-1]
        self.assertEqual(dsh_cmd[dsh_cmd.index("--session-id") + 1], "session-9")

    def test_same_name_different_member_is_a_usage_error(self):
        council._upsert_session("parser-review", "opencode", self.cwd, "ses_old")
        called, runners = self._guard_runners()
        argv = [
            "council.py", "--with", "dsh", "--session", "parser-review",
            "--cd", self.cwd, "--PROMPT", "delta",
        ]
        with self.assertRaises(SystemExit) as error:
            self._main(argv, runners=runners)
        self.assertEqual(error.exception.code, 2)
        payload = json.loads(self.output)
        self.assertIn("parser-review", payload["error"])
        self.assertIn("--session-forget", payload["error"])
        self.assertEqual(called, [])
        self.assertEqual(
            json.loads(self.ledger_path.read_text(encoding="utf-8"))["parser-review"]["SESSION_ID"],
            "ses_old",
        )

    def test_same_name_different_cwd_is_a_usage_error(self):
        other = Path(self.tmp.name) / "elsewhere"
        other.mkdir()
        council._upsert_session("parser-review", "opencode", self.cwd, "ses_old")
        called, runners = self._guard_runners()
        argv = [
            "council.py", "--with", "opencode", "--session", "parser-review",
            "--cd", str(other), "--PROMPT", "delta",
        ]
        with self.assertRaises(SystemExit) as error:
            self._main(argv, runners=runners)
        self.assertEqual(error.exception.code, 2)
        self.assertIn("--session-forget", json.loads(self.output)["error"])
        self.assertEqual(called, [])

    def test_explicit_session_id_overrides_then_upserts_returned_id(self):
        council._upsert_session("parser-review", "opencode", self.cwd, "ses_old")
        captured = {}

        def collect(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return {"success": True, "SESSION_ID": "ses_returned"}

        argv = [
            "council.py", "--with", "opencode", "--session", "parser-review",
            "--SESSION_ID", "ses_explicit", "--cd", self.cwd, "--PROMPT", "delta",
        ]
        with patch.object(council, "_collect", side_effect=collect):
            self._main(argv)
        cmd = captured["cmd"]
        self.assertEqual(cmd[cmd.index("-s") + 1], "ses_explicit")
        saved = json.loads(self.ledger_path.read_text(encoding="utf-8"))["parser-review"]
        self.assertEqual(saved["SESSION_ID"], "ses_returned")
        self.assertEqual(saved["member"], "opencode")

    def test_failed_turn_does_not_upsert(self):
        def collect(_cmd, **kwargs):
            return {"success": False, "SESSION_ID": "ses_bad", "error": "provider failed"}

        argv = [
            "council.py", "--with", "opencode", "--session", "parser-review",
            "--cd", self.cwd, "--PROMPT", "delta",
        ]
        with patch.object(council, "_collect", side_effect=collect):
            result = json.loads(self._main(argv))
        self.assertFalse(result["success"])
        self.assertFalse(self.ledger_path.exists())

    def test_success_without_session_id_does_not_create_an_entry(self):
        def collect(_cmd, **kwargs):
            return {"success": True, "agent_messages": "no id"}

        argv = [
            "council.py", "--with", "dsh", "--session", "parser-review",
            "--cd", self.cwd, "--PROMPT", "delta",
        ]
        with patch.object(council, "_collect", side_effect=collect):
            self._main(argv)
        self.assertFalse(self.ledger_path.exists())

    def test_session_requires_one_member(self):
        called, runners = self._guard_runners()
        argv = [
            "council.py", "--with", "opencode,dsh", "--session", "parser-review",
            "--cd", self.cwd, "--PROMPT", "delta",
        ]
        with self.assertRaises(SystemExit) as error:
            self._main(argv, runners=runners)
        self.assertEqual(error.exception.code, 2)
        self.assertIn("exactly one member", json.loads(self.output)["error"])
        self.assertEqual(called, [])
        self.assertFalse(self.ledger_path.exists())

    def test_list_and_forget_do_not_run_members(self):
        called, runners = self._guard_runners()
        self.assertEqual(json.loads(self._main(["council.py", "--session-list"], runners)), {})
        self._main(["council.py", "--session-forget", "missing"], runners)
        self.assertFalse(self.ledger_path.exists())
        self.assertEqual(called, [])

        council._upsert_session("parser-review", "opencode", self.cwd, "ses_a")
        council._upsert_session("other-review", "dsh", self.cwd, "ses_b")
        self._main(["council.py", "--session-forget", "missing"], runners)
        listed = json.loads(self._main(["council.py", "--session-list"], runners))
        self.assertEqual(set(listed), {"parser-review", "other-review"})
        self._main(["council.py", "--session-forget", "parser-review"], runners)
        listed = json.loads(self._main(["council.py", "--session-list"], runners))
        self.assertEqual(set(listed), {"other-review"})
        self.assertEqual(listed["other-review"]["SESSION_ID"], "ses_b")
        self.assertEqual(called, [])

    def test_help_lists_session_flags(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["council.py", "--help"]), patch("sys.stdout", output):
            with self.assertRaises(SystemExit) as error:
                council.main()
        self.assertEqual(error.exception.code, 0)
        text = output.getvalue()
        self.assertRegex(text, r"(?m)^\s+--session(?:\s|$)")
        self.assertIn("--session-list", text)
        self.assertIn("--session-forget", text)


if __name__ == "__main__":
    unittest.main()
