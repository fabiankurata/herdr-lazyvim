#!/usr/bin/env python3
"""Check the cross-record invariants promised by the version 1 fixtures."""
import hashlib
import json
from pathlib import Path
import uuid

ROOT = Path(__file__).resolve().parents[2] / "docs/contracts"


def read(name):
    return json.loads((ROOT / f"{name}-v1.json").read_text())


def key(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def check():
    context = read("context")
    session = read("session")
    annotation = read("annotation")
    batch = read("delivery")
    receipt = read("receipt")
    adapter = read("source-adapter")
    workspace_key = context["workspace_session_key"]
    tab_key = context["tab_session_key"]
    assert workspace_key["scope"] == "workspace" and "tab_id" not in workspace_key
    assert tab_key["scope"] == "tab" and tab_key["tab_id"] == "tab:1"
    assert key(workspace_key) != key(tab_key)
    assert key(workspace_key) != key(context["other_server_session_key"])
    assert key(context["review"]) != key(context["linked_worktree_review"])
    assert session["session_key"] == workspace_key
    assert session["operation"]["desired_action"] == "hide"
    assert session["operation"]["outcome"] == "uncertain"
    assert session["operation"]["guarded_tabs"] == ["owned-parking:1", "tab:1"]
    for record in (session, annotation, batch, receipt):
        assert uuid.UUID(record.get("id", record.get("session_id"))).version == 4
    assert type(annotation["revision"]) is int and annotation["revision"] == 1
    assert annotation["schema_version"] == receipt["schema_version"] == 1
    assert batch["api_version"] == adapter["api_version"] == 1
    assert annotation["review"] == batch["review"] == receipt["review"] == context["review"]
    source = annotation["source"]
    assert source["location"]["worktree"] == context["review"]["worktree"]
    assert source["range"] == {"version": 1, "kind": "complete_lines", "start_line": 2, "end_line": 3}
    assert len(source["lines"]) == 2
    assert source["content_sha256"] == hashlib.sha256(("\n".join(source["lines"]) + "\n").encode()).hexdigest()
    assert "buffer" not in source and "bufnr" not in source["location"]
    assert adapter["resolve"]["result"]["value"] == source["location"]
    assert adapter["capture"]["result"]["value"] == source
    assert batch["members"] == [{"id": annotation["id"], "revision": 1}]
    assert batch["payload"] == (
        "src/example.ts:2-3\n```\nexport const enabled = true;\n"
        "export const retries = 2;\n```\nExplain the retry budget.\n"
    )
    assert receipt["batch_id"] == batch["id"]
    assert receipt["acknowledged"] == batch["members"]
    assert receipt["outcome"] == "delivered_to_input"
    assert batch["target"]["connection"] == context["connection"]
    assert batch["submit"] is False and batch["strict_session_guard"] is True
    print("PASS: version 1 fixture identities, captures, batches, and receipts agree")


if __name__ == "__main__":
    check()
