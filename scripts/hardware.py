#!/usr/bin/env python3
"""Detección de hardware + perfil de calidad — FUENTE ÚNICA de ambas tablas.

Antes vivían separadas: el mapeo QUALITY→pc-quality estaba en un if/elif de
bash dentro de entrypoint.sh, y no había forma de mostrarle al usuario, ANTES
de arrancar, a qué se traducía el número que eligió. Esto lo centraliza para
que el entrypoint (bash), la webapp (formulario) y el CLI (`./raptor run
--quality`) lean la MISMA tabla — sin dos copias que puedan desincronizarse.

Solo depende de la librería estándar: tiene que poder importarse desde
`webapp/main.py` (proceso Python largo) y llamarse por subprocess desde
`docker/entrypoint.sh` (bash) sin arrastrar GDAL ni nada pesado.

Uso por CLI:
  python3 scripts/hardware.py info
  python3 scripts/hardware.py concurrency <megapixels>
  python3 scripts/hardware.py quality-tier <0-100>
  python3 scripts/hardware.py estimate --quality 75 --photos 1652
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

# ── Detección de hardware ───────────────────────────────────────────
def cpu_count():
    return os.cpu_count() or 4


def mem_available_mb():
    """RAM realmente disponible (no la total): lo que puede usarse ahora sin
    empujar a otros procesos a swap. Ninguna corrida de ODM debería
    planificarse contra la RAM total de la máquina."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (FileNotFoundError, ValueError, IndexError):
        pass
    return None


def gpu_info():
    """(hay_gpu, nombre, vram_mb) — best-effort vía nvidia-smi. VRAM en MiB
    (None si no se pudo leer). None si no hay GPU o si el binario no está
    (WSL/CPU-only, muy común en este proyecto).

    La VRAM importa para la estimación de tiempo: la reconstrucción densa
    (OpenMVS) escala los depthmaps a la memoria de la GPU, así que una
    laptop con 6 GB no es "una GPU que acelera 2x" sino el cuello de
    botella real de la corrida (ver el comentario de la calibración abajo)."""
    if shutil.which("nvidia-smi") is None:
        return False, None, None
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name",
                            "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            name = r.stdout.strip().splitlines()[0].strip()
            vram = None
            try:
                rv = subprocess.run(["nvidia-smi", "--query-gpu=memory.total",
                                     "--format=csv,noheader,nounits"],
                                    capture_output=True, text=True, timeout=5)
                if rv.returncode == 0 and rv.stdout.strip():
                    vram = int(rv.stdout.strip().splitlines()[0].split()[0])
            except (OSError, ValueError, subprocess.TimeoutExpired):
                vram = None
            return True, name, vram
    except (OSError, subprocess.TimeoutExpired):
        pass
    return False, None, None


def detect_hardware():
    has_gpu, gpu_name, vram_mb = gpu_info()
    return {"cores": cpu_count(), "mem_available_mb": mem_available_mb(),
            "gpu": has_gpu, "gpu_name": gpu_name, "vram_mb": vram_mb}


def _gpu_dense_offload(vram_min_mb=4096):
    """True si hay GPU con VRAM suficiente para que la etapa densa
    (DensifyPointCloud) corra acelerada — en ese caso el trabajo pesado de
    los depthmaps lo paga en gran parte la VRAM, no la RAM del host, así que
    la guía de ODM ("~1GB por hilo cada 2 MP") es la cifra CPU-only, pensada
    para cuando NO hay ese descargue. Reportado en vivo: en una misión real
    (RGB 12.3 MP, GPU RTX 2080) el contenedor ENTERO usó ~6 GB de RAM con 3
    hilos activos ya pasada la etapa densa — bien por debajo de lo que la
    fórmula CPU-only hubiera reservado (~19 GB para esos 3 hilos) — señal de
    que la reserva CPU-only es demasiado conservadora cuando hay GPU, aunque
    esa muestra puntual no midió el pico exacto de la propia etapa densa.
    vram_min_mb=4 GB a propósito: por debajo de eso (una laptop chica) la GPU
    puede terminar siendo el cuello de botella real, no un ahorro de RAM.
    """
    has_gpu, _, vram_mb = gpu_info()
    return bool(has_gpu and vram_mb and vram_mb >= vram_min_mb)


# Reducción del costo de RAM/hilo asumido para la etapa densa cuando hay
# descargue a GPU (ver _gpu_dense_offload) — CONSERVADOR frente a lo medido
# en vivo (~3x menos que la fórmula CPU-only), no lo elimina: 0.5 dejó a RGB
# con más margen del que la muestra puntual de arriba sugería que hacía
# falta, a propósito, porque esa muestra no midió el pico real de la etapa
# densa en sí.
GPU_DENSE_RAM_FACTOR = 0.5


def safe_concurrency(megapixels, ram_frac=0.8, override=None, gpu_aware=False):
    """Hilos seguros para una etapa de ODM que carga ~megapixels/2 GB por
    hilo (documentado por el propio ODM: "~1GB per thread and 2 megapixel
    image resolution"). Nunca más que los núcleos reales; nunca menos de 1.

    `override` gana siempre — es MAX_CONCURRENCY, la vía de escape manual.
    Generaliza lo que antes solo protegía al multiespectral (el bug de OOM en
    band alignment): CUALQUIER etapa de ODM usa todos los núcleos si no se le
    dice lo contrario, así que el mismo riesgo existe en RGB y en térmico
    sobre una máquina con muchos núcleos y fotos grandes.

    `gpu_aware`: aplica GPU_DENSE_RAM_FACTOR si hay descargue a GPU (ver
    _gpu_dense_offload) — SOLO tiene sentido para la etapa densa de ODM
    (DensifyPointCloud), que es la que corre acelerada. La fase liviana de
    SfM (features/matching/reconstrucción incremental, en OpenSfM) y los
    scripts de preparación (dji_irp, exiftool, hardlinks) no descargan nada a
    GPU, así que sus llamadas dejan esto en False — el default — y siguen
    con la cifra CPU-only completa.
    """
    if override is not None:
        return int(override)
    cores = cpu_count()
    avail = mem_available_mb()
    if not avail or avail <= 0:
        return cores
    factor = GPU_DENSE_RAM_FACTOR if (gpu_aware and _gpu_dense_offload()) else 1.0
    mb_per_thread = max(512, int(megapixels * 1024 / 2 * factor))
    n = int(avail * ram_frac / mb_per_thread)
    return max(1, min(cores, n))


# ── Cuántos proyectos ODM caben a la vez ─────────────────────────────
# Los proyectos de RAPTOR (rgb_odm, thermal_native_odm, multispectral_odm,
# dband_odm) son INDEPENDIENTES: distinta carpeta fuente, distinta carpeta
# destino, ningún archivo compartido. Correrlos en serie desperdicia la
# máquina de forma medible: en barbosa-picodegallo, la fase de SfM incremental
# de la banda D usó ~1 núcleo durante 4 h 27 min mientras los otros 19 no
# hacían nada, y la reconstrucción siguiente esperaba su turno.
#
# El riesgo tampoco es teórico: en esta misma máquina ya hubo un cuelgue por
# memoria cuando UN proceso pidió demasiados hilos. Con varios proyectos a la
# vez el riesgo se multiplica, así que el reparto se hace acá, con la misma
# cuenta memory-aware de safe_concurrency() y contra la RAM disponible REAL —
# no contra la total, y nunca por encima de los núcleos que hay.
#
# La regla: correr N proyectos a la vez significa que cada uno se lleva 1/N
# del presupuesto de memoria Y de los núcleos — no que cada uno pida lo que
# pediría solo. Se prueba el N más grande permitido y se baja hasta que TODOS
# los de la tanda reciban al menos MIN_HILOS_POR_PROYECTO; si ni siquiera dos
# entran, se corre de a uno. El comportamiento de siempre queda como PISO,
# nunca como techo.
#
# En una máquina de 23 GB eso deja al RGB (12.3 MP: ~6.3 GB por hilo en MVS)
# corriendo solo, que es lo correcto — su etapa densa necesita de verdad esa
# memoria. Multiespectral, banda D y térmico sí comparten.
MIN_HILOS_POR_PROYECTO = 2

# "Peso" en megapíxeles de la fase LIVIANA (todo el SfM: detect/match features,
# reconstrucción incremental, undistort). ODM reduce las fotos a
# feature_process_size (~2048 px de lado mayor) antes de tocarlas, así que el
# footprint por hilo no depende de la resolución nativa del sensor — 2 MP es
# conservador para lo que en la práctica pesa bastante menos. Es el mismo
# número que usa LIGHT_CONCURRENCY en docker/entrypoint.sh; vive acá para que
# el reparto entre proyectos concurrentes lo pueda tener en cuenta.
LIGHT_MP = 2.0

# Tope de proyectos en simultáneo. Dos, no cuatro, a propósito: en esta misma
# máquina ya hubo un cuelgue por memoria con UN proceso pidiendo de más, y el
# grueso de la ganancia está en solapar la fase de SfM incremental (~1 núcleo
# durante horas) de un proyecto con la de otro — para eso alcanza con dos.
# Subirlo es un número acá, no un cambio de diseño.
MAX_PROYECTOS_PARALELOS = 2


def odm_slots(perfiles_mp, ram_frac=0.8, override=None, max_paralelo=None):
    """Cómo repartir la máquina entre varios proyectos ODM que podrían correr
    a la vez.

    `perfiles_mp` son los megapíxeles del sensor de cada proyecto pendiente
    (12.3 RGB, 5 multiespectral/banda D, 0.33 térmico), en el orden en que se
    van a lanzar. Devuelve:

      {"slots": n,            cuántos proyectos corren en simultáneo
       "hilos": [...],        hilos para la fase PESADA (MVS/band alignment)
       "hilos_light": [...]}  hilos para la fase liviana (todo el SfM)

    Las dos listas hacen falta porque run_odm() parte cada proyecto en dos
    invocaciones con concurrencias distintas (ver docker/entrypoint.sh). Si se
    repartiera solo la pesada, dos proyectos solapados pedirían CADA UNO la
    concurrencia liviana entera — que en esta máquina son 16 hilos a ~1 GB, o
    sea 32 GB entre los dos: exactamente el tipo de suma sin coordinar que ya
    causó un cuelgue por memoria.

    `override` (MAX_CONCURRENCY) fija los hilos por proyecto pero NO cuántos
    corren juntos: es la perilla de "cuidame la memoria", y respetarla
    lanzando varios proyectos con ese valor cada uno sería justo lo contrario.
    Con override se cae a un proyecto por vez.
    """
    perfiles = list(perfiles_mp)
    if not perfiles:
        return {"slots": 0, "hilos": [], "hilos_light": []}
    if override is not None:
        fijo = [int(override)] * len(perfiles)
        return {"slots": 1, "hilos": fijo, "hilos_light": list(fijo)}

    tope = MAX_PROYECTOS_PARALELOS if max_paralelo is None else max_paralelo
    tope = max(1, min(tope, len(perfiles)))

    cores = cpu_count()
    avail = mem_available_mb()

    def _reparto(n, mem_mps, gpu_aware=False):
        """Hilos por proyecto si corren `n` a la vez.

        Dos cosas distintas, a propósito:

        · NÚCLEOS, repartidos PROPORCIONALES AL COSTO real de cada proyecto
          (`perfiles`: 12.3 MP RGB, 5 MS/banda D, 0.33 térmico) — no en
          partes iguales. Medido en vivo (misión medellin_palmas): el SfM de
          RGB tardó 33 min con 16 hilos cuando corría solo, y 56 min con 5
          hilos cuando los tres sensores se repartían los núcleos parejo —
          1.69x más lento. Y no compraba nada: térmico (34 min) y
          multiespectral (60 min) terminan MUY dentro de las 2 h de RGB
          incluso con pocos hilos, así que darles la misma tajada que al
          sensor del que depende el final de la corrida solo alarga el
          camino crítico. El que manda el reloj se lleva la mayor parte.

        · MEMORIA (`mem_mps`), sin cambios: cada proyecto sigue acotado
          contra ram_frac/n con su propio perfil. Es el límite duro que evita
          el OOM y ahí no hay nada que optimizar — sobreasignar memoria no
          acelera, cuelga la máquina.

        En la fase liviana los dos argumentos difieren: el peso de núcleos
        sale del perfil REAL del sensor (RGB es el caro) pero el techo de
        memoria usa LIGHT_MP para todos, que es lo que de verdad pesa un
        hilo de SfM sobre fotos ya reducidas a feature_process_size.
        """
        total = sum(perfiles[:n]) or 1.0
        out = []
        for i, mem_mp in enumerate(mem_mps):
            if n <= 1:
                por_proyecto = cores
            else:
                # Los i < n son los que coinciden en la tanda inicial; los que
                # esperan turno heredan la cuota del último de esa tanda.
                peso = perfiles[i if i < n else n - 1] / total
                por_proyecto = max(MIN_HILOS_POR_PROYECTO, round(cores * peso))
            out.append(max(1, min(por_proyecto,
                                  safe_concurrency(mem_mp, ram_frac=ram_frac / n,
                                                   gpu_aware=gpu_aware))))
        return out

    livianos = [LIGHT_MP] * len(perfiles)
    if tope == 1 or not avail or avail <= 0:
        # Sin dato de memoria no se arriesga nada: uno por vez, como siempre.
        return {"slots": 1,
                "hilos": _reparto(1, perfiles, gpu_aware=True),
                "hilos_light": _reparto(1, livianos)}

    slots = 1
    for n in range(tope, 1, -1):
        # Solo importan los n primeros: son los que de verdad van a coincidir
        # en la tanda inicial, y las siguientes usan el mismo reparto. Tienen
        # que entrar con un mínimo digno en AMBAS fases, no solo en una.
        pesados = _reparto(n, perfiles, gpu_aware=True)[:n]
        light = _reparto(n, livianos)[:n]
        if all(h >= MIN_HILOS_POR_PROYECTO for h in pesados + light):
            slots = n
            break

    return {"slots": slots,
            "hilos": _reparto(slots, perfiles, gpu_aware=True),
            "hilos_light": _reparto(slots, livianos)}


# ── Presets de calidad ───────────────────────────────────────────────
# Elegidos por USO Y TIEMPO, que es la decisión real frente a un incendio.
# Cada campo, y por qué su valor (todo medido en misiones reales):
#
# fast_orthophoto — LA palanca grande. Salta DensifyPointCloud (MVS): la
#   ortofoto se arma de la nube DISPERSA de SfM en vez de la densa. Medido en
#   medellin_palmas: MVS solo fueron 28 min de las 2 h de RGB, y malla +
#   texturizado (que también se abaratan) otros 29. El DSM y la nube de
#   puntos exportada SÍ se degradan (salen de la dispersa), por eso `alta`
#   para arriba mantiene la densa: ahí son el producto pedido.
#   Pero el ortomosaico TAMBIÉN se degrada más de lo que el ahorro de tiempo
#   justifica: reportado en vivo, una misión real con fast_orthophoto=True
#   en `estandar` salió con el mosaico fragmentado en 32 islas
#   desconectadas (la malla de la nube dispersa deja huecos reales en zonas
#   de poca textura, aunque las poses de cámara se reconstruyan casi
#   completas). Por eso solo `vistazo`/`rápido` la usan — ahí la prioridad
#   es velocidad y el mosaico es desechable si hace falta reprocesar;
#   `estandar` es "la entrega normal de una misión" y se paga el ~2x de MVS
#   para que salga completo.
#
# matcher_neighbors — nº de vecinos por GPS contra los que se matchea cada
#   foto. 0 = grafo completo, O(n²): en un vuelo de 485 fotos son 235.000
#   pares contra 3.900 con vecindario 8. Estaba en 0 en alta/máxima, que es
#   de dónde salía el grueso de su lentitud — en un vuelo en grilla con GPS
#   los pares lejanos no pueden matchear igual. Se acota en todos los
#   presets; los de calidad usan un vecindario más ancho, no infinito.
#
# pc_quality / feature_quality — resolución de depthmaps y de extracción de
#   features. Cada escalón multiplica el tiempo por ~4 (ODM, --pc-quality
#   --help). pc_quality NO se usa cuando fast_orthophoto está activo (no
#   corre MVS): en esos presets queda en low por coherencia, no por efecto.
#
# res_cm — techo de resolución del ortomosaico y el DSM. ODM nunca va más
#   fino que el GSD real (opendm/gsd.py::cap_resolution) pero sí más grueso
#   si se le pide, y el tamaño del ráster manda en render, recorte, tiles y
#   COG. 1 cm en máxima = "sin techo, dame el GSD real".
#
# min_features — features por foto. La extracción es una de las dos
#   sub-etapas más caras del SfM (108 min en una banda D grande).
#
# sfm_algorithm — `planar` asume vuelo nadir a altura fija y es mucho más
#   rápido, pero descarta fotos en terreno con relieve (misión
#   mision_2026-08-08: de 1199 fotos solo ubicó 283). Solo en los presets
#   rápidos, y TERRENO=escarpado lo fuerza a incremental igual (ver TERRENOS).
#
# hybrid_ba — bundle adjustment local por foto + global cada 100. Sin esto el
#   costo de cada foto crece con la reconstrucción ya armada: una de 224
#   fotos quedó ~40 min en esa sola etapa con la CPU ociosa. Se apaga solo en
#   `maxima`, donde se prioriza la consistencia del ajuste global.
#
# tiempo_relativo — costo relativo a `rapido`, recalibrado contra la corrida
#   completa de medellin_palmas (2 h 46 min en `estandar` con la tabla vieja).
PRESETS = {
    "tactico": {
        "orden": 0,
        "titulo": "Táctico",
        "para_que": "Respuesta inmediata en campo: ortomosaico 2.5D ultrarrápido con ODM multiband seamline blending, sin nube densa.",
        "pc_quality": "low", "feature_quality": "lowest", "res_cm": 10,
        "min_features": 3000, "matcher_neighbors": 4,
        "sfm_algorithm": "incremental", "hybrid_ba": True, "fast_orthophoto": True,
        "tiempo_relativo": 0.25,
    },
    "cartografico": {
        "orden": 1,
        "titulo": "Cartográfico",
        "para_que": "La entrega normal de una misión: ortomosaico corregido, DSM y nube 3D.",
        "pc_quality": "medium", "feature_quality": "high", "res_cm": 4,
        "min_features": 8000, "matcher_neighbors": 8,
        "sfm_algorithm": "incremental", "hybrid_ba": True, "fast_orthophoto": False,
        "tiempo_relativo": 4.0,
    },
    "forense": {
        "orden": 2,
        "titulo": "Forense",
        "para_que": "Peritaje y archivo: el máximo detalle físico que dé el vuelo.",
        "pc_quality": "high", "feature_quality": "ultra", "res_cm": 1,
        "min_features": 16000, "matcher_neighbors": 24,
        "sfm_algorithm": "incremental", "hybrid_ba": False, "fast_orthophoto": False,
        "tiempo_relativo": 16.0,
    },
}
PRESET_DEFAULT = "cartografico"
PRESETS_ORDENADOS = sorted(PRESETS, key=lambda n: PRESETS[n]["orden"])

PRESET_ALIASES = {
    "vistazo": "tactico",
    "rapido": "tactico",
    "estandar": "cartografico",
    "alta": "cartografico",
    "maxima": "forense",
}

# Mapeo del QUALITY 0-100 histórico al preset equivalente, para que las
# corridas, los scripts y el CI que ya pasan un número sigan funcionando.
_QUALITY_A_PRESET = ((90, "forense"), (40, "cartografico"), (0, "tactico"))


# ── Terreno: plano vs escarpado ──────────────────────────────────────
# `sfm_algorithm: planar` alinea las fotos por HOMOGRAFÍAS entre pares.
# `sfm_algorithm: incremental` resuelve posiciones 3D exactas incluso en montaña.
TERRENOS = ("plano", "escarpado")
TERRENO_DEFAULT = "plano"

def preset(nombre, terreno=None):
    """Config de un preset por nombre. Acepta también un QUALITY numérico
    (0-100) por compatibilidad — ver _QUALITY_A_PRESET."""
    if nombre is None:
        nombre = PRESET_DEFAULT
    clave = str(nombre).strip().lower()
    if clave in PRESETS:
        p = dict(PRESETS[clave], nombre=clave)
    elif clave in PRESET_ALIASES:
        destino = PRESET_ALIASES[clave]
        p = dict(PRESETS[destino], nombre=destino, alias=clave)
    elif clave.isdigit():
        n = max(0, min(100, int(clave)))
        p = None
        for corte, destino in _QUALITY_A_PRESET:
            if n >= corte:
                p = dict(PRESETS[destino], nombre=destino)
                break
        if p is None:
            raise ValueError(
                f"preset desconocido: {nombre!r}. Opciones: "
                f"{', '.join(PRESETS_ORDENADOS)} (o un número 0-100 heredado)")
    else:
        raise ValueError(
            f"preset desconocido: {nombre!r}. Opciones: "
            f"{', '.join(PRESETS_ORDENADOS)} (o un número 0-100 heredado)")

    terreno = (terreno or TERRENO_DEFAULT).strip().lower()
    if terreno not in TERRENOS:
        raise ValueError(f"terreno desconocido: {terreno!r}. Opciones: {', '.join(TERRENOS)}")
    p["terreno"] = terreno
    if terreno == "escarpado" and p["sfm_algorithm"] == "planar":
        p["sfm_algorithm"] = "incremental"
    return p


# ── Perfil de calidad (compatibilidad) ───────────────────────────────
# (quality_min, pc_quality, feature_quality, res_cm, tiempo_relativo, label)
#
# pc_quality/feature_quality: resolución de los mapas de profundidad (nube
# densa, malla 2.5D, DSM) — lo que decide si una copa de árbol se resuelve o
# la superficie sale lisa (la causa real de "no parece true-ortho": con
# depthmaps a 320px sobre fotos de 4056px, los árboles no quedan en el modelo
# y se desplazan al proyectarlos). Cada escalón multiplica el tiempo de SfM/MVS
# por ~4 (documentado por ODM en --pc-quality --help).
#
# res_cm: el TECHO de resolución que se le pide a ODM para el ortomosaico y el
# DSM. ODM nunca puede ir más FINO que el GSD real del vuelo (lo mide de la
# reconstrucción y recorta cualquier pedido más ambicioso — ver
# opendm/gsd.py::cap_resolution) pero SÍ puede ir deliberadamente más GRUESO
# si se lo pide un valor mayor, y eso reduce el total de píxeles del ráster
# final — lo que acelera de verdad el renderizado de ortofoto, el recorte de
# bordes, la generación de tiles y la exportación COG, que son proporcionales
# al tamaño del ráster. Por eso 1cm en el escalón de 100% NO es "2 cm mágicos
# de Terra": es "no self-impongas un techo, dame el GSD real completo".
#
# tiempo_relativo: multiplicador contra el escalón MÁS RÁPIDO (25), para dar
# una cifra relativa defendible (viene directo del ~4x/escalón documentado).
#
# hybrid_ba (--use-hybrid-bundle-adjustment de ODM): bundle adjustment LOCAL
# (solo las cámaras cercanas a la que se acaba de agregar) en vez de GLOBAL
# completo en CADA foto agregada a la reconstrucción incremental — con
# ajuste global completo cada 100 fotos para no perder consistencia. Sin
# esto (default de ODM: False) el costo de cada foto agregada crece con el
# tamaño de la reconstrucción ya armada, no es lineal — confirmado en vivo:
# una reconstrucción RGB de 224 fotos quedó ~40 min en esta sola etapa con
# la CPU casi ociosa (2 núcleos, nada por paralelizar: bundle adjustment
# incremental es secuencial por diseño, no un problema de concurrencia).
# Se activa en todos los escalones salvo "ultra" — ahí el usuario ya está
# pagando 64x el tiempo base a propósito por el máximo detalle posible, así
# que se prioriza la consistencia del ajuste global sobre la velocidad.
#
# matcher_neighbors: 0 = grafo completo (cada foto contra TODAS las demás),
# el costo real detrás de reconstrucciones lentas con muchas fotos — bug
# real, reportado en vivo: estaba hardcodeado en 0 en docker/entrypoint.sh
# SIN importar la calidad elegida, así que "calidad mínima" seguía pagando
# el matching más caro posible. En ultra/high se mantiene el grafo completo
# (ahí importa más no perderse pares de fotos que podrían cerrar un loop),
# pero medium/low/lowest usan un vecindario acotado — cada foto solo se
# empareja contra sus 8 más cercanas, que es lo que de verdad tira abajo el
# tiempo en vuelos de cientos de fotos.
# Este bloque ya no define la tabla: PRESETS es la fuente única. Queda como
# envoltorio para lo que sigue hablando en números 0-100 (corridas guardadas,
# CI, `./raptor run --quality N`), que se mapean al preset equivalente.
def quality_tier(quality):
    """Config del preset correspondiente a un QUALITY 0-100, con las claves
    que usaba la tabla vieja (`label`, `quality`) además de las nuevas."""
    n = max(0, min(100, int(quality)))
    p = preset(str(n))
    return dict(p, quality=n, label=p["titulo"].lower())


# ── Estimación de tiempo ─────────────────────────────────────────────
# Modelo POR ETAPAS, calibrado contra las corridas reales más grandes de
# esta máquina (laptop 20 núcleos, RTX A1000 6 GB, 23 GB RAM):
#
#   barbosa-picodegallo (3149 imágenes, preset «alta»): 24 h 15 min
#   mision_2026-08-08     (2398 imágenes, «vistazo»+escarpado): 17 h 42 min
#     → RGB 10.3 h (etapa opensfm 8.5 h) + térmico 6.2 h (MVS 3.3 h)
#
# El modelo anterior era un solo número por foto (16.4 s) multiplicado por
# el tiempo_relativo del preset — y sobre mision_2026-08-08 prometía
# "31 min – 2.1 h" contra las 17.7 h reales (≈10x optimista). La causa de
# fondo: la corrección de escarpado (×3 sobre el tiempo_relativo) no
# alcanza para la reconstrucción incremental, que es SECUENCIAL por diseño
# (bundle adjustment foto por foto, ~12 s/foto medidos) y no se paraleliza
# entre fotos; y el térmico paga la fase densa completa aunque su sensor
# sea de 640×512. Un escalar por preset no puede representar eso.
#
# Ahora el modelo suma costos POR FASE, cada uno con su propia dependencia:
#
#   features/matching/undistort  s/foto, PARALELA (se divide por núcleos)
#   incremental (BA foto a foto)  s/foto, SECUENCIAL (no se divide por nada)
#   densa (DensifyPointCloud)     s/foto, solo si NO hay --fast-orthophoto,
#                                 escala con pc_quality y con la GPU (VRAM)
#   malla+textura                 s/foto, PARALELA, más barata con
#                                 --fast-orthophoto (malla desde la nube
#                                 dispersa en vez de la densa)
#
# Primera calibración, contra los substages reales de mision_2026-08-08
# (outputs/logs/odm_rgb_substages.json): features 13 s/foto, incremental
# 12.4 s, densa 13.7 s, malla+textura 5.1 s. Esa corrida es ANTERIOR a que
# hybrid_ba y matcher_neighbors acotado estuvieran cableados en el preset
# (ver docker/entrypoint.sh) — mide un pipeline más lento del que hay hoy,
# no un techo a igualar.
#
# Recalibrados contra medellin_palmas (485 fotos RGB, 16 núcleos / 5 hilos en
# SfM, RTX 2080, YA con hybrid_ba/matcher_neighbors activos): 119 min de ODM
# repartidos en features 3.53 s/foto, incremental 3.39, densa 3.51,
# malla+textura 3.56 — valores "por foto a 1 hilo" (el modelo los divide por
# el paralelismo efectivo) salvo el incremental, que no escala con núcleos.
# Con esto el rango YA NO cubre las corridas viejas (barbosa-picodegallo
# 24h15min, mision_2026-08-08 17h42min) a propósito: esas corridas pagaron
# el costo del bundle adjustment SIN hybrid_ba (~3.5x más caro, ver
# _INCR_SIN_HYBRID_BA) y con grafo de matching sin acotar. Seguir prometiendo
# esos números sería no contar la mejora real. Sigue siendo una
# aproximación (RANGO 0.5x-2x, rotulada "aproximada"): la escena real pesa
# más que cualquier coeficiente.
_SEG_FEATURES = 7.4    # features+matching+undistort, s/foto, paralela
_SEG_INCR = 3.4        # bundle adjustment incremental CON hybrid_ba, s/foto
_SEG_MVS = 3.7         # DensifyPointCloud, s/foto, sobre GPU de referencia
_SEG_MESH = 11.9       # malla+textura, s/foto, paralela

# hybrid_ba apaga el ajuste global en cada foto: sin él el costo del
# incremental crece con la reconstrucción ya armada (una de 224 fotos quedó
# ~40 min en esa sola etapa). Solo `maxima` lo desactiva. El modelo lo
# ignoraba por completo y por eso subestimaba ese preset.
_INCR_SIN_HYBRID_BA = 3.5

# pc_quality/feature_quality escalan las fases que resuelven (mapas de
# profundidad y features). Cada escalón de pc_quality es ~4x el tiempo de
# SfM/MVS (documentado por ODM en --pc-quality --help) — acá como
# multiplicador por nivel, amortiguado (no todo el SfM es MVS).
_PC_Q_FACTOR = {"lowest": 0.6, "low": 1.0, "medium": 1.6, "high": 2.8, "ultra": 5.0}
_FEAT_Q_FACTOR = {"lowest": 0.6, "low": 1.0, "medium": 1.2, "high": 1.6, "ultra": 2.2}

# Peso del costo POR ARCHIVO de cada sensor, relativo a RGB=1.0. Medido en
# medellin_palmas con la misma tabla y el mismo hardware para los tres:
# RGB 119 min/485 fotos, térmico 34/485, multiespectral 60/1092 bandas.
#   térmico 0.29 — 640x512 (0.33 MP) extrae y empareja mucho más barato.
#   multiespectral 0.22 — se cuenta por ARCHIVO de banda, pero solo la banda
#     primaria pasa por el SfM; las otras tres solo se deshacen la distorsión
#     y se alinean (~1/4 del costo, que es justo lo que dio la medición).
# Estaban en 0.55 y 1.10 "ajustados en orden de magnitud": el multiespectral
# quedaba estimado 5x de más y aparecía como camino crítico cuando no lo es.
_SENSOR_PESO = {"rgb": 1.0, "thermal": 0.29, "multispectral": 0.22, "dband": 1.0}

_TITULO_SENSOR = {"rgb": "RGB", "thermal": "térmico",
                  "multispectral": "multiespectral", "dband": "banda D"}
_TITULO_SENSOR_EN = {"rgb": "RGB", "thermal": "thermal",
                     "multispectral": "multispectral", "dband": "D band"}

# Traducción de titulo/para_que para el mensaje en inglés (estimate_message,
# preset_options): PRESETS arriba es la fuente única de los VALORES
# (pc_quality, min_features, etc.), pero titulo/para_que son texto para
# mostrar y necesitan su propia versión en cada idioma — igual que el
# webapp/static/index.html::PRESET_UI_OVERRIDE del lado del cliente, que
# traduce lo mismo para las tarjetas visibles ahí (vistazo/estandar/maxima).
_PRESETS_EN = {
    "tactico":      {"titulo": "Tactical",     "para_que": "Immediate field response: fast 2.5D orthomosaic with ODM seamline blending, no dense cloud."},
    "cartografico": {"titulo": "Cartographic", "para_que": "Standard mission delivery: corrected orthomosaic, DSM and 3D cloud."},
    "forense":      {"titulo": "Forensic",     "para_que": "Archival and forensic detail: maximum physical resolution the flight can deliver."},
    # Aliases
    "vistazo":      {"titulo": "Tactical",     "para_que": "Immediate field response: fast 2.5D orthomosaic with ODM seamline blending, no dense cloud."},
    "rapido":       {"titulo": "Tactical",     "para_que": "Immediate field response: fast 2.5D orthomosaic with ODM seamline blending, no dense cloud."},
    "estandar":     {"titulo": "Cartographic", "para_que": "Standard mission delivery: corrected orthomosaic, DSM and 3D cloud."},
    "alta":         {"titulo": "Cartographic", "para_que": "Standard mission delivery: corrected orthomosaic, DSM and 3D cloud."},
    "maxima":       {"titulo": "Forensic",     "para_que": "Archival and forensic detail: maximum physical resolution the flight can deliver."},
}


def _gpu_factor(vram_mb, has_gpu):
    """Cuánto acelera la GPU de verdad según su VRAM (no "sí/no").

    La reconstrucción densa (MVS/OpenMVS) escala los depthmaps a la memoria
    de la GPU: con poca VRAM baja la resolución efectiva de los mapas de
    profundidad y el resultado se acerca al de correr en CPU. Una GPU de
    laptop/entry (≤8 GB) acelera apenas; recién una GPU de estación
    (16 GB+) hace valer el factor alto."""
    if not has_gpu:
        return 1.0
    if vram_mb is None:
        return 0.85  # GPU detectada pero VRAM desconocida: conservador
    if vram_mb < 8 * 1024:
        return 0.85
    if vram_mb < 16 * 1024:
        return 0.6
    return 0.4


def _paralelismo_efectivo(cores):
    """Núcleos con rendimientos decrecientes (overhead de coordinación): no
    se divide linealmente por núcleo."""
    return max(1, cores) ** 0.75


def _seconds_per_photo(tier, hw):
    """Segundos por foto de TODA la reconstrucción de un sensor con este
    preset y este hardware — suma de fases, ver el comentario del modelo."""
    par = _paralelismo_efectivo(hw["cores"])
    factor_gpu = _gpu_factor(hw.get("vram_mb"), hw.get("gpu", False))
    fast = tier["fast_orthophoto"]
    feats = (_SEG_FEATURES * _FEAT_Q_FACTOR.get(tier["feature_quality"], 1.0)) / par
    # Planar sigue haciendo un alineamiento barato por pares, no la
    # reconstrucción foto por foto (que es la que domina en incremental).
    if tier["sfm_algorithm"] == "incremental":
        incr = _SEG_INCR * (1.0 if tier["hybrid_ba"] else _INCR_SIN_HYBRID_BA) * (0.5 if fast else 1.0)
    else:
        incr = (_SEG_FEATURES * 0.2) / par
    # matcher_neighbors acota los pares a emparejar: el costo del matching es
    # proporcional a cuántos vecinos se prueban por foto (8 es la referencia
    # de la calibración). Estaba fuera del modelo pese a ser justo la palanca
    # que hacía lentos a alta/máxima cuando usaban grafo completo.
    feats *= max(1, tier["matcher_neighbors"]) / 8.0
    mvs = 0.0 if fast else _SEG_MVS * _PC_Q_FACTOR.get(tier["pc_quality"], 1.0) * factor_gpu
    mesh = (_SEG_MESH * (0.25 if fast else 1.0)) / par
    return feats + incr + mvs + mesh


def estimate_minutes(quality, n_photos, hw=None, terreno=None):
    """(min_low, min_high) minutos estimados, MUY aproximados a propósito.
    `quality` es un nombre de preset o un QUALITY 0-100 heredado.

    Modelo por etapas calibrado contra corridas reales, incluyendo con
    hybrid_ba/matcher_neighbors ya cableados (ver el comentario del modelo
    arriba) — nunca "minutos" para una reconstrucción incremental grande,
    aunque ya no repita el piso pre-optimización de "31 min – 2.1 h"."""
    if n_photos <= 0:
        return (0.0, 0.0)
    hw = hw or detect_hardware()
    tier = preset(quality, terreno=terreno)
    minutos = n_photos * _seconds_per_photo(tier, hw) / 60
    # Banda ancha (0.5x-2x) a propósito: el modelo es aproximado y la escena
    # real (solape, vegetación) pesa más que cualquier coeficiente. Un rango
    # angosto promete una precisión que no existe.
    return (round(minutos * 0.5, 1), round(minutos * 2.0, 1))


def estimate_sensor_minutes(sensor, n_photos, quality, hw=None, terreno=None):
    """(min_low, min_high) para UN sensor con n_photos fotos — lo que usa
    estimate_message() con `por_sensor` para mostrar "térmico ≈ X–Y, RGB ≈
    X–Y" en vez de un solo número que mezcla sensores con costos muy
    distintos (un térmico de 0.33 MP no cuesta lo mismo que un RGB de 12 MP)."""
    if n_photos <= 0:
        return (0.0, 0.0)
    hw = hw or detect_hardware()
    tier = preset(quality, terreno=terreno)
    minutos = n_photos * _seconds_per_photo(tier, hw) * _SENSOR_PESO.get(sensor, 1.0) / 60
    return (round(minutos * 0.5, 1), round(minutos * 2.0, 1))


def _fmt_minutos(lo, hi, idioma="es"):
    def _f(m):
        if m < 1:
            return "less than 1 min" if idioma == "en" else "menos de 1 min"
        if m < 90:
            return f"{m:.0f} min"
        return f"{m/60:.1f} h"
    if hi <= 0:
        return "—"
    return f"{_f(lo)} – {_f(hi)}" if _f(lo) != _f(hi) else _f(lo)


def _texto_hardware(hw, idioma="es"):
    if idioma == "en":
        partes = [f"{hw['cores']} cores"]
        if hw.get("mem_available_mb"):
            partes.append(f"{hw['mem_available_mb']/1024:.0f} GB RAM free")
        if hw.get("gpu"):
            vram = hw.get("vram_mb")
            partes.append(f"GPU: {hw['gpu_name'] or 'yes'}"
                          + (f" ({vram/1024:.0f} GB VRAM)" if vram else ""))
        else:
            partes.append("no GPU (CPU)")
        return partes
    partes = [f"{hw['cores']} núcleos"]
    if hw.get("mem_available_mb"):
        partes.append(f"{hw['mem_available_mb']/1024:.0f} GB RAM libres")
    if hw.get("gpu"):
        vram = hw.get("vram_mb")
        partes.append(f"GPU: {hw['gpu_name'] or 'sí'}"
                      + (f" ({vram/1024:.0f} GB VRAM)" if vram else ""))
    else:
        partes.append("sin GPU (CPU)")
    return partes


def preset_options(n_photos, hw=None, terreno=None, idioma="es"):
    """Todos los presets con su tiempo estimado PARA ESTA misión.

    Es lo que hace que la elección sea informada: el operador ve los cinco
    con su número al lado y elige por tiempo, en vez de mover un slider de
    0 a 100 sin saber qué compra cada tramo."""
    hw = hw or detect_hardware()
    opciones = []
    for nombre in PRESETS_ORDENADOS:
        p = preset(nombre, terreno=terreno)
        textos = _PRESETS_EN[nombre] if idioma == "en" else p
        lo, hi = estimate_minutes(nombre, n_photos, hw, terreno=terreno)
        opciones.append({
            "nombre": nombre,
            "titulo": textos["titulo"],
            "para_que": textos["para_que"],
            "res_cm": p["res_cm"],
            "pc_quality": p["pc_quality"],
            "sfm_algorithm": p["sfm_algorithm"],
            "minutos_estimados": [lo, hi],
            "tiempo_texto": _fmt_minutos(lo, hi, idioma),
            "por_defecto": nombre == PRESET_DEFAULT,
        })
    return opciones


def estimate_message(quality, n_photos, hw=None, terreno=None, por_sensor=None, idioma="es"):
    """Mensaje completo (texto + datos crudos) para mostrarle al usuario
    ANTES de arrancar: a qué resolución van a salir los productos y cuánto se
    espera que tarde, con las cuentas claras de por qué.

    `por_sensor` (opcional): dict {sensor: fotos} para desglosar el tiempo
    por sensor (térmico ≈ X–Y, RGB ≈ X–Y) en vez de un solo número — los
    sensores tienen costos por foto muy distintos y, en modo escarpado, el
    térmico termina mucho antes que el RGB. Lo usan el entrypoint y la
    webapp, que saben cuántas fotos hay de cada uno.

    `idioma`: 'es' (default, sin tocar nada de lo que ya devolvía esta
    función) o 'en'. Los presets (PRESETS) y sus valores numéricos son la
    misma fuente única para los dos — solo el TEXTO armado acá cambia."""
    hw = hw or detect_hardware()
    tier = preset(quality, terreno=terreno)
    en = idioma == "en"
    titulo_tier = _PRESETS_EN[tier["nombre"]]["titulo"] if en else tier["titulo"]
    para_que_tier = _PRESETS_EN[tier["nombre"]]["para_que"] if en else tier["para_que"]
    titulo_sensor = _TITULO_SENSOR_EN if en else _TITULO_SENSOR
    partes_hw = _texto_hardware(hw, idioma)
    aviso_escarpado = (PRESETS[tier["nombre"]]["sfm_algorithm"] != tier["sfm_algorithm"])
    if por_sensor:
        activos = {s: n for s, n in por_sensor.items() if n > 0}
        # Los sensores son proyectos ODM independientes y corren en paralelo
        # cuando la RAM alcanza (ver odm_slots), o de a uno cuando no. El
        # rango cubre los dos regímenes a propósito: el piso es el CAMINO
        # CRÍTICO (todos solapados: manda el más lento, normalmente RGB) y el
        # techo es la SUMA (nada solapa). Antes sumaba en los dos extremos, y
        # sobre una máquina que sí solapa prometía casi el doble del tiempo
        # real — medido en palmas: 119 min de camino crítico contra 213 de
        # suma.
        por_s = {s: estimate_sensor_minutes(s, n, quality, hw, terreno=terreno)
                 for s, n in activos.items()}
        lo = max((v[0] for v in por_s.values()), default=0.0)
        hi = sum(v[1] for v in por_s.values())
        desglose = " · ".join(
            f"{titulo_sensor.get(s, s)} ≈ "
            f"{_fmt_minutos(*estimate_sensor_minutes(s, n, quality, hw, terreno=terreno), idioma)}"
            for s, n in activos.items())
        lo_hi_por_sensor = {s: list(estimate_sensor_minutes(s, n, quality, hw, terreno=terreno))
                            for s, n in activos.items()}
    else:
        lo, hi = estimate_minutes(quality, n_photos, hw, terreno=terreno)
        desglose = None
        lo_hi_por_sensor = None

    if en:
        resolucion = (
            f"Up to {tier['res_cm']} cm/px in the orthomosaic and DSM — the real "
            f"ceiling is set by your flight's GSD (altitude × sensor): if the "
            f"flight has less detail than that, it comes out at the most the "
            f"flight allows; never finer, no matter the preset chosen."
        )
        modelo = (
            f'Preset "{titulo_tier}": {para_que_tier} Point cloud and mesh at '
            f"'{tier['pc_quality']}' level, {tier['min_features']} features per "
            f"photo. Higher resolves objects like treetops and roof edges "
            f"better — lower is faster but can flatten those details in the "
            f"surface model."
            + (" Planar reconstruction (assumes a fixed-altitude nadir flight), "
               "much faster in the incremental stage."
               if tier["sfm_algorithm"] == "planar" else
               " Full incremental reconstruction.")
            + (" Hybrid bundle adjustment (local per photo, global every 100) "
               "to avoid wasting time on large flights."
               if tier["hybrid_ba"] else
               " Full global bundle adjustment on every photo — the most "
               "precise option, slower on large flights.")
            + (" The RGB flight's surface model (DSM) comes from the SPARSE "
               "point cloud (dense reconstruction is skipped) — on flights of "
               "hundreds of photos that's the difference between minutes and "
               "hours, but the resulting DSM is poorer: fewer points, more "
               "gaps."
               if tier["fast_orthophoto"] else "")
            + (f' Steep terrain: incremental reconstruction is forced even '
               f'though "{titulo_tier}" defaults to planar — on terrain with '
               f"strong relief, planar reconstruction can silently drop most "
               f"photos (confirmed live: 76-81% of photos lost on a "
               f"rocky-terrain mission). ⚠ And the cost is high: incremental "
               f"reconstruction is sequential (~12 s per photo, not "
               f"parallelized across photos) and comes to dominate the time "
               f"— on a flight of hundreds of photos, expect HOURS per "
               f"sensor, not minutes. Urgent mode (1 out of every 3 photos) "
               f"or subsampling shorten that wait."
               if aviso_escarpado else "")
        )
        if desglose:
            tiempo = (
                f"Estimated time with your hardware ({', '.join(partes_hw)}): "
                f"{_fmt_minutos(lo, hi, idioma)} for {n_photos} photos. "
                f"Approximate — the real scene (overlap, vegetation) weighs "
                f"more than this number."
                f" By sensor: {desglose} (sensors reconstruct in parallel "
                f"when available RAM allows it — the total can finish before "
                f"the sum)."
            )
        else:
            tiempo = (
                f"Estimated time with your hardware ({', '.join(partes_hw)}): "
                f"{_fmt_minutos(lo, hi, idioma)} for {n_photos} photos. "
                f"Approximate — the real scene (overlap, vegetation) weighs "
                f"more than this number."
            )
        if tier["nombre"] not in ("tactico", "vistazo") and n_photos > 0:
            vis = _fmt_minutos(*estimate_minutes("tactico", n_photos, hw, terreno=terreno), idioma)
            tiempo += (f' For a quick response: "{_PRESETS_EN["tactico"]["titulo"]}" ≈ {vis} — same flight, '
                       f'orthomosaic from the sparse cloud, without the dense stage.')
        return {"tier": tier, "preset": tier["nombre"], "terreno": tier["terreno"],
                "hardware": hw, "n_photos": n_photos, "minutos_estimados": [lo, hi],
                "por_sensor": lo_hi_por_sensor, "aviso_escarpado": aviso_escarpado,
                "opciones": preset_options(n_photos, hw, terreno=terreno, idioma=idioma),
                "resolucion_texto": resolucion, "modelo_texto": modelo,
                "tiempo_texto": tiempo}

    resolucion = (
        f"Hasta {tier['res_cm']} cm/px en el ortomosaico y el DSM — el techo real "
        f"lo pone el GSD de tu vuelo (altura × sensor): si el vuelo da menos detalle "
        f"que eso, sale al máximo que el vuelo permita; nunca más fino, sin importar "
        f"el preset elegido."
    )
    modelo = (
        f"Preset «{tier['titulo']}»: {tier['para_que']} Nube de puntos y malla en "
        f"nivel '{tier['pc_quality']}', {tier['min_features']} features por foto. "
        f"Más alto resuelve mejor objetos como copas de árboles y bordes de tejados "
        f"— más bajo es más rápido pero puede aplanar esos detalles en el modelo de "
        f"superficie."
        + (" Reconstrucción planar (asume vuelo nadir a altura fija), mucho más "
           "rápida en la etapa incremental."
           if tier["sfm_algorithm"] == "planar" else
           " Reconstrucción incremental completa.")
        + (" Bundle adjustment híbrido (ajuste local por foto, global cada 100) "
           "para no perder tiempo de más con vuelos grandes."
           if tier["hybrid_ba"] else
           " Bundle adjustment global completo en cada foto — la opción más "
           "precisa, más lenta en vuelos grandes.")
        + (" El modelo de superficie (DSM) del vuelo RGB sale de la nube de "
           "puntos DISPERSA (se salta la reconstrucción densa) — en vuelos de "
           "cientos de fotos eso es la diferencia entre minutos y horas, pero "
           "el DSM resultante es más pobre: menos puntos, más huecos."
           if tier["fast_orthophoto"] else "")
        + (f" Terreno escarpado: se fuerza reconstrucción incremental aunque "
           f"«{tier['titulo']}» por defecto sea planar — en terreno con "
           f"relieve fuerte, la reconstrucción planar puede descartar la "
           f"mayoría de las fotos en silencio (confirmado en vivo: 76-81% de "
           f"las fotos perdidas en una misión de terreno rocoso). "
           f"⚠ Y el costo es alto: la reconstrucción incremental es secuencial "
           f"(~12 s por foto, no se paraleliza entre fotos) y pasa a dominar "
           f"el tiempo — en un vuelo de cientos de fotos esperá HORAS por "
           f"sensor, no minutos. El modo urgencia (1 de cada 3 fotos) o el "
           f"submuestreo acortan esa espera."
           if aviso_escarpado else "")
    )
    if desglose:
        tiempo = (
            f"Tiempo estimado con tu hardware ({', '.join(partes_hw)}): "
            f"{_fmt_minutos(lo, hi)} para {n_photos} fotos. Aproximado — la escena real "
            f"(solape, vegetación) pesa más que este número."
            f" Por sensor: {desglose} (los sensores reconstruyen en paralelo "
            f"cuando la RAM disponible lo permite — el total puede terminar "
            f"antes que la suma)."
        )
    else:
        tiempo = (
            f"Tiempo estimado con tu hardware ({', '.join(partes_hw)}): "
            f"{_fmt_minutos(lo, hi)} para {n_photos} fotos. Aproximado — la escena real "
            f"(solape, vegetación) pesa más que este número."
        )
    # Guía de respuesta rápida: se ofrece justo en los presets que pagan la
    # reconstrucción densa (fast_orthophoto=False), que es lo que de verdad
    # separa "minutos" de "horas". Atado a esa bandera y no a un umbral de
    # tiempo_relativo: la escala se recalibra con cada corrida real y el
    # número mágico quedaba desactualizado sin que nada avisara.
    if tier["nombre"] not in ("tactico", "vistazo") and n_photos > 0:
        vis = _fmt_minutos(*estimate_minutes("tactico", n_photos, hw, terreno=terreno))
        tiempo += (f" Para respuesta rápida: «Táctico» ≈ {vis} "
                   f"— mismo vuelo, ortomosaico desde la nube dispersa, sin la etapa densa.")
    return {"tier": tier, "preset": tier["nombre"], "terreno": tier["terreno"],
            "hardware": hw, "n_photos": n_photos, "minutos_estimados": [lo, hi],
            "por_sensor": lo_hi_por_sensor, "aviso_escarpado": aviso_escarpado,
            "opciones": preset_options(n_photos, hw, terreno=terreno),
            "resolucion_texto": resolucion, "modelo_texto": modelo,
            "tiempo_texto": tiempo}


# ── CLI ───────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    pc = sub.add_parser("concurrency")
    pc.add_argument("megapixels", type=float)
    sl = sub.add_parser("odm-slots")
    sl.add_argument("megapixels", type=float, nargs="+",
                    help="MP del sensor de cada proyecto ODM pendiente, en orden")
    sl.add_argument("--max-paralelo", type=int, default=None)
    sl.add_argument("--shell", action="store_true",
                    help="3 líneas (slots / hilos / hilos_light) en vez de JSON, "
                         "para leerlas de bash con un solo arranque de Python")
    qt = sub.add_parser("quality-tier")
    qt.add_argument("quality", type=int)
    pr = sub.add_parser("preset", help="config de un preset (o de un QUALITY 0-100)")
    pr.add_argument("nombre")
    pr.add_argument("--terreno", default=None,
                    help="'plano' (default) o 'escarpado' — 'escarpado' fuerza "
                         "incremental incluso en vistazo/rápido, ver TERRENOS arriba")
    pr.add_argument("--shell", action="store_true",
                    help="una línea por campo, en el orden que lee "
                         "docker/entrypoint.sh (un solo arranque de Python)")
    sub.add_parser("presets", help="lista de presets disponibles, en orden")
    es = sub.add_parser("estimate")
    # `--preset` es la forma nueva; `--quality N` sigue aceptándose porque hay
    # corridas, scripts y CI que la pasan.
    es.add_argument("--preset", default=None)
    es.add_argument("--quality", type=int, default=None)
    es.add_argument("--terreno", default=None)
    es.add_argument("--photos", type=int, required=True)
    args = p.parse_args()

    if args.cmd == "info":
        print(json.dumps(detect_hardware()))
    elif args.cmd == "concurrency":
        override = os.environ.get("MAX_CONCURRENCY")
        print(safe_concurrency(args.megapixels,
                               override=int(override) if override else None))
    elif args.cmd == "odm-slots":
        override = os.environ.get("MAX_CONCURRENCY")
        r = odm_slots(args.megapixels,
                      override=int(override) if override else None,
                      max_paralelo=args.max_paralelo)
        if args.shell:
            # Arrancar el intérprete de Python cuesta ~0.3 s, y esto se
            # consulta justo antes de la primera reconstrucción: pedirlo tres
            # veces (una por campo) retrasaba un segundo el arranque de ODM.
            print(r["slots"])
            print(" ".join(str(h) for h in r["hilos"]))
            print(" ".join(str(h) for h in r["hilos_light"]))
        else:
            print(json.dumps(r))
    elif args.cmd == "quality-tier":
        print(json.dumps(quality_tier(args.quality)))
    elif args.cmd == "preset":
        try:
            p = preset(args.nombre, terreno=args.terreno)
        except ValueError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        if args.shell:
            # Orden FIJO — docker/entrypoint.sh lo lee posicionalmente con
            # `read`. Antes eran seis invocaciones de python3 seguidas (una
            # por campo) solo para arrancar la corrida.
            for clave in ("nombre", "titulo", "pc_quality", "feature_quality",
                          "res_cm", "min_features", "matcher_neighbors",
                          "sfm_algorithm"):
                print(p[clave])
            print("1" if p["hybrid_ba"] else "0")
            print("1" if p["fast_orthophoto"] else "0")
        else:
            print(json.dumps(p))
    elif args.cmd == "presets":
        print(json.dumps([dict(PRESETS[n], nombre=n) for n in PRESETS_ORDENADOS]))
    elif args.cmd == "estimate":
        elegido = args.preset if args.preset is not None else args.quality
        if elegido is None:
            elegido = PRESET_DEFAULT
        print(json.dumps(estimate_message(elegido, args.photos, terreno=args.terreno)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
