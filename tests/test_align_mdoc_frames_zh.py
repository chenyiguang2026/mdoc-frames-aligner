import ast
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "align_mdoc_frames.py"
spec = importlib.util.spec_from_file_location("align_mdoc_frames", SCRIPT)
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)


class AlignmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.frames = self.root / "frames"
        self.frames.mkdir()
        self.mdocs = self.root / "mdocs"
        self.mdocs.mkdir()
        self.backup = self.root / "frames_backup"

    def frame(self, name="raw.tif", data=b"\x00TIFF\xff\x10"):
        path = self.frames / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def mdoc(self, name="sample.mdoc", value=r"Z:\old\raw.tif", encoding="utf-8", text=None):
        path = self.mdocs / name
        text = text if text is not None else f"[ZValue = 0]\r\nSubFramePath = {value}\r\nTiltAngle = -60\r\n"
        path.write_bytes(text.encode(encoding))
        return path

    def run_script(self, *extra):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            result = app.main(["--mdoc", str(self.mdocs), "--frames", str(self.frames), *extra])
        return result, stream.getvalue()

    def test_preview_writes_nothing(self):
        raw = self.frame()
        doc = self.mdoc()
        before = doc.read_bytes()
        result, _ = self.run_script()
        self.assertEqual(result, 0)
        self.assertTrue(raw.exists())
        self.assertEqual(doc.read_bytes(), before)
        self.assertFalse(self.backup.exists())
        self.assertEqual(list(self.mdocs.iterdir()), [doc])

    def test_backup_rename_rewrite_and_repeat(self):
        raw = self.frame("nested/raw.tif")
        data = raw.read_bytes()
        extra = self.frame("unreferenced.TIFF", b"other data")
        doc = self.mdoc()
        original = doc.read_bytes()
        result, output = self.run_script("--apply")
        self.assertEqual(result, 0, output)
        target = self.frames / "nested/sample_raw.tif"
        self.assertEqual(target.read_bytes(), data)
        self.assertFalse(raw.exists())
        self.assertEqual((self.backup / "nested/raw.tif").read_bytes(), data)
        self.assertEqual((self.backup / extra.name).read_bytes(), extra.read_bytes())
        self.assertEqual(Path(str(doc) + ".bak").read_bytes(), original)
        self.assertEqual(doc.read_bytes(), original.replace(b"Z:\\old\\raw.tif", str(target).encode()))
        result, output = self.run_script("--apply")
        self.assertEqual(result, 0, output)
        self.assertNotIn("sample_sample", " ".join(p.name for p in self.frames.rglob("*")))
        self.assertEqual(Path(str(doc) + ".bak").read_bytes(), original)

    def test_all_missing_is_counted_without_mutation(self):
        self.frame()
        doc = self.mdoc(value="missing.tif")
        before = doc.read_bytes()
        result, output = self.run_script("--apply")
        self.assertEqual(result, 0, output)
        self.assertFalse(self.backup.exists())
        self.assertEqual(doc.read_bytes(), before)
        self.assertFalse(Path(str(doc) + ".bak").exists())
        self.assertIn("未找到文件：1 个（1 条引用", output)
        self.assertIn("本次成功重命名：0 个文件", output)
        self.assertNotIn("名称和引用已经对齐", output)

    def test_missing_before_and_after_found_reference_is_preserved(self):
        self.frame()
        content = "SubFramePath = /old/missing1.tif\r\nSubFramePath = raw.tif\r\nSubFramePath = /old/missing2.tif\r\n"
        doc = self.mdoc(text=content)
        before = doc.read_bytes()
        result, output = self.run_script("--apply", "--path-mode", "filename")
        self.assertEqual(result, 0, output)
        self.assertEqual(doc.read_bytes(), before.replace(b"raw.tif", b"sample_raw.tif"))
        self.assertTrue((self.frames / "sample_raw.tif").exists())
        self.assertEqual(Path(str(doc) + ".bak").read_bytes(), before)
        self.assertIn("未找到文件：2 个（2 条引用", output)
        self.assertIn("本次成功重命名：1 个文件", output)

    def test_all_missing_mdoc_does_not_stop_later_mdocs(self):
        self.frame()
        missing_doc = self.mdoc("a.mdoc", value="absent.tif", encoding="utf-16-be",
                                text="\ufeffSubFramePath = absent.tif\r\n")
        before = missing_doc.read_bytes()
        self.mdoc("z.mdoc")
        result, output = self.run_script("--apply")
        self.assertEqual(result, 0, output)
        self.assertEqual(missing_doc.read_bytes(), before)
        self.assertFalse(Path(str(missing_doc) + ".bak").exists())
        self.assertTrue((self.frames / "z_raw.tif").exists())
        self.assertIn("未找到文件：1 个", output)
        self.assertIn("本次成功重命名：1 个文件", output)

    def test_missing_preview_reports_estimate_and_does_not_write(self):
        raw = self.frame()
        doc = self.mdoc(text="SubFramePath = absent.tif\nSubFramePath = raw.tif\n")
        before = doc.read_bytes()
        result, output = self.run_script("--dry-run")
        self.assertEqual(result, 0, output)
        self.assertTrue(raw.exists())
        self.assertEqual(doc.read_bytes(), before)
        self.assertFalse(self.backup.exists())
        self.assertIn("未找到文件：1 个", output)
        self.assertIn("预计重命名：1 个文件；本次实际重命名：0 个", output)
        self.assertNotIn("本次成功重命名：1", output)

    def test_duplicate_references_have_separate_reference_and_file_counts(self):
        self.frame()
        self.mdoc(text="SubFramePath = absent.tif\nSubFramePath = ABSENT.TIF\nSubFramePath = raw.tif\nSubFramePath = raw.tif\n")
        result, output = self.run_script("--apply")
        self.assertEqual(result, 0, output)
        self.assertIn("未找到文件：1 个（2 条引用", output)
        self.assertIn("成功匹配引用：2 条", output)
        self.assertIn("本次成功重命名：1 个文件", output)

    def test_missing_same_basename_in_different_mdocs_counts_separately(self):
        self.mdoc("a.mdoc", value="absent.tif")
        self.mdoc("b.mdoc", value="absent.tif")
        result, output = self.run_script("--apply")
        self.assertEqual(result, 0, output)
        self.assertIn("未找到文件：2 个（2 条引用", output)
        self.assertFalse(self.backup.exists())

    def test_repeat_with_missing_does_not_claim_previous_renames(self):
        self.frame()
        self.mdoc(text="SubFramePath = absent.tif\nSubFramePath = raw.tif\n")
        result, output = self.run_script("--apply")
        self.assertEqual(result, 0, output)
        result, output = self.run_script("--apply")
        self.assertEqual(result, 0, output)
        self.assertIn("未找到文件：1 个", output)
        self.assertIn("本次成功重命名：0 个文件", output)
        self.assertIn("已有前缀、无需重命名：1 个文件", output)

    def test_missing_with_no_update_mdoc_keeps_original_content(self):
        self.frame()
        doc = self.mdoc(text="SubFramePath = absent.tif\nSubFramePath = raw.tif\n")
        before = doc.read_bytes()
        result, output = self.run_script("--apply", "--no-update-mdoc")
        self.assertEqual(result, 0, output)
        self.assertEqual(doc.read_bytes(), before)
        self.assertFalse(Path(str(doc) + ".bak").exists())
        self.assertTrue((self.frames / "sample_raw.tif").exists())
        self.assertIn("本次成功重命名：1 个文件", output)
        self.assertIn("本次更新 mdoc：0 个", output)

    def test_duplicate_basename_rejected(self):
        self.frame("one/raw.tif")
        self.frame("two/RAW.TIF")
        self.mdoc()
        result, _ = self.run_script("--apply")
        self.assertEqual(result, 1)
        self.assertFalse(self.backup.exists())

    def test_destination_collision_rejected(self):
        self.frame()
        occupied = self.frame("sample_raw.tif", b"do not overwrite")
        self.mdoc()
        result, _ = self.run_script("--apply")
        self.assertEqual(result, 1)
        self.assertFalse(self.backup.exists())
        self.assertEqual(occupied.read_bytes(), b"do not overwrite")

    def test_shared_frame_rejected(self):
        self.frame()
        self.mdoc("a.mdoc")
        self.mdoc("b.mdoc")
        result, _ = self.run_script("--apply")
        self.assertEqual(result, 1)
        self.assertFalse(self.backup.exists())

    def test_multiple_mdocs_and_tiff_case(self):
        self.frame("one.TIFF")
        self.frame("two.tif")
        a = self.mdoc("a.mdoc", "/wrong/one.tiff")
        b = self.mdoc("b.MDOC", "C:\\bad\\two.tif")
        result, output = self.run_script("--apply", "--path-mode", "filename")
        self.assertEqual(result, 0, output)
        self.assertIn(b"a_one.TIFF", a.read_bytes())
        self.assertIn(b"b_two.tif", b.read_bytes())
        self.assertTrue((self.frames / "a_one.TIFF").exists())
        self.assertTrue((self.frames / "b_two.tif").exists())

    def test_encoding_quotes_newlines_and_non_tif(self):
        self.frame("raw space.tif")
        text = 'Title = 中文\r\n[ZValue = 0]\r\n  SubFramePath = "Z:\\旧路径\\raw space.tif"  \r\nTiltAngle = -2\r\nSubFramePath = gain.mrc\r\n'
        for codec in ("utf-8-sig", "gb18030", "utf-16-le", "utf-16-be"):
            with self.subTest(codec=codec):
                content = ("\ufeff" if codec.startswith("utf-16-") else "") + text
                doc = self.mdoc(text=content, encoding=codec)
                plan = app.build_plan([doc], self.frames, "filename", None, True)
                updated = plan.documents[0].updated.decode(codec)
                self.assertEqual(updated, content.replace("Z:\\旧路径\\raw space.tif", "sample_raw space.tif"))
                self.assertEqual(plan.skipped, 1)

    def test_relative_path(self):
        self.frame()
        doc = self.mdoc()
        plan = app.build_plan([doc], self.frames, "relative", None, True)
        self.assertIn(b"../frames/sample_raw.tif", plan.documents[0].updated)

    def test_no_update_and_stale_reference_repeat(self):
        self.frame()
        doc = self.mdoc()
        before = doc.read_bytes()
        result, output = self.run_script("--apply", "--no-update-mdoc")
        self.assertEqual(result, 0, output)
        self.assertEqual(doc.read_bytes(), before)
        self.assertFalse(Path(str(doc) + ".bak").exists())
        result, output = self.run_script("--apply", "--no-update-mdoc")
        self.assertEqual(result, 0, output)

    def test_existing_backup_rejected(self):
        raw = self.frame()
        self.mdoc()
        self.backup.mkdir()
        marker = self.backup / "important.txt"
        marker.write_text("preserve")
        result, _ = self.run_script("--apply")
        self.assertEqual(result, 1)
        self.assertTrue(raw.exists())
        self.assertEqual(marker.read_text(), "preserve")

    def test_existing_mdoc_backup_rejected(self):
        raw = self.frame()
        doc = self.mdoc()
        saved = Path(str(doc) + ".bak")
        saved.write_bytes(b"earlier backup")
        result, _ = self.run_script("--apply")
        self.assertEqual(result, 1)
        self.assertTrue(raw.exists())
        self.assertFalse(self.backup.exists())
        self.assertEqual(saved.read_bytes(), b"earlier backup")

    def test_backup_inside_frames_rejected(self):
        self.frame()
        self.mdoc()
        result, _ = self.run_script("--apply", "--backup-dir", str(self.frames / "backup"))
        self.assertEqual(result, 1)
        self.assertFalse((self.frames / "backup").exists())

    def test_repeated_reference_renamed_once(self):
        self.frame()
        doc = self.mdoc(text="SubFramePath = raw.tif\nSubFramePath = raw.tif\n")
        plan = app.build_plan([doc], self.frames, "filename", None, True)
        self.assertEqual(len(plan.renames), 1)
        self.assertEqual(plan.references, 2)
        self.assertEqual(plan.documents[0].updated.count(b"sample_raw.tif"), 2)

    def test_prefix_uses_only_last_extension(self):
        self.frame()
        doc = self.mdoc("sample.mrc.mdoc")
        plan = app.build_plan([doc], self.frames, "filename", None, True)
        self.assertIn(b"sample.mrc_raw.tif", plan.documents[0].updated)

    def test_copy_failure_never_renames(self):
        raw = self.frame()
        doc = self.mdoc()
        before = doc.read_bytes()
        with mock.patch.object(app.shutil, "copytree", side_effect=OSError("disk full")):
            result, _ = self.run_script("--apply")
        self.assertEqual(result, 1)
        self.assertTrue(raw.exists())
        self.assertEqual(doc.read_bytes(), before)

    def test_write_failure_rolls_back_frames_and_mdocs(self):
        self.frame("one.tif")
        self.frame("two.tif")
        a = self.mdoc("a.mdoc", "one.tif")
        b = self.mdoc("b.mdoc", "two.tif")
        originals = {p: p.read_bytes() for p in (a, b)}
        real_write = app.atomic_write

        def fail_second(path, data):
            if path == b and data != originals[b]:
                raise OSError("simulated write failure")
            real_write(path, data)

        with mock.patch.object(app, "atomic_write", side_effect=fail_second):
            result, _ = self.run_script("--apply")
        self.assertEqual(result, 1)
        self.assertEqual(sorted(p.name for p in self.frames.iterdir()), ["one.tif", "two.tif"])
        for path, data in originals.items():
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(Path(str(path) + ".bak").read_bytes(), data)

    def test_no_tiff_reference_rejected(self):
        self.frame()
        self.mdoc(value="movie.eer")
        result, _ = self.run_script("--apply")
        self.assertEqual(result, 1)
        self.assertFalse(self.backup.exists())

    def test_python38_syntax_and_unix_line_endings(self):
        source = SCRIPT.read_bytes()
        ast.parse(source.decode("utf-8"), feature_version=(3, 8))
        self.assertNotIn(b"\r\n", source)

    def test_explicit_preview_leaves_no_lock(self):
        self.frame()
        self.mdoc()
        result, _ = self.run_script("--dry-run")
        self.assertEqual(result, 0)
        self.assertFalse((self.root / ".frames.align_mdoc_frames.lock").exists())
        self.assertFalse(self.backup.exists())

    def test_existing_cluster_lock_not_removed(self):
        raw = self.frame()
        self.mdoc()
        lock = self.root / ".frames.align_mdoc_frames.lock"
        lock.write_text("host=another-node\npid=123\n", encoding="utf-8")
        result, _ = self.run_script("--apply")
        self.assertEqual(result, 1)
        self.assertEqual(lock.read_text(), "host=another-node\npid=123\n")
        self.assertTrue(raw.exists())
        self.assertFalse(self.backup.exists())

    def test_insufficient_space_stops_before_backup(self):
        raw = self.frame()
        self.mdoc()
        with mock.patch.object(app.shutil, "disk_usage", return_value=mock.Mock(free=0)):
            result, _ = self.run_script("--apply")
        self.assertEqual(result, 1)
        self.assertTrue(raw.exists())
        self.assertFalse(self.backup.exists())
        self.assertFalse((self.root / ".frames.align_mdoc_frames.lock").exists())

    def test_large_file_backup_covers_multiple_chunks(self):
        data = b"01234567" * (1024 * 1024) + b"last chunk"
        self.frame(data=data)
        self.mdoc()
        result, output = self.run_script("--apply", "--progress-interval", "0.000001")
        self.assertEqual(result, 0, output)
        self.assertEqual((self.backup / "raw.tif").read_bytes(), data)
        self.assertEqual((self.frames / "sample_raw.tif").read_bytes(), data)
        self.assertIn("100.0%", output)
        self.assertFalse((self.root / ".frames.align_mdoc_frames.lock").exists())

    def test_sigterm_during_update_rolls_back_and_releases_lock(self):
        self.frame("one.tif")
        self.frame("two.tif")
        a = self.mdoc("a.mdoc", "one.tif")
        b = self.mdoc("b.mdoc", "two.tif")
        originals = {p: p.read_bytes() for p in (a, b)}
        real_write = app.atomic_write
        previous = app.signal.getsignal(app.signal.SIGTERM)

        def interrupt_second(path, data):
            if path == b and data != originals[b]:
                handler = app.signal.getsignal(app.signal.SIGTERM)
                handler(app.signal.SIGTERM, None)
            real_write(path, data)

        with mock.patch.object(app, "atomic_write", side_effect=interrupt_second):
            result, _ = self.run_script("--apply")
        self.assertEqual(result, 128 + app.signal.SIGTERM)
        self.assertEqual(sorted(p.name for p in self.frames.iterdir()), ["one.tif", "two.tif"])
        self.assertFalse((self.root / ".frames.align_mdoc_frames.lock").exists())
        self.assertEqual(app.signal.getsignal(app.signal.SIGTERM), previous)
        for path, data in originals.items():
            self.assertEqual(path.read_bytes(), data)

    def test_signal_during_backup_does_not_rename(self):
        raw = self.frame()
        doc = self.mdoc()
        original = doc.read_bytes()

        def interrupt_copy(*_):
            app.signal.getsignal(app.signal.SIGTERM)(app.signal.SIGTERM, None)

        with mock.patch.object(app.BackupProgress, "copy_file", side_effect=interrupt_copy):
            result, _ = self.run_script("--apply")
        self.assertEqual(result, 128 + app.signal.SIGTERM)
        self.assertTrue(raw.exists())
        self.assertEqual(doc.read_bytes(), original)
        self.assertFalse((self.root / ".frames.align_mdoc_frames.lock").exists())

    def test_modified_source_stops_before_renames(self):
        raw = self.frame()
        doc = self.mdoc()
        original = doc.read_bytes()

        def change_source(*args, **kwargs):
            raw.write_bytes(b"changed while copying")

        with mock.patch.object(app.BackupProgress, "report", side_effect=change_source):
            result, _ = self.run_script("--apply")
        self.assertEqual(result, 1)
        self.assertTrue(raw.exists())
        self.assertFalse((self.frames / "sample_raw.tif").exists())
        self.assertEqual(doc.read_bytes(), original)

    def test_no_backup_rename_only_does_not_copy_or_check_space(self):
        raw = self.frame()
        payload = raw.read_bytes()
        doc = self.mdoc(text="SubFramePath = missing.tif\nSubFramePath = raw.tif\n")
        original = doc.read_bytes()
        with mock.patch.object(app.shutil, "copytree") as copy, mock.patch.object(app.shutil, "disk_usage") as space:
            result, output = self.run_script("--apply", "--no-backup", "--no-update-mdoc")
        self.assertEqual(result, 0, output)
        copy.assert_not_called()
        space.assert_not_called()
        self.assertFalse(raw.exists())
        self.assertEqual((self.frames / "sample_raw.tif").read_bytes(), payload)
        self.assertEqual(doc.read_bytes(), original)
        self.assertFalse(self.backup.exists())
        self.assertFalse(Path(str(doc) + ".bak").exists())
        self.assertFalse((self.root / ".frames.align_mdoc_frames.lock").exists())
        self.assertIn("未找到文件：1 个", output)
        self.assertIn("本次成功重命名：1 个文件", output)

    def test_no_backup_updates_mdoc_without_bak(self):
        self.frame()
        doc = self.mdoc()
        original = doc.read_bytes()
        result, output = self.run_script("--apply", "--no-backup", "--path-mode", "filename")
        self.assertEqual(result, 0, output)
        self.assertEqual(doc.read_bytes(), original.replace(b"Z:\\old\\raw.tif", b"sample_raw.tif"))
        self.assertTrue((self.frames / "sample_raw.tif").exists())
        self.assertFalse(self.backup.exists())
        self.assertFalse(Path(str(doc) + ".bak").exists())

    def test_no_backup_preserves_existing_backups(self):
        self.frame()
        doc = self.mdoc()
        self.backup.mkdir()
        marker = self.backup / "raw.tif"
        marker.write_bytes(b"old frames backup")
        old_mdoc = Path(str(doc) + ".bak")
        old_mdoc.write_bytes(b"old mdoc backup")
        with mock.patch.object(app, "validate_backup_location") as validate:
            result, output = self.run_script("--apply", "--no-backup")
        self.assertEqual(result, 0, output)
        validate.assert_not_called()
        self.assertEqual(marker.read_bytes(), b"old frames backup")
        self.assertEqual(old_mdoc.read_bytes(), b"old mdoc backup")
        self.assertEqual(list(self.backup.iterdir()), [marker])

    def test_no_backup_preview_does_not_modify_files(self):
        raw = self.frame()
        doc = self.mdoc()
        original = doc.read_bytes()
        self.backup.mkdir()
        result, output = self.run_script("--dry-run", "--no-backup")
        self.assertEqual(result, 0, output)
        self.assertTrue(raw.exists())
        self.assertEqual(doc.read_bytes(), original)
        self.assertEqual(list(self.backup.iterdir()), [])
        self.assertFalse(Path(str(doc) + ".bak").exists())
        self.assertFalse((self.root / ".frames.align_mdoc_frames.lock").exists())
        self.assertIn("预计重命名：1 个文件；本次实际重命名：0 个", output)

    def test_no_backup_write_failure_rolls_back_from_memory(self):
        self.frame("one.tif")
        self.frame("two.tif")
        a = self.mdoc("a.mdoc", "one.tif")
        b = self.mdoc("b.mdoc", "two.tif")
        originals = {p: p.read_bytes() for p in (a, b)}
        real_write = app.atomic_write

        def fail_second(path, data):
            if path == b and data != originals[b]:
                raise OSError("simulated no-backup write failure")
            real_write(path, data)

        with mock.patch.object(app, "atomic_write", side_effect=fail_second):
            result, _ = self.run_script("--apply", "--no-backup")
        self.assertEqual(result, 1)
        self.assertEqual(sorted(p.name for p in self.frames.iterdir()), ["one.tif", "two.tif"])
        self.assertFalse(self.backup.exists())
        for path, data in originals.items():
            self.assertEqual(path.read_bytes(), data)
            self.assertFalse(Path(str(path) + ".bak").exists())
        self.assertFalse((self.root / ".frames.align_mdoc_frames.lock").exists())

    def test_no_backup_signal_rolls_back(self):
        raw = self.frame()
        doc = self.mdoc()
        original = doc.read_bytes()
        previous = app.signal.getsignal(app.signal.SIGTERM)

        def interrupt_write(*_):
            app.signal.getsignal(app.signal.SIGTERM)(app.signal.SIGTERM, None)

        with mock.patch.object(app, "atomic_write", side_effect=interrupt_write):
            result, _ = self.run_script("--apply", "--no-backup")
        self.assertEqual(result, 128 + app.signal.SIGTERM)
        self.assertTrue(raw.exists())
        self.assertEqual(doc.read_bytes(), original)
        self.assertFalse(self.backup.exists())
        self.assertFalse((self.root / ".frames.align_mdoc_frames.lock").exists())
        self.assertEqual(app.signal.getsignal(app.signal.SIGTERM), previous)

    def test_no_backup_keeps_collision_checks(self):
        raw = self.frame()
        occupied = self.frame("sample_raw.tif", b"keep destination")
        self.mdoc()
        result, _ = self.run_script("--apply", "--no-backup")
        self.assertEqual(result, 1)
        self.assertTrue(raw.exists())
        self.assertEqual(occupied.read_bytes(), b"keep destination")
        self.assertFalse(self.backup.exists())

    def test_no_backup_and_backup_dir_are_mutually_exclusive(self):
        raw = self.frame()
        self.mdoc()
        with self.assertRaises(SystemExit) as raised:
            self.run_script("--apply", "--no-backup", "--backup-dir", str(self.backup))
        self.assertEqual(raised.exception.code, 2)
        self.assertTrue(raw.exists())
        self.assertFalse(self.backup.exists())

    def test_backup_mdoc_without_frames_backup_preserves_original(self):
        self.frame()
        doc = self.mdoc()
        original = doc.read_bytes()
        with mock.patch.object(app.shutil, "copytree") as copy, mock.patch.object(app.shutil, "disk_usage") as space:
            result, output = self.run_script("--apply", "--no-backup", "--backup-mdoc", "--path-mode", "filename")
        self.assertEqual(result, 0, output)
        copy.assert_not_called()
        space.assert_not_called()
        self.assertEqual(Path(str(doc) + ".bak").read_bytes(), original)
        self.assertEqual(doc.read_bytes(), original.replace(b"Z:\\old\\raw.tif", b"sample_raw.tif"))
        self.assertFalse(self.backup.exists())
        self.assertTrue((self.frames / "sample_raw.tif").exists())

    def test_backup_mdoc_includes_unchanged_and_missing_only_docs(self):
        self.frame()
        a = self.mdoc("a.mdoc")
        b = self.mdoc("b.mdoc", value="missing.tif")
        originals = {doc: doc.read_bytes() for doc in (a, b)}
        result, output = self.run_script("--apply", "--no-backup", "--backup-mdoc", "--no-update-mdoc")
        self.assertEqual(result, 0, output)
        for doc, original in originals.items():
            self.assertEqual(doc.read_bytes(), original)
            self.assertEqual(Path(str(doc) + ".bak").read_bytes(), original)
        self.assertFalse(self.backup.exists())
        self.assertTrue((self.frames / "a_raw.tif").exists())

    def test_explicit_mdoc_backup_runs_even_without_data_changes(self):
        doc = self.mdoc(value="missing.tif")
        original = doc.read_bytes()
        self.backup.mkdir()
        with mock.patch.object(app.shutil, "copytree") as copy:
            result, output = self.run_script("--apply", "--backup-mdoc")
        self.assertEqual(result, 0, output)
        copy.assert_not_called()
        self.assertEqual(Path(str(doc) + ".bak").read_bytes(), original)
        self.assertEqual(doc.read_bytes(), original)
        self.assertEqual(list(self.backup.iterdir()), [])

    def test_backup_mdoc_preview_creates_no_copies(self):
        raw = self.frame()
        doc = self.mdoc()
        original = doc.read_bytes()
        result, output = self.run_script("--dry-run", "--no-backup", "--backup-mdoc")
        self.assertEqual(result, 0, output)
        self.assertTrue(raw.exists())
        self.assertEqual(doc.read_bytes(), original)
        self.assertFalse(Path(str(doc) + ".bak").exists())
        self.assertFalse(self.backup.exists())
        self.assertFalse((self.root / ".frames.align_mdoc_frames.lock").exists())

    def test_backup_mdoc_checks_all_existing_copies_before_any_changes(self):
        raw = self.frame()
        a = self.mdoc("a.mdoc")
        b = self.mdoc("b.mdoc", value="missing.tif")
        old_copy = Path(str(b) + ".bak")
        old_copy.write_bytes(b"preserve previous original")
        result, _ = self.run_script("--apply", "--no-backup", "--backup-mdoc", "--no-update-mdoc")
        self.assertEqual(result, 1)
        self.assertTrue(raw.exists())
        self.assertEqual(old_copy.read_bytes(), b"preserve previous original")
        self.assertFalse(Path(str(a) + ".bak").exists())
        self.assertFalse(self.backup.exists())

    def test_mdoc_backup_write_failure_never_starts_renaming(self):
        raw = self.frame()
        doc = self.mdoc()
        original = doc.read_bytes()
        with mock.patch.object(app, "write_mdoc_backups", side_effect=OSError("mdoc backup disk full")):
            result, _ = self.run_script("--apply", "--no-backup", "--backup-mdoc")
        self.assertEqual(result, 1)
        self.assertTrue(raw.exists())
        self.assertEqual(doc.read_bytes(), original)
        self.assertFalse(self.backup.exists())
        self.assertFalse((self.root / ".frames.align_mdoc_frames.lock").exists())

    def test_mdoc_only_backup_survives_interrupted_update(self):
        raw = self.frame()
        doc = self.mdoc()
        original = doc.read_bytes()

        def interrupt_write(*_):
            app.signal.getsignal(app.signal.SIGTERM)(app.signal.SIGTERM, None)

        with mock.patch.object(app, "atomic_write", side_effect=interrupt_write):
            result, _ = self.run_script("--apply", "--no-backup", "--backup-mdoc")
        self.assertEqual(result, 128 + app.signal.SIGTERM)
        self.assertTrue(raw.exists())
        self.assertEqual(doc.read_bytes(), original)
        self.assertEqual(Path(str(doc) + ".bak").read_bytes(), original)
        self.assertFalse(self.backup.exists())

    def test_explicit_mdoc_backup_with_default_frames_backup(self):
        raw = self.frame()
        payload = raw.read_bytes()
        doc = self.mdoc()
        original = doc.read_bytes()
        result, output = self.run_script("--apply", "--backup-mdoc")
        self.assertEqual(result, 0, output)
        self.assertEqual((self.backup / "raw.tif").read_bytes(), payload)
        self.assertEqual(Path(str(doc) + ".bak").read_bytes(), original)
        self.assertTrue((self.frames / "sample_raw.tif").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
