# claude-move

Move or rename a project directory without losing its Claude Code state.

Claude Code stores a project's memory, permissions and session history outside
the project folder, keyed by the project's **absolute path**. Move the folder
with `mv` and none of it follows: the new location starts with empty memory, no
permissions, and no past sessions.

`claude-move` performs the move and updates everything that referred to the old
path.

## Quick start

```bash
git clone https://github.com/NotebookNomad/claude-move.git
cd claude-move

./claude-move.py --list                                  # what does Claude know about?
./claude-move.py ~/dev/api ~/work/api-server --dry-run    # show the plan, change nothing
./claude-move.py ~/dev/api ~/work/api-server              # do it
```

Then pick up where you left off:

```bash
cd ~/work/api-server && claude --continue
```

Renaming is the same operation — `~/dev/api` → `~/dev/backend` works exactly
like a move.

The destination follows `mv`: if it names an **existing directory**, the project
moves *into* it and keeps its own name.

```bash
./claude-move.py ~/dev/api ~/work            # ~/work exists  → ~/work/api
./claude-move.py ~/dev/api ~/work/api-2      # doesn't exist  → renamed to api-2
```

Single file, Python 3.8+, standard library only. Nothing to install.

**Quit any Claude Code session running in the project first.** A live session
holds `~/.claude.json` in memory and writes it back when it exits, which would
undo half the migration. The script checks for this and refuses rather than
letting it happen.

## What moves

| State | Where it lives |
| --- | --- |
| Session transcripts | `~/.claude/projects/<encoded-path>/*.jsonl` |
| Memory files | `~/.claude/projects/<encoded-path>/memory/` |
| Permissions, MCP servers, trust | `~/.claude.json` → `projects["<abs path>"]` |
| Prompt history | `~/.claude/history.jsonl` |
| File backups behind `/rewind` | `~/.claude/file-history/<session-id>/` |
| Background job and session state | `~/.claude/jobs/`, `sessions/`, `session-env/` |

`<encoded-path>` is the absolute path with every non-alphanumeric character
replaced by `-`, so `/Users/me/dev/my_app` becomes `-Users-me-dev-my-app`.

Beyond relocating those directories, the script rewrites references to the old
path wherever they are embedded: the `cwd` recorded in each transcript, commands
and output quoting the old path, memory file contents, `~/` shorthand forms, and
absolute paths inside the project's own `.claude/` settings and hooks. Backup
blobs behind `/rewind` are named from a hash of the file's path, so those get
renamed too and stay resolvable.

Files to update are found by scanning `~/.claude` for the old path, so state
that a future Claude Code version introduces is covered automatically. Projects
nested inside the folder you are moving are remapped as well.

## Options

| Flag | Effect |
| --- | --- |
| `-n`, `--dry-run` | Print the full plan and exit without changing anything |
| `-y`, `--yes` | Skip the confirmation prompt |
| `--state-only` | The folder is already at the new path; just fix Claude's state |
| `--merge` | Combine with state that already exists at the new path |
| `--no-subprojects` | Don't remap projects nested inside the folder |
| `--no-project-settings` | Don't rewrite paths in the project's own `.claude/` files |
| `--no-backup` | Skip the safety copy |
| `--force` | Proceed despite non-fatal blockers, such as a live session |
| `--list` | List known projects and exit |

## Safety

- **Everything it will change is copied first** to
  `~/.claude/claude-move-backups/<timestamp>/`. To undo a migration, restore
  from there.
- **`--dry-run` shows the exact file list** before anything happens.
- **Writes are atomic** — temp file plus rename — so an interruption never
  leaves a truncated `~/.claude.json`.
- **It refuses rather than guesses.** A live session in the project, a
  non-empty destination, or existing state at the new path all stop the run
  with an explanation. `--force` covers the recoverable ones; it will not push
  past a state that would fail partway through.
- **Your files are never rewritten.** Only Claude's own state is edited. The
  backups behind `/rewind` are verbatim copies of your source files, so those
  get renamed but their contents are left alone.

If you have already run Claude at the new path, `--merge` combines the two:
transcripts join, `allowedTools` union, `memory/MEMORY.md` index entries merge,
and any other conflicting file is kept alongside the original as
`name.migrated.ext` and reported so you can reconcile it.

### One caveat: the encoding is lossy

`my_app`, `my app` and `my-app` all encode to `-my-app`, so those projects
*share a single state directory*. If the project you are moving collides with
another this way, the script warns you before doing anything — the other
project's transcripts would move too.

## Compatibility

This tool works against Claude Code's on-disk layout, which is internal and not
a documented API — a future version could rearrange it.

Developed and tested against **Claude Code 2.1.226** on macOS, with Python
3.11. The layout is the same on Linux, though it hasn't been exercised there.

If a future version moves things around, the failure is visible rather than
silent: `--dry-run` prints exactly which files it found and what it will do, so
a plan that looks too small is the signal to check before running it. Nothing is
edited until you confirm, and everything it touches is backed up first.

## Tests

```bash
python3 tests/test_claude_move.py            # runs in a temp dir, cleaned up
python3 tests/test_claude_move.py /tmp/keep  # keep the fixtures to inspect
```

The suite builds a synthetic `~/.claude` mirroring the real layout and checks
the full migration, subprojects, merging, blockers, and `--dry-run` being
read-only. Your real `~/.claude` is never touched.

## License

MIT — see [LICENSE](LICENSE).

---

An independent project, not affiliated with, endorsed by, or supported by
Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic.
