import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "session_digest.py"
SPEC = importlib.util.spec_from_file_location("session_digest_under_test", SCRIPT)
assert SPEC and SPEC.loader
sd = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sd
SPEC.loader.exec_module(sd)


class CodexLineageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.old_codex_home = sd.CODEX_HOME
        sd.CODEX_HOME = Path(self.tempdir.name) / ".codex"
        self.addCleanup(setattr, sd, "CODEX_HOME", self.old_codex_home)
        self.sessions = sd.CODEX_HOME / "sessions" / "2026" / "08" / "31"
        self.sessions.mkdir(parents=True)

    def write_rollout(self, session_id, created_at, messages, parent_id=""):
        path = self.sessions / f"rollout-{session_id}.jsonl"
        records = [
            {
                "timestamp": created_at,
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "session_id": session_id,
                    "forked_from_id": parent_id or None,
                    "timestamp": created_at,
                },
            }
        ]
        for timestamp, role, text in messages:
            records.append(
                {
                    "timestamp": timestamp,
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": role,
                        "content": [{"type": "input_text", "text": text}],
                    },
                }
            )
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        return path

    def test_nested_fork_includes_only_parent_prefixes(self):
        main_id = "00000000-0000-0000-0000-000000000001"
        branch_id = "00000000-0000-0000-0000-000000000002"
        leaf_id = "00000000-0000-0000-0000-000000000003"
        self.write_rollout(
            main_id,
            "2026-08-31T00:00:00Z",
            [
                ("2026-08-31T00:01:00Z", "user", "main before fork"),
                ("2026-08-31T00:02:00Z", "assistant", "main answer"),
                ("2026-08-31T00:03:00.500Z", "user", "main fractional after fork"),
                ("2026-08-31T00:10:00Z", "user", "main after fork"),
            ],
        )
        self.write_rollout(
            branch_id,
            "2026-08-31T00:03:00Z",
            [
                ("2026-08-31T00:04:00Z", "user", "branch before leaf"),
                ("2026-08-31T00:05:00Z", "assistant", "branch answer"),
                ("2026-08-31T00:08:00Z", "user", "branch after leaf fork"),
            ],
            parent_id=main_id,
        )
        leaf = self.write_rollout(
            leaf_id,
            "2026-08-31T00:06:00Z",
            [("2026-08-31T00:07:00Z", "user", "selected leaf")],
            parent_id=branch_id,
        )

        transcript = sd.parse_codex(leaf)

        self.assertEqual(
            [event.text for event in transcript.events],
            [
                "main before fork",
                "main answer",
                "branch before leaf",
                "branch answer",
                "selected leaf",
            ],
        )
        self.assertEqual(
            transcript.stats["lineage_session_ids"],
            [main_id, branch_id, leaf_id],
        )
        self.assertTrue(transcript.stats["lineage_complete"])

    def test_selected_sibling_does_not_include_other_branch(self):
        main_id = "00000000-0000-0000-0000-000000000010"
        sibling_id = "00000000-0000-0000-0000-000000000011"
        selected_id = "00000000-0000-0000-0000-000000000012"
        self.write_rollout(
            main_id,
            "2026-08-31T00:00:00Z",
            [
                ("2026-08-31T00:01:00Z", "user", "shared main history"),
                ("2026-08-31T00:09:00Z", "user", "main after selected fork"),
            ],
        )
        self.write_rollout(
            sibling_id,
            "2026-08-31T00:02:00Z",
            [("2026-08-31T00:03:00Z", "user", "unselected sibling")],
            parent_id=main_id,
        )
        selected = self.write_rollout(
            selected_id,
            "2026-08-31T00:04:00Z",
            [("2026-08-31T00:05:00Z", "user", "selected sibling")],
            parent_id=main_id,
        )

        transcript = sd.parse_codex(selected)

        self.assertEqual(
            [event.text for event in transcript.events],
            ["shared main history", "selected sibling"],
        )

    def test_missing_parent_is_partial_not_error(self):
        missing_id = "00000000-0000-0000-0000-000000000099"
        leaf_id = "00000000-0000-0000-0000-000000000100"
        leaf = self.write_rollout(
            leaf_id,
            "2026-08-31T00:06:00Z",
            [("2026-08-31T00:07:00Z", "user", "selected leaf")],
            parent_id=missing_id,
        )

        transcript = sd.parse_codex(leaf)

        self.assertEqual([event.text for event in transcript.events], ["selected leaf"])
        self.assertEqual(
            transcript.stats["lineage_missing_parent_ids"], [missing_id]
        )
        self.assertFalse(transcript.stats["lineage_complete"])
        rendered = sd.render_markdown(transcript, mode="dialogue")
        self.assertIn("lineage_status", rendered)
        self.assertIn(missing_id, rendered)


if __name__ == "__main__":
    unittest.main()
