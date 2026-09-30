#!/usr/bin/env python3
"""按 mdoc 文件名为 TIF 帧文件加前缀；默认预览，--apply 执行，--no-backup 可禁用备份。

适用于 Linux 集群交互式计算节点。Python 3.8+，只使用标准库。
示例：python3 -u align_mdoc_frames.py --mdoc ./mdocs --frames ./frames --apply
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
    """可直接展示给用户的错误。"""


class JobInterrupted(KeyboardInterrupt):
    def __init__(self, signum):
        self.signum = signum


@contextmanager
def termination_signals():
    """将集群的终止信号转为可回退的中断；SIGKILL 无法捕获。"""
    previous = {}

    def interrupted(signum, _frame):
        # 收到一次终止请求后，为回退尽量留出调度器提供的宽限时间。
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
    """在共享目录中排他创建锁，不以本节点 PID 判断其他节点是否存活。"""
    path = frames.parent / ("." + frames.name + ".align_mdoc_frames.lock")
    try:
        stream = path.open("x", encoding="utf-8")
    except FileExistsError as error:
        raise AlignmentError(f"执行锁已存在：{path}\n请先检查锁中记录的节点和 PID；确认旧作业已结束后才可移除残留锁。") from error
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
    # 也检测失效的符号链接，避免将它当成空闲的目标路径。
    return os.path.lexists(path)


def is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def decode_mdoc(data: bytes, encoding: str | None) -> tuple[str, str]:
    if encoding:
        return data.decode(encoding), encoding
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        # 明确字节序，并保留 BOM 字符，避免写回时改变字节序。
        codec = "utf-16-le" if data.startswith(codecs.BOM_UTF16_LE) else "utf-16-be"
        return data.decode(codec), codec
    for codec in ("utf-8", "gb18030"):
        try:
            return data.decode(codec), codec
        except UnicodeDecodeError:
            pass
    raise AlignmentError("无法识别 mdoc 编码，请通过 --encoding 指定，例如 cp1252。")


def collect_mdocs(inputs: list[str]) -> list[Path]:
    result: set[Path] = set()
    for item in inputs:
        path = Path(item).expanduser().resolve()
        if path.is_dir():
            candidates = sorted(p for p in path.iterdir() if p.suffix.lower() == ".mdoc")
            if not candidates:
                raise AlignmentError(f"目录中没有 .mdoc 文件：{path}")
        else:
            candidates = [path]
        for candidate in candidates:
            if is_link(candidate) or not candidate.is_file() or candidate.suffix.lower() != ".mdoc":
                raise AlignmentError(f"不是普通 .mdoc 文件：{candidate}")
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
                raise AlignmentError(f"frames 内有符号链接或目录联接，无法按普通文件备份：{path}")
        for name in files:
            path = Path(root) / name
            if not path.is_file():
                raise AlignmentError(f"frames 内存在非普通文件：{path}")
            total_files += 1
            total_bytes += path.stat().st_size
            if path.suffix.lower() in TIFF_SUFFIXES:
                index.setdefault(name.casefold(), []).append(path)
    return index, total_files, total_bytes


def unique_match(index: dict[str, list[Path]], name: str) -> Path | None:
    candidates = index.get(name.casefold(), [])
    if len(candidates) > 1:
        listing = "\n    ".join(str(p) for p in candidates)
        raise AlignmentError(f"文件名不唯一，不能可靠匹配 {name}：\n    {listing}")
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
            # UTF-16 解码时保留的 BOM 只可能位于首行。
            bom = "\ufeff" if number == 1 and body.startswith("\ufeff") else ""
            match = FIELD.match(body[len(bom):])
            if not match:
                output.append(line)
                continue
            lead, value, trailing = match.groups()
            quote = value[0] if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0] else ""
            raw_path = value[1:-1] if quote else value
            # 不在本机解析旧路径；同时支持 Windows、UNC 和 Unix 路径。
            name = re.split(r"[/\\]", raw_path)[-1]
            if not name:
                raise AlignmentError(f"{mdoc}:{number} 的 SubFramePath 没有文件名。")
            if Path(name).suffix.lower() not in TIFF_SUFFIXES:
                skipped += 1
                output.append(line)
                continue
            # 统计 TIF 引用，即使对应文件缺失也继续处理整个 mdoc。
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
                    raise AlignmentError(f"同一帧文件被多个 mdoc 引用：{source}\n    {owners[source]}\n    {mdoc}")
                owners[source] = mdoc
                target = source if source.name.startswith(prefix) else source.with_name(prefix + source.name)
                target_key = str(target).casefold()
                if target_key in targets and targets[target_key] != source:
                    raise AlignmentError(f"多个文件会改成同一个名称：{target}")
                targets[target_key] = source
                if target != source:
                    # 即使在区分大小写的系统上，也不创建仅大小写不同的目标文件。
                    conflicts = [p for p in index.get(target.name.casefold(), []) if p.parent == target.parent]
                    if exists(target) or conflicts:
                        raise AlignmentError(f"目标文件已经存在，拒绝覆盖：{target}")
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
            raise AlignmentError(f"没有找到引用 .tif/.tiff 文件的 SubFramePath：{mdoc}")
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
    print(f"备份数据：{required / 2**30:.2f} GiB；目标文件系统可用空间：{free / 2**30:.2f} GiB（不代表个人配额）", flush=True)
    if free < required:
        raise AlignmentError("目标文件系统可用空间不足，请通过 --backup-dir 选择空间充足的目录。")


class BackupProgress:
    """顺序分块复制；内存不随单个 TIFF 大小增长。"""
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
        print(f"备份进度：{self.files_done}/{self.total_files} 文件；"
              f"{self.bytes_done / 2**30:.2f}/{self.total_bytes / 2**30:.2f} GiB "
              f"({percent:.1f}%)；平均 {self.bytes_done / 2**20 / elapsed:.1f} MiB/s", flush=True)
        self.last_report = now

    def copy_file(self, source, destination):
        source, destination = Path(source), Path(destination)
        before = source.stat()
        # copytree 通过本回调复制所有文件（包括未被引用的文件）。
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
            raise AlignmentError(f"复制时源文件发生变化，备份未完成：{source}")
        if destination.stat().st_size != before.st_size:
            raise AlignmentError(f"备份文件大小不一致：{destination}")
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
        raise AlignmentError(f"备份路径已存在，拒绝覆盖：{backup}\n可用 --backup-dir 指定一个新的备份目录。")
    if backup == frames or frames in backup.parents or backup in frames.parents:
        raise AlignmentError("备份目录与 frames 不能相同，也不能互相包含。")
    for doc in documents:
        if backup == doc.path or backup in doc.path.parents:
            raise AlignmentError("备份目录不能包含输入 mdoc。")


def select_mdoc_backups(plan: Plan, backup: Path | None, backup_mdoc: bool) -> list[Document]:
    # 显式选项备份所有输入 mdoc；默认只随 frames 备份保留将修改的 mdoc。
    if backup_mdoc:
        return plan.documents
    if backup is not None:
        return [doc for doc in plan.documents if doc.original != doc.updated]
    return []


def validate_mdoc_backups(documents: list[Document]) -> None:
    for doc in documents:
        if exists(Path(str(doc.path) + ".bak")):
            raise AlignmentError(f"mdoc 备份已存在，拒绝覆盖：{doc.path}.bak\n请先另存旧备份再重新运行。")


def write_mdoc_backups(documents: list[Document]) -> None:
    for doc in documents:
        # 排他创建，不覆盖已有的 mdoc 备份。
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
            raise AlignmentError(f"预览后 mdoc 内容发生变化，请重新运行：{doc.path}")
    if backup is not None:
        print(f"备份整个目录：{frames} -> {backup}", flush=True)
        # 启用备份时，完整复制成功后才改名；失败可能留下未完成的备份目录。
        progress = BackupProgress(plan, progress_interval)
        shutil.copytree(frames, backup, copy_function=progress.copy_file)
        progress.report(force=True)
        print("备份完成。", flush=True)
    if mdoc_backups:
        # 所有 mdoc 备份完成后才开始改名或写回引用。
        write_mdoc_backups(mdoc_backups)
        print(f"原始 mdoc 备份完成：{len(mdoc_backups)} 个（同目录 .mdoc.bak）。", flush=True)
    renamed: list[tuple[Path, Path]] = []
    rewritten: list[Document] = []
    try:
        print(f"开始重命名 {len(plan.renames)} 个文件并更新 {len(changed_docs)} 个 mdoc。", flush=True)
        for source, target in plan.renames.items():
            if exists(target):
                raise AlignmentError(f"目标文件在执行前出现，拒绝覆盖：{target}")
            source.rename(target)
            renamed.append((source, target))
        for doc in changed_docs:
            if doc.path.read_bytes() != doc.original:
                raise AlignmentError(f"mdoc 在执行期间被其他程序修改：{doc.path}")
            atomic_write(doc.path, doc.updated)
            rewritten.append(doc)
    except (Exception, KeyboardInterrupt):
        print("执行中断，正在回退已完成的修改。", file=sys.stderr)
        print("本次备份保留。" if backup is not None or mdoc_backups else "本次未创建磁盘备份。", file=sys.stderr)
        for doc in reversed(rewritten):
            try:
                atomic_write(doc.path, doc.original)
            except OSError as error:
                if mdoc_backups:
                    print(f"mdoc 回退失败，请从 .bak 恢复：{doc.path}：{error}", file=sys.stderr)
                else:
                    print(f"mdoc 回退失败，本次无磁盘备份：{doc.path}：{error}", file=sys.stderr)
        for source, target in reversed(renamed):
            try:
                if exists(source):
                    raise OSError(f"原文件名已被占用：{source}")
                target.rename(source)
            except OSError as error:
                if backup is not None:
                    print(f"改名回退失败，请从 {backup} 恢复：{error}", file=sys.stderr)
                else:
                    print(f"改名回退失败，本次未创建 frames 备份：{target} -> {source}：{error}", file=sys.stderr)
        raise


def print_summary(plan: Plan, dry_run: bool, mdoc_backup_count: int = 0) -> None:
    # 同一 mdoc 多次引用同一个缺失文件，只计为一个文件，同时报告引用次数。
    missing_files = len({(item.mdoc, item.filename.casefold()) for item in plan.missing})
    changed_docs = sum(doc.original != doc.updated for doc in plan.documents)
    print("\n处理汇总（预览）：" if dry_run else "\n处理汇总（执行）：")
    print(f"  未找到文件：{missing_files} 个（{len(plan.missing)} 条引用；按 mdoc + 文件名去重）")
    print(f"  成功匹配引用：{plan.references} 条")
    if dry_run:
        print(f"  预计重命名：{len(plan.renames)} 个文件；本次实际重命名：0 个")
        print(f"  预计更新 mdoc：{changed_docs} 个")
        print(f"  预计备份原始 mdoc：{mdoc_backup_count} 个")
    else:
        # 仅在执行完成或确认无需修改后调用，不把失败/回退计为成功。
        print(f"  本次成功重命名：{len(plan.renames)} 个文件")
        print(f"  本次更新 mdoc：{changed_docs} 个")
        print(f"  本次备份原始 mdoc：{mdoc_backup_count} 个")
    print(f"  已有前缀、无需重命名：{plan.already_prefixed} 个文件")
    if plan.missing:
        print("  未找到的引用已跳过，对应 mdoc 行保留原文。")


def run(args, frames: Path) -> int:
    with execution_lock(frames) if args.apply else nullcontext():
        backup = None
        backup_occupied = False
        if not args.no_backup:
            backup = Path(args.backup_dir).expanduser().absolute() if args.backup_dir else frames.parent / "frames_backup"
            # 不解析已有的符号链接，仍把它视作已占用的备份路径。
            backup_occupied = exists(backup)
            backup = backup.resolve()
        print(f"运行节点：{socket.gethostname()}；PID：{os.getpid()}；模式：{'执行' if args.apply else '预览'}", flush=True)
        if args.no_backup and args.backup_mdoc:
            print("frames 备份已禁用；将独立备份所选原始 mdoc。", flush=True)
        elif args.no_backup:
            print("备份已禁用：不创建 frames 副本或 .mdoc.bak；已有备份保持不变。", flush=True)
        print(f"正在扫描 mdoc 和 frames：{frames}", flush=True)
        plan = build_plan(collect_mdocs(args.mdoc), frames, args.path_mode, args.encoding, not args.no_update_mdoc)
        changed_docs = [d for d in plan.documents if d.original != d.updated]
        has_changes = bool(plan.renames or changed_docs)
        if not has_changes:
            backup = None
        mdoc_backups = select_mdoc_backups(plan, backup, args.backup_mdoc)
        print(f"mdoc：{len(plan.documents)}；TIF 引用：{plan.references + len(plan.missing)}；待改名：{len(plan.renames)}；待更新 mdoc：{len(changed_docs)}")
        print(f"未被引用的 TIF 保留：{plan.unmatched}；跳过非 TIF 引用：{plan.skipped}")
        shown_renames = len(plan.renames) if args.verbose else min(20, len(plan.renames))
        for source, target in islice(plan.renames.items(), shown_renames):
            print(f"  {source.relative_to(frames)} -> {target.relative_to(frames)}")
        shown_docs = len(changed_docs) if args.verbose else min(20, len(changed_docs))
        for doc in islice(changed_docs, shown_docs):
            if mdoc_backups:
                print(f"  更新引用：{doc.path}（原文备份到 {doc.path}.bak）")
            else:
                print(f"  更新引用：{doc.path}（不创建 mdoc 备份）")
        shown_missing = len(plan.missing) if args.verbose else min(20, len(plan.missing))
        for item in islice(plan.missing, shown_missing):
            print(f"  未找到，跳过：{item.mdoc}:{item.line_number} -> {item.filename}")
        if shown_renames < len(plan.renames) or shown_docs < len(changed_docs) or shown_missing < len(plan.missing):
            print("各类明细最多展示前 20 条；加 --verbose 可查看全部。所有引用均已纳入检查。")
        if mdoc_backups:
            print(f"将备份原始 mdoc：{len(mdoc_backups)} 个，保存为各文件同目录下的 .mdoc.bak。")
        if not has_changes and not mdoc_backups:
            print("没有需要执行的改名或引用更新，无需新增备份。")
            print_summary(plan, dry_run=not args.apply)
            return 0
        validate_mdoc_backups(mdoc_backups)
        if backup is not None:
            if backup_occupied:
                raise AlignmentError(f"备份路径已经存在，请用 --backup-dir 指定新目录：{backup}")
            validate_backup_location(backup, frames, plan.documents)
            print(f"备份位置：{backup}")
            check_backup_space(backup, plan.total_bytes)
        if not args.apply:
            print("当前为预览：未创建备份、未修改任何文件。确认后加 --apply 执行。")
            print_summary(plan, dry_run=True, mdoc_backup_count=len(mdoc_backups))
            return 0
        apply_plan(plan, frames, backup, args.progress_interval, backup_mdoc=args.backup_mdoc)
        print("完成：已处理找到的帧文件及所选 mdoc 引用更新。")
        print_summary(plan, dry_run=False, mdoc_backup_count=len(mdoc_backups))
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mdoc", required=True, nargs="+", help="一个或多个 mdoc 文件/目录；目录仅扫描第一层")
    parser.add_argument("--frames", required=True, help="原始 frames 目录；递归搜索 .tif 和 .tiff")
    backup_options = parser.add_mutually_exclusive_group()
    backup_options.add_argument("--backup-dir", help="备份目录；默认是 frames 同级的 frames_backup")
    backup_options.add_argument("--no-backup", action="store_true", help="关闭默认备份；可用 --backup-mdoc 单独启用 mdoc 备份；不能与 --backup-dir 同时使用")
    parser.add_argument("--backup-mdoc", action="store_true", help="备份所有输入 mdoc 的原文为 .mdoc.bak，即使不更新 mdoc；可与 --no-backup 同时使用")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="完成检查后执行，默认先备份；不加此参数仅预览")
    mode.add_argument("--dry-run", action="store_true", help="显式指定预览模式，也是默认模式")
    parser.add_argument("--path-mode", choices=("absolute", "relative", "filename"), default="absolute",
                        help="写回 mdoc 的路径：absolute 默认绝对路径；relative 相对 mdoc；filename 仅文件名")
    parser.add_argument("--no-update-mdoc", action="store_true", help="只重命名帧文件，不更新 mdoc 引用；备份行为由备份选项决定")
    parser.add_argument("--encoding", help="指定 mdoc 原编码；默认自动识别 UTF-8、带 BOM 的 UTF-16、GB18030")
    parser.add_argument("--progress-interval", type=float, default=10.0, help="备份进度输出间隔，单位秒，默认 10")
    parser.add_argument("--verbose", action="store_true", help="显示所有改名、mdoc 更新及缺失引用明细，默认各显示前 20 条")
    args = parser.parse_args(argv)
    if not 0 < args.progress_interval < float("inf"):
        parser.error("--progress-interval 必须是有限的正数")
    try:
        frames = Path(args.frames).expanduser().resolve()
        if not frames.is_dir():
            raise AlignmentError(f"frames 目录不存在：{frames}")
        with termination_signals():
            return run(args, frames)
    except (AlignmentError, OSError, UnicodeError, LookupError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    except JobInterrupted as error:
        print(f"收到终止信号 {error.signum}；请检查上述执行及回退信息。", file=sys.stderr)
        return 128 + error.signum
    except KeyboardInterrupt:
        print("操作已取消；请检查上述执行及回退信息。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    # 日志经 tee 或重定向时仍及时输出；避免集群 C locale 下中文输出失败。
    for console in (sys.stdout, sys.stderr):
        if hasattr(console, "reconfigure"):
            console.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)
    sys.exit(main())
