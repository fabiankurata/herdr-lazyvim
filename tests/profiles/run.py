#!/usr/bin/env python3
"""Plain and archived LazyVim keyboard workloads under the shared supervisor."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import pwd
import select
import shutil
import subprocess
import struct
import sys
import termios
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "live"))
from owned_session import OwnedSession, exception_evidence, write_json
from source import checked_source, exact_revision, git, new_artifact, source_archive

HARNESS = Path(__file__).resolve().parents[2]


def archive_copy(repo, revision, destination):
    with source_archive(repo, revision) as (source, provenance):
        shutil.copytree(source, destination)
    return provenance


def dependency_paths(environ=None):
    environ = os.environ if environ is None else environ
    # The profile inputs are archived from the account's installed plugin sources.
    # Test runtimes isolate XDG paths, so those runtime paths cannot identify them.
    installed = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/share/nvim/lazy"
    names = ("LazyVim", "lazy.nvim", "snacks.nvim", "persistence.nvim", "nvim-lspconfig", "mason.nvim", "mason-lspconfig.nvim", "blink.cmp", "friendly-snippets", "mini.pairs", "ts-comments.nvim", "mini.ai", "lazydev.nvim", "tokyonight.nvim", "catppuccin", "grug-far.nvim", "flash.nvim", "which-key.nvim", "gitsigns.nvim", "trouble.nvim", "todo-comments.nvim", "conform.nvim", "nvim-lint", "nvim-treesitter", "nvim-treesitter-textobjects", "nvim-ts-autotag", "bufferline.nvim", "lualine.nvim", "noice.nvim", "mini.icons", "nui.nvim", "plenary.nvim")
    paths = {name: installed / name for name in names}
    paths["LazyVim"] = Path(environ.get("LAZYVIM_SOURCE", paths["LazyVim"]))
    paths["lazy.nvim"] = Path(environ.get("LAZY_SOURCE", paths["lazy.nvim"]))
    return paths


def is_comment_composer(state):
    return (isinstance(state, dict) and str(state.get("mode", "")).startswith("i")
            and state.get("filetype") == "markdown" and state.get("buftype") == "nofile")


def is_comment_text_ready(state):
    return is_comment_composer(state) and state.get("lines") == ["profile keyboard comment"]


def bounded_redacted(value, root):
    text = str(value)
    for path in {str(root), str(root.resolve())}:
        text = text.replace(path, "<owned-runtime>")
    return text[:1024]


def remote_expression_observation(result, root):
    if result.returncode != 0:
        return {"category": "remote-expr-exit", "returncode": result.returncode,
                "stdout": bounded_redacted(result.stdout, root),
                "stderr": bounded_redacted(result.stderr, root)}
    try:
        state = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"category": "remote-expr-invalid-json",
                "stdout": bounded_redacted(result.stdout, root),
                "stderr": bounded_redacted(result.stderr, root)}
    return {"category": "success", "state": state}


def owned_composer_state(runtime, socket, artifact, profile):
    observation = {"socket": str(socket), "resolved_socket": str(socket.resolve())}
    path = artifact / (profile + "-composer-probe.json")
    if socket.parent.resolve() != runtime.root.resolve() or not socket.exists():
        write_json(path, {**observation, "category": "socket-unavailable"})
        return None
    expression = ("json_encode({\"mode\": mode(1), \"buffer\": bufnr(\"%\"), "
                  "\"filetype\": &filetype, \"buftype\": &buftype, "
                  "\"name\": bufname(\"%\"), "
                  "\"lines\": getbufline(bufnr(\"%\"), 1, \"$\")})")
    try:
        result = subprocess.run(["nvim", "--server", str(socket), "--remote-expr", expression],
                                cwd=runtime.root, env=runtime.env, capture_output=True, text=True,
                                timeout=1.0, check=False)
        observation.update(remote_expression_observation(result, runtime.root))
        write_json(path, observation)
        return observation.get("state")
    except subprocess.TimeoutExpired:
        write_json(path, {**observation, "category": "remote-expr-timeout"})
        return None
    except OSError as error:
        write_json(path, {**observation, "category": "remote-expr-os-error",
                          "error": bounded_redacted(error, runtime.root)})
        return None


def pty_comment(runtime, argv, artifact, profile):
    master, slave = pty.openpty()
    capture = bytearray()
    socket = runtime.root / (profile + ".nvim.sock")
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 120, 0, 0))
        child = runtime._spawn([*argv, "--listen", str(socket)], stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        slave = None
        def wait_for(predicate):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                runtime._remember()
                if predicate():
                    return
                if child.poll() is not None:
                    raise RuntimeError(profile + " editor exited before keyboard receipt")
                if select.select([master], [], [], .05)[0]:
                    capture.extend(os.read(master, 65536))
            raise TimeoutError(profile + " keyboard receipt deadline")
        wait_for(lambda: (runtime.root / "ready.json").exists())
        ready = json.loads((runtime.root / "ready.json").read_text())
        write_json(artifact / (profile + "-startup.json"), ready)
        if ready["errors"] or ready["comment_command"] != 2:
            raise RuntimeError(profile + " startup failed: " + json.dumps(ready))
        if profile == "lazy" and not ready["lazyvim_imported"]:
            raise RuntimeError("LazyVim plugin spec was not imported")
        os.write(master, b" rc")
        def composer_ready():
            state = owned_composer_state(runtime, socket, artifact, profile)
            if not is_comment_composer(state):
                return False
            write_json(artifact / (profile + "-composer.json"), {
                "socket": str(socket), "resolved_socket": str(socket.resolve()), "state": state,
            })
            return True
        wait_for(composer_ready)
        os.write(master, b"profile keyboard comment")
        def comment_text_ready():
            return is_comment_text_ready(owned_composer_state(runtime, socket, artifact, profile))
        wait_for(comment_text_ready)
        os.write(master, b"\x13")
        def persisted():
            state = Path(runtime.env["XDG_STATE_HOME"]) / "nvim/herdr-review"
            return state.exists() and any("profile keyboard comment" in path.read_text()
                                          for path in state.rglob("*.json"))
        wait_for(persisted)
        # Exercise normal exit; the supervisor owns all failure and timeout cleanup.
        os.write(master, b"\x1b\x1b:qa!\r")
        wait_for(lambda: child.poll() is not None)
        if child.returncode != 0:
            raise RuntimeError(profile + " editor exit failed")
    finally:
        (artifact / (profile + "-terminal.bin")).write_bytes(capture)
        os.close(master)
        if slave is not None:
            os.close(slave)


def profile_init(runtime, profile, plugin, site):
    init = runtime.root / "init.lua"
    spec = ""
    if profile == "lazy":
        spec = '''
vim.opt.rtp:prepend(SITE .. "/lazy.nvim")
local spec = {
  { "LazyVim/LazyVim", dir = SITE .. "/LazyVim", opts = { colorscheme = "habamax", news = { lazyvim = false, neovim = false } } },
  { import = "lazyvim.plugins" },
  { "folke/lazy.nvim", dir = SITE .. "/lazy.nvim" },
  { "folke/snacks.nvim", dir = SITE .. "/snacks.nvim", opts = { dashboard = { enabled = false } } },
  { "folke/persistence.nvim", dir = SITE .. "/persistence.nvim" },
  { "neovim/nvim-lspconfig", dir = SITE .. "/nvim-lspconfig" },
  { "mason-org/mason.nvim", dir = SITE .. "/mason.nvim" },
  { "mason-org/mason-lspconfig.nvim", dir = SITE .. "/mason-lspconfig.nvim" },
  { "saghen/blink.cmp", dir = SITE .. "/blink.cmp", build = false,
    opts = { fuzzy = { implementation = "lua" } } },
  { "rafamadriz/friendly-snippets", dir = SITE .. "/friendly-snippets" },
  { "nvim-treesitter/nvim-treesitter", build = false, opts = function(_, opts)
    opts.ensure_installed = {}
  end },
}
local core = {
  { "nvim-mini/mini.pairs", "mini.pairs", "mini.pairs" },
  { "folke/ts-comments.nvim", "ts-comments.nvim", "ts-comments.nvim" },
  { "nvim-mini/mini.ai", "mini.ai", "mini.ai" },
  { "folke/lazydev.nvim", "lazydev.nvim", "lazydev.nvim" },
  { "folke/tokyonight.nvim", "tokyonight.nvim", "tokyonight.nvim" },
  { "catppuccin/nvim", "catppuccin", "catppuccin" },
  { "MagicDuck/grug-far.nvim", "grug-far.nvim", "grug-far.nvim" },
  { "folke/flash.nvim", "flash.nvim", "flash.nvim" },
  { "folke/which-key.nvim", "which-key.nvim", "which-key.nvim" },
  { "lewis6991/gitsigns.nvim", "gitsigns.nvim", "gitsigns.nvim" },
  { "folke/trouble.nvim", "trouble.nvim", "trouble.nvim" },
  { "folke/todo-comments.nvim", "todo-comments.nvim", "todo-comments.nvim" },
  { "stevearc/conform.nvim", "conform.nvim", "conform.nvim" },
  { "mfussenegger/nvim-lint", "nvim-lint", "nvim-lint" },
  { "nvim-treesitter/nvim-treesitter", "nvim-treesitter", "nvim-treesitter" },
  { "nvim-treesitter/nvim-treesitter-textobjects", "nvim-treesitter-textobjects", "nvim-treesitter-textobjects" },
  { "windwp/nvim-ts-autotag", "nvim-ts-autotag", "nvim-ts-autotag" },
  { "akinsho/bufferline.nvim", "bufferline.nvim", "bufferline.nvim" },
  { "nvim-lualine/lualine.nvim", "lualine.nvim", "lualine.nvim" },
  { "folke/noice.nvim", "noice.nvim", "noice.nvim" },
  { "nvim-mini/mini.icons", "mini.icons", "mini.icons" },
  { "MunifTanjim/nui.nvim", "nui.nvim", "nui.nvim" },
  { "nvim-lua/plenary.nvim", "plenary.nvim", "plenary.nvim" },
}
for _, plugin in ipairs(core) do
  table.insert(spec, { plugin[1], dir = SITE .. "/" .. plugin[2] })
end
-- Keep the imported distribution's configuration and shortcuts. Optional plugin
-- integrations require their own pinned profile lane and are disabled here.
local enabled = {
  LazyVim = true,
  ["lazy.nvim"] = true,
  ["snacks.nvim"] = true,
  ["persistence.nvim"] = true,
  ["nvim-lspconfig"] = true,
  ["mason.nvim"] = true,
  ["mason-lspconfig.nvim"] = true,
  ["blink.cmp"] = true,
  ["friendly-snippets"] = true,
}
for _, plugin in ipairs(core) do enabled[plugin[3]] = true end
local Config = require("lazy.core.config")
local Plugin = require("lazy.core.plugin")
local original = Plugin.Spec.add
Plugin.Spec.add = function(self, value)
  if type(value) == "table" and type(value[1]) == "string" then
    local name = value.name or value[1]:match("([^/]+)$")
    if not enabled[name] then value.enabled = false end
  end
  return original(self, value)
end
require("lazy").setup(spec, {
  local_spec = false, checker = { enabled = false },
  change_detection = { enabled = false }, install = { missing = false },
  rocks = { enabled = false }, pkg = { enabled = false },
})
Plugin.Spec.add = original
vim.g.profile_lazyvim_imported = vim.tbl_contains(Config.spec.modules, "lazyvim.plugins")
'''
    init.write_text('''vim.g.mapleader = " "
vim.g.maplocalleader = "\\\\"
vim.g.profile_errors = {}
local notify = vim.notify
vim.notify = function(message, level, opts)
  if level == vim.log.levels.ERROR then table.insert(vim.g.profile_errors, tostring(message)) end
  return notify(message, level, opts)
end
local SITE = ''' + json.dumps(str(site)) + "\n" + spec + '''
vim.opt.rtp:prepend(''' + json.dumps(str(plugin)) + ''')
require("herdr_lazyvim").setup()
vim.api.nvim_create_autocmd("VimEnter", { once = true, callback = function()
  vim.defer_fn(function()
    local data = { comment_command = vim.fn.exists(":HerdrReviewComment"),
      lazyvim_imported = vim.g.profile_lazyvim_imported == true,
      lazyvim_config_loaded = package.loaded["lazyvim.config"] ~= nil,
      errors = vim.g.profile_errors }
    vim.fn.writefile({vim.json.encode(data)}, ''' + json.dumps(str(runtime.root / "ready.json")) + ''')
  end, 500)
end })
''')
    return init


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact_dir", type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--source-mode", choices=("checkout", "archive"), default="checkout")
    parser.add_argument("--source-repo", type=Path, default=HARNESS)
    args = parser.parse_args()
    artifact = None
    try:
        provenance = (checked_source if args.source_mode == "checkout" else exact_revision)(args.source_repo, args.revision)
        artifact = new_artifact(args.artifact_dir)
        dependencies = dependency_paths()
        results = {"source": provenance, "profiles": {}, "dependencies": {},
                   "optional_plugins": "UNVERIFIED", "native_cmd_keys": "UNVERIFIED",
                   "screenshots": "UNVERIFIED"}
        for profile in ("plain", "lazy"):
            with OwnedSession(artifact / profile, real=False) as runtime:
                source = runtime.root / "source"
                results["source"] = archive_copy(args.source_repo, args.revision, source)
                site = runtime.root / "site"
                site.mkdir()
                if profile == "lazy":
                    for name, repo in dependencies.items():
                        revision = git(repo, "rev-parse", "HEAD")
                        exact_revision(repo, revision)
                        results["dependencies"][name] = archive_copy(repo, revision, site / name)
                write_json(artifact / "sources.json", results)
                fixture = runtime.root / "profile.lua"
                fixture.write_text('local value = "synthetic profile fixture"\n')
                init = profile_init(runtime, profile, source / "nvim", site)
                pty_comment(runtime, ["nvim", "-u", str(init), "-i", "NONE", str(fixture)], artifact, profile)
            results["profiles"][profile] = "PASS"
        results["status"] = "PASS"
        write_json(artifact / "results.json", results)
        print(json.dumps(results))
        return 0
    except BaseException as exc:
        failure = {"status": "FAIL", **exception_evidence(exc)}
        if artifact is not None:
            write_json(artifact / "results.json", failure)
        print(failure["error"], file=sys.stderr)
        return 1


if __name__ == "__main__":
    from runtime_lease import runtime_lease_owner
    with runtime_lease_owner(repo=HARNESS):
        sys.exit(main())
