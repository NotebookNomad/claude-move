#!/usr/bin/env python3
"""Tests for `claude-move prune`: the state Claude Code still keeps for folders
that are gone.

This is the one command here that deletes, and a folder that moved looks
exactly like a folder that was deleted -- the path is missing either way.  So
almost everything below asserts a refusal: the neighbour that shares a prefix,
the project whose folder is still there under another name, the one the search
can still find, the blobs of a session that is very much alive.  A prune that
takes those is worse than no prune at all.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_export_import import Machine, blob_name, cm, encode, read, write  # noqa: E402


class Fixture(Machine):
    """A machine where a project's folder can be there or not, on purpose."""

    def alive(self, rel, **kw):
        """A project whose folder is still on disk."""
        project = self.add_project(rel, **kw)
        os.makedirs(project, exist_ok=True)
        return project

    def gone(self, rel, **kw):
        """A project Claude has state for whose folder no longer exists."""
        return self.add_project(rel, **kw)

    def config_only(self, rel):
        """A ~/.claude.json entry with no state directory behind it at all."""
        project = os.path.join(self.home, rel)
        cfg = json.loads(read(self.config))
        cfg["projects"][project] = {"allowedTools": ["Bash(ls:*)"]}
        write(self.config, json.dumps(cfg, indent=2))
        return project

    def blobs_for(self, session, *files):
        """A file-history folder for one session id."""
        for name in files:
            write(os.path.join(self.claude, "file-history", session,
                               blob_name(name)), "old contents of " + name)
        return os.path.join(self.claude, "file-history", session)

    def prune(self, *argv, answer=None):
        """Run prune, returning (exit code, everything it printed)."""
        return self.run_interactive("prune", *argv, answer=answer)


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claude-move-prune-")
        self.m = Fixture(self.tmp, "me")
        self.home = self.m.home

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, rel):
        return os.path.join(self.home, rel)

    def make_dir(self, rel):
        os.makedirs(self.path(rel), exist_ok=True)
        return self.path(rel)

    def backup(self):
        """The one backup directory a run made, if it made one."""
        root = os.path.join(self.m.claude, "claude-move-backups")
        stamps = sorted(os.listdir(root)) if os.path.isdir(root) else []
        return os.path.join(root, stamps[-1], "pruned") if stamps else None

    # -- what it takes ----------------------------------------------------

    def test_a_vanished_project_is_deleted_whole(self):
        """Folder gone, nothing anywhere to say where it went: state
        directory, config entry, shell history and blobs all go."""
        gone = self.m.gone("dev/vanished", session="s1", tracked=["/a.py"],
                           memory={"notes.md": "some notes\n"})
        self.m.add_history(gone, "make test")
        state = self.m.state(gone)

        code, out = self.m.prune("-y")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(state), out)
        self.assertNotIn(gone, self.m.config_projects(), out)
        self.assertFalse(os.path.exists(
            os.path.join(self.m.claude, "file-history", "s1")), out)
        history = os.path.join(self.m.claude, "history.jsonl")
        self.assertNotIn("make test", read(history), out)

    def test_a_config_entry_with_no_state_directory_still_goes(self):
        gone = self.m.config_only("dev/only-config")
        code, out = self.m.prune("-y")
        self.assertEqual(code, 0, out)
        self.assertNotIn(gone, self.m.config_projects(), out)

    def test_blobs_of_a_deleted_transcript_are_collected(self):
        """Claude Code drops transcripts once they age out; the /rewind blobs
        they pointed at are left behind and nothing else ever collects them."""
        alive = self.m.alive("dev/api", session="live1", tracked=["/x.py"])
        stray = self.m.blobs_for("aged-out", "/y.py")

        code, out = self.m.prune("-y")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(stray), out)
        # the living session's blobs are not "left behind" by anything
        self.assertTrue(os.path.isdir(
            os.path.join(self.m.claude, "file-history", "live1")), out)
        self.assertTrue(os.path.isdir(self.m.state(alive)), out)

    # -- what it refuses --------------------------------------------------

    def test_a_project_still_on_disk_is_never_offered(self):
        alive = self.m.alive("dev/api", session="s1", memory={"n.md": "x\n"})
        code, out = self.m.prune("-y")
        self.assertEqual(code, 0, out)
        self.assertIn("nothing to prune", out)
        self.assertTrue(os.path.isdir(self.m.state(alive)), out)
        self.assertIn(alive, self.m.config_projects(), out)

    def test_the_prefix_neighbour_is_untouched(self):
        """~/dev/api is gone; ~/dev/api-server is not, and merely starts the
        same way."""
        gone = self.m.gone("dev/api", session="s1", memory={"n.md": "x\n"})
        kept = self.m.alive("dev/api-server", session="s2",
                            memory={"n.md": "y\n"})
        self.m.add_history(kept, "npm start")

        code, out = self.m.prune("-y", "--no-search")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(self.m.state(gone)), out)
        self.assertTrue(os.path.isdir(self.m.state(kept)), out)
        self.assertIn(kept, self.m.config_projects(), out)
        self.assertIn("npm start",
                      read(os.path.join(self.m.claude, "history.jsonl")), out)

    def test_a_folder_the_search_can_still_find_is_reported_not_deleted(self):
        """The folder was moved by hand.  Deleting its state here is exactly
        the loss this whole tool exists to prevent."""
        gone = self.m.gone("dev/relocated", session="s1",
                           memory={"n.md": "x\n"})
        self.make_dir("work/relocated")

        code, out = self.m.prune("-y")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isdir(self.m.state(gone)), out)
        self.assertIn(gone, self.m.config_projects(), out)
        self.assertIn("Moved rather than deleted", out)
        self.assertIn("--state-only", out)

    def test_a_renamed_state_directory_is_left_alone(self):
        """Folder and state directory were renamed together; the transcripts
        inside still name where it used to be.  Nothing is gone."""
        new = self.make_dir("work/renamed")
        state = self.m.state(new)
        write(os.path.join(state, "s0.jsonl"),
              json.dumps({"type": "user", "cwd": self.path("dev/renamed"),
                          "sessionId": "s0"}) + "\n")

        code, out = self.m.prune("-y")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isdir(state), out)

    def test_a_state_directory_named_for_nowhere_keeps_its_live_project(self):
        """The directory name is the wrong question to ask.

        A state directory renamed by hand -- to a path that never existed, or
        to one since abandoned -- still holds the transcripts of a project that
        is alive and well at the cwd recorded inside it.  Nothing decodes the
        name, so only what the transcripts say saves it.
        """
        live = self.make_dir("dev/live")
        state = os.path.join(self.m.claude, "projects",
                             encode(self.path("work/never-existed")))
        write(os.path.join(state, "s0.jsonl"),
              json.dumps({"type": "user", "cwd": live, "sessionId": "s0"}) + "\n")
        write(os.path.join(state, "memory", "n.md"), "still in use\n")

        code, out = self.m.prune("-y", "--no-search")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isdir(state), out)
        self.assertEqual("still in use\n",
                         read(os.path.join(state, "memory", "n.md")))

    def test_an_ambiguous_state_directory_name_is_left_alone(self):
        """Two live folders encode to one directory name, so nothing resolves
        it -- and an unresolvable name is not evidence that anything is gone."""
        self.make_dir("dev/my_app")
        self.make_dir("dev/my-app")
        state = os.path.join(self.m.claude, "projects",
                             encode(self.path("dev/my_app")))
        write(os.path.join(state, "memory", "n.md"), "notes\n")

        code, out = self.m.prune("-y", "--no-search")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isdir(state), out)

    def test_a_shared_state_directory_survives_while_either_project_lives(self):
        """The encoded name is lossy: two paths can share one directory, and
        one of them being gone does not make the directory leftover."""
        gone = self.m.gone("dev/my_app", session="s1")
        alive = self.m.alive("dev/my-app", session="s2")
        self.assertEqual(self.m.state(gone), self.m.state(alive))

        code, out = self.m.prune("-y", "--no-search")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isdir(self.m.state(gone)), out)

    def test_a_live_worktree_keeps_its_state(self):
        """A session run inside ~/.claude is not a project, but the directory
        it ran in still exists and its state is still in use."""
        worktree = os.path.join(self.m.claude, "worktrees", "spike")
        os.makedirs(worktree, exist_ok=True)
        state = self.m.state(worktree)
        write(os.path.join(state, "s0.jsonl"),
              json.dumps({"type": "user", "cwd": worktree,
                          "sessionId": "s0"}) + "\n")

        code, out = self.m.prune("-y")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isdir(state), out)

    def test_a_path_with_nothing_behind_it_is_not_offered(self):
        """A cwd recorded in some other project's transcript is a place a
        session once ran, not a project with anything of its own.  It has no
        state directory, no settings and no history, so there is nothing to
        delete and nothing to ask about."""
        gone = self.m.gone("dev/vanished", session="s1")
        ghost = self.path("dev/ghost")
        write(os.path.join(self.m.state(gone), "s1.jsonl"),
              json.dumps({"type": "user", "cwd": gone, "sessionId": "s1"}) + "\n"
              + json.dumps({"type": "user", "cwd": ghost, "sessionId": "s1"}) + "\n")

        code, out = self.m.prune("-y", "--no-search")
        self.assertEqual(code, 0, out)
        # the real leftovers still go, named after the project that owns them
        self.assertFalse(os.path.exists(self.m.state(gone)), out)
        self.assertIn("1. ~/dev/vanished", out)
        # and the bare path is not a second item of its own
        self.assertIn("1 item(s)", out)
        self.assertNotIn("2. ", out)

    def test_an_unparseable_history_line_survives_the_rewrite(self):
        gone = self.m.gone("dev/vanished", session="s1")
        self.m.add_history(gone, "make test")
        history = os.path.join(self.m.claude, "history.jsonl")
        with open(history, "a", encoding="utf-8") as fh:
            fh.write("not json at all\n")

        code, out = self.m.prune("-y", "--no-search")
        self.assertEqual(code, 0, out)
        body = read(history)
        self.assertIn("not json at all", body, out)
        self.assertNotIn("make test", body, out)

    # -- asking -----------------------------------------------------------

    def test_dry_run_deletes_nothing(self):
        gone = self.m.gone("dev/vanished", session="s1", memory={"n.md": "x\n"})
        code, out = self.m.prune("-n", "--no-search")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isdir(self.m.state(gone)), out)
        self.assertIn(gone, self.m.config_projects(), out)
        self.assertIn("dry run", out)

    def test_answering_no_deletes_nothing(self):
        gone = self.m.gone("dev/vanished", session="s1")
        code, out = self.m.prune("--no-search", answer="n")
        self.assertEqual(code, 1, out)
        self.assertTrue(os.path.isdir(self.m.state(gone)), out)

    def test_no_answer_at_all_deletes_nothing(self):
        """A closed stdin -- a pipe, a cron job -- reads as a refusal."""
        gone = self.m.gone("dev/vanished", session="s1")
        code, out = self.m.prune("--no-search")
        self.assertEqual(code, 1, out)
        self.assertTrue(os.path.isdir(self.m.state(gone)), out)

    def test_a_number_picks_out_one_orphan(self):
        big = self.m.gone("dev/big-one", session="s1",
                          memory={"n.md": "x" * 4000})
        small = self.m.gone("dev/small-one", session="s2",
                            memory={"n.md": "y\n"})

        code, out = self.m.prune("--no-search", answer="1")
        self.assertEqual(code, 0, out)
        # the listing is ordered by what it reclaims, biggest first
        self.assertFalse(os.path.exists(self.m.state(big)), out)
        self.assertTrue(os.path.isdir(self.m.state(small)), out)

    def test_naming_a_project_limits_the_sweep(self):
        wanted = self.m.gone("dev/wanted", session="s1")
        other = self.m.gone("dev/other", session="s2")
        stray = self.m.blobs_for("aged-out", "/y.py")

        code, out = self.m.prune("wanted", "-y", "--no-search")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(self.m.state(wanted)), out)
        self.assertTrue(os.path.isdir(self.m.state(other)), out)
        self.assertIn(other, self.m.config_projects(), out)
        # blobs belong to no named project, so naming one leaves them
        self.assertTrue(os.path.isdir(stray), out)

    def test_naming_nothing_that_matches_is_not_a_clean_bill_of_health(self):
        gone = self.m.gone("dev/vanished", session="s1")
        code, out = self.m.prune("typo", "-y", "--no-search")
        self.assertEqual(code, 0, out)
        self.assertIn("for the projects named", out)
        self.assertTrue(os.path.isdir(self.m.state(gone)), out)

    # -- the safety copy --------------------------------------------------

    def test_everything_deleted_is_backed_up_first(self):
        gone = self.m.gone("dev/vanished", session="s1", tracked=["/a.py"],
                           memory={"notes.md": "keep me\n"})
        self.m.add_history(gone, "make test")

        code, out = self.m.prune("-y", "--no-search")
        self.assertEqual(code, 0, out)
        backup = self.backup()
        self.assertTrue(backup and os.path.isdir(backup), out)
        self.assertEqual("keep me\n", read(os.path.join(
            backup, "projects", encode(gone), "memory", "notes.md")))
        self.assertTrue(os.path.isdir(os.path.join(backup, "file-history", "s1")))
        saved = json.loads(read(os.path.join(backup, "config.json")))
        self.assertIn(gone, saved["projects"])
        self.assertIn("make test", read(os.path.join(backup, "history.jsonl")))

    def test_no_backup_skips_the_copy_but_still_deletes(self):
        gone = self.m.gone("dev/vanished", session="s1")
        code, out = self.m.prune("-y", "--no-backup", "--no-search")
        self.assertEqual(code, 0, out)
        self.assertIsNone(self.backup(), out)
        self.assertFalse(os.path.exists(self.m.state(gone)), out)

    def test_the_backup_takes_the_directory_rather_than_a_second_copy(self):
        """Reclaiming space must not first require a spare copy of everything.

        The backup is on the same filesystem as the state it saves, so what
        goes there is moved, not duplicated -- one directory afterwards, in
        the backup, and none left behind.
        """
        gone = self.m.gone("dev/vanished", session="s1", tracked=["/a.py"],
                           memory={"notes.md": "keep me\n"})
        state = self.m.state(gone)
        marker = os.stat(os.path.join(state, "memory", "notes.md")).st_ino

        code, out = self.m.prune("-y", "--no-search")
        self.assertEqual(code, 0, out)
        saved = os.path.join(self.backup(), "projects", encode(gone),
                             "memory", "notes.md")
        self.assertFalse(os.path.exists(state), out)
        self.assertEqual(marker, os.stat(saved).st_ino,
                         "the backup should be the same file moved, not a copy")


if __name__ == "__main__":
    unittest.main(verbosity=2)
