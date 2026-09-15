# Priorizacion On Premise

Pipeline que puntua y prioriza puntos de venta On Premise. Reemplaza a los notebooks
`1_Priority_OnPremise.ipynb` y `2_Priority_Geo_OnPremise.ipynb`: misma logica, como codigo
Python configurado por YAML.

Para correr una corrida nueva, usa la skill **`corrida-priorizacion`** (en `.claude/skills/`):
tiene el procedimiento completo, las verificaciones previas y las trampas conocidas.

## Como se corre

```bash
uv sync                                          # instala todo (Python 3.12)
uv run python -m pipeline --config peru_all.yaml # una corrida completa
```

- `--step 1` / `--step 2` corren un paso suelto; sin `--step` corre los dos y le pasa el
  DataFrame en memoria al paso 2, sin rebotar el CSV por Azure.
- `--config <archivo>.yaml` elige la corrida. Hay un YAML por corrida, versionado.

## Estructura

```
config.yaml / <pais>_<embotellador>.yaml   una corrida: entradas, salidas, parametros
.env                                       credenciales (gitignored, ver .env.example)
azure_utils.py                             lectura/escritura en Azure Blob Storage
pipeline/
  __main__.py      CLI
  config.py        carga y valida el YAML; encadena paso 1 -> paso 2
  geodata.py       descubre y reutiliza las capas geograficas del pais
  io_utils.py      csv/xlsx/parquet/json/geojson
  step1_priority.py  cruce Datamatch x Dataplor + weighted_combined_score
  step2_geo.py       hex H3, poblacion, distancias, terciles
scripts/
  ensure_geodata.py      garantiza las capas de un pais en Azure (idempotente)
  upload_geodata.py      sube las capas de country-geodata a Azure
  geodata_subregiones.py une subregiones de Geofabrik (Brasil, EEUU, Rusia...)
```

## Convenciones del datalake

Todo vive en el contenedor `prospecting-data`, bajo
`bronze/providers/dataplor/<region>/<pais>/`:

| Region | Paises |
|---|---|
| `sl` | argentina, bolivia, chile |
| `lc` | peru, colombia, republicaDominicana |
| `br` | brasil |

- Resultados: `<region>/<pais>/<embotellador>/<AAAAMM>/{1,2}_priority_<embotellador>_<pais>_<AAAAMMDD>.csv`
- Capas geograficas del pais: `<region>/<pais>/WorldPop_Overpass/`
  (`<iso3>_red_vial_overpass_turbo.geojson`, `<iso3>_parada_bus_overpass_turbo.geojson`,
  `<iso3>_pop_<ano>_CN_100m_<release>.tif`, `pop_by_hex_res8.parquet`)
- Las fuentes de entrada estan en el contenedor **`prospection-datacleaning`**, no en
  `prospecting-data`. Los pedidos suelen venir sin el nombre del contenedor: verificalo
  antes de escribir el YAML.

## Reglas del proyecto

**Las credenciales nunca van en el codigo ni en el YAML.** Se leen del `.env`
(`AZURE_STORAGE_CONNECTION_STRING`, o `AZURE_STORAGE_ACCOUNT` + `AZURE_STORAGE_KEY` /
`AZURE_STORAGE_SAS_TOKEN`).

**Las capas geograficas no se vuelven a descargar.** Son del pais entero, no de la corrida:
sirven para cualquier embotellador y cualquier mes. El YAML solo declara `geodata.prefix` y
el pipeline descubre los archivos. Antes de bajar nada, corre `scripts/ensure_geodata.py`,
que no hace nada si ya estan.

**El agregado raster -> H3 se cachea por pais** en `pop_by_hex_res8.parquet`. Borralo solo si
cambia el raster.

**Verifica contra Azure antes de asumir.** Contenedor, existencia del archivo y columnas: un
`list_blobs` cuesta segundos y evita descubrir el problema a los 20 minutos de descarga.
