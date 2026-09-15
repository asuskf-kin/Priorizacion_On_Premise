# Priorizacion_On_Premise

Pipeline que evalua, puntua y prioriza puntos de venta On Premise cruzando Datamatch
con Dataplor y enriqueciendolos con capas geograficas (poblacion WorldPop, red vial y
paradas de colectivo).

Reemplaza a los notebooks `1_Priority_OnPremise.ipynb` y `2_Priority_Geo_OnPremise.ipynb`:
misma logica, pero como codigo Python ejecutable de punta a punta.

## Instalacion (uv)

```bash
uv sync                 # crea .venv e instala todo, con la version de Python correcta
cp .env.example .env    # y completa las credenciales de Azure
```

`uv sync` se encarga del interprete (Python 3.12: geopandas, rasterio y h3 todavia no
tienen wheels para 3.14) y de las dependencias fijadas en `uv.lock`. Para agregar una
libreria: `uv add <paquete>`.

Si preferis pip: `pip install -r requirements.txt` (archivo generado con
`uv export --format requirements-txt --no-hashes -o requirements.txt`; regeneralo cuando
cambien las dependencias).

## Uso

```bash
uv run python -m pipeline                      # config.yaml, pasos 1 y 2
uv run python -m pipeline --step 1             # solo Priority
uv run python -m pipeline --step 2             # solo geo (lee el output del paso 1 desde Azure)
uv run python -m pipeline --config arca.yaml   # otra corrida (otro pais / embotellador)
```

## `config.yaml`: lo unico que tocas por corrida

Ahi declaras que bases lees y donde se guardan los resultados. Para una corrida nueva,
copia el archivo (`arca.yaml`, `chile_andina.yaml`, ...) y cambia rutas, contenedores y
nombres de salida. Los parametros del modelo (meses de popularidad, pesos, cortes de
categoria, resolucion H3, CRS metrico) tambien salen de ahi, sin tocar codigo.

Si `step2_geo.inputs.priority` queda vacio, el paso 2 usa automaticamente el output del
paso 1.

## Corrida nueva

> Si trabajas con Claude Code, la skill `corrida-priorizacion` (en `.claude/skills/`) tiene
> el procedimiento completo con las verificaciones previas y las trampas conocidas.
> `CLAUDE.md` resume las convenciones del datalake.


1. **Capas geograficas del pais** (una sola vez por pais):

   ```bash
   uv run python scripts/ensure_geodata.py --country Peru --iso per        --prefix bronze/providers/dataplor/lc/peru/WorldPop_Overpass/
   ```

   Es idempotente: si las tres capas ya estan en Azure no descarga nada y te lo dice.
   Solo baja (con la skill `country-geodata`) las que falten.

2. **Config de la corrida**: copia `peru_all.yaml`, ajusta rutas de entrada, nombres de
   salida y el `geodata.prefix` del pais.

3. **Correr**: `uv run python -m pipeline --config peru_all.yaml`

### Las capas geograficas se reutilizan

Vias, paradas de bus y poblacion son del **pais entero**, no de una corrida: sirven igual
para cualquier embotellador y cualquier mes. Por eso el config no repite las tres rutas,
solo declara donde viven:

```yaml
step2_geo:
  geodata:
    prefix: bronze/providers/dataplor/lc/peru/WorldPop_Overpass/
    container_name: prospecting-data
```

El pipeline descubre los archivos ahi (`*_red_vial_*.geojson`, `*_parada_bus_*.geojson`,
`*.tif`) y los reusa. Si falta alguna, el error te dice cual y como generarla. Para forzar
un archivo puntual, declaralo en `step2_geo.inputs` y eso manda sobre el descubrimiento.

Regiones en uso: `sl/` para Argentina y Bolivia, `lc/` para Peru.

### Con o sin Datamatch

Si el pais no tiene una base Datamatch aparte, dejas solo `dataplor` en
`step1_priority.inputs` (o apuntas las dos entradas al mismo archivo): el pipeline lo
detecta, lee el archivo una sola vez, no hace el cruce y se queda con las columnas
equivalentes. Es el caso de Peru; Argentina si usa las dos bases.

## Credenciales

Nunca van en el codigo ni en `config.yaml`: se leen del `.env` (que esta en `.gitignore`).
`azure_utils.py` acepta, en este orden:

1. `AZURE_STORAGE_CONNECTION_STRING`
2. `AZURE_STORAGE_ACCOUNT` + `AZURE_STORAGE_KEY`
3. `AZURE_STORAGE_ACCOUNT` + `AZURE_STORAGE_SAS_TOKEN`

## Estructura

```
pyproject.toml           dependencias del proyecto (uv)
uv.lock                  versiones exactas, reproducibles
config.yaml              configuracion de la corrida (inputs / outputs / parametros)
peru_all.yaml            corrida de Peru (fuente unica, sin Datamatch)
pipeline/geodata.py      descubre y reutiliza las capas geograficas del pais
scripts/ensure_geodata.py  garantiza las capas del pais en Azure (idempotente)
scripts/upload_geodata.py  sube las capas de country-geodata a Azure
.env                     credenciales (no se versiona)
azure_utils.py           lectura y escritura en Azure Blob Storage
pipeline/
  __main__.py            CLI
  config.py              carga del config y encadenado paso 1 -> paso 2
  io_utils.py            carga de csv/xlsx/parquet/json/geojson y escritura de csv
  step1_priority.py      cruce Datamatch x Dataplor + weighted_combined_score
  step2_geo.py           hex H3, pop_count/pop_density, distancias y mobility
```

## Salidas

Paso 1 agrega: `historical_popularity_scores_in_last_6_months`, `weighted_combined_score`,
`probability_existence_cat`.

Paso 2 agrega: `hex_id`, `pop_count`, `pop_density`, `dist_to_main_road`, `car_mobility`,
`dist_to_bus_stop`, `walk_mobility`.
