#!/usr/bin/env python3
"""
claude-move — move or rename a project directory and carry its Claude Code
state along with it (transcripts, memory files, permissions, history).

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

This script performs the move and rewrites every reference.  Rather than
enumerating the state files it knows about (a list that goes stale every time
Claude Code grows a new directory), it scans ~/.claude for files that mention
the old path and rewrites those.

Usage:
    claude-move.py /old/path /new/path            # move files + state
    claude-move.py /old/path /new/path --dry-run  # show the plan only
    claude-move.py /old/path /new/path --state-only   # folder already moved
    claude-move.py --list                         # show known projects

Stdlib only, Python 3.8+.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import sys
import time
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

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

# ---------------------------------------------------------------------------
# path encoding
# ---------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]")
_CWD_RE = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')


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
    """Ordered set of literal string substitutions applied to every string in
    a JSON document (keys included) or to raw text.

    Replacements are applied longest-old-first so that the more specific
    strings (the state directory, subproject paths) win over the shorter ones
    they contain.
    """

    def __init__(self) -> None:
        self._pairs: List[Tuple[str, str]] = []
        self.hits = 0

    def add(self, old: str, new: str) -> None:
        self._add(old, new)
        # a path containing characters JSON escapes (non-ASCII, quotes,
        # backslashes) appears in transcripts in escaped form; match that
        # spelling too, so the raw-line fast path in _rewrite_jsonl stays sound
        self._add(json.dumps(old)[1:-1], json.dumps(new)[1:-1])

    def _add(self, old: str, new: str) -> None:
        if old and new and old != new and (old, new) not in self._pairs:
            self._pairs.append((old, new))
            self._pairs.sort(key=lambda pair: len(pair[0]), reverse=True)

    def text(self, value: str) -> str:
        out = value
        for old, new in self._pairs:
            if old in out:
                out = out.replace(old, new)
        if out != value:
            self.hits += 1
        return out

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
        return any(old in blob for old, _ in self._pairs)


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


def memory_files(state_dir: str) -> List[str]:
    memory = os.path.join(state_dir, "memory")
    return sorted(os.listdir(memory)) if os.path.isdir(memory) else []


def known_projects(layout: Layout) -> Set[str]:
    """Every project path Claude knows about, from ~/.claude.json plus the cwd
    recorded inside transcripts (which catches projects the config forgot)."""
    found: Set[str] = set()
    try:
        cfg = json.loads(read_text(layout.config) or "{}")
        found.update(norm(p) for p in (cfg.get("projects") or {}))
    except (ValueError, AttributeError):
        pass

    for state in state_dirs(layout):
        for cwd in cwds_in_state_dir(state):
            # a session can run with its cwd inside ~/.claude (e.g. a subagent
            # editing memory); that is not a project
            if not is_under(cwd, layout.dir):
                found.add(cwd)
    return found


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
    def __init__(self, args: argparse.Namespace, layout: Layout, log: Log) -> None:
        self.args = args
        self.layout = layout
        self.log = log
        self.src = norm(args.src)
        self.dst = norm(args.dst)
        # ~/.claude.json lives in the home dir; deriving it this way keeps
        # --config overrides self-consistent.
        self.home = norm(os.path.dirname(layout.config))
        self.mappings: List[Tuple[str, str]] = []      # (old project path, new)
        self.state_moves: List[Tuple[str, str]] = []   # (old state dir, new)
        self.targets: List[str] = []                   # files naming the old path
        self.rewriter = Rewriter()
        self.move_files = not args.state_only
        self.blockers: List[Tuple[str, bool]] = []     # (message, fatal)

    # -- build ------------------------------------------------------------

    def build(self) -> None:
        projects = known_projects(self.layout)   # scans every transcript; do it once
        self._resolve_mappings(projects)
        self._build_rewriter()
        self._check_files()
        self.targets = self._discover_targets()
        self._check_live_sessions()
        self._check_collisions(projects)
        self._check_state_dest()

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
            self.rewriter.add(self.layout.state_dir(old), self.layout.state_dir(new))
            self.rewriter.add(old, new)
            old_tilde, new_tilde = tilde(old, self.home), tilde(new, self.home)
            if old_tilde and new_tilde:
                self.rewriter.add(old_tilde, new_tilde)
            # the bare encoded token, e.g. inside scratchpad paths under /tmp
            self.rewriter.add(encode_path(old), encode_path(new))

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
            if dst_exists and os.listdir(self.dst):
                self.block(
                    f"destination already exists and is not empty: {self.dst}\n"
                    f"           move the folder yourself, then re-run with --state-only",
                    fatal=True)
            if src_exists and is_under(self.dst, self.src):
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
                    f"           (Claude collapses _, spaces and . to -).  Their transcripts "
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
        self.files_rewritten = 0
        self.backups_renamed = 0
        self.conflicts: List[str] = []
        self.stale: List[str] = []

    # -- backup -----------------------------------------------------------

    def backup(self) -> Optional[str]:
        if self.args.no_backup:
            return None
        root = os.path.join(self.layout.dir, "claude-move-backups",
                            time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(os.path.join(root, "files"), exist_ok=True)
        for path in [self.layout.config] + self.plan.targets:
            if os.path.isfile(path):
                shutil.copy2(path, unique(os.path.join(root, "files", os.path.basename(path))))
        for old_state, _ in self.plan.state_moves:
            shutil.copytree(old_state,
                            os.path.join(root, "projects", os.path.basename(old_state)),
                            dirs_exist_ok=True)
            for sid in session_ids(old_state):
                blobs = os.path.join(self.layout.file_history, sid)
                if os.path.isdir(blobs):
                    shutil.copytree(blobs, os.path.join(root, "file-history", sid),
                                    dirs_exist_ok=True)
        return root

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
                self._rewrite_jsonl(path)
            elif path.endswith(".json"):
                self._rewrite_json(path)
            else:
                self._rewrite_text(path)

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
                    renames.update(self._rewrite_jsonl(path))
                else:
                    self._rewrite_text(path)

        for sid in session_ids(state_dir):
            blobs = os.path.join(self.layout.file_history, sid)
            for old_name, new_name in renames.items():
                source = os.path.join(blobs, old_name)
                target = os.path.join(blobs, new_name)
                if os.path.exists(source) and not os.path.exists(target):
                    os.rename(source, target)
                    self.backups_renamed += 1

    def _rewrite_jsonl(self, path: str) -> Dict[str, str]:
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
                    if not self.rewriter.touches(line):
                        out.write(line)
                        continue
                    changed = True
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        out.write(self._rewritten(path, line))
                        continue
                    for file_path, meta in tracked_backups(rec):
                        new_path = self.rewriter.text(file_path)
                        if new_path == file_path:
                            continue
                        # keep the blob name consistent with the new path, in
                        # the same pass that rewrites the path itself
                        old_name = meta["backupFileName"]
                        renames[old_name] = meta["backupFileName"] = \
                            backup_file_name(new_path, old_name)
                    encoded = json.dumps(self.rewriter.obj(rec), ensure_ascii=False)
                    out.write(self._note_stale(path, encoded) + "\n")
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

    def _rewrite_json(self, path: str) -> None:
        blob = read_text(path)
        if blob is None or not self.rewriter.touches(blob):
            return
        try:
            data = self.rewriter.obj(json.loads(blob))
        except ValueError:
            self._rewrite_text(path)
            return
        write_json_atomic(path, data)
        self._note_stale(path, json.dumps(data, ensure_ascii=False))
        self.files_rewritten += 1

    def _rewrite_text(self, path: str) -> None:
        text = read_text(path)
        if text is None or not self.rewriter.touches(text):
            return
        write_text_atomic(path, self._rewritten(path, text))
        self.files_rewritten += 1

    def _rewritten(self, path: str, text: str) -> str:
        return self._note_stale(path, self.rewriter.text(text))

    def _note_stale(self, path: str, text: str) -> str:
        """Record anything still naming the old path after rewriting, so
        verification is a byproduct of the write pass rather than a third
        walk over everything it just wrote."""
        if path not in self.stale and self.rewriter.touches(text):
            self.stale.append(path)
        return text

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
            self.files_rewritten += 1
        write_json_atomic(path, cfg)
        self._note_stale(path, json.dumps(cfg, ensure_ascii=False))

    # -- verify -----------------------------------------------------------

    def verify(self) -> List[str]:
        """Anything still pointing at the old location.  The rewrite pass
        already recorded residual matches as it wrote; only the directory
        checks are left."""
        return self.stale + [
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
# CLI
# ---------------------------------------------------------------------------


def cmd_list(layout: Layout, log: Log) -> int:
    projects = sorted(known_projects(layout))
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


def confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-move",
        description="Move or rename a project directory and carry its Claude Code "
                    "state (transcripts, memory, permissions, history) with it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  claude-move.py ~/dev/api ~/work/api-server     move and rename
  claude-move.py ~/dev/api ~/work/api --dry-run  show the plan, change nothing
  claude-move.py ~/dev/api ~/work/api --state-only
                                                 folder already moved by hand
  claude-move.py --list                          list known projects
""")
    parser.add_argument("src", nargs="?", help="current project path")
    parser.add_argument("dst", nargs="?", help="new project path (rename included)")
    parser.add_argument("--list", action="store_true", help="list known projects and exit")
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
    parser = build_parser()
    args = parser.parse_args(argv)
    log = Log(quiet=args.quiet)
    layout = Layout(args.claude_dir, args.config)

    if args.list:
        return cmd_list(layout, log)
    if not args.src or not args.dst:
        parser.print_usage(sys.stderr)
        log.error("both a source and a destination path are required (or use --list)")
        return 2
    if not os.path.isdir(layout.dir):
        log.error(f"no Claude state directory at {layout.dir}")
        return 1

    plan = Plan(args, layout, log)
    if plan.src == plan.dst:
        log.error("source and destination are the same path")
        return 2
    plan.build()
    plan.describe()

    if plan.blockers:
        log.info()
        for message, _fatal in plan.blockers:
            log.error(f"blocked: {message}")
        fatal = any(fatal for _, fatal in plan.blockers)
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
    mover = Mover(plan)
    backup = mover.backup()
    if backup:
        log.step(f"backed up current state to {backup}")

    try:
        mover.move_project_files()
        mover.move_state_dirs()
        mover.rewrite_everything()
    except Exception as exc:  # noqa: BLE001 - report and point at the backup
        log.error(f"failed partway through: {exc}")
        if backup:
            log.error(f"a copy of the original state is at {backup}")
        raise

    stale = mover.verify()

    log.info()
    log.info("Done")
    log.step(f"{plan.dst}")
    log.step(f"state dir: {layout.state_dir(plan.dst)}")
    log.step(f"{mover.files_rewritten} file(s) rewritten, "
             f"{mover.backups_renamed} file-history backup(s) renamed")
    if mover.conflicts:
        log.info()
        log.info("Merge conflicts kept side by side (review these):")
        for item in mover.conflicts:
            log.step(item)
    if stale:
        log.info()
        log.info("Still referencing the old path (probably harmless, e.g. quoted text):")
        for item in stale[:20]:
            log.step(item)
        if len(stale) > 20:
            log.step(f"... and {len(stale) - 20} more")
    log.info()
    log.info(f"Start Claude there with:  cd {plan.dst} && claude --continue")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        sys.exit(130)
