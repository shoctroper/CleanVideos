#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F6.5 — CleanVideos: pipeline probable sin GPU, con ediciones (pinned).

QUE MIDE Y POR QUE
------------------
Mario: "que sean proyectos funcionales, quiero poder probar metiendo ediciones".
CleanVideos (video_pipeline) limpia voz con Demucs y produce entregables horizontal
4K + vertical 9:16 con seguimiento de persona (mediapipe). Verificado 2026-09-25:
  - NO tiene NI UNA prueba automatizada (`find . -name "test_*.py"` vacio fuera de .venv).
  - Solo funciona como watcher de carpeta: no hay forma de correr un archivo suelto y
    ver el resultado, ni de repetir una corrida sobre el mismo archivo.
  - Demucs y mediapipe tardan minutos y bajan modelos de cientos de MB: cualquier
    prueba que los invoque de verdad es inusable como compuerta.

Por eso esta fase exige SEPARAR la orquestacion de los motores pesados: los motores
se inyectan, y la compuerta prueba la orquestacion completa con motores falsos RAPIDOS
pero que se comportan como los reales (escriben archivos de verdad con ffmpeg, respetan
rutas y codigos de salida). Leccion del handoff §6: un falso instantaneo que no se
parece al real deja pasar defectos; estos falsos producen medios reales.

CHECKS (C1..C10). Imprime {"f6_5_cleanvideos": 0|1, "progress": N, "of": 10, ...}
C1  Existe video_pipeline/scripts/cli.py con `--input ARCHIVO --output DIR` para correr
    UN archivo sin watcher, y `--help` funciona.
C2  Los motores pesados son inyectables: existe una forma documentada de sustituir
    separacion de voz y recorte vertical (p.ej. `--separator`/`--cropper` o variables
    CLEANVIDEOS_SEPARATOR/_CROPPER) SIN editar el codigo.
C3  Corrida end-to-end con motores falsos: produce entregable horizontal y vertical,
    ambos con duracion > 0 segun ffprobe.
C4  El vertical tiene relacion de aspecto 9:16 (ancho/alto ~= 0.5625, tolerancia 2%).
C5  IDEMPOTENTE: repetir la corrida no rehace lo ya hecho y no corrompe la salida.
C6  EDICIONES: se puede pedir un recorte temporal (`--trim INICIO:FIN`) y el entregable
    resultante tiene esa duracion (tolerancia 0.5 s); sin --trim la duracion es la del
    original.
C7  Un --trim invalido (fin <= inicio, o fuera del video) se rechaza con exit != 0 y
    mensaje claro, sin dejar un archivo corrupto en output.
C8  ERROR DE MOTOR: si el separador falla, el pipeline NO deja un entregable a medias
    presentado como bueno: o aborta con exit != 0, o marca el resultado como degradado
    en el estado. Un archivo de salida silenciosamente mudo es el fallo peor.
C9  Estado consultable: tras la corrida, state.db (o un manifest.json) registra el
    archivo con su estado final y las rutas producidas.
C10 Existe video_pipeline/tests/test_pipeline.py y pasa; la suite completa NO invoca
    Demucs ni mediapipe reales (se verifica que corre en menos de 120 s).

CONTRATO: SIEMPRE exit 0; el veredicto viaja en la metrica (GOV descarta
la lectura si el proceso sale != 0).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]      # .../CleanVideos
VP = REPO / "video_pipeline"
FAILED = []
PROGRESS = 0
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
PY = sys.executable


def check(name, fn):
    global PROGRESS
    try:
        fn()
        PROGRESS += 1
    except Exception as exc:  # noqa: BLE001
        FAILED.append("%s: %s" % (name, exc))


def _mkvideo(path, seconds=6, w=1920, h=1080):
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i",
                    "testsrc=size=%dx%d:rate=25:duration=%d" % (w, h, seconds),
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=%d" % seconds,
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    "-shortest", str(path)], capture_output=True, timeout=240, check=True)


def _probe(path, entries):
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", entries, "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=60)
    return json.loads(r.stdout or "{}")


def _duration(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True, timeout=60)
    return float((r.stdout or "0").strip() or 0)


def _dims(path):
    d = _probe(path, "stream=width,height")
    st = (d.get("streams") or [{}])[0]
    return int(st.get("width") or 0), int(st.get("height") or 0)


# ---- motores falsos que se comportan como los reales (escriben medios de verdad) ----
FAKE_SEP = """#!/usr/bin/env python3
import subprocess, sys, argparse
p = argparse.ArgumentParser(); p.add_argument('--input'); p.add_argument('--output')
a, _ = p.parse_known_args()
subprocess.run(['ffmpeg','-y','-i',a.input,'-af','volume=0.3','-c:v','copy',a.output],
               capture_output=True, check=True)
"""
FAKE_SEP_FAIL = """#!/usr/bin/env python3
import sys
sys.stderr.write('separador caido\\n'); sys.exit(3)
"""
FAKE_CROP = """#!/usr/bin/env python3
import subprocess, sys, argparse
p = argparse.ArgumentParser(); p.add_argument('--input'); p.add_argument('--output')
a, _ = p.parse_known_args()
subprocess.run(['ffmpeg','-y','-i',a.input,'-vf',
                'crop=ih*9/16:ih,scale=1080:1920','-c:a','copy',a.output],
               capture_output=True, check=True)
"""


def _fakes(td):
    d = Path(td) / "fakes"
    d.mkdir(exist_ok=True)
    sep, crop = d / "sep.py", d / "crop.py"
    sep.write_text(FAKE_SEP, encoding="utf-8")
    crop.write_text(FAKE_CROP, encoding="utf-8")
    for f in (sep, crop):
        os.chmod(f, 0o755)
    return sep, crop


def _run_cli(workdir, inp, out, extra=None, env_extra=None, timeout=900):
    sep, crop = _fakes(workdir)
    env = dict(os.environ,
               CLEANVIDEOS_SEPARATOR="%s %s" % (PY, sep),
               CLEANVIDEOS_CROPPER="%s %s" % (PY, crop))
    if env_extra:
        env.update(env_extra)
    argv = [PY, str(VP / "scripts" / "cli.py"), "--input", str(inp), "--output", str(out)]
    if extra:
        argv += extra
    return subprocess.run(argv, capture_output=True, text=True, cwd=str(VP),
                          env=env, timeout=timeout)


def c1():
    cli = VP / "scripts" / "cli.py"
    assert cli.exists(), "falta video_pipeline/scripts/cli.py (correr UN archivo sin watcher)"
    r = subprocess.run([PY, str(cli), "--help"], capture_output=True, text=True,
                       cwd=str(VP), timeout=120)
    assert r.returncode == 0, "cli.py --help fallo: %s" % r.stderr[-300:]
    for flag in ("--input", "--output"):
        assert flag in r.stdout, "cli.py no expone %s" % flag


def c2():
    blob = ""
    for f in (VP / "scripts").rglob("*.py"):
        if ".venv" in str(f):
            continue
        blob += f.read_text(encoding="utf-8", errors="ignore")
    assert ("CLEANVIDEOS_SEPARATOR" in blob or "--separator" in blob), \
        "el separador de voz no es inyectable (obliga a correr Demucs real)"
    assert ("CLEANVIDEOS_CROPPER" in blob or "--cropper" in blob), \
        "el recorte vertical no es inyectable (obliga a correr mediapipe real)"


def c3():
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "clip.mp4"
        _mkvideo(inp)
        out = Path(td) / "out"
        r = _run_cli(td, inp, out)
        assert r.returncode == 0, "corrida fallo: %s" % ((r.stderr or r.stdout)[-600:])
        vids = [p for p in out.rglob("*.mp4")]
        assert len(vids) >= 2, "esperaba horizontal + vertical, hay %d: %r" % (
            len(vids), [p.name for p in vids])
        for v in vids:
            assert _duration(v) > 0, "entregable con duracion 0: %s" % v.name


def c4():
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "clip.mp4"
        _mkvideo(inp)
        out = Path(td) / "out"
        r = _run_cli(td, inp, out)
        assert r.returncode == 0, "la corrida base fallo: %s" % ((r.stderr or r.stdout)[-400:])
        verticales = [p for p in out.rglob("*.mp4")
                      if "vert" in p.name.lower() or "vert" in str(p.parent).lower()]
        assert verticales, "no encuentro el entregable vertical por nombre/carpeta"
        w, h = _dims(verticales[0])
        assert h > 0 and abs((w / h) - 0.5625) < 0.02, \
            "el vertical no es 9:16: %dx%d (ratio %.4f)" % (w, h, (w / h) if h else 0)


def c5():
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "clip.mp4"
        _mkvideo(inp)
        out = Path(td) / "out"
        assert _run_cli(td, inp, out).returncode == 0, "primera corrida fallo"
        first = {p.name: p.stat().st_mtime for p in out.rglob("*.mp4")}
        r2 = _run_cli(td, inp, out)
        assert r2.returncode == 0, "segunda corrida fallo: %s" % r2.stderr[-400:]
        second = {p.name: p.stat().st_mtime for p in out.rglob("*.mp4")}
        assert set(first) == set(second), "la segunda corrida cambio el set de entregables"
        rehizo = [n for n in first if second[n] != first[n]]
        assert not rehizo, "no es idempotente: rehizo %r" % rehizo[:3]


def c6():
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "clip.mp4"
        _mkvideo(inp, seconds=8)
        out = Path(td) / "out"
        r = _run_cli(td, inp, out, ["--trim", "1.0:4.0"])
        assert r.returncode == 0, "la corrida con --trim fallo: %s" % (r.stderr[-500:])
        vids = list(out.rglob("*.mp4"))
        assert vids, "no produjo entregables con --trim"
        for v in vids:
            d = _duration(v)
            assert abs(d - 3.0) < 0.5, \
                "--trim 1.0:4.0 debia dar ~3.0 s, %s dura %.2f s" % (v.name, d)


def c7():
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "clip.mp4"
        _mkvideo(inp, seconds=6)
        out = Path(td) / "out"
        base = _run_cli(td, inp, out)
        assert base.returncode == 0, \
            "la corrida base debe funcionar antes de juzgar --trim invalidos"
        r = _run_cli(td, inp, Path(td) / "out_bad", ["--trim", "5.0:2.0"])
        assert r.returncode != 0, "acepto un --trim con fin <= inicio"
        r2 = _run_cli(td, inp, Path(td) / "out2", ["--trim", "1.0:99.0"])
        assert r2.returncode != 0, "acepto un --trim mas alla del final del video"


def c8():
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "clip.mp4"
        _mkvideo(inp)
        out = Path(td) / "out"
        bad = Path(td) / "fakes"
        bad.mkdir(exist_ok=True)
        sepbad = bad / "sepbad.py"
        sepbad.write_text(FAKE_SEP_FAIL, encoding="utf-8")
        sano = _run_cli(td, inp, Path(td) / "out_ok")
        assert sano.returncode == 0, \
            "la corrida sana debe funcionar antes de probar el fallo del separador"
        r = _run_cli(td, inp, out,
                     env_extra={"CLEANVIDEOS_SEPARATOR": "%s %s" % (PY, sepbad)})
        if r.returncode == 0:
            # si no aborta, TIENE que declarar la degradacion en algun estado
            blob = ""
            for m in list(out.rglob("*.json")) + list(out.rglob("*.log")):
                blob += m.read_text(encoding="utf-8", errors="ignore")
            assert re.search(r"degrad|fallback|error|failed", blob, re.I), \
                "el separador fallo y el pipeline entrego sin declarar la degradacion"


def c9():
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "clip.mp4"
        _mkvideo(inp)
        out = Path(td) / "out"
        r = _run_cli(td, inp, out)
        assert r.returncode == 0, "la corrida base fallo: %s" % ((r.stderr or r.stdout)[-400:])
        manifests = list(out.rglob("manifest.json"))
        if manifests:
            data = json.loads(manifests[0].read_text(encoding="utf-8"))
            assert data, "manifest.json vacio"
            assert "clip" in json.dumps(data), \
                "el manifest no menciona el archivo procesado"
            return
        db = VP / "state.db"
        assert db.exists(), "ni manifest.json ni state.db: el estado no es consultable"
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        rows = con.execute("select * from files").fetchall()
        con.close()
        assert any("clip" in str(rw) for rw in rows), \
            "state.db no registro el archivo recien procesado"


def c10():
    t = VP / "tests" / "test_pipeline.py"
    assert t.exists(), "falta video_pipeline/tests/test_pipeline.py"
    t0 = time.time()
    r = subprocess.run([PY, "-m", "pytest", "-q", str(VP / "tests"), "-p", "no:cacheprovider"],
                       capture_output=True, text=True, cwd=str(VP), timeout=600)
    elapsed = time.time() - t0
    assert r.returncode == 0, "la suite esta en rojo: %s" % (r.stdout[-500:])
    assert elapsed < 120, \
        "la suite tarda %.0f s: esta invocando Demucs/mediapipe reales" % elapsed


if not FFMPEG or not FFPROBE:
    print(json.dumps({"f6_5_cleanvideos": 0, "progress": 0, "of": 10,
                      "failed": ["ffmpeg/ffprobe no estan en PATH"]}, ensure_ascii=False))
    sys.exit(0)

for nm, fn in [("C1", c1), ("C2", c2), ("C3", c3), ("C4", c4), ("C5", c5),
               ("C6", c6), ("C7", c7), ("C8", c8), ("C9", c9), ("C10", c10)]:
    check(nm, fn)

metric = 1 if PROGRESS == 10 else 0
print(json.dumps({"f6_5_cleanvideos": metric, "progress": PROGRESS,
                  "of": 10, "failed": FAILED}, ensure_ascii=False))
# GOV lee la metrica SOLO si el evaluador sale 0 (GovernanceOs/goal/evaluator.py:36:
# exit_code != 0 -> ok=False, observed=None, reason=EVALUATOR_ERROR). Salir 1 en rojo
# tira el progreso a la basura y deja al planificador ciego: no sabria que subio de
# 4/11 a 7/11 ni que checks faltan. El veredicto viaja en la metrica, no en el exit code.
sys.exit(0)
