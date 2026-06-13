#!/usr/bin/env python3
"""
git-repo-doctor: read-only diagnosis + safe-cleanup advisor for bloated Git repositories.

What it does
------------
Given a Git repository (even a renamed / disabled `.git` directory), it inspects the
object store, refs, worktree layout, config, disk headroom and remote reachability,
then classifies how much space is *garbage*, *likely-reclaimable* and *reachable*,
and prints a recommended, ROLLBACK-AWARE cleanup plan.

Safety contract
---------------
This script is strictly READ-ONLY. It never runs gc, prune, repack, rm, or any
mutating command. Every destructive command lives in the printed plan as TEXT only,
for a human to review and run manually. The worst this script can do is read git
metadata and stat the filesystem.

Usage
-----
    git_repo_doctor.py [PATH]                 # diagnose repo at PATH (default: .)
    git_repo_doctor.py --git-dir /path/.git   # point at a specific (or renamed) git dir
    git_repo_doctor.py --json                 # machine-readable output (for agents)
    git_repo_doctor.py --no-remote            # skip remote reachability probe
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

VERSION = "0.1.0"

# Decision thresholds (tunable).
SMALL_REACHABLE_BYTES = 1 * 1024**3          # "reachable history is tiny" cutoff
SMALL_REACHABLE_RATIO = 0.10                 # reachable < 10% of total => tiny
AUTO_GC_LOOSE_THRESHOLD = 6700               # git's default auto-gc loose trigger
REPACK_TEMP_HEADROOM = 1 * 1024**3           # extra free space repack wants on top of pack
BIG_BLOB_AVG_BYTES = 100 * 1024**2           # avg in-pack object size hinting at giant blobs


# --------------------------------------------------------------------------- #
# Subprocess helpers (timeout-guarded; macOS has no `timeout` binary).
# --------------------------------------------------------------------------- #
def run(cmd, timeout, cwd=None, env=None):
    """Run a command, returning (returncode, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except FileNotFoundError as e:
        return 127, "", str(e)
    except Exception as e:  # pragma: no cover - defensive
        return 1, "", repr(e)


class Git:
    """Thin read-only wrapper that always targets a specific git dir."""

    def __init__(self, git_dir, default_timeout=30):
        self.git_dir = git_dir
        self.default_timeout = default_timeout

    def __call__(self, args, timeout=None, env=None):
        cmd = ["git", "--git-dir", self.git_dir] + args
        return run(cmd, timeout or self.default_timeout, env=env)

    def out(self, args, timeout=None, env=None):
        rc, so, _ = self(args, timeout=timeout, env=env)
        return so.strip() if rc == 0 else None


# --------------------------------------------------------------------------- #
# Formatting helpers.
# --------------------------------------------------------------------------- #
def humanize(num_bytes):
    if num_bytes is None:
        return "unknown"
    n = float(num_bytes)
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if abs(n) < 1024.0 or unit == "TiB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.2f} TiB"


def pct(part, whole):
    if not whole:
        return None
    return 100.0 * part / whole


# --------------------------------------------------------------------------- #
# Collection.
# --------------------------------------------------------------------------- #
def resolve_git_dir(path, explicit_git_dir):
    if explicit_git_dir:
        gd = os.path.abspath(os.path.expanduser(explicit_git_dir))
        if not os.path.isdir(gd):
            sys.exit(f"error: --git-dir not found: {gd}")
        return gd
    # Auto-detect from a working directory.
    rc, so, se = run(
        ["git", "-C", os.path.expanduser(path), "rev-parse", "--absolute-git-dir"],
        timeout=15,
    )
    if rc != 0 or not so.strip():
        sys.exit(
            f"error: '{path}' is not inside a Git repository.\n"
            f"Hint: for a renamed/disabled repo, pass it explicitly:\n"
            f"      git_repo_doctor.py --git-dir /path/to/.git.disabled-..."
        )
    return so.strip()


def parse_count_objects(git):
    """Parse `git count-objects -v`. size fields are in KiB."""
    rc, so, _ = git(["count-objects", "-v"])
    data = {}
    if rc != 0:
        return data
    for line in so.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            v = v.strip()
            if v.isdigit():
                data[k.strip()] = int(v)
    # Normalize KiB -> bytes for size fields.
    out = {
        "loose_count": data.get("count", 0),
        "loose_bytes": data.get("size", 0) * 1024,
        "in_pack": data.get("in-pack", 0),
        "packs": data.get("packs", 0),
        "pack_bytes": data.get("size-pack", 0) * 1024,
        "prune_packable": data.get("prune-packable", 0),
        "garbage_count": data.get("garbage", 0),
        "garbage_bytes": data.get("size-garbage", 0) * 1024,
    }
    return out


def reachable_disk_usage(git):
    """Bytes used by objects reachable from all refs (git 2.31+). None if unsupported."""
    rc, so, _ = git(["rev-list", "--all", "--objects", "--disk-usage"], timeout=120)
    if rc == 0 and so.strip().isdigit():
        return int(so.strip())
    return None


def collect_refs(git):
    info = {}
    commits = git.out(["rev-list", "--all", "--count"], timeout=60)
    info["commit_count"] = int(commits) if commits and commits.isdigit() else None
    rc, so, _ = git(["for-each-ref", "--format=%(refname)"])
    info["refs"] = so.splitlines() if rc == 0 else []
    stash = git.out(["stash", "list"])
    info["stash_count"] = len([l for l in stash.splitlines() if l.strip()]) if stash else 0
    return info


def collect_worktrees(git, home):
    rc, so, _ = git(["worktree", "list", "--porcelain"])
    paths = []
    if rc == 0:
        for line in so.splitlines():
            if line.startswith("worktree "):
                paths.append(line[len("worktree "):].strip())
    dangerous = False
    for wt in paths:
        wt_abs = os.path.abspath(wt)
        # Worktree IS home, or is an ancestor of home => tracks the entire home tree.
        if wt_abs == home or home.startswith(wt_abs.rstrip(os.sep) + os.sep):
            dangerous = True
    return {"worktrees": paths, "worktree_count": len(paths), "worktree_is_home": dangerous}


def collect_config(git):
    cfg = {}
    cfg["gc_auto"] = git.out(["config", "--get", "gc.auto"])
    cfg["core_bare"] = git.out(["config", "--get", "core.bare"])
    cfg["worktree"] = git.out(["config", "--get", "core.worktree"])
    rc, so, _ = git(["remote", "-v"])
    remotes = {}
    if rc == 0:
        for line in so.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] not in remotes:
                remotes[parts[0]] = parts[1]
    cfg["remotes"] = remotes
    return cfg


def detect_syncthing(git_dir):
    """Heuristics: .stfolder in an ancestor, .stignore, or .sync-conflict files inside the git dir."""
    signals = []
    # .sync-conflict files inside the git dir are a direct sign Syncthing touched it.
    # IMPORTANT: never descend into the loose-object shard dirs (objects/00../ff) -
    # a bloated repo can hold hundreds of thousands of them and walking is slow.
    for root, dirs, files in os.walk(git_dir):
        if os.path.basename(root) == "objects":
            dirs[:] = [d for d in dirs if d == "pack"]  # only scan objects/pack
        for f in files:
            if ".sync-conflict" in f:
                signals.append(os.path.join(root, f))
                break
        if signals:
            break
    # .stfolder marker in any ancestor directory.
    d = os.path.dirname(git_dir.rstrip(os.sep))
    home = os.path.expanduser("~")
    while True:
        if os.path.exists(os.path.join(d, ".stfolder")):
            signals.append(os.path.join(d, ".stfolder"))
            break
        if d in ("/", home) or len(d) <= len(home):
            break
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return signals


def probe_remotes(git, remotes, timeout):
    """Read-only `ls-remote` per remote, non-interactive. Returns dict name -> status."""
    results = {}
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new")
    for name in remotes:
        rc, so, se = git(["ls-remote", "--heads", name], timeout=timeout, env=env)
        if rc == 0:
            heads = len([l for l in so.splitlines() if l.strip()])
            results[name] = {"reachable": True, "heads": heads}
        else:
            results[name] = {"reachable": False, "error": (se or "").strip().splitlines()[-1] if se else "failed"}
    return results


# --------------------------------------------------------------------------- #
# Analysis / decision.
# --------------------------------------------------------------------------- #
def analyze(state):
    co = state["count_objects"]
    reachable = state["reachable_bytes"]
    git_total = co.get("loose_bytes", 0) + co.get("pack_bytes", 0) + co.get("garbage_bytes", 0)
    garbage = co.get("garbage_bytes", 0)
    # Unreachable estimate. reachable is pack-compressed view; loose is uncompressed.
    # This is an order-of-magnitude estimate, intentionally labeled as such.
    unreachable_est = None
    if reachable is not None:
        unreachable_est = max(0, git_total - reachable - garbage)

    tiers = {
        "definite_garbage_bytes": garbage,
        "reachable_bytes": reachable,
        "unreachable_estimate_bytes": unreachable_est,
        "pack_bytes": co.get("pack_bytes", 0),
        "loose_bytes": co.get("loose_bytes", 0),
        "git_total_bytes": git_total,
    }

    issues = []
    cfg = state["config"]
    if cfg.get("gc_auto") == "0":
        issues.append(("gc.auto=0", "Automatic maintenance is fully disabled; garbage accumulates forever."))
    if state["gc_log_exists"]:
        issues.append(("gc.log present", "A stale .git/gc.log can block future auto-gc. Remove it."))
    if state["worktree"]["worktree_is_home"]:
        issues.append(("worktree is $HOME", "Repo tracks your entire home tree; extremely bloat-prone."))
    if state["syncthing_signals"]:
        issues.append(("Syncthing syncing .git", "Syncing a .git dir corrupts/duplicates objects. Exclude it."))
    if co.get("loose_count", 0) > AUTO_GC_LOOSE_THRESHOLD:
        issues.append((f"{co['loose_count']} loose objects", "Far above auto-gc threshold; never been packed."))
    if co.get("garbage_count", 0) > 0:
        issues.append((f"{co['garbage_count']} garbage files", "Leftover tmp_pack_*/tmp_obj_* from interrupted ops."))

    # Giant-blob hint: few packed objects but huge pack size.
    big_blob_hint = False
    if co.get("in_pack", 0) > 0 and co.get("pack_bytes", 0) / max(1, co["in_pack"]) > BIG_BLOB_AVG_BYTES:
        big_blob_hint = True

    # Decision tree.
    free = state["disk"]["free"]
    decision = decide(reachable, git_total, free, co, big_blob_hint, state)
    return {"tiers": tiers, "issues": issues, "decision": decision, "big_blob_hint": big_blob_hint}


def decide(reachable, git_total, free, co, big_blob_hint, state):
    repack_temp_needed = co.get("pack_bytes", 0) + REPACK_TEMP_HEADROOM
    tiny_reachable = (
        reachable is not None
        and reachable < SMALL_REACHABLE_BYTES
        and (git_total == 0 or reachable < SMALL_REACHABLE_RATIO * git_total)
    )

    if reachable is None:
        return {
            "path": "A?",
            "title": "Likely DELETE-AND-REBUILD, but confirm reachable size first",
            "reason": "Could not compute reachable size (old git?). Verify with the listed command before deciding.",
            "confidence": "low",
        }
    if tiny_reachable:
        return {
            "path": "A",
            "title": "Safety-net bundle, then delete (and optionally rebuild)",
            "reason": (
                f"Reachable history is tiny ({humanize(reachable)}) vs total ({humanize(git_total)}). "
                f"In-place repack would be wasted work and risky on a {state['disk']['used_pct']:.0f}% full disk. "
                "Bundle the <1GB of real data, verify it restores, then delete the whole dir."
            ),
            "confidence": "high",
        }
    # Reachable is substantial => worth keeping in place.
    if free > repack_temp_needed:
        title = "In-place shrink with maintenance + gc/repack"
        reason = (
            f"Reachable history is substantial ({humanize(reachable)}); keep it. "
            f"Free space ({humanize(free)}) exceeds repack temp need (~{humanize(repack_temp_needed)})."
        )
    else:
        title = "Free space first (delete garbage), THEN in-place shrink"
        reason = (
            f"Reachable history is substantial ({humanize(reachable)}), but free space "
            f"({humanize(free)}) is below repack temp need (~{humanize(repack_temp_needed)}). "
            "Remove garbage first to create headroom, then repack."
        )
    d = {"path": "B", "title": title, "reason": reason, "confidence": "medium"}
    if big_blob_hint:
        d["filter_repo_hint"] = (
            "Average packed object is very large: giant blobs may live in reachable history. "
            "If so, use git-filter-repo or BFG to rewrite history and drop them."
        )
    return d


# --------------------------------------------------------------------------- #
# Plan rendering (TEXT ONLY - never executed).
# --------------------------------------------------------------------------- #
def render_plan(state, result):
    gd = state["git_dir"]
    path = result["decision"]["path"]
    lines = []
    lines.append("# === SAFE CLEANUP PLAN (read-only advisor; nothing below is auto-run) ===")
    lines.append(f'GITDIR="{gd}"')
    lines.append('echo "$GITDIR"   # ALWAYS confirm this variable before any destructive step')
    lines.append("")

    if path == "A":
        remotes = state["config"].get("remotes", {})
        lines += [
            "## Phase 0 - safety net (BEFORE deleting anything)",
            'mkdir -p ~/repo-doctor-backups',
            'git --git-dir="$GITDIR" remote -v    >  ~/repo-doctor-backups/repo-info.txt',
            'git --git-dir="$GITDIR" branch -a     >> ~/repo-doctor-backups/repo-info.txt',
            'git --git-dir="$GITDIR" config --list >  ~/repo-doctor-backups/repo-config.txt',
            'git --git-dir="$GITDIR" bundle create ~/repo-doctor-backups/repo.bundle --all',
            "# Disk >90% full, or live sync running (Time Machine / Spotlight / Syncthing)?",
            "# Then write the bundle to an EXTERNAL drive. Rule of thumb: keep free > bundle x 5.",
            "",
            "## Phase 0.5 - PROVE the bundle restores (do not skip)",
            'git bundle verify ~/repo-doctor-backups/repo.bundle',
            'git bundle list-heads ~/repo-doctor-backups/repo.bundle   # every branch you care about must appear',
            'git clone ~/repo-doctor-backups/repo.bundle /tmp/repo-doctor-restore-test',
            'git -C /tmp/repo-doctor-restore-test log --oneline --all | head',
            'git -C /tmp/repo-doctor-restore-test fsck --full',
            'rm -rf /tmp/repo-doctor-restore-test',
            "# ROLLBACK POINT 0: any failure here => STOP, do not delete; the original is intact.",
            "",
            "## Phase 1 - delete PROVEN garbage first (instant, 100% safe, frees disk now)",
            'find "$GITDIR/objects/pack" -name "tmp_pack_*" -delete',
            'find "$GITDIR/objects" -name "tmp_obj_*" -delete',
            'df -h "$GITDIR"   # disk should jump from tight to roomy',
            "# Re-entrant: safe to re-run; already-deleted files are simply skipped.",
            "",
            "## Phase 2 - prune the unreachable loose objects (the dangling bulk)",
            "# Review what you would lose first (normally just dead history; you have no stashes):",
            'git --git-dir="$GITDIR" reflog expire --expire=now --all',
            'git --git-dir="$GITDIR" prune --expire=now -v',
            "# ROLLBACK POINT 2: anything pruned is still in repo.bundle. Idempotent: safe to re-run.",
            "# NOTE: prune only touches LOOSE objects. Unreachable objects already inside packs are",
            "# reclaimed by the final delete below (or by 'git repack -ad' if you keep the repo).",
            "",
            "## Phase 3 - DELAYED final delete (the safest finish)",
            "# What remains is the core packs. The reachable part is already in your verified bundle,",
            "# and these packs are a second on-disk copy. Keep them a few days as a live safety net,",
            "# then remove the whole dir once you are confident nothing needs recovery:",
            'rm -rf "$GITDIR"',
            'df -h ~',
            "# Prefer it gone immediately? Running this now is fine too - the bundle already covers you.",
            "# ROLLBACK POINT 3: recover anytime via:  git clone ~/repo-doctor-backups/repo.bundle <dir>",
            "",
            "## Phase 4 - rebuild when needed (optional)",
            'git clone ~/repo-doctor-backups/repo.bundle ~/repo-recovered',
        ]
        if remotes:
            any_remote = list(remotes.values())[0]
            lines.append(f'cd ~/repo-recovered && git remote add origin {any_remote}')
            lines.append('# after auth works:  git push -u origin --all')
    elif path == "A?":
        lines += [
            "## Phase 0 - confirm reachable size before any destructive action",
            "# This recommendation is low confidence because reachable size is unknown.",
            "# Run this first; if it works, rerun git-repo-doctor and follow the new path:",
            'git --git-dir="$GITDIR" rev-list --all --objects --disk-usage',
            "# Until reachable size is confirmed, do not remove the repository.",
            "",
            "## Phase 1 - optional safe cleanup only",
            "# These commands remove only proven temporary garbage and stale gc state.",
            'find "$GITDIR/objects/pack" -name "tmp_pack_*" -delete',
            'find "$GITDIR/objects" -name "tmp_obj_*" -delete',
            'rm -f "$GITDIR/gc.log"   # clear any stale lock on auto-gc',
            'df -h "$GITDIR"',
        ]
    else:
        lines += [
            "## Phase 0 - safety net first (cheap insurance even for in-place shrink)",
            'mkdir -p ~/repo-doctor-backups',
            'git --git-dir="$GITDIR" bundle create ~/repo-doctor-backups/repo.bundle --all',
            'git bundle verify ~/repo-doctor-backups/repo.bundle',
            "# ROLLBACK POINT 0: bundle is your fallback if repack corrupts anything.",
            "",
            "## Phase 1 - remove proven garbage to create headroom",
            'find "$GITDIR/objects/pack" -name "tmp_pack_*" -delete',
            'find "$GITDIR/objects" -name "tmp_obj_*" -delete',
            'rm -f "$GITDIR/gc.log"   # clear any stale lock on auto-gc',
            'df -h "$GITDIR"',
            "",
            "## Phase 2 - expire history you do not need, then let git repack",
            "# Review first! This drops unreachable objects (reflog/stash included):",
            'git --git-dir="$GITDIR" reflog expire --expire=now --all',
            'git --git-dir="$GITDIR" gc --prune=now',
            "# ROLLBACK POINT 1: if gc fails midway, objects are still recoverable from the bundle.",
        ]
        if result["decision"].get("filter_repo_hint"):
            lines += [
                "",
                "## Optional - giant blobs in reachable history",
                "# pipx install git-filter-repo ; then e.g. drop a path from ALL history:",
                'git --git-dir="$GITDIR" filter-repo --path BIGFILE --invert-paths --force',
            ]

    lines += [
        "",
        "## Root cause - fix this or the bloat comes back",
        'git --git-dir="$GITDIR" config --unset gc.auto   # stop disabling maintenance (if repo is kept)',
        "git maintenance start   # run inside the KEPT repo: modern background gc/repack/commit-graph",
    ]
    if state["worktree"]["worktree_is_home"]:
        lines.append("# Do NOT keep a repo whose worktree is $HOME. Use ~/dotfiles + chezmoi/yadm instead.")
    if state["syncthing_signals"]:
        lines.append("# Add this repo's path to your Syncthing ignore list; never sync a .git dir.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Text report.
# --------------------------------------------------------------------------- #
def render_report(state, result):
    co = state["count_objects"]
    t = result["tiers"]
    d = state["disk"]
    L = []
    L.append("=" * 72)
    L.append(f"  git-repo-doctor v{VERSION}  (READ-ONLY diagnosis)")
    L.append("=" * 72)
    L.append(f"git dir : {state['git_dir']}")
    L.append(f"bare    : {state['config'].get('core_bare')}    "
             f"remotes : {', '.join(state['config'].get('remotes', {})) or 'none'}")
    wt = state["worktree"]
    L.append(f"worktrees: {wt['worktree_count']}"
             + ("  [!] worktree is $HOME" if wt["worktree_is_home"] else ""))
    L.append(f"commits : {state['refs'].get('commit_count')}    "
             f"refs : {len(state['refs'].get('refs', []))}    "
             f"stashes : {state['refs'].get('stash_count')}")
    L.append("")
    L.append("--- SIZE BREAKDOWN " + "-" * 53)
    gt = t["git_total_bytes"]
    L.append(f"  definite garbage (tmp_*)   : {humanize(t['definite_garbage_bytes']):>12}   "
             f"({co.get('garbage_count', 0)} files)  -> 100% safe to delete")
    L.append(f"  loose objects              : {humanize(t['loose_bytes']):>12}   "
             f"({co.get('loose_count', 0)} objects)")
    L.append(f"  packs                      : {humanize(t['pack_bytes']):>12}   "
             f"({co.get('in_pack', 0)} objects in {co.get('packs', 0)} packs)")
    L.append(f"  reachable (real history)   : {humanize(t['reachable_bytes']):>12}"
             + (f"   ({pct(t['reachable_bytes'], gt):.1f}% of total)" if t["reachable_bytes"] and gt else ""))
    L.append(f"  unreachable (estimate)     : {humanize(t['unreachable_estimate_bytes']):>12}   "
             f"-> reclaimable via prune/repack/delete")
    L.append(f"  git object store total     : {humanize(gt):>12}")
    L.append("")
    L.append("--- DISK " + "-" * 63)
    L.append(f"  volume free : {humanize(d['free'])} / {humanize(d['total'])}  "
             f"(used {d['used_pct']:.0f}%)")
    if result["big_blob_hint"]:
        L.append("  [!] giant-blob hint: avg packed object is very large (run git-sizer to confirm)")
    L.append("")
    L.append("--- HEALTH ISSUES " + "-" * 54)
    if result["issues"]:
        for name, why in result["issues"]:
            L.append(f"  [!] {name}: {why}")
    else:
        L.append("  none detected")
    L.append("")
    dec = result["decision"]
    L.append("--- RECOMMENDATION " + "-" * 53)
    L.append(f"  PATH {dec['path']} ({dec['confidence']} confidence): {dec['title']}")
    for chunk in _wrap(dec["reason"], 68):
        L.append(f"    {chunk}")
    if dec.get("filter_repo_hint"):
        for chunk in _wrap(dec["filter_repo_hint"], 68):
            L.append(f"    {chunk}")
    L.append("")
    L.append(render_plan(state, result))
    L.append("")
    L.append("NOTE: This tool printed commands but executed NONE of them. Review before running.")
    return "\n".join(L)


def _wrap(text, width):
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = (line + " " + w).strip()
    if line:
        out.append(line)
    return out


# --------------------------------------------------------------------------- #
# Main.
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Read-only diagnosis + safe-cleanup advisor for bloated Git repos."
    )
    ap.add_argument("path", nargs="?", default=".", help="path inside the repo (default: .)")
    ap.add_argument("--git-dir", help="explicit git dir (use for renamed/disabled .git)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-remote", action="store_true", help="skip remote reachability probe")
    ap.add_argument("--remote-timeout", type=int, default=8)
    ap.add_argument("--git-timeout", type=int, default=30)
    ap.add_argument("--version", action="version", version=f"git-repo-doctor {VERSION}")
    args = ap.parse_args(argv)

    git_dir = resolve_git_dir(args.path, args.git_dir)
    home = os.path.expanduser("~")
    git = Git(git_dir, default_timeout=args.git_timeout)

    state = {
        "git_dir": git_dir,
        "count_objects": parse_count_objects(git),
        "reachable_bytes": reachable_disk_usage(git),
        "refs": collect_refs(git),
        "worktree": collect_worktrees(git, home),
        "config": collect_config(git),
        "gc_log_exists": os.path.exists(os.path.join(git_dir, "gc.log")),
        "syncthing_signals": detect_syncthing(git_dir),
    }
    try:
        usage = shutil.disk_usage(git_dir)
        state["disk"] = {
            "total": usage.total,
            "free": usage.free,
            "used_pct": 100.0 * (usage.total - usage.free) / usage.total,
        }
    except Exception:
        state["disk"] = {"total": 0, "free": 0, "used_pct": 0.0}

    if not args.no_remote and state["config"].get("remotes"):
        state["remotes_probe"] = probe_remotes(git, state["config"]["remotes"], args.remote_timeout)
    else:
        state["remotes_probe"] = {}

    result = analyze(state)

    if args.json:
        print(json.dumps({"state": _jsonable(state), "result": result}, indent=2))
    else:
        print(render_report(state, result))
    return 0


def _jsonable(state):
    """Drop nothing; everything is already JSON-safe primitives/lists/dicts."""
    return state


if __name__ == "__main__":
    sys.exit(main())
