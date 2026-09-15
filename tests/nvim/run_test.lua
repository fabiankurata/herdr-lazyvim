local script = assert(vim.env.HERDR_NVIM_TEST_SCRIPT, "HERDR_NVIM_TEST_SCRIPT is required")
local ok, error = xpcall(dofile, debug.traceback, script)
if not ok then
  io.stderr:write("headless Neovim test failed: " .. error .. "\n")
  vim.cmd("cquit 1")
end
