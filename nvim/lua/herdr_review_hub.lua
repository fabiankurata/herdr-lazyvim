local M = {}

local ref = "refs/worktree/herdr-review/turn-base"
local reviewr_ref = "refs/worktree/reviewr/turn-base"
local state = {
  roots = {},
  status = {},
  pr = {},
  agents = {},
  refresh_generation = 0,
}

local function notify(message, level)
  vim.notify(message, level or vim.log.levels.INFO, { title = "Review Hub" })
end

local function realpath(path)
  return vim.uv.fs_realpath(path) or vim.fs.normalize(path)
end

local function current_root()
  local ok, lib = pcall(require, "diffview.lib")
  local view = ok and lib.get_current_view() or nil
  if view and view.adapter and view.adapter.ctx then
    return realpath(view.adapter.ctx.toplevel)
  end
  local name = vim.api.nvim_buf_get_name(0)
  local root = name ~= "" and vim.fs.root(name, ".git") or vim.fs.root(vim.fn.getcwd(), ".git")
  return root and realpath(root) or nil
end

local function run(args, root)
  local result = vim.system(args, { cwd = root, text = true }):wait()
  return result.code == 0 and vim.trim(result.stdout or "") or nil, result
end

local function git(root, args)
  local command = { "git" }
  vim.list_extend(command, args)
  return run(command, root)
end

local function root_state(root)
  state.roots[root] = state.roots[root] or { scope = "branch" }
  return state.roots[root]
end

local function default_base(root)
  local saved = root_state(root).base
  if saved and git(root, { "rev-parse", "--verify", "--quiet", saved .. "^{commit}" }) then
    return saved
  end
  local remote = git(root, { "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD" })
  if remote then
    return remote
  end
  for _, candidate in ipairs({ "origin/main", "main", "origin/master", "master" }) do
    if git(root, { "rev-parse", "--verify", "--quiet", candidate .. "^{commit}" }) then
      return candidate
    end
  end
end

local function close_diffview(callback)
  local ok, lib = pcall(require, "diffview.lib")
  if ok and lib.get_current_view() then
    vim.cmd("DiffviewClose")
    vim.schedule(callback)
  else
    callback()
  end
end

local function diffview_open(root, spec, scope, label)
  root_state(root).scope = scope
  root_state(root).spec = spec
  root_state(root).label = label
  vim.cmd.cd(vim.fn.fnameescape(root))
  close_diffview(function()
    vim.api.nvim_cmd({ cmd = "DiffviewOpen", args = { spec } }, {})
    M.refresh()
  end)
end

function M.uncommitted()
  local root = current_root()
  if not root then
    return notify("Open this from a Git project", vim.log.levels.WARN)
  end
  diffview_open(root, "HEAD", "uncommitted", "uncommitted")
end

function M.branch()
  local root = current_root()
  if not root then
    return notify("Open this from a Git project", vim.log.levels.WARN)
  end
  local base = default_base(root)
  if not base then
    return notify("Could not find main, master, or origin/HEAD", vim.log.levels.ERROR)
  end
  local merge_base = git(root, { "merge-base", "HEAD", base })
  if not merge_base then
    return notify("Could not find the merge base with " .. base, vim.log.levels.ERROR)
  end
  root_state(root).base = base
  diffview_open(root, merge_base, "branch", "branch vs " .. base)
end

function M.changes()
  local root = current_root()
  if not root then
    return notify("Open this from a Git project", vim.log.levels.WARN)
  end
  local config = root_state(root)
  if config.scope == "uncommitted" then
    M.uncommitted()
  elseif config.scope == "last_turn" then
    M.last_turn()
  elseif config.scope == "commits" and config.spec then
    diffview_open(root, config.spec, "commits", config.label or config.spec)
  else
    M.branch()
  end
end

local function snapshot_program()
  local plugin_root = vim.g.herdr_lazyvim_root
  if type(plugin_root) == "string" and plugin_root ~= "" then
    return plugin_root .. "/scripts/herdr-review-snapshot"
  end
  return vim.fn.exepath("herdr-review-snapshot")
end

local function snapshot(root, callback)
  local program = snapshot_program()
  if program == "" then
    return callback(nil, { stderr = "herdr-review-snapshot was not found" })
  end
  vim.system({ program, root }, { text = true }, function(result)
    vim.schedule(function()
      callback(result.code == 0 and vim.trim(result.stdout or "") or nil, result)
    end)
  end)
end

local function turn_base(root)
  local own = git(root, { "rev-parse", "--verify", "--quiet", ref })
  if own then
    return own, "last agent turn"
  end
  local fallback = git(root, { "rev-parse", "--verify", "--quiet", reviewr_ref })
  if fallback then
    return fallback, "last agent turn (existing Reviewr snapshot)"
  end
end

function M.last_turn()
  local root = current_root()
  if not root then
    return notify("Open this from a Git project", vim.log.levels.WARN)
  end
  local base, label = turn_base(root)
  if not base then
    return notify("No completed Herdr agent turn has been captured yet", vim.log.levels.WARN)
  end
  notify("Building the current worktree snapshot…")
  snapshot(root, function(tree, result)
    if not tree then
      notify("Could not snapshot the worktree: " .. vim.trim(result.stderr or ""), vim.log.levels.ERROR)
      return
    end
    diffview_open(root, base .. ".." .. tree, "last_turn", label)
  end)
end

function M.commits()
  local root = current_root()
  if not root then
    return notify("Open this from a Git project", vim.log.levels.WARN)
  end
  vim.ui.input({ prompt = "Commit or range: ", default = "HEAD^!" }, function(spec)
    if not spec or vim.trim(spec) == "" then
      return
    end
    spec = vim.trim(spec)
    local _, result = git(root, { "rev-list", "--max-count=1", spec })
    if result.code ~= 0 then
      notify("Git does not recognize " .. spec, vim.log.levels.ERROR)
      return
    end
    diffview_open(root, spec, "commits", spec)
  end)
end

function M.choose_base()
  local root = current_root()
  if not root then
    return notify("Open this from a Git project", vim.log.levels.WARN)
  end
  vim.ui.input({ prompt = "Compare branch against: ", default = default_base(root) or "origin/main" }, function(base)
    if not base or vim.trim(base) == "" then
      return
    end
    base = vim.trim(base)
    if not git(root, { "rev-parse", "--verify", "--quiet", base .. "^{commit}" }) then
      notify("Git does not recognize " .. base, vim.log.levels.ERROR)
      return
    end
    root_state(root).base = base
    M.branch()
  end)
end

function M.files()
  local root = current_root()
  if not root then
    return notify("Open this from a project", vim.log.levels.WARN)
  end
  Snacks.explorer({ cwd = root })
end

function M.lazygit()
  local root = current_root()
  if root then
    Snacks.lazygit({ cwd = root })
  end
end

local function pr_number(root, callback)
  vim.system(
    { "gh", "pr", "view", "--json", "number", "--jq", ".number" },
    { cwd = root, text = true },
    function(result)
      vim.schedule(function()
        callback(result.code == 0 and tonumber(vim.trim(result.stdout or "")) or nil, result)
      end)
    end
  )
end

local function with_github_auth(callback, quiet)
  vim.system({ "gh", "auth", "token", "-h", "github.com" }, { text = true }, function(result)
    vim.schedule(function()
      if result.code ~= 0 then
        if not quiet then
          notify("GitHub CLI needs authentication: run gh auth login -h github.com", vim.log.levels.ERROR)
        end
        return
      end
      callback()
    end)
  end)
end

function M.pr()
  local root = current_root()
  if not root then
    return notify("Open this from a GitHub project", vim.log.levels.WARN)
  end
  with_github_auth(function()
    pr_number(root, function(number)
      if number then
        vim.api.nvim_cmd({ cmd = "Octo", args = { "pr", "edit", tostring(number) } }, {})
      else
        vim.api.nvim_cmd({ cmd = "Octo", args = { "pr", "list" } }, {})
      end
    end)
  end)
end

function M.checks()
  local root = current_root()
  if root then
    with_github_auth(function()
      Snacks.terminal.open({ "gh", "pr", "checks", "--watch" }, {
        cwd = root,
        win = { position = "float", border = "rounded", width = 0.86, height = 0.82 },
      })
    end)
  end
end

function M.pr_menu()
  vim.ui.select({
    { label = "Open pull request", run = M.pr },
    { label = "Watch checks", run = M.checks },
    {
      label = "Review PR comments",
      run = function()
        vim.api.nvim_cmd({ cmd = "Octo", args = { "review", "comments" } }, {})
      end,
    },
    {
      label = "Start GitHub review",
      run = function()
        vim.api.nvim_cmd({ cmd = "Octo", args = { "review", "start" } }, {})
      end,
    },
  }, {
    prompt = "Pull request",
    format_item = function(item)
      return item.label
    end,
  }, function(item)
    if item then
      item.run()
    end
  end)
end

function M.open()
  vim.ui.select({
    { label = "Changes · current scope", run = M.changes },
    { label = "Changes · branch vs base", run = M.branch },
    { label = "Changes · uncommitted", run = M.uncommitted },
    { label = "Changes · last agent turn", run = M.last_turn },
    { label = "Changes · commit or range", run = M.commits },
    { label = "Files · project explorer", run = M.files },
    { label = "Git · LazyGit", run = M.lazygit },
    { label = "PR · pull request and checks", run = M.pr_menu },
    { label = "Base · choose comparison branch", run = M.choose_base },
  }, {
    prompt = "Review hub",
    format_item = function(item)
      return item.label
    end,
  }, function(item)
    if item then
      item.run()
    end
  end)
end

local function shortstat(text)
  local files = tonumber((text or ""):match("(%d+) files? changed")) or 0
  local additions = tonumber((text or ""):match("(%d+) insertions?")) or 0
  local deletions = tonumber((text or ""):match("(%d+) deletions?")) or 0
  return files, additions, deletions
end

local function refresh_pr(root)
  local cached = state.pr[root]
  local now = vim.uv.now()
  if cached and now - cached.updated_at < 30000 then
    return
  end
  state.pr[root] = { updated_at = now }
  local remote = git(root, { "remote", "get-url", "origin" })
  if not remote or not remote:match("github%.com") then
    return
  end
  with_github_auth(function()
    vim.system(
      { "gh", "pr", "view", "--json", "number,statusCheckRollup" },
      { cwd = root, text = true },
      function(result)
        local label
        if result.code == 0 then
          local ok, data = pcall(vim.json.decode, result.stdout)
          if ok and data and data.number then
            local failed, pending, total = 0, 0, 0
            for _, check in ipairs(data.statusCheckRollup or {}) do
              total = total + 1
              local conclusion = check.conclusion or ""
              local check_status = check.status or ""
              if
                conclusion == "FAILURE"
                or conclusion == "CANCELLED"
                or conclusion == "TIMED_OUT"
                or conclusion == "ACTION_REQUIRED"
              then
                failed = failed + 1
              elseif check_status ~= "COMPLETED" then
                pending = pending + 1
              end
            end
            if failed > 0 then
              label = string.format("PR #%d ✗%d", data.number, failed)
            elseif pending > 0 then
              label = string.format("PR #%d …%d/%d", data.number, pending, total)
            elseif total > 0 then
              label = string.format("PR #%d ✓%d", data.number, total)
            else
              label = string.format("PR #%d", data.number)
            end
          end
        end
        vim.schedule(function()
          state.pr[root] = { updated_at = now, label = label }
          vim.cmd("redrawstatus")
        end)
      end
    )
  end, true)
end

function M.refresh()
  local root = current_root()
  if not root then
    return
  end
  state.refresh_generation = state.refresh_generation + 1
  local generation = state.refresh_generation
  local config = root_state(root)
  local diff_spec = "HEAD"
  if config.scope == "branch" then
    local base = default_base(root)
    config.label = base and ("branch vs " .. base) or "branch"
    diff_spec = base and git(root, { "merge-base", "HEAD", base }) or "HEAD"
  elseif config.scope == "last_turn" then
    diff_spec = turn_base(root) or "HEAD"
  elseif config.scope == "commits" then
    diff_spec = config.spec or "HEAD^!"
  end
  local pending = { untracked = 0 }
  local function finish()
    if not pending.diff_done or not pending.untracked_done then
      return
    end
    vim.schedule(function()
      if generation ~= state.refresh_generation then
        return
      end
      local suffix = pending.untracked > 0 and string.format(" · ?%d", pending.untracked) or ""
      local pr = state.pr[root] and state.pr[root].label
      state.status[root] = string.format(
        "Review [%s] %d files +%d -%d%s",
        config.label or config.scope,
        pending.files + pending.untracked,
        pending.additions,
        pending.deletions,
        suffix
      ) .. (pr and (" · " .. pr) or "")
      vim.cmd("redrawstatus")
    end)
  end
  vim.system({ "git", "diff", "--shortstat", diff_spec }, { cwd = root, text = true }, function(result)
    if result.code ~= 0 then
      return
    end
    pending.files, pending.additions, pending.deletions = shortstat(result.stdout)
    pending.diff_done = true
    finish()
  end)
  if config.scope == "commits" then
    pending.untracked_done = true
    finish()
  else
    vim.system({ "git", "ls-files", "--others", "--exclude-standard" }, { cwd = root, text = true }, function(result)
      if result.code ~= 0 then
        return
      end
      for line in (result.stdout or ""):gmatch("[^\n]+") do
        if line ~= "" then
          pending.untracked = pending.untracked + 1
        end
      end
      pending.untracked_done = true
      finish()
    end)
  end
  refresh_pr(root)
end

function M.status()
  local root = current_root()
  local review = root and state.status[root] or ""
  local pr = root and state.pr[root] and state.pr[root].label
  if pr and not review:find(pr, 1, true) then
    return review ~= "" and (review .. " · " .. pr) or pr
  end
  return review
end

local function inside_root(path, root)
  path = path and realpath(path)
  return path and (path == root or vim.startswith(path, root .. "/"))
end

local function finish_turn(root, tracker)
  snapshot(root, function(tree)
    if not tree or not tracker.candidate then
      tracker.candidate = nil
      tracker.pending_end = false
      return
    end
    if tree ~= tracker.candidate then
      git(root, { "update-ref", ref, tracker.candidate })
      M.refresh()
    end
    tracker.candidate = nil
    tracker.pending_end = false
  end)
end

local function start_turn(root, tracker)
  tracker.generation = (tracker.generation or 0) + 1
  local generation = tracker.generation
  tracker.candidate = nil
  tracker.pending_end = false
  snapshot(root, function(tree)
    if generation ~= tracker.generation then
      return
    end
    tracker.candidate = tree
    if tracker.pending_end and tree then
      finish_turn(root, tracker)
    elseif not tree then
      tracker.pending_end = false
    end
  end)
end

local function poll_agents()
  if vim.env.HERDR_ENV ~= "1" or not vim.env.HERDR_WORKSPACE_ID then
    return
  end
  local root = current_root()
  if not root then
    return
  end
  local herdr = vim.env.HERDR_BIN_PATH or "herdr"
  vim.system({ herdr, "agent", "list" }, { text = true }, function(result)
    if result.code ~= 0 then
      return
    end
    local ok, response = pcall(vim.json.decode, result.stdout)
    local agents = ok and response and response.result and response.result.agents or {}
    vim.schedule(function()
      local seen = {}
      for _, agent in ipairs(agents) do
        if
          agent.workspace_id == vim.env.HERDR_WORKSPACE_ID
          and agent.pane_id ~= vim.env.HERDR_PANE_ID
          and inside_root(agent.foreground_cwd or agent.cwd, root)
        then
          seen[agent.pane_id] = true
          local tracker = state.agents[agent.pane_id] or {}
          local status_now = agent.agent_status
          local was_working = tracker.status == "working"
          local is_working = status_now == "working"
          if tracker.status and not was_working and is_working then
            start_turn(root, tracker)
          elseif was_working and not is_working then
            if tracker.candidate then
              finish_turn(root, tracker)
            else
              tracker.pending_end = true
            end
          end
          tracker.status = status_now
          tracker.root = root
          state.agents[agent.pane_id] = tracker
        end
      end
      for pane, tracker in pairs(state.agents) do
        if tracker.root == root and not seen[pane] then
          state.agents[pane] = nil
        end
      end
    end)
  end)
end

function M.setup()
  local commands = {
    HerdrReviewHub = M.open,
    HerdrReviewChanges = M.changes,
    HerdrReviewUncommitted = M.uncommitted,
    HerdrReviewBranch = M.branch,
    HerdrReviewLastTurn = M.last_turn,
    HerdrReviewCommits = M.commits,
    HerdrReviewFiles = M.files,
    HerdrReviewPR = M.pr,
    HerdrReviewChecks = M.checks,
  }
  for name, callback in pairs(commands) do
    vim.api.nvim_create_user_command(name, callback, {})
  end

  local maps = {
    { "<leader>ro", M.open, "Review hub" },
    { "<leader>r1", M.changes, "Review changes" },
    { "<leader>r2", M.files, "Review project files" },
    { "<leader>r3", M.pr_menu, "Review pull request" },
    { "<leader>ru", M.uncommitted, "Review uncommitted changes" },
    { "<leader>rb", M.branch, "Review branch changes" },
    { "<leader>rt", M.last_turn, "Review last agent turn" },
    { "<leader>rg", M.commits, "Review commit or range" },
    { "<leader>rf", M.files, "Review project files" },
    { "<leader>rp", M.pr_menu, "Review pull request" },
    { "<leader>ra", M.checks, "Review GitHub checks" },
    { "<leader>rB", M.choose_base, "Review choose base branch" },
  }
  for _, map in ipairs(maps) do
    vim.keymap.set("n", map[1], map[2], { desc = map[3] })
  end

  local refresh_group = vim.api.nvim_create_augroup("HerdrReviewHub", { clear = true })
  vim.api.nvim_create_autocmd({ "BufEnter", "FocusGained", "BufWritePost" }, {
    group = refresh_group,
    callback = function()
      vim.defer_fn(M.refresh, 150)
    end,
  })
  vim.defer_fn(M.refresh, 300)

  if vim.env.HERDR_ENV == "1" then
    local timer = vim.uv.new_timer()
    timer:start(1000, 2000, vim.schedule_wrap(poll_agents))
    vim.api.nvim_create_autocmd("VimLeavePre", {
      group = refresh_group,
      once = true,
      callback = function()
        timer:stop()
        timer:close()
      end,
    })
    M._timer = timer
  end
end

M._current_root = current_root
M._default_base = default_base
M._shortstat = shortstat
M._state = state

return M
