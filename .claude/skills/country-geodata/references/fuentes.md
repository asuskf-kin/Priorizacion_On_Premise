# Fuentes, etiquetas y trampas conocidas

Consulta este archivo cuando necesites capas mas alla de las tres del default, cuando algo falle
de forma rara, o cuando el usuario pregunte de donde salen los datos.

## Indice

- [Overpass API](#overpass-api)
- [Geofabrik: la fuente alternativa](#geofabrik-la-fuente-alternativa)
- [Etiquetas OSM utiles por tema](#etiquetas-osm-utiles-por-tema)
- [WorldPop](#worldpop)
- [Conversion y uso posterior](#conversion-y-uso-posterior)
- [Problemas frecuentes](#problemas-frecuentes)

## Overpass API

`https://overpass-turbo.eu/` es solo una interfaz web sobre la Overpass API. El script habla
directo con la API, que es lo mismo que haria el boton "Ejecutar" del sitio, pero sin navegador y
sin limite de tamano de descarga del browser.

Espejos usados, en orden:

| Espejo | Nota |
|---|---|
| `overpass-api.de` | El principal. Da 504 seguido cuando esta saturado; no es un error de la consulta |
| `overpass.kumi.systems` | Suele aguantar consultas grandes mejor que el principal |
| `overpass.private.coffee` | Respaldo |
| `overpass.osm.jp` | Respaldo, servidor en Japon (mas latencia desde America) |

Patron de consulta que usa el script:

```overpassql
[out:json][timeout:1800];
area(3600000000 + <relation_id>)->.a;
(
  way["clave"="valor"](area.a)(sur,oeste,norte,este);
);
out geom;
```

Dos detalles que ahorran dolores de cabeza:

- `out geom;` devuelve la geometria dentro de cada way, asi no hace falta una segunda pasada para
  resolver nodos (`out body;` + `>;` es mas lento y mas fragil).
- Los filtros se encadenan con AND: `(area.a)(bbox)` permite recortar por cuadrantes sin perder el
  recorte por pais. Es lo que hace posible dividir consultas grandes.

## Geofabrik: la fuente alternativa

Geofabrik corta el planeta OSM cada dia en extractos por pais y region. El script usa la variante
shapefile (`<pais>-latest-free.shp.zip`), que no necesita herramientas de PBF.

```bash
curl -s https://download.geofabrik.de/index-v1.json | python -m json.tool | head -40
python scripts/geofabrik.py BO      # resuelve el pais y lista las capas del extracto
```

El indice `index-v1.json` es un GeoJSON con las 555 regiones y, en `properties`, el codigo
`iso3166-1:alpha2` y las URLs. Por eso el script nunca adivina la ruta: busca por ISO2 y toma el
extracto mas especifico. Algunos paises chicos no tienen extracto propio y viven dentro del de su
region (Centroamerica, islas del Caribe); en ese caso `find_region` avisa y hay que usar Overpass
o recortar el extracto regional.

**Capas del extracto** (las que interesan aqui):

| Capa | Contenido | Campos |
|---|---|---|
| `gis_osm_roads_free_1` | Toda la red vial, lineas | osm_id, code, fclass, name, ref, oneway, maxspeed, layer, bridge, tunnel |
| `gis_osm_transport_free_1` | Puntos de transporte | osm_id, code, fclass, name |
| `gis_osm_transport_a_free_1` | Areas de transporte (terminales) | idem |
| `gis_osm_pois_free_1` | Puntos de interes (salud, educacion, comercio) | osm_id, code, fclass, name |
| `gis_osm_railways_free_1`, `gis_osm_waterways_free_1`, `gis_osm_places_free_1` | Ferrocarril, hidrografia, lugares poblados | |

`fclass` es el equivalente de la etiqueta principal: en vias toma los mismos valores que
`highway` (motorway, trunk, primary, secondary, tertiary, residential, track...) y en transporte
`bus_stop`, `bus_station`, `railway_station`, `tram_stop`, etc.

**Lectura sin dependencias.** `geofabrik.py` abre el ZIP por rangos HTTP (`Accept-Ranges: bytes`,
que Geofabrik soporta) y parsea SHP y DBF a mano, asi que de un extracto de 400 MB solo baja las
capas que se usan. Si el servidor no aceptara rangos, cae a descargar el ZIP completo en la
carpeta de salida. El SHP es un formato simple: cabecera de 100 bytes y registros con tipo 1
(punto), 3 (polilinea) y 5 (poligono); el DBF es dBase III. No hace falta GDAL ni pyshp.

**Lo que se pierde frente a Overpass**, y conviene decirlo al usuario cuando el respaldo actua:

- Solo los atributos del shapefile. Si el analisis depende de `surface`, `lanes`, `access`,
  `operator` o cualquier etiqueta fuera de la lista, hay que ir por Overpass.
- Las paradas del esquema nuevo (`public_transport=platform` / `stop_position`) no estan. En
  Uruguay eso son ~1 900 paradas por Geofabrik contra ~2 400 por Overpass: los mismos paises,
  distinto criterio de la fuente.
- El recorte tiene un margen alrededor de la frontera, asi que pueden colarse tramos del pais
  vecino. Si eso molesta, recorta despues con el limite administrativo.

## Etiquetas OSM utiles por tema

Para agregar capas nuevas al script, reusa `fetch_tiled` + `write_geojson` y cambia solo la
consulta. Las etiquetas mas usadas:

| Tema | Consulta |
|---|---|
| Salud | `node/way["amenity"~"^(hospital\|clinic\|doctors\|pharmacy)$"]`, `["healthcare"]` |
| Educacion | `node/way["amenity"~"^(school\|college\|university\|kindergarten)$"]` |
| Ferrocarril | `way["railway"~"^(rail\|light_rail\|subway\|tram)$"]`, estaciones `node["railway"="station"]` |
| Aeropuertos | `node/way["aeroway"~"^(aerodrome\|terminal)$"]` |
| Rutas de bus (recorridos) | `relation["route"="bus"]` — ojo: son relaciones, hay que procesarlas distinto a nodos |
| Edificios | `way["building"]` — volumen enorme, solo por ciudad |
| Hidrografia | `way["waterway"~"^(river\|stream)$"]`, `way["natural"="water"]` |
| Lugares poblados | `node["place"~"^(city\|town\|village)$"]` |

Para paradas de bus, el modelo de datos de OSM tiene dos escuelas conviviendo y por eso el script
pide las cuatro variantes: el esquema viejo (`highway=bus_stop`) y el nuevo de transporte publico
(`public_transport=platform` / `stop_position` con `bus=yes`), mas `amenity=bus_station` para
terminales. Pedir solo una deja fuera ciudades enteras segun quien las mapeo.

## WorldPop

Catalogo REST (util para explorar sin abrir el navegador):

```bash
curl -s https://hub.worldpop.org/rest/data/pop | python -m json.tool          # alias disponibles
curl -s "https://hub.worldpop.org/rest/data/pop/G2_CN_POP_R25A_100m?iso3=BOL" # datasets por pais
```

Productos de poblacion a 100 m, de mas nuevo a mas viejo:

| Alias | Que es |
|---|---|
| `G2_CN_POP_R25A_100m` | Release R2025A, anos 2015-2030. El default del script |
| `G2_CN_POP_R24B_100m` / `G2_UC_POP_R24B_100m` | Release R2024B, 2015-2030, constrained / unconstrained |
| `G2_CN_POP_2024_100m` / `G2_UC_POP_2024_100m` | Solo 2024 |
| `wpgpunadj` / `wpgp` | Serie clasica 2000-2020, con y sin ajuste a totales de Naciones Unidas |
| `cic2020_100m` / `cic2020_UNadj_100m` | Constrained 2020 |

**Constrained vs unconstrained** es la distincion que mas confunde: el *constrained* solo asigna
poblacion donde hay edificios detectados por satelite, el *unconstrained* reparte por todo el
territorio habitable. Para accesibilidad, cobertura de servicios o analisis urbano el constrained
es casi siempre el correcto, y por eso es el default. El unconstrained sirve cuando la capa de
edificios de un pais es mala y deja huecos.

**Los anos futuros son proyecciones.** La serie llega a 2030 porque el modelo extrapola. Decirlo
al usuario no es un tecnicismo: cambia como se debe citar el dato en un informe.

Otros productos del hub (mismo patron de URL, distinto alias): estructura por edad y sexo
(`age_structures`), nacimientos (`births`), embarazos (`pregnancies`), tiempo de viaje a centros de
salud. Se consultan igual con `?iso3=XXX`.

## Conversion y uso posterior

El script deja GeoJSON en EPSG:4326 a proposito: se abre en cualquier lado sin dependencias. Si el
usuario necesita otro formato y tiene GDAL:

```bash
ogr2ogr -f GPKG salida.gpkg entrada.geojson              # GeoPackage
ogr2ogr -f "ESRI Shapefile" salida_shp entrada.geojson   # Shapefile (trunca nombres de campo)
```

Con Python y geopandas/rasterio, el cruce tipico (poblacion cerca de una parada) se hace
reproyectando a un CRS metrico del pais antes de calcular distancias — en grados, un buffer de
"500 m" no significa lo mismo en el ecuador que a 50 grados de latitud.

## Problemas frecuentes

**Todos los espejos devuelven 504.** Suele ser saturacion temporal. Con `--source auto` el script
ya cae solo a Geofabrik, asi que la descarga igual termina; si prefieres datos con todas las
etiquetas, reintentar en unos minutos con `--source overpass` suele bastar, y bajar a
`--roads trunk` reduce mucho el peso de la consulta.

**Geofabrik no tiene extracto para el pais.** Pasa con paises chicos que viven dentro del
extracto de su region. Opciones: usar Overpass (`--source overpass`), o bajar el extracto regional
y recortarlo. No inventes una ruta de descarga: el indice `index-v1.json` es la autoridad.

**El pais no aparece en Nominatim.** Probar el nombre en ingles o el oficial. Casos ambiguos
tipicos: Congo (hay dos), Georgia (pais vs estado de EEUU), Corea (Norte/Sur). Si hay duda real,
preguntale al usuario en vez de adivinar: bajar el pais equivocado desperdicia mucho tiempo.

**Sin ISO3.** Algunos territorios no tienen `ISO3166-1:alpha3` en OSM (territorios dependientes,
estados con reconocimiento parcial). WorldPop tampoco suele cubrirlos. Usa `--skip pop` y avisa.

**GeoJSON gigante.** Es esperable en paises grandes: la geometria domina el tamano. Bajar a
`--roads trunk`, o recortar despues al area de interes, es mejor que simplificar geometrias sin
avisar — simplificar cambia longitudes y distancias, y eso corrompe cualquier analisis de red.

**Conteo de paradas sospechosamente bajo.** Casi siempre es real: OSM tiene poco transporte
publico mapeado fuera de las capitales. Verificalo mirando la composicion por `stop_kind` y dilo
con claridad en vez de presentar el archivo como completo.
