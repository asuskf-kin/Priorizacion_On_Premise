#!/usr/bin/env python3
"""Descarga datos geoespaciales base de un pais.

  1. Carreteras principales  (OpenStreetMap: Overpass API o Geofabrik) -> GeoJSON LineString
  2. Paradas de buses        (OpenStreetMap: Overpass API o Geofabrik) -> GeoJSON Point
  3. Poblacion 100 m         (WorldPop hub, con URL directa de respaldo) -> GeoTIFF

Las capas OSM tienen dos fuentes intercambiables. Por defecto (`--source auto`) se pide a
Overpass y, si esta saturado o la consulta no entra, se cae solo al extracto diario de
Geofabrik, que es una descarga estatica y por lo tanto no se satura.

Uso:
  python country_geodata.py "Bolivia"
  python country_geodata.py "Uruguay" --out ./Uruguay --roads trunk
  python country_geodata.py "Peru" --skip pop
  python country_geodata.py "Brasil" --source geofabrik
  python country_geodata.py "India" --year 2025 --max-depth 4

Solo requiere `requests`. Sin geopandas / rasterio / GDAL.
"""

import argparse
import json
import os
import re
import struct
import sys
import time
import unicodedata
from datetime import date, datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import geofabrik  # noqa: E402  (vive junto a este script)

UA = "country-geodata-skill/1.1 (OSM via Overpass|Geofabrik + WorldPop)"

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.osm.jp/api/interpreter",
]

ROAD_PROFILES = {
    "trunk":    ["motorway", "trunk", "primary", "secondary"],
    "standard": ["motorway", "trunk", "primary", "secondary", "tertiary"],
    "all":      ["motorway", "trunk", "primary", "secondary", "tertiary",
                 "unclassified", "residential"],
}
ROAD_LINKS = ["motorway_link", "trunk_link", "primary_link", "secondary_link"]

# De mas nuevo/preferido a mas viejo. La lista viva se consulta igual, por si WorldPop
# publica una release nueva que todavia no esta aqui.
WORLDPOP_ALIASES = [
    "G2_CN_POP_R25A_100m",   # Individual countries 2015-2030, R2025A v1 (100 m)
    "G2_CN_POP_R24B_100m",   # Constrained 2015-2030, R2024B v1 (100 m)
    "G2_UC_POP_R24B_100m",   # Unconstrained 2015-2030, R2024B v1 (100 m)
    "G2_CN_POP_2024_100m",   # Constrained 2024 (100 m)
    "G2_UC_POP_2024_100m",   # Unconstrained 2024 (100 m)
    "wpgpunadj",             # Unconstrained 2000-2020 UN adjusted (100 m)
    "wpgp",                  # Unconstrained 2000-2020 (100 m)
    "cic2020_UNadj_100m",    # Constrained 2020 UN adjusted (100 m)
    "cic2020_100m",          # Constrained 2020 (100 m)
]


def log(msg=""):
    print(msg, flush=True)


def slug(text):
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9]+", "_", t).strip("_")
    return t or "pais"


def r6(x):
    return round(float(x), 6)


# --------------------------------------------------------------------------- pais


def resolve_country(name):
    """Nombre de pais -> relacion OSM, area id de Overpass, ISO3 y bbox."""
    url = "https://nominatim.openstreetmap.org/search"
    attempts = [
        {"country": name, "format": "jsonv2", "extratags": 1, "limit": 5},
        {"q": name, "format": "jsonv2", "extratags": 1, "limit": 5,
         "featuretype": "country"},
    ]
    for params in attempts:
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=60)
        r.raise_for_status()
        for item in r.json():
            if item.get("osm_type") != "relation":
                continue
            tags = item.get("extratags") or {}
            south, north, west, east = [float(v) for v in item["boundingbox"]]
            rel = int(item["osm_id"])
            return {
                "input": name,
                "display_name": item.get("display_name", name),
                "osm_relation_id": rel,
                "area_id": 3600000000 + rel,
                "iso2": tags.get("ISO3166-1:alpha2") or tags.get("ISO3166-1"),
                "iso3": tags.get("ISO3166-1:alpha3"),
                "bbox": (south, west, north, east),
            }
        time.sleep(1.1)  # politica de uso de Nominatim
    raise SystemExit(
        "No se encontro el pais '%s' en Nominatim. Revisa la ortografia o prueba con el "
        "nombre en ingles (p. ej. 'Ivory Coast' en vez de 'Costa de Marfil')." % name
    )


# ---------------------------------------------------------------------- overpass


class OverpassError(RuntimeError):
    """kind='rejected' -> el servidor contesto pero no pudo con la consulta (partirla ayuda).
    kind='unreachable' -> ningun espejo contesto (partirla no ayuda: hay que cambiar de fuente).
    """

    def __init__(self, message, kind="rejected"):
        RuntimeError.__init__(self, message)
        self.kind = kind


def overpass(query, tries=2, timeout=1800):
    """POST a Overpass probando espejos. Los 504/429 son rutina, no un fallo real."""
    last = None
    answered = False
    for attempt in range(1, tries + 1):
        for url in OVERPASS_MIRRORS:
            host = url.split("/")[2]
            try:
                r = requests.post(
                    url, data=query.encode("utf-8"),
                    headers={"Content-Type": "text/plain; charset=utf-8",
                             "User-Agent": UA},
                    timeout=timeout,
                )
                answered = True
                if r.status_code == 200 and r.text.lstrip().startswith("{"):
                    data = r.json()
                    remark = (data.get("remark") or "").lower()
                    if "timed out" in remark or "out of memory" in remark or "error" in remark:
                        log("      %s: %s" % (host, data["remark"].strip()[:90]))
                        last = data["remark"]
                        continue
                    log("      %s: OK (%.1f MB)" % (host, len(r.content) / 1e6))
                    return data
                log("      %s: HTTP %s" % (host, r.status_code))
                last = "HTTP %s" % r.status_code
            except Exception as exc:  # red, timeout, json invalido
                log("      %s: %s" % (host, type(exc).__name__))
                last = "%s: %s" % (type(exc).__name__, exc)
        if attempt < tries and answered:
            time.sleep(20)   # darle aire al servidor; si nadie contesto, no hay a quien
    raise OverpassError(str(last), "rejected" if answered else "unreachable")


def split_bbox(bbox):
    s, w, n, e = bbox
    ms, mw = (s + n) / 2.0, (w + e) / 2.0
    return [(s, w, ms, mw), (s, mw, ms, e), (ms, w, n, mw), (ms, mw, n, e)]


def fetch_tiled(build_query, area_id, bbox, sink, depth=0, max_depth=3):
    """Pide el pais entero; si Overpass no aguanta, parte el bbox en 4 y reintenta.

    Asi un pais chico se resuelve en una sola consulta y uno grande (Brasil, India)
    se descarga por cuadrantes sin que tengas que pensarlo.
    """
    label = ("pais completo" if depth == 0 else
             "tile %.2f,%.2f .. %.2f,%.2f" % bbox)
    log("   [%d] %s" % (depth, label))
    try:
        data = overpass(build_query(area_id, bbox))
    except OverpassError as exc:
        if exc.kind == "unreachable":
            # Ningun espejo contesto: partir la consulta solo multiplicaria la espera.
            raise
        if depth >= max_depth:
            raise OverpassError(
                "Overpass sigue fallando tras %d divisiones: %s" % (max_depth, exc),
                exc.kind)
        log("   [%d] demasiado grande/ocupado -> dividiendo en 4" % depth)
        for sub in split_bbox(bbox):
            fetch_tiled(build_query, area_id, sub, sink, depth + 1, max_depth)
        return
    sink(data.get("elements", []))


# ------------------------------------------------------------------- carreteras


def roads_query(classes):
    main = "|".join(classes)
    links = "|".join(ROAD_LINKS)

    def build(area_id, bbox):
        s, w, n, e = bbox
        return (
            "[out:json][timeout:1800];\n"
            "area(%d)->.a;\n"
            "(\n"
            '  way["highway"~"^(%s)$"](area.a)(%s,%s,%s,%s);\n'
            '  way["highway"~"^(%s)$"](area.a)(%s,%s,%s,%s);\n'
            ");\n"
            "out geom;\n"
        ) % (area_id, main, s, w, n, e, links, s, w, n, e)
    return build


def road_feature(el):
    coords = [[r6(p["lon"]), r6(p["lat"])] for p in el.get("geometry") or []]
    if len(coords) < 2:
        return None
    tags = dict(el.get("tags", {}))
    props = {"osm_id": el["id"], "osm_type": "way"}
    props.update(tags)
    return {
        "type": "Feature",
        "id": el["id"],
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": props,
    }


# ----------------------------------------------------------------------- buses


def bus_query(area_id, bbox):
    b = "(%s,%s,%s,%s)" % bbox
    return (
        "[out:json][timeout:1800];\n"
        "area(%d)->.a;\n"
        "(\n"
        '  node["highway"="bus_stop"](area.a)%s;\n'
        '  node["public_transport"="platform"]["bus"="yes"](area.a)%s;\n'
        '  node["public_transport"="stop_position"]["bus"="yes"](area.a)%s;\n'
        '  node["amenity"="bus_station"](area.a)%s;\n'
        '  way["public_transport"="platform"]["bus"="yes"](area.a)%s;\n'
        '  way["amenity"="bus_station"](area.a)%s;\n'
        ");\n"
        "out geom;\n"
    ) % (area_id, b, b, b, b, b, b)


def bus_feature(el):
    tags = dict(el.get("tags", {}))
    kind = el.get("type")
    if kind == "node":
        lon, lat, source = r6(el["lon"]), r6(el["lat"]), "node"
    elif kind == "way" and el.get("geometry"):
        g = el["geometry"]
        lon = r6(sum(p["lon"] for p in g) / len(g))
        lat = r6(sum(p["lat"] for p in g) / len(g))
        source = "way_centroid"
    else:
        return None
    if tags.get("amenity") == "bus_station":
        stop_kind = "bus_station"
    elif tags.get("highway") == "bus_stop":
        stop_kind = "bus_stop"
    else:
        stop_kind = tags.get("public_transport", "other")
    props = {"osm_id": el["id"], "osm_type": kind, "stop_kind": stop_kind,
             "geometry_source": source}
    props.update(tags)
    return {
        "type": "Feature",
        "id": "%s/%s" % (kind, el["id"]),
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
    }


def write_geojson(features, name, path):
    fc = {
        "type": "FeatureCollection",
        "name": name,
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3/CRS84"}},
        "features": features,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False)
    return os.path.getsize(path)


# -------------------------------------------------------------------- worldpop


def worldpop_aliases():
    aliases = list(WORLDPOP_ALIASES)
    try:
        r = requests.get("https://hub.worldpop.org/rest/data/pop",
                         headers={"User-Agent": UA}, timeout=60)
        for row in r.json().get("data", []):
            alias, name = row.get("alias"), (row.get("name") or "")
            if alias and "100m" in name.replace(" ", "") and alias not in aliases:
                aliases.append(alias)
    except Exception:
        pass
    return aliases


# Respaldo por si el REST del hub no responde: las rutas de data.worldpop.org siguen un
# patron fijo, asi que se pueden construir y verificar con un HEAD.
WORLDPOP_DIRECT = [
    ("G2_CN_POP_R25A_100m",
     "https://data.worldpop.org/GIS/Population/Global_2015_2030/R2025A/%(y)s/%(I)s/v1/"
     "100m/constrained/%(i)s_pop_%(y)s_CN_100m_R2025A_v1.tif"),
    ("G2_CN_POP_R24B_100m",
     "https://data.worldpop.org/GIS/Population/Global_2015_2030/R2024B/%(y)s/%(I)s/v1/"
     "100m/constrained/%(i)s_pop_%(y)s_CN_100m_R2024B_v1.tif"),
    ("G2_UC_POP_R24B_100m",
     "https://data.worldpop.org/GIS/Population/Global_2015_2030/R2024B/%(y)s/%(I)s/v1/"
     "100m/unconstrained/%(i)s_pop_%(y)s_UC_100m_R2024B_v1.tif"),
    ("wpgpunadj",
     "https://data.worldpop.org/GIS/Population/Global_2000_2020/%(y)s/%(I)s/"
     "%(i)s_ppp_%(y)s_UNadj.tif"),
    ("wpgp",
     "https://data.worldpop.org/GIS/Population/Global_2000_2020/%(y)s/%(I)s/"
     "%(i)s_ppp_%(y)s.tif"),
]


def worldpop_direct(iso3, want_year, back=12):
    """Arma las URLs conocidas y se queda con la primera que exista (HEAD 200)."""
    for year in range(want_year, want_year - back, -1):
        for alias, pattern in WORLDPOP_DIRECT:
            url = pattern % {"y": year, "I": iso3.upper(), "i": iso3.lower()}
            try:
                r = requests.head(url, headers={"User-Agent": UA}, timeout=60,
                                  allow_redirects=True)
            except Exception:
                continue
            if r.status_code == 200:
                return url, {
                    "alias": alias,
                    "year": year,
                    "requested_year": want_year,
                    "is_latest_available": None,
                    "years_available": [year],
                    "title": None,
                    "doi": None,
                    "citation": None,
                    "url": url,
                    "discovery": "url-directa (el REST del hub no respondio)",
                }
    return None, None


def worldpop_find(iso3, want_year):
    """Devuelve (url_tif, meta) del dataset 100 m mas reciente disponible."""
    for alias in worldpop_aliases():
        try:
            r = requests.get("https://hub.worldpop.org/rest/data/pop/%s" % alias,
                             params={"iso3": iso3}, headers={"User-Agent": UA},
                             timeout=120)
            rows = r.json().get("data", [])
        except Exception:
            continue
        by_year = {}
        for row in rows:
            tifs = [f for f in (row.get("files") or []) if f.lower().endswith(".tif")]
            if not tifs:
                continue
            try:
                by_year[int(row["popyear"])] = (row, tifs[0])
            except (KeyError, TypeError, ValueError):
                continue
        if not by_year:
            continue
        if want_year in by_year:
            year = want_year
        else:
            past = [y for y in by_year if y <= want_year]
            year = max(past) if past else max(by_year)
        row, url = by_year[year]
        return url, {
            "alias": alias,
            "year": year,
            "requested_year": want_year,
            "is_latest_available": year == max(by_year),
            "years_available": sorted(by_year),
            "title": row.get("title"),
            "doi": row.get("doi"),
            "citation": row.get("citation"),
            "url": url,
            "discovery": "REST del hub",
        }
    log("   el REST del hub no devolvio nada; probando URLs directas")
    url, meta = worldpop_direct(iso3, want_year)
    if url:
        return url, meta
    raise SystemExit(
        "WorldPop no tiene producto 100 m para ISO3 '%s'. Revisa el codigo de pais en "
        "https://hub.worldpop.org/ o usa --skip pop." % iso3
    )


def download(url, dest, tries=3):
    for attempt in range(1, tries + 1):
        try:
            with requests.get(url, stream=True, timeout=(30, 300),
                              headers={"User-Agent": UA}) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or 0)
                tmp, done, mark = dest + ".part", 0, 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                        done += len(chunk)
                        if done - mark >= 25 << 20:
                            mark = done
                            pct = (" (%.0f%%)" % (done * 100.0 / total)) if total else ""
                            log("      %.0f MB%s" % (done / 1e6, pct))
            os.replace(tmp, dest)
            return os.path.getsize(dest)
        except Exception as exc:
            log("      intento %d fallo: %s: %s" % (attempt, type(exc).__name__, exc))
            if attempt == tries:
                raise
            time.sleep(15)


def tiff_info(path):
    """Lee cabecera (Big)TIFF sin GDAL: tamano, tipo, escala de pixel, esquina NW."""
    try:
        with open(path, "rb") as f:
            bo = "<" if f.read(2) == b"II" else ">"
            ver = struct.unpack(bo + "H", f.read(2))[0]
            big = ver == 43
            if big:
                f.read(4)
                off = struct.unpack(bo + "Q", f.read(8))[0]
            else:
                off = struct.unpack(bo + "I", f.read(4))[0]
            f.seek(off)
            n = struct.unpack(bo + ("Q" if big else "H"), f.read(8 if big else 2))[0]
            sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 11: 4, 12: 8, 16: 8}
            tags = {}
            for _ in range(n):
                rec = f.read(20 if big else 12)
                tag, typ = struct.unpack(bo + "HH", rec[:4])
                cnt = struct.unpack(bo + ("Q" if big else "I"),
                                    rec[4:12] if big else rec[4:8])[0]
                raw = rec[12:] if big else rec[8:]
                total = cnt * sizes.get(typ, 1)
                if total <= len(raw):
                    data = raw[:total]
                else:
                    ptr = struct.unpack(bo + ("Q" if big else "I"),
                                        raw[:8] if big else raw[:4])[0]
                    here = f.tell()
                    f.seek(ptr)
                    data = f.read(total)
                    f.seek(here)
                if typ == 12:
                    tags[tag] = struct.unpack(bo + "%dd" % cnt, data)
                elif typ in (3, 4, 16):
                    code = {3: "H", 4: "I", 16: "Q"}[typ]
                    tags[tag] = struct.unpack(bo + "%d" % cnt + code, data)
                else:
                    tags[tag] = data[:40]
            scale = tags.get(33550)
            tie = tags.get(33922)
            nodata = tags.get(42113)
            return {
                "format": "BigTIFF" if big else "TIFF",
                "width": tags.get(256, [None])[0],
                "height": tags.get(257, [None])[0],
                "bits_per_sample": tags.get(258, [None])[0],
                "pixel_size_deg": scale[0] if scale else None,
                "origin_lon": tie[3] if tie and len(tie) > 4 else None,
                "origin_lat": tie[4] if tie and len(tie) > 4 else None,
                "nodata": (nodata.split(b"\x00")[0].decode(errors="ignore")
                           if isinstance(nodata, (bytes, bytearray)) else None),
            }
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


# ---------------------------------------------------------------------- salida


def _compo(counter):
    return " · ".join("%s %s" % (k, "{:,}".format(v))
                      for k, v in sorted(counter.items(), key=lambda kv: -kv[1]))


def _source_block(entry, geofabrik_layers, geofabrik_attrs):
    """Como se obtuvo la capa: consulta Overpass exacta, o capa del extracto Geofabrik."""
    if entry.get("source") == "geofabrik":
        region = entry.get("geofabrik") or {}
        return [
            "- Fuente: **extracto Geofabrik** `%s` — %s"
            % (region.get("id", "?"), region.get("url", "")),
            "  (%s; el extracto se regenera a diario desde OSM)."
            % (entry.get("source_reason") or "fuente elegida"),
            "- Capas leidas: %s." % geofabrik_layers,
            "- Atributos: %s — **no** todas las etiquetas OSM. El extracto tambien incluye "
            "un margen alrededor de la frontera, asi que puede traer geometrias apenas "
            "fuera del pais." % geofabrik_attrs,
            "",
        ]
    return [
        "- Fuente: **OpenStreetMap via Overpass API** (mismo motor que "
        "https://overpass-turbo.eu/).",
        "",
        "```overpassql",
        entry["query"].strip(),
        "```",
        "",
    ]


def write_readme(path, ctx):
    c = ctx["country"]
    short = c["display_name"].split(",")[0]
    lines = ["# %s — datos geoespaciales base" % short, ""]
    lines.append("Descargados el %s con la skill `country-geodata`." % ctx["fecha"])
    lines.append("")
    lines.append("- Pais resuelto: **%s** (relacion OSM %s, ISO3 %s)"
                 % (c["display_name"], c["osm_relation_id"], c.get("iso3") or "?"))
    lines.append("- Area Overpass: `area(%s)` · bbox S,W,N,E = %s"
                 % (c["area_id"], tuple(c["bbox"])))
    lines.append("")
    if ctx.get("roads"):
        r = ctx["roads"]
        lines += [
            "## 1. Carreteras principales — `%s`" % os.path.basename(r["file"]),
            "",
            "- %s tramos (LineString), EPSG:4326, con `osm_id`."
            % "{:,}".format(r["count"]),
            "- Perfil `%s`: clases %s (+ enlaces `*_link`)."
            % (r["profile"], ", ".join(r["classes"])),
            "- Composicion por `highway`: %s" % _compo(r["by_class"]),
        ] + _source_block(
            r, "`gis_osm_roads_free_1`",
            "`osm_id`, `fclass`→`highway`, `name`, `ref`, `oneway`, `maxspeed`, "
            "`layer`, `bridge`, `tunnel`")
    if ctx.get("bus"):
        b = ctx["bus"]
        lines += [
            "## 2. Paradas de buses — `%s`" % os.path.basename(b["file"]),
            "",
            "- %s puntos, EPSG:4326. Campos anadidos: `osm_id`, `osm_type`, `stop_kind`, "
            "`geometry_source` (las terminales y andenes mapeados como poligono se "
            "convirtieron a su centroide)." % "{:,}".format(b["count"]),
            "- Composicion por `stop_kind`: %s" % _compo(b["by_kind"]),
        ] + _source_block(
            b,
            "`gis_osm_transport_free_1` y `gis_osm_transport_a_free_1`, filtrando "
            "`fclass` bus_stop / bus_station — el extracto no incluye "
            "`public_transport=platform/stop_position`, asi que el conteo es menor "
            "que por Overpass",
            "`osm_id`, `fclass`→`stop_kind`, `name`")
    if ctx.get("pop"):
        p = ctx["pop"]
        i = p.get("tiff") or {}
        year_note = ("" if p["year"] == p["requested_year"]
                     else " (se pidio %s; no disponible)" % p["requested_year"])
        lines += [
            "## 3. Poblacion WorldPop — `%s`" % os.path.basename(p["file"]),
            "",
            "- Fuente: https://hub.worldpop.org — dataset `%s` (hallado via %s)."
            % (p["alias"], p.get("discovery") or "REST del hub"),
            "- URL: %s" % p["url"],
            "- Ano **%s**%s. Anos en la serie: %s–%s."
            % (p["year"], year_note, p["years_available"][0], p["years_available"][-1]),
        ]
        if i and not i.get("error"):
            lines.append(
                "- GeoTIFF %s × %s px, EPSG:4326, pixel %s grados (~100 m), NoData = %s. "
                "Valores = personas por pixel."
                % ("{:,}".format(i["width"]), "{:,}".format(i["height"]),
                   i["pixel_size_deg"], i["nodata"]))
        if p["year"] > date.today().year:
            lines.append("- Ojo: ese ano es una **proyeccion** del modelo, no un censo.")
        lines.append("")
    lines += [
        "## Licencias",
        "",
        "- OSM: © colaboradores de OpenStreetMap, ODbL 1.0 — vale igual si los datos "
        "llegaron por Overpass o por un extracto de Geofabrik.",
        "- WorldPop: CC BY 4.0 (WorldPop, University of Southampton).",
        "",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ------------------------------------------------------------------------ main


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("country", help="Nombre del pais, p. ej. 'Bolivia' o 'Costa Rica'")
    ap.add_argument("--out", help="Carpeta destino (por defecto ./<Pais>)")
    ap.add_argument("--roads", choices=sorted(ROAD_PROFILES), default="standard",
                    help="Perfil de vias (default: standard = troncal + tertiary)")
    ap.add_argument("--source", choices=["auto", "overpass", "geofabrik"], default="auto",
                    help="Fuente de las capas OSM. auto = Overpass y, si falla, Geofabrik")
    ap.add_argument("--year", type=int, default=date.today().year,
                    help="Ano de poblacion WorldPop (default: ano actual)")
    ap.add_argument("--skip", default="", help="Capas a omitir: roads,bus,pop")
    ap.add_argument("--max-depth", type=int, default=3,
                    help="Divisiones maximas del bbox si Overpass se atora (default 3)")
    args = ap.parse_args(argv)

    skip = set(s.strip() for s in args.skip.split(",") if s.strip())
    log("Resolviendo '%s' en Nominatim..." % args.country)
    country = resolve_country(args.country)
    short = country["display_name"].split(",")[0]
    log("  -> %s" % country["display_name"])
    log("     relacion OSM %s | ISO3 %s | area(%s)"
        % (country["osm_relation_id"], country.get("iso3"), country["area_id"]))

    out = args.out or os.path.join(os.getcwd(), slug(short))
    os.makedirs(out, exist_ok=True)
    name = slug(short).lower()
    ctx = {"country": country, "fecha": date.today().isoformat(),
           "generated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    summary = []

    extract = {}

    def geofabrik_zip():
        """Abre el extracto del pais una sola vez y lo comparte entre capas."""
        if "zf" not in extract:
            region = geofabrik.find_region(country.get("iso2"), short)
            log("   Geofabrik: %s" % region["url"])
            zf, how = geofabrik.open_extract(region["url"], cache_dir=out)
            log("   extracto abierto %s" % how)
            extract.update(zf=zf, region=region, how=how)
        return extract["zf"]

    if "roads" not in skip:
        classes = ROAD_PROFILES[args.roads]
        log("\n[1/3] Carreteras (%s: %s)" % (args.roads, ", ".join(classes)))
        build = roads_query(classes)
        feats, by_class, used = {}, {}, None
        reason = "solicitado con --source geofabrik"

        def sink_roads(elements):
            for el in elements:
                if el.get("type") != "way" or el["id"] in feats:
                    continue
                feat = road_feature(el)
                if feat:
                    feats[el["id"]] = feat
                    hw = feat["properties"].get("highway")
                    by_class[hw] = by_class.get(hw, 0) + 1

        if args.source in ("auto", "overpass"):
            try:
                fetch_tiled(build, country["area_id"], country["bbox"], sink_roads,
                            max_depth=args.max_depth)
                used = "overpass"
            except OverpassError as exc:
                if args.source == "overpass":
                    raise
                log("   Overpass no pudo: %s" % str(exc)[:130])
                log("   -> cambiando a Geofabrik (extracto diario del pais)")
                reason = "Overpass %s: %s" % (
                    "inalcanzable" if exc.kind == "unreachable" else "no pudo",
                    str(exc)[:120])
                feats.clear()
                by_class.clear()
        if used is None:
            zf = geofabrik_zip()
            gf_feats, gf_by_class = geofabrik.roads(zf, classes, ROAD_LINKS)
            feats.update(gf_feats)
            by_class.update(gf_by_class)
            used = "geofabrik"

        path = os.path.join(out, "%s_carreteras_principales_osm.geojson" % name)
        size = write_geojson(list(feats.values()),
                             "%s_carreteras_principales" % name, path)
        ctx["roads"] = {"file": path, "count": len(feats), "by_class": by_class,
                        "profile": args.roads, "classes": classes, "source": used,
                        "source_reason": reason if used == "geofabrik" else None,
                        "query": build(country["area_id"], country["bbox"]),
                        "geofabrik": extract.get("region") if used == "geofabrik" else None}
        log("   %s tramos [%s] -> %s (%.1f MB)"
            % ("{:,}".format(len(feats)), used, os.path.basename(path), size / 1e6))
        summary.append((os.path.basename(path),
                        "%s tramos" % "{:,}".format(len(feats)), size))

    if "bus" not in skip:
        log("\n[2/3] Paradas de buses")
        stops, by_kind, used = {}, {}, None
        reason = "solicitado con --source geofabrik"

        def sink_bus(elements):
            for el in elements:
                key = (el.get("type"), el.get("id"))
                if key in stops:
                    continue
                feat = bus_feature(el)
                if feat:
                    stops[key] = feat
                    k = feat["properties"]["stop_kind"]
                    by_kind[k] = by_kind.get(k, 0) + 1

        if args.source in ("auto", "overpass"):
            try:
                fetch_tiled(bus_query, country["area_id"], country["bbox"], sink_bus,
                            max_depth=args.max_depth)
                used = "overpass"
            except OverpassError as exc:
                if args.source == "overpass":
                    raise
                log("   Overpass no pudo: %s" % str(exc)[:130])
                log("   -> cambiando a Geofabrik (extracto diario del pais)")
                reason = "Overpass %s: %s" % (
                    "inalcanzable" if exc.kind == "unreachable" else "no pudo",
                    str(exc)[:120])
                stops.clear()
                by_kind.clear()
        if used is None:
            zf = geofabrik_zip()
            gf_stops, gf_by_kind = geofabrik.bus_stops(zf)
            stops.update(gf_stops)
            by_kind.update(gf_by_kind)
            used = "geofabrik"

        path = os.path.join(out, "%s_paradas_buses_osm.geojson" % name)
        size = write_geojson(list(stops.values()), "%s_paradas_buses" % name, path)
        ctx["bus"] = {"file": path, "count": len(stops), "by_kind": by_kind,
                      "source": used,
                      "source_reason": reason if used == "geofabrik" else None,
                      "query": bus_query(country["area_id"], country["bbox"]),
                      "geofabrik": extract.get("region") if used == "geofabrik" else None}
        log("   %s paradas [%s] -> %s (%.1f MB)"
            % ("{:,}".format(len(stops)), used, os.path.basename(path), size / 1e6))
        summary.append((os.path.basename(path),
                        "%s paradas" % "{:,}".format(len(stops)), size))

    if "pop" not in skip:
        iso3 = country.get("iso3")
        if not iso3:
            raise SystemExit("No se pudo determinar el ISO3 del pais; usa --skip pop o "
                             "descarga WorldPop manualmente.")
        log("\n[3/3] WorldPop 100 m (%s, ano %s)" % (iso3, args.year))
        url, meta = worldpop_find(iso3, args.year)
        log("   dataset %s | ano %s" % (meta["alias"], meta["year"]))
        path = os.path.join(out, os.path.basename(url))
        size = download(url, path)
        info = tiff_info(path)
        meta.update({"file": path, "tiff": info})
        ctx["pop"] = meta
        px = info.get("pixel_size_deg")
        extra = (" | %sx%s px | pixel %s deg" % (info["width"], info["height"], px)
                 if px else "")
        log("   %.1f MB -> %s%s" % (size / 1e6, os.path.basename(path), extra))
        summary.append((os.path.basename(path), "WorldPop %s" % meta["year"], size))

    write_readme(os.path.join(out, "README.md"), ctx)
    with open(os.path.join(out, "_descarga_metadata.json"), "w", encoding="utf-8") as f:
        json.dump(ctx, f, ensure_ascii=False, indent=2)

    log("\n" + "=" * 70)
    log("Listo: %s" % out)
    for fname, what, size in summary:
        log("  %-52s %16s  %7.1f MB" % (fname, what, size / 1e6))
    log("  README.md / _descarga_metadata.json (fuentes, consultas, conteos)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
