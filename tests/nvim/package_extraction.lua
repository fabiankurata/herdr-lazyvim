local M = {}

local function bytes(path)
  local file = assert(io.open(path, "rb"))
  local value = assert(file:read("*a"))
  assert(file:close())
  return value
end

local function wait_for(predicate, message)
  assert(vim.wait(500, predicate, 5), message)
end

local function state_file(root)
  local directory = vim.fn.stdpath("state") .. "/herdr-review"
  vim.fn.mkdir(directory, "p")
  return directory .. "/" .. vim.fn.sha256(root):sub(1, 16) .. ".json"
end

local function clear_review_runtime(source)
  if source then vim.opt.runtimepath:remove(source .. "/nvim") end
  for name in pairs(package.loaded) do
    if name == "herdr_review" or name:match("^herdr_feedback") then package.loaded[name] = nil end
  end
  for _, command in ipairs({ "HerdrReviewComment", "HerdrReviewList", "HerdrReviewEdit", "HerdrReviewDelete", "HerdrReviewSend" }) do
    pcall(vim.api.nvim_del_user_command, command)
  end
  for _, mapping in ipairs({ { "n", "<leader>rc" }, { "x", "<leader>rc" }, { "n", "<leader>rl" },
    { "n", "<leader>re" }, { "n", "<leader>rd" }, { "n", "<leader>rs" } }) do
    pcall(vim.keymap.del, mapping[1], mapping[2])
  end
  for _, group in ipairs({ "HerdrReview", "HerdrReviewDiffview" }) do
    pcall(vim.api.nvim_del_augroup_by_name, group)
  end
  local namespaces = vim.api.nvim_get_namespaces()
  for _, buffer in ipairs(vim.api.nvim_list_bufs()) do
    if vim.api.nvim_buf_is_valid(buffer) then
      for _, name in ipairs({ "herdr_review", "herdr_review_editor" }) do
        if namespaces[name] then vim.api.nvim_buf_clear_namespace(buffer, namespaces[name], 0, -1) end
      end
    end
  end
end

function M.reset(source, root)
  clear_review_runtime(source)
  vim.fn.delete(state_file(root))
  vim.fn.delete(vim.fn.stdpath("state") .. "/herdr-review/last-agents.json")
  vim.cmd("silent! only")
  vim.wait(20)
end

function M.run(source, fixture, state_root)
  assert(vim.fn.stdpath("state"):sub(1, #state_root) == state_root, "fixture must use isolated state")
  vim.opt.runtimepath:prepend(source .. "/nvim")
  vim.env.HERDR_WORKSPACE_ID = "fixture-workspace"
  vim.env.HERDR_PANE_ID = "fixture-editor"
  vim.env.HERDR_BIN_PATH = "FAKE-HERDR-OPERATIONS"

  local uuid_call = 0
  vim.uv.random = function(length)
    assert(length == 16, "legacy UUIDs must request sixteen bytes")
    uuid_call = uuid_call + 1
    return string.rep(string.char(uuid_call), length)
  end

  vim.fn.mkdir(fixture .. "/worktree/.git", "p")
  local root = assert(vim.uv.fs_realpath(fixture .. "/worktree"))
  vim.fn.writefile({ "first source line", "second source line", "third source line", "fourth source line" }, root .. "/example.lua")
  local path = state_file(root)
  vim.fn.writefile({ vim.json.encode({ version = 1, root = root, comments = {
    { file = "example.lua", start = 1, finish = 1, lines = "first source line", text = "legacy one" },
    { file = "example.lua", start = 2, finish = 2, lines = "second source line", text = "legacy two" },
    { file = "example.lua", start = 3, finish = 3, lines = "third source line", text = "legacy three" },
  } }) }, path)
  local old_store_bytes = bytes(path)

  local listed, sent_payload
  vim.ui.select = function(items, options, callback)
    if options.prompt == "Review comments" then listed = vim.deepcopy(items) end
    callback(nil)
  end
  vim.system = function(argv, _, callback)
    if argv[2] == "agent" and argv[3] == "list" then
      callback({ code = 0, stdout = vim.json.encode({ result = { agents = {
        { workspace_id = "fixture-workspace", pane_id = "fixture-agent", agent = "fixture" },
      } } }), stderr = "" })
    elseif argv[2] == "pane" and argv[3] == "send-text" then
      sent_payload = argv[5]
      callback({ code = 1, stdout = "", stderr = "fixture retains drafts" })
    elseif argv[2] == "agent" and argv[3] == "focus" then
      callback({ code = 0, stdout = "", stderr = "" })
    else
      error("unexpected fixture command " .. vim.inspect(argv))
    end
    return {}
  end
  vim.notify = function() end

  local started = vim.uv.hrtime()
  local review = require("herdr_review")
  review.setup()
  local module_setup_ms = (vim.uv.hrtime() - started) / 1e6
  vim.cmd.edit(vim.fn.fnameescape(root .. "/example.lua"))
  started = vim.uv.hrtime()
  review.list()
  assert(#listed == 3, "old-format list must expose every legacy record")
  local old_format_store_access_ms = (vim.uv.hrtime() - started) / 1e6
  local old_store_after_access_bytes = bytes(path)
  assert(old_store_after_access_bytes ~= old_store_bytes, "legacy access must persist normalized IDs and revisions")

  local function save_composer(text)
    local composer = vim.api.nvim_get_current_win()
    vim.api.nvim_buf_set_lines(vim.api.nvim_win_get_buf(composer), 0, -1, false, { text })
    vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes("<C-s>", true, false, true), "xt", false)
  end
  started = vim.uv.hrtime()
  vim.api.nvim_win_set_cursor(0, { 4, 0 }); review.comment(false); save_composer("created through public comment")
  wait_for(function() local value = vim.json.decode(bytes(path)); return #value.comments == 4 and value.comments[4].text == "created through public comment" end, "public comment must persist")
  vim.api.nvim_win_set_cursor(0, { 4, 0 }); review.edit(); save_composer("edited through public edit")
  wait_for(function() local value = vim.json.decode(bytes(path)); return #value.comments == 4 and value.comments[4].text == "edited through public edit" and value.comments[4].revision == 2 end, "public edit must persist")
  vim.api.nvim_win_set_cursor(0, { 4, 0 }); review.delete()
  wait_for(function() return #vim.json.decode(bytes(path)).comments == 3 end, "public delete must persist")
  local sequential_crud_ms = (vim.uv.hrtime() - started) / 1e6

  started = vim.uv.hrtime()
  review.send()
  wait_for(function() return sent_payload ~= nil end, "public send must create an export payload")
  local export_ms = (vim.uv.hrtime() - started) / 1e6
  local final_store_bytes = bytes(path)
  local expected = "\27[200~" .. table.concat({ "example.lua:1", "first source line", "legacy one", "", "example.lua:2", "second source line", "legacy two", "", "example.lua:3", "third source line", "legacy three" }, "\n") .. "\27[201~"
  assert(sent_payload == expected, "public send must export the remaining review state")
  return {
    status = "PASS", export_entrypoint = "herdr_review.send", uuid_random_calls = uuid_call,
    old_store_bytes = old_store_bytes, old_store_after_access_bytes = old_store_after_access_bytes,
    final_store_bytes = final_store_bytes, export_payload = sent_payload,
    module_setup_ms = module_setup_ms, old_format_store_access_ms = old_format_store_access_ms,
    sequential_crud_ms = sequential_crud_ms, export_ms = export_ms,
  }, root
end

if vim.env.PACKAGE_EXTRACTION_LIBRARY ~= "1" then
  local result, root = M.run(assert(vim.env.SOURCE_REPO), assert(vim.env.FIXTURE_ROOT), assert(vim.env.HERDR_TEST_STATE_ROOT))
  M.reset(vim.env.SOURCE_REPO, root)
  vim.fn.writefile({ vim.json.encode(result) }, assert(vim.env.OPERATION_RESULT))
  vim.cmd("qa!")
end

return M
