if vim.g.loaded_herdr_feedback then return end
vim.g.loaded_herdr_feedback = true

local function legacy_action(name, visual)
  return function()
    require("herdr_feedback").setup()
    require("herdr_feedback.legacy")[name](visual)
  end
end

vim.api.nvim_create_user_command("HerdrReviewComment", legacy_action("comment", false), {})
vim.api.nvim_create_user_command("HerdrReviewList", legacy_action("list"), {})
vim.api.nvim_create_user_command("HerdrReviewEdit", legacy_action("edit"), {})
vim.api.nvim_create_user_command("HerdrReviewDelete", legacy_action("delete"), {})
vim.api.nvim_create_user_command("HerdrReviewSend", legacy_action("send"), {})
