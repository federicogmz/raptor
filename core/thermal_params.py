"""Parámetros de calibración radiométrica para el DJI Thermal SDK en RAPTOR.

Define los estándares operativos SIATA por defecto, rangos físicos válidos
según especificación del SDK de DJI (dirp API) y resolución de parámetros
con precedencia estricta: argumento explícito > variable de entorno > SIATA.
"""

import math
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

_BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DJI_IRP = _BASE_DIR / "dji_thermal_sdk" / "utility" / "bin" / "linux" / "release_x64" / "dji_irp"

# Parámetros ambientales estándar SIATA para emergencias e incendios de cobertura vegetal.
# Distancia 25m: cota superior de DJI Thermal SDK [1, 25] m para evitar la compresión de
# rango dinámico del valor de fábrica (5m) en vuelos AGL (~120-500m).
SIATA_EMISSIVITY: float = 0.96
SIATA_HUMIDITY: float = 15.0
SIATA_DISTANCE: float = 25.0
SIATA_REFLECTION: float = 19.0

# Rangos físicos permitidos por DJI Thermal SDK v1.8 (dirp_set_measurement_params)
PARAM_RANGES = {
    "emissivity": (0.10, 1.00, "Emisividad superficial"),
    "humidity": (1.0, 100.0, "Humedad relativa (%)"),
    "distance": (1.0, 25.0, "Distancia al objetivo (m)"),
    "reflection": (-40.0, 500.0, "Temperatura reflejada (°C)"),
}

ENV_VARS = {
    "emissivity": "THERMAL_EMISSIVITY",
    "humidity": "THERMAL_HUMIDITY",
    "distance": "THERMAL_DISTANCE",
    "reflection": "THERMAL_REFLECTION",
}

SIATA_DEFAULTS = {
    "emissivity": SIATA_EMISSIVITY,
    "humidity": SIATA_HUMIDITY,
    "distance": SIATA_DISTANCE,
    "reflection": SIATA_REFLECTION,
}


def _validate_and_parse_param(name: str, value: Any) -> float:
    """Valida y convierte un valor a float rechazando bool, NaN, inf y fuera de rango."""
    if isinstance(value, bool):
        raise ValueError(f"El parámetro radiométrico '{name}' no puede ser booleano.")
    try:
        val_f = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Valor numérico inválido para el parámetro radiométrico '{name}': {value}") from exc

    if math.isnan(val_f) or math.isinf(val_f):
        raise ValueError(f"El parámetro radiométrico '{name}' no puede ser NaN ni infinito ({val_f}).")

    lo, hi, desc = PARAM_RANGES[name]
    if not (lo <= val_f <= hi):
        raise ValueError(
            f"El parámetro radiométrico '{name}' ({desc}) tiene valor {val_f}, fuera del rango permitido [{lo}, {hi}]."
        )
    return val_f


def resolve_thermal_params(
    emissivity: Optional[Any] = None,
    humidity: Optional[Any] = None,
    distance: Optional[Any] = None,
    reflection: Optional[Any] = None,
) -> Dict[str, Any]:
    """Resuelve parámetros radiométricos con precedencia: explícito > variable de entorno > estándar SIATA.

    Retorna un diccionario con:
      - emissivity: float
      - humidity: float
      - distance: float
      - reflection: float
      - source: "custom" | "default_siata"
    """
    args = {
        "emissivity": emissivity,
        "humidity": humidity,
        "distance": distance,
        "reflection": reflection,
    }

    resolved = {}
    is_custom = False

    for name in ("emissivity", "humidity", "distance", "reflection"):
        arg_val = args[name]
        env_var = ENV_VARS[name]
        env_val = os.environ.get(env_var)

        # 1. Argumento explícito si no es None ni cadena vacía
        if arg_val is not None and not (isinstance(arg_val, str) and arg_val.strip() == ""):
            parsed = _validate_and_parse_param(name, arg_val)
            resolved[name] = parsed
            is_custom = True
        # 2. Variable de entorno si está presente y no vacía
        elif env_val is not None and env_val.strip() != "":
            parsed = _validate_and_parse_param(name, env_val.strip())
            resolved[name] = parsed
            is_custom = True
        # 3. Estándar operativo SIATA
        else:
            resolved[name] = SIATA_DEFAULTS[name]

    resolved["source"] = "custom" if is_custom else "default_siata"
    return resolved


def build_dji_irp_cmd(
    src_jpg: str,
    dst_raw: str,
    emissivity: float,
    humidity: float,
    distance: float,
    reflection: float,
    dji_irp_bin: Optional[str] = None,
) -> List[str]:
    bin_path = str(dji_irp_bin) if dji_irp_bin else str(DEFAULT_DJI_IRP)
    return [
        bin_path,
        "-s",
        str(src_jpg),
        "-a",
        "measure",
        "-o",
        str(dst_raw),
        "--measurefmt",
        "float32",
        "--emissivity",
        str(emissivity),
        "--humidity",
        str(humidity),
        "--distance",
        str(distance),
        "--reflection",
        str(reflection),
    ]


def run_dji_irp_extract(
    src_jpg: str,
    dst_raw: str,
    emissivity: float,
    humidity: float,
    distance: float,
    reflection: float,
    env: Optional[dict] = None,
    dji_irp_bin: Optional[str] = None,
    timeout: int = 30,
    run_fn=None,
) -> Tuple[bool, float]:
    """Extrae datos float32 con dji_irp aplicando fallback para H20T si humidity < 20.0.

    Retorna (ok: bool, effective_humidity: float).
    """
    if run_fn is None:
        run_fn = subprocess.run
    eff_humidity = float(humidity)
    cmd = build_dji_irp_cmd(
        src_jpg,
        dst_raw,
        emissivity=emissivity,
        humidity=humidity,
        distance=distance,
        reflection=reflection,
        dji_irp_bin=dji_irp_bin,
    )
    try:
        res = run_fn(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if (
            res.returncode != 0
            and eff_humidity < 20.0
            and (res.returncode == 253 or "dirp_set_measurement_params" in ((res.stdout or "") + (res.stderr or "")))
        ):
            eff_humidity = 20.0
            cmd_fallback = build_dji_irp_cmd(
                src_jpg,
                dst_raw,
                emissivity=emissivity,
                humidity=20.0,
                distance=distance,
                reflection=reflection,
                dji_irp_bin=dji_irp_bin,
            )
            res = run_fn(
                cmd_fallback,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        if res.returncode != 0:
            err = (res.stderr or "").strip()
            if err:
                print(f"  ❌ dji_irp error ({os.path.basename(src_jpg)}): {err[:200]}", file=sys.stderr)
            return False, eff_humidity

        if not os.path.exists(dst_raw) or os.path.getsize(dst_raw) == 0:
            print(f"  ❌ dji_irp no produjo salida para {os.path.basename(src_jpg)}", file=sys.stderr)
            return False, eff_humidity

        return True, eff_humidity
    except subprocess.TimeoutExpired:
        print(f"  ❌ dji_irp no respondió en {timeout}s para {os.path.basename(src_jpg)}", file=sys.stderr)
        return False, eff_humidity
    except Exception as exc:
        print(f"  ⚠ Fallo al extraer {os.path.basename(src_jpg)}: {exc}", file=sys.stderr)
        return False, eff_humidity
