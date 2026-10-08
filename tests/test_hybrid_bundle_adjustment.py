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
    @pytest.mark.parametrize("p", ["forense", "maxima"])
    def test_forense_no_agrega_la_bandera(self, p):
        """En el preset más caro el usuario ya está pagando el máximo tiempo a
        propósito: ahí se prioriza la consistencia del ajuste global."""
        assert "FLAG:ninguna" in _correr(p)

    @pytest.mark.parametrize("p", ["tactico", "cartografico", "alta", "estandar"])
    def test_cartografico_agrega_la_bandera(self, p):
        assert "FLAG:--use-hybrid-bundle-adjustment" in _correr(p)

    @pytest.mark.parametrize("q,esperado", [(95, "forense"), (75, "cartografico"),
                                            (50, "cartografico"), (25, "tactico"),
                                            (0, "tactico")])
    def test_quality_numerico_heredado_sigue_funcionando(self, q, esperado):
        """Hay corridas guardadas, scripts y CI que pasan un número 0-100."""
        assert f"PRESET:{esperado}" in _correr(q)


class TestOtrasPalancasDelPreset:
    """min-num-features y sfm-algorithm estaban HARDCODEADOS en entrypoint.sh
    (12000 / 8000 y siempre incremental) sin importar la calidad pedida, pese
    a ser dos de las palancas que más pesan en el tiempo de reconstrucción."""

    @pytest.mark.parametrize("p,sfm", [("tactico", "incremental"),
                                       ("cartografico", "incremental"),
                                       ("forense", "incremental")])
    def test_el_algoritmo_de_sfm_sale_del_preset(self, p, sfm):
        assert f"SFM:{sfm}" in _correr(p)


class TestTerrenoEscarpado:
    """Modo escarpado para presets con SfM."""

    @pytest.mark.parametrize("p", ["cartografico", "forense"])
    def test_mantiene_incremental_en_escarpado(self, p):
        assert "SFM:incremental" in _correr(p, terreno="escarpado")

    def test_terreno_invalido_falla_claro(self):
        guion = f"""
set -euo pipefail
cd {REPO}
PRESET=cartografico
TERRENO=montañoso
""" + _bloque_preset()
        r = subprocess.run(["bash", "-c", guion], capture_output=True, text=True, timeout=15)
        assert r.returncode != 0
        assert "terreno desconocido" in r.stdout

    def test_los_presets_rapidos_piden_menos_features(self):
        import re
        feats = {}
        for p in ("tactico", "cartografico", "forense"):
            feats[p] = int(re.search(r"FEATS:(\d+)", _correr(p)).group(1))
        orden = [feats[p] for p in ("tactico", "cartografico", "forense")]
        assert orden == sorted(orden), orden
        assert feats["tactico"] < feats["cartografico"] < feats["forense"]

    @pytest.mark.parametrize("p,esperado", [("tactico", "1"), ("vistazo", "1"),
                                            ("cartografico", "0"), ("estandar", "0"),
                                            ("forense", "0"), ("maxima", "0")])
    def test_fast_orthophoto_solo_en_tactico(self, p, esperado):
        assert f"FASTORTHO:{esperado}" in _correr(p)
