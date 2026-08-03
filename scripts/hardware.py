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
    """(hay_gpu, nombre) — best-effort vía nvidia-smi. None si no hay o si el
    binario no está (WSL/CPU-only, muy común en este proyecto)."""
    if shutil.which("nvidia-smi") is None:
        return False, None
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name",
                            "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return True, r.stdout.strip().splitlines()[0].strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return False, None


def detect_hardware():
    has_gpu, gpu_name = gpu_info()
    return {"cores": cpu_count(), "mem_available_mb": mem_available_mb(),
            "gpu": has_gpu, "gpu_name": gpu_name}


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


# ── Perfil de calidad ────────────────────────────────────────────────
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
QUALITY_TIERS = [
    (90, "ultra",  "ultra",  1,  64, "máxima"),
    (70, "high",   "high",   2,  16, "alta"),
    (40, "medium", "high",   4,   4, "media"),
    (20, "low",    "medium", 8,   1.5, "baja"),
    (0,  "lowest", "medium", 15,  1, "mínima (previsualización rápida)"),
]


def quality_tier(quality):
    quality = max(0, min(100, int(quality)))
    for qmin, pc, feat, res_cm, tfactor, label in QUALITY_TIERS:
        if quality >= qmin:
            return {"quality": quality, "pc_quality": pc, "feature_quality": feat,
                    "res_cm": res_cm, "tiempo_relativo": tfactor, "label": label}
    raise AssertionError("QUALITY_TIERS no cubre 0 — no debería pasar")


# ── Estimación de tiempo ─────────────────────────────────────────────
# Coeficiente MUY aproximado: segundos por foto en el escalón MÁS RÁPIDO
# (lowest) con un núcleo de referencia. Calibrado en orden de magnitud, no en
# precisión — la variación real entre escenas (solape, nº de features,
# GPU sí/no) es de sobra mayor que cualquier error de este número. Por eso la
# estimación siempre se muestra como RANGO y rotulada "aproximada".
_SEG_POR_FOTO_BASE = 2.5
# La GPU acelera SfM/MVS pero no todo el pipeline (nuestro post-procesamiento
# en Python/GDAL es CPU-only) — un factor conservador, no "la GPU lo hace 10x".
_FACTOR_GPU = 0.5


def estimate_minutes(quality, n_photos, hw=None):
    """(min_low, min_high) minutos estimados, MUY aproximados a propósito."""
    if n_photos <= 0:
        return (0.0, 0.0)
    hw = hw or detect_hardware()
    tier = quality_tier(quality)
    cores = max(1, hw["cores"])
    # Paralelismo con rendimientos decrecientes (overhead de coordinación):
    # no se divide linealmente por núcleo.
    paralelismo_efectivo = cores ** 0.75
    factor_gpu = _FACTOR_GPU if hw.get("gpu") else 1.0
    base = n_photos * _SEG_POR_FOTO_BASE * tier["tiempo_relativo"] * factor_gpu
    base /= paralelismo_efectivo
    minutos = base / 60
    return (round(minutos * 0.6, 1), round(minutos * 1.6, 1))


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


def estimate_message(quality, n_photos, hw=None):
    """Mensaje completo (texto + datos crudos) para mostrarle al usuario
    ANTES de arrancar: a qué resolución van a salir los productos y cuánto se
    espera que tarde, con las cuentas claras de por qué."""
    hw = hw or detect_hardware()
    tier = quality_tier(quality)
    lo, hi = estimate_minutes(quality, n_photos, hw)

    partes_hw = [f"{hw['cores']} núcleos"]
    if hw.get("mem_available_mb"):
        partes_hw.append(f"{hw['mem_available_mb']/1024:.0f} GB RAM libres")
    partes_hw.append(f"GPU: {hw['gpu_name'] or 'sí'}" if hw.get("gpu") else "sin GPU (CPU)")

    resolucion = (
        f"Hasta {tier['res_cm']} cm/px en el ortomosaico y el DSM — el techo real "
        f"lo pone el GSD de tu vuelo (altura × sensor): si el vuelo da menos detalle "
        f"que eso, sale al máximo que el vuelo permita; nunca más fino, sin importar "
        f"la calidad elegida."
    )
    modelo = (
        f"Calidad {tier['label']} ({quality}%): nube de puntos y malla en nivel "
        f"'{tier['pc_quality']}'. Más alto resuelve mejor objetos como copas de "
        f"árboles y bordes de tejados — más bajo es más rápido pero puede aplanar "
        f"esos detalles en el modelo de superficie."
    )
    tiempo = (
        f"Tiempo estimado con tu hardware ({', '.join(partes_hw)}): "
        f"{_fmt_minutos(lo, hi)} para {n_photos} fotos. Aproximado — la escena real "
        f"(solape, vegetación) pesa más que este número."
    )
    return {"tier": tier, "hardware": hw, "n_photos": n_photos,
            "minutos_estimados": [lo, hi],
            "resolucion_texto": resolucion, "modelo_texto": modelo,
            "tiempo_texto": tiempo}


# ── CLI ───────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    pc = sub.add_parser("concurrency")
    pc.add_argument("megapixels", type=float)
    qt = sub.add_parser("quality-tier")
    qt.add_argument("quality", type=int)
    es = sub.add_parser("estimate")
    es.add_argument("--quality", type=int, required=True)
    es.add_argument("--photos", type=int, required=True)
    args = p.parse_args()

    if args.cmd == "info":
        print(json.dumps(detect_hardware()))
    elif args.cmd == "concurrency":
        override = os.environ.get("MAX_CONCURRENCY")
        print(safe_concurrency(args.megapixels,
                               override=int(override) if override else None))
    elif args.cmd == "quality-tier":
        print(json.dumps(quality_tier(args.quality)))
    elif args.cmd == "estimate":
        print(json.dumps(estimate_message(args.quality, args.photos)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
