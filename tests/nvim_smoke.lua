vim.opt.runtimepath:prepend(vim.fn.getcwd() .. "/nvim")

local review = require("herdr_review")
local payload = review._format_comments({
  { file = "b.ts", start = 9, finish = 10, lines = "old\nlines", text = "replace this", removed = true },
  { file = "a.go", start = 3, finish = 3, lines = "return nil", text = "handle the error", removed = false },
})

local expected = table.concat({
  "a.go:3",
  "return nil",
  "handle the error",
  "",
  "b.ts:9-10 (removed)",
  "old",
  "lines",
  "replace this",
}, "\n")

assert(payload == expected, "review comments should be sorted and retain selected source")
local tabs = review._tab_labels('{"result":{"tabs":[{"tab_id":"workspace:t2","label":"2"}]}}')
local agent_label = review._agent_label(
  { pane_id = "workspace:p7", tab_id = "workspace:t2", agent = "codex", agent_status = "idle" },
  "workspace",
  tabs
)
assert(agent_label == "codex · idle · 2", "agent labels should use the tab label, not the global pane id")
assert(type(require("herdr_lazyvim").setup) == "function", "integration entrypoint should load")

local plugin_spec = dofile("nvim/lua/plugins/herdr-lazyvim.lua")
for _, spec in ipairs(plugin_spec) do
  assert(spec.import == nil, "the Herdr plugin must not import LazyVim review extras")
  assert(spec[1] ~= "sindrets/diffview.nvim", "Diffview belongs to the user's editor configuration")
end

local review_tools = dofile("setup/nvim/lua/plugins/review.lua")
local diffview
for _, spec in ipairs(review_tools) do
  if spec[1] == "sindrets/diffview.nvim" then
    diffview = spec
  end
end
assert(diffview and type(diffview.keys) == "table", "workstation review config should define Diffview mappings")
local diffview_keys = {}
for _, mapping in ipairs(diffview.keys) do
  diffview_keys[mapping[1]] = mapping[2]
end
assert(diffview_keys["<leader>gv"] == "<cmd>DiffviewOpen HEAD<cr>", "gv should review uncommitted changes")
assert(
  diffview_keys["<leader>gV"] == "<cmd>DiffviewOpen origin/main...HEAD --imply-local<cr>",
  "gV should review branch changes in local buffers"
)
assert(diffview_keys["<leader>gH"] == "<cmd>DiffviewFileHistory %<cr>", "gH should open file history")
assert(diffview_keys["<leader>gq"] == "<cmd>DiffviewClose<cr>", "gq should close Diffview")
for _, context in ipairs({ "view", "file_panel", "file_history_panel" }) do
  local mapping = diffview.opts.keymaps[context][1]
  assert(mapping[2] == "q" and mapping[3] == "<cmd>DiffviewClose<cr>", "q should close every Diffview context")
end

local theme = require("herdr_theme")
assert(theme.mode_kind("n") == "normal", "normal mode should be identified")
assert(theme.mode_kind("i") == "insert", "insert mode should be identified")
assert(theme.mode_kind("v") == "visual", "visual mode should be identified")
assert(theme.mode_kind("R") == "replace", "replace mode should be identified")
assert(theme.mode_kind("c") == "command", "command mode should be identified")
assert(theme.mode_kind("t") == "terminal", "terminal mode should be identified")

theme.setup({ transparent_background = true, mode_emphasis = "cursorline" })
assert(vim.api.nvim_get_hl(0, { name = "SnacksPickerList" }).bg == nil, "Snacks explorer list should be transparent")

review.setup({
  comment_range_style = "subtle",
  comment_display = "card",
  comment_card_position = "below",
  comment_card_width = 44,
  comment_card_background = "NONE",
  comment_card_border = "#ff9e64",
})
local review_buf = vim.api.nvim_create_buf(false, true)
vim.api.nvim_buf_set_lines(review_buf, 0, -1, false, { "one", "two", "three", "four", "five" })
review._decorate_comment(review_buf, { start = 2, finish = 4, text = "Review this range" }, { [3] = 2 })
local marks = vim.api.nvim_buf_get_extmarks(review_buf, review._namespace, 0, -1, { details = true })
assert(#marks == 4, "a three-line comment should have three gutter marks and one label")
local signs = 0
local card
local card_row
for _, mark in ipairs(marks) do
  local details = mark[4]
  if details.sign_text then
    signs = signs + 1
    assert(details.hl_group == "HerdrReviewRange", "comment ranges should use the subtle highlight")
    assert(details.hl_eol == true, "comment range backgrounds should fill the complete source row")
    if mark[2] == 2 then
      assert(details.sign_text == "┊ ", "overlapping comment ranges should use a dotted rail")
    end
  end
  if details.virt_lines then
    card = details.virt_lines
    card_row = mark[2]
    assert(details.virt_lines_above ~= true, "the comment card should render below its range")
  end
end
assert(signs == 3, "every commented line should have a gutter marker")
local gutter_hl = vim.api.nvim_get_hl(0, { name = "HerdrReviewGutter", link = false })
local range_hl = vim.api.nvim_get_hl(0, { name = "HerdrReviewRange", link = false })
local comment_hl = vim.api.nvim_get_hl(0, { name = "HerdrReviewComment", link = false })
assert(gutter_hl.fg == tonumber("ff9e64", 16), "range rails should share the card border color")
assert(range_hl.bg == nil and comment_hl.bg == nil, "source ranges and comment rows should be transparent")
assert(card_row == 3, "the comment card should attach to the final commented line")
assert(#card == 3, "a one-line comment card should include top, body, and bottom rows")
assert(card[1][1][1]:match("^╭─ comment"), "the comment card should use a rounded top border without a left indent")
assert(card[#card][1][1]:match("^╰"), "the comment card should use a rounded bottom border without a left indent")
assert(review._range_label({ start = 2, finish = 4 }) == "lines 2–4", "comment labels should show the full range")
local wrapped_card = review._build_comment_card({
  file = "src/example.ts",
  start = 2,
  finish = 4,
  text = "This long review comment should wrap cleanly inside the darker bordered card.",
}, 32, 48)
assert(#wrapped_card > 3, "long comments should wrap to multiple card rows")
for _, row in ipairs(wrapped_card) do
  local width = 0
  for _, chunk in ipairs(row) do
    width = width + vim.fn.strdisplaywidth(chunk[1])
  end
  assert(width == 48, "comment rows should fill the complete annotation band")
end

local source_win = vim.api.nvim_get_current_win()
local float_buf = vim.api.nvim_create_buf(false, true)
local float_win = vim.api.nvim_open_win(float_buf, true, {
  relative = "editor",
  row = 1,
  col = 1,
  width = 20,
  height = 2,
  style = "minimal",
})
vim.cmd.startinsert()
review._restore_source({ win = source_win, mode = "n" })
assert(vim.api.nvim_get_current_win() == source_win, "closing a comment should restore its source window")
assert(vim.api.nvim_get_mode().mode == "n", "a comment opened from Normal mode should return to Normal mode")
vim.api.nvim_win_close(float_win, true)

vim.api.nvim_buf_set_lines(vim.api.nvim_win_get_buf(source_win), 0, -1, false, { "one", "two", "three" })
vim.cmd("normal! ggVj")
assert(vim.api.nvim_get_mode().mode == "V", "visual restoration fixture should start in linewise Visual mode")
local visual_float_buf = vim.api.nvim_create_buf(false, true)
local visual_float_win = vim.api.nvim_open_win(visual_float_buf, true, {
  relative = "editor",
  row = 1,
  col = 1,
  width = 20,
  height = 2,
  style = "minimal",
})
vim.cmd.startinsert()
review._restore_source({ win = source_win, mode = "V" })
assert(vim.api.nvim_get_current_win() == source_win, "a visual comment should restore its source window")
assert(vim.api.nvim_get_mode().mode == "V", "a visual comment should restore its Visual selection")
assert(vim.fn.line("v") == 1 and vim.fn.line(".") == 2, "the restored Visual selection should retain its range")
vim.cmd("normal! \27")
vim.api.nvim_win_close(visual_float_win, true)

local editor_source_buf = vim.api.nvim_get_current_buf()
local editor_textoff = vim.fn.getwininfo(vim.api.nvim_get_current_win())[1].textoff
review._comment_editor("Comment test.lua:2", nil, 2, function() end)
local editor_win = vim.api.nvim_get_current_win()
local editor_buf = vim.api.nvim_win_get_buf(editor_win)
local editor_config = vim.api.nvim_win_get_config(editor_win)
assert(editor_config.relative == "win", "the comment editor should be anchored to the source window")
assert(editor_config.height == 1, "the comment editor should start at one line")
assert(editor_config.bufpos[1] == 1, "the comment editor should anchor after the selected source line")
assert(editor_config.col == editor_textoff, "the inline editor should align after the source window gutter")
assert(vim.wo[editor_win].winblend == 30, "the comment editor should use the default translucent blend")
local spacers = vim.api.nvim_buf_get_extmarks(editor_source_buf, review._editor_namespace, 0, -1, { details = true })
assert(#spacers == 1 and #spacers[1][4].virt_lines == 3, "a one-line editor should reserve its row and border")
vim.api.nvim_buf_set_lines(editor_buf, 0, -1, false, { "first", "second" })
vim.api.nvim_exec_autocmds("TextChanged", { buffer = editor_buf })
assert(vim.api.nvim_win_get_config(editor_win).height == 2, "the comment editor should grow with multiline input")
spacers = vim.api.nvim_buf_get_extmarks(editor_source_buf, review._editor_namespace, 0, -1, { details = true })
assert(#spacers[1][4].virt_lines == 4, "reserved source space should grow with the editor")
vim.api.nvim_win_close(editor_win, true)
vim.wait(30)
spacers = vim.api.nvim_buf_get_extmarks(editor_source_buf, review._editor_namespace, 0, -1, { details = true })
assert(#spacers == 0, "closing the comment editor should release its reserved source space")

print("nvim smoke tests: ok")
vim.cmd("qa!")
