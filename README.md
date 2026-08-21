# claude-move

Move a project without losing its Claude Code state — across your disk, or
across machines.

Claude Code stores a project's memory, permissions and session history outside
the project folder, keyed by the project's **absolute path**. Move the folder
with `mv` and none of it follows: the new location starts with empty memory, no
permissions, and no past sessions.

`claude-move` handles both versions of that problem.

| Command | What it does |
| --- | --- |
| `claude-move.py OLD NEW` | Move or rename a project here, and repoint everything that referred to the old path |
| `claude-move.py export` → `import` | Pack that state into a bundle and unpack it on another computer, repathed for the home directory there |

Single file, Python 3.8+, standard library only. Nothing to install.

```bash
git clone https://github.com/NotebookNomad/claude-move.git
cd claude-move

./claude-move.py --list        # what does Claude know about?
```

> **Quit any Claude Code session running in the project first.** A live session
> holds `~/.claude.json` in memory and writes it back when it exits, which would
> undo half the migration. Both the move and the import check for this and
> refuse rather than letting it happen.

## Moving a project on this machine

```bash
./claude-move.py ~/dev/api ~/work/api-server --dry-run    # show the plan, change nothing
./claude-move.py ~/dev/api ~/work/api-server              # do it

cd ~/work/api-server && claude --continue                 # pick up where you left off
```

Renaming is the same operation — `~/dev/api` → `~/dev/backend` works exactly
like a move.

### Where the project lands

The destination follows `mv`: if it names an **existing directory**, the project
moves *into* it and keeps its own name.

```bash
./claude-move.py ~/dev/api ~/work            # ~/work exists  → ~/work/api
./claude-move.py ~/dev/api ~/work/api-2      # doesn't exist  → renamed to api-2
```

If you already moved the folder yourself, pass the same two paths you gave `mv`
and the state catches up — `mv ~/dev/api ~/work` then
`claude-move.py ~/dev/api ~/work` finds the project at `~/work/api`.

### Moving several at once

Pass any number of projects followed by an existing directory, exactly like
`mv`. Each keeps its own name inside it.

```bash
./claude-move.py ~/dev/api ~/dev/web ~/archive     # three named paths
./claude-move.py ~/dev/*-service ~/archive         # the shell expands this
./claude-move.py '~/dev/*-service' ~/archive       # quoted: expanded by the tool
```

Wildcards normally never reach the script — your shell expands them first, and
that works with no special support. Quoting one hands the pattern over instead,
which is what you want when the folders have already been moved by hand: a
pattern that matches nothing on disk is matched against the projects Claude
still holds state for, so `--state-only` runs can use wildcards too.

Every project is planned before any of them is touched, so anything that would
block one — a live session, two projects that would land on the same name, one
nested inside another, a folder that is not where you said it was — stops the
whole batch before it starts. One safety copy covers the run. If a move still
fails partway through, it names the projects that already completed and points
at that copy.

### If Claude has already run at the new path

`--merge` combines the two sets of state: transcripts join, `allowedTools`
union, `memory/MEMORY.md` index entries merge, and any other conflicting file is
kept alongside the original as `name.migrated.ext` and reported so you can
reconcile it. Without `--merge`, existing state at the destination stops the run.

### Move options

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

## Moving to another computer

Moving a project on this machine and carrying it to a different one are the same
problem: Claude's state is keyed by an absolute path, and the path changes. The
`export` / `import` pair does the second one.

```bash
# on the old machine
./claude-move.py export -o claude-state.tar.gz

# move the file across however you like — AirDrop, scp, a USB stick

# on the new machine, with claude-move.py next to the bundle
./claude-move.py import claude-state.tar.gz
```

`/Users/dana/Documents/projects/api` becomes
`/home/casey/Documents/projects/api` — the home directory is swapped, the layout
below it is kept, and every reference inside the memory files, transcripts and
config follows.

Look before you leap:

```bash
./claude-move.py export --list                  # what would be exported
./claude-move.py inspect bundle.tar.gz          # what a bundle holds
./claude-move.py import bundle.tar.gz --dry-run # where each project would land
```

### Every memory file comes across

`export` with no project names selects **every project Claude holds state for**,
and each one carries its **complete memory**: every `.md` file in
`~/.claude/projects/<encoded-path>/memory/`, plus that project's `MEMORY.md`
index. `import` writes all of them on the other side, with the paths inside
their contents rewritten for the new home directory. Nothing is sampled and
nothing is left behind — if a memory file exists for an exported project, it is
in the bundle, and it lands on the new machine.

Memory is the one part you cannot switch off. `--no-sessions`,
`--no-file-history`, `--no-config` and `--no-history` narrow everything else;
`--memory-only` narrows the run to memory alone. There is no flag that drops
memory, on either end.

Two files named `CLAUDE.md` are *not* project memory, and follow different rules:

- `~/.claude/CLAUDE.md`, your user-level memory, belongs to no project. It
  travels only with `export --globals`, and an import never overwrites the one
  already on the machine — it is kept, and the incoming copy is written beside
  it as `CLAUDE.md.incoming`.
- A `CLAUDE.md` committed inside a project folder is your file, in your repo. It
  travels with the folder itself; a bundle holds only Claude's own state from
  `~/.claude`, and never your project's contents.

### What else a bundle carries

Everything, by default:

| | |
|---|---|
| **memory** | every `memory/*.md` plus the `MEMORY.md` index — **always, on both ends** |
| **sessions** | `.jsonl` transcripts, plus each session's subagent and tool-result spill |
| **file-history** | the blobs behind `/rewind`, renamed to match their new paths |
| **config** | allowed tools, MCP servers and trust, from `~/.claude.json` |
| **shell history** | the `history.jsonl` lines belonging to these projects |

The narrowing flags are the same on `export` and `import`, so you can pack
everything once and take only part of it on a given machine:

```bash
./claude-move.py export --memory-only        # memory and nothing else
./claude-move.py export --no-sessions        # skip transcripts
./claude-move.py import bundle.tar.gz --memory-only
```

`--globals` additionally carries the user-level `~/.claude` files —
`settings.json`, `CLAUDE.md`, `agents/`, `commands/`, `skills/`. Left out by
default, because those are usually the new machine's own.

Machine-local telemetry in `~/.claude.json` — `lastCost`, `lastSessionId`,
`lastVersionBase` and friends — is dropped, since it would be actively
misleading on the other machine. `--config-all` keeps it anyway.

### Picking projects, and where they land

```bash
./claude-move.py export                              # every project
./claude-move.py export icepick-offsec claude-skills # by directory name
./claude-move.py export '~/dev/*-service'            # or a pattern
```

By default every project keeps its position relative to your home directory.
When the new machine is laid out differently:

```bash
./claude-move.py import bundle.tar.gz --into ~/code
./claude-move.py import bundle.tar.gz --map api=~/work/api-server
```

`--map` takes the source path or just its directory name. A project that lived
outside your home directory has nothing to key the remap off, so its path is
kept verbatim and the run says so — `--map` it if that is wrong.

### Merging, not clobbering

`import` merges into whatever is already on the machine:

- `MEMORY.md` indexes are **unioned**, line by line.
- Any other file that already exists and differs is left alone; the incoming
  copy is written beside it as `*.incoming` and reported. `--overwrite` reverses
  that.
- `~/.claude.json` gains the project entries and loses nothing else.
- Shell-history lines already present are not appended twice.
- Everything the merge could overwrite is backed up first, in the same
  `~/.claude/claude-move-backups/` a move uses.

Import is idempotent — running it twice changes nothing the second time.

### Export and import options

Shared by both, narrowing what the run carries:

| Flag | Effect |
| --- | --- |
| `--memory-only` | Memory files and nothing else |
| `--no-sessions` | Skip session transcripts |
| `--no-file-history` | Skip the blobs behind `/rewind` |
| `--no-config` | Skip permissions, MCP servers and trust |
| `--no-history` | Skip the shell-history lines for these projects |

`export [PROJECT ...]`:

| Flag | Effect |
| --- | --- |
| `-o`, `--out FILE` | Bundle to write (default `claude-state-<timestamp>.tar.gz`) |
| `--list` | List what would be exported, write nothing |
| `--globals` | Also carry `~/.claude` `settings.json`, `CLAUDE.md`, `agents/`, `commands/`, `skills/` |
| `--config-all` | Keep this machine's cost and session counters in the config entries |

`import BUNDLE`:

| Flag | Effect |
| --- | --- |
| `-n`, `--dry-run` | Show where each project would land, change nothing |
| `--into DIR` | Place every project directly inside `DIR` |
| `--map OLD=NEW` | Send one project somewhere specific; `OLD` may be its source path or just its directory name (repeatable) |
| `--home DIR` | Treat `DIR` as this machine's home instead of `~` |
| `--overwrite` | Let incoming files replace ones already here |
| `--no-globals` | Ignore any `~/.claude`-level files in the bundle |
| `--no-backup` | Skip the safety copy |
| `--force` | Proceed despite live sessions or mapping collisions |

### The bundle is private

A full bundle contains your session transcripts — everything Claude read, wrote
and was told, in every session of every project exported. Treat it like a
password store; encrypt it if it goes anywhere you do not control.

```bash
gpg -c claude-state.tar.gz            # → claude-state.tar.gz.gpg
```

`--memory-only` produces a far smaller and far less sensitive bundle (24K
against 13M, on the tree this was developed against).

## What counts as state

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

## How projects are found

A project is discovered from `~/.claude.json`, from the `cwd` recorded inside its
transcripts, and — failing both — by resolving the state directory's name against
the filesystem.

That last one matters more than it sounds. The encoding is lossy, so a name
cannot be reversed on its own; it *can* be reversed against the directories that
actually exist. This is the only way to find a project whose folder was renamed
while its transcripts kept naming the path it moved away from — the case that
turned up nine memory files during development that every other method missed.

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
python3 tests/test_export_import.py          # the export/import round trip
```

The move suite builds a synthetic `~/.claude` mirroring the real layout and
checks the full migration, subprojects, merging, wildcards, batch moves,
blockers, and `--dry-run` being read-only.

The export/import suite runs a full round trip between two fake machines with
different home directories. Most of its cases assert a **negative** — that a
project never named on the command line did not come across, that a
prefix-sharing neighbour (`api-server` beside `api`) kept its own name, that a
bystander already on the target was untouched, that the source machine was never
written to. That is where this class of tool actually breaks.

Your real `~/.claude` is never touched by either.

## License

MIT — see [LICENSE](LICENSE).

---

An independent project, not affiliated with, endorsed by, or supported by
Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic.
