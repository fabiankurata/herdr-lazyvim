local M = {}

local ns = vim.api.nvim_create_namespace("herdr_review")
local comments = {}
local active_root
local config = {
  comment_completion = false,
  comment_display = "card",
  comment_range_style = "subtle",
  comment_card_position = "below",
  comment_card_width = 72,
  comment_card_background = "#16161e",
  comment_card_border = "#ff9e64",
  comment_save_keys = { "<D-CR>", "<C-s>" },
  last_agents = {},
}

local function notify(message, level)
  vim.notify(message, level or vim.log.levels.INFO, { title = "Herdr Review" })
end

local function realpath(path)
  return vim.uv.fs_realpath(path) or vim.fs.normalize(path)
end

local function diffview_location(buf)
  local ok, lib = pcall(require, "diffview.lib")
  local view = ok and lib.get_current_view() or nil
  if not view or not view.cur_entry or not view.cur_entry.layout then
    return nil
  end
  for _, file in ipairs(view.cur_entry.layout:files()) do
    if file.bufnr == buf then
      return {
        root = realpath(file.adapter.ctx.toplevel),
        file = file.path,
        removed = file.symbol == "a",
      }
    end
  end
end

local function buffer_location(buf)
  local diff = diffview_location(buf)
  if diff then
    return diff
  end
  local name = vim.api.nvim_buf_get_name(buf)
  if name == "" or name:match("^diffview://") then
    return nil
  end
  local root = realpath(vim.fs.root(name, ".git") or vim.fn.getcwd())
  return {
    root = root,
    file = vim.fs.relpath(root, realpath(name)) or name,
    removed = false,
  }
end

local function root_for(buf)
  local location = buffer_location(buf)
  return location and location.root or realpath(vim.fn.getcwd())
end

local function state_file(root)
  local dir = vim.fn.stdpath("state") .. "/herdr-review"
  vim.fn.mkdir(dir, "p")
  return dir .. "/" .. vim.fn.sha256(root):sub(1, 16) .. ".json"
end

local function last_agents_file()
  local dir = vim.fn.stdpath("state") .. "/herdr-review"
  vim.fn.mkdir(dir, "p")
  return dir .. "/last-agents.json"
end

local function load_last_agents()
  local path = last_agents_file()
  if vim.fn.filereadable(path) == 0 then
    return
  end
  local ok, decoded = pcall(vim.json.decode, table.concat(vim.fn.readfile(path), "\n"))
  if ok and type(decoded) == "table" then
    config.last_agents = decoded
  end
end

local function save_last_agents()
  pcall(vim.fn.writefile, { vim.json.encode(config.last_agents) }, last_agents_file())
end

local function save()
  if not active_root then
    return
  end
  if #comments == 0 then
    vim.fn.delete(state_file(active_root))
    return
  end
  local encoded = vim.json.encode({ version = 1, root = active_root, comments = comments })
  local ok = pcall(vim.fn.writefile, { encoded }, state_file(active_root))
  if not ok then
    notify("Could not save review comments", vim.log.levels.ERROR)
  end
end

local function load(root)
  if active_root == root then
    return
  end
  active_root = root
  comments = {}
  local path = state_file(root)
  if vim.fn.filereadable(path) == 0 then
    return
  end
  local ok, decoded = pcall(vim.json.decode, table.concat(vim.fn.readfile(path), "\n"))
  if ok and type(decoded) == "table" and decoded.root == root and type(decoded.comments) == "table" then
    comments = decoded.comments
  end
end

local function clear_decorations()
  for _, buf in ipairs(vim.api.nvim_list_bufs()) do
    if vim.api.nvim_buf_is_valid(buf) then
      vim.api.nvim_buf_clear_namespace(buf, ns, 0, -1)
    end
  end
end

local function range_label(comment)
  if comment.start == comment.finish then
    return string.format("line %d", comment.start)
  end
  return string.format("lines %d–%d", comment.start, comment.finish)
end

local function take_display_prefix(text, width)
  local chunk = ""
  local count = vim.fn.strchars(text)
  local used = 0
  for index = 0, count - 1 do
    local char = vim.fn.strcharpart(text, index, 1)
    local char_width = vim.fn.strdisplaywidth(char)
    if used + char_width > width then
      return chunk, vim.fn.strcharpart(text, index)
    end
    chunk = chunk .. char
    used = used + char_width
  end
  return chunk, ""
end

local function wrap_text(text, width)
  local wrapped = {}
  for _, source_line in ipairs(vim.split(text, "\n", { plain = true })) do
    if source_line == "" then
      table.insert(wrapped, "")
    else
      local current = ""
      for word in source_line:gmatch("%S+") do
        if vim.fn.strdisplaywidth(word) > width then
          if current ~= "" then
            table.insert(wrapped, current)
            current = ""
          end
          local rest = word
          while vim.fn.strdisplaywidth(rest) > width do
            local chunk
            chunk, rest = take_display_prefix(rest, width)
            table.insert(wrapped, chunk)
          end
          current = rest
        elseif current == "" then
          current = word
        elseif vim.fn.strdisplaywidth(current .. " " .. word) <= width then
          current = current .. " " .. word
        else
          table.insert(wrapped, current)
          current = word
        end
      end
      if current ~= "" then
        table.insert(wrapped, current)
      end
    end
  end
  return #wrapped > 0 and wrapped or { "" }
end

local function tail_to_width(text, width)
  if vim.fn.strdisplaywidth(text) <= width then
    return text
  end
  local chars = vim.fn.strchars(text)
  for start = 1, chars - 1 do
    local tail = "…" .. vim.fn.strcharpart(text, start)
    if vim.fn.strdisplaywidth(tail) <= width then
      return tail
    end
  end
  return "…"
end

local function build_comment_card(comment, width)
  width = math.max(32, width)
  local prefix = " comment · "
  local suffix = ":" .. (comment.start == comment.finish and tostring(comment.start)
    or string.format("%d-%d", comment.start, comment.finish)) .. " "
  local file_width = math.max(1, width - 3 - vim.fn.strdisplaywidth(prefix .. suffix))
  local title = prefix .. tail_to_width(comment.file or "", file_width) .. suffix
  local top_fill = math.max(0, width - 3 - vim.fn.strdisplaywidth(title))
  local lines = {
    { { "  ", "Normal" }, { "╭─" .. title .. string.rep("─", top_fill) .. "╮", "HerdrReviewCardBorder" } },
  }
  for _, content in ipairs(wrap_text(comment.text, width - 4)) do
    local padding = math.max(0, width - 4 - vim.fn.strdisplaywidth(content))
    table.insert(lines, {
      { "  ", "Normal" },
      { "│ ", "HerdrReviewCardBorder" },
      { content .. string.rep(" ", padding), "HerdrReviewComment" },
      { " │", "HerdrReviewCardBorder" },
    })
  end
  table.insert(lines, {
    { "  ", "Normal" },
    { "╰" .. string.rep("─", width - 2) .. "╯", "HerdrReviewCardBorder" },
  })
  return lines
end

local function decorate_comment(buf, comment)
  local line_count = vim.api.nvim_buf_line_count(buf)
  local first = math.max(1, comment.start)
  local last = math.min(comment.finish, line_count)
  for line = first, last do
    local sign = "┃"
    if first == last then
      sign = "●"
    elseif line == first then
      sign = "┏"
    elseif line == last then
      sign = "┗"
    end
    local range_mark = {
      sign_text = sign,
      sign_hl_group = "HerdrReviewGutter",
      number_hl_group = "HerdrReviewGutter",
      priority = 120,
    }
    if config.comment_range_style == "subtle" then
      range_mark.line_hl_group = "HerdrReviewRange"
    elseif config.comment_range_style == "selection" then
      range_mark.line_hl_group = "Visual"
    end
    pcall(vim.api.nvim_buf_set_extmark, buf, ns, line - 1, 0, range_mark)
  end

  local comment_mark = { priority = 121 }
  if config.comment_display == "eol" then
    local label = comment.text:gsub("\n.*", "")
    if #label > 70 then
      label = label:sub(1, 67) .. "..."
    end
    comment_mark.virt_text = {
      { "  󰆉 review · " .. range_label(comment) .. ": ", "HerdrReviewHeader" },
      { label, "HerdrReviewComment" },
    }
    comment_mark.virt_text_pos = "eol"
  else
    comment_mark.virt_lines = build_comment_card(comment, config.comment_card_width)
    comment_mark.virt_lines_above = config.comment_card_position == "above"
  end
  local card_line = config.comment_card_position == "above" and first or last
  pcall(vim.api.nvim_buf_set_extmark, buf, ns, card_line - 1, 0, comment_mark)
end

local function decorate(buf)
  if not vim.api.nvim_buf_is_valid(buf) or vim.api.nvim_buf_get_name(buf) == "" then
    return
  end
  local location = buffer_location(buf)
  if not location then
    return
  end
  load(location.root)
  vim.api.nvim_buf_clear_namespace(buf, ns, 0, -1)
  for _, comment in ipairs(comments) do
    if
      comment.file == location.file
      and not not comment.removed == location.removed
      and comment.start <= vim.api.nvim_buf_line_count(buf)
    then
      decorate_comment(buf, comment)
    end
  end
end

local function refresh()
  local root = active_root
  for _, buf in ipairs(vim.api.nvim_list_bufs()) do
    if vim.api.nvim_buf_is_loaded(buf) and vim.api.nvim_buf_get_name(buf) ~= "" and root_for(buf) == root then
      decorate(buf)
    end
  end
end

local function restore_source(context)
  if not context or not vim.api.nvim_win_is_valid(context.win) then
    return
  end
  vim.api.nvim_set_current_win(context.win)
  if context.mode:match("^[vV\22]") then
    vim.cmd("normal! gv")
  elseif context.mode:match("^i") then
    vim.cmd.startinsert()
  else
    vim.cmd.stopinsert()
  end
end

local function comment_editor(title, initial, callback)
  local source = {
    win = vim.api.nvim_get_current_win(),
    mode = vim.api.nvim_get_mode().mode,
  }
  local width = math.min(78, math.max(40, vim.o.columns - 8))
  local height = math.min(10, math.max(5, vim.o.lines - 8))
  local buf = vim.api.nvim_create_buf(false, true)
  vim.bo[buf].bufhidden = "wipe"
  vim.bo[buf].filetype = "markdown"
  vim.bo[buf].swapfile = false
  vim.b[buf].completion = config.comment_completion
  if not config.comment_completion then
    vim.bo[buf].completefunc = ""
    vim.bo[buf].omnifunc = ""
  end
  vim.diagnostic.enable(false, { bufnr = buf })
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, initial and vim.split(initial, "\n", { plain = true }) or { "" })

  local win = vim.api.nvim_open_win(buf, true, {
    relative = "editor",
    row = math.floor((vim.o.lines - height) / 2) - 1,
    col = math.floor((vim.o.columns - width) / 2),
    width = width,
    height = height,
    style = "minimal",
    border = "rounded",
    title = " " .. title .. " · Cmd-Enter / Ctrl-S save · q cancel ",
    title_pos = "center",
  })
  vim.wo[win].wrap = true
  vim.wo[win].winhl = "FloatBorder:DiagnosticInfo"

  local finished = false
  local function finish()
    if finished then
      return
    end
    finished = true
    vim.schedule(function()
      restore_source(source)
    end)
  end

  local function close()
    if vim.api.nvim_win_is_valid(win) then
      vim.api.nvim_win_close(win, true)
    end
    finish()
  end

  vim.api.nvim_create_autocmd("WinClosed", {
    pattern = tostring(win),
    once = true,
    callback = finish,
  })

  local function submit()
    local lines = vim.api.nvim_buf_get_lines(buf, 0, -1, false)
    while #lines > 0 and vim.trim(lines[1]) == "" do
      table.remove(lines, 1)
    end
    while #lines > 0 and vim.trim(lines[#lines]) == "" do
      table.remove(lines)
    end
    local text = table.concat(lines, "\n")
    if text == "" then
      notify("Write a comment before saving", vim.log.levels.WARN)
      return
    end
    close()
    callback(text)
  end

  for _, key in ipairs(config.comment_save_keys) do
    vim.keymap.set({ "n", "i" }, key, submit, { buffer = buf, desc = "Save review comment" })
  end
  vim.keymap.set("n", "q", close, { buffer = buf, desc = "Cancel review comment" })
  vim.cmd.startinsert()
end

local function selection(buf, visual)
  local first, last
  if visual then
    -- Read the active selection. The '< and '> marks are only guaranteed to
    -- describe the selection after Visual mode exits and may still be stale
    -- while this mapping is executing.
    first = vim.fn.line("v")
    last = vim.fn.line(".")
  else
    first = vim.api.nvim_win_get_cursor(0)[1]
    last = first
  end
  if first > last then
    first, last = last, first
  end
  local location = buffer_location(buf)
  if not location then
    return nil
  end
  local lines = vim.api.nvim_buf_get_lines(buf, first - 1, last, false)
  return location.root, location.file, first, last, table.concat(lines, "\n"), location.removed
end

function M.comment(visual)
  local buf = vim.api.nvim_get_current_buf()
  local name = vim.api.nvim_buf_get_name(buf)
  if name == "" then
    notify("This buffer has no file", vim.log.levels.WARN)
    return
  end
  local root, file, first, last, snippet, removed = selection(buf, visual)
  if not root then
    notify("This buffer is not a project file", vim.log.levels.WARN)
    return
  end
  load(root)
  local location = first == last and string.format("%s:%d", file, first) or string.format("%s:%d-%d", file, first, last)
  if removed then
    location = location .. " (removed)"
  end
  comment_editor("Comment " .. location, nil, function(text)
    table.insert(comments, {
      file = file,
      start = first,
      finish = last,
      lines = snippet,
      text = text,
      removed = removed or nil,
    })
    save()
    refresh()
    notify(string.format("Added comment %d of %d", #comments, #comments))
  end)
end

local function jump_to(comment)
  local path = active_root .. "/" .. comment.file
  vim.cmd.edit(vim.fn.fnameescape(path))
  vim.api.nvim_win_set_cursor(0, { comment.start, 0 })
end

function M.list()
  load(root_for(0))
  if #comments == 0 then
    notify("No review comments")
    return
  end
  vim.ui.select(comments, {
    prompt = "Review comments",
    format_item = function(comment)
      local range = comment.start == comment.finish and tostring(comment.start)
        or string.format("%d-%d", comment.start, comment.finish)
      return string.format("%s:%s  %s", comment.file, range, comment.text:gsub("\n.*", ""))
    end,
  }, function(comment)
    if comment then
      jump_to(comment)
    end
  end)
end

function M.delete()
  local location = buffer_location(0)
  if not location then
    notify("This buffer is not a project file", vim.log.levels.WARN)
    return
  end
  load(location.root)
  local line = vim.api.nvim_win_get_cursor(0)[1]
  for index = #comments, 1, -1 do
    local comment = comments[index]
    if
      comment.file == location.file
      and not not comment.removed == location.removed
      and line >= comment.start
      and line <= comment.finish
    then
      table.remove(comments, index)
      save()
      refresh()
      notify("Deleted review comment")
      return
    end
  end
  notify("No review comment on this line", vim.log.levels.WARN)
end

local function edit_comment(comment)
  local range = comment.start == comment.finish and tostring(comment.start)
    or string.format("%d-%d", comment.start, comment.finish)
  comment_editor(string.format("Edit %s:%s", comment.file, range), comment.text, function(text)
    comment.text = text
    save()
    refresh()
    notify("Updated review comment")
  end)
end

function M.edit()
  local location = buffer_location(0)
  if not location then
    notify("This buffer is not a project file", vim.log.levels.WARN)
    return
  end
  load(location.root)
  local line = vim.api.nvim_win_get_cursor(0)[1]
  local matches = {}
  for _, comment in ipairs(comments) do
    if
      comment.file == location.file
      and not not comment.removed == location.removed
      and line >= comment.start
      and line <= comment.finish
    then
      table.insert(matches, comment)
    end
  end
  if #matches == 0 then
    notify("No review comment on this line", vim.log.levels.WARN)
  elseif #matches == 1 then
    edit_comment(matches[1])
  else
    vim.ui.select(matches, {
      prompt = "Edit review comment",
      format_item = function(comment)
        return comment.text:gsub("\n.*", "")
      end,
    }, function(comment)
      if comment then
        edit_comment(comment)
      end
    end)
  end
end

local function normalize(text)
  local result = {}
  for _, line in ipairs(vim.split(text:gsub("\r", ""), "\n", { plain = true })) do
    line = line:gsub("%s+$", "")
    if vim.trim(line) ~= "" then
      table.insert(result, line)
    end
  end
  return table.concat(result, "\n")
end

local function format_comments(items)
  local sorted = vim.deepcopy(items)
  table.sort(sorted, function(a, b)
    return a.file == b.file and a.start < b.start or a.file < b.file
  end)
  local blocks = {}
  for _, comment in ipairs(sorted) do
    local location = comment.start == comment.finish and string.format("%s:%d", comment.file, comment.start)
      or string.format("%s:%d-%d", comment.file, comment.start, comment.finish)
    if comment.removed then
      location = location .. " (removed)"
    end
    table.insert(blocks, table.concat({ location, comment.lines, normalize(comment.text) }, "\n"))
  end
  return table.concat(blocks, "\n\n")
end

local function format_all()
  return format_comments(comments)
end

local function agent_name(agent)
  for _, key in ipairs({ "name", "display_agent", "agent" }) do
    if type(agent[key]) == "string" and agent[key] ~= "" then
      return agent[key]
    end
  end
  return agent.pane_id
end

local function pane_number(agent)
  return agent.pane_id:match(":p(%d+)$") or agent.pane_id
end

local function agent_label(agent, workspace)
  local last_used = config.last_agents[workspace] == agent.pane_id and " · last used" or ""
  return string.format(
    "%s · pane %s · %s%s",
    agent_name(agent),
    pane_number(agent),
    agent.agent_status or "unknown",
    last_used
  )
end

local function choose_agent(callback)
  local workspace = vim.env.HERDR_WORKSPACE_ID
  local own_pane = vim.env.HERDR_PANE_ID
  if not workspace or workspace == "" then
    notify("LazyVim is not running inside a Herdr workspace", vim.log.levels.ERROR)
    return
  end
  local herdr = vim.env.HERDR_BIN_PATH or "herdr"
  vim.system({ herdr, "agent", "list" }, { text = true }, function(result)
    vim.schedule(function()
      if result.code ~= 0 then
        notify("Herdr did not return the workspace agents", vim.log.levels.ERROR)
        return
      end
      local ok, response = pcall(vim.json.decode, result.stdout)
      local rows = ok and response and response.result and response.result.agents or {}
      local agents = {}
      for _, agent in ipairs(rows) do
        if agent.workspace_id == workspace and agent.pane_id ~= own_pane and type(agent.agent) == "string" then
          table.insert(agents, agent)
        end
      end
      if #agents == 0 then
        notify("No agent is running in this Herdr workspace", vim.log.levels.WARN)
      elseif #agents == 1 then
        callback(agents[1])
      else
        vim.ui.select(agents, {
          prompt = "Send review to agent",
          format_item = function(agent)
            return agent_label(agent, workspace)
          end,
        }, function(agent)
          if agent then
            callback(agent)
          end
        end)
      end
    end)
  end)
end

local function pasted(text)
  local paste_end = "\27[201~"
  text = text:gsub(vim.pesc(paste_end), "")
  return "\27[200~" .. text .. paste_end
end

function M.send()
  load(root_for(0))
  if #comments == 0 then
    notify("No review comments to send", vim.log.levels.WARN)
    return
  end
  local payload = format_all()
  local count = #comments
  choose_agent(function(agent)
    local herdr = vim.env.HERDR_BIN_PATH or "herdr"
    vim.system({ herdr, "pane", "send-text", agent.pane_id, pasted(payload) }, { text = true }, function(result)
      vim.schedule(function()
        if result.code ~= 0 then
          notify("Agent not found; comments were kept", vim.log.levels.ERROR)
          return
        end
        config.last_agents[agent.workspace_id] = agent.pane_id
        save_last_agents()
        comments = {}
        save()
        clear_decorations()
        notify(string.format("Added %d comment%s to %s", count, count == 1 and "" or "s", agent_name(agent)))
        vim.system({ herdr, "agent", "focus", agent.pane_id }, { text = true })
      end)
    end)
  end)
end

function M.setup(opts)
  load_last_agents()
  config = vim.tbl_deep_extend("force", config, opts or {})
  local normal = vim.api.nvim_get_hl(0, { name = "Normal", link = false })
  local card_background = tonumber(config.comment_card_background:sub(2), 16)
  local card_border = tonumber(config.comment_card_border:sub(2), 16)
  vim.api.nvim_set_hl(0, "HerdrReviewComment", { fg = normal.fg, bg = card_background })
  vim.api.nvim_set_hl(0, "HerdrReviewHeader", { fg = "#7dcfff", bold = true, default = true })
  vim.api.nvim_set_hl(0, "HerdrReviewGutter", { fg = "#7dcfff", bold = true, default = true })
  vim.api.nvim_set_hl(0, "HerdrReviewRange", { bg = "#20283a", default = true })
  vim.api.nvim_set_hl(0, "HerdrReviewCardBorder", { fg = card_border, bg = card_background, bold = true })
  vim.api.nvim_create_autocmd({ "BufEnter", "BufWritePost" }, {
    group = vim.api.nvim_create_augroup("HerdrReview", { clear = true }),
    callback = function(args)
      decorate(args.buf)
    end,
  })
  vim.api.nvim_create_autocmd("User", {
    group = vim.api.nvim_create_augroup("HerdrReviewDiffview", { clear = true }),
    pattern = "DiffviewViewOpened",
    callback = function()
      vim.schedule(refresh)
    end,
  })

  vim.api.nvim_create_user_command("HerdrReviewComment", function()
    M.comment(false)
  end, {})
  vim.api.nvim_create_user_command("HerdrReviewList", M.list, {})
  vim.api.nvim_create_user_command("HerdrReviewEdit", M.edit, {})
  vim.api.nvim_create_user_command("HerdrReviewDelete", M.delete, {})
  vim.api.nvim_create_user_command("HerdrReviewSend", M.send, {})

  vim.keymap.set("n", "<leader>rc", function()
    M.comment(false)
  end, { desc = "Review comment" })
  vim.keymap.set("x", "<leader>rc", function()
    M.comment(true)
  end, { desc = "Review comment selection" })
  vim.keymap.set("n", "<leader>rl", M.list, { desc = "Review comments" })
  vim.keymap.set("n", "<leader>re", M.edit, { desc = "Edit review comment" })
  vim.keymap.set("n", "<leader>rd", M.delete, { desc = "Delete review comment" })
  vim.keymap.set("n", "<leader>rs", M.send, { desc = "Send review to Herdr agent" })

  vim.schedule(function()
    decorate(vim.api.nvim_get_current_buf())
  end)
end

M._format_all = format_all
M._format_comments = format_comments
M._selection = selection
M._buffer_location = buffer_location
M._agent_label = agent_label
M._decorate_comment = decorate_comment
M._namespace = ns
M._range_label = range_label
M._restore_source = restore_source
M._build_comment_card = build_comment_card

return M
