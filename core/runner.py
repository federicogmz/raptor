"""Activación de misión (symlinks) + invocación del pipeline real
(docker/entrypoint.sh) como subprocess, reusando toda la lógica ya
validada — este módulo no reimplementa nada del pipeline, solo lo orquesta.
Lo usa webapp/main.py.
"""
import asyncio
import os
import re
import shutil
import signal
import time
from pathlib import Path

APP_DIR = Path("/app")
LINKED_DIRS = ["processing", "outputs", "preprocessing"]
TILES_LINK = APP_DIR / "geovisor" / "tiles"

_LINE_SPLIT = re.compile(r"[\r\n]")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _replace_with_symlink(link: Path, target: Path):
    target.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        link.unlink()
    elif link.is_dir():
        shutil.rmtree(link)
    elif link.exists():
        link.unlink()
    link.symlink_to(target)


def activate_mission(mission_dir: Path):
    """Redirige /app/{processing,outputs,preprocessing,geovisor/tiles} a
    /app/runs/<mision>/* vía symlinks — así el pipeline (entrypoint.sh,
    Makefile, todos los scripts) escribe donde siempre sin ningún cambio,
    y varias misiones pueden coexistir en una sola sesión de contenedor."""
    for d in LINKED_DIRS:
        _replace_with_symlink(APP_DIR / d, mission_dir / d)
    _replace_with_symlink(TILES_LINK, mission_dir / "tiles")


class PipelineRun:
    """Corre docker/entrypoint.sh como subprocess, exponiendo eventos de
    progreso estructurados (ver PROGRESS_FILE en scripts/progress.py)
    y el log crudo (para mostrar si falla)."""

    def __init__(self, *, mode, source_dir, ms_source_dir=None,
                 skip_odm=False, port=8080, progress_file, export=None,
                 preset=None, quality=None, dband=False, terreno=None,
                 subsample=0, mission_name=None):
        self.mode = mode
        self.source_dir = str(source_dir)
        self.ms_source_dir = str(ms_source_dir) if ms_source_dir else None
        self.skip_odm = skip_odm
        # `preset` es la forma actual (vistazo/rapido/estandar/alta/maxima);
        # `quality` (0-100) es la anterior y se sigue aceptando — el mapeo lo
        # hace scripts/hardware.py, que es donde vive la tabla.
        self.preset = str(preset if preset is not None
                          else (quality if quality is not None else "estandar"))
        self.port = port
        self.progress_file = str(progress_file)
        # export: dict con las EXPORT_* que entiende docker/entrypoint.sh
        # (ver scripts/export_products.py). None/vacío = no se exporta nada.
        self.export = export or {}
        # Banda D del M3M (mosaico visible rápido, opt-in) — ver DBAND en
        # docker/entrypoint.sh. Solo tiene efecto si además hay ms_source_dir
        # con archivos *_D.JPG; entrypoint.sh lo ignora silenciosamente si no.
        self.dband = bool(dband)
        # "plano" (default) o "escarpado" — ver TERRENOS en scripts/hardware.py.
        # "escarpado" fuerza sfm_algorithm=incremental incluso en vistazo/
        # rápido: en terreno plano/aéreo estándar reconstruir por homografías
        # (planar) es válido y rápido, pero en relieve fuerte puede descartar
        # la mayoría de las fotos en silencio — confirmado en vivo.
        self.terreno = str(terreno) if terreno else "plano"
        # Modo urgencia: procesar 1 de cada N fotos (ver SUB_SAMPLE en
        # docker/entrypoint.sh y scripts/subsample_photos.py). Solo tiene
        # sentido con reconstrucción (reuse_odm=False); el entrypoint lo
        # ignora con SKIP_ODM=1.
        self.subsample = int(subsample) if subsample else 0
        # Nombre de la misión para el payload de alerta/informe (opcional).
        self.mission_name = mission_name
        self.returncode = None
        self.raw_lines = []
        self._proc = None

    def _env(self):
        env = os.environ.copy()
        env.update({
            "MODE": self.mode,
            "SOURCE_DIR": self.source_dir,
            "SKIP_ODM": "1" if self.skip_odm else "0",
            "PRESET": self.preset,
            "TERRENO": self.terreno,
            "DBAND": "1" if self.dband else "0",
            "SUB_SAMPLE": str(self.subsample),
            "SERVE": "0",
            "PORT": str(self.port),
            "PROGRESS_FILE": self.progress_file,
        })
        if self.mission_name:
            env["RAPTOR_MISSION"] = self.mission_name
        if self.ms_source_dir:
            env["MS_SOURCE_DIR"] = self.ms_source_dir
        else:
            env.pop("MS_SOURCE_DIR", None)
        # Se limpian SIEMPRE las EXPORT_* heredadas del proceso padre antes de
        # aplicar las de esta corrida: si no, una misión configurada sin
        # exportación heredaría el destino de otra corrida y escribiría ahí.
        for k in ("EXPORT_DIR", "EXPORT_PRODUCTS", "EXPORT_RASTER_FORMAT",
                  "EXPORT_VECTOR_FORMAT", "EXPORT_EPSG"):
            env.pop(k, None)
        env.update({k: str(v) for k, v in self.export.items() if v})
        return env

    async def start(self):
        Path(self.progress_file).write_text("")
        self._proc = await asyncio.create_subprocess_exec(
            "/app/docker/entrypoint.sh", "run",
            cwd="/app", env=self._env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    async def read_output(self):
        """Generador async: yieldea cada línea de stdout a medida que llega
        (para el log de la pantalla de progreso).

        NO usa readline() a propósito. Las barras de progreso
        (scripts/progress.py) se redibujan en el lugar con `\\r` y SIN
        newline, y stderr viene mezclado en stdout — así que una etapa larga
        produce UNA sola "línea" de cientos de KB. asyncio.StreamReader
        .readline() aborta a los 64 KiB con
        `ValueError: Separator is not found, and chunk exceed the limit`,
        lo que MATA la tarea que consume la salida: el pipeline sigue
        corriendo y termina bien, pero la UI no se entera y queda colgada
        para siempre en la última etapa que alcanzó a mostrar.

        Se lee por chunks y se corta por `\\r` Y por `\\n`, así cada redibujo
        de la barra es su propia línea — sin límite de tamaño que reventar y
        con un log legible en vez de un chorizo con códigos ANSI.
        """
        assert self._proc is not None
        assert self._proc.stdout is not None
        buf = ""
        while True:
            chunk = await self._proc.stdout.read(8192)
            if not chunk:
                break
            buf += chunk.decode(errors="replace")
            parts = _LINE_SPLIT.split(buf)
            buf = parts.pop()          # el último trozo puede estar incompleto
            for raw in parts:
                text = _ANSI.sub("", raw).rstrip()
                if not text:
                    continue
                self.raw_lines.append(text)
                yield text
        tail = _ANSI.sub("", buf).rstrip()
        if tail:
            self.raw_lines.append(tail)
            yield tail
        self.returncode = await self._proc.wait()

    async def wait(self):
        """Espera al proceso y cachea su código de salida. Idempotente — se
        llama también desde el `finally` del consumidor, para que un fallo
        leyendo la salida no deje la corrida sin resultado conocido."""
        if self._proc is None:
            return None
        if self.returncode is None:
            self.returncode = await self._proc.wait()
        return self.returncode

    def kill(self):
        """Mata el pipeline COMPLETO, no solo el bash de entrypoint.sh.

        entrypoint.sh es apenas la raíz: ODM (run.py), opensfm y el resto de
        las herramientas corren como nietos/bisnietos de ese proceso, y
        alguna (opensfm) arranca su propia sesión nueva — matar solo el
        `_proc` directo (lo que hacía esto antes) deja a esos hijos
        huérfanos pero VIVOS, reparentados a PID 1, consumiendo CPU/RAM
        igual que si nada hubiera pasado (confirmado en vivo: la corrida
        seguía usando el GPU/CPU entero después de "cancelarla"). Se
        recorre /proc para juntar TODO el árbol de descendientes antes de
        matar nada — no depende de grupos de proceso, así que también
        alcanza a lo que se haya desprendido a su propia sesión."""
        if not self._proc or self._proc.returncode is not None:
            return
        for pid in reversed(_descendant_pids(self._proc.pid)):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self._proc.kill()


def _descendant_pids(pid):
    """Todos los PIDs descendientes de `pid`, recursivo, vía /proc — no usa
    grupos de proceso (ver el comentario de kill() sobre por qué no alcanza)."""
    pids = []
    frontier = [pid]
    while frontier:
        p = frontier.pop()
        try:
            with open(f"/proc/{p}/task/{p}/children") as f:
                children = [int(x) for x in f.read().split()]
        except OSError:
            children = []
        pids.extend(children)
        frontier.extend(children)
    return pids


def parse_progress_events(path: str, since_pos: int):
    """Lee líneas nuevas de PROGRESS_FILE desde since_pos. Devuelve
    (nueva_posición, lista_de_eventos) — evento = dict con al menos 'event'."""
    events = []
    try:
        with open(path, "r") as f:
            f.seek(since_pos)
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                parts = line.split("\t")
                ev = {"event": parts[0]}
                for kv in parts[1:]:
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        ev[k] = v
                events.append(ev)
            since_pos = f.tell()
    except FileNotFoundError:
        pass
    return since_pos, events
