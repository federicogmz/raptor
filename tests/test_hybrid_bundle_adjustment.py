"""--use-hybrid-bundle-adjustment de ODM (docker/entrypoint.sh): reportado en
vivo — bundle adjustment incremental es secuencial por diseño (agrega UNA
foto a la vez a la reconstrucción, no hay nada que paralelizar ahí), y sin
esta bandera ODM hace un ajuste GLOBAL completo en CADA foto agregada, que
se pone más caro a medida que crece la reconstrucción. scripts/hardware.py
decide el valor por PRESET (ver TestBundleAdjustmentHibrido en
test_quality_estimate.py); este test extrae el bloque real de bash que lo
traduce a la bandera de ODM, para que no queden desincronizados.
"""
import os
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRYPOINT = os.path.join(REPO, "docker", "entrypoint.sh")


def _bloque_preset():
    """El bloque que resuelve PRESET -> PC_QUALITY/FEAT_QUALITY/MIN_FEATURES/
    SFM_ALGORITHM/HYBRID_BA_FLAG."""
    src = open(ENTRYPOINT, encoding="utf-8").read()
    ini = src.index('if ! _PRESET_TXT=$(python3 scripts/hardware.py preset')
    fin = src.index('HYBRID_BA_FLAG=(--use-hybrid-bundle-adjustment)')
    fin = src.index("\n", fin) + 1
    return src[ini:fin]


def _correr(preset, terreno="plano"):
    guion = f"""
set -euo pipefail
cd {REPO}
PRESET={preset}
TERRENO={terreno}
""" + _bloque_preset() + (
        '\necho "FLAG:${HYBRID_BA_FLAG[@]:-ninguna}"\n'
        'echo "PRESET:$PRESET_NOMBRE SFM:$SFM_ALGORITHM FEATS:$MIN_FEATURES"\n'
        'echo "FASTORTHO:$FAST_ORTHOPHOTO_RGB"\n')
    r = subprocess.run(["bash", "-c", guion], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


class TestBanderaDeBundleAdjustment:
    def test_maxima_no_agrega_la_bandera(self):
        """En el preset más caro el usuario ya está pagando el máximo tiempo a
        propósito: ahí se prioriza la consistencia del ajuste global."""
        assert "FLAG:ninguna" in _correr("maxima")

    @pytest.mark.parametrize("p", ["alta", "estandar", "rapido", "vistazo"])
    def test_resto_de_presets_agrega_la_bandera(self, p):
        assert "FLAG:--use-hybrid-bundle-adjustment" in _correr(p)

    @pytest.mark.parametrize("q,esperado", [(95, "maxima"), (75, "alta"),
                                            (50, "estandar"), (25, "rapido"),
                                            (0, "vistazo")])
    def test_quality_numerico_heredado_sigue_funcionando(self, q, esperado):
        """Hay corridas guardadas, scripts y CI que pasan un número 0-100."""
        assert f"PRESET:{esperado}" in _correr(q)


class TestOtrasPalancasDelPreset:
    """min-num-features y sfm-algorithm estaban HARDCODEADOS en entrypoint.sh
    (12000 / 8000 y siempre incremental) sin importar la calidad pedida, pese
    a ser dos de las palancas que más pesan en el tiempo de reconstrucción."""

    @pytest.mark.parametrize("p,sfm", [("vistazo", "planar"), ("rapido", "planar"),
                                       ("estandar", "incremental"),
                                       ("alta", "incremental"),
                                       ("maxima", "incremental")])
    def test_el_algoritmo_de_sfm_sale_del_preset(self, p, sfm):
        assert f"SFM:{sfm}" in _correr(p)


class TestTerrenoEscarpado:
    """Bug real, encontrado en vivo (misión mision_2026-08-08, terreno
    rocoso): sfm_algorithm=planar alinea por homografías, válido solo si la
    escena es efectivamente plana. En terreno con relieve fuerte descartó el
    76-81% de las fotos en silencio. TERRENO=escarpado fuerza incremental
    incluso en vistazo/rápido para no perder cobertura."""

    @pytest.mark.parametrize("p", ["vistazo", "rapido"])
    def test_fuerza_incremental_en_los_presets_planares(self, p):
        assert "SFM:incremental" in _correr(p, terreno="escarpado")

    @pytest.mark.parametrize("p", ["estandar", "alta", "maxima"])
    def test_no_cambia_nada_en_los_que_ya_son_incremental(self, p):
        assert "SFM:incremental" in _correr(p, terreno="escarpado")

    def test_terreno_plano_no_toca_el_default_del_preset(self):
        assert "SFM:planar" in _correr("vistazo", terreno="plano")

    def test_no_afecta_fast_orthophoto(self):
        """La decisión de saltar MVS (velocidad) es independiente de CÓMO se
        alinean las fotos (cobertura) — forzar incremental por terreno no
        debe desactivar fast-orthophoto en vistazo/rápido."""
        assert "FASTORTHO:1" in _correr("vistazo", terreno="escarpado")

    def test_terreno_invalido_falla_claro(self):
        guion = f"""
set -euo pipefail
cd {REPO}
PRESET=vistazo
TERRENO=montañoso
""" + _bloque_preset()
        r = subprocess.run(["bash", "-c", guion], capture_output=True, text=True, timeout=15)
        assert r.returncode != 0
        assert "terreno desconocido" in r.stdout

    def test_los_presets_rapidos_piden_menos_features(self):
        import re
        feats = {}
        for p in ("vistazo", "rapido", "estandar", "alta", "maxima"):
            feats[p] = int(re.search(r"FEATS:(\d+)", _correr(p)).group(1))
        orden = [feats[p] for p in ("vistazo", "rapido", "estandar", "alta", "maxima")]
        assert orden == sorted(orden), orden
        assert feats["vistazo"] < feats["alta"]

    @pytest.mark.parametrize("p,esperado", [("vistazo", "1"), ("rapido", "1"),
                                            ("estandar", "0"), ("alta", "0"),
                                            ("maxima", "0")])
    def test_fast_orthophoto_de_rgb_solo_en_los_presets_rapidos(self, p, esperado):
        """DensifyPointCloud (MVS) confirmado en vivo como el costo dominante
        de una reconstrucción RGB grande (~11 de ~15h, misión
        mision_2026-08-08, 1199 fotos) — vistazo/rápido lo saltan igual que
        banda D ya hace siempre; estándar en adelante mantienen la nube
        densa porque RGB es la fuente del DSM de la misión."""
        assert f"FASTORTHO:{esperado}" in _correr(p)
