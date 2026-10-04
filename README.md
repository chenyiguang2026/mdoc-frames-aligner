# mdoc Frames Aligner

English | [简体中文](README.zh-CN.md)

Match SerialEM `.mdoc` references to TIFF frame files and prefix each matched frame filename with the name of its mdoc. Backups are enabled by default and can be disabled with `--no-backup`. Designed for direct use in an interactive Linux cluster session.

The script aligns **filenames and metadata references**. It does not align images, perform motion correction, or modify TIFF pixel data.

## Scripts and requirements

| Script | Comments, help, and console messages |
| --- | --- |
| [`align_mdoc_frames_en.py`](align_mdoc_frames_en.py) | English |
| [`align_mdoc_frames.py`](align_mdoc_frames.py) | Chinese |

Both scripts are standalone and have the same options and behavior. Choose one; you do not need to run both.

- Python **3.8 or later**; standard library only.
- One process; no GPU required.
- Read access to input data and write access to the frames, mdoc, backup, and lock locations.
- When backups are enabled, a full backup requires space for the complete frames directory, plus filesystem metadata and mdoc backups.

No package installation is required. If your cluster's default Python is too old, load a suitable Python module or activate an existing environment using your cluster's instructions.

## Quick start on an interactive node

**Recommended: back up all original files before running the script**, including every mdoc and the entire frames directory (all files and subdirectories). Keep the backups until you have verified the renamed files and downstream processing results. Retain the default frames backup and add `--backup-mdoc` to also back up every selected original mdoc; use `--no-backup` only when you already have a complete, verified backup elsewhere.

After obtaining a compute node, upload the script and run it from your terminal. Replace the example paths with your actual data paths.

```bash
python3 --version

# Preview: inspect the plan without modifying files or creating backups.
python3 -u align_mdoc_frames_en.py \
  --mdoc /shared/project/data/mdocs \
  --frames /shared/project/data/frames \
  --dry-run

# Apply: complete the backup, rename matched files, and update their mdocs.
python3 -u align_mdoc_frames_en.py \
  --mdoc /shared/project/data/mdocs \
  --frames /shared/project/data/frames \
  --apply
```

If the script, `mdocs`, and `frames` are in your current directory:

```bash
python3 -u align_mdoc_frames_en.py --mdoc ./mdocs --frames ./frames --apply
```

`./frames` means the frames folder in the current directory. `/frames` means a folder at the filesystem root. Use `pwd` and `ls` to check your location.

`--mdoc` accepts one or more files or directories. Mdoc directories are scanned only at their top level; the frames directory is always searched recursively.

```bash
python3 -u align_mdoc_frames_en.py \
  --mdoc ./mdocs/sample01.mdoc ./mdocs/sample02.mdoc \
  --frames ./frames \
  --apply
```

If `frames_backup` already exists, choose a new, unused backup directory:

```bash
python3 -u align_mdoc_frames_en.py \
  --mdoc ./mdocs \
  --frames ./frames \
  --backup-dir ./frames_backup_rename_v2 \
  --apply
```

## Choose backup and mdoc behavior

| Options added to `--apply` | Frame and mdoc backups | Update mdoc references |
| --- | --- | --- |
| None | Yes | Yes |
| `--no-backup` | No | Yes |
| `--no-update-mdoc` | Frames backed up; unchanged mdocs need no backup | No |
| `--no-backup --no-update-mdoc` | No | No |
| `--no-backup --backup-mdoc` | All input mdocs only | Yes |
| `--no-backup --backup-mdoc --no-update-mdoc` | All input mdocs only | No |

To **only prefix frame filenames, without any backups or mdoc changes**:

```bash
python3 -u align_mdoc_frames_en.py \
  --mdoc ./mdocs \
  --frames ./frames \
  --no-backup \
  --no-update-mdoc \
  --apply
```

To rename frames and update mdoc references without creating backups, omit `--no-update-mdoc`. To preview either mode, replace `--apply` with `--dry-run`.

`--no-backup` skips frames copying and the default mdoc backups. Without `--backup-mdoc`, it also skips backup space/destination checks and leaves existing backups untouched. It cannot be combined with `--backup-dir`. Missing-file handling, collision checks, execution locks, and attempted rollback remain active. Without either type of backup, no new on-disk recovery copy is created; if you keep mdocs unchanged, their references will retain the old filenames.

To **back up original mdocs without copying frames**, use:

```bash
python3 -u align_mdoc_frames_en.py \
  --mdoc ./mdocs \
  --frames ./frames \
  --no-backup \
  --backup-mdoc \
  --apply
```

`--backup-mdoc` independently saves **all selected input mdocs** before any renaming or metadata updates: `sample01.mdoc` becomes an additional `sample01.mdoc.bak` in the same directory. It also works with `--no-update-mdoc`, with missing-only mdocs, or when no data changes are needed. In the last case, only mdocs are backed up; no frames copy is made. Existing `.mdoc.bak` files are never overwritten: preserve them elsewhere before another explicit backup. Preview mode reports planned backups without creating them.

## What changes

For `sample01.mdoc` containing:

```text
[ZValue = 0]
SubFramePath = Z:\old_computer\acquisition\image001.tif
TiltAngle = -60
```

The old directory is ignored. The script searches the supplied frames directory by the basename `image001.tif`, including subdirectories, and renames a unique match to `sample01_image001.tif`.

```text
data/
├── mdocs/
│   ├── sample01.mdoc       # Reference updated to the new frame path
│   └── sample01.mdoc.bak   # Original mdoc content
├── frames/
│   └── sample01_image001.tif
└── frames_backup/
    └── image001.tif       # Copy with the original filename
```

- Only `.tif` and `.tiff` references are processed. Windows, UNC, and Unix paths are supported in the original mdoc.
- Matching is case-insensitive. Ambiguous basenames are rejected even if their old directories differ.
- The prefix is the mdoc name without its final `.mdoc`: `sample01.mrc.mdoc` produces `sample01.mrc_`.
- Frame files stay in their original subdirectories. Files without a matching mdoc reference keep their original names and are included in the full backup.
- By default, matched `SubFramePath` values are updated to the new absolute paths on the current node. Other fields, section order, encoding, and line endings are retained.
- Mdoc filenames themselves do not change. By default, each changed mdoc gets an adjacent `.mdoc.bak` file. `--no-backup` disables default backups; `--backup-mdoc` independently requests backups of all input mdocs.

## Missing files and summary counts

Missing frame files are recorded and skipped. Processing continues with the remaining references and mdocs; the original lines for missing references remain unchanged.

Illustrative output, not results from a real dataset:

```text
Summary (apply):
  Missing files: 3 (references: 4; deduplicated by mdoc + filename)
  Matched references: 120
  Successfully renamed this run: 118
  mdocs updated this run: 2
  Original mdocs backed up this run: 2
  Already prefixed; no rename needed: 2
  Missing references were skipped; their mdoc lines were left unchanged.
```

Missing-file counts are deduplicated by **mdoc path + case-insensitive filename**. Missing-reference counts include every occurrence. A frame referenced repeatedly within one mdoc is renamed only once. Already-prefixed files are counted separately from files renamed during this run.

Dry runs report planned renames and zero actual renames, along with planned mdoc backup counts. If every TIFF reference is missing, the script reports the counts without changing data; it creates mdoc backups only when explicitly requested with `--backup-mdoc --apply`. Completion with missing files returns exit code `0`; check the summary to assess completeness.

Each mdoc must contain at least one `SubFramePath` pointing to a `.tif` or `.tiff` filename. Invalid metadata, ambiguous matches, shared frames referenced by different mdocs, and destination collisions still stop processing before any data changes.

## Options

| Option | Meaning |
| --- | --- |
| `--mdoc PATH [PATH ...]` | Required: mdoc files or directories |
| `--frames PATH` | Required: recursively searched source frames directory |
| `--apply` | Apply changes, with backups enabled by default; otherwise preview only |
| `--dry-run` | Explicit preview; mutually exclusive with `--apply` |
| `--backup-dir PATH` | An unused backup directory; default is `frames_backup` beside frames |
| `--no-backup` | Disable default backups; `--backup-mdoc` can independently re-enable mdoc backups; incompatible with `--backup-dir` |
| `--backup-mdoc` | Back up all original input mdocs as adjacent `.mdoc.bak` files, even with `--no-backup` or `--no-update-mdoc` |
| `--path-mode absolute` | Write absolute frame paths to mdocs; the default |
| `--path-mode relative` | Write paths relative to each mdoc's directory; downstream software must support that interpretation |
| `--path-mode filename` | Write only filenames; downstream software must locate the frame directory separately |
| `--no-update-mdoc` | Rename frames while keeping all mdoc content unchanged; backup behavior is controlled separately |
| `--encoding NAME` | Specify the original mdoc encoding; otherwise detect UTF-8, UTF-16 with BOM, or GB18030 |
| `--progress-interval SECONDS` | Backup progress interval, default `10`; must be finite and positive |
| `--verbose` | Show all renames, mdoc updates, and missing references; default is the first 20 per category |

## Progress and logs

The script prints the host, PID, mode, and counts. When backups are enabled, it also prints the backup location, available filesystem space, and copy progress. Copies use an 8 MiB buffer and sequential I/O. Memory also depends on the file index and loaded mdoc text. Progress updates resume after any blocking filesystem operation returns.

To display output and save a log in Bash:

```bash
set -o pipefail
python3 -u align_mdoc_frames_en.py \
  --mdoc ./mdocs \
  --frames ./frames \
  --backup-dir ./frames_backup_new \
  --apply 2>&1 | tee "align_frames_$(date +%Y%m%d_%H%M%S).log"
```

Logs use UTF-8. Data paths and filenames are printed as supplied or resolved, including any Unicode characters they contain.

## Backup, locks, and recovery

- Run after acquisition has finished, with no other program modifying the dataset. Symlinks or junctions inside frames are rejected; use regular data files and directories.
- With backups enabled, the entire frames directory is copied before any renaming. The copy checks source size and modification time before/after each file, checks destination size, and flushes copied data. It does not compute full-file hashes.
- Existing backup directories and `.mdoc.bak` files are never overwritten. Both preview and apply validate the backup destinations they will use. For another run that needs frames backups, use a new backup directory. Preserve old mdoc backups elsewhere before creating new ones. `--no-backup` skips frames backup checks; `--backup-mdoc` still checks every selected mdoc's backup path before any changes or copies begin.
- Space checks reflect filesystem free bytes, not user/project quotas, inode quotas, or reserved space. The script does not automatically use node-local temporary storage. If you choose temporary storage, check its retention policy.
- Apply mode creates a sibling lock named `.<frames-directory-name>.align_mdoc_frames.lock`. It records the host and PID and prevents cooperating instances from processing the same directory concurrently. Normal exits and handled errors remove it; previews do not create a lock. Other programs are not controlled by this lock.
- A forced termination can leave a lock behind. Verify that the job on the recorded host has stopped before removing a stale lock. A PID on the current node alone is not sufficient evidence.
- A backup failure leaves original names untouched but may leave an incomplete backup directory. Inspect it and choose a new backup path for another attempt.
- All requested mdoc backups finish before any renaming or metadata updates begin. A failure while writing these backups stops processing before data changes, but may leave partial backup files; check them before retrying.
- During renaming or mdoc updates, exceptions, Ctrl+C, SIGTERM, and SIGHUP trigger an attempted rollback. Any backups created are retained. With `--no-backup` and no `--backup-mdoc`, rollback uses only the original mdoc content and rename list held in memory. With `--backup-mdoc`, on-disk mdoc copies are also retained. SIGKILL, node failure, or exhaustion of the scheduler's grace period can prevent rollback from finishing.
- To restore manually, first preserve the current frames directory, copy a **complete** backup back to the original frames path, and restore each mdoc from its `.mdoc.bak`. Keep backups until results have been verified.

Exit codes: `0` for completion (including skipped missing references), `1` for processing errors, `2` for argument errors, `130` for Ctrl+C, or `128 + signal number` for a handled termination signal.

## Repository layout and tests

```text
mdoc-frames-aligner/
├── align_mdoc_frames_en.py
├── align_mdoc_frames.py
├── README.md
├── README.zh-CN.md
├── .gitignore
├── .gitattributes
└── tests/
    ├── test_align_mdoc_frames_en.py
    ├── test_align_mdoc_frames_zh.py
    └── test_english_cli.py
```

Run from the repository root:

```bash
python3 -B -m unittest discover -s tests -v
```

Tests use temporary synthetic files, not microscope data. They cover both languages, enabled/disabled backups, independent original mdoc backups, missing references, collisions, repeated runs, encodings, interrupts, rollback, and the English CLI. The prepared version passed 102 local tests with Python 3.12; Python 3.8 syntax was checked, but it has not been executed on your cluster.


