#!/usr/bin/env python3
"""Poblar el directorio images/ de un proyecto ODM y armar su geo.txt.

Lo comparten prepare_multispectral_odm.py, prepare_dband_odm.py,
prepare_thermal_native_odm.py y (en modo condicional) prepare_rgb_odm.py.

POR QUÉ ENLAZA EN VEZ DE COPIAR. Hasta acá cada foto se copiaba DOS veces:
de la fuente a data/<sensor>_mosaico/ (docker/setup-data*.sh) y de ahí a
processing/<proyecto>/images/. En la misión barbosa-picodegallo son 2340
bandas multiespectrales más 585 imágenes de banda D: del orden de 23 GB
escritos dos veces, y la etapa de preparación entera tardó 20 min. ODM solo
LEE processing/<proyecto>/images/, así que un hardlink alcanza y cuesta
prácticamente cero.

Ojo con generalizarlo: `Makefile::prepare-rgb` NO puede usar hardlink porque
corre `exiftool -overwrite_original` sobre las copias, y con un hardlink eso
modificaría también el archivo de data/. Los preparadores que usan
`poblar_images` solo leen metadata (exiftool sin -overwrite_original), por eso
acá sí es seguro.

Si el hardlink no se puede hacer (fuente y destino en filesystems distintos,
que es lo normal cuando data/ y processing/ son volúmenes separados) se cae a
copy2 sin decir nada: el resultado es el mismo, solo más lento.
"""

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# Precisión asumida cuando una captura puntual no trae fix RTK. Conservador a
# propósito: es peor decirle a ODM que confíe en un GPS que no lo merece que
# quedarse corto.
H_ACC_SIN_RTK = 3.0
V_ACC_SIN_RTK = 5.0
# En bloques mixtos (donde otras capturas SÍ tienen RTK FIX=50), las tomas sin
# FIX se downweightean aún más para que su deriva barométrica/autónoma no
# arrastre el datum vertical del bloque RTK.
H_ACC_MIXTO_SIN_RTK = 10.0
V_ACC_MIXTO_SIN_RTK = 20.0
# Piso mínimo cuando la solución es RTK FLOAT (RtkFlag=32/34).
H_ACC_RTK_FLOAT = 0.30
V_ACC_RTK_FLOAT = 0.60

EXIF_CACHE_SCHEMA = 2
_GPS_EPOCH_TS = 315964800.0  # 1980-01-06T00:00:00Z
_GPS_UTC_LEAP_S = 18.0
_DJI_RE = re.compile(r"DJI_(\d{14})_(\d{4})", re.IGNORECASE)


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
    """
    os.makedirs(images_dir, exist_ok=True)
    sufijos = tuple(s.upper() for s in sufijos)

    nombres = sorted(f for f in os.listdir(src_dir) if f.upper().endswith(sufijos))
    if not nombres:
        return []

    nombres_set = set(nombres)
    viejos = [f for f in os.listdir(images_dir) if f.upper().endswith(sufijos) and f not in nombres_set]
    if viejos:
        print(f"🧹 Limpiando {len(viejos)} de otra misión/corrida …")
        for f in viejos:
            os.remove(os.path.join(images_dir, f))

    pendientes = [f for f in nombres if not os.path.exists(os.path.join(images_dir, f))]
    ya = len(nombres) - len(pendientes)
    if not pendientes:
        print(f"✅ {ya} {etiqueta} ya estaban preparados, nada que enlazar")
        return nombres

    print(
        f"🔗 Enlazando {len(pendientes)} {etiqueta} nuevos con {nprocs} en paralelo"
        + (f" ({ya} ya existentes, reutilizados) …" if ya else " …")
    )
    with ThreadPoolExecutor(max_workers=nprocs) as pool:
        list(pool.map(lambda f: enlazar_o_copiar(os.path.join(src_dir, f), os.path.join(images_dir, f)), pendientes))
    return nombres


def _lotes(lista, n):
    tam = max(1, -(-len(lista) // n))
    return [lista[i : i + tam] for i in range(0, len(lista), tam)]


def _leer_gps_lote(rutas):
    # La lista de archivos va por STDIN (`-@ -`), no como argumentos: un vuelo
    # M3M son 4 archivos por captura, así que una misión mediana ya pasa el
    # millar y la línea de comandos tiene un tope duro (ARG_MAX).
    r = subprocess.run(
        [
            "exiftool",
            "-j",
            "-n",
            "-GPSLatitude",
            "-GPSLongitude",
            "-GPSAltitude",
            "-AbsoluteAltitude",
            "-RtkFlag",
            "-GimbalYawDegree",
            "-GimbalPitchDegree",
            "-GimbalRollDegree",
            "-RtkStdLon",
            "-RtkStdLat",
            "-RtkStdHgt",
            "-DateTimeOriginal",
            "-@",
            "-",
        ],
        input="\n".join(rutas),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    return json.loads(r.stdout)


def _file_sig(ruta):
    """Firma (size, mtime_ns) para invalidar caché EXIF si la foto cambió."""
    try:
        st = os.stat(ruta)
        return [st.st_size, st.st_mtime_ns]
    except OSError:
        return None


def _exif_cache_path(geo_txt):
    return f"{geo_txt}.exif_cache.json"


def _leer_exif_cache(geo_txt):
    """Lee `<geo_txt>.exif_cache.json` versionado; devuelve `{nombre: entry}`."""
    path = _exif_cache_path(geo_txt)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("schema") != EXIF_CACHE_SCHEMA:
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def _guardar_exif_cache(geo_txt, entries):
    """Persiste el caché EXIF crudo de forma atómica (tmp + os.replace)."""
    path = _exif_cache_path(geo_txt)
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".exif_cache_", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"schema": EXIF_CACHE_SCHEMA, "entries": entries}, fh)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _to_float(val):
    if val is None or isinstance(val, bool):
        return None
    try:
        num = float(str(val).strip())
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _to_int(val):
    num = _to_float(val)
    return int(round(num)) if num is not None else None


def _es_rtk_fix(exif):
    """True si la foto trae solución RTK FIX (`RtkFlag == 50`, o legado sin
    `RtkFlag` cuando `RtkStdLon`/`RtkStdLat` están presentes).
    """
    std_lon = _to_float(exif.get("RtkStdLon"))
    std_lat = _to_float(exif.get("RtkStdLat"))
    if std_lon is None or std_lat is None:
        return False
    flag = _to_int(exif.get("RtkFlag"))
    return flag == 50 or flag is None


def _calcular_precision(exif, bloque_tiene_rtk_fix):
    """Calcula `(h_acc, v_acc, estado)` validando `RtkFlag`."""
    flag = _to_int(exif.get("RtkFlag"))
    std_lon = _to_float(exif.get("RtkStdLon"))
    std_lat = _to_float(exif.get("RtkStdLat"))
    std_hgt = _to_float(exif.get("RtkStdHgt"))

    if _es_rtk_fix(exif):
        s_xy = max(std_lon, std_lat)
        s_z = std_hgt if std_hgt is not None else std_lon
        return max(0.01, s_xy * 2.0), max(0.02, s_z * 2.0), "fix"

    if flag in (32, 34):
        s_xy = max(v for v in (std_lon, std_lat, 0.0) if v is not None)
        s_z = std_hgt if std_hgt is not None else s_xy
        return max(H_ACC_RTK_FLOAT, s_xy * 2.0), max(V_ACC_RTK_FLOAT, s_z * 2.0), "float"

    if bloque_tiene_rtk_fix:
        return H_ACC_MIXTO_SIN_RTK, V_ACC_MIXTO_SIN_RTK, "sin_rtk"
    return H_ACC_SIN_RTK, V_ACC_SIN_RTK, "sin_rtk"


def _parse_capture_seq(nombre):
    m = _DJI_RE.search(os.path.basename(nombre))
    return int(m.group(2)) if m else None


def _parse_capture_ts(exif, nombre):
    """Devuelve epoch naive (hora local cámara o UTC) de la foto, si existe."""
    dto = exif.get("DateTimeOriginal")
    if isinstance(dto, str) and len(dto) >= 19:
        try:
            dt = datetime.strptime(dto[:19], "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
            return dt.timestamp(), True
        except ValueError:
            pass
    m = _DJI_RE.search(os.path.basename(nombre))
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            return dt.timestamp(), False
        except ValueError:
            pass
    return None, False


def _es_lente_secundaria(nombre):
    u = os.path.basename(nombre).upper()
    return "_MS_" in u or u.endswith(("_T.JPG", "_T.JPEG", "_T.TIF", "_T.TIFF"))


def _primera_float(campo):
    return _to_float(campo.split(",", 1)[0])


def cargar_mrk_dir(mrk_dir):
    """Carga entradas `Q == 50` de todos los `*Timestamp.MRK` en `mrk_dir`.

    Nota DJI: las columnas 6–8 (`Lat, Lon, Ellh`) ya incorporan el vector
    antena→cámara (`cols[3..5]` N/E/V; verificado empíricamente contra el
    XMP de la lente primaria `_D.JPG` a 0.0 mm), por lo que no se vuelve a
    sumar el lever-arm.
    """
    if not mrk_dir or not os.path.isdir(mrk_dir):
        return {}
    por_seq = {}
    for fname in sorted(os.listdir(mrk_dir)):
        if not fname.upper().endswith(".MRK"):
            continue
        fpath = os.path.join(mrk_dir, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    cols = line.strip().split("\t")
                    if len(cols) < 11:
                        continue
                    seq = _to_int(cols[0])
                    tow = _to_float(cols[1])
                    week = _to_int(cols[2].strip("[] "))
                    lat = _primera_float(cols[6])
                    lon = _primera_float(cols[7])
                    ellh = _primera_float(cols[8])
                    std_parts = [_to_float(p) for p in cols[9].split(",")]
                    q = _to_int(cols[10].split(",", 1)[0])
                    if (
                        q != 50
                        or None in (seq, tow, week, lat, lon, ellh)
                        or len(std_parts) < 3
                        or any(v is None for v in std_parts[:3])
                    ):
                        continue
                    gps_utc = _GPS_EPOCH_TS + week * 604800.0 + tow - _GPS_UTC_LEAP_S
                    por_seq.setdefault(seq, []).append(
                        {
                            "lat": lat,
                            "lon": lon,
                            "ellh": ellh,
                            "std_lat": std_parts[0],
                            "std_lon": std_parts[1],
                            "std_hgt": std_parts[2],
                            "gps_utc": gps_utc,
                        }
                    )
        except OSError:
            continue
    return por_seq


def _tiempo_mrk_compatible(gps_utc, cap_ts, exacto):
    if cap_ts is None:
        return True
    tol = 5.0 if exacto else 30.0
    delta = gps_utc - cap_ts
    # Husos horarios posibles [-12h, +14h] en múltiplos de 1800 s.
    if not (-43200.0 - tol <= delta <= 50400.0 + tol):
        return False
    tz_err = abs(delta - round(delta / 1800.0) * 1800.0)
    return tz_err <= tol


def _aplicar_mrk_si_ppk(nombre, exif, mrk_por_seq, seq_counts):
    """Actualiza coordenadas desde `.MRK` (`Q==50`) solo si:
    - no es lente secundaria (`_MS_` / `_T`),
    - el EXIF aún NO trae `RtkFlag == 50`,
    - la secuencia `_NNNN` es única en el bloque, y
    - existe un único candidato MRK dentro de ~5.5 m (`5e-5°`) y horario compatible.
    """
    if _to_int(exif.get("RtkFlag")) == 50 or _es_lente_secundaria(nombre):
        return exif, False
    seq = _parse_capture_seq(nombre)
    if seq is None or seq_counts.get(seq, 0) != 1:
        return exif, False
    lat = _to_float(exif.get("GPSLatitude"))
    lon = _to_float(exif.get("GPSLongitude"))
    if lat is None or lon is None:
        return exif, False
    cap_ts, exacto = _parse_capture_ts(exif, nombre)
    cands = [
        c
        for c in mrk_por_seq.get(seq, ())
        if abs(c["lat"] - lat) < 5e-5
        and abs(c["lon"] - lon) < 5e-5
        and _tiempo_mrk_compatible(c["gps_utc"], cap_ts, exacto)
    ]
    if len(cands) != 1:
        return exif, False
    c = cands[0]
    actualizado = dict(exif)
    actualizado["GPSLatitude"] = c["lat"]
    actualizado["GPSLongitude"] = c["lon"]
    actualizado["AbsoluteAltitude"] = c["ellh"]
    actualizado["RtkFlag"] = 50
    actualizado["RtkStdLat"] = c["std_lat"]
    actualizado["RtkStdLon"] = c["std_lon"]
    actualizado["RtkStdHgt"] = c["std_hgt"]
    return actualizado, True


def recolectar_exif(rutas, geo_txt, nprocs, etiqueta="archivos", mrk_dir=None):
    """Lee EXIF con caché versionada, aplica PPK `.MRK` si aplica y devuelve:
    `([(nombre, exif_efectivo), ...], stats_dict)`.
    """
    cache_prev = _leer_exif_cache(geo_txt)
    cache_actual = {}
    pendientes = []

    for ruta in rutas:
        nombre = os.path.basename(ruta)
        sig = _file_sig(ruta)
        cached = cache_prev.get(nombre)
        if isinstance(cached, dict) and cached.get("sig") == sig and isinstance(cached.get("exif"), dict):
            cache_actual[nombre] = cached
        else:
            pendientes.append((ruta, sig))

    if pendientes:
        reutilizados = len(cache_actual)
        print(
            f"🔍 Extrayendo GPS de {len(pendientes)} {etiqueta} nuevos con exiftool "
            f"({nprocs} en paralelo)"
            + (f" — {reutilizados} en caché EXIF, reutilizados" if reutilizados else "")
            + " …"
        )
        try:
            rutas_pend = [r for r, _sig in pendientes]
            sigs_por_nombre = {os.path.basename(r): sig for r, sig in pendientes}
            with ThreadPoolExecutor(max_workers=nprocs) as pool:
                for datos in pool.map(_leer_gps_lote, _lotes(rutas_pend, nprocs)):
                    for item in datos:
                        nombre = os.path.basename(item.get("SourceFile", ""))
                        if nombre:
                            cache_actual[nombre] = {"sig": sigs_por_nombre.get(nombre), "exif": item}
        except subprocess.TimeoutExpired as exc:
            print(f"❌ ERROR: exiftool no terminó a tiempo: {exc}")
            sys.exit(1)
        except (RuntimeError, json.JSONDecodeError) as exc:
            print(f"❌ ERROR: exiftool falló:\n{exc}")
            sys.exit(1)
        _guardar_exif_cache(geo_txt, cache_actual)
    elif cache_actual:
        print(f"✅ Caché EXIF al día para los {len(cache_actual)} {etiqueta}")

    mrk_por_seq = {}
    if mrk_dir and os.environ.get("RAPTOR_DISABLE_MRK") != "1":
        mrk_por_seq = cargar_mrk_dir(mrk_dir)

    seq_counts = {}
    if mrk_por_seq:
        for r in rutas:
            s = _parse_capture_seq(r)
            if s is not None:
                seq_counts[s] = seq_counts.get(s, 0) + 1

    registros = []
    n_mrk_ppk = 0
    for r in rutas:
        nombre = os.path.basename(r)
        entry = cache_actual.get(nombre)
        exif = entry["exif"] if entry else {}
        if mrk_por_seq:
            exif, usado_mrk = _aplicar_mrk_si_ppk(nombre, exif, mrk_por_seq, seq_counts)
            if usado_mrk:
                n_mrk_ppk += 1
        registros.append((nombre, exif))

    con_gps_regs = [
        (nombre, exif)
        for nombre, exif in registros
        if _to_float(exif.get("GPSLatitude")) is not None and _to_float(exif.get("GPSLongitude")) is not None
    ]
    bloque_tiene_rtk_fix = any(_es_rtk_fix(exif) for _, exif in con_gps_regs)
    n_fix = sum(1 for _, exif in con_gps_regs if _es_rtk_fix(exif))
    n_float = sum(1 for _, exif in con_gps_regs if _to_int(exif.get("RtkFlag")) in (32, 34))
    mixto = bloque_tiene_rtk_fix and n_fix < len(con_gps_regs)
    stats = {
        "total": len(rutas),
        "con_gps": len(con_gps_regs),
        "sin_gps": len(rutas) - len(con_gps_regs),
        "bloque_tiene_rtk_fix": bloque_tiene_rtk_fix,
        "fix": n_fix,
        "float": n_float,
        "mixto": mixto,
        "mrk_ppk": n_mrk_ppk,
    }
    return registros, stats


def escribir_geo_txt(rutas, geo_txt, nprocs, etiqueta="archivos", mrk_dir=None):
    """Arma `geo.txt` leyendo EXIF de `rutas` con caché versionada, validación
    de `RtkFlag`, altitud elipsoidal uniforme (`AbsoluteAltitude` cuando todo
    el bloque la trae; `GPSAltitude` uniforme en caso contrario) y soporte
    `.MRK`. Devuelve `(con_gps, sin_gps)`.
    """
    registros, stats = recolectar_exif(rutas, geo_txt, nprocs, etiqueta=etiqueta, mrk_dir=mrk_dir)
    bloque_tiene_rtk_fix = stats["bloque_tiene_rtk_fix"]

    con_gps_exifs = [
        exif
        for _, exif in registros
        if _to_float(exif.get("GPSLatitude")) is not None and _to_float(exif.get("GPSLongitude")) is not None
    ]
    abs_flags = [_to_float(e.get("AbsoluteAltitude")) is not None for e in con_gps_exifs]
    gps_flags = [_to_float(e.get("GPSAltitude")) is not None for e in con_gps_exifs]
    todos_con_abs = bool(con_gps_exifs) and all(abs_flags)
    todos_con_gps_alt = bool(con_gps_exifs) and all(gps_flags)
    if any(abs_flags) and not todos_con_abs:
        if todos_con_gps_alt:
            print(
                f"  ⚠ {etiqueta}: AbsoluteAltitude parcial — usando GPSAltitude "
                "uniformemente en todo el bloque para evitar mezclar datums."
            )
        else:
            print(
                f"  ⚠ {etiqueta}: fuente de altura heterogénea entre imágenes "
                "(algunas sin AbsoluteAltitude y otras sin GPSAltitude)."
            )

    lineas = ["EPSG:4326"]
    sin_gps = 0
    con_rtk_fix = 0
    con_rtk_float = 0
    for nombre, exif in registros:
        lat = _to_float(exif.get("GPSLatitude"))
        lon = _to_float(exif.get("GPSLongitude"))
        if not nombre or lat is None or lon is None:
            sin_gps += 1
            continue
        if todos_con_abs:
            alt = _to_float(exif.get("AbsoluteAltitude"))
        elif todos_con_gps_alt:
            alt = _to_float(exif.get("GPSAltitude"))
        else:
            alt = _to_float(exif.get("AbsoluteAltitude"))
            if alt is None:
                alt = _to_float(exif.get("GPSAltitude")) or 0.0
        yaw = _to_float(exif.get("GimbalYawDegree")) or 0.0
        pitch = _to_float(exif.get("GimbalPitchDegree")) or 0.0
        roll = _to_float(exif.get("GimbalRollDegree")) or 0.0
        h_acc, v_acc, estado = _calcular_precision(exif, bloque_tiene_rtk_fix)
        if estado == "fix":
            con_rtk_fix += 1
        elif estado == "float":
            con_rtk_float += 1
        lineas.append(f"{nombre}\t{lon}\t{lat}\t{alt}\t{yaw}\t{pitch}\t{roll}\t{h_acc:.5f}\t{v_acc:.5f}")

    if sin_gps:
        print(f"  ⚠ {sin_gps} {etiqueta} sin GPS (omitidos de geo.txt)")
    con_gps = len(lineas) - 1
    if con_gps:
        msg = f"  precisión RTK FIX: {con_rtk_fix}/{con_gps} {etiqueta}"
        if con_rtk_float:
            msg += f" · FLOAT: {con_rtk_float}"
        if stats["mrk_ppk"]:
            msg += f" · MRK(Q=50): {stats['mrk_ppk']}"
        print(msg)

    parent = os.path.dirname(geo_txt) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".geo_", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lineas) + "\n")
        os.replace(tmp_path, geo_txt)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    return con_gps, sin_gps
