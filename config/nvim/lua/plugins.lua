return {
  -- Theme: Catppuccin Mocha matching Starship & Fastfetch
  {
    "catppuccin/nvim",
    name = "catppuccin",
    priority = 1000,
    config = function()
      require("catppuccin").setup({
        flavour = "mocha",
        integrations = {
          nvimtree = true,
          telescope = true,
          treesitter = true,
          gitsigns = true,
        },
      })
      vim.cmd.colorscheme("catppuccin")
    end,
  },

  -- File tree explorer
  {
    "nvim-tree/nvim-tree.lua",
    dependencies = { "nvim-tree/nvim-web-devicons" },
    config = function()
      require("nvim-tree").setup({
        git = {
          enable = true,
          ignore = false,
        },
        filters = {
          dotfiles = false,
          custom = { "^.git$" },
        },
        sync_root_with_cwd = true,
        update_focused_file = {
          enable = true,
          update_root = true,
        },
        view = {
          width = 30,
          preserve_window_proportions = true,
        },
        renderer = {
          root_folder_label = false,
          highlight_git = true,
          icons = {
            show = {
              file = true,
              folder = true,
              folder_arrow = true,
              git = true,
            },
          },
        },
      })
    end,
  },

  -- Fuzzy finder (Telescope)
  {
    "nvim-telescope/telescope.nvim",
    branch = "0.1.x",
    dependencies = {
      "nvim-lua/plenary.nvim",
      "nvim-tree/nvim-web-devicons",
    },
    config = function()
      local telescope = require("telescope")
      telescope.setup({
        defaults = {
          vimgrep_arguments = {
            "rg",
            "--color=never",
            "--no-heading",
            "--with-filename",
            "--line-number",
            "--column",
            "--smart-case",
            "--no-ignore",
            "--hidden",
          },
          file_ignore_patterns = { "^.git/" },
        },
        pickers = {
          find_files = {
            hidden = true,
            no_ignore = true,
          },
        },
      })
    end,
  },

  -- Syntax highlighting (Treesitter)
  {
    "nvim-treesitter/nvim-treesitter",
    build = ":TSUpdate",
    event = { "BufReadPost", "BufNewFile" },
    cmd = { "TSUpdateSync", "TSUpdate", "TSInstall" },
    config = function()
      local ok, ts = pcall(require, "nvim-treesitter.configs")
      if ok then
        ts.setup({
          ensure_installed = {
            "bash",
            "c",
            "cpp",
            "html",
            "javascript",
            "json",
            "lua",
            "markdown",
            "markdown_inline",
            "python",
            "typescript",
            "vim",
            "vimdoc",
            "yaml",
          },
          highlight = { enable = true },
          indent = { enable = true },
        })
      end
    end,
  },

  -- Statusline (Lualine)
  {
    "nvim-lualine/lualine.nvim",
    dependencies = { "nvim-tree/nvim-web-devicons" },
    config = function()
      require("lualine").setup({
        options = {
          theme = "catppuccin",
          icons_enabled = true,
        },
      })
    end,
  },

  -- Git status signs
  {
    "lewis6991/gitsigns.nvim",
    event = { "BufReadPre", "BufNewFile" },
    config = function()
      require("gitsigns").setup()
    end,
  },
}
