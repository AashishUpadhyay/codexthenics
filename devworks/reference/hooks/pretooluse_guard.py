#!/usr/bin/env python3
"""PreToolUse guard hook for Claude Code (Write / Edit / Bash).

Enforces two rules from devworks/policy-req.md:
  1. Python playground confinement: .py / .ipynb files may only be
     created or written outside a designated "playground" directory
     never - whether via the Write/Edit tools directly, or indirectly
     through a Bash command (redirect, `tee`, `cp`, `mv`, `touch`), and
     inline interpreters (`python -c`, `python -`, piping into python)
     are banned outright.
  2. Secrets-path scan: every stage of a (possibly compound/piped)
     Bash command is checked against the policy doc's secrets deny
     list, because prefix-style allow/deny rules cannot see past
     `&&`, `|`, `;`, etc. (e.g. `safe && cat ~/.ssh/id_rsa`).

Fail-closed: any parse error, unrecognized shell syntax, command
substitution (`$(...)`, backticks), subshell/process-substitution
`(...)`, or heredoc (`<<`) causes this hook to ASK rather than
silently allow, because such constructs can hide an unanalyzed
command from the stage-by-stage scan below.

PROTOCOL CONVENTION USED (documented here per task requirement):
This hook uses Claude Code's *JSON-on-stdout* PreToolUse convention,
exiting 0 and printing:
    {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                             "permissionDecision": "deny" | "ask",
                             "permissionDecisionReason": "..."}}
verified live against https://code.claude.com/docs/en/hooks (fetched
during authoring, Aug 2026). This convention was chosen over the
bare exit-code convention (0=allow / 2=block) because exit-code-2
can only express a hard block - it cannot express "ask", which the
policy's fail-closed posture requires for merely-unparseable (but
not necessarily malicious) commands. Per that same doc, exit code 2
is a *blocking* override regardless of JSON body, so this hook never
uses exit 2 for an "ask" decision - only JSON decisions, always with
exit 0, are used for both "deny" and "ask" so the fine-grained
decision field is actually honored. A human-readable copy of the
reason is also written to stderr for visibility in transcripts/logs.
When no violation is found, this hook emits *no* JSON and exits 0 -
it never emits an explicit "allow", so it can only add restrictions
on top of the normal permission flow (settings.json rules + the
interactive prompt), never grant extra allowances.
"""

import fnmatch
import json
import os
import re
import shlex
import sys

# ---------------------------------------------------------------------------
# Playground resolution
# ---------------------------------------------------------------------------

BASE_CWD = os.getcwd()
PLAYGROUND_DIR = None  # resolved in main()


def resolve_playground_dir(data):
    """Playground path comes from local (machine-specific) config only -
    never hardcoded here. Falls back to <project>/python-playground if
    that directory already exists, matching this repo's own convention.
    Returns None if it cannot be determined (callers must fail closed)."""
    env_val = os.environ.get("PYTHON_PLAYGROUND_DIR")
    if env_val:
        return env_val
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd")
    if project_dir:
        candidate = os.path.join(project_dir, "python-playground")
        if os.path.isdir(candidate):
            return candidate
    return None


def is_within_playground(path):
    """Returns True, False, or None (= "cannot verify", caller must ask)."""
    if PLAYGROUND_DIR is None:
        return None
    try:
        abs_path = os.path.abspath(os.path.join(BASE_CWD, os.path.expanduser(path)))
        abs_pg = os.path.abspath(os.path.expanduser(PLAYGROUND_DIR))
    except Exception:
        return None
    return abs_path == abs_pg or abs_path.startswith(abs_pg + os.sep)


# ---------------------------------------------------------------------------
# Secrets deny list (mirrors the "Secrets deny list" section of
# devworks/policy-req.md)
# ---------------------------------------------------------------------------

GLOB_BASENAME_PATTERNS = [
    ".env",
    ".env.local",
    "*secret*",
    "*credential*",
    "*.pem",
    "*.p12",
    "*service-account*.json",
    "credentials.json",
    "*.tfstate",
]

HOME_RELATIVE_EXACT = [
    "~/.netrc",
    "~/.npmrc",
    "~/.pypirc",
    "~/.docker/config.json",
    "~/.config/gh/hosts.yml",
    "~/.git-credentials",
    "~/.pgpass",
    "~/.zsh_history",
    "~/.bash_history",
]

HOME_RELATIVE_DIRS = [
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.config/gcloud",
    "~/.kube",
]


def _is_dotenv_local_variant(basename_lower):
    # **/.env.*.local, e.g. .env.production.local
    return basename_lower.startswith(".env.") and basename_lower.endswith(".local")


def secrets_match(candidate):
    """True if `candidate` (a path/argument string appearing in a tool
    call) matches any entry in the policy doc's secrets deny list."""
    if not candidate:
        return False
    basename = os.path.basename(candidate.rstrip("/"))
    low_basename = basename.lower()
    if _is_dotenv_local_variant(low_basename):
        return True
    for pattern in GLOB_BASENAME_PATTERNS:
        if fnmatch.fnmatch(low_basename, pattern.lower()):
            return True
    try:
        abs_candidate = os.path.abspath(os.path.join(BASE_CWD, os.path.expanduser(candidate)))
    except Exception:
        return True  # cannot resolve -> fail closed, treat as suspicious
    for p in HOME_RELATIVE_EXACT:
        if abs_candidate == os.path.abspath(os.path.expanduser(p)):
            return True
    for d in HOME_RELATIVE_DIRS:
        target = os.path.abspath(os.path.expanduser(d))
        if abs_candidate == target or abs_candidate.startswith(target + os.sep):
            return True
    return False


# ---------------------------------------------------------------------------
# Bash command parsing: split on &&, ||, ;, |, |&, & (per policy doc /
# issue #6), fail closed on anything we can't safely decompose.
# ---------------------------------------------------------------------------

SEPARATORS = {"&&", "||", ";", "|", "|&", "&"}
REDIRECTS = {">", ">>", "<"}
KNOWN_OPERATORS = SEPARATORS | REDIRECTS
PUNCT_CHARS = set("();<>|&")

INTERPRETERS = {"python", "python3", "python2"}
COPY_VERBS = {"cp", "mv", "install", "rsync"}


class UnsafeCommand(Exception):
    """Raised for anything this hook cannot safely analyze -> caller asks."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def tokenize(command):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    tokens = []
    while True:
        tok = lexer.get_token()
        if tok is None:
            break
        tokens.append(tok)
    return tokens


def classify(tokens):
    """Returns list of (kind, tok) with kind in {"sep", "redir", "word"}.
    Raises UnsafeCommand on any punctuation token this hook doesn't
    recognize (e.g. `&>`, `;;`, `<<<` slipping through) rather than
    silently mis-parsing it."""
    classified = []
    for tok in tokens:
        if tok and all(c in PUNCT_CHARS for c in tok):
            if tok not in KNOWN_OPERATORS:
                raise UnsafeCommand(
                    f"command contains an operator this hook does not recognize ('{tok}'); "
                    "cannot safely analyze all stages"
                )
            kind = "sep" if tok in SEPARATORS else "redir"
            classified.append((kind, tok))
        else:
            classified.append(("word", tok))
    return classified


def split_stages(classified):
    """Groups classified tokens into (stage_tokens, preceding_separator)."""
    stages = []
    current = []
    seps = []
    last_sep = None
    for kind, tok in classified:
        if kind == "sep":
            stages.append(current)
            seps.append(last_sep)
            current = []
            last_sep = tok
        else:
            current.append((kind, tok))
    stages.append(current)
    seps.append(last_sep)
    return list(zip(stages, seps))


def check_bash_command(command):
    """Returns (decision, reason) or None if no violation found.
    decision is "deny" or "ask"."""

    # --- raw fail-closed guards: constructs we refuse to reason about ---
    if "`" in command:
        return ("ask", "command contains a backtick command substitution; cannot safely analyze all stages")
    if "$(" in command:
        return ("ask", "command contains $(...) command substitution; cannot safely analyze all stages")
    if "(" in command or ")" in command:
        return ("ask", "command contains parentheses (subshell / process substitution); cannot safely analyze all stages")
    if "<<" in command:
        return ("ask", "command contains a heredoc or herestring (<< / <<<); cannot safely analyze all stages")

    normalized = re.sub(r"[\r\n]+", " ; ", command)

    try:
        tokens = tokenize(normalized)
    except ValueError as e:
        return ("ask", f"command failed to tokenize safely ({e}); cannot verify it is free of secrets access")

    try:
        classified = classify(tokens)
    except UnsafeCommand as e:
        return ("ask", e.reason)

    for stage, preceding_sep in split_stages(classified):
        if not stage:
            continue
        words = []
        redirects = []
        i = 0
        while i < len(stage):
            kind, tok = stage[i]
            if kind == "redir":
                if i + 1 >= len(stage) or stage[i + 1][0] != "word":
                    return ("ask", f"redirection operator '{tok}' with no clear target; cannot safely analyze")
                redirects.append((tok, stage[i + 1][1]))
                i += 2
                continue
            words.append(tok)
            i += 1

        if not words:
            continue

        cmd0 = os.path.basename(words[0])
        stage_str = " ".join(t for _, t in stage)

        # 1. secrets-path scan across every word / redirect target in this stage
        for cand in list(words) + [t for (_, t) in redirects]:
            if secrets_match(cand):
                return (
                    "deny",
                    f"command stage `{stage_str}` accesses a path matched by the secrets deny list ('{cand}')",
                )

        # 2. environment / secret-store dumping commands
        if cmd0 in ("env", "printenv"):
            return ("deny", f"command stage `{stage_str}` runs '{cmd0}', which can dump secret environment variables")
        if cmd0 == "set" and len(words) == 1:
            return ("deny", "unqualified 'set' dumps the shell environment, including secret vars")
        if cmd0 == "security" and len(words) >= 2 and (words[1].startswith("find-") or words[1].startswith("dump-")):
            return ("deny", f"command stage `{stage_str}` reads the OS keychain/secret store")

        # 3. python playground enforcement
        if cmd0 in INTERPRETERS:
            rest = words[1:]
            if "-c" in rest:
                return ("deny", "inline Python via 'python -c' is banned; write a .py file in the playground instead")
            if rest and rest[-1] == "-":
                return ("deny", "Python reading from stdin ('python -') is banned; write a .py file in the playground instead")
            has_script_arg = any(not w.startswith("-") for w in rest)
            if preceding_sep in ("|", "|&") and not has_script_arg:
                return (
                    "deny",
                    "piping data into a Python interpreter with no script file is banned; write a .py file in the playground instead",
                )

        for op, target in redirects:
            if op in (">", ">>") and target.lower().endswith((".py", ".ipynb")):
                verdict = is_within_playground(target)
                if verdict is False:
                    return ("deny", f"redirect '{op} {target}' would create a Python file outside the playground")
                if verdict is None:
                    return ("ask", f"cannot verify playground location for redirect target '{target}'; set PYTHON_PLAYGROUND_DIR")

        if cmd0 == "tee":
            for arg in words[1:]:
                if arg.startswith("-") or not arg.lower().endswith((".py", ".ipynb")):
                    continue
                verdict = is_within_playground(arg)
                if verdict is False:
                    return ("deny", f"'tee {arg}' would create a Python file outside the playground")
                if verdict is None:
                    return ("ask", f"cannot verify playground location for 'tee {arg}'; set PYTHON_PLAYGROUND_DIR")

        if cmd0 == "touch":
            for arg in words[1:]:
                if arg.startswith("-") or not arg.lower().endswith((".py", ".ipynb")):
                    continue
                verdict = is_within_playground(arg)
                if verdict is False:
                    return ("deny", f"'touch {arg}' would create a Python file outside the playground")
                if verdict is None:
                    return ("ask", f"cannot verify playground location for 'touch {arg}'; set PYTHON_PLAYGROUND_DIR")

        if cmd0 in COPY_VERBS and len(words) >= 3:
            dest = words[-1]
            for src in words[1:-1]:
                if not src.lower().endswith((".py", ".ipynb")):
                    continue
                check_target = dest if dest.lower().endswith((".py", ".ipynb")) else os.path.join(dest, os.path.basename(src))
                verdict = is_within_playground(check_target)
                if verdict is False:
                    return ("deny", f"'{cmd0} {src} {dest}' would place a Python file outside the playground")
                if verdict is None:
                    return ("ask", f"cannot verify playground location for '{cmd0} {src} {dest}'; set PYTHON_PLAYGROUND_DIR")

    return None


def check_write_edit(tool_name, tool_input):
    file_path = tool_input.get("file_path") or tool_input.get("path") or ""
    if not file_path:
        return None
    if secrets_match(file_path):
        return ("deny", f"{tool_name} targets a path matched by the secrets deny list: {file_path}")
    lower = file_path.lower()
    if lower.endswith(".py") or lower.endswith(".ipynb"):
        verdict = is_within_playground(file_path)
        if verdict is False:
            return (
                "deny",
                f"{tool_name} would create/modify a Python file outside the configured playground: {file_path}. "
                "Move it into the playground (set via PYTHON_PLAYGROUND_DIR).",
            )
        if verdict is None:
            return (
                "ask",
                f"cannot verify playground location for '{file_path}' (PYTHON_PLAYGROUND_DIR is not set and no "
                "default playground directory was found); confirm before allowing this Python file write.",
            )
    return None


# ---------------------------------------------------------------------------
# Decision emission
# ---------------------------------------------------------------------------

def emit_decision(decision, reason):
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,  # "deny" or "ask" - never "allow" from this hook
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(payload))
    sys.stderr.write(reason + "\n")
    sys.exit(0)  # exit 0 so the JSON decision (deny/ask) is honored; see module docstring


def main():
    try:
        raw = sys.stdin.read()
    except Exception as e:
        emit_decision("ask", f"hook failed to read stdin: {e}")
        return

    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        emit_decision("ask", f"hook failed to parse tool-call JSON: {e}")
        return

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {}) or {}

    global BASE_CWD, PLAYGROUND_DIR
    BASE_CWD = data.get("cwd") or os.getcwd()
    PLAYGROUND_DIR = resolve_playground_dir(data)

    try:
        if tool_name in ("Write", "Edit"):
            result = check_write_edit(tool_name, tool_input)
        elif tool_name == "Bash":
            command = tool_input.get("command", "")
            result = check_bash_command(command) if isinstance(command, str) and command.strip() else None
        else:
            result = None
    except Exception as e:
        emit_decision("ask", f"policy hook internal error, failing closed: {e}")
        return

    if result:
        emit_decision(*result)
    # else: no opinion - exit 0 with no output, normal permission flow applies.


if __name__ == "__main__":
    main()
