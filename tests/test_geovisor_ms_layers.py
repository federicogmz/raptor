"""Geovisor: hotspot/índices clasificados no deben ofrecerse en el panel
cuando la misión no tiene los datos de origen — y cuando el motivo es
"falta el vuelo multiespectral", tiene que haber una vía directa para
agregarlo.

La capa "severidad" (registerSeveridad(), SEVERIDAD_LUT) fue eliminada por
completo junto con el resto de la funcionalidad de área afectada — hotspot
térmico es ahora la ÚNICA capa de impacto que existe.

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
    """hotspot_termico/índices clasificados: el objeto Layer de Leaflet
    existe siempre (su pane ya está creado desde antes), pero la entrada en
    LAYER_REGISTRY —lo que los hace aparecer en el panel— se crea SOLO si
    CAPAS_DISPONIBLES lo confirma."""

    def test_hotspot_verifica_capas_disponibles_antes_de_registrar(self):
        cuerpo = _cuerpo_de(_js(), "registerHotspot")
        assert "CAPAS_DISPONIBLES.has(" in cuerpo and "'hotspot_termico'" in cuerpo
        assert "return false" in cuerpo

    def test_index_class_verifica_capas_disponibles(self):
        cuerpo = _cuerpo_de(_js(), "registerIndexClass")
        assert "CAPAS_DISPONIBLES.has(name)" in cuerpo
        assert "return false" in cuerpo

    def test_no_quedo_ningun_rastro_de_severidad(self):
        """La capa severidad (registerSeveridad(), LAYER_REGISTRY.severidad,
        SEVERIDAD_LUT) fue eliminada por completo — no debe quedar ninguna
        referencia viva en el fuente."""
        js = _js()
        assert "function registerSeveridad" not in js
        assert not re.search(r"LAYER_REGISTRY\.severidad\s*=", js)
        assert "SEVERIDAD_LUT" not in js

    def test_bounds_json_capas_disponibles_alimenta_el_registro(self):
        js = _js()
        assert "b.capas_disponibles" in js
        assert "CAPAS_DISPONIBLES=new Set(" in js

    def test_el_sondeo_en_vivo_reintenta_el_registro(self):
        """pollBoundsForChanges corre cada 6s durante una misión en curso —
        hotspot/índices pueden aparecer bastante después de que bounds.json
        cambie por primera vez, y el registro tiene que reintentarse ahí, no
        solo una vez al cargar la página."""
        cuerpo = _cuerpo_de(_js(), "pollBoundsForChanges")
        assert "registerHotspot()" in cuerpo
        assert "registerIndexClass" in cuerpo


class TestBotonAgregarMultiespectral:
    def test_existe_la_funcion_de_llamada_a_la_accion(self):
        js = _js()
        assert "function addMsCtaHTML" in js
        assert "addms=1" in js

    def test_se_ofrece_cuando_el_grupo_de_indices_esta_vacio(self):
        cuerpo = _cuerpo_de(_js(), "layersPanelHTML")
        assert "addMsEmptyGroupHTML" in cuerpo
        assert "'indices'" in cuerpo or '"indices"' in cuerpo

    def test_la_webapp_reconoce_el_parametro_addms(self):
        """El botón linkea a /?mission=X&addms=1 — la webapp tiene que leerlo
        y llevar al usuario derecho a dónde agregar la carpeta multiespectral,
        no dejar que la tenga que encontrar por su cuenta. Ya no existe un
        checkbox de sensor que marcar (el formulario detecta el sensor solo
        por el contenido agregado) — lo que corresponde acá es apuntar al
        cuadro único de subida/importación."""
        html = open(INDEX_HTML, encoding="utf-8").read()
        js = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
        assert "addms" in js
        assert "dz-add" in js


class TestCajaAgregarMsNoSeDuplica:
    """Reportado: la caja "Agregar vuelo multiespectral" aparecía DOS veces
    a la vez — la contextual (dentro de la tarjeta de "Área afectada,
    severidad y vegetación" en modo simple, o del grupo vacío en el panel de
    Capas) y la nueva fija al pie del sidebar (ver renderFooterAddMsCta()).
    addMsCtaHTML() (el botón real, con el link) ahora es EXCLUSIVO del pie;
    los otros dos lugares usan addMsEmptyGroupHTML(), que explica el motivo
    pero no repite el botón."""

    def test_layersPanelHTML_no_repite_el_boton(self):
        cuerpo = _cuerpo_de(_js(), "layersPanelHTML")
        assert "addMsCtaHTML(" not in cuerpo, (
            "el panel de Capas no debe generar su propio botón — "
            "addMsEmptyGroupHTML() ya cubre la explicación sin duplicar el "
            "que vive al pie del sidebar")

    def test_renderSummaryCards_no_repite_el_boton(self):
        cuerpo = _cuerpo_de(_js(), "renderSummaryCards")
        assert "addMsCtaHTML(" not in cuerpo, (
            "la tarjeta de 'Área afectada, severidad y vegetación' no debe "
            "traer su propio botón — el del pie del sidebar ya cubre la acción")
        assert "Agregar vuelo multiespectral" not in cuerpo

    def test_addMsCtaHTML_solo_lo_llama_el_pie_del_sidebar(self):
        js = _js()
        llamadas = [m.start() for m in re.finditer(r"addMsCtaHTML\(", js)]
        # Una es la propia definición de la función (function addMsCtaHTML(...),
        # la otra tiene que ser la única llamada real — renderFooterAddMsCta().
        assert len(llamadas) == 2, (
            f"se esperaban 2 apariciones de 'addMsCtaHTML(' (la definición + "
            f"UNA sola llamada, desde renderFooterAddMsCta), hay {len(llamadas)}")
        cuerpo = _cuerpo_de(js, "renderFooterAddMsCta")
        assert "addMsCtaHTML(" in cuerpo


class TestHotspotDefaultOnSiempre:
    """Reportado (originalmente): en una misión sin multiespectral,
    hotspot_termico era la ÚNICA capa de impacto disponible pero quedaba con
    defaultOn:false — entraba invisible, el usuario tenía que saber que
    existía un panel de Capas/pestaña Impacto y tildarla a mano para ver
    algo. defaultOn dependía de si la capa 'severidad' existía; con esa capa
    eliminada por completo (junto con toda la funcionalidad de área
    afectada), hotspot_termico es ahora la ÚNICA capa de impacto posible en
    el geovisor — entra encendida siempre, incondicionalmente."""

    def test_defaultOn_es_siempre_true(self):
        cuerpo = _cuerpo_de(_js(), "registerHotspot")
        assert "defaultOn:true" in cuerpo
        assert "severidad" not in cuerpo

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
    ráster posible (RASTER_LAYER_FACTORY tiene entradas fijas para hotspot y
    los 4 índices y sus clasificados, sin importar qué tenga la misión).
    Antes buildCompareSelect() iteraba esa lista sin filtrar y leía
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
            "LAYER_REGISTRY (p.ej. un índice en una misión sin "
            "multiespectral) revienta en LAYER_REGISTRY[id].label y corta "
            "toggleCompare() a la mitad")

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


# TestPestanaVegetacionSinMultiespectral existía acá: protegía la pestaña
# "Vegetación" del modo simple (renderSimpleTabs/selectSimpleTab) contra
# quedar seleccionada-pero-oculta cuando la misión no tenía multiespectral.
# El modo simple con pestañas Riesgo/Cobertura/Evidencia se eliminó — el
# geovisor ahora tiene un solo panel de capas (layersPanelHTML), sin tabs que
# puedan quedar "activas pero invisibles". La clase de bug que este test
# vigilaba ya no puede pasar (no hay estado de pestaña que perder), y el caso
# que sí sigue vivo —el grupo "índices" vacío sin multiespectral— lo cubre
# TestBotonAgregarMultiespectral::test_se_ofrece_cuando_el_grupo_de_indices_esta_vacio.


class TestEstadoDeEtapaRendereaHTML:
    """Reportado: al fallar una corrida, en vez del ícono de cruz aparecía
    el tag SVG entero como texto literal ("<svg...><use.../></svg> Falló
    (código 1)") en el estado del HUD de progreso.

    Causa real: phSetStage() pintaba con `el.textContent=...`, pero el
    handler del evento 'done' (connectLiveMission()) le pasa markup HTML
    (ícono ✓/✗ + texto) esperando que se RENDERICE, no que se muestre como
    texto plano. textContent escapa cualquier `<` — el navegador nunca lo
    interpreta como tag, así que el ícono nunca aparecía, se veía el
    markup crudo."""

    def test_phSetStage_usa_innerHTML(self):
        cuerpo = _cuerpo_de(_js(), "phSetStage")
        assert "el.innerHTML=" in cuerpo, (
            "phSetStage tiene que asignar con innerHTML — el handler 'done' "
            "le pasa un ícono <svg> esperando que se renderice, no texto plano")
        assert "el.textContent=" not in cuerpo

    def test_el_handler_done_le_pasa_markup_de_icono(self):
        """Confirma que el caso que de verdad importa (el mensaje de fallo,
        con el ícono de cruz) sigue pasando por phSetStage — si en algún
        refactor futuro dejara de llamarlo, este test lo nota."""
        js = _js()
        i = js.index("}else if(d.kind==='done'){")
        bloque = js[i:i + 800]
        assert "phSetStage(ok" in bloque
        assert "<svg" in bloque
