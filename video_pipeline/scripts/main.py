import queue
import sys
import threading
import time
from pathlib import Path

from audio_cleaner import clean_background_audio
from config import load_config
from database import Database
from utils import has_audio_stream, sha256_file, setup_logging
from vertical_cropper import build_vertical
from video_muxer import mux_horizontal
from watcher import start_watcher


def process_file(video_path: str, cfg: dict, db: Database, logger) -> None:
    file_hash = sha256_file(video_path)

    if db.is_done(file_hash):
        logger.info(f"Ya procesado (hash conocido), se omite: {video_path}")
        return

    existing = db.get_by_hash(file_hash)
    file_id = existing[0] if existing else db.insert_file(video_path, file_hash, state="pending")
    db.update_state(file_id, "processing")

    name = Path(video_path).stem
    file_processing_dir = str(Path(cfg["processing_dir"]) / name)
    Path(file_processing_dir).mkdir(parents=True, exist_ok=True)

    try:
        logger.info(f"[{name}] Procesando {video_path}")

        clean_audio = None
        if has_audio_stream(video_path):
            logger.info(f"[{name}] Separando voces con Demucs...")
            clean_audio = clean_background_audio(video_path, file_processing_dir, model=cfg["demucs_model"])
        else:
            logger.info(f"[{name}] Sin pista de audio, se omite separación de voces")

        hw, hh = cfg["horizontal_resolution"]
        horizontal_out = str(Path(cfg["output_dir"]) / "video_clean" / f"{name}.mp4")
        logger.info(f"[{name}] Generando entregable horizontal ({hw}x{hh})...")
        mux_horizontal(video_path, clean_audio, horizontal_out, hw, hh)

        vw, vh = cfg["vertical_resolution"]
        vertical_out = str(Path(cfg["output_dir"]) / "vertical" / f"{name}.mp4")
        logger.info(f"[{name}] Generando entregable vertical con recorte de persona ({vw}x{vh})...")
        build_vertical(video_path, clean_audio, file_processing_dir, vertical_out, out_width=vw, out_height=vh)

        db.set_outputs(file_id, horizontal_out, vertical_out)
        db.update_state(file_id, "done")
        logger.info(f"[{name}] Listo. Importar manualmente a Resolve:\n  {horizontal_out}\n  {vertical_out}")

    except Exception as exc:
        logger.exception(f"[{name}] Error procesando {video_path}: {exc}")
        db.update_state(file_id, "error", error=str(exc))


def run_watcher(cfg: dict, db: Database, logger) -> None:
    work_queue: queue.Queue[str] = queue.Queue()

    def worker():
        while True:
            path = work_queue.get()
            try:
                process_file(path, cfg, db, logger)
            finally:
                work_queue.task_done()

    threading.Thread(target=worker, daemon=True).start()

    observer = start_watcher(
        cfg["watch_folder"],
        cfg["video_extensions"],
        cfg["stable_wait_seconds"],
        on_stable=work_queue.put,
    )
    logger.info(f"Vigilando {cfg['watch_folder']} ...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    cfg = load_config()
    logger = setup_logging(cfg["logs_dir"])
    db = Database(cfg["state_db"])

    if len(sys.argv) > 1:
        # Modo prueba: procesa un archivo puntual sin levantar el watcher.
        process_file(sys.argv[1], cfg, db, logger)
    else:
        run_watcher(cfg, db, logger)
