import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "peek"))

os.environ.setdefault("HERDR_PLUGIN_STATE_DIR", tempfile.mkdtemp(prefix="peek-state-"))

import kinds  # noqa: E402
import render  # noqa: E402
import term  # noqa: E402


def plain(lines):
    return [term.ANSI_RE.sub("", l) for l in lines]


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data, mode="w"):
        path = os.path.join(self.tmp.name, name)
        with open(path, mode) as fh:
            fh.write(data)
        return path

    def test_classify(self):
        cases = {
            "a.png": (b"\x89PNG\r\n\x1a\n" + b"\0" * 16, "image"),
            "a.gif": (b"GIF89a", "anim"),
            "a.pdf": (b"%PDF-1.4", "pdf"),
            "a.md": (b"# hi", "markdown"),
            "a.csv": (b"a,b\n1,2\n", "csv"),
            "a.jsonl": (b"{}\n", "jsonl"),
            "a.py": (b"print(1)\n", "text"),
            "noext": (b"\x00\x01\x02", "binary"),
            "pic": (b"\x89PNG\r\n\x1a\n", "image"),
        }
        for name, (data, kind) in cases.items():
            self.assertEqual(kinds.classify(self.write(name, data, "wb")), kind, name)
        self.assertEqual(kinds.classify(self.tmp.name), "dir")

    def test_csv_table_aligns_numbers(self):
        path = self.write("t.csv", "name,n\nalpha,1\nb,200\n")
        lines, meta = render.csv_table(path, 80)
        text = plain(lines)
        self.assertEqual(meta, "2 rows × 2 cols")
        self.assertTrue(text[2].endswith("  1"))
        self.assertTrue(text[3].endswith("200"))

    def test_json_tree(self):
        path = self.write("t.json", json.dumps({"a": [1, 2], "b": {"c": None}}))
        lines, meta = render.json_tree(path, 80)
        self.assertEqual(meta, "object · 2 keys")
        self.assertIn('  "a": [1, 2],', plain(lines))

    def test_invalid_json_falls_back(self):
        path = self.write("bad.json", "{nope")
        lines, _ = render.json_tree(path, 80)
        self.assertTrue(plain(lines)[0].startswith("invalid JSON"))

    def test_markdown_builtin(self):
        path = self.write("r.md", "# Title\n\n- [x] done\n- todo\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
        import convert

        convert._which["glow"] = None  # force the built-in renderer
        lines, tip = render.markdown(path, 60)
        text = "\n".join(plain(lines))
        self.assertIn("TITLE", text)
        self.assertIn("[x] done", text)
        self.assertIn("a │ b", text)
        self.assertIn("glow", tip)

    def test_sqlite_read_only(self):
        path = os.path.join(self.tmp.name, "a.db")
        con = sqlite3.connect(path)
        con.execute("create table t(x)")
        con.execute("insert into t values (42)")
        con.commit()
        con.close()
        lines, meta = render.sqlite(path, 80)
        text = "\n".join(plain(lines))
        self.assertIn("▸ t", text)
        self.assertIn("42", text)
        self.assertIn("read-only", meta)

    def test_zip_listing(self):
        path = os.path.join(self.tmp.name, "a.zip")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("x/y.txt", "hello")
        lines, meta = render.archive(path, 80)
        self.assertIn("1 entries", meta)
        self.assertTrue(any(l.endswith("x/y.txt") for l in plain(lines)))

    def test_hex_fallback(self):
        path = self.write("b.bin", bytes(range(32)), "wb")
        lines, _ = render.info_hex(path, 80)
        self.assertTrue(any(l.startswith("00000000") for l in plain(lines)))

    def test_ansi_slice_keeps_width(self):
        s = term.BOLD + "hello" + term.RESET + " 日本語"
        self.assertEqual(term.visible_len(term.ansi_slice(s, 0, 8)), 8)
        # a wide char never gets split in half
        self.assertEqual(term.visible_len(term.ansi_slice(s, 0, 7)), 6)
        self.assertEqual(term.ANSI_RE.sub("", term.ansi_slice(s, 2, 3)), "llo")

    def test_kitty_chunks(self):
        out = term.kitty_transmit(b"x" * 10000, 7, cols=10, rows=5)
        self.assertTrue(out.startswith("\x1b_Ga=T,f=100,t=d,i=7,q=2,p=1,C=1,c=10,r=5,m=1;"))
        self.assertEqual(out.count("\x1b_G"), 4)
        self.assertIn("m=0", out.split("\x1b_G")[-1])


if __name__ == "__main__":
    unittest.main()
