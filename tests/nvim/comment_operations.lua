local fixture = assert(vim.env.FIXTURE_ROOT, "FIXTURE_ROOT is required")
local repo = assert(vim.env.SOURCE_REPO, "SOURCE_REPO is required")
local state_root = assert(vim.env.HERDR_TEST_STATE_ROOT, "HERDR_TEST_STATE_ROOT is required")
local count = assert(tonumber(vim.env.OPERATION_COUNT), "OPERATION_COUNT is required")

vim.opt.runtimepath:prepend(repo .. "/nvim")
vim.env.HERDR_WORKSPACE_ID = "fixture-workspace"
vim.env.HERDR_PANE_ID = "fixture-editor"
vim.env.HERDR_BIN_PATH = "FAKE-HERDR-OPERATIONS"
vim.fn.mkdir(fixture .. "/worktree/.git", "p")
local root = assert(vim.uv.fs_realpath(fixture .. "/worktree"))
vim.fn.writefile({ "fixture source" }, root .. "/example.lua")
local state_dir = vim.fn.stdpath("state") .. "/herdr-review"
vim.fn.mkdir(state_dir, "p")
local state_file = state_dir .. "/" .. vim.fn.sha256(root):sub(1, 16) .. ".json"

local started = vim.uv.hrtime()
local records = {}
for index = 1, count do
  records[index] = {
    file = "example.lua", start = 1, finish = 1, lines = "fixture source",
    text = "fixture comment " .. index,
  }
end
vim.fn.writefile({ vim.json.encode({ version = 1, root = root, comments = records }) }, state_file)
local seed_ms = (vim.uv.hrtime() - started) / 1e6

local pending = {}
vim.system = function(argv, _, callback)
  if argv[2] == "agent" and argv[3] == "list" then
    pending.list = callback
  elseif argv[2] == "pane" and argv[3] == "send-text" then
    pending.payload = argv[5]
    pending.send = callback
  elseif argv[2] == "agent" and argv[3] == "focus" then
    return {}
  else
    error("unexpected command " .. vim.inspect(argv))
  end
  return {}
end

local selected
vim.ui.select = function(items, _, callback)
  selected = #items
  callback(nil)
end
local review = require("herdr_review")
vim.cmd.edit(root .. "/example.lua")
review.setup()
vim.wait(50)
started = vim.uv.hrtime()
review.comment(false)
local editor = vim.api.nvim_get_current_buf()
vim.api.nvim_buf_set_lines(editor, 0, -1, false, { "public fixture comment" })
vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes("<C-s>", true, false, true), "x", false)
assert(vim.wait(200, function()
  local decoded = vim.json.decode(table.concat(vim.fn.readfile(state_file), "\n"))
  return #decoded.comments == count + 1 and decoded.comments[#decoded.comments].text == "public fixture comment"
end), "public comment/save did not persist the captured text")
local add_total_ms = (vim.uv.hrtime() - started) / 1e6

started = vim.uv.hrtime()
review.list()
local list_total_ms = (vim.uv.hrtime() - started) / 1e6
assert(selected == count + 1, "list must expose fixture records plus the public add")

started = vim.uv.hrtime()
review.send()
assert(pending.list, "send must use the public agent list operation")
pending.list({ code = 0, stdout = vim.json.encode({ result = { agents = { {
  workspace_id = "fixture-workspace", pane_id = "fixture-agent", agent = "fixture", agent_status = "idle",
} } } }), stderr = "" })
assert(vim.wait(200, function() return pending.send ~= nil end), "send callback was not registered")
pending.send({ code = 0, stdout = "{}", stderr = "" })
assert(vim.wait(200, function() return vim.fn.filereadable(state_file) == 0 end), "acknowledged drafts were not cleared")
local send_ack_total_ms = (vim.uv.hrtime() - started) / 1e6
assert(pending.payload:find("fixture comment " .. count, 1, true), "payload omitted final fixture comment")
assert(pending.payload:find("public fixture comment", 1, true), "payload omitted public add")

local result = {
  status = "PASS", seeded_count = count, listed_count = selected,
  seed_setup_ms = seed_ms, fake_delivery_delay_ms = 0,
  add_bookkeeping_ms = add_total_ms, add_total_ms = add_total_ms,
  list_bookkeeping_ms = list_total_ms, list_total_ms = list_total_ms,
  send_ack_bookkeeping_ms = send_ack_total_ms, send_ack_total_ms = send_ack_total_ms,
}
vim.fn.writefile({ vim.json.encode(result) }, assert(vim.env.OPERATION_RESULT))
print(vim.json.encode(result))
vim.cmd("qa!")
