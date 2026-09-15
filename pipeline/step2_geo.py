"""Paso 2 - Enriquecimiento geografico: poblacion H3, red vial y paradas de bus.

Equivalente automatizado de 2_Priority_Geo_OnPremise.ipynb. Agrega 7 columnas:
hex_id, pop_count, pop_density, dist_to_main_road, car_mobility,
dist_to_bus_stop, walk_mobility.
"""

import logging
import posixpath

import azure_utils as az
import geopandas as gpd
import h3
import pandas as pd
import rasterio
from rasterio.io import MemoryFile
from rasterio.windows import Window

from pipeline.config import resolve_step2_input
from pipeline.geodata import resolve_inputs
from pipeline.io_utils import load_data, load_source, read_blob_bytes, save_csv

log = logging.getLogger(__name__)


def assign_tertiles(series, ascending=True, labels=("low", "moderate", "high")):
    """Clasifica una serie numerica en 3 cuantiles (por ranking, tolera empates).

    ascending=True  -> valores mas altos reciben la etiqueta mas alta (pop_count).
    ascending=False -> valores mas bajos reciben la etiqueta mas alta (distancias).
    """
    ranks = series.rank(method="first", ascending=ascending)
    return pd.qcut(ranks, q=3, labels=list(labels))


def population_by_hex(raster_bytes, resolution, chunk_rows=1024):
    """Suma la poblacion del raster WorldPop por celda H3.

    Se lee por franjas de filas en vez de entero: el raster de Brasil son 53607x46827
    pixeles (~10 GB como float32 en memoria), mas de lo que tiene la maquina. El
    resultado es identico, porque sumar por hexagono es asociativo.
    """
    acumulado = {}
    with MemoryFile(raster_bytes) as memfile:
        with memfile.open() as src:
            transform = src.transform
            total = src.height
            for inicio in range(0, total, chunk_rows):
                alto = min(chunk_rows, total - inicio)
                datos = src.read(1, window=Window(0, inicio, src.width, alto))

                filas, columnas = (datos > 0).nonzero()
                if filas.size == 0:
                    continue

                xs, ys = rasterio.transform.xy(transform, filas + inicio, columnas)
                celdas = [
                    h3.latlng_to_cell(lat, lon, resolution) for lat, lon in zip(ys, xs)
                ]
                parcial = pd.Series(datos[filas, columnas], index=celdas).groupby(level=0).sum()
                for celda, valor in parcial.items():
                    acumulado[celda] = acumulado.get(celda, 0.0) + float(valor)

    pop = pd.DataFrame({"hex_id": list(acumulado), "pop_count": list(acumulado.values())})
    log.info("Hexagonos con poblacion > 0: %s", f"{len(pop):,}")
    return pop


def pop_cache_ref(step, resolution):
    """Ruta del cache de poblacion por hexagono, junto a las capas del pais."""
    geodata = step.get("geodata")
    if not geodata:
        return None
    return {
        "container_name": geodata["container_name"],
        "file_path": posixpath.join(
            geodata["prefix"].rstrip("/"), f"pop_by_hex_res{resolution}.parquet"
        ),
    }


def population_by_hex_cached(step, capas, resolution):
    """pop_count por hexagono, calculado una vez por pais y reutilizado despues.

    El agregado raster -> H3 depende solo del pais y de la resolucion, no del
    embotellador ni del mes: con Brasil son 16 minutos y 2.510 millones de pixeles
    que daban el mismo resultado en cada corrida. Se guarda como parquet junto a las
    capas; borrarlo fuerza el recalculo (por ejemplo si cambia el raster).
    """
    ref = pop_cache_ref(step, resolution)

    if ref and az.blob_exists(ref["container_name"], ref["file_path"]):
        pop = az.read_parquet_blob(ref["file_path"], ref["container_name"])
        log.info("Poblacion por hexagono: cache reutilizado (%s hexagonos)", f"{len(pop):,}")
        return pop

    log.info("Calculando poblacion por hexagono H3....")
    pop = population_by_hex(read_blob_bytes(capas["population_raster"]), resolution)

    if ref:
        az.write_blob_parquet(ref["file_path"], ref["container_name"], pop)
        log.info("Cache de poblacion guardado en %s", ref["file_path"])
    return pop


def nearest_distance(pois, layer, column, use_centroid=False):
    """Distancia en metros de cada POI al elemento mas cercano de la capa."""
    layer = layer[["geometry"]].to_crs(pois.crs)
    if use_centroid:
        # normaliza paradas/estaciones en poligono o linea a un punto
        layer["geometry"] = layer.geometry.centroid

    nearest = gpd.sjoin_nearest(pois, layer, distance_col=column, how="left")
    nearest = nearest[~nearest.index.duplicated(keep="first")]
    return nearest[column]


def run(config, priority=None):
    step = config["step2_geo"]
    params = step["params"]
    resolution = params["h3_resolution"]
    labels = tuple(params["tertile_labels"])

    log.info("=== PASO 2 - Enriquecimiento geografico ===")
    capas = resolve_inputs(step)

    if priority is None:
        source = resolve_step2_input(config)
        priority = load_data(source["file_path"], source["container_name"])
    priority = priority.reset_index(drop=True)

    coord_mask = priority["latitude"].notna() & priority["longitude"].notna()
    if (~coord_mask).any():
        log.warning("POIs sin coordenadas (quedan en NaN): %s", f"{(~coord_mask).sum():,}")

    priority["hex_id"] = pd.Series(
        [
            h3.latlng_to_cell(lat, lon, resolution)
            for lat, lon in zip(
                priority.loc[coord_mask, "latitude"], priority.loc[coord_mask, "longitude"]
            )
        ],
        index=priority.index[coord_mask],
    )

    pop = population_by_hex_cached(step, capas, resolution)
    priority = priority.merge(pop, on="hex_id", how="left")
    # pop_count=0 solo para hexagonos validos sin poblacion registrada
    priority.loc[coord_mask, "pop_count"] = priority.loc[coord_mask, "pop_count"].fillna(0)
    priority["pop_density"] = assign_tertiles(priority["pop_count"], ascending=True, labels=labels)

    pois = gpd.GeoDataFrame(
        index=priority.index[coord_mask],
        geometry=gpd.points_from_xy(
            priority.loc[coord_mask, "longitude"], priority.loc[coord_mask, "latitude"]
        ),
        crs="EPSG:4326",
    ).to_crs(params["metric_crs"])

    log.info("Calculando distancia a la red vial principal....")
    roads = load_source(capas["roads"])
    priority["dist_to_main_road"] = (
        nearest_distance(pois, roads, "dist_to_main_road").reindex(priority.index).round(1)
    )
    priority["car_mobility"] = assign_tertiles(
        priority["dist_to_main_road"], ascending=False, labels=labels
    )

    log.info("Calculando distancia a la parada de colectivo mas cercana....")
    bus_stops = load_source(capas["bus_stops"])
    priority["dist_to_bus_stop"] = (
        nearest_distance(pois, bus_stops, "dist_to_bus_stop", use_centroid=True)
        .reindex(priority.index)
        .round(1)
    )
    priority["walk_mobility"] = assign_tertiles(
        priority["dist_to_bus_stop"], ascending=False, labels=labels
    )

    for column in ("pop_density", "car_mobility", "walk_mobility"):
        log.info("%s:", column)
        conteo = priority[column].value_counts(dropna=False)
        for valor, cantidad in conteo.items():
            log.info("  %-10s %8s  (%.2f%%)", valor, f"{cantidad:,}", cantidad / len(priority) * 100)

    save_csv(priority, step["output"])
    return priority
