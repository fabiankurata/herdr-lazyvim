local M = {}

local configured = false

function M.setup(opts)
  if configured then
    return
  end
  configured = true
  opts = opts or {}

  local ok_theme, theme = pcall(require, "herdr_theme")
  if ok_theme then
    theme.setup({
      transparent_background = opts.transparent_background ~= false,
      mode_emphasis = opts.mode_emphasis or "cursorline",
    })
  else
    vim.notify("Could not load Herdr editor theme: " .. tostring(theme), vim.log.levels.ERROR)
  end

  local ok_review, review = pcall(require, "herdr_review")
  if ok_review then
    review.setup({
      comment_completion = opts.comment_completion == true,
      comment_display = opts.comment_display or "card",
      comment_range_style = opts.comment_range_style or "subtle",
      comment_card_position = opts.comment_card_position or "below",
      comment_card_width = opts.comment_card_width or 72,
      comment_card_background = opts.comment_card_background or "#16161e",
      comment_card_border = opts.comment_card_border or "#ff9e64",
      comment_save_keys = opts.comment_save_keys or { "<D-CR>", "<C-s>" },
    })
  else
    vim.notify("Could not load Herdr review comments: " .. tostring(review), vim.log.levels.ERROR)
  end

end

return M
