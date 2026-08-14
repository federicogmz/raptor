# RAPTOR — DJI M3T / H20T / M3M

**R**econstrucción **A**érea de **P**roductos **T**érmicos, **Ó**pticos (y
multiespectrales) para **R**espuesta — pipeline reproducible para generar
ortomosaicos RGB, térmicos e índices de vegetación (NDVI/GNDVI/NDRE/MSAVI2)
georreferenciados a partir de vuelos fotogramétricos DJI (Mavic 3T, Matrice
300 RTK + Zenmuse H20T, y opcionalmente Mavic 3 Multispectral), con geovisor
Leaflet interactivo.

**Todo el pipeline corre en UN SOLO contenedor Docker** (ODM + post-procesamiento
propio, sin contenedores anidados). El DJI Thermal SDK ya está incluido en el
repositorio. Solo necesitas las imágenes fuente del vuelo.

Los sensores (RGB+térmico del M3T/H20T, multiespectral del M3M, banda D del
M3M) son **independientes**: cada uno prepara sus imágenes, reconstruye y
publica sus productos por su cuenta, sin esperar a los demás salvo que la RAM
disponible obligue a turnarse (ver «Cuántos sensores reconstruyen a la vez»
más abajo).

---

## Instalación — webapp persistente (por defecto)

La forma de instalar RAPTOR en una máquina (propia o de otra persona) es
desplegar la **webapp interactiva** (FastAPI) una vez y dejarla corriendo:
subís las fotos crudas del vuelo desde el navegador, elegís qué sensores y
productos procesar, el tipo de terreno y el preset de calidad, ves el
progreso en vivo (SSE) y al terminar se muestra directo el geovisor con los
tiles — todo en un solo puerto, sin flags de `docker run` ni montar
volúmenes de antemano.

La webapp detecta qué tipo de archivo subiste (`*_V/_W`, `*_T`, `*_MS_*`,
`*_D`) y valida la combinación **antes** de lanzar el pipeline, así una
misión a la que le falten fotos falla en el formulario —corregible ahí
mismo, sin resubir lo ya cargado— y no horas después adentro de ODM. Si la
misión ya tiene reconstrucciones ODM guardadas, ofrece reusarlas en vez de
rehacer el SfM.

```bash
./raptor build
./raptor webapp --export ~/entregas
# abrir http://localhost:8080
```

`./raptor webapp` queda **en segundo plano** (`-d`) con `--restart
unless-stopped`: si el contenedor se cae o se reinicia la máquina, Docker lo
vuelve a levantar solo — no hay que dejar una terminal ni una sesión `tmux`
abierta, ni volver a correr el comando después de un reinicio (para eso
alcanza con que el propio servicio de Docker arranque solo:
`sudo systemctl enable --now docker` en una instalación estándar, root, no
rootless). Correrlo de nuevo con las mismas opciones reemplaza **solo** ese
contenedor (`raptor-web` por nombre), nunca toca otros contenedores que haya
en la misma máquina.

```bash
./raptor logs     # ver el progreso en vivo (Ctrl+C no la apaga, solo sale del log)
./raptor status   # ver si está corriendo
./raptor stop      # apagarla
```

Para debug puntual, sin dejarla instalada (corre pegada a la terminal, sin
reinicio automático, se borra sola al salir): `./raptor webapp --fg`.

`--export` es la carpeta **de tu disco** donde van a salir los productos: el
lanzador la monta en el contenedor, así que lo que elijas en el formulario es
una ruta real del host y no algo que desaparece al cerrar el contenedor. Sin
`--export` la webapp funciona igual, pero la exportación aparece deshabilitada.

Las fotos subidas y los resultados quedan en `runs/<misión>/`, que el
lanzador monta por vos; `--runs DIR` lo cambia de lugar.

---

## Automatización y CI

`./raptor run` procesa una misión sin interacción, con códigos de salida y un
resumen legible por máquina — apto para encadenar en un pipeline:

```bash
./raptor run \
  --input ./vuelos/la_clara \
  --input-ms ./vuelos/la_clara_ms \
  --preset estandar --terreno escarpado \
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

## Calidad, terreno y hardware

La calidad se pide por **preset**, elegido por uso y tiempo:

| Preset | Para qué | Techo de resolución |
|---|---|---|
| `vistazo` | Ver algo utilizable en minutos, durante la emergencia | 15 cm/px |
| `rapido` | Respuesta operativa el mismo día | 8 cm/px |
| `estandar` *(default)* | La entrega normal de una misión | 4 cm/px |
| `alta` | Análisis fino y medición sobre el modelo de superficie | 2 cm/px |
| `maxima` | Archivo y peritaje: el máximo detalle que dé el vuelo | 1 cm/px |

Cada preset fija varias cosas a la vez: el detalle del modelo de superficie
(la nube de puntos y la malla de las que salen el ortomosaico y el DSM), el
techo de resolución que se le pide a ODM, cuántos features se extraen por
foto, cuántas fotos vecinas se comparan al emparejar, qué algoritmo de SfM se
usa y el tipo de bundle adjustment. La tabla vive en un solo lugar,
`scripts/hardware.py::PRESETS`.

### Terreno: plano vs escarpado

`vistazo` y `rapido` reconstruyen por defecto con `sfm_algorithm: planar`
— alinea las fotos por homografías entre pares, válido solo si la escena es
efectivamente plana (vuelo nadir, altura fija). **En terreno con relieve
fuerte (montaña, cañón, cara rocosa) esa homografía deja de aproximar bien
la escena, y OpenSfM descarta en silencio las fotos que no puede encajar** —
confirmado en vivo: en una misión de terreno rocoso, `vistazo` con `planar`
solo incorporó el 19-24% de las fotos a la reconstrucción final, sin ningún
error visible (el pipeline terminaba "bien", con un ortomosaico recortado
muy por debajo del área real volada).

`--terreno escarpado` (o el selector correspondiente en la webapp) fuerza
`sfm_algorithm: incremental` incluso en `vistazo`/`rapido` — reconstruye foto
por foto vía bundle adjustment, sin asumir un plano, a costa de ser más
lento (secuencial por diseño, no paralelizable entre fotos). Las demás
palancas de velocidad del preset (resolución de features, mínimo de
features, `--fast-orthophoto` en RGB) se mantienen — la idea es "completo
pero seguir siendo lo más rápido posible", no perder toda la ganancia de
velocidad del preset.

```bash
./raptor run --input ./vuelos/la_clara --preset vistazo --terreno escarpado
```

Default de `./raptor run --terreno` (CLI): `plano` — no es obligatorio; con
terreno chato o de relieve suave, `planar` es válido y bastante más rápido.

El formulario de la **webapp** arranca en `escarpado` en cambio: la mayoría
de las misiones de emergencia real (incendio, montaña, terreno rocoso) no
son planas, y perder cobertura en silencio por no acordarse de tildar la
opción correcta es peor que la corrida ser más lenta por defecto. Se puede
cambiar a `plano` en el mismo formulario cuando el vuelo sí lo es.

### `--fast-orthophoto` en RGB (vistazo/rápido)

RGB es el único sensor cuyo DSM es la fuente del modelo de superficie de la
misión, así que en `estandar` y superior siempre pasa por
`DensifyPointCloud` (la nube densa/MVS) para un DSM de la mejor calidad
posible. En `vistazo`/`rapido`, en cambio, RGB salta esa etapa
(`--fast-orthophoto`, el mismo mecanismo que banda D ya usaba siempre): el
DSM sale de la nube dispersa de SfM en vez de la densa — más pobre, con más
huecos, pero la diferencia real es de horas. Confirmado en vivo: una
reconstrucción RGB de ~1200 fotos con GPU de laptop se llevó ~11h en
`DensifyPointCloud` — con `--fast-orthophoto` esa etapa no corre.

### Estimación antes de arrancar

Antes de arrancar, `./raptor run` (y el formulario de la webapp) muestran a
qué resolución van a salir los productos y cuánto se espera que tarde, según
tu hardware y el terreno elegido:

```bash
./raptor run --input ./vuelos/la_clara --preset alta
```
```
🖥️  Hardware detectado: 20 núcleos, 27 GB RAM libres — sin GPU (corre en CPU)
🎚️  Preset «Alta»: Análisis fino y medición sobre el modelo de superficie…
📐 Hasta 2 cm/px en el ortomosaico y el DSM — el techo real lo pone el GSD…
⏱️  Tiempo estimado con tu hardware: 1.4 h – 5.6 h para 1652 fotos. Aproximado…
```

Para ver los cinco presets con su tiempo estimado sin arrancar nada:

```bash
python3 scripts/hardware.py estimate --photos 1652 --terreno escarpado
```

`--quality N` (0-100) se sigue aceptando y se mapea al preset equivalente,
para no romper corridas y scripts que ya lo usan (siempre en terreno plano —
el número heredado no distingue terreno).

Dos cosas que conviene tener claras sobre la estimación:

- **La estimación es aproximada a propósito.** El modelo es lineal en el
  número de fotos y la escena real (solape, vegetación, terreno) no lo es —
  se muestra siempre como rango, no como un número que promete una precisión
  que no existe.
- **La resolución de salida no la decide la calidad, la decide el vuelo.**
  El GSD (metros por píxel en el suelo) lo fija la altura de vuelo y el
  sensor; ODM lo mide de la reconstrucción y **nunca puede ir más fino** que
  eso, sin importar qué tan alta sea la calidad pedida — pedir 1 cm a un
  vuelo cuyo GSD real es 9 cm no agrega detalle, solo produce píxeles más
  chicos. Lo que la calidad SÍ decide es si se puede pedir un techo más
  **grueso** a propósito (para terminar antes), y si el modelo 3D resuelve o
  no objetos como copas de árboles y bordes de tejados.

### Cuántos sensores reconstruyen a la vez

Los proyectos ODM (`rgb_odm`, `thermal_native_odm`, `multispectral_odm`,
`dband_odm`) son independientes entre sí — distinta carpeta fuente, distinta
carpeta destino, ningún archivo compartido. Cada uno **prepara sus imágenes
de forma independiente** (no espera a que los demás terminen de preparar) y
recién compite por un cupo de reconstrucción cuando SU preparación termina,
vía un semáforo memory-aware (`odm_slots()` en `scripts/hardware.py`): en
una máquina con RAM de sobra, dos o más reconstrucciones corren en
simultáneo; en una más chica, se turnan — el comportamiento de siempre
(uno por vez) queda como piso, nunca como techo. RGB en particular, con su
perfil de foto más pesado, suele necesitar la máquina para sí solo salvo que
use `--fast-orthophoto` (vistazo/rápido), que reduce su necesidad real de
memoria y permite compartir cupo con otro sensor.

`RAPTOR_ODM_PARALELO=1` fuerza el modo secuencial de siempre (uno por vez);
`MAX_CONCURRENCY N` fija a mano los hilos por proyecto (perilla de "cuidame
la memoria", no cuántos proyectos corren juntos).

---

## Uso manual con `docker run` (para scripts / control fino)

Si preferís invocar Docker vos mismo (automatización, CI, una sola misión
puntual) en vez de la webapp, seguí esta guía:

### Guía rápida: de la tarjeta SD al geovisor

### Requisitos

- Docker
- GPU NVIDIA + `nvidia-container-toolkit` (recomendado; ver «GPU» abajo para
  cuándo es realmente indispensable)

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

### GPU

El binario de reconstrucción densa de la imagen base (`DensifyPointCloud`)
está enlazado contra `libcuda.so.1`, y sin el runtime de NVIDIA montado no
carga —

```
DensifyPointCloud: error while loading shared libraries: libcuda.so.1
opendm.system.SubprocessException: Child returned 127
```

— o sea que la corrida muere en la etapa `openmvs`, **después** de haber
pagado la preparación y todo el SfM, si `--gpus all` faltaba y encima esa
misión SÍ iba a tocar la etapa densa. Que ODM "detecta `nvidia-smi` y cae a
CPU" es cierto para elegir el *algoritmo*, pero no evita ese enlace dinámico.

El entrypoint lo comprueba en el primer segundo, **solo para los sensores
que de verdad van a correr `DensifyPointCloud`** — RGB en `vistazo`/`rapido`
(que usan `--fast-orthophoto`) y banda D (que siempre lo usa) quedan afuera
del chequeo, igual que cualquier corrida con `SKIP_ODM=1`. Si algún sensor
activo sí la va a tocar (térmico, multiespectral, o RGB fuera de
`vistazo`/`rapido`) y falta el runtime, aborta con instrucciones en vez de
reventar horas después. `./raptor run` pasa `--gpus all` por su cuenta salvo
que se le dé `--no-gpu`.

**Recomendación práctica: pasá `--gpus all` siempre que la máquina lo
permita** — es la única forma de que la etapa densa (cuando corre) sea
rápida; el chequeo automático es una red de seguridad para cuando no hace
falta, no una razón para omitirlo por costumbre.

El mismatch de versión CUDA (driver algo más viejo que lo que pide la imagen
de ODM) ya viene resuelto por defecto (`NVIDIA_DISABLE_REQUIRE=1` horneado
en la imagen) — no hace falta pasarlo a mano.

> **Montá los volúmenes en este modo.** La imagen **no declara `VOLUME`**
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
> [webapp persistente](#instalación--webapp-persistente-por-defecto) resuelve exactamente este
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
de sol embebido en cada banda, sin necesitar panel de calibración física).

**Banda D del M3M (mosaico visible rápido, opcional):** el M3M trae además
una cámara RGB propia (sensor aparte de las 4 lentes multiespectrales,
archivos `*_D.JPG`). Es su propio proyecto ODM independiente
(`dband_odm`), siempre con `--fast-orthophoto` (sin esto, banda D corría la
etapa más cara de toda la misión — `DensifyPointCloud` — sin necesitarlo:
nunca pide `--dsm`, ya lo tiene RGB o multiespectral). Opt-in explícito, no
automático solo porque haya archivos `*_D.JPG` — es procesamiento extra que
no todas las misiones quieren pagar:

```bash
docker run --gpus all \
  -v /ruta/al/vuelo-m3m:/input_ms \
  -e DBAND=1 \
  -v $PWD/outputs:/app/outputs \
  -p 8080:8080 \
  raptor run
```

En la webapp es un checkbox junto al bloque multiespectral. Sus productos
(`outputs/dband_orthomosaic.tif`, `outputs/point_cloud_dband.copc.laz`) y su
capa en el geovisor son independientes de las 4 bandas espectrales.

Variables de entorno útiles (`-e VAR=valor`):

| Variable | Default | Uso |
|---|---|---|
| `MODE` | `rgb+thermal` | `rgb` para saltar todo el térmico; `none` si la misión NO tiene vuelo RGB/térmico (solo multiespectral — requiere `/input_ms`) |
| `SKIP_ODM` | `0` | `1` para reusar `processing/*_odm/` de una corrida previa |
| `MS_SOURCE_DIR` | `/input_ms` | Carpeta fuente del vuelo multiespectral (el módulo corre SOLO si existe) |
| `DBAND` | `0` | `1` para además reconstruir la banda D del M3M (mosaico visible, opt-in — requiere `MS_SOURCE_DIR` con archivos `*_D.JPG`) |
| `PORT` | `8080` | Puerto del geovisor |
| `SERVE` | `1` | `0` para no levantar el geovisor al terminar |
| `PRESET` | `estandar` | `vistazo` \| `rapido` \| `estandar` \| `alta` \| `maxima`. Detalle del modelo de superficie, techo de resolución, features por foto y algoritmo de SfM. Ver «Calidad» arriba |
| `TERRENO` | `plano` | `plano` \| `escarpado`. `escarpado` fuerza reconstrucción incremental incluso en `vistazo`/`rapido` — ver «Terreno» arriba |
| `QUALITY` | — | *(heredado)* 0-100; se mapea al preset equivalente |
| `RAPTOR_ODM_PARALELO` | automático | `1` fuerza una reconstrucción ODM por vez; por defecto se calcula según la RAM disponible (`odm_slots()` en `scripts/hardware.py`) |
| `MAX_CONCURRENCY` | automático | Hilos de ODM por proyecto. Por defecto se calcula según la RAM disponible; bajalo si el proceso muere sin mensaje (ver «Memoria» en Notas) |
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
(incluye banda D si corrió) (o `all`). Los que la misión no generó se omiten
sin fallar.

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
1. **Organiza** las imágenes desde `/input` (busca `*_V.JPG`, `*_W.JPG`, `*_T.JPG` recursivamente) y, en paralelo, si `/input_ms` existe, las bandas MS y banda D desde ahí (`*_MS_G/R/RE/NIR.TIF`, `*_D.JPG`)
2. **Extrae metadatos** GPS/EXIF; **convierte** R-JPEG → °C (DJI SDK) + denoise + re-encoding para ODM. Cada sensor prepara sus imágenes de forma independiente
3. **Reconstruye cada sensor** apenas SU preparación termina, sin esperar a los demás — RGB, térmico nativo, multiespectral y banda D compiten por un cupo de reconstrucción memory-aware (ver «Cuántos sensores reconstruyen a la vez»)
4. **Recorta bordes** de bajo solape apenas SU sensor termina de reconstruir (mismo criterio para los cuatro productos, con máscara de confianza según solape de cámaras RGB∩térmico), limpia el DSM (descarta relleno sintético), y **exporta ese sensor a COG/COPC** — no espera a los demás
5. En paralelo entre sí: **calcula NDVI/GNDVI/NDRE/MSAVI2** (si hay multiespectral), **clasifica área afectada, severidad y hotspot térmico** (si hay multiespectral + térmico), y **calcula la calidad del levantamiento** (solape de cámaras, velocidad de vuelo, % reconstruido)
6. **Genera tiles** (incrementalmente, apenas cada producto está listo — no solo al final) + un pase final de exportación COG/COPC que saltea lo que cada sensor ya exportó
7. **Despliega** el geovisor en `http://localhost:8080`

### Paso 3 — Abrir el geovisor

**http://localhost:8080**

Incluye:
- Capas RGB, térmica y banda D con opacidad ajustable
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
- Funciona sin conexión a internet una vez cargado: Leaflet está vendorizado
  (`geovisor/vendor/`), no depende de un CDN

---

## Pipeline

```
data/rgb_mosaico/ + data/termica_mosaico/  [+ data/multiespectral_mosaico/ + data/dband_mosaico/]
        │  (organización en paralelo)
        ▼
  1. Metadatos (GPS, EXIF) + ruta de vuelo (geovisor muestra algo desde el minuto uno)
  2. Preparación POR SENSOR, cada uno independiente: geo.txt/imágenes RGB+MS+D;
     térmico → DJI SDK °C + denoise + re-encode Kelvin×100
  3. Reconstrucción POR SENSOR, cada uno arranca apenas SU preparación termina,
     compitiendo por un cupo memory-aware (odm_slots()):
       ODM RGB (SfM+[MVS]+DSM+orto — MVS salvo vistazo/rápido, ahí --fast-orthophoto)
       ODM Térmico nativo (SfM+MVS+malla+textura+orto en °C)
       ODM Multiespectral (SfM+calibración+orto multibanda, si aplica)
       ODM Banda D (SfM+orto rápido, --fast-orthophoto siempre, si aplica)
  4. Por sensor, apenas termina SU reconstrucción: limpieza DSM (solo RGB/MS)
     + recorte de bordes de bajo solape + export COG/COPC de ESE sensor
  5. En paralelo entre sí, sobre lo ya recortado: máscara de confianza
     (RGB∩térmico) + calidad del levantamiento + índices de vegetación +
     área afectada/severidad/hotspot térmico (si hay MS+térmico)
  6. Tiles XYZ (incrementales) + geovisor (con selector de combinación de bandas)
  7. Pase final de exportación cloud-optimized: lo que falte → COG/COPC
        │
        ▼
  outputs/: dsm.tif, rgb_orthomosaic.tif, thermal_orthomosaic.tif (todos COG)
            [multispectral_orthomosaic.tif, dband_orthomosaic.tif,
             indices/{ndvi,gndvi,ndre,msavi2}.tif (COG),
             area_afectada.geojson, severidad_class.tif, termico_hotspot_class.tif]
            point_cloud_{rgb,thermal,multispectral,dband}.copc.laz
  geovisor/: http://localhost:8080
```

**Tiempos**: dependen demasiado del preset, el terreno y el hardware para una
tabla fija — usá `python3 scripts/hardware.py estimate --photos N [--preset
P] [--terreno T]` para la estimación real de tu caso. Dos referencias medidas
en vivo (laptop de 20 núcleos, RTX A1000 6GB, ~1200 fotos por sensor):

| Caso | RGB | Térmico |
|---|---|---|
| `vistazo`, terreno plano (`planar`, con `--fast-orthophoto`) | minutos | minutos |
| `vistazo`, terreno escarpado (`incremental` forzado) | ~10h (cobertura 99.2%, vs 23.6% con `planar` en el mismo terreno) | ~6h (cobertura 98.7%, vs 18.7% con `planar`) |

La reconstrucción incremental es secuencial por diseño (una foto a la vez,
no paralelizable entre fotos) — es el costo real de no perder cobertura en
terreno con relieve fuerte, no una config subóptima.

---

## Estructura del proyecto

```
raptor/
├── data/                          ← Imágenes fuente (organizadas por el entrypoint desde /input)
├── preprocessing/thermal_dji_sdk/ ← TIFF °C (regenerables)
├── processing/                    ← Directorios de trabajo ODM (rgb_odm, thermal_native_odm,
│                                     multispectral_odm, dband_odm)
├── outputs/                       ← Productos finales
├── scripts/                       ← Pipeline (Python)
│   ├── hardware.py                ← Presets, terreno, detección de hardware, odm_slots()
│   ├── progress.py, progress.sh   ← Barras de progreso
│   ├── odm_staging.py             ← Compartido: poblar images/ + geo.txt vía hardlink (MS y banda D)
│   ├── prepare_multispectral_odm.py ← 2. Preparación multiespectral (geo.txt 4 bandas)
│   ├── prepare_dband_odm.py       ← 2. Preparación banda D (cámara RGB del M3M)
│   ├── convert_thermal_tiff.py    ← 2. R-JPEG → °C (DJI SDK)
│   ├── denoise_thermal_frames.py  ← 2. Filtro bilateral
│   ├── prepare_thermal_native_odm.py ← 2. Re-encode °C→Kelvin×100 + tags para ODM
│   ├── camera_ns.exiftool.config  ← config exiftool (namespace XMP Camera:BandName)
│   ├── dsm_clean.py               ← 4. Limpieza DSM (RGB o multiespectral si no hay RGB)
│   ├── trim_low_overlap_edges.py  ← 4. Recorte bordes (los cuatro sensores)
│   ├── confidence_mask.py         ← 5. Máscara confianza (RGB∩térmico)
│   ├── compute_flight_quality.py  ← 5. Calidad del levantamiento (solape, velocidad, % reconstruido)
│   ├── compute_vegetation_indices.py ← 5. NDVI/GNDVI/NDRE/MSAVI2
│   ├── detect_area_afectada.py    ← 5. Área afectada (multiespectral+térmico)
│   ├── compute_severity_classes.py ← 5. Severidad + hotspot térmico (recortado al área)
│   ├── compute_thermal_hotspot.py ← 5. Hotspot térmico standalone (térmico sin multiespectral)
│   ├── compute_situation_summary.py ← 5. Resumen ejecutivo (situation.json)
│   ├── export_flight_path.py      ← 1. Ruta de vuelo (GeoJSON), antes de ODM
│   ├── generate_tiles.py          ← 6. Tiles XYZ (incluye índices, bandas MS y banda D)
│   ├── export_cog.py              ← 4/7. Rasters finales → COG (por sensor, o pase de seguridad)
│   ├── export_copc.py             ← 4/7. Nubes de puntos → COPC (por sensor, o pase de seguridad)
│   ├── export_products.py         ← 8. Entrega a EXPORT_DIR
│   ├── print_timings.py           ← Resumen de tiempos por etapa (outputs/logs/timings.json)
│   ├── fast_median.py             ← Filtro de mediana threaded (usado por dsm_clean.py)
│   └── debug/thermal_diag.py      ← Diagnóstico
├── geovisor/                      ← Dashboard Leaflet (selector de bandas, panel "Situación actual",
│                                     Leaflet vendorizado — funciona sin internet)
├── webapp/                        ← Webapp interactiva (FastAPI): main.py, static/index.html
├── core/                          ← Orquestador del pipeline (activate_mission, PipelineRun,
│                                     escaneo de misiones) — lo usa webapp/main.py
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
make trim-edges-thermal confidence-mask flight-quality
make prepare-multispectral trim-edges-multispectral compute-indices  # Solo si hay /input_ms
make prepare-dband trim-edges-dband                         # Solo si DBAND=1
make detect-area-afectada compute-severity                 # Solo si hay MS + térmico
make compute-thermal-hotspot situation-summary              # Térmico sin multiespectral
make tiles serve                                            # Tiles + visor
make export-cog export-copc                                 # Rasters → COG, nubes → COPC
make info                                                   # Estado
make clean-all                                              # Limpiar resultados
```

(El SfM/MVS/malla/textura/orto de ODM —para los cuatro sensores— no tiene
target de `make` — se invoca directamente como `python3 /code/run.py ...`,
ver `docker/entrypoint.sh`, función `run_odm()` y `_odm_args()`.)

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
├── DJI_20240615100000_0001_D.JPG       ← RGB "display" (banda D, opcional — ver DBAND=1)
└── ...
```

El entrypoint busca recursivamente `*_MS_G.TIF`, `*_MS_R.TIF`, `*_MS_RE.TIF`,
`*_MS_NIR.TIF` dentro de `/input_ms` para las 4 bandas espectrales, y
`*_D.JPG` para la banda D (opt-in, ver `DBAND=1` arriba).

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
  y la suite de `tests/`. No hacen falta datos de vuelo: los `trim_*` se
  ejercitan sobre una misión sintética (`tests/synthetic.py`), y la webapp se
  prueba con `TestClient` sin levantar el servidor. Muchos tests extraen
  bloques REALES de `docker/entrypoint.sh`/`Makefile` y los corren con stubs,
  así que no pueden quedar desincronizados de la fuente. Cubre los umbrales
  adaptativos y el recorte, el despacho asíncrono por sensor y su semáforo de
  memoria, los presets y `terreno`, la exportación (formatos, CRS y
  georreferenciación, idempotencia de COG/COPC), el resumen JSON de CI, la
  validación previa del formulario y la validación de argumentos del
  lanzador.
  El test de caracterización compara el recorte contra
  `tests/golden/trim_masks.json` — si un cambio mueve un solo píxel, falla. Si
  el cambio es intencional, borrá ese archivo y regeneralo corriendo la suite
  dos veces.
- **Dependencias**: las que RAPTOR instala están fijadas en `requirements.txt`.
  GDAL, pyproj y scipy se heredan de `opendronemap/odm:gpu` (un tag móvil) y no
  se pinean con pip para no pelear con su SuperBuild; en su lugar
  `scripts/check_deps.py` verifica los rangos soportados y **falla el build** si
  la base se movió.
- **Memoria**: ODM documenta un pico de ~1 GB por hilo cada 2 MP de imagen y por
  defecto usa **todos** los núcleos. El pipeline acota los hilos de CADA sensor
  a lo que la RAM disponible aguanta (`safe_concurrency()`) y ADEMÁS decide
  cuántos sensores pueden reconstruir a la vez sin sumar más hilos de los que
  la RAM banca (`odm_slots()`, ver «Cuántos sensores reconstruyen a la vez»
  arriba) — antes esto solo protegía al band alignment del multiespectral;
  ahora protege cualquier combinación de sensores corriendo en simultáneo.
  Cuando el kernel mata el proceso por falta de memoria no hay excepción que
  loguear: el log simplemente termina en seco a mitad de una etapa. Para
  forzarlo a mano: `-e MAX_CONCURRENCY=4` (hilos por proyecto) o
  `-e RAPTOR_ODM_PARALELO=1` (uno por vez).
- **Tiempos de cada corrida**: `outputs/logs/timings.json` (y el resumen que
  imprime el entrypoint al final) desglosan preparación, cada reconstrucción
  ODM y post-procesamiento — `scripts/print_timings.py`. Útil para calibrar
  cuánto tarda tu propia máquina con tus propias misiones.
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
  aplica el mismo criterio geométrico de solape de cámaras a los cuatro
  productos (RGB, térmico, multiespectral, banda D) para que sus bordes
  recortados coincidan entre sí.
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
  banda D, índices) se convierten a **COG** (Cloud Optimized GeoTIFF —
  overviews embebidos, se pueden leer por rangos HTTP sin bajar el archivo
  entero; QGIS/ArcGIS los abren igual que un GeoTIFF normal), apenas termina
  el post-procesamiento de CADA sensor — no al final de toda la misión. Las
  nubes de puntos densas georreferenciadas de ODM
  (`odm_georeferencing/odm_georeferenced_model.laz`, de los cuatro
  proyectos) se exportan además a **COPC**
  (`outputs/point_cloud_{rgb,thermal,multispectral,dband}.copc.laz`) para
  streaming en Potree/QGIS/CloudCompare, con el mismo criterio incremental.
  Un pase final sin argumentos saltea lo que ya esté al día (COG: chequea el
  layout real; COPC: compara mtimes) y solo convierte lo que falte. Targets
  manuales: `make export-cog` / `make export-copc`.
- **Multiespectral**: la calibración `camera+sun` usa el sensor de sol
  embebido en cada banda del M3M — no hace falta panel de calibración física.
  Corrección PPK no soportada todavía (requiere archivo de estación base, no
  incluido); se usa el GPS RTK ya embebido en el EXIF, igual que RGB/térmico.

---

## Documentación técnica

Ver `docs/PIPELINE.md` para:
- Detalles de cada etapa
- Parámetros ajustables de ODM (RGB, térmico nativo, multiespectral, banda D)
- El despacho asíncrono por sensor y el reparto de memoria entre proyectos
- Criterios de recorte de bordes (estilo Agisoft/Pix4D)
- Diagnóstico de artefactos (arcos, peine, barridos)
