#!/usr/bin/env python3
"""Resumen de tiempos por etapa — lee los T_*_START/T_*_END que
docker/entrypoint.sh va exportando durante la corrida, imprime una tabla
legible y escribe outputs/logs/timings.json.

Sin esto, saber dónde se va el tiempo real de una misión significaba
reconstruirlo a mano con `ps`/mtimes de archivos — que es exactamente lo
que hizo falta hacer para encontrar el cuello de botella real de esta
sesión (35 min de preparación antes de que ODM arrancara siquiera).

Uso: python3 scripts/print_timings.py
(no recibe argumentos — todo sale de las variables T_* ya exportadas al
entorno por entrypoint.sh)
"""
import json
import os

# (clave interna, etiqueta legible, prefijo de la variable de entorno)
STAGES = [
    ("prep",         "Preparación (RGB + MS + térmico, en paralelo)", "T_PREP"),
    ("odm_rgb",       "ODM RGB",                                       "T_ODM_RGB"),
    ("odm_thermal",   "ODM Térmico",                                   "T_ODM_THERMAL"),
    ("odm_ms",        "ODM Multiespectral",                            "T_ODM_MS"),
    ("odm_dband",     "ODM Banda D",                                   "T_ODM_DBAND"),
    # "post" mide desde que termina la ÚLTIMA reconstrucción hasta el final
    # (T_POST_START se setea después del bloque ODM en docker/entrypoint.sh):
    # lo que corre en paralelo con la reconstrucción siguiente (recortes) es
    # tiempo gratuito en reloj de pared y no debe inflar esta etapa — antes
    # T_POST_START se seteaba ANTES del bloque ODM y "post" tragaba toda la
    # reconstrucción (en barbosa-chorrera figuraba 4813s para una corrida de
    # 82 min que además saltó ODM, y en la corrida completa hubiera incluido
    # las ~9.5 h de ODM).
    ("post",          "Post-procesamiento (recortes finales, tiles, export)", "T_POST"),
    ("total",         "Total de la misión",                            "T_MISSION"),
]


# Sub-etapas de OpenSfM/OpenMVS, cronometradas por
# scripts/odm_progress_filter.py y dejadas en outputs/logs/odm_<id>_substages.json.
# Sin esto, "ODM multiespectral: 8:27" no dice si el tiempo se fue en algo
# paralelizable (features, undistort) o en algo secuencial por diseño (el
# bundle adjustment incremental) — que es justo la diferencia entre "se puede
# arreglar con hilos" y "hay que cambiar de algoritmo".
SUBSTAGE_LABELS = {
    "features":  "features (paralelizable)",
    "matching":  "matching",
    "sfm":       "reconstrucción incremental (secuencial)",
    "undistort": "undistort (paralelizable)",
}

# Etapas de ODM, cronometradas de "Running X stage" a "Finished X stage".
# Estas SÍ cubren el 100% del tiempo de la reconstrucción — las sub-etapas de
# arriba solo ven lo que imprime una línea por imagen, y en la corrida de
# verificación eso dejaba 27 de 34 minutos sin explicar.
ODM_STAGE_LABELS = {
    "dataset":            "cargar dataset",
    "split":              "división en sub-modelos",
    "merge":              "fusión de sub-modelos",
    "opensfm":            "SfM (features + matching + reconstrucción)",
    "openmvs":            "nube de puntos densa (MVS)",
    "odm_filterpoints":   "filtrado de la nube",
    "odm_meshing":        "malla 3D",
    "mvs_texturing":      "texturizado",
    "odm_georeferencing": "georreferenciación",
    "odm_dem":            "DSM",
    "odm_orthophoto":     "renderizado de la ortofoto",
    "odm_report":         "reporte",
    "odm_postprocess":    "post-procesamiento de ODM",
}


def fmt(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def leer_substages():
    """{id_odm: {sub-etapa: {segundos, n}}} de los JSON que dejó el filtro."""
    fuera = {}
    try:
        nombres = sorted(os.listdir("outputs/logs"))
    except OSError:
        return fuera
    for nombre in nombres:
        if not (nombre.startswith("odm_") and nombre.endswith("_substages.json")):
            continue
        odm_id = nombre[len("odm_"):-len("_substages.json")]
        try:
            with open(os.path.join("outputs/logs", nombre)) as f:
                fuera[odm_id] = json.load(f)
        except (OSError, ValueError):
            continue
    return fuera


def main():
    rows = []
    data = {}
    for key, label, prefix in STAGES:
        start = os.environ.get(f"{prefix}_START")
        end = os.environ.get(f"{prefix}_END")
        if not start or not end:
            continue
        secs = int(end) - int(start)
        rows.append((label, secs))
        data[key] = secs

    substages = leer_substages()
    if not rows and not substages:
        return

    print()
    print("═══ Tiempos por etapa ═══")
    for label, secs in rows:
        print(f"  {label:<46} {fmt(secs)}")
    print("═════════════════════════")

    if substages:
        data["substages"] = substages
        print()
        print("═══ Dentro de cada reconstrucción de ODM ═══")
        for odm_id in sorted(substages):
            detalle = dict(substages[odm_id])
            etapas = detalle.pop("etapas", None)
            print(f"  {odm_id}:")
            # Primero las ETAPAS: cubren el 100% del tiempo y suman el total
            # de la reconstrucción, así que son el mapa real de dónde se fue.
            if etapas:
                for nombre, segundos in sorted(etapas.items(), key=lambda kv: -kv[1]):
                    if segundos >= 1:
                        print(f"    {ODM_STAGE_LABELS.get(nombre, nombre):<44} {fmt(segundos)}")
            # Después el detalle de adentro del SfM. NO suma el total a
            # propósito: solo ve las sub-etapas que imprimen una línea por
            # imagen (ver SUBSTAGES en scripts/odm_progress_filter.py).
            if detalle:
                print("    · dentro del SfM:")
                for clave, valores in detalle.items():
                    etiqueta = SUBSTAGE_LABELS.get(clave, clave)
                    print(f"      {etiqueta:<42} {fmt(valores['segundos'])}"
                          f"  ({valores['n']} imágenes)")
        print("════════════════════════════════════════════")

    os.makedirs("outputs/logs", exist_ok=True)
    with open("outputs/logs/timings.json", "w") as f:
        json.dump(data, f, indent=2)


if __name__ == "__main__":
    main()
