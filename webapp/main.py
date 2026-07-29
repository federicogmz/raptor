#!/usr/bin/env python3
"""
Backend web de RAPTOR: subida de fotos crudas + selección de sensores/modo
+ progreso en vivo + geovisor, todo en un solo proceso.

No reimplementa nada del pipeline — reusa tal cual core/scan.py y
core/runner.py (activate_mission, PipelineRun, parse_progress_events), que ya
corren docker/entrypoint.sh como subprocess y exponen progreso estructurado
(ver PROGRESS_FILE en scripts/progress.py). core/ vivía como tui/ cuando
existía además una interfaz de terminal (Textual) — se retiró (jul 2026,
la webapp la reemplaza por completo) y este módulo quedó como lo que
siempre fue en realidad: el orquestador del pipeline, sin ninguna UI
propia.

Los tres sensores son OPCIONALES e independientes: se puede volar solo el
M3T/H20T (RGB+térmico), solo el M3M (multiespectral) o los dos. El modo se
deriva de qué se subió + qué eligió el usuario, y se VALIDA acá antes de
lanzar el pipeline: una misión que no puede terminar bien tiene que fallar
en el formulario (en segundos, corregible ahí mismo) y no 40 minutos después
adentro de ODM.

Solo UNA misión corre a la vez (activate_mission() redirige
processing/outputs/preprocessing/geovisor/tiles vía symlinks — dos corridas
simultáneas se pisarían).

Uso:
  python3 -m webapp.main [puerto]
"""
import asyncio
import json
import math
import os
import shutil
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from osgeo import gdal, osr
from starlette.middleware.base import BaseHTTPMiddleware

gdal.UseExceptions()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.runner import PipelineRun, activate_mission, parse_progress_events
from core.scan import RUNS_ROOT, sanitize_mission_name, scan_existing_runs

APP_DIR = Path(os.environ.get("RAPTOR_APP_DIR", "/app"))
STATIC_DIR = Path(__file__).resolve().parent / "static"
GEOVISOR_DIR = APP_DIR / "geovisor"

UPLOAD_KINDS = ("rgb_thermal", "multispectral")
VALID_MODES = ("rgb", "rgb+thermal", "none")

app = FastAPI(title="Pipeline UAV de respuesta rápida")
RUNS_ROOT.mkdir(parents=True, exist_ok=True)


# StaticFiles no manda Cache-Control — sin eso, el navegador puede quedarse
# con una copia vieja de app.js/index.html/style.css sin revalidar (pasó en
# la práctica: cambios reales en el servidor, "no veo cambios" del lado del
# usuario). "no-cache" NO es "sin caché" — el navegador SIGUE cacheando,
# pero tiene que revalidar con ETag/Last-Modified en cada carga; si el
# archivo no cambió, el servidor responde 304 y no reenvía el contenido, así
# que no cuesta ancho de banda de más. Los tiles PNG de geovisor/tiles NO
# cambian una vez generados (cada corrida los regenera desde cero, nunca los
# pisa in-place), así que a esos sí les sirve un cache "fuerte" — se
# distinguen por el path.
class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith(("/geovisor", "/static")):
            if "/tiles/" in path:
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "no-cache"
        return response


app.add_middleware(NoCacheStaticMiddleware)


# ── Clasificación de archivos crudos ────────────────────────────────────
# Los mismos sufijos que busca docker/setup-data.sh recursivamente. Se
# clasifica acá para poder decirle al usuario QUÉ subió realmente (y qué le
# falta) antes de arrancar, en vez de que se entere por un error de ODM.
def classify_files(names):
    counts = {"rgb": 0, "thermal": 0, "ms": 0, "dband": 0, "otros": 0}
    for n in names:
        u = n.upper()
        if u.endswith("_V.JPG") or u.endswith("_W.JPG"):
            counts["rgb"] += 1
        elif u.endswith("_T.JPG"):
            counts["thermal"] += 1
        elif "_MS_" in u and u.endswith(".TIF"):
            counts["ms"] += 1
        elif u.endswith("_D.JPG"):
            counts["dband"] += 1
        else:
            counts["otros"] += 1
    counts["total"] = sum(counts[k] for k in ("rgb", "thermal", "ms", "dband", "otros"))
    return counts


def _listdir_names(d: Path):
    return [p.name for p in d.iterdir() if p.is_file()] if d.is_dir() else []


# ── Estado de la corrida activa (una sola a la vez, ver docstring) ─────────
class RunState:
    # Con las barras de progreso partidas por `\r` (ver runner.read_output)
    # el log son miles de redibujos: se guarda una ventana, no todo.
    MAX_LOG = 4000

    def __init__(self, mission_name, mission_dir, run_obj, mode, has_ms):
        self.mission_name = mission_name
        self.mission_dir = mission_dir
        self.run = run_obj
        self.mode = mode
        self.has_ms = has_ms
        self.log_lines = []
        self.log_dropped = 0        # cuántas se descartaron por la ventana
        self.done = False
        self.returncode = None
        self.error = None
        # Reloj del SERVIDOR: el cronómetro de la UI se calcula contra esto,
        # así recargar la página no lo reinicia desde cero.
        self.started_at = time.time()
        self.finished_at = None

    @property
    def log_total(self):
        """Índice absoluto de líneas emitidas (incluidas las descartadas)."""
        return self.log_dropped + len(self.log_lines)

    def _append(self, line):
        self.log_lines.append(line)
        excess = len(self.log_lines) - self.MAX_LOG
        if excess > 0:
            del self.log_lines[:excess]
            self.log_dropped += excess

    async def consume(self):
        # try/finally: pase lo que pase acá adentro, `done` TIENE que quedar
        # marcado. Cuando una excepción escapaba de este método (ver el
        # ValueError de readline en runner.py) el proceso terminaba bien pero
        # la UI se quedaba esperando un `done` que ya nunca iba a llegar.
        try:
            async for line in self.run.read_output():
                self._append(line)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self._append(f"[webapp] error leyendo la salida del pipeline: {self.error}")
        finally:
            # wait() explícito: si read_output() abortó, nadie cosechó el
            # proceso todavía y `returncode` seguiría en None (la UI no
            # podría distinguir éxito de fallo).
            try:
                self.returncode = await self.run.wait()
            except Exception:
                self.returncode = self.run.returncode
            self.finished_at = time.time()
            self.done = True

    def elapsed(self):
        return (self.finished_at or time.time()) - self.started_at


# Se conserva DESPUÉS de terminar (no se limpia): es lo que permite volver a
# la pantalla de progreso —o al log de error— tras recargar el navegador, en
# vez de perder el motivo de la falla y tener que rearrancar a ciegas.
_state: RunState | None = None


def _mission_dir(name, create=False):
    """(nombre saneado, directorio) de una misión. `create` SOLO en los
    endpoints que realmente reciben datos (upload/start): antes esto hacía
    mkdir siempre, así que un GET de lectura —/status, /sample, /view— sobre
    un nombre inexistente creaba el directorio y esa misión fantasma vacía
    aparecía después en la lista del selector."""
    safe = sanitize_mission_name(name)
    d = RUNS_ROOT / safe
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return safe, d


def _running_mission():
    """Nombre de la misión que está procesándose AHORA, o None."""
    return _state.mission_name if (_state and not _state.done) else None


def _refuse_if_other_mission_running(safe):
    """Rechaza activar una misión DISTINTA de la que está procesándose.

    activate_mission() redirige processing/outputs/preprocessing/geovisor/tiles
    por symlink (ver core/runner.py) — es un estado GLOBAL del contenedor, no
    algo por pestaña. Sin esta guarda, abrir el geovisor de una misión vieja
    mientras otra está corriendo movía esos symlinks debajo del pipeline en
    curso, que seguía escribiendo tan tranquilo en el directorio equivocado:
    los productos de la misión que corre terminaban mezclados dentro de la
    misión que se abrió para mirar. Es la misma clase de incidente de mezcla
    de misiones que documenta `clean-all` en el Makefile, pero disparado por
    un simple clic en el selector.

    /clear-uploads y DELETE /misión ya se protegían así; /activate y /view
    eran los dos que faltaban — y son justo los que tocan los symlinks."""
    running = _running_mission()
    if running is not None and running != safe:
        raise HTTPException(
            409, f"«{running}» está procesándose ahora mismo. Abrir otra misión "
                 f"redirigiría los directorios de trabajo del pipeline en curso "
                 f"y mezclaría los productos de las dos. Esperá a que termine.")


def _sample_raster(path: Path, lat: float, lon: float, band_idx: int = 1):
    """Lee UN valor de un GeoTIFF en (lat, lon) WGS84 — para la ficha 'qué
    significa este punto' del geovisor. None si el archivo no existe, el
    punto cae fuera del raster o es nodata — nunca se inventa un valor."""
    if not path.is_file():
        return None
    ds = gdal.Open(str(path))
    gt = ds.GetGeoTransform()
    inv = gdal.InvGeoTransform(gt)
    if inv is None:
        return None
    raster_srs = osr.SpatialReference(wkt=ds.GetProjection())
    wgs84 = osr.SpatialReference()
    wgs84.ImportFromEPSG(4326)
    wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    mx, my, _ = osr.CoordinateTransformation(wgs84, raster_srs).TransformPoint(lon, lat)
    px, py = (int(v) for v in gdal.ApplyGeoTransform(inv, mx, my))
    if px < 0 or py < 0 or px >= ds.RasterXSize or py >= ds.RasterYSize:
        return None
    band = ds.GetRasterBand(band_idx)
    arr = band.ReadAsArray(px, py, 1, 1)
    if arr is None:
        return None
    v = float(arr[0, 0])
    nodata = band.GetNoDataValue()
    if (nodata is not None and abs(v - nodata) < 1e-6) or not math.isfinite(v):
        return None
    return v


# Umbral de "misma zona": dos misiones cuyo centro cae a menos de esta
# distancia se consideran el mismo incidente/predio capturado en fechas
# distintas — es lo único que habilita el comparador Antes/Después en el
# geovisor (con una sola captura no hay nada que comparar). 400m cubre el
# tamaño típico de un vuelo de esta escala (decenas de hectáreas) sin
# confundir dos incendios cercanos pero distintos.
SAME_LOCATION_M = 400


def _mission_center(mission_dir: Path):
    bounds_path = mission_dir / "tiles" / "bounds.json"
    if not bounds_path.is_file():
        return None
    try:
        b = json.loads(bounds_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    c = b.get("center")
    return tuple(c) if c and len(c) == 2 else None


def _haversine_m(p1, p2):
    r = 6371000.0
    lat1, lon1, lat2, lon2 = map(math.radians, (p1[0], p1[1], p2[0], p2[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _related_missions(safe_name: str, mission_dir: Path):
    """Otras misiones (terminadas, con bounds.json) cuyo centro cae dentro
    de SAME_LOCATION_M de esta — candidatas para el comparador temporal."""
    center = _mission_center(mission_dir)
    if center is None:
        return []
    out = []
    for run in scan_existing_runs():
        if run.name == safe_name or not run.has_tiles:
            continue
        other_center = _mission_center(run.path)
        if other_center is None or _haversine_m(center, other_center) > SAME_LOCATION_M:
            continue
        captura = None
        situation_path = run.path / "outputs" / "situation.json"
        if situation_path.is_file():
            try:
                captura = json.loads(situation_path.read_text(encoding="utf-8")).get("captura")
            except (OSError, json.JSONDecodeError):
                pass
        out.append({"name": run.name, "captura": captura})
    out.sort(key=lambda m: m["captura"] or "")
    return out


def _odm_projects(mission_dir: Path):
    """Proyectos ODM ya reconstruidos de esta misión (lo caro: SfM+MVS).
    Su presencia es lo que habilita ofrecer 'reusar' en vez del checkbox
    SKIP_ODM que el usuario tenía que entender y marcar a mano."""
    proc = mission_dir / "processing"
    found = {}
    for key, sub in (("rgb", "rgb_odm"), ("thermal", "thermal_native_odm"),
                     ("multispectral", "multispectral_odm")):
        d = proc / sub
        found[key] = (d / "opensfm" / "reconstruction.json").exists()
    return found


# ── Landing + páginas estáticas de la webapp (no confundir con geovisor/) ──
@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/missions")
def list_missions():
    out = []
    for r in scan_existing_runs():
        out.append({"name": r.name, "has_outputs": r.has_outputs,
                    "has_processing": r.has_processing, "has_tiles": r.has_tiles})
    running = _running_mission()
    last = None
    if _state and _state.done:
        last = {"name": _state.mission_name, "ok": _state.returncode == 0}
    return {"missions": out, "active": running, "last_run": last}


@app.get("/api/missions/{mission}/status")
def mission_status(mission: str):
    """Todo lo que la UI necesita para decidir qué ofrecer: qué se subió (por
    tipo real de archivo), qué reconstrucciones ODM ya existen (para ofrecer
    reusarlas) y en qué quedó la última corrida."""
    safe, mission_dir = _mission_dir(mission)
    uploads = {k: classify_files(_listdir_names(mission_dir / "raw" / k))
               for k in UPLOAD_KINDS}
    tiles_ready = (mission_dir / "tiles" / "bounds.json").exists()
    running = bool(_state and not _state.done and _state.mission_name == safe)
    last_run = None
    if _state and _state.mission_name == safe and _state.done:
        last_run = {"ok": _state.returncode == 0, "returncode": _state.returncode,
                    "mode": _state.mode, "elapsed": round(_state.elapsed())}
    # elapsed del SERVIDOR: la UI lo usa para que el cronómetro sobreviva a
    # un F5 en vez de arrancar de nuevo en 00:00.
    elapsed = round(_state.elapsed()) if (_state and _state.mission_name == safe) else None
    return {"name": safe, "uploads": uploads, "odm": _odm_projects(mission_dir),
            "has_tiles": tiles_ready, "running": running, "last_run": last_run,
            "elapsed": elapsed, "mode": _state.mode if (_state and _state.mission_name == safe) else None,
            "has_multispectral": _state.has_ms if (_state and _state.mission_name == safe) else None,
            # Otras misiones en la misma zona: es lo único que habilita el
            # comparador Antes/Después en el geovisor (ver _related_missions).
            "related_missions": _related_missions(safe, mission_dir) if tiles_ready else []}


# ── Subida de archivos: un POST por archivo (carpeta completa vía
# <input webkitdirectory multiple> en el navegador). Solo el nombre base del
# archivo se conserva (sin subcarpetas) — el pipeline busca por SUFIJO de
# nombre en TODO SOURCE_DIR recursivamente (ver docker/setup-data.sh), no
# importa la estructura de carpetas original. ──
@app.post("/api/upload")
async def upload(mission: str = Form(...), kind: str = Form(...), file: UploadFile = None):
    if kind not in UPLOAD_KINDS:
        raise HTTPException(400, f"kind debe ser uno de {UPLOAD_KINDS}")
    if file is None:
        raise HTTPException(400, "falta el archivo")
    safe, mission_dir = _mission_dir(mission, create=True)
    dest_dir = mission_dir / "raw" / kind
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = os.path.basename(file.filename or "archivo")
    dest = dest_dir / fname
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    return {"ok": True, "mission": safe, "file": fname, "size": dest.stat().st_size}


@app.post("/api/missions/{mission}/clear-uploads")
def clear_uploads(mission: str, kind: str = Form(...)):
    """Vaciar un lote ya subido — para cuando se eligió la carpeta equivocada,
    sin tener que inventar otro nombre de misión."""
    if kind not in UPLOAD_KINDS:
        raise HTTPException(400, f"kind debe ser uno de {UPLOAD_KINDS}")
    if _state and not _state.done and _state.mission_name == sanitize_mission_name(mission):
        raise HTTPException(409, "no se pueden borrar archivos de una misión que está procesándose")
    _safe, mission_dir = _mission_dir(mission)
    d = mission_dir / "raw" / kind
    if d.is_dir():
        shutil.rmtree(d)
    return {"ok": True}


# ── Validación previa: la misma decisión que toma docker/entrypoint.sh, pero
# ANTES de lanzarlo, para poder devolver un mensaje corregible en el
# formulario en vez de un fallo a mitad de una corrida larga. ──
def _validate(mode, has_ms, uploads):
    rt, ms = uploads["rgb_thermal"], uploads["multispectral"]
    errors = []
    if mode not in VALID_MODES:
        errors.append(f"Modo inválido: {mode}")
        return errors
    if mode != "none":
        if rt["rgb"] == 0:
            errors.append(
                "Elegiste procesar el vuelo RGB/térmico pero no hay fotos RGB "
                "(*_V.JPG / *_W.JPG) entre los archivos subidos"
                + (f" ({rt['total']} archivos)" if rt["total"] else "")
                + ". Subí la carpeta del vuelo M3T/H20T, o desactivá ese sensor "
                  "si solo vas a procesar el multiespectral.")
        if mode == "rgb+thermal" and rt["thermal"] == 0:
            errors.append(
                "Elegiste RGB + térmico pero no hay fotos térmicas (*_T.JPG) "
                f"entre los {rt['total']} archivos subidos. Agregá las fotos "
                "térmicas del vuelo, o cambiá el producto a «Solo RGB».")
    if has_ms and ms["ms"] == 0:
        errors.append(
            "Activaste el multiespectral pero no hay bandas (*_MS_*.TIF) entre "
            f"los {ms['total']} archivos subidos en ese sensor. Subí la carpeta "
            "del vuelo M3M, o desactivá ese sensor.")
    if mode == "none" and not has_ms:
        errors.append("No seleccionaste ningún sensor para procesar.")
    return errors


@app.post("/api/missions/{mission}/validate")
def validate_mission(mission: str, mode: str = Form(...), has_multispectral: bool = Form(False)):
    _safe, mission_dir = _mission_dir(mission)
    uploads = {k: classify_files(_listdir_names(mission_dir / "raw" / k))
               for k in UPLOAD_KINDS}
    errors = _validate(mode, has_multispectral, uploads)
    return {"ok": not errors, "errors": errors}


# ── Iniciar procesamiento ───────────────────────────────────────────────
@app.post("/api/missions/{mission}/start")
async def start_mission(mission: str, mode: str = Form(...),
                        has_multispectral: bool = Form(False),
                        reuse_odm: bool = Form(False)):
    global _state
    if _state is not None and not _state.done:
        raise HTTPException(409, f"Ya hay una misión procesándose: {_state.mission_name}")

    safe, mission_dir = _mission_dir(mission, create=True)
    uploads = {k: classify_files(_listdir_names(mission_dir / "raw" / k))
               for k in UPLOAD_KINDS}
    errors = _validate(mode, has_multispectral, uploads)
    if errors:
        raise HTTPException(400, " ".join(errors))

    # MODE=none no toca SOURCE_DIR (ver entrypoint.sh), pero igual se pasa un
    # directorio existente para no depender de ese detalle.
    source_dir = mission_dir / "raw" / "rgb_thermal"
    source_dir.mkdir(parents=True, exist_ok=True)
    ms_source_dir = mission_dir / "raw" / "multispectral" if has_multispectral else None

    activate_mission(mission_dir)
    progress_file = mission_dir / "progress.ndjson"
    run_obj = PipelineRun(
        mode=mode, source_dir=source_dir, ms_source_dir=ms_source_dir,
        skip_odm=reuse_odm, port=8080, progress_file=progress_file,
    )
    await run_obj.start()
    _state = RunState(safe, mission_dir, run_obj, mode, has_multispectral)
    asyncio.create_task(_state.consume())
    return {"ok": True, "mission": safe}


# ── Progreso en vivo (SSE): lee el mismo PROGRESS_FILE que escribe
# core/runner.py (PipelineRun._env) y reempaqueta cada línea como
# Server-Sent Events para el HUD del geovisor. ──
@app.get("/api/missions/{mission}/events")
async def events(mission: str):
    safe = sanitize_mission_name(mission)
    if _state is None or _state.mission_name != safe:
        raise HTTPException(404, "esa misión no está corriendo ni corrió en esta sesión")
    state = _state

    async def gen():
        pos = 0
        log_pos = 0   # índice ABSOLUTO (la ventana de log descarta las viejas)
        # Primer evento: el reloj del servidor y el plan real de la corrida,
        # para que una pestaña que se reengancha reconstruya la vista completa
        # (cronómetro incluido) sin depender de lo que tenía en memoria.
        yield ("data: " + json.dumps({
            "kind": "hello", "elapsed": round(state.elapsed()),
            "mode": state.mode, "has_multispectral": state.has_ms,
            "done": state.done}) + "\n\n")
        while True:
            pos, evs = parse_progress_events(str(state.run.progress_file), pos)
            for ev in evs:
                yield f"data: {json.dumps({'kind': 'progress', **ev})}\n\n"
            if log_pos < state.log_total:
                start = max(log_pos, state.log_dropped)
                for line in state.log_lines[start - state.log_dropped:]:
                    yield f"data: {json.dumps({'kind': 'log', 'line': line})}\n\n"
                log_pos = state.log_total
            if state.done:
                yield ("data: " + json.dumps({
                    "kind": "done", "returncode": state.returncode,
                    "elapsed": round(state.elapsed()), "error": state.error}) + "\n\n")
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.delete("/api/missions/{mission}")
def delete_mission(mission: str):
    """Borra por completo una misión (raw/, processing/, outputs/, tiles/) —
    para deshacerse de pruebas o corridas fallidas sin salir al shell del
    contenedor. Bloqueada mientras la misión está procesándose: borrar el
    directorio debajo de un pipeline corriendo (activate_mission() ya la
    symlinkeó a processing/outputs/...) lo dejaría escribiendo a la nada."""
    safe = sanitize_mission_name(mission)
    if _state is not None and _state.mission_name == safe and not _state.done:
        raise HTTPException(409, "no se puede eliminar una misión que está procesándose")
    mission_dir = RUNS_ROOT / safe
    if not mission_dir.is_dir():
        raise HTTPException(404, "esa misión no existe")
    shutil.rmtree(mission_dir)
    return {"ok": True}


@app.get("/api/missions/{mission}/log")
def run_log(mission: str, tail: int = 200):
    """Cola del log de la última corrida — para mostrar el motivo real de una
    falla al volver a la misión (o tras recargar), sin dejar al usuario
    adivinando qué pasó."""
    safe = sanitize_mission_name(mission)
    if _state is None or _state.mission_name != safe:
        raise HTTPException(404, "esa misión no corrió en esta sesión")
    return {"lines": _state.log_lines[-tail:], "done": _state.done,
            "returncode": _state.returncode}


SEVERITY_LABELS = {1: "leve", 2: "leve", 3: "moderado", 4: "severo"}
SEVERITY_RECS = {
    "leve": "Sin anomalía relevante en este punto — no requiere acción inmediata.",
    "moderado": "Daño moderado — sumar a la ronda de verificación cuando se pueda.",
    "severo": "Daño alto — priorizar verificación en terreno antes de dar la zona por controlada.",
}


@app.get("/api/missions/{mission}/sample")
def sample_point(mission: str, lat: float, lon: float):
    """Ficha 'qué significa este punto' del geovisor: valores REALES leídos
    de los rasters de la misión en (lat, lon), no aproximaciones calculadas
    en el navegador. Cada campo queda en null si ese producto no existe
    para la misión (p.ej. sin multiespectral) o el punto cae fuera de su
    cobertura — el frontend decide cómo mostrar esa ausencia, nunca se
    rellena acá con un valor inventado."""
    _safe, mission_dir = _mission_dir(mission)
    outputs = mission_dir / "outputs"
    sev_val = _sample_raster(outputs / "severidad_class.tif", lat, lon)
    temp_val = _sample_raster(outputs / "thermal_orthomosaic.tif", lat, lon)
    ndvi_val = _sample_raster(outputs / "indices" / "ndvi.tif", lat, lon)
    sev_class = int(sev_val) if sev_val is not None else None
    sev_label = SEVERITY_LABELS.get(sev_class)

    confianza = captura = None
    situation_path = outputs / "situation.json"
    if situation_path.is_file():
        try:
            s = json.loads(situation_path.read_text(encoding="utf-8"))
            confianza, captura = s.get("confianza"), s.get("captura")
        except (OSError, json.JSONDecodeError):
            pass

    return {
        "dentro_del_area": sev_class is not None,
        "severidad": sev_label,
        "temperatura_c": round(temp_val, 1) if temp_val is not None else None,
        "ndvi": round(ndvi_val, 2) if ndvi_val is not None else None,
        "confianza": confianza,
        "captura": captura,
        "recomendacion": SEVERITY_RECS.get(sev_label),
    }


# ── Activar misión (symlinks) sin redirigir — la llama el propio geovisor
# ANTES de leer tiles/bounds.json. /view/{mission} activa y redirige, pero
# eso solo pasa al ENTRAR desde la webapp: un F5 sobre la URL ya redirigida
# (/geovisor/index.html?mission=X) es un GET directo a un archivo estático,
# nunca pasa por /view/ de nuevo — si mientras tanto se activó OTRA misión
# (o ninguna), el geovisor quedaba mostrando bounds.json de lo que sea que
# esté activo en ese momento, no el de la URL. Idempotente: activar la
# misión que ya está activa no hace nada raro.
@app.post("/api/missions/{mission}/activate")
def activate_mission_endpoint(mission: str):
    safe, mission_dir = _mission_dir(mission)
    _refuse_if_other_mission_running(safe)
    if not (mission_dir / "tiles" / "bounds.json").exists():
        raise HTTPException(404, "esta misión todavía no tiene tiles generados")
    activate_mission(mission_dir)
    return {"ok": True, "mission": safe}


# ── Geovisor: activa la misión (symlinks) y redirige a la MISMA página que
# usa el flujo "en vivo" (?mission=<nombre>) — un solo punto de entrada al
# geovisor para misión recién arrancada o ya terminada. El geovisor decide
# solo si hay algo en curso: conecta a /api/missions/<mision>/events y, si el
# servidor no la tiene activa (404), simplemente no muestra el HUD de
# progreso. Sirve los estáticos ya existentes de geovisor/ (index.html/
# app.js/style.css/tiles/outputs) — cero cambios ahí, ver geovisor/serve.py
# para el endpoint POST hermano de guardado de polígono, reimplementado abajo
# porque StaticFiles es de solo lectura. ──
@app.get("/view/{mission}")
def view_mission(mission: str):
    safe, mission_dir = _mission_dir(mission)
    _refuse_if_other_mission_running(safe)
    running = _running_mission() == safe
    if not running and not (mission_dir / "tiles" / "bounds.json").exists():
        raise HTTPException(404, "esta misión todavía no tiene tiles generados")
    activate_mission(mission_dir)
    return RedirectResponse(url=f"/geovisor/index.html?mission={safe}")


@app.post("/geovisor/api/save-area-afectada")
async def save_area_afectada(request: Request):
    body = await request.json()
    if body.get("type") != "FeatureCollection":
        raise HTTPException(400, "se esperaba un GeoJSON FeatureCollection")
    path = GEOVISOR_DIR / "outputs" / "area_afectada.geojson"
    path.write_text(json.dumps(body), encoding="utf-8")
    os.chmod(path, 0o666)
    return {"ok": True}


app.mount("/geovisor", StaticFiles(directory=str(GEOVISOR_DIR), html=True, follow_symlink=True), name="geovisor")
# follow_symlink=True: webapp/static/fonts es un symlink a geovisor/fonts
# (una sola copia de Rubik para las dos UIs) — sin esto Starlette no lo sigue
# y las fuentes devuelven 404.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR), follow_symlink=True), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print(f"""
╔══════════════════════════════════════════════════╗
║  🛰️  Pipeline UAV de respuesta rápida            ║
║                                                  ║
║  Abrir en el navegador:                          ║
║  → http://localhost:{port}                          ║
╚══════════════════════════════════════════════════╝
""")
    uvicorn.run(app, host="0.0.0.0", port=port)
