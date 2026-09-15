local context = require("herdr_feedback.context")
local operations = require("herdr_feedback.operations")
local legacy = require("herdr_feedback.legacy")

local M = {}
local configured = false
local source_adapters = {}
local transports = {}

local function invalid(done, message)
  return operations.scheduled(done, function() return operations.failure("invalid_request", message) end)
end

local function callback(done)
  if type(done) ~= "function" then error("done callback is required") end
end

function M.setup(opts)
  local validated, error = legacy.validate_setup(opts)
  if not validated then return operations.failure("invalid_request", error) end
  if configured then return { ok = true, value = { already_configured = true } } end
  local result = legacy.setup(validated)
  if result.ok then configured = true end
  return result
end

function M.add(request, done)
  callback(done)
  local captured, error = context.capture(request)
  local text = type(request) == "table" and request.text or nil
  return operations.scheduled(done, function(state)
    if not captured then return operations.failure("invalid_request", error) end
    return operations.add_captured(captured, text, state)
  end)
end

local function existing(name, id, request, done)
  callback(done)
  local copied = type(request) == "table" and vim.deepcopy(request) or request
  return operations.scheduled(done, function(state) return operations[name](id, copied, state) end)
end
function M.edit(id, request, done) return existing("edit", id, request, done) end
function M.delete(id, request, done) return existing("delete", id, request, done) end
function M.list(request, done)
  callback(done)
  local copied = type(request) == "table" and vim.deepcopy(request) or request
  return operations.scheduled(done, function(state) return operations.list(copied, state) end)
end
function M.export(request, done)
  callback(done)
  local copied = type(request) == "table" and vim.deepcopy(request) or request
  return operations.scheduled(done, function(state) return operations.export(copied, state) end)
end

local function target_error(code, message)
  return { code = code, message = message }
end

local function validate_target(target, review)
  if type(target) ~= "table" then return nil, target_error("invalid_request", "target must be a table") end
  local connection = target.connection
  local authority = type(connection) == "table" and connection.authority
  if type(connection) ~= "table" or type(connection.socket) ~= "string" or connection.socket == ""
      or type(authority) ~= "table" or type(authority.host) ~= "string" or authority.host == ""
      or type(authority.path_authority) ~= "string" or authority.path_authority == "" then
    return nil, target_error("invalid_request", "target requires a complete connection")
  end
  for _, field in ipairs({ "workspace_id", "tab_id", "pane_id", "agent_session_id" }) do
    if type(target[field]) ~= "string" or target[field] == "" then
      return nil, target_error("invalid_request", "target requires " .. field)
    end
  end
  local _, error = context.validate_review({ worktree = target.worktree, review_id = review.review_id })
  if error then return nil, target_error("invalid_request", error) end
  if not vim.deep_equal(target.worktree, review.worktree) then
    return nil, target_error("invalid_request", "target worktree must match the review worktree")
  end
  if not vim.deep_equal(connection.authority, review.worktree.authority) then
    return nil, target_error("invalid_request", "target connection authority must match the review authority")
  end
  local socket = vim.env.HERDR_SOCKET_PATH
  if type(socket) ~= "string" or socket == "" or connection.socket ~= socket then
    return nil, target_error("unsupported_capability", "the bundled Herdr boundary cannot route to the requested connection")
  end
  return vim.deepcopy(target)
end

local function malformed(stage)
  return operations.failure("invalid_state", "transport " .. stage .. " returned an invalid result")
end

local function transport_result(stage, result)
  if type(result) ~= "table" or type(result.ok) ~= "boolean" then return nil, malformed(stage) end
  if result.ok then return result.value end
  local error = result.error
  if type(error) ~= "table" or type(error.code) ~= "string" or error.code == ""
      or type(error.message) ~= "string" or error.message == "" or type(error.context) ~= "table" then
    return nil, malformed(stage)
  end
  return nil, { ok = false, error = vim.deepcopy(error) }
end

local function cancelled()
  return operations.failure("cancelled", "operation was cancelled")
end

local function send_with_transport(request, transport, done)
  local operation, state, finish
  operation, state, finish = operations.scheduled(done, function()
    local exported = operations.export(request, state)
    if not exported.ok then return exported end
    local batch = vim.deepcopy(exported.value)
    batch.transport = request.transport
    batch.submit = request.submit or false
    batch.strict_session_guard = request.strict_session_guard or false

    local function invoke(stage, values, success)
      local token = { name = stage, status = "starting", callback_seen = false }
      state.active_stage = token

      local function current()
        return not state.finished and state.active_stage == token and token.status == "waiting"
      end

      local function consume()
        if not current() or token.processing then return end
        token.processing = true
        vim.schedule(function()
          if state.finished or state.active_stage ~= token or not token.processing then return end
          token.processing = false
          token.status = "consumed"
          state.active_stage = nil
          local value, failure = transport_result(stage, token.callback_result)
          if failure then
            if stage == "deliver" then return finish(operations.failure("uncertain", "transport delivery was not confirmed; comments were kept")) end
            return finish(failure)
          end
          if state.cancelled then
            if stage == "deliver" then return finish(operations.failure("uncertain", "transport delivery was not confirmed; comments were kept")) end
            return finish(cancelled())
          end
          local ok, error = xpcall(function() success(value) end, debug.traceback)
          if not ok then
            if stage == "deliver" then return finish(operations.failure("uncertain", "transport delivery was not confirmed; comments were kept")) end
            return finish(operations.failure("failed", error))
          end
        end)
      end

      local function callback(result)
        if state.finished or state.active_stage ~= token or token.callback_seen or token.status == "terminal" then return end
        token.callback_seen = true
        local copied, value = pcall(vim.deepcopy, result)
        token.callback_result = copied and value or nil
        if token.status == "waiting" then consume() end
      end
      local ok, handle = pcall(transport[stage], vim.deepcopy(values), callback)
      if state.finished or state.active_stage ~= token or token.status == "terminal" then return end
      if not ok then
        token.status, state.active_stage = "terminal", nil
        if stage == "deliver" then return finish(operations.failure("uncertain", "transport delivery was not confirmed; comments were kept")) end
        return finish(operations.failure("invalid_state", "transport " .. stage .. " raised an error"))
      end
      if type(handle) ~= "table" or type(handle.cancel) ~= "function" then
        token.status, state.active_stage = "terminal", nil
        if stage == "deliver" then return finish(operations.failure("uncertain", "transport delivery was not confirmed; comments were kept")) end
        return finish(malformed(stage))
      end
      token.handle, token.status = handle, "waiting"
      if token.callback_seen then consume() end
    end

    local function deliver(target)
      if state.finished then return end
      state.delivery = "started"
      batch.target = vim.deepcopy(target)
      invoke("deliver", {
        batch = vim.deepcopy(batch),
        target = vim.deepcopy(target),
        submit = batch.submit,
        strict_session_guard = batch.strict_session_guard,
      }, function(value)
        if type(value) ~= "table" or value.outcome ~= "delivered_to_input" then
          return finish(operations.failure("uncertain", "transport delivery was not confirmed; comments were kept"))
        end
        state.delivery = "confirmed"
        if state.finished then return end
        local called, acknowledged = pcall(operations.acknowledge, batch.review, batch.members, state)
        if not called or not acknowledged.ok then
          return finish({ ok = true, value = {
            outcome = "delivered_to_input", batch_id = batch.id, warning = "acknowledgement could not be saved",
          } })
        end
        return finish({ ok = true, value = {
          outcome = "delivered_to_input", batch_id = batch.id, members = vim.deepcopy(batch.members), transport = request.transport,
        } })
      end)
    end

    local function validate(target)
      if state.finished then return end
      invoke("validate_target", {
        review = vim.deepcopy(batch.review),
        batch = vim.deepcopy(batch),
        target = vim.deepcopy(target),
        submit = batch.submit,
        strict_session_guard = batch.strict_session_guard,
      }, function(value)
        if type(value) ~= "table" then return finish(malformed("validate_target")) end
        deliver(value)
      end)
    end

    invoke("list_targets", {
      review = vim.deepcopy(batch.review),
      target = vim.deepcopy(request.target),
      submit = batch.submit,
      strict_session_guard = batch.strict_session_guard,
    }, function(value)
      local targets = type(value) == "table" and value.targets
      if type(targets) ~= "table" then return finish(malformed("list_targets")) end
      local selected = request.target
      if selected == nil then
        if #targets ~= 1 or type(targets[1]) ~= "table" then
          return finish(operations.failure("invalid_request", "transport send requires one discovered target or request.target"))
        end
        selected = targets[1]
      elseif type(selected) ~= "table" then
        return finish(operations.failure("invalid_request", "transport target must be a table"))
      else
        local found = false
        for _, target in ipairs(targets) do if vim.deep_equal(target, selected) then found = true; break end end
        if not found then return finish(operations.failure("target_mismatch", "transport target was not discovered")) end
      end
      if not state.finished then validate(selected) end
    end)
  end)
  local cancel = operation.cancel
  operation.cancel = function()
    cancel()
    if state.delivery == "confirmed" then return end
    local token = state.active_stage
    if token then
      state.active_stage, token.status = nil, "terminal"
      if type(token.handle) == "table" and type(token.handle.cancel) == "function" then pcall(token.handle.cancel) end
      if token.name == "deliver" then return finish(operations.failure("uncertain", "transport delivery was not confirmed; comments were kept")) end
    end
    finish(cancelled())
  end
  return operation
end

function M.send(request, done)
  callback(done)
  local review, error = context.validate_review(type(request) == "table" and request.review)
  if not review then return invalid(done, error) end
  local copied = vim.deepcopy(request)
  copied.review = review
  if copied.transport ~= nil and (type(copied.transport) ~= "string" or copied.transport == "") then
    return invalid(done, "transport must be a nonempty registered transport name")
  end
  if copied.submit ~= nil and type(copied.submit) ~= "boolean" then
    return invalid(done, "submit must be a boolean")
  end
  if copied.strict_session_guard ~= nil and type(copied.strict_session_guard) ~= "boolean" then
    return invalid(done, "strict_session_guard must be a boolean")
  end
  if copied.transport then
    local transport = transports[copied.transport]
    if not transport then return invalid(done, "transport is not registered") end
    return send_with_transport(copied, transport, done)
  end
  if copied.target ~= nil then
    local target, target_failure = validate_target(copied.target, review)
    if not target then
      return operations.scheduled(done, function()
        return operations.failure(target_failure.code, target_failure.message)
      end)
    end
    copied.target = target
  end
  if copied.strict_session_guard then
    return operations.scheduled(done, function()
      return operations.failure("unsupported_capability", "the bundled Herdr boundary has no conditional session delivery")
    end)
  end
  return legacy.send(copied, done)
end

local function register(registry, kind, name, value, methods)
  if type(name) ~= "string" or name == "" or type(value) ~= "table" then
    return { ok = false, error = { code = "invalid_request", message = kind .. " registration requires a name and table", context = {} } }
  end
  for _, method in ipairs(methods) do
    if type(value[method]) ~= "function" then
      return { ok = false, error = { code = "invalid_request", message = kind .. " is missing " .. method, context = {} } }
    end
  end
  registry[name] = value
  return { ok = true, value = { name = name, registered = true } }
end
function M.register_source_adapter(name, adapter)
  return register(source_adapters, "source adapter", name, adapter, { "resolve", "capture", "navigate" })
end
function M.register_transport(name, transport)
  return register(transports, "transport", name, transport, { "list_targets", "validate_target", "deliver" })
end

return M
