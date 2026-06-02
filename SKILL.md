---
name: git-repo-doctor
description: Diagnose a bloated or corrupted Git repository and produce a safe, rollback-aware cleanup plan. Use when a .git directory is huge (many GB), full of objects/pack/tmp_pack_* or tmp_obj_* files, or holding hundreds of thousands of loose objects; when git gc/repack/fetch was interrupted (OOM, crash, timeout); when git count-objects reports "garbage"; when an editor or coding agent crashes while scanning a repo; or when the user asks how to safely shrink, clean, prune, or reclaim disk space from a Git repository. The tool is strictly read-only: it inspects and prints a plan but never runs destructive commands.
---

# git-repo-doctor

Read-only diagnosis plus safe-cleanup advisor for bloated Git repositories.

## When to use this skill
- A `.git` directory has grown to many gigabytes.
- `objects/pack/tmp_pack_*` or `objects/??/tmp_obj_*` files are piling up.
- `git count-objects -v` shows a large `garbage` count or hundreds of thousands of loose objects.
- `gc.auto=0`, or gc / repack / fetch keeps getting interrupted.
- An editor or coding agent (Codex, Claude Code, Cursor, an IDE indexer) crashes or OOMs while scanning a repository.
- The user asks to safely shrink, clean, prune, garbage-collect, or reclaim space from a repo.

## How to run
Single zero-dependency Python 3 script. It is strictly READ-ONLY.

```bash
# Diagnose the repo containing the current directory:
python3 scripts/git_repo_doctor.py

# Point at a specific git dir, including a renamed / disabled one:
python3 scripts/git_repo_doctor.py --git-dir /path/to/.git.disabled-xyz

# Machine-readable output (parse this when acting as an agent):
python3 scripts/git_repo_doctor.py --json

# Skip the remote reachability probe (offline or slow network):
python3 scripts/git_repo_doctor.py --no-remote
```

## What it returns
1. **Size breakdown** - definite garbage / loose / packs / reachable history / unreachable estimate / disk headroom.
2. **Health issues** - `gc.auto=0`, stale `gc.log`, worktree equal to `$HOME`, Syncthing syncing `.git`, loose-object flood, leftover temp files.
3. **A recommendation**, one of:
   - **PATH A** (tiny reachable history): bundle safety net, prove it restores, then a layered, delayed delete.
   - **PATH B** (substantial history): in-place `git maintenance` + `gc`/`repack`; if disk is tight, delete garbage first.
   - **filter-repo / BFG hint** when giant blobs live in reachable history.
4. **A ready-to-paste, rollback-aware cleanup plan** with explicit rollback points. The script prints the commands and executes none of them.

## How the agent should use the output
- Run the script, then summarize the size breakdown, health issues, and the recommended PATH for the user.
- Present the printed plan but DO NOT execute any command from it automatically. Cleanup is irreversible; the human must confirm each destructive step.
- Always confirm the `$GITDIR` value, ensure the bundle is created and verified, and keep the bundle until the user is sure.

## Safety contract
This tool never runs `gc`, `prune`, `repack`, `rm`, `reflog expire`, or any mutating
command. The only things it executes are read-only `git` queries and filesystem
stats, all timeout-guarded. Every destructive action exists solely as text in the
printed plan for a human to review and run.

## Decision logic (summary)
| Condition | Recommendation |
|---|---|
| reachable < 1 GiB and < 10% of total | PATH A: bundle + layered delete / rebuild |
| reachable substantial, free space ample | PATH B: `git maintenance` + `gc`/`repack` in place |
| reachable substantial, disk tight | delete garbage first, then repack |
| few packed objects but huge pack size | giant-blob hint (run `git-sizer`, consider `git-filter-repo`) |

See `README.md` for full documentation and an example report.
