"""scripts/subsample_photos.py — modo urgencia: procesar 1 de cada N fotos.

Es la palanca más grande para acortar una corrida en vistazo/rápido con
terreno escarpado: la reconstrucción incremental es secuencial (~12 s/foto
de bundle adjustment, no paralelizable entre fotos), así que procesar la
tercera parte de las fotos recorta ese costo casi a la tercera parte. El
conjunto completo queda intacto en data/ y en images_full/; la próxima
preparación lo restaura solo.
"""
import os
import subprocess


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "subsample_photos.py")


def _correr(proj, n, cwd=None):
    return subprocess.run(["python3", SCRIPT, str(proj), str(n)],
                          capture_output=True, text=True, timeout=15,
                          cwd=str(cwd) if cwd else None)


def _proyecto(tmp_path, n_archivos=10, con_geo=True):
    """Proyecto ODM falso con n_archivos imágenes y geo.txt (formato ODM real:
    línea 1 = header de proyección "EPSG:4326" que escribe
    escribir_geo_txt()/odm_staging.py, NO una fila de imagen; línea 2 en
    adelante = una fila por foto, primera columna = nombre de archivo,
    tab-separado)."""
    proj = tmp_path / "proyecto"
    images = proj / "images"
    images.mkdir(parents=True)
    for i in range(n_archivos):
        (images / f"DJI_20260615_{i:04d}_V.JPG").write_bytes(b"foto")
    if con_geo:
        with open(proj / "geo.txt", "w") as f:
            f.write("EPSG:4326\n")
            for i in range(n_archivos):
                f.write(f"DJI_20260615_{i:04d}_V.JPG 6.0 -75.0 2500.0 0.0 0.0 0.0\n")
    return proj


class TestSubsample:
    def test_deja_1_de_cada_n_y_mueve_el_resto(self, tmp_path):
        proj = _proyecto(tmp_path, 10)
        r = _correr(proj, 3)
        assert r.returncode == 0, r.stdout + r.stderr
        quedan = sorted(p.name for p in (proj / "images").iterdir())
        # files[::3] de 0..9 = índices 0,3,6,9
        assert quedan == [f"DJI_20260615_{i:04d}_V.JPG" for i in (0, 3, 6, 9)], quedan
        movidos = sorted(p.name for p in (proj / "images_full").iterdir()
                         if p.suffix == ".JPG")
        assert len(movidos) == 6
        # Nada se perdió: juntos suman el conjunto original.
        assert len(quedan) + len(movidos) == 10

    def test_filtra_geo_txt_a_las_que_quedan(self, tmp_path):
        proj = _proyecto(tmp_path, 9)
        _correr(proj, 3)
        lineas = (proj / "geo.txt").read_text().splitlines()
        # Línea 1 = header de proyección, intacto — ver
        # test_preserva_header_de_proyeccion_en_geo_txt para el caso que
        # reventaba ODM cuando esto se filtraba junto con las imágenes.
        assert lineas[0] == "EPSG:4326", lineas
        nombres = [ln.split()[0] for ln in lineas[1:]]
        assert nombres == ["DJI_20260615_0000_V.JPG",
                           "DJI_20260615_0003_V.JPG",
                           "DJI_20260615_0006_V.JPG"], nombres
        # El geo.txt completo queda guardado al lado.
        assert (proj / "images_full" / "geo.txt").exists()

    def test_preserva_header_de_proyeccion_en_geo_txt(self, tmp_path):
        """Bug real: el header (línea 1, "EPSG:4326") no es una fila de
        imagen — su primer token nunca matchea un nombre de archivo, así que
        filtrarlo con el mismo criterio que las filas de imagen lo hacía
        desaparecer SIEMPRE. Con el header borrado, ODM lee la primera fila
        de datos como si fuera el SRS y revienta con "Bad SRS supplied:
        DJI_...JPG" en vez de reconstruir — visto en una corrida real con
        modo urgencia. La primera línea tiene que sobrevivir sin condición,
        sin importar qué quede en `keep`."""
        proj = _proyecto(tmp_path, 9)
        _correr(proj, 3)
        primera = (proj / "geo.txt").read_text().splitlines()[0]
        assert primera == "EPSG:4326", primera

    def test_n_uno_no_hace_nada(self, tmp_path):
        proj = _proyecto(tmp_path, 5)
        r = _correr(proj, 1)
        assert r.returncode == 0
        assert len(list((proj / "images").iterdir())) == 5
        assert not (proj / "images_full").exists() or \
            len(list((proj / "images_full").iterdir())) == 0

    def test_menos_fotos_que_n_no_submuestrea(self, tmp_path):
        proj = _proyecto(tmp_path, 2)
        r = _correr(proj, 5)
        assert r.returncode == 0
        assert len(list((proj / "images").iterdir())) == 2

    def test_sin_carpeta_images_falla_con_mensaje_claro(self, tmp_path):
        r = _correr(tmp_path / "no_existe", 3)
        assert r.returncode != 0
        assert "no existe" in (r.stdout + r.stderr)

    def test_borra_el_cache_images_json_de_odm(self, tmp_path):
        """Bug real: ODM cachea el escaneo de images/ en images.json y lo
        reusa SIN comparar contra el contenido actual (stages/dataset.py) —
        un reintento que cambia qué queda en images/ (este script es el
        único que lo hace, en modo urgencia) deja ese caché apuntando a un
        set de fotos que ya no existe. Visto en una corrida real: ODM
        reportaba conteos de banda desparejados ("band Red has only 151
        images instead of 152") y reventaba, aunque images/ en disco
        estuviera perfectamente balanceado. images.json tiene que
        desaparecer en cada corrida de este script, para forzar a ODM a
        reescanear desde cero."""
        proj = _proyecto(tmp_path, 9)
        (proj / "images.json").write_text('{"stale": "de una corrida anterior"}')
        _correr(proj, 3)
        assert not (proj / "images.json").exists()

    def test_es_reapplicable_despues_de_repoblar(self, tmp_path):
        """La preparación repuebla images/ con el conjunto completo en la
        próxima corrida; re-submuestrear re-aplica el recorte y limpia el
        images_full/ anterior (los originales viven en data/, no ahí)."""
        proj = _proyecto(tmp_path, 9)
        _correr(proj, 3)
        # Simula la preparación de la próxima corrida: vuelve a copiar TODO.
        for i in range(9):
            (proj / "images" / f"DJI_20260615_{i:04d}_V.JPG").write_bytes(b"foto")
        assert len(list((proj / "images").iterdir())) == 9
        _correr(proj, 3)
        assert len(list((proj / "images").iterdir())) == 3
        # Solo las fotos (el geo.txt original respaldado suma un archivo más).
        assert len([p for p in (proj / "images_full").iterdir()
                    if p.suffix == ".JPG"]) == 6


def _proyecto_multiespectral(tmp_path, n_capturas=9):
    """Proyecto ODM falso con n_capturas capturas M3M, 4 bandas cada una
    (G/R/RE/NIR) — mismo naming real que prepare_multispectral_odm.py."""
    proj = tmp_path / "proyecto_ms"
    images = proj / "images"
    images.mkdir(parents=True)
    bandas = ("G", "R", "RE", "NIR")
    for i in range(n_capturas):
        for banda in bandas:
            (images / f"DJI_20260615_{i:04d}_MS_{banda}.TIF").write_bytes(b"foto")
    return proj, bandas


class TestSubsampleMultiespectral:
    """Bug real: subsamplear la lista PLANA de archivos (los 4 de una
    captura quedan intercalados alfabéticamente: _G, _NIR, _R, _RE) deja
    bandas con conteos distintos entre sí. ODM arma pares de banda por
    CAPTURA — con conteos desparejados revienta con "Cannot match bands by
    filename..." antes de reconstruir nada (visto en una corrida real:
    banda Red con 151 imágenes, NIR con 151, de 152 esperadas)."""

    def test_las_4_bandas_quedan_balanceadas(self, tmp_path):
        proj, bandas = _proyecto_multiespectral(tmp_path, 9)
        r = _correr(proj, 3)
        assert r.returncode == 0, r.stdout + r.stderr
        quedan = [p.name for p in (proj / "images").iterdir()]
        conteos = {b: sum(1 for f in quedan if f.endswith(f"_MS_{b}.TIF"))
                   for b in bandas}
        assert len(set(conteos.values())) == 1, (
            f"las 4 bandas tienen que quedar con el mismo conteo, salió {conteos} "
            f"— con conteos distintos ODM no puede emparejar bandas por captura")
        assert conteos["G"] == 3, conteos  # 9 capturas / 3 = 3 capturas → 3 por banda

    def test_las_4_bandas_de_cada_captura_kept_quedan_juntas(self, tmp_path):
        """No alcanza con que los CONTEOS coincidan (podría ser casualidad) —
        tienen que ser exactamente las mismas capturas en las 4 bandas."""
        proj, bandas = _proyecto_multiespectral(tmp_path, 9)
        _correr(proj, 3)
        quedan = [p.name for p in (proj / "images").iterdir()]
        capturas_por_banda = {
            b: sorted(f.replace(f"_MS_{b}.TIF", "") for f in quedan if f.endswith(f"_MS_{b}.TIF"))
            for b in bandas
        }
        todas_iguales = len({tuple(v) for v in capturas_por_banda.values()}) == 1
        assert todas_iguales, capturas_por_banda

    def test_menos_capturas_que_n_no_submuestrea(self, tmp_path):
        """2 capturas (8 archivos) con n=5: menos CAPTURAS que el factor, no
        menos archivos — con la lista plana (8 archivos > 5) esto se
        submuestreaba igual y rompía el balance de bandas."""
        proj, bandas = _proyecto_multiespectral(tmp_path, 2)
        r = _correr(proj, 5)
        assert r.returncode == 0
        assert len(list((proj / "images").iterdir())) == 8
