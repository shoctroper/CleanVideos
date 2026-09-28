from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
CLI_PATH = SCRIPTS_DIR / "cli.py"

# Ensure scripts directory is in sys.path
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from cli import build_parser, main
from engines import EngineSeam


def create_test_clip(path: Path, duration: float = 1.0) -> Path:
    """Creates a short 1-second synthetic MP4 clip with audio using ffmpeg."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=1920x1080:rate=30",
        "-f", "lavfi", "-i", f"sine=frequency=1000:duration={duration}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def get_video_dimensions(path: Path) -> tuple[int, int]:
    """Uses ffprobe to get video stream width and height."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        str(path),
    ]
    res = subprocess.run(cmd, check=True, capture_output=True, text=True)
    data = json.loads(res.stdout)
    w = int(data["streams"][0]["width"])
    h = int(data["streams"][0]["height"])
    return w, h


def get_video_duration(path: Path) -> float:
    """Uses ffprobe to get media duration in seconds."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1",
        str(path),
    ]
    res = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return float(res.stdout.strip())



def create_hermetic_config(tmp_path: Path) -> Path:
    """Generates a temporary config.yaml with all absolute paths pointing inside tmp_path."""
    cfg = {
        "watch_folder": str(tmp_path / "watch_folder"),
        "processing_dir": str(tmp_path / "processing"),
        "output_dir": str(tmp_path / "output"),
        "logs_dir": str(tmp_path / "logs"),
        "state_db": str(tmp_path / "state.db"),
        "models_dir": str(tmp_path / "models"),
        "video_extensions": [".mp4", ".mov", ".mkv"],
        "stable_wait_seconds": 1.0,
        "demucs_model": "htdemucs",
        "horizontal_resolution": [3840, 2160],
        "vertical_resolution": [1080, 1920],
    }
    cfg_file = tmp_path / "config.yaml"
    with open(cfg_file, "w") as f:
        yaml.dump(cfg, f)
    return cfg_file


def create_fake_engines(tmp_path: Path) -> tuple[str, str, str]:
    """
    Creates python fake engine scripts that follow the [ *parts, '--input', src, '--output', dst ] contract:
    - fake_separator: ffmpeg -y -i in -af volume=0.3 -c:v copy out
    - fake_cropper: ffmpeg -y -i in -vf crop=ih*9/16:ih,scale=1080:1920 out
    - fake_sepbad: writes to stderr and exits with code 3
    """
    fake_sep = tmp_path / "fake_sep.py"
    fake_sep.write_text(
        "import argparse, subprocess\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--input', required=True)\n"
        "p.add_argument('--output', required=True)\n"
        "args = p.parse_args()\n"
        "subprocess.run(['ffmpeg', '-y', '-i', args.input, '-af', 'volume=0.3', '-c:v', 'copy', args.output], check=True)\n"
    )

    fake_crop = tmp_path / "fake_crop.py"
    fake_crop.write_text(
        "import argparse, subprocess\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--input', required=True)\n"
        "p.add_argument('--output', required=True)\n"
        "args = p.parse_args()\n"
        "subprocess.run(['ffmpeg', '-y', '-i', args.input, '-vf', 'crop=ih*9/16:ih,scale=1080:1920', '-c:a', 'copy', args.output], check=True)\n"
    )

    fake_sepbad = tmp_path / "fake_sepbad.py"
    fake_sepbad.write_text(
        "import sys\n"
        "sys.stderr.write('Fake separator crashed with code 3\\n')\n"
        "sys.exit(3)\n"
    )

    sep_cmd = f"{sys.executable} {fake_sep}"
    crop_cmd = f"{sys.executable} {fake_crop}"
    sepbad_cmd = f"{sys.executable} {fake_sepbad}"
    return sep_cmd, crop_cmd, sepbad_cmd


def test_cli_file_exists():
    """Assert cli.py exists in scripts directory."""
    assert CLI_PATH.is_file(), f"CLI file not found at {CLI_PATH}"


def test_cli_help_subprocess():
    """Assert that running cli.py with --help exits 0 and advertises --input and --output."""
    result = subprocess.run(
        [sys.executable, str(CLI_PATH), "--help"],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0
    assert "--input" in result.stdout
    assert "--output" in result.stdout
    assert "--separator" in result.stdout
    assert "--cropper" in result.stdout
    assert "--trim" in result.stdout


def test_cli_parser_help_content():
    """Assert that parser exposes --input, --output, --force, --dry-run, --separator, --cropper, --trim flags."""
    parser = build_parser()
    help_text = parser.format_help()
    assert "--input" in help_text
    assert "--output" in help_text
    assert "--force" in help_text
    assert "--dry-run" in help_text
    assert "--separator" in help_text
    assert "--cropper" in help_text
    assert "--trim" in help_text



def test_cli_missing_input_rejects_subprocess():
    """Assert running cli.py without --input fails with a non-zero exit code."""
    result = subprocess.run(
        [sys.executable, str(CLI_PATH)],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode != 0
    assert "the following arguments are required: --input" in result.stderr or "--input" in result.stderr


def test_cli_invalid_input_file_rejects_subprocess(tmp_path):
    """Assert passing a non-existent file to --input fails with a non-zero exit code."""
    non_existent = tmp_path / "does_not_exist.mp4"
    result = subprocess.run(
        [sys.executable, str(CLI_PATH), "--input", str(non_existent)],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode != 0


def test_cli_missing_input_raises_system_exit():
    """Assert main([]) raises SystemExit with non-zero exit code."""
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code != 0


def test_cli_invalid_input_raises_system_exit(tmp_path):
    """Assert main with non-existent file raises SystemExit with non-zero exit code."""
    non_existent = str(tmp_path / "non_existent.mp4")
    with pytest.raises(SystemExit) as exc_info:
        main(["--input", non_existent])
    assert exc_info.value.code != 0


def test_engine_seam_defaults():
    """Assert EngineSeam exposes callable engine hooks by default."""
    seam = EngineSeam()
    assert callable(seam.separator)
    assert callable(seam.cropper)
    assert callable(seam.separate_audio)
    assert callable(seam.mux_horizontal)
    assert callable(seam.build_vertical)


def test_case_a_and_b_env_override_produces_horizontal_and_vertical_9_16(tmp_path):
    """
    Cases (a) and (b):
    (a) override por env produce horizontal + vertical
    (b) el vertical es 9:16 medido con ffprobe
    """
    cfg_file = create_hermetic_config(tmp_path)
    sep_cmd, crop_cmd, _ = create_fake_engines(tmp_path)
    clip = create_test_clip(tmp_path / "input" / "sample.mp4", duration=1.0)
    out_dir = tmp_path / "output"

    env = os.environ.copy()
    env["CLEANVIDEOS_SEPARATOR"] = sep_cmd
    env["CLEANVIDEOS_CROPPER"] = crop_cmd

    result = subprocess.run(
        [
            sys.executable, str(CLI_PATH),
            "--input", str(clip),
            "--config", str(cfg_file),
            "--output", str(out_dir),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"CLI execution failed: {result.stderr}"

    horiz_out = out_dir / "video_clean" / "sample.mp4"
    vert_out = out_dir / "vertical" / "sample.mp4"
    assert horiz_out.is_file(), f"Horizontal deliverable missing at {horiz_out}"
    assert vert_out.is_file(), f"Vertical deliverable missing at {vert_out}"

    # Verify vertical aspect ratio is 9:16
    w, h = get_video_dimensions(vert_out)
    assert abs((w / h) - (9.0 / 16.0)) < 1e-2, f"Expected 9:16 aspect ratio, got {w}x{h}"


def test_case_c_separator_failed_exits_nonzero_and_no_deliverables(tmp_path):
    """
    Case (c): separador caido -> exit != 0 y ningun entregable
    """
    cfg_file = create_hermetic_config(tmp_path)
    _, crop_cmd, sepbad_cmd = create_fake_engines(tmp_path)
    clip = create_test_clip(tmp_path / "input" / "sample_fail.mp4", duration=1.0)
    out_dir = tmp_path / "output"

    env = os.environ.copy()
    env["CLEANVIDEOS_SEPARATOR"] = sepbad_cmd
    env["CLEANVIDEOS_CROPPER"] = crop_cmd

    result = subprocess.run(
        [
            sys.executable, str(CLI_PATH),
            "--input", str(clip),
            "--config", str(cfg_file),
            "--output", str(out_dir),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0
    assert "Error:" in result.stderr or "failed" in result.stderr

    horiz_out = out_dir / "video_clean" / "sample_fail.mp4"
    vert_out = out_dir / "vertical" / "sample_fail.mp4"
    assert not horiz_out.exists(), f"Horizontal deliverable should not exist on failure: {horiz_out}"
    assert not vert_out.exists(), f"Vertical deliverable should not exist on failure: {vert_out}"


def test_case_d_repeat_same_run_preserves_mtimes(tmp_path):
    """
    Case (d): repetir la MISMA corrida no cambia mtimes (idempotent skip)
    """
    cfg_file = create_hermetic_config(tmp_path)
    sep_cmd, crop_cmd, _ = create_fake_engines(tmp_path)
    clip = create_test_clip(tmp_path / "input" / "sample_repeat.mp4", duration=1.0)
    out_dir = tmp_path / "output"

    env = os.environ.copy()
    env["CLEANVIDEOS_SEPARATOR"] = sep_cmd
    env["CLEANVIDEOS_CROPPER"] = crop_cmd

    cmd = [
        sys.executable, str(CLI_PATH),
        "--input", str(clip),
        "--config", str(cfg_file),
        "--output", str(out_dir),
    ]

    # First run
    res1 = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert res1.returncode == 0

    horiz_out = out_dir / "video_clean" / "sample_repeat.mp4"
    vert_out = out_dir / "vertical" / "sample_repeat.mp4"
    assert horiz_out.is_file()
    assert vert_out.is_file()

    horiz_mtime_1 = horiz_out.stat().st_mtime_ns
    vert_mtime_1 = vert_out.stat().st_mtime_ns

    time.sleep(0.05)

    # Second run with same parameters
    res2 = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert res2.returncode == 0

    horiz_mtime_2 = horiz_out.stat().st_mtime_ns
    vert_mtime_2 = vert_out.stat().st_mtime_ns

    assert horiz_mtime_1 == horiz_mtime_2, "Horizontal deliverable was rewritten on idempotent run"
    assert vert_mtime_1 == vert_mtime_2, "Vertical deliverable was rewritten on idempotent run"


def test_case_e_same_file_second_output_reprocesses(tmp_path):
    """
    Case (e): el mismo archivo con un SEGUNDO --output no se salta
    """
    cfg_file = create_hermetic_config(tmp_path)
    sep_cmd, crop_cmd, _ = create_fake_engines(tmp_path)
    clip = create_test_clip(tmp_path / "input" / "sample_multiout.mp4", duration=1.0)
    out_dir_1 = tmp_path / "output1"
    out_dir_2 = tmp_path / "output2"

    env = os.environ.copy()
    env["CLEANVIDEOS_SEPARATOR"] = sep_cmd
    env["CLEANVIDEOS_CROPPER"] = crop_cmd

    # First run to out_dir_1
    res1 = subprocess.run(
        [
            sys.executable, str(CLI_PATH),
            "--input", str(clip),
            "--config", str(cfg_file),
            "--output", str(out_dir_1),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res1.returncode == 0
    assert (out_dir_1 / "video_clean" / "sample_multiout.mp4").is_file()
    assert (out_dir_1 / "vertical" / "sample_multiout.mp4").is_file()

    # Second run to out_dir_2
    res2 = subprocess.run(
        [
            sys.executable, str(CLI_PATH),
            "--input", str(clip),
            "--config", str(cfg_file),
            "--output", str(out_dir_2),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res2.returncode == 0
    assert (out_dir_2 / "video_clean" / "sample_multiout.mp4").is_file(), "Second output dir missing horizontal deliverable"
    assert (out_dir_2 / "vertical" / "sample_multiout.mp4").is_file(), "Second output dir missing vertical deliverable"


def test_cli_flags_separator_and_cropper_override(tmp_path):
    """Assert passing --separator and --cropper flags overrides engines directly."""
    cfg_file = create_hermetic_config(tmp_path)
    sep_cmd, crop_cmd, _ = create_fake_engines(tmp_path)
    clip = create_test_clip(tmp_path / "input" / "sample_flags.mp4", duration=1.0)
    out_dir = tmp_path / "output"

    result = subprocess.run(
        [
            sys.executable, str(CLI_PATH),
            "--input", str(clip),
            "--config", str(cfg_file),
            "--output", str(out_dir),
            "--separator", sep_cmd,
            "--cropper", crop_cmd,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert (out_dir / "video_clean" / "sample_flags.mp4").is_file()
    assert (out_dir / "vertical" / "sample_flags.mp4").is_file()


def test_force_flag_forces_reprocessing(tmp_path):
    """Assert passing --force causes existing outputs to be re-rendered."""
    cfg_file = create_hermetic_config(tmp_path)
    sep_cmd, crop_cmd, _ = create_fake_engines(tmp_path)
    clip = create_test_clip(tmp_path / "input" / "sample_force.mp4", duration=1.0)
    out_dir = tmp_path / "output"

    cmd = [
        sys.executable, str(CLI_PATH),
        "--input", str(clip),
        "--config", str(cfg_file),
        "--output", str(out_dir),
        "--separator", sep_cmd,
        "--cropper", crop_cmd,
    ]

    res1 = subprocess.run(cmd, capture_output=True, text=True)
    assert res1.returncode == 0

    horiz_out = out_dir / "video_clean" / "sample_force.mp4"
    vert_out = out_dir / "vertical" / "sample_force.mp4"
    horiz_mtime_1 = horiz_out.stat().st_mtime_ns
    vert_mtime_1 = vert_out.stat().st_mtime_ns

    time.sleep(0.05)

    # Force reprocess
    res2 = subprocess.run([*cmd, "--force"], capture_output=True, text=True)
    assert res2.returncode == 0

    horiz_mtime_2 = horiz_out.stat().st_mtime_ns
    vert_mtime_2 = vert_out.stat().st_mtime_ns

    assert horiz_mtime_2 > horiz_mtime_1
    assert vert_mtime_2 > vert_mtime_1


def test_valid_trim_produces_trimmed_deliverables(tmp_path):
    """Assert running CLI with a valid --trim produces ~3.0s horizontal and vertical deliverables."""
    cfg_file = create_hermetic_config(tmp_path)
    sep_cmd, crop_cmd, _ = create_fake_engines(tmp_path)
    clip = create_test_clip(tmp_path / "input" / "clip.mp4", duration=6.0)
    out_dir = tmp_path / "output"

    env = os.environ.copy()
    env["CLEANVIDEOS_SEPARATOR"] = sep_cmd
    env["CLEANVIDEOS_CROPPER"] = crop_cmd

    cmd = [
        sys.executable, str(CLI_PATH),
        "--input", str(clip),
        "--config", str(cfg_file),
        "--output", str(out_dir),
        "--trim", "1.0:4.0",
    ]

    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert res.returncode == 0, f"Valid trim failed: {res.stderr}"

    horiz_out = out_dir / "video_clean" / "clip_trim1.0-4.0.mp4"
    vert_out = out_dir / "vertical" / "clip_trim1.0-4.0.mp4"

    assert horiz_out.is_file(), f"Expected horizontal deliverable at {horiz_out}"
    assert vert_out.is_file(), f"Expected vertical deliverable at {vert_out}"

    d_horiz = get_video_duration(horiz_out)
    d_vert = get_video_duration(vert_out)

    assert abs(d_horiz - 3.0) < 0.5, f"Expected ~3.0s horizontal, got {d_horiz:.2f}s"
    assert abs(d_vert - 3.0) < 0.5, f"Expected ~3.0s vertical, got {d_vert:.2f}s"


def test_invalid_trim_end_le_start_rejects(tmp_path):
    """Assert running CLI with end <= start rejects before work and produces no deliverables."""
    cfg_file = create_hermetic_config(tmp_path)
    sep_cmd, crop_cmd, _ = create_fake_engines(tmp_path)
    clip = create_test_clip(tmp_path / "input" / "clip_bad.mp4", duration=6.0)
    out_dir = tmp_path / "output_bad"

    env = os.environ.copy()
    env["CLEANVIDEOS_SEPARATOR"] = sep_cmd
    env["CLEANVIDEOS_CROPPER"] = crop_cmd

    cmd = [
        sys.executable, str(CLI_PATH),
        "--input", str(clip),
        "--config", str(cfg_file),
        "--output", str(out_dir),
        "--trim", "5.0:2.0",
    ]

    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert res.returncode != 0, "Expected non-zero exit for end <= start"
    assert "Error:" in res.stderr or "Invalid trim range" in res.stderr

    created_files = list(out_dir.rglob("*.mp4")) if out_dir.exists() else []
    assert not created_files, f"No deliverables should be created on invalid trim, found: {created_files}"


def test_invalid_trim_end_exceeds_duration_rejects(tmp_path):
    """Assert running CLI with end > input_duration + 0.25s rejects before work and produces no deliverables."""
    cfg_file = create_hermetic_config(tmp_path)
    sep_cmd, crop_cmd, _ = create_fake_engines(tmp_path)
    clip = create_test_clip(tmp_path / "input" / "clip_exceed.mp4", duration=6.0)
    out_dir = tmp_path / "output_exceed"

    env = os.environ.copy()
    env["CLEANVIDEOS_SEPARATOR"] = sep_cmd
    env["CLEANVIDEOS_CROPPER"] = crop_cmd

    cmd = [
        sys.executable, str(CLI_PATH),
        "--input", str(clip),
        "--config", str(cfg_file),
        "--output", str(out_dir),
        "--trim", "1.0:99.0",
    ]

    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert res.returncode != 0, "Expected non-zero exit for end exceeding duration"
    assert "Error:" in res.stderr or "exceeds" in res.stderr

    created_files = list(out_dir.rglob("*.mp4")) if out_dir.exists() else []
    assert not created_files, f"No deliverables should be created on invalid trim, found: {created_files}"


def test_trim_idempotence_and_coexistence_with_untrimmed(tmp_path):
    """Assert repeated trim runs skip reprocessing and untrimmed runs coexist without collision."""
    cfg_file = create_hermetic_config(tmp_path)
    sep_cmd, crop_cmd, _ = create_fake_engines(tmp_path)
    clip = create_test_clip(tmp_path / "input" / "clip_coexist.mp4", duration=6.0)
    out_dir = tmp_path / "output"

    env = os.environ.copy()
    env["CLEANVIDEOS_SEPARATOR"] = sep_cmd
    env["CLEANVIDEOS_CROPPER"] = crop_cmd

    trim_cmd = [
        sys.executable, str(CLI_PATH),
        "--input", str(clip),
        "--config", str(cfg_file),
        "--output", str(out_dir),
        "--trim", "1.0:4.0",
    ]

    # First trim run
    res1 = subprocess.run(trim_cmd, capture_output=True, text=True, env=env)
    assert res1.returncode == 0
    horiz_trim = out_dir / "video_clean" / "clip_coexist_trim1.0-4.0.mp4"
    vert_trim = out_dir / "vertical" / "clip_coexist_trim1.0-4.0.mp4"
    assert horiz_trim.is_file()
    assert vert_trim.is_file()
    horiz_mtime_1 = horiz_trim.stat().st_mtime_ns
    vert_mtime_1 = vert_trim.stat().st_mtime_ns

    time.sleep(0.05)

    # Second trim run (idempotent skip)
    res2 = subprocess.run(trim_cmd, capture_output=True, text=True, env=env)
    assert res2.returncode == 0
    assert horiz_trim.stat().st_mtime_ns == horiz_mtime_1
    assert vert_trim.stat().st_mtime_ns == vert_mtime_1

    # Full untrimmed run on same video
    full_cmd = [
        sys.executable, str(CLI_PATH),
        "--input", str(clip),
        "--config", str(cfg_file),
        "--output", str(out_dir),
    ]
    res3 = subprocess.run(full_cmd, capture_output=True, text=True, env=env)
    assert res3.returncode == 0

    horiz_full = out_dir / "video_clean" / "clip_coexist.mp4"
    vert_full = out_dir / "vertical" / "clip_coexist.mp4"
    assert horiz_full.is_file()
    assert vert_full.is_file()

    # Verify both full and trimmed deliverables coexist
    assert horiz_trim.is_file()
    assert vert_trim.is_file()
    assert get_video_duration(horiz_full) > 5.0
    assert abs(get_video_duration(horiz_trim) - 3.0) < 0.5

