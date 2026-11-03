local map = vim.keymap.set

-- General mappings (matching host conventions)
map("n", ";", ":", { desc = "Enter command mode" })
map("i", "jk", "<ESC>", { desc = "Exit insert mode" })
map("i", "kj", "<ESC>", { desc = "Exit insert mode" })

-- Clear search highlight
map("n", "<Esc>", "<cmd>nohlsearch<CR>", { desc = "Clear search highlight" })

-- File explorer (nvim-tree)
map("n", "<C-n>", "<cmd>NvimTreeToggle<CR>", { desc = "Toggle file tree" })
map("n", "<Leader>e", "<cmd>NvimTreeFocus<CR>", { desc = "Focus file tree" })

-- Fuzzy finder (Telescope)
map("n", "<Leader>ff", "<cmd>Telescope find_files<CR>", { desc = "Find files" })
map("n", "<Leader>fw", "<cmd>Telescope live_grep<CR>", { desc = "Live grep (word search)" })
map("n", "<Leader>fb", "<cmd>Telescope buffers<CR>", { desc = "Find open buffers" })
map("n", "<Leader>fh", "<cmd>Telescope help_tags<CR>", { desc = "Help tags" })
map("n", "<Leader>fo", "<cmd>Telescope oldfiles<CR>", { desc = "Recent files" })

-- Tab navigation
map("n", "<Leader>tn", ":tabnext<CR>", { silent = true, desc = "Next tab" })
map("n", "<Leader>tp", ":tabprevious<CR>", { silent = true, desc = "Previous tab" })

-- Window navigation between splits
map("n", "<C-h>", "<C-w>h", { desc = "Move to left window" })
map("n", "<C-j>", "<C-w>j", { desc = "Move to lower window" })
map("n", "<C-k>", "<C-w>k", { desc = "Move to upper window" })
map("n", "<C-l>", "<C-w>l", { desc = "Move to right window" })
