from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from util import REPO_ROOT, load_module


class TestShuffleDiff(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module("shuffle_diff_module", "quality/scripts/llm-proxy/shuffle_diff.py")
        cls.script = REPO_ROOT / "quality" / "scripts" / "llm-proxy" / "shuffle_diff.py"

    def test_shuffle_diff_preserves_preamble_and_blocks_deterministically(self) -> None:
        diff_text = (
            "NOTE: synthetic review artifact\n\n"
            "diff --git a/a.go b/a.go\n"
            "@@ -1 +1 @@\n"
            "-old a\n"
            "+new a\n"
            "diff --git a/b.go b/b.go\n"
            "@@ -1 +1 @@\n"
            "-old b\n"
            "+new b\n"
            "diff --git a/c.bin b/c.bin\n"
            "Binary files a/c.bin and b/c.bin differ\n"
        )

        first = self.module.shuffle_diff(diff_text, seed=42)
        second = self.module.shuffle_diff(diff_text, seed=42)

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("NOTE: synthetic review artifact\n\n"))
        self.assertEqual(first.count("diff --git a/a.go b/a.go"), 1)
        self.assertEqual(first.count("diff --git a/b.go b/b.go"), 1)
        self.assertEqual(first.count("diff --git a/c.bin b/c.bin"), 1)
        self.assertIn("Binary files a/c.bin and b/c.bin differ", first)

    def test_shuffle_diff_noops_on_empty_or_single_block(self) -> None:
        self.assertEqual(self.module.shuffle_diff("", seed=7), "")
        single = "diff --git a/x.go b/x.go\n@@ -1 +1 @@\n-old\n+new\n"
        self.assertEqual(self.module.shuffle_diff(single, seed=7), single)

    def test_cli_reads_and_writes_files(self) -> None:
        diff_text = (
            "diff --git a/a.go b/a.go\n"
            "@@ -1 +1 @@\n"
            "-old a\n"
            "+new a\n"
            "diff --git a/b.go b/b.go\n"
            "@@ -1 +1 @@\n"
            "-old b\n"
            "+new b\n"
        )

        with tempfile.TemporaryDirectory() as tmp:
            input_file = Path(tmp) / "input.diff"
            output_file = Path(tmp) / "output.diff"
            input_file.write_text(diff_text, encoding="utf-8")

            subprocess.run(
                [
                    "python3",
                    str(self.script),
                    "--input-file",
                    str(input_file),
                    "--output-file",
                    str(output_file),
                    "--seed",
                    "9",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            written = output_file.read_text(encoding="utf-8")
            self.assertEqual(written, self.module.shuffle_diff(diff_text, seed=9))


if __name__ == "__main__":
    unittest.main()
