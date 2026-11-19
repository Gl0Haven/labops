import tempfile
import unittest
from pathlib import Path

from labops.policy import changed_symbols, frozen_hits, glob_match, out_of_scope
from tests.helpers import git, make_repo


BASE = """VALUE = 1

class A:
    @staticmethod
    def b():
        return 1

    def c(self):
        return 2

def free():
    return 3
"""


def freeze(symbols):
    return {"schema": 1, "frozen": [{"path": "calc.py", "symbols": symbols, "ref": "§3.3", "note": "frozen"}]}


class PolicyTests(unittest.TestCase):
    def setup_repo(self, root, text=BASE):
        repo = root / "repo"
        head = make_repo(repo, files={"calc.py": text, "docs/readme.md": "initial\n"})
        return repo, head

    def commit(self, repo, text, *, path="calc.py", delete=False):
        if delete:
            git(repo, "rm", "--", path)
            git(repo, "commit", "-m", "delete")
            return git(repo, "rev-parse", "HEAD").stdout.decode().strip()
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
        git(repo, "add", "--", path)
        git(repo, "commit", "-m", "change")
        return git(repo, "rev-parse", "HEAD").stdout.decode().strip()

    def test_glob_rules_and_allowed_paths(self):
        self.assertTrue(glob_match("**/README?.md", "README1.md"))
        self.assertTrue(glob_match("**/README?.md", "docs/README1.md"))
        self.assertFalse(glob_match("src/*.py", "src/nested/a.py"))
        with tempfile.TemporaryDirectory() as td:
            repo, first = self.setup_repo(Path(td))
            second = self.commit(repo, "changed\n", path="docs/readme.md")
            self.assertEqual(out_of_scope(repo, ["calc.py", "src/**"], first, second), ["docs/readme.md"])
            self.assertEqual(out_of_scope(repo, [], first, second), [])

    def test_non_frozen_function_and_frozen_method_or_class(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, base = self.setup_repo(root / "free")
            free_changed = self.commit(repo, BASE.replace("return 3", "return 4"))
            self.assertEqual(frozen_hits(repo, freeze(["A.b"]), base, free_changed), [])
            self.assertIn({"path": "calc.py", "qualname": "free"}, changed_symbols(repo, base, free_changed))
            method_repo, method_base = self.setup_repo(root / "method")
            method_changed = self.commit(method_repo, BASE.replace("return 1", "return 10"))
            hits = frozen_hits(method_repo, freeze(["A.b"]), method_base, method_changed)
            self.assertEqual(hits[0]["reason"], "frozen_symbol")
            self.assertIn("A.b", hits[0]["symbols"])
            class_repo, class_base = self.setup_repo(root / "class")
            class_changed = self.commit(class_repo, BASE.replace("return 2", "return 20"))
            hits = frozen_hits(class_repo, freeze(["A"]), class_base, class_changed)
            self.assertEqual(hits[0]["reason"], "frozen_symbol")
            self.assertIn("A.c", hits[0]["symbols"])

    def test_decorator_and_module_level_changes_hit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, base = self.setup_repo(root / "decorator")
            decorator_text = BASE.replace("@staticmethod", "@classmethod")
            changed = self.commit(repo, decorator_text)
            hit = frozen_hits(repo, freeze(["A.b"]), base, changed)[0]
            self.assertEqual(hit["reason"], "decorator")
            self.assertIn("@decorator:A.b", hit["symbols"])
            const_repo, const_base = self.setup_repo(root / "constant")
            constant = self.commit(const_repo, BASE.replace("VALUE = 1", "VALUE = 2"))
            hit = frozen_hits(const_repo, freeze(["A.b"]), const_base, constant)[0]
            self.assertEqual(hit["reason"], "module_level")

    def test_deleted_non_python_and_path_level_changes(self):
        with tempfile.TemporaryDirectory() as td:
            repo, base = self.setup_repo(Path(td))
            deleted = self.commit(repo, "", delete=True)
            self.assertEqual(frozen_hits(repo, freeze(["A.b"]), base, deleted)[0]["reason"], "file_deleted")
            doc_changed = self.commit(repo, "updated\n", path="docs/readme.md")
            symbol_policy = {"schema": 1, "frozen": [{"path": "docs/**", "symbols": ["A.b"], "ref": "§x", "note": ""}]}
            self.assertEqual(frozen_hits(repo, symbol_policy, base, doc_changed)[0]["reason"], "non_python_symbol_policy")
            path_policy = {"schema": 1, "frozen": [{"path": "docs/**", "symbols": None, "ref": "§x", "note": ""}]}
            self.assertEqual(frozen_hits(repo, path_policy, base, doc_changed)[0]["reason"], "frozen_path")

    def test_syntax_error_on_either_side_is_a_hit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, base = self.setup_repo(root)
            bad_new = self.commit(repo, "def broken(:\n    pass\n")
            self.assertEqual(frozen_hits(repo, freeze(["A.b"]), base, bad_new)[0]["reason"], "ast_parse_failed")
            repo2 = root / "old_bad"
            old_bad = make_repo(repo2, files={"calc.py": "def broken(:\n    pass\n"})
            new_good = self.commit(repo2, "def broken():\n    return 1\n")
            self.assertEqual(frozen_hits(repo2, freeze(["broken"]), old_bad, new_good)[0]["reason"], "ast_parse_failed")


if __name__ == "__main__":
    unittest.main()
