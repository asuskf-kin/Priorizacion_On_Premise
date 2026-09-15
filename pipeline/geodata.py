"""Capas geograficas por pais: se descargan una sola vez y se reutilizan.

Las capas (red vial, paradas de bus, raster de poblacion) son del pais entero, no
de una corrida: sirven igual para cualquier embotellador y cualquier mes. Por eso
viven en `<region>/<pais>/WorldPop_Overpass/` y este modulo las descubre ahi en vez
de exigir que cada config repita las tres rutas.
"""

import logging

import azure_utils as az

log = logging.getLogger(__name__)

LAYERS = ("population_raster", "roads", "bus_stops")


def classify(nombre):
    """A que capa corresponde un archivo, por su nombre."""
    nombre = nombre.lower()
    if nombre.endswith(".tif"):
        return "population_raster"
    if not nombre.endswith(".geojson"):
        return None
    if "red_vial" in nombre or "carretera" in nombre or "road" in nombre:
        return "roads"
    if "parada" in nombre or "bus" in nombre:
        return "bus_stops"
    return None


def discover(container_name, prefix):
    """Busca las capas ya publicadas bajo un prefijo. Devuelve {capa: file_path}."""
    encontradas = {}
    for blob in az.list_blob_files(container_name, prefix.rstrip("/") + "/"):
        capa = classify(blob.rsplit("/", 1)[-1])
        if capa and capa not in encontradas:
            encontradas[capa] = blob
    return encontradas


def resolve_inputs(step2):
    """Rutas finales de las 3 capas: lo declarado en el config o lo que haya en Azure.

    Asi una corrida nueva del mismo pais (otro embotellador, otro mes) solo declara
    `geodata.prefix` y reusa las capas ya subidas, sin volver a descargar nada.
    """
    inputs = dict(step2.get("inputs") or {})
    geodata = step2.get("geodata")

    faltan = [c for c in LAYERS if not inputs.get(c)]
    if not faltan:
        return inputs

    if not geodata:
        raise ValueError(
            f"Faltan las capas {faltan} en step2_geo.inputs y no hay un bloque "
            "step2_geo.geodata (prefix + container_name) para resolverlas."
        )

    container = geodata["container_name"]
    prefix = geodata["prefix"]
    disponibles = discover(container, prefix)
    log.info("Capas reutilizadas de '%s/%s': %s", container, prefix, sorted(disponibles))

    sin_resolver = [c for c in faltan if c not in disponibles]
    if sin_resolver:
        raise ValueError(
            f"No encontre {sin_resolver} en '{container}/{prefix}'. "
            "Generalas con: uv run python scripts/ensure_geodata.py --help"
        )

    for capa in faltan:
        inputs[capa] = {"file_path": disponibles[capa], "container_name": container}
    return inputs
