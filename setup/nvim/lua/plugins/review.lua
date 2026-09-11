-- Workstation review tools. These are regular LazyVim plugins and are not
-- dependencies of the Herdr comments integration.
return {
  { import = "lazyvim.plugins.extras.util.octo" },

  {
    "sindrets/diffview.nvim",
    cmd = {
      "DiffviewOpen",
      "DiffviewClose",
      "DiffviewToggleFiles",
      "DiffviewFocusFiles",
      "DiffviewFileHistory",
    },
    keys = {
      { "<leader>gv", "<cmd>DiffviewOpen HEAD<cr>", desc = "Diffview uncommitted changes" },
      {
        "<leader>gV",
        "<cmd>DiffviewOpen origin/main...HEAD --imply-local<cr>",
        desc = "Diffview branch against main",
      },
      { "<leader>gH", "<cmd>DiffviewFileHistory %<cr>", desc = "Diffview current file history" },
      { "<leader>gq", "<cmd>DiffviewClose<cr>", desc = "Close Diffview" },
    },
    dependencies = { "nvim-lua/plenary.nvim" },
    opts = {
      enhanced_diff_hl = true,
      keymaps = {
        view = {
          { "n", "q", "<cmd>DiffviewClose<cr>", { desc = "Close Diffview" } },
        },
        file_panel = {
          { "n", "q", "<cmd>DiffviewClose<cr>", { desc = "Close Diffview" } },
        },
        file_history_panel = {
          { "n", "q", "<cmd>DiffviewClose<cr>", { desc = "Close Diffview" } },
        },
      },
      view = {
        default = { layout = "diff2_horizontal" },
        merge_tool = { layout = "diff3_mixed" },
        file_history = { layout = "diff2_horizontal" },
      },
      file_panel = {
        listing_style = "tree",
        win_config = { position = "left", width = 38 },
      },
    },
  },
}
