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

    def record_cwd(self, project, session, cwd):
        """Another directory the same session ran in.  Claude Code writes a
        cwd on every record, and `known_projects` reads all of them."""
        with open(os.path.join(self.state(project), session + ".jsonl"), "a") as fh:
            fh.write(json.dumps({"type": "user", "cwd": cwd,
                                 "sessionId": session}) + "\n")
        return cwd

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

    # -- what it counts ---------------------------------------------------

    def test_a_subdirectory_a_session_ran_in_is_not_its_own_project(self):
        """A cwd is not a project.  Counting one says this machine is keeping
        state for more projects than it is, and the reader checks that number
        against ~/.claude/projects."""
        api = self.m.alive("dev/api", session="s-1")
        self.m.record_cwd(api, "s-1", self.make_dir("dev/api/workspace"))
        code, out = self.m.prune("-n")
        self.assertEqual(code, 0, out)
        self.assertIn("Checked 1 project(s)", out)
        self.assertNotIn("workspace", out)

    def test_a_worktree_under_a_project_is_not_its_own_project(self):
        api = self.m.alive("dev/api", session="s-1")
        # gone, as Claude Code's own worktrees are once the branch is done
        self.m.record_cwd(api, "s-1", self.path("dev/api/.claude/worktrees/wt"))
        code, out = self.m.prune("-n")
        self.assertEqual(code, 0, out)
        self.assertIn("Checked 1 project(s)", out)
        self.assertNotIn("worktrees", out)

    def test_a_name_a_folder_wore_briefly_is_not_counted_or_chased(self):
        """The cwd of a folder that has since been renamed, with no state
        directory, no config entry and no history of its own.  There is
        nothing to carry across, so offering to move it sends the reader after
        a command that would move nothing."""
        api = self.m.alive("projects/api", session="s-1")
        self.m.record_cwd(api, "s-1", self.path("api-under-its-old-name"))
        code, out = self.m.prune("-n")
        self.assertEqual(code, 0, out)
        self.assertIn("Checked 1 project(s)", out)
        self.assertNotIn("api-under-its-old-name", out)

    def test_state_with_nothing_on_disk_behind_it_is_still_counted(self):
        """The other side of the same rule: a config entry alone, or a state
        directory alone, is a project this command is responsible for."""
        self.m.alive("dev/api", session="s-1")
        self.m.config_only("dev/gone-a")
        self.m.gone("dev/gone-b", session="s-2")
        code, out = self.m.prune("-n")
        self.assertEqual(code, 0, out)
        self.assertIn("Checked 3 project(s)", out)

    def test_a_subdirectory_is_not_offered_as_where_a_project_went(self):
        """`by_name` is the source for "the only project Claude knows by that
        name".  A cwd is not a project, so a scratch directory inside a live
        one must not become the proposed new home of a missing project that
        happens to share its last segment -- prune would refuse to delete
        state on the strength of it, and repair would rewrite paths to it."""
        api = self.m.alive("dev/api", session="s-1")
        self.m.record_cwd(api, "s-1", self.make_dir("dev/api/workspace"))
        self.m.gone("elsewhere/workspace", session="s-2")

        code, out = self.m.prune("-n", "--no-search")
        self.assertEqual(code, 0, out)
        self.assertNotIn("Moved rather than deleted", out)
        # prune prints every path tilde-folded, so the absolute form could
        # never appear here however wrong the answer was
        self.assertNotIn("~/dev/api/workspace", out)
        self.assertIn("Gone from disk", out)
        self.assertIn("elsewhere/workspace", out)

    def test_a_worktree_is_not_offered_as_where_a_project_went(self):
        """Claude Code writes a config entry for every directory it is opened
        in, its own worktrees under ~/.claude included.  Something is filed
        under one, so only the `.claude` in the path tells it from a project."""
        self.m.alive("dev/api", session="s-1")
        self.make_dir(".claude/worktrees/spike")
        self.m.config_only(".claude/worktrees/spike")
        self.m.gone("elsewhere/spike", session="s-2")

        code, out = self.m.prune("-n", "--no-search")
        self.assertEqual(code, 0, out)
        self.assertNotIn("Moved rather than deleted", out)
        self.assertNotIn("worktrees/spike", out)
        self.assertIn("Gone from disk", out)

    def test_a_worktree_inside_a_project_is_not_offered_either(self):
        """The same worktree one level down, where Claude Code really does put
        them: a state directory and sessions of its own, from running there."""
        self.m.alive("dev/api", session="s-1")
        self.m.alive("dev/api/.claude/worktrees/feat", session="s-3")
        self.m.gone("elsewhere/feat", session="s-2")

        code, out = self.m.prune("-n", "--no-search")
        self.assertEqual(code, 0, out)
        self.assertNotIn("Moved rather than deleted", out)
        self.assertNotIn("worktrees/feat", out)
        self.assertIn("Gone from disk", out)

    def test_a_subdirectory_sharing_a_siblings_encoded_name(self):
        """encode_path is lossy: ~/dev/api/workspace and ~/dev/api-workspace
        name the same state directory.  A scratch directory must not be
        readmitted because its sibling's state dir happens to sit where its
        own would."""
        api = self.m.alive("dev/api", session="s-1")
        self.m.record_cwd(api, "s-1", self.make_dir("dev/api/workspace"))
        self.m.alive("dev/api-workspace", session="s-3")
        self.m.gone("elsewhere/workspace", session="s-2")

        code, out = self.m.prune("-n", "--no-search")
        self.assertEqual(code, 0, out)
        self.assertNotIn("Moved rather than deleted", out)
        self.assertNotIn("~/dev/api/workspace", out)
        self.assertIn("Gone from disk", out)

    def test_a_folder_moved_by_hand_still_stops_a_delete(self):
        """The other direction.  A folder moved by hand and not reopened has
        nothing filed under its new path -- Claude knows it only as a cwd
        another project's transcript recorded.  That is still enough to stop
        prune deleting the state of a project sitting right there."""
        other = self.m.alive("dev/other", session="s-9")
        self.m.record_cwd(other, "s-9", self.make_dir("dev/api"))
        gone = self.m.gone("old/api", session="s-2", memory={"n.md": "x\n"})
        state = self.m.state(gone)

        code, out = self.m.prune("-y", "--no-search")
        self.assertEqual(code, 0, out)
        self.assertIn("Moved rather than deleted", out)
        self.assertIn("~/old/api  ->  ~/dev/api", out)
        self.assertTrue(os.path.isdir(state), out)

    def test_naming_one_project_never_reaches_another(self):
        """A cwd recorded inside a transcript can name a different project.

        Filtering on those rather than on what the directory belongs to meant
        naming a path with nothing of its own deleted the state of the project
        whose transcript merely mentioned it.
        """
        gone = self.m.gone("dev/vanished", session="s1")
        ghost = self.path("dev/ghost")
        write(os.path.join(self.m.state(gone), "s1.jsonl"),
              json.dumps({"type": "user", "cwd": gone, "sessionId": "s1"}) + "\n"
              + json.dumps({"type": "user", "cwd": ghost, "sessionId": "s1"}) + "\n")

        code, out = self.m.prune("ghost", "-y", "--no-search")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isdir(self.m.state(gone)), out)
        self.assertIn(gone, self.m.config_projects(), out)

    def test_declining_an_orphan_keeps_all_of_it(self):
        """Picking item 1 must not take item 2's settings and history with it.

        Each orphan owns only what its own directory does; a cwd recorded in
        someone else's transcript brings none of its owner's belongings along.
        """
        big = self.m.gone("dev/vanished", session="s1",
                          memory={"n.md": "x" * 4000})
        small = self.m.gone("dev/ghost", session="s2")
        self.m.add_history(small, "ghost command")
        # the big project's transcript also records the small one as a cwd
        write(os.path.join(self.m.state(big), "s1.jsonl"),
              json.dumps({"type": "user", "cwd": big, "sessionId": "s1"}) + "\n"
              + json.dumps({"type": "user", "cwd": small, "sessionId": "s1"}) + "\n")

        code, out = self.m.prune("--no-search", answer="1")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(self.m.state(big)), out)
        # the one that was declined keeps every part of itself
        self.assertTrue(os.path.isdir(self.m.state(small)), out)
        self.assertIn(small, self.m.config_projects(), out)
        self.assertIn("ghost command",
                      read(os.path.join(self.m.claude, "history.jsonl")), out)

    def test_a_subagent_cwd_inside_the_state_dir_does_not_keep_it_alive(self):
        """A session editing memory runs with its cwd inside the state
        directory itself.  That path lives exactly as long as the directory
        does, so counting it as proof of life made the thing unprunable."""
        gone = self.m.gone("dev/vanished", session="s1")
        state = self.m.state(gone)
        write(os.path.join(state, "s1.jsonl"),
              json.dumps({"type": "user", "cwd": gone, "sessionId": "s1"}) + "\n"
              + json.dumps({"type": "user", "cwd": os.path.join(state, "memory"),
                            "sessionId": "s1"}) + "\n")

        code, out = self.m.prune("-y", "--no-search")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(state), out)

    def test_an_unnamed_orphan_still_lists_its_blobs(self):
        """A state directory nothing resolves onto is named by its encoded
        form, and its inventory must still say what goes with it -- an item
        whose listing understates itself is being approved blind."""
        state = os.path.join(self.m.claude, "projects",
                             encode(self.path("work/never-existed")))
        write(os.path.join(state, "s9.jsonl"),
              json.dumps({"type": "user", "cwd": self.path("dev/long-gone"),
                          "sessionId": "s9"}) + "\n")
        blobs = self.m.blobs_for("s9", "/a.py")

        code, out = self.m.prune("-n", "--no-search")
        self.assertEqual(code, 0, out)
        self.assertIn("1 file-history folder(s)", out)
        self.assertTrue(os.path.isdir(blobs), out)

    def test_a_config_that_is_not_an_object_does_not_crash_the_run(self):
        """~/.claude.json parsing to something other than an object must not
        raise -- least of all part-way through, once the state directories
        have already been moved into the backup."""
        gone = self.m.gone("dev/vanished", session="s1")
        write(self.m.config, json.dumps(["not", "a", "config"]))

        code, out = self.m.prune("-y", "--no-search")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(self.m.state(gone)), out)
        # and the file it could not read is left exactly as it was
        self.assertEqual(["not", "a", "config"],
                         json.loads(read(self.m.config)))

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

    def test_a_mistyped_name_stops_the_run(self):
        """This command deletes, so a name it cannot place is a reason to stop
        rather than to go ahead with whatever the other names matched."""
        gone = self.m.gone("dev/vanished", session="s1")
        code, out = self.m.prune("vanished", "typo", "-y", "--no-search")
        self.assertEqual(code, 2, out)
        self.assertIn("no project matches 'typo'", out)
        self.assertTrue(os.path.isdir(self.m.state(gone)), out)

    def test_naming_a_live_project_is_a_clean_bill_of_health(self):
        """A name that matched a project which simply has nothing left over is
        not the same as a name that matched nothing at all."""
        alive = self.m.alive("dev/api", session="s1")
        code, out = self.m.prune("api", "-y", "--no-search")
        self.assertEqual(code, 0, out)
        self.assertIn("nothing left over", out)
        self.assertTrue(os.path.isdir(self.m.state(alive)), out)

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
