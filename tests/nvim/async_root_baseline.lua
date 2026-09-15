local fixture = assert(vim.env.HERDR_TEST_FIXTURE_ROOT, "HERDR_TEST_FIXTURE_ROOT is required")
local repo = assert(vim.env.HERDR_TEST_REPO, "HERDR_TEST_REPO is required")
local state_root = assert(vim.env.HERDR_TEST_STATE_ROOT, "HERDR_TEST_STATE_ROOT is required")
assert(vim.fn.stdpath("state"):sub(1, #state_root) == state_root, "test must use an isolated state directory")

vim.opt.runtimepath:prepend(repo .. "/nvim")
vim.env.HERDR_WORKSPACE_ID = "fixture-workspace"
vim.env.HERDR_PANE_ID = "fixture-editor"
vim.env.HERDR_BIN_PATH = "FAKE-HERDR-NO-PROCESS"

vim.fn.mkdir(fixture .. "/worktree-a/.git", "p")
vim.fn.mkdir(fixture .. "/worktree-b/.git", "p")
local roots = {
  a = assert(vim.uv.fs_realpath(fixture .. "/worktree-a")),
  b = assert(vim.uv.fs_realpath(fixture .. "/worktree-b")),
}
local state_dir = vim.fn.stdpath("state") .. "/herdr-review"
vim.fn.mkdir(state_dir, "p")

local function state_file(root)
  return state_dir .. "/" .. vim.fn.sha256(root):sub(1, 16) .. ".json"
end

local function read(root)
  local path = state_file(root)
  if vim.fn.filereadable(path) == 0 then
    return nil
  end
  return vim.json.decode(table.concat(vim.fn.readfile(path), "\n"))
end

for label, root in pairs(roots) do
  vim.fn.writefile({ "source in " .. label }, root .. "/example.lua")
  vim.fn.writefile({ vim.json.encode({
    version = 1,
    root = root,
    comments = { { file = "example.lua", start = 1, finish = 1, lines = "source in " .. label, text = "comment from " .. label } },
  }) }, state_file(root))
end

local pending = {}
local calls = {}
vim.system = function(argv, _, callback)
  table.insert(calls, vim.deepcopy(argv))
  assert(argv[1] == "FAKE-HERDR-NO-PROCESS", "unexpected external process")
  if argv[2] == "agent" and argv[3] == "list" then
    pending.agents = assert(callback)
  elseif argv[2] == "pane" and argv[3] == "send-text" then
    pending.payload = argv[5]
    pending.send = assert(callback)
  elseif argv[2] == "agent" and argv[3] == "focus" then
    assert(callback == nil)
  else
    error("unexpected fake CLI invocation: " .. vim.inspect(argv))
  end
  return {}
end

local review = require("herdr_review")
vim.cmd.edit(roots.a .. "/example.lua")
local location = assert(review._buffer_location(0), "fixture A must resolve to a project location")
assert(location.root == roots.a, "fixture A must preserve its canonical root")
review.setup()
vim.wait(50)
review.send()
assert(pending.agents, "send must wait for in-process fake agent discovery")
vim.cmd.edit(roots.b .. "/example.lua")
pending.agents({ code = 0, stdout = vim.json.encode({ result = { agents = { {
  workspace_id = "fixture-workspace", pane_id = "fixture-agent", agent = "codex", agent_status = "idle",
} } } }), stderr = "" })
assert(vim.wait(200, function() return pending.send ~= nil end), "send callback was not registered")
assert(pending.payload:find("comment from a", 1, true), "payload must retain root A")
assert(not pending.payload:find("comment from b", 1, true), "payload must exclude root B")
pending.send({ code = 0, stdout = "{}", stderr = "" })
vim.wait(50)

local after_a, after_b = read(roots.a), read(roots.b)
assert(after_a == nil, "acknowledgement must clear only the originating root A revision")
assert(after_b and #after_b.comments == 1 and after_b.comments[1].text == "comment from b", "root B must survive A acknowledgement")
vim.fn.writefile({ vim.json.encode({ calls = calls, result = "root-switch preserved" }) }, fixture .. "/async-root-baseline.json")
print("async root baseline: preserved")
vim.cmd("qa!")
