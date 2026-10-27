"""Public API behavior and runtime offline/transaction guarantees."""

import socket
import subprocess
import sys
from pathlib import Path

import pytest
from airferry import VideoProgress, file_to_video, video


def test_offline_generation(tmp_path, monkeypatch):
    """Real native + Pillow + bundled FFmpeg, with Python networking disabled."""

    def forbidden(*args, **kwargs):
        raise AssertionError("network access during video generation")

    for name in (
        "socket",
        "create_connection",
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostname",
    ):
        monkeypatch.setattr(socket, name, forbidden)
    # Even an explicitly configured remote FFmpeg must not be consulted.
    monkeypatch.setenv("IMAGEIO_FFMPEG_EXE", "https://example.invalid/ffmpeg")
    monkeypatch.setenv("FFREPORT", "file=//example.invalid/share/ffmpeg.log")
    real_popen = subprocess.Popen
    commands = []

    def checked_popen(command, **kwargs):
        # Python 3.10/3.11 platform detection may run the local `ver` command.
        if isinstance(command, str) and command in (
            "ver",
            "command /c ver",
            "cmd /c ver",
        ):
            return real_popen(command, **kwargs)
        commands.append(command)
        whitelist_positions = [
            i for i, item in enumerate(command) if item == "-protocol_whitelist"
        ]
        assert len(whitelist_positions) == 2
        assert all(command[i + 1] == "file,pipe" for i in whitelist_positions)
        assert "FFREPORT" not in kwargs["env"]
        assert Path(command[0]).parent.name == "binaries"
        return real_popen(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", checked_popen)
    source = tmp_path / "offline.bin"
    source.write_bytes(b"entirely offline\x00\xff" * 1024)
    updates = []
    result = file_to_video(source, tmp_path / "offline.mp4", progress=updates.append)
    assert result.path.read_bytes()[4:8] == b"ftyp"
    assert len(commands) == 1
    assert updates[-1] == VideoProgress(result.frame_count, result.frame_count, 0, 1)
    assert result.duration > 0


def test_cancel_keeps_previous_output_and_cleans_temporary_video(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(bytes(range(256)) * 32)
    destination = tmp_path / "output.mp4"
    destination.write_bytes(b"previous video")

    def cancel(_):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        file_to_video(source, destination, overwrite=True, progress=cancel)
    assert destination.read_bytes() == b"previous video"
    assert set(tmp_path.iterdir()) == {source, destination}


def test_ffmpeg_failure_preserves_existing_output(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload" * 1024)
    destination = tmp_path / "previous.mp4"
    destination.write_bytes(b"previous")
    original_command = video._ffmpeg_command

    def invalid_codec(*args):
        command = original_command(*args)
        command[command.index("libx264")] = "airferry-nonexistent-codec"
        return command

    monkeypatch.setattr(video, "_ffmpeg_command", invalid_codec)
    with pytest.raises(RuntimeError, match="airferry-nonexistent-codec"):
        file_to_video(source, destination, overwrite=True)
    assert destination.read_bytes() == b"previous"
    assert set(tmp_path.iterdir()) == {source, destination}


def test_output_created_during_conversion_is_not_overwritten(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload" * 256)
    destination = tmp_path / "competing.mp4"

    def concurrent_writer(update):
        if update.frames_written == 1:
            destination.write_bytes(b"another writer")

    with pytest.raises(FileExistsError):
        file_to_video(source, destination, progress=concurrent_writer)
    assert destination.read_bytes() == b"another writer"


def test_rejects_unsafe_paths_and_input_overwrite(tmp_path):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"input must survive")
    with pytest.raises(ValueError, match="different files"):
        file_to_video(source, source, overwrite=True)
    for name in (
        "https://example.invalid/a.bin",
        "//server/share/a.bin",
        "\\\\server\\share\\a.bin",
    ):
        with pytest.raises(ValueError, match="local filesystem"):
            file_to_video(name, tmp_path / "output.mp4")
    assert source.read_bytes() == b"input must survive"


def test_empty_file_and_invalid_qr_configuration(tmp_path):
    source = tmp_path / "empty.bin"
    source.touch()
    with pytest.raises(ValueError, match="empty"):
        file_to_video(source, tmp_path / "output.mp4")
    assert not (tmp_path / "output.mp4").exists()
    source.write_bytes(b"nonempty")
    with pytest.raises(ValueError, match="symbol_size"):
        file_to_video(source, tmp_path / "output.mp4", symbol_size=1401)
    with pytest.raises(ValueError, match="qr_count"):
        file_to_video(source, tmp_path / "output.mp4", qr_count=1.0)


def test_repeats_and_cli(tmp_path):
    source = tmp_path / "input.bin"
    source.write_bytes(b"replay" * 100)
    once = file_to_video(source, tmp_path / "once.mp4")
    repeated = file_to_video(source, tmp_path / "twice.mp4", repeats=2)
    assert repeated.frame_count == once.frame_count * 2
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "airferry",
            str(source),
            str(tmp_path / "cli.mp4"),
            "--qr-count",
            "4",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "cli.mp4").read_bytes()[4:8] == b"ftyp"
    # Also exercise failure without Python tracebacks.
    failure = subprocess.run(
        [sys.executable, "-m", "airferry", str(source), str(tmp_path / "cli.mp4")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert failure.returncode == 1
    assert "Traceback" not in failure.stderr


def test_bundled_ffmpeg_missing_does_not_download(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    source.write_bytes(b"local only")
    monkeypatch.setattr(
        video, "FNAME_PER_PLATFORM", {video._platform_key(): "missing-ffmpeg"}
    )
    with pytest.raises(FileNotFoundError, match="Bundled FFmpeg is missing"):
        file_to_video(source, tmp_path / "output.mp4")
    assert not (tmp_path / "output.mp4").exists()


def test_fresh_process_generates_video_with_all_socket_audit_events_denied(tmp_path):
    source = tmp_path / "isolated.bin"
    source.write_bytes(b"offline subprocess\0\xff" * 512)
    destination = tmp_path / "isolated.mp4"
    script = """
import sys
def deny_network(event, args):
    if event.startswith('socket.'):
        raise RuntimeError('network access denied: ' + event)
sys.addaudithook(deny_network)
from airferry import file_to_video
file_to_video(sys.argv[1], sys.argv[2], qr_count=4)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(source), str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert destination.read_bytes()[4:8] == b"ftyp"
