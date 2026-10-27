# AirFerry Python

将本地文件转换成可由 AirFerry 接收端扫描的 MP4 二维码视频。Python 负责接口与视频输出，RaptorQ、帧格式和二维码矩阵直接复用项目的 Rust 核心。

Python 包版本独立为 **0.1.0**。当前仓库已提供构建配置，尚未发布到 PyPI；`airferry` 发行名称的可用性需在实际发布时确认。

## 安装与使用

需要 CPython 3.10 或更新版本。当前源码安装需要完整 AirFerry 仓库、Rust 和本机 C/C++ 工具链：

```bash
# 在 AirFerry 仓库根目录
python -m pip install ./apps/python
```

```python
from airferry import file_to_video

result = file_to_video("report.pdf", "report.mp4")
print(result.path, result.frame_count, result.frame_count / result.fps)
```

```bash
airferry report.pdf report.mp4
airferry report.pdf report-4qr.mp4 --qr-count 4 --fps 30 --compression auto
python -m airferry report.pdf report.mp4 --overwrite
```

安装完成后，生成视频全程在本机执行：不下载模型或编码器、不发送遥测、不调用在线服务。`imageio-ffmpeg` 的平台 wheel 在安装时带入 FFmpeg；运行时只使用该已安装 wheel 自带的本地可执行文件，缺失就报错，不回退系统 PATH、Conda 或 `IMAGEIO_FFMPEG_EXE`。输入、输出和临时目录必须位于本地文件系统，UNC、远程映射盘及网络 URL 不受支持。

需要在隔离环境安装时，先在相同系统与 CPU 架构的联网环境准备本包 wheel 和所有依赖的 wheel，再复制到隔离设备：

```bash
python -m pip install --no-index --find-links ./wheelhouse airferry==0.1.0
```

此离线契约覆盖本包调用链和本包启动的 FFmpeg；操作系统及其他程序的网络行为由其自身配置决定。

## 接口

```python
file_to_video(
    input_path,
    output_path,
    *,
    fps=30,
    symbol_size=1400,
    qr_count=1,
    module_size=4,
    redundancy_pct=50,
    repeats=1,
    compression="auto",
    overwrite=False,
    progress=None,
)
```

| 参数 | 含义 |
|------|------|
| `input_path` / `output_path` | 本地文件路径；输出必须为 `.mp4`，父目录需已存在 |
| `fps` | 视频帧率，整数 1–120，默认 30 |
| `symbol_size` | 每个协议码的负载字节数，默认 1400；64–2888 且按 8 对齐，还需容纳完整描述符 |
| `qr_count` | 每视频帧 1 个或 4 个不同协议二维码 |
| `module_size` | 每个二维码模块的整数像素边长，1–16，默认 4 |
| `redundancy_pct` | 每段源符号之外的修复比例，5–50，默认 50；至少追加 8 个修复符号 |
| `repeats` | 将完整传输重复写入视频的次数，整数 1–100，默认 1 |
| `compression` | `auto`、`none`、`zstd` 或 `xz`；`auto` 在 Raw 与 Zstd Lv1 之间选择更小结果 |
| `overwrite` | 默认拒绝覆盖；显式设为 `True` 才替换已有输出 |
| `progress` | 可选回调，接收 `VideoProgress`；包含已写帧数、总帧数、从 0 开始的段索引和总段数 |

返回不可变 `VideoResult`，包含 `path`、`width`、`height`、`frame_count`、`fps`、`segment_count`、`compression`、`original_size` 和 `compressed_size`；`duration` 属性返回秒数。进度回调抛出异常可取消转换，已有输出仍会保留。

```python
from airferry import file_to_video

def show_progress(progress):
    print(f"{progress.frames_written}/{progress.total_frames}")

result = file_to_video(
    "archive.zip",
    "archive.mp4",
    qr_count=4,
    progress=show_progress,
)
```

视频采用 H.264、CRF 18、`yuv420p`，默认单码画面为 532×532，四码画面为 1064×1064。二维码使用整数倍最近邻放大，每码四模块白边。输出在同目录临时文件中完成编码，成功后才提交；出错会清理临时结果。

每个分段的每次传输至少写入 1 秒视频；小文件达到最低符号预算后继续产生新的修复符号，给扫码和元数据同步留出时间。

## 播放与边界

用播放器正常速度播放视频，保持清晰的黑白边缘与完整白边，再由 AirFerry Android、Windows 或网页接收端扫描。实际光学速度取决于屏幕、缩放、摄像头曝光及扫码能力；自动测试不能代替手机实拍验收。

与持续生成新修复符号的交互发送端不同，MP4 包含有限符号。反复播放同一视频，或提高 `repeats`，不会增加独立修复符号的数量。丢失过多不同符号时应重新生成更有利于扫描的视频，例如降低帧率或增大模块像素。

大文件先形成一条完整压缩流，再按协议约 32 MiB 分段；内存中只构建当前段的 RaptorQ 编码器，原始快照与压缩流占用临时磁盘空间。原文件超过 256 MiB 时也使用 descriptor v5，即使只产生一个压缩段。空文件明确拒绝，文件名最多 255 个 UTF-8 字节，且必须能放入当前 symbol_size 的描述符。该接口处理单文件，文件夹、多文件容器和文字专用容器不在本版本范围内。

Python 发送端自动管理会话 ID，并将其绑定到原文件及精确压缩流的 SHA-256、压缩算法、`symbol_size` 和文件身份。改变编码参数或压缩结果会使用不同会话，避免接收端混用旧符号；无需调用者管理 ID。

开发、验证、离线部署和发布流程见[构建指南](https://github.com/UR-SillyB/AirFerry/blob/main/docs/build-python.md)。
