import unittest
from pathlib import Path
from types import SimpleNamespace

import run


class ComposerReadinessTests(unittest.TestCase):
    def test_requires_owned_markdown_composer_in_insert_mode(self):
        self.assertTrue(run.is_comment_composer({
            "mode": "i", "filetype": "markdown", "buftype": "nofile",
        }))
        self.assertFalse(run.is_comment_composer({
            "mode": "n", "filetype": "markdown", "buftype": "nofile",
        }))
        self.assertFalse(run.is_comment_composer({
            "mode": "i", "filetype": "lua", "buftype": "",
        }))

    def test_requires_exact_comment_text(self):
        composer = {
            "mode": "i", "filetype": "markdown", "buftype": "nofile",
        }
        self.assertTrue(run.is_comment_text_ready({**composer, "lines": ["profile keyboard comment"]}))
        self.assertFalse(run.is_comment_text_ready({**composer, "lines": ["wrong text"]}))
        self.assertFalse(run.is_comment_text_ready(composer))

    def test_remote_expression_evidence_parses_and_redacts_failures(self):
        root = Path("/tmp/hf-private")
        success = run.remote_expression_observation(SimpleNamespace(
            returncode=0, stdout='{"mode":"i"}', stderr=""), root)
        self.assertEqual(success, {"category": "success", "state": {"mode": "i"}})
        failure = run.remote_expression_observation(SimpleNamespace(
            returncode=1, stdout=str(root / "stdout"), stderr=str(root / "stderr")), root)
        self.assertEqual(failure["category"], "remote-expr-exit")
        self.assertNotIn(str(root), failure["stdout"] + failure["stderr"])


if __name__ == "__main__":
    unittest.main()
