#!/usr/bin/env python3
"""Regression suite for claude-move.py.

Builds a synthetic ~/.claude tree mirroring the real layout, runs the tool
against it, and asserts the migration is complete.  Nothing outside the
temporary directory is touched -- the tool is always invoked with
--claude-dir/--config pointed at the fixture.

    python3 tests/test_claude_move.py            # temp dir, cleaned up
    python3 tests/test_claude_move.py /tmp/keep  # keep the fixtures to inspect
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "claude-move.py")
FAILS = []


def enc(path):
    """The same lossy encoding Claude Code uses to name project state dirs."""
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def ok(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


def run(home, *args):
    return subprocess.run(
        [sys.executable, SCRIPT, *args,
         "--claude-dir", os.path.join(home, ".claude"),
         "--config", os.path.join(home, ".claude.json")],
        capture_output=True, text=True)


def fixture(root, tag):
    """A synthetic home containing one project, one nested subproject, and a
    sample of every kind of state that references a project path."""
    home = os.path.join(root, tag, "home")
    shutil.rmtree(os.path.join(root, tag), ignore_errors=True)
    claude = os.path.join(home, ".claude")
    proj = os.path.join(home, "dev", "my_app")
    sub = os.path.join(proj, "packages", "core")
    for path in (sub, os.path.join(proj, ".claude")):
        os.makedirs(path, exist_ok=True)
    open(os.path.join(proj, "main.py"), "w").write("print('hi')\n")
    json.dump({"hooks": {"Stop": [{"hooks": [{"type": "command",
              "command": f"{proj}/scripts/notify.sh"}]}]}},
              open(os.path.join(proj, ".claude", "settings.local.json"), "w"))

    sid, subsid = "aaaa-1111", "bbbb-2222"
    state = os.path.join(claude, "projects", enc(proj))
    substate = os.path.join(claude, "projects", enc(sub))
    memory = os.path.join(state, "memory")
    os.makedirs(memory, exist_ok=True)
    os.makedirs(substate, exist_ok=True)
    open(os.path.join(memory, "MEMORY.md"), "w").write(f"- [Deploy](deploy.md) — {proj}/deploy.sh\n")
    open(os.path.join(memory, "deploy.md"), "w").write(f"Run from {proj}. See [[other]].\n")

    memfile = os.path.join(memory, "MEMORY.md")
    blob = hashlib.sha256(memfile.encode()).hexdigest()[:16] + "@v1"
    blobs = os.path.join(claude, "file-history", sid)
    os.makedirs(blobs, exist_ok=True)
    # a file-history blob is a copy of the USER'S source file; its contents
    # must never be rewritten, only its name
    open(os.path.join(blobs, blob), "w").write(f"literal text naming {proj} must survive\n")

    records = [
        {"type": "mode", "mode": "normal", "sessionId": sid},
        {"type": "system", "cwd": proj, "sessionId": sid, "content": f"cd {proj} && pytest"},
        {"type": "user", "cwd": proj,
         "message": {"role": "user", "content": "open ~/dev/my_app/main.py"}},
        {"type": "user", "cwd": proj, "message": {"role": "user", "content": "unrelated line"}},
        {"type": "file-history-snapshot", "messageId": "m1",
         "snapshot": {"messageId": "m1", "trackedFileBackups": {
             memfile: {"backupFileName": blob, "version": 1, "realParentDir": memory}}}},
    ]
    with open(os.path.join(state, sid + ".jsonl"), "w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    open(os.path.join(substate, subsid + ".jsonl"), "w").write(
        json.dumps({"type": "system", "cwd": sub}) + "\n")

    os.makedirs(os.path.join(claude, "session-env", sid), exist_ok=True)
    open(os.path.join(claude, "session-env", sid, "env"), "w").write(f"PWD={proj}\n")
    with open(os.path.join(claude, "history.jsonl"), "w") as fh:
        fh.write(json.dumps({"display": "fix parser", "project": proj}) + "\n")
        fh.write(json.dumps({"display": "other", "project": "/other/proj"}) + "\n")
    os.makedirs(os.path.join(claude, "sessions"), exist_ok=True)
    json.dump({"pid": 999999, "sessionId": sid, "cwd": proj, "name": "stale"},
              open(os.path.join(claude, "sessions", "999999.json"), "w"))
    # state an allowlist of known directories would miss
    os.makedirs(os.path.join(claude, "jobs", "job1"), exist_ok=True)
    json.dump({"cwd": proj, "transcript": f"{claude}/projects/{enc(proj)}/{sid}.jsonl"},
              open(os.path.join(claude, "jobs", "job1", "state.json"), "w"))
    json.dump({"numStartups": 5,
               "githubRepoPaths": {proj: "git@gh:me/app.git"},
               "projects": {proj: {"allowedTools": ["Bash(pytest:*)"],
                                   "mcpServers": {"db": {"command": f"{proj}/bin/db"}},
                                   "hasTrustDialogAccepted": True},
                            sub: {"allowedTools": ["Bash(npm:*)"]},
                            "/other/proj": {"allowedTools": ["Read"]}}},
              open(os.path.join(home, ".claude.json"), "w"))
    return home, proj, sub, sid


def test_core_migration(root):
    print("== core migration ==")
    home, old, sub, sid = fixture(root, "core")
    new = os.path.join(home, "work", "api-server")
    result = run(home, old, new, "-y")
    ok(result.returncode == 0, f"exit 0 (stderr: {result.stderr.strip()[:200]})")

    state = os.path.join(home, ".claude", "projects", enc(new))
    cfg = json.load(open(os.path.join(home, ".claude.json")))
    ok(not os.path.exists(old) and os.path.exists(os.path.join(new, "main.py")),
       "project folder moved")
    ok(new in cfg["projects"] and old not in cfg["projects"], "config key renamed")
    ok(cfg["projects"][new]["allowedTools"] == ["Bash(pytest:*)"], "permissions preserved")
    ok(cfg["projects"][new]["mcpServers"]["db"]["command"] == f"{new}/bin/db",
       "mcp server path rewritten")
    ok(f"{new}/packages/core" in cfg["projects"], "subproject key renamed")
    ok(cfg["projects"]["/other/proj"] == {"allowedTools": ["Read"]}, "other project untouched")
    ok(list(cfg["githubRepoPaths"]) == [new], "githubRepoPaths rewritten")
    ok(os.path.isdir(state), "state dir moved")
    ok(not os.path.isdir(os.path.join(home, ".claude", "projects", enc(old))),
       "old state dir gone")
    ok(os.path.isdir(os.path.join(home, ".claude", "projects", enc(sub.replace(old, new)))),
       "subproject state moved")
    ok(new in open(os.path.join(state, "memory", "deploy.md")).read(), "memory bodies rewritten")
    ok(os.path.exists(os.path.join(state, "memory", "MEMORY.md")), "memory files preserved")

    recs = [json.loads(l) for l in open(os.path.join(state, sid + ".jsonl"))]
    ok(len(recs) == 5, "no transcript records lost")
    ok(recs[1]["cwd"] == new and recs[1]["content"] == f"cd {new} && pytest",
       "transcript cwd and body rewritten")
    ok(recs[2]["message"]["content"] == "open ~/work/api-server/main.py", "tilde form rewritten")
    ok(recs[3]["message"]["content"] == "unrelated line", "unmatched line preserved verbatim")

    snap = recs[4]["snapshot"]["trackedFileBackups"]
    key = list(snap)[0]
    want = hashlib.sha256(key.encode()).hexdigest()[:16] + "@v1"
    ok(key == os.path.join(state, "memory", "MEMORY.md"), "tracked path key rewritten")
    ok(snap[key]["realParentDir"] == os.path.join(state, "memory"), "realParentDir rewritten")
    ok(snap[key]["backupFileName"] == want, "backupFileName re-hashed")
    blob = os.path.join(home, ".claude", "file-history", sid, want)
    ok(os.path.exists(blob), "backup blob renamed to the new hash")
    ok(os.path.exists(blob) and old in open(blob).read(), "backup blob CONTENTS untouched")

    history = [json.loads(l) for l in open(os.path.join(home, ".claude", "history.jsonl"))]
    ok([h["project"] for h in history] == [new, "/other/proj"], "history tags rewritten")
    ok(json.load(open(os.path.join(home, ".claude", "sessions", "999999.json")))["cwd"] == new,
       "stale session record rewritten")
    ok(f"{new}/scripts/notify.sh" in
       open(os.path.join(new, ".claude", "settings.local.json")).read(),
       "project's own .claude/ rewritten")
    ok(f"PWD={new}" in open(os.path.join(home, ".claude", "session-env", sid, "env")).read(),
       "session-env rewritten")
    job = json.load(open(os.path.join(home, ".claude", "jobs", "job1", "state.json")))
    ok(job["cwd"] == new and enc(new) in job["transcript"],
       "jobs/ state rewritten (an allowlist missed this)")
    ok(os.path.isdir(os.path.join(home, ".claude", "claude-move-backups")), "backup taken")

    leftover = subprocess.run(
        ["grep", "-rl", old,
         os.path.join(home, ".claude.json"),
         os.path.join(home, ".claude", "projects"),
         os.path.join(home, ".claude", "jobs"),
         os.path.join(home, ".claude", "history.jsonl")],
        capture_output=True, text=True).stdout
    ok(not leftover, f"no stale references remain ({leftover.strip()[:120]})")


def test_dry_run(root):
    print("== dry run changes nothing ==")
    home, old, _, _ = fixture(root, "dry")
    snapshot = lambda: subprocess.run(["find", home, "-type", "f"],
                                      capture_output=True, text=True).stdout
    before = snapshot()
    result = run(home, old, os.path.join(home, "work", "x"), "--dry-run")
    ok(result.returncode == 0 and before == snapshot(), "dry run is read-only")
    ok("dry run" in result.stdout, "dry run says so")


def test_merge(root):
    print("== destination state already exists ==")
    home, old, _, _ = fixture(root, "merge")
    new = os.path.join(home, "work", "api")
    dest = os.path.join(home, ".claude", "projects", enc(new))
    os.makedirs(os.path.join(dest, "memory"), exist_ok=True)
    open(os.path.join(dest, "c.jsonl"), "w").write("{}\n")
    open(os.path.join(dest, "memory", "MEMORY.md"), "w").write("- [Style](s.md)\n")

    result = run(home, old, new, "-y")
    ok(result.returncode != 0 and os.path.isdir(old), "refuses without --merge, changes nothing")

    result = run(home, old, new, "-y", "--merge")
    index = open(os.path.join(dest, "memory", "MEMORY.md")).read()
    ok(result.returncode == 0, "--merge proceeds")
    ok("- [Style](s.md)" in index and "Deploy" in index, "MEMORY.md index lines merged")
    ok(os.path.exists(os.path.join(dest, "aaaa-1111.jsonl")), "transcripts combined")
    cfg = json.load(open(os.path.join(home, ".claude.json")))
    ok(cfg["projects"][new]["allowedTools"] == ["Bash(pytest:*)"], "merged config keeps tools")


def test_blockers(root):
    print("== blockers ==")
    home, old, _, _ = fixture(root, "live")
    json.dump({"pid": os.getpid(), "cwd": old, "name": "live-one"},
              open(os.path.join(home, ".claude", "sessions", "999999.json"), "w"))
    result = run(home, old, os.path.join(home, "work", "api"), "-y")
    ok(result.returncode != 0 and "live" in result.stderr, "live session blocks the move")
    ok(os.path.isdir(old), "nothing moved when blocked")

    home, _, _, _ = fixture(root, "fatal")
    result = run(home, os.path.join(home, "nope"), os.path.join(home, "work", "api"),
                 "-y", "--force")
    ok(result.returncode != 0, "--force cannot override a missing source")
    ok(not os.path.isdir(os.path.join(home, ".claude", "claude-move-backups")),
       "fatal blocker refuses before taking a backup")


def test_already_moved(root):
    print("== folder already moved by hand ==")
    home, old, _, _ = fixture(root, "stateonly")
    new = os.path.join(home, "work", "api")
    os.makedirs(os.path.join(home, "work"), exist_ok=True)
    shutil.move(old, new)
    result = run(home, old, new, "-y")
    cfg = json.load(open(os.path.join(home, ".claude.json")))
    ok(result.returncode == 0 and new in cfg["projects"], "auto-detects an already-moved folder")


def test_escaped_non_ascii(root):
    """Claude writes transcripts with ensure_ascii=True, so a path containing
    non-ASCII appears escaped (café -> caf\\u00e9).  The raw-line fast path in
    _rewrite_jsonl must still match it."""
    print("== JSON-escaped non-ASCII path ==")
    home = os.path.join(root, "unicode", "home")
    shutil.rmtree(os.path.join(root, "unicode"), ignore_errors=True)
    claude = os.path.join(home, ".claude")
    proj = os.path.join(home, "dev", "café_app")
    os.makedirs(proj, exist_ok=True)
    state = os.path.join(claude, "projects", enc(proj))
    os.makedirs(state, exist_ok=True)
    os.makedirs(os.path.join(claude, "sessions"), exist_ok=True)
    with open(os.path.join(state, "s1.jsonl"), "w") as fh:
        fh.write(json.dumps({"type": "system", "cwd": proj}, ensure_ascii=True) + "\n")
    json.dump({"projects": {proj: {"allowedTools": ["Read"]}}},
              open(os.path.join(home, ".claude.json"), "w"))
    ok("caf\\u00e9" in open(os.path.join(state, "s1.jsonl")).read(),
       "fixture stores the path escaped")

    new = os.path.join(home, "dev", "cafe-app")
    run(home, proj, new, "-y")
    rec = json.loads(open(os.path.join(claude, "projects", enc(new), "s1.jsonl")).read())
    ok(rec["cwd"] == new, "escaped non-ASCII path rewritten")


def test_destination_is_a_directory(root):
    """mv semantics: an existing directory is a container, so the project keeps
    its own name inside it."""
    print("== destination is an existing directory ==")
    home, old, _, sid = fixture(root, "container")
    container = os.path.join(home, "work")
    os.makedirs(container, exist_ok=True)
    result = run(home, old, container, "-y")
    landed = os.path.join(container, "my_app")
    ok(result.returncode == 0, f"exit 0 (stderr: {result.stderr.strip()[:200]})")
    ok(os.path.exists(os.path.join(landed, "main.py")), "moved into the directory by name")
    ok(not os.path.exists(container + "/main.py"), "did not splat contents into the container")
    cfg = json.load(open(os.path.join(home, ".claude.json")))
    ok(landed in cfg["projects"], "config keyed by the landed path")
    ok(os.path.isdir(os.path.join(home, ".claude", "projects", enc(landed))),
       "state dir follows the landed path")

    print("== an empty destination directory is still a container ==")
    home, old, _, _ = fixture(root, "container-empty")
    dest = os.path.join(home, "work", "api")
    os.makedirs(dest, exist_ok=True)
    run(home, old, dest, "-y")
    ok(os.path.exists(os.path.join(dest, "my_app", "main.py")),
       "empty directory behaves like mv, not like a rename target")

    print("== already-moved detection is not treated as a container ==")
    home, old, _, _ = fixture(root, "container-moved")
    new = os.path.join(home, "work", "api")
    os.makedirs(os.path.join(home, "work"), exist_ok=True)
    shutil.move(old, new)
    run(home, old, new, "-y")
    cfg = json.load(open(os.path.join(home, ".claude.json")))
    ok(new in cfg["projects"], "already-moved folder keeps the given path")
    ok(not os.path.exists(os.path.join(new, "my_app")), "was not nested one level deeper")

    print("== --state-only is not treated as a container ==")
    home, old, _, _ = fixture(root, "container-stateonly")
    new = os.path.join(home, "work", "api")
    os.makedirs(new, exist_ok=True)
    run(home, old, new, "-y", "--state-only")
    cfg = json.load(open(os.path.join(home, ".claude.json")))
    ok(new in cfg["projects"] and f"{new}/my_app" not in cfg["projects"],
       "--state-only keeps the given path")

    print("== degenerate destinations ==")
    home, old, _, _ = fixture(root, "container-degenerate")
    result = run(home, old, os.path.dirname(old), "-y")   # ~/dev/my_app -> ~/dev
    ok(result.returncode != 0 and os.path.isdir(old),
       "moving into its own parent resolves to itself and is refused")
    target = os.path.join(home, "afile")
    open(target, "w").write("not a directory\n")
    result = run(home, old, target, "-y")
    ok(result.returncode != 0 and os.path.isfile(target),
       "destination that is a file is refused")


def test_list(root):
    print("== --list ==")
    home, old, _, _ = fixture(root, "list")
    result = run(home, "--list")
    ok(result.returncode == 0 and old in result.stdout and "sessions" in result.stdout,
       "--list reports known projects")


def main():
    keep = len(sys.argv) > 1
    root = sys.argv[1] if keep else tempfile.mkdtemp(prefix="claude-move-tests-")
    try:
        for test in (test_core_migration, test_dry_run, test_merge, test_blockers,
                     test_already_moved, test_destination_is_a_directory,
                     test_escaped_non_ascii, test_list):
            test(root)
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
        else:
            print(f"\nfixtures kept in {root}")

    print()
    print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURE(S): " + "; ".join(FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
