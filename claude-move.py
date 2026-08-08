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
  ~/.claude/session-env/<session-id>/      per-session env
  ~/.claude/sessions/<pid>.json            live session records (cwd)

where <encoded-path> is the absolute path with every non-alphanumeric
character replaced by "-".  Moving a project with `mv` alone orphans all of
it: Claude starts the new location with empty memory and no permissions.

This script performs the move and rewrites every reference.

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
from typing import Any, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# path encoding
# ---------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]")


def encode_path(path: str) -> str:
    """Encode an absolute path the way Claude Code names its project dirs.

    /Users/me/Documents/my_app  ->  -Users-me-Documents-my-app

    Note this is lossy: "my_app", "my app" and "my-app" all collapse to the
    same directory name.  collision checks below account for that.
    """
    return _NON_ALNUM.sub("-", path)


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

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        sys.stdout.flush()
        print(f"  warning: {msg}", file=sys.stderr)
        sys.stderr.flush()

    def error(self, msg: str) -> None:
        sys.stdout.flush()
        print(f"error: {msg}", file=sys.stderr)
        sys.stderr.flush()


def read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json_atomic(path: str, data: Any) -> None:
    """Write JSON via a temp file + rename, so a crash or a concurrent reader
    never sees a half-written ~/.claude.json."""
    tmp = f"{path}.claude-move.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    if os.path.exists(path):
        shutil.copystat(path, tmp)
    os.replace(tmp, path)


def write_text_atomic(path: str, text: str) -> None:
    tmp = f"{path}.claude-move.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    if os.path.exists(path):
        shutil.copystat(path, tmp)
    os.replace(tmp, path)


def looks_binary(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\0" in fh.read(8192)
    except OSError:
        return True


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


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
        if old and new and old != new and (old, new) not in self._pairs:
            self._pairs.append((old, new))
            self._pairs.sort(key=lambda pair: len(pair[0]), reverse=True)

    @property
    def pairs(self) -> List[Tuple[str, str]]:
        return list(self._pairs)

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
            return {self.text(k) if isinstance(k, str) else k: self.obj(v) for k, v in value.items()}
        return value

    def touches(self, blob: str) -> bool:
        return any(old in blob for old, _ in self._pairs)


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


class Layout:
    """Locations of Claude Code's on-disk state."""

    def __init__(self, claude_dir: str, config_path: str) -> None:
        self.dir = norm(claude_dir)
        self.config = norm(config_path)
        self.projects = os.path.join(self.dir, "projects")
        self.history = os.path.join(self.dir, "history.jsonl")
        self.sessions = os.path.join(self.dir, "sessions")
        self.file_history = os.path.join(self.dir, "file-history")
        self.session_env = os.path.join(self.dir, "session-env")
        self.todos = os.path.join(self.dir, "todos")

    def state_dir(self, project_path: str) -> str:
        return os.path.join(self.projects, encode_path(project_path))


def known_projects(layout: Layout) -> Dict[str, Dict[str, Any]]:
    """Every project path Claude knows about, from ~/.claude.json plus the cwd
    recorded inside transcripts (which catches projects the config forgot)."""
    found: Dict[str, Dict[str, Any]] = {}

    def note(path: str, source: str) -> None:
        entry = found.setdefault(path, {"sources": set(), "sessions": 0, "memory": 0})
        entry["sources"].add(source)

    if os.path.exists(layout.config):
        try:
            cfg = read_json(layout.config)
            for path in (cfg.get("projects") or {}):
                note(norm(path), "config")
        except (ValueError, OSError):
            pass

    if os.path.isdir(layout.projects):
        for encoded in sorted(os.listdir(layout.projects)):
            state = os.path.join(layout.projects, encoded)
            if not os.path.isdir(state):
                continue
            for cwd in cwds_in_state_dir(state):
                # a session can be run with its cwd inside ~/.claude (e.g. a
                # subagent editing memory); that is not a project
                if is_under(cwd, layout.dir):
                    continue
                note(cwd, "transcript")

    for path, entry in found.items():
        state = layout.state_dir(path)
        if os.path.isdir(state):
            entry["sessions"] = len([f for f in os.listdir(state) if f.endswith(".jsonl")])
            memory = os.path.join(state, "memory")
            if os.path.isdir(memory):
                entry["memory"] = len(os.listdir(memory))
    return found


def cwds_in_state_dir(state_dir: str, limit_lines: int = 400) -> Set[str]:
    """Pull the cwd values recorded in a state directory's transcripts."""
    out: Set[str] = set()
    try:
        names = [f for f in os.listdir(state_dir) if f.endswith(".jsonl")]
    except OSError:
        return out
    for name in names:
        try:
            with open(os.path.join(state_dir, name), "r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i >= limit_lines:
                        break
                    if '"cwd"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    cwd = rec.get("cwd")
                    if isinstance(cwd, str) and cwd.startswith("/"):
                        out.add(norm(cwd))
        except OSError:
            continue
    return out


def session_ids(state_dir: str) -> List[str]:
    if not os.path.isdir(state_dir):
        return []
    return sorted(f[:-len(".jsonl")] for f in os.listdir(state_dir) if f.endswith(".jsonl"))


def live_sessions(layout: Layout) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(layout.sessions):
        return out
    for name in sorted(os.listdir(layout.sessions)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(layout.sessions, name)
        try:
            rec = read_json(path)
        except (ValueError, OSError):
            continue
        rec["_file"] = path
        rec["_alive"] = isinstance(rec.get("pid"), int) and pid_alive(rec["pid"])
        out.append(rec)
    return out


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
        self.rewriter = Rewriter()
        self.move_files = not args.state_only
        self.backup_dir: Optional[str] = None
        self.blockers: List[str] = []

    # -- build ------------------------------------------------------------

    def build(self) -> None:
        self._resolve_mappings()
        self._build_rewriter()
        self._check_files()
        self._check_live_sessions()
        self._check_collisions()
        self._check_state_dest()

    def _resolve_mappings(self) -> None:
        self.mappings.append((self.src, self.dst))
        if self.args.subprojects:
            for path in sorted(known_projects(self.layout)):
                if path != self.src and is_under(path, self.src):
                    self.mappings.append((path, remap(path, self.src, self.dst)))
        for old, new in self.mappings:
            old_state = self.layout.state_dir(old)
            new_state = self.layout.state_dir(new)
            if os.path.isdir(old_state) and old_state != new_state:
                self.state_moves.append((old_state, new_state))

    def _build_rewriter(self) -> None:
        for old, new in self.mappings:
            old_state = self.layout.state_dir(old)
            new_state = self.layout.state_dir(new)
            # most specific first (the sort in Rewriter.add handles ordering)
            self.rewriter.add(old_state, new_state)
            self.rewriter.add(old, new)
            old_tilde, new_tilde = tilde(old, self.home), tilde(new, self.home)
            if old_tilde and new_tilde:
                self.rewriter.add(old_tilde, new_tilde)
            # the bare encoded token, e.g. inside scratchpad paths under /tmp
            self.rewriter.add(encode_path(old), encode_path(new))

    # -- safety checks ----------------------------------------------------

    def _check_files(self) -> None:
        src_exists = os.path.isdir(self.src)
        dst_exists = os.path.isdir(self.dst)

        if self.move_files and not src_exists and dst_exists:
            self.log.info(f"note: {self.src} is gone and {self.dst} exists -- "
                          f"assuming the folder was already moved (--state-only)")
            self.move_files = False

        if self.move_files:
            if not src_exists:
                self.blockers.append(f"source directory does not exist: {self.src}")
            if dst_exists and os.listdir(self.dst):
                self.blockers.append(
                    f"destination already exists and is not empty: {self.dst}\n"
                    f"           move the folder yourself, then re-run with --state-only")
            if src_exists and is_under(self.dst, self.src):
                self.blockers.append("destination is inside the source directory")
        elif not dst_exists:
            self.log.warn(f"destination folder does not exist yet: {self.dst}")

    def _check_live_sessions(self) -> None:
        touched = {old for old, _ in self.mappings}
        for rec in live_sessions(self.layout):
            cwd = rec.get("cwd")
            if not isinstance(cwd, str):
                continue
            if not any(is_under(norm(cwd), path) for path in touched):
                continue
            if rec.get("_alive"):
                name = rec.get("name") or rec.get("sessionId", "?")
                self.blockers.append(
                    f"a Claude Code session is live in this project (pid {rec.get('pid')}, {name}).\n"
                    f"           quit it first -- it holds ~/.claude.json in memory and will\n"
                    f"           write the old paths back when it exits.  Override with --force.")

    def _check_collisions(self) -> None:
        """The encoded name is lossy, so two distinct project paths can share
        one state directory.  Moving it would drag the other project's
        transcripts along."""
        by_encoded: Dict[str, Set[str]] = {}
        for path in known_projects(self.layout):
            by_encoded.setdefault(encode_path(path), set()).add(path)
        for old, _ in self.mappings:
            others = by_encoded.get(encode_path(old), set()) - {old}
            if others:
                self.log.warn(
                    f"{old} shares its state directory with: {', '.join(sorted(others))}\n"
                    f"           (Claude collapses _, spaces and . to -).  Their transcripts "
                    f"will move too.")
        for old, new in self.mappings:
            clashes = by_encoded.get(encode_path(new), set()) - {new, old}
            if clashes:
                self.log.warn(f"the new path collides with existing project(s): "
                              f"{', '.join(sorted(clashes))}")

    def _check_state_dest(self) -> None:
        for _, new_state in self.state_moves:
            if os.path.isdir(new_state) and os.listdir(new_state) and not self.args.merge:
                self.blockers.append(
                    f"state already exists for the new path: {new_state}\n"
                    f"           (you have run Claude there already).  Re-run with --merge "
                    f"to combine them.")

    # -- description ------------------------------------------------------

    def describe(self) -> None:
        log = self.log
        log.info()
        log.info("Project")
        log.info(f"  from  {self.src}")
        log.info(f"  to    {self.dst}")

        log.info()
        log.info("Files")
        if self.move_files:
            log.step(f"move {self.src}  ->  {self.dst}")
        else:
            log.step("(skipped -- folder already in place)")

        log.info()
        log.info("Claude state")
        if not self.state_moves:
            log.step("no state directory found -- nothing recorded for this project yet")
        for old_state, new_state in self.state_moves:
            sessions = len(session_ids(old_state))
            memory_dir = os.path.join(old_state, "memory")
            mem = len(os.listdir(memory_dir)) if os.path.isdir(memory_dir) else 0
            verb = "merge into" if os.path.isdir(new_state) and os.listdir(new_state) else "move to"
            log.step(f"{os.path.basename(old_state)}")
            log.step(f"    {sessions} transcript(s), {mem} memory file(s)")
            log.step(f"    {verb} {os.path.basename(new_state)}")

        if len(self.mappings) > 1:
            log.info()
            log.info("Subprojects also remapped")
            for old, new in self.mappings[1:]:
                log.step(f"{old}  ->  {new}")

        log.info()
        log.info("References rewritten in")
        for target in self._rewrite_targets(existing_only=True):
            log.step(self.shorten(target))
        log.step("moved transcripts, memory files and per-session env")

    def shorten(self, path: str) -> str:
        return tilde(path, self.home) or path

    # -- targets ----------------------------------------------------------

    def _rewrite_targets(self, existing_only: bool = False) -> List[str]:
        targets = [self.layout.config, self.layout.history]
        if os.path.isdir(self.layout.sessions):
            targets += [os.path.join(self.layout.sessions, f)
                        for f in sorted(os.listdir(self.layout.sessions)) if f.endswith(".json")]
        if os.path.isdir(self.layout.todos):
            targets += [os.path.join(self.layout.todos, f)
                        for f in sorted(os.listdir(self.layout.todos)) if f.endswith(".json")]
        if existing_only:
            targets = [t for t in targets if os.path.exists(t)]
        return targets


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

    # -- backup -----------------------------------------------------------

    def backup(self) -> Optional[str]:
        if self.args.no_backup:
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        root = os.path.join(self.layout.dir, "claude-move-backups", stamp)
        os.makedirs(root, exist_ok=True)
        for path in (self.layout.config, self.layout.history):
            if os.path.exists(path):
                shutil.copy2(path, os.path.join(root, os.path.basename(path)))
        for old_state, _ in self.plan.state_moves:
            dest = os.path.join(root, "projects", os.path.basename(old_state))
            shutil.copytree(old_state, dest, dirs_exist_ok=True)
            for sid in session_ids(old_state):
                fh_dir = os.path.join(self.layout.file_history, sid)
                if os.path.isdir(fh_dir):
                    shutil.copytree(fh_dir, os.path.join(root, "file-history", sid),
                                    dirs_exist_ok=True)
        self.plan.backup_dir = root
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
            if os.path.isdir(new_state) and os.listdir(new_state):
                self._merge_tree(old_state, new_state)
                self.log.step(f"merged {os.path.basename(old_state)} -> "
                              f"{os.path.basename(new_state)}")
            else:
                if os.path.isdir(new_state):
                    os.rmdir(new_state)
                os.makedirs(os.path.dirname(new_state), exist_ok=True)
                shutil.move(old_state, new_state)
                self.log.step(f"moved {os.path.basename(old_state)} -> "
                              f"{os.path.basename(new_state)}")

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
        """Append the pointer lines the destination index is missing."""
        try:
            incoming = open(source, "r", encoding="utf-8", errors="replace").read()
            existing = open(target, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            return
        have = {line.strip() for line in existing.splitlines() if line.strip()}
        added = [line for line in incoming.splitlines()
                 if line.strip() and line.strip() not in have]
        if not added:
            return
        text = existing if existing.endswith("\n") else existing + "\n"
        text += "\n".join(added) + "\n"
        write_text_atomic(target, text)
        self.log.step(f"merged {len(added)} line(s) into memory/MEMORY.md")

    # -- rewriting --------------------------------------------------------

    def rewrite_everything(self) -> None:
        for target in self.plan._rewrite_targets(existing_only=True):
            if target == self.layout.config:
                self._rewrite_config()
            elif target.endswith(".jsonl"):
                self._rewrite_jsonl(target)
            else:
                self._rewrite_json(target)

        for _old_state, new_state in self.plan.state_moves:
            self._rewrite_state_dir(new_state)

        if self.args.project_settings:
            self._rewrite_project_settings()

    def _rewrite_config(self) -> None:
        """~/.claude.json: rename the projects key (merging if the new key
        already exists) then sweep the rest of the file for stale paths."""
        path = self.layout.config
        try:
            cfg = read_json(path)
        except (ValueError, OSError) as exc:
            self.log.warn(f"could not read {path}: {exc}")
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

        before = json.dumps(cfg, ensure_ascii=False)
        cfg = self.rewriter.obj(cfg)
        after = json.dumps(cfg, ensure_ascii=False)
        if before != after:
            self.files_rewritten += 1
        write_json_atomic(path, cfg)

    def _rewrite_json(self, path: str) -> None:
        try:
            blob = open(path, "r", encoding="utf-8").read()
        except OSError:
            return
        if not self.rewriter.touches(blob):
            return
        try:
            data = json.loads(blob)
        except ValueError:
            write_text_atomic(path, self.rewriter.text(blob))
            self.files_rewritten += 1
            return
        write_json_atomic(path, self.rewriter.obj(data))
        self.files_rewritten += 1

    def _rewrite_jsonl(self, path: str) -> Dict[str, Dict[str, Any]]:
        """Rewrite a JSONL file line by line.  Returns the file-history backup
        entries seen, so their on-disk names can be fixed up afterwards."""
        backups: Dict[str, Dict[str, Any]] = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError as exc:
            self.log.warn(f"could not read {path}: {exc}")
            return backups

        changed = False
        out: List[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                out.append(line)
                continue
            try:
                rec = json.loads(stripped)
            except ValueError:
                new_line = self.rewriter.text(line)
                changed = changed or new_line != line
                out.append(new_line)
                continue

            tracked = (rec.get("snapshot") or {}).get("trackedFileBackups")
            if isinstance(tracked, dict):
                for file_path, meta in tracked.items():
                    if isinstance(meta, dict) and isinstance(meta.get("backupFileName"), str):
                        new_path = self.rewriter.text(file_path)
                        if new_path != file_path:
                            backups[meta["backupFileName"]] = {
                                "old_path": file_path, "new_path": new_path,
                                "version": meta.get("version"),
                            }

            new_rec = self.rewriter.obj(rec)
            new_line = json.dumps(new_rec, ensure_ascii=False) + "\n"
            if json.dumps(rec, ensure_ascii=False) + "\n" != new_line:
                changed = True
            out.append(new_line)

        if changed:
            write_text_atomic(path, "".join(out))
            self.files_rewritten += 1
        return backups

    def _rewrite_state_dir(self, state_dir: str) -> None:
        """Transcripts, memory files, and each session's env directory."""
        renames: Dict[str, Dict[str, Any]] = {}
        for root, _dirs, files in os.walk(state_dir):
            for name in files:
                path = os.path.join(root, name)
                if name.endswith(".jsonl"):
                    renames.update(self._rewrite_jsonl(path))
                elif not looks_binary(path):
                    self._rewrite_text(path)

        for sid in session_ids(state_dir):
            env_dir = os.path.join(self.layout.session_env, sid)
            if os.path.isdir(env_dir):
                for root, _dirs, files in os.walk(env_dir):
                    for name in files:
                        path = os.path.join(root, name)
                        if not looks_binary(path):
                            self._rewrite_text(path)

        self._rename_file_history(state_dir, renames)

    def _rewrite_text(self, path: str) -> None:
        try:
            text = open(path, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            return
        if not self.rewriter.touches(text):
            return
        write_text_atomic(path, self.rewriter.text(text))
        self.files_rewritten += 1

    def _rename_file_history(self, state_dir: str, renames: Dict[str, Dict[str, Any]]) -> None:
        """File backups are stored as sha256(abs path)[:16]@v<n>.  When the
        tracked path changes, rename the blob so the hash still matches."""
        if not renames:
            return
        for sid in session_ids(state_dir):
            fh_dir = os.path.join(self.layout.file_history, sid)
            if not os.path.isdir(fh_dir):
                continue
            for old_name, info in renames.items():
                src = os.path.join(fh_dir, old_name)
                if not os.path.exists(src):
                    continue
                suffix = old_name.split("@", 1)[1] if "@" in old_name else ""
                digest = hashlib.sha256(info["new_path"].encode("utf-8")).hexdigest()[:16]
                new_name = f"{digest}@{suffix}" if suffix else digest
                dst = os.path.join(fh_dir, new_name)
                if os.path.exists(dst):
                    continue
                os.rename(src, dst)
                self.backups_renamed += 1

        # the transcripts still name the old blobs -- fix them up
        for root, _dirs, files in os.walk(state_dir):
            for name in files:
                if name.endswith(".jsonl"):
                    self._fix_backup_names(os.path.join(root, name))

    def _fix_backup_names(self, path: str) -> None:
        try:
            lines = open(path, "r", encoding="utf-8", errors="replace").readlines()
        except OSError:
            return
        changed = False
        out: List[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped or '"trackedFileBackups"' not in stripped:
                out.append(line)
                continue
            try:
                rec = json.loads(stripped)
            except ValueError:
                out.append(line)
                continue
            tracked = (rec.get("snapshot") or {}).get("trackedFileBackups") or {}
            for file_path, meta in tracked.items():
                if not isinstance(meta, dict) or not isinstance(meta.get("backupFileName"), str):
                    continue
                old_name = meta["backupFileName"]
                suffix = old_name.split("@", 1)[1] if "@" in old_name else ""
                digest = hashlib.sha256(file_path.encode("utf-8")).hexdigest()[:16]
                new_name = f"{digest}@{suffix}" if suffix else digest
                if new_name != old_name:
                    meta["backupFileName"] = new_name
                    changed = True
            out.append(json.dumps(rec, ensure_ascii=False) + "\n")
        if changed:
            write_text_atomic(path, "".join(out))

    def _rewrite_project_settings(self) -> None:
        """The project's own .claude/ travels with the folder, but its settings
        can hold absolute paths (hooks, permissions)."""
        settings_dir = os.path.join(self.plan.dst, ".claude")
        if not os.path.isdir(settings_dir):
            return
        for root, _dirs, files in os.walk(settings_dir):
            for name in files:
                if name.endswith((".json", ".md", ".sh", ".toml", ".yaml", ".yml")):
                    path = os.path.join(root, name)
                    if not looks_binary(path):
                        self._rewrite_text(path)

    # -- verify -----------------------------------------------------------

    def verify(self) -> List[str]:
        """Report anything still pointing at the old location."""
        stale: List[str] = []
        needles = [old for old, _ in self.rewriter.pairs]

        def scan(path: str) -> None:
            try:
                if looks_binary(path):
                    return
                blob = open(path, "r", encoding="utf-8", errors="replace").read()
            except OSError:
                return
            if any(n in blob for n in needles):
                stale.append(path)

        for path in self.plan._rewrite_targets(existing_only=True):
            scan(path)
        for _old, new_state in self.plan.state_moves:
            for root, _dirs, files in os.walk(new_state):
                for name in files:
                    scan(os.path.join(root, name))
        for old, _new in self.plan.mappings:
            if os.path.isdir(self.layout.state_dir(old)):
                stale.append(self.layout.state_dir(old) + "  (old state dir still present)")
        return stale


def merge_settings(incoming: Any, existing: Any) -> Any:
    """Merge two ~/.claude.json project entries.  Lists are unioned (so
    allowedTools from both survive); on scalars the destination wins."""
    if isinstance(incoming, dict) and isinstance(existing, dict):
        out = dict(existing)
        for key, value in incoming.items():
            out[key] = merge_settings(value, existing[key]) if key in existing else value
        return out
    if isinstance(incoming, list) and isinstance(existing, list):
        out = list(existing)
        for item in incoming:
            if item not in out:
                out.append(item)
        return out
    return existing


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_list(layout: Layout, log: Log) -> int:
    projects = known_projects(layout)
    if not projects:
        log.info("no Claude Code projects found")
        return 0
    width = max(len(p) for p in projects)
    log.info(f"{'project'.ljust(width)}  sessions  memory  encoded")
    for path in sorted(projects):
        info = projects[path]
        log.info(f"{path.ljust(width)}  {info['sessions']:>8}  {info['memory']:>6}  "
                 f"{encode_path(path)}")
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
    args = build_parser().parse_args(argv)
    log = Log(quiet=args.quiet)
    layout = Layout(args.claude_dir, args.config)

    if args.list:
        return cmd_list(layout, log)
    if not args.src or not args.dst:
        build_parser().print_usage(sys.stderr)
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
        sys.stdout.flush()
        for blocker in plan.blockers:
            print(f"blocked: {blocker}", file=sys.stderr)
        sys.stderr.flush()
        if not args.force:
            log.info()
            log.error("nothing was changed.  fix the above, or pass --force.")
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
