# RAPTOR — DJI M3T / H20T / M3M

**R**econstrucción **A**érea de **P**roductos **T**érmicos, **Ó**pticos (y
multiespectrales) para **R**espuesta — pipeline reproducible para generar
ortomosaicos RGB, térmicos e índices de vegetación (NDVI/GNDVI/NDRE)
georreferenciados a partir de vuelos fotogramétricos DJI (Mavic 3T, Matrice
300 RTK + Zenmuse H20T, y opcionalmente Mavic 3 Multispectral), con geovisor
Leaflet interactivo.

**Todo el pipeline corre en UN SOLO contenedor Docker** (ODM + post-procesamiento
propio, sin contenedores anidados). El DJI Thermal SDK ya está incluido en el
repositorio. Solo necesitas las imágenes fuente del vuelo.

---

## Modo webapp — por defecto

`docker run` sin argumentos levanta una **webapp interactiva** (FastAPI):
subís las fotos crudas del vuelo desde el navegador, elegís qué sensores y
productos procesar, ves el progreso en vivo (SSE) y al terminar se muestra
directo el geovisor con los tiles — todo en un solo puerto, sin flags de
`docker run` ni montar volúmenes de antemano.

Los sensores son **independientes**: podés cargar solo el vuelo M3T/H20T
(RGB + térmico), solo el M3M (multiespectral) o los dos en la misma misión.
La webapp detecta qué tipo de archivo subiste (`*_V/_W`, `*_T`, `*_MS_*`) y
valida la combinación **antes** de lanzar el pipeline, así una misión a la
que le falten fotos falla en el formulario —corregible ahí mismo, sin
resubir lo ya cargado— y no 40 minutos después adentro de ODM. Si la misión
ya tiene reconstrucciones ODM guardadas, ofrece reusarlas en vez de rehacer
el SfM.

```bash
./raptor build
./raptor webapp --export ~/entregas
# abrir http://localhost:8080
```

`--export` es la carpeta **de tu disco** donde van a salir los productos: el
lanzador la monta en el contenedor, así que lo que elijas en el formulario es
una ruta real del host y no algo que desaparece al cerrar el contenedor. Sin
`--export` la webapp funciona igual, pero la exportación aparece deshabilitada.

Sin GPU se cae a CPU solo (`--no-gpu` lo fuerza). Las fotos subidas y los
resultados quedan en `runs/<misión>/`, que el lanzador monta por vos;
`--runs DIR` lo cambia de lugar.

---

## Automatización y CI

`./raptor run` procesa una misión sin interacción, con códigos de salida y un
resumen legible por máquina — apto para encadenar en un pipeline:

```bash
./raptor run \
  --input ./vuelos/la_clara \
  --input-ms ./vuelos/la_clara_ms \
  --export ./entregas/la_clara \
  --export-products rgb,thermal,dsm,area,classes \
  --export-epsg 9377 \
  --json
```

Sale con código distinto de 0 si el pipeline falla. `--json` imprime
`run_summary.json`: qué productos salieron, con qué GSD, CRS y cobertura real,
cuántas entidades tiene cada vector y dónde quedó la entrega **en el disco del
host**. Ese resumen se escribe siempre, también cuando la corrida falla, con el
código de salida adentro — en CI importa tanto en qué etapa murió como el
código. `./raptor run --help` lista todas las opciones.

A diferencia del modo interactivo, `run` **no** deja el geovisor sirviendo al
terminar (eso sería un cuelgue en CI); agregá `--serve` si lo querés.

> **Nota:** el `CMD` por defecto de la imagen es `webapp`. Para el pipeline
> batch hay que pasar `run` como argumento explícito — `./raptor run` ya lo
> hace; ver "Uso manual con `docker run`" más abajo si invocás Docker a mano.

## Calidad y hardware

`--quality N` (0-100, default 75) controla el **detalle del modelo de
superficie** — la nube de puntos densa y la malla de la que sale el
ortomosaico y el DSM — y el **techo de resolución** que se le pide a ODM.
Antes de arrancar, `./raptor run` (y el formulario de la webapp) muestran a
qué resolución van a salir los productos y cuánto se espera que tarde, según
tu hardware:

```bash
./raptor run --input ./vuelos/la_clara --quality 90
```
```
🖥️  Hardware detectado: 20 núcleos, 27 GB RAM libres — sin GPU (corre en CPU)
🎚️  Calidad máxima (90%): nube de puntos y malla en nivel 'ultra'…
📐 Hasta 1 cm/px en el ortomosaico y el DSM — el techo real lo pone el GSD…
⏱️  Tiempo estimado con tu hardware: 70 min – 3.1 h para 1652 fotos. Aproximado…
```

Dos cosas que conviene tener claras:

- **La resolución de salida no la decide la calidad, la decide el vuelo.**
  El GSD (metros por píxel en el suelo) lo fija la altura de vuelo y el
  sensor; ODM lo mide de la reconstrucción y **nunca puede ir más fino** que
  eso, sin importar qué tan alta sea la calidad pedida — pedir 1 cm a un
  vuelo cuyo GSD real es 9 cm no agrega detalle, solo produce píxeles más
  chicos. Lo que la calidad SÍ decide es si se puede pedir un techo más
  **grueso** a propósito (para terminar antes), y si el modelo 3D resuelve o
  no objetos como copas de árboles y bordes de tejados.
- **Calidad baja + fotos grandes puede aplanar la vegetación.** Con muy poco
  detalle en la malla, una copa de árbol no llega a resolverse: la superficie
  sale lisa y el árbol se desplaza al proyectarse (ortomosaicos que "no
  parecen true-ortho", con sombras corridas). Subir la calidad es la forma
  correcta de arreglarlo — no un ajuste de nitidez ni de resolución.

El nº de hilos de ODM se calcula solo a partir de los núcleos y la RAM
disponible (`scripts/hardware.py`) y se reparte por sensor: en una máquina más
grande, ODM paraleliza más de una; en una más chica, se acota antes de que el
kernel mate el proceso por falta de memoria (mismo mecanismo que ya protegía
al multiespectral, generalizado a RGB y térmico). `--max-concurrency N` lo
fuerza a mano si hace falta.

---

## Uso manual con `docker run` (para scripts / control fino)

Si preferís invocar Docker vos mismo (automatización, CI, una sola misión
puntual) en vez de la webapp, seguí esta guía:

### Guía rápida: de la tarjeta SD al geovisor

### Requisitos

- Docker
- GPU NVIDIA + `nvidia-container-toolkit` (opcional; sin GPU, ODM usa CPU)

```bash
# Ubuntu 24.04:
sudo apt install docker.io nvidia-container-toolkit
sudo systemctl restart docker
```

### Paso 1 — Construir la imagen

```bash
docker build -t raptor .
```

### Paso 2 — Ejecutar (¡todo en un comando!)

```bash
docker run --gpus all -v /ruta/a/fotos:/input -p 8080:8080 raptor run
```

> El argumento `run` al final es obligatorio: el `CMD` por defecto de la
> imagen es `webapp` (ver arriba), así que para el modo batch/CLI clásico
> hay que pedirlo explícitamente.

Sin GPU en el host, omitir `--gpus all`: ODM detecta la ausencia de
`nvidia-smi` en tiempo de ejecución y cae a CPU automáticamente. El
mismatch de versión CUDA (driver algo más viejo que lo que pide la imagen
de ODM) ya viene resuelto por defecto (`NVIDIA_DISABLE_REQUIRE=1` horneado
en la imagen) — no hace falta pasarlo a mano.

> **Montá los volúmenes en este modo.** La imagen **ya no declara `VOLUME`**
> para `processing/`, `preprocessing/`, `outputs/` ni `geovisor/tiles/` (la
> webapp necesita poder reemplazar esas rutas por symlinks en runtime para
> manejar varias misiones en una sesión — ver el comentario en el
> `Dockerfile`). Sin `VOLUME` declarado no hay red de seguridad de volumen
> anónimo: **si no montás nada en modo `run`, los productos se pierden al
> borrar el contenedor.** `./raptor run` monta lo necesario por vos.

Montalos para conservar los resultados, ver los archivos directamente (p.ej.
los TIFF de temperatura °C en `preprocessing/thermal_dji_sdk/` para usarlos en
otro programa) y no perder el trabajo de ODM si el pipeline falla a mitad de
camino (para poder retomar con `SKIP_ODM=1`):

```bash
docker run --gpus all \
  -v /ruta/a/fotos:/input \
  -v $PWD/processing:/app/processing \
  -v $PWD/preprocessing:/app/preprocessing \
  -v $PWD/outputs:/app/outputs \
  -v $PWD/geovisor/tiles:/app/geovisor/tiles \
  -p 8080:8080 \
  raptor run
```

Si ya corriste sin montar `preprocessing/` (como en un `docker run` previo
sin `--rm`), sacá los archivos con `docker cp <container>:/app/preprocessing/thermal_dji_sdk ./preprocessing`.

**Una carpeta por misión (recomendado):** `processing/`, `outputs/` y
`geovisor/tiles/` guardan el estado de LA misión que se procesó ahí — si
corrés una misión nueva montando las mismas rutas de una anterior, ODM
reconstruye sobre datos mezclados de dos vuelos distintos.
Usá una carpeta `runs/<nombre-misión>/` por corrida:

```bash
mkdir -p runs/la_clara/{processing,outputs,tiles}
docker run --gpus all --rm \
  -v /ruta/a/la_clara:/input \
  -v $PWD/runs/la_clara/processing:/app/processing \
  -v $PWD/runs/la_clara/outputs:/app/outputs \
  -v $PWD/runs/la_clara/tiles:/app/geovisor/tiles \
  -p 8080:8080 \
  raptor run
```

Así ninguna corrida pisa ni mezcla el trabajo de otra. Para ver el geovisor
de una misión ya procesada más adelante: `docker run --rm -p 8080:8080 -v
$PWD/runs/la_clara/tiles:/app/geovisor/tiles raptor serve`.

> Nota: si preferís no lidiar con volúmenes/rutas del host a mano, la
> [webapp por defecto](#modo-webapp--por-defecto) resuelve exactamente este
> mismo problema (una carpeta por misión, sin mezclar corridas) subiendo
> las fotos por navegador en vez de montarlas.

**Multiespectral (DJI M3M, opcional):** si además tenés un vuelo multiespectral
de la misma zona, montá un SEGUNDO volumen aparte de `/input` (es otro
vuelo/sensor, no se mezcla con el RGB+térmico):

```bash
docker run --gpus all \
  -v /ruta/a/fotos:/input \
  -v /ruta/al/vuelo-m3m:/input_ms \
  -v $PWD/outputs:/app/outputs \
  -v $PWD/geovisor/tiles:/app/geovisor/tiles \
  -p 8080:8080 \
  raptor run
```

También se puede correr un vuelo M3M **solo** (sin `/input`), montando
únicamente `/input_ms` y pasando `-e MODE=none`: en ese caso el DSM sale del
propio proyecto multiespectral (que también corre con `--dsm`).

Si `/input_ms` no está montado, este módulo ni se toca — cero impacto en
misiones RGB+térmico existentes. El vuelo M3M se procesa con ODM (que soporta
multiespectral nativamente vía el tag XMP `Camera:BandName`) usando
`--radiometric-calibration camera+sun` (calibra a reflectancia con el sensor
de sol embebido en cada banda, sin necesitar panel de calibración física). La
cámara RGB "D" del M3M (sensor aparte de las 4 lentes MS) no se procesa por
este módulo.

Variables de entorno útiles (`-e VAR=valor`):

| Variable | Default | Uso |
|---|---|---|
| `MODE` | `rgb+thermal` | `rgb` para saltar todo el térmico; `none` si la misión NO tiene vuelo RGB/térmico (solo multiespectral — requiere `/input_ms`) |
| `SKIP_ODM` | `0` | `1` para reusar `processing/*_odm/` de una corrida previa |
| `MS_SOURCE_DIR` | `/input_ms` | Carpeta fuente del vuelo multiespectral (el módulo corre SOLO si existe) |
| `PORT` | `8080` | Puerto del geovisor |
| `SERVE` | `1` | `0` para no levantar el geovisor al terminar |
| `QUALITY` | `75` | 0-100. Detalle del modelo de superficie (copas de árboles, bordes) y techo de resolución del ortomosaico/DSM — nunca más fino que el GSD real del vuelo. Ver «Calidad» abajo |
| `MAX_CONCURRENCY` | automático | Hilos de ODM. Por defecto se calcula según la RAM disponible y se reparte por sensor/etapa; bajalo si el proceso muere sin mensaje (ver «Memoria» en Notas) |
| `EXPORT_DIR` | — | Carpeta de entrega. Sin esto no se exporta nada |
| `EXPORT_PRODUCTS` | `all` | Qué exportar, separado por comas (ver abajo) |
| `EXPORT_RASTER_FORMAT` | `cog` | `cog` \| `gtiff` |
| `EXPORT_VECTOR_FORMAT` | `geojson` | `geojson` \| `gpkg` \| `shp` \| `kml` |
| `EXPORT_EPSG` | `source` | EPSG de salida, o `source` para no reproyectar |

### Entrega de los productos

Al final del pipeline se pueden copiar los productos a una carpeta elegida, en
el formato y el sistema de referencia que necesite quien los recibe. En la
webapp es un bloque del formulario (destino, qué exportar, formato y CRS); por
CLI son las variables `EXPORT_*`:

```bash
docker run --gpus all \
  -v /ruta/a/fotos:/input \
  -v /ruta/de/entregas:/entregas \
  -e EXPORT_DIR=/entregas/la_clara \
  -e EXPORT_PRODUCTS=rgb,thermal,dsm,area,classes \
  -e EXPORT_EPSG=9377 \
  -e EXPORT_VECTOR_FORMAT=gpkg \
  -p 8080:8080 raptor run
```

Productos disponibles: `rgb`, `thermal`, `dsm`, `multispectral`, `indices`,
`classes`, `confidence`, `area`, `flight_path`, `situation`, `pointclouds`
(o `all`). Los que la misión no generó se omiten sin fallar.

**EPSG:9377 (MAGNA-SIRGAS / Origen-Nacional)** es el valor recomendado: es el
sistema único nacional de Colombia adoptado por el IGAC, el que esperan las
entidades para cartografía oficial. ODM produce sus salidas en la UTM WGS84 que
corresponde al GPS del vuelo, así que sin este paso hay que reproyectar a mano
en QGIS después de cada misión. Los rásters de clases (severidad, hotspot,
índices clasificados) se remuestrean por vecino más cercano, nunca promediando
— promediar clases inventa categorías intermedias que no existen.

La carpeta destino es una ruta **de adentro del contenedor**: para escribir en
el disco del host hay que montarla (`-v /ruta/del/host:/entregas`). La webapp lo
verifica mientras se escribe y avisa ahí mismo si la ruta no existe o no se
puede escribir, en vez de fallar al final de la corrida. Junto a los archivos se
escribe un `export_manifest.json` con qué se exportó, en qué CRS y con qué
formatos.

El entrypoint hace **todo automáticamente**:
1. **Organiza** las imágenes desde `/input` (busca `*_V.JPG`, `*_W.JPG`, `*_T.JPG` recursivamente) y, si `/input_ms` existe, las bandas MS desde ahí (`*_MS_G/R/RE/NIR.TIF`)
2. **Extrae metadatos** GPS/EXIF; **convierte** R-JPEG → °C (DJI SDK) + denoise + re-encoding para ODM
3. **Ejecuta ODM RGB** (SfM + DSM + ortofoto, ~20-30 min con GPU), **ODM Térmico nativo** (SfM + malla + textura + ortofoto calibrada en °C, ~15-20 min) y, si aplica, **ODM Multiespectral** (SfM + calibración + ortofoto multibanda)
4. **Recorta bordes** de bajo solape (mismo criterio para los tres productos, con máscara de confianza según solape de cámaras) y limpia el DSM (descarta relleno sintético); si aplica, **calcula NDVI/GNDVI/NDRE/MSAVI2** y clasifica **área afectada, severidad y hotspot térmico** (combina brillo multiespectral + anomalía térmica)
5. **Genera tiles** + exporta COG/COPC
6. **Despliega** el geovisor en `http://localhost:8080`

### Paso 3 — Abrir el geovisor

**http://localhost:8080**

Incluye:
- Capas RGB y térmica con opacidad ajustable
- **Selector de combinación de bandas** (desplegable): para la capa RGB,
  reordenar sus canales R/G/B; para multiespectral, elegir entre
  combinaciones predefinidas (CIR, RedEdge) o armar una personalizada
  combinando cualquiera de las 4 bandas espectrales (Red/Green/RedEdge/NIR)
- Paletas de color (inferno, viridis, jet, ironbow, hot/cold)
- Slider de comparación RGB vs térmico
- Detección de hotspots
- Herramienta de medición de distancias
- Hillshade del DSM
- Exportación de vista
- Capas de índices de vegetación (NDVI/GNDVI/NDRE/MSAVI2, paleta RdYlGn),
  área afectada, severidad y hotspot térmico, si la misión incluyó un vuelo
  multiespectral

---

## Pipeline

```
data/rgb_mosaico/ + data/termica_mosaico/ [+ data/multiespectral_mosaico/]
        │
        ▼
  1. Metadatos (GPS, EXIF)
  2. Preparación: geo.txt/imágenes RGB+MS; térmico → DJI SDK °C + denoise +
     re-encode Kelvin×100 (formato que ODM calibra nativamente)
  3. ODM RGB (SfM+DSM+orto) + ODM Térmico nativo (SfM+malla+textura+orto en °C)
     [+ ODM Multiespectral (SfM+calibración+orto multibanda)]
  4. Limpieza DSM (descarta relleno sintético) + recorte de bordes de bajo
     solape (mismo criterio geométrico para los 3 productos, máscara de
     confianza según solape de cámaras)
     [+ índices de vegetación + área afectada/severidad/hotspot térmico]
  5. Tiles XYZ + geovisor (con selector de combinación de bandas)
  6. Exportación cloud-optimized: rasters → COG, nubes de puntos → COPC
        │
        ▼
  outputs/: dsm.tif, rgb_orthomosaic.tif, thermal_orthomosaic.tif (todos COG)
            [multispectral_orthomosaic.tif,
             indices/{ndvi,gndvi,ndre,msavi2}.tif (COG),
             area_afectada.geojson, severidad_class.tif, termico_hotspot_class.tif]
            point_cloud_{rgb,thermal}.copc.laz [point_cloud_multispectral.copc.laz]
  geovisor/: http://localhost:8080
```

**Tiempos estimados** (con GPU):

| Etapa | Tiempo |
|---|---|
| Preparación + metadatos | ~2 min |
| ODM RGB | 20-30 min |
| ODM Térmico nativo | ~15-20 min |
| ODM Multiespectral (si aplica) | ~10-15 min (114 capturas × 4 bandas) |
| Recorte + tiles + export | 10-15 min |
| **Total** | **~50-80 min** (+~15 min si hay multiespectral) |

---

## Estructura del proyecto

```
raptor/
├── data/                          ← Imágenes fuente (organizadas por el entrypoint desde /input)
├── preprocessing/thermal_dji_sdk/ ← TIFF °C (regenerables)
├── processing/                    ← Directorios de trabajo ODM
├── outputs/                       ← Productos finales
├── scripts/                       ← Pipeline (Python)
│   ├── progress.py, progress.sh   ← Barras de progreso
│   ├── prepare_multispectral_odm.py ← 2. Preparación multiespectral (geo.txt 4 bandas)
│   ├── convert_thermal_tiff.py    ← 2. R-JPEG → °C (DJI SDK)
│   ├── denoise_thermal_frames.py  ← 2. Filtro bilateral
│   ├── prepare_thermal_native_odm.py ← 2. Re-encode °C→Kelvin×100 + tags para ODM
│   ├── camera_ns.exiftool.config  ← config exiftool (namespace XMP Camera:BandName)
│   ├── dsm_clean.py               ← 3. Limpieza DSM (RGB)
│   ├── trim_low_overlap_edges.py  ← 4. Recorte bordes (RGB, térmico nativo, multiespectral)
│   ├── confidence_mask.py         ← 4. Máscara confianza
│   ├── compute_vegetation_indices.py ← 4. NDVI/GNDVI/NDRE/MSAVI2
│   ├── detect_area_afectada.py    ← 4. Área afectada (multiespectral+térmico)
│   ├── compute_severity_classes.py ← 4. Severidad + hotspot térmico
│   ├── compute_situation_summary.py ← 4. Resumen ejecutivo (situation.json): área, focos discretos, confianza
│   ├── export_flight_path.py      ← 0. Ruta de vuelo (GeoJSON), antes de ODM — geovisor muestra algo desde el minuto uno
│   ├── generate_tiles.py          ← 5. Tiles XYZ (incluye índices y bandas MS)
│   ├── export_cog.py              ← 6. Rasters finales → COG
│   ├── export_copc.py             ← 6. Nubes de puntos → COPC
│   └── debug/thermal_diag.py      ← Diagnóstico
├── geovisor/                      ← Dashboard Leaflet (selector de bandas, panel "Situación actual")
├── webapp/                        ← Webapp interactiva (FastAPI): main.py, static/index.html
├── core/                          ← Orquestador del pipeline (activate_mission, PipelineRun, escaneo de
│                                     misiones) — lo usa webapp/main.py
├── docker/                        ← entrypoint.sh (orquesta todo) + setup-data.sh + setup-data-multispectral.sh
├── dji_thermal_sdk/               ← DJI Thermal SDK (incluido)
├── docs/PIPELINE.md               ← Documentación técnica
├── Dockerfile
├── Makefile
└── README.md
```

---

## Uso avanzado

### Pasos individuales (dentro del contenedor)

Para debug manual, entrá a un shell del contenedor (monta los mismos
volúmenes que la corrida normal) y corré targets de `make` sueltos:

```bash
docker run --rm -it \
  -v $PWD/processing:/app/processing -v $PWD/outputs:/app/outputs \
  --entrypoint bash raptor

# dentro del contenedor:
make clean-dsm trim-edges-dsm trim-edges-rgb
make sdk-convert denoise-thermal prepare-thermal-native  # antes de invocar ODM térmico
make trim-edges-thermal confidence-mask
make prepare-multispectral trim-edges-multispectral compute-indices  # Solo si hay /input_ms
make detect-area-afectada compute-severity                 # Solo si hay MS + térmico
make tiles serve                                            # Tiles + visor
make export-cog export-copc                                 # Rasters → COG, nubes → COPC
make info                                                   # Estado
make clean-all                                              # Limpiar resultados
```

(El SfM/MVS/malla/textura/orto de ODM —para los tres sensores— no tiene
target de `make` — se invoca directamente como `python3 /code/run.py ...`,
ver `docker/entrypoint.sh`, función `run_odm()`.)

---

## Estructura esperada en `/input`

El pipeline está diseñado para **no versionar datos** — solo se monta la
carpeta de la misión como volumen:

```
<directorio_fuente>/
├── DJI_20240615100000_0001_V.JPG   ← RGB
├── DJI_20240615100003_0002_V.JPG
├── ...
└── THERMAL/                        ← o en la misma carpeta
    ├── DJI_20240615100000_0001_T.JPG   ← Térmico
    ├── DJI_20240615100003_0002_T.JPG
    └── ...
```

El entrypoint busca recursivamente `*_V.JPG`, `*_W.JPG` (RGB) y `*_T.JPG` (térmico)
dentro de `/input` (tarjeta SD, disco, o carpeta local montada con `-v`).

### Estructura esperada en `/input_ms` (multiespectral, opcional)

Vuelo DJI M3M, montado como volumen SEPARADO de `/input` (es otro dron/sensor
de la misma zona, no se mezcla):

```
<vuelo-m3m>/
├── DJI_20240615100000_0001_MS_G.TIF    ← Green
├── DJI_20240615100000_0001_MS_R.TIF    ← Red
├── DJI_20240615100000_0001_MS_RE.TIF   ← RedEdge
├── DJI_20240615100000_0001_MS_NIR.TIF  ← NIR
├── DJI_20240615100000_0001_D.JPG       ← RGB "display" (sensor aparte, no se usa acá)
└── ...
```

El entrypoint busca recursivamente `*_MS_G.TIF`, `*_MS_R.TIF`, `*_MS_RE.TIF`,
`*_MS_NIR.TIF` dentro de `/input_ms` — la banda `_D.JPG` se ignora (cámara RGB
físicamente aparte de las 4 lentes MS, fuera de alcance de este módulo).

### Solo el visor (si ya tienes tiles generados)

```bash
docker run --rm -p 8080:8080 -v $PWD/geovisor/tiles:/app/geovisor/tiles \
  raptor serve
```

---

## Notas

- **Sensor térmico**: El SDK de DJI calibra la corrección atmosférica hasta
  25m. A ~500m AGL esto es una limitación del hardware.
- **Tests**: `make test` (dentro del contenedor) corre `scripts/check_deps.py`
  y la suite de `tests/`. No hacen falta datos de vuelo: los cuatro `trim_*` se
  ejercitan sobre una misión sintética (`tests/synthetic.py`), y la webapp se
  prueba con `TestClient` sin levantar el servidor. Cubre los umbrales
  adaptativos y el recorte, la exportación (formatos, CRS y
  georreferenciación), el resumen JSON de CI, la validación previa del
  formulario y la validación de argumentos del lanzador.
  El test de caracterización compara el recorte contra
  `tests/golden/trim_masks.json` — si un cambio mueve un solo píxel, falla. Si
  el cambio es intencional, borrá ese archivo y regeneralo corriendo la suite
  dos veces.
- **Dependencias**: las que RAPTOR instala están fijadas en `requirements.txt`.
  GDAL, pyproj y scipy se heredan de `opendronemap/odm:gpu` (un tag móvil) y no
  se pinean con pip para no pelear con su SuperBuild; en su lugar
  `scripts/check_deps.py` verifica los rangos soportados y **falla el build** si
  la base se movió.
- **Sin GPU**: omitir `--gpus all` — ODM detecta la ausencia de `nvidia-smi`
  y usa CPU (más lento pero funcional).
- **Memoria**: ODM documenta un pico de ~1 GB por hilo cada 2 MP de imagen y por
  defecto usa **todos** los núcleos. En el vuelo multiespectral eso importa
  especialmente: el *band alignment* carga dos bandas completas (5 MP en el M3M,
  ~2.5 GB por hilo) simultáneamente por hilo, así que en una máquina de 20
  núcleos pediría ~50 GB. Cuando el kernel mata el proceso por falta de memoria
  no hay excepción que loguear: **el log simplemente termina en seco a mitad de
  una etapa**. El pipeline ahora acota los hilos a lo que la RAM disponible
  aguanta y, si aun así lo matan, lo dice explícitamente en vez de dejar un log
  truncado. Para forzarlo a mano: `-e MAX_CONCURRENCY=4`.
- **EPSG**: se toma de la proyección del propio raster de cada misión (la UTM
  que ODM eligió según su GPS) — no hay que ajustar nada para volar en otra
  zona. `UTM_EPSG` en `trim_low_overlap_edges.py` es solo el respaldo si un
  raster llegara sin proyección legible.
- **Reprocesar**: borrar `processing/` y `outputs/` (o `make clean-all`
  dentro del contenedor) y volver a correr `docker run ... raptor run`
  (o resubir la misión desde la webapp).
- **DSM y recorte de bordes**: `dsm_clean.py` descarta el relleno sintético
  de huecos que aplica ODM (una plataforma de altura constante, no terreno
  real) sin rellenarlo con ningún valor inventado; `trim_low_overlap_edges.py`
  aplica el mismo criterio geométrico de solape de cámaras a los tres
  productos (RGB, térmico, multiespectral) para que sus bordes recortados
  coincidan entre sí.
- **Térmico nativo (ODM)**: el ortomosaico térmico se genera con el
  renderizador de malla 3D de ODM (igual que RGB/multiespectral), no con un
  blending heurístico propio. `scripts/prepare_thermal_native_odm.py`
  re-encodea los TIFF Float32 °C (`convert_thermal_tiff.py`) a uint16
  Kelvin×100 y los etiqueta como `Make=DJI`/`Model=ZH20T`/XMP
  `Camera:BandName=LWIR` — el formato exacto que `opendm/thermal.py` de ODM
  reconoce para aplicar su propia calibración Kelvin→°C durante el render de
  textura. Contra una entrega de referencia de Agisoft, el render nativo da
  68.6% de cobertura frente al 16.9% de un blending heurístico propio, sin
  artefactos de fragmentación y a mayor resolución. Ver `docs/PIPELINE.md`.
- **TIFF térmico geolocalizado**: cada TIFF de temperatura en
  `preprocessing/thermal_dji_sdk/*.tif` trae embebido el GPS/gimbal EXIF de
  su R-JPEG fuente — lo usa `prepare_thermal_native_odm.py` para armar el
  `geo.txt` del proyecto ODM térmico, y además permite exportar/entregar
  estos TIFF Float32 °C a un software externo (Agisoft, Pix4D) que arma su
  propia alineación a partir del GPS de cada foto.
- **Productos cloud-optimized (COG + COPC)**: todos los rasters finales en
  `outputs/*.tif` (RGB, térmico, DSM, máscara de confianza, multiespectral,
  índices) se convierten a **COG** (Cloud Optimized GeoTIFF — overviews
  embebidos, se pueden leer por rangos HTTP sin bajar el archivo entero;
  QGIS/ArcGIS los abren igual que un GeoTIFF normal). Las nubes de puntos
  densas georreferenciadas de ODM (`odm_georeferencing/odm_georeferenced_model.laz`,
  de los tres proyectos: RGB, térmico nativo, multiespectral si aplica) se
  exportan además a **COPC** (`outputs/point_cloud_{rgb,thermal,multispectral}.copc.laz`)
  para streaming en Potree/QGIS/CloudCompare. Targets manuales:
  `make export-cog` / `make export-copc`.
- **Multiespectral**: la calibración `camera+sun` usa el sensor de sol
  embebido en cada banda del M3M — no hace falta panel de calibración física.
  Corrección PPK no soportada todavía (requiere archivo de estación base, no
  incluido); se usa el GPS RTK ya embebido en el EXIF, igual que RGB/térmico.
  La banda RGB "D" del M3M (sensor aparte, no coalineado con las 4 lentes MS)
  no se reconstruye automáticamente — se puede procesar aparte con el
  pipeline RGB existente apuntándolo a `*_D.JPG` (`RGB_PATH=outputs/multispectral_rgb_dband.tif`
  para no pisar el RGB principal) y sus 3 canales quedan disponibles como
  bandas adicionales en el selector de combinaciones del geovisor.

---

## Documentación técnica

Ver `docs/PIPELINE.md` para:
- Detalles de cada etapa
- Parámetros ajustables de ODM (RGB, térmico nativo, multiespectral)
- Criterios de recorte de bordes (estilo Agisoft/Pix4D)
- Diagnóstico de artefactos (arcos, peine, barridos)
