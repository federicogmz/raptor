#!/usr/bin/env python3
"""Poblar el directorio images/ de un proyecto ODM y armar su geo.txt.

Lo comparten prepare_multispectral_odm.py y prepare_dband_odm.py: los dos leen
del mismo sensor (M3M), con el mismo GPS EXIF nativo (RTK), y hacían
exactamente lo mismo en ~100 líneas duplicadas que ya se habían empezado a
desincronizar (un mensaje decía "bandas", el otro "imágenes", y los
comentarios de uno remitían al otro).

POR QUÉ ENLAZA EN VEZ DE COPIAR. Hasta acá cada foto se copiaba DOS veces:
de la fuente a data/<sensor>_mosaico/ (docker/setup-data*.sh) y de ahí a
processing/<proyecto>/images/. En la misión barbosa-picodegallo son 2340
bandas multiespectrales más 585 imágenes de banda D: del orden de 23 GB
escritos dos veces, y la etapa de preparación entera tardó 20 min. ODM solo
LEE processing/<proyecto>/images/, así que un hardlink alcanza y cuesta
prácticamente cero.

Ojo con generalizarlo: `Makefile::prepare-rgb` NO puede usar hardlink porque
corre `exiftool -overwrite_original` sobre las copias, y con un hardlink eso
modificaría también el archivo de data/. Los dos scripts que usan este módulo
solo leen metadata (exiftool sin -overwrite_original), por eso acá sí es
seguro.

Si el hardlink no se puede hacer (fuente y destino en filesystems distintos,
que es lo normal cuando data/ y processing/ son volúmenes separados) se cae a
copy2 sin decir nada: el resultado es el mismo, solo más lento.
"""
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

# Precisión asumida cuando una captura puntual no trae fix RTK. Conservador a
# propósito: es peor decirle a ODM que confíe en un GPS que no lo merece que
# quedarse corto.
H_ACC_SIN_RTK = 3.0
V_ACC_SIN_RTK = 5.0


def enlazar_o_copiar(src, dst):
    """Hardlink de src a dst; copy2 si el filesystem no lo permite."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def poblar_images(src_dir, images_dir, sufijos, nprocs, etiqueta="archivos"):
    """Deja en images_dir exactamente los archivos de src_dir que terminan en
    `sufijos` (comparación sin distinguir mayúsculas). Devuelve la lista de
    nombres.

    Limpia solo lo que quedó de OTRA corrida (un nombre que ya no está en
    src_dir — misión nueva sobre el mismo contenedor, sin eso mezclaría dos
    vuelos), pero NO reenlaza lo que ya está: src_dir son fotos ya capturadas
    (inmutables), así que si el destino ya existe es porque una corrida
    anterior de ESTA misma misión ya lo dejó ahí — un retry no debería volver
    a enlazar/copiar miles de archivos que no cambiaron.
    """
    os.makedirs(images_dir, exist_ok=True)
    sufijos = tuple(s.upper() for s in sufijos)

    nombres = sorted(f for f in os.listdir(src_dir) if f.upper().endswith(sufijos))
    if not nombres:
        return []

    nombres_set = set(nombres)
    viejos = [f for f in os.listdir(images_dir)
              if f.upper().endswith(sufijos) and f not in nombres_set]
    if viejos:
        print(f"🧹 Limpiando {len(viejos)} de otra misión/corrida …")
        for f in viejos:
            os.remove(os.path.join(images_dir, f))

    pendientes = [f for f in nombres if not os.path.exists(os.path.join(images_dir, f))]
    ya = len(nombres) - len(pendientes)
    if not pendientes:
        print(f"✅ {ya} {etiqueta} ya estaban preparados, nada que enlazar")
        return nombres

    print(f"🔗 Enlazando {len(pendientes)} {etiqueta} nuevos con {nprocs} en paralelo"
          + (f" ({ya} ya existentes, reutilizados) …" if ya else " …"))
    with ThreadPoolExecutor(max_workers=nprocs) as pool:
        # list() para que una excepción de cualquier enlace salga acá con su
        # traceback real, en vez de perderse en un futuro sin cosechar.
        list(pool.map(lambda f: enlazar_o_copiar(os.path.join(src_dir, f),
                                                 os.path.join(images_dir, f)),
                      pendientes))
    return nombres


def _lotes(lista, n):
    tam = max(1, -(-len(lista) // n))
    return [lista[i:i + tam] for i in range(0, len(lista), tam)]


def _leer_gps_lote(rutas):
    # La lista de archivos va por STDIN (`-@ -`), no como argumentos: un vuelo
    # M3M son 4 archivos por captura, así que una misión mediana ya pasa el
    # millar y la línea de comandos tiene un tope duro (ARG_MAX).
    r = subprocess.run(
        ["exiftool", "-j", "-n",
         "-GPSLatitude", "-GPSLongitude", "-GPSAltitude",
         "-GimbalYawDegree", "-GimbalPitchDegree", "-GimbalRollDegree",
         "-RtkStdLon", "-RtkStdLat", "-RtkStdHgt", "-@", "-"],
        input="\n".join(rutas),
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    return json.loads(r.stdout)


def _leer_geo_txt_previo(geo_txt, nombres_actuales):
    """Líneas ya armadas en una corrida anterior, indexadas por nombre de
    archivo. Solo se reusan las que siguen correspondiendo a `rutas` actual
    (nombres_actuales) — una foto no cambia de GPS entre corridas, así que
    releer su EXIF de nuevo es puro costo sin beneficio.
    """
    if not os.path.exists(geo_txt):
        return {}
    try:
        with open(geo_txt) as f:
            lineas = f.read().splitlines()
    except OSError:
        return {}
    if not lineas or lineas[0].strip() != "EPSG:4326":
        return {}
    previas = {}
    for linea in lineas[1:]:
        nombre = linea.split("\t", 1)[0]
        if nombre in nombres_actuales:
            previas[nombre] = linea
    return previas


def escribir_geo_txt(rutas, geo_txt, nprocs, etiqueta="archivos"):
    """Arma geo.txt leyendo el EXIF de `rutas`. Devuelve (con_gps, sin_gps).

    Se agregan las columnas opcionales 8-9 de ODM (horizontal_accuracy,
    vertical_accuracy, ver opendm/geo.py) desde los tags RtkStdLon/Lat/Hgt del
    propio EXIF. Sin eso, ODM (opendm/photo.py update_with_geo_entry) pisa con
    None la precisión RTK que él mismo sabe auto-detectar del EXIF cuando NO
    hay geo.txt, y fuerza un DOP genérico (~metros) en el bundle adjustment en
    vez de confiar en la precisión RTK real (~1-3 cm).

    Reusa las filas de un geo.txt previo para los archivos que ya estaban ahí
    (ver _leer_geo_txt_previo): confirmado en vivo como el cuello de botella
    real de esta etapa — un vuelo M3M mediano son miles de TIFF, y releer el
    EXIF de todos en cada retry cuesta lo mismo que la primera vez aunque casi
    ninguno haya cambiado.

    Lo pendiente se parte en `nprocs` lotes en paralelo: cada exiftool lee
    metadata (no los píxeles), así que el techo real es E/S por archivo, no
    CPU. Una sola llamada con las 2340 bandas de una corrida real superó el
    timeout de 180 s que había antes.
    """
    nombres_actuales = {os.path.basename(r) for r in rutas}
    previas = _leer_geo_txt_previo(geo_txt, nombres_actuales)
    pendientes = [r for r in rutas if os.path.basename(r) not in previas]

    exif_todos = []
    if pendientes:
        print(f"🔍 Extrayendo GPS de {len(pendientes)} {etiqueta} nuevos con exiftool "
              f"({nprocs} en paralelo)"
              + (f" — {len(previas)} ya en geo.txt, reutilizados" if previas else "") + " …")
        try:
            with ThreadPoolExecutor(max_workers=nprocs) as pool:
                for datos in pool.map(_leer_gps_lote, _lotes(pendientes, nprocs)):
                    exif_todos.extend(datos)
        except subprocess.TimeoutExpired as exc:
            print(f"❌ ERROR: exiftool no terminó a tiempo: {exc}")
            sys.exit(1)
        except (RuntimeError, json.JSONDecodeError) as exc:
            print(f"❌ ERROR: exiftool falló:\n{exc}")
            sys.exit(1)
    elif previas:
        print(f"✅ geo.txt ya cubre los {len(previas)} {etiqueta}, nada que extraer")

    nuevas = {}
    sin_gps = 0
    con_rtk_nuevas = 0
    for exif in exif_todos:
        # El nombre sale de SourceFile —que exiftool incluye en cada
        # registro—, no de emparejar por posición con la lista de entrada:
        # ligar la fila i de la salida al archivo i de la entrada da por
        # sentado un orden y un conteo exactos, y si alguna vez no se cumplen,
        # geo.txt queda con coordenadas asignadas al archivo equivocado. Eso
        # no falla: produce una reconstrucción mal georreferenciada.
        nombre = os.path.basename(exif.get("SourceFile", ""))
        lat = exif.get("GPSLatitude")
        lon = exif.get("GPSLongitude")
        if not nombre or lat is None or lon is None:
            sin_gps += 1
            continue
        alt = exif.get("GPSAltitude", 0)
        yaw = exif.get("GimbalYawDegree", 0)
        pitch = exif.get("GimbalPitchDegree", 0)
        roll = exif.get("GimbalRollDegree", 0)
        std_lon = exif.get("RtkStdLon")
        std_lat = exif.get("RtkStdLat")
        std_hgt = exif.get("RtkStdHgt")
        if std_lon is not None and std_lat is not None:
            # Mismo margen de seguridad ×2 que ODM aplica internamente al
            # auto-detectar estos tags.
            h_acc = max(std_lon, std_lat) * 2.0
            v_acc = (std_hgt if std_hgt is not None else std_lon) * 2.0
            con_rtk_nuevas += 1
        else:
            h_acc, v_acc = H_ACC_SIN_RTK, V_ACC_SIN_RTK
        nuevas[nombre] = (f"{nombre}\t{lon}\t{lat}\t{alt}\t{yaw}\t{pitch}\t{roll}"
                          f"\t{h_acc:.5f}\t{v_acc:.5f}")

    # Orden estable según `rutas` (no según el orden de respuesta de exiftool,
    # que llega por lote y no coincide con el de entrada).
    lineas = ["EPSG:4326"]
    for r in rutas:
        nombre = os.path.basename(r)
        if nombre in previas:
            lineas.append(previas[nombre])
        elif nombre in nuevas:
            lineas.append(nuevas[nombre])

    if sin_gps:
        print(f"  ⚠ {sin_gps} {etiqueta} sin GPS (omitidos de geo.txt)")
    con_gps = len(lineas) - 1
    if pendientes:
        print(f"  precisión RTK disponible en los nuevos: {con_rtk_nuevas}/{len(nuevas)} {etiqueta}")

    os.makedirs(os.path.dirname(geo_txt) or ".", exist_ok=True)
    with open(geo_txt, "w") as f:
        f.write("\n".join(lineas) + "\n")
    return con_gps, sin_gps
