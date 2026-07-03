# Metroviolencia

Visualización en **tiempo real** de los trenes de Metrovalencia sobre un mapa
de la red. Sondea los tiempos de llegada públicos de cada estación, estima la
posición de cada tren y los pinta como puntos que se mueven por las vías.

![preview](docs/preview.png)

## Cómo funciona

1. **Fuente de datos.** La web de Metrovalencia
   (`/ca/consulta-estaciones/`) consulta, por estación, un endpoint AJAX de
   WordPress:

   ```
   POST https://www.metrovalencia.es/wp-admin/admin-ajax.php
   action=formularios_ajax&data=action%3Dinfo-estacion%26id%3D<ID>
   ```

   Devuelve un fragmento HTML con las próximas llegadas: **línea + destino + ETA**
   (la ocupación aparece comentada en el servidor, aún no se sirve).

2. **Geometría de la red.** De `/ca/consulta-mapes/` (variable JS
   `datosMapaGeneral`) se extraen las coordenadas geográficas reales de las 144
   estaciones, los trazados de vía y los colores oficiales de línea.
   → `scripts/extract_network.py` → `data/network.json`.

3. **Poller.** `backend/poller.py` sondea las 144 estaciones cada ~30 s, parsea
   las llegadas y las guarda en SQLite (`data/metro.db`): histórico completo.

4. **Estimador de posiciones.** `backend/positions.py` sitúa un tren por tramo
   ocupado: para cada estación toma el tren inminente (menor ETA) y lo coloca
   entre la estación anterior y esta según su ETA. Evita el doble conteo.

5. **API + frontend.** `backend/api.py` (FastAPI) sirve la red, las llegadas y
   los trenes estimados; `web/` es un mapa Leaflet (tiles CartoDB dark) que los
   anima.

## Uso local

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # Linux: .venv/bin/pip

# 1) generar la geometría (una vez; ya versionada en data/network.json)
python scripts/extract_network.py

# 2) arrancar el poller (bucle continuo)
python -m backend.poller

# 3) arrancar la API + web (otra terminal)
python -m uvicorn backend.api:app --host 0.0.0.0 --port 8770
# -> http://localhost:8770
```

Una sola pasada de sondeo: `python -m backend.poller --once`.

## Despliegue (Raspa)

Dos servicios systemd: `metroviolencia-poller` (sondeo) y `metroviolencia-web`
(API+frontend). Ver `deploy/`.

## Aviso

Datos obtenidos de la web pública de Metrovalencia con fines no comerciales.
Las posiciones son **estimaciones** a partir de los tiempos de llegada, no
telemetría real de los trenes.
