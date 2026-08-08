# claude-move

Move or rename a project directory and carry its Claude Code state with it.

Claude Code keys everything off the project's **absolute path**. `mv` alone
orphans all of it — the new location starts with empty memory, no permissions,
no session history:

| State | Location | Keyed by |
| --- | --- | --- |
| Session transcripts | `~/.claude/projects/<encoded-path>/*.jsonl` | path |
| Memory files | `~/.claude/projects/<encoded-path>/memory/` | path |
| Permissions, MCP servers, trust | `~/.claude.json` → `projects["<abs path>"]` | path |
| Prompt history | `~/.claude/history.jsonl` | path |
| File backups (rewind) | `~/.claude/file-history/<session-id>/` | `sha256(path)[:16]` |
| Background job state | `~/.claude/jobs/<id>/state.json` | path |
| Per-session env | `~/.claude/session-env/<session-id>/` | session id |

`<encoded-path>` is the absolute path with every non-alphanumeric character
replaced by `-`, so `/Users/me/dev/my_app` → `-Users-me-dev-my-app`.

## Usage

```bash
./claude-move.py ~/dev/api ~/work/api-server     # move + rename, state follows
./claude-move.py ~/dev/api ~/work/api --dry-run  # show the plan, change nothing
./claude-move.py ~/dev/api ~/work/api --state-only   # folder already moved by hand
./claude-move.py --list                          # list known projects
```

Renaming is just a move to a different final path — `~/dev/api` → `~/dev/backend`
works the same way.

Stdlib-only Python 3.8+, no dependencies. Run `--dry-run` first; it prints the
full plan and touches nothing.

## What it does

1. Moves the project folder (skipped with `--state-only`, or automatically if
   the folder is already at the destination).
2. Moves `~/.claude/projects/<old>/` → `<new>/`, transcripts and `memory/` intact.
3. Renames the `~/.claude.json` `projects` key, keeping `allowedTools`,
   `mcpServers`, trust flags and stats.
4. Rewrites every embedded reference — transcript `cwd`, tool calls and outputs
   quoting the old path, memory file bodies, `history.jsonl` project tags,
   `~/` shorthand forms, and the encoded directory name where it appears in
   scratchpad paths.

   These files are found by **scanning** `~/.claude` for the old path rather
   than by an allowlist of state files, so directories a future Claude Code
   version adds are covered without a code change. (An allowlist had already
   gone stale: it missed `jobs/<id>/state.json`, which stores a `cwd`.) Skipped
   during the scan: `projects/` (moved explicitly), `file-history/` (blobs are
   verbatim copies of *your* files — only their names encode a path), and this
   tool's own backups. `--dry-run` lists every file it matched, so you can see
   the exact set before anything happens — including Claude's own
   `backups/.claude.json.backup.*`, which are rewritten so that rolling one
   back doesn't resurrect dead paths.
5. Re-hashes `file-history` backup blobs to the new path so `/rewind` still
   resolves them.
6. Remaps **nested subprojects** too (`--no-subprojects` to skip).
7. Rewrites absolute paths inside the project's own `.claude/` settings and
   hooks (`--no-project-settings` to skip).
8. Verifies afterwards and reports anything still pointing at the old path.

## Safety

- **Backup first.** Everything it is about to change is copied to
  `~/.claude/claude-move-backups/<timestamp>/` (`--no-backup` to skip).
- **Live sessions block the move.** A running Claude Code session holds
  `~/.claude.json` in memory and writes it back on exit, undoing the config
  half of the migration. Quit it first, or override with `--force`.
- **`--force` cannot override the impossible.** Blockers that make execution
  fail outright — a missing source, a non-empty destination — are fatal and
  refuse regardless, rather than crashing halfway through.
- **Atomic writes.** `~/.claude.json` and friends are written to a temp file
  and renamed, so a crash never leaves a truncated config.
- **Destination collisions are refused,** not silently merged. If you've already
  run Claude at the new path, re-run with `--merge`: transcripts combine,
  `allowedTools` union, `memory/MEMORY.md` index lines merge, and any other
  conflicting file is kept beside the original as `name.migrated.ext` and
  reported.

### The encoding is lossy

`my_app`, `my app` and `my-app` all encode to `-my-app` and therefore *share one
state directory*. If the project being moved collides with another, the script
warns before doing anything — the other project's transcripts will move too.

## Tests

```bash
python3 tests/test_claude_move.py            # temp dir, cleaned up
python3 tests/test_claude_move.py /tmp/keep  # keep the fixtures to inspect
```

45 assertions over a synthetic `~/.claude` built to mirror the real layout:
the full migration, subprojects, `--merge`, `--dry-run` being read-only, the
live-session and fatal blockers, already-moved detection, `--list`, and a
JSON-escaped non-ASCII path. Your real `~/.claude` is never touched — the tool
is always invoked with `--claude-dir`/`--config` pointed at the fixture.

## After the move

```bash
cd /new/path && claude --continue
```

Memory, permissions and past sessions are all there. To undo, restore from the
printed backup directory.
