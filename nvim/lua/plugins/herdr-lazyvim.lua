-- Copy this file into ~/.config/nvim/lua/plugins/ when using LazyVim.
return {
  {
    dir = vim.env.HERDR_PLUGIN_ROOT and (vim.env.HERDR_PLUGIN_ROOT .. "/nvim") or vim.fn.stdpath("config"),
    name = "herdr-lazyvim",
    lazy = false,
    cond = vim.env.HERDR_ENV == "1" and vim.env.HERDR_PLUGIN_ROOT ~= nil,
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
    end,
  },
}
