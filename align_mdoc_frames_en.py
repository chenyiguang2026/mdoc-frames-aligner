#!/usr/bin/env python3
"""Prefix TIFF frame filenames with their mdoc name. Preview by default; --apply executes, and --no-backup disables backups.

For interactive Linux cluster nodes. Requires Python 3.8+ and the standard library only.
Example: python3 -u align_mdoc_frames_en.py --mdoc ./mdocs --frames ./frames --apply
"""

from __future__ import annotations

import argparse
import codecs
from contextlib import contextmanager, nullcontext
from itertools import islice
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sys
import tempfile
import time
from dataclasses import dataclass


TIFF_SUFFIXES = {".tif", ".tiff"}
FIELD = re.compile(r"^([ \t]*SubFramePath[ \t]*=[ \t]*)(.*?)([ \t]*)$", re.I)


class AlignmentError(Exception):
    """An error message suitable for display to the user."""


class JobInterrupted(KeyboardInterrupt):
    def __init__(self, signum):
        self.signum = signum


@contextmanager
def termination_signals():
    """Turn termination signals into exceptions for rollback; SIGKILL cannot be caught."""
    previous = {}

    def interrupted(signum, _frame):
        # Ignore further termination requests to use the scheduler's grace period for rollback.
        for number in previous:
            signal.signal(number, signal.SIG_IGN)
        raise JobInterrupted(signum)

    try:
        for name in ("SIGTERM", "SIGHUP"):
            number = getattr(signal, name, None)
            if number is not None:
                previous[number] = signal.signal(number, interrupted)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


@contextmanager
def execution_lock(frames):
    """Create an exclusive shared lock; local PIDs cannot identify live jobs on other nodes."""
    path = frames.parent / ("." + frames.name + ".align_mdoc_frames.lock")
    try:
        stream = path.open("x", encoding="utf-8")
    except FileExistsError as error:
        raise AlignmentError(f"Execution lock already exists: {path}\nCheck the recorded host and PID. Remove a stale lock only after confirming that the previous job has stopped.") from error
    try:
        with stream:
            stream.write(f"host={socket.gethostname()}\npid={os.getpid()}\nstarted={time.strftime('%Y-%m-%d %H:%M:%S')}\nframes={frames}\n")
        yield
    finally:
        path.unlink()


@dataclass
class Document:
    path: Path
    original: bytes
    updated: bytes


@dataclass
class MissingReference:
    mdoc: Path
    line_number: int
    filename: str


@dataclass
class Plan:
    documents: list[Document]
    renames: dict[Path, Path]
    references: int
    skipped: int
    unmatched: int
    total_files: int
    total_bytes: int
    missing: list[MissingReference]
    already_prefixed: int


def exists(path: Path) -> bool:
    # Detect broken symlinks too, so they are not treated as available destinations.
    return os.path.lexists(path)


def is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def decode_mdoc(data: bytes, encoding: str | None) -> tuple[str, str]:
    if encoding:
        return data.decode(encoding), encoding
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        # Preserve the byte order and BOM when writing UTF-16 back to disk.
        codec = "utf-16-le" if data.startswith(codecs.BOM_UTF16_LE) else "utf-16-be"
        return data.decode(codec), codec
    for codec in ("utf-8", "gb18030"):
        try:
            return data.decode(codec), codec
        except UnicodeDecodeError:
            pass
    raise AlignmentError("Cannot detect the mdoc encoding. Specify --encoding, for example cp1252.")


def collect_mdocs(inputs: list[str]) -> list[Path]:
    result: set[Path] = set()
    for item in inputs:
        path = Path(item).expanduser().resolve()
        if path.is_dir():
            candidates = sorted(p for p in path.iterdir() if p.suffix.lower() == ".mdoc")
            if not candidates:
                raise AlignmentError(f"No .mdoc files found in directory: {path}")
        else:
            candidates = [path]
        for candidate in candidates:
            if is_link(candidate) or not candidate.is_file() or candidate.suffix.lower() != ".mdoc":
                raise AlignmentError(f"Not a regular .mdoc file: {candidate}")
            result.add(candidate.resolve())
    return sorted(result)


def frame_index(frames: Path) -> tuple[dict[str, list[Path]], int, int]:
    index: dict[str, list[Path]] = {}
    total_files = total_bytes = 0

    def walk_error(error: OSError) -> None:
        raise error

    for root, dirs, files in os.walk(frames, onerror=walk_error, followlinks=False):
        for name in dirs + files:
            path = Path(root) / name
            if is_link(path):
                raise AlignmentError(f"A symlink or junction inside frames cannot be backed up as a regular file: {path}")
        for name in files:
            path = Path(root) / name
            if not path.is_file():
                raise AlignmentError(f"Non-regular file found inside frames: {path}")
            total_files += 1
            total_bytes += path.stat().st_size
            if path.suffix.lower() in TIFF_SUFFIXES:
                index.setdefault(name.casefold(), []).append(path)
    return index, total_files, total_bytes


def unique_match(index: dict[str, list[Path]], name: str) -> Path | None:
    candidates = index.get(name.casefold(), [])
    if len(candidates) > 1:
        listing = "\n    ".join(str(p) for p in candidates)
        raise AlignmentError(f"Ambiguous filename; cannot safely match {name}:\n    {listing}")
    return candidates[0] if candidates else None


def reference_text(target: Path, mdoc: Path, mode: str) -> str:
    if mode == "filename":
        return target.name
    if mode == "relative":
        return Path(os.path.relpath(target, mdoc.parent)).as_posix()
    return str(target)


def build_plan(mdocs: list[Path], frames: Path, mode: str, encoding: str | None,
               update_mdoc: bool) -> Plan:
    index, total_files, total_bytes = frame_index(frames)
    documents: list[Document] = []
    renames: dict[Path, Path] = {}
    owners: dict[Path, Path] = {}
    targets: dict[str, Path] = {}
    missing: list[MissingReference] = []
    references = skipped = 0

    for mdoc in mdocs:
        original = mdoc.read_bytes()
        text, codec = decode_mdoc(original, encoding)
        output: list[str] = []
        prefix = mdoc.stem + "_"
        document_refs = 0
        for number, line in enumerate(text.splitlines(keepends=True), start=1):
            body = line.rstrip("\r\n")
            newline = line[len(body):]
            # A BOM preserved during UTF-16 decoding can only occur at the start of the first line.
            bom = "\ufeff" if number == 1 and body.startswith("\ufeff") else ""
            match = FIELD.match(body[len(bom):])
            if not match:
                output.append(line)
                continue
            lead, value, trailing = match.groups()
            quote = value[0] if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0] else ""
            raw_path = value[1:-1] if quote else value
            # Extract the basename without resolving the old Windows, UNC, or Unix path locally.
            name = re.split(r"[/\\]", raw_path)[-1]
            if not name:
                raise AlignmentError(f"{mdoc}:{number}: SubFramePath has no filename.")
            if Path(name).suffix.lower() not in TIFF_SUFFIXES:
                skipped += 1
                output.append(line)
                continue
            # Count TIFF references and continue processing the mdoc even when a file is missing.
            document_refs += 1
            try:
                source = unique_match(index, name)
                if source is None and not name.startswith(prefix):
                    source = unique_match(index, prefix + name)
                if source is None:
                    missing.append(MissingReference(mdoc, number, name))
                    output.append(line)
                    continue
                if source in owners and owners[source] != mdoc:
                    raise AlignmentError(f"The same frame file is referenced by multiple mdocs: {source}\n    {owners[source]}\n    {mdoc}")
                owners[source] = mdoc
                target = source if source.name.startswith(prefix) else source.with_name(prefix + source.name)
                target_key = str(target).casefold()
                if target_key in targets and targets[target_key] != source:
                    raise AlignmentError(f"Multiple files would receive the same destination name: {target}")
                targets[target_key] = source
                if target != source:
                    # Reject case-only destination collisions even on case-sensitive filesystems.
                    conflicts = [p for p in index.get(target.name.casefold(), []) if p.parent == target.parent]
                    if exists(target) or conflicts:
                        raise AlignmentError(f"Destination already exists; refusing to overwrite: {target}")
                    renames[source] = target
                references += 1
                if update_mdoc:
                    value = quote + reference_text(target, mdoc, mode) + quote
                    output.append(bom + lead + value + trailing + newline)
                else:
                    output.append(line)
            except AlignmentError as error:
                raise AlignmentError(f"{mdoc}:{number}\n{error}") from error
        if document_refs == 0:
            raise AlignmentError(f"No SubFramePath entries referencing .tif/.tiff files found in: {mdoc}")
        updated_text = "".join(output)
        updated = original if updated_text == text else updated_text.encode(codec)
        documents.append(Document(mdoc, original, updated))
    return Plan(documents, renames, references, skipped,
                sum(len(paths) for paths in index.values()) - len(owners),
                total_files, total_bytes, missing, len(owners) - len(renames))


def check_backup_space(backup: Path, required: int) -> None:
    parent = backup.parent
    while not parent.exists():
        parent = parent.parent
    free = shutil.disk_usage(parent).free
    print(f"Backup data: {required / 2**30:.2f} GiB; free space on destination filesystem: {free / 2**30:.2f} GiB (does not reflect user quotas)", flush=True)
    if free < required:
        raise AlignmentError("Insufficient free space on the destination filesystem. Choose another location with --backup-dir.")


class BackupProgress:
    """Copy sequentially in chunks without loading an entire TIFF into memory."""
    def __init__(self, plan: Plan, interval: float):
        self.total_bytes = plan.total_bytes
        self.total_files = plan.total_files
        self.bytes_done = self.files_done = 0
        self.interval = interval
        self.started = self.last_report = time.monotonic()

    def report(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_report < self.interval:
            return
        elapsed = max(now - self.started, 0.001)
        percent = 100 * self.bytes_done / max(self.total_bytes, 1)
        print(f"Backup progress: {self.files_done}/{self.total_files} files; "
              f"{self.bytes_done / 2**30:.2f}/{self.total_bytes / 2**30:.2f} GiB "
              f"({percent:.1f}%); average {self.bytes_done / 2**20 / elapsed:.1f} MiB/s", flush=True)
        self.last_report = now

    def copy_file(self, source, destination):
        source, destination = Path(source), Path(destination)
        before = source.stat()
        # copytree calls this function for every file, including unreferenced files.
        with source.open("rb") as reader, destination.open("xb") as writer:
            buffer = bytearray(8 * 1024 * 1024)
            view = memoryview(buffer)
            while True:
                count = reader.readinto(buffer)
                if not count:
                    break
                writer.write(view[:count])
                self.bytes_done += count
                self.report()
            writer.flush()
            os.fsync(writer.fileno())
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise AlignmentError(f"Source changed during copying; backup is incomplete: {source}")
        if destination.stat().st_size != before.st_size:
            raise AlignmentError(f"Backup file size mismatch: {destination}")
        shutil.copystat(source, destination)
        self.files_done += 1
        self.report()
        return str(destination)


def atomic_write(path: Path, data: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def validate_backup_location(backup: Path, frames: Path, documents: list[Document]) -> None:
    if exists(backup):
        raise AlignmentError(f"Backup path already exists; refusing to overwrite: {backup}\nUse --backup-dir to specify a new backup directory.")
    if backup == frames or frames in backup.parents or backup in frames.parents:
        raise AlignmentError("Backup and frames directories must be distinct and must not contain each other.")
    for doc in documents:
        if backup == doc.path or backup in doc.path.parents:
            raise AlignmentError("The backup directory must not contain any input mdoc.")


def select_mdoc_backups(plan: Plan, backup: Path | None, backup_mdoc: bool) -> list[Document]:
    # Explicitly back up all input mdocs; by default, back up changed mdocs with frames only.
    if backup_mdoc:
        return plan.documents
    if backup is not None:
        return [doc for doc in plan.documents if doc.original != doc.updated]
    return []


def validate_mdoc_backups(documents: list[Document]) -> None:
    for doc in documents:
        if exists(Path(str(doc.path) + ".bak")):
            raise AlignmentError(f"mdoc backup already exists; refusing to overwrite: {doc.path}.bak\nPreserve the previous backup elsewhere before running again.")


def write_mdoc_backups(documents: list[Document]) -> None:
    for doc in documents:
        # Create exclusively to avoid overwriting an existing mdoc backup.
        with Path(str(doc.path) + ".bak").open("xb") as stream:
            stream.write(doc.original)
            stream.flush()
            os.fsync(stream.fileno())


def apply_plan(plan: Plan, frames: Path, backup: Path | None, progress_interval: float = 10.0,
               backup_mdoc: bool = False) -> None:
    mdoc_backups = select_mdoc_backups(plan, backup, backup_mdoc)
    if backup is not None:
        validate_backup_location(backup, frames, plan.documents)
    validate_mdoc_backups(mdoc_backups)
    changed_docs = [d for d in plan.documents if d.original != d.updated]
    for doc in plan.documents:
        if doc.path.read_bytes() != doc.original:
            raise AlignmentError(f"mdoc changed after planning. Run the script again: {doc.path}")
    if backup is not None:
        print(f"Backing up the entire directory: {frames} -> {backup}", flush=True)
        # When backups are enabled, rename only after a full copy; failure may leave an incomplete backup.
        progress = BackupProgress(plan, progress_interval)
        shutil.copytree(frames, backup, copy_function=progress.copy_file)
        progress.report(force=True)
        print("Backup complete.", flush=True)
    if mdoc_backups:
        # Complete all mdoc backups before renaming frames or updating references.
        write_mdoc_backups(mdoc_backups)
        print(f"Original mdocs backed up: {len(mdoc_backups)} (adjacent .mdoc.bak files).", flush=True)
    renamed: list[tuple[Path, Path]] = []
    rewritten: list[Document] = []
    try:
        print(f"Starting changes. Files to rename: {len(plan.renames)}; mdocs to update: {len(changed_docs)}.", flush=True)
        for source, target in plan.renames.items():
            if exists(target):
                raise AlignmentError(f"Destination appeared before execution; refusing to overwrite: {target}")
            source.rename(target)
            renamed.append((source, target))
        for doc in changed_docs:
            if doc.path.read_bytes() != doc.original:
                raise AlignmentError(f"mdoc was modified by another process during execution: {doc.path}")
            atomic_write(doc.path, doc.updated)
            rewritten.append(doc)
    except (Exception, KeyboardInterrupt):
        print("Execution interrupted. Rolling back completed changes.", file=sys.stderr)
        print("Backups from this run will be retained." if backup is not None or mdoc_backups else "No on-disk backups were created during this run.", file=sys.stderr)
        for doc in reversed(rewritten):
            try:
                atomic_write(doc.path, doc.original)
            except OSError as error:
                if mdoc_backups:
                    print(f"mdoc rollback failed; restore from .bak: {doc.path}: {error}", file=sys.stderr)
                else:
                    print(f"mdoc rollback failed; no on-disk backup was created: {doc.path}: {error}", file=sys.stderr)
        for source, target in reversed(renamed):
            try:
                if exists(source):
                    raise OSError(f"Original filename is now occupied: {source}")
                target.rename(source)
            except OSError as error:
                if backup is not None:
                    print(f"Rename rollback failed; restore from {backup}: {error}", file=sys.stderr)
                else:
                    print(f"Rename rollback failed; no frames backup was created: {target} -> {source}: {error}", file=sys.stderr)
        raise


def print_summary(plan: Plan, dry_run: bool, mdoc_backup_count: int = 0) -> None:
    # Deduplicate missing files within each mdoc and also report the number of missing references.
    missing_files = len({(item.mdoc, item.filename.casefold()) for item in plan.missing})
    changed_docs = sum(doc.original != doc.updated for doc in plan.documents)
    print("\nSummary (dry run):" if dry_run else "\nSummary (apply):")
    print(f"  Missing files: {missing_files} (references: {len(plan.missing)}; deduplicated by mdoc + filename)")
    print(f"  Matched references: {plan.references}")
    if dry_run:
        print(f"  Planned renames: {len(plan.renames)}; actually renamed this run: 0")
        print(f"  Planned mdoc updates: {changed_docs}")
        print(f"  Planned original mdoc backups: {mdoc_backup_count}")
    else:
        # Called only after success or a confirmed no-op; failed or rolled-back changes are not counted.
        print(f"  Successfully renamed this run: {len(plan.renames)}")
        print(f"  mdocs updated this run: {changed_docs}")
        print(f"  Original mdocs backed up this run: {mdoc_backup_count}")
    print(f"  Already prefixed; no rename needed: {plan.already_prefixed}")
    if plan.missing:
        print("  Missing references were skipped; their mdoc lines were left unchanged.")


def run(args, frames: Path) -> int:
    with execution_lock(frames) if args.apply else nullcontext():
        backup = None
        backup_occupied = False
        if not args.no_backup:
            backup = Path(args.backup_dir).expanduser().absolute() if args.backup_dir else frames.parent / "frames_backup"
            # Treat an existing symlink as an occupied backup path before resolving it.
            backup_occupied = exists(backup)
            backup = backup.resolve()
        print(f"Host: {socket.gethostname()}; PID: {os.getpid()}; mode: {'apply' if args.apply else 'dry-run'}", flush=True)
        if args.no_backup and args.backup_mdoc:
            print("Frames backup disabled; original input mdocs will be backed up separately.", flush=True)
        elif args.no_backup:
            print("Backups disabled: no frames copy or .mdoc.bak will be created; existing backups remain unchanged.", flush=True)
        print(f"Scanning mdocs and frames: {frames}", flush=True)
        plan = build_plan(collect_mdocs(args.mdoc), frames, args.path_mode, args.encoding, not args.no_update_mdoc)
        changed_docs = [d for d in plan.documents if d.original != d.updated]
        has_changes = bool(plan.renames or changed_docs)
        if not has_changes:
            backup = None
        mdoc_backups = select_mdoc_backups(plan, backup, args.backup_mdoc)
        print(f"mdocs: {len(plan.documents)}; TIFF references: {plan.references + len(plan.missing)}; planned renames: {len(plan.renames)}; planned mdoc updates: {len(changed_docs)}")
        print(f"Unreferenced TIFFs left unchanged: {plan.unmatched}; non-TIFF references skipped: {plan.skipped}")
        shown_renames = len(plan.renames) if args.verbose else min(20, len(plan.renames))
        for source, target in islice(plan.renames.items(), shown_renames):
            print(f"  {source.relative_to(frames)} -> {target.relative_to(frames)}")
        shown_docs = len(changed_docs) if args.verbose else min(20, len(changed_docs))
        for doc in islice(changed_docs, shown_docs):
            if mdoc_backups:
                print(f"  Update references: {doc.path} (original saved to {doc.path}.bak)")
            else:
                print(f"  Update references: {doc.path} (no mdoc backup will be created)")
        shown_missing = len(plan.missing) if args.verbose else min(20, len(plan.missing))
        for item in islice(plan.missing, shown_missing):
            print(f"  Missing, skipped: {item.mdoc}:{item.line_number} -> {item.filename}")
        if shown_renames < len(plan.renames) or shown_docs < len(changed_docs) or shown_missing < len(plan.missing):
            print("Showing up to 20 entries per category. Use --verbose for all entries. All references have been checked.")
        if mdoc_backups:
            print(f"Original mdocs to back up: {len(mdoc_backups)}; each copy will be saved as an adjacent .mdoc.bak file.")
        if not has_changes and not mdoc_backups:
            print("No renames or reference updates are needed; no new backup will be created.")
            print_summary(plan, dry_run=not args.apply)
            return 0
        validate_mdoc_backups(mdoc_backups)
        if backup is not None:
            if backup_occupied:
                raise AlignmentError(f"Backup path already exists. Use --backup-dir to specify a new directory: {backup}")
            validate_backup_location(backup, frames, plan.documents)
            print(f"Backup location: {backup}")
            check_backup_space(backup, plan.total_bytes)
        if not args.apply:
            print("Dry run: no backups created and no files modified. Add --apply to execute the plan.")
            print_summary(plan, dry_run=True, mdoc_backup_count=len(mdoc_backups))
            return 0
        apply_plan(plan, frames, backup, args.progress_interval, backup_mdoc=args.backup_mdoc)
        print("Done: matched frame files and the selected mdoc reference updates have been processed.")
        print_summary(plan, dry_run=False, mdoc_backup_count=len(mdoc_backups))
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mdoc", required=True, nargs="+", help="one or more mdoc files/directories; directories are scanned non-recursively")
    parser.add_argument("--frames", required=True, help="source frames directory; recursively search for .tif and .tiff files")
    backup_options = parser.add_mutually_exclusive_group()
    backup_options.add_argument("--backup-dir", help="backup directory; defaults to frames_backup beside the frames directory")
    backup_options.add_argument("--no-backup", action="store_true", help="disable default backups; --backup-mdoc can enable mdoc backups separately; incompatible with --backup-dir")
    parser.add_argument("--backup-mdoc", action="store_true", help="back up all original input mdocs as .mdoc.bak, even without mdoc updates; compatible with --no-backup")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="apply after validation, with backups by default; otherwise only preview")
    mode.add_argument("--dry-run", action="store_true", help="explicitly select dry-run mode (the default)")
    parser.add_argument("--path-mode", choices=("absolute", "relative", "filename"), default="absolute",
                        help="mdoc path format: absolute (default), relative to the mdoc directory, or filename only")
    parser.add_argument("--no-update-mdoc", action="store_true", help="rename frames without updating mdocs; backup behavior is controlled separately")
    parser.add_argument("--encoding", help="original mdoc encoding; autodetect UTF-8, UTF-16 with BOM, or GB18030 by default")
    parser.add_argument("--progress-interval", type=float, default=10.0, help="backup progress reporting interval in seconds (default: 10)")
    parser.add_argument("--verbose", action="store_true", help="show all renames, mdoc updates, and missing references (default: first 20 per category)")
    args = parser.parse_args(argv)
    if not 0 < args.progress_interval < float("inf"):
        parser.error("--progress-interval must be a finite positive number")
    try:
        frames = Path(args.frames).expanduser().resolve()
        if not frames.is_dir():
            raise AlignmentError(f"Frames directory does not exist: {frames}")
        with termination_signals():
            return run(args, frames)
    except (AlignmentError, OSError, UnicodeError, LookupError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except JobInterrupted as error:
        print(f"Received termination signal {error.signum}. Check the execution and rollback messages above.", file=sys.stderr)
        return 128 + error.signum
    except KeyboardInterrupt:
        print("Operation cancelled. Check the execution and rollback messages above.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    # Flush redirected logs promptly and handle Unicode paths even under a cluster C locale.
    for console in (sys.stdout, sys.stderr):
        if hasattr(console, "reconfigure"):
            console.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)
    sys.exit(main())
