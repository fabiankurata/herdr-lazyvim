# Clipboard helper

`clipboard.swift` is a macOS-only line-JSON helper for the Appendix A native clipboard experiment. Named pasteboards must be fresh `org.openai.pr00.clipboard.` names with a 32-character lowercase hexadecimal suffix. Access to `NSPasteboard.general` requires the separate explicit `--general` flag.

At startup the helper copies every item, type, and materialized representation into memory and accepts no mutation unless that copy has a stable change count. It never writes those original bytes, strings, or type data to receipts or diagnostic streams. Receipts contain only status, reason, synthetic comparison state, and pasteboard change counts.

Commands are one JSON object per input line:

```json
{"op":"seed","nonce":"fixture-owned-nonce"}
{"op":"check","expected":"fixture-yank-nonce"}
{"op":"restore"}
{"op":"terminate"}
```

`seed` writes one permitted synthetic text item and records the resulting complete pasteboard snapshot as owned. `check` accepts only that exact one-item permitted-text shape written by the fixture; a matching string inside a mixed clipboard, extra item, or unknown representation remains `UNVERIFIED`. A successful clear is first recorded as an owned empty snapshot, so a subsequent synthetic-write failure can restore the original only if that empty state still belongs to the helper. `restore`, `terminate`, EOF, `SIGTERM`, and timeout restore the original items only when both the change count and complete current contents still match the owned snapshot. A changed or unstable pasteboard returns `UNVERIFIED` and leaves the newer contents in place. The first terminal result is retained: later restore calls repeat that verdict, while seed and check reject a finalized session.

`--idle-timeout-ms` is a hard lifetime from process startup, despite its historical name; input does not extend it. The helper wakes its main loop on `SIGTERM` and performs the same ownership check there. The test-only `--test-snapshot-unavailable` and `--test-fail-after-clear` switches are rejected for `--general` and exist only to exercise failure recovery on a fresh named board. Invalid named aliases and `--general` with either test switch exit before the helper reports readiness or accesses a pasteboard.

The helper proves only synthetic pasteboard equality. It does not prove that Neovim pasted or yanked, and it does not create native UI evidence. A process crash, `SIGKILL`, host crash, or termination that prevents normal Swift cleanup can leave the synthetic value in the pasteboard because AppKit restoration cannot safely run from a signal handler. NSPasteboard has no compare-and-swap write, so a third-party write in the narrow interval after the ownership check and before `clearContents()` cannot be made atomic. A future general-pasteboard run must treat both limits as explicit recovery risks and retain a bounded helper lifetime.

Run the isolated named-pasteboard checks on macOS:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests/native/test_clipboard.py
```
