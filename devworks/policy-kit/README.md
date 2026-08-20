# Claude Code Policy Kit

This is the reference implementation of the agent permission and sandbox
policy in [`devworks/policy-req.md`](../policy-req.md), implementing GitHub
issues #6–#11. It contains ready-to-drop-in Claude Code configuration and a
hook script so nobody has to hand-translate the policy prose into
`settings.json` / hook logic again — copy the `claude/` directory into a repo
(or into your user-global config) and adjust the local placeholders.

## Layout

```
devworks/policy-kit/
  README.md
  bootstrap.py           <- installs the below for you
  verify_checklist.py    <- automated self-check for policy-req.md's checklist
  claude/                <- copy-ready: this whole subtree is deployed
                             verbatim to .claude/ (repo-local) or ~/.claude/
                             (global)
    settings.json
    settings.local.json.example
    hooks/
      pretooluse_guard.py
```

The `claude/` subdirectory is deliberately structured to mirror the deploy
target 1:1 — installing it is just "copy this directory to `.claude/`", no
per-file assembly required. See the `bootstrap.py` and "Installation" sections
below.

## Files

### `claude/settings.json`

The shared/committed layer. This is what would live at `.claude/settings.json`
in a repo, or be merged into `~/.claude/settings.json` for user-global use. It
contains:

- **Deny list** — the secrets paths/patterns from the policy doc's "Secrets
  deny list" section: dotenv files (`**/.env`, `**/.env.local`,
  `**/.env.*.local`), generic secret/credential globs, key material
  (`*.pem`, `*.p12`, service-account JSON, `credentials.json`, `*.tfstate`),
  the home-relative credential-store paths (`~/.ssh`, `~/.aws`, `~/.gnupg`,
  `~/.netrc`, `~/.npmrc`, `~/.pypirc`, `~/.config/gcloud`, `~/.kube`,
  `~/.docker/config.json`, `~/.config/gh/hosts.yml`, `~/.git-credentials`,
  `~/.pgpass`, shell history files), plus the equivalent `Bash(cat ...)`
  forms for those same home-relative paths, and environment/keychain dumping
  commands (`env`, `printenv`, `set`, `security find-*`/`dump-*`,
  `gh secret list`/`gh secret set`/`gh auth token`).
- **Baseline safe allow prefixes** — the read-only inspection tools named in
  the policy doc (`which`, `type`, `head`, `tail`, `wc`, `less`, `file`,
  `stat`, `du`, `df`, `tree`, `diff`, `cut`, `tr`, `column`, `uniq`, `sort`,
  `printf`, `grep`, `strings`, `jq`, `base64`, `yq`, `mkdir`), plus
  `WebSearch`.
- **`defaultMode: "acceptEdits"`** — never `bypassPermissions`, per the
  policy's permission posture.
- **Git/`gh`, curl/wget** — broadly allowed, but with `ask` carve-outs for
  destructive/irreversible git operations (force-push, hard reset,
  `clean -fdx`, `filter-branch`, branch/remote deletion, `rebase`), for
  credential-exposing `gh` subcommands (denied outright, not just asked), and
  for `curl`/`wget` invocations that carry data-posting flags (`-d`,
  `--data*`, `-F`/`--form`, `--upload-file`/`-T`, `--post-data`/
  `--post-file`) — read-only fetches are unaffected.
- **PreToolUse hook wiring** — registers `hooks/pretooluse_guard.py` on
  `Write|Edit|Bash`, invoked via `${CLAUDE_PROJECT_DIR}`.

**Why the hook path looks "wrong" inside this kit.** The committed hook
`command` reads:

```
python3 "${CLAUDE_PROJECT_DIR}/.claude/hooks/pretooluse_guard.py"
```

Notice that path doesn't exist yet anywhere under `devworks/policy-kit/` —
inside the kit the hook lives at `claude/hooks/pretooluse_guard.py`, with no
`.claude` segment. That's intentional: the path is written for where the file
will live *after* deployment, once the whole `claude/` subtree has been
copied to `.claude/` in the target repo — not for its current location inside
this kit. This is what makes the template copy-ready as-is: `bootstrap.py`
(and a manual `cp -r`) can drop `claude/` straight onto `.claude/` without
ever needing to rewrite this string, because the string was already written
for the destination, not the source. This holds for repo-local installs and
as the starting template for global installs; for `--layer global`,
`bootstrap.py` rewrites the *installed* copy's hook command to an absolute
path afterward — see "Global installs and the hook path caveat" under
`bootstrap.py` below.

### `claude/settings.local.json.example`

A template for the local/gitignored layer (`.claude/settings.local.json`).
It has two placeholder fields that **must** be edited before use:
`env.PYTHON_PLAYGROUND_DIR` and `permissions.additionalDirectories[0]`, both
marked with the literal placeholder `<YOUR_PLAYGROUND_PATH>` (the playground
may live outside the repo, hence the extra working-directory grant). It also
shows where to add personal allow prefixes via the
`<YOUR_PERSONAL_SAFE_PREFIX>` placeholder — delete that entry if you have no
personal prefixes to add.

### `claude/hooks/pretooluse_guard.py`

The `PreToolUse` hook that runs on every `Write`, `Edit`, and `Bash` call. It
enforces the two things a static allow/deny prefix list cannot express on its
own:

1. **Python playground confinement.** `.py`/`.ipynb` files may only be
   created or modified inside the configured playground directory — whether
   via `Write`/`Edit` directly, or indirectly through Bash (`>`/`>>`
   redirects, `tee`, `cp`/`mv`/`install`/`rsync`, `touch`). Inline
   interpreters are banned outright: `python -c ...`, `python -` (stdin),
   and piping data straight into a bare interpreter with no script argument
   all get denied, regardless of where they run.
2. **Whole-command secrets-path scanning.** Because prefix-style allow/deny
   rules can't see past shell separators, the hook tokenizes each Bash
   command with `shlex` and splits it into stages on `&&`, `||`, `;`, `|`,
   `|&`, and `&`, then checks every word and redirect target in every stage
   against the policy doc's secrets deny list (e.g. it catches
   `echo hi && cat ~/.aws/credentials`, which a simple `Bash(cat ~/.aws/**)`
   deny rule alone would miss if it only matched the whole string). It also
   denies bare `env`/`printenv`/`set` and macOS `security find-*`/`dump-*`
   invocations wherever they appear in the stage sequence.

**Fail-closed by design.** Any command containing backticks, `$(...)`
command substitution, parentheses (subshells/process substitution), or a
heredoc (`<<`) is not analyzed further — the hook immediately returns `ask`,
because those constructs can hide an unparsed command from the stage-by-stage
scan. The same applies to tokenization failures, unrecognized shell
operators, and any unexpected internal error (caught and turned into an
`ask` rather than crashing open). When nothing is flagged, the hook prints no
JSON and exits `0` with no opinion, letting the normal `settings.json`
permission flow (and the interactive prompt) proceed — it never emits an
explicit "allow", so it can only add restrictions, never grant extra ones.

**Protocol convention.** The hook communicates its decision through Claude
Code's JSON-on-stdout `PreToolUse` contract: it always exits `0` and prints
`{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision":
"deny" | "ask", "permissionDecisionReason": "..."}}` (a human-readable copy
of the reason also goes to stderr for transcript/log visibility). This was
chosen over the older bare exit-code convention (`0` = allow, `2` = block)
because the policy's fail-closed posture needs a genuine three-way signal —
allow (no opinion), ask, deny — and exit code `2` can only express a hard
block; per the Claude Code hooks docs, exit code `2` is treated as a blocking
override regardless of any JSON body, so it cannot be used to request "ask".
Using JSON decisions with exit `0` for both `deny` and `ask` is the only way
to get the "ask" branch honored.

### `bootstrap.py`

A script that installs the `claude/` subtree for you, instead of doing the
copy/edit steps in the "Installation" sections below by hand. It implements
GitHub issue #7: the shared-layer location (repo-local, committed vs.
user-global) is a decision made explicit per environment, with a user-global
default when there's no repo to commit into.

**What it does, each run:**

1. Detects whether `--project-dir` (default: current directory) is inside a
   commit-capable git repo (a real, non-bare, writable working tree).
2. Decides where the shared layer goes:
   - `--layer repo` / `--layer global` (non-interactive) always wins.
   - Otherwise, a prior recorded choice (see "the marker file" below) is
     honored silently, unless `--reset` is passed.
   - Otherwise, if there's no repo, or no commit capability: defaults to
     user-global, with an explanation printed, no prompt.
   - Otherwise: prompts interactively for `repo` or `global`, printing the
     tradeoff.
3. Backs up any existing destination file (`settings.json`,
   `hooks/pretooluse_guard.py`, `settings.local.json`) to a timestamped
   `<name>.bak.<UTC-timestamp>` copy in the same directory **before** writing
   anything — whether the destination previously held a full prior kit
   install or just a stray individual file. If a backup itself can't be
   made, it fails loudly and does not touch the original.
4. Installs the shared `claude/settings.json` and `claude/hooks/` tree
   verbatim to the chosen destination directory (`.claude/` for repo-local,
   `~/.claude/` for user-global — done with `shutil.copytree`, not per-file
   copy logic, since the kit's own layout now mirrors the deploy target) and
   `settings.local.json.example` to `.claude/settings.local.json` at the
   detected project root — the local file is always installed locally,
   regardless of the shared-layer choice.
5. Records the choice in the marker file so future runs don't re-ask.

**Running it, interactively** (prompts if no repo choice can be inferred and
none is recorded yet):

```
python3 devworks/policy-kit/bootstrap.py --project-dir /path/to/your/repo
```

**Running it non-interactively / scripted**, with the layer forced by flag
(no prompt, ever, regardless of marker state):

```
python3 devworks/policy-kit/bootstrap.py --project-dir /path/to/your/repo --layer repo
# or
python3 devworks/policy-kit/bootstrap.py --project-dir /path/to/your/repo --layer global
```

`--layer` also updates the marker, so a later plain run without `--layer`
will honor that choice.

**The "don't re-ask" marker file.** After the first run, the choice is
recorded at `.claude/.bootstrap-choice.json` under the detected project
root (`{"layer": "repo"|"global", "timestamp": ..., "project_root": ...,
"source": "flag"|"marker"|"default-no-repo"|"prompt"}`). Every subsequent
run reads this file first and, if present and well-formed, reuses the
recorded layer silently instead of prompting or re-deriving the no-repo
default — this is what makes it safe to re-run the script (e.g. as part of
setup automation) without it nagging you every time. A missing or corrupt
marker is treated the same as no prior choice (re-decided, with a warning
for the corrupt case), never a hard failure.

To force it to forget the recorded choice and decide again (prompting,
or re-applying the no-repo default, or honoring `--layer` if you also pass
one), use `--reset`:

```
python3 devworks/policy-kit/bootstrap.py --project-dir /path/to/your/repo --reset
```

**`--home-dir` (testing only).** By default "user-global" resolves under the
real home directory (`Path.home()/.claude/`). `--home-dir
<dir>` (or the `CLAUDE_BOOTSTRAP_HOME` env var, if you'd rather not pass a
flag — the flag wins if both are set) redirects that resolution to
`<dir>/.claude/` instead. This exists purely so tests and
dry-runs of a `--layer global` / no-repo-default install can point at a
throwaway temp directory instead of risking a write to someone's actual
`~/.claude/`. Normal use never needs to pass it — the default
is already the real home directory. If given, the directory must already
exist (the script fails loudly rather than silently falling back if it
doesn't). If the resolved directory turns out (after following symlinks) to
be the real `~/.claude` or somewhere inside it, the script refuses to
proceed unless `--i-really-mean-global` is also passed.

**Global installs and the hook path caveat.** The committed template's hook
command reads `${CLAUDE_PROJECT_DIR}/.claude/hooks/pretooluse_guard.py`,
which resolves relative to whichever project you're *currently* in, not to
your home directory — left as-is in a global install, the hook would only be
found while you're inside a project that also has its own
`.claude/hooks/pretooluse_guard.py`. `bootstrap.py` handles this
automatically: for a `--layer global` run, after copying the shared layer to
`~/.claude/` (or `--home-dir`), it rewrites the *installed*
`settings.json`'s hook command in place to the absolute path the hook was
just installed to (e.g. `$HOME/.claude/hooks/pretooluse_guard.py`), so the
hook is found regardless of which project you currently have open. No manual
path edit or reminder is needed for `bootstrap.py`-driven global installs —
repo-local installs are left untouched, since `${CLAUDE_PROJECT_DIR}` is
already correct for them.

### `verify_checklist.py`

Implements GitHub issue #10: an automated self-check for the policy doc's
"Done when" acceptance checklist (see the bottom of `policy-req.md`), so the
checklist is testable after every `bootstrap.py` run or settings/hook change
instead of being verified by hand.

Driving a real interactive Claude Code session's permission prompts isn't
scriptable, so this validates `claude/hooks/pretooluse_guard.py`'s decision
logic directly — the same way Claude Code's `PreToolUse` hook protocol does:
JSON describing a tool call (`tool_name` / `tool_input` / `cwd`) on stdin, a
JSON decision on stdout. It builds a throwaway temp sandbox with planted
fixtures (a playground dir with a real script, a dummy `.env`, a clearly-fake
`fake.pem`, and a non-secret file — never real credentials), constructs
`Bash`/`Write` tool-call inputs matching each checklist item, feeds them to
the hook, and checks the decision against what that item requires. It also
statically audits `claude/settings.json`'s `allow`/`ask`/`deny` arrays for
leftover one-off entries (URLs, ports, scratch paths) instead of prefixes.
The sandbox is deleted on exit whether the run passes or fails — including
if sandbox setup itself fails partway through, which is reported as a clean
`FAIL` result rather than an uncaught traceback.

Because the hook only ever has an opinion on `Write`, `Edit`, and `Bash`
calls (it has no logic for `Read` at all), the "read a secret file" checklist
items are expressed as `Bash cat` invocations, direct and piped — not as
`Read`-tool calls, which would trivially and meaninglessly pass.

Two checklist items are out of the hook's scope and are reported as an
explicit `MANUAL` result (with the reason) rather than a faked pass/fail:

- **"Playground scripts cannot open host secrets or spawn subprocesses"** —
  a runtime-sandbox/OS-isolation concern (the policy doc's "Python
  isolation" section); the hook is a static analyzer only, per the "Known
  limitations" / mapping-table notes above.
- **"Policy files still prompt before edit"** — native Claude Code
  settings.json file-edit permission behavior, not `PreToolUse` hook logic;
  must be verified live in an actual session against the shipped
  `settings.json`.

Run it with:

```
python3 devworks/policy-kit/verify_checklist.py
```

Add `--verbose` to also print each hook invocation's stdin/stdout/stderr/exit
code. It prints a `PASS`/`FAIL`/`MANUAL` line per checklist item (with the
`policy-req.md` bullet it maps to) and a summary line, e.g. `12 passed, 0
failed, 2 manual`. It exits non-zero if any automated item fails; `MANUAL`
items never affect the exit code.

## Installation into a fresh repo (repo-local, committed)

`bootstrap.py` (see above) does this for you, with backup-before-overwrite.
The manual steps remain useful for understanding what it's doing, or if you
need to deviate from the defaults.

1. Copy `devworks/policy-kit/claude/` to `.claude/` in the target repo — a
   straight directory copy (`cp -r devworks/policy-kit/claude .claude`, or
   equivalent), since the kit's layout mirrors the deploy target.
2. Rename `.claude/settings.local.json.example` to
   `.claude/settings.local.json`, and replace every `<YOUR_PLAYGROUND_PATH>`
   with the absolute path to your Python playground directory (it may live
   outside the repo), and `<YOUR_PERSONAL_SAFE_PREFIX>` with any personal
   allow prefix you want, or delete that line if you have none.
   `.claude/settings.local.json` should stay gitignored (the `.example`
   template itself is fine to leave in place or delete).
3. Make sure `.claude/hooks/pretooluse_guard.py` is executable (`chmod +x`).
   `.claude/settings.json`'s hook command already points at it via
   `${CLAUDE_PROJECT_DIR}/.claude/hooks/pretooluse_guard.py` — no path
   editing needed, since that's exactly where step 1 just put it.
4. No further manual editing is required beyond the local-path edits in
   step 2.

## Installation into user-global config (no repo, or shared across all repos)

1. Copy `devworks/policy-kit/claude/hooks/pretooluse_guard.py` to e.g.
   `~/.claude/hooks/pretooluse_guard.py` (`chmod +x` it).
2. Copy the contents of `devworks/policy-kit/claude/settings.json` into
   `~/.claude/settings.json`, but change the hook `command` to reference the
   absolute/`$HOME`-relative path you used in step 1 (e.g.
   `python3 "$HOME/.claude/hooks/pretooluse_guard.py"`), since
   `${CLAUDE_PROJECT_DIR}` resolves to whatever project you're currently in,
   not to your home directory — see the "Global installs and the hook path
   caveat" note under `bootstrap.py` above.
3. Set `PYTHON_PLAYGROUND_DIR` and any personal allow prefixes directly in
   `~/.claude/settings.json` — user-global config has no separate "local"
   layer. Note the tradeoff the policy doc calls out: user-global settings
   are machine-scoped, not portable or committed anywhere.

## Testing the hook manually

Pipe a synthetic `PreToolUse` payload into the script on stdin, for example:

```
echo '{"tool_name":"Bash","tool_input":{"command":"cat ~/.ssh/id_rsa"}}' \
  | python3 devworks/policy-kit/claude/hooks/pretooluse_guard.py
```

should print a JSON payload with `"permissionDecision": "deny"`.

## Known limitations

- This is a best-effort static analyzer, not a full shell interpreter. It
  deliberately asks — never silently allows — on constructs it can't safely
  reason about: command substitution, subshells, heredocs, and unrecognized
  shell operators.
- It stops at the first violation found per Bash command; it does not
  enumerate every issue in a single call.
- The `Bash(git *)`-style rule syntax in `settings.json` matches the
  convention documented at code.claude.com/docs/en/hooks as of Aug 2026. If
  your installed Claude Code version uses different permission-rule syntax,
  adjust the `allow`/`ask`/`deny` patterns to match (check the in-app
  permissions UI to confirm current syntax).
- Per code.claude.com/docs/en/hooks, PreToolUse hooks fail **open**, not
  closed: if the hook `command` can't be found or executed (e.g. a missing
  or non-executable script exiting 127), Claude Code treats that as a
  non-blocking error and lets the tool call proceed — only exit code `2`
  from a hook that actually ran and chose to block stops the call. There is
  no built-in "hook missing = block" fallback. Practically: if
  `pretooluse_guard.py` is later deleted, moved, or has its permissions
  changed after a correct install, the secrets/playground guard silently
  stops running and every `Write`/`Edit`/`Bash` call proceeds exactly as if
  no hook were configured at all, with no error surfaced to block it.

## Mapping to the policy doc's "Done when" checklist

`verify_checklist.py` (above) automates checking every row of this table
except the "Playground scripts cannot open host secrets..." and "Policy
files still prompt before edit" rows, which it reports as `MANUAL` for the
reasons given in its own section above — those two are already noted below
as out of the hook's static-analysis scope.

| `policy-req.md` "Done when" item | Satisfied by |
| --- | --- |
| One-off allow rules are gone; remaining allows are prefixes | `claude/settings.json` — every `allow`/`ask`/`deny` entry is a reusable prefix or glob pattern, no URLs/ports/scratch paths/quoted script bodies |
| Python outside the playground is blocked, including `python -c` | `claude/hooks/pretooluse_guard.py` — `check_write_edit` (Write/Edit) and `check_bash_command`'s interpreter/redirect/`tee`/`touch`/copy-verb checks (Bash), both denying `.py`/`.ipynb` outside `PYTHON_PLAYGROUND_DIR` and denying `-c`/stdin/bare-pipe interpreter invocations outright |
| Secret files cannot be read or `cat`'d; other reads do not prompt | `claude/settings.json` deny list (Read/Edit/Write + `Bash(cat ...)` entries) plus the hook's `secrets_match` stage-by-stage scan for compound commands the deny-list prefixes can't see past |
| Playground scripts cannot open host secrets or spawn subprocesses | Documented as a runtime-sandbox concern in the policy doc's "Python isolation" section (host sandbox / OS isolation for the playground process) — outside this hook's static-analysis scope; the hook's secrets deny-list scan is the static-checks half of the "sandbox if available, else static checks + deny list + hooks" fallback posture |
| Normal playground runs, web search, and non-destructive git/`gh` do not prompt | `claude/settings.json` `allow` list (`WebSearch`, `Bash(git *)`, `Bash(gh *)`, baseline read-only prefixes) plus `defaultMode: "acceptEdits"`, with the hook returning no opinion (exit 0, no output) for anything it doesn't flag |
| Policy files still prompt before edit | Not covered by an explicit rule in this reference `settings.json` (no repo-specific policy-file path to name generically) — reference implementers should add an `ask` rule for their own policy file(s), e.g. `Edit(devworks/policy-req.md)`, or rely on the default interactive prompt since these files aren't covered by any `allow` entry above |
