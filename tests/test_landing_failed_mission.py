"""Tarjeta de misión en el landing (webapp/static/index.html::loadMissions):
una misión que FALLÓ pero ya tenía tiles parciales (publish_partial() los va
publicando a medida que corre, así que una corrida que se cae en
post-procesamiento después de que ODM ya escribió capas puede tener
has_tiles=true) tiene que mandar a "Continuar"/setup, no al geovisor —
reportado en vivo: el botón mandaba al geovisor (mapa estático, sin forma de
reintentar ni de ver la opción "Reusar lo ya reconstruido").
"""
import os
import re

INDEX_HTML = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "webapp", "static", "index.html")


def _js():
    html = open(INDEX_HTML, encoding="utf-8").read()
    return re.search(r"<script>(.*?)</script>", html, re.S).group(1)


def _bloque_boton():
    js = _js()
    ini = js.index("let btn = '';")
    fin = js.index("\n", js.index("Continuar</button>`;", ini)) + 1
    return js[ini:fin]


class TestBotonMisionFallida:
    def test_failed_se_chequea_antes_que_has_tiles(self):
        bloque = _bloque_boton()
        assert "if(failed)" in bloque, \
            "el botón no está usando la variable `failed` ya calculada arriba"
        assert bloque.index("if(failed)") < bloque.index("m.has_tiles"), \
            "failed tiene que chequearse ANTES que has_tiles, si no una " \
            "corrida fallida con tiles parciales manda al geovisor en vez " \
            "de a corregir/reintentar"

    def test_failed_manda_a_setup_no_al_geovisor(self):
        bloque = _bloque_boton()
        i = bloque.index("if(failed)")
        rama = bloque[i:bloque.index("\n", i)]
        assert "openSetup(" in rama
        assert "/view/" not in rama

    def test_la_variable_failed_ya_existia(self):
        """No es nueva — ya se usaba para el badge "falló", solo no
        alcanzaba al botón."""
        js = _js()
        assert "const failed" in js
        assert "badge fail" in js
