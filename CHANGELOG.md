# Changelog

All notable changes to git-repo-doctor are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com), and this
project adheres to [Semantic Versioning](https://semver.org).

## [0.1.0] - 2026-06-03
### Added
- Initial release of `scripts/git_repo_doctor.py`, a zero-dependency, strictly
  read-only diagnostic and safe-cleanup advisor for bloated Git repositories.
- Size breakdown: definite garbage (`tmp_pack_*`/`tmp_obj_*`), loose objects,
  packs, reachable history, and an unreachable estimate.
- Health checks: `gc.auto=0`, stale `gc.log`, worktree equal to `$HOME`,
  Syncthing syncing a `.git` dir, loose-object flood, leftover temp files.
- Decision engine: PATH A (bundle safety net + layered, delayed delete) vs
  PATH B (in-place maintenance + gc/repack), plus giant-blob / `git-filter-repo`
  hints when large objects live in reachable history.
- Rollback-aware cleanup plan generator: prints commands, executes none.
- `--json` output for agent consumption, `--no-remote`, and timeout-guarded
  git calls (safe on huge repos and slow networks).
- Claude Code `SKILL.md` interface, README, and a real-world example report.
