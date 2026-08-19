# devworks/reference/

This directory is the reference implementation of the agent permission and
sandbox policy in [`devworks/policy-req.md`](../policy-req.md), implementing
GitHub issue #6. It contains ready-to-drop-in Claude Code configuration and a
hook script so nobody has to hand-translate the policy prose into
`settings.json` / hook logic again — copy these files into a repo (or into
your user-global config) and adjust the local placeholders.

## Files

### `settings.json`

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
  `Write|Edit|Bash`, invoked via `${CLAUDE_PROJECT_DIR}` so the file contains
  no machine-specific absolute paths and can be committed as-is.

### `settings.local.json.example`

A template for the local/gitignored layer (`.claude/settings.local.json`).
It has two placeholder fields that **must** be edited before use:
`env.PYTHON_PLAYGROUND_DIR` and `permissions.additionalDirectories[0]`, both
marked with the literal placeholder `<YOUR_PLAYGROUND_PATH>` (the playground
may live outside the repo, hence the extra working-directory grant). It also
shows where to add personal allow prefixes via the
`<YOUR_PERSONAL_SAFE_PREFIX>` placeholder — delete that entry if you have no
personal prefixes to add.

### `hooks/pretooluse_guard.py`

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

## Installation into a fresh repo (repo-local, committed)

1. Copy this whole `devworks/reference/` directory into the target repo.
2. Copy `devworks/reference/settings.json` to `.claude/settings.json`
   (create `.claude/` if it doesn't exist).
3. Copy `devworks/reference/settings.local.json.example` to
   `.claude/settings.local.json`, and replace every `<YOUR_PLAYGROUND_PATH>`
   with the absolute path to your Python playground directory (it may live
   outside the repo), and `<YOUR_PERSONAL_SAFE_PREFIX>` with any personal
   allow prefix you want, or delete that line if you have none.
   `.claude/settings.local.json` should stay gitignored.
4. Make sure `devworks/reference/hooks/pretooluse_guard.py` is executable
   (`chmod +x`). The `settings.json` hook command already points at it via
   `${CLAUDE_PROJECT_DIR}/devworks/reference/hooks/pretooluse_guard.py`, so
   it must stay at that relative path — or you must update the `command` in
   `settings.json` to match wherever you actually put it.
5. No further manual editing is required beyond the local-path edits in
   step 3.

## Installation into user-global config (no repo, or shared across all repos)

1. Copy `devworks/reference/hooks/pretooluse_guard.py` to e.g.
   `~/.claude/hooks/pretooluse_guard.py` (`chmod +x` it).
2. Copy the contents of `devworks/reference/settings.json` into
   `~/.claude/settings.json`, but change the hook `command` to reference the
   absolute/`$HOME`-relative path you used in step 1 (e.g.
   `python3 "$HOME/.claude/hooks/pretooluse_guard.py"`), since there is no
   `${CLAUDE_PROJECT_DIR}` outside a project.
3. Set `PYTHON_PLAYGROUND_DIR` and any personal allow prefixes directly in
   `~/.claude/settings.json` — user-global config has no separate "local"
   layer. Note the tradeoff the policy doc calls out: user-global settings
   are machine-scoped, not portable or committed anywhere.

## Testing the hook manually

Pipe a synthetic `PreToolUse` payload into the script on stdin, for example:

```
echo '{"tool_name":"Bash","tool_input":{"command":"cat ~/.ssh/id_rsa"}}' \
  | python3 devworks/reference/hooks/pretooluse_guard.py
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

## Mapping to the policy doc's "Done when" checklist

| `policy-req.md` "Done when" item | Satisfied by |
| --- | --- |
| One-off allow rules are gone; remaining allows are prefixes | `settings.json` — every `allow`/`ask`/`deny` entry is a reusable prefix or glob pattern, no URLs/ports/scratch paths/quoted script bodies |
| Python outside the playground is blocked, including `python -c` | `hooks/pretooluse_guard.py` — `check_write_edit` (Write/Edit) and `check_bash_command`'s interpreter/redirect/`tee`/`touch`/copy-verb checks (Bash), both denying `.py`/`.ipynb` outside `PYTHON_PLAYGROUND_DIR` and denying `-c`/stdin/bare-pipe interpreter invocations outright |
| Secret files cannot be read or `cat`'d; other reads do not prompt | `settings.json` deny list (Read/Edit/Write + `Bash(cat ...)` entries) plus the hook's `secrets_match` stage-by-stage scan for compound commands the deny-list prefixes can't see past |
| Playground scripts cannot open host secrets or spawn subprocesses | Documented as a runtime-sandbox concern in the policy doc's "Python isolation" section (host sandbox / OS isolation for the playground process) — outside this hook's static-analysis scope; the hook's secrets deny-list scan is the static-checks half of the "sandbox if available, else static checks + deny list + hooks" fallback posture |
| Normal playground runs, web search, and non-destructive git/`gh` do not prompt | `settings.json` `allow` list (`WebSearch`, `Bash(git *)`, `Bash(gh *)`, baseline read-only prefixes) plus `defaultMode: "acceptEdits"`, with the hook returning no opinion (exit 0, no output) for anything it doesn't flag |
| Policy files still prompt before edit | Not covered by an explicit rule in this reference `settings.json` (no repo-specific policy-file path to name generically) — reference implementers should add an `ask` rule for their own policy file(s), e.g. `Edit(devworks/policy-req.md)`, or rely on the default interactive prompt since these files aren't covered by any `allow` entry above |
