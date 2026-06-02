# git-repo-doctor

> Read-only diagnosis and a safe, rollback-aware cleanup plan for bloated Git repositories.

When a `.git` directory balloons to tens of gigabytes, stuffed with `tmp_pack_*`
leftovers, hundreds of thousands of loose objects, or a handful of giant blobs, the
scary part is not running `git gc`. It is deciding **what is safe to delete** without
losing real history, especially when the disk is nearly full and one wrong `rm -rf`
is irreversible.

`git-repo-doctor` answers that. It inspects the repo **read-only**, tells you exactly
how much space is garbage vs. reclaimable vs. real history, and prints a
**rollback-aware cleanup plan** tailored to your situation. It never runs a
destructive command itself.

```text
--- SIZE BREAKDOWN -----------------------------------------------------
  definite garbage (tmp_*)   :    31.46 GiB   (71 files)  -> 100% safe to delete
  loose objects              :    50.26 GiB   (223851 objects)
  packs                      :    19.68 GiB   (11 objects in 6 packs)
  reachable (real history)   :   859.23 MiB   (0.8% of total)
  unreachable (estimate)     :    69.10 GiB   -> reclaimable via prune/repack/delete
  git object store total     :   101.40 GiB
...
  PATH A (high confidence): Safety-net bundle, then delete (and optionally rebuild)
```

See [`examples/sample-report.md`](examples/sample-report.md) for the full output of a
real 101 GiB incident.

## Why this exists

Git repo bloat is a well-known problem with mature low-level tools: `git gc`,
`git prune`, `git repack`, `git maintenance`, `git-sizer`, `git-filter-repo`, BFG.
What is missing is the **diagnosis and decision layer**: given one specific bloated
repo, which tool do you reach for, in what order, and how do you avoid losing data?
People usually hand-roll this or guess on StackOverflow. This script encodes that
judgment into a single read-only command.

It is especially useful in the AI-agent era, where coding agents and IDE indexers can
create or bloat repos in surprising places (for example a repo whose worktree is your
entire `$HOME`, with `gc.auto=0`) and then crash while scanning them.

## Features

- **Read-only and safe.** Runs only `git` queries and filesystem stats. Prints every destructive command as text and executes none.
- **Clear size breakdown.** Definite garbage, loose objects, packs, reachable history, an unreachable estimate, and disk headroom.
- **Health checks.** `gc.auto=0`, stale `gc.log`, worktree equal to `$HOME`, Syncthing syncing a `.git`, loose-object flood, leftover temp files.
- **Decision engine.** Picks PATH A (bundle + layered delete) or PATH B (in-place gc/repack), and flags giant blobs that need `git-filter-repo` or BFG.
- **Rollback-aware plan.** Bundle safety net, gold-standard restore verification, layered and delayed deletion, explicit rollback points at every step.
- **Agent-friendly.** `--json` for machine consumption, and ships as a Claude Code skill via `SKILL.md`.
- **Zero dependencies.** Pure Python 3 standard library plus the `git` CLI. Works on macOS and Linux.

## Install

```bash
git clone https://github.com/LaughingisLaughing/git-repo-doctor.git
python3 git-repo-doctor/scripts/git_repo_doctor.py --help
```

Optional, put it on your PATH:

```bash
ln -s "$PWD/git-repo-doctor/scripts/git_repo_doctor.py" /usr/local/bin/git-repo-doctor
```

### As a Claude Code / Agent skill

Symlink the repo into your skills directory so the agent can discover it:

```bash
ln -s "$PWD/git-repo-doctor" ~/.claude/skills/git-repo-doctor
```

The agent reads `SKILL.md` and runs the script when it detects a bloated repo.

## Usage

```bash
# Diagnose the repo containing the current directory
python3 scripts/git_repo_doctor.py

# Point at a specific or renamed / disabled .git directory
python3 scripts/git_repo_doctor.py --git-dir /path/to/.git.disabled-xyz

# Machine-readable output for tooling / agents
python3 scripts/git_repo_doctor.py --json

# Skip the (network) remote reachability probe
python3 scripts/git_repo_doctor.py --no-remote
```

## How it decides

| Condition | Recommendation |
|---|---|
| reachable < 1 GiB and < 10% of total | **PATH A**: bundle safety net, then layered delete / rebuild |
| reachable substantial, free space ample | **PATH B**: `git maintenance` + `gc` / `repack` in place |
| reachable substantial, disk tight | delete garbage first to free headroom, then repack |
| few packed objects but a huge pack | giant-blob hint: run `git-sizer`, consider `git-filter-repo` |

## The safe cleanup model

The generated plan favors **layered, reversible** steps over one irreversible
`rm -rf`:

1. **Safety net first.** Bundle all reachable refs (usually under 1 GiB even for a
   100 GiB repo), then *prove* it restores with `git bundle verify`, a trial
   `git clone`, and `git fsck --full`. If the disk is over 90% full or a live sync
   service is running, write the bundle to an external drive.
2. **Delete proven garbage.** `tmp_pack_*` and `tmp_obj_*` are interrupted-operation
   leftovers and are always safe to remove. This frees space immediately and is
   re-entrant.
3. **Prune unreachable loose objects.** The dangling bulk. Everything pruned is still
   in the verified bundle, and the step is idempotent.
4. **Delayed final delete.** What remains is the core packs, a second on-disk copy of
   data you already bundled. Keep them a few days as a live safety net, then remove
   the directory once you are confident.
5. **Fix the root cause.** Re-enable maintenance (`git maintenance start`), and stop
   whatever created the bloat (a `$HOME` worktree, `gc.auto=0`, or Syncthing on a
   `.git`).

Every phase prints a rollback point. Nothing runs automatically.

## Safety contract

This tool never runs `gc`, `prune`, `repack`, `rm`, `reflog expire`, or any mutating
command. The only things it executes are read-only `git` queries and filesystem
stats, all timeout-guarded so they stay responsive even on a 100 GiB repo. Every
destructive action exists solely as text in the printed plan for a human to review.

## Limitations and roadmap

- The unreachable figure is an order-of-magnitude estimate (loose objects are
  uncompressed, reachable size is the packed view). It is meant for decisions, not
  accounting.
- The giant-blob check is a heuristic hint; it does not run `git-sizer` for you.
- Roadmap: optional `git-sizer` integration, a multi-repo scan mode to catch bloat
  early across many projects, and a `--plan-only` mode.

## Contributing

Issues and pull requests are welcome. The script is intentionally a single
dependency-free file so it stays easy to audit and drop into any environment. Please
keep it read-only by default: destructive operations should only ever be emitted as
text in the plan.

## Acknowledgements

The decision model was distilled from a multi-model investigation (web research plus
Grok, GPT, and DeepSeek) of a real 101 GiB home-directory repository that crashed a
coding agent during scanning.

## License

MIT. See [LICENSE](LICENSE).
