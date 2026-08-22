#!/usr/bin/env python3
"""Tests for `claude-move repair`: memory files that still name a directory
which has since moved.

Repair guesses, so most of what matters here is what it declines to touch --
a neighbour whose name merely starts the same way, a path that is still on
disk, a slash command that looks like one.  Those are the assertions that
would have caught every bug this feature had while it was being written.
"""

import builtins
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_export_import import Machine, cm, encode, read, write   # noqa: E402


class Fixture(Machine):
    """A machine whose projects can be knocked out of sync on purpose."""

    def memory(self, project, name, body):
        write(os.path.join(self.state(project), "memory", name), body)

    def read_memory(self, project, name):
        return read(os.path.join(self.state(project), "memory", name))

    def sessions_naming(self, state, *cwds):
        """A transcript per cwd, inside a state directory named however the
        caller likes -- which is how a project that moved by hand looks."""
        for i, cwd in enumerate(cwds):
            write(os.path.join(state, f"s{i}.jsonl"),
                  json.dumps({"type": "user", "cwd": cwd,
                              "sessionId": f"s{i}"}) + "\n")

    def repair(self, *argv, answer=None):
        """Run repair, returning (exit code, everything it printed)."""
        out = io.StringIO()
        real_input = builtins.input
        builtins.input = lambda _prompt="": (_ for _ in ()).throw(EOFError) \
            if answer is None else answer
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = cm.main(["repair", *argv, "--claude-dir", self.claude,
                                "--config", self.config])
        finally:
            builtins.input = real_input
        return code, out.getvalue()


class RepairTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claude-move-repair-")
        self.m = Fixture(self.tmp, "me")
        self.home = self.m.home

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, rel):
        return os.path.join(self.home, rel)

    def make_dir(self, rel):
        os.makedirs(self.path(rel), exist_ok=True)
        return self.path(rel)

    # -- the certain kind -------------------------------------------------

    def test_state_dir_name_proves_the_move(self):
        """The folder and its state directory were renamed by hand; the
        transcripts inside still name where it used to be."""
        new = self.make_dir("work/api")
        old = self.path("dev/api")
        state = self.m.state(new)
        self.m.sessions_naming(state, old)
        self.m.memory(new, "notes.md",
                      f"Build from {old}, and from ~/dev/api/pkg too.\n")

        code, out = self.m.repair("-y")
        self.assertEqual(code, 0, out)
        body = self.m.read_memory(new, "notes.md")
        self.assertIn(new, body)
        self.assertIn("~/work/api/pkg", body)
        self.assertNotIn(old, body)
        self.assertNotIn("~/dev/api", body)

    def test_a_newer_transcript_proves_the_move(self):
        """Nothing was renamed, but the same state directory holds a session
        from before the move and one from after it."""
        new = self.make_dir("work/api")
        old = self.path("dev/api")
        state = os.path.join(self.m.claude, "projects", encode(old))
        self.m.sessions_naming(state, old, new)
        write(os.path.join(state, "memory", "notes.md"), f"lives at {old}\n")

        code, out = self.m.repair("-y")
        self.assertEqual(code, 0, out)
        self.assertIn(f"lives at {new}", read(os.path.join(state, "memory",
                                                           "notes.md")))

    def test_a_bystander_project_is_corrected_too(self):
        """A memory in one project naming another project's old path."""
        new = self.make_dir("work/api")
        old = self.path("dev/api")
        self.m.sessions_naming(self.m.state(new), old)
        other = self.m.add_project("notes", session="n1")
        self.m.memory(other, "where.md", f"the api lives at {old}\n")

        code, out = self.m.repair("-y")
        self.assertEqual(code, 0, out)
        self.assertIn(new, self.m.read_memory(other, "where.md"))

    def test_the_state_directory_path_is_rewritten_with_it(self):
        """A memory naming ~/.claude/projects/<encoded> must not be left
        pointing at a directory name that no longer exists."""
        new = self.make_dir("work/api")
        old = self.path("dev/api")
        self.m.sessions_naming(self.m.state(new), old)
        self.m.memory(new, "state.md",
                      f"transcripts in {self.m.claude}/projects/{encode(old)}/\n")

        code, out = self.m.repair("-y")
        self.assertEqual(code, 0, out)
        body = self.m.read_memory(new, "state.md")
        self.assertIn(encode(new), body)
        self.assertNotIn(encode(old), body)

    # -- the negatives ----------------------------------------------------

    def test_a_neighbour_sharing_a_prefix_is_untouched(self):
        new = self.make_dir("work/api")
        old = self.path("dev/api")
        self.m.sessions_naming(self.m.state(new), old)
        self.m.memory(new, "notes.md",
                      f"moved: {old}\nnot this one: {old}-server\n"
                      f"nor this: {old}.bak\n")

        self.m.repair("-y")
        body = self.m.read_memory(new, "notes.md")
        self.assertIn(f"not this one: {old}-server", body)
        self.assertIn(f"nor this: {old}.bak", body)
        self.assertIn(f"moved: {new}", body)

    def test_a_deleted_subdirectory_is_not_a_relocation(self):
        """A session records a cwd inside the project -- a worktree under
        .claude/, a scratch directory -- and that directory is later removed.
        It was deleted, not moved: reading it as a relocation would collapse
        every mention of a subdirectory onto the project root."""
        project = self.make_dir("work/api")
        worktree = os.path.join(project, ".claude", "worktrees", "feat")
        scratch = os.path.join(project, "scratch")
        self.m.sessions_naming(self.m.state(project), project, worktree, scratch)
        self.m.memory(project, "notes.md",
                      f"worktree at {worktree}\nscratch at {scratch}\n")

        code, out = self.m.repair("-y")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.m.read_memory(project, "notes.md"),
                         f"worktree at {worktree}\nscratch at {scratch}\n")
        self.assertIn("nothing to fix", out)

    def test_a_sibling_that_moved_is_still_found_alongside_one_that_was_deleted(self):
        """The subdirectory guard must not swallow the real relocation sharing
        its state directory."""
        project = self.make_dir("work/api")
        old = self.path("dev/api")
        scratch = os.path.join(project, "scratch")
        self.m.sessions_naming(self.m.state(project), old, scratch)
        self.m.memory(project, "notes.md", f"was {old}, scratch {scratch}\n")

        code, out = self.m.repair("-y")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.m.read_memory(project, "notes.md"),
                         f"was {project}, scratch {scratch}\n")

    def test_naming_a_project_that_matches_nothing_fails(self):
        self.m.add_project("api", session="a1")

        code, out = self.m.repair("nosuchproject", "-y")
        self.assertEqual(code, 1, out)

    def test_paths_that_still_exist_are_left_alone(self):
        new = self.make_dir("work/api")
        keep = self.make_dir("work/other")
        self.m.sessions_naming(self.m.state(new), self.path("dev/api"))
        self.m.memory(new, "notes.md", f"see {keep} and ~/work/other\n")

        self.m.repair("-y")
        self.assertEqual(self.m.read_memory(new, "notes.md"),
                         f"see {keep} and ~/work/other\n")

    def test_slash_commands_are_not_paths(self):
        project = self.m.add_project("api", session="a1")
        self.m.memory(project, "notes.md",
                      "run /simplify then /code-review, see /mcp\n")

        code, out = self.m.repair("-n")
        self.assertEqual(code, 0, out)
        self.assertNotIn("/simplify", out)
        self.assertNotIn("/code-review", out)

    def test_an_example_path_with_no_evidence_is_only_reported(self):
        """~/dev/api in a memory file, with nothing on disk suggesting where
        it went, is left exactly as written."""
        project = self.m.add_project("api", session="a1")
        self.make_dir("dev")          # the parent exists; the child does not
        self.m.memory(project, "notes.md", "for example ~/dev/gone\n")

        code, out = self.m.repair("-y")
        self.assertEqual(code, 0, out)
        self.assertIn("~/dev/gone", out)
        self.assertIn("nowhere obvious", out)
        self.assertEqual(self.m.read_memory(project, "notes.md"),
                         "for example ~/dev/gone\n")

    def test_dry_run_changes_nothing(self):
        new = self.make_dir("work/api")
        old = self.path("dev/api")
        self.m.sessions_naming(self.m.state(new), old)
        self.m.memory(new, "notes.md", f"at {old}\n")

        code, out = self.m.repair("-n")
        self.assertEqual(code, 0, out)
        self.assertIn("dry run", out)
        self.assertEqual(self.m.read_memory(new, "notes.md"), f"at {old}\n")

    def test_declining_the_prompt_changes_nothing(self):
        new = self.make_dir("work/api")
        old = self.path("dev/api")
        self.m.sessions_naming(self.m.state(new), old)
        self.m.memory(new, "notes.md", f"at {old}\n")

        code, out = self.m.repair(answer="n")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.m.read_memory(new, "notes.md"), f"at {old}\n")

    # -- helping the reader spot a false positive -------------------------

    def moved_project(self):
        """A project whose old path is proven, ready for a memory to name."""
        new = self.make_dir("work/api")
        old = self.path("dev/api")
        self.m.sessions_naming(self.m.state(new), old)
        return new, old

    def test_each_rewrite_is_quoted_before_and_after(self):
        new, old = self.moved_project()
        self.m.memory(new, "notes.md", f"The service lives at {old} today.\n")

        code, out = self.m.repair("-n")
        self.assertEqual(code, 0, out)
        self.assertIn("was  ", out)
        self.assertIn("now  ", out)
        # the temp paths are long enough that the window trims the left, so
        # assert on the tail that identifies which path is which
        self.assertIn("/dev/api today.", out)
        self.assertIn("/work/api today.", out)

    def test_a_mention_that_reads_like_history_is_flagged(self):
        new, old = self.moved_project()
        self.m.memory(new, "notes.md",
                      f"The folder had been renamed while the transcripts kept "
                      f"naming {old}.\n")

        code, out = self.m.repair("-n")
        self.assertEqual(code, 0, out)
        self.assertIn("reads like history", out)
        self.assertIn("Every reference to this reads like history", out)

    def test_a_marker_on_the_previous_line_still_counts(self):
        """Memory files are wrapped prose: the path often lands on its own
        line, with the words that give it meaning on the line above."""
        new, old = self.moved_project()
        self.m.memory(new, "notes.md",
                      f"The folder had been renamed while the transcripts\n"
                      f"kept naming {old}. Every other method missed it.\n")

        code, out = self.m.repair("-n")
        self.assertEqual(code, 0, out)
        self.assertIn("reads like history", out)

    def test_a_live_pointer_is_not_flagged(self):
        new, old = self.moved_project()
        self.m.memory(new, "notes.md", f"Run the build from {old}/scripts.\n")

        code, out = self.m.repair("-n")
        self.assertEqual(code, 0, out)
        self.assertNotIn("reads like history", out)

    def test_a_mixed_finding_counts_the_historical_ones(self):
        new, old = self.moved_project()
        self.m.memory(new, "live.md", f"Run the build from {old}/scripts.\n")
        self.m.memory(new, "past.md", f"It had been at {old} before.\n")

        code, out = self.m.repair("-n")
        self.assertEqual(code, 0, out)
        self.assertIn("1 of 2 references read like history", out)

    def test_files_are_named_by_their_project_not_the_encoding(self):
        new, old = self.moved_project()
        self.m.memory(new, "notes.md", f"at {old}\n")

        code, out = self.m.repair("-n")
        self.assertEqual(code, 0, out)
        self.assertIn("api/memory/notes.md:1", out)
        self.assertNotIn(f"{encode(new)}/memory/notes.md", out)

    def test_a_change_near_the_start_of_a_line_is_quoted_whole(self):
        """The window would slide off the left edge here.  A short line needs
        no trimming at all, and must come through intact."""
        self.moved_project()
        new = self.path("work/api")
        self.m.memory(new, "notes.md", "~/dev/api is where it was.\n")

        code, out = self.m.repair("-n")
        self.assertEqual(code, 0, out)
        self.assertIn("was  ~/dev/api is where it was.", out)
        self.assertIn("now  ~/work/api is where it was.", out)

    # -- the guesses ------------------------------------------------------

    def test_a_project_of_the_same_name_is_offered_as_a_guess(self):
        here = self.m.add_project("work/api", session="a1")
        os.makedirs(here, exist_ok=True)
        other = self.m.add_project("notes", session="n1")
        self.m.memory(other, "where.md", f"the api lives at {self.path('dev/api')}\n")

        code, out = self.m.repair("-n")
        self.assertEqual(code, 0, out)
        self.assertIn("(a guess)", out)
        self.assertIn("the only project Claude knows by that name", out)
        self.assertIn(here, out.replace("~", self.home))

    def test_a_directory_found_by_search_is_offered_as_a_guess(self):
        self.make_dir("elsewhere/widget")
        project = self.m.add_project("api", session="a1")
        self.m.memory(project, "where.md", f"built in {self.path('gone/widget')}\n")

        code, out = self.m.repair("-n")
        self.assertEqual(code, 0, out)
        self.assertIn("the only directory by that name", out)

        code, out = self.m.repair("-n", "--no-search")
        self.assertEqual(code, 0, out)
        self.assertNotIn("the only directory by that name", out)

    def test_the_search_ignores_directories_inside_a_project(self):
        """A project's own tree is full of names like tests and src; a guess
        drawn from one of those would match almost anything."""
        project = self.m.add_project("api", session="a1")
        os.makedirs(os.path.join(project, "widget"), exist_ok=True)
        self.m.memory(project, "where.md", f"built in {self.path('gone/widget')}\n")

        code, out = self.m.repair("-n")
        self.assertEqual(code, 0, out)
        self.assertNotIn("the only directory by that name", out)

    def test_choosing_by_number_applies_only_that_one(self):
        new = self.make_dir("work/api")
        old = self.path("dev/api")
        self.m.sessions_naming(self.m.state(new), old)
        self.make_dir("elsewhere/widget")
        self.m.memory(new, "notes.md",
                      f"at {old}, built in {self.path('gone/widget')}\n")

        code, out = self.m.repair(answer="1")
        self.assertEqual(code, 0, out)
        body = self.m.read_memory(new, "notes.md")
        self.assertIn(new, body)                          # the certain one
        self.assertIn(self.path("gone/widget"), body)     # the guess, declined

    def test_the_certain_finding_is_listed_first(self):
        new = self.make_dir("work/api")
        old = self.path("dev/api")
        self.m.sessions_naming(self.m.state(new), old)
        self.make_dir("elsewhere/widget")
        self.m.memory(new, "notes.md",
                      f"at {old}, built in {self.path('gone/widget')}\n")

        code, out = self.m.repair("-n")
        self.assertEqual(code, 0, out)
        self.assertLess(out.index("Claude's own state"),
                        out.index("the only directory by that name"))

    # -- safety -----------------------------------------------------------

    def test_a_backup_holds_the_original_text(self):
        new = self.make_dir("work/api")
        old = self.path("dev/api")
        self.m.sessions_naming(self.m.state(new), old)
        self.m.memory(new, "notes.md", f"at {old}\n")

        code, out = self.m.repair("-y")
        self.assertEqual(code, 0, out)
        root = os.path.join(self.m.claude, cm.BACKUP_DIR)
        copies = [os.path.join(base, f)
                  for base, _dirs, files in os.walk(root) for f in files]
        self.assertEqual(len(copies), 1, copies)
        self.assertEqual(read(copies[0]), f"at {old}\n")

    def test_no_backup_skips_the_copy(self):
        new = self.make_dir("work/api")
        self.m.sessions_naming(self.m.state(new), self.path("dev/api"))
        self.m.memory(new, "notes.md", f"at {self.path('dev/api')}\n")

        self.m.repair("-y", "--no-backup")
        self.assertFalse(os.path.exists(os.path.join(self.m.claude,
                                                     cm.BACKUP_DIR)))

    def test_naming_a_project_limits_what_is_read(self):
        new = self.make_dir("work/api")
        old = self.path("dev/api")
        self.m.sessions_naming(self.m.state(new), old)
        other = self.m.add_project("notes", session="n1")
        self.m.memory(new, "notes.md", f"at {old}\n")
        self.m.memory(other, "where.md", f"the api is at {old}\n")

        code, out = self.m.repair("notes", "-y")
        self.assertEqual(code, 0, out)
        self.assertIn(new, self.m.read_memory(other, "where.md"))
        self.assertEqual(self.m.read_memory(new, "notes.md"), f"at {old}\n")

    def test_nothing_to_fix_says_so(self):
        project = self.m.add_project("api", session="a1")
        self.m.memory(project, "notes.md", "nothing here names a path\n")

        code, out = self.m.repair("-y")
        self.assertEqual(code, 0, out)
        self.assertIn("nothing to fix", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
