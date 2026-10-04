# mdoc Frames Aligner

[English](README.md) | 简体中文

根据文件名匹配 SerialEM `.mdoc` 与 TIFF 帧文件，为匹配到的帧文件添加 mdoc 名称前缀，并同步更新引用。默认完整备份，可通过 `--no-backup` 关闭备份。适合申请计算节点后，在 Linux 集群的交互式终端直接运行。

本工具对齐的是**文件名与元数据引用**，不进行图像配准、运动校正，也不修改 TIFF 像素数据。

## 脚本与环境要求

| 脚本 | 注释、帮助及终端输出语言 |
| --- | --- |
| [`align_mdoc_frames_en.py`](align_mdoc_frames_en.py) | 英文 |
| [`align_mdoc_frames.py`](align_mdoc_frames.py) | 中文 |

两个脚本都可独立运行，参数和处理行为一致，选择其中一个即可。

- Python **3.8 或更高版本**，仅使用标准库，无需安装第三方包。
- 单进程运行，无需 GPU。
- 节点能够读取原始数据，并对 frames、mdoc、备份及执行锁所在目录具有所需的写权限。
- 启用备份时，空间需要容纳整个 frames，并为文件系统元数据和 mdoc 备份留出余量。

如果集群默认 Python 版本过旧，请按集群说明加载合适的 Python 模块，或激活已有环境。

## 在交互式节点上运行

**建议运行前备份所有原始文件**，包括全部 mdoc 和整个 frames 目录（所有文件及子目录）。请保留备份，直到确认重命名结果和后续处理结果正确。建议保留默认的 frames 备份，并添加 `--backup-mdoc` 备份所有选中的原始 mdoc；仅在其他位置已有完整且经过核验的备份时，再考虑使用 `--no-backup`。

申请好计算节点后，上传脚本，在终端进入脚本所在目录。以下 `/shared/project/data` 是示例路径，请替换为真实路径。

```bash
python3 --version

# 先预览：读取和检查，不改名、不创建备份。
python3 -u align_mdoc_frames.py \
  --mdoc /shared/project/data/mdocs \
  --frames /shared/project/data/frames \
  --dry-run

# 正式执行：完整备份后，改名并更新对应 mdoc。
python3 -u align_mdoc_frames.py \
  --mdoc /shared/project/data/mdocs \
  --frames /shared/project/data/frames \
  --apply
```

如果脚本、`mdocs` 和 `frames` 都在当前目录：

```bash
python3 -u align_mdoc_frames.py --mdoc ./mdocs --frames ./frames --apply
```

`./frames` 指当前目录下的 frames；`/frames` 指文件系统根目录下的 frames。可以用 `pwd` 和 `ls` 核对位置。

`--mdoc` 支持一个或多个文件/目录。mdoc 目录仅扫描第一层，frames 始终递归搜索其所有子目录。

```bash
python3 -u align_mdoc_frames.py \
  --mdoc ./mdocs/sample01.mdoc ./mdocs/sample02.mdoc \
  --frames ./frames \
  --apply
```

已有 `frames_backup` 时，指定一个尚不存在的新目录：

```bash
python3 -u align_mdoc_frames.py \
  --mdoc ./mdocs \
  --frames ./frames \
  --backup-dir ./frames_backup_rename_v2 \
  --apply
```

使用英文版时，将以上命令中的脚本名替换为 `align_mdoc_frames_en.py` 即可。

## 选择是否备份、是否修改 mdoc

| 在 `--apply` 之外添加的选项 | frames 与 mdoc 备份 | 更新 mdoc 引用 |
| --- | --- | --- |
| 不添加 | 是 | 是 |
| `--no-backup` | 否 | 是 |
| `--no-update-mdoc` | 备份 frames；mdoc 不变，无需备份 | 否 |
| `--no-backup --no-update-mdoc` | 否 | 否 |
| `--no-backup --backup-mdoc` | 仅备份所有输入 mdoc | 是 |
| `--no-backup --backup-mdoc --no-update-mdoc` | 仅备份所有输入 mdoc | 否 |

**只给帧文件加前缀，不备份、不修改 mdoc：**

```bash
python3 -u align_mdoc_frames.py \
  --mdoc ./mdocs \
  --frames ./frames \
  --no-backup \
  --no-update-mdoc \
  --apply
```

如果不备份，但仍要同步更新 mdoc 引用，去掉 `--no-update-mdoc`。把 `--apply` 换成 `--dry-run` 可预览任一种模式。

`--no-backup` 会关闭 frames 复制和默认的 mdoc 备份；未加 `--backup-mdoc` 时，也跳过备份空间和备份位置检查，已有备份保持不变。该选项不能与 `--backup-dir` 同时使用。缺失统计、重名检查、执行锁和中断时尝试回退的行为保留。两种备份都未启用时，本次不会创建磁盘恢复副本；如果同时选择不更新 mdoc，其中的引用仍保留旧文件名。

**只备份原始 mdoc，不复制 frames：**

```bash
python3 -u align_mdoc_frames.py \
  --mdoc ./mdocs \
  --frames ./frames \
  --no-backup \
  --backup-mdoc \
  --apply
```

`--backup-mdoc` 会在任何改名和引用更新之前，独立备份**所有选中的输入 mdoc**：例如在 `sample01.mdoc` 同目录下创建 `sample01.mdoc.bak`。即使加了 `--no-update-mdoc`、mdoc 中的帧全部缺失，或已经无需改名/更新，也会按显式请求备份。无需修改数据时，只备份 mdoc，不复制 frames。已有 `.mdoc.bak` 不会被覆盖，请先另存旧备份再重新执行。预览模式只显示计划，不创建备份。

## 处理结果

例如 `sample01.mdoc` 原来包含：

```text
[ZValue = 0]
SubFramePath = Z:\old_computer\acquisition\image001.tif
TiltAngle = -60
```

脚本忽略旧目录，只根据 `image001.tif` 在指定 frames 及其子目录中查找，唯一匹配的文件将改为 `sample01_image001.tif`。

```text
data/
├── mdocs/
│   ├── sample01.mdoc       # 引用更新为新帧文件路径
│   └── sample01.mdoc.bak   # 原始 mdoc 内容
├── frames/
│   └── sample01_image001.tif
└── frames_backup/
    └── image001.tif       # 原名称的数据副本
```

- 只处理 `.tif` / `.tiff` 引用，兼容 mdoc 中的 Windows、UNC 和 Linux 旧路径。
- 搜索时不区分大小写。出现同名歧义时停止，不根据可能过时的旧目录猜测。
- 前缀是 mdoc 文件名去掉最后的 `.mdoc`：`sample01.mrc.mdoc` 对应 `sample01.mrc_`。
- 帧文件保留所在子目录；未被引用的文件保留原名，也会进入完整备份。
- 默认将匹配成功的 `SubFramePath` 更新为当前节点上的新绝对路径。其余字段、节顺序、编码和换行方式保留。
- mdoc 自身的文件名不变，默认将每个被修改的 mdoc 另存原文为同目录下的 `.mdoc.bak`。`--no-backup` 关闭默认备份；`--backup-mdoc` 可独立启用所有输入 mdoc 的原文备份。

## 缺失文件与统计口径

找不到的文件会记录并跳过，继续处理其余引用和后续 mdoc；缺失引用对应的原始行保持不变。

以下仅为输出格式示例，不是实际数据处理结果：

```text
处理汇总（执行）：
  未找到文件：3 个（4 条引用；按 mdoc + 文件名去重）
  成功匹配引用：120 条
  本次成功重命名：118 个文件
  本次更新 mdoc：2 个
  本次备份原始 mdoc：2 个
  已有前缀、无需重命名：2 个文件
  未找到的引用已跳过，对应 mdoc 行保留原文。
```

未找到文件数按 **mdoc 路径 + 文件名（不区分大小写）** 去重，缺失引用数则包括所有出现次数。同一帧在同一 mdoc 中被多次引用，只计一次重命名。已有前缀的文件单独统计，不计为本次新改名。

预览显示预计改名数和 mdoc 备份数，实际改名数为 0。若全部 TIF 引用都缺失，则不修改数据；仅在显式使用 `--backup-mdoc --apply` 时，仍会备份原始 mdoc。允许有缺失文件的正常完成仍返回退出码 `0`，完整性请以最后汇总为准。

每个 mdoc 必须至少包含一条指向 `.tif/.tiff` 文件名的 `SubFramePath`。元数据格式错误、同名歧义、不同 mdoc 共用同一帧文件、目标名称冲突，仍会在数据修改前停止。

## 参数

| 参数 | 作用 |
| --- | --- |
| `--mdoc PATH [PATH ...]` | 必填：一个或多个 mdoc 文件/目录 |
| `--frames PATH` | 必填：递归搜索的原始 frames 目录 |
| `--apply` | 实际执行，默认先备份；不加此参数仅预览 |
| `--dry-run` | 显式预览，不能与 `--apply` 同时使用 |
| `--backup-dir PATH` | 尚不存在的备份目录，默认在 frames 同级创建 `frames_backup` |
| `--no-backup` | 关闭默认备份，可用 `--backup-mdoc` 独立启用 mdoc 备份；不能与 `--backup-dir` 同时使用 |
| `--backup-mdoc` | 将所有输入 mdoc 原文备份为同目录下的 `.mdoc.bak`，可与 `--no-backup`、`--no-update-mdoc` 同时使用 |
| `--path-mode absolute` | 写回绝对路径，默认选项 |
| `--path-mode relative` | 写回相对于每个 mdoc 所在目录的路径，下游软件需支持这种解析方式 |
| `--path-mode filename` | 只写文件名，下游软件需另行指定 frames 位置 |
| `--no-update-mdoc` | 重命名帧文件，保持所有 mdoc 内容不变；备份行为单独控制 |
| `--encoding NAME` | 指定原始 mdoc 编码；默认识别 UTF-8、带 BOM 的 UTF-16、GB18030 |
| `--progress-interval SECONDS` | 备份进度输出间隔，默认 `10` 秒，必须为有限正数 |
| `--verbose` | 显示所有改名、mdoc 更新及缺失引用明细，默认各显示前 20 条 |

## 进度与日志

脚本显示节点名、PID、运行模式和统计数量。启用备份时，还会显示备份位置、文件系统可用空间和复制进度。采用 8 MiB 缓冲区顺序复制；额外内存随文件索引和 mdoc 文本数量增长。单次文件系统操作阻塞时，会在其返回后继续刷新进度。

在 Bash 中同时显示并保存日志：

```bash
set -o pipefail
python3 -u align_mdoc_frames.py \
  --mdoc ./mdocs \
  --frames ./frames \
  --backup-dir ./frames_backup_new \
  --apply 2>&1 | tee "align_frames_$(date +%Y%m%d_%H%M%S).log"
```

日志使用 UTF-8。路径及文件名按实际内容显示；英文版也会原样显示数据路径中自带的中文等字符。

## 备份、执行锁与恢复

- 请在采集完成、其他程序停止修改数据后运行。frames 内部的符号链接或目录联接会报错，请使用实际数据文件和目录。
- 启用备份时，整个 frames 复制完成后才开始改名。复制时检查源文件复制前后的大小、修改时间及副本大小，并刷新写入；不计算全量文件哈希。
- 不覆盖已有备份目录或 `.mdoc.bak`。预览和正式执行都会检查本次将使用的备份位置。后续仍需 frames 备份时，请指定新目录；创建新的 mdoc 备份前，请先另存旧备份。`--no-backup` 跳过 frames 备份检查，但 `--backup-mdoc` 仍会在任何修改或复制开始前检查全部所选 mdoc 的备份路径。
- 空间检查只反映文件系统可用字节数，不能确认用户/项目配额、inode 配额或预留空间。脚本不自动使用节点临时目录；若自行指定临时存储，请了解其保留策略。
- 执行模式会在 frames 同级创建 `.<frames目录名>.align_mdoc_frames.lock`，记录节点和 PID，阻止本脚本同时处理同一目录。正常退出和可处理异常后释放，预览不创建锁。其他程序不受该锁约束。
- 强制终止后可能残留锁。确认记录的节点上对应作业已结束后，才可移除残留锁；不能仅凭当前节点是否存在同一 PID 判断。
- 备份失败时原文件名不会被修改，但可能留下不完整备份。检查后为重试选择新的备份位置。
- 所有请求的 mdoc 备份完成后才开始改名和更新引用。写入 mdoc 备份失败会在数据修改前停止，但可能留下不完整的备份文件，重试前请先检查。
- 改名和更新阶段遇到异常、Ctrl+C、SIGTERM 或 SIGHUP 时会尝试回退，保留本次已创建的备份。使用 `--no-backup` 且未加 `--backup-mdoc` 时，只依靠内存中的原 mdoc 内容和改名记录尝试回退；加了 `--backup-mdoc` 则还会保留磁盘上的 mdoc 副本。SIGKILL、节点故障或调度器宽限时间耗尽时，不能保证回退完成。
- 手动恢复时，先另存当前 frames，再将**完整**备份复制回原位置，并用各 `.mdoc.bak` 恢复原 mdoc。保留备份直到确认结果。

退出码：完成（含跳过缺失文件）为 `0`，处理错误为 `1`，参数错误为 `2`，Ctrl+C 为 `130`，可处理终止信号为 `128 + 信号编号`。

## 仓库结构与测试

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



