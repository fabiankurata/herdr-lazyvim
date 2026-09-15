import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("comment_operations", Path(__file__).with_name("comment_operations.py"))
comment_operations = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(comment_operations)


class CommentOperationToolTests(unittest.TestCase):
    def test_closed_origin_fixture_keeps_the_composer_and_unrelated_window_distinct(self):
        fixture = Path(__file__).with_name("comment_operations_fixture.lua").read_text()
        closed_origin = fixture.split('elseif name == "closed-origin" then', 1)[1].split('elseif name == "new-comment"', 1)[0]
        self.assertIn("local source_window = vim.api.nvim_get_current_win()", closed_origin)
        self.assertIn("active.b_window = window_snapshot(b_window)", closed_origin)
        self.assertIn("vim.api.nvim_win_close(source_window, true)", closed_origin)
        self.assertNotIn("vim.api.nvim_win_close(active.composer_window", closed_origin)
        self.assertIn("active.focus_after_save == active.b_window.window", fixture)
        same_window = fixture.split("local function same_window", 1)[1].split("local function begin", 1)[0]
        self.assertNotIn("vim.api.nvim_set_current_win", same_window)

    def test_ten_lanes_have_the_review_names_from_the_plan(self):
        self.assertEqual([name for name, _ in comment_operations.LANES], [
            "root-switch", "composer-root", "new-comment", "edited-comment", "cancel-send",
            "send-failure", "uncertain", "closed-origin", "restart", "batch",
        ])
        self.assertEqual([image for _, image in comment_operations.LANES], [
            "root-switch.png", "composer-root.png", "new-comment.png", "edited-comment.png", "cancel-send.png",
            "send-failure.png", "uncertain.png", "closed-origin.png", "restart.png", "batch.png",
        ])

    def test_fixture_seeds_the_same_app_specific_state_path_the_product_reads(self):
        fixture = Path(__file__).with_name("comment_operations_fixture.lua").read_text()
        self.assertIn('assert(state == vim.fn.stdpath("state")', fixture)
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            init = comment_operations.init_file(runtime, Path("/tmp/source"), Path("/tmp/fixture"), Path("/tmp/socket"))
            self.assertIn("vim.env.HERDR_TEST_STATE_ROOT = vim.fn.stdpath('state')", init.read_text())

    def test_fixture_reloads_review_state_after_every_lane_seed(self):
        fixture = Path(__file__).with_name("comment_operations_fixture.lua").read_text()
        begin = fixture.split("local function begin", 1)[1].split("local function complete", 1)[0]
        seeds = begin.rfind('else seed("a"')
        self.assertGreater(seeds, 0)
        self.assertIn("reset_review()\n  edit(\"a\")", begin[seeds:])

    def test_fixture_waits_for_send_completion_without_assuming_notice_order(self):
        fixture = Path(__file__).with_name("comment_operations_fixture.lua").read_text()
        complete = fixture.split("local function complete", 1)[1].split("local function prepare_restart", 1)[0]
        self.assertIn("has_notice(success_notice)", complete)
        self.assertNotIn("notices[1].message == success_notice", complete)

    def test_fixture_drains_the_cancelled_picker_before_the_next_lane(self):
        fixture = Path(__file__).with_name("comment_operations_fixture.lua").read_text()
        cancel = fixture.split('elseif name == "cancel-send" then', 1)[1].split('elseif name == "send-failure"', 1)[0]
        self.assertIn("active.picker_cancelled = true", fixture)
        self.assertIn("active.picker_cancelled end, 5", cancel)

    def test_headless_environment_isolated_before_nvim_resolves_stdpath(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            environment = comment_operations.isolated_nvim_environment(runtime)
            self.assertEqual(environment["NVIM_APPNAME"], comment_operations.NVIM_APPNAME)
            self.assertEqual(environment["XDG_STATE_HOME"], str(runtime / "state"))
            self.assertTrue((runtime / "config").is_dir())
            self.assertTrue((runtime / "data").is_dir())

    def test_video_without_a_qualified_excerpt_is_unverified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            class Fixture:
                def capture(self, name, **_):
                    (root / name).write_bytes(b"same PNG bytes")
                    return {"calibrated_capture": {"screen_rectangle_points": {"X": 1}}}
            video = comment_operations.ReviewVideo(root, Fixture(), {"columns": 80, "rows": 24})
            video.capture("opened")
            receipt = video.finish()
            self.assertEqual(receipt["status"], "UNVERIFIED")
            self.assertEqual(receipt["review_excerpt"]["status"], "UNVERIFIED")

    def test_review_excerpt_uses_a_contiguous_composer_interaction_interval(self):
        frames = [
            {"label": "root-switch-opened", "captured_at": 0},
            {"label": "composer-root-opened", "captured_at": 4},
            {"label": "composer-root-saved", "captured_at": 8},
            {"label": "new-comment-opened", "captured_at": 32},
            {"label": "batch-complete", "captured_at": 63},
            {"label": "batch-complete", "captured_at": 70},
        ]
        self.assertEqual(comment_operations.ReviewVideo.review_excerpt(frames), {
            "interaction": "composer-root", "start_index": 1, "end_index": 4,
        })

    def test_scenario_requires_a_passing_observable_result_before_screenshot(self):
        calls = []
        class Fixture:
            def capture(self, name, **_): calls.append(name); return {"path": name}
        class Video:
            viewport_grid = {"columns": 80, "rows": 24}
            def capture(self, name): calls.append(name)
            def capture_for(self, name): calls.append(name)
        states = iter(({}, {"composer": False}, {}, {"lane": "root-switch", "complete": True, "status": "FAIL"}))
        with mock.patch.object(comment_operations, "remote", side_effect=lambda *_: next(states)):
            with self.assertRaisesRegex(comment_operations.OwnershipError, "root-switch failed"):
                comment_operations.run_scenario("nvim", "socket", "root-switch", Fixture(), Video())
        self.assertNotIn("root-switch.png", calls)

    def test_scenario_replaces_native_composer_text_before_saving(self):
        keys = []
        class Fixture:
            def key(self, kind, text=None): keys.append((kind, text))
            def capture(self, name, **_): return {"path": name}
        class Video:
            viewport_grid = {"columns": 80, "rows": 24}
            def capture(self, _): pass
            def capture_for(self, _): pass
        draft = "edited-comment native draft"
        states = iter((
            {},
            {"composer": True, "composer_owned": True, "composer_window": 14,
             "composer_text": draft, "save_key": "cmd-enter"},
            {"window": 14, "buffer": 7, "filetype": "markdown", "text": "captured", "mode": "i", "cursor": [1, 8]},
            {"window": 14, "buffer": 7, "filetype": "markdown", "text": "captured", "mode": "n", "cursor": [1, 8]},
            {"window": 14, "buffer": 7, "filetype": "markdown", "text": "", "mode": "i", "cursor": [1, 0]},
            {"window": 14, "buffer": 7, "filetype": "markdown", "text": draft, "mode": "i", "cursor": [1, len(draft)]},
            {"composer": False, "save_complete": True},
            {"window": 3, "buffer": 2, "filetype": "lua", "text": "local fixture = true", "mode": "n", "cursor": [1, 0]},
            {},
            {"lane": "edited-comment", "complete": True, "status": "PASS"},
        ))
        with mock.patch.object(comment_operations, "remote", side_effect=lambda *_: next(states)):
            comment_operations.run_scenario("nvim", "socket", "edited-comment", Fixture(), Video())
        self.assertEqual(keys, [("escape", None), ("text", "ggVGc"), ("text", draft), ("cmd-enter", None)])


if __name__ == "__main__":
    unittest.main()
