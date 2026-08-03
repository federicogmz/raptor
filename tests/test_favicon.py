"""Favicon: pasó de satélite (🛰️) a helicóptero (🚁) por error — un dron
cuatrimotor no es un helicóptero (un solo rotor grande + cola), y usar un
emoji de por medio deja el resultado a la suerte de qué glifo trae cada
plataforma. Ahora es un ícono propio (SVG inline: 4 hélices + brazos +
cuerpo central), sin depender de ningún emoji.

Corre sobre los dos puntos de entrada (geovisor/index.html,
webapp/static/index.html) — cada uno declaró su propio favicon por
separado, así que un fix en uno no corrige el otro solo.
"""
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_FILES = {
    "geovisor": os.path.join(REPO, "geovisor", "index.html"),
    "webapp": os.path.join(REPO, "webapp", "static", "index.html"),
}
# Emojis descartados en el camino — ninguno debe reaparecer como favicon
# ni como logo del header.
EMOJIS_DESCARTADOS = ["\U0001F6F0", "\U0001F681"]  # 🛰️ satélite, 🚁 helicóptero


def _html(nombre):
    return open(HTML_FILES[nombre], encoding="utf-8").read()


class TestFaviconEsUnDronNoUnEmoji:
    def test_las_dos_paginas_declaran_favicon(self):
        for nombre in HTML_FILES:
            assert 'rel="icon"' in _html(nombre), f"{nombre}: falta <link rel=\"icon\">"

    def test_ningun_emoji_descartado_en_el_favicon_ni_el_logo(self):
        for nombre in HTML_FILES:
            html = _html(nombre)
            for emoji in EMOJIS_DESCARTADOS:
                assert emoji not in html, (
                    f"{nombre}: quedó el emoji {emoji!r} (satélite o helicóptero) "
                    "en vez del ícono de dron")

    def test_el_favicon_es_un_svg_con_las_4_helices(self):
        """No alcanza con "no es el emoji viejo" — confirma que el nuevo
        favicon es el SVG del dron (4 <circle> = 4 hélices), no otro
        emoji cualquiera puesto sin pensarlo."""
        for nombre in HTML_FILES:
            html = _html(nombre)
            i = html.index('rel="icon"')
            href = html[i:i + 2000]
            assert "image/svg+xml" in href
            assert href.count("%3Ccircle") == 4, (
                "se esperaban 4 <circle> (una hélice por brazo) en el SVG del favicon")
