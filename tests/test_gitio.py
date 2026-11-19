import tempfile
import unittest
from pathlib import Path

from labops import gitio
from tests.helpers import commit_file, git, make_repo


class GitIoTests(unittest.TestCase):
    def test_ref_cas_and_object_format(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            head = make_repo(repo)
            self.assertEqual(gitio.object_format(repo), "sha1")
            self.assertEqual(len(gitio.zero_oid(repo)), 40)
            ref = "refs/labops/test/submitted"
            self.assertTrue(gitio.update_ref_cas(repo, ref, head, gitio.zero_oid(repo)))
            self.assertFalse(gitio.update_ref_cas(repo, ref, head, gitio.zero_oid(repo)))
            self.assertEqual(gitio.read_ref(repo, ref), head)
            self.assertEqual(gitio.for_each_ref(repo, "refs/labops/test"), [(ref, head)])

    def test_path_parsing_hunks_worktree_and_trailers(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "主 仓库"
            first = make_repo(repo, files={"带 空格 中文.txt": "one\ntwo\nthree\n"})
            second = commit_file(repo, "带 空格 中文.txt", "one\nchanged\nthree\nfour\n", "update")
            self.assertEqual(gitio.status_porcelain(repo), [])
            self.assertIn(("M", "带 空格 中文.txt"), gitio.diff_name_status(repo, first, second))
            self.assertIn((2, 1, "带 空格 中文.txt"), gitio.diff_numstat(repo, first, second))
            self.assertTrue(gitio.diff_hunks(repo, first, second, "带 空格 中文.txt"))
            self.assertEqual(gitio.show_file(repo, second, "带 空格 中文.txt"), b"one\nchanged\nthree\nfour\n")
            self.assertIsNone(gitio.show_file(repo, second, "missing.txt"))
            self.assertIn("refs/heads/main", [x["branch"] for x in gitio.worktrees(repo)])
            git(repo, "commit", "--allow-empty", "-m", "trailered", "--trailer", "LabOps-Task: T20261019-demo")
            current = git(repo, "rev-parse", "HEAD").stdout.decode().strip()
            self.assertEqual(gitio.commit_trailers(repo, current)["LabOps-Task"], "T20261019-demo")

    def test_porcelain_preserves_untracked_unicode_path(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            make_repo(repo)
            (repo / "有 空格.txt").write_text("x", encoding="utf-8")
            self.assertEqual(gitio.status_porcelain(repo), [("??", "有 空格.txt")])

    def test_hunk_insert_delete_boundaries_and_sha256(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            old = make_repo(repo, files={"f.txt": "a\nb\nc\n"})
            inserted = commit_file(repo, "f.txt", "a\nb\nx\nc\n")
            insert_hunks = gitio.diff_hunks(repo, old, inserted, "f.txt")
            self.assertTrue(any(old_len == 0 and new_len == 1 for _, old_len, _, new_len in insert_hunks))
            deleted = commit_file(repo, "f.txt", "a\nc\n")
            delete_hunks = gitio.diff_hunks(repo, inserted, deleted, "f.txt")
            self.assertTrue(any(old_len > 0 and new_len == 0 for _, old_len, _, new_len in delete_hunks))
            sha_repo = Path(td) / "sha256"
            make_repo(sha_repo, sha256=True)
            self.assertEqual(gitio.object_format(sha_repo), "sha256")
            self.assertEqual(len(gitio.zero_oid(sha_repo)), 64)


if __name__ == "__main__":
    unittest.main()
