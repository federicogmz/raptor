#!/usr/bin/env python3
"""Informe de emergencia HTML — imprimible en una página, para el puesto de
mando (PDF directo desde el navegador con Ctrl+P).

Reúne en UN archivo lo que ya calculó el pipeline, sin inventar nada:
situation.json (focos, temperaturas, confianza),
coverage.json (qué fracción del área volada quedó cubierta), flight_quality
(cómo se voló) y run_summary.json (qué productos salieron y a qué GSD), más
vistas previas embebidas del RGB y del térmico.

Escribe:
  outputs/reporte_emergencia.html
  y una copia en EXPORT_DIR si la variable está definida (la entrega).

Uso: python3 scripts/export_report.py
"""
import base64
import html
import json
import os
import shutil
import tempfile

from osgeo import gdal

gdal.UseExceptions()

OUT_PATH = "outputs/reporte_emergencia.html"
PREVIEWS = {
    "rgb": "outputs/rgb_orthomosaic.tif",
    "termico": "outputs/thermal_orthomosaic.tif",
}


def _read_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _preview_data_uri(path, width=520):
    """Vista previa PNG (submuestreada, ~pocos KB) como data URI para
    incrustarla sin archivos externos. None si no se puede generar."""
    if not os.path.isfile(path):
        return None
    try:
        ds = gdal.Open(path)
        src_w = ds.RasterXSize
        out_h = max(1, int(ds.RasterYSize * width / src_w)) if src_w else 1
        fd, tmp_png = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            gdal.Translate(tmp_png, ds, format="PNG", width=width, height=out_h)
            with open(tmp_png, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        finally:
            try:
                os.unlink(tmp_png)
            except OSError:
                pass
        return f"data:image/png;base64,{b64}"
    except Exception:
        return None


def _fmt_fecha(iso):
    if not iso:
        return "—"
    return str(iso).replace("T", " ")[:16]


def _esc(v):
    return html.escape(str(v)) if v is not None else "—"


def main():
    sit = _read_json("outputs/situation.json") or {}
    cov = _read_json("outputs/coverage.json") or {}
    fq = _read_json("outputs/flight_quality.json") or {}
    resumen = _read_json("outputs/run_summary.json") or {}

    # ── Tarjetas ejecutivas ─────────────────────────────────────────────
    if sit:
        cards = f"""
      <div class="card"><div class="l">Focos térmicos activos</div>
        <div class="v">{_esc(sit.get('hotspots_activos'))}</div></div>
      <div class="card"><div class="l">Temp. máxima</div>
        <div class="v">{_esc(sit.get('temp_max'))} °C</div></div>
      <div class="card"><div class="l">Temp. promedio</div>
        <div class="v">{_esc(sit.get('temp_promedio'))} °C</div></div>
      <div class="card"><div class="l">Cobertura</div>
        <div class="v">{_esc(sit.get('cobertura_pct'))} %</div></div>"""
    else:
        cards = "<div class='card' style='grid-column:1/-1'>Sin datos de impacto detectados en esta misión.</div>"

    confianza = sit.get("confianza")
    cobertura_baja = sit.get("alerta_cobertura_baja")
    avisos = []
    if cobertura_baja:
        avisos.append("⚠ Cobertura baja vs. el área volada — verificar antes de decidir sobre este mapa.")
    if confianza and confianza != "alta":
        avisos.append(f"Confianza del dato: {confianza}.")

    # ── Tabla de focos ──────────────────────────────────────────────────
    focos_rows = ""
    for h in sit.get("hotspots") or []:
        focos_rows += (f"<tr><td>{_esc(h.get('lat'))}, {_esc(h.get('lon'))}</td>"
                       f"<td>{_esc(h.get('temp_c'))} °C</td></tr>")
    if not focos_rows:
        focos_rows = "<tr><td colspan=2>Ninguno detectado</td></tr>"

    # ── Productos (run_summary) ─────────────────────────────────────────
    prods_rows = ""
    for clave, p in (resumen.get("productos") or {}).items():
        if isinstance(p, dict) and "error" not in p:
            prods_rows += (f"<tr><td>{_esc(clave)}</td>"
                           f"<td>{_esc(p.get('gsd_m'))} m/px</td>"
                           f"<td>{_esc(p.get('crs'))}</td>"
                           f"<td>{_esc(p.get('cobertura_pct'))} %</td></tr>")
    if not prods_rows:
        prods_rows = "<tr><td colspan=4>Sin productos todavía</td></tr>"

    # ── Cobertura vs. vuelo ─────────────────────────────────────────────
    cov_rows = ""
    for nombre, p in (cov.get("productos") or {}).items():
        cov_rows += (f"<tr><td>{_esc(nombre)}</td>"
                     f"<td>{_esc(p.get('cobertura_area_volada_pct'))} %</td></tr>")
    if not cov_rows:
        cov_rows = "<tr><td colspan=2>—</td></tr>"

    # ── Vistas previas ──────────────────────────────────────────────────
    imgs = ""
    for nombre, path in PREVIEWS.items():
        uri = _preview_data_uri(path)
        imgs += (f'<figure><img src="{uri}" alt="{nombre}">'
                 f"<figcaption>{nombre}</figcaption></figure>" if uri else "")
    imgs_html = ("<h2>Vistas previas</h2>\n<div class=\"imgs\">" + imgs + "</div>"
                 if imgs else "")

    aviso_html = ("".join(f'<div class="aviso">{a}</div>' for a in avisos)
                  if avisos else "")

    doc = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Informe de emergencia — {_esc(sit.get('captura') or resumen.get('terminado'))}</title>
<style>
  body{{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;margin:0;padding:24px;color:#1a1a1a;background:#fff}}
  h1{{font-size:1.3rem;margin:0 0 4px}} .meta{{color:#555;font-size:.85rem;margin-bottom:16px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}}
  .card{{border:1px solid #ddd;border-radius:8px;padding:10px 12px;background:#fafafa}}
  .card .l{{font-size:.75rem;color:#555;text-transform:uppercase;letter-spacing:.03em}}
  .card .v{{font-size:1.25rem;font-weight:700}} .card .v.s{{font-size:.85rem}}
  h2{{font-size:.95rem;margin:20px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}}
  table{{border-collapse:collapse;width:100%;font-size:.85rem}}
  th,td{{border:1px solid #ddd;padding:5px 8px;text-align:left}} th{{background:#f0f0f0}}
  .aviso{{background:#fff3cd;border:1px solid #e6c200;border-radius:8px;padding:8px 12px;margin-bottom:12px;font-size:.85rem}}
  .imgs{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin-top:12px}}
  figure{{margin:0}} img{{max-width:100%;border:1px solid #ddd;border-radius:6px}}
  figcaption{{font-size:.75rem;color:#555;margin-top:4px}}
  @media print{{body{{padding:8px}} .aviso{{-webkit-print-color-adjust:exact}}}}
</style></head><body>
<h1>🚨 Informe de emergencia</h1>
<div class="meta">Captura: {_esc(_fmt_fecha(sit.get('captura')))} · Misión: {_esc(os.environ.get('RAPTOR_MISSION', 'mision'))} · Generado: {_fmt_fecha(resumen.get('terminado'))} · Equipo: {_esc(fq.get('equipo'))}</div>
{aviso_html}
<div class="grid">{cards}</div>
<h2>Focos térmicos</h2>
<table><tr><th>Coordenadas (lat, lon)</th><th>Temp. pico</th></tr>{focos_rows}</table>
<h2>Cobertura vs. área volada ({_esc(cov.get('area_volada_km2'))} km² volados)</h2>
<table><tr><th>Producto</th><th>% del área volada con dato</th></tr>{cov_rows}</table>
<h2>Calidad del levantamiento</h2>
<p class="meta">{_esc(fq.get('calidad'))} · solape p50 {_esc(fq.get('solape_p50'))}% · velocidad media {_esc(fq.get('velocidad_media_ms'))} m/s</p>
<h2>Productos</h2>
<table><tr><th>Producto</th><th>GSD</th><th>CRS</th><th>Cobertura del ráster</th></tr>{prods_rows}</table>
{imgs_html}
</body></html>"""

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(doc)
    try:
        os.chmod(OUT_PATH, 0o666)
    except OSError:
        pass

    export_dir = os.environ.get("EXPORT_DIR", "").strip()
    if export_dir and os.path.isdir(export_dir):
        try:
            copia = os.path.join(export_dir, "reporte_emergencia.html")
            shutil.copy2(OUT_PATH, copia)
            os.chmod(copia, 0o666)
            print(f"  ✅ copia en {export_dir}")
        except OSError as exc:
            print(f"  ⚠ no se pudo copiar a {export_dir}: {exc}")

    print(f"✅ {OUT_PATH} (informe de emergencia)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
