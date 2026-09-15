local harness = assert(vim.env.HARNESS, "HARNESS is required")
local fixture = dofile(harness .. "/tests/nvim/package_extraction.lua")
local root
local arms = {}
local prior_source
for _, side in ipairs(vim.split(assert(vim.env.PAIR_ORDER), ",", { plain = true })) do
  local source = assert(vim.env[side:upper() .. "_SOURCE_REPO"], "source is required")
  if root then fixture.reset(prior_source, root) end
  local result
  result, root = fixture.run(source, assert(vim.env.FIXTURE_ROOT), assert(vim.env.HERDR_TEST_STATE_ROOT))
  arms[side] = result
  prior_source = source
end
fixture.reset(prior_source, root)
vim.fn.writefile({ vim.json.encode({ status = "PASS", order = vim.env.PAIR_ORDER, arms = arms,
  reset_boundary = { "herdr_review and herdr_feedback package entries", "HerdrReview commands and mappings", "HerdrReview augroups and namespaces", "owned review state files", "prior source runtimepath entry" },
}) }, assert(vim.env.OPERATION_RESULT))
vim.cmd("qa!")
