#!/usr/bin/env python3
"""Alerta de focos activos / área afectada — para que el puesto de mando se
entere sin tener que abrir el geovisor.

Se llama DESPUÉS de cada situation-summary (ver docker/entrypoint.sh). Si
situation.json reporta focos activos (hotspots_activos > 0) o área afectada,
escribe outputs/alert.json (siempre) y, si ALERT_WEBHOOK_URL está definida,
hace un POST con el payload a ese webhook (Slack/Telegram/Teams/Genérico —
cualquiera que acepte un JSON por POST).

Nunca rompe el pipeline: el entrypoint la llama con `|| true`, y los errores
de red se loguean y se ignoran — la alerta es un extra, no un requisito.

Variables de entorno:
  ALERT_WEBHOOK_URL   URL a la que se hace POST con el payload (opcional)
  RAPTOR_MISSION      nombre de la misión para el payload (opcional)

Uso: python3 scripts/notify_alert.py
Lee: outputs/situation.json
Escribe: outputs/alert.json (solo si hay algo que alertar)
"""
import json
import os
import sys
import urllib.request

SITUATION = "outputs/situation.json"
ALERT = "outputs/alert.json"


def main():
    if not os.path.isfile(SITUATION):
        print("  📡 sin situation.json todavía — sin alerta")
        return 0
    with open(SITUATION, encoding="utf-8") as f:
        s = json.load(f)

    n_focos = s.get("hotspots_activos") or 0
    area = s.get("area_ha")
    activo = n_focos > 0 or (area not in (None, 0))
    if not activo:
        print("  📡 sin focos activos ni área detectada — no se genera alerta")
        return 0

    payload = {
        "evento": "focos_activos" if n_focos > 0 else "area_afectada",
        "mision": os.environ.get("RAPTOR_MISSION", "mision"),
        "captura": s.get("captura"),
    }
    for k in ("hotspots_activos", "area_ha", "severidad", "temp_max",
              "temp_promedio", "confianza", "cobertura_pct", "solo_termico",
              "area_volada_km2", "alerta_cobertura_baja"):
        if k in s:
            payload[k] = s[k]
    if s.get("hotspots"):
        payload["hotspots"] = s["hotspots"][:10]

    with open(ALERT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(ALERT, 0o666)
    except OSError:
        pass

    url = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    if not url:
        print(f"  📡 alerta guardada en {ALERT} "
              "(sin ALERT_WEBHOOK_URL para enviarla — arrancá con "
              "-e ALERT_WEBHOOK_URL=... si querés el aviso por webhook)")
        return 0

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"  📡 alerta enviada a {url} (HTTP {r.status})")
    except Exception as exc:
        print(f"  ⚠ no se pudo enviar la alerta ({exc}) — quedó en {ALERT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
