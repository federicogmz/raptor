"""_reliability_crop: núcleo contiguo con contorno regular y margen.

Es el recorte que decide la forma final del ortomosaico. El test más
importante es el último: protege un bug REAL documentado en el propio código
(el closing dilata y, si se re-intersecta contra la máscara equivocada,
reintroduce justo las zonas de bajo solape que el piso acababa de descartar).
"""
import numpy as np

from trim_low_overlap_edges import _reliability_crop


def _blob(shape, sl):
    m = np.zeros(shape, bool)
    m[sl] = True
    return m


class TestReliabilityCrop:
    def test_conserva_solo_el_componente_mas_grande(self):
        m = np.zeros((200, 200), bool)
        m[20:180, 20:180] = True     # cuerpo principal
        m[5:12, 5:12] = True         # isla chica, lejos
        out = _reliability_crop(m, px_size_m=1.0, open_m=2.0, close_m=2.0)
        assert out[100, 100], "el cuerpo principal sobrevive"
        assert not out[5:12, 5:12].any(), "la isla suelta se descarta"

    def test_elimina_penínsulas_finas(self):
        m = np.zeros((200, 200), bool)
        m[50:150, 50:150] = True
        m[99:101, 150:190] = True    # tentáculo de 2 px de ancho
        out = _reliability_crop(m, px_size_m=1.0, open_m=4.0, close_m=4.0)
        assert out[100, 100]
        assert not out[99:101, 170:190].any(), "el tentáculo fino se abre y desaparece"

    def test_deja_margen_de_seguridad_hacia_adentro(self):
        m = _blob((200, 200), (slice(40, 160), slice(40, 160)))
        out = _reliability_crop(m, px_size_m=1.0, open_m=2.0, close_m=2.0)
        assert out.sum() < m.sum(), "la erosión final tiene que recortar algo"
        assert out[100, 100], "pero el centro se conserva"

    def test_el_resultado_es_siempre_subconjunto_de_la_entrada(self):
        """BUG REAL protegido acá (documentado en la función): el closing
        DILATA el componente limpio. Si se re-intersecta contra la máscara
        original sin filtrar en vez de contra la ya filtrada, reaparecen las
        zonas de bajo solape que el piso había descartado — el "peine" que
        crece de vuelta desde el borde. El resultado NUNCA puede tener un
        píxel que no estuviera en la entrada."""
        rng = np.random.default_rng(7)
        m = np.zeros((200, 200), bool)
        m[40:160, 40:160] = True
        # agujeros dispersos, como los que deja dsm_clean
        holes = rng.random((200, 200)) < 0.05
        m &= ~holes
        out = _reliability_crop(m, px_size_m=1.0, open_m=3.0, close_m=6.0)
        assert not (out & ~m).any(), "el closing no puede reintroducir píxeles descartados"

    def test_mascara_vacia_no_revienta(self):
        out = _reliability_crop(np.zeros((50, 50), bool), px_size_m=1.0,
                                open_m=1.0, close_m=1.0)
        assert out.sum() == 0

    def test_el_radio_va_en_metros_no_en_pixeles(self):
        """Mismo radio físico debe recortar lo mismo en dos GSD distintos —
        es lo que permite usar la función en RGB (cm/px) y en la grilla gruesa
        del DSM (m/celda) sin recalibrar."""
        fino = np.zeros((400, 400), bool); fino[100:300, 100:300] = True
        grueso = np.zeros((200, 200), bool); grueso[50:150, 50:150] = True
        # fino: 0.5 m/px sobre 200px = 100 m de lado; grueso: 1.0 m/px sobre 100px = 100 m
        of = _reliability_crop(fino, px_size_m=0.5, open_m=5.0, close_m=5.0)
        og = _reliability_crop(grueso, px_size_m=1.0, open_m=5.0, close_m=5.0)
        # área conservada en metros² comparable (±10%)
        area_f = of.sum() * 0.5 ** 2
        area_g = og.sum() * 1.0 ** 2
        assert abs(area_f - area_g) / max(area_g, 1) < 0.10, \
            f"mismo radio en metros debe recortar área equivalente ({area_f} vs {area_g} m²)"
