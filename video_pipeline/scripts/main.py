from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path

# Ensure scripts directory is in sys.path for flat imports
scripts_dir = Path(__file__).resolve().parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from config import load_config
from database import Database
from engines import EngineError, EngineSeam
from utils import sha256_file, setup_logging, get_video_duration
from watcher import start_watcher


def process_file(
    video_path: str,
    cfg: dict,
    db: Database,
    logger,
    engines: EngineSeam | None = None,
    force: bool = False,
    trim: tuple[float, float] | None = None,
) -> None:
    if engines is None:
        engines = EngineSeam()

    # Pre-validation for trim option before any work is performed or DB updated
    if trim is not None:
        start, end = trim
        if end <= start:
            raise ValueError(f"Invalid trim range: end ({end}) must be greater than start ({start})")
        if start < 0:
            raise ValueError(f"Invalid trim range: start ({start}) cannot be negative")
        input_duration = get_video_duration(video_path)
        if end > input_duration + 0.25:
            raise ValueError(
                f"Invalid trim range: end ({end:.2f}s) exceeds input duration ({input_duration:.2f}s + 0.25s tolerance)"
            )

    file_hash = sha256_file(video_path)
    name = Path(video_path).stem

    if trim is not None:
        start, end = trim
        deliverable_name = f"{name}_trim{start}-{end}"
        state_key = f"{file_hash}_trim{start}-{end}"
    else:
        deliverable_name = name
        state_key = file_hash

    horizontal_out = str(Path(cfg["output_dir"]) / "video_clean" / f"{deliverable_name}.mp4")
    vertical_out = str(Path(cfg["output_dir"]) / "vertical" / f"{deliverable_name}.mp4")

    # Destination-scoped idempotent skip:
    # Only skip if not force, DB state is 'done', AND both deliverables for this output_dir exist on disk.
    if not force and db.is_done(state_key) and Path(horizontal_out).is_file() and Path(vertical_out).is_file():
        logger.info(f"Ya procesado (hash conocido y entregables existen en {cfg['output_dir']}), se omite: {video_path}")
        return

    existing = db.get_by_hash(state_key)
    file_id = existing[0] if existing else db.insert_file(video_path, state_key, state="pending")
    db.update_state(file_id, "processing")

    file_processing_dir = str(Path(cfg["processing_dir"]) / deliverable_name)
    Path(file_processing_dir).mkdir(parents=True, exist_ok=True)

    try:
        logger.info(f"[{deliverable_name}] Procesando {video_path}")

        if trim is not None:
            start, end = trim
            trimmed_intermediate = str(Path(file_processing_dir) / f"{deliverable_name}_intermediate.mp4")
            logger.info(f"[{deliverable_name}] Creando corte temporal {start}:{end} -> {trimmed_intermediate}")
            engines.cut_trimmed_video(video_path, trimmed_intermediate, start, end)
            source_video = trimmed_intermediate
        else:
            source_video = video_path

        logger.info(f"[{deliverable_name}] Ejecutando motor separador -> {horizontal_out}")
        engines.separator(source_video, horizontal_out, cfg=cfg, processing_dir=file_processing_dir)

        logger.info(f"[{deliverable_name}] Ejecutando motor cropper -> {vertical_out}")
        engines.cropper(horizontal_out, vertical_out, cfg=cfg, processing_dir=file_processing_dir)

        db.set_outputs(file_id, horizontal_out, vertical_out)
        db.update_state(file_id, "done")
        logger.info(f"[{deliverable_name}] Listo. Entregables generados:\n  {horizontal_out}\n  {vertical_out}")

    except Exception as exc:
        logger.exception(f"[{deliverable_name}] Error procesando {video_path}: {exc}")
        db.update_state(file_id, "error", error=str(exc))
        raise



def run_watcher(cfg: dict, db: Database, logger, engines: EngineSeam | None = None) -> None:
    if engines is None:
        engines = EngineSeam()
    work_queue: queue.Queue[str] = queue.Queue()

    def worker():
        while True:
            path = work_queue.get()
            try:
                process_file(path, cfg, db, logger, engines=engines)
            except Exception as exc:
                logger.error(f"Watcher error procesando {path}: {exc}")
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
    engines = EngineSeam()

    if len(sys.argv) > 1:
        # Modo prueba: procesa un archivo puntual sin levantar el watcher.
        try:
            process_file(sys.argv[1], cfg, db, logger, engines=engines)
        except Exception as exc:
            logger.error(f"Error procesando archivo: {exc}")
            sys.exit(1)
    else:
        run_watcher(cfg, db, logger, engines=engines)
