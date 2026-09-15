"""Capas OSM de paises que Geofabrik solo publica partidos en subregiones.

Brasil (y tambien EEUU, Rusia, Canada...) no tiene `<pais>-latest-free.shp.zip`: el
extracto shapefile existe solo por subregion. Este script las une en un archivo por
capa, deduplicando por osm_id, y deja los mismos nombres que genera la skill
`country-geodata`, para que scripts/upload_geodata.py los reconozca igual.

    uv run python scripts/geodata_subregiones.py --country Brazil --out ./Brasil

Se escribe feature por feature: nunca hay mas de una subregion en memoria.
"""

import argparse
import json
import os
import sys
import unicodedata

import requests

SKILL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     ".claude", "skills", "country-geodata", "scripts")
sys.path.insert(0, SKILL)

import geofabrik  # noqa: E402

ROAD_PROFILES = {
    "trunk": ["motorway", "trunk", "primary", "secondary"],
    "standard": ["motorway", "trunk", "primary", "secondary", "tertiary"],
    "all": ["motorway", "trunk", "primary", "secondary", "tertiary",
            "unclassified", "residential"],
}
ROAD_LINKS = ["motorway_link", "trunk_link", "primary_link", "secondary_link"]


def slug(texto):
    normal = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return normal.lower().replace(" ", "_")


def subregiones(country, session):
    """URLs de los shp.zip de las subregiones de un pais que no tiene extracto propio."""
    idx = session.get(geofabrik.INDEX_URL, headers={"User-Agent": geofabrik.UA}, timeout=180).json()
    props = [f["properties"] or {} for f in idx["features"]]

    padres = [p for p in props if p.get("name", "").lower() == country.lower()]
    if not padres:
        raise LookupError(f"Geofabrik no conoce '{country}'")
    padre = padres[0]["id"]

    hijos = [(p["id"], (p.get("urls") or {}).get("shp")) for p in props if p.get("parent") == padre]
    con_shp = [(i, u) for i, u in hijos if u]
    if not con_shp:
        raise LookupError(f"'{country}' no tiene subregiones con shapefile en Geofabrik")
    return padre, sorted(con_shp)


class StreamGeoJSON:
    """Escribe una FeatureCollection sin mantener las features en memoria."""

    def __init__(self, path, name):
        self.path = path
        self.n = 0
        self.f = open(path, "w", encoding="utf-8")
        self.f.write('{"type":"FeatureCollection","name":"%s",'
                     '"crs":{"type":"name","properties":{"name":"urn:ogc:def:crs:OGC:1.3/CRS84"}},'
                     '"features":[' % name)

    def add(self, feature):
        if self.n:
            self.f.write(",")
        json.dump(feature, self.f, ensure_ascii=False)
        self.n += 1

    def close(self):
        self.f.write("]}")
        self.f.close()
        return self.n, os.path.getsize(self.path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", required=True, help="nombre del pais en Geofabrik, ej: Brazil")
    parser.add_argument("--out", required=True, help="carpeta destino")
    parser.add_argument("--nombre", help="slug para los archivos (default: el del pais)")
    parser.add_argument("--roads", default="standard", choices=list(ROAD_PROFILES))
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    nombre = slug(args.nombre or args.country)
    session = requests.Session()

    padre, regiones = subregiones(args.country, session)
    print(f"{args.country}: sin extracto de pais, se unen {len(regiones)} subregiones de '{padre}'")
    for rid, _ in regiones:
        print(f"   - {rid}")

    vias = StreamGeoJSON(os.path.join(args.out, f"{nombre}_carreteras_principales_osm.geojson"),
                         f"{nombre}_carreteras_principales")
    paradas = StreamGeoJSON(os.path.join(args.out, f"{nombre}_paradas_buses_osm.geojson"),
                            f"{nombre}_paradas_buses")
    vistos_vias, vistos_paradas, por_clase = set(), set(), {}

    for rid, url in regiones:
        print(f"\n[{rid}] {url.rsplit('/', 1)[-1]}")
        zf, how = geofabrik.open_extract(url, cache_dir=args.out, session=session)
        try:
            feats, by_class = geofabrik.roads(zf, ROAD_PROFILES[args.roads], ROAD_LINKS)
            nuevas = 0
            for osm_id, feat in feats.items():
                if osm_id in vistos_vias:
                    continue
                vistos_vias.add(osm_id)
                vias.add(feat)
                nuevas += 1
            for clase, n in by_class.items():
                por_clase[clase] = por_clase.get(clase, 0) + n
            print(f"   vias: {len(feats):,} en el extracto, {nuevas:,} nuevas")
            del feats

            stops, _ = geofabrik.bus_stops(zf)
            nuevas = 0
            for key, feat in stops.items():
                if key in vistos_paradas:
                    continue
                vistos_paradas.add(key)
                paradas.add(feat)
                nuevas += 1
            print(f"   paradas: {len(stops):,} en el extracto, {nuevas:,} nuevas")
            del stops
        finally:
            zf.close()

    n_vias, mb_vias = vias.close()
    n_par, mb_par = paradas.close()
    print(f"\n{'=' * 60}")
    print(f"vias   : {n_vias:,} tramos  ({mb_vias / 1e6:,.1f} MB)")
    print(f"paradas: {n_par:,} paradas ({mb_par / 1e6:,.1f} MB)")
    print("por clase:", dict(sorted(por_clase.items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    main()
