local opt = vim.opt

-- Line numbers
opt.number = true
opt.relativenumber = true

-- Tabs and indentation (4 spaces)
opt.tabstop = 4
opt.shiftwidth = 4
opt.expandtab = true
opt.autoindent = true
opt.smartindent = true

-- Search
opt.ignorecase = true
opt.smartcase = true
opt.hlsearch = true
opt.incsearch = true

-- Appearance
opt.termguicolors = true
opt.cursorline = true
opt.signcolumn = "yes"
opt.scrolloff = 8
opt.sidescrolloff = 8

-- Splits
opt.splitright = true
opt.splitbelow = true

-- Mouse and clipboard
opt.mouse = "a"
opt.clipboard = "unnamedplus"

-- Undo history
opt.undofile = true
opt.swapfile = false
opt.backup = false

-- Responsiveness
opt.updatetime = 250
opt.timeoutlen = 300
