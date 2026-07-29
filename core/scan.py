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
        if not entry.is_dir():
            continue
        outputs = entry / "outputs"
        processing = entry / "processing"
        tiles = entry / "tiles"
        has_outputs = outputs.is_dir() and any(outputs.glob("*.tif"))
        has_processing = processing.is_dir() and any(processing.iterdir()) if processing.is_dir() else False
        has_tiles = tiles.is_dir() and any(tiles.glob("*/*.json")) or (tiles / "bounds.json").exists()
        out.append(ExistingRun(entry.name, entry, has_outputs, has_processing, bool(has_tiles)))
    return out


def sanitize_mission_name(raw: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "_" for c in raw.strip()]
    name = "".join(keep).strip("_") or "mision"
    return name.lower()
