local context = require("herdr_feedback.context")
local M = {}
local CANCELLED = {}
local LOCK_BUSY = {}
local acquire_lock

local ffi_ok, ffi = pcall(require, "ffi")
if ffi_ok then pcall(ffi.cdef, "int flock(int fd, int operation);") end
local retryable_flock_errors = { [4] = true, [11] = true, [35] = true }

local function root_of(review)
  return type(review) == "table" and type(review.worktree) == "table" and review.worktree.canonical_root
end

local function close_file(file)
  local ok, result = pcall(vim.uv.fs_close, file)
  return ok and result and true or false
end

local function remove_owned_file(path)
  local ok, removed = pcall(vim.uv.fs_unlink, path)
  return ok and removed and true or false
end

local function uuid()
  local bytes = assert(vim.uv.random(16), "could not generate temporary identity")
  local parts = {}
  for index = 1, #bytes do parts[index] = string.format("%02x", bytes:byte(index)) end
  local hex = table.concat(parts)
  return table.concat({ hex:sub(1, 8), hex:sub(9, 12), hex:sub(13, 16), hex:sub(17, 20), hex:sub(21, 32) }, "-")
end

function M.root(review)
  local normalized, error = context.validate_review(review)
  if not normalized then return nil, error end
  local root = root_of(normalized)
  return vim.uv.fs_realpath(root) or context.normalize_path(root)
end

function M.path(review)
  local root, error = M.root(review)
  if not root then return nil, error end
  local directory = vim.fn.stdpath("state") .. "/herdr-review"
  vim.fn.mkdir(directory, "p")
  return directory .. "/" .. vim.fn.sha256(root):sub(1, 16) .. ".json", root
end

local function read_path(path, root)
  if not vim.uv.fs_stat(path) then return {}, nil end
  if vim.fn.filereadable(path) == 0 then return nil, "could not read review state" end
  local read_ok, lines = pcall(vim.fn.readfile, path)
  if not read_ok then return nil, "could not read review state" end
  local ok, value = pcall(vim.json.decode, table.concat(lines, "\n"))
  if not ok or type(value) ~= "table" or value.root ~= root or type(value.comments) ~= "table" then
    return nil, "review state is malformed or belongs to another root"
  end
  return value.comments, nil
end

local function cancelled(control)
  return control and control.cancelled_before_commit and control:cancelled_before_commit()
end

local function begin_commit(control)
  if not control then return true end
  return control:begin_commit()
end

local function abort_commit(control)
  if control and control.abort_commit then control:abort_commit() end
end

local function finish_commit(control)
  if control and control.finish_commit then control:finish_commit() end
end

local function write_path(path, root, comments, control)
  if #comments == 0 then
    if not vim.uv.fs_stat(path) then return true end
    if not begin_commit(control) then return nil, CANCELLED end
    local ok, removed = pcall(vim.uv.fs_unlink, path)
    if ok and removed then finish_commit(control); return true end
    abort_commit(control)
    return nil, "could not save review comments"
  end
  local temporary, file, closed
  local ok, written, write_error = xpcall(function()
    local contents = vim.json.encode({ version = 1, root = root, comments = comments }) .. "\n"
    temporary = string.format("%s.tmp-%d-%s", path, vim.uv.os_getpid(), uuid())
    local open_ok
    open_ok, file = pcall(vim.uv.fs_open, temporary, "wx", 384)
    if not open_ok or not file then return nil, "could not save review comments" end
    local offset = 0
    while offset < #contents do
      local write_ok, count = pcall(vim.uv.fs_write, file, contents:sub(offset + 1), offset)
      if not write_ok or not count or count == 0 then return nil, "could not save review comments" end
      offset = offset + count
    end
    if not close_file(file) then return nil, "could not close temporary review state" end
    closed, file = true, nil
    if not begin_commit(control) then return nil, CANCELLED end
    local rename_ok, committed = pcall(vim.uv.fs_rename, temporary, path)
    if not rename_ok or not committed then abort_commit(control); return nil, "could not save review comments" end
    finish_commit(control)
    return true
  end, debug.traceback)
  local cleanup = {}
  if not ok or not written then
    if file and not closed and not close_file(file) then table.insert(cleanup, "could not close temporary review state") end
    if temporary and not remove_owned_file(temporary) then table.insert(cleanup, "could not remove temporary review state") end
  end
  local function with_cleanup(error)
    if #cleanup == 0 then return error end
    return tostring(error) .. "; additionally " .. table.concat(cleanup, "; ")
  end
  if not ok then return nil, with_cleanup(written) end
  if not written and write_error ~= CANCELLED then abort_commit(control) end
  if not written then return nil, with_cleanup(write_error) end
  return true
end

function M.is_cancelled(error)
  return error == CANCELLED
end

function M.update(review, callback, control)
  local path, root = M.path(review)
  if not path then return nil, root end
  local lock, lock_error = acquire_lock(path)
  if not lock then
    if lock_error == LOCK_BUSY then
      if cancelled(control) then return nil, CANCELLED end
      return nil, "Review state is busy; try again"
    end
    return nil, lock_error
  end
  local ok, succeeded, value, failure = xpcall(function()
    if cancelled(control) then return false, nil, CANCELLED end
    local comments, read_error = read_path(path, root)
    if not comments then return false, nil, read_error end
    local called, replacement, callback_value, callback_error = pcall(callback, comments)
    if not called then error(replacement) end
    if replacement == false then return true, callback_value, nil end
    if replacement == nil then return false, nil, callback_error or callback_value end
    if cancelled(control) then return false, nil, CANCELLED end
    local written, write_error = write_path(path, root, replacement, control)
    if not written then return false, nil, write_error end
    return true, callback_value, nil
  end, debug.traceback)
  local closed = close_file(lock)
  if not ok then
    local transaction_error = tostring(succeeded)
    if not closed then return nil, transaction_error .. "; additionally could not release review-state lock" end
    return nil, transaction_error
  end
  if not succeeded then
    if not closed and failure ~= CANCELLED then return nil, tostring(failure) .. "; additionally could not release review-state lock" end
    return nil, failure
  end
  -- A close failure after a successful rename/unlink cannot make the durable
  -- commit disappear, so report the committed value rather than a false refusal.
  return value, nil
end

local function try_lock(file)
  if not ffi_ok then return nil end
  local call_ok, result = pcall(function() return ffi.C.flock(file, 2 + 4) end)
  if not call_ok then return nil end
  if result == 0 then return true end
  if retryable_flock_errors[ffi.errno()] then return false end
end

acquire_lock = function(path)
  local lock_path = path .. ".lock-v2"
  local existing_ok, existing = pcall(vim.uv.fs_lstat, lock_path)
  if not existing_ok or existing and existing.type ~= "file" then return nil, "could not coordinate review state" end
  local open_ok, file = pcall(vim.uv.fs_open, lock_path, "a", 384)
  if not open_ok or not file then return nil, "could not coordinate review state" end
  local stat_ok, identity = pcall(vim.uv.fs_fstat, file)
  local uid_ok, uid = pcall(vim.uv.getuid)
  local chmod_ok, private = pcall(vim.uv.fs_fchmod, file, 384)
  if not stat_ok or not identity or identity.type ~= "file"
      or not uid_ok or identity.uid ~= uid or not chmod_ok or not private then
    close_file(file)
    return nil, "could not coordinate review state"
  end
  local deadline = vim.uv.hrtime() + 1000000000
  repeat
    local locked = try_lock(file)
    if locked then return file end
    if locked == nil then close_file(file); return nil, "could not coordinate review state" end
    vim.wait(10)
  until vim.uv.hrtime() >= deadline
  close_file(file)
  return nil, LOCK_BUSY
end

function M.read(review)
  local path, root = M.path(review)
  if not path then return nil, root end
  local comments, error = read_path(path, root)
  return comments, error, path, root
end

function M.write(review, comments)
  local _, error = M.update(review, function() return comments, true end)
  if error then return nil, error end
  return true
end

function M.read_root(root)
  return M.read(context.review_for_root(root))
end

function M.write_root(root, comments)
  return M.write(context.review_for_root(root), comments)
end

return M
