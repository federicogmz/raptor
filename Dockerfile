# ═══════════════════════════════════════════════════════════════════
# Imagen ÚNICA de producción: ODM (SfM+MVS+DSM+ortofoto) + pipeline
# propio (radiometría térmica, blending, recorte, tiles) en el mismo
# contenedor. Un solo `docker run` procesa una misión de punta a punta.
#
# Base: opendronemap/odm:gpu (Ubuntu 24.04 + CUDA + OpenSfM/OpenMVS/GDAL).
# Ya trae en su venv (/code/venv, en el PATH) numpy/scipy/GDAL-py/pyproj/
# piexif/matplotlib. Solo se agregan gdal-bin (gdal2tiles.py/gdaldem, CLI
# propios del pipeline de tiles), exiftool y make.
# ═══════════════════════════════════════════════════════════════════
FROM opendronemap/odm:gpu

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        gdal-bin libimage-exiftool-perl make \
    && rm -rf /var/lib/apt/lists/*

# Versiones FIJADAS en requirements.txt (incluye el motivo de cada una, en
# particular por qué numpy<2 es obligatorio y no una preferencia). Se copia
# solo ese archivo antes que el resto del código para que el layer de pip no
# se invalide con cada cambio de un script.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
COPY . /app
# Falla el build si la imagen base movió GDAL/pyproj/scipy fuera de lo
# soportado, en vez de que aparezca como un resultado raro en una misión.
RUN python3 scripts/check_deps.py
RUN chmod +x dji_thermal_sdk/utility/bin/linux/release_x64/* \
    && chmod +x docker/entrypoint.sh docker/setup-data.sh docker/setup-data-multispectral.sh \
    && ln -s ../outputs geovisor/outputs
# El geovisor se sirve bajo /geovisor/ y pide sus datos con rutas relativas
# (outputs/area_afectada.geojson, tiles/bounds.json), pero outputs/ es un
# volumen hermano montado en /app/outputs, no adentro de geovisor/ — el
# symlink lo resuelve sin duplicar el volumen ni
# tocar las rutas relativas que ya usa el Makefile (todas relativas a /app).

# nvidia-container-cli exige cuda>=12.9; con drivers algo más viejos (p.ej.
# CUDA 12.8) el contenedor con --gpus all ni arranca. CUDA 12.x es
# compatible en versión menor en la práctica, y esta variable solo importa
# cuando esa validación estricta fallaría — inofensiva si el driver del
# host sí cumple la versión exigida. La horneamos por defecto para no
# tener que pasarla en cada `docker run`.
ENV NVIDIA_DISABLE_REQUIRE=1

# NO se declaran /app/processing, /app/outputs, /app/preprocessing ni
# /app/geovisor/tiles como VOLUME:
# la webapp necesita poder REEMPLAZARLOS por symlinks a
# /app/runs/<misión>/* en runtime (activate_mission(), core/runner.py) para
# procesar varias misiones en una sola sesión de contenedor — un VOLUME fija
# esos paths como mountpoints reales, imposibles de symlinkear desde
# adentro. El modo `run` de un solo `docker run` por misión sigue
# funcionando igual montándolos con `-v` explícito (ver README) — sin
# VOLUME declarado no hay "red de seguridad" de volumen anónimo si el usuario
# NO monta nada en ese modo: sin montaje los datos se pierden al borrar el
# contenedor.
EXPOSE 8080

ENTRYPOINT ["/app/docker/entrypoint.sh"]
# Default: webapp interactiva (subir fotos + elegir parámetros + progreso en
# vivo + geovisor, todo por navegador — ver docker/entrypoint.sh case
# `webapp` y webapp/main.py). El pipeline batch necesita `run` como argumento
# explícito al final del `docker run`; ./raptor run ya lo pasa.
CMD ["webapp"]
