# Agent permission and sandbox policy

Portable rules for any repo, machine, and coding agent. Map them onto the host's settings, hooks, and permission model. Do not hardcode usernames, home directories, or repo names in committed config.

## Goals

- Minimum approval prompts for routine work
- Hard blocks for secrets, credential stores, and irreversible damage
- All agent-authored Python runs from a designated playground under isolation
- Shared policy is committed; machine-specific paths and personal allows stay local

## Settings layers

- **Shared / committed:** deny lists, hooks, playground convention, default permission mode. No absolute machine paths.
- **Local / gitignored:** personal allow prefixes, playground absolute path, extra working directories.
- **User-global:** optional personal defaults that apply to every repo.

**Baseline safe allow prefixes** (keep these present, not just remove one-off allows): `which`, `type`, `head`, `tail`, `wc`, `less`, `file`, `stat`, `du`, `df`, `tree`, `diff`, `cut`, `tr`, `column`, `uniq`, `sort`, `printf`, `grep`, `strings`, `jq`, `base64`, `yq`, `mkdir`, version probes (`--version`/`-v`), and read-only git verbs.

**Pipe tails:** safe, read-only tail commands (e.g. the baseline prefixes above) should be allow-listed as pipe tails, so a pipeline composed entirely of allowed stages does not prompt. Any un-allowed stage still forces a prompt — prefix-allow rules do not see past `&&` or `|` into unapproved commands.

Deny always wins over allow. Back up local settings before rewriting them. The agent must not edit its own permission/settings files without asking, and must not enable a full permission bypass.

## Permission posture

- Default: auto-accept in-project edits. Never skip the deny list or hook layer.
- Allow rules are reusable prefixes only (`git *`, `uv run *`). Delete any rule that contains a URL, port, scratchpad/tmp path, or quoted script body. Collapse duplicates to the prefix form.
- If the host ignores some allow-rule tool names (for example Write-in-allow), use the tool the host actually honors.
- **Read** is allowed everywhere except the secrets deny list.
- **Web search** is allowed without prompting.
- **Git and `gh`** are allowed without prompting except destructive or irreversible operations (force-push, history rewrite, hard reset, `clean -fdx`, deleting remotes/protected branches, filter-branch). Those must ask or deny.

## Python playground

All agent-authored Python, including one-liners, lives in a single designated playground directory (configured locally; may sit outside the current repo).

- Ban inline interpreters: `-c`, stdin (`python -`), heredocs. Write a `.py` in the playground instead.
- Enforce with project instructions plus a pre-tool hook on file write/edit **and** shell.
- The hook must catch `.py` / `.ipynb` created outside the playground, including via `cat`, `tee`, `cp`, `mv`.
- Grant the agent working-directory access to the playground if it is outside the repo.

## Python isolation

Fail closed: parse or hook errors ask or deny, never allow.

- Run playground Python inside the host sandbox (or equivalent OS isolation): no secret env vars, limited filesystem and network.
- Static import/`open` checks are extra, not sufficient.
- Block host credential dirs, keychain/secret-store CLIs, and subprocess escapes from playground scripts.
- Do not blanket-allow package installs (`pip`, `uv add`, and equivalents).

## Secrets deny list

Apply to read, edit, write, **and** shell (`cat`, `grep`, `less`, and equivalents). Cover home-relative and absolute forms of the same paths. Typical entries:

- `**/.env`, `**/.env.local`, `**/.env.*.local` — narrower than blanket `**/.env.*` because allow-over-deny negation isn't expressible in most host permission models; sample/example/template files fall outside this pattern by construction, not by exemption
- `*secret*`, `*credential*`, `*.pem`, `*.p12`, `*service-account*.json`, `credentials.json`
- `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.netrc`, `~/.npmrc`, `~/.pypirc`, `~/.config/gcloud`, `~/.kube`
- OS secret stores (for example `security find-` / `security dump-` on macOS)

Prefix allow rules do not see the rest of `safe && cat ~/.ssh/id_rsa`. Whole-command hooks must.

## Host mapping (Claude Code)

Use this mapping when the host is Claude Code. Other hosts: same behavior, their native files.

| Policy | Claude Code |
| --- | --- |
| Shared settings | `.claude/settings.json` |
| Local settings | `.claude/settings.local.json` |
| User defaults | `~/.claude/settings.json` |
| Default mode | `acceptEdits` — never `bypassPermissions` |
| Extra dirs | `permissions.additionalDirectories` |
| Pre-tool hook | `PreToolUse` on Write, Edit, Bash |
| Project instructions | `CLAUDE.md` |
| Allow-rule caveat | `Write(...)` in allow is ignored; use `Edit(...)` |
| Full bypass | deny `--dangerously-skip-permissions` |
| Runtime isolation | `sandbox` for playground Python |

## Done when

- One-off allow rules are gone; remaining allows are prefixes
- Python outside the playground is blocked, including `python -c`
- Secret files cannot be read or `cat`'d; other reads do not prompt
- Playground scripts cannot open host secrets or spawn subprocesses
- Normal playground runs, web search, and non-destructive git/`gh` do not prompt
- Policy files still prompt before edit
