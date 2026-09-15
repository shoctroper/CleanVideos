# Video Pipeline — limpieza de audio + recorte vertical para DaVinci Resolve

Automatiza la parte pesada de preparar un video para DaVinci Resolve:

1. Quita las voces del audio y deja solo el fondo (música/ambiente).
2. Genera un entregable horizontal en 4K (para YouTube) con ese audio limpio.
3. Genera un entregable vertical 9:16 (para Reels/Shorts) con recorte automático que sigue a la persona en cuadro.

**Lo que NO hace:** no toca DaVinci Resolve directamente. La versión Free de Resolve no permite scripting externo (solo Resolve Studio, de pago), así que el flujo es semi-automático: el pipeline te deja los dos archivos listos en una carpeta y tú los arrastras a Resolve para armar el timeline/grading/exportación final. Ver la sección "Limitaciones" más abajo.

---

## 1. Requisitos

- macOS (probado en Apple Silicon).
- [Homebrew](https://brew.sh) con `ffmpeg` instalado: `brew install ffmpeg`
- Python 3.11 gestionado con [`uv`](https://github.com/astral-sh/uv) (`brew install uv`), o cualquier Python 3.11 de 64 bits.
- Espacio en disco: Demucs y mediapipe descargan modelos (unos cientos de MB) la primera vez que corren.

No hace falta que DaVinci Resolve esté abierto para correr el pipeline — solo lo necesitas después, para importar los resultados.

## 2. Instalación (una sola vez)

```bash
cd CleanVideos/video_pipeline

# Crear el entorno virtual con Python 3.11
uv venv --python 3.11 .venv
source .venv/bin/activate

# Instalar dependencias
uv pip install -r requirements.txt
```

El modelo de detección de persona (`pose_landmarker_lite.task`, ~5.7MB) ya está descargado en `models/`. Si necesitas volver a descargarlo:

```bash
curl -L -o models/pose_landmarker_lite.task \
  "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task"
```

La primera vez que corras el pipeline, Demucs va a descargar el modelo `htdemucs` (~80MB) automáticamente y guardarlo en caché — ese primer video tarda más.

## 3. Estructura de carpetas

```
video_pipeline/
├── config.yaml              # parámetros (resoluciones, modelo de Demucs, etc.)
├── watch_folder/             # <- dejar acá los videos a procesar
├── output/
│   ├── video_clean/          # entregable horizontal 4K, audio sin voces
│   └── vertical/              # entregable vertical 9:16, recorte por persona
├── processing/                # archivos intermedios por video (audio separado, crop.txt, etc.)
├── logs/pipeline.log          # log de cada corrida
├── state.db                   # SQLite: qué videos ya se procesaron (evita reprocesar)
└── scripts/                   # código del pipeline
```

## 4. Uso

Siempre con el entorno activado:

```bash
cd CleanVideos/video_pipeline
source .venv/bin/activate
```

### Modo A — un solo archivo (recomendado para probar)

```bash
python scripts/main.py /ruta/a/tu/video.mp4
```

Procesa ese archivo puntual y termina. Al finalizar vas a ver en consola (y en `logs/pipeline.log`) algo como:

```
[mi_video] Listo. Importar manualmente a Resolve:
  .../output/video_clean/mi_video.mp4
  .../output/vertical/mi_video.mp4
```

### Modo B — carpeta vigilada (watch folder)

```bash
python scripts/main.py
```

Sin argumentos, el script se queda corriendo y vigila `watch_folder/`. Cada vez que sueltes (o copies) un video ahí:

1. Espera 10 segundos sin cambios en el archivo antes de procesarlo (para no agarrarlo a mitad de una copia larga).
2. Calcula un hash SHA-256 y lo guarda en `state.db` — si ya procesaste ese archivo exacto antes, lo ignora.
3. Corre el pipeline completo y deja los resultados en `output/`.

Para cortarlo: `Ctrl+C`.

Formatos de video aceptados por el watcher: `.mp4`, `.mov`, `.mkv`, `.mxf`, `.m4v` (configurable en `config.yaml`).

### Qué hacer con los resultados

1. Abre (o crea) tu proyecto en DaVinci Resolve.
2. Arrastra `output/video_clean/<nombre>.mp4` a un timeline nuevo → ese es tu entregable horizontal, expórtalo a 4K para YouTube (ya viene en 3840x2160 con el audio de fondo, sin voces).
3. Arrastra `output/vertical/<nombre>.mp4` a otro timeline → ya viene recortado en 9:16 (2160x3840) siguiendo a la persona, listo para exportar a Reels/Shorts.

No hace falta reencuadrar ni tocar el audio en Resolve — el pipeline ya dejó ambos archivos en su resolución y aspect ratio final. Resolve queda solo para grading, títulos, o cualquier edición adicional que quieras hacerle a mano.

## 5. Configuración (`config.yaml`)

| Clave | Qué hace |
|---|---|
| `demucs_model` | Modelo de separación de voz. `htdemucs` (default) es el más balanceado. |
| `horizontal_resolution` | Resolución del entregable horizontal, `[ancho, alto]`. Default `[3840, 2160]` (4K). |
| `vertical_resolution` | Resolución del entregable vertical. Default `[2160, 3840]` (9:16 en 4K). |
| `stable_wait_seconds` | Segundos sin cambios en el archivo antes de considerarlo "listo" en el watcher. |
| `video_extensions` | Extensiones que el watcher reconoce como video. |

## 6. Cómo funciona por dentro (resumen)

Para cada video:

1. **`audio_cleaner.py`** — extrae el audio con `ffmpeg`, corre Demucs (`--two-stems=vocals`) y se queda con el stem `no_vocals.wav` (todo lo que no es voz: música, ambiente).
2. **`video_muxer.py`** — remuxa el video original con ese audio limpio. Si la fuente es menor a la resolución objetivo (p. ej. no es nativamente 4K), la escala explícitamente — ojo, esto es upscale, no agrega detalle real, solo entrega el archivo en la resolución pedida.
3. **`vertical_cropper.py`** — detecta la posición de la persona en cuadro con `mediapipe` (Pose Landmarker), suaviza el movimiento, y genera un recorte 9:16 dinámico con `ffmpeg` que sigue a la persona horizontalmente. Si no detecta a nadie, hace un recorte centrado fijo.
4. **`watcher.py` / `main.py`** — vigilan la carpeta, evitan reprocesar archivos ya hechos (`state.db`), y orquestan los tres pasos anteriores.

## 7. Limitaciones conocidas

- **No hay integración con la API de Resolve.** Se intentó y se confirmó que "External scripting" está restringido a Resolve Studio en la versión Free — por eso el último paso (armar timeline y exportar) es manual.
- **La separación de voz no es perfecta.** Demucs (`htdemucs`) hace un buen trabajo, pero en video con mucho ruido de fondo o voces superpuestas a la música puede dejar residuos o afectar la calidad del audio de fondo.
- **El recorte vertical depende de que mediapipe detecte a la persona.** Si no hay nadie en cuadro, o la detección falla (mala iluminación, persona muy pequeña en el encuadre, etc.), cae a un recorte centrado fijo — no sigue a nadie en ese tramo.
- **Upscaling ≠ más detalle.** Si tu material fuente no es 4K nativo, el entregable horizontal se escala a 4K por compatibilidad, pero no gana nitidez real.
- **Verificar licencia de Demucs antes de uso comercial.** Los pesos del modelo `htdemucs` tienen su propia licencia; revisarla si el video resultante se va a monetizar.
- **Rendimiento:** Demucs y mediapipe corren en CPU (Apple Silicon). Videos largos (>10 min) pueden tardar varios minutos por etapa.

## 8. Troubleshooting

- **"No se encontró stream de video"**: el archivo puede estar corrupto o el `ffprobe` no reconoce el formato. Verifica con `ffprobe tu_video.mp4`.
- **Demucs muy lento la primera vez**: está descargando el modelo (~80MB), es normal solo la primera corrida.
- **El watcher no detecta el archivo**: confirma que la extensión está en `video_extensions` en `config.yaml`, y que esperaste los `stable_wait_seconds` configurados (10s por default) sin que el archivo siga copiándose.
- **Un archivo quedó en estado `error` en `state.db`**: revisa `logs/pipeline.log` para el traceback completo. Podés inspeccionar el estado con:
  ```bash
  sqlite3 state.db "select id, file_path, state, error from files;"
  ```

---

## Estado probado (verificado 2026-09-15, AL-DÍA Fase A)

| Capacidad | Estado | Evidencia |
|---|---|---|
| Separación de voz/fondo con Demucs (`htdemucs`) | Corrió el 2026-08-12, no re-ejecutada | `state.db`: 1 archivo `done` (2026-08-12 20:22 UTC); `logs/pipeline.log` |
| Entregable horizontal 4K con audio limpio | Corrió el 2026-08-12 | `output/video_clean/test_full.mp4` |
| Entregable vertical 9:16 con recorte que sigue a la persona (mediapipe) | Corrió el 2026-08-12 | `output/vertical/test_full.mp4` |
| Vigilancia de carpeta y estado persistente | Implementado | `scripts/watcher.py`, `scripts/database.py` |
| Tests automatizados | **No hay** | — |

Sólo hay una corrida registrada, sobre un vídeo de prueba; «funciona hoy» no está demostrado. Datos de ejecución (`state.db`, `logs/`, `processing/`, `output/`, `watch_folder/`) y modelos (`models/`) no se versionan.

**Dependencias reales:** Python 3.11, FFmpeg, Demucs (descarga `htdemucs`), mediapipe (`models/pose_landmarker_lite.task`). **DaVinci Resolve 21.0.1 Free:** sin scripting externo, así que la importación a Resolve es manual.
