---@alias Action 'show'|'hide'|'toggle'
---@alias Observation 'absent'|'hidden'|'here'|'elsewhere'|'ambiguous'
---@alias Progress 'idle'|'uncertain_show'|'uncertain_hide'|'confirmed_show'|'confirmed_hide'
---@class PrototypeRequest
---@field action Action
---@field observed Observation
---@field progress Progress
---@field owned boolean

local function parse(args)
  if #args ~= 4 then return nil, 'invalid_request' end
  local sets = {
    { show = true, hide = true, toggle = true },
    { absent = true, hidden = true, here = true, elsewhere = true, ambiguous = true },
    { idle = true, uncertain_show = true, uncertain_hide = true, confirmed_show = true, confirmed_hide = true },
    { verified = true, unverified = true },
  }
  local errors = { 'invalid_action', 'invalid_observation', 'invalid_progress', 'invalid_ownership' }
  for i, allowed in ipairs(sets) do
    if not allowed[args[i]] then return nil, errors[i] end
  end
  return { action = args[1], observed = args[2], progress = args[3], owned = args[4] == 'verified' }
end

---@param r PrototypeRequest
local function plan(r)
  if r.progress == 'uncertain_show' or r.progress == 'uncertain_hide' then
    return 'observe', 'uncertain', true
  end
  if r.observed == 'ambiguous' or (not r.owned and r.observed ~= 'absent') then
    return 'none', 'needs_attention', true
  end
  if r.progress == 'confirmed_show' then return 'none', 'show', false end
  if r.progress == 'confirmed_hide' then return 'none', 'hide', false end
  local show = r.action == 'show' or (r.action == 'toggle' and r.observed ~= 'here')
  if show then
    if r.observed == 'absent' then return 'spawn', 'show', true end
    if r.observed == 'here' then return 'none', 'show', false end
    return 'move', 'show', true
  end
  if r.observed == 'here' or r.observed == 'elsewhere' then return 'park', 'hide', true end
  return 'none', 'hide', false
end

local request, err = parse(arg)
if err then
  io.stdout:write(vim.json.encode({ error = err }) .. '\n')
  vim.cmd('cquit 2')
else
  local effect, outcome, retain_guard = plan(request)
  io.stdout:write(vim.json.encode({ effect = effect, outcome = outcome, retain_guard = retain_guard }) .. '\n')
  vim.cmd('qa!')
end
