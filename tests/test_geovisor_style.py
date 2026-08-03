"""Regresión puntual: el texto de la escala del geovisor se veía duplicado
y borroso.

Causa real: leaflet.css trae `text-shadow: 1px 1px #fff` de fábrica en
`.leaflet-control-scale-line` (pensado para texto oscuro sobre mapa satelital
claro). Nuestro override pisa background/color/border pero no tocaba
text-shadow, así que en tema oscuro el texto (claro) quedaba con un halo casi
del mismo tono desplazado 1px — se ve como el número duplicado.
"""
import os
import re

STYLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "geovisor", "style.css")


def _regla(selector):
    css = open(STYLE, encoding="utf-8").read()
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"no se encontró la regla {selector}"
    return m.group(1)


def test_la_escala_no_hereda_el_text_shadow_de_leaflet():
    regla = _regla(".leaflet-control-scale-line")
    assert re.search(r"text-shadow\s*:\s*none\s*!important", regla), (
        "sin 'text-shadow:none!important' vuelve el halo blanco de leaflet.css "
        "que se ve como el número de la escala duplicado")


def test_panel_toggle_sigue_visible_en_pantallas_angostas():
    """Regresión real: en @media (max-width:920px) el panel colapsado se
    desliza dejando visible solo una franja de 46px del PIE del panel (no la
    cabecera con el botón ✕) — .panel-toggle (el botón ☰ para reabrirlo)
    vivía OCULTO del todo ahí (`display:none!important`), así que cerrar el
    panel en una ventana angosta no dejaba NADA clickeable para reabrirlo.
    .panel-toggle no vive dentro de .panel (está en .map-area,
    position:absolute), así que no había ninguna razón real para ocultarlo
    en ese breakpoint — no chocaba con la franja colapsada."""
    css = open(STYLE, encoding="utf-8").read()
    m = re.search(r"@media\s*\(max-width:920px\)\s*\{(.*)\}\s*$", css, re.S)
    assert m, "no se encontró el bloque @media (max-width:920px)"
    bloque = m.group(1)
    assert not re.search(r"\.panel-toggle\s*\{[^}]*display\s*:\s*none", bloque), (
        "el botón para reabrir el panel (.panel-toggle) no debe ocultarse "
        "en pantallas angostas — es la única forma de reabrirlo una vez "
        "colapsado ahí")
