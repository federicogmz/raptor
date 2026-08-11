"""PipelineRun.kill(): cancelar una corrida tiene que matar el árbol de
procesos COMPLETO, no solo el bash de entrypoint.sh.

Reportado en vivo (no hipotético): una misión sin multiespectral corriendo
que había que cancelar. El primer intento (solo `self._proc.kill()`) mató
entrypoint.sh pero dejó ODM/opensfm huérfanos y VIVOS, reparentados a PID 1,
usando el CPU/GPU entero igual que antes — confirmado con `docker stats`
antes y después. Acá se prueba con un árbol de procesos real (bash
generando sus propios hijos con `sleep`, sin nada simulado) que
_descendant_pids()/kill() de verdad los alcanzan a todos.
"""
import asyncio
import os
import signal
import subprocess
import time

from core.runner import PipelineRun, _descendant_pids


def _vivo(pid):
    """True si el PID sigue corriendo de verdad (consumiendo CPU/GPU) — no
    alcanza con os.kill(pid, 0), que también da éxito para un ZOMBIE (ya
    murió, solo falta que su padre haga wait()). Acá adentro de `docker run`
    pytest es PID 1 del namespace: cuando kill() mata a un nieto reparentado,
    nadie lo re-cosecha nunca (PID1 no hace wait() de hijos que no lanzó él
    mismo) y queda zombie para siempre — sin que eso implique el leak real
    (CPU/GPU) que este test existe para atrapar."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
    except FileNotFoundError:
        return False
    # Campo 3 de /proc/pid/stat es el estado ('Z' = zombie). El nombre de
    # comm puede tener espacios/paréntesis, así que se parte desde el ")".
    estado = stat.split(") ", 1)[1].split()[0]
    return estado != "Z"


class TestDescendantPids:
    def test_encuentra_hijos_directos_e_indirectos(self):
        # bash que arranca un hijo, que arranca un nieto — árbol real de 3
        # niveles, cada uno un `sleep` propio para poder verificar quién
        # sigue vivo después.
        p = subprocess.Popen(
            ["bash", "-c", "sleep 30 & child=$!; "
             "bash -c 'sleep 30 & wait' & grandchild=$!; wait"])
        try:
            time.sleep(0.3)  # que el árbol termine de armarse
            hijos = _descendant_pids(p.pid)
            assert len(hijos) >= 2, f"se esperaban al menos 2 descendientes, hubo {hijos}"
        finally:
            for pid in [p.pid] + _descendant_pids(p.pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            p.wait(timeout=5)

    def test_pid_sin_hijos_devuelve_lista_vacia(self):
        p = subprocess.Popen(["sleep", "30"])
        try:
            assert _descendant_pids(p.pid) == []
        finally:
            p.kill()
            p.wait(timeout=5)


class TestKillMataElArbolCompleto:
    def test_kill_mata_nietos_no_solo_el_hijo_directo(self, tmp_path):
        """Equivalente mínimo del caso real: entrypoint.sh (el proceso que
        arranca PipelineRun) lanza un hijo, que lanza un nieto — como
        entrypoint.sh -> run.py -> opensfm. kill() tiene que tumbar los
        tres, no solo el primero."""
        async def _correr():
            run = PipelineRun(mode="rgb", source_dir="/tmp",
                              progress_file=str(tmp_path / "progress.ndjson"))
            # Reemplaza el binario real (docker/entrypoint.sh) por un árbol
            # de bash equivalente, para no depender de nada del pipeline
            # de verdad.
            run._proc = await asyncio.create_subprocess_exec(
                "bash", "-c",
                "sleep 30 & echo hijo:$! ; "
                "bash -c 'sleep 30 & echo nieto:$! >&2; wait' & wait",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.sleep(0.3)
            nietos = _descendant_pids(run._proc.pid)
            assert len(nietos) >= 2, "el árbol de prueba no se armó a tiempo"
            assert all(_vivo(p) for p in nietos), "los descendientes tendrían que estar vivos antes de kill()"

            run.kill()
            await asyncio.sleep(0.3)

            assert not _vivo(run._proc.pid), "el proceso raíz sigue vivo después de kill()"
            for pid in nietos:
                assert not _vivo(pid), f"PID {pid} (descendiente) sigue vivo después de kill() — quedó huérfano"

            try:
                await asyncio.wait_for(run._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        asyncio.run(_correr())

    def test_kill_no_hace_nada_si_no_hay_proceso_corriendo(self):
        """No debe reventar si se llama sin start() (nunca arrancó) o
        después de que el proceso ya terminó solo."""
        run = PipelineRun(mode="rgb", source_dir="/tmp", progress_file="/dev/null")
        run.kill()  # sin _proc: no debe lanzar
