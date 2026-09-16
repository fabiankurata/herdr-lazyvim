-- Two-process cancellation regression: cancellation runs while the real
-- kernel-lock acquisition yields, before the public add reaches its commit.
local repo = assert(vim.env.HERDR_TEST_REPO)
local configured_root = assert(vim.env.HERDR_REVIEW_ROOT)
local root = assert(vim.uv.fs_realpath(configured_root))
local control = assert(vim.env.HERDR_REVIEW_CONTROL)
local role = assert(vim.env.HERDR_REVIEW_ROLE)
vim.opt.runtimepath:prepend(repo .. "/nvim")

local function marker(name) return control .. "/" .. name end
local function touch(name) assert(vim.fn.writefile({ "ready" }, marker(name)) == 0) end
local function wait_for(name)
  assert(vim.wait(5000, function() return vim.fn.filereadable(marker(name)) == 1 end, 10), name .. " barrier timed out")
end
local function review()
  return require("herdr_feedback.context").review_for_root(root)
end

if role == "holder" then
  local store = require("herdr_feedback.store")
  local destination = assert(store.path(review()))
  local open = vim.uv.fs_open
  vim.uv.fs_open = function(path, flags, mode, callback)
    local file, message, code = open(path, flags, mode, callback)
    if not callback and file and path:find(destination .. ".tmp-", 1, true) == 1 then
      touch("holder-locked")
      wait_for("release-holder")
    end
    return file, message, code
  end
  local value, error = store.update(review(), function(records)
    table.insert(records, {
      id = "00000000-0000-4000-8000-0000000000a1", revision = 1,
      file = "example.lua", start = 1, finish = 1, lines = "source", text = "holder mutation",
    })
    return records, true
  end)
  assert(value and not error, "holder write failed")
  touch("holder-done")
  vim.cmd("qa!")
  return
end

vim.cmd("edit " .. vim.fn.fnameescape(root .. "/example.lua"))
local feedback = require("herdr_feedback")
if role == "cancelled" then
  local result, callbacks
  callbacks = 0
  local operation = feedback.add({ bufnr = vim.api.nvim_get_current_buf(), range = { start_line = 1, end_line = 1 }, text = "cancelled mutation" }, function(value)
    callbacks = callbacks + 1
    result = value
  end)
  vim.schedule(function() operation.cancel(); touch("cancel-issued") end)
  assert(vim.wait(5000, function() return result ~= nil end, 10), "cancelled operation did not complete")
  assert(vim.fn.writefile({ vim.json.encode({
    callbacks = callbacks,
    code = result.ok and "ok" or result.error.code,
  }) }, marker("cancelled-result.json")) == 0)
  touch("cancelled-done")
  vim.cmd("qa!")
  return
end

assert(role == "writer", "unknown role")
local result, callbacks
callbacks = 0
feedback.add({ bufnr = vim.api.nvim_get_current_buf(), range = { start_line = 1, end_line = 1 }, text = "subsequent mutation" }, function(value)
  callbacks = callbacks + 1
  result = value
end)
assert(vim.wait(5000, function() return result ~= nil end, 10), "subsequent writer did not complete")
assert(callbacks == 1 and result.ok, "subsequent writer could not acquire released lock")
touch("writer-done")
vim.cmd("qa!")
