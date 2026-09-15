import subprocess
from pathlib import Path

import cv2
import mediapipe as mp
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python import vision as mp_vision

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
POSE_MODEL_PATH = MODELS_DIR / "pose_landmarker_lite.task"


def detect_person_centers(video_path: str, sample_stride: int = 3) -> tuple[list[float | None], float, int, int]:
    """
    Recorre el video y devuelve, para cada frame muestreado, el centro X
    normalizado (0..1) de la persona detectada (o None si no se detectó).
    Solo procesa 1 de cada `sample_stride` frames por rendimiento; el resto
    se interpola en generate_sendcmd.
    """
    options = mp_vision.PoseLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(POSE_MODEL_PATH)),
        running_mode=mp_vision.RunningMode.VIDEO,
        num_poses=1,
    )
    landmarker = mp_vision.PoseLandmarker.create_from_options(options)

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    centers: list[float | None] = []
    frame_idx = 0
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            if frame_idx % sample_stride == 0:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                timestamp_ms = int((frame_idx / fps) * 1000)
                result = landmarker.detect_for_video(mp_image, timestamp_ms)
                if result.pose_landmarks:
                    xs = [lm.x for lm in result.pose_landmarks[0]]
                    centers.append((min(xs) + max(xs)) / 2)
                else:
                    centers.append(None)
            frame_idx += 1
    finally:
        cap.release()
        landmarker.close()

    return centers, fps, width, height


def _fill_and_smooth(centers: list[float | None], smoothing_window: int = 5) -> list[float]:
    """Rellena huecos (None) sosteniendo el último valor conocido (o 0.5 si
    no hubo ninguna detección todavía) y aplica una media móvil simple."""
    filled: list[float] = []
    last = 0.5
    for c in centers:
        if c is not None:
            last = c
        filled.append(last)

    if all(c is None for c in centers):
        return [0.5] * len(centers)

    smoothed = []
    for i in range(len(filled)):
        lo = max(0, i - smoothing_window // 2)
        hi = min(len(filled), i + smoothing_window // 2 + 1)
        window = filled[lo:hi]
        smoothed.append(sum(window) / len(window))
    return smoothed


def generate_sendcmd(
    centers: list[float | None],
    sample_stride: int,
    fps: float,
    src_width: int,
    crop_w: int,
    output_txt: str,
    change_threshold_px: int = 8,
) -> None:
    """
    Escribe un archivo sendcmd para ffmpeg. Corrige el bug del borrador
    original: el timestamp se calcula en SEGUNDOS (idx * sample_stride / fps),
    no como índice de frame crudo. Además solo emite una línea cuando el
    crop-x cambia más de `change_threshold_px`, para no generar miles de
    líneas redundantes.
    """
    smoothed = _fill_and_smooth(centers)

    Path(output_txt).parent.mkdir(parents=True, exist_ok=True)
    last_x = None
    with open(output_txt, "w") as f:
        for i, center_norm in enumerate(smoothed):
            timestamp_sec = (i * sample_stride) / fps
            cx_px = center_norm * src_width
            x = int(max(0, min(src_width - crop_w, cx_px - crop_w / 2)))

            if last_x is None or abs(x - last_x) >= change_threshold_px:
                f.write(f"{timestamp_sec:.3f} crop x {x};\n")
                last_x = x


def crop_vertical(
    video_path: str,
    clean_audio_path: str | None,
    crop_txt: str,
    output_path: str,
    crop_w: int,
    crop_h: int,
    out_width: int = 2160,
    out_height: int = 3840,
) -> None:
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    cmd = ["ffmpeg", "-y", "-i", video_path]
    if clean_audio_path:
        cmd += ["-i", clean_audio_path]

    filter_complex = (
        f"[0:v]sendcmd=f={crop_txt},"
        f"crop=w={crop_w}:h={crop_h}:x=0:y=0,"
        f"scale={out_width}:{out_height}:flags=lanczos[v]"
    )
    cmd += ["-filter_complex", filter_complex, "-map", "[v]"]

    if clean_audio_path:
        cmd += ["-map", "1:a", "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]

    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", output_path]
    subprocess.run(cmd, check=True, capture_output=True)


def build_vertical(
    video_path: str,
    clean_audio_path: str | None,
    processing_dir: str,
    output_path: str,
    out_width: int = 2160,
    out_height: int = 3840,
    sample_stride: int = 3,
) -> None:
    centers, fps, src_w, src_h = detect_person_centers(video_path, sample_stride=sample_stride)

    crop_w = round(src_h * out_width / out_height)
    crop_w = min(crop_w, src_w)
    crop_h = src_h

    crop_txt = str(Path(processing_dir) / "crop.txt")
    generate_sendcmd(centers, sample_stride, fps, src_w, crop_w, crop_txt)

    crop_vertical(video_path, clean_audio_path, crop_txt, output_path, crop_w, crop_h, out_width, out_height)
