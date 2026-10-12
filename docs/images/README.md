# Visual Documentation & Screenshots

This directory contains visual assets and screenshots demonstrating RAPTOR's interactive Leaflet web geovisor and sensor outputs.

## Capturing Screenshots

To update or replace screenshots from your local RAPTOR deployment:

1. **Access the Geovisor:**
   - Ensure the RAPTOR container / web application is running (`raptor web` or `./raptor web`).
   - Open your browser at [http://localhost:8080](http://localhost:8080).

2. **Standard Screenshots:**
   - `geovisor_overview.png`: General overview of the interactive Leaflet interface with layer controls, status metrics, and base map.
   - `thermal_radiometric.png`: Radiometric thermal mosaic view illustrating surface temperatures with colormap palette (°C).
   - `multispectral_indices.png`: Orthomosaic of vegetation indices (NDVI / NDRE) highlighting canopy health and vigor.

3. **Confidentiality & Data Protection Guidelines:**
   - **Do not include confidential client flight data, restricted airspace information, or sensitive GPS coordinates.**
   - Prefer synthetic flights or de-identified public demo datasets.
   - Ensure high resolution (recommended minimum 1280×720, default 1920×1080) in PNG format with optimized file size.

4. **Deep-linking & Layer Selection Shortcuts:**
   - Overview: `http://localhost:8080/geovisor/index.html?mission=<nombre>`
   - Thermal with colormap legend: `http://localhost:8080/geovisor/index.html?mission=<nombre>&solo=thermal&legend=thermal`
   - NDVI with vegetation vigor legend: `http://localhost:8080/geovisor/index.html?mission=<nombre>&solo=ndvi&legend=ndvi`
   - Satellite base layer: add `&base=sat`
