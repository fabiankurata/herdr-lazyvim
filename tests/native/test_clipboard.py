import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid


HERE = Path(__file__).resolve().parent

PROBE = r'''
import AppKit
import Foundation
let mode = CommandLine.arguments[1]
let board = NSPasteboard(name: NSPasteboard.Name(CommandLine.arguments[2]))
func emit(_ value: Any) {
 let data = try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
 print(String(data: data, encoding: .utf8)!)
}
func original() -> [NSPasteboardItem] {
 let first = NSPasteboardItem(); first.setString("first", forType: .string); first.setData(Data([0, 1, 2]), forType: NSPasteboard.PasteboardType("org.openai.pr00.one"))
 let second = NSPasteboardItem(); second.setString("second", forType: .string); second.setData(Data([3, 4]), forType: NSPasteboard.PasteboardType("org.openai.pr00.two"))
 return [first, second]
}
if mode == "original" { _ = board.clearContents(); precondition(board.writeObjects(original())) }
if mode == "replace" { let item = NSPasteboardItem(); item.setString(CommandLine.arguments[3], forType: .string); _ = board.clearContents(); precondition(board.writeObjects([item])) }
if mode == "clear" { _ = board.clearContents() }
if mode == "inspect" {
 let items = (board.pasteboardItems ?? []).map { item in
  ["representations": item.types.map { type in ["type": type.rawValue, "data": item.data(forType: type)!.base64EncodedString()] }]
 }
 emit(["items": items, "change_count": board.changeCount])
}
'''


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"),
                     "UNVERIFIED: NSPasteboard helper requires macOS AppKit and swiftc")
class ClipboardHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        root = Path(cls.build.name)
        cls.helper = root / "clipboard-helper"
        cls.probe = root / "pasteboard-probe"
        probe_source = root / "probe.swift"
        probe_source.write_text(PROBE)
        subprocess.run(["swiftc", str(HERE / "clipboard.swift"), "-o", str(cls.helper)], check=True)
        subprocess.run(["swiftc", str(probe_source), "-o", str(cls.probe)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.name = "org.openai.pr00.clipboard." + uuid.uuid4().hex
        self.processes = []
        self.probe_command("clear")

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                try:
                    process.stdin.write(json.dumps({"op": "terminate"}) + "\n")
                    process.stdin.flush()
                    process.wait(timeout=3)
                except (BrokenPipeError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        self.probe_command("clear")

    def probe_command(self, mode, *arguments):
        result = subprocess.run([str(self.probe), mode, self.name, *arguments], text=True,
                                capture_output=True, check=True, timeout=5)
        return json.loads(result.stdout) if result.stdout else None

    def original(self):
        self.probe_command("original")
        return self.probe_command("inspect")["items"]

    def start(self, *, idle_timeout_ms=2_000, snapshot_available=True, fail_after_clear=False):
        options = []
        if not snapshot_available:
            options.append("--test-snapshot-unavailable")
        if fail_after_clear:
            options.append("--test-fail-after-clear")
        process = subprocess.Popen([str(self.helper), "--pasteboard", self.name,
                                    "--idle-timeout-ms", str(idle_timeout_ms), *options], text=True,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ready = json.loads(process.stdout.readline())
        self.assertEqual(ready["event"], "ready")
        self.assertEqual(ready["status"], "PASS" if snapshot_available else "UNVERIFIED")
        self.processes.append(process)
        return process

    def test_rejects_arbitrary_or_general_alias_arguments_before_helper_ready_or_mutation(self):
        expected = self.original()
        for arguments in (
                ("--pasteboard", "org.example.untrusted.clipboard"),
                ("--pasteboard", "general"),
                ("--general", "--test-snapshot-unavailable")):
            with self.subTest(arguments=arguments):
                result = subprocess.run([str(self.helper), *arguments], text=True, input="",
                                        capture_output=True, timeout=3)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage:", result.stderr)
                self.assertEqual(self.probe_command("inspect")["items"], expected)

    def send(self, process, command):
        process.stdin.write(json.dumps(command) + "\n")
        process.stdin.flush()
        return json.loads(process.stdout.readline())

    def finish_eof(self, process):
        process.stdin.close()
        final = json.loads(process.stdout.readline())
        process.wait(timeout=3)
        self.assertEqual(process.returncode, 0, process.stderr.read())
        return final

    def test_restores_every_item_and_materialized_format(self):
        expected = self.original()
        process = self.start()
        seeded = self.send(process, {"op": "seed", "nonce": "synthetic-nonce"})
        self.assertEqual(seeded["status"], "PASS")
        self.assertNotIn("first", json.dumps(seeded))
        self.assertNotIn("org.openai.pr00", json.dumps(seeded))
        restored = self.send(process, {"op": "restore"})
        self.assertEqual(restored["restoration"], "restored")
        self.assertEqual(self.probe_command("inspect")["items"], expected)
        self.assertEqual(self.send(process, {"op": "terminate"})["restoration"], "restored")
        process.wait(timeout=3)

    def test_accepts_fixture_expected_synthetic_string_then_restores(self):
        expected = self.original()
        process = self.start()
        self.assertEqual(self.send(process, {"op": "seed", "nonce": "seed-value"})["synthetic"], "seeded")
        self.probe_command("replace", "fixture-yank")
        receipt = self.send(process, {"op": "check", "expected": "fixture-yank"})
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["synthetic"], "matched")
        self.assertEqual(self.send(process, {"op": "terminate"})["restoration"], "restored")
        process.wait(timeout=3)
        self.assertEqual(self.probe_command("inspect")["items"], expected)

    def test_check_rejects_an_expected_string_inside_a_mixed_clipboard(self):
        self.original()
        process = self.start()
        receipt = self.send(process, {"op": "check", "expected": "first"})
        self.assertEqual(receipt["status"], "UNVERIFIED")
        self.assertEqual(receipt["synthetic"], "mismatch")
        self.send(process, {"op": "terminate"})
        process.wait(timeout=3)

    def test_intervening_change_is_preserved(self):
        self.original()
        process = self.start()
        self.assertEqual(self.send(process, {"op": "seed", "nonce": "owned-value"})["status"], "PASS")
        self.probe_command("replace", "newer-user-value")
        receipt = self.send(process, {"op": "restore"})
        self.assertEqual(receipt["status"], "UNVERIFIED")
        self.assertEqual(receipt["reason"], "clipboard-ownership-changed")
        self.assertEqual(self.probe_command("inspect")["items"][0]["representations"][0]["data"], "bmV3ZXItdXNlci12YWx1ZQ==")
        self.send(process, {"op": "terminate"})
        process.wait(timeout=3)

    def test_refuses_mutation_when_original_snapshot_is_no_longer_owned(self):
        self.original()
        process = self.start()
        self.probe_command("replace", "changed-before-seed")
        receipt = self.send(process, {"op": "seed", "nonce": "must-not-write"})
        self.assertEqual(receipt["status"], "UNVERIFIED")
        self.assertEqual(receipt["reason"], "clipboard-ownership-changed")
        self.assertEqual(self.probe_command("inspect")["items"][0]["representations"][0]["data"], "Y2hhbmdlZC1iZWZvcmUtc2VlZA==")
        self.send(process, {"op": "terminate"})
        process.wait(timeout=3)

    def test_unavailable_original_snapshot_refuses_mutation(self):
        expected = self.original()
        process = self.start(snapshot_available=False)
        receipt = self.send(process, {"op": "seed", "nonce": "must-not-write"})
        self.assertEqual(receipt, {"status": "UNVERIFIED", "reason": "original-snapshot-unavailable"})
        self.assertEqual(self.probe_command("inspect")["items"], expected)
        self.assertEqual(self.send(process, {"op": "terminate"}), receipt)
        process.wait(timeout=3)

    def test_partial_seed_failure_restores_only_the_owned_clear_state(self):
        expected = self.original()
        process = self.start(fail_after_clear=True)
        failed = self.send(process, {"op": "seed", "nonce": "write-failure"})
        self.assertEqual(failed["status"], "FAIL")
        self.assertEqual(failed["reason"], "synthetic-write-failed")
        self.assertEqual(failed["restoration"]["restoration"], "restored")
        self.assertEqual(self.probe_command("inspect")["items"], expected)
        self.assertEqual(self.send(process, {"op": "restore"}), failed)
        self.assertEqual(self.send(process, {"op": "seed", "nonce": "after-final"})["reason"], "clipboard-session-finalized")
        process.wait(timeout=3)

    def test_eof_restores_owned_clipboard(self):
        expected = self.original()
        process = self.start()
        self.send(process, {"op": "seed", "nonce": "eof-value"})
        final = self.finish_eof(process)
        self.assertEqual(final["reason"], "eof")
        self.assertEqual(final["restoration"]["restoration"], "restored")
        self.assertEqual(self.probe_command("inspect")["items"], expected)

    def test_idle_timeout_restores_owned_clipboard(self):
        expected = self.original()
        process = self.start(idle_timeout_ms=100)
        self.send(process, {"op": "seed", "nonce": "timeout-value"})
        final = json.loads(process.stdout.readline())
        process.wait(timeout=3)
        self.assertEqual(final["reason"], "idle-timeout")
        self.assertEqual(final["restoration"]["restoration"], "restored")
        self.assertEqual(self.probe_command("inspect")["items"], expected)

    def test_sigterm_wakes_the_helper_for_owned_restoration(self):
        expected = self.original()
        process = self.start()
        self.send(process, {"op": "seed", "nonce": "sigterm-value"})
        process.terminate()
        final = json.loads(process.stdout.readline())
        process.wait(timeout=3)
        self.assertEqual(final["reason"], "sigterm")
        self.assertEqual(final["restoration"]["restoration"], "restored")
        self.assertEqual(self.probe_command("inspect")["items"], expected)


if __name__ == "__main__":
    unittest.main()
