import subprocess
from pathlib import Path


def extract_audio(video_path: str, output_wav: str) -> None:
    Path(output_wav).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
        output_wav,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def separate_vocals(audio_path: str, separation_dir: str, model: str = "htdemucs") -> str:
    """
    Corre Demucs con --two-stems=vocals. Devuelve la ruta al stem
    'no_vocals.wav' (el fondo/musica/ambiente sin las voces), que es
    el que el resto del pipeline usa como audio limpio.
    """
    Path(separation_dir).mkdir(parents=True, exist_ok=True)
    cmd = [
        "demucs", f"--two-stems=vocals", "-n", model,
        "-o", separation_dir,
        audio_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)

    stem_name = Path(audio_path).stem
    no_vocals = Path(separation_dir) / model / stem_name / "no_vocals.wav"
    if not no_vocals.exists():
        raise RuntimeError(f"Demucs no generó {no_vocals}")
    return str(no_vocals)


def clean_background_audio(video_path: str, processing_dir: str, model: str = "htdemucs") -> str:
    """Orquesta extract_audio + separate_vocals. Devuelve la ruta al WAV de fondo limpio."""
    raw_wav = str(Path(processing_dir) / "audio_raw.wav")
    extract_audio(video_path, raw_wav)
    return separate_vocals(raw_wav, processing_dir, model=model)
