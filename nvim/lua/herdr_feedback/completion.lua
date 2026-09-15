local M = {}

function M.scheduled(done, work)
  local state = { finished = false, cancelled = false, mutation = "open", delivery = "not_started" }
  function state:cancelled_before_commit()
    return self.cancelled and self.mutation ~= "committing"
  end
  function state:begin_commit()
    if self:cancelled_before_commit() then return false end
    self.mutation = "committing"
    return true
  end
  function state:finish_commit() self.mutation = "committed" end
  function state:abort_commit()
    if self.mutation == "committing" then self.mutation = "open" end
  end
  local function finish(value)
    if state.finished then return end
    state.finished = true
    vim.schedule(function() done(value) end)
  end
  vim.schedule(function()
    if state.finished then return end
    if state:cancelled_before_commit() then return finish(M.failure("cancelled", "operation was cancelled")) end
    local ok, value = xpcall(function() return work(state, finish) end, debug.traceback)
    if not ok then return finish(M.failure("failed", value)) end
    if value ~= nil then finish(value) end
  end)
  return { cancel = function()
    if not state.finished then state.cancelled = true end
  end }, state, finish
end

function M.failure(code, message, context)
  return { ok = false, error = { code = code, message = message, context = context or {} } }
end

return M
