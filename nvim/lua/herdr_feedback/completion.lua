local M = {}

function M.scheduled(done, work)
  local state = { finished = false, cancelled = false, committed = false }
  local function finish(value)
    if state.finished then return end
    state.finished = true
    vim.schedule(function() done(value) end)
  end
  vim.schedule(function()
    if state.finished then return end
    if state.cancelled then return finish({ ok = false, error = { code = "cancelled", message = "operation was cancelled", context = {} } }) end
    local value = work(state, finish)
    if value ~= nil then state.committed = value.ok == true; finish(value) end
  end)
  return { cancel = function() if not state.finished and not state.committed then state.cancelled = true end end }, state, finish
end

function M.failure(code, message, context)
  return { ok = false, error = { code = code, message = message, context = context or {} } }
end

return M
