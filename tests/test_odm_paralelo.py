"""Reparto de la máquina entre proyectos ODM concurrentes, Y el despacho
asíncrono por sensor (preparación → semáforo de reconstrucción → recorte →
exportación) que reemplazó a la barrera global de preparación.

Los cuatro proyectos (rgb_odm, thermal_native_odm, multispectral_odm,
dband_odm) son independientes, pero corrían estrictamente en serie: en
barbosa-picodegallo la fase de SfM incremental de la banda D usó ~1 núcleo
durante 4 h 27 min con los otros 19 esperando su turno.

El riesgo de solaparlos tampoco es teórico — en esta misma máquina ya hubo un
cuelgue por memoria con UN proceso pidiendo demasiados hilos. De ahí que el
reparto sea explícito y memory-aware: correr N a la vez significa que cada uno
se lleva 1/N del presupuesto de RAM Y de los núcleos, no que cada uno pida lo
que pediría solo.

Además, antes había una SEGUNDA barrera: TODAS las preparaciones (RGB,
multiespectral, térmico, banda D) tenían que terminar antes de que
CUALQUIERA pudiera empezar a reconstruir — aunque la preparación del térmico
(20-40 min cada 200 fotos, invoca el SDK de DJI foto por foto) no tiene nada
que ver con la de los demás (segundos a minutos). Ahora cada sensor prepara
y recién compite por un cupo de reconstrucción cuando SU preparación termina
— ver TestDespachoAsincronoPorSensor.
"""
import json
import os
import re
import subprocess

import pytest

import hardware
from hardware import MIN_HILOS_POR_PROYECTO, odm_slots

ENTRYPOINT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "docker", "entrypoint.sh")

RGB_MP, MS_MP, TERMICO_MP = 12.3, 5.0, 0.33


@pytest.fixture
def maquina(monkeypatch):
    """Fija núcleos y RAM disponible: el reparto no puede depender de en qué
    máquina corran los tests."""
    def _fijar(cores, mem_mb):
        monkeypatch.setattr(hardware, "cpu_count", lambda: cores)
        monkeypatch.setattr(hardware, "mem_available_mb", lambda: mem_mb)
    return _fijar


class TestOdmSlots:
    def test_reparte_el_presupuesto_no_lo_duplica(self, maquina):
        """Lo que este módulo existe para evitar: dos proyectos pidiendo cada
        uno lo que pedirían solos."""
        maquina(20, 20480)
        solo = odm_slots([MS_MP])["hilos"][0]
        juntos = odm_slots([MS_MP, MS_MP])

        assert juntos["slots"] == 2
        assert sum(juntos["hilos"]) <= solo + 1, (
            f"dos proyectos juntos ({juntos['hilos']}) no pueden sumar más "
            f"hilos que uno solo con toda la máquina ({solo})")

    def test_el_rgb_no_comparte_en_una_maquina_chica(self, maquina):
        """12.3 MP son ~6.3 GB por hilo en MVS: en 23 GB no entra con nadie
        más sin quedar por debajo del mínimo, y ahí correr solo es lo
        correcto — su etapa densa necesita de verdad esa memoria."""
        maquina(20, 20480)
        assert odm_slots([RGB_MP, MS_MP])["slots"] == 1

    def test_en_una_maquina_grande_si_solapa(self, maquina):
        """Nada de esto puede ser un número fijo: en un servidor con RAM de
        sobra los mismos proyectos tienen que solaparse."""
        maquina(64, 256 * 1024)
        assert odm_slots([RGB_MP, MS_MP])["slots"] == 2

    def test_nunca_por_debajo_del_minimo_util(self, maquina):
        maquina(20, 20480)
        r = odm_slots([MS_MP, MS_MP, TERMICO_MP])
        for h in r["hilos"][:r["slots"]]:
            assert h >= MIN_HILOS_POR_PROYECTO

    def test_reparte_tambien_la_fase_liviana(self, maquina):
        """Si solo se repartiera la fase pesada, dos proyectos solapados
        pedirían CADA UNO la concurrencia liviana entera — 16 hilos a ~1 GB
        cada uno, o sea 32 GB entre los dos. Exactamente la suma sin
        coordinar que ya causó un cuelgue."""
        maquina(20, 20480)
        solo = odm_slots([MS_MP])["hilos_light"][0]
        juntos = odm_slots([MS_MP, MS_MP])
        assert juntos["slots"] == 2
        assert sum(juntos["hilos_light"]) <= solo + 1

    def test_nunca_mas_hilos_que_nucleos(self, maquina):
        maquina(4, 512 * 1024)     # RAM de sobra, pocos núcleos
        r = odm_slots([MS_MP, MS_MP])
        assert sum(r["hilos"][:r["slots"]]) <= 4
        assert sum(r["hilos_light"][:r["slots"]]) <= 4

    def test_max_concurrency_cae_a_secuencial(self, maquina):
        """MAX_CONCURRENCY es la perilla de 'cuidame la memoria': respetarla
        lanzando varios proyectos con ese valor cada uno sería lo contrario
        de lo que pide."""
        maquina(64, 256 * 1024)
        r = odm_slots([RGB_MP, MS_MP], override=4)
        assert r["slots"] == 1
        assert r["hilos"] == [4, 4]

    def test_sin_dato_de_memoria_no_arriesga(self, maquina):
        maquina(20, None)
        assert odm_slots([MS_MP, MS_MP])["slots"] == 1

    def test_un_solo_proyecto(self, maquina):
        maquina(20, 20480)
        r = odm_slots([MS_MP])
        assert r["slots"] == 1 and len(r["hilos"]) == 1

    def test_sin_proyectos(self, maquina):
        maquina(20, 20480)
        assert odm_slots([]) == {"slots": 0, "hilos": [], "hilos_light": []}

    def test_cli_devuelve_json(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run(["python3", os.path.join(repo, "scripts", "hardware.py"),
                            "odm-slots", "5", "5", "--max-paralelo", "1"],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        datos = json.loads(r.stdout)
        assert datos["slots"] == 1
        assert len(datos["hilos"]) == 2


def _bloque_odm_args():
    """Solo la función _odm_args() — para probar el string de argumentos por
    sensor sin tener que correr todo el despacho."""
    src = open(ENTRYPOINT, encoding="utf-8").read()
    ini = src.index("_odm_args() {")
    fin = src.index("\n}\n", ini) + 3
    return src[ini:fin]


class TestFastOrthophotoEnRgb:
    """--fast-orthophoto en RGB (vistazo/rápido, ver FAST_ORTHOPHOTO_RGB en
    entrypoint.sh): salta DensifyPointCloud, el mismo salto que banda D ya
    usa siempre. Confirmado en vivo (misión mision_2026-08-08, 1199 fotos):
    esa sola etapa se llevó ~11 de ~15h con GPU activa — el costo es ~lineal
    por foto y "pc-quality low" no lo evita. A diferencia de banda D, en RGB
    queda atado al preset: RGB es la fuente del DSM de la misión, así que
    estándar/alta/máxima mantienen la nube densa completa."""

    def _args_rgb(self, fast, extra_vars=""):
        guion = f"""
set -uo pipefail
FEAT_QUALITY=medium; ODM_RES_CM=15; MIN_FEATURES=4000; MATCHER_NEIGHBORS=8
PC_QUALITY=low; SFM_ALGORITHM=planar
FAST_ORTHOPHOTO_RGB={fast}
{extra_vars}
""" + _bloque_odm_args() + '\n_odm_args rgb\n'
        r = subprocess.run(["bash", "-c", guion], capture_output=True, text=True, timeout=10)
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout

    def test_vistazo_agrega_fast_orthophoto(self):
        assert "--fast-orthophoto" in self._args_rgb(fast=1)

    def test_estandar_no_lo_agrega(self):
        assert "--fast-orthophoto" not in self._args_rgb(fast=0)

    def test_dsm_se_pide_en_los_dos_casos(self):
        """--fast-orthophoto no reemplaza --dsm: sigue pidiéndose, solo que
        va a salir de la nube dispersa en vez de la densa."""
        assert "--dsm" in self._args_rgb(fast=1)
        assert "--dsm" in self._args_rgb(fast=0)

    def test_no_se_filtra_a_otros_sensores(self):
        """La decisión es SOLO para RGB — thermal/multispectral no deben
        llevar --fast-orthophoto sin importar FAST_ORTHOPHOTO_RGB (banda D
        ya lo lleva siempre, por su cuenta, y no depende de esta variable)."""
        guion = """
set -uo pipefail
FEAT_QUALITY=medium; ODM_RES_CM=15; MIN_FEATURES=4000; MATCHER_NEIGHBORS=8
PC_QUALITY=low; SFM_ALGORITHM=planar
FAST_ORTHOPHOTO_RGB=1
""" + _bloque_odm_args() + '\n_odm_args thermal\n_odm_args multispectral\n'
        r = subprocess.run(["bash", "-c", guion], capture_output=True, text=True, timeout=10)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "--fast-orthophoto" not in r.stdout


def _bloque_odm_mp():
    """Solo la función _odm_mp() — para probar el perfil de MP por sensor
    sin correr todo el despacho."""
    src = open(ENTRYPOINT, encoding="utf-8").read()
    ini = src.index("_odm_mp() {")
    fin = src.index("\n}\n", ini) + 3
    return src[ini:fin]


class TestPerfilDeMemoriaRgbConFastOrthophoto:
    """Bug real, encontrado en vivo (misión mision_2026-08-08, vistazo +
    escarpado): con --fast-orthophoto, RGB NUNCA corre DensifyPointCloud (ver
    run_odm() — sin esa bandera en los args no hay fase pesada separada), así
    que reservarle la máquina entera con el perfil de 12.3 MP (calibrado para
    la etapa que ya no corre) le negaba el solapamiento con otro sensor sin
    necesidad real. Confirmado en vivo: 10% CPU y >15 GB libres mientras RGB
    reconstruía solo, con térmico ya terminado."""

    def _mp(self, fast, tmp_path):
        guion = f"""
set -uo pipefail
FAST_ORTHOPHOTO_RGB={fast}
""" + _bloque_odm_mp() + '\n_odm_mp rgb\n'
        r = subprocess.run(["bash", "-c", guion], capture_output=True, text=True, timeout=10)
        assert r.returncode == 0, r.stdout + r.stderr
        return float(r.stdout.strip())

    def test_perfil_completo_sin_fast_orthophoto(self, tmp_path):
        assert self._mp(fast=0, tmp_path=tmp_path) == 12.3

    def test_perfil_reducido_con_fast_orthophoto(self, tmp_path):
        """Reducido, no al nivel de térmico: texturizar sobre las imágenes
        "undistorted" (casi resolución nativa) sigue siendo más caro que el
        perfil liviano del SfM (LIGHT_MP en hardware.py)."""
        mp = self._mp(fast=1, tmp_path=tmp_path)
        assert mp < 12.3
        assert mp > 0.33

    def test_otros_sensores_no_cambian(self, tmp_path):
        """FAST_ORTHOPHOTO_RGB es una decisión de RGB — no debe afectar el
        perfil de los demás sensores."""
        guion = """
set -uo pipefail
FAST_ORTHOPHOTO_RGB=1
""" + _bloque_odm_mp() + '\n_odm_mp thermal\n_odm_mp multispectral\n_odm_mp dband\n'
        r = subprocess.run(["bash", "-c", guion], capture_output=True, text=True, timeout=10)
        assert r.returncode == 0, r.stdout + r.stderr
        assert r.stdout.split() == ["0.33", "5", "5"]


def _bloque_despacho():
    """El bloque REAL de entrypoint.sh: las funciones _odm_mp/_odm_proyecto/
    _odm_prep/_odm_args/_odm_post/_odm_export, la lista ODM_ORDEN, y el
    despacho asíncrono por sensor (semáforo de reconstrucción + camino de
    reuso con SKIP_ODM=1) — hasta el chequeo de fallo inclusive, que es
    parte de lo que hay que probar (sin él, un fallo de reconstrucción
    seguiría de largo)."""
    src = open(ENTRYPOINT, encoding="utf-8").read()
    ini = src.index("_odm_mp() {")
    fin = src.index('[[ "$DO_TRIM" -eq 1 ]] && pipeline_progress_done "Reconstrucción y recortes listos"', ini)
    return src[ini:fin]


STUBS_COMUNES = r"""
set -uo pipefail
SKIP_ODM=0
PRESET=estandar; PRESET_NOMBRE=estandar; PRESET_TITULO=Estándar
PC_QUALITY=medium; FEAT_QUALITY=high; ODM_RES_CM=4; MIN_FEATURES=8000
SFM_ALGORITHM=incremental; MATCHER_NEIGHBORS=8
HYBRID_BA=1; HYBRID_BA_FLAG=(--use-hybrid-bundle-adjustment)
FAST_ORTHOPHOTO_RGB=0
PREP_SPLIT=4
nproc() { echo 20; }
publish_partial() { :; }
"""


class TestDespachoAsincronoPorSensor:
    """El despacho REAL: cada sensor prepara (make prepare-X / la cadena
    térmica) de forma independiente, y recién compite por un cupo de
    reconstrucción cuando SU preparación termina — no cuando terminan las
    de los demás."""

    def _correr(self, tmp_path, orden, slots="1", extra=""):
        guion = STUBS_COMUNES + f"""
cd "{tmp_path}"
mkdir -p outputs/logs scripts
RUN_RGB=0; RUN_THERMAL=0; RUN_MULTISPECTRAL=0; DO_DBAND=0
{chr(10).join(f'{v}=1' for v in orden)}
T0=$(date +%s%N)
marca() {{ echo "MARCA $1 $(( ($(date +%s%N)-T0)/1000000 ))" >> traza.txt; }}
python3() {{
  if [[ "$1" == "scripts/hardware.py" ]]; then
    printf '%s\n4 4 4 4\n8 8 8 8\n' {slots}
    return 0
  fi
  if [[ "$1" == scripts/export_* ]]; then
    echo "$*" >> exports.txt
    return 0
  fi
  command python3 "$@"
}}
""" + extra + "\n" + _bloque_despacho() + '\necho "SALIDA=$?"\n'
        return subprocess.run(["bash", "-c", guion], capture_output=True,
                              text=True, timeout=30, cwd=str(tmp_path))

    def _marcas(self, tmp_path):
        eventos = {}
        f = tmp_path / "traza.txt"
        if not f.is_file():
            return eventos
        for linea in f.read_text().splitlines():
            _, resto = linea.split(" ", 1)
            nombre, t = resto.rsplit(" ", 1)
            eventos[nombre] = int(t)
        return eventos

    def test_un_sensor_rapido_no_espera_la_preparacion_del_lento(self, tmp_path):
        """El caso central: RGB (preparación instantánea) tiene que poder
        arrancar A RECONSTRUIR antes de que el térmico (preparación lenta)
        termine la SUYA — antes, una barrera común los igualaba a todos por
        el más lento."""
        extra = r"""
make() {
  case "$1" in
    prepare-rgb) marca "rgb-prep-lista" ;;
    sdk-convert) sleep 0.4 ;;
    denoise-thermal) : ;;
    prepare-thermal-native) marca "thermal-prep-lista" ;;
    *) : ;;
  esac
}
run_odm() {
  marca "$1-reconstruccion-inicio"
  [[ "$1" == "rgb" ]] && sleep 0.05
  return 0
}
"""
        r = self._correr(tmp_path, ["RUN_RGB", "RUN_THERMAL"], slots="2", extra=extra)
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        m = self._marcas(tmp_path)
        assert set(m) >= {"rgb-prep-lista", "thermal-prep-lista", "rgb-reconstruccion-inicio"}
        # RGB entró a reconstruir ANTES de que el térmico terminara de
        # preparar — la barrera vieja lo hubiera obligado a esperar hasta
        # thermal-prep-lista.
        assert m["rgb-reconstruccion-inicio"] < m["thermal-prep-lista"], m

    def test_la_cadena_termica_sigue_siendo_secuencial_puertas_adentro(self, tmp_path):
        """sdk-convert → denoise-thermal → prepare-thermal-native sigue
        siendo una cadena (cada uno depende del anterior) aunque el sensor
        entero corra independiente de los demás."""
        extra = r"""
make() {
  case "$1" in
    sdk-convert) sleep 0.1; echo "orden:sdk-convert" ;;
    denoise-thermal) echo "orden:denoise-thermal" ;;
    prepare-thermal-native) echo "orden:prepare-thermal-native" ;;
    *) : ;;
  esac
}
run_odm() { :; }
"""
        r = self._correr(tmp_path, ["RUN_THERMAL"], extra=extra)
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        orden = re.findall(r"orden:(\S+)", r.stdout)
        assert orden == ["sdk-convert", "denoise-thermal", "prepare-thermal-native"]

    def test_presupuesto_de_concurrencia_no_se_le_pasa_a_rgb_ni_a_termico(self, tmp_path):
        """PREP_SPLIT es para multiespectral/banda D — prepare-rgb no tiene
        hilos propios, y sdk-convert usa su propio perfil liviano
        (safe_concurrency(0.33) adentro del script, no este presupuesto)."""
        extra = r"""
make() {
  case "$1" in
    prepare-rgb) echo "rgb:MAX_CONCURRENCY=${MAX_CONCURRENCY:-sin-fijar}" ;;
    prepare-multispectral) echo "ms:MAX_CONCURRENCY=${MAX_CONCURRENCY:-sin-fijar}" ;;
    sdk-convert) echo "th:MAX_CONCURRENCY=${MAX_CONCURRENCY:-sin-fijar}" ;;
    *) : ;;
  esac
}
run_odm() { :; }
"""
        r = self._correr(tmp_path, ["RUN_RGB", "RUN_THERMAL", "RUN_MULTISPECTRAL"],
                         slots="3", extra=extra)
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        assert "rgb:MAX_CONCURRENCY=sin-fijar" in r.stdout
        assert "ms:MAX_CONCURRENCY=4" in r.stdout
        assert "th:MAX_CONCURRENCY=sin-fijar" in r.stdout

    def test_una_preparacion_que_falla_no_llega_a_reconstruir(self, tmp_path):
        extra = r"""
make() { [[ "$1" == "prepare-rgb" ]] && exit 1; :; }
run_odm() { echo "NO-DEBERIA-CORRER" >> traza.txt; }
"""
        r = self._correr(tmp_path, ["RUN_RGB"], extra=extra)
        assert r.returncode != 0 or "SALIDA=0" not in r.stdout
        assert not (tmp_path / "traza.txt").exists() or \
            "NO-DEBERIA-CORRER" not in (tmp_path / "traza.txt").read_text()

    def test_solo_preparan_los_sensores_presentes(self, tmp_path):
        extra = r"""
make() { echo "make:$1"; }
run_odm() { :; }
"""
        r = self._correr(tmp_path, ["RUN_MULTISPECTRAL"], extra=extra)
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        assert "make:prepare-rgb" not in r.stdout
        assert "make:sdk-convert" not in r.stdout
        assert "make:prepare-multispectral" in r.stdout


class TestSemaforoDeReconstruccion:
    def _correr(self, tmp_path, orden, slots, dormir="0.3"):
        guion = STUBS_COMUNES + f"""
cd "{tmp_path}"
mkdir -p outputs/logs scripts
RUN_RGB=0; RUN_THERMAL=0; RUN_MULTISPECTRAL=0; DO_DBAND=0
{chr(10).join(f'{v}=1' for v in orden)}
make() {{ [[ "$1" == prepare-* ]] && return 0; echo "make $*" >> makes.txt; }}
python3() {{
  if [[ "$1" == "scripts/hardware.py" ]]; then
    printf '%s\n4 4 4 4\n8 8 8 8\n' {slots}
    return 0
  fi
  if [[ "$1" == scripts/export_* ]]; then return 0; fi
  command python3 "$@"
}}
run_odm() {{
  echo "INICIO $1 $(date +%s%N)" >> traza.txt
  sleep {dormir}
  echo "FIN $1 $(date +%s%N)" >> traza.txt
}}
""" + _bloque_despacho() + '\necho "SALIDA=$?"\n'
        return subprocess.run(["bash", "-c", guion], capture_output=True,
                              text=True, timeout=30, cwd=str(tmp_path))

    def _intervalos(self, tmp_path):
        eventos = {}
        for linea in (tmp_path / "traza.txt").read_text().splitlines():
            tipo, label, t = linea.split()
            eventos.setdefault(label, {})[tipo] = int(t)
        return eventos

    def test_con_un_slot_no_se_solapan(self, tmp_path):
        r = self._correr(tmp_path, ["RUN_MULTISPECTRAL", "DO_DBAND"], slots=1)
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        ev = self._intervalos(tmp_path)
        assert set(ev) == {"multispectral", "dband"}
        assert (ev["dband"]["INICIO"] >= ev["multispectral"]["FIN"]
                or ev["multispectral"]["INICIO"] >= ev["dband"]["FIN"]), ev

    def test_con_dos_slots_si_se_solapan(self, tmp_path):
        r = self._correr(tmp_path, ["RUN_MULTISPECTRAL", "DO_DBAND"], slots=2)
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        ev = self._intervalos(tmp_path)
        a, b = ev["multispectral"], ev["dband"]
        assert a["INICIO"] < b["FIN"] and b["INICIO"] < a["FIN"], (
            f"con 2 slots tenían que solaparse: {ev}")

    def test_lanza_el_recorte_y_la_exportacion_de_cada_sensor_al_terminar(self, tmp_path):
        r = self._correr(tmp_path, ["RUN_MULTISPECTRAL", "DO_DBAND"], slots=2)
        assert "SALIDA=0" in r.stdout, r.stdout + r.stderr
        makes = (tmp_path / "makes.txt").read_text()
        assert "trim-edges-multispectral" in makes
        assert "compute-indices" in makes
        assert "trim-edges-dband" in makes

    def test_un_fallo_corta_la_corrida_y_no_arranca_lo_pendiente(self, tmp_path):
        guion = STUBS_COMUNES + f"""
cd "{tmp_path}"
mkdir -p outputs/logs scripts
RUN_RGB=0; RUN_THERMAL=0; RUN_MULTISPECTRAL=1; DO_DBAND=1
make() {{ [[ "$1" == prepare-* ]] && return 0; echo "make $*" >> makes.txt; }}
python3() {{
  if [[ "$1" == "scripts/hardware.py" ]]; then printf '1\n4 4\n8 8\n'; return 0; fi
  if [[ "$1" == scripts/export_* ]]; then return 0; fi
  command python3 "$@"
}}
run_odm() {{ echo "CORRIO $1" >> traza2.txt; exit 7; }}
""" + _bloque_despacho() + '\necho "NO-DEBERIA-LLEGAR"\n'
        r = subprocess.run(["bash", "-c", guion], capture_output=True,
                           text=True, timeout=30, cwd=str(tmp_path))
        assert "NO-DEBERIA-LLEGAR" not in r.stdout, r.stdout
        assert "falló al menos una reconstrucción" in r.stdout, r.stdout
        corridas = (tmp_path / "traza2.txt").read_text().splitlines()
        assert len(corridas) == 1, corridas
