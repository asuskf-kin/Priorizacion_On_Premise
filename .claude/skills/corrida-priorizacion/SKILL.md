---
name: corrida-priorizacion
description: >-
  Procedimiento para correr el pipeline de priorizacion On Premise sobre un pais o
  embotellador nuevo: verificar la fuente en Azure, escribir el YAML de la corrida,
  garantizar las capas geograficas del pais y ejecutar los dos pasos. Usala siempre que
  alguien pida procesar una base de Dataplor — por ejemplo "ahora vamos a hacer
  Femsa_ON_Premise_COL_.../dataplor_final_pipeline_output_co_on_premise.csv" con un
  file_path y un file_name de salida, "corre la priorizacion de Ecuador", "agrega a la cola
  este embotellador", "haz lo mismo para Guatemala" — o cuando den una ruta de
  `dataplor_cleaned/` y una carpeta destino en `bronze/providers/dataplor/`. Cubre las
  trampas que ya costaron tiempo: contenedor sin declarar, splits por embotellador, paises
  grandes que Overpass rechaza, rasters que no entran en memoria y como encolar varias
  corridas sin tumbar la maquina.
---

# Correr una corrida de priorizacion

## Lo que te van a dar, y lo que falta

El pedido tipico trae tres cosas:

```
Femsa_ON_Premise_COL_20260817/dataplor_cleaned/dataplor_final_pipeline_output_co_on_premise.csv
file_path: bronze/providers/dataplor/lc/colombia/femsa/202608/
container_name: prospecting-data
file_name: 2_priority_femsa_colombia_20260817
```

Fijate que **el `file_name` que dan es el del paso 2**. El del paso 1 es el mismo con `1_`
al principio. Y **la fuente viene sin contenedor**: casi siempre es
`prospection-datacleaning`, pero verificalo, no lo asumas.

## 1. Verificar la fuente antes de escribir nada

```python
import azure_utils as az
svc = az.get_blob_service_client_sas()
for b in svc.get_container_client("prospection-datacleaning").list_blobs(
        name_starts_with="Femsa_ON_Premise_COL_20260817/dataplor_cleaned/"):
    print(f"{b.size/1e6:9.1f} MB  {b.name}")
```

Mira dos cosas en el listado:

- **El tamano del archivo.** Marca cuanto va a tardar el paso 1 y cuanta memoria pide:
  ~50 MB son 2 minutos, ~500 MB son 15-20 minutos y un pico de ~8 GB.
- **Si hay una carpeta `dataplor_cleaned_split_por_bottler/`.** Si existe, hay que decidir
  (ver punto 2). Chile la tenia; Peru, Bolivia, Brasil, Colombia y RD no.

## 2. Una base o dos

El pipeline soporta las dos formas y elige sola segun lo que declare el YAML:

- **Fuente unica** (Peru, Bolivia, Brasil, Colombia, RD): solo `dataplor`. El archivo
  `dataplor_final_pipeline_output_*` ya viene filtrado a On Premise y trae las 63 columnas.
  El pipeline se queda con 26.
- **Dos bases** (Argentina, Chile): `datamatch` es el split del embotellador — define **que**
  POIs entran — y `dataplor` es el archivo de pais, que aporta popularidad, reviews,
  horarios y confianza. Cruzan por `dataplor_id`. La salida trae 31 columnas: las 26 de
  siempre mas `bottler`, `city_bottler`, `metodo_mapeo`, `confianza_mapeo`,
  `flag_escala_mapeo`.

**Si la salida se llama por un embotellador pero te dan el archivo de pais, preguntalo.** El
resultado va a incluir POIs de los otros embotelladores.

**El split nunca va como `dataplor`.** Tiene 15 columnas de identificacion y le faltan las 16
que el score necesita; la corrida falla al arrancar. Si el pedido lo pone ahi, es un error de
copiado: el split va en `datamatch`.

## 3. Escribir el YAML

Copia el de una corrida parecida y cambia rutas y nombres. Es un `sed` de seis lineas:

```bash
sed -e 's|<fuente vieja>|<fuente nueva>|' \
    -e 's|<carpeta salida vieja>/|<carpeta salida nueva>/|' \
    -e 's|<prefijo geodata viejo>/|<prefijo geodata nuevo>/|' \
    -e 's|1_priority_<vieja>|1_priority_<nueva>|' \
    -e 's|2_priority_<vieja>|2_priority_<nueva>|' \
    bolivia_embol.yaml > <pais>_<embotellador>.yaml
```

Verifica que resolvio bien antes de correr:

```bash
uv run python -c "
from pipeline.config import load_config, blob_uri
c = load_config('<archivo>.yaml')
print('paso 1 ->', blob_uri(c['step1_priority']['output']))
print('paso 2 ->', blob_uri(c['step2_geo']['output']))
print('geodata:', c['step2_geo']['geodata']['prefix'])
"
```

Las regiones no son intercambiables: `sl` para Argentina, Bolivia y Chile; `lc` para Peru,
Colombia y Republica Dominicana; `br` para Brasil.

## 4. Garantizar las capas del pais

```bash
uv run python scripts/ensure_geodata.py --country Peru --iso per \
    --prefix bronze/providers/dataplor/lc/peru/WorldPop_Overpass/
```

Es idempotente: si las tres capas ya estan, no descarga nada y te lo dice. Si falta alguna,
baja solo esa. **Nunca bajes capas de un pais que ya las tiene.**

Cosas a tener en cuenta cuando el pais es nuevo:

- **No corras dos consultas Overpass a la vez.** Es infraestructura donada; dos consultas
  grandes en paralelo se estorban y multiplican los 504. Si hay otra corriendo, espera.
- **Paises grandes.** Overpass rechaza la consulta de Brasil con `standard` (504 en los
  cuatro espejos). Ahi va `--source geofabrik`. Y si Geofabrik tampoco publica shapefile a
  nivel pais — le pasa a Brasil, EEUU, Rusia, Canada — usa
  `scripts/geodata_subregiones.py`, que une las subregiones deduplicando por `osm_id`, y
  despues `scripts/upload_geodata.py`.
- **Perfil de vias.** `standard` (con `tertiary`) es el default y es el correcto en
  Latinoamerica: en Brasil `tertiary` era el 39 % de la red. `trunk` solo si el usuario lo
  pide a sabiendas.

## 5. Correr

```bash
uv run python -m pipeline --config <archivo>.yaml
```

Sin `--step`, el paso 2 recibe el DataFrame en memoria y se ahorra bajar el CSV que el paso 1
acaba de subir. Corre en background: una corrida va de 2 a 50 minutos.

## 6. Verificar la salida

```python
p = svc.get_blob_client("prospecting-data", "<ruta del 2_priority>.csv").get_blob_properties()
print(p.size/1e6, "MB", p.last_modified)
```

Debe tener **36 columnas** (33 si la corrida uso dos bases y arrastro las del split) y la
misma cantidad de filas que reporto el paso 1.

## Varias corridas: encolar, no lanzar todo junto

Cada corrida pide varios GB. Lanzar cuatro en paralelo tumba la maquina. Encadenalas
esperando el archivo de salida de la anterior:

```bash
until grep -q "exited with code" "$T/<id-anterior>.output"; do sleep 20; done
uv run python -u -m pipeline --config <siguiente>.yaml
```

**De a dos en paralelo si hay caché de poblacion del pais.** Emparejalas cruzadas —una fuente
grande con una chica en cada via— para que los dos picos de memoria del paso 1 no coincidan.
Con 23,6 GB de RAM, dos corridas grandes simultaneas son riesgo real.

Antes de sumar un proceso, mira la memoria libre:

```powershell
$os = Get-CimInstance Win32_OperatingSystem
"{0:N1} GB libres" -f ($os.FreePhysicalMemory/1MB)
```

## Al reportar la corrida

Da los numeros y, sobre todo, **lo que puede malinterpretarse**:

- La distribucion de `probability_existence_cat`. Chile y Argentina rondan 57-59 % en `low`;
  el resto de los paises, 67-75 %. Es la señal de Dataplor en cada mercado, no el pipeline.
- **Los POIs sin match**, si la corrida uso dos bases. El log dice
  `Cruce listo: N sin datos de Dataplor`. Esos caen a `low` con score 0 por falta de datos,
  no por baja actividad — decilo, es la confusion mas cara del entregable.
- **La cobertura de paradas de bus del pais.** Los terciles siempre reparten 33/33/33, asi
  que `walk_mobility` parece sana aunque el pais tenga 473 paradas mapeadas (RD). Nunca
  compares esa columna entre paises.
- **Que los terciles son relativos a cada corrida.** Para comparar entre archivos van las
  columnas crudas: `pop_count`, `dist_to_main_road`, `dist_to_bus_stop`.
- Si la geodata salio por Geofabrik en vez de Overpass, y que el ano de WorldPop es
  proyeccion modelada.
