"""Parámetros de calibración radiométrica para el DJI Thermal SDK en RAPTOR.

Define los estándares operativos SIATA por defecto, rangos físicos válidos
según especificación del SDK de DJI (dirp API) y resolución de parámetros
con precedencia estricta: argumento explícito > variable de entorno > SIATA.
"""

import math
import os
from typing import Any, Dict, Optional

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
