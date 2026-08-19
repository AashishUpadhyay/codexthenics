#!/usr/bin/env python3
"""Automated self-check for the policy-req.md "Done when" checklist.

Implements GitHub issue #10. The policy doc (devworks/policy-req.md) ends
with an acceptance checklist, but verifying it has been manual so far. This
script probes each checklist item and reports pass/fail so the checklist is
testable after every bootstrap.py run or settings/hook change.

Scope: driving a real interactive Claude Code session's permission prompts
is not scriptable. The most faithful and practical thing this script can do
is call hooks/pretooluse_guard.py's actual decision logic directly, the same
way Claude Code's PreToolUse hook protocol does - JSON on stdin describing a
tool call (tool_name / tool_input / cwd), JSON on stdout describing the
decision - against constructed tool-call inputs matching each checklist
item, using planted fixture files under a throwaway temp sandbox (never real
credentials, never the real repo or $HOME).

pretooluse_guard.py only ever has an opinion on Write, Edit, and Bash tool
calls (see its main()); it has no logic for Read at all. So checklist items
about "reading" a secret file are expressed here as Bash `cat` invocations
(direct and piped), not as Read-tool calls - a Read-tool probe would trivially
and meaninglessly pass, since the hook never opines on Read either way.

One checklist item - "Policy files still prompt before edit" - is *not*
logic pretooluse_guard.py implements: it's native Claude Code settings.json
permission behavior (the interactive edit-confirmation prompt), not
something a PreToolUse hook decides. This script prints a SKIP/MANUAL result
for that item instead of faking a pass/fail. Likewise, "Playground scripts
cannot open host secrets or spawn subprocesses" is a runtime-sandbox / OS
isolation concern (policy-req.md's "Python isolation" section) that the hook
explicitly does not implement (it is a static analyzer only) - also reported
as SKIP/MANUAL.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent  # devworks/reference/
HOOK_PATH = SCRIPT_DIR / "hooks" / "pretooluse_guard.py"
SETTINGS_PATH = SCRIPT_DIR / "settings.json"

POLICY_DONE_WHEN = {
    "playground": "Python outside the playground is blocked, including `python -c`",
    "secrets": "Secret files cannot be read or `cat`'d; other reads do not prompt",
    "isolation": "Playground scripts cannot open host secrets or spawn subprocesses",
    "no_prompt": "Normal playground runs, web search, and non-destructive git/`gh` do not prompt",
    "prefixes": "One-off allow rules are gone; remaining allows are prefixes",
    "policy_edit": "Policy files still prompt before edit",
}

PASS, FAIL, MANUAL = "PASS", "FAIL", "MANUAL"


class Result:
    def __init__(self, number, description, bullet_key, status, detail=""):
        self.number = number
        self.description = description
        self.bullet_key = bullet_key
        self.status = status
        self.detail = detail

    def line(self):
        bullet = POLICY_DONE_WHEN[self.bullet_key]
        text = f"[{self.status:6}] #{self.number:<2} {self.description}\n         Done-when: {bullet}"
        if self.detail:
            text += f"\n         {self.detail}"
        return text


def run_hook(sandbox, tool_name, tool_input, verbose=False):
    """Feeds a synthetic PreToolUse payload to pretooluse_guard.py exactly as
    Claude Code's hook protocol would, and returns the resulting decision:
    "allow" (hook printed nothing -> no opinion), "deny", or "ask"."""
    payload = json.dumps(
        {"tool_name": tool_name, "tool_input": tool_input, "cwd": str(sandbox)}
    )
    env = os.environ.copy()
    env["PYTHON_PLAYGROUND_DIR"] = str(sandbox / "playground")
    env.pop("CLAUDE_PROJECT_DIR", None)

    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(sandbox),
        env=env,
        timeout=30,
    )

    if verbose:
        print(f"    >> stdin:  {payload}")
        print(f"    >> stdout: {proc.stdout.strip()!r}")
        print(f"    >> stderr: {proc.stderr.strip()!r}")
        print(f"    >> exit:   {proc.returncode}")

    if proc.returncode != 0:
        raise RuntimeError(
            f"hook exited {proc.returncode} (expected 0 per its documented contract); "
            f"stderr: {proc.stderr.strip()}"
        )

    if not proc.stdout.strip():
        return "allow", None

    decoded = json.loads(proc.stdout)
    decision = decoded["hookSpecificOutput"]["permissionDecision"]
    reason = decoded["hookSpecificOutput"].get("permissionDecisionReason")
    return decision, reason


def setup_sandbox():
    sandbox = Path(tempfile.mkdtemp(prefix="verify_checklist_"))
    playground = sandbox / "playground"
    playground.mkdir()
    (playground / "hello.py").write_text('print("hello")\n')
    (sandbox / ".env").write_text("FAKE_API_KEY=not_a_real_secret_12345\n")
    (sandbox / "fake.pem").write_text(
        "-----BEGIN FAKE PRIVATE KEY-----\nTHIS IS NOT A REAL KEY\n-----END FAKE PRIVATE KEY-----\n"
    )
    (sandbox / "notes.txt").write_text("just some notes\n")
    return sandbox


def check_must_block(results, sandbox, number, description, bullet_key, tool_name, tool_input, verbose):
    try:
        decision, reason = run_hook(sandbox, tool_name, tool_input, verbose)
    except Exception as exc:
        results.append(Result(number, description, bullet_key, FAIL, f"error running hook: {exc}"))
        return
    if decision in ("deny", "ask"):
        results.append(Result(number, description, bullet_key, PASS, f"hook decision: {decision}"))
    else:
        results.append(
            Result(number, description, bullet_key, FAIL, f"expected deny/ask, hook said: {decision}")
        )


def check_must_not_prompt(results, sandbox, number, description, bullet_key, tool_name, tool_input, verbose):
    try:
        decision, reason = run_hook(sandbox, tool_name, tool_input, verbose)
    except Exception as exc:
        results.append(Result(number, description, bullet_key, FAIL, f"error running hook: {exc}"))
        return
    if decision == "allow":
        results.append(Result(number, description, bullet_key, PASS, "hook decision: allow (no opinion)"))
    else:
        results.append(
            Result(
                number, description, bullet_key, FAIL,
                f"expected allow (no opinion), hook said: {decision} ({reason})",
            )
        )


def check_settings_prefixes(results, number):
    description = "settings.json allow/ask/deny entries are reusable prefixes, not one-offs"
    try:
        data = json.loads(SETTINGS_PATH.read_text())
    except Exception as exc:
        results.append(Result(number, description, "prefixes", FAIL, f"could not read/parse settings.json: {exc}"))
        return

    flagged = []
    for bucket in ("allow", "ask", "deny"):
        for entry in data.get("permissions", {}).get(bucket, []):
            lowered = entry.lower()
            if "http" in lowered:
                flagged.append((bucket, entry, "contains a literal URL"))
            elif any(f":{d}" in entry for d in "0123456789"):
                flagged.append((bucket, entry, "looks like it embeds a port"))
            elif "/tmp" in entry or "/var/folders" in entry:
                flagged.append((bucket, entry, "embeds a scratch/tmp path"))

    if flagged:
        detail = "; ".join(f"{b}:{e!r} ({why})" for b, e, why in flagged)
        results.append(Result(number, description, "prefixes", FAIL, detail))
    else:
        results.append(
            Result(number, description, "prefixes", PASS, f"{sum(len(data.get('permissions', {}).get(b, [])) for b in ('allow','ask','deny'))} entries checked, none flagged")
        )


def main():
    parser = argparse.ArgumentParser(
        description="Probe the policy-req.md 'Done when' checklist against pretooluse_guard.py's actual decision logic."
    )
    parser.add_argument(
        "--verbose", action="store_true", help="print each hook invocation's stdin/stdout/stderr/exit code"
    )
    args = parser.parse_args()

    if not HOOK_PATH.exists():
        print(f"ERROR: hook not found at {HOOK_PATH}", file=sys.stderr)
        return 1

    sandbox = setup_sandbox()
    results = []
    try:
        # --- must be blocked: python playground confinement ---
        check_must_block(
            results, sandbox, 1, "python -c 'print(1)' (inline interpreter)", "playground",
            "Bash", {"command": "python -c 'print(1)'"}, args.verbose,
        )
        check_must_block(
            results, sandbox, 2, "python3 <<EOF heredoc variant", "playground",
            "Bash", {"command": "python3 <<'EOF'\nprint(1)\nEOF"}, args.verbose,
        )
        check_must_block(
            results, sandbox, 3, "python3 - (bare stdin variant)", "playground",
            "Bash", {"command": "python3 -"}, args.verbose,
        )
        check_must_block(
            results, sandbox, 4, "Write tool targeting a .py file outside the playground", "playground",
            "Write", {"file_path": str(sandbox / "outside.py"), "content": "print(1)\n"}, args.verbose,
        )
        check_must_block(
            results, sandbox, 5, "shell redirect creating a .py file outside the playground", "playground",
            "Bash", {"command": 'echo "print(1)" > outside2.py'}, args.verbose,
        )
        check_must_block(
            results, sandbox, 6, "tee creating a .py file outside the playground", "playground",
            "Bash", {"command": 'echo "print(1)" | tee outside3.py'}, args.verbose,
        )

        # --- must be blocked: secrets ---
        check_must_block(
            results, sandbox, 7, "cat .env (direct read of planted fake secret)", "secrets",
            "Bash", {"command": "cat .env"}, args.verbose,
        )
        check_must_block(
            results, sandbox, 8, "cat fake.pem | wc -l (piped read of planted fake secret)", "secrets",
            "Bash", {"command": "cat fake.pem | wc -l"}, args.verbose,
        )

        # --- must not prompt ---
        check_must_not_prompt(
            results, sandbox, 9, "cat notes.txt (non-secret file read)", "secrets",
            "Bash", {"command": "cat notes.txt"}, args.verbose,
        )
        check_must_not_prompt(
            results, sandbox, 10, "python3 playground/hello.py (normal playground run)", "no_prompt",
            "Bash", {"command": "python3 playground/hello.py"}, args.verbose,
        )
        check_must_not_prompt(
            results, sandbox, 11, "git status (read-only git command)", "no_prompt",
            "Bash", {"command": "git status"}, args.verbose,
        )

        # --- static check, not via the hook ---
        check_settings_prefixes(results, 12)

        # --- manual/skip ---
        results.append(
            Result(
                13, "Playground scripts cannot open host secrets or spawn subprocesses", "isolation", MANUAL,
                "This is a runtime-sandbox / OS-isolation concern (policy-req.md 'Python isolation'). "
                "pretooluse_guard.py is a static analyzer only and does not implement runtime sandboxing "
                "(see its own README mapping table: 'outside this hook's static-analysis scope'). "
                "Verify by running an actual playground script under the configured host sandbox and "
                "confirming it cannot read host secrets or spawn a subprocess.",
            )
        )
        results.append(
            Result(
                14, "Policy files still prompt before edit", "policy_edit", MANUAL,
                "This is Claude Code's native file-edit permission behavior (settings.json rules + the "
                "interactive prompt), not logic pretooluse_guard.py's PreToolUse hook decides. Verify live: "
                "in an actual Claude Code session with the shipped settings.json installed, attempt to Edit "
                "a policy file (e.g. devworks/policy-req.md or .claude/settings.json) and confirm it prompts "
                "for confirmation rather than auto-applying.",
            )
        )
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    for r in results:
        print(r.line())
        print()

    passed = sum(1 for r in results if r.status == PASS)
    failed = sum(1 for r in results if r.status == FAIL)
    manual = sum(1 for r in results if r.status == MANUAL)
    print(f"{passed} passed, {failed} failed, {manual} manual")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
