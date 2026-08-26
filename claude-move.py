#!/usr/bin/env python3
"""
claude-move — move a project without losing its Claude Code state
(transcripts, memory files, permissions, history), across your disk or across
machines.

Claude Code keys almost everything off the project's absolute path:

  ~/.claude/projects/<encoded-path>/      session transcripts (*.jsonl)
  ~/.claude/projects/<encoded-path>/memory/   persistent memory files
  ~/.claude.json  -> projects["<abs path>"]   permissions, MCP servers, trust
  ~/.claude/history.jsonl                 prompt history, tagged by project
  ~/.claude/file-history/<session-id>/     file backups, named sha256(path)[:16]
  ~/.claude/session-env/, jobs/, sessions/ assorted per-session state

where <encoded-path> is the absolute path with every non-alphanumeric
character replaced by "-".  Moving a project with `mv` alone orphans all of
it: Claude starts the new location with empty memory and no permissions.

This script performs the move and rewrites every reference.  The files to
update are found by scanning ~/.claude for the old path, so state belonging to
Claude Code versions newer than this script is covered too.

Switching computers is the same problem with a different substitution -- the
home directory changes rather than one project's path -- so `export` packs that
state into a .tar.gz and `import` unpacks it on the other machine, rewritten for
whatever home lives there.  Both halves share the path matching, the file
rewriting and the backup directory; only the substitutions differ.

Usage:
    claude-move.py /old/path /new/path            # move files + state
    claude-move.py /old/path /existing/dir        # moves in, keeping its name
    claude-move.py a b c /existing/dir            # several at once, like mv
    claude-move.py '/dev/api-*' /existing/dir     # quoted glob, expanded here
    claude-move.py /old/path /new/path --dry-run  # show the plan only
    claude-move.py /old/path /new/path --state-only   # folder already moved
    claude-move.py --list                         # show projects with state

    claude-move.py export -o state.tar.gz         # pack it up on this machine
    claude-move.py inspect state.tar.gz           # what a bundle holds
    claude-move.py import state.tar.gz            # unpack it there, repathed

    claude-move.py repair                         # stale paths in memory files

Stdlib only, Python 3.8+.
"""

from __future__ import annotations

import argparse
import errno
import fnmatch
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tarfile
import tempfile
import time
from typing import (Any, Callable, Container, Dict, Iterable, Iterator, List,
                    Optional, Set, Tuple)

# Directories under ~/.claude that the path sweep must not touch:
#   projects/            the moved state dirs are handled explicitly; other
#                        projects' transcripts are none of our business
#   file-history/        blobs are verbatim copies of the user's own files --
#                        only their *names* encode a path
#   claude-move-backups/ our own safety copies
SKIP_TOP_LEVEL = {"projects", "file-history", "claude-move-backups"}

# transcripts can be hundreds of MB; only the first lines are needed to learn
# which directory a session ran in
CWD_SCAN_LINES = 400

# why two different paths can share one state directory; said wherever a
# collision is reported, so the two messages cannot drift apart
COLLAPSE_NOTE = "Claude collapses _, spaces and . to -"

# ---------------------------------------------------------------------------
# path encoding
# ---------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]")

# A path only matches when what follows cannot continue its last segment, so
# "/dev/api" claims "/dev/api/pkg" and '/dev/api"' but never "/dev/api-server".
# The second lookahead is for the dot: "api.bak" is a different directory, while
# a path ending a sentence in a memory file ("Run it from /dev/api.") is not --
# a dot only blocks the match when a name character follows it.
_SEGMENT_END = r"(?![A-Za-z0-9_-])(?!\.[A-Za-z0-9_-])"
BACKUP_DIR = "claude-move-backups"
_CWD_RE = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')
_GLOB_MAGIC = re.compile(r"[*?\[]")


def encode_path(path: str) -> str:
    """Encode an absolute path the way Claude Code names its project dirs.

    /Users/me/Documents/my_app  ->  -Users-me-Documents-my-app

    Note this is lossy: "my_app", "my app" and "my-app" all collapse to the
    same directory name.  The collision checks below account for that.
    """
    return _NON_ALNUM.sub("-", path)


def backup_file_name(abs_path: str, previous_name: str) -> str:
    """Name of a file-history blob for `abs_path`, keeping the @v<n> suffix.

    Claude Code stores backups as sha256(absolute path)[:16]@v<version>, so a
    moved file needs its blob renamed to stay resolvable.
    """
    digest = hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:16]
    _, sep, suffix = previous_name.partition("@")
    return digest + sep + suffix


def norm(path: str) -> str:
    """Absolute, normalized, no trailing slash.  Symlinks are NOT resolved --
    Claude records the cwd as given, so we match that."""
    p = os.path.abspath(os.path.expanduser(path))
    return p.rstrip("/") or "/"


def is_under(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def remap(path: str, src: str, dst: str) -> str:
    """Map a path under `src` onto the same position under `dst`."""
    if path == src:
        return dst
    return dst.rstrip("/") + path[len(src.rstrip("/")):]


def tilde(path: str, home: str) -> Optional[str]:
    if is_under(path, home) and path != home:
        return "~" + path[len(home):]
    return None


# ---------------------------------------------------------------------------
# small utilities
# ---------------------------------------------------------------------------


class Log:
    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self.warnings: List[str] = []

    def info(self, msg: str = "") -> None:
        if not self.quiet:
            print(msg)

    def step(self, msg: str) -> None:
        if not self.quiet:
            print(f"  {msg}")

    def _stderr(self, msg: str) -> None:
        sys.stdout.flush()
        print(msg, file=sys.stderr)
        sys.stderr.flush()

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        self._stderr(f"  warning: {msg}")

    def error(self, msg: str) -> None:
        self._stderr(f"error: {msg}")


def read_text(path: str) -> Optional[str]:
    """Whole file as text, or None if it is binary or unreadable.  One open
    per file -- the binary sniff and the read share it."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", "replace")


def tmp_path(path: str) -> str:
    return f"{path}.claude-move.{os.getpid()}.tmp"


def commit(tmp: str, path: str) -> None:
    if os.path.exists(path):
        shutil.copystat(path, tmp)
    os.replace(tmp, path)


def discard(tmp: str) -> None:
    try:
        os.unlink(tmp)
    except OSError:
        pass


def write_text_atomic(path: str, text: str) -> None:
    """Write via a temp file + rename, so a crash or a concurrent reader never
    sees a half-written file."""
    tmp = tmp_path(path)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        commit(tmp, path)
    except OSError:
        discard(tmp)
        raise


def write_json_atomic(path: str, data: Any) -> None:
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def unique(path: str) -> str:
    """A path that does not exist yet, by suffixing .1, .2, ..."""
    candidate, n = path, 1
    while os.path.exists(candidate):
        candidate = f"{path}.{n}"
        n += 1
    return candidate


# ---------------------------------------------------------------------------
# path rewriting
# ---------------------------------------------------------------------------


class Rewriter:
    """Ordered set of path substitutions applied to every string in a JSON
    document (keys included) or to raw text.

    Replacements are applied longest-old-first so that the more specific
    strings (the state directory, subproject paths) win over the shorter ones
    they contain.

    A match must end on a path-segment boundary.  Plain substring replacement
    would rewrite every neighbour that merely starts the same way: moving
    ~/dev/api would silently re-key ~/dev/api-server, a project that was never
    named, onto a directory that does not exist.
    """

    def __init__(self) -> None:
        self._pairs: List[Tuple[str, str, "re.Pattern[str]"]] = []
        self.hits = 0

    def add(self, old: str, new: str) -> None:
        self._add(old, new)
        # a path containing characters JSON escapes (non-ASCII, quotes,
        # backslashes) appears in transcripts in escaped form; match that
        # spelling too, so the raw-line fast path in _rewrite_jsonl stays sound
        self._add(json.dumps(old)[1:-1], json.dumps(new)[1:-1])

    def _add(self, old: str, new: str) -> None:
        if old and new and old != new and not any(o == old for o, _, _ in self._pairs):
            self._pairs.append((old, new, re.compile(re.escape(old) + _SEGMENT_END)))
            self._pairs.sort(key=lambda pair: len(pair[0]), reverse=True)

    def _apply(self, value: str) -> Tuple[str, int]:
        out, changes = value, 0
        for old, new, pattern in self._pairs:
            if old in out:   # cheap guard; the regex can only match where this does
                # a function replacement, so backslashes in `new` (the
                # JSON-escaped spellings are full of them) stay literal
                out, made = pattern.subn(lambda _match, new=new: new, out)
                changes += made
        return out, changes

    def text(self, value: str) -> str:
        out, changes = self._apply(value)
        if changes:
            self.hits += 1
        return out

    def preview(self, value: str) -> str:
        """What `text` would produce, without counting the string as changed --
        for showing someone a rewrite before they agree to it."""
        return self._apply(value)[0]

    def count(self, value: str) -> int:
        """How many substitutions `text` would make.

        Counted by actually making them, longest match first, so a state
        directory name counts once rather than once per shorter spelling it
        happens to contain.
        """
        return self._apply(value)[1]

    def obj(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.obj(item) for item in value]
        if isinstance(value, dict):
            return {self.text(k) if isinstance(k, str) else k: self.obj(v)
                    for k, v in value.items()}
        return value

    def touches(self, blob: str) -> bool:
        return any(old in blob and pattern.search(blob)
                   for old, _, pattern in self._pairs)


class FileRewriter:
    """Applies a Rewriter to files on disk, counting what changed and noting
    what still names an old path afterwards.

    Both halves of this tool need exactly this: a move rewrites the state it
    just relocated, and an import rewrites the state it just unpacked from
    another machine.  Only the substitutions differ.
    """

    def __init__(self, rules: "Rewriter", log: Log) -> None:
        self.rules = rules
        self.log = log
        self.files_rewritten = 0
        self.stale: List[str] = []

    def jsonl(self, path: str) -> Dict[str, str]:
        """Stream a JSONL file, rewriting only the lines that name an old path.
        Returns the file-history blob renames its records imply.

        Streaming keeps peak memory at one line, and the per-line guard skips
        parsing the majority of records -- transcripts and history.jsonl are
        the largest files this tool touches.
        """
        renames: Dict[str, str] = {}
        tmp = tmp_path(path)
        changed = False
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as source, \
                    open(tmp, "w", encoding="utf-8") as out:
                for line in source:
                    if not self.rules.touches(line):
                        out.write(line)
                        continue
                    changed = True
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        out.write(self.rewritten(path, line))
                        continue
                    for file_path, meta in tracked_backups(rec):
                        new_path = self.rules.text(file_path)
                        if new_path == file_path:
                            continue
                        # keep the blob name consistent with the new path, in
                        # the same pass that rewrites the path itself
                        old_name = meta["backupFileName"]
                        renames[old_name] = meta["backupFileName"] = \
                            backup_file_name(new_path, old_name)
                    encoded = json.dumps(self.rules.obj(rec), ensure_ascii=False)
                    out.write(self.note_stale(path, encoded) + "\n")
        except OSError as exc:
            discard(tmp)
            self.log.warn(f"could not rewrite {path}: {exc}")
            return {}

        if changed:
            commit(tmp, path)
            self.files_rewritten += 1
        else:
            discard(tmp)
        return renames

    def json(self, path: str) -> None:
        blob = read_text(path)
        if blob is None or not self.rules.touches(blob):
            return
        try:
            data = self.rules.obj(json.loads(blob))
        except ValueError:
            self.text(path)
            return
        write_json_atomic(path, data)
        self.note_stale(path, json.dumps(data, ensure_ascii=False))
        self.files_rewritten += 1

    def text(self, path: str) -> None:
        text = read_text(path)
        if text is None or not self.rules.touches(text):
            return
        write_text_atomic(path, self.rewritten(path, text))
        self.files_rewritten += 1

    def rewritten(self, path: str, text: str) -> str:
        return self.note_stale(path, self.rules.text(text))

    def note_stale(self, path: str, text: str) -> str:
        """Record anything still naming the old path after rewriting, so
        verification is a byproduct of the write pass rather than a third
        walk over everything it just wrote."""
        if path not in self.stale and self.rules.touches(text):
            self.stale.append(path)
        return text


def backup_root(layout: "Layout") -> str:
    """A fresh dated directory for a safety copy.  Every command that takes one
    puts it in the same place, which is the place the README tells people to
    look."""
    return os.path.join(layout.dir, BACKUP_DIR, time.strftime("%Y%m%d-%H%M%S"))


def project_pairs(layout: "Layout", old: str, new: str) -> List[Tuple[str, str]]:
    """Every spelling of one project path that moving it changes.

    A project's location is written down four ways -- the path, the state
    directory derived from it, the ~-relative form, and the bare encoded token
    on its own (inside scratchpad paths under /tmp, say).  A move and a repair
    both have to correct all four, and a list built twice is a list that ends
    up correcting three.
    """
    pairs = [(layout.state_dir(old), layout.state_dir(new)), (old, new)]
    old_tilde, new_tilde = tilde(old, layout.home), tilde(new, layout.home)
    if old_tilde and new_tilde:
        pairs.append((old_tilde, new_tilde))
    pairs.append((encode_path(old), encode_path(new)))
    return pairs


def tracked_backups(rec: Any) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Yield (tracked file path, backup metadata) pairs from a transcript
    record, skipping anything that isn't shaped like a backup entry."""
    if not isinstance(rec, dict):
        return
    tracked = (rec.get("snapshot") or {}).get("trackedFileBackups")
    if not isinstance(tracked, dict):
        return
    for path, meta in tracked.items():
        if isinstance(meta, dict) and isinstance(meta.get("backupFileName"), str):
            yield path, meta


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


class Layout:
    """Locations of Claude Code's on-disk state."""

    def __init__(self, claude_dir: str, config_path: str) -> None:
        self.dir = norm(claude_dir)
        self.config = norm(config_path)
        self.projects = os.path.join(self.dir, "projects")
        self.sessions = os.path.join(self.dir, "sessions")
        self.file_history = os.path.join(self.dir, "file-history")
        self.history = os.path.join(self.dir, "history.jsonl")
        # the home these paths belong to.  Normally just ~, but --config
        # pointing elsewhere (a restored backup, a test fixture) describes a
        # different home, and every path in it is relative to that one rather
        # than to whoever is running the script.
        self.home = norm(os.path.dirname(self.config))

    def state_dir(self, project_path: str) -> str:
        return os.path.join(self.projects, encode_path(project_path))


def state_dirs(layout: Layout) -> List[str]:
    if not os.path.isdir(layout.projects):
        return []
    paths = (os.path.join(layout.projects, name)
             for name in sorted(os.listdir(layout.projects)))
    return [p for p in paths if os.path.isdir(p)]


def session_ids(state_dir: str) -> List[str]:
    if not os.path.isdir(state_dir):
        return []
    return sorted(f[:-len(".jsonl")] for f in os.listdir(state_dir) if f.endswith(".jsonl"))


def is_memory_entry(name: str) -> bool:
    """Split a state directory into its memory half and its session half.

    Memory is memory/ plus a MEMORY.md index, which has been seen both inside
    memory/ and at the state-dir root.  Everything else belongs to a session --
    not only the .jsonl transcripts but the <session-id>/ directories of
    subagent and tool-result spill beside them.
    """
    return name in ("memory", "MEMORY.md")


def memory_files(state_dir: str) -> List[str]:
    """Memory file paths relative to the state directory."""
    out = []
    memory = os.path.join(state_dir, "memory")
    if os.path.isdir(memory):
        out += [os.path.join("memory", f) for f in sorted(os.listdir(memory))
                if f.endswith(".md")]
    if os.path.isfile(os.path.join(state_dir, "MEMORY.md")):
        out.append("MEMORY.md")
    return out


def decode_state_dir(name: str) -> List[str]:
    """Recover the project paths a state-directory name could have come from.

    encode_path is lossy -- every non-alphanumeric character becomes "-" -- so
    the name cannot be reversed on its own.  It can be reversed against the
    filesystem: walk down from the root taking only the children whose own
    encoding is a prefix of what is left, and every leaf reached is a directory
    that really would produce this name.  Usually exactly one exists.

    This is the only way to find a project whose state directory was renamed
    (by a move, or by hand) while its transcripts still record the old path.
    """
    found: List[str] = []
    if not name.startswith("-"):
        return found

    def walk(parent: str, remaining: str) -> None:
        if not remaining.startswith("-"):
            return
        rest = remaining[1:]
        try:
            children = os.listdir(parent or "/")
        except OSError:
            return
        for child in children:
            encoded = _NON_ALNUM.sub("-", child)
            path = os.path.join(parent or "/", child)
            if rest == encoded:
                if os.path.isdir(path):
                    found.append(path)
            elif rest.startswith(encoded + "-") and os.path.isdir(path):
                walk(path, remaining[1 + len(encoded):])

    walk("", name)
    return sorted(set(found))


def known_projects(layout: Layout, log: Optional[Log] = None,
                   cwds: Optional[Dict[str, Set[str]]] = None) -> Dict[str, str]:
    """Every project path Claude knows about -> its state directory.

    Three sources, in decreasing order of reliability: ~/.claude.json, the cwd
    recorded inside transcripts (which catches projects the config forgot), and
    finally the state directory name resolved against the filesystem (which
    catches a project whose state dir was renamed while its transcripts kept
    naming the path it moved away from -- nothing else on disk records that).

    `log`, when given, reports what the third pass found or could not resolve.
    `cwds`, when given a dict, is filled with the cwd set found in each state
    directory.  Reading those is the expensive half of this scan, and a caller
    that needs them too should not pay for the transcripts twice.
    """
    found: Dict[str, str] = {}
    try:
        cfg = json.loads(read_text(layout.config) or "{}")
        for path in (cfg.get("projects") or {}):
            found[norm(path)] = layout.state_dir(norm(path))
    except (ValueError, AttributeError):
        pass

    for state in state_dirs(layout):
        recorded = cwds_in_state_dir(state)
        if cwds is not None:
            cwds[state] = recorded
        for cwd in recorded:
            # a session can run with its cwd inside ~/.claude (e.g. a subagent
            # editing memory); that is not a project
            if not is_under(cwd, layout.dir):
                found.setdefault(cwd, layout.state_dir(cwd))

    claimed = {os.path.basename(d) for d in found.values()}
    for state in state_dirs(layout):
        name = os.path.basename(state)
        if name in claimed or not (memory_files(state) or session_ids(state)):
            continue
        candidates = [c for c in decode_state_dir(name) if norm(c) not in found]
        if len(candidates) == 1:
            found[norm(candidates[0])] = state
            if log:
                log.info(f"  recovered {tilde(norm(candidates[0]), layout.home)} "
                         f"from its state dir name -- no config entry, and its "
                         f"transcripts name an older path")
        elif candidates and log:
            log.warn(f"state dir {name} could belong to any of: "
                     f"{', '.join(candidates)} -- skipped")
        elif log:
            log.warn(f"state dir {name} holds {len(memory_files(state))} memory "
                     f"file(s) but no matching project directory exists here")
    return found


def kept_state(state: str, paths: Iterable[str],
               entries: Container[str], counts: Container[str]) -> bool:
    """Whether Claude has actually filed anything under this project.

    `known_projects` promotes every cwd a transcript ever recorded to a
    project path and maps it to the state directory it *would* have.  Most are
    not projects: a subdirectory someone cd'd into for one command, one of
    Claude Code's own worktrees under a project's .claude/, a name a folder
    wore for an afternoon before being renamed.

    A command that reports or deletes what this machine is keeping must not
    count those -- there is nothing filed under them to report or delete, and
    counting them claims more projects than ~/.claude/projects holds.  The
    move path deliberately keeps them, which is why this narrows at the
    consumer rather than inside `known_projects`: a session that ran in a
    subdirectory really does have a scratchpad filed under that
    subdirectory's encoded name, and a rewrite that skipped it would strand
    the path it names.

    Blobs need no test of their own: they hang off session ids read out of a
    state directory, so a project without one has none.  `entries` and
    `counts` are membership-tested rather than read -- the maps behind them
    only ever hold a non-empty list and a count of at least one.
    """
    return (os.path.isdir(state)
            or any(path in entries or path in counts for path in paths))


def cwds_in_state_dir(state_dir: str) -> Set[str]:
    """Pull the cwd values recorded in a state directory's transcripts.

    Matches the raw JSON text instead of parsing each record -- these files are
    large and a full parse of every line costs several times more for one field.
    """
    out: Set[str] = set()
    for sid in session_ids(state_dir):
        try:
            with open(os.path.join(state_dir, sid + ".jsonl"), "r",
                      encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i >= CWD_SCAN_LINES:
                        break
                    match = _CWD_RE.search(line)
                    if not match:
                        continue
                    try:
                        cwd = json.loads('"' + match.group(1) + '"')
                    except ValueError:
                        continue
                    if cwd.startswith("/"):
                        out.add(norm(cwd))
        except OSError:
            continue
    return out


def live_sessions(layout: Layout) -> Iterator[Dict[str, Any]]:
    """Session records Claude Code writes per running process."""
    if not os.path.isdir(layout.sessions):
        return
    for name in sorted(os.listdir(layout.sessions)):
        if not name.endswith(".json"):
            continue
        try:
            rec = json.loads(read_text(os.path.join(layout.sessions, name)) or "")
        except (ValueError, AttributeError):
            continue
        if isinstance(rec, dict):
            yield rec


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


class Plan:
    def __init__(self, args: argparse.Namespace, layout: Layout, log: Log,
                 src: str, dst: str, batched: bool = False) -> None:
        self.args = args
        self.layout = layout
        self.log = log
        self.src = norm(src)
        self.dst = norm(dst)
        self.batched = batched
        # ~/.claude.json lives in the home dir; deriving it this way keeps
        # --config overrides self-consistent.
        self.home = layout.home
        self.mappings: List[Tuple[str, str]] = []      # (old project path, new)
        self.state_moves: List[Tuple[str, str]] = []   # (old state dir, new)
        self.targets: List[str] = []                   # files naming the old path
        self.rewriter = Rewriter()
        self.move_files = not args.state_only
        self.blockers: List[Tuple[str, bool]] = []     # (message, fatal)

    # -- build ------------------------------------------------------------

    def build(self, projects: Set[str]) -> None:
        """`projects` is passed in rather than looked up here: it costs a scan
        of every transcript, and a batch of moves shares one answer."""
        self._resolve_destination()
        self._resolve_mappings(projects)
        self._build_rewriter()
        self._check_files()
        self.targets = self._discover_targets()
        self._check_live_sessions()
        self._check_collisions(projects)
        self._check_state_dest()

    def _resolve_destination(self) -> None:
        """`mv` semantics: an existing directory is a container, so
        `claude-move ~/dev/api ~/work` lands the project at ~/work/api.

        For a single move this is not applied when the folder is already at the
        destination (--state-only, or auto-detected because the source is gone):
        there the destination names the project itself, and treating it as a
        container would nest it one level deeper than the user meant.
        """
        landed = os.path.join(self.dst, os.path.basename(self.src))

        if self.batched:
            # Several sources can only mean a container -- main requires the
            # destination to be an existing directory -- so every project lands
            # at <dst>/<its own name>.  This holds under --state-only and when
            # the folder is already gone; without it, a project whose folder had
            # been moved elsewhere would rewrite its state onto the container.
            self.dst = landed
            return

        if not os.path.isdir(self.src):
            # The folder is already gone, so it was moved by hand -- and `mv`
            # into an existing directory leaves it at <dst>/<name>, not at
            # <dst>.  Prefer that when it is there, or the state would be
            # rewritten to point at the container.
            if os.path.isdir(landed):
                self.log.warn(
                    f"{self.src} is gone and {landed} exists -- assuming the folder was "
                    f"moved *into* {self.dst}.\n"
                    f"           pass the full path if {self.dst} is itself the project.")
                self.dst = landed
            return

        if self.args.state_only or not os.path.isdir(self.dst):
            return

        container, self.dst = self.dst, landed
        note = (f"{container} is an existing directory -- "
                f"moving into it as {os.path.basename(self.dst)}")
        if os.listdir(container):
            self.log.info(f"note: {note}")
        else:
            # an empty directory looks like a rename target, so say it louder
            self.log.warn(note)

    def _resolve_mappings(self, projects: Set[str]) -> None:
        self.mappings.append((self.src, self.dst))
        if self.args.subprojects:
            nested = sorted(p for p in projects if p != self.src and is_under(p, self.src))
            self.mappings.extend((p, remap(p, self.src, self.dst)) for p in nested)
        for old, new in self.mappings:
            old_state = self.layout.state_dir(old)
            new_state = self.layout.state_dir(new)
            if os.path.isdir(old_state) and old_state != new_state:
                self.state_moves.append((old_state, new_state))

    def _build_rewriter(self) -> None:
        for old, new in self.mappings:
            for spelling, replacement in project_pairs(self.layout, old, new):
                self.rewriter.add(spelling, replacement)

    # -- target discovery -------------------------------------------------

    def _discover_targets(self) -> List[str]:
        """Every text file outside the moved state dirs that names the old
        path.  Found by scanning rather than by an allowlist of state files
        we happen to know about, so directories Claude Code adds in future
        versions are covered without a code change.  The old absolute path is
        a long, highly specific needle -- a file under ~/.claude containing it
        is by construction a reference to this project."""
        return [path for path in self._candidates()
                if self.rewriter.touches(read_text(path) or "")]

    def _candidates(self) -> Iterator[str]:
        seen: Set[str] = set()

        def offer(path: str) -> Iterator[str]:
            if path not in seen and os.path.isfile(path):
                seen.add(path)
                yield path

        def walk(root: str) -> Iterator[str]:
            for parent, _dirs, files in os.walk(root):
                for name in sorted(files):
                    yield from offer(os.path.join(parent, name))

        yield from offer(self.layout.config)
        for entry in sorted(os.listdir(self.layout.dir)):
            if entry in SKIP_TOP_LEVEL:
                continue
            path = os.path.join(self.layout.dir, entry)
            yield from (walk(path) if os.path.isdir(path) else offer(path))

        # the project's own .claude/ travels with the folder, but its settings
        # and hooks can hold absolute paths
        if self.args.project_settings:
            settings = os.path.join(self.src if self.move_files else self.dst, ".claude")
            if os.path.isdir(settings):
                yield from walk(settings)

    # -- safety checks ----------------------------------------------------

    def block(self, msg: str, fatal: bool = False) -> None:
        """A fatal blocker makes execution impossible, so --force must not
        skip it -- forcing past a missing source only crashes later."""
        self.blockers.append((msg, fatal))

    def _check_files(self) -> None:
        src_exists = os.path.isdir(self.src)
        dst_exists = os.path.isdir(self.dst)

        if self.move_files and not src_exists and dst_exists:
            self.log.info(f"note: {self.src} is gone and {self.dst} exists -- "
                          f"assuming the folder was already moved (--state-only)")
            self.move_files = False

        if self.move_files:
            if not src_exists:
                self.block(f"source directory does not exist: {self.src}", fatal=True)
            # lexists, not exists: a dangling symlink is not a directory but
            # still makes the move fail, and exists() follows the link
            if os.path.lexists(self.dst) and not dst_exists:
                self.block(f"destination exists and is not a directory: {self.dst}", fatal=True)
            if src_exists and self.src == self.dst:
                # e.g. moving a project into its own parent directory; the
                # remaining destination checks would only restate this
                self.block(f"source and destination resolve to the same path: {self.src}",
                           fatal=True)
                return
            if dst_exists and os.listdir(self.dst):
                self.block(
                    f"destination already exists and is not empty: {self.dst}\n"
                    f"           move the folder yourself, then re-run with --state-only",
                    fatal=True)
            if src_exists and self.dst != self.src and is_under(self.dst, self.src):
                self.block("destination is inside the source directory", fatal=True)
        elif not dst_exists:
            self.log.warn(f"destination folder does not exist yet: {self.dst}")

    def _check_live_sessions(self) -> None:
        touched = {old for old, _ in self.mappings}
        for rec in live_sessions(self.layout):
            cwd, pid = rec.get("cwd"), rec.get("pid")
            if not isinstance(cwd, str) or not isinstance(pid, int):
                continue
            if not any(is_under(norm(cwd), path) for path in touched) or not pid_alive(pid):
                continue
            name = rec.get("name") or rec.get("sessionId", "?")
            self.block(
                f"a Claude Code session is live in this project (pid {pid}, {name}).\n"
                f"           quit it first -- it holds ~/.claude.json in memory and will\n"
                f"           write the old paths back when it exits.  Override with --force.")

    def _check_collisions(self, projects: Set[str]) -> None:
        """The encoded name is lossy, so two distinct project paths can share
        one state directory.  Moving it would drag the other project's
        transcripts along."""
        by_encoded: Dict[str, Set[str]] = {}
        for path in projects:
            by_encoded.setdefault(encode_path(path), set()).add(path)
        for old, new in self.mappings:
            shared = by_encoded.get(encode_path(old), set()) - {old}
            if shared:
                self.log.warn(
                    f"{old} shares its state directory with: {', '.join(sorted(shared))}\n"
                    f"           ({COLLAPSE_NOTE}).  Their transcripts "
                    f"will move too.")
            clashes = by_encoded.get(encode_path(new), set()) - {new, old}
            if clashes:
                self.log.warn(f"the new path collides with existing project(s): "
                              f"{', '.join(sorted(clashes))}")

    def _check_state_dest(self) -> None:
        for _, new_state in self.state_moves:
            if os.path.isdir(new_state) and os.listdir(new_state) and not self.args.merge:
                self.block(
                    f"state already exists for the new path: {new_state}\n"
                    f"           (you have run Claude there already).  Re-run with --merge "
                    f"to combine them.")

    # -- description ------------------------------------------------------

    def shorten(self, path: str) -> str:
        return tilde(path, self.home) or path

    def describe_compact(self) -> None:
        """Two lines for a batch listing, where the full form would bury it."""
        bits = []
        if not self.move_files:
            bits.append("folder already in place")
        sessions = sum(len(session_ids(old)) for old, _ in self.state_moves)
        memory = sum(len(memory_files(old)) for old, _ in self.state_moves)
        bits.append(f"{sessions} transcript(s), {memory} memory file(s)"
                    if self.state_moves else "no state recorded yet")
        if len(self.mappings) > 1:
            bits.append(f"{len(self.mappings) - 1} subproject(s)")
        bits.append(f"{len(self.targets)} file(s) rewritten")
        self.log.step(f"{self.shorten(self.src)}  ->  {self.shorten(self.dst)}")
        self.log.step(f"    {'; '.join(bits)}")

    def describe(self) -> None:
        log = self.log
        log.info()
        log.info("Project")
        log.info(f"  from  {self.src}")
        log.info(f"  to    {self.dst}")

        log.info()
        log.info("Files")
        log.step(f"move {self.src}  ->  {self.dst}" if self.move_files
                 else "(skipped -- folder already in place)")

        log.info()
        log.info("Claude state")
        if not self.state_moves:
            log.step("no state directory found -- nothing recorded for this project yet")
        for old_state, new_state in self.state_moves:
            verb = "merge into" if os.path.isdir(new_state) and os.listdir(new_state) else "move to"
            log.step(os.path.basename(old_state))
            log.step(f"    {len(session_ids(old_state))} transcript(s), "
                     f"{len(memory_files(old_state))} memory file(s)")
            log.step(f"    {verb} {os.path.basename(new_state)}")

        if len(self.mappings) > 1:
            log.info()
            log.info("Subprojects also remapped")
            for old, new in self.mappings[1:]:
                log.step(f"{old}  ->  {new}")

        log.info()
        log.info(f"References rewritten in {len(self.targets)} file(s) outside the state dir")
        for target in self.targets[:12]:
            log.step(self.shorten(target))
        if len(self.targets) > 12:
            log.step(f"... and {len(self.targets) - 12} more")
        log.step("plus the moved transcripts and memory files")


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------


class Mover:
    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self.args = plan.args
        self.layout = plan.layout
        self.log = plan.log
        self.rewriter = plan.rewriter
        self.writes = FileRewriter(plan.rewriter, plan.log)
        self.backups_renamed = 0
        self.conflicts: List[str] = []

    # -- moves ------------------------------------------------------------

    def move_project_files(self) -> None:
        if not self.plan.move_files:
            return
        parent = os.path.dirname(self.plan.dst)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        if os.path.isdir(self.plan.dst) and not os.listdir(self.plan.dst):
            os.rmdir(self.plan.dst)
        shutil.move(self.plan.src, self.plan.dst)
        self.log.step(f"moved {self.plan.src} -> {self.plan.dst}")

    def move_state_dirs(self) -> None:
        for old_state, new_state in self.plan.state_moves:
            name = f"{os.path.basename(old_state)} -> {os.path.basename(new_state)}"
            if os.path.isdir(new_state) and os.listdir(new_state):
                self._merge_tree(old_state, new_state)
                self.log.step(f"merged {name}")
            else:
                if os.path.isdir(new_state):
                    os.rmdir(new_state)
                os.makedirs(os.path.dirname(new_state), exist_ok=True)
                shutil.move(old_state, new_state)
                self.log.step(f"moved {name}")

    def _merge_tree(self, src: str, dst: str) -> None:
        """Copy src into dst.  Existing files win; the incoming version is kept
        beside them with a .migrated suffix so nothing is silently lost."""
        for root, _dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            target_dir = dst if rel == "." else os.path.join(dst, rel)
            os.makedirs(target_dir, exist_ok=True)
            for name in files:
                source = os.path.join(root, name)
                target = os.path.join(target_dir, name)
                if os.path.exists(target):
                    if name == "MEMORY.md":
                        self._merge_memory_index(source, target)
                        continue
                    stem, ext = os.path.splitext(name)
                    target = os.path.join(target_dir, f"{stem}.migrated{ext}")
                    self.conflicts.append(os.path.relpath(target, self.layout.projects))
                shutil.copy2(source, target)
        shutil.rmtree(src, ignore_errors=True)

    def _merge_memory_index(self, source: str, target: str) -> None:
        """MEMORY.md is an index of one-line pointers -- union it rather than
        stranding the incoming copy in a .migrated file."""
        incoming, existing = read_text(source), read_text(target)
        if incoming is None or existing is None:
            return
        have = {line.strip() for line in existing.splitlines() if line.strip()}
        added = [line for line in incoming.splitlines()
                 if line.strip() and line.strip() not in have]
        if not added:
            return
        text = existing if existing.endswith("\n") else existing + "\n"
        write_text_atomic(target, text + "\n".join(added) + "\n")
        self.log.step(f"merged {len(added)} line(s) into memory/MEMORY.md")

    # -- rewriting --------------------------------------------------------

    def rewrite_everything(self) -> None:
        for target in self.plan.targets:
            # targets inside the project moved along with it
            path = (remap(target, self.plan.src, self.plan.dst)
                    if is_under(target, self.plan.src) else target)
            if not os.path.isfile(path):
                continue
            if path == self.layout.config:
                self._rewrite_config()
            elif path.endswith(".jsonl"):
                self.writes.jsonl(path)
            elif path.endswith(".json"):
                self.writes.json(path)
            else:
                self.writes.text(path)

        for _old_state, new_state in self.plan.state_moves:
            self._rewrite_state_dir(new_state)

    def _rewrite_state_dir(self, state_dir: str) -> None:
        """Transcripts and memory files, then the file-history blobs whose
        names encode a path that just changed."""
        renames: Dict[str, str] = {}
        for root, _dirs, files in os.walk(state_dir):
            for name in sorted(files):
                path = os.path.join(root, name)
                if name.endswith(".jsonl"):
                    renames.update(self.writes.jsonl(path))
                else:
                    self.writes.text(path)

        for sid in session_ids(state_dir):
            blobs = os.path.join(self.layout.file_history, sid)
            for old_name, new_name in renames.items():
                source = os.path.join(blobs, old_name)
                target = os.path.join(blobs, new_name)
                if os.path.exists(source) and not os.path.exists(target):
                    os.rename(source, target)
                    self.backups_renamed += 1

    def _rewrite_config(self) -> None:
        """~/.claude.json: rename the projects key (merging if the new key
        already exists -- a plain rewrite would silently drop one entry), then
        let the generic sweep handle every other reference in the file."""
        path = self.layout.config
        blob = read_text(path)
        if blob is None:
            self.log.warn(f"could not read {path}")
            return
        try:
            cfg = json.loads(blob)
        except ValueError as exc:
            self.log.warn(f"could not parse {path}: {exc}")
            return

        projects = cfg.get("projects")
        if isinstance(projects, dict):
            for old, new in self.plan.mappings:
                if old not in projects:
                    continue
                entry = projects.pop(old)
                if new in projects:
                    projects[new] = merge_settings(entry, projects[new])
                    self.log.step(f"merged config entry for {new}")
                else:
                    projects[new] = entry
                    self.log.step(f"config: {old} -> {new}")

        before = self.rewriter.hits
        cfg = self.rewriter.obj(cfg)
        if self.rewriter.hits != before:
            self.writes.files_rewritten += 1
        write_json_atomic(path, cfg)
        self.writes.note_stale(path, json.dumps(cfg, ensure_ascii=False))

    # -- verify -----------------------------------------------------------

    def verify(self) -> List[str]:
        """Anything still pointing at the old location.  The rewrite pass
        already recorded residual matches as it wrote; only the directory
        checks are left."""
        return self.writes.stale + [
            f"{self.layout.state_dir(old)}  (old state dir still present)"
            for old, _ in self.plan.mappings
            if os.path.isdir(self.layout.state_dir(old))]


def merge_settings(incoming: Any, existing: Any) -> Any:
    """Merge two ~/.claude.json project entries.  Lists are unioned (so
    allowedTools from both survive); on scalars the destination wins."""
    if isinstance(incoming, dict) and isinstance(existing, dict):
        out = dict(existing)
        for key, value in incoming.items():
            out[key] = merge_settings(value, existing[key]) if key in existing else value
        return out
    if isinstance(incoming, list) and isinstance(existing, list):
        return existing + [item for item in incoming if item not in existing]
    return existing


# ---------------------------------------------------------------------------
# batches
# ---------------------------------------------------------------------------


class Batch:
    """One or more moves sharing a destination, planned together and then
    executed in order.

    Everything is planned before anything is executed, so a problem with the
    last project stops the first from being touched -- one bad path in a
    wildcard should not leave half a batch migrated.
    """

    def __init__(self, args: argparse.Namespace, layout: Layout, log: Log,
                 sources: List[str], dst: str) -> None:
        self.args = args
        self.layout = layout
        self.log = log
        self.dst = norm(dst)
        self.plans = [Plan(args, layout, log, src, dst, len(sources) > 1)
                      for src in sources]
        self.blockers: List[Tuple[str, bool]] = []
        self.done: List[Mover] = []   # filled by run(), readable after a failure

    def build(self, projects: Set[str]) -> None:
        for plan in self.plans:
            plan.build(projects)
        self._check_batch()
        self.blockers.extend(b for plan in self.plans for b in plan.blockers)

    def _check_batch(self) -> None:
        """Problems that only exist between projects, so no single plan sees
        them.  Every check below is naturally vacuous for a single plan."""
        for i, plan in enumerate(self.plans):
            for other in self.plans[:i]:
                # both directions: the inner project may be named first, and
                # then the outer plan's own subproject remapping collides with
                # the move the inner one already made
                pair = ((plan.src, other.src) if is_under(plan.src, other.src)
                        else (other.src, plan.src) if is_under(other.src, plan.src)
                        else None)
                if pair:
                    self.block(f"{pair[0]} is inside {pair[1]} -- it would be moved "
                               f"twice.\n           drop the inner one; it is remapped "
                               f"as a subproject anyway.", fatal=True)

        for dst, group in sorted(self._group(lambda plan: plan.dst).items()):
            if len(group) > 1:
                self.block(f"these would all land on {dst}:\n           "
                           + "\n           ".join(sorted(p.src for p in group)) +
                           "\n           give them distinct names first.", fatal=True)

        for _, group in sorted(self._group(lambda plan: encode_path(plan.dst)).items()):
            dsts = sorted({plan.dst for plan in group})
            if len(dsts) > 1:
                # not fatal: the move itself works, but the two projects end up
                # sharing one state directory, which is rarely what was meant
                self.block(f"these new paths share a single state directory "
                           f"({COLLAPSE_NOTE}):\n           "
                           + "\n           ".join(dsts) +
                           "\n           their transcripts would be combined.")

    def _group(self, key: Callable[[Plan], str]) -> Dict[str, List[Plan]]:
        out: Dict[str, List[Plan]] = {}
        for plan in self.plans:
            out.setdefault(key(plan), []).append(plan)
        return out

    def block(self, msg: str, fatal: bool = False) -> None:
        self.blockers.append((msg, fatal))

    # -- description ------------------------------------------------------

    def describe(self) -> None:
        if len(self.plans) == 1:
            self.plans[0].describe()
            return
        self.log.info()
        self.log.info(f"{len(self.plans)} projects  ->  {self.dst}")
        for plan in self.plans:
            self.log.info()
            plan.describe_compact()

    # -- execution --------------------------------------------------------

    def backup(self) -> Optional[str]:
        """One safety copy for the whole batch, taken before any of it runs."""
        if self.args.no_backup:
            return None
        root = backup_root(self.layout)
        files = os.path.join(root, "files")
        os.makedirs(files, exist_ok=True)

        targets = {self.layout.config}
        targets.update(t for plan in self.plans for t in plan.targets)
        for path in sorted(targets):
            if os.path.isfile(path):
                shutil.copy2(path, unique(os.path.join(files, os.path.basename(path))))

        for plan in self.plans:
            for old_state, _ in plan.state_moves:
                shutil.copytree(old_state,
                                os.path.join(root, "projects", os.path.basename(old_state)),
                                dirs_exist_ok=True)
                for sid in session_ids(old_state):
                    blobs = os.path.join(self.layout.file_history, sid)
                    if os.path.isdir(blobs):
                        shutil.copytree(blobs, os.path.join(root, "file-history", sid),
                                        dirs_exist_ok=True)
        return root

    def run(self) -> None:
        """Execute each plan in turn, recording the movers that finished in
        self.done so a failure partway can still report what did land."""
        for plan in self.plans:
            if len(self.plans) > 1:
                self.log.info()
                self.log.info(plan.shorten(plan.src))
            mover = Mover(plan)
            try:
                mover.move_project_files()
                mover.move_state_dirs()
                mover.rewrite_everything()
            except Exception as exc:  # noqa: BLE001 - reported, then re-raised
                self.log.error(f"failed partway through, on {plan.src}: {exc}")
                raise
            self.done.append(mover)

    # -- result -----------------------------------------------------------

    def report(self) -> None:
        log, movers = self.log, self.done
        log.info()
        log.info("Done")
        if len(movers) == 1:
            log.step(movers[0].plan.dst)
            log.step(f"state dir: {self.layout.state_dir(movers[0].plan.dst)}")
        else:
            for mover in movers:
                log.step(mover.plan.dst)
        log.step(f"{sum(m.writes.files_rewritten for m in movers)} file(s) rewritten, "
                 f"{sum(m.backups_renamed for m in movers)} file-history backup(s) renamed")

        conflicts = [c for m in movers for c in m.conflicts]
        if conflicts:
            log.info()
            log.info("Merge conflicts kept side by side (review these):")
            for item in conflicts:
                log.step(item)

        stale = [s for m in movers for s in m.verify()]
        if stale:
            log.info()
            log.info("Still referencing the old path (probably harmless, e.g. quoted text):")
            for item in stale[:20]:
                log.step(item)
            if len(stale) > 20:
                log.step(f"... and {len(stale) - 20} more")

        log.info()
        if len(movers) == 1:
            log.info(f"Start Claude there with:  cd {movers[0].plan.dst} && claude --continue")
        else:
            log.info("Start Claude in any of them with:  cd <path> && claude --continue")


# ---------------------------------------------------------------------------
# export / import: the same state, carried to another machine
# ---------------------------------------------------------------------------


def short(path: str, home: str) -> str:
    """A path with its home directory folded to ~, for display."""
    return tilde(path, home) or path


def live_project_paths(layout: Layout) -> Set[str]:
    """Project paths with a Claude Code process currently running in them.

    A live session holds ~/.claude.json in memory and writes it back on exit,
    which would undo half an import.
    """
    out: Set[str] = set()
    for rec in live_sessions(layout):
        pid, cwd = rec.get("pid"), rec.get("cwd")
        if isinstance(cwd, str) and isinstance(pid, int) and pid_alive(pid):
            out.add(norm(cwd))
    return out


BUNDLE_VERSION = 1


# The portable half of a ~/.claude.json project entry.  Everything else in
# there is this machine's telemetry -- lastCost, lastFpsAverage, lastSessionId,
# lastVersionBase -- which would be actively misleading on the new machine.
PORTABLE_CONFIG_KEYS = (
    "allowedTools",
    "ignorePatterns",
    "mcpServers",
    "mcpContextUris",
    "enabledMcpjsonServers",
    "disabledMcpjsonServers",
    "hasTrustDialogAccepted",
    "hasClaudeMdExternalIncludesApproved",
    "hasCompletedProjectOnboarding",
    "exampleFiles",
    "exampleFilesGeneratedAt",
)


def config_projects(config: Any) -> Dict[str, Any]:
    """The projects map inside a parsed ~/.claude.json.

    Returns the live inner dict, so a caller removing a key from it and
    writing `config` back works -- and an empty one when the file parses to
    anything not shaped like a config, which must not raise halfway through
    a run that has already deleted something.
    """
    entries = config.get("projects") if isinstance(config, dict) else None
    return entries if isinstance(entries, dict) else {}


def read_json(path: str, default: Any = None) -> Any:
    blob = read_text(path)
    if blob is None:
        return default
    try:
        return json.loads(blob)
    except ValueError:
        return default


def human(size: float) -> str:
    for unit in ("B", "K", "M"):
        if size < 1024:
            suffix = "" if unit == "B" else unit
            return f"{round(size)}{suffix}"
        size /= 1024.0
    return f"{round(size)}G"


def tree_stat(path: str) -> Tuple[int, float]:
    """Total bytes under `path`, and when anything in it was last written."""
    total, newest = 0, 0.0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                info = os.stat(os.path.join(root, name))
            except OSError:
                continue
            total += info.st_size
            newest = max(newest, info.st_mtime)
    return total, newest


def tree_size(path: str) -> int:
    return tree_stat(path)[0]


def same_bytes(a: str, b: str) -> bool:
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


class Parts:
    """Which kinds of state a run carries.  Everything, less what was waived."""

    def __init__(self, args: argparse.Namespace) -> None:
        memory_only = getattr(args, "memory_only", False)
        self.memory = True
        self.sessions = not memory_only and not args.no_sessions
        self.config = not memory_only and not args.no_config
        self.history = not memory_only and not args.no_history
        # file-history blobs belong to sessions; without the transcripts that
        # reference them they are unreachable
        self.file_history = self.sessions and not args.no_file_history

    def summary(self) -> str:
        names = [name for name, on in (("memory", self.memory),
                                       ("sessions", self.sessions),
                                       ("file-history", self.file_history),
                                       ("config", self.config),
                                       ("shell history", self.history)) if on]
        return ", ".join(names)


def copy_state_dir(src: str, dst: str, parts: Parts) -> None:
    """Copy one project's state directory, minus the half being skipped."""
    os.makedirs(dst, exist_ok=True)
    for name in sorted(os.listdir(src)):
        if not (parts.memory if is_memory_entry(name) else parts.sessions):
            continue
        source, target = os.path.join(src, name), os.path.join(dst, name)
        try:
            if os.path.isdir(source):
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target)
        except OSError as exc:
            raise OSError(f"could not copy {source}: {exc}")


def history_entries(layout: Layout) -> List[Tuple[str, Optional[str]]]:
    """Every line of ~/.claude/history.jsonl, paired with the project it names.

    A blank or unparseable line is kept with no project rather than dropped, so
    that reading the file in order to remove one project's lines can put the
    rest back exactly as they were.
    """
    out: List[Tuple[str, Optional[str]]] = []
    if not os.path.isfile(layout.history):
        return out
    try:
        with open(layout.history, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.endswith("\n"):
                    line += "\n"
                try:
                    rec = json.loads(line) if line.strip() else None
                except ValueError:
                    rec = None
                project = rec.get("project") if isinstance(rec, dict) else None
                out.append((line, norm(str(project)) if project else None))
    except OSError:
        pass
    return out


def history_lines_for(layout: Layout, paths: Set[str]) -> List[str]:
    """Lines of ~/.claude/history.jsonl belonging to the exported projects."""
    return [line for line, project in history_entries(layout)
            if project in paths]


def filter_config(entry: Any, keep_all: bool) -> Any:
    if keep_all or not isinstance(entry, dict):
        return entry
    return {k: v for k, v in entry.items() if k in PORTABLE_CONFIG_KEYS}


def stage_globals(staged: str, layout: Layout, log: Log) -> None:
    """User-level files that are not tied to any one project."""
    dst = os.path.join(staged, "global")
    os.makedirs(dst, exist_ok=True)
    for name in ("settings.json", "CLAUDE.md"):
        src = os.path.join(layout.dir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dst, name))
            log.step(f"global: {name}")
    for name in ("agents", "commands", "skills"):
        src = os.path.join(layout.dir, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dst, name), dirs_exist_ok=True)
            log.step(f"global: {name}/")


def matches_pattern(path: str, pattern: str) -> bool:
    """One project path against one command-line pattern: an exact path, a
    bare directory name ("api" matches ~/dev/api), or a shell-style pattern."""
    literal = norm(pattern)
    if _GLOB_MAGIC.search(pattern):
        return glob_match(path, literal)
    return path == literal or os.path.basename(path) == pattern


def select(projects: Dict[str, str], patterns: List[str],
           log: Log) -> Dict[str, str]:
    """Filter discovered projects by the command line.

    A pattern is an exact path, a bare directory name ("api" matches ~/dev/api),
    or a shell-style pattern.  No patterns means every project.  Unlike a move,
    a pattern that matches nothing is a warning rather than an error -- an
    export of everything else is still worth writing.
    """
    if not patterns:
        return projects
    chosen: Dict[str, str] = {}
    for pattern in patterns:
        hits = [p for p in projects if matches_pattern(p, pattern)]
        if not hits:
            log.warn(f"no project matches {pattern!r}")
        for hit in hits:
            chosen[hit] = projects[hit]
    return chosen


def do_export(args: argparse.Namespace, layout: Layout, log: Log) -> int:
    parts = Parts(args)
    projects = known_projects(layout, log)
    projects = select(projects, args.projects, log)
    projects = {p: s for p, s in projects.items() if os.path.isdir(s)}
    if not projects:
        log.error("nothing to export")
        return 1

    cfg = read_json(layout.config, {}) or {}
    cfg_projects = cfg.get("projects") or {}
    home = layout.home

    entries: List[Dict[str, Any]] = []
    for path in sorted(projects):
        state = projects[path]
        sids = session_ids(state) if parts.sessions else []
        entries.append({
            "path": path,
            "display": short(path, home),
            "key": os.path.basename(state),
            "memory": memory_files(state) if parts.memory else [],
            "sessions": sids,
            "has_config": path in cfg_projects and parts.config,
            "bytes": 0,   # filled in once staged, so it counts what is packed
        })

    # a project can contribute nothing to this particular bundle -- no memory
    # under --memory-only, say -- and carrying it would only create an empty
    # state directory on the other machine
    skipped = [e for e in entries
               if not (e["memory"] or e["sessions"] or e["has_config"])]
    entries = [e for e in entries if e not in skipped]
    if not entries:
        log.error("none of the selected projects have anything to export")
        return 1

    log.info(f"Exporting {len(entries)} project(s): {parts.summary()}")
    for entry in entries:
        extra = ", config" if entry["has_config"] else ""
        log.step(f"{entry['display']:<46} {len(entry['memory']):2d} memory, "
                 f"{len(entry['sessions']):2d} session(s){extra}")
    for entry in skipped:
        log.step(f"{entry['display']:<46} nothing to carry, skipped")
    if args.dry_run:
        log.info()
        log.info("Dry run -- nothing written.")
        return 0

    out = norm(args.out or f"claude-state-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
    staged = tempfile.mkdtemp(prefix="claude-move-")
    try:
        history = history_lines_for(layout, set(projects)) if parts.history else []
        config_slice: Dict[str, Any] = {}

        for entry in entries:
            path, key = entry["path"], entry["key"]
            packed = os.path.join(staged, "projects", key)
            copy_state_dir(projects[path], packed, parts)
            entry["bytes"] = tree_size(packed)
            if parts.file_history:
                for sid in entry["sessions"]:
                    blobs = os.path.join(layout.file_history, sid)
                    if os.path.isdir(blobs):
                        shutil.copytree(blobs, os.path.join(staged, "file-history", sid),
                                        dirs_exist_ok=True)
            if entry["has_config"]:
                config_slice[path] = filter_config(cfg_projects[path], args.config_all)

        if config_slice:
            os.makedirs(os.path.join(staged, "config"), exist_ok=True)
            write_json_atomic(os.path.join(staged, "config", "projects.json"),
                              config_slice)
        if history:
            os.makedirs(os.path.join(staged, "history"), exist_ok=True)
            write_text_atomic(os.path.join(staged, "history", "history.jsonl"),
                              "".join(history))
        if args.globals:
            stage_globals(staged, layout, log)

        manifest = {
            "bundle_version": BUNDLE_VERSION,
            "tool": "claude-move",
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source": {
                "home": home,
                "user": os.path.basename(home),
                "platform": platform.system(),
                "hostname": platform.node(),
                "claude_dir": layout.dir,
            },
            "parts": {"memory": parts.memory, "sessions": parts.sessions,
                      "file_history": parts.file_history, "config": parts.config,
                      "history": parts.history, "globals": bool(args.globals),
                      "config_all": bool(args.config_all)},
            "history_lines": len(history),
            "projects": entries,
        }
        write_json_atomic(os.path.join(staged, "manifest.json"), manifest)

        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with tarfile.open(out, "w:gz") as tar:
            for name in sorted(os.listdir(staged)):
                tar.add(os.path.join(staged, name), arcname=name)
    finally:
        shutil.rmtree(staged, ignore_errors=True)

    log.info()
    log.info(f"Wrote {out} ({human(os.path.getsize(out))})")
    if parts.sessions:
        log.step("contains full session transcripts -- treat the bundle as "
                 "private, and encrypt it if it leaves your control")
    log.step(f"on the other machine: claude-move.py import {os.path.basename(out)}")
    return 0


def safe_extract(bundle: str, dest: str) -> None:
    """Unpack a bundle, refusing members that escape `dest`.

    The bundle came from another machine, so its member names are untrusted:
    an absolute path or a "../" would otherwise write anywhere on this one.
    (tarfile's own `filter="data"` only exists from Python 3.12.)
    """
    root = os.path.realpath(dest)
    with tarfile.open(bundle, "r:*") as tar:
        for member in tar.getmembers():
            if member.issym() or member.islnk():
                raise ValueError(f"bundle contains a link: {member.name}")
            target = os.path.realpath(os.path.join(root, member.name))
            if not (target == root or target.startswith(root + os.sep)):
                raise ValueError(
                    f"bundle member escapes the archive: {member.name}")
        tar.extractall(root)


def load_manifest(staged: str) -> Dict[str, Any]:
    manifest = read_json(os.path.join(staged, "manifest.json"))
    if not isinstance(manifest, dict) or manifest.get("tool") != "claude-move":
        raise ValueError("not a claude-move bundle (no usable manifest.json)")
    version = manifest.get("bundle_version")
    if not isinstance(version, int) or version > BUNDLE_VERSION:
        raise ValueError(f"bundle version {version} is newer than this script "
                         f"(supports {BUNDLE_VERSION}) -- update claude-move.py")
    return manifest


def do_inspect(args: argparse.Namespace, log: Log) -> int:
    staged = tempfile.mkdtemp(prefix="claude-move-")
    try:
        safe_extract(norm(args.bundle), staged)
        manifest = load_manifest(staged)
    finally:
        shutil.rmtree(staged, ignore_errors=True)

    src = manifest["source"]
    bundle = norm(args.bundle)
    log.info(f"{bundle}  ({human(os.path.getsize(bundle))})")
    log.info(f"  created   {manifest.get('created', '?')}")
    log.info(f"  from      {src.get('user')} on {src.get('hostname')} "
             f"({src.get('platform')}), home {src.get('home')}")
    carried = ", ".join(k for k, v in manifest["parts"].items() if v)
    log.info(f"  contains  {carried}")
    log.info()
    log.info(f"  {'PROJECT':<46} {'MEMORY':<8} {'SESSIONS':<9} SIZE")
    for entry in manifest["projects"]:
        log.info(f"  {entry['display']:<46} {len(entry['memory']):<8} "
                 f"{len(entry['sessions']):<9} {human(entry.get('bytes', 0))}")
    if manifest.get("history_lines"):
        log.info()
        log.info(f"  {manifest['history_lines']} shell-history line(s)")
    return 0


class Mapping:
    """Where each project in the bundle lands on this machine."""

    def __init__(self, manifest: Dict[str, Any], args: argparse.Namespace,
                 layout: Layout, log: Log) -> None:
        self.log = log
        self.layout = layout
        self.src_home = norm(manifest["source"]["home"])
        self.src_claude = norm(manifest["source"].get("claude_dir")
                               or os.path.join(self.src_home, ".claude"))
        self.dst_home = norm(args.home) if args.home else layout.home
        self.explicit = self._parse_maps(args.map or [])
        self.into = norm(args.into) if args.into else None
        self.pairs: List[Tuple[Dict[str, Any], str]] = []
        self.blocked: List[str] = []
        for entry in manifest["projects"]:
            self.pairs.append((entry, self._target(entry["path"])))
        self._check_collisions()

    def _parse_maps(self, raw: List[str]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for item in raw:
            old, sep, new = item.partition("=")
            if not sep or not old or not new:
                raise ValueError(f"--map wants OLD=NEW, got {item!r}")
            out[old if not old.startswith(("/", "~")) else norm(old)] = norm(new)
        return out

    def _target(self, src: str) -> str:
        if src in self.explicit:
            return self.explicit[src]
        if os.path.basename(src) in self.explicit:
            return self.explicit[os.path.basename(src)]
        if self.into:
            return os.path.join(self.into, os.path.basename(src))
        if is_under(src, self.src_home):
            return remap(src, self.src_home, self.dst_home)
        # outside the source home there is nothing to key the remap off, so the
        # path is kept verbatim -- right when both machines mount it the same
        # way, wrong otherwise, hence the warning
        self.log.warn(f"{src} is outside the source home; keeping the path "
                      f"as-is (use --map {src}=/new/path to place it)")
        return src

    def _check_collisions(self) -> None:
        """Two projects landing on one path, or on one state-directory name.

        The directory-name encoding is lossy -- my_app, my app and my-app all
        become my-app -- so distinct target paths can still collide inside
        ~/.claude and quietly merge two projects' memory.
        """
        by_path: Dict[str, List[str]] = {}
        by_key: Dict[str, List[str]] = {}
        for entry, dst in self.pairs:
            by_path.setdefault(dst, []).append(entry["display"])
            by_key.setdefault(encode_path(dst), []).append(dst)
        for dst, names in sorted(by_path.items()):
            if len(names) > 1:
                listed = ", ".join(names)
                self.blocked.append(f"{listed} all map to {dst}")
        for key, targets in sorted(by_key.items()):
            distinct = sorted(set(targets))
            if len(distinct) > 1:
                listed = " and ".join(distinct)
                self.blocked.append(f"{listed} collapse to the same state dir "
                                    f"name {key}")

    def rewriter(self) -> Rewriter:
        """Substitutions to apply to every file being imported.

        Ordered longest-first inside Rewriter, so a project path wins over the
        home directory that contains it.  The bare home pair is the catch-all
        that fixes references to files outside any project -- notes about
        ~/Downloads, a path in a transcript -- which would otherwise still name
        a directory that does not exist here.
        """
        rw = Rewriter()
        for entry, dst in self.pairs:
            rw.add(entry["path"], dst)
            rw.add(os.path.join(self.src_claude, "projects", entry["key"]),
                   self.layout.state_dir(dst))
        rw.add(self.src_claude, self.layout.dir)
        rw.add(self.src_home, self.dst_home)
        return rw

    def describe(self) -> None:
        same = "this machine" if self.src_home == self.dst_home else "remapped"
        self.log.info(f"From {self.src_home} ({same}) -> {self.dst_home}")
        self.log.info()
        for entry, dst in self.pairs:
            arrow = "  (unchanged)" if entry["path"] == dst else ""
            self.log.info(f"  {entry['display']}")
            self.log.info(f"      -> {short(dst, self.dst_home)}{arrow}")
            state = short(self.layout.state_dir(dst), self.dst_home)
            self.log.info(f"         state: {state}")


class Importer:
    """Rewrites a staged bundle for this machine, then merges it into ~/.claude.

    Everything is rewritten in the staging directory first, so a failure partway
    through leaves ~/.claude untouched apart from whatever already merged -- and
    that much is recoverable from the backup.
    """

    def __init__(self, staged: str, manifest: Dict[str, Any], mapping: Mapping,
                 args: argparse.Namespace, layout: Layout, log: Log) -> None:
        self.staged = staged
        self.manifest = manifest
        self.mapping = mapping
        self.args = args
        self.layout = layout
        self.log = log
        self.rules = mapping.rewriter()
        self.writes = FileRewriter(self.rules, log)
        self.blob_renames: Dict[str, str] = {}
        self.merged = 0
        self.kept_aside: List[str] = []

    # -- safety -----------------------------------------------------------

    def check(self) -> List[str]:
        problems = list(self.mapping.blocked)
        live = live_project_paths(self.layout)
        for _, dst in self.mapping.pairs:
            if dst in live:
                problems.append(f"a Claude Code session is running in {dst} -- "
                                f"quit it first (it rewrites ~/.claude.json "
                                f"on exit)")
        if not os.access(self.layout.dir, os.W_OK):
            problems.append(f"{self.layout.dir} is not writable")
        return problems

    def backup(self) -> Optional[str]:
        """One safety copy of everything the merge can overwrite, taken before
        any of it runs."""
        if self.args.no_backup:
            return None
        root = backup_root(self.layout)
        os.makedirs(root, exist_ok=True)
        if os.path.isfile(self.layout.config):
            shutil.copy2(self.layout.config, os.path.join(root, ".claude.json"))
        if os.path.isfile(self.layout.history):
            shutil.copy2(self.layout.history, os.path.join(root, "history.jsonl"))
        for _, dst in self.mapping.pairs:
            state = self.layout.state_dir(dst)
            if os.path.isdir(state):
                shutil.copytree(state, os.path.join(root, "projects",
                                                    os.path.basename(state)),
                                dirs_exist_ok=True)
        return root

    # -- rewriting --------------------------------------------------------

    def rewrite_staged(self) -> None:
        """Rewrite every path reference in the staging directory."""
        for root, _, files in os.walk(self.staged):
            if os.path.basename(root) == "file-history" or \
                    os.path.basename(os.path.dirname(root)) == "file-history":
                continue  # blobs are file contents, not path references
            for name in files:
                path = os.path.join(root, name)
                if name == "manifest.json":
                    continue
                if name.endswith(".jsonl"):
                    self.blob_renames.update(self.writes.jsonl(path))
                elif name.endswith(".json"):
                    self.writes.json(path)
                else:
                    self.writes.text(path)

    # -- merging ----------------------------------------------------------

    def merge(self) -> None:
        for entry, dst in self.mapping.pairs:
            src = os.path.join(self.staged, "projects", entry["key"])
            if os.path.isdir(src):
                self.merge_tree(src, self.layout.state_dir(dst))
            for sid in entry.get("sessions", []):
                self.merge_blobs(sid)
        self.merge_history()
        self.merge_config()
        self.merge_globals()

    def merge_tree(self, src: str, dst: str) -> None:
        """Copy a rewritten state dir over the existing one without losing what
        is already there: MEMORY.md indexes are unioned, and any other file that
        differs is kept beside the existing one rather than replacing it."""
        os.makedirs(dst, exist_ok=True)
        for root, _, files in os.walk(src):
            rel = os.path.relpath(root, src)
            rel = "" if rel == "." else rel
            os.makedirs(os.path.join(dst, rel), exist_ok=True)
            for name in sorted(files):
                source, target = os.path.join(root, name), os.path.join(dst, rel, name)
                if not os.path.exists(target):
                    shutil.copy2(source, target)
                    self.merged += 1
                elif name == "MEMORY.md":
                    self.merge_memory_index(source, target)
                elif self.args.overwrite:
                    shutil.copy2(source, target)
                    self.merged += 1
                elif not same_bytes(source, target):
                    aside = unique(target + ".incoming")
                    shutil.copy2(source, aside)
                    self.kept_aside.append(aside)

    def merge_memory_index(self, source: str, target: str) -> None:
        """MEMORY.md is an index of one-line pointers -- union it rather than
        stranding the incoming copy in an .incoming file."""
        incoming, existing = read_text(source), read_text(target)
        if incoming is None or existing is None:
            return
        have = {line.strip() for line in existing.splitlines() if line.strip()}
        added = [line for line in incoming.splitlines()
                 if line.strip() and line.strip() not in have]
        if not added:
            return
        text = existing if existing.endswith("\n") else existing + "\n"
        write_text_atomic(target, text + "\n".join(added) + "\n")
        self.merged += 1

    def merge_blobs(self, sid: str) -> None:
        """File-history blobs for one session, renamed to match the paths their
        transcript now points at."""
        src = os.path.join(self.staged, "file-history", sid)
        if not os.path.isdir(src):
            return
        dst = os.path.join(self.layout.file_history, sid)
        os.makedirs(dst, exist_ok=True)
        for name in sorted(os.listdir(src)):
            target = os.path.join(dst, self.blob_renames.get(name, name))
            if not os.path.exists(target):
                shutil.copy2(os.path.join(src, name), target)

    def merge_history(self) -> None:
        """Append the bundle's shell-history lines, skipping ones already here."""
        src = os.path.join(self.staged, "history", "history.jsonl")
        incoming = read_text(src)
        if not incoming:
            return
        seen = set()
        existing = read_text(self.layout.history) or ""
        for line in existing.splitlines():
            if line.strip():
                seen.add(line.strip())
        added = [ln for ln in incoming.splitlines()
                 if ln.strip() and ln.strip() not in seen]
        if not added:
            return
        text = existing if not existing or existing.endswith("\n") else existing + "\n"
        write_text_atomic(self.layout.history, text + "\n".join(added) + "\n")
        self.log.step(f"shell history: +{len(added)} line(s)")

    def merge_config(self) -> None:
        """Merge the per-project entries into ~/.claude.json under their new
        keys, touching only the keys the bundle carries."""
        src = os.path.join(self.staged, "config", "projects.json")
        incoming = read_json(src)
        if not isinstance(incoming, dict) or not incoming:
            return
        cfg = read_json(self.layout.config, {}) or {}
        if not isinstance(cfg, dict):
            self.log.warn(f"{self.layout.config} is not a JSON object -- "
                          f"skipping config merge")
            return
        projects = cfg.setdefault("projects", {})
        changed = 0
        for path, entry in incoming.items():
            if not isinstance(entry, dict):
                continue
            target = projects.setdefault(norm(path), {})
            if isinstance(target, dict):
                target.update(entry)
                changed += 1
        if changed:
            write_json_atomic(self.layout.config, cfg)
            where = short(self.layout.config, self.mapping.dst_home)
            self.log.step(f"config: {changed} project entry/entries merged "
                          f"into {where}")
            if any(e.get("hasTrustDialogAccepted") for e in incoming.values()
                   if isinstance(e, dict)):
                self.log.step("(these projects are marked trusted -- Claude will not "
                              "ask again for them here)")

    def merge_globals(self) -> None:
        """User-level files, if the bundle carries them.  Never overwritten:
        settings.json in particular is this machine's, and merging two of them
        by hand is safer than guessing."""
        src = os.path.join(self.staged, "global")
        if not os.path.isdir(src):
            return
        for name in sorted(os.listdir(src)):
            source, target = os.path.join(src, name), os.path.join(self.layout.dir, name)
            if os.path.isdir(source):
                for root, _, files in os.walk(source):
                    rel = os.path.relpath(root, source)
                    out = os.path.join(target, "" if rel == "." else rel)
                    os.makedirs(out, exist_ok=True)
                    for fname in files:
                        dest = os.path.join(out, fname)
                        if not os.path.exists(dest):
                            shutil.copy2(os.path.join(root, fname), dest)
                self.log.step(f"global: merged {name}/ (existing files kept)")
            elif not os.path.exists(target):
                shutil.copy2(source, target)
                self.log.step(f"global: {name}")
            elif not same_bytes(source, target):
                aside = unique(target + ".incoming")
                shutil.copy2(source, aside)
                self.kept_aside.append(aside)


def prune_staged(staged: str, parts: Parts, keep_globals: bool, log: Log) -> None:
    """Drop the parts of a bundle this run was told not to import."""
    def drop(path: str) -> None:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.unlink(path)

    if not parts.config:
        drop(os.path.join(staged, "config"))
    if not parts.history:
        drop(os.path.join(staged, "history"))
    if not parts.file_history:
        drop(os.path.join(staged, "file-history"))
    if not keep_globals:
        drop(os.path.join(staged, "global"))
    projects = os.path.join(staged, "projects")
    if not os.path.isdir(projects):
        return
    for key in sorted(os.listdir(projects)):
        state = os.path.join(projects, key)
        if not os.path.isdir(state):
            continue
        for name in sorted(os.listdir(state)):
            if not (parts.memory if is_memory_entry(name) else parts.sessions):
                drop(os.path.join(state, name))


def do_import(args: argparse.Namespace, layout: Layout, log: Log) -> int:
    bundle = norm(args.bundle)
    if not os.path.isfile(bundle):
        log.error(f"no such bundle: {bundle}")
        return 1

    parts = Parts(args)
    staged = tempfile.mkdtemp(prefix="claude-move-")
    try:
        safe_extract(bundle, staged)
        manifest = load_manifest(staged)
        mapping = Mapping(manifest, args, layout, log)
        importer = Importer(staged, manifest, mapping, args, layout, log)

        log.info(f"Importing {bundle} ({parts.summary()})")
        log.info()
        mapping.describe()

        problems = importer.check()
        if problems:
            log.info()
            for problem in problems:
                log.error(problem)
            if not args.force:
                log.info()
                log.error("nothing was written (--force overrides)")
                return 1

        if args.dry_run:
            log.info()
            log.info("Dry run -- nothing written.")
            return 0

        prune_staged(staged, parts, not args.no_globals, log)
        backup = importer.backup()
        log.info()
        importer.rewrite_staged()
        importer.merge()
    except (ValueError, tarfile.TarError) as exc:
        log.error(str(exc))
        return 1
    finally:
        shutil.rmtree(staged, ignore_errors=True)

    log.info("Done")
    log.step(f"{importer.merged} file(s) merged, {importer.rules.hits} path "
             f"reference(s) rewritten")
    if backup:
        log.step(f"backup: {backup}")
    for aside in importer.kept_aside:
        log.step(f"kept aside (differs from what was already here): {aside}")
    for path in importer.writes.stale:
        log.warn(f"{os.path.relpath(path, staged)} still names a source path "
                 f"after rewriting")
    log.info()
    log.step("open a project and run `claude --continue` to pick it up")
    return 0

# ---------------------------------------------------------------------------
# repair: memory files still naming a path that moved
# ---------------------------------------------------------------------------

# A path written in prose: absolute, or spelled with a leading ~.  The
# lookbehind stops a match starting mid-token, so "and/or" is not a path and
# neither is the "//" in a URL.
_MENTION = re.compile(r"(?<![\w./~-])(?:~/|/)[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")

# directories the hunt for a moved project will not descend into, and the
# limits that keep a repair from turning into a full-disk scan
_SEARCH_SKIP = {"node_modules", "Library", "Applications", "__pycache__",
                "target", "build", "dist", "venv", ".venv", ".git", ".cache",
                ".Trash", ".npm", ".cargo", ".rustup", ".claude"}
_SEARCH_DEPTH = 5
_SEARCH_LIMIT = 40000

# how many unfixable paths to print before summarising the rest
UNKNOWN_SHOWN = 10

# how many rewritten lines to quote per file before summarising the rest
CONTEXT_SHOWN = 4

# Phrases that suggest a path is being described rather than pointed at.  A
# memory recording "the folder had been renamed while the transcripts kept
# naming X" is saying where X used to be; rewriting X to where the folder is
# now leaves a sentence that describes nothing.  Nothing on the filesystem can
# tell those two apart -- only the words around the path can, and only well
# enough to be worth a second look.
_HISTORY_MARKERS = (
    "had been", "used to", "formerly", "previously", "originally", "renamed",
    "moved from", "was at", "old path", "back when", "at the time",
    "matched from", "copied from", "imported from", "no longer",
    "for example", "e.g.", "for instance", "such as", "say,",
)


def reads_like_history(before: str, after: str) -> bool:
    """Whether the words around a mention suggest it is quoting a path rather
    than pointing at one."""
    window = " ".join((before[-160:], after[:40])).lower()
    return (any(marker in window for marker in _HISTORY_MARKERS)
            or after.lstrip().startswith(")")
            or before.rstrip().endswith("("))


def changed_span(old: str, new: str) -> Tuple[int, int]:
    """Where two versions of one line stop and start agreeing."""
    limit = min(len(old), len(new))
    start = 0
    while start < limit and old[start] == new[start]:
        start += 1
    tail = 0
    while tail < limit - start and old[-1 - tail] == new[-1 - tail]:
        tail += 1
    return start, len(old) - tail


def excerpt(lead: str, line: str, at: int, ends: int, width: int) -> str:
    """A slice of prose showing a change and the words around it.

    The line before is folded in because memory files are wrapped prose: a path
    often lands at the start of its own line, with the words that give it its
    meaning -- "kept naming", "matched from" -- on the line above.  The window
    slides right far enough to show the end of the change, because a rewrite
    the reader cannot see the end of is one they cannot judge.
    """
    joined = f"{lead} {line}" if lead else line
    offset = len(lead) + 1 if lead else 0
    at, ends = at + offset, ends + offset
    # a few characters past the change, so the closing quote or bracket after
    # a path is visible and a trailing "..." reads as "the sentence goes on"
    # rather than "the path was cut off"
    start = max(0, min(at - width // 3, ends + 16 - width))
    piece = joined[start:start + width]
    return ("..." if start else "") + piece.strip() + \
           ("..." if start + width < len(joined) else "")


def mentioned_paths(text: str, home: str) -> Dict[str, str]:
    """Absolute path -> the spelling it was written in, for every path the
    text names."""
    out: Dict[str, str] = {}
    for raw in _MENTION.findall(text):
        spelled = raw.rstrip(".")     # a path that ended a sentence
        if not spelled or spelled in ("~/", "/"):
            continue
        # "/simplify", "/mcp", "/v1" -- a single leading segment is a slash
        # command or a fragment far more often than a directory anyone keeps
        # a project in.  ~/foo needs no such guard: the ~ says it is a path.
        if spelled.startswith("/") and spelled.count("/") < 2:
            continue
        expanded = home + spelled[1:] if spelled.startswith("~") else spelled
        out.setdefault(norm(expanded), spelled)
    return out


def ancestors(path: str) -> List[str]:
    """`path` and every directory above it, longest first, stopping at the
    root."""
    out = []
    while path and path != "/" and os.path.dirname(path) != path:
        out.append(path)
        path = os.path.dirname(path)
    return out


class Clue:
    """One path that has moved, where it went, and how we know.

    `sure` separates what Claude's own state proves from what a name match
    merely suggests, so the two can be presented differently.
    """

    def __init__(self, old: str, new: str, why: str, sure: bool) -> None:
        self.old = old
        self.new = new
        self.why = why
        self.sure = sure


class Relocations:
    """Where the projects Claude still names have actually gone.

    Three sources, in decreasing order of certainty:

      1. a state directory naming a path that no longer exists while exactly
         one path it could belong to does -- Claude's own state proves the
         move, so this one is certain;
      2. a missing path whose last segment names a project that still exists
         somewhere else;
      3. a missing path whose last segment names exactly one directory found
         under the home directory.

    The last two are guesses.  A directory name is not an identity -- a memory
    file may be quoting an example rather than a real location -- so they are
    offered for the user to accept or reject rather than applied.
    """

    def __init__(self, layout: Layout, log: Log, search: bool = True) -> None:
        self.layout = layout
        self.log = log
        self.search = search
        self.clues: Dict[str, Clue] = {}
        # one scan of every transcript, whose cwd sets and resolved project
        # list are both needed below
        self.cwds: Dict[str, Set[str]] = {}
        self.projects = known_projects(layout, log, self.cwds)
        self._claimants: Dict[str, Set[str]] = {}
        for path, state in self.projects.items():
            self._claimants.setdefault(state, set()).add(path)
        self.here = {p for p in self.projects if os.path.isdir(p)}
        # Somewhere a project could have moved *to* has to look like one.
        # `known_projects` also hands back every cwd a transcript recorded, so
        # without this a scratch directory inside a live project -- or one of
        # Claude Code's own worktrees -- can be offered as the new home of a
        # missing project that shares its last segment, on the strength of
        # nothing but the name.  prune then refuses to delete state because of
        # it, and repair rewrites paths to it.
        #
        # `here` itself stays wide: `index` uses it to stop the home search
        # descending into a project, and a cwd inside one is exactly where
        # that search should stop.
        entries = {norm(key) for key in config_projects(read_json(layout.config, {}))}
        counts = {project for _line, project in history_entries(layout) if project}
        kept = {p for p in self.here
                if kept_state(self.projects[p], (p,), entries, counts)}
        self.by_name: Dict[str, Set[str]] = {}
        for path in self.here:
            if self._destination(path, kept):
                self.by_name.setdefault(os.path.basename(path), set()).add(path)
        self._index: Optional[Dict[str, Set[str]]] = None
        self._from_state_dirs()

    def _destination(self, path: str, kept: Set[str]) -> bool:
        """Whether a path is somewhere a project could have moved *to*.

        The same question `index` answers as it walks, asked of a path already
        in hand: not inside an application's private state, and not inside
        another project.  Both sources of a guess have to agree on it, or the
        one this rejects the other still offers.

        The test is the shape of the path, not what Claude has filed under it.
        Claude Code files a worktree exactly the way it files a project -- a
        state directory and a config entry, because it ran there -- so what is
        filed cannot tell the two apart, while the `.claude` in the path can.
        Asking what is filed also gets the honest case backwards: a folder
        moved by hand and not yet reopened has nothing filed under its new
        path either, and dropping it leaves prune deleting the state of a
        project sitting right there.

        `kept` is the projects something is actually filed under rather than
        every path in `here`, so that a directory someone cd'd into once does
        not disqualify the projects beneath it.
        """
        return (not any(part.startswith(".")
                        for part in path.strip("/").split("/"))
                and not any(a in kept for a in ancestors(path)[1:]))

    # -- evidence ---------------------------------------------------------

    def claims(self, state: str) -> Set[str]:
        """Every project path that resolves onto one state directory.

        Two sources, both already scanned: the cwds its transcripts record,
        and every path `known_projects` mapped onto it -- which is where its
        config entry and its decoded name have been accounted for.  Asked in
        one place because more than one command has to ask it, and a rule
        added to one copy would be missing from the other.
        """
        return self._claimants.get(state, set()) | self.cwds.get(state, set())

    def _from_state_dirs(self) -> None:
        """The certain kind: a state directory that names both a path which is
        gone and one which is here.

        What a state directory claims comes from the scan already done -- the
        cwds its transcripts record, plus every path `known_projects` resolved
        onto it, which is where its config entry and its decoded name have
        already been accounted for.
        """
        for state in self.cwds:
            # a session can run with its cwd inside ~/.claude; that is not a
            # project, and so not evidence about one either
            claims = {c for c in self.claims(state)
                      if not is_under(c, self.layout.dir)}
            here = {c for c in claims if os.path.isdir(c)}
            if len(here) != 1:
                continue
            new = here.pop()
            # A path *inside* the surviving one was deleted, not moved: a
            # scratch directory, or one of Claude Code's own worktrees under
            # .claude/.  Reading those as relocations would collapse every
            # mention of a subdirectory onto the project root.
            gone = sorted(c for c in claims
                          if not os.path.isdir(c) and not is_under(c, new))
            for old in gone:
                if old != new:
                    self.add(Clue(old, new, "Claude's own state for that "
                                            "project resolves to it", True))

    def add(self, clue: Clue) -> None:
        # proof beats a guess whichever order they turn up in; between two
        # guesses the first wins, and `resolve` always tries the better source
        # first, so that is the better guess rather than the earlier mention
        known = self.clues.get(clue.old)
        if known is None or (clue.sure and not known.sure):
            self.clues[clue.old] = clue

    def index(self) -> Dict[str, Set[str]]:
        """Directory name -> where directories of that name live under the
        home directory.  Built once, and bounded in every direction: by depth,
        by count, and by where it is willing to look.

        It looks for somewhere a project could have been *moved to*, which is
        neither inside an application's private state (a dot-directory) nor
        inside another project -- and a project's own tree is full of common
        names like src and tests that would match anything.  A project that
        really did move inside another one is missed; a search that guessed
        from a "tests" directory would be worse than useless.
        """
        if self._index is not None:
            return self._index
        self._index = {}
        home, seen = self.layout.home, 0
        for root, dirs, _files in os.walk(home):
            if root[len(home):].count("/") >= _SEARCH_DEPTH:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs
                       if not d.startswith(".") and d not in _SEARCH_SKIP]
            for name in dirs:
                self._index.setdefault(name, set()).add(os.path.join(root, name))
            seen += len(dirs)
            dirs[:] = [d for d in dirs if os.path.join(root, d) not in self.here]
            if seen > _SEARCH_LIMIT:
                self.log.warn(f"stopped looking for moved directories after "
                              f"{_SEARCH_LIMIT} of them -- pass --no-search to "
                              f"skip that search entirely")
                break
        return self._index

    # -- resolution -------------------------------------------------------

    def resolve(self, path: str) -> Optional[Clue]:
        """Explain a path a memory file names but the disk does not have.

        Only the missing part of the path can have moved: if ~/dev/api is
        still there, ~/dev/api/gone.md is a deleted file, not a relocation.
        """
        missing = [a for a in ancestors(path) if not os.path.exists(a)]
        for anc in missing:
            if anc in self.clues:
                return self.clues[anc]
        for anc in missing:
            if not self._plausible(anc):
                continue
            clue = self._named_project(anc) or self._searched(anc)
            if clue:
                self.add(clue)
                return clue
        return None

    def _plausible(self, path: str) -> bool:
        """Worth guessing about: a directory deep enough to be a project, and
        not part of Claude's own state."""
        return (not is_under(path, self.layout.dir)
                and path != self.layout.home
                and len(path.strip("/").split("/")) >= 2)

    def _named_project(self, path: str) -> Optional[Clue]:
        hits = self.by_name.get(os.path.basename(path), set()) - {path}
        if len(hits) == 1:
            return Clue(path, hits.pop(),
                        "the only project Claude knows by that name", False)
        return None

    def _searched(self, path: str) -> Optional[Clue]:
        if not self.search:
            return None
        hits = self.index().get(os.path.basename(path), set()) - {path}
        if len(hits) == 1:
            where = short(self.layout.home, self.layout.home)
            return Clue(path, hits.pop(),
                        f"the only directory by that name under {where}", False)
        return None


class Finding:
    """One relocation, and the memory files that still name the old path.

    Counting is done by the same Rewriter that will do the writing, so the
    number reported to the user is by construction the number of replacements
    they are agreeing to.
    """

    def __init__(self, clue: Clue, layout: Layout) -> None:
        self.clue = clue
        self.files: Dict[str, int] = {}
        self.pairs = project_pairs(layout, clue.old, clue.new)
        self.rules = Rewriter()
        for old, new in self.pairs:
            self.rules.add(old, new)

    def count(self, path: str, text: str) -> None:
        hits = self.rules.count(text)
        if hits:
            self.files[path] = hits

    @property
    def hits(self) -> int:
        return sum(self.files.values())


class Repair:
    """A pass over memory files, looking for paths that have moved."""

    def __init__(self, args: argparse.Namespace, layout: Layout, log: Log) -> None:
        self.args = args
        self.layout = layout
        self.log = log
        self.relocations = Relocations(layout, log, search=not args.no_search)
        self.dirs = self._targets()
        self.texts: Dict[str, str] = {}
        self.findings: List[Finding] = []
        self.unknown: Dict[str, Set[str]] = {}
        # state directory -> the project it holds, for naming files readably.
        # An existing folder wins: after a move both the old and new path can
        # still resolve onto one directory, and the old one is not what to
        # call it.
        self.owners: Dict[str, str] = {}
        for project, state in sorted(self.relocations.projects.items()):
            if state not in self.owners or os.path.isdir(project):
                self.owners[state] = project

    def _targets(self) -> List[str]:
        """State directories to read.  Naming projects narrows it; naming none
        checks every directory that holds memory at all -- including ones no
        config entry survives for, which is exactly where stale paths collect.
        """
        if self.args.projects:
            chosen = select(self.relocations.projects, self.args.projects,
                            self.log)
            return sorted({d for d in chosen.values() if os.path.isdir(d)})
        return [d for d in state_dirs(self.layout) if memory_files(d)]

    def scan(self) -> None:
        for state in self.dirs:
            for name in memory_files(state):
                path = os.path.join(state, name)
                text = read_text(path)
                if text is not None:
                    self.texts[path] = text

        # Everything Claude's own state proves has moved is worth checking for,
        # whether or not a memory file spells it out as a path: a file may name
        # only the state directory, which is derived from the project path
        # rather than equal to it.  Findings nothing names are dropped below.
        found = {clue.old: Finding(clue, self.layout)
                 for clue in self.relocations.clues.values() if clue.sure}
        for path, text in self.texts.items():
            for abs_path, spelled in mentioned_paths(text, self.layout.home).items():
                # a ~/.claude path is Claude's own state, not a project
                # location, and never something to guess a relocation from
                if os.path.exists(abs_path) or is_under(abs_path, self.layout.dir):
                    continue
                clue = self.relocations.resolve(abs_path)
                if clue:
                    found.setdefault(clue.old, Finding(clue, self.layout))
                elif os.path.isdir(os.path.dirname(abs_path)):
                    # the parent is still there and the child is not, which is
                    # what a move looks like; a path whose whole tree is absent
                    # is far more often an example someone wrote down
                    self.unknown.setdefault(spelled, set()).add(path)

        # counting is a second pass: a path first noticed in the last file
        # scanned is usually named in the first one too
        for finding in found.values():
            for path, text in self.texts.items():
                finding.count(path, text)
        self.findings = sorted((f for f in found.values() if f.files),
                               key=lambda f: (not f.clue.sure, f.clue.old))

    # -- reporting --------------------------------------------------------

    def shorten(self, path: str) -> str:
        return short(path, self.layout.home)

    def relative(self, path: str) -> str:
        return os.path.relpath(path, self.layout.projects)

    def label(self, path: str) -> str:
        """A memory file named the way its owner thinks of it.

        The encoded state directory name is unreadable at a glance and every
        one of them shares a long prefix, which is the worst possible shape for
        a list someone has to scan.  The project's own folder name is what they
        recognise; the encoded name stays as the fallback when no project
        resolves onto that directory.
        """
        state = os.path.dirname(path)
        if os.path.basename(state) == "memory":
            state = os.path.dirname(state)
        owner = self.owners.get(state)
        rest = os.path.relpath(path, state)
        return f"{os.path.basename(owner)}/{rest}" if owner else self.relative(path)

    def describe(self) -> None:
        self.log.info(f"Read {len(self.texts)} memory file(s) in "
                      f"{len(self.dirs)} project(s)")
        if self.findings:
            self.log.info()
            self.log.info(f"Found {len(self.findings)} path(s) that moved:")
        for i, finding in enumerate(self.findings, 1):
            clue = finding.clue
            self.log.info()
            self.log.info(f"  {i}. {self.shorten(clue.old)}")
            self.log.info(f"     -> {self.shorten(clue.new)}"
                          f"{'' if clue.sure else '   (a guess)'}")
            self.log.info(f"     {clue.why}")
            self.log.info(f"     {finding.hits} reference(s) in "
                          f"{len(finding.files)} memory file(s):")
            quoted = self.describe_references(finding)
            if any(quoted):
                seen = (f"Every reference to this reads" if all(quoted)
                        else f"{sum(quoted)} of {len(quoted)} references read")
                self.log.info(f"     {seen} like history rather than a live "
                              f"path.  Check the")
                self.log.info(f"     wording before applying -- a rewrite may "
                              f"not be what you want here.")
        self.describe_unknown()
        self.describe_state_dirs()
        self.describe_live()

    def describe_references(self, finding: "Finding") -> List[bool]:
        """Quote each line this finding would rewrite, before and after.

        The path evidence can prove a directory moved.  It cannot tell whether
        a sentence is pointing at that path or quoting it, and the only thing
        that can is the sentence itself -- so put it in front of the person
        being asked to approve the rewrite.
        """
        width = max(60, min(shutil.get_terminal_size((100, 24)).columns - 14, 150))
        history: List[bool] = []
        for path in sorted(finding.files):
            text = self.texts[path]
            before, after = text.splitlines(), finding.rules.preview(text).splitlines()
            changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
            for i in changed[:CONTEXT_SHOWN]:
                start, end = changed_span(before[i], after[i])
                lead = " ".join(before[max(0, i - 1):i])
                quoting = reads_like_history(lead + " " + before[i][:start],
                                             before[i][end:])
                history.append(quoting)
                self.log.info(f"       {self.label(path)}:{i + 1}"
                              f"{'   reads like history' if quoting else ''}")
                _, grew = changed_span(after[i], before[i])
                self.log.info(f"         was  "
                              f"{excerpt(lead, before[i], start, end, width)}")
                self.log.info(f"         now  "
                              f"{excerpt(lead, after[i], start, grew, width)}")
            if len(changed) > CONTEXT_SHOWN:
                self.log.info(f"       ... and {len(changed) - CONTEXT_SHOWN} "
                              f"more line(s) in the same file")
        return history

    def describe_live(self) -> None:
        """A running session is not the blocker here that it is for a move --
        nothing this command writes is held in ~/.claude.json -- but a session
        with a memory file already open can write its own copy back over the
        repair, so say so while the user can still answer no."""
        live = sorted(p for p in live_project_paths(self.layout)
                      if self.layout.state_dir(p) in self.dirs)
        if not live:
            return
        self.log.info()
        for path in live:
            self.log.warn(f"Claude Code is running in {self.shorten(path)} -- "
                          f"quit it first, or it may write its own copy of "
                          f"these memory files back")

    def describe_unknown(self) -> None:
        if not self.unknown:
            return
        self.log.info()
        self.log.info("Named in memory, not on disk, and nowhere obvious to "
                      "point them:")
        for spelled in sorted(self.unknown)[:UNKNOWN_SHOWN]:
            files = sorted(self.unknown[spelled])
            more = f" and {len(files) - 1} more" if len(files) > 1 else ""
            self.log.info(f"  {spelled}   ({self.label(files[0])}{more})")
        if len(self.unknown) > UNKNOWN_SHOWN:
            self.log.info(f"  ... and {len(self.unknown) - UNKNOWN_SHOWN} more")
        self.log.info("  Left alone -- put the directory back, or fix the "
                      "wording by hand.")

    def describe_state_dirs(self) -> None:
        """Memory text is all this command rewrites.  When a project's whole
        state directory is still keyed to the old path, say so and name the
        command that fixes the rest of it.

        Only for a relocation Claude's own state proves.  Recommending a move
        on the strength of two directories sharing a name would be advice this
        command is not entitled to give.
        """
        for finding in self.findings:
            clue = finding.clue
            if not clue.sure or not os.path.isdir(self.layout.state_dir(clue.old)):
                continue
            self.log.info()
            self.log.info(f"  Note: {self.shorten(clue.old)} still has a whole "
                          f"state directory of its own.")
            self.log.info(f"        repair rewrites memory text and nothing "
                          f"else.  To carry that project's")
            self.log.info(f"        transcripts, permissions and history "
                          f"across too, run:")
            self.log.info(f"          claude-move.py --state-only "
                          f"{self.shorten(clue.old)} {self.shorten(clue.new)}")

    # -- applying ---------------------------------------------------------

    def choose(self) -> List[Finding]:
        """Which findings to apply.  Guesses sit in the same list as the
        certain ones, so the answer has to be able to name a subset."""
        if self.args.yes:
            return self.findings
        return pick(self.findings, "Apply?", self.log)

    def backup(self, files: List[str]) -> Optional[str]:
        if self.args.no_backup:
            return None
        root = os.path.join(backup_root(self.layout), "memory")
        for path in files:
            dest = os.path.join(root, self.relative(path))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(path, dest)
        return root

    def apply(self, chosen: List[Finding]) -> None:
        rules = Rewriter()
        for finding in chosen:
            for old, new in finding.pairs:
                rules.add(old, new)
        files = sorted({path for finding in chosen for path in finding.files})

        backup = self.backup(files)
        if backup:
            self.log.step(f"backed up {len(files)} memory file(s) to {backup}")
        writes = FileRewriter(rules, self.log)
        for path in files:
            writes.text(path)
        self.log.step(f"{writes.files_rewritten} memory file(s) updated, "
                      f"{sum(f.hits for f in chosen)} path reference(s) "
                      f"rewritten")
        for path in writes.stale:
            self.log.warn(f"{self.label(path)} still names an old path "
                          f"after rewriting")


def do_repair(args: argparse.Namespace, layout: Layout, log: Log) -> int:
    repair = Repair(args, layout, log)
    if not repair.dirs:
        # naming projects that match nothing is a mistyped argument, not a
        # clean bill of health, and must not read as one from a script
        if args.projects:
            log.error("no memory files to check for the projects named")
            return 1
        log.info("no memory files to check")
        return 0
    repair.scan()
    repair.describe()

    if not repair.findings:
        log.info()
        log.info("no memory file names a path that moved -- nothing to fix")
        return 0
    if args.dry_run:
        log.info()
        log.info("dry run -- nothing was changed")
        return 0

    chosen = repair.choose()
    if not chosen:
        log.info("nothing applied")
        return 1
    log.info()
    repair.apply(chosen)
    return 0


# ---------------------------------------------------------------------------
# prune: state left behind by folders that are gone
# ---------------------------------------------------------------------------

# how many orphans to describe in full before summarising the rest
ORPHANS_SHOWN = 20


def all_session_ids(layout: Layout) -> Set[str]:
    """Every session id that still has a transcript on disk."""
    out: Set[str] = set()
    for state in state_dirs(layout):
        out.update(session_ids(state))
    return out


def file_history_dirs(layout: Layout) -> List[str]:
    """The per-session folders of /rewind blobs."""
    if not os.path.isdir(layout.file_history):
        return []
    paths = (os.path.join(layout.file_history, name)
             for name in sorted(os.listdir(layout.file_history)))
    return [p for p in paths if os.path.isdir(p)]


class Orphan:
    """Everything one vanished project left behind, and what it is worth.

    Also carries the file-history blobs that belong to no project at all --
    one group of them, with no path and no state directory of its own.
    """

    def __init__(self, paths: Iterable[str],
                 state: Optional[str] = None) -> None:
        self.paths = list(paths)    # real project paths first; see _examine
        self.state = state
        self.sessions = session_ids(state) if state else []
        self.memory = len(memory_files(state)) if state else 0
        self.config: List[str] = []     # ~/.claude.json keys naming these paths
        self.history = 0                # lines of ~/.claude/history.jsonl
        self.blobs: List[str] = []      # file-history folders
        self._stat: Optional[Tuple[int, float]] = None

    def stat(self) -> Tuple[int, float]:
        """Bytes on disk, and when anything in here was last written."""
        if self._stat is None:
            total, newest = 0, 0.0
            for path in ([self.state] if self.state else []) + self.blobs:
                size, seen = tree_stat(path)
                total, newest = total + size, max(newest, seen)
            self._stat = (total, newest)
        return self._stat

    @property
    def size(self) -> int:
        return self.stat()[0]

    def title(self, home: str) -> str:
        if self.paths:
            return short(self.paths[0], home)
        if self.state:
            # nothing on disk decodes onto it -- the encoded name is all there is
            return os.path.basename(self.state)
        return (f"file-history for {len(self.blobs)} session(s) whose "
                f"transcripts are gone")

    def details(self) -> str:
        """The inventory line under the title."""
        bits = []
        if self.state:
            bits.append(f"{len(self.sessions)} transcript(s)")
            bits.append(f"{self.memory} memory file(s)")
        if self.blobs and (self.state or self.paths):
            # the stray group is nothing but blobs, and its title says so
            bits.append(f"{len(self.blobs)} file-history folder(s)")
        if self.config:
            bits.append("permissions and settings")
        if self.history:
            bits.append(f"{self.history} shell-history line(s)")
        return ", ".join(bits)


class Prune:
    """Find the state whose project folder is gone, and delete what is picked.

    The dangerous mistake here is repair's in reverse: repair rewrites a path
    that had not moved, and prune deletes the state of a folder that had.  A
    folder that moved looks exactly like one that was deleted -- the path is
    missing either way -- so every candidate goes through the same hunt repair
    uses, and anything with somewhere to point at is reported rather than
    offered.  Only what nothing on this machine can account for is offered.
    """

    def __init__(self, args: argparse.Namespace, layout: Layout, log: Log) -> None:
        self.args = args
        self.layout = layout
        self.log = log
        self.relocations = Relocations(layout, log, search=not args.no_search)
        self.orphans: List[Orphan] = []
        self.moved: List[Clue] = []
        self.checked = 0
        # the config keyed as it is actually written, so an entry is removed by
        # the key it has rather than by the normalised form we compare on
        self.entries: Dict[str, List[str]] = {}
        for key in config_projects(read_json(layout.config, {})):
            self.entries.setdefault(norm(key), []).append(key)
        # session id -> its folder of /rewind blobs.  Every state directory
        # asks this the same question, and the answer does not change between
        # them; looking it up per directory rescans the whole tree each time.
        self.blob_dirs: Dict[str, str] = {
            os.path.basename(d): d for d in file_history_dirs(layout)}
        # project path -> how many lines of the shell history it owns
        self.counts: Dict[str, int] = {}
        for _line, project in history_entries(layout):
            if project:
                self.counts[project] = self.counts.get(project, 0) + 1

    # -- scanning ---------------------------------------------------------

    def scan(self) -> None:
        # A state directory sitting on disk and one that only a config entry
        # names are the same question asked about different leftovers.
        candidates: Dict[str, Set[str]] = {s: set() for s in state_dirs(self.layout)}
        for path, state in self.relocations.projects.items():
            candidates.setdefault(state, set()).add(path)

        for state in sorted(candidates):
            if not kept_state(state, candidates[state], self.entries, self.counts):
                continue
            self.checked += 1
            orphan = self._examine(state, candidates[state])
            if orphan:
                self.orphans.append(orphan)

        self._stray_blobs()
        self.orphans.sort(key=lambda o: (-o.size, o.title(self.layout.home)))

    def _examine(self, state: str, known: Set[str]) -> Optional[Orphan]:
        """Whether one state directory is genuinely left over, and what of.

        Two questions, deliberately asked of two different sets of paths.
        Whether anything still uses this directory takes every path recorded
        anywhere inside it, including a cwd some session merely ran in.  What
        the directory *is* -- what to call it, whose settings and whose shell
        history go with it -- takes only the paths it belongs to.  A cwd
        recorded here can belong to another project entirely, and attributing
        that project's settings to this one deletes them out from under it.
        """
        mentioned = known | self.relocations.claims(state)
        owners = sorted(known)
        if not self._wanted(owners):
            return None
        if any(self._alive(path, state) for path in mentioned):
            return None                     # the project is still right there

        # The folder may have been renamed together with its state directory,
        # in which case nothing recorded inside either one names where it went
        # -- but the encoded name still decodes onto it.
        if any(os.path.isdir(c) for c in decode_state_dir(os.path.basename(state))):
            return None

        # Several owners means the lossy encoding collapsed them onto one
        # directory ("my_app" and "my-app"), so they all encode to its name
        # and none of them is the one it is "really" called after.  The first
        # alphabetically names it; the rest are listed under it.
        for path in owners or sorted(mentioned):
            clue = self.relocations.resolve(path)
            if clue:
                # the clue may be about an ancestor that moved; say where this
                # project itself lands under it, since that is the move to run
                self.moved.append(Clue(path, remap(path, clue.old, clue.new),
                                       clue.why, clue.sure))
                return None

        orphan = Orphan(owners, state if os.path.isdir(state) else None)
        for path in orphan.paths:
            orphan.config.extend(self.entries.get(path, []))
            orphan.history += self.counts.get(path, 0)
        orphan.blobs = [self.blob_dirs[sid] for sid in orphan.sessions
                        if sid in self.blob_dirs]
        return orphan

    def _alive(self, path: str, state: str) -> bool:
        """Whether one recorded path being on disk keeps this directory in use.

        Claude's own space under ~/.claude is the exception that matters.  A
        subagent can run with its cwd inside a state directory -- editing the
        memory files that live in *this very directory* -- and such a path
        exists for exactly as long as the directory does, so reading it as
        proof of life would make the thing immortal.  It counts only when the
        directory is named for it, which is what one of Claude Code's own
        worktrees looks like.
        """
        if (is_under(path, self.layout.dir)
                and encode_path(path) != os.path.basename(state)):
            return False
        return os.path.isdir(path)

    def _stray_blobs(self) -> None:
        """/rewind blobs whose transcript is gone.

        Claude Code deletes transcripts once they age past its retention
        setting; the blobs they refer to stay, and nothing else ever collects
        them.  Naming projects narrows this command to those projects, and
        these belong to none.
        """
        if self.args.projects:
            return
        live = all_session_ids(self.layout)
        stray = [d for sid, d in sorted(self.blob_dirs.items()) if sid not in live]
        if stray:
            group = Orphan([])
            group.blobs = stray
            self.orphans.append(group)

    def unmatched(self) -> List[str]:
        """Patterns that name nothing Claude has ever heard of.

        Distinct from a pattern that matched a project which turned out to
        have nothing left over -- that one is a clean bill of health, and
        saying otherwise would train people to ignore the difference.
        """
        return [p for p in self.args.projects
                if not any(matches_pattern(path, p)
                           for path in self.relocations.projects)]

    def _wanted(self, claims: Iterable[str]) -> bool:
        """Whether the command line asked about this one.  Applied before the
        hunt for where it went, so naming one project neither reports another
        one's move nor pays for the search that found it."""
        return not self.args.projects or any(
            matches_pattern(path, pattern)
            for path in claims for pattern in self.args.projects)

    # -- reporting --------------------------------------------------------

    def shorten(self, path: str) -> str:
        return short(path, self.layout.home)

    @property
    def total(self) -> int:
        return sum(o.size for o in self.orphans)

    def describe(self) -> None:
        log = self.log
        log.info(f"Checked {self.checked} project(s) Claude has state for")
        self.describe_moved()
        if not self.orphans:
            return
        log.info()
        log.info("Gone from disk, with nothing on this machine to say where "
                 "they went:")
        for i, orphan in enumerate(self.orphans[:ORPHANS_SHOWN], 1):
            log.info()
            log.info(f"  {i}. {orphan.title(self.layout.home)}   "
                     f"{human(orphan.size)}")
            details = orphan.details()
            if details:
                log.info(f"     {details}")
            for path in orphan.paths[1:]:
                log.info(f"     {self.shorten(path)} maps onto the same "
                         f"state, and is gone too")
            newest = orphan.stat()[1]
            if newest:
                log.info(f"     last written "
                         f"{time.strftime('%Y-%m-%d', time.localtime(newest))}")
        if len(self.orphans) > ORPHANS_SHOWN:
            rest = self.orphans[ORPHANS_SHOWN:]
            log.info()
            log.info(f"  ... and {len(rest)} more, {human(sum(o.size for o in rest))} "
                     f"in total -- too many to number, but [a] covers them")

    def describe_moved(self) -> None:
        """A folder that moved is the one thing here that must not be deleted,
        so say where it went and name the command that follows it."""
        if not self.moved:
            return
        self.log.info()
        self.log.info("Moved rather than deleted -- left alone:")
        for clue in sorted(self.moved, key=lambda c: c.old):
            self.log.info()
            self.log.info(f"  {self.shorten(clue.old)}  ->  "
                          f"{self.shorten(clue.new)}"
                          f"{'' if clue.sure else '   (a guess)'}")
            self.log.info(f"     {clue.why}")
            self.log.info(f"     to carry its state across, run:")
            self.log.info(f"       claude-move.py --state-only "
                          f"{self.shorten(clue.old)} {self.shorten(clue.new)}")

    def describe_live(self, chosen: List[Orphan]) -> None:
        """A running session writes ~/.claude.json back as it exits, which
        would put the entries just removed straight back."""
        if not any(o.config for o in chosen) or not live_project_paths(self.layout):
            return
        self.log.warn("Claude Code is running somewhere -- it holds "
                      "~/.claude.json in memory and may write\n"
                      "           the removed settings back when it exits.  "
                      "Quit it and re-run if they come back.")

    # -- applying ---------------------------------------------------------

    def choose(self) -> List[Orphan]:
        """Which orphans to delete.  Only the numbered ones can be picked out
        individually; [a] takes the summarised tail as well."""
        if self.args.yes:
            return self.orphans
        return pick(self.orphans, "Delete?", self.log,
                    min(len(self.orphans), ORPHANS_SHOWN))

    def doomed_paths(self, chosen: List[Orphan]) -> Set[str]:
        return {path for o in chosen if o.history for path in o.paths}

    def doomed_entries(self, chosen: List[Orphan], config: Any) -> Dict[str, Any]:
        """The ~/.claude.json project entries the chosen orphans own, under the
        keys they are written with."""
        entries = config_projects(config)
        keys = {key for o in chosen for key in o.config}
        return {k: v for k, v in entries.items() if k in keys}

    def backup(self, chosen: List[Orphan],
               history: List[Tuple[str, Optional[str]]],
               config: Any) -> Optional[str]:
        """Open the backup and save the two things that are edited rather than
        removed: the config entries and the shell-history lines.

        The directories themselves are not copied here.  They are *moved* into
        this same backup as they go (see `retire`), which costs nothing and
        needs no free space -- and a command whose whole point is reclaiming
        space must not demand a spare copy of everything first.
        """
        if self.args.no_backup:
            return None
        root = os.path.join(backup_root(self.layout), "pruned")
        os.makedirs(root, exist_ok=True)
        entries = self.doomed_entries(chosen, config)
        if entries:
            write_json_atomic(os.path.join(root, "config.json"),
                              {"projects": entries})
        doomed = self.doomed_paths(chosen)
        dropped = [line for line, project in history if project in doomed]
        if dropped:
            write_text_atomic(os.path.join(root, "history.jsonl"),
                              "".join(dropped))
        return root

    @staticmethod
    def retire(path: str, root: Optional[str], where: str) -> None:
        """See one directory out: into the backup if there is one, and
        straight to deletion if the user waived it."""
        if root is None:
            shutil.rmtree(path)
            return
        dest = os.path.join(root, where, os.path.basename(path))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(path, dest)

    def apply(self, chosen: List[Orphan]) -> None:
        freed = sum(o.size for o in chosen)
        # Read both files once here rather than trusting the scan: a live
        # session may have written to them since, and what is saved and what
        # is removed have to be the same snapshot or the backup will not hold
        # everything that went.
        history = history_entries(self.layout)
        config = read_json(self.layout.config, {}) or {}

        root = self.backup(chosen, history, config)
        trees, blobs = 0, 0
        for orphan in chosen:
            if orphan.state:
                self.retire(orphan.state, root, "projects")
                trees += 1
            for blob in orphan.blobs:
                self.retire(blob, root, "file-history")
                blobs += 1

        done = [(trees, "state directory(s)"),
                (blobs, "file-history folder(s)"),
                (self.forget_config(chosen), "config entry(s)"),
                (self.forget_history(chosen, history), "shell-history line(s)")]
        self.log.step("deleted " + ", ".join(f"{n} {what}"
                                             for n, what in done if n))
        if root:
            # the space is not back yet, and saying it is would be a lie the
            # user only finds out about when the disk stays full
            self.log.step(f"{human(freed)} moved to {root}")
            self.log.step("delete that directory to reclaim the space")
        else:
            self.log.step(f"reclaimed {human(freed)}")

    def forget_config(self, chosen: List[Orphan]) -> int:
        keys = {key for o in chosen for key in o.config}
        if not keys:
            return 0
        # re-read rather than trust the scan: a session may have rewritten the
        # file since, and a key no longer in it is already forgotten
        config = read_json(self.layout.config, {}) or {}
        entries = config_projects(config)
        gone = [k for k in keys if k in entries]
        for key in gone:
            del entries[key]
        if gone:
            write_json_atomic(self.layout.config, config)
        return len(gone)

    def forget_history(self, chosen: List[Orphan],
                       history: List[Tuple[str, Optional[str]]]) -> int:
        doomed = self.doomed_paths(chosen)
        if not doomed:
            return 0
        kept = [line for line, project in history if project not in doomed]
        if len(kept) == len(history):
            return 0
        write_text_atomic(self.layout.history, "".join(kept))
        return len(history) - len(kept)


def do_prune(args: argparse.Namespace, layout: Layout, log: Log) -> int:
    prune = Prune(args, layout, log)

    # A pattern naming nothing Claude knows about is a typo, and this command
    # deletes: stop on it rather than quietly pruning whatever the other
    # patterns did match.  Reporting every bad name at once beats making the
    # user fix them one run at a time.
    missing = prune.unmatched()
    if missing:
        for pattern in missing:
            log.error(f"no project matches {pattern!r}")
        log.error("nothing was deleted -- fix the names above")
        return 2

    prune.scan()
    prune.describe()

    if not prune.orphans:
        log.info()
        # every name given did match a project; they simply have nothing left
        # over, which is a clean bill of health and reports as one
        log.info("nothing left over for the projects named" if args.projects
                 else "nothing to prune -- every project Claude has state for "
                      "is still on disk")
        return 0
    log.info()
    log.info(f"{len(prune.orphans)} item(s), {human(prune.total)} in total")
    if args.dry_run:
        log.info()
        log.info("dry run -- nothing was deleted")
        return 0

    chosen = prune.choose()
    if not chosen:
        log.info("nothing deleted")
        return 1
    prune.describe_live(chosen)
    log.info()
    prune.apply(chosen)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def glob_match(path: str, pattern: str) -> bool:
    """Shell-style match, one path segment at a time.

    fnmatch on the whole string would let * and ? cross a "/", so a pattern
    like ~/dev/my_* would also claim ~/dev/my_app/packages/core.
    """
    parts, specs = path.split("/"), pattern.split("/")
    return len(parts) == len(specs) and all(
        fnmatch.fnmatch(part, spec) for part, spec in zip(parts, specs))


def expand_sources(patterns: Iterable[str], projects: Set[str],
                   log: Log) -> Optional[List[str]]:
    """Resolve the source arguments, expanding any that look like a wildcard.

    The shell normally expands these before the script is even started, which
    is why `claude-move ~/dev/* ~/archive` works without any help.  Doing it
    here as well means a quoted pattern behaves the same, and lets a pattern
    match projects whose folders are already gone -- those are matched against
    the paths Claude still holds state for rather than against the filesystem.
    """
    out: Dict[str, None] = {}   # an ordered set: a path named twice is one move
    empty: List[str] = []
    for pattern in patterns:
        literal = norm(pattern)
        # a real project whose name happens to contain [ * or ? is a path, not
        # a pattern; taking it literally keeps such folders movable
        if (not _GLOB_MAGIC.search(pattern)
                or os.path.isdir(literal) or literal in projects):
            out[literal] = None
            continue
        matches = sorted(norm(p) for p in glob.glob(os.path.expanduser(pattern))
                         if os.path.isdir(p))
        if not matches:
            # nothing on disk -- the folders may already have been moved by
            # hand, so fall back to what Claude still knows about
            matches = sorted(p for p in projects if glob_match(p, literal))
        if not matches:
            # keep going: report every dead pattern at once rather than making
            # the user fix them one run at a time
            empty.append(pattern)
            continue
        log.info(f"{pattern} -> {len(matches)} match(es)")
        out.update(dict.fromkeys(matches))

    for pattern in empty:
        log.error(f"no directories match: {pattern}")
    return None if empty else list(out)


def cmd_list(layout: Layout, log: Log) -> int:
    known = known_projects(layout)
    entries = {norm(key) for key in config_projects(read_json(layout.config, {}))}
    counts = {project for _line, project in history_entries(layout) if project}
    projects = sorted(path for path, state in known.items()
                      if kept_state(state, (path,), entries, counts))
    if not projects:
        log.info("no Claude Code projects found")
        return 0
    width = max(len(p) for p in projects)
    log.info(f"{'project'.ljust(width)}  sessions  memory  encoded")
    for path in projects:
        state = layout.state_dir(path)
        log.info(f"{path.ljust(width)}  {len(session_ids(state)):>8}  "
                 f"{len(memory_files(state)):>6}  {encode_path(path)}")
    return 0


def ask(prompt: str) -> Optional[str]:
    """One lowercased line from the user, or None when there is nobody there.

    Both prompts in this tool answer an interrupt or a closed stdin the same
    way -- by taking it as a refusal -- so that decision lives here rather than
    in each of them.
    """
    try:
        return input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def confirm(prompt: str) -> bool:
    return ask(f"{prompt} [y/N] ") in ("y", "yes")


def pick(items: List[Any], verb: str, log: Log,
         numbered: Optional[int] = None) -> List[Any]:
    """Which of a numbered list to act on: all of it, none of it, or some.

    `numbered` caps what a number can reach, for a list too long to print in
    full -- "all" still means all of it.  Shared because every command that
    asks this has to parse the same answer, and a second copy is a second
    place for "1,3" to stop meaning what it means here.
    """
    count = len(items) if numbered is None else numbered
    answer = ask(f"\n{verb} [a] all, [n] none"
                 + (f", or numbers like 1,3 (1-{count})" if count > 1 else "")
                 + " ")
    if answer is None or answer in ("", "n", "no", "none"):
        return []
    if answer in ("a", "all", "y", "yes"):
        return list(items)
    picked: Dict[int, None] = {}
    for word in re.split(r"[,\s]+", answer):
        if word.isdigit() and 1 <= int(word) <= count:
            picked[int(word) - 1] = None
        elif word:
            log.warn(f"ignoring {word!r}")
    return [items[i] for i in sorted(picked)]


# the words that mean "not a move".  A folder genuinely called one of these is
# still movable by qualifying it -- ./export, or an absolute path.
SUBCOMMANDS = ("export", "inspect", "import", "repair", "prune")

# argparse would otherwise spell out every move flag here and no command at
# all, which is the one thing the reader cannot guess.  Built from the tuple
# above so a command added there shows up without a second edit.
USAGE = ("claude-move [options] SOURCE... DEST\n"
         "       claude-move --list\n"
         "       claude-move {" + ",".join(SUBCOMMANDS) + "} [options]")

# the only global flags that swallow the word after them, so the scan below
# does not mistake a flag's value for a subcommand
_VALUE_FLAGS = ("--claude-dir", "--config")


def first_word(argv: List[str]) -> Optional[str]:
    """The first bare word on the command line, skipping flags and the values
    they take -- so `--claude-dir /tmp/x export` still reads as `export`."""
    i = 0
    while i < len(argv):
        if argv[i] in _VALUE_FLAGS:
            i += 2
        elif argv[i].startswith("-"):
            i += 1
        else:
            return argv[i]
    return None


def add_part_flags(parser: argparse.ArgumentParser, verb: str) -> None:
    group = parser.add_argument_group(
        f"what to {verb}", "everything, less whatever you waive here")
    group.add_argument("--memory-only", action="store_true",
                       help="memory files and nothing else")
    group.add_argument("--no-sessions", action="store_true",
                       help="skip session transcripts")
    group.add_argument("--no-file-history", action="store_true",
                       help="skip the file-history blobs behind /rewind")
    group.add_argument("--no-config", action="store_true",
                       help="skip permissions, MCP servers and trust from ~/.claude.json")
    group.add_argument("--no-history", action="store_true",
                       help="skip the shell-history lines for these projects")


def repeated_globals() -> argparse.ArgumentParser:
    """The global flags again, for every subcommand to inherit, so they can be
    written on either side of the subcommand word.  SUPPRESS keeps an unused
    one from overwriting what was given before the word."""
    shared = argparse.ArgumentParser(add_help=False)
    for flag in _VALUE_FLAGS:
        shared.add_argument(flag, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    shared.add_argument("-q", "--quiet", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return shared


def add_global_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--claude-dir", default=os.path.expanduser("~/.claude"),
                        help="override the Claude state directory (default ~/.claude)")
    parser.add_argument("--config", default=os.path.expanduser("~/.claude.json"),
                        help="override the Claude config file (default ~/.claude.json)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="only warnings and errors")


def build_subcommand_parser() -> argparse.ArgumentParser:
    """Everything that is not a move.  A move rewrites state in place as a
    folder goes; export and import carry that state to another machine, and
    repair cleans up after the folders that went without this tool."""
    shared = repeated_globals()

    parser = argparse.ArgumentParser(
        prog="claude-move",
        description="Carry Claude Code's project state to another computer, "
                    "repair the paths it has left behind, or delete the state "
                    "of folders that are gone.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  claude-move.py export                          everything, every project
  claude-move.py export --list                   what would be exported
  claude-move.py export icepick-offsec -o work.tar.gz
  claude-move.py export --memory-only -o mem.tar.gz

  claude-move.py inspect work.tar.gz             read a bundle, change nothing

  claude-move.py import work.tar.gz --dry-run    show the plan
  claude-move.py import work.tar.gz              paths remapped to this home
  claude-move.py import work.tar.gz --into ~/code
  claude-move.py import work.tar.gz --map api=~/work/api

  claude-move.py repair                          stale paths in memory files
  claude-move.py repair -n                       what it found, change nothing

  claude-move.py prune                           state for folders that are gone
  claude-move.py prune -n                        what it found, delete nothing

A full bundle holds your session transcripts.  Treat it as private, and
encrypt it if it leaves your control.
""")
    add_global_flags(parser)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    exp = sub.add_parser("export", parents=[shared],
                         help="pack project state into a .tar.gz")
    exp.add_argument("projects", nargs="*", metavar="PROJECT",
                     help="project paths, directory names or patterns; "
                          "omit for every project")
    exp.add_argument("-o", "--out", metavar="FILE", help="bundle to write")
    exp.add_argument("--list", "--dry-run", dest="dry_run", action="store_true",
                     help="list what would be exported, write nothing")
    exp.add_argument("--globals", action="store_true",
                     help="also carry ~/.claude settings.json, CLAUDE.md, "
                          "agents/, commands/ and skills/")
    exp.add_argument("--config-all", action="store_true",
                     help="carry whole ~/.claude.json project entries, including "
                          "this machine's cost and session counters")
    add_part_flags(exp, "export")

    ins = sub.add_parser("inspect", parents=[shared],
                         help="show what a bundle holds")
    ins.add_argument("bundle", metavar="BUNDLE")

    imp = sub.add_parser("import", parents=[shared],
                         help="merge a bundle into this machine")
    imp.add_argument("bundle", metavar="BUNDLE")
    imp.add_argument("-n", "--dry-run", action="store_true",
                     help="show the plan, change nothing")
    imp.add_argument("--home", metavar="DIR",
                     help="treat DIR as this machine's home (default: ~)")
    imp.add_argument("--into", metavar="DIR",
                     help="place every project directly inside DIR")
    imp.add_argument("--map", metavar="OLD=NEW", action="append",
                     help="send one project somewhere specific; OLD may be the "
                          "source path or just its directory name (repeatable)")
    imp.add_argument("--overwrite", action="store_true",
                     help="let incoming files replace ones already here "
                          "(default: keep both, incoming as *.incoming)")
    imp.add_argument("--no-globals", action="store_true",
                     help="ignore any ~/.claude-level files in the bundle")
    imp.add_argument("--no-backup", action="store_true",
                     help="skip the safety copy of the state being changed")
    imp.add_argument("--force", action="store_true",
                     help="proceed despite live sessions or mapping collisions")
    add_part_flags(imp, "import")

    rep = sub.add_parser(
        "repair", parents=[shared],
        help="fix stale paths in memory files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Read every memory file, find the paths that no longer "
                    "exist, work out where they went, and offer to rewrite "
                    "them.  Nothing is written until you say so.",
        epilog="""examples:
  claude-move.py repair                     check every project's memory
  claude-move.py repair -n                  show what it found, change nothing
  claude-move.py repair icepick-offsec      one project's memory only
  claude-move.py repair --no-search         go on evidence alone, no hunting

Memory files are all this touches.  Transcripts, permissions and the state
directory itself belong to a move, and repair names the command to run when
it finds one of those is stale too.
""")
    rep.add_argument("projects", nargs="*", metavar="PROJECT",
                     help="project paths, directory names or patterns whose "
                          "memory to check; omit for every project")
    rep.add_argument("-n", "--dry-run", action="store_true",
                     help="report what it found, change nothing")
    rep.add_argument("-y", "--yes", action="store_true",
                     help="apply everything found without asking")
    rep.add_argument("--no-search", action="store_true",
                     help="do not hunt the home directory for a moved "
                          "directory by name")
    rep.add_argument("--no-backup", action="store_true",
                     help="skip the safety copy of the memory files being changed")

    pru = sub.add_parser(
        "prune", parents=[shared],
        help="delete state for folders that are gone",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Find the transcripts, memory, permissions and /rewind "
                    "blobs Claude Code is still keeping for folders that no "
                    "longer exist, and offer to delete them.  A folder that "
                    "moved is reported, never offered.  Nothing goes without "
                    "a copy in the backup directory first.",
        epilog="""examples:
  claude-move.py prune                      offer everything left over
  claude-move.py prune -n                   show what it found, delete nothing
  claude-move.py prune icepick-offsec       one project's leftovers only
  claude-move.py prune --no-search          go on evidence alone, no hunting

A missing folder was either deleted or moved, and the two look identical from
here.  Anything this can find a new home for is listed as moved and left
alone -- run repair, or the move it names, for those.
""")
    pru.add_argument("projects", nargs="*", metavar="PROJECT",
                     help="project paths, directory names or patterns to "
                          "consider; omit for everything left over")
    pru.add_argument("-n", "--dry-run", action="store_true",
                     help="report what it found, delete nothing")
    pru.add_argument("-y", "--yes", action="store_true",
                     help="delete everything found without asking")
    pru.add_argument("--no-search", action="store_true",
                     help="do not hunt the home directory for a moved "
                          "directory by name")
    pru.add_argument("--no-backup", action="store_true",
                     help="skip the safety copy of what is being deleted")
    return parser


def main_subcommand(argv: List[str]) -> int:
    args = build_subcommand_parser().parse_args(argv)
    log = Log(quiet=args.quiet)
    layout = Layout(args.claude_dir, args.config)
    try:
        if args.command == "inspect":       # reads a bundle, not this machine
            return do_inspect(args, log)
        if args.command == "import":
            os.makedirs(layout.projects, exist_ok=True)
            return do_import(args, layout, log)
        if not os.path.isdir(layout.dir):
            log.error(f"no Claude state directory at {layout.dir}")
            return 1
        return {"export": do_export, "repair": do_repair,
                "prune": do_prune}[args.command](args, layout, log)
    except (ValueError, tarfile.TarError, OSError) as exc:
        log.error(str(exc))
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-move",
        usage=USAGE,
        description="Move or rename a project directory and carry its Claude Code "
                    "state (transcripts, memory, permissions, history) with it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  claude-move.py ~/dev/api ~/work/api-server     move and rename
  claude-move.py ~/dev/api ~/work                ~/work exists -> ~/work/api
  claude-move.py ~/dev/api ~/dev/web ~/archive   several at once, like mv
  claude-move.py ~/dev/*-service ~/archive       whatever the shell expands
  claude-move.py '~/dev/*-service' ~/archive     quoted: expanded here instead
  claude-move.py ~/dev/api ~/work/api --dry-run  show the plan, change nothing
  claude-move.py ~/dev/api ~/work/api --state-only
                                                 folder already moved by hand
  claude-move.py --list                          projects Claude has state for

Moving more than one project requires the destination to be an existing
directory; each keeps its own name inside it.

To carry state to another computer instead of moving it on this one:

  claude-move.py export -o state.tar.gz          pack it up here
  claude-move.py import state.tar.gz             unpack it there, repathed
  claude-move.py inspect state.tar.gz            what a bundle holds

To clean up after a folder that was moved without this tool:

  claude-move.py repair                          stale paths in memory files

To delete what folders that are gone for good left behind:

  claude-move.py prune                           offers each one, asks first

Run any of those with --help for their own options.
""")
    parser.add_argument("paths", nargs="*", metavar="PATH",
                        help="one or more project paths, then the destination")
    parser.add_argument("--list", action="store_true",
                        help="list the projects Claude has state for, and exit")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="print the plan without touching anything")
    parser.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--state-only", action="store_true",
                        help="the folder is already at the new path; only fix Claude's state")
    parser.add_argument("--merge", action="store_true",
                        help="allow merging into state that already exists at the new path")
    parser.add_argument("--no-subprojects", dest="subprojects", action="store_false",
                        help="do not remap projects nested inside the moved folder")
    parser.add_argument("--no-project-settings", dest="project_settings", action="store_false",
                        help="do not rewrite paths inside the project's own .claude/ files")
    parser.add_argument("--no-backup", action="store_true",
                        help="skip the safety copy of the state being changed")
    parser.add_argument("--force", action="store_true",
                        help="proceed despite blockers (e.g. a live Claude session)")
    parser.add_argument("--claude-dir", default=os.path.expanduser("~/.claude"),
                        help="override the Claude state directory (default ~/.claude)")
    parser.add_argument("--config", default=os.path.expanduser("~/.claude.json"),
                        help="override the Claude config file (default ~/.claude.json)")
    parser.add_argument("-q", "--quiet", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # A bare first word naming a subcommand switches tools; anything else is a
    # move, so `claude-move.py ~/dev/api ~/work` keeps working untouched.  A
    # folder genuinely called "export" is still movable by qualifying it --
    # ./export or an absolute path.
    if first_word(argv) in SUBCOMMANDS:
        return main_subcommand(argv)

    parser = build_parser()
    args = parser.parse_args(argv)
    log = Log(quiet=args.quiet)
    layout = Layout(args.claude_dir, args.config)

    if args.list:
        return cmd_list(layout, log)
    if len(args.paths) < 2:
        # Bare, this is the first thing a reader sees, and a move is only one
        # of the things here -- someone who came to clean up after a folder
        # that is already gone would otherwise have to guess that prune
        # exists.  Given a source but no destination they know what they came
        # for, so answer the question they actually asked instead.
        parser.print_usage(sys.stderr)
        if args.paths:
            log.error(f"a destination path is required: where should "
                      f"{args.paths[0]} go?")
        else:
            log.error(
                "no project paths given.  moving a folder is one of "
                "several commands:\n"
                "       --list     every project Claude has state for\n"
                "       prune      delete what folders that are gone left behind\n"
                "       repair     fix stale paths after a move done by hand\n"
                "       export     pack state up to carry to another computer\n"
                "       import     unpack a bundle here, repathed for it\n"
                "       inspect    show what a bundle holds\n"
                "       --help, or COMMAND --help, for what each one takes")
        return 2
    if not os.path.isdir(layout.dir):
        log.error(f"no Claude state directory at {layout.dir}")
        return 1

    *patterns, dst = args.paths
    dst = norm(dst)
    projects = known_projects(layout)   # a scan of every transcript; do it once
    sources = expand_sources(patterns, projects, log)
    if sources is None:
        return 2
    if dst in sources:
        # a wildcard covering the destination's own parent sweeps it up, and
        # "same path" alone does not explain where it came from
        log.error(f"the destination is also one of the sources: {dst}"
                  if len(sources) > 1 else "source and destination are the same path")
        return 2
    if len(sources) > 1 and not os.path.isdir(dst):
        log.error(f"moving {len(sources)} projects at once needs an existing "
                  f"directory to move them into: {dst}")
        return 2

    batch = Batch(args, layout, log, sources, dst)
    batch.build(projects)
    batch.describe()

    if batch.blockers:
        log.info()
        for message, _fatal in batch.blockers:
            log.error(f"blocked: {message}")
        fatal = any(fatal for _, fatal in batch.blockers)
        if fatal or not args.force:
            log.info()
            log.error("nothing was changed." + ("  this cannot be overridden." if fatal
                                                else "  fix the above, or pass --force."))
            return 1
        log.warn("proceeding anyway (--force)")

    if args.dry_run:
        log.info()
        log.info("dry run -- nothing was changed")
        return 0

    if not args.yes and not confirm("\nProceed?"):
        log.info("aborted")
        return 1

    log.info()
    backup = batch.backup()
    if backup:
        log.step(f"backed up current state to {backup}")

    try:
        batch.run()
    except Exception:  # noqa: BLE001 - already reported; point at the backup
        for mover in batch.done:
            log.error(f"already completed: {mover.plan.dst}")
        if backup:
            log.error(f"a copy of the original state is at {backup}")
        raise

    batch.report()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        sys.exit(130)
