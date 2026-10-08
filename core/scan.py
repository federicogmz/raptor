"""Descubrimiento de corridas existentes en /app/runs."""
import os
from dataclasses import dataclass
from pathlib import Path

RUNS_ROOT = Path(os.environ.get("RAPTOR_RUNS_ROOT", "/app/runs"))


@dataclass
class ExistingRun:
    name: str
    path: Path
    has_outputs: bool
    has_processing: bool
    has_tiles: bool


def scan_existing_runs():
    out = []
    if not RUNS_ROOT.is_dir():
        return out
    for entry in sorted(RUNS_ROOT.iterdir()):
        # .borrando_*: misión ya eliminada, renombrada por delete_mission()
        # mientras su rmtree corre en segundo plano — no es una misión más.
        if not entry.is_dir() or entry.name.startswith(".borrando_"):
            continue
        outputs = entry / "outputs"
        processing = entry / "processing"
        tiles = entry / "tiles"
        has_outputs = outputs.is_dir() and any(outputs.glob("*.tif"))
        has_processing = processing.is_dir() and any(processing.iterdir()) if processing.is_dir() else False
        bounds_file = tiles / "bounds.json"
        is_preliminary = False
        if bounds_file.is_file():
            try:
                import json
                with open(bounds_file) as f:
                    is_preliminary = bool(json.load(f).get("preliminary"))
            except Exception:
                pass
        has_tiles = tiles.is_dir() and (
            any(d.is_dir() for d in tiles.iterdir() if not d.name.startswith("."))
            or (bounds_file.exists() and not is_preliminary)
        )
        out.append(ExistingRun(entry.name, entry, has_outputs, has_processing, bool(has_tiles)))
    return out


def sanitize_mission_name(raw: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "_" for c in raw.strip()]
    name = "".join(keep).strip("_") or "mision"
    return name.lower()
