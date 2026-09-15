import subprocess
from pathlib import Path

from utils import get_video_resolution_fps


def mux_horizontal(
    video_path: str,
    clean_audio_path: str | None,
    output_path: str,
    target_width: int,
    target_height: int,
) -> None:
    """
    Remuxa el video original con el audio de fondo limpio (o sin audio si
    clean_audio_path es None). Si la fuente es menor a target_width x
    target_height, hace upscale explicito (corrección plan #8) -- esto NO
    agrega detalle real, solo entrega un archivo que cumple la resolución
    pedida para el entregable "4K".
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    src_w, src_h, _fps = get_video_resolution_fps(video_path)
    needs_upscale = src_w < target_width or src_h < target_height

    cmd = ["ffmpeg", "-y", "-i", video_path]
    if clean_audio_path:
        cmd += ["-i", clean_audio_path]

    if needs_upscale:
        cmd += ["-vf", f"scale={target_width}:{target_height}:flags=lanczos"]
        video_codec = ["-c:v", "libx264", "-preset", "medium", "-crf", "16"]
    else:
        video_codec = ["-c:v", "copy"]

    cmd += video_codec

    if clean_audio_path:
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]

    cmd += [output_path]
    subprocess.run(cmd, check=True, capture_output=True)
