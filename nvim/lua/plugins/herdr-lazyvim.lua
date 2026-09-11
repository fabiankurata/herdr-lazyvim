-- Copy this file into ~/.config/nvim/lua/plugins/ when using LazyVim.
return {
  {
    dir = vim.env.HERDR_PLUGIN_ROOT and (vim.env.HERDR_PLUGIN_ROOT .. "/nvim") or vim.fn.stdpath("config"),
    name = "herdr-lazyvim",
    lazy = false,
    cond = vim.env.HERDR_ENV == "1" and vim.env.HERDR_PLUGIN_ROOT ~= nil,
  },

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
    dependencies = { "nvim-lua/plenary.nvim" },
    opts = {
      enhanced_diff_hl = true,
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

  {
    "nvim-lualine/lualine.nvim",
    opts = function(_, opts)
      if vim.env.HERDR_ENV ~= "1" or vim.env.HERDR_PLUGIN_ROOT == nil then
        return
      end
      opts.sections.lualine_a = {
        {
          function()
            return require("herdr_theme").mode_label()
          end,
          color = function()
            return require("herdr_theme").mode_color()
          end,
          padding = 0,
        },
      }
      table.insert(opts.sections.lualine_c, {
        function()
          return require("herdr_review_hub").status()
        end,
        cond = function()
          return require("herdr_review_hub").status() ~= ""
        end,
      })
    end,
  },
}
