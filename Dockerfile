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

# El _gdal_array.so de ODM (SuperBuild) está compilado contra la ABI de
# NumPy 1.x; el venv del ODM base trae NumPy 2.3.2, que rompe en runtime
# (ImportError) apenas se usa gdal Python (ReadAsArray/UseExceptions) —
# ODM mismo nunca lo toca (usa gdalbuildvrt/gdal_translate como binarios,
# no vía Python), así que el problema es latente hasta que nuestros
# scripts lo ejercitan. Bajar a NumPy 1.26 lo resuelve.
RUN pip install --no-cache-dir "numpy<2" fastapi "uvicorn[standard]" python-multipart

WORKDIR /app
COPY . /app
RUN chmod +x dji_thermal_sdk/utility/bin/linux/release_x64/* \
    && chmod +x docker/entrypoint.sh docker/setup-data.sh docker/setup-data-multispectral.sh \
    && ln -s ../outputs geovisor/outputs
# geovisor/serve.py sirve solo desde geovisor/ (Python http.server no permite
# salir de su raíz) pero outputs/ (donde vive area_afectada.geojson, que el
# geovisor pide por fetch) es un volumen hermano montado en /app/outputs, no
# adentro de geovisor/ — el symlink lo resuelve sin duplicar el volumen ni
# tocar las rutas relativas que ya usa el Makefile (todas relativas a /app).

# nvidia-container-cli exige cuda>=12.9; con drivers algo más viejos (p.ej.
# CUDA 12.8) el contenedor con --gpus all ni arranca. CUDA 12.x es
# compatible en versión menor en la práctica, y esta variable solo importa
# cuando esa validación estricta fallaría — inofensiva si el driver del
# host sí cumple la versión exigida. La horneamos por defecto para no
# tener que pasarla en cada `docker run`.
ENV NVIDIA_DISABLE_REQUIRE=1

# NO se declaran /app/processing, /app/outputs, /app/preprocessing ni
# /app/geovisor/tiles como VOLUME (a diferencia de versiones anteriores):
# la webapp necesita poder REEMPLAZARLOS por symlinks a
# /app/runs/<misión>/* en runtime (activate_mission(), core/runner.py) para
# procesar varias misiones en una sola sesión de contenedor — un VOLUME fija
# esos paths como mountpoints reales, imposibles de symlinkear desde
# adentro. El modo `run` de un solo `docker run` por misión sigue
# funcionando igual montándolos con `-v` explícito (ver README) — sin
# VOLUME declarado ya no hay "red de seguridad" de volumen anónimo si el
# usuario NO monta nada en ese modo, pero ese caso ya perdía los datos al
# borrar el contenedor igual.
EXPOSE 8080

ENTRYPOINT ["/app/docker/entrypoint.sh"]
# Default: webapp interactiva (subir fotos + elegir parámetros + progreso en
# vivo + geovisor, todo por navegador — ver docker/entrypoint.sh case
# `webapp` y webapp/main.py). Scripts de automatización existentes que ya
# pasaban `-e MODE=... -v ...:/input` SIN un argumento final explícito
# quedan afectados por este cambio de default (antes corrían el pipeline
# batch directo) — agregar `run` como argumento explícito al final del
# `docker run` para preservar el comportamiento anterior.
CMD ["webapp"]
