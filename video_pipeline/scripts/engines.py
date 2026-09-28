"""
Engine Seam Contract for CleanVideos Pipeline.

Uniform video->video engine interface:
Each engine implements a transformation from an input video file to an output video file:
    engine(src_video_path, dst_video_path) -> None

1. Uniform Contract (video->video):
   - Separator engine: (video_entrada, video_salida)
     Takes an input video and produces a horizontal MP4 video that already includes the processed/cleaned audio.
   - Cropper engine: (video_entrada, video_salida)
     Takes an input video (typically the clean horizontal video) and produces a vertical MP4 video (9:16 aspect ratio).

2. Engine Resolution:
   - If an override is provided via CLI flag (--separator / --cropper) or environment variable
     (CLEANVIDEOS_SEPARATOR / CLEANVIDEOS_CROPPER):
     The command string is parsed with shlex.split and executed as:
         subprocess.run([*parts, '--input', src, '--output', dst])
     If the subprocess exits with returncode != 0 or the output file is missing after execution,
     an EngineError is raised containing the stderr from the engine process.
   - If no override is provided:
     The default in-process adapters are used:
       - Default separator: audio_cleaner.clean_background_audio (extracts audio, separates vocal stems with Demucs)
         + remux of no_vocals.wav over original video using video_muxer.mux_horizontal.
       - Default cropper: vertical_cropper.build_vertical (pose detection with MediaPipe and smart reframing).
   - Environment variables (CLEANVIDEOS_SEPARATOR, CLEANVIDEOS_CROPPER) MUST be resolved at runtime,
     never at module import time.

3. Failure Handling:
   - Engine failures raise EngineError.
   - process_file propagates the exception (and logs error state in database).
   - cli.main exits with a non-zero exit code and writes a clear error message to stderr.
   - run_watcher catches exceptions per file so that a bad video does not crash the watcher loop.

4. Idempotent Destination-Scoped Skip:
   - Skipping is scoped to the target destination: a file is only skipped if state == 'done'
     AND both expected deliverable files (<output_dir>/video_clean/<name>.mp4 and <output_dir>/vertical/<name>.mp4)
     exist on disk.
   - Passing --force bypasses skip checks and forces reprocessing.

5. Intermediates:
   - All intermediate files (crop.txt, separated stems, raw audio) remain in processing_dir, never in output_dir.

6. Output Routes & Database:
   - Deliverables are placed at <output_dir>/video_clean/<name>.mp4 and <output_dir>/vertical/<name>.mp4.
   - The 'files' table in state.db is maintained with file records, status, and output paths.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Callable

from audio_cleaner import clean_background_audio
from utils import has_audio_stream
from vertical_cropper import build_vertical
from video_muxer import mux_horizontal


class EngineError(RuntimeError):
    """Raised when an external or default engine fails during execution."""
    pass


def run_command_engine(
    cmd_str: str | list[str],
    input_path: str,
    output_path: str,
    engine_name: str = "engine",
) -> None:
    """
    Executes an external command engine following the [ *parts, '--input', src, '--output', dst ] contract.
    """
    if isinstance(cmd_str, str):
        parts = shlex.split(cmd_str)
    else:
        parts = list(cmd_str)

    if not parts:
        raise EngineError(f"Engine command for {engine_name} is empty.")

    full_cmd = [*parts, "--input", str(input_path), "--output", str(output_path)]
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    try:
        proc = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        raise EngineError(f"Failed to execute {engine_name} command {full_cmd}: {exc}") from exc

    if proc.returncode != 0:
        err_msg = proc.stderr.strip() if proc.stderr else f"Process exited with returncode {proc.returncode}"
        if Path(output_path).exists():
            try:
                Path(output_path).unlink()
            except OSError:
                pass
        raise EngineError(f"Engine '{engine_name}' failed (exit code {proc.returncode}): {err_msg}")

    if not Path(output_path).is_file():
        raise EngineError(
            f"Engine '{engine_name}' completed with exit code 0 but output file was not created: {output_path}"
        )


def default_separator_adapter(
    input_path: str,
    output_path: str,
    processing_dir: str | None = None,
    demucs_model: str = "htdemucs",
    hw: int = 3840,
    hh: int = 2160,
) -> None:
    """
    Default in-process separator: extracts audio, cleans vocals via Demucs,
    and remuxes clean audio with original video.
    """
    if processing_dir is None:
        processing_dir = str(Path(output_path).parent)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(processing_dir).mkdir(parents=True, exist_ok=True)

    clean_audio = None
    if has_audio_stream(input_path):
        clean_audio = clean_background_audio(input_path, processing_dir, model=demucs_model)

    mux_horizontal(input_path, clean_audio, output_path, hw, hh)


def default_cropper_adapter(
    input_path: str,
    output_path: str,
    processing_dir: str | None = None,
    vw: int = 2160,
    vh: int = 3840,
    sample_stride: int = 3,
) -> None:
    """
    Default in-process vertical cropper: detects pose landmarks and builds 9:16 vertical video.
    """
    if processing_dir is None:
        processing_dir = str(Path(output_path).parent)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(processing_dir).mkdir(parents=True, exist_ok=True)

    build_vertical(
        input_path,
        clean_audio_path=None,
        processing_dir=processing_dir,
        output_path=output_path,
        out_width=vw,
        out_height=vh,
        sample_stride=sample_stride,
    )


def cut_trimmed_video(
    input_path: str,
    output_path: str,
    start: float,
    end: float,
) -> str:
    """
    Cuts a frame-accurate trimmed intermediate using ffmpeg re-encode.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-to", str(end),
        "-i", str(input_path),
        "-c:v", "libx264",
        "-c:a", "aac",
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not Path(output_path).is_file():
        err_msg = proc.stderr.strip() if proc.stderr else f"ffmpeg exited with code {proc.returncode}"
        raise EngineError(f"Failed to cut trimmed video from {start} to {end}: {err_msg}")
    return str(output_path)


class EngineSeam:
    """Single injectable seam routing engine operations to Demucs/mediapipe/ffmpeg or external subprocess engines."""

    def __init__(
        self,
        separator: str | Callable[..., None] | None = None,
        cropper: str | Callable[..., None] | None = None,
        separate_audio: Callable[..., None] | None = None,
        mux_horizontal: Callable[..., None] | None = None,
        build_vertical: Callable[..., None] | None = None,
    ):
        self._separator = separator
        self._cropper = cropper
        self._legacy_separate_audio = separate_audio
        self._legacy_mux_horizontal = mux_horizontal
        self._legacy_build_vertical = build_vertical

    def cut_trimmed_video(
        self,
        input_path: str,
        output_path: str,
        start: float,
        end: float,
    ) -> str:
        return cut_trimmed_video(input_path, output_path, start, end)

    def separator(
        self,
        input_path: str,
        output_path: str,
        cfg: dict | None = None,
        processing_dir: str | None = None,
    ) -> None:
        override = self._separator if self._separator is not None else os.environ.get("CLEANVIDEOS_SEPARATOR")
        if override:
            if callable(override):
                override(input_path, output_path, cfg=cfg, processing_dir=processing_dir)
            else:
                run_command_engine(override, input_path, output_path, engine_name="separator")
        else:
            model = cfg.get("demucs_model", "htdemucs") if cfg else "htdemucs"
            hw, hh = (cfg.get("horizontal_resolution", [3840, 2160]) if cfg else [3840, 2160])
            default_separator_adapter(
                input_path,
                output_path,
                processing_dir=processing_dir,
                demucs_model=model,
                hw=hw,
                hh=hh,
            )

    def cropper(
        self,
        input_path: str,
        output_path: str,
        cfg: dict | None = None,
        processing_dir: str | None = None,
    ) -> None:
        override = self._cropper if self._cropper is not None else os.environ.get("CLEANVIDEOS_CROPPER")
        if override:
            if callable(override):
                override(input_path, output_path, cfg=cfg, processing_dir=processing_dir)
            else:
                run_command_engine(override, input_path, output_path, engine_name="cropper")
        else:
            vw, vh = (cfg.get("vertical_resolution", [2160, 3840]) if cfg else [2160, 3840])
            default_cropper_adapter(
                input_path,
                output_path,
                processing_dir=processing_dir,
                vw=vw,
                vh=vh,
            )

    @property
    def separate_audio(self):
        return self._legacy_separate_audio or clean_background_audio

    @property
    def mux_horizontal(self):
        return self._legacy_mux_horizontal or mux_horizontal

    @property
    def build_vertical(self):
        return self._legacy_build_vertical or build_vertical

