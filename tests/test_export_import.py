#!/usr/bin/env python3
"""Round-trip tests for claude-move's export/import: pack state on a fake
machine A, unpack it on a fake machine B with a different home, then check what
moved *and* what didn't.

The bugs this class of tool actually ships are the negatives -- a bystander
project silently re-keyed, a neighbour matched by prefix -- so most of these
assert that something stayed exactly as it was.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "claude-move.py")
sys.path.insert(0, os.path.dirname(HERE))
cm = __import__("importlib").machinery.SourceFileLoader(
    "claude_move", TOOL).load_module()

encode = cm.encode_path


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def blob_name(path, version="v1"):
    return hashlib.sha256(path.encode()).hexdigest()[:16] + "@" + version


class Machine:
    """A throwaway home directory with a Claude state tree inside it."""

    def __init__(self, root, user):
        self.home = os.path.join(root, user)
        self.claude = os.path.join(self.home, ".claude")
        self.config = os.path.join(self.home, ".claude.json")
        os.makedirs(os.path.join(self.claude, "projects"), exist_ok=True)
        write(self.config, json.dumps({"projects": {}}))

    def state(self, project):
        return os.path.join(self.claude, "projects", encode(project))

    def add_project(self, rel, memory=None, session=None, tracked=None,
                    config=None):
        project = os.path.join(self.home, rel)
        state = self.state(project)
        os.makedirs(os.path.join(state, "memory"), exist_ok=True)
        for name, body in (memory or {}).items():
            write(os.path.join(state, "memory", name), body)
        if session:
            records = [{"type": "user", "cwd": project, "sessionId": session}]
            if tracked:
                records.append({
                    "type": "snapshot",
                    "snapshot": {"trackedFileBackups": {
                        f: {"backupFileName": blob_name(f)} for f in tracked}},
                })
            write(os.path.join(state, session + ".jsonl"),
                  "\n".join(json.dumps(r) for r in records) + "\n")
            for f in tracked or []:
                write(os.path.join(self.claude, "file-history", session,
                                   blob_name(f)), "old contents of " + f)
        cfg = json.loads(read(self.config))
        cfg["projects"][project] = config if config is not None else {
            "allowedTools": ["Bash(ls:*)"], "hasTrustDialogAccepted": True,
            "lastCost": 1.25, "lastSessionId": session or "x"}
        write(self.config, json.dumps(cfg, indent=2))
        return project

    def add_history(self, project, display):
        line = json.dumps({"display": display, "project": project,
                           "timestamp": 1}) + "\n"
        path = os.path.join(self.claude, "history.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)

    def run(self, *argv):
        return cm.main(list(argv) + ["--claude-dir", self.claude,
                                     "--config", self.config, "-q"])

    def config_projects(self):
        return json.loads(read(self.config))["projects"]


class ExportImportTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="claude-move-transfer-test-")
        self.a = Machine(self.root, "alice")
        self.b = Machine(self.root, "bob")
        self.bundle = os.path.join(self.root, "bundle.tar.gz")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # -- fixtures ---------------------------------------------------------

    def seed(self):
        """One project to migrate, plus a prefix-sharing neighbour that must
        survive untouched."""
        self.api = self.a.add_project(
            "dev/api",
            memory={
                "MEMORY.md": "# Memory\n- [Notes](notes.md) - the api\n",
                "notes.md": ("The project lives at %s/dev/api and reads config\n"
                             "from %s/dev/api-server/shared.yml. See also "
                             "%s/dev/api.bak.\n"
                             % (self.a.home, self.a.home, self.a.home)),
            },
            session="11111111-1111-1111-1111-111111111111",
            tracked=[os.path.join(self.a.home, "dev/api/main.py")])
        self.neighbour = self.a.add_project(
            "dev/api-server",
            memory={"MEMORY.md": "# Memory\n- [Other](other.md) - neighbour\n"})
        self.a.add_history(self.api, "run the api")
        self.a.add_history(self.neighbour, "run the neighbour")

    def export(self, *argv):
        self.assertEqual(0, self.a.run("export", "-o", self.bundle, *argv))
        self.assertTrue(os.path.isfile(self.bundle))

    def imp(self, *argv):
        self.assertEqual(0, self.b.run("import", self.bundle, "--home", self.b.home,
                                       *argv))

    # -- the happy path ---------------------------------------------------

    def test_memory_lands_under_the_new_home(self):
        self.seed()
        self.export()
        self.imp()

        want = os.path.join(self.b.home, "dev/api")
        notes = os.path.join(self.b.state(want), "memory", "notes.md")
        self.assertTrue(os.path.isfile(notes), "memory did not land at " + notes)
        body = read(notes)
        self.assertIn(want, body)
        self.assertNotIn(self.a.home, body, "a source path survived the rewrite")

    def test_neighbour_path_keeps_its_own_name(self):
        """The catch-all home rewrite must move ~alice/dev/api-server to
        ~bob/dev/api-server -- not onto the api project's target."""
        self.seed()
        self.export()
        self.imp("--map", "api=" + os.path.join(self.b.home, "work/api"))

        state = self.b.state(os.path.join(self.b.home, "work/api"))
        body = read(os.path.join(state, "memory", "notes.md"))
        self.assertIn(os.path.join(self.b.home, "work/api") + " and reads", body)
        self.assertIn(os.path.join(self.b.home, "dev/api-server/shared.yml"), body)
        self.assertNotIn("work/api-server", body,
                         "prefix match leaked onto the neighbour")

    def test_dotted_sibling_is_not_matched(self):
        """api.bak is a different directory from api."""
        self.seed()
        self.export()
        self.imp("--map", "api=" + os.path.join(self.b.home, "work/api"))

        body = read(os.path.join(self.b.state(os.path.join(self.b.home, "work/api")),
                                 "memory", "notes.md"))
        self.assertIn(os.path.join(self.b.home, "dev/api.bak"), body)

    def test_transcript_cwd_and_blob_are_remapped(self):
        self.seed()
        self.export()
        self.imp()

        want = os.path.join(self.b.home, "dev/api")
        sid = "11111111-1111-1111-1111-111111111111"
        records = [json.loads(l) for l in
                   read(os.path.join(self.b.state(want), sid + ".jsonl")).splitlines()]
        self.assertEqual(want, records[0]["cwd"])

        new_file = os.path.join(want, "main.py")
        tracked = records[1]["snapshot"]["trackedFileBackups"]
        self.assertIn(new_file, tracked)
        self.assertEqual(blob_name(new_file), tracked[new_file]["backupFileName"])
        self.assertTrue(os.path.isfile(os.path.join(
            self.b.claude, "file-history", sid, blob_name(new_file))),
            "file-history blob was not renamed to match the new path")

    def test_config_carries_permissions_but_not_machine_stats(self):
        self.seed()
        self.export()
        self.imp()

        entry = self.b.config_projects()[os.path.join(self.b.home, "dev/api")]
        self.assertEqual(["Bash(ls:*)"], entry["allowedTools"])
        self.assertTrue(entry["hasTrustDialogAccepted"])
        self.assertNotIn("lastCost", entry)
        self.assertNotIn("lastSessionId", entry)

    def test_history_lines_follow_and_dedupe(self):
        self.seed()
        self.export()
        self.imp()
        self.imp()  # twice: the second must add nothing

        lines = [json.loads(l) for l in
                 read(os.path.join(self.b.claude, "history.jsonl")).splitlines() if l]
        self.assertEqual(2, len(lines))
        self.assertEqual({os.path.join(self.b.home, "dev/api"),
                          os.path.join(self.b.home, "dev/api-server")},
                         {l["project"] for l in lines})

    # -- the negatives ----------------------------------------------------

    def test_unselected_project_is_not_exported(self):
        self.seed()
        self.export("api")
        self.imp()

        self.assertTrue(os.path.isdir(self.b.state(os.path.join(self.b.home, "dev/api"))))
        self.assertFalse(os.path.exists(
            self.b.state(os.path.join(self.b.home, "dev/api-server"))),
            "a project that was never named came across anyway")
        self.assertNotIn(os.path.join(self.b.home, "dev/api-server"),
                         self.b.config_projects())

    def test_existing_bystander_on_the_target_is_untouched(self):
        self.seed()
        bystander = self.b.add_project(
            "dev/other", memory={"notes.md": "bob's own notes\n"},
            config={"allowedTools": ["Read"], "hasTrustDialogAccepted": False})
        before_dir = read(os.path.join(self.b.state(bystander), "memory", "notes.md"))
        before_cfg = dict(self.b.config_projects()[bystander])

        self.export()
        self.imp()

        self.assertEqual(before_dir,
                         read(os.path.join(self.b.state(bystander), "memory", "notes.md")))
        self.assertEqual(before_cfg, self.b.config_projects()[bystander])

    def test_existing_memory_is_kept_and_index_is_unioned(self):
        self.seed()
        target = os.path.join(self.b.home, "dev/api")
        state = self.b.state(target)
        write(os.path.join(state, "memory", "notes.md"), "bob wrote this first\n")
        write(os.path.join(state, "memory", "MEMORY.md"),
              "# Memory\n- [Local](local.md) - bob's own\n")

        self.export()
        self.imp()

        self.assertEqual("bob wrote this first\n",
                         read(os.path.join(state, "memory", "notes.md")))
        self.assertTrue(os.path.isfile(os.path.join(state, "memory",
                                                    "notes.md.incoming")))
        index = read(os.path.join(state, "memory", "MEMORY.md"))
        self.assertIn("- [Local](local.md) - bob's own", index)
        self.assertIn("- [Notes](notes.md) - the api", index)

    def test_overwrite_replaces_instead(self):
        self.seed()
        state = self.b.state(os.path.join(self.b.home, "dev/api"))
        write(os.path.join(state, "memory", "notes.md"), "bob wrote this first\n")

        self.export()
        self.imp("--overwrite")

        self.assertIn("dev/api", read(os.path.join(state, "memory", "notes.md")))

    def test_source_machine_is_never_written_to(self):
        self.seed()
        before = snapshot(self.a.claude) | snapshot_file(self.a.config)
        self.export()
        self.imp()
        self.assertEqual(before, snapshot(self.a.claude) | snapshot_file(self.a.config))

    # -- narrowing and mapping -------------------------------------------

    def test_memory_only_leaves_sessions_and_config_behind(self):
        self.seed()
        self.export("--memory-only")
        self.imp()

        state = self.b.state(os.path.join(self.b.home, "dev/api"))
        self.assertTrue(os.path.isfile(os.path.join(state, "memory", "notes.md")))
        self.assertEqual([], [f for f in os.listdir(state) if f.endswith(".jsonl")])
        self.assertEqual({}, self.b.config_projects())
        self.assertFalse(os.path.exists(os.path.join(self.b.claude, "history.jsonl")))

    def test_memory_only_leaves_session_spill_directories_behind(self):
        """A session's subagent and tool-result directories sit beside the
        transcript and are not .jsonl files -- they are session state all the
        same."""
        self.seed()
        state = self.a.state(self.api)
        sid = "11111111-1111-1111-1111-111111111111"
        write(os.path.join(state, sid, "subagents", "one.jsonl"), "{}\n")
        write(os.path.join(state, sid, "tool-results", "r.json"), "{}\n")

        self.export("--memory-only")
        self.imp()

        landed = self.b.state(os.path.join(self.b.home, "dev/api"))
        self.assertEqual(["memory"], sorted(os.listdir(landed)))

    def test_import_narrowing_also_drops_session_spill(self):
        self.seed()
        state = self.a.state(self.api)
        write(os.path.join(state, "11111111-1111-1111-1111-111111111111",
                           "subagents", "one.jsonl"), "{}\n")
        self.export()
        self.imp("--memory-only")
        self.assertEqual(
            ["memory"],
            sorted(os.listdir(self.b.state(os.path.join(self.b.home, "dev/api")))))

    def test_project_with_nothing_to_carry_is_skipped(self):
        """Under --memory-only a project with no memory contributes nothing;
        carrying it would just leave an empty state dir on the other side."""
        self.seed()
        self.a.add_project("dev/sessions_only",
                           session="33333333-3333-3333-3333-333333333333")
        self.export("--memory-only")
        self.imp()
        self.assertFalse(os.path.exists(
            self.b.state(os.path.join(self.b.home, "dev/sessions_only"))))

    def test_import_can_narrow_a_full_bundle(self):
        self.seed()
        self.export()
        self.imp("--memory-only")

        state = self.b.state(os.path.join(self.b.home, "dev/api"))
        self.assertTrue(os.path.isfile(os.path.join(state, "memory", "notes.md")))
        self.assertEqual([], [f for f in os.listdir(state) if f.endswith(".jsonl")])
        self.assertEqual({}, self.b.config_projects())

    def test_into_flattens_everything_under_one_directory(self):
        self.seed()
        self.export()
        self.imp("--into", os.path.join(self.b.home, "code"))

        self.assertTrue(os.path.isdir(self.b.state(os.path.join(self.b.home, "code/api"))))
        self.assertTrue(os.path.isdir(
            self.b.state(os.path.join(self.b.home, "code/api-server"))))

    def test_colliding_map_is_refused(self):
        self.seed()
        self.export()
        target = os.path.join(self.b.home, "one")
        rc = self.b.run("import", self.bundle, "--home", self.b.home,
                        "--map", "api=" + target, "--map", "api-server=" + target)
        self.assertEqual(1, rc)
        self.assertFalse(os.path.exists(self.b.state(target)))

    def test_backup_is_taken_before_merging(self):
        self.seed()
        state = self.b.state(os.path.join(self.b.home, "dev/api"))
        write(os.path.join(state, "memory", "notes.md"), "bob wrote this first\n")
        self.export()
        self.imp("--overwrite")

        backups = os.path.join(self.b.claude, "claude-move-backups")
        saved = os.path.join(backups, sorted(os.listdir(backups))[0], "projects",
                             encode(os.path.join(self.b.home, "dev/api")),
                             "memory", "notes.md")
        self.assertEqual("bob wrote this first\n", read(saved))

    # -- recovering a renamed state dir -----------------------------------

    def test_state_dir_renamed_past_its_transcripts_is_recovered(self):
        """The real case this was written for: the project directory was
        renamed and its state dir followed, but the transcripts inside still
        record the old path and no config entry exists.  Nothing but the state
        dir's own name says where it belongs."""
        moved = os.path.join(self.a.home, "dev/renamed_app")
        os.makedirs(moved)
        state = self.a.state(moved)
        os.makedirs(state)
        write(os.path.join(state, "memory", "notes.md"), "memory worth keeping\n")
        write(os.path.join(state, "22222222-2222-2222-2222-222222222222.jsonl"),
              json.dumps({"cwd": os.path.join(self.a.home, "dev/old_name")}) + "\n")

        found = cm.known_projects(
            cm.Layout(self.a.claude, self.a.config), cm.Log(quiet=True))
        self.assertIn(moved, found)
        self.assertEqual(state, found[moved])

        self.export()
        self.imp()
        self.assertEqual("memory worth keeping\n", read(os.path.join(
            self.b.state(os.path.join(self.b.home, "dev/renamed_app")),
            "memory", "notes.md")))

    def test_recovery_ignores_a_state_dir_with_no_directory_behind_it(self):
        state = self.a.state(os.path.join(self.a.home, "dev/vanished"))
        os.makedirs(state)
        write(os.path.join(state, "memory", "notes.md"), "orphan\n")
        log = cm.Log(quiet=True)
        found = cm.known_projects(
            cm.Layout(self.a.claude, self.a.config), log)
        self.assertNotIn(os.path.join(self.a.home, "dev/vanished"), found)
        self.assertTrue(any("no matching project directory" in w for w in log.warnings))

    def test_lossy_encoding_recovers_the_directory_that_really_exists(self):
        """my_app, my app and my-app all encode to my-app; only the one on disk
        can be the answer."""
        target = os.path.join(self.a.home, "dev/my_app")
        os.makedirs(target)
        self.assertEqual([target], cm.decode_state_dir(encode(target)))

    # -- bundle hygiene ---------------------------------------------------

    def test_inspect_reads_a_bundle_without_touching_state(self):
        self.seed()
        self.export()
        before = snapshot(self.b.claude)
        self.assertEqual(0, self.b.run("inspect", self.bundle))
        self.assertEqual(before, snapshot(self.b.claude))

    def test_bundle_escaping_the_archive_is_refused(self):
        import tarfile
        evil = os.path.join(self.root, "evil.tar.gz")
        payload = os.path.join(self.root, "payload")
        write(payload, "pwned")
        with tarfile.open(evil, "w:gz") as tar:
            tar.add(payload, arcname="../../escaped")
        with self.assertRaises(ValueError):
            cm.safe_extract(evil, os.path.join(self.root, "unpack"))
        self.assertFalse(os.path.exists(os.path.join(self.root, "escaped")))

    def test_runs_as_a_script(self):
        self.seed()
        out = subprocess.run(
            [sys.executable, TOOL, "--claude-dir", self.a.claude,
             "--config", self.a.config, "export", "--list"],
            capture_output=True, text=True)
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn("dev/api", out.stdout)


def snapshot(root):
    """path -> contents, for every file under root."""
    out = {}
    for base, _, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            try:
                with open(path, "rb") as fh:
                    out[os.path.relpath(path, root)] = fh.read()
            except OSError:
                pass
    return out


def snapshot_file(path):
    with open(path, "rb") as fh:
        return {os.path.basename(path): fh.read()}


if __name__ == "__main__":
    unittest.main(verbosity=2)
