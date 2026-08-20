#!/usr/bin/env python3
"""Bootstrap the agent permission/sandbox policy (devworks/policy-req.md)
into a project or into user-global Claude Code config.

Implements GitHub issue #7: the policy says the shared-layer location
(repo-local committed vs user-global) is a user decision made explicit
per environment, with a user-global default when no repo exists. This
script is the concrete mechanism that asks that question (or honors a
prior answer) and acts on it.

What it does, each run:
  1. Detects whether the target directory is inside a git repo, and
     whether that repo is actually commit-capable (not bare, writable).
  2. Determines the shared-layer destination:
       - `--layer repo|global` (explicit, non-interactive) always wins.
       - Otherwise, a prior recorded choice (the marker file, see below)
         is honored silently, unless `--reset` is passed.
       - Otherwise, if no repo / no commit capability: default to
         user-global, per policy-req.md, and explain why, with no
         prompt.
       - Otherwise: prompt interactively, printing the tradeoff.
  3. Backs up any existing destination `settings.json` (and
     `settings.local.json`) to a timestamped `*.bak.<timestamp>` copy
     in the same directory *before* writing anything, per
     policy-req.md's "back up local settings before rewriting them"
     rule. Fails loudly, before any overwrite, if a backup cannot be
     made.
  4. Installs the shared `claude/` subtree (`settings.json` and
     `hooks/pretooluse_guard.py`, this directory's templates) verbatim
     to the chosen destination directory (`.claude/` for repo-local, or
     `~/.claude/` for user-global) and `settings.local.json.example` to
     `.claude/settings.local.json` at the detected project root (always
     local, never shared, regardless of the shared-layer choice).
  5. Records the choice in `.claude/.bootstrap-choice.json` (the
     "marker" file) so subsequent runs don't re-ask.

See devworks/policy-kit/README.md for usage, the marker format, and how
to reset/force a re-prompt.

Testing note: "user-global" resolves under the real home directory
(Path.home()) by default. Pass --home-dir <dir> (or set
CLAUDE_BOOTSTRAP_HOME) to redirect it elsewhere - this is for
tests/dry-runs only, so a global-layer test run can never land on a
developer's actual ~/.claude/settings.json. Normal use never needs it.
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent  # devworks/policy-kit/
CLAUDE_SUBTREE = SCRIPT_DIR / "claude"  # settings.json + hooks/ (the shared layer)
TEMPLATE_SETTINGS = CLAUDE_SUBTREE / "settings.json"
TEMPLATE_HOOKS_DIR = CLAUDE_SUBTREE / "hooks"
TEMPLATE_SETTINGS_LOCAL_EXAMPLE = CLAUDE_SUBTREE / "settings.local.json.example"

MARKER_FILENAME = ".bootstrap-choice.json"

TRADEOFF_TEXT = """
Where should the shared policy layer (settings.json) live?

  1) repo-local   .claude/settings.json, committed to git
                   - shared with your team, travels with the repo
  2) user-global   ~/.claude/settings.json
                   - machine-scoped only: NOT portable, NOT committed
                     anywhere; applies to every repo on this machine,
                     but nowhere else
"""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def fail(message):
    """Fail loudly: clear message on stderr, non-zero exit. Never used to
    paper over a partially-completed write - callers must call this
    *before* any destructive step whose precondition (e.g. a backup)
    failed."""
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Git detection
# ---------------------------------------------------------------------------


def _run_git(args, cwd):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def detect_git_repo(project_dir):
    """Returns (is_repo, repo_root, commit_capable, has_remote).

    commit_capable means: it's a real (non-bare) working tree and the
    repo root directory is writable by us. A missing remote does NOT
    make a repo non-commit-capable (you can commit locally without
    one) - it's surfaced separately as an informational note.
    """
    inside = _run_git(["rev-parse", "--is-inside-work-tree"], project_dir)
    if inside != "true":
        return False, None, False, False

    repo_root_str = _run_git(["rev-parse", "--show-toplevel"], project_dir)
    if not repo_root_str:
        return False, None, False, False
    repo_root = Path(repo_root_str)

    bare = _run_git(["rev-parse", "--is-bare-repository"], project_dir)
    commit_capable = (bare == "false") and os.access(repo_root, os.W_OK)

    remote_out = _run_git(["remote"], project_dir) or ""
    has_remote = bool(remote_out.strip())

    return True, repo_root, commit_capable, has_remote


# ---------------------------------------------------------------------------
# Marker (prior recorded choice)
# ---------------------------------------------------------------------------


def marker_path_for(claude_dir):
    return claude_dir / MARKER_FILENAME


def load_marker(claude_dir):
    """Returns the parsed marker dict, or None if absent/unreadable.
    A corrupt marker is treated as absent (with a warning) rather than
    a hard failure, since re-prompting is always a safe recovery."""
    path = marker_path_for(claude_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"WARNING: could not read marker file {path} ({exc}); "
            "ignoring it and re-deciding the layer.",
            file=sys.stderr,
        )
        return None
    if not isinstance(data, dict) or data.get("layer") not in ("repo", "global"):
        print(
            f"WARNING: marker file {path} has unexpected contents; "
            "ignoring it and re-deciding the layer.",
            file=sys.stderr,
        )
        return None
    return data


def write_marker(claude_dir, layer, project_root, source):
    path = marker_path_for(claude_dir)
    data = {
        "layer": layer,
        "timestamp": datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds"),
        "project_root": str(project_root),
        "source": source,  # "flag" | "marker" | "default-no-repo" | "prompt"
    }
    try:
        claude_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as exc:
        fail(f"Failed to write marker file {path}: {exc}")
    print(f"Recorded choice ({layer}) in {path}")


# ---------------------------------------------------------------------------
# Backup + install
# ---------------------------------------------------------------------------


def backup_if_exists(dest_path):
    """If dest_path exists, copy it to a timestamped .bak file in the
    same directory before anything overwrites it. Fails loudly (and
    does not proceed) if the backup itself cannot be made."""
    if not dest_path.exists():
        return None
    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    backup_path = dest_path.with_name(dest_path.name + f".bak.{ts}")
    try:
        shutil.copy2(dest_path, backup_path)
    except OSError as exc:
        fail(
            f"Failed to back up existing {dest_path} to {backup_path}: {exc}. "
            "Refusing to overwrite without a successful backup."
        )
    print(f"Backed up existing {dest_path} -> {backup_path}")
    return backup_path


def install_file(template_path, dest_path, label):
    if not template_path.exists():
        fail(f"Template {template_path} not found; cannot install {label}.")
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(f"Failed to create directory {dest_path.parent}: {exc}")
    backup_if_exists(dest_path)
    try:
        shutil.copy2(template_path, dest_path)
    except OSError as exc:
        fail(f"Failed to write {label} to {dest_path}: {exc}")
    print(f"Wrote {label} -> {dest_path}")


def _backup_then_copy(src, dst):
    """copytree's copy_function hook: back up whatever already sits at
    `dst` (a lone pre-existing file just as much as a file from a prior
    full kit install) before it's overwritten, then copy2 as usual."""
    backup_if_exists(Path(dst))
    return shutil.copy2(src, dst)


def install_tree(src_dir, dest_dir, label):
    """Copy `src_dir` into `dest_dir` verbatim (shutil.copytree), backing
    up any pre-existing destination file - individually, whichever files
    already happen to be there - before it's overwritten. Merges into an
    existing dest_dir rather than requiring it be absent."""
    if not src_dir.exists():
        fail(f"Template directory {src_dir} not found; cannot install {label}.")
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(f"Failed to create directory {dest_dir}: {exc}")
    try:
        shutil.copytree(src_dir, dest_dir, copy_function=_backup_then_copy, dirs_exist_ok=True)
    except OSError as exc:
        fail(f"Failed to install {label} to {dest_dir}: {exc}")
    print(f"Wrote {label} -> {dest_dir}")


def rewrite_global_hook_path(settings_path, absolute_hook_path):
    """For a user-global install only: rewrite the just-installed
    settings.json's PreToolUse hook command from the template's
    project-relative '${CLAUDE_PROJECT_DIR}/.claude/hooks/pretooluse_guard.py'
    to `absolute_hook_path` - the actual path the hook was just installed to.

    This matters because ${CLAUDE_PROJECT_DIR} resolves to whatever project
    is currently open, not to the home directory a global install lives
    under. Left unrewritten, a globally-installed hook silently fails to be
    found (python3 exits non-zero on the missing path) in any project that
    doesn't *also* have its own repo-local .claude/hooks/pretooluse_guard.py
    - and per Claude Code's hook contract, a hook command that can't be
    found or executed fails OPEN (the tool call proceeds), not closed. See
    the "Known limitations" section of README.md.

    Repo-local installs must never call this - their settings.json is
    already correct as installed and must remain byte-identical to the
    template's ${CLAUDE_PROJECT_DIR} form."""
    template_fragment = "${CLAUDE_PROJECT_DIR}/.claude/hooks/pretooluse_guard.py"
    try:
        data = json.loads(settings_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Failed to read/parse {settings_path} to rewrite the hook path: {exc}")

    matched = False
    for group in data.get("hooks", {}).get("PreToolUse", []):
        for hook in group.get("hooks", []):
            if hook.get("type") != "command":
                continue
            command = hook.get("command", "")
            if template_fragment in command:
                hook["command"] = command.replace(template_fragment, str(absolute_hook_path))
                matched = True

    if not matched:
        fail(
            f"Could not find the expected PreToolUse hook command fragment "
            f"({template_fragment!r}) to rewrite in {settings_path} - the "
            "settings.json structure or template path may have changed; "
            "update rewrite_global_hook_path() to match, rather than "
            "silently leaving a broken global hook path installed."
        )

    try:
        settings_path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as exc:
        fail(f"Failed to write rewritten {settings_path}: {exc}")
    print(f"Rewrote global hook path in {settings_path} -> {absolute_hook_path}")


# ---------------------------------------------------------------------------
# Layer decision
# ---------------------------------------------------------------------------


def prompt_layer_choice():
    """Interactive prompt. No default is offered here - the "default
    when uncertain" behavior belongs only to the no-repo/no-commit
    path, which never reaches this function (per policy-req.md, the
    default applies specifically to the no-repo/no-commit case)."""
    print(TRADEOFF_TEXT)
    while True:
        try:
            answer = input("Choose shared-layer location [repo/global]: ").strip().lower()
        except EOFError:
            fail(
                "No input available to answer the repo-vs-global prompt "
                "(stdin closed). Re-run with --layer repo|global for "
                "non-interactive use."
            )
        if answer in ("repo", "r", "repo-local"):
            return "repo"
        if answer in ("global", "g", "user-global"):
            return "global"
        print("Please answer 'repo' or 'global'.")


def determine_layer(args, is_repo, commit_capable, has_remote, project_root, claude_dir):
    """Returns (layer, source) where source explains how it was decided."""

    if args.layer:
        print(f"Using explicit --layer {args.layer} (non-interactive override).")
        return args.layer, "flag"

    if not args.reset:
        prior = load_marker(claude_dir)
        if prior is not None:
            print(
                f"Found recorded choice: layer={prior['layer']} "
                f"(recorded {prior['timestamp']}, source={prior.get('source', '?')}). "
                "Honoring it without re-prompting. Pass --reset to change it."
            )
            return prior["layer"], "marker"

    if not is_repo or not commit_capable:
        reason = (
            "no git repository detected"
            if not is_repo
            else "the git repo here is bare or not writable (no commit capability)"
        )
        print(f"Not prompting: {reason} under {project_root}.")
        print(
            "Defaulting to user-global shared settings (~/.claude/settings.json), "
            "per policy-req.md: 'Default when no repo exists, or the environment "
            "gives no way to commit: user-global.' Note the tradeoff: user-global "
            "is machine-scoped, not portable or committed anywhere."
        )
        return "global", "default-no-repo"

    if not has_remote:
        print(
            f"Note: {project_root} is a git repo with no configured remote - "
            "repo-local settings will be committed locally but not yet shared "
            "with a team until a remote is added."
        )
    return prompt_layer_choice(), "prompt"


# ---------------------------------------------------------------------------
# Home-dir resolution (where "user-global" points)
# ---------------------------------------------------------------------------


def _is_relative_to(path, other):
    """Path.is_relative_to() back-port (that method is 3.9+ only): True if
    `path` equals `other` or is nested underneath it."""
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def resolve_home_dir(args):
    """Resolve the directory that "user-global" (~/.claude/...) resolves
    under. Precedence: --home-dir flag > CLAUDE_BOOTSTRAP_HOME env var >
    the real home directory (Path.home()).

    This exists so testing/dry-run invocations can never accidentally
    write to a developer's real ~/.claude - normal use should never need
    to set either override, since the default is the real home dir.

    Both overrides are canonicalized with Path.resolve() (which follows
    symlinks all the way to the real filesystem path) and the resulting
    `<home>/.claude` is compared against where the real `~/.claude`
    resolves to. This is required, not just a nicety: `.is_dir()` alone
    does not detect a "sandboxed" test directory that is itself a symlink
    to the real `~/.claude`, or that merely contains a `.claude` symlink
    pointing back at it - either would let a test/dry-run run silently
    overwrite a developer's actual global settings, which is exactly the
    incident this override flag exists to prevent (see QUESTION 2 in the
    peer review). If the override does land on the real global config,
    refuse unless the caller passes --i-really-mean-global."""
    if args.home_dir:
        home_dir = Path(args.home_dir).expanduser().resolve()
        source = "--home-dir"
    elif os.environ.get("CLAUDE_BOOTSTRAP_HOME"):
        home_dir = Path(os.environ["CLAUDE_BOOTSTRAP_HOME"]).expanduser().resolve()
        source = "CLAUDE_BOOTSTRAP_HOME"
    else:
        return Path.home()

    if not home_dir.is_dir():
        fail(f"{source} {home_dir} is not a directory.")

    real_claude_dir = (Path.home().resolve() / ".claude").resolve()
    candidate_claude_dir = (home_dir / ".claude").resolve()
    lands_on_real_global = candidate_claude_dir == real_claude_dir or _is_relative_to(
        candidate_claude_dir, real_claude_dir
    )
    if lands_on_real_global and not args.i_really_mean_global:
        fail(
            f"{source} {home_dir} resolves (following symlinks) to "
            f"{candidate_claude_dir}, which is the real global Claude Code "
            f"config directory ({real_claude_dir}) or somewhere inside it. "
            "Refusing to write there: this override exists specifically so "
            "test/dry-run invocations can never touch a developer's actual "
            "~/.claude. Pass --i-really-mean-global if you really do intend "
            "to target the real global config non-interactively."
        )
    return home_dir


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Install the devworks/policy-kit agent permission policy "
            "(settings.json / hooks/ / settings.local.json) into the repo-local "
            "or user-global Claude Code config, per devworks/policy-req.md."
        )
    )
    parser.add_argument(
        "--project-dir",
        default=".",
        help=(
            "Directory to treat as the project root for detection and "
            "for the local (.claude/settings.local.json) install. "
            "Default: current directory. Mainly useful for testing or "
            "scripting against a repo you're not currently inside."
        ),
    )
    parser.add_argument(
        "--layer",
        choices=["repo", "global"],
        default=None,
        help=(
            "Non-interactive override: install the shared layer to "
            "repo-local or user-global config without prompting. "
            "Overrides any recorded choice and updates the marker."
        ),
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Ignore any previously recorded choice for this run (forces "
            "the prompt, or the no-repo default, or --layer if given) "
            "and overwrite the marker with the new choice."
        ),
    )
    parser.add_argument(
        "--home-dir",
        default=None,
        help=(
            "TESTING ONLY - override where 'user-global' resolves to: "
            "<home-dir>/.claude/settings.json instead of the real "
            "~/.claude/settings.json. Also honors the CLAUDE_BOOTSTRAP_HOME "
            "env var if this flag is not given (flag wins if both are "
            "set). Not needed for normal use - the real home directory is "
            "the default. Exists so test/dry-run invocations can never "
            "accidentally write to a developer's actual global Claude "
            "Code config."
        ),
    )
    parser.add_argument(
        "--i-really-mean-global",
        action="store_true",
        help=(
            "Required alongside --home-dir/CLAUDE_BOOTSTRAP_HOME when that "
            "override resolves (after following symlinks) to the real "
            "~/.claude directory, or to somewhere inside it. Without this "
            "flag, such an override is refused rather than silently "
            "writing to a developer's actual global config - see the "
            "warning on --home-dir above."
        ),
    )
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        fail(f"--project-dir {project_dir} is not a directory.")

    is_repo, repo_root, commit_capable, has_remote = detect_git_repo(project_dir)
    project_root = repo_root if is_repo else project_dir
    claude_dir = project_root / ".claude"

    print(f"Project root: {project_root}")
    print(f"Git repo detected: {is_repo}" + (f" (remote configured: {has_remote})" if is_repo else ""))

    layer, source = determine_layer(
        args, is_repo, commit_capable, has_remote, project_root, claude_dir
    )

    if layer == "repo":
        shared_dest_dir = claude_dir
    else:
        shared_dest_dir = resolve_home_dir(args) / ".claude"

    local_dest = claude_dir / "settings.local.json"

    print(f"Shared layer -> {shared_dest_dir}/settings.json (+ hooks/)")
    print(f"Local layer  -> {local_dest}")

    install_file(TEMPLATE_SETTINGS, shared_dest_dir / "settings.json", "shared settings.json")
    install_tree(TEMPLATE_HOOKS_DIR, shared_dest_dir / "hooks", "shared hooks/")

    if layer == "global":
        rewrite_global_hook_path(
            shared_dest_dir / "settings.json",
            shared_dest_dir / "hooks" / "pretooluse_guard.py",
        )

    install_file(
        TEMPLATE_SETTINGS_LOCAL_EXAMPLE, local_dest, "local settings.local.json"
    )

    write_marker(claude_dir, layer, project_root, source)

    if layer == "global":
        print(
            "Global install: rewrote the installed settings.json's PreToolUse "
            "hook command to the absolute installed path "
            f"({shared_dest_dir / 'hooks' / 'pretooluse_guard.py'}) in place of "
            "the template's project-relative "
            "'${CLAUDE_PROJECT_DIR}/.claude/hooks/pretooluse_guard.py', so the "
            "hook is found regardless of which project you currently have open."
        )

    print("Bootstrap complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
