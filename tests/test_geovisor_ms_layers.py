"""Geovisor: severidad/hotspot/índices clasificados no deben ofrecerse en el
panel cuando la misión no tiene los datos de origen — y cuando el motivo es
"falta el vuelo multiespectral", tiene que haber una vía directa para
agregarlo.

LÍMITE DE ESTOS TESTS: la imagen raptor:latest no trae runtime de JavaScript
(mismo caso que tests/test_form_export.py), así que lo que corre acá dentro de
`make test` es ESTRUCTURAL sobre el fuente, no de comportamiento. Una
validación de comportamiento REAL es posible fuera de esta suite (jsdom +
Node en el host, con fetch/EventSource polyfillados) — así se encontró y
confirmó el bug de TestOrdenDeDeclaracion más abajo, que ningún test
estructural habría detectado. Se deja como test manual, no automatizado:
agregar Node+jsdom a la imagen de test es una decisión de infraestructura
aparte, no tomada acá.
"""
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(REPO, "geovisor", "app.js")
INDEX_HTML = os.path.join(REPO, "webapp", "static", "index.html")


def _js():
    return open(APP_JS, encoding="utf-8").read()


def _cuerpo_de(js, nombre):
    m = re.search(rf"function {nombre}\s*\([^)]*\)\s*{{", js)
    assert m, f"no se encontró {nombre}()"
    i = m.end()
    prof = 1
    while prof:
        if js[i] == "{":
            prof += 1
        elif js[i] == "}":
            prof -= 1
        i += 1
    return js[m.end():i - 1]


class TestOrdenDeDeclaracion:
    """Regresión real (no hipotética): encontrada corriendo el geovisor real
    en jsdom con datos reales de una misión RGB+térmico sin multiespectral.

    `renderCapasPanel()` arma el panel de Capas llamando a `def.legend()` de
    forma SÍNCRONA para cada capa registrada — no perezoso, no en el click del
    usuario. `registerHotspot()`'s legend cierra sobre `liveMsBandIds`
    (`let`), y si esa declaración vive DESPUÉS del punto donde
    `renderCapasPanel()` se invoca por primera vez, la lectura cae en la zona
    muerta temporal: `ReferenceError: Cannot access 'liveMsBandIds' before
    initialization`, sin capturar, que corta la ejecución del script ahí
    mismo. Todo lo que venía después en el archivo —incluida la inicialización
    del panel "Situación actual"— nunca llegaba a correr: la página quedaba
    pegada en "Cargando datos de la misión…" para siempre, sin ningún error
    visible salvo en la consola del navegador.

    Verificado con una ejecución real (jsdom + fetch/EventSource polyfilled)
    contra el servidor corriendo con datos reales: con la declaración después
    del primer uso, revienta con exactamente ese ReferenceError; declarada
    antes, el panel renderiza sin errores. Acá se fija la invariante de orden
    en el fuente, para que un futuro refactor no la rompa de nuevo sin que
    nada lo note."""

    def test_liveMsBandIds_se_declara_antes_de_renderCapasPanel(self):
        js = _js()
        i_decl = js.index("let liveMsBandIds=")
        i_uso = js.index("\nrenderCapasPanel();")
        assert i_decl < i_uso, (
            "let liveMsBandIds tiene que declararse ANTES de la primera llamada "
            "a renderCapasPanel() — esa llamada invoca legend() de forma "
            "síncrona para cada capa, y una 'let' declarada después cae en la "
            "zona muerta temporal (bug real, ver el docstring de esta clase)")

    def test_solo_hay_una_declaracion_de_liveMsBandIds(self):
        js = _js()
        n = len(re.findall(r"\blet liveMsBandIds\s*=", js))
        assert n == 1, f"se esperaba una sola declaración, hay {n}"


class TestRegistroCondicionado:
    """severidad/hotspot_termico/índices clasificados: el objeto Layer de
    Leaflet existe siempre (su pane ya está creado desde antes), pero la
    entrada en LAYER_REGISTRY —lo que los hace aparecer en el panel— se crea
    SOLO si CAPAS_DISPONIBLES lo confirma."""

    @pytest.mark.parametrize("fn,capa", [
        ("registerSeveridad", "'severidad'"),
        ("registerHotspot", "'hotspot_termico'"),
    ])
    def test_verifica_capas_disponibles_antes_de_registrar(self, fn, capa):
        cuerpo = _cuerpo_de(_js(), fn)
        assert "CAPAS_DISPONIBLES.has(" in cuerpo and capa in cuerpo
        assert "return false" in cuerpo

    def test_index_class_verifica_capas_disponibles(self):
        cuerpo = _cuerpo_de(_js(), "registerIndexClass")
        assert "CAPAS_DISPONIBLES.has(name)" in cuerpo
        assert "return false" in cuerpo

    def test_no_quedo_ninguna_asignacion_incondicional_de_severidad(self):
        """La regresión concreta: antes de este fix,
        `LAYER_REGISTRY.severidad={...}` corría SIEMPRE, sin ningún `if`
        antepuesto. Ahora solo debe existir DENTRO de registerSeveridad()."""
        js = _js()
        asignaciones = [m.start() for m in re.finditer(
            r"LAYER_REGISTRY\.severidad\s*=\s*\{", js)]
        assert len(asignaciones) == 1, \
            "tiene que haber exactamente una asignación (dentro de registerSeveridad)"
        cuerpo = _cuerpo_de(js, "registerSeveridad")
        assert "LAYER_REGISTRY.severidad={" in cuerpo or "LAYER_REGISTRY.severidad = {" in cuerpo

    def test_bounds_json_capas_disponibles_alimenta_el_registro(self):
        js = _js()
        assert "b.capas_disponibles" in js
        assert "CAPAS_DISPONIBLES=new Set(" in js

    def test_el_sondeo_en_vivo_reintenta_el_registro(self):
        """pollBoundsForChanges corre cada 6s durante una misión en curso —
        severidad/hotspot pueden aparecer bastante después de que bounds.json
        cambie por primera vez, y el registro tiene que reintentarse ahí, no
        solo una vez al cargar la página."""
        cuerpo = _cuerpo_de(_js(), "pollBoundsForChanges")
        assert "registerSeveridad()" in cuerpo
        assert "registerHotspot()" in cuerpo
        assert "registerIndexClass" in cuerpo


class TestBotonAgregarMultiespectral:
    def test_existe_la_funcion_de_llamada_a_la_accion(self):
        js = _js()
        assert "function addMsCtaHTML" in js
        assert "addms=1" in js

    def test_se_ofrece_cuando_el_grupo_de_indices_esta_vacio(self):
        cuerpo = _cuerpo_de(_js(), "layersPanelHTML")
        assert "addMsCtaHTML" in cuerpo
        assert "'indices'" in cuerpo or '"indices"' in cuerpo

    def test_la_webapp_reconoce_el_parametro_addms(self):
        """El botón linkea a /?mission=X&addms=1 — la webapp tiene que leerlo
        y marcar el sensor multiespectral solo, no dejar que el usuario tenga
        que descubrir el checkbox por su cuenta."""
        html = open(INDEX_HTML, encoding="utf-8").read()
        js = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
        assert "addms" in js
        assert "sensors.ms = true" in js


class TestReusarOdmNoSaltaUnSensorNuevo:
    """docker/entrypoint.sh: SKIP_ODM es un interruptor GLOBAL (una sola
    reconstrucción, no una por sensor) — agregar multiespectral a una misión
    que ya tenía RGB+térmico y pedir 'reusar' saltearía la reconstrucción MS
    que nunca existió, sin ningún error claro más adelante."""

    def test_start_rechaza_reusar_si_falta_reconstruir_el_sensor_nuevo(self):
        """La función en sí (_validate_reuse_odm) se prueba de punta a punta
        en tests/test_webapp.py::TestAgregarSensorYReusarOdm — acá solo se
        confirma que start_mission() la llama de verdad."""
        main_py = os.path.join(REPO, "webapp", "main.py")
        src = open(main_py, encoding="utf-8").read()
        assert "def _validate_reuse_odm(" in src
        i = src.index("def _validate_reuse_odm(")
        bloque = src[i:i + 1600]
        assert 'odm_prev["multispectral"]' in bloque
        assert 'odm_prev["rgb"]' in bloque
        assert 'odm_prev["thermal"]' in bloque
        assert "errors += _validate_reuse_odm(mission_dir, mode, has_multispectral, reuse_odm)" in src


class TestHotspotDefaultOnSinSeveridad:
    """Reportado: en una misión sin multiespectral, hotspot_termico es la
    ÚNICA capa de impacto disponible pero quedaba con defaultOn:false —
    entraba invisible, el usuario tenía que saber que existía un panel de
    Capas/pestaña Impacto y tildarla a mano para ver algo. defaultOn ahora
    depende de si severidad existe: si NO existe (típicamente sin
    multiespectral), hotspot es la señal principal y entra encendida; si SÍ
    existe, severidad manda y hotspot se queda apagada por defecto (mismo
    comportamiento de siempre, para no encimar dos capas de impacto)."""

    def test_defaultOn_depende_de_si_hay_severidad(self):
        cuerpo = _cuerpo_de(_js(), "registerHotspot")
        assert "defaultOn:!CAPAS_DISPONIBLES.has('severidad')" in cuerpo

    def test_el_registro_en_vivo_tambien_agrega_la_capa_si_defaultOn(self):
        """El registro inicial pasa por un loop que agrega al mapa toda capa
        con defaultOn (ver Object.entries(LAYER_REGISTRY)...forEach de más
        arriba en el archivo) — pero un registro que llega DESPUÉS, durante
        pollBoundsForChanges() (misión todavía procesando), no pasa por ese
        loop. Sin este agregado explícito, una misión que empieza siendo
        solo-térmica y consigue su hotspot_termico recién en una pasada
        posterior se quedaría con defaultOn:true pero invisible en el mapa
        hasta que el usuario la tildara a mano — el mismo bug, solo que en
        el camino 'en vivo' en vez del de carga inicial."""
        cuerpo = _cuerpo_de(_js(), "pollBoundsForChanges")
        assert "hotspotLayer.addTo(map)" in cuerpo


class TestComparadorNoRevientaSinCapasOpcionales:
    """Reportado: al abrir "Comparar" en una misión sin multiespectral, los
    dos mapas del comparador terminaban desincronizados — se podían arrastrar
    de forma independiente y no coincidían espacialmente.

    Causa real: COMPARABLE_IDS es la lista ESTÁTICA de todo tipo de capa
    ráster posible (RASTER_LAYER_FACTORY tiene entradas fijas para severidad,
    hotspot y los 4 índices, sin importar qué tenga la misión). Antes
    buildCompareSelect() iteraba esa lista sin filtrar y leía
    `LAYER_REGISTRY[id].label` — para una misión sin esos datos,
    LAYER_REGISTRY[id] es undefined y `.label` revienta con un TypeError sin
    capturar. Esa excepción cortaba toggleCompare() a la mitad: nunca se
    llegaba a fijar el ancho de #compare-left-map ni a conectar los
    listeners 'move' que sincronizan los dos mapas — quedaban del todo
    independientes, exactamente el síntoma reportado."""

    def test_buildCompareSelect_filtra_por_layer_registry(self):
        cuerpo = _cuerpo_de(_js(), "buildCompareSelect")
        assert "COMPARABLE_IDS.filter(id=>LAYER_REGISTRY[id])" in cuerpo, (
            "sin este filtro, un id de COMPARABLE_IDS sin entrada en "
            "LAYER_REGISTRY (p.ej. 'severidad' sin multiespectral) revienta "
            "en LAYER_REGISTRY[id].label y corta toggleCompare() a la mitad")

    def test_initSliderDrag_no_se_llama_en_cada_activacion(self):
        """Antes initSliderDrag() corría en cada apertura de 'Comparar', no
        solo la primera vez — cada llamada cuelga listeners nuevos en
        document (mousemove/mouseup/touchmove/touchend) que nunca se
        sueltan. Tiene que quedar DENTRO del bloque `if(!compareLeftMap)`
        (inicialización única), no suelta en el cuerpo de toggleCompare()."""
        cuerpo = _cuerpo_de(_js(), "toggleCompare")
        i_guard = cuerpo.index("if(!compareLeftMap)")
        i_init = cuerpo.index("initSliderDrag();")
        # El cierre del bloque `if(!compareLeftMap){...}` es la línea
        # `initSliderDrag();` misma si quedó adentro — se verifica indirecto:
        # tiene que aparecer ANTES del `fullW=` que sí corre en cada
        # activación (eso confirma que quedó en la rama de una sola vez).
        i_fullw = cuerpo.index("const fullW=")
        assert i_guard < i_init < i_fullw, (
            "initSliderDrag() tiene que llamarse dentro de la inicialización "
            "de una sola vez (if(!compareLeftMap){...}), antes de fullW=, no "
            "en cada activación de toggleCompare()")


class TestPestanaVegetacionSinMultiespectral:
    """Reportado: la pestaña "Vegetación" (modo simple) aparecía aunque la
    misión no tuviera multiespectral — al abrirla siempre mostraba "No hay
    información de este tipo en esta misión", un callejón sin salida
    redundante con la tarjeta "Agregar vuelo multiespectral" que ya se
    ofrece en la pestaña Impacto."""

    def test_renderSimpleTabs_oculta_vegetacion_sin_indices(self):
        cuerpo = _cuerpo_de(_js(), "renderSimpleTabs")
        assert "liveMsBandIds.length===0" in cuerpo
        assert "vegTab.hidden=hide" in cuerpo

    def test_no_deja_la_pestana_activa_oculta(self):
        """Si el usuario estaba parado en Vegetación y el multiespectral
        desaparece de la señal (o nunca estuvo), no debe quedar una pestaña
        activa pero invisible — tiene que saltar a Impacto."""
        cuerpo = _cuerpo_de(_js(), "renderSimpleTabs")
        assert "selectSimpleTab('impacto')" in cuerpo
