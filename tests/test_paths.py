import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "peek"))

import paths  # noqa: E402


class FindPathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for rel in ("shots/a.png", "shots/b.png", "docs/r.pdf", "my dir/x y.txt", "main.py"):
            full = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            open(full, "w").close()

    def tearDown(self):
        self.tmp.cleanup()

    def find(self, text, **kw):
        return paths.find_paths(text, cwd=self.root, **kw)

    def rel(self, hit):
        return os.path.relpath(hit.path, self.root)

    def test_plain_and_punctuated(self):
        hits = self.find("● Saved to:\n   shots/a.png, and (shots/b.png).\n")
        self.assertEqual([self.rel(h) for h in hits], ["shots/a.png", "shots/b.png"])
        self.assertEqual((hits[0].row, hits[0].col), (1, 3))
        self.assertEqual(hits[0].text, "shots/a.png")

    def test_line_suffix(self):
        hits = self.find("error at main.py:42:3 here\n")
        self.assertEqual(self.rel(hits[0]), "main.py")
        self.assertEqual(hits[0].line, 42)

    def test_quoted_with_spaces(self):
        hits = self.find('wrote "my dir/x y.txt" ok\n')
        self.assertEqual(self.rel(hits[0]), "my dir/x y.txt")

    def test_escaped_space(self):
        hits = self.find("open my\\ dir/x\\ y.txt now\n")
        self.assertEqual(self.rel(hits[0]), "my dir/x y.txt")

    def test_file_url(self):
        url = "file://" + os.path.join(self.root, "main.py")
        hits = self.find("link %s here\n" % url)
        self.assertEqual(self.rel(hits[0]), "main.py")
        self.assertEqual(hits[0].text, url)

    def test_ignores_urls_and_missing(self):
        hits = self.find("https://example.com/shots/a.png no-such.png README\n")
        self.assertEqual(hits, [])

    def test_git_diff_prefix_and_at(self):
        hits = self.find("--- a/main.py\n+++ b/main.py\n@docs/r.pdf\n")
        self.assertEqual([self.rel(h) for h in hits], ["main.py", "main.py", "docs/r.pdf"])

    def test_directory_and_absolute(self):
        hits = self.find("cd %s/shots\n" % self.root)
        self.assertTrue(hits[0].is_dir)

    def test_wrapped_row(self):
        text = "xx shots/a.p\nng tail\n"
        hits = self.find(text, width=12)
        self.assertEqual(self.rel(hits[0]), "shots/a.png")
        self.assertEqual((hits[0].row, hits[0].col), (0, 3))

    def test_wide_chars_shift_columns(self):
        hits = self.find("日本 main.py\n")
        self.assertEqual(hits[0].col, 5)

    def test_newest_prefers_files(self):
        hits = self.find("shots/a.png\ndocs/r.pdf\nshots\n")
        self.assertEqual(self.rel(paths.newest(hits)), "docs/r.pdf")

    def test_hints_shared_for_duplicates(self):
        hits = self.find("shots/a.png docs/r.pdf shots/a.png\n")
        hints = paths.assign_hints(hits)
        self.assertEqual(sorted(hints.values()), ["a", "s"])

    def test_two_letter_hints(self):
        hits = [paths.Hit("/p%d" % i, None, i, 0, 1, "x", False) for i in range(30)]
        hints = paths.assign_hints(hits)
        self.assertEqual(len(set(hints.values())), 30)
        self.assertTrue(all(len(v) == 2 for v in hints.values()))

    def test_selection(self):
        hits = paths.path_from_selection("  docs/r.pdf ", cwd=self.root)
        self.assertEqual(self.rel(hits[0]), "docs/r.pdf")
        hits = paths.path_from_selection("my dir/x y.txt", cwd=self.root)
        self.assertEqual(self.rel(hits[0]), "my dir/x y.txt")


if __name__ == "__main__":
    unittest.main()
