# Example report

A real run against a 101 GiB Git repository that had been accidentally created
with its worktree set to `$HOME`, with `gc.auto=0`, and fed by repeated
interrupted fetches. Paths and the remote URL are anonymized.

```text
========================================================================
  git-repo-doctor v0.1.0  (READ-ONLY diagnosis)
========================================================================
git dir : /Users/you/.git.disabled-codex-crash-20260531-125547
bare    : false    remotes : origin
worktrees: 1
commits : 10    refs : 3    stashes : 0

--- SIZE BREAKDOWN -----------------------------------------------------
  definite garbage (tmp_*)   :    31.46 GiB   (71 files)  -> 100% safe to delete
  loose objects              :    50.26 GiB   (223851 objects)
  packs                      :    19.68 GiB   (11 objects in 6 packs)
  reachable (real history)   :   859.23 MiB   (0.8% of total)
  unreachable (estimate)     :    69.10 GiB   -> reclaimable via prune/repack/delete
  git object store total     :   101.40 GiB

--- DISK ---------------------------------------------------------------
  volume free : 14.78 GiB / 460.43 GiB  (used 97%)
  [!] giant-blob hint: avg packed object is very large (run git-sizer to confirm)

--- HEALTH ISSUES ------------------------------------------------------
  [!] gc.auto=0: Automatic maintenance is fully disabled; garbage accumulates forever.
  [!] 223851 loose objects: Far above auto-gc threshold; never been packed.
  [!] 71 garbage files: Leftover tmp_pack_*/tmp_obj_* from interrupted ops.

--- RECOMMENDATION -----------------------------------------------------
  PATH A (high confidence): Safety-net bundle, then delete (and optionally rebuild)
    Reachable history is tiny (859.23 MiB) vs total (101.40 GiB).
    In-place repack would be wasted work and risky on a 97% full disk.
    Bundle the <1GB of real data, verify it restores, then delete the
    whole dir.

# === SAFE CLEANUP PLAN (read-only advisor; nothing below is auto-run) ===
GITDIR="/Users/you/.git.disabled-codex-crash-20260531-125547"
echo "$GITDIR"   # ALWAYS confirm this variable before any destructive step

## Phase 0 - safety net (BEFORE deleting anything)
mkdir -p ~/repo-doctor-backups
git --git-dir="$GITDIR" remote -v    >  ~/repo-doctor-backups/repo-info.txt
git --git-dir="$GITDIR" branch -a     >> ~/repo-doctor-backups/repo-info.txt
git --git-dir="$GITDIR" config --list >  ~/repo-doctor-backups/repo-config.txt
git --git-dir="$GITDIR" bundle create ~/repo-doctor-backups/repo.bundle --all
# Disk >90% full, or live sync running (Time Machine / Spotlight / Syncthing)?
# Then write the bundle to an EXTERNAL drive. Rule of thumb: keep free > bundle x 5.

## Phase 0.5 - PROVE the bundle restores (do not skip)
git bundle verify ~/repo-doctor-backups/repo.bundle
git bundle list-heads ~/repo-doctor-backups/repo.bundle   # every branch you care about must appear
git clone ~/repo-doctor-backups/repo.bundle /tmp/repo-doctor-restore-test
git -C /tmp/repo-doctor-restore-test log --oneline --all | head
git -C /tmp/repo-doctor-restore-test fsck --full
rm -rf /tmp/repo-doctor-restore-test
# ROLLBACK POINT 0: any failure here => STOP, do not delete; the original is intact.

## Phase 1 - delete PROVEN garbage first (instant, 100% safe, frees disk now)
find "$GITDIR/objects/pack" -name "tmp_pack_*" -delete
find "$GITDIR/objects" -name "tmp_obj_*" -delete
df -h "$GITDIR"   # disk should jump from tight to roomy
# Re-entrant: safe to re-run; already-deleted files are simply skipped.

## Phase 2 - prune the unreachable loose objects (the dangling bulk)
# Review what you would lose first (normally just dead history; you have no stashes):
git --git-dir="$GITDIR" reflog expire --expire=now --all
git --git-dir="$GITDIR" prune --expire=now -v
# ROLLBACK POINT 2: anything pruned is still in repo.bundle. Idempotent: safe to re-run.
# NOTE: prune only touches LOOSE objects. Unreachable objects already inside packs are
# reclaimed by the final delete below (or by 'git repack -ad' if you keep the repo).

## Phase 3 - DELAYED final delete (the safest finish)
# What remains is the core packs. The reachable part is already in your verified bundle,
# and these packs are a second on-disk copy. Keep them a few days as a live safety net,
# then remove the whole dir once you are confident nothing needs recovery:
rm -rf "$GITDIR"
df -h ~
# Prefer it gone immediately? Running this now is fine too - the bundle already covers you.
# ROLLBACK POINT 3: recover anytime via:  git clone ~/repo-doctor-backups/repo.bundle <dir>

## Phase 4 - rebuild when needed (optional)
git clone ~/repo-doctor-backups/repo.bundle ~/repo-recovered
cd ~/repo-recovered && git remote add origin git@github.com:you/your-repo.git
# after auth works:  git push -u origin --all

## Root cause - fix this or the bloat comes back
git --git-dir="$GITDIR" config --unset gc.auto   # stop disabling maintenance (if repo is kept)
git maintenance start   # run inside the KEPT repo: modern background gc/repack/commit-graph
# Do NOT keep a repo whose worktree is $HOME. Use ~/dotfiles + chezmoi/yadm instead.

NOTE: This tool printed commands but executed NONE of them. Review before running.
```
