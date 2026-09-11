local M = {}

local config = {
  transparent_background = true,
  mode_emphasis = "cursorline",
}

local palette = {
  normal = { label = "NORMAL", accent = "#7aa2f7", line = "#1f2942" },
  insert = { label = "INSERT", accent = "#9ece6a", line = "#20352d" },
  visual = { label = "VISUAL", accent = "#bb9af7", line = "#302642" },
  replace = { label = "REPLACE", accent = "#f7768e", line = "#3a252d" },
  command = { label = "COMMAND", accent = "#e0af68", line = "#372f22" },
  terminal = { label = "TERMINAL", accent = "#7dcfff", line = "#1f3440" },
}

local transparent_groups = {
  "Normal",
  "NormalNC",
  "EndOfBuffer",
  "SignColumn",
  "FoldColumn",
  "LineNr",
  "CursorLineSign",
  "CursorLineFold",
  "WinBar",
  "WinBarNC",
  "NeoTreeNormal",
  "NeoTreeNormalNC",
  "NvimTreeNormal",
  "NvimTreeNormalNC",
  "SnacksNormal",
  "SnacksNormalNC",
  "SnacksPicker",
  "SnacksPickerList",
  "SnacksPickerInput",
  "SnacksPickerPreview",
  "SnacksPickerBox",
  "SnacksPickerBorder",
  "SnacksPickerListBorder",
  "SnacksPickerInputBorder",
  "SnacksPickerPreviewBorder",
  "SnacksPickerBoxBorder",
}

function M.mode_kind(mode)
  mode = mode or vim.api.nvim_get_mode().mode
  if mode:match("^[i]") then
    return "insert"
  elseif mode:match("^[vV\22sS\19]") then
    return "visual"
  elseif mode:match("^[rR]") then
    return "replace"
  elseif mode:match("^[c]") then
    return "command"
  elseif mode:match("^[t]") then
    return "terminal"
  end
  return "normal"
end

function M.mode_label()
  return " " .. palette[M.mode_kind()].label .. " "
end

function M.mode_color()
  local color = palette[M.mode_kind()]
  return { fg = "#15161e", bg = color.accent, gui = "bold" }
end

local function apply_transparency()
  if not config.transparent_background then
    return
  end
  for _, group in ipairs(transparent_groups) do
    pcall(vim.cmd, "highlight " .. group .. " guibg=NONE ctermbg=NONE")
  end
end

local function apply_mode()
  local color = palette[M.mode_kind()]
  if config.mode_emphasis == "cursorline" then
    vim.api.nvim_set_hl(0, "CursorLine", { bg = color.line })
    vim.api.nvim_set_hl(0, "CursorLineNr", { fg = color.accent, bg = color.line, bold = true })
  elseif config.mode_emphasis == "window" then
    vim.api.nvim_set_hl(0, "Normal", { bg = color.line })
    vim.api.nvim_set_hl(0, "NormalNC", { bg = color.line })
    vim.api.nvim_set_hl(0, "CursorLineNr", { fg = color.accent, bg = color.line, bold = true })
  end
  vim.cmd("redrawstatus")
end

local function apply()
  apply_transparency()
  apply_mode()
end

function M.setup(opts)
  config = vim.tbl_deep_extend("force", config, opts or {})
  vim.opt.cursorline = config.mode_emphasis ~= "none"
  vim.opt.cursorlineopt = "both"
  vim.opt.guicursor = "n-v-c:block,i-ci-ve:ver25,r-cr:hor20,o:hor50"

  local group = vim.api.nvim_create_augroup("HerdrEditorTheme", { clear = true })
  vim.api.nvim_create_autocmd("ColorScheme", {
    group = group,
    callback = function()
      vim.schedule(apply)
    end,
  })
  vim.api.nvim_create_autocmd("ModeChanged", {
    group = group,
    callback = apply_mode,
  })
  apply()
end

M._palette = palette

return M
