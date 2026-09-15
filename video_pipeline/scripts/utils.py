import hashlib
import json
import logging
import subprocess
from pathlib import Path


def setup_logging(logs_dir: str) -> logging.Logger:
    Path(logs_dir).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("video_pipeline")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(Path(logs_dir) / "pipeline.log")
        sh = logging.StreamHandler()
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        fh.setFormatter(fmt)
        sh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(sh)
    return logger


def sha256_file(path: str, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def is_video_file(path: str, extensions: list[str]) -> bool:
    return Path(path).suffix.lower() in extensions


def ffprobe_stream_info(path: str) -> dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        path,
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def has_audio_stream(path: str) -> bool:
    info = ffprobe_stream_info(path)
    return any(s.get("codec_type") == "audio" for s in info.get("streams", []))


def get_video_resolution_fps(path: str) -> tuple[int, int, float]:
    info = ffprobe_stream_info(path)
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            width = int(s["width"])
            height = int(s["height"])
            num, den = s.get("r_frame_rate", "30/1").split("/")
            fps = float(num) / float(den) if float(den) != 0 else 30.0
            return width, height, fps
    raise RuntimeError(f"No se encontró stream de video en {path}")
