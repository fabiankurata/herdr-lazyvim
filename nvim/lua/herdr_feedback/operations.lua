local context = require("herdr_feedback.context")
local store = require("herdr_feedback.store")
local completion = require("herdr_feedback.completion")
local M = {}

local function uuid()
  local bytes = assert(vim.uv.random(16)); local parts = {}
  for i = 1, #bytes do parts[i] = string.format("%02x", bytes:byte(i)) end
  parts[7] = "4" .. parts[7]:sub(2); parts[9] = string.format("%x", (tonumber(parts[9]:sub(1, 1), 16) % 4) + 8) .. parts[9]:sub(2)
  local hex = table.concat(parts)
  return table.concat({ hex:sub(1, 8), hex:sub(9, 12), hex:sub(13, 16), hex:sub(17, 20), hex:sub(21, 32) }, "-")
end
local function success(value) return { ok = true, value = value } end
local function failure(code, message, extra)
  local error = { code = code, message = message, context = {} }
  if extra then for key, value in pairs(extra) do error[key] = value end end
  return { ok = false, error = error }
end

function M.normalize(records)
  local changed = false
  for _, item in ipairs(records) do
    local valid_id = type(item.id) == "string" and item.id:match("^%x%x%x%x%x%x%x%x%-%x%x%x%x%-4%x%x%x%-[89ab]%x%x%x%-%x%x%x%x%x%x%x%x%x%x%x%x$")
    if not valid_id then item.id, changed = uuid(), true end
    if type(item.revision) ~= "number" or item.revision % 1 ~= 0 or item.revision < 1 then item.revision, changed = 1, true end
  end
  return records, changed
end

function M.new_id()
  return uuid()
end

local function records_for(review, control)
  local records, error = store.read(review)
  if not records then return nil, failure("invalid_state", error) end
  local _, changed = M.normalize(records)
  if changed then
    local normalized, update_error = store.update(review, function(current)
      M.normalize(current)
      return current, vim.deepcopy(current)
    end, control)
    if store.is_cancelled(update_error) then return nil, failure("cancelled", "operation was cancelled") end
    if not normalized then return nil, failure("write_failed", update_error) end
    return normalized
  end
  records = vim.deepcopy(records)
  return records
end

function M.list(request, control)
  local review, error = context.validate_review(type(request) == "table" and request.review)
  if not review then return failure("invalid_request", error) end
  local records, failed = records_for(review, control); if not records then return failed end
  return success(vim.deepcopy(records))
end

function M.add_captured(captured, text, control)
  local source = captured.source
  local record, error = store.update(captured.review, function(records)
    M.normalize(records)
    local created = { id = uuid(), revision = 1, file = source.file, start = source.start, finish = source.finish, lines = source.lines, text = text, removed = source.removed or nil }
    table.insert(records, created)
    return records, vim.deepcopy(created)
  end, control)
  if store.is_cancelled(error) then return failure("cancelled", "operation was cancelled") end
  if not record then return failure("write_failed", error) end
  return success(record)
end

function M.add(request, control)
  local captured, error = context.capture(request)
  if not captured then return failure("invalid_request", error) end
  return M.add_captured(captured, request.text, control)
end

local function existing(operation, id, request, control)
  if type(id) ~= "string" or type(request) ~= "table" then return failure("invalid_request", operation .. " requires an id and request") end
  local review, error = context.validate_review(request.review)
  if not review then return failure("invalid_request", error) end
  if type(request.expected_revision) ~= "number" or request.expected_revision % 1 ~= 0 or request.expected_revision < 1 then return failure("invalid_request", operation .. " requires expected_revision") end
  if operation == "edit" and type(request.text) ~= "string" then return failure("invalid_request", "edit requires text") end
  local result, write_error = store.update(review, function(records)
    M.normalize(records)
    for index, record in ipairs(records) do
      if record.id == id then
        if record.revision ~= request.expected_revision then
          return false, failure("revision_conflict", "annotation changed", { local_text = record.text })
        end
        if operation == "edit" then
          record.text, record.revision = request.text, record.revision + 1
          return records, success(vim.deepcopy(record))
        end
        table.remove(records, index)
        return records, success({ id = id, revision = request.expected_revision + 1 })
      end
    end
    return false, failure("not_found", "annotation was not found")
  end, control)
  if store.is_cancelled(write_error) then return failure("cancelled", "operation was cancelled") end
  if not result then return failure("write_failed", write_error) end
  return result
end
function M.edit(id, request, control) return existing("edit", id, request, control) end
function M.delete(id, request, control) return existing("delete", id, request, control) end

function M.export(request, control)
  local listed = M.list(request, control); if not listed.ok then return listed end
  local requested = {}
  if request.annotation_ids then
    if type(request.annotation_ids) ~= "table" then return failure("invalid_request", "annotation_ids must be a list") end
    for _, id in ipairs(request.annotation_ids) do requested[id] = true end
  end
  local records = {}
  for _, record in ipairs(listed.value) do if not request.annotation_ids or requested[record.id] then table.insert(records, record) end end
  table.sort(records, function(a, b) return a.file == b.file and a.start < b.start or a.file < b.file end)
  local blocks = {}
  for _, record in ipairs(records) do
    local location = record.file .. ":" .. record.start .. (record.start == record.finish and "" or "-" .. record.finish)
    if record.removed then location = location .. " (removed)" end
    table.insert(blocks, table.concat({ location, record.lines, record.text }, "\n"))
  end
  local review = assert(context.validate_review(request.review))
  local members = {}; for _, record in ipairs(records) do table.insert(members, { id = record.id, revision = record.revision }) end
  return success({ api_version = 1, id = uuid(), review = review, members = members, records = vim.deepcopy(records), payload = table.concat(blocks, "\n\n") })
end

function M.acknowledge(review, members, control)
  if type(members) ~= "table" then return failure("invalid_request", "delivery members must be a list") end
  local selected = {}
  for _, member in ipairs(members) do
    if type(member) ~= "table" or type(member.id) ~= "string" or type(member.revision) ~= "number" then
      return failure("invalid_state", "delivery members are malformed")
    end
    selected[member.id .. "\0" .. member.revision] = true
  end
  local acknowledged, write_error = store.update(review, function(records)
    M.normalize(records)
    local remaining = {}
    for _, record in ipairs(records) do
      if not selected[record.id .. "\0" .. record.revision] then table.insert(remaining, record) end
    end
    return remaining, vim.deepcopy(members)
  end, control)
  if store.is_cancelled(write_error) then return failure("cancelled", "operation was cancelled") end
  if not acknowledged then return failure("write_failed", write_error) end
  return success({ acknowledged = acknowledged })
end

function M.scheduled(done, work) return completion.scheduled(done, work) end
function M.failure(code, message, context_) return completion.failure(code, message, context_) end
return M
