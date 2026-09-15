local repo = assert(vim.env.HERDR_TEST_REPO, "HERDR_TEST_REPO is required")
local configured_root = assert(vim.env.HERDR_REVIEW_ROOT, "HERDR_REVIEW_ROOT is required")
local root = assert(vim.uv.fs_realpath(configured_root))
local role = assert(vim.env.HERDR_REVIEW_ROLE, "HERDR_REVIEW_ROLE is required")
local scenario = assert(vim.env.HERDR_REVIEW_SCENARIO, "HERDR_REVIEW_SCENARIO is required")
local control = assert(vim.env.HERDR_REVIEW_CONTROL, "HERDR_REVIEW_CONTROL is required")

vim.opt.runtimepath:prepend(repo .. "/nvim")
vim.env.HERDR_WORKSPACE_ID = "fixture-workspace"
vim.env.HERDR_PANE_ID = "fixture-editor"
vim.env.HERDR_BIN_PATH = "FAKE-HERDR-REVIEW-LOCK"
local notices = {}
vim.notify = function(message) table.insert(notices, message) end

local function marker(name)
  return control .. "/" .. name
end

local function touch(name)
  assert(vim.fn.writefile({ "ready" }, marker(name)) == 0)
end

local function exists(name)
  return vim.fn.filereadable(marker(name)) == 1
end

local function wait_for(name)
  assert(vim.wait(5000, function() return exists(name) end, 10), name .. " barrier timed out")
end

local state = vim.fn.stdpath("state") .. "/herdr-review/" .. vim.fn.sha256(root):sub(1, 16) .. ".json"
local legacy_lock = state .. ".lock"
local kernel_lock = state .. ".lock-v2"
local real_open = vim.uv.fs_open
local real_close = vim.uv.fs_close
local real_mkdir = vim.uv.fs_mkdir
local real_rename = vim.uv.fs_rename
local real_rmdir = vim.uv.fs_rmdir
local real_writefile = vim.fn.writefile
local kernel_descriptor

if scenario == "stale-reclaim" and role == "first" then
  vim.uv.fs_rename = function(source, target, callback)
    if not callback and source == legacy_lock and target:find(legacy_lock .. ".stale-", 1, true) == 1 then
      touch("first-legacy-reclaim")
      wait_for("second-legacy-snapshot")
    end
    return real_rename(source, target, callback)
  end
end

vim.uv.fs_open = function(path, flags, mode, callback)
  local descriptor, message, code = real_open(path, flags, mode, callback)
  if not callback and path == kernel_lock and descriptor then kernel_descriptor = descriptor end
  if not callback and path:find(state .. ".tmp-", 1, true) == 1 then
    if scenario == "stale-reclaim" and role == "first" and not exists("first-legacy-reclaim") then
      touch("first-kernel-snapshot")
      wait_for("release-first")
    elseif scenario == "stale-reclaim" and role == "second" and exists("first-legacy-reclaim") then
      touch("second-legacy-snapshot")
      wait_for("first-committed")
    elseif scenario == "live-refusal" and role == "owner" then
      touch("owner-snapshot")
      wait_for("release-owner")
    elseif scenario == "abandon-acquire" then
      touch("kernel-acquired")
      wait_for("never-release")
    end
  end
  return descriptor, message, code
end

if scenario == "abandon-acquire" then
  vim.uv.fs_mkdir = function(path, mode, callback)
    local created, message, code = real_mkdir(path, mode, callback)
    if not callback and path == legacy_lock and created then
      touch("legacy-owner-unpublished")
      wait_for("never-release")
    end
    return created, message, code
  end
end

if scenario == "abandon-release" then
  vim.uv.fs_rmdir = function(path, callback)
    if not callback and path == legacy_lock then
      touch("legacy-owner-removed")
      wait_for("never-release")
    end
    return real_rmdir(path, callback)
  end
  vim.uv.fs_close = function(descriptor, callback)
    if not callback and descriptor == kernel_descriptor then
      touch("kernel-release-pending")
      wait_for("never-release")
    end
    return real_close(descriptor, callback)
  end
end

vim.cmd("edit " .. vim.fn.fnameescape(root .. "/example.lua"))
vim.api.nvim_win_set_cursor(0, { 1, 0 })
local review = require("herdr_review")

if scenario == "stale-reclaim" and role == "second" then touch("second-started") end

local text = ({
  first = "first update",
  second = "second update",
  owner = "owner update",
  contender = "contender draft",
  abandoned = "abandoned update",
  recovery = "recovery update",
})[role]
assert(text, "unknown lock worker role")
review.comment(false)
local composer = vim.api.nvim_get_current_win()
vim.api.nvim_buf_set_lines(vim.api.nvim_win_get_buf(composer), 0, -1, false, { text })
vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes("<C-s>", true, false, true), "xt", false)

if scenario == "live-refusal" and role == "contender" then
  assert(vim.wait(2000, function()
    for _, message in ipairs(notices) do
      if message:find("Review state is busy", 1, true) then return true end
    end
    return false
  end, 10), "contender did not receive bounded refusal")
  local retained = vim.api.nvim_win_is_valid(composer)
  local lines = retained and vim.api.nvim_buf_get_lines(vim.api.nvim_win_get_buf(composer), 0, -1, false) or {}
  assert(real_writefile({ vim.json.encode({ retained = retained, lines = lines }) }, marker("contender-draft.json")) == 0)
  touch("contender-refused")
else
  assert(vim.wait(2000, function() return not vim.api.nvim_win_is_valid(composer) end, 10),
         "successful update left its composer open")
  touch(role .. "-committed")
end

vim.cmd("qa!")
