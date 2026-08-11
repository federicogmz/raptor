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


def safe_concurrency(megapixels, ram_frac=0.8, override=None):
    """Hilos seguros para una etapa de ODM que carga ~megapixels/2 GB por
    hilo (documentado por el propio ODM: "~1GB per thread and 2 megapixel
    image resolution"). Nunca más que los núcleos reales; nunca menos de 1.

    `override` gana siempre — es MAX_CONCURRENCY, la vía de escape manual.
    Generaliza lo que antes solo protegía al multiespectral (el bug de OOM en
    band alignment): CUALQUIER etapa de ODM usa todos los núcleos si no se le
    dice lo contrario, así que el mismo riesgo existe en RGB y en térmico
    sobre una máquina con muchos núcleos y fotos grandes.
    """
    if override is not None:
        return int(override)
    cores = cpu_count()
    avail = mem_available_mb()
    if not avail or avail <= 0:
        return cores
    mb_per_thread = max(512, int(megapixels * 1024 / 2))
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

    def _reparto(n, mps):
        """Hilos por proyecto si corren `n` a la vez: cada uno se lleva 1/n
        del presupuesto de memoria y de los núcleos."""
        por_proyecto = max(1, cores // n)
        return [max(1, min(por_proyecto, safe_concurrency(mp, ram_frac=ram_frac / n)))
                for mp in mps]

    livianos = [LIGHT_MP] * len(perfiles)
    if tope == 1 or not avail or avail <= 0:
        # Sin dato de memoria no se arriesga nada: uno por vez, como siempre.
        return {"slots": 1,
                "hilos": _reparto(1, perfiles),
                "hilos_light": _reparto(1, livianos)}

    slots = 1
    for n in range(tope, 1, -1):
        # Solo importan los n primeros: son los que de verdad van a coincidir
        # en la tanda inicial, y las siguientes usan el mismo reparto. Tienen
        # que entrar con un mínimo digno en AMBAS fases, no solo en una.
        pesados = _reparto(n, perfiles)[:n]
        light = _reparto(n, livianos)[:n]
        if all(h >= MIN_HILOS_POR_PROYECTO for h in pesados + light):
            slots = n
            break

    return {"slots": slots,
            "hilos": _reparto(slots, perfiles),
            "hilos_light": _reparto(slots, livianos)}


# ── Presets de calidad ───────────────────────────────────────────────
# Antes esto era un número de 0 a 100 con cinco escalones adentro. El número
# no le decía nada al operador: "75" no responde ni cuánto va a tardar ni para
# qué sirve, y la diferencia real entre 71 y 89 era ninguna (mismo escalón).
# Ahora son presets con nombre, elegidos por USO y TIEMPO, que es la decisión
# que de verdad se toma frente a un incendio: "necesito mirar algo en una
# hora" vs. "esto es la entrega".
#
# Cada preset fija cinco cosas:
#
# pc_quality/feature_quality: resolución de los mapas de profundidad (nube
# densa, malla 2.5D, DSM) — lo que decide si una copa de árbol se resuelve o
# la superficie sale lisa (la causa real de "no parece true-ortho": con
# depthmaps a 320px sobre fotos de 4056px, los árboles no quedan en el modelo
# y se desplazan al proyectarlos). Cada escalón multiplica el tiempo de
# SfM/MVS por ~4 (documentado por ODM en --pc-quality --help).
#
# res_cm: el TECHO de resolución que se le pide a ODM para el ortomosaico y el
# DSM. ODM nunca puede ir más FINO que el GSD real del vuelo (lo mide de la
# reconstrucción y recorta cualquier pedido más ambicioso — ver
# opendm/gsd.py::cap_resolution) pero SÍ puede ir deliberadamente más GRUESO
# si se lo pide un valor mayor, y eso reduce el total de píxeles del ráster
# final — lo que acelera de verdad el renderizado de ortofoto, el recorte de
# bordes, la generación de tiles y la exportación COG, que son proporcionales
# al tamaño del ráster. Por eso 1 cm en el preset máximo NO es "2 cm mágicos
# de Terra": es "no te autoimpongas un techo, dame el GSD real completo".
#
# min_features: cuántos features pide ODM por foto. Estaba hardcodeado en
# 12000 (8000 en térmico) sin importar la calidad elegida, y la extracción de
# features es una de las dos sub-etapas más caras del SfM (108 min en la banda
# D de barbosa-picodegallo). Pedir menos en los presets rápidos es
# exactamente lo que un vistazo necesita.
#
# sfm_algorithm: `planar` (ODM lo documenta como "for planar scenes captured
# at fixed altitude with nadir-only images, planar can be much faster") ataca
# la etapa que domina las corridas largas — la reconstrucción incremental, que
# es secuencial por diseño y se llevó 4 h 27 min de las 10 h de la banda D.
# Es el perfil exacto de estos vuelos, pero asume nadir y altura fija, así que
# vive SOLO en los presets rápidos: de `estandar` para arriba sigue
# `incremental`, que es lo que corrió siempre.
#
# hybrid_ba (--use-hybrid-bundle-adjustment de ODM): bundle adjustment LOCAL
# (solo las cámaras cercanas a la que se acaba de agregar) en vez de GLOBAL
# completo en CADA foto agregada — con ajuste global completo cada 100 fotos
# para no perder consistencia. Sin esto (default de ODM: False) el costo de
# cada foto agregada crece con el tamaño de la reconstrucción ya armada, no es
# lineal — confirmado en vivo: una reconstrucción RGB de 224 fotos quedó ~40
# min en esta sola etapa con la CPU casi ociosa. Se apaga solo en `maxima`:
# ahí el usuario ya está pagando el máximo tiempo a propósito, así que se
# prioriza la consistencia del ajuste global.
#
# matcher_neighbors: 0 = grafo completo (cada foto contra TODAS las demás), el
# costo real detrás de reconstrucciones lentas con muchas fotos — bug real:
# estaba hardcodeado en 0 sin importar la calidad, así que "calidad mínima"
# seguía pagando el matching más caro posible. En alta/máxima se mantiene el
# grafo completo (ahí importa más no perderse pares que podrían cerrar un
# loop); el resto usa un vecindario acotado de 8.
#
# fast_orthophoto (RGB únicamente — ver docker/entrypoint.sh::_odm_args):
# salta DensifyPointCloud (MVS), el mismo salto que banda D ya usa siempre y
# por la misma razón. Confirmado en vivo (misión mision_2026-08-08, 1199
# fotos RGB, preset vistazo): DensifyPointCloud solo se llevó ~11h de las
# ~15h totales de esa reconstrucción, con GPU activa — con una sola GPU de
# laptop el costo es ~lineal por foto y "pc_quality: low" reduce la
# resolución de cada mapa de profundidad, no cuántas fotos hay que pasar por
# la GPU. Con --fast-orthophoto el DSM sale de la nube DISPERSA de SfM en vez
# de la densa (mucho más pobre — menos puntos, más huecos), pero vistazo y
# rápido ya asumen ese trade-off a cambio de pasar de horas a minutos en
# misiones grandes. estandar/alta/maxima mantienen la nube densa completa.
PRESETS = {
    "vistazo": {
        "orden": 0,
        "titulo": "Vistazo",
        "para_que": "Ver algo utilizable en minutos, durante la emergencia.",
        "pc_quality": "low", "feature_quality": "medium", "res_cm": 15,
        "min_features": 4000, "matcher_neighbors": 8,
        "sfm_algorithm": "planar", "hybrid_ba": True, "fast_orthophoto": True,
        "tiempo_relativo": 0.35,
    },
    "rapido": {
        "orden": 1,
        "titulo": "Rápido",
        "para_que": "Respuesta operativa el mismo día, con medidas creíbles.",
        "pc_quality": "medium", "feature_quality": "medium", "res_cm": 8,
        "min_features": 6000, "matcher_neighbors": 8,
        "sfm_algorithm": "planar", "hybrid_ba": True, "fast_orthophoto": True,
        "tiempo_relativo": 1.0,
    },
    "estandar": {
        "orden": 2,
        "titulo": "Estándar",
        "para_que": "La entrega normal de una misión. Es el valor por defecto.",
        "pc_quality": "medium", "feature_quality": "high", "res_cm": 4,
        "min_features": 8000, "matcher_neighbors": 8,
        "sfm_algorithm": "incremental", "hybrid_ba": True, "fast_orthophoto": False,
        "tiempo_relativo": 4.0,
    },
    "alta": {
        "orden": 3,
        "titulo": "Alta",
        "para_que": "Análisis fino y medición sobre el modelo de superficie.",
        "pc_quality": "high", "feature_quality": "high", "res_cm": 2,
        "min_features": 12000, "matcher_neighbors": 0,
        "sfm_algorithm": "incremental", "hybrid_ba": True, "fast_orthophoto": False,
        "tiempo_relativo": 16.0,
    },
    "maxima": {
        "orden": 4,
        "titulo": "Máxima",
        "para_que": "Archivo y peritaje: el máximo detalle que dé el vuelo.",
        "pc_quality": "ultra", "feature_quality": "ultra", "res_cm": 1,
        "min_features": 16000, "matcher_neighbors": 0,
        "sfm_algorithm": "incremental", "hybrid_ba": False, "fast_orthophoto": False,
        "tiempo_relativo": 64.0,
    },
}
PRESET_DEFAULT = "estandar"
PRESETS_ORDENADOS = sorted(PRESETS, key=lambda n: PRESETS[n]["orden"])

# Mapeo del QUALITY 0-100 histórico al preset equivalente, para que las
# corridas, los scripts y el CI que ya pasan un número sigan funcionando. Los
# cortes son los mismos escalones que tenía la tabla vieja.
_QUALITY_A_PRESET = ((90, "maxima"), (70, "alta"), (40, "estandar"),
                     (20, "rapido"), (0, "vistazo"))


# ── Terreno: plano vs escarpado ──────────────────────────────────────
# `sfm_algorithm: planar` (vistazo/rápido) alinea las fotos por HOMOGRAFÍAS
# entre pares — una transformación que solo aproxima bien la escena real
# cuando el terreno es efectivamente plano. Bug real, encontrado en vivo
# (misión mision_2026-08-08, terreno rocoso/escarpado, preset vistazo):
# de 1199 fotos RGB con match válido, la reconstrucción planar solo pudo
# ubicar 283 (23.6%) — de 1199 térmicas, 224 (18.7%). El resto no fue un
# error visible: OpenSfM simplemente las descartó del grafo de poses porque
# su homografía con las vecinas no encajaba, y ODM siguió de largo con lo
# que sí pudo alinear ("decision: Success" en su propio reporte). El síntoma
# en el geovisor es un ortomosaico/DSM "recortado" muy por debajo del área
# real volada — no es un bug de recorte de bordes, es cobertura real perdida
# en la reconstrucción.
#
# `incremental` no asume un plano: reconstruye foto por foto vía bundle
# adjustment, válido para cualquier geometría 3D a costa de ser más lento
# (secuencial por diseño). TERRENO_ESCARPADO fuerza incremental incluso en
# vistazo/rápido — las demás palancas de velocidad del preset (pc_quality,
# feature_quality, min_features, fast_orthophoto) se mantienen: la idea es
# "rápido pero completo", no perder la ganancia de velocidad entera.
TERRENOS = ("plano", "escarpado")
TERRENO_DEFAULT = "plano"

# Corrección de tiempo_relativo cuando se fuerza incremental sobre un preset
# que por defecto es planar (vistazo/rápido). NO es una calibración medida
# (no hay todavía una corrida real de "vistazo + escarpado" para calibrar
# contra — ver mision_2026-08-08): es una estimación conservadora a partir
# de la nota de _SEG_POR_FOTO_BASE, donde la reconstrucción incremental
# (bundle adjustment secuencial) se llevó ~44% del tiempo total de una banda
# D grande. PENDIENTE: recalibrar con la primera corrida real de este modo.
_CORRECCION_INCREMENTAL_FORZADO = 3.0


def preset(nombre, terreno=None):
    """Config de un preset por nombre. Acepta también un QUALITY numérico
    (0-100) por compatibilidad — ver _QUALITY_A_PRESET.

    `terreno`: "plano" (default) o "escarpado" — ver TERRENOS arriba. Con
    "escarpado", sfm_algorithm se fuerza a "incremental" sin importar el
    preset, y tiempo_relativo se ajusta para que la estimación no prometa
    la velocidad de "planar" en un modo que ya no lo usa."""
    if nombre is None:
        nombre = PRESET_DEFAULT
    clave = str(nombre).strip().lower()
    if clave in PRESETS:
        p = dict(PRESETS[clave], nombre=clave)
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
        p["tiempo_relativo"] = p["tiempo_relativo"] * _CORRECCION_INCREMENTAL_FORZADO
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
# Coeficiente MUY aproximado: segundos por foto en el escalón MÁS RÁPIDO
# (lowest) por "núcleo efectivo" (cores^0.75, el paralelismo real con
# rendimientos decrecientes). Calibrado en orden de magnitud, no en
# precisión — la variación real entre escenas (solape, nº de features,
# GPU) es de sobra mayor que cualquier error de este número. Por eso la
# estimación siempre se muestra como RANGO y rotulada "aproximada".
#
# CALIBRADO contra barbosa-picodegallo, la corrida completa más grande y más
# reciente de esta máquina (laptop, RTX A1000 6 GB, 20 núcleos, 23 GB RAM):
# 3149 imágenes (2340 bandas multiespectrales + 585 de banda D + 224
# térmicas) a calidad 75 — el preset «alta», tiempo_relativo 16 — en 24 h 15
# min de punta a punta (outputs/logs/timings.json: 87 300 s).
#   S = total_real * cores^0.75 / (n_fotos * tiempo_relativo)
#     = 87300 * 20^0.75 / (3149 * 16) ≈ 16.4 s/foto
#
# El valor anterior (110) venía de barbosa-chorrera, una misión de 221
# imágenes, y sobre picodegallo predecía 83-221 h contra las 24 h reales:
# 5.8x pesimista. La causa es que este modelo es LINEAL en el número de
# fotos y las dos misiones reales no lo son — 17x más imágenes costaron solo
# 4x más tiempo (el matching no crece con n² porque ODM acota los pares con
# matching_graph_rounds, y buena parte del costo por foto se amortiza). Se
# calibra contra la misión GRANDE a propósito: es donde la espera duele y
# donde la decisión de preset importa. En vuelos chicos la estimación va a
# leerse pesimista.
#
# PENDIENTE de volver a medir: estos 24 h se midieron ANTES de arreglar el
# split de concurrencia de run_odm (features y undistort corrían con 2 y 6
# hilos sobre 20 núcleos, ver docker/entrypoint.sh) y antes de solapar
# proyectos. O sea que hoy es una COTA SUPERIOR, no una predicción centrada.
# Recalibrar con la primera corrida completa post-fix y anotar acá contra
# qué misión se hizo.
_SEG_POR_FOTO_BASE = 16.4


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


def estimate_minutes(quality, n_photos, hw=None, terreno=None):
    """(min_low, min_high) minutos estimados, MUY aproximados a propósito.
    `quality` es un nombre de preset o un QUALITY 0-100 heredado."""
    if n_photos <= 0:
        return (0.0, 0.0)
    hw = hw or detect_hardware()
    tier = preset(quality, terreno=terreno)
    cores = max(1, hw["cores"])
    # Paralelismo con rendimientos decrecientes (overhead de coordinación):
    # no se divide linealmente por núcleo.
    paralelismo_efectivo = cores ** 0.75
    factor_gpu = _gpu_factor(hw.get("vram_mb"), hw.get("gpu", False))
    base = n_photos * _SEG_POR_FOTO_BASE * tier["tiempo_relativo"] * factor_gpu
    base /= paralelismo_efectivo
    minutos = base / 60
    # Banda ancha (0.5x-2x, antes 0.6x-1.6x) y a propósito: el modelo es
    # lineal en el número de fotos y las dos misiones reales medidas no lo
    # son (ver la nota de _SEG_POR_FOTO_BASE). Un rango angosto sobre un
    # modelo que se sabe aproximado promete una precisión que no existe.
    return (round(minutos * 0.5, 1), round(minutos * 2.0, 1))


def _fmt_minutos(lo, hi):
    def _f(m):
        if m < 1:
            return "menos de 1 min"
        if m < 90:
            return f"{m:.0f} min"
        return f"{m/60:.1f} h"
    if hi <= 0:
        return "—"
    return f"{_f(lo)} – {_f(hi)}" if _f(lo) != _f(hi) else _f(lo)


def _texto_hardware(hw):
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


def preset_options(n_photos, hw=None, terreno=None):
    """Todos los presets con su tiempo estimado PARA ESTA misión.

    Es lo que hace que la elección sea informada: el operador ve los cinco
    con su número al lado y elige por tiempo, en vez de mover un slider de
    0 a 100 sin saber qué compra cada tramo."""
    hw = hw or detect_hardware()
    opciones = []
    for nombre in PRESETS_ORDENADOS:
        p = preset(nombre, terreno=terreno)
        lo, hi = estimate_minutes(nombre, n_photos, hw, terreno=terreno)
        opciones.append({
            "nombre": nombre,
            "titulo": p["titulo"],
            "para_que": p["para_que"],
            "res_cm": p["res_cm"],
            "pc_quality": p["pc_quality"],
            "sfm_algorithm": p["sfm_algorithm"],
            "minutos_estimados": [lo, hi],
            "tiempo_texto": _fmt_minutos(lo, hi),
            "por_defecto": nombre == PRESET_DEFAULT,
        })
    return opciones


def estimate_message(quality, n_photos, hw=None, terreno=None):
    """Mensaje completo (texto + datos crudos) para mostrarle al usuario
    ANTES de arrancar: a qué resolución van a salir los productos y cuánto se
    espera que tarde, con las cuentas claras de por qué."""
    hw = hw or detect_hardware()
    tier = preset(quality, terreno=terreno)
    lo, hi = estimate_minutes(quality, n_photos, hw, terreno=terreno)
    partes_hw = _texto_hardware(hw)

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
           f"las fotos perdidas en una misión de terreno rocoso)."
           if PRESETS[tier["nombre"]]["sfm_algorithm"] != tier["sfm_algorithm"] else "")
    )
    tiempo = (
        f"Tiempo estimado con tu hardware ({', '.join(partes_hw)}): "
        f"{_fmt_minutos(lo, hi)} para {n_photos} fotos. Aproximado — la escena real "
        f"(solape, vegetación) pesa más que este número."
    )
    # Guía de respuesta rápida: en los presets altos la reconstrucción densa
    # domina el tiempo (cada escalón multiplica por ~4) y los rápidos son
    # mucho más baratos. Se muestran sus tiempos acá, ANTES de arrancar — la
    # decisión se toma con números, no después de una corrida larga.
    if tier["tiempo_relativo"] >= 16 and n_photos > 0:
        rap = _fmt_minutos(*estimate_minutes("rapido", n_photos, hw, terreno=terreno))
        vis = _fmt_minutos(*estimate_minutes("vistazo", n_photos, hw, terreno=terreno))
        tiempo += (f" Para respuesta rápida: «Rápido» ≈ {rap} y «Vistazo» ≈ {vis} "
                   f"— mismo vuelo, con techo de 8/15 cm en vez de "
                   f"{tier['res_cm']} cm (el GSD real del vuelo manda igual).")
    return {"tier": tier, "preset": tier["nombre"], "terreno": tier["terreno"],
            "hardware": hw, "n_photos": n_photos, "minutos_estimados": [lo, hi],
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
