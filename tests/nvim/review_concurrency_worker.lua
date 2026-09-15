local repo = assert(vim.env.HERDR_TEST_REPO, "HERDR_TEST_REPO is required")
local configured_root = assert(vim.env.HERDR_REVIEW_ROOT, "HERDR_REVIEW_ROOT is required")
local root = assert(vim.uv.fs_realpath(configured_root))
local role = assert(vim.env.HERDR_REVIEW_ROLE, "HERDR_REVIEW_ROLE is required")
local control = assert(vim.env.HERDR_REVIEW_CONTROL, "HERDR_REVIEW_CONTROL is required")

vim.opt.runtimepath:prepend(repo .. "/nvim")
vim.env.HERDR_WORKSPACE_ID = "fixture-workspace"
vim.env.HERDR_PANE_ID = "fixture-editor"
vim.env.HERDR_BIN_PATH = "FAKE-HERDR-REVIEW-CONCURRENCY"
vim.notify = function() end

local function marker(name)
  return control .. "/" .. name
end

local function touch(name)
  assert(vim.fn.writefile({ "ready" }, marker(name)) == 0)
end

local function save_composer(text)
  local win = vim.api.nvim_get_current_win()
  vim.api.nvim_buf_set_lines(vim.api.nvim_win_get_buf(win), 0, -1, false, { text })
  vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes("<C-s>", true, false, true), "xt", false)
end

vim.cmd("edit " .. vim.fn.fnameescape(root .. "/example.lua"))
vim.api.nvim_win_set_cursor(0, { 1, 0 })
local review = require("herdr_review")

if role == "writer" then
  vim.ui.select = function(items, _, callback)
    callback(items[1])
  end
  review.list()
  review.edit()
  save_composer("revision two")
  assert(vim.wait(1000, function()
    local state = vim.fn.stdpath("state") .. "/herdr-review/" .. vim.fn.sha256(root):sub(1, 16) .. ".json"
    local decoded = vim.json.decode(table.concat(vim.fn.readfile(state), "\n"))
    return decoded.comments[1].revision == 2 and decoded.comments[1].text == "revision two"
  end, 5), "writer edit did not persist")
  review.comment(false)
  save_composer("new annotation")
  assert(vim.wait(1000, function()
    local state = vim.fn.stdpath("state") .. "/herdr-review/" .. vim.fn.sha256(root):sub(1, 16) .. ".json"
    local decoded = vim.json.decode(table.concat(vim.fn.readfile(state), "\n"))
    return #decoded.comments == 2
  end, 5), "writer annotation did not persist")
  touch("writer-done")
  vim.cmd("qa!")
  return
end

local delivered = false
local scenario = assert(vim.env.HERDR_REVIEW_SCENARIO, "HERDR_REVIEW_SCENARIO is required")
vim.ui.select = function(_, _, callback)
  callback(nil)
end
vim.system = function(argv, _, callback)
  if argv[2] == "agent" and argv[3] == "list" then
    if scenario == "discovery-failure" then
      callback({ code = 1, stdout = "", stderr = "fixture unavailable" })
    else
      callback({
        code = 0,
        stdout = vim.json.encode({ result = { agents = {
          { workspace_id = "fixture-workspace", tab_id = "fixture-tab", pane_id = "agent-1", agent = "one" },
          { workspace_id = "fixture-workspace", tab_id = "fixture-tab", pane_id = "agent-2", agent = "two" },
        } } }),
        stderr = "",
      })
    end
  elseif argv[2] == "tab" and argv[3] == "list" then
    callback({ code = 0, stdout = '{"result":{"tabs":[]}}', stderr = "" })
  elseif argv[2] == "pane" and argv[3] == "send-text" then
    delivered = true
    callback({ code = 0, stdout = "{}", stderr = "" })
  end
  return {}
end

review.list()
touch("reader-ready")
assert(vim.wait(5000, function()
  return vim.fn.filereadable(marker("continue")) == 1
end, 10), "reader continuation timed out")

if scenario == "empty-selection" then
  review.send({ annotation_ids = {} })
else
  review.send()
end
vim.wait(200, function() return false end, 10)
assert(not delivered, "non-delivery scenario sent input")
touch("reader-done")
vim.cmd("qa!")
