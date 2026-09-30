"""Check English localization and the standalone command-line entry point."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "align_mdoc_frames_en.py"


class EnglishCliTests(unittest.TestCase):
    def invoke(self, *args):
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *map(str, args)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", timeout=30,
        )

    def test_english_source_contains_only_ascii(self):
        self.assertTrue(SCRIPT.read_text(encoding="utf-8").isascii())

    def test_help_is_english(self):
        result = self.invoke("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Prefix TIFF frame filenames", result.stdout)
        self.assertIn("--backup-dir", result.stdout)
        self.assertIn("--backup-mdoc", result.stdout)
        self.assertNotRegex(result.stdout, r"[\u3400-\u9fff]")

    def test_missing_directory_error_is_english(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.invoke("--mdoc", temporary, "--frames", Path(temporary) / "absent")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Frames directory does not exist", result.stderr)

    def test_standalone_apply_reports_missing_and_renamed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frames = root / "frames"
            frames.mkdir()
            raw = frames / "raw.tif"
            raw.write_bytes(b"binary test payload\x00\xff")
            mdoc = root / "sample.mdoc"
            original = b"SubFramePath = Z:\\old\\raw.tif\nSubFramePath = missing.tif\n"
            mdoc.write_bytes(original)
            result = self.invoke("--mdoc", mdoc, "--frames", frames, "--path-mode", "filename", "--apply")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertIn("Missing files: 1 (references: 1", result.stdout)
            self.assertIn("Successfully renamed this run: 1", result.stdout)
            self.assertTrue((frames / "sample_raw.tif").exists())
            self.assertEqual((root / "frames_backup" / "raw.tif").read_bytes(), b"binary test payload\x00\xff")
            self.assertEqual(mdoc.read_bytes(), original.replace(b"Z:\\old\\raw.tif", b"sample_raw.tif"))
            self.assertEqual(Path(str(mdoc) + ".bak").read_bytes(), original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
