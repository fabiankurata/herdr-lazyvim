#!/usr/bin/env python3
"""Stateful synthetic pane API. Unknown operations fail without pretending."""
import json
import os
from pathlib import Path
import sys


def initial_state():
    return {
        "generation": 0, "calls": [], "focused_pane": "w:p1", "zoomed": {},
        "panes": {"w:p1": {"pane_id": "w:p1", "tab_id": "w:t1", "agent": "codex",
                             "foreground_cwd": "/tmp", "process_id": "fake-agent-1", "input_hex": ""}},
        "next_pane": 2, "next_tab": 2,
    }


def apply(state, args):
    command = tuple(args[:2])
    def option(name, default=None):
        return args[args.index(name) + 1] if name in args else default
    def pane_id():
        return option("--pane", args[2] if len(args) > 2 else None)
    if command in {("api", "snapshot"), ("session", "snapshot")}:
        return dict(state)
    if command == ("pane", "list"):
        return {"panes": list(state["panes"].values())}
    if command == ("pane", "process-info"):
        pane = state["panes"][pane_id()]
        return {"process_info": {"foreground_processes": [], "identity": pane["process_id"]}}
    if command == ("plugin", "pane") and args[2] == "open":
        identity = "w:p" + str(state["next_pane"])
        target = state["panes"][option("--target-pane", "w:p1")]
        state["next_pane"] += 1
        state["panes"][identity] = {"pane_id": identity, "tab_id": target["tab_id"],
                                     "agent": "", "process_id": "fake-editor-" + identity,
                                     "input_hex": ""}
        state["generation"] += 1
        return {"pane": state["panes"][identity]}
    if command[0] != "pane":
        raise ValueError("unsupported_fake_command")
    pane = state["panes"][pane_id()]
    if command == ("pane", "get"):
        return {"pane": pane}
    if command == ("pane", "move"):
        if "--new-tab" in args:
            target = "w:t" + str(state["next_tab"])
            state["next_tab"] += 1
        else:
            target = option("--tab")
        if not target:
            raise ValueError("missing_target_tab")
        previous = pane["tab_id"]
        if previous != target and state["zoomed"].get(previous) == pane["pane_id"]:
            del state["zoomed"][previous]
        pane["tab_id"] = target
    elif command == ("pane", "zoom"):
        state["zoomed"][pane["tab_id"]] = None if "--off" in args else pane["pane_id"]
    elif command == ("pane", "focus"):
        state["focused_pane"] = pane["pane_id"]
    elif command == ("pane", "resize"):
        pane["last_resize"] = {"direction": option("--direction"), "amount": option("--amount")}
    elif command == ("pane", "send"):
        pane["input_hex"] += bytes.fromhex(option("--hex", "")).hex()
    elif command == ("pane", "close"):
        del state["panes"][pane["pane_id"]]
        if state["zoomed"].get(pane["tab_id"]) == pane["pane_id"]:
            del state["zoomed"][pane["tab_id"]]
        if state["focused_pane"] == pane["pane_id"]:
            neighbors = [p["pane_id"] for p in state["panes"].values() if p["tab_id"] == pane["tab_id"]]
            state["focused_pane"] = next(iter(neighbors or state["panes"]), None)
    else:
        raise ValueError("unsupported_fake_command")
    state["generation"] += 1
    return {"pane": pane, "generation": state["generation"]}


def main():
    path = Path(os.environ["HERDR_TEST_MODEL"])
    calls = Path(os.environ["HERDR_TEST_CALLS"])
    args = sys.argv[1:]
    with calls.open("ab") as stream:
        for arg in args:
            stream.write(os.fsencode(arg) + b"\0")
    state = json.loads(path.read_text())
    state["calls"].append(args)
    failure = state.pop("fail_next", None)
    try:
        if failure == "before":
            raise RuntimeError("injected_before_mutation")
        result = apply(state, args)
        if failure == "after":
            raise RuntimeError("injected_after_mutation")
        print(json.dumps({"result": result}))
        code = 0
    except (ValueError, KeyError, IndexError, RuntimeError) as exc:
        print(json.dumps({"error": {"code": str(exc), "args": args}}))
        code = 1
    path.write_text(json.dumps(state, indent=2) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
