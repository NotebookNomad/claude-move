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


def add_project(home, path, sid):
    """A second project in an existing fixture home: folder, state dir with one
    transcript and a memory file, and a ~/.claude.json entry."""
    os.makedirs(path, exist_ok=True)
    open(os.path.join(path, "main.py"), "w").write(f"# {os.path.basename(path)}\n")
    state = os.path.join(home, ".claude", "projects", enc(path))
    os.makedirs(os.path.join(state, "memory"), exist_ok=True)
    open(os.path.join(state, "memory", "notes.md"), "w").write(f"lives at {path}\n")
    open(os.path.join(state, sid + ".jsonl"), "w").write(
        json.dumps({"type": "system", "cwd": path, "content": f"cd {path}"}) + "\n")
    cfg_path = os.path.join(home, ".claude.json")
    cfg = json.load(open(cfg_path))
    cfg["projects"][path] = {"allowedTools": [f"Bash({os.path.basename(path)}:*)"]}
    json.dump(cfg, open(cfg_path, "w"))
    return path


def three_projects(root, tag):
    """A home with my_app plus two sibling services, and an empty ~/archive."""
    home, old, _, _ = fixture(root, tag)
    dev = os.path.dirname(old)
    api = add_project(home, os.path.join(dev, "api-service"), "cccc-3333")
    web = add_project(home, os.path.join(dev, "web-service"), "dddd-4444")
    archive = os.path.join(home, "archive")
    os.makedirs(archive, exist_ok=True)
    return home, old, api, web, archive


def landed_ok(home, srcs, archive, label):
    """Every src ended up at <archive>/<its own name>, state and all."""
    cfg = json.load(open(os.path.join(home, ".claude.json")))
    for src in srcs:
        name = os.path.basename(src)
        dest = os.path.join(archive, name)
        ok(os.path.exists(os.path.join(dest, "main.py")), f"{label}: {name} folder moved")
        ok(dest in cfg["projects"] and src not in cfg["projects"],
           f"{label}: {name} config key renamed")
        ok(os.path.isdir(os.path.join(home, ".claude", "projects", enc(dest))),
           f"{label}: {name} state dir follows")

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
    ok(result.returncode == 0, f"exit 0 (stderr: {result.stderr.strip()[:200]})")
    landed_ok(home, [old], container, "container")
    ok(not os.path.exists(container + "/main.py"), "did not splat contents into the container")

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

    print("== moved by hand into a container, then run with the same arguments ==")
    for extra in ([], ["--state-only"]):
        home, old, _, _ = fixture(root, "container-byhand" + "".join(extra))
        container = os.path.join(home, "work")
        os.makedirs(container, exist_ok=True)
        shutil.move(old, container)                 # exactly what `mv` does
        landed = os.path.join(container, "my_app")
        run(home, old, container, "-y", *extra)
        cfg = json.load(open(os.path.join(home, ".claude.json")))
        label = " ".join(extra) or "(no flag)"
        ok(landed in cfg["projects"], f"{label}: config points at the landed folder")
        ok(container not in cfg["projects"], f"{label}: container did not become a project")
        ok(os.path.isdir(os.path.join(home, ".claude", "projects", enc(landed))),
           f"{label}: state dir follows the landed folder")

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

    home, old, _, _ = fixture(root, "container-dangling")
    container = os.path.join(home, "work")
    os.makedirs(container, exist_ok=True)
    os.symlink(os.path.join(home, "gone"), os.path.join(container, "my_app"))
    result = run(home, old, container, "-y")
    ok(result.returncode != 0 and "Traceback" not in result.stderr,
       "dangling symlink at the destination is refused, not crashed into")
    ok(not os.path.isdir(os.path.join(home, ".claude", "claude-move-backups")),
       "refused before taking a backup")



def test_multiple_sources(root):
    print("== several projects into one directory ==")
    home, old, api, web, archive = three_projects(root, "multi")
    result = run(home, old, api, web, archive, "-y")
    ok(result.returncode == 0, f"exit 0 (stderr: {result.stderr.strip()[:300]})")
    landed_ok(home, [old, api, web], archive, "multi")

    state = os.path.join(home, ".claude", "projects", enc(os.path.join(archive, "api-service")))
    ok(os.path.join(archive, "api-service") in
       open(os.path.join(state, "memory", "notes.md")).read(),
       "multi: each project's own memory rewritten")
    sub = os.path.join(archive, "my_app", "packages", "core")
    ok(os.path.isdir(os.path.join(home, ".claude", "projects", enc(sub))),
       "multi: subprojects still remapped inside a batch")
    backups = os.listdir(os.path.join(home, ".claude", "claude-move-backups"))
    ok(len(backups) == 1, f"multi: one backup for the whole batch (got {len(backups)})")

    leftover = subprocess.run(
        ["grep", "-rl", os.path.dirname(old) + "/",
         os.path.join(home, ".claude.json"), os.path.join(home, ".claude", "projects")],
        capture_output=True, text=True).stdout
    ok(not leftover, f"multi: no stale references remain ({leftover.strip()[:200]})")

    print("== several projects moved in by hand, then --state-only ==")
    home, old, api, web, archive = three_projects(root, "multi-stateonly")
    for src in (old, api, web):
        shutil.move(src, os.path.join(archive, os.path.basename(src)))
    result = run(home, old, api, web, archive, "--state-only", "-y")
    ok(result.returncode == 0, f"exit 0 (stderr: {result.stderr.strip()[:200]})")
    landed_ok(home, [old, api, web], archive, "state-only")
    cfg = json.load(open(os.path.join(home, ".claude.json")))
    ok(archive not in cfg["projects"], "state-only: container did not become a project")


def test_wildcards(root):
    print("== wildcards ==")
    home, old, api, web, archive = three_projects(root, "glob")
    pattern = os.path.join(os.path.dirname(old), "*-service")
    # run() goes through subprocess without a shell, so the pattern arrives
    # literally -- exactly as if the user had quoted it
    result = run(home, pattern, archive, "-y")
    ok(result.returncode == 0, f"exit 0 (stderr: {result.stderr.strip()[:300]})")
    landed_ok(home, [api, web], archive, "glob")
    ok(os.path.isdir(old), "glob: non-matching project left alone")
    ok(not os.path.exists(os.path.join(archive, "my_app")), "glob: only matches moved")

    print("== a wildcard matching nothing ==")
    home, old, _, _, archive = three_projects(root, "glob-none")
    dev = os.path.dirname(old)
    result = run(home, os.path.join(dev, "nope-*"), archive, "-y")
    ok(result.returncode == 2 and "no directories match" in result.stderr,
       "a pattern matching nothing is an error")
    ok(os.path.isdir(old) and not os.listdir(archive), "nothing changed")

    # every dead pattern at once, so the user does not fix them one run at a time
    result = run(home, os.path.join(dev, "aa-*"), os.path.join(dev, "bb-*"),
                 os.path.join(dev, "cc-*"), archive, "-y")
    ok(result.stderr.count("no directories match") == 3,
       f"all three dead patterns reported (got {result.stderr.count('no directories match')})")

    print("== a folder whose real name contains glob characters ==")
    home, old, _, _ = fixture(root, "glob-literal")
    literal = os.path.join(os.path.dirname(old), "notes[2024]")
    add_project(home, literal, "ffff-6666")
    dest = os.path.join(home, "archive")
    os.makedirs(dest, exist_ok=True)
    result = run(home, literal, dest, "-y")
    cfg = json.load(open(os.path.join(home, ".claude.json")))
    landed = os.path.join(dest, "notes[2024]")
    ok(result.returncode == 0, f"exit 0 (stderr: {result.stderr.strip()[:200]})")
    ok(os.path.isdir(landed) and landed in cfg["projects"],
       "a real directory named like a pattern is taken literally")

    print("== a wildcard whose folders were already moved by hand ==")
    home, old, _, _ = fixture(root, "glob-moved")
    dest = os.path.join(home, "work")
    os.makedirs(dest, exist_ok=True)
    shutil.move(old, dest)
    result = run(home, os.path.join(os.path.dirname(old), "my_*"), dest, "-y")
    cfg = json.load(open(os.path.join(home, ".claude.json")))
    landed = os.path.join(dest, "my_app")
    ok(result.returncode == 0, f"exit 0 (stderr: {result.stderr.strip()[:300]})")
    ok(landed in cfg["projects"],
       "pattern falls back to Claude's known projects when the folder is gone")
    ok(f"{landed}/packages/core" in cfg["projects"] and
       f"{dest}/packages/core" not in cfg["projects"],
       "the subproject was remapped once, not matched by the pattern itself")


def test_batch_blockers(root):
    print("== batch blockers ==")
    home, _, api, _, archive = three_projects(root, "batch-same-name")
    twin = add_project(home, os.path.join(home, "other", "api-service"), "eeee-5555")
    result = run(home, api, twin, archive, "-y")
    ok(result.returncode != 0 and "land on" in result.stderr,
       "two projects with the same name are refused")
    ok(os.path.isdir(api) and os.path.isdir(twin), "nothing moved")
    ok(not os.path.isdir(os.path.join(home, ".claude", "claude-move-backups")),
       "refused before taking a backup")

    print("== a source inside another source, named in either order ==")
    for order in ("outer first", "inner first"):
        home, old, _, _, archive = three_projects(root, "batch-nested-" + order[:5])
        inner = os.path.join(old, "packages", "core")
        pair = [old, inner] if order == "outer first" else [inner, old]
        result = run(home, *pair, archive, "-y")
        ok(result.returncode != 0 and "is inside" in result.stderr,
           f"{order}: a nested source is refused")
        ok("Traceback" not in result.stderr, f"{order}: refused cleanly, not crashed into")
        ok(os.path.isdir(inner) and not os.path.isdir(os.path.join(archive, "core")),
           f"{order}: nothing moved")

    print("== the destination must be a real directory for a batch ==")
    home, old, api, _, _ = three_projects(root, "batch-nodir")
    result = run(home, old, api, os.path.join(home, "nope"), "-y")
    ok(result.returncode == 2 and "existing directory" in result.stderr,
       "a batch into a non-existent directory is refused")
    ok(os.path.isdir(old) and os.path.isdir(api), "nothing moved")

    print("== a batched source whose folder went somewhere else entirely ==")
    home, old, api, web, archive = three_projects(root, "batch-strayed")
    shutil.move(web, os.path.join(home, "elsewhere"))
    result = run(home, api, web, archive, "-y")
    cfg = json.load(open(os.path.join(home, ".claude.json")))
    ok(result.returncode != 0 and "does not exist" in result.stderr,
       "a source that is not where it was named is refused")
    ok(archive not in cfg["projects"], "the container did not become a project")
    ok(not os.path.isdir(os.path.join(home, ".claude", "projects", enc(archive))),
       "no state directory was created for the container")
    ok(os.path.isdir(api), "the healthy project was left alone")

    print("== the destination swept up by its own wildcard ==")
    home, old, _, _, _ = three_projects(root, "batch-dst-in-glob")
    dev = os.path.dirname(old)
    inside = os.path.join(dev, "archive")
    os.makedirs(inside, exist_ok=True)
    result = run(home, os.path.join(dev, "*"), inside, "-y")
    ok(result.returncode == 2 and "also one of the sources" in result.stderr,
       "says the destination was matched by the pattern, not just 'same path'")

    print("== one bad project stops the whole batch ==")
    home, old, api, web, archive = three_projects(root, "batch-allornothing")
    json.dump({"pid": os.getpid(), "cwd": web, "name": "live-one"},
              open(os.path.join(home, ".claude", "sessions", "999999.json"), "w"))
    result = run(home, old, api, web, archive, "-y")
    ok(result.returncode != 0 and "live" in result.stderr, "the live session blocks")
    ok(os.path.isdir(old) and os.path.isdir(api) and os.path.isdir(web),
       "the unaffected projects were left alone too (all or nothing)")
    ok(not os.listdir(archive), "destination untouched")


def test_prefix_siblings(root):
    """A neighbour that merely starts with the same characters is a different
    project.  Plain substring replacement used to drag it along."""
    print("== a neighbour sharing the moved project's name prefix ==")
    for suffix in ("-server", "2", "_old", ".bak"):
        home, old, _, _ = fixture(root, "prefix" + suffix)
        sib = add_project(home, old + suffix, "gggg-7777")
        with open(os.path.join(home, ".claude", "history.jsonl"), "a") as fh:
            fh.write(json.dumps({"display": "sib", "project": sib}) + "\n")
        new = os.path.join(home, "work", "api")
        run(home, old, new, "-y")
        cfg = json.load(open(os.path.join(home, ".claude.json")))
        hist = [json.loads(l) for l in open(os.path.join(home, ".claude", "history.jsonl"))]
        tag = [h["project"] for h in hist if h["display"] == "sib"][0]
        name = os.path.basename(sib)
        ok(sib in cfg["projects"], f"{name}: config entry left alone")
        ok(tag == sib, f"{name}: history entry left alone")
        ok(new in cfg["projects"], f"{name}: the named project still moved")

    print("== boundaries that must still match ==")
    home, old, _, _ = fixture(root, "prefix-ok")
    new = os.path.join(home, "work", "api")
    run(home, old, new, "-y")
    cfg = json.load(open(os.path.join(home, ".claude.json")))
    state = os.path.join(home, ".claude", "projects", enc(new))
    ok(os.path.join(new, "packages", "core") in cfg["projects"], "a child path still matches")
    ok(new in open(os.path.join(state, "memory", "deploy.md")).read(),
       "a path ending a sentence in prose still matches")
    ok(json.load(open(os.path.join(home, ".claude", "jobs", "job1",
                                   "state.json")))["cwd"] == new,
       "a path followed by a quote still matches")


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
                     test_multiple_sources, test_wildcards, test_batch_blockers,
                     test_escaped_non_ascii, test_prefix_siblings, test_list):
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
