#!/usr/bin/env python3
"""
Servidor HTTP para el Geovisor RAPTOR (DJI H20T/M3T/M3M).

Sirve el dashboard + tiles XYZ (RGB, térmico, hillshade) desde geovisor/.
Soporta CORS, caché selectivo y compresión.

Uso:
  python3 geovisor/serve.py [puerto]
"""

import http.server
import json
import os
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
os.chdir(os.path.dirname(os.path.abspath(__file__)))

AREA_AFECTADA_PATH = os.path.join("outputs", "area_afectada.geojson")


class GeovisorHandler(http.server.SimpleHTTPRequestHandler):
    """Servidor estático con CORS — sirve el dashboard + tiles XYZ, más un
    único endpoint POST para persistir ediciones del polígono de área
    afectada hechas a mano en el geovisor."""

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        # Sin esto el navegador puede quedarse con una copia vieja de
        # app.js/index.html sin revalidar entre corridas de desarrollo — los
        # tiles (que no cambian una vez generados) sí se cachean fuerte.
        if "/tiles/" in self.path:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        if self.path != "/api/save-area-afectada":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            if data.get("type") != "FeatureCollection":
                raise ValueError("se esperaba un GeoJSON FeatureCollection")
            with open(AREA_AFECTADA_PATH, "w") as f:
                json.dump(data, f)
            # El servidor corre como root en el contenedor; sin esto el
            # archivo queda root:root y bloquea futuras ediciones/lecturas
            # desde el host o desde otro proceso del pipeline.
            os.chmod(AREA_AFECTADA_PATH, 0o666)
            self._send_json(200, {"ok": True})
        except Exception as e:
            self._send_json(400, {"ok": False, "error": str(e)})

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        sys.stdout.write(f"  {args[0]}\n")
        sys.stdout.flush()

    def guess_type(self, path):
        ext = os.path.splitext(path)[1].lower()
        mime_map = {
            ".html": "text/html; charset=utf-8",
            ".js":   "application/javascript; charset=utf-8",
            ".css":  "text/css; charset=utf-8",
            ".json": "application/json",
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".svg":  "image/svg+xml",
            ".ico":  "image/x-icon",
            ".mapml":"application/xml",
            ".xml":  "application/xml",
        }
        return mime_map.get(ext, "application/octet-stream")


if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════════════════╗
║  🛰️  Geovisor RAPTOR — DJI H20T/M3T/M3M        ║
║                                                  ║
║  Abrir en el navegador:                          ║
║  → http://localhost:{PORT}                          ║
║                                                  ║
║  Atajos de teclado:                              ║
║  C = Comparar   B = Sidebar                      ║
║                                                  ║
║  Ctrl+C para detener                             ║
╚══════════════════════════════════════════════════╝
""")
    # ThreadingHTTPServer (no HTTPServer a secas): con un solo hilo, una
    # conexión colgada (keep-alive de un cliente lento/roto) bloquea el
    # servidor ENTERO para cualquier otro pedido — pasó en la práctica
    # durante pruebas automatizadas del visor.
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), GeovisorHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Servidor detenido")
        server.server_close()
