return {
  -- Vue also enables LazyVim's TypeScript integration.
  { import = "lazyvim.plugins.extras.lang.vue" },

  -- Backend and infrastructure languages used across the repository.
  { import = "lazyvim.plugins.extras.lang.go" },
  { import = "lazyvim.plugins.extras.lang.rust" },
  { import = "lazyvim.plugins.extras.lang.terraform" },
  { import = "lazyvim.plugins.extras.lang.docker" },

  -- Schema-aware configuration, including devcontainer and Compose files.
  { import = "lazyvim.plugins.extras.lang.json" },
  { import = "lazyvim.plugins.extras.lang.yaml" },
  { import = "lazyvim.plugins.extras.lang.toml" },

  -- Large repositories can contain generated files and dependency trees.
  -- Avoid registering one libuv watcher per matching path.
  -- LSP features and updates for files opened or saved in Neovim still work.
  {
    "neovim/nvim-lspconfig",
    opts = {
      servers = {
        ["*"] = {
          capabilities = {
            workspace = {
              didChangeWatchedFiles = {
                dynamicRegistration = false,
              },
            },
          },
        },
        -- terraform-ls provides the editor features. Keep TFLint available as
        -- a CLI, since its legacy --langserver mode currently exits on start.
        tflint = false,
        gopls = {
          settings = {
            gopls = {
              directoryFilters = {
                "-.git",
                "-.idea",
                "-.vscode",
                "-.vscode-test",
                "-node_modules",
                "-.pnpm-store",
                "-.terraform",
              },
            },
          },
        },
      },
    },
  },

  -- Makefiles have Tree-sitter support but no general-purpose LSP.
  {
    "nvim-treesitter/nvim-treesitter",
    opts = function(_, opts)
      vim.list_extend(opts.ensure_installed, { "make" })
    end,
  },
}
