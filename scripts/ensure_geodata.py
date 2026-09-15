"""Garantiza que las capas geograficas de un pais esten en Azure. Idempotente.

    uv run python scripts/ensure_geodata.py --country Peru --iso per \
        --prefix bronze/providers/dataplor/lc/peru/WorldPop_Overpass/

Si las tres capas ya estan publicadas no descarga nada: son datos del pais entero y
sirven para cualquier embotellador y cualquier mes. Solo baja (con la skill
`country-geodata`) las que falten.
"""

import argparse
import logging
import os
import subprocess
import sys
import tempfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

from pipeline.geodata import LAYERS, classify, discover  # noqa: E402

SKILL = os.path.join(RAIZ, ".claude", "skills", "country-geodata", "scripts", "country_geodata.py")
SKIP = {"roads": "roads", "bus_stops": "bus", "population_raster": "pop"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", required=True, help="nombre del pais, ej: Peru")
    parser.add_argument("--iso", required=True, help="prefijo iso3 en minusculas, ej: per")
    parser.add_argument("--prefix", required=True, help="carpeta destino en el contenedor")
    parser.add_argument("--container", default="prospecting-data")
    parser.add_argument("--roads", default="standard", choices=["trunk", "standard", "all"])
    parser.add_argument("--source", default="auto", choices=["auto", "overpass", "geofabrik"],
                        help="de donde salen las capas OSM (auto: Overpass y si falla Geofabrik)")
    parser.add_argument("--force", action="store_true", help="rehacer aunque ya existan")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for ruidoso in ("azure", "urllib3"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)

    existentes = {} if args.force else discover(args.container, args.prefix)
    faltantes = [c for c in LAYERS if c not in existentes]

    for capa in LAYERS:
        estado = f"ya existe -> {existentes[capa]}" if capa in existentes else "FALTA"
        print(f"  {capa:18s} {estado}")

    if not faltantes:
        print(f"\nNada que hacer: {args.country} ya tiene sus tres capas en "
              f"{args.container}/{args.prefix}")
        return 0

    print(f"\nDescargando {faltantes} para {args.country}...")
    with tempfile.TemporaryDirectory() as tmp:
        destino = os.path.join(tmp, args.country)
        comando = [sys.executable, SKILL, args.country, "--out", destino,
                   "--roads", args.roads, "--source", args.source]
        omitir = [SKIP[c] for c in LAYERS if c in existentes]
        if omitir:
            comando += ["--skip", ",".join(omitir)]

        subprocess.run(comando, check=True)

        from scripts.upload_geodata import subir  # noqa: E402
        subir(destino, args.prefix, args.iso, args.container, solo=faltantes)

    return 0


if __name__ == "__main__":
    sys.exit(main())
