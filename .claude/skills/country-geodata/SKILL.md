---
name: country-geodata
description: >-
  Descarga en un solo paso los datos geoespaciales base de un pais: carreteras principales y
  paradas de buses desde OpenStreetMap — por Overpass API (el motor de overpass-turbo.eu) con
  respaldo automatico en los extractos de Geofabrik — y el raster de poblacion WorldPop de 100 m
  del ano actual, todo guardado en una carpeta con el nombre del pais y documentado con sus
  fuentes. Usala siempre que alguien pida datos de vias, red vial, carreteras, paradas/estaciones
  de bus, transporte publico o poblacion/densidad para un pais entero — por ejemplo "descarga las
  carreteras y paradas de bus de Peru", "necesito el WorldPop 100m de Colombia", "bajame de
  overpass turbo los datos de Ecuador", "get the road network and bus stops for Kenya", "haz el
  mismo proceso para Bolivia" — o cuando solo den el nombre de un pais en un contexto de analisis
  geoespacial, accesibilidad, cobertura o riesgo. Preferila sobre armar consultas Overpass a mano:
  ya maneja espejos caidos, consultas que revientan en paises grandes, la fuente alternativa
  cuando Overpass no responde, la version mas reciente de WorldPop y la procedencia documentada.
---

# Datos geoespaciales base por pais

## Que entrega

Dado solo el nombre de un pais, produce una carpeta `<Pais>/` con:

| Archivo | Contenido |
|---|---|
| `<pais>_carreteras_principales_osm.geojson` | Red vial principal, LineString, EPSG:4326, con todas las etiquetas OSM |
| `<pais>_paradas_buses_osm.geojson` | Paradas, andenes y terminales de bus, Point, EPSG:4326 |
| `<iso3>_pop_<ano>_CN_100m_<release>.tif` | Poblacion WorldPop 100 m del ano actual (personas por pixel) |
| `README.md` | Fuentes, consultas Overpass exactas, conteos, licencias |
| `_descarga_metadata.json` | Lo mismo en formato legible por maquina, para reproducir |

El README y el metadata importan tanto como los datos: dentro de dos meses nadie recuerda si el
GeoJSON incluia `tertiary` ni de que ano era el raster, y esa duda invalida analisis enteros.

## Como ejecutarlo

Un solo script hace todo. Solo necesita `requests` (nada de GDAL/geopandas/rasterio):

```bash
python <skill>/scripts/country_geodata.py "Bolivia"
```

Guarda en `./Bolivia/` dentro del directorio de trabajo actual. Opciones:

| Flag | Para que |
|---|---|
| `--out RUTA` | Carpeta destino explicita (usala si el usuario ya tiene una estructura de proyecto) |
| `--roads trunk\|standard\|all` | `standard` (default) = motorway/trunk/primary/secondary/tertiary + enlaces; `trunk` quita tertiary; `all` agrega unclassified y residential (pesa muchisimo) |
| `--source auto\|overpass\|geofabrik` | De donde salen las capas OSM. `auto` (default) intenta Overpass y cae a Geofabrik si falla |
| `--year 2025` | Otro ano de WorldPop (default: ano actual) |
| `--skip roads,bus,pop` | Bajar solo algunas capas |
| `--max-depth 4` | Cuantas veces puede subdividir el bbox si Overpass se atora (default 3) |

Antes de correrlo, decide dos cosas y dilas en voz alta al usuario en vez de asumirlas en silencio:

1. **Donde va la carpeta.** Si el usuario esta trabajando en un proyecto concreto, `--out` hacia
   ese proyecto suele ser lo que quiere; si no dijo nada, el default (`./<Pais>`) esta bien.
2. **Si `standard` es el perfil correcto.** En muchos paises de America Latina, Africa y Asia
   buena parte de la red interurbana esta clasificada como `tertiary`, por eso es el default.
   Si el usuario quiere solo la red troncal para un mapa de rutas largas, `trunk` genera archivos
   mucho mas livianos. El campo `highway` permite filtrar despues, asi que ante la duda conviene
   bajar de mas.

## Dos fuentes para lo mismo, y cuando usa cada una

Los datos son los mismos de OpenStreetMap, pero llegan por dos caminos con caracter distinto:

| | Overpass API | Extracto Geofabrik |
|---|---|---|
| Que es | Servidor de consultas (lo que hay detras de overpass-turbo.eu) | Descarga estatica: `<pais>-latest-free.shp.zip`, regenerado a diario |
| Frescura | Minutos | Hasta 24 h de atraso |
| Atributos | **Todas** las etiquetas OSM | Solo las del shapefile: `osm_id`, `fclass`, `name`, `ref`, `oneway`, `maxspeed`, `bridge`, `tunnel` |
| Paradas de bus | Esquema viejo **y** nuevo (`public_transport=platform/stop_position`) | Solo `highway=bus_stop` y `amenity=bus_station` → suele dar menos paradas |
| Cuando falla | Saturacion (504), timeouts, consultas gigantes | Casi nunca; es un archivo estatico |
| Costo | Infraestructura donada, hay que ser considerado | Ancho de banda propio |

`auto` pide primero a Overpass porque entrega datos mas ricos y mas frescos, y solo si Overpass
se rinde — tras rotar espejos y subdividir el bbox — baja el extracto. El script lee el ZIP por
rangos HTTP, asi que de un extracto de 400 MB solo trae las capas de vias y transporte.

Fuerza `--source geofabrik` cuando ya sabes que Overpass esta caido, cuando el pais es enorme y no
quieres castigar el servidor, o cuando necesitas reproducibilidad (el extracto tiene fecha; una
consulta Overpass devuelve el OSM del momento). Fuerza `--source overpass` cuando el usuario
necesite etiquetas completas y prefiera fallar antes que recibir datos recortados.

**Esto importa al reportar.** Si la corrida cayo a Geofabrik, dilo: menos etiquetas y menos
paradas no es un error del script, es una diferencia real de la fuente, y el usuario tiene que
saberlo antes de comparar conteos con una corrida anterior. El README y el `_descarga_metadata.json`
registran que fuente se uso en cada capa.

## Que hace el script por dentro (y por que)

- **Resuelve el pais con Nominatim** para obtener la relacion OSM, el ISO3 y el bbox. Nunca
  hardcodees el id de area: `area(3600000000 + relation_id)` se calcula solo.
- **Rota entre cuatro espejos de Overpass.** Un 504 de `overpass-api.de` es rutina, no una falla
  del plan; el script pasa al siguiente espejo sin intervencion.
- **Subdivide el bbox cuando la consulta no entra.** Paises grandes (Brasil, India, Rusia) hacen
  reventar una sola consulta por timeout o memoria; el script parte el area en cuadrantes y
  deduplica por `osm_id` al unir. Por eso da igual el tamano del pais.
- **Busca el producto WorldPop mas nuevo disponible.** Consulta el REST del hub y recorre los
  alias de 100 m del mas reciente al mas viejo (R2025A → R2024B → 2024 → series 2000-2020). Si el
  ano pedido no existe, toma el mas cercano hacia atras y lo deja anotado en el README.
- **Verifica el GeoTIFF leyendo su cabecera** (sin GDAL): dimensiones, tamano de pixel, NoData.
  Si el pixel no es ~0.000833 grados, algo se bajo mal y hay que decirlo.

## Al terminar: verifica y reporta

El script imprime un resumen. Antes de darlo por bueno, revisa que los numeros tengan sentido y
reportalos al usuario — un GeoJSON de 0 features tambien "se descarga bien":

- **Que fuente se uso en cada capa** (el script la imprime entre corchetes: `[overpass]` o
  `[geofabrik]`). Si hubo respaldo, explica en una linea que cambia — atributos reducidos y menos
  paradas — para que nadie interprete la diferencia como perdida de datos.
- Conteos plausibles para el tamano del pais. Cero paradas de bus es posible y honesto en paises
  con poco mapeo, pero hay que decirlo explicitamente, no dejar el archivo vacio sin comentario.
- La composicion por clase de via (`by_class`): si `tertiary` domina por mucho, mencionalo para
  que el usuario sepa que puede filtrar.
- El ano real de WorldPop y si es proyeccion. La serie R2025A llega a 2030: cualquier ano futuro
  es **modelado**, no observado, y el usuario tiene que saberlo antes de usarlo en un informe.
- Tamano de cada archivo, para que el usuario sepa que esperar al abrirlo en QGIS.

Menciona tambien las advertencias que el script haya impreso (espejos caidos, bbox subdividido,
ano sustituido). Un resumen que oculta esos detalles es peor que uno que los enumera.

## Limites conocidos y como manejarlos

**OSM es colaborativo, no oficial.** La cobertura de paradas de bus varia muchisimo: excelente en
ciudades grandes, casi nula en zonas rurales. No presentes el conteo como el universo real de
paradas del pais; es lo que hay mapeado hoy.

**GeoJSON y nada mas.** Se eligio a proposito: se lee en QGIS, GeoPandas, R y navegadores sin
dependencias. Si el usuario necesita Shapefile o GeoPackage y tiene GDAL o geopandas instalados,
conviertelo despues (`ogr2ogr salida.gpkg entrada.geojson`) en vez de complicar el script.

**Paises muy grandes generan archivos enormes.** Brasil con `--roads standard` pasa facil del
gigabyte. Si el usuario solo necesita una region, es mas sensato bajar `trunk` primero, o recortar
despues con el limite administrativo.

**El limite del pais.** Este skill no descarga fronteras ni divisiones administrativas. Para eso
esta `geojson-boundary-finder`, que es mejor fuente para provincias/municipios. Si el usuario
quiere cruzar poblacion con divisiones, combina ambos.

**Etica de uso de las APIs.** Nominatim pide maximo 1 consulta por segundo y Overpass es
infraestructura donada por voluntarios. No corras el script en bucle sobre decenas de paises sin
pausas; si hace falta un continente entero, mejor un extracto de Geofabrik.

## Otras capas

Si el usuario pide algo mas (hospitales, escuelas, ferrocarriles, rios), el patron ya esta en el
script: agrega un bloque de consulta Overpass y reutiliza `fetch_tiled` + `write_geojson`, que ya
resuelven espejos, subdivision y deduplicacion. Por el lado de Geofabrik, el extracto ya trae las
capas equivalentes (`gis_osm_pois_free_1` para puntos de interes, `gis_osm_railways_free_1`,
`gis_osm_waterways_free_1`, `gis_osm_places_free_1`) y `geofabrik.read_layer()` las recorre igual
que las que ya estan implementadas.

Ver `references/fuentes.md` para las etiquetas OSM mas utiles, el detalle de los dos caminos de
descarga y los otros productos de WorldPop (edad/sexo, nacimientos, embarazos).
