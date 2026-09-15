-- Controlled PR01 transport. It never invokes a Herdr binary or a real agent.
local root = assert(vim.env.HERDR_TEST_FIXTURE_ROOT)
local state = assert(vim.env.HERDR_TEST_STATE_ROOT)
assert(state == vim.fn.stdpath("state"), "fixture state must match Neovim stdpath(state)")
vim.fn.mkdir(state .. "/herdr-review", "p")
local roots = { a = root .. "/root-a", b = root .. "/root-b" }
for _, path in pairs(roots) do
  vim.fn.mkdir(path .. "/.git", "p")
  vim.fn.writefile({ "local fixture = true", "return fixture" }, path .. "/example.lua")
end
for name, path in pairs(roots) do roots[name] = assert(vim.uv.fs_realpath(path)) end
local function file(root_path) return state .. "/herdr-review/" .. vim.fn.sha256(root_path):sub(1, 16) .. ".json" end
local function record(id, text, revision) return { id = id, revision = revision or 1, file = "example.lua", start = 1, finish = 1, lines = "local fixture = true", text = text } end
local ids = { one = "00000000-0000-4000-8000-000000000001", two = "00000000-0000-4000-8000-000000000002" }
local function seed(which, comments) vim.fn.writefile({ vim.json.encode({ version = 1, root = roots[which], comments = comments }) }, file(roots[which])) end
local function read(which)
  local path = file(roots[which]); if vim.fn.filereadable(path) == 0 then return { comments = {} } end
  return vim.json.decode(table.concat(vim.fn.readfile(path), "\n"))
end
local function edit(which) vim.cmd("edit " .. vim.fn.fnameescape(roots[which] .. "/example.lua")); vim.api.nvim_win_set_cursor(0, { 1, 0 }) end
local function reset_review() package.loaded.herdr_review = nil; require("herdr_review").setup() end

local pending, notices, commands = {}, {}, {}
local transport = { attempts = {}, accepted = {}, acceptance = true }
local active = { lane = nil, complete = false, composer = false }
vim.notify = function(message, level) table.insert(notices, { message = message, level = level }) end
vim.ui.select = function(items, _, callback)
  if vim.g.pr01_picker_cancel then
    active.picker_cancelled = true
    callback(nil)
  else
    callback(items[1])
  end
end
vim.system = function(argv, _, callback)
  table.insert(commands, vim.deepcopy(argv))
  if argv[2] == "agent" and argv[3] == "list" then pending.agents = callback
  elseif argv[2] == "pane" and argv[3] == "send-text" then
    local entry = { pane_id = argv[4], bytes = argv[5] }; table.insert(transport.attempts, entry); pending.send = callback
    if transport.acceptance then table.insert(transport.accepted, entry) end
  elseif argv[2] == "tab" then pending.tabs = callback
  elseif argv[2] == "agent" and argv[3] == "focus" then
    -- Production supplies no callback for the successful focus request.
  else error("unexpected controlled command: " .. table.concat(argv, " ")) end
  return {}
end

local function expected(comment) return "\27[200~example.lua:1\nlocal fixture = true\n" .. comment .. "\27[201~" end
local function selected_agent() return { workspace_id = "fixture-workspace", pane_id = "fixture-agent", agent = "fixture" } end
local function choose(code, stderr)
  pending.agents({ code = 0, stdout = vim.json.encode({ result = { agents = { selected_agent() } } }), stderr = "" })
  vim.wait(1000, function() return pending.send ~= nil end, 5)
  if code then pending.send({ code = code, stdout = "", stderr = stderr or "" }) end
end
local function composer(text, key)
  active.composer, active.composer_text, active.save_key = true, text, key
  active.composer_window, active.composer_owned = vim.api.nvim_get_current_win(), vim.bo.filetype == "markdown"
end
local function visit_b_then_return_to_composer(source_window)
  active.source_window = source_window
  if not vim.api.nvim_win_is_valid(source_window) then error("fixture source window disappeared before root B visit") end
  vim.api.nvim_set_current_win(source_window)
  edit("b")
  active.visited_root_b = vim.api.nvim_get_current_win() == source_window
    and vim.api.nvim_buf_get_name(0) == roots.b .. "/example.lua"
  vim.api.nvim_set_current_win(active.composer_window)
  active.composer_owned = vim.bo.filetype == "markdown"
end
local function observed_send(expected_bytes, attempts, accepted)
  return #transport.attempts == attempts and #transport.accepted == accepted
    and (attempts == 0 or transport.attempts[1].bytes == expected_bytes)
    and (accepted == 0 or transport.accepted[1].bytes == expected_bytes)
end
local function has_notice(message)
  for _, notice in ipairs(notices) do
    if notice.message == message then return true end
  end
  return false
end
local function window_snapshot(window)
  if not vim.api.nvim_win_is_valid(window) then error("fixture normal window is invalid") end
  vim.api.nvim_set_current_win(window)
  return {
    window = window,
    buffer = vim.api.nvim_get_current_buf(),
    name = vim.api.nvim_buf_get_name(0),
    cursor = vim.api.nvim_win_get_cursor(0),
    view = vim.fn.winsaveview(),
    lines = vim.api.nvim_buf_get_lines(0, 0, -1, false),
  }
end
local function same_window(snapshot)
  if not snapshot or not vim.api.nvim_win_is_valid(snapshot.window) then return false end
  local view = vim.api.nvim_win_call(snapshot.window, vim.fn.winsaveview)
  return vim.api.nvim_win_get_buf(snapshot.window) == snapshot.buffer
    and vim.api.nvim_buf_get_name(snapshot.buffer) == snapshot.name
    and vim.deep_equal(vim.api.nvim_win_get_cursor(snapshot.window), snapshot.cursor)
    and vim.deep_equal(view, snapshot.view)
    and vim.deep_equal(vim.api.nvim_buf_get_lines(snapshot.buffer, 0, -1, false), snapshot.lines)
end
local function begin(name)
  active = { lane = name, complete = false, composer = false, status = "PENDING" }; pending, notices, commands = {}, {}, {}; transport = { attempts = {}, accepted = {}, acceptance = true }; vim.g.pr01_picker_cancel = false
  if name == "restart" then seed("a", { { file = "example.lua", start = 1, finish = 1, lines = "local fixture = true", text = "legacy" } }); seed("b", { record(ids.two, "from B") })
  elseif name == "batch" then seed("a", { record(ids.one, "first"), record(ids.two, "second") }); seed("b", { record(ids.two, "from B") })
  else seed("a", { record(ids.one, "captured") }); seed("b", { record(ids.two, "from B") }) end
  reset_review()
  edit("a")
  if name == "composer-root" then
    local source_window = vim.api.nvim_get_current_win()
    vim.cmd("HerdrReviewComment"); composer("composer-root native draft", "ctrl-s"); visit_b_then_return_to_composer(source_window)
  elseif name == "closed-origin" then
    local source_window = vim.api.nvim_get_current_win()
    vim.cmd("vsplit " .. vim.fn.fnameescape(roots.b .. "/example.lua"))
    local b_window = vim.api.nvim_get_current_win()
    active.b_window = window_snapshot(b_window)
    vim.api.nvim_set_current_win(source_window)
    vim.cmd("HerdrReviewComment"); composer("closed-origin native draft", "ctrl-s")
    active.closed_source_window = source_window
    active.composer_survived_before_save = vim.api.nvim_win_is_valid(active.composer_window)
      and vim.api.nvim_get_current_win() == active.composer_window and vim.bo.filetype == "markdown"
    vim.api.nvim_win_close(source_window, true)
    active.source_closed = not vim.api.nvim_win_is_valid(source_window)
    active.composer_survived_before_save = active.composer_survived_before_save
      and vim.api.nvim_win_is_valid(active.composer_window) and vim.api.nvim_get_current_win() == active.composer_window
  elseif name == "new-comment" or name == "edited-comment" then vim.cmd("HerdrReviewSend"); choose(); vim.cmd(name == "new-comment" and "HerdrReviewComment" or "HerdrReviewEdit"); composer(name .. " native draft", "cmd-enter")
  elseif name == "cancel-send" then
    vim.g.pr01_picker_cancel = true; vim.cmd("HerdrReviewSend")
    pending.agents({ code = 0, stdout = vim.json.encode({ result = { agents = { selected_agent(), { workspace_id = "fixture-workspace", pane_id = "two", agent = "two" } } } }), stderr = "" })
    vim.wait(1000, function() return pending.tabs ~= nil end, 5); pending.tabs({ code = 0, stdout = '{"result":{"tabs":[]}}', stderr = "" })
    if not vim.wait(1000, function() return active.picker_cancelled end, 5) then error("fixture cancellation picker did not complete") end
  elseif name == "send-failure" then transport.acceptance = false; vim.cmd("HerdrReviewSend"); choose(1, "controlled input rejection")
  elseif name == "uncertain" then vim.cmd("HerdrReviewSend"); choose(1, "accepted bytes; acknowledgement lost")
  elseif name == "batch" then vim.cmd("lua require('herdr_review').send({annotation_ids={'" .. ids.two .. "'}})"); choose(0)
  elseif name == "restart" then vim.cmd("HerdrReviewList"); vim.cmd("HerdrReviewComment"); composer("restart native draft", "ctrl-s")
  else vim.cmd("HerdrReviewSend"); edit("b"); choose(0) end
  return active
end
local function complete(name)
  if name == "closed-origin" then active.focus_after_save = vim.api.nvim_get_current_win() end
  if name == "new-comment" or name == "edited-comment" or name == "batch" then pending.send({ code = 0, stdout = "{}", stderr = "" }) end
  local expected_bytes = expected(name == "batch" and "second" or "captured")
  local success_notice = "Pasted 1 comment to fixture; agent execution is not confirmed"
  local uncertain_notice = "Delivery outcome is uncertain; comments were kept and will not be resent automatically"
  vim.wait(1000, function()
    local a = read("a")
    if name == "root-switch" then return #a.comments == 0 and #notices == 1 end
    if name == "new-comment" then return #a.comments == 1 and has_notice(success_notice) end
    if name == "edited-comment" then return #a.comments == 1 and a.comments[1].revision == 2 and has_notice(success_notice) end
    if name == "batch" then return #a.comments == 1 and has_notice(success_notice) end
    if name == "send-failure" or name == "uncertain" then return #notices == 1 end
    if name == "closed-origin" then return active.source_closed and active.composer_survived_before_save
      and active.focus_after_save == active.b_window.window and same_window(active.b_window) end
    return true
  end, 10)
  local a, b = read("a"), read("b"); local pass = true
  if name == "root-switch" then active.defect = #b.comments == 0 and "root-b-deleted" or nil; pass = #b.comments == 1 and b.comments[1].text == "from B" and #a.comments == 0
  elseif name == "composer-root" then pass = active.visited_root_b and #a.comments == 2 and a.comments[2].text == "composer-root native draft" and #b.comments == 1
  elseif name == "new-comment" then pass = #a.comments == 1 and a.comments[1].text == "new-comment native draft" and observed_send(expected_bytes, 1, 1) and has_notice(success_notice)
  elseif name == "edited-comment" then pass = #a.comments == 1 and a.comments[1].revision == 2 and a.comments[1].text == "edited-comment native draft" and observed_send(expected_bytes, 1, 1) and has_notice(success_notice)
  elseif name == "cancel-send" then pass = active.picker_cancelled and observed_send(nil, 0, 0) and #a.comments == 1
  elseif name == "send-failure" then pass = observed_send(expected("captured"), 1, 0) and #a.comments == 1 and notices[1] and notices[1].message == uncertain_notice
  elseif name == "uncertain" then pass = observed_send(expected("captured"), 1, 1) and #a.comments == 1 and notices[1] and notices[1].message == uncertain_notice
  elseif name == "closed-origin" then pass = #a.comments == 2 and a.comments[2].text == "closed-origin native draft"
    and active.source_closed and not vim.api.nvim_win_is_valid(active.closed_source_window)
    and active.composer_survived_before_save and active.focus_after_save == active.b_window.window
    and vim.api.nvim_get_current_win() == active.b_window.window and same_window(active.b_window)
  elseif name == "batch" then pass = #a.comments == 1 and a.comments[1].id == ids.one and observed_send(expected_bytes, 1, 1) and has_notice(success_notice) end
  active.complete, active.status, active.composer, active.transport, active.notices, active.persisted, active.commands = true, pass and "PASS" or "FAIL", false, transport, notices, { a = a, b = b }, commands
  return active
end
local function prepare_restart()
  local before = read("a").comments; vim.fn.writefile({ vim.json.encode(before) }, root .. "/restart-comments.json"); return active
end
local function headless_save()
  if not active.composer then return active end
  vim.api.nvim_buf_set_lines(0, 0, -1, false, { active.composer_text })
  vim.cmd("stopinsert")
  local keys = active.save_key == "cmd-enter" and "<D-CR>" or "<C-s>"
  vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes(keys, true, false, true), "xt", false)
  vim.wait(1000, function() return vim.bo.filetype ~= "markdown" end, 5)
  return active
end
_G.PR01Native = {
  ready = function() return { ready = vim.fn.exists(":HerdrReviewComment") == 2 } end, begin = begin, complete = complete, prepare_restart = prepare_restart, headless_save = headless_save,
  status = function()
    active.composer_owned = active.composer and vim.bo.filetype == "markdown"
    if active.composer_owned then active.composer_survived_before_save = true end
    active.save_complete = active.composer and not active.composer_owned
    return active
  end,
  after_restart = function()
    edit("a"); vim.cmd("HerdrReviewList"); local before = vim.json.decode(table.concat(vim.fn.readfile(root .. "/restart-comments.json"), "\n")); local after = read("a").comments; local legacy, fresh = after[1], after[2]
    local uuid = function(item) return item and item.id and item.id:match("^[0-9a-f]+%-[0-9a-f]+%-4[0-9a-f]+%-[89ab][0-9a-f]+%-[0-9a-f]+$") end
    local same = function(left, right) return left and right and left.id == right.id and left.revision == right.revision and left.text == right.text end
    return { lane = "restart", complete = true, status = uuid(legacy) and uuid(fresh) and #before == 2 and #after == 2 and same(before[1], legacy) and same(before[2], fresh) and "PASS" or "FAIL", before = before, after = after }
  end,
}
