local M = {}

function M.normalize_path(path)
  if type(vim.fs.normalize) == "function" then return vim.fs.normalize(path) end
  return vim.fn.fnamemodify(path, ":p"):gsub("/+$", "")
end

local function realpath(path)
  return vim.uv.fs_realpath(path) or M.normalize_path(path)
end

function M.relative_path(root, path)
  if type(vim.fs.relpath) == "function" then return vim.fs.relpath(root, path) end
  local separator = package.config:sub(1, 1)
  local prefix = root:sub(-1) == separator and root or root .. separator
  if path:sub(1, #prefix) == prefix then return path:sub(#prefix + 1) end
end

local function local_authority()
  return { host = vim.uv.os_gethostname() or "localhost", path_authority = "local" }
end

function M.review_for_root(root)
  return { worktree = { authority = local_authority(), canonical_root = realpath(root) }, review_id = "draft" }
end

function M.validate_review(review)
  local worktree = type(review) == "table" and review.worktree
  local authority = type(worktree) == "table" and worktree.authority
  if type(worktree) ~= "table" or type(worktree.canonical_root) ~= "string" or worktree.canonical_root == "" then
    return nil, "review requires worktree.canonical_root"
  end
  if type(authority) ~= "table" or type(authority.host) ~= "string" or authority.host == ""
      or type(authority.path_authority) ~= "string" or authority.path_authority == "" then
    return nil, "review requires complete worktree authority"
  end
  if type(review.review_id) ~= "string" or review.review_id == "" then return nil, "review requires review_id" end
  local supported = local_authority()
  if authority.host ~= supported.host or authority.path_authority ~= supported.path_authority then
    return nil, "PR02 supports only the local worktree authority"
  end
  if review.review_id ~= "draft" then return nil, "PR02 supports only the draft review" end
  return vim.deepcopy(review)
end

function M.buffer_location(bufnr)
  if type(bufnr) ~= "number" or not vim.api.nvim_buf_is_valid(bufnr) then return nil, "bufnr must identify a valid buffer" end
  local name = vim.api.nvim_buf_get_name(bufnr)
  if name == "" or name:match("^diffview://") then return nil, "buffer has no ordinary file source" end
  local absolute = realpath(name)
  local root = realpath(vim.fs.root(absolute, ".git") or vim.fn.getcwd())
  return { root = root, file = M.relative_path(root, absolute) or absolute, removed = false }
end

function M.capture(request)
  if type(request) ~= "table" or type(request.text) ~= "string" then return nil, "add requires bufnr, range, and text" end
  local range = request.range
  if type(range) ~= "table" or type(range.start_line) ~= "number" or type(range.end_line) ~= "number"
      or range.start_line % 1 ~= 0 or range.end_line % 1 ~= 0 or range.start_line < 1 or range.end_line < range.start_line then
    return nil, "add requires an inclusive complete-line range"
  end
  local location, error = M.buffer_location(request.bufnr)
  if not location then return nil, error end
  if range.end_line > vim.api.nvim_buf_line_count(request.bufnr) then return nil, "range exceeds buffer" end
  return { review = M.review_for_root(location.root), source = {
    file = location.file, start = range.start_line, finish = range.end_line,
    lines = table.concat(vim.api.nvim_buf_get_lines(request.bufnr, range.start_line - 1, range.end_line, false), "\n"),
    removed = location.removed or nil,
  } }
end

return M
