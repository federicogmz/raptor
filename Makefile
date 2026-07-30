# ═══════════════════════════════════════════════════════════════════
# Pipeline RAPTOR — RGB + Térmico + Multiespectral — DJI M3T / H20T / M3M
#
# PRODUCTOS FINALES → outputs/: dsm.tif, rgb_orthomosaic.tif,
#   thermal_orthomosaic.tif, confidence_mask.tif
#
# Uso en producción: un solo contenedor (ver Dockerfile + docker/entrypoint.sh)
#   docker run --gpus all -v /mision:/input -v $PWD/outputs:/app/outputs \
#     -v $PWD/geovisor/tiles:/app/geovisor/tiles -p 8080:8080 raptor
#
# El entrypoint invoca ODM directamente (python3 /code/run.py) y luego
# corre los targets de abajo uno por uno. Para debug manual dentro del
# contenedor (docker run ... raptor shell), usar `make <target>`.
# ═══════════════════════════════════════════════════════════════════

.PHONY: prepare-rgb prepare-multispectral prepare-thermal-native \
        sdk-convert denoise-thermal clean-dsm \
        trim-edges trim-edges-rgb trim-edges-dsm trim-edges-thermal trim-edges-multispectral \
        compute-indices confidence-mask detect-area-afectada compute-severity \
        situation-summary flight-path tiles export-cog export-copc export-products serve info test \
        clean clean-all

# ── Modo ───────────────────────────────────────────────────────────
MODE ?= rgb+thermal

# ── Directorios ────────────────────────────────────────────────────
OUTPUTS      := outputs
PROCESSING   := processing
RGB_PROC     := $(PROCESSING)/rgb_odm
MS_PROC      := $(PROCESSING)/multispectral_odm
THNAT_PROC   := $(PROCESSING)/thermal_native_odm
DATA_THERMAL := data/termica_mosaico
DATA_RGB     := data/rgb_mosaico
DATA_MS      := data/multiespectral_mosaico
TIFF_DIR     := preprocessing/thermal_dji_sdk
TIFF_DENOISED_DIR := preprocessing/thermal_dji_sdk_denoised
TILES_DIR    := geovisor/tiles
SERVER_PORT  ?= 8080

# ── Productos finales ──────────────────────────────────────────────
DSM_TIF      := $(OUTPUTS)/dsm.tif
RGB_TIF      := $(OUTPUTS)/rgb_orthomosaic.tif
THERMAL_TIF  := $(OUTPUTS)/thermal_orthomosaic.tif

export ODM_RGB_DIR   := $(RGB_PROC)
export ODM_MS_DIR    := $(MS_PROC)
export ODM_THNAT_DIR := $(THNAT_PROC)
export DSM_PATH := $(DSM_TIF)

# ── Verbosidad ─────────────────────────────────────────────────────
# Por defecto cada etapa muestra SOLO su barra de progreso (progress.py);
# la salida real del script (diagnósticos, stats detalladas) queda en
# $(LOG_DIR)/<etapa>.log. Si el script falla, se vuelca el log completo
# antes de propagar el error — nunca se pierde un traceback.
# VERBOSE=1 (make VERBOSE=1 ... / -e VERBOSE=1 en docker run) muestra todo
# crudo en pantalla, como antes.
VERBOSE ?= 0
LOG_DIR := $(OUTPUTS)/logs

define run_quiet
	@mkdir -p $(LOG_DIR)
	@if [ "$(VERBOSE)" = "1" ]; then \
		$(1) ; \
	else \
		if ! ( $(1) ) >$(LOG_DIR)/$(2).log 2>&1 ; then \
			echo "❌ Falló: $(2) — log completo ($(LOG_DIR)/$(2).log):" ; \
			cat $(LOG_DIR)/$(2).log ; \
			exit 1 ; \
		fi ; \
	fi
endef

# ── 1. Preparación de imágenes (GPS lo lee ODM directo del EXIF — sin
# geo.txt: nunca se demostró necesario, ver paper/COMPARATIVA_ODM_VANILLA_VS_RAPTOR.md,
# y una corrida real mostró que puede degradar la escala de la reconstrucción) ──
# El -all= previo borraba tambien los tags XMP-drone-dji:RtkStd{Lon,Lat,Hgt} -
# ODM los auto-detecta desde el EXIF (opendm/photo.py) y ajusta el peso GPS del
# bundle adjustment segun la precision RTK real (~1-3cm) en vez de caer a un DOP
# generico (~metros), PERO solo si sobreviven en el archivo. Se agrega
# -xmp-drone-dji:all para preservarlos. -api Compact=Shorthand es necesario: sin
# esto, exiftool reserializa esos tags como elementos XML anidados en vez de
# atributos (formato original de DJI), y el parser XMP propio de ODM (busca
# '@drone-dji:RtkStdLon' con prefijo @ = atributo) no los reconoce - se
# verifico que sin este flag gps_xy_stddev quedaba en None pese a copiar
# los tags "correctamente" segun exiftool.
#
# Se restaura -exif:all y NO solo -gps:all: -gps:all devuelve la posicion pero
# NO Make/Model/FocalLength, asi que ODM ve una camara anonima y usa su focal
# por defecto (focal_ratio 0.85) en vez de la real del M3T (0.667) - 27% de
# error inicial. Con esa semilla el bundle adjustment diverge a focal_x=75 y la
# reconstruccion COLAPSA de escala: un vuelo de 500x416 m produce una ortofoto
# de 34x23 m, y el recorte por solape
# —haciendo lo correcto sobre una geometria imposible— dejo el mosaico en 2% de
# cobertura. -exif:all es superconjunto de -gps:all (el GPS vive en el EXIF), asi
# que preserva la posicion igual y ademas la identidad de la camara. Verificado
# con opendm.photo.ODM_Photo sobre las 3 variantes: make/model/focal_ratio
# vuelven a los valores de la foto cruda y gps_xy_stddev sigue en 0.0235 m.
prepare-rgb:
	@python3 scripts/progress.py stage-header "Preparación imágenes RGB" 1 1
	@mkdir -p $(RGB_PROC)/images
	@rm -f $(RGB_PROC)/images/*_V.JPG $(RGB_PROC)/images/*_W.JPG
	@cp $(DATA_RGB)/*_V.JPG $(RGB_PROC)/images/ 2>/dev/null || true
	@cp $(DATA_RGB)/*_W.JPG $(RGB_PROC)/images/ 2>/dev/null || true
	@exiftool -api Compact=Shorthand -all= -tagsfromfile @ -exif:all -xmp-drone-dji:all -overwrite_original -q $(RGB_PROC)/images/*_V.JPG $(RGB_PROC)/images/*_W.JPG 2>/dev/null || true
	@python3 scripts/progress.py done "Imágenes RGB listas"

prepare-multispectral:
	@python3 scripts/progress.py stage-header "Preparación bandas multiespectrales" 1 1
	$(call run_quiet,python3 scripts/prepare_multispectral_odm.py,prepare-multispectral)
	@python3 scripts/progress.py done "Bandas multiespectrales listas"

# ── 3. ODM ─────────────────────────────────────────────────────────
# El SfM/MVS de ODM (RGB y térmico) lo invoca directamente el entrypoint
# del contenedor (python3 /code/run.py ...) — no hay target de Make para
# eso: no se lanza como un `docker run` anidado. Ver
# docker/entrypoint.sh, función run_odm() (usa scripts/odm_progress_filter.py
# para mostrar el mismo estilo de barra de progreso sobre la salida nativa
# de ODM, que es MUY verbosa).

clean-dsm:
	@python3 scripts/progress.py stage-header "Limpieza del DSM" 1 1
	@mkdir -p $(OUTPUTS)
	$(call run_quiet,python3 scripts/dsm_clean.py,clean-dsm)
	@python3 scripts/progress.py done "DSM limpio"

# ── 4. Conversión radiométrica + preparación térmica nativa ─────────
sdk-convert:
	@python3 scripts/progress.py stage-header "Conversión R-JPEG → °C (DJI SDK)" 1 1
	@if [ ! -f "dji_thermal_sdk/utility/bin/linux/release_x64/dji_irp" ]; then \
		echo "❌ DJI Thermal SDK no encontrado."; exit 1; fi
	@mkdir -p $(TIFF_DIR)
	$(call run_quiet,python3 scripts/convert_thermal_tiff.py $(DATA_THERMAL) $(TIFF_DIR),sdk-convert)
	@python3 scripts/progress.py done "Conversión radiométrica completada"

denoise-thermal:
	@python3 scripts/progress.py stage-header "Filtro bilateral (denoise)" 1 1
	$(call run_quiet,python3 scripts/denoise_thermal_frames.py,denoise-thermal)
	@python3 scripts/progress.py done "Denoise completado"

prepare-thermal-native:
	@python3 scripts/progress.py stage-header "Preparación térmica nativa (ODM)" 1 1
	$(call run_quiet,python3 scripts/prepare_thermal_native_odm.py,prepare-thermal-native)
	@python3 scripts/progress.py done "Imágenes térmicas listas para ODM"

trim-edges-rgb:
	@python3 scripts/progress.py stage-header "Recorte de bordes RGB" 1 1
	$(call run_quiet,python3 -c "from scripts.trim_low_overlap_edges import trim_rgb; trim_rgb()",trim-edges-rgb)
	@python3 scripts/progress.py done "Bordes RGB recortados"

# Recorta el DSM con el mismo piso de solape de cámaras que RGB/térmico/
# multiespectral (ver trim_dsm() en trim_low_overlap_edges.py). Requiere
# outputs/dsm.tif ya existente (correr clean-dsm antes) — sin prerequisito
# declarado a propósito, mismo criterio que trim-edges-rgb: el entrypoint
# ya secuencia clean-dsm → trim-edges-dsm explícitamente, y ambos son
# .PHONY (sin tracking de timestamps) — declarar el prerequisito aquí
# haría que `make trim-edges-dsm` SIEMPRE re-corra clean-dsm también,
# duplicando esa etapa cada vez que el entrypoint llama a las dos por
# separado.
trim-edges-dsm:
	@python3 scripts/progress.py stage-header "Recorte de bordes del DSM" 1 1
	$(call run_quiet,python3 -c "from scripts.trim_low_overlap_edges import trim_dsm; trim_dsm()",trim-edges-dsm)
	@python3 scripts/progress.py done "Bordes del DSM recortados"

trim-edges-thermal:
	@python3 scripts/progress.py stage-header "Recorte de bordes térmicos" 1 1
	$(call run_quiet,python3 -c "from scripts.trim_low_overlap_edges import trim_thermal_native; trim_thermal_native()",trim-edges-thermal)
	@python3 scripts/progress.py done "Bordes térmicos recortados"

trim-edges-multispectral:
	@python3 scripts/progress.py stage-header "Recorte de bordes multiespectrales" 1 1
	$(call run_quiet,python3 -c "from scripts.trim_low_overlap_edges import trim_multispectral; trim_multispectral()",trim-edges-multispectral)
	@python3 scripts/progress.py done "Bordes multiespectrales recortados"

# El multiespectral va incluido: quedaba afuera y `make trim-edges` recortaba
# tres de los cuatro productos sin decir nada. El target es idempotente y
# trim_multispectral() sale solo si no hay ortomosaico MS, así que incluirlo no
# afecta a las misiones sin vuelo M3M.
trim-edges: trim-edges-rgb trim-edges-dsm trim-edges-thermal trim-edges-multispectral

compute-indices:
	@python3 scripts/progress.py stage-header "Índices de vegetación (NDVI/GNDVI/NDRE)" 1 1
	@mkdir -p $(OUTPUTS)/indices
	$(call run_quiet,python3 scripts/compute_vegetation_indices.py,compute-indices)
	@python3 scripts/progress.py done "Índices de vegetación generados"

confidence-mask:
	@python3 scripts/progress.py stage-header "Máscara de confianza" 1 1
	$(call run_quiet,python3 scripts/confidence_mask.py,confidence-mask)
	@python3 scripts/progress.py done "Máscara generada"

# ── 5b. Área afectada + severidad (solo si hay multiespectral+térmico) ──
detect-area-afectada:
	@python3 scripts/progress.py stage-header "Detección de área afectada" 1 1
	$(call run_quiet,python3 scripts/detect_area_afectada.py,detect-area-afectada)
	@python3 scripts/progress.py done "Área afectada detectada"

compute-severity:
	@python3 scripts/progress.py stage-header "Clasificación de severidad" 1 1
	$(call run_quiet,python3 scripts/compute_severity_classes.py,compute-severity)
	@python3 scripts/progress.py done "Severidad clasificada"

situation-summary:
	@python3 scripts/progress.py stage-header "Resumen de situación" 1 1
	$(call run_quiet,python3 scripts/compute_situation_summary.py,situation-summary)
	@python3 scripts/progress.py done "Resumen de situación listo"

# ── 6. Tiles + visor ─────────────────────────────────────────────────
# Corre TEMPRANO (antes de ODM): da el recorrido del vuelo al geovisor para
# que haya algo que mirar durante la reconstrucción.
flight-path:
	@python3 scripts/progress.py stage-header "Ruta de vuelo" 1 1
	$(call run_quiet,python3 scripts/export_flight_path.py,flight-path)
	@python3 scripts/progress.py done "Ruta de vuelo lista"

tiles:
	@python3 scripts/progress.py stage-header "Generación de tiles XYZ" 1 1
	$(call run_quiet,python3 scripts/generate_tiles.py,tiles)
	@python3 scripts/progress.py done "Tiles generados"

# ── 7. Exportación cloud-optimized ──────────────────────────────────
export-cog:
	@python3 scripts/progress.py stage-header "Rasters finales → COG" 1 1
	$(call run_quiet,python3 scripts/export_cog.py,export-cog)
	@python3 scripts/progress.py done "Rasters COG listos"

export-copc:
	@python3 scripts/progress.py stage-header "Nubes de puntos → COPC" 1 1
	$(call run_quiet,python3 scripts/export_copc.py,export-copc)
	@python3 scripts/progress.py done "Nubes de puntos COPC listas"

# ── Tests ───────────────────────────────────────────────────────────
# Corren sin datos de vuelo: los cuatro trim_* se ejercitan sobre una misión
# sintética (tests/synthetic.py). El test de caracterización compara contra
# tests/golden/trim_masks.json — si cambia un solo píxel del recorte, falla.
test:
	python3 scripts/check_deps.py
	python3 -m pytest tests/ -q

# ── 7b. Entrega al usuario (opcional) ───────────────────────────────
# Copia los productos elegidos a EXPORT_DIR, en el formato y la CRS pedidos
# (ver scripts/export_products.py). Sin EXPORT_DIR es un no-op — las corridas
# que no lo usan no cambian en nada.
export-products:
	@python3 scripts/progress.py stage-header "Exportación a carpeta de entrega" 1 1
	$(call run_quiet,python3 scripts/export_products.py,export-products)
	@python3 scripts/progress.py done "Entrega exportada"

serve:
	@echo "Visor en http://localhost:$(SERVER_PORT)/geovisor/index.html"
	python3 -m webapp.main $(SERVER_PORT)

# ── Información ────────────────────────────────────────────────────
info:
	@echo "─── DATOS FUENTE ───"
	@echo "  RGB:     $$(ls $(DATA_RGB)/*_V.JPG $(DATA_RGB)/*_W.JPG 2>/dev/null | wc -l) imágenes"
	@echo "  Térmico: $$(ls $(DATA_THERMAL)/*_T.JPG 2>/dev/null | wc -l) imágenes"
	@echo "  Multiespectral: $$(ls $(DATA_MS)/*_MS_NIR.TIF 2>/dev/null | wc -l) capturas (4 bandas c/u)"
	@echo "─── PRODUCTOS ───"
	@ls -lh $(DSM_TIF) $(RGB_TIF) $(THERMAL_TIF) $(OUTPUTS)/multispectral_orthomosaic.tif $(OUTPUTS)/indices/*.tif 2>/dev/null || echo "  No generados aún"
	@echo "─── NUBES DE PUNTOS (COPC) ───"
	@ls -lh $(OUTPUTS)/*.copc.laz 2>/dev/null || echo "  No generadas aún (make export-copc)"
	@echo "─── TILES ───"
	@echo "  RGB: $$(find $(TILES_DIR)/rgb -name '*.png' 2>/dev/null | wc -l) tiles"
	@echo "  Térmico: $$(find $(TILES_DIR)/thermal -name '*.png' 2>/dev/null | wc -l) tiles"
	@echo "  NDVI: $$(find $(TILES_DIR)/ndvi -name '*.png' 2>/dev/null | wc -l) tiles"
	@echo "─── VISOR ───"
	@echo "  make serve → http://localhost:$(SERVER_PORT)"

# ── Limpieza ───────────────────────────────────────────────────────
clean:
	@echo "🧹 Limpiando temporales …"
	@find . -iname "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@rm -f /tmp/rgb_8bit_tiles.tif /tmp/thermal_8bit_tiles.tif
	@echo "✅ Temporales limpiados"

clean-all: clean
	@echo "🧹 Limpieza total — deja el repo listo para una misión nueva …"
	@echo "   (borra TAMBIÉN data/: si queda de una misión vieja y no se limpia,"
	@echo "    la siguiente corrida mezcla dos vuelos)"
	rm -rf $(DATA_RGB) $(DATA_THERMAL) $(DATA_MS)
	rm -rf $(RGB_PROC) $(THNAT_PROC) $(MS_PROC)
	rm -f $(PROCESSING)/*.csv
	rm -rf $(TIFF_DIR) $(TIFF_DENOISED_DIR)
	rm -rf $(TILES_DIR)/rgb $(TILES_DIR)/thermal $(TILES_DIR)/hillshade $(TILES_DIR)/ndvi $(TILES_DIR)/gndvi $(TILES_DIR)/ndre
	rm -f $(TILES_DIR)/bounds.json
	rm -f $(OUTPUTS)/*.tif $(OUTPUTS)/*.aux.xml $(OUTPUTS)/*.copc.laz
	rm -rf $(OUTPUTS)/indices
	rm -rf $(LOG_DIR)
	@echo "✅ Repo limpio. Procesar misión nueva con:"
	@echo "   docker run --gpus all -v <mision>:/input -v \$$PWD/outputs:/app/outputs -v \$$PWD/geovisor/tiles:/app/geovisor/tiles -p 8080:8080 raptor"
