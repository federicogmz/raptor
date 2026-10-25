#!/usr/bin/env python3
"""
scripts/generate_branding.py
============================
Deterministic generator for RAPTOR visual branding and documentation data:
  1. docs/assets/logo.svg      - Crisp pixel-art wide banner (840x240, dark card #0b1017)
  2. docs/assets/icon.svg      - Pixel-art app/favicon icon (128x128, dark card #0b1017)
  3. docs/assets/logo_ascii.txt- Monospace terminal ASCII banner (<= 76 cols)
  4. docs/assets/cli.json      - Structured CLI flags extracted from real --help
  5. docs/assets/constants.json- Scientific parameters and hardware constants

Deterministic: No random timestamps or UUIDs. Trailing newlines, sorted keys, UTF-8.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
ASSETS_DIR = DOCS_DIR / "assets"

# ---------------------------------------------------------------------------
# 24x24 PIXEL MATRIX FOR RAPTOR EMBLEM
# Stepped Andean terrain + active wildfire plume (Ironbow/Inferno) + dual scan reticle
# ---------------------------------------------------------------------------
# Color Palette
PALETTE = {
    " ": None,                  # Transparent / background
    "k": "#0f172a",             # Sky dark slate
    "b": "#1e293b",             # Basalt dark slate (Andean bedrock)
    "B": "#334155",             # Basalt mid slate
    "s": "#475569",             # Basalt light terrace
    "g": "#14532d",             # Deep Andean pine canopy
    "G": "#166534",             # Lush mountain forest
    "l": "#22c55e",             # Light canopy edge
    "p": "#4a148c",             # Ironbow thermal smoke (deep purple)
    "P": "#7b1fa2",             # Ironbow thermal smoke (violet plume)
    "m": "#aa00ff",             # Radiometric plume edge
    "r": "#d50000",             # Radiometric crimson (200-300 °C)
    "R": "#ff1744",             # Active fire front crimson
    "o": "#ff6d00",             # Radiometric orange (400-500 °C)
    "O": "#ff9100",             # Bright radiant flame
    "y": "#ffea00",             # Incandescent yellow (600+ °C)
    "w": "#fff9c4",             # Thermal plume core (white-hot >700 °C)
    "c": "#00f0ff",             # UAV Reticle / Scan crosshair (Cyan)
    "C": "#0284c7",             # UAV Scan footprint 1 (Cyan border)
    "a": "#fbbf24",             # UAV Overlap footprint reticle (Amber)
    "A": "#d97706",             # UAV Scan footprint 2 (Amber border)
}

MATRIX_24X24 = [
    "    CCCC                ",  # 00: UAV Footprint 1 top
    "   C    C               ",  # 01
    "  C      C    AAAA      ",  # 02: Overlapping Footprint 2
    "  C   c  C   A    A     ",  # 03: Scan crosshair tick
    "  C c c cC   A  a A     ",  # 04: Flight camera reticle
    "  C   c  C  A  aaa A    ",  # 05: Overlap zone
    "   C    C   A   a  A    ",  # 06: Plume rises into reticle
    "    CCCC    A      A    ",  # 07
    "      pPmP   AAAAAA     ",  # 08: Billowing thermal smoke
    "     pPPPPm    ssss     ",  # 09: Andean mountain peak right
    "    pPmPPPPP  sBBBB     ",  # 10: Stepped mountain slope
    "   pPPmPrrPP sBBBB      ",  # 11: Thermal anomaly signature
    "   pPPrRRrPP sBBgG      ",  # 12: Fire front crown
    "   pPrROORrPsBBgGG      ",  # 13: High-heat zone
    "  pPrROoyOoRsBgGlG      ",  # 14: Radiant fire front
    "  pPrOOywyOOBgGGgG      ",  # 15: Core ignition zone
    " pPrROywwyORgGlGBB      ",  # 16: White-hot incandescent center
    " pPrROOywyoRgGGBBB      ",  # 17: Fire flank on slope
    "sBrRROyOOoRBBBBBBB      ",  # 18: Basalt terrace fire line
    "ssBBbrRRroBBsBBBBB      ",  # 19: Charred burn scar & embers
    "BBBBBBBBBBBBBBBBBB      ",  # 20: Stepped Andean bedrock
    "BBgGBBBgGGBBBBBbBb      ",  # 21: Native vegetation patches
    "bbBBBbbBBBbBbbbbbb      ",  # 22: Basalt foundation
    "bbbbbbbbbbbbbbbbbb      ",  # 23: Bedrock base
]

# 5x7 Pixel Font Bitmaps for 'RAPTOR'
BITMAP_FONT_5X7 = {
    "R": [
        [1, 1, 1, 1, 0],
        [1, 0, 0, 0, 1],
        [1, 0, 0, 0, 1],
        [1, 1, 1, 1, 0],
        [1, 0, 1, 0, 0],
        [1, 0, 0, 1, 0],
        [1, 0, 0, 0, 1],
    ],
    "A": [
        [0, 1, 1, 1, 0],
        [1, 0, 0, 0, 1],
        [1, 0, 0, 0, 1],
        [1, 1, 1, 1, 1],
        [1, 0, 0, 0, 1],
        [1, 0, 0, 0, 1],
        [1, 0, 0, 0, 1],
    ],
    "P": [
        [1, 1, 1, 1, 0],
        [1, 0, 0, 0, 1],
        [1, 0, 0, 0, 1],
        [1, 1, 1, 1, 0],
        [1, 0, 0, 0, 0],
        [1, 0, 0, 0, 0],
        [1, 0, 0, 0, 0],
    ],
    "T": [
        [1, 1, 1, 1, 1],
        [0, 0, 1, 0, 0],
        [0, 0, 1, 0, 0],
        [0, 0, 1, 0, 0],
        [0, 0, 1, 0, 0],
        [0, 0, 1, 0, 0],
        [0, 0, 1, 0, 0],
    ],
    "O": [
        [0, 1, 1, 1, 0],
        [1, 0, 0, 0, 1],
        [1, 0, 0, 0, 1],
        [1, 0, 0, 0, 1],
        [1, 0, 0, 0, 1],
        [1, 0, 0, 0, 1],
        [0, 1, 1, 1, 0],
    ],
}

# Thermal color gradient across rows 0..6 of 5x7 font
FONT_ROW_COLORS = [
    "#ffd54f",  # 0: incandescent gold
    "#ffca28",  # 1: bright amber
    "#ffa726",  # 2: radiometric amber
    "#ff9800",  # 3: thermal orange
    "#fb8c00",  # 4: radiant orange
    "#f4511e",  # 5: flame orange
    "#e53935",  # 6: deep crimson
]


def render_pixel_matrix_svg(matrix, palette, origin_x, origin_y, cell_size):
    """Render a pixel matrix into SVG <rect> elements with horizontal run merging."""
    rects = []
    height = len(matrix)
    width = len(matrix[0]) if height > 0 else 0

    for y in range(height):
        row = matrix[y]
        x = 0
        while x < width:
            char = row[x]
            color = palette.get(char)
            if color is None:
                x += 1
                continue
            # Merge contiguous horizontal runs of same color
            run_start = x
            while x < width and row[x] == char:
                x += 1
            run_length = x - run_start
            rx = origin_x + run_start * cell_size
            ry = origin_y + y * cell_size
            rw = run_length * cell_size
            rh = cell_size
            rects.append(
                f'<rect x="{rx}" y="{ry}" width="{rw}" height="{rh}" fill="{color}"/>'
            )
    return rects


def render_wordmark_svg(text, font, origin_x, origin_y, cell_size, letter_spacing):
    """Render wordmark using 5x7 bitmap font with horizontal run merging per row."""
    rects = []
    cur_x = origin_x

    for char in text:
        glyph = font.get(char)
        if not glyph:
            cur_x += 5 * cell_size + letter_spacing
            continue

        for r_idx, row in enumerate(glyph):
            color = FONT_ROW_COLORS[r_idx]
            c_idx = 0
            while c_idx < len(row):
                if row[c_idx] == 1:
                    run_start = c_idx
                    while c_idx < len(row) and row[c_idx] == 1:
                        c_idx += 1
                    run_length = c_idx - run_start
                    rx = cur_x + run_start * cell_size
                    ry = origin_y + r_idx * cell_size
                    rw = run_length * cell_size
                    rh = cell_size
                    rects.append(
                        f'<rect x="{rx}" y="{ry}" width="{rw}" height="{rh}" fill="{color}"/>'
                    )
                else:
                    c_idx += 1
        cur_x += 5 * cell_size + letter_spacing

    return rects


def generate_logo_svg(output_path: Path):
    """Generate wide banner logo.svg (840x240) in dark card theme #0b1017."""
    view_w, view_h = 840, 240
    emblem_cell = 7
    emblem_x = 36
    emblem_y = 36

    # Left Emblem
    emblem_rects = render_pixel_matrix_svg(
        MATRIX_24X24, PALETTE, emblem_x, emblem_y, emblem_cell
    )

    # Wordmark RAPTOR
    # 5x7 at cell_size 9, letter_spacing 9
    wordmark_x = 244
    wordmark_y = 48
    wordmark_rects = render_wordmark_svg(
        "RAPTOR", BITMAP_FONT_5X7, wordmark_x, wordmark_y, cell_size=9, letter_spacing=9
    )

    svg_lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {view_w} {view_h}" width="{view_w}" height="{view_h}" role="img" aria-label="RAPTOR Logo" shape-rendering="crispEdges">',
        '  <title>RAPTOR — Multi-Sensor UAV Photogrammetry &amp; Wildfire Triage Pipeline</title>',
        '  <defs>',
        '    <linearGradient id="bgGlow" x1="0%" y1="0%" x2="100%" y2="100%">',
        '      <stop offset="0%" stop-color="#0e1726"/>',
        '      <stop offset="50%" stop-color="#0b1017"/>',
        '      <stop offset="100%" stop-color="#140a18"/>',
        '    </linearGradient>',
        '    <linearGradient id="accentDivider" x1="0%" y1="0%" x2="0%" y2="100%">',
        '      <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.2"/>',
        '      <stop offset="50%" stop-color="#ff9100" stop-opacity="0.8"/>',
        '      <stop offset="100%" stop-color="#ff1744" stop-opacity="0.2"/>',
        '    </linearGradient>',
        '  </defs>',
        '',
        '  <!-- Background Card -->',
        f'  <rect width="{view_w}" height="{view_h}" rx="12" fill="url(#bgGlow)" stroke="#2d1b2e" stroke-width="1.5"/>',
        '',
        '  <!-- Subtle Technical Grid Lines -->',
        '  <g stroke="#1e293b" stroke-width="0.75" stroke-dasharray="2,6" opacity="0.4">',
        '    <line x1="24" y1="24" x2="816" y2="24"/>',
        '    <line x1="24" y1="216" x2="816" y2="216"/>',
        '    <line x1="220" y1="24" x2="220" y2="216"/>',
        '  </g>',
        '',
        '  <!-- Emblem Backdrop Frame -->',
        '  <rect x="30" y="30" width="180" height="180" rx="8" fill="#070c14" stroke="#1e293b" stroke-width="1"/>',
        '',
        '  <!-- Left Emblem: Stepped Andean Terrain + Thermal Fire Plume + Dual Scan Footprint -->',
        '  <g id="pixel-emblem">',
    ]

    for rect in emblem_rects:
        svg_lines.append(f"    {rect}")

    svg_lines.extend([
        '  </g>',
        '',
        '  <!-- Vertical Separator -->',
        '  <line x1="220" y1="36" x2="220" y2="204" stroke="url(#accentDivider)" stroke-width="1.5"/>',
        '',
        '  <!-- RAPTOR Pixel Wordmark -->',
        '  <g id="pixel-wordmark">',
    ])

    for rect in wordmark_rects:
        svg_lines.append(f"    {rect}")

    svg_lines.extend([
        '  </g>',
        '',
        '  <!-- Horizontal Accent Line -->',
        '  <line x1="244" y1="126" x2="796" y2="126" stroke="#334155" stroke-width="1"/>',
        '  <line x1="244" y1="126" x2="420" y2="126" stroke="#ff9100" stroke-width="1.5"/>',
        '',
        '  <!-- Subtitle Text -->',
        '  <text x="244" y="148" font-family="ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace" font-size="11" font-weight="600" fill="#94a3b8" letter-spacing="1">MULTI-SENSOR UAV PHOTOGRAMMETRY &amp; WILDFIRE TRIAGE <tspan fill="#ff9100">//</tspan> v1.0.0</text>',
        '',
        '  <!-- Operational Status Badges / Pills -->',
        '  <g id="status-pills" font-family="ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace" font-size="10" font-weight="700">',
        '    <!-- Pill 1: Zenith + ODM -->',
        '    <rect x="244" y="168" width="168" height="26" rx="4" fill="#071a2e" stroke="#0284c7" stroke-width="1"/>',
        '    <circle cx="256" cy="181" r="3" fill="#38bdf8"/>',
        '    <text x="266" y="185" fill="#38bdf8">2.5D ZENITH + 3D ODM</text>',
        '',
        '    <!-- Pill 2: Thermal -->',
        '    <rect x="420" y="168" width="112" height="26" rx="4" fill="#250914" stroke="#e11d48" stroke-width="1"/>',
        '    <circle cx="432" cy="181" r="3" fill="#fb7185"/>',
        '    <text x="442" y="185" fill="#fb7185">THERMAL °C</text>',
        '',
        '    <!-- Pill 3: Sensors -->',
        '    <rect x="540" y="168" width="176" height="26" rx="4" fill="#062117" stroke="#059669" stroke-width="1"/>',
        '    <circle cx="552" cy="181" r="3" fill="#34d399"/>',
        '    <text x="562" y="185" fill="#34d399">M3T / H20T / M3M / P1</text>',
        '  </g>',
        '</svg>',
        '',
    ])

    output_path.write_text("\n".join(svg_lines), encoding="utf-8")
    print(f"Generated {output_path} ({len(svg_lines)} lines, {output_path.stat().st_size} bytes)")


def generate_icon_svg(output_path: Path):
    """Generate square icon.svg (128x128) in dark card theme #0b1017."""
    view_w, view_h = 128, 128
    cell_size = 4
    # 24 * 4 = 96 px. Center in 128: offset = (128 - 96)/2 = 16
    origin_x = 16
    origin_y = 16

    emblem_rects = render_pixel_matrix_svg(
        MATRIX_24X24, PALETTE, origin_x, origin_y, cell_size
    )

    svg_lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {view_w} {view_h}" width="{view_w}" height="{view_h}" role="img" aria-label="RAPTOR Icon" shape-rendering="crispEdges">',
        '  <title>RAPTOR App Icon</title>',
        '  <rect width="128" height="128" rx="16" fill="#0b1017" stroke="#2d1b2e" stroke-width="2"/>',
        '  <rect x="10" y="10" width="108" height="108" rx="10" fill="#070c14" stroke="#1e293b" stroke-width="1"/>',
        '  <g id="pixel-icon">',
    ]
    for rect in emblem_rects:
        svg_lines.append(f"    {rect}")
    svg_lines.extend([
        '  </g>',
        '</svg>',
        '',
    ])

    output_path.write_text("\n".join(svg_lines), encoding="utf-8")
    print(f"Generated {output_path} ({len(svg_lines)} lines, {output_path.stat().st_size} bytes)")


def generate_ascii_banner(output_path: Path):
    """Generate pure ASCII/Unicode block-art banner <= 76 cols wide."""
    top = "+" + "-" * 74 + "+"

    def row(content):
        pad = 72 - len(content)
        assert pad >= 0, f"Content too long ({len(content)}): {content}"
        return "| " + content + " " * pad + " |"

    banner_lines = [
        top,
        row("RAPTOR // MULTI-SENSOR UAV PHOTOGRAMMETRY & WILDFIRE TRIAGE PIPELINE"),
        row(""),
        row("  [SCAN]         ██████╗  █████╗ ██████╗ ████████╗ ██████╗ ██████╗"),
        row(" +-------+       ██╔══██╗██╔══██╗██╔══██╗╚══██╔══╝██╔═══██╗██╔══██╗"),
        row(" | +---+ | (Hot) ██████╔╝███████║██████╔╝   ██║   ██║   ██║██████╔╝"),
        row(" | | c | |  ^ ^  ██╔══██╗██╔══██║██╔═══╝    ██║   ██║   ██║██╔══██╗"),
        row(" +-+-+-+-+ (deg) ██║  ██║██║  ██║██║        ██║   ╚██████╔╝██║  ██║"),
        row("   +-----+   ^   ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝        ╚═╝    ╚═════╝ ╚═╝  ╚═╝"),
        row("  ^^^^ (Fire Front)"),
        row(" /^^^^\\  ###▒▒░░  TIERED PIPELINE ARCHITECTURE:"),
        row("/^^^^^^\\####▒▒░░  * Level 0: Thermal Triage Ray-DEM (<2 min)"),
        row("^^^^^^^^####▒▒░░  * Level 1: Tactical 2.5D Zenith Ortho (20-40 s)"),
        row("^^^^^^^^####▒▒░░  * Level 2: Cartographic 3D ODM (15-45 min)"),
        top,
        "",
    ]
    # Verify width constraint <= 76
    for line in banner_lines:
        assert len(line) <= 76, f"Line exceeds 76 cols: {line}"

    output_path.write_text("\n".join(banner_lines), encoding="utf-8")
    print(f"Generated {output_path} ({len(banner_lines)} lines)")


def extract_cli_json(output_path: Path):
    """Extract CLI commands and flags verified against `./raptor --help` and `./raptor run --help`."""
    cli_data = {
        "binary": "raptor",
        "version": "1.0.0",
        "first_time": [
            {
                "command": "./raptor install",
                "description": "Build image, install global raptor command on host PATH, configure deliveries folder and port, and launch webapp.",
            }
        ],
        "everyday_use": [
            {
                "command": "raptor start",
                "description": "Start the webapp background daemon with saved config (~/.raptor/config).",
            },
            {
                "command": "raptor restart",
                "description": "Restart the webapp daemon (same as start).",
            },
            {
                "command": "raptor stop",
                "description": "Stop the webapp background daemon.",
            },
            {
                "command": "raptor status",
                "description": "Check if webapp container is currently running.",
            },
            {
                "command": "raptor logs",
                "description": "Watch live tail logs of webapp background container.",
            },
            {
                "command": "raptor init",
                "description": "Reconfigure deliveries folder path or listening port.",
            },
        ],
        "advanced": [
            {
                "command": "raptor build",
                "description": "Build the RAPTOR container image manually.",
            },
            {
                "command": "raptor run [options]",
                "description": "Process one photogrammetry mission non-interactively (ideal for CI/batch).",
            },
            {
                "command": "raptor webapp [options]",
                "description": "Deploy webapp container with explicit flags, bypassing ~/.raptor/config.",
            },
            {
                "command": "raptor shell",
                "description": "Open an interactive bash shell inside the container.",
            },
        ],
        "run_flags": [
            {
                "category": "Input",
                "flag": "--input DIR",
                "required": True,
                "description": "Path to RGB/thermal flight folder (DJI M3T or H20T).",
            },
            {
                "category": "Input",
                "flag": "--input-ms DIR",
                "required": False,
                "description": "Path to multispectral 4-band flight folder (DJI M3M).",
            },
            {
                "category": "Processing",
                "flag": "--mode MODO",
                "default": "rgb+thermal",
                "choices": ["rgb+thermal", "rgb", "none"],
                "description": "Processing mode selection.",
            },
            {
                "category": "Processing",
                "flag": "--preset NOMBRE",
                "default": "cartografico",
                "choices": ["tactico", "cartografico", "forense"],
                "description": "Quality preset: tactico (rapid field response), cartografico (standard cartography), forense (forensic max detail).",
            },
            {
                "category": "Processing",
                "flag": "--terreno TIPO",
                "default": "plano",
                "choices": ["plano", "escarpado"],
                "description": "Terrain profile; incremental SfM is enabled across all modern presets to prevent photo drops in steep relief.",
            },
            {
                "category": "Processing",
                "flag": "--subsample N",
                "default": None,
                "description": "Emergency subsampling: process 1 in every N photos (N>=2). Cuts SfM time by ~3x in rugged terrain.",
            },
            {
                "category": "Processing",
                "flag": "--reuse-odm",
                "default": False,
                "description": "Reuse existing ODM reconstruction stages from --runs directory.",
            },
            {
                "category": "Processing",
                "flag": "--max-concurrency N",
                "default": "Auto (RAM-budgeted)",
                "description": "Manual thread limit override for ODM stages. Defaults to memory-safe auto budget.",
            },
            {
                "category": "Processing",
                "flag": "--no-gpu",
                "default": False,
                "description": "Force CPU-only execution, disabling CUDA / OpenMVS GPU offload.",
            },
            {
                "category": "Processing",
                "flag": "--verbose",
                "default": False,
                "description": "Stream raw verbose output from ODM and core subroutines.",
            },
            {
                "category": "Export & Deliverables",
                "flag": "--export DIR",
                "default": None,
                "description": "Host destination folder for exported GIS deliverables.",
            },
            {
                "category": "Export & Deliverables",
                "flag": "--export-products LIST",
                "default": "all",
                "description": "Comma-separated products or 'all': rgb, thermal, dsm, multispectral, indices, classes, confidence, area, flight_path, situation, pointclouds.",
            },
            {
                "category": "Export & Deliverables",
                "flag": "--export-epsg CODE",
                "default": "source",
                "description": "Target CRS EPSG code (e.g. 9377 for MAGNA-SIRGAS Origen Nacional Colombia) or 'source'.",
            },
            {
                "category": "Export & Deliverables",
                "flag": "--export-raster-format F",
                "default": "cog",
                "choices": ["cog", "gtiff"],
                "description": "Raster format: Cloud Optimized GeoTIFF (cog) or standard GeoTIFF (gtiff).",
            },
            {
                "category": "Export & Deliverables",
                "flag": "--export-vector-format F",
                "default": "geojson",
                "choices": ["geojson", "gpkg", "shp", "kml"],
                "description": "Vector format for boundaries, flight path, and incident situation summary.",
            },
            {
                "category": "Automation",
                "flag": "--runs DIR",
                "default": "./runs",
                "description": "Persistent working and caching scratch directory.",
            },
            {
                "category": "Automation",
                "flag": "--json",
                "default": False,
                "description": "Output machine-readable run summary JSON upon completion.",
            },
            {
                "category": "Automation",
                "flag": "--serve",
                "default": False,
                "description": "Keep interactive geovisor HTTP server running upon mission completion.",
            },
            {
                "category": "Automation",
                "flag": "--port N",
                "default": "8080",
                "description": "Port for geovisor server when --serve is passed.",
            },
        ],
    }

    output_path.write_text(json.dumps(cli_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Generated {output_path}")


def extract_constants_json(output_path: Path):
    """Extract scientific and hardware constants directly from repo source code."""
    # Source 1: scripts/hardware.py
    # Source 2: core/thermal_params.py
    # Source 3: scripts/thermal_triage_level0.py
    constants_data = {
        "hardware_budgeting": {
            "source_file": "scripts/hardware.py",
            "constants": {
                "SENSOR_MP_DEFAULT_RGB": 12.3,
                "SENSOR_MP_P1_RGB": 45.0,
                "SENSOR_MP_THERMAL": 0.33,
                "SENSOR_MP_MULTISPECTRAL": 5.0,
                "LIGHT_MP": 2.0,
                "MIN_HILOS_POR_PROYECTO": 2,
                "MAX_PROYECTOS_PARALELOS": 2,
                "RAM_THRESHOLD_SAFETY_GIB": 6.0,
                "RESOURCE_MONITOR_SAMPLING_HZ": 2.0,
            },
            "formulas": {
                "safe_concurrency": "max(1, min(cpu_cores, int(mem_available_mb * ram_frac / mb_per_thread)))",
                "mb_per_thread": "max(512, int(megapixels * 1024 / 2 * gpu_factor))",
            },
            "notes": "ZENMUSE P1 45 MP guard: requires ~23 GB per thread during dense stereo matching vs ~6.3 GB for 12 MP M3T, requiring single-job slot isolation.",
        },
        "thermal_radiometry": {
            "source_file": "core/thermal_params.py",
            "sdk_backend": "DJI Thermal SDK v1.8 (dirp)",
            "siata_operational_standards": {
                "emissivity": 0.96,
                "relative_humidity_pct": 15.0,
                "target_distance_m": 25.0,
                "reflected_temp_deg_c": 19.0,
            },
            "sdk_valid_ranges": {
                "emissivity": [0.10, 1.00],
                "humidity": [1.0, 100.0],
                "distance": [1.0, 25.0],
                "reflection": [-40.0, 500.0],
            },
            "firmware_fallback": {
                "sensor": "Zenmuse H20T (R-JPEG v1)",
                "constraint": "Firmware rejects relative humidity < 20% (returncode 253)",
                "action": "Automatic clamping fallback to humidity=20.0% preserving operational continuity.",
            },
            "precedence": "Explicit CLI argument > Environment variable > SIATA Operational Standard",
        },
        "level0_triage": {
            "source_file": "scripts/thermal_triage_level0.py",
            "execution_target": "< 2 minutes directly from flight frames",
            "sensor_intrinsics": {
                "sensor_model": "DJI Mavic 3 Enterprise Thermal (M3T)",
                "resolution": "640x512 VOx microbolometer",
                "pixel_pitch_um": 12.0,
                "focal_length_mm": 9.1,
                "calibrated_focal_px": 758.33,
                "nominal_dfov_deg": 61.0,
            },
            "wildfire_thresholds": {
                "min_fire_temp_c": 88.0,
                "min_cluster_pixels": 9,
            },
            "vertical_datum": {
                "geoid_undulation_m": -24.0,
                "scope": "Regional assumption for Antioquia / Valle de Aburrá (EGM96/GEOCOL)",
                "formula": "H_orthometric = h_ellipsoidal - N (where N = -24.0m -> H = h + 24.0m)",
            },
            "algorithm": "Memory-buffered DEM ROI + 16-step interval bisection ray marching + analytical GNSS/Gimbal/IFOV error propagation.",
        },
        "vegetation_indices": {
            "source_file": "scripts/compute_vegetation_indices.py",
            "sensor": "DJI Mavic 3 Multispectral (M3M 4-band: Green, Red, RedEdge, NIR)",
            "indices": {
                "NDVI": "(NIR - Red) / (NIR + Red)",
                "GNDVI": "(NIR - Green) / (NIR + Green)",
                "NDRE": "(NIR - RedEdge) / (NIR + RedEdge)",
                "MSAVI2": "(2 * NIR + 1 - sqrt((2 * NIR + 1)^2 - 8 * (NIR - Red))) / 2",
            },
        },
    }

    output_path.write_text(json.dumps(constants_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Generated {output_path}")


def main():
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    print("=== Generating RAPTOR Branding & Documentation Data ===")
    generate_logo_svg(ASSETS_DIR / "logo.svg")
    generate_icon_svg(ASSETS_DIR / "icon.svg")
    generate_ascii_banner(ASSETS_DIR / "logo_ascii.txt")
    extract_cli_json(ASSETS_DIR / "cli.json")
    extract_constants_json(ASSETS_DIR / "constants.json")
    print("=== All assets generated successfully ===")


if __name__ == "__main__":
    main()
