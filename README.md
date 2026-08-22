# claude-move

**Moved a project folder? Claude Code just forgot everything about it. This puts
it back.**

Claude Code remembers a lot about each project you work in — the memory files it
keeps for you, the permissions you granted it, and every past conversation. None
of that is stored in the project folder itself. It is filed away under the
folder's exact location on disk.

So the moment you drag that folder somewhere else, rename it, or switch to a new
computer, Claude opens it as if it had never seen it before. Blank memory. No
permissions. No history.

`claude-move` moves your project *and* everything Claude remembers about it, so
you carry on where you left off.

## What you need

A terminal, and Python 3.8 or newer. Most Macs and Linux machines already have
it — run `python3 --version` to check. There is nothing to install, nothing to
sign up for, and no dependencies. The whole tool is a single file.

```bash
git clone https://github.com/NotebookNomad/claude-move.git
cd claude-move

./claude-move.py --list        # show every project Claude knows about
```

If you don't have `git`, download `claude-move.py` from the project page
instead, put it anywhere, and run it the same way.

Every command below is run from the folder holding that file.

> [!IMPORTANT]
> **Close Claude Code before you run this.** If Claude is still open in the
> project, it will overwrite half the work as it shuts down. The tool checks,
> and stops with a message rather than letting that happen.

## "I want to move or rename a folder"

Say your project lives at `~/dev/api` and you want it at `~/work/api-server`.
Preview it first — this changes nothing:

```bash
./claude-move.py ~/dev/api ~/work/api-server --dry-run
```

You will see a list of exactly what it plans to do. If it looks right, run the
same line without `--dry-run`:

```bash
./claude-move.py ~/dev/api ~/work/api-server
```

That's it. Open the project at its new home and everything is there:

```bash
cd ~/work/api-server && claude --continue
```

**Renaming works the same way.** `~/dev/api` → `~/dev/backend` is just a move.

**Already moved the folder yourself?** Run the same command anyway, with the
same two locations you used before. It will find the folder where you put it and
fix Claude's records to match.

**Moving several at once?** List them, then the folder they all go into:

```bash
./claude-move.py ~/dev/api ~/dev/web ~/archive
```

It checks every one of them before touching any of them, so a problem with one
stops the whole batch before it starts.

## "I'm switching to a new computer"

Same problem, bigger version: your files are on the new machine, but Claude's
memory of them is still on the old one. Two commands fix it.

On the **old** computer, pack everything into a single file:

```bash
./claude-move.py export -o claude-state.tar.gz
```

Copy that file across however you normally would — AirDrop, a USB stick, email
it to yourself. Then on the **new** computer, with `claude-move.py` sitting next
to the file you copied:

```bash
./claude-move.py import claude-state.tar.gz
```

Your projects now remember everything they did on the old machine.

This works even though the two computers store things differently. If you were
`dana` on a Mac and you're `casey` on a Linux laptop,
`/Users/dana/Documents/projects/api` quietly becomes
`/home/casey/Documents/projects/api`, and every mention of the old location
inside your memory files and conversations is updated to match.

Want to look before you leap? None of these change anything:

```bash
./claude-move.py export --list                  # what would be packed up
./claude-move.py inspect bundle.tar.gz          # what's inside a file you were given
./claude-move.py import bundle.tar.gz --dry-run # where everything would land
```

### Every memory file comes with it

This is the part people worry about, so to be plain about it:

**Nothing is left behind, and nothing is picked out.** `export` on its own packs
up *every* project Claude knows about, and for each one it takes *every* memory
file it has — the whole memory folder, plus the index file that lists them.
`import` puts all of them on the new computer, with the old computer's file
locations corrected inside them.

**Memory is the one thing you cannot accidentally switch off.** There are
options to leave out past conversations, permissions and so on. There is no
option that leaves out memory. It always travels, in both directions.

If you want *only* your memory files and none of the rest, `--memory-only` does
that — and it makes for a much smaller, much less private file:

```bash
./claude-move.py export --memory-only -o memory.tar.gz
```

<details>
<summary><b>One thing that is <i>not</i> project memory</b></summary>

Your personal `~/.claude/CLAUDE.md` — the notes that apply to you everywhere,
rather than to one project — is not part of any project, so it is left out by
default. Add `--globals` to bring it along, together with your settings, agents,
commands and skills. Even then, an import will never overwrite the version
already on the new computer; it saves the incoming one next to it, named
`CLAUDE.md.incoming`, and tells you.

A `CLAUDE.md` file kept *inside* a project folder is your own file, in your own
project. It travels with the folder like any other file, and is none of this
tool's business.

</details>

### Importing never overwrites what's already there

If the new computer already has some of these projects, `import` blends the two
together rather than steamrolling anything:

- Memory index files are combined, so you keep both sets of entries.
- Any other file that already exists is **left exactly as it is**. The incoming
  version is saved beside it, ending in `.incoming`, and the tool tells you which
  ones so you can compare them yourself.
- Nothing is removed from your settings; entries are only added.
- Running the same import twice does nothing the second time.

### Keep that file to yourself

A full export contains your past conversations with Claude — everything it read,
wrote, and was told, in every project. Treat the file like a password manager
export. If it is going anywhere you don't control, lock it first:

```bash
gpg -c claude-state.tar.gz            # → claude-state.tar.gz.gpg
```

## "I already moved things the hard way"

If folders have been moved around without this tool, Claude's memory files are
left describing a layout that no longer exists — notes pointing at `~/dev/api`
when the project has been at `~/work/api` for months.

```bash
./claude-move.py repair
```

It reads every memory file, picks out the locations that are no longer there,
works out where each one went, and shows you the list before touching anything:

```
Found 1 path(s) that moved:

  1. ~/dev/api
     -> ~/work/api
     Claude's own state for that project resolves to it
     1 reference(s) in 1 memory file(s):
       api/memory/layout.md:12
         was  The service lives at ~/dev/api and the docs are next to it.
         now  The service lives at ~/work/api and the docs are next to it.

Apply? [a] all, [n] none
```

Answer `a` to take all of it, `n` to take none, or type numbers — `1,3` — to
take only some. Nothing is written until you answer, and `-n` shows the same
list and then stops.

### Every rewrite is quoted before you agree to it

That `was` / `now` pair is the important part. It is there because **a path can
have genuinely moved and still be wrong to rewrite** — when the sentence is
recording history rather than pointing somewhere:

```
       api/memory/history.md:24   reads like history
         was  ...had been renamed while the notes kept naming `~/dev/api`. Every...
         now  ...had been renamed while the notes kept naming `~/work/api`. Every...
```

That second line is nonsense: the sentence exists to say the notes held the
*old* path. No amount of checking the disk can spot that — only the words
around the path can. So `repair` looks for phrases like "had been", "formerly",
"matched from" and "for example" near each mention, marks those **reads like
history**, and says so again underneath the finding when several of them are:

```
     4 of 5 references read like history rather than a live path.  Check the
     wording before applying -- a rewrite may not be what you want here.
```

It is a hint, not a verdict — read the quoted line and decide. Leaving a
finding's number out of your answer skips it.

Some of what it finds is certain and some is a guess, and it says which is
which. A location Claude's own records account for is certain. A folder matched
only by its name is labelled **a guess**, because a memory file might simply be
quoting an example rather than pointing at a real place. Read those before
accepting them — declining one is just a matter of leaving its number out.

The weakest guesses come from searching your home folder for a folder of the
same name. `--no-search` turns that search off, leaving only the guesses drawn
from projects Claude already knows about.

Locations it cannot place are listed separately and left exactly as written.

> [!NOTE]
> `repair` fixes the *wording inside memory files*. If a whole project has
> moved, its conversations and permissions need moving too — the run tells you
> so, and prints the `--state-only` command that does it.

## If something goes wrong

**Everything is backed up before it is touched**, into a dated folder at
`~/.claude/claude-move-backups/`. To undo a migration, copy the files back out
of there.

The tool is built to stop rather than guess. If Claude is still running, if the
destination already has something in it, or if anything looks unlike what it
expects, it says so and does nothing. `--dry-run` shows you the full plan first,
and nothing is written until you confirm.

**Your own files are never edited.** Only Claude's records of them.

---

## Details

Everything below is reference material. You don't need it to use the tool.

<details>
<summary><b>All the options</b></summary>

**Moving a folder** — `claude-move.py OLD NEW`

| Flag | Effect |
| --- | --- |
| `-n`, `--dry-run` | Print the full plan and exit without changing anything |
| `-y`, `--yes` | Skip the confirmation prompt |
| `--state-only` | The folder is already at the new path; just fix Claude's records |
| `--merge` | Combine with records that already exist at the new path |
| `--no-subprojects` | Don't remap projects nested inside the folder |
| `--no-project-settings` | Don't rewrite paths in the project's own `.claude/` files |
| `--no-backup` | Skip the safety copy |
| `--force` | Proceed despite non-fatal blockers, such as a live session |
| `--list` | List known projects and exit |

**Choosing what to carry** — the same on `export` and `import`, so you can pack
everything once and take only part of it on a given machine

| Flag | Effect |
| --- | --- |
| `--memory-only` | Memory files and nothing else |
| `--no-sessions` | Skip past conversations |
| `--no-file-history` | Skip the saved copies behind `/rewind` |
| `--no-config` | Skip permissions, MCP servers and trust |
| `--no-history` | Skip the shell-history lines for these projects |

**`export [PROJECT ...]`** — with no projects named, it takes them all

| Flag | Effect |
| --- | --- |
| `-o`, `--out FILE` | File to write (default `claude-state-<timestamp>.tar.gz`) |
| `--list` | List what would be exported, write nothing |
| `--globals` | Also carry `~/.claude` `settings.json`, `CLAUDE.md`, `agents/`, `commands/`, `skills/` |
| `--config-all` | Keep this machine's cost and session counters in the config entries |

**`import BUNDLE`**

| Flag | Effect |
| --- | --- |
| `-n`, `--dry-run` | Show where each project would land, change nothing |
| `--into DIR` | Place every project directly inside `DIR` |
| `--map OLD=NEW` | Send one project somewhere specific; `OLD` may be its old location or just its folder name (repeatable) |
| `--home DIR` | Treat `DIR` as this machine's home instead of `~` |
| `--overwrite` | Let incoming files replace ones already here |
| `--no-globals` | Ignore any `~/.claude`-level files in the bundle |
| `--no-backup` | Skip the safety copy |
| `--force` | Proceed despite live sessions or mapping collisions |

**`repair [PROJECT ...]`** — with no projects named, it checks them all

| Flag | Effect |
| --- | --- |
| `-n`, `--dry-run` | Show what it found, change nothing |
| `-y`, `--yes` | Apply everything found, guesses included, without asking |
| `--no-search` | Don't match a missing folder to one of the same name elsewhere |
| `--no-backup` | Skip the safety copy |

</details>

<details>
<summary><b>Naming projects, and where they land on the new machine</b></summary>

You can name the projects you want rather than taking everything. A project can
be named by its full location, just its folder name, or a pattern:

```bash
./claude-move.py export                              # every project
./claude-move.py export icepick-offsec claude-skills # by folder name
./claude-move.py export '~/dev/*-service'            # or a pattern
```

On import, every project keeps its position relative to your home folder by
default. When the new machine is arranged differently:

```bash
./claude-move.py import bundle.tar.gz --into ~/code
./claude-move.py import bundle.tar.gz --map api=~/work/api-server
```

A project that lived outside your home folder has nothing to anchor the move to,
so its location is kept exactly as it was and the run says so — use `--map` if
that is wrong.

</details>

<details>
<summary><b>Moving several folders at once</b></summary>

Name any number of projects followed by an existing folder, exactly like the
`mv` command. Each keeps its own name inside it.

```bash
./claude-move.py ~/dev/api ~/dev/web ~/archive     # three named paths
./claude-move.py ~/dev/*-service ~/archive         # the shell expands this
./claude-move.py '~/dev/*-service' ~/archive       # quoted: expanded by the tool
```

Wildcards normally never reach the script — your shell expands them first, and
that works with no special support. Quoting one hands the pattern over instead,
which is what you want when the folders have already been moved by hand: a
pattern that matches nothing on disk is matched against the projects Claude
still holds records for, so `--state-only` runs can use wildcards too.

Every project is planned before any of them is touched, so anything that would
block one — a live session, two projects that would land on the same name, one
nested inside another, a folder that is not where you said it was — stops the
whole batch before it starts. One safety copy covers the run. If a move still
fails partway through, it names the projects that already completed and points
at that copy.

Where a single move lands follows `mv` too: if the destination names a folder
that **already exists**, the project moves *into* it and keeps its own name.

```bash
./claude-move.py ~/dev/api ~/work            # ~/work exists  → ~/work/api
./claude-move.py ~/dev/api ~/work/api-2      # doesn't exist  → renamed to api-2
```

If Claude has already run at the new location, `--merge` combines the two sets:
conversations join, permissions merge, memory index entries merge, and any other
clashing file is kept alongside the original as `name.migrated.ext` and reported
so you can reconcile it. Without `--merge`, existing records at the destination
stop the run.

</details>

<details>
<summary><b>What Claude actually stores, and where</b></summary>

| What | Where it lives |
| --- | --- |
| Past conversations | `~/.claude/projects/<encoded-path>/*.jsonl` |
| Memory files | `~/.claude/projects/<encoded-path>/memory/` |
| Permissions, MCP servers, trust | `~/.claude.json` → `projects["<abs path>"]` |
| Prompt history | `~/.claude/history.jsonl` |
| File backups behind `/rewind` | `~/.claude/file-history/<session-id>/` |
| Background job and session state | `~/.claude/jobs/`, `sessions/`, `session-env/` |

`<encoded-path>` is the folder's full location with every non-alphanumeric
character replaced by `-`, so `/Users/me/dev/my_app` becomes
`-Users-me-dev-my-app`.

Beyond relocating those directories, the script rewrites references to the old
location wherever they are embedded: the working directory recorded in each
conversation, commands and output quoting the old path, memory file contents,
`~/` shorthand forms, and absolute paths inside the project's own `.claude/`
settings and hooks. Backup copies behind `/rewind` are named from a hash of the
file's path, so those get renamed too and stay usable.

Files to update are found by scanning `~/.claude` for the old location, so
anything a future Claude Code version adds is covered automatically. Projects
nested inside the folder you are moving are remapped as well.

Writes are atomic — temp file plus rename — so an interruption never leaves a
half-written settings file.

</details>

<details>
<summary><b>How projects are found</b></summary>

A project is discovered from `~/.claude.json`, from the working directory
recorded inside its conversations, and — failing both — by resolving the storage
folder's name against the filesystem.

That last one matters more than it sounds. The encoding is lossy, so a name
cannot be reversed on its own; it *can* be reversed against the folders that
actually exist. This is the only way to find a project whose folder was renamed
while its conversations kept naming the location it moved away from — the case
that turned up nine memory files during development that every other method
missed.

**One caveat: the encoding is lossy.** `my_app`, `my app` and `my-app` all
encode to `-my-app`, so those projects *share a single storage folder*. If the
project you are moving collides with another this way, the script warns you
before doing anything — the other project's conversations would move too.

</details>

<details>
<summary><b>Compatibility, and how it fails</b></summary>

This tool works against Claude Code's on-disk layout, which is internal and not
a documented API — a future version could rearrange it.

Developed and tested against **Claude Code 2.1.226** on macOS, with Python
3.11. The layout is the same on Linux, though it hasn't been exercised there.

If a future version moves things around, the failure is visible rather than
silent: `--dry-run` prints exactly which files it found and what it will do, so
a plan that looks too small is the signal to check before running it. Nothing is
edited until you confirm, and everything it touches is backed up first.

</details>

<details>
<summary><b>Tests</b></summary>

```bash
python3 tests/test_claude_move.py            # runs in a temp dir, cleaned up
python3 tests/test_claude_move.py /tmp/keep  # keep the fixtures to inspect
python3 tests/test_export_import.py          # the export/import round trip
python3 tests/test_repair.py                 # what repair fixes, and what it won't
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

</details>

## License

MIT — see [LICENSE](LICENSE).

---

An independent project, not affiliated with, endorsed by, or supported by
Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic.
