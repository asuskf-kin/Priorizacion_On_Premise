"""Sube a Azure las capas geograficas que genera la skill `country-geodata`.

    uv run python scripts/upload_geodata.py Peru --iso per \
        --prefix bronze/providers/dataplor/lc/peru/WorldPop_Overpass/

Renombra los archivos a la convencion del datalake (<iso>_red_vial_overpass_turbo,
<iso>_parada_bus_overpass_turbo) e imprime el bloque YAML para el config.
Por defecto no pisa lo que ya existe: usa --force para resubir.
"""

import argparse
import glob
import os
import posixpath
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import azure_utils as az
from pipeline.geodata import LAYERS, classify, discover


def nombre_final(archivo, iso):
    """Nombre en Azure segun la capa: el .tif conserva el suyo (trae ano y release)."""
    capa = classify(archivo)
    if capa == "population_raster":
        return archivo
    if capa == "roads":
        return f"{iso}_red_vial_overpass_turbo.geojson"
    if capa == "bus_stops":
        return f"{iso}_parada_bus_overpass_turbo.geojson"
    return None


def subir(folder, prefix, iso, container="prospecting-data", solo=None, force=False):
    """Sube las capas de `folder`. `solo` limita a ciertas capas; devuelve {capa: blob}."""
    existentes = {} if force else discover(container, prefix)
    subidas = {}

    for local in sorted(glob.glob(os.path.join(folder, "*"))):
        archivo = os.path.basename(local)
        capa = classify(archivo)
        if not capa or (solo and capa not in solo):
            continue
        if capa in existentes and not force:
            print(f"  {capa}: ya existe, no se resube -> {existentes[capa]}")
            subidas[capa] = existentes[capa]
            continue

        blob = posixpath.join(prefix.rstrip("/"), nombre_final(archivo, iso))
        print(f"  Subiendo {archivo} ({os.path.getsize(local)/1e6:,.1f} MB) -> {container}/{blob}")
        az.write_blob_file(blob, container, local)
        subidas[capa] = blob

    print("\nBloque para el config (step2_geo.geodata):\n")
    print(f"    geodata:\n      prefix: {prefix}\n      container_name: {container}")
    return subidas


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", help="carpeta local generada por country-geodata")
    parser.add_argument("--prefix", required=True, help="carpeta destino en el contenedor")
    parser.add_argument("--iso", required=True, help="prefijo iso3 en minusculas (per, arg, bol...)")
    parser.add_argument("--container", default="prospecting-data")
    parser.add_argument("--force", action="store_true", help="resubir aunque ya exista")
    args = parser.parse_args()
    subir(args.folder, args.prefix, args.iso, args.container, force=args.force)


if __name__ == "__main__":
    main()
