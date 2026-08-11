"""Bug real, reportado en vivo: una misión que ya llevaba horas de ODM
(térmico + multiespectral) fallaba al final porque detect_area_afectada.py
trataba "ningún píxel superó el umbral" (un resultado VÁLIDO — la misión
puede legítimamente no tener área quemada detectable) como un error fatal
(sys.exit(1)), lo que abortaba la corrida ENTERA antes de llegar a generar
tiles. compute_severity_classes.py tenía el mismo problema del otro lado
(exigía area_afectada.geojson incondicionalmente).

Estos tests mockean compute_z_scores()/detect_polygon() en vez de armar
GeoTIFFs sintéticos completos — lo que se prueba es el CONTRATO de main()
(qué hace cuando el resultado es "no hay área"), no el álgebra de detección
en sí (ya cubierta por otros tests de este módulo si existen).

Nota: sin importlib.reload() a propósito — ninguno de los dos módulos tiene
efectos de import dependientes del cwd (a diferencia de generate_tiles.py,
que sí corre su cuerpo entero al importarse), así que reload() no hace
falta y ROMPE otros tests: crea objetos nuevos para las constantes del
módulo (p.ej. TERM_BREAKS), y test_thermal_hotspot_standalone.py compara
esa constante por identidad (`is`) contra la que importó compute_thermal_
hotspot.py al principio de la sesión de pytest."""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))


class TestDetectAreaAfectadaSinArea:
    def test_result_none_no_hace_sys_exit_1(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        os.makedirs("outputs")
        import detect_area_afectada as mod
        # MS_PATH/TH_PATH tienen que "existir" para pasar el primer chequeo
        # (ambos son "outputs/*.tif", ya creado arriba).
        open(mod.MS_PATH, "w").close()
        open(mod.TH_PATH, "w").close()
        monkeypatch.setattr(mod, "compute_z_scores", lambda: (1, 2, 3, 4, 5, 6, 7, 8))
        monkeypatch.setattr(mod, "detect_polygon", lambda *a, **k: None)
        # No debe lanzar SystemExit(1) — a lo sumo SystemExit(0)/None.
        try:
            mod.main()
        except SystemExit as e:
            assert e.code in (0, None), \
                f"main() no debería abortar con código {e.code} cuando no hay área — es un resultado válido"

    def test_result_valido_sigue_imprimiendo_ok(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        os.makedirs("outputs")
        import detect_area_afectada as mod
        open(mod.MS_PATH, "w").close()
        open(mod.TH_PATH, "w").close()
        monkeypatch.setattr(mod, "compute_z_scores", lambda: (1, 2, 3, 4, 5, 6, 7, 8))
        monkeypatch.setattr(mod, "detect_polygon", lambda *a, **k: ("outputs/area_afectada.geojson", 1234.0))
        mod.main()
        assert "área detectada" in capsys.readouterr().out


class TestComputeSeverityClassesSinArea:
    def test_sin_cache_es_error_real(self, tmp_path, monkeypatch):
        """CACHE_PATH faltante SÍ es un prerrequisito real (detect-area-
        afectada nunca corrió) — eso sigue siendo un error fatal."""
        monkeypatch.chdir(tmp_path)
        import compute_severity_classes as mod
        try:
            mod.main()
            assert False, "debería haber abortado sin CACHE_PATH"
        except SystemExit as e:
            assert e.code == 1

    def test_con_cache_sin_poligono_no_es_error(self, tmp_path, monkeypatch):
        """CACHE_PATH existe (detect-area-afectada SÍ corrió) pero
        POLY_PATH no (no encontró área) — resultado válido, no error."""
        monkeypatch.chdir(tmp_path)
        import compute_severity_classes as mod
        os.makedirs("outputs")
        open(mod.CACHE_PATH, "wb").close()
        assert not os.path.isfile(mod.POLY_PATH)
        try:
            mod.main()
        except SystemExit as e:
            assert e.code in (0, None), \
                f"main() no debería abortar con código {e.code} cuando no hay área que clasificar"
