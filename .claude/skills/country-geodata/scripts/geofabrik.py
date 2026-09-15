#!/usr/bin/env python3
"""Fuente alternativa a Overpass: extractos shapefile de Geofabrik.

Geofabrik publica cada dia un extracto por pais en `<pais>-latest-free.shp.zip`.
Es infraestructura distinta (descarga estatica, no un servidor de consultas), asi que
sirve justo cuando Overpass esta saturado o la consulta es demasiado grande.

Lo que cambia respecto de Overpass, y hay que decirlo al usuario:

- Los shapefiles de Geofabrik traen un subconjunto fijo de atributos (osm_id, fclass,
  name, ref, oneway, maxspeed, bridge, tunnel), no todas las etiquetas OSM.
- La capa de transporte cubre `highway=bus_stop` y `amenity=bus_station`, pero no los
  `public_transport=platform/stop_position` del esquema nuevo, asi que suele dar menos
  paradas que Overpass.
- El extracto esta recortado con un margen alrededor de la frontera: puede incluir
  tramos apenas fuera del pais.

Sin dependencias: lee el ZIP por rangos HTTP (sin bajarlo entero cuando el servidor lo
permite) y parsea SHP/DBF a mano.
"""

import io
import json
import os
import struct
import zipfile

import requests

INDEX_URL = "https://download.geofabrik.de/index-v1.json"
UA = "country-geodata-skill/1.1 (Geofabrik extract reader)"

ROADS_LAYER = "gis_osm_roads_free_1"
TRANSPORT_POINTS = "gis_osm_transport_free_1"
TRANSPORT_AREAS = "gis_osm_transport_a_free_1"


def log(msg=""):
    print(msg, flush=True)


# ------------------------------------------------------------------ indice


def find_region(iso2, country_name=None, session=None):
    """ISO2 -> url del shp.zip del pais en Geofabrik.

    Se usa el indice oficial en vez de adivinar la ruta porque Geofabrik agrupa por
    continente y algunos paises cambian de carpeta o comparten extracto con vecinos.
    """
    session = session or requests.Session()
    idx = session.get(INDEX_URL, headers={"User-Agent": UA}, timeout=180).json()
    by_iso, by_name = [], []
    for feat in idx.get("features", []):
        p = feat.get("properties") or {}
        url = (p.get("urls") or {}).get("shp")
        if not url:
            continue
        if iso2 and iso2.upper() in (p.get("iso3166-1:alpha2") or []):
            by_iso.append((p, url))
        elif country_name and p.get("name", "").lower() == country_name.lower():
            by_name.append((p, url))
    hits = by_iso or by_name
    if not hits:
        raise LookupError(
            "Geofabrik no publica un extracto shapefile para '%s' (%s). Algunos paises "
            "solo estan dentro del extracto de su region." % (country_name, iso2))
    # El extracto mas especifico es el que menos subdivisiones tiene: preferimos el que
    # declara exactamente este pais.
    hits.sort(key=lambda h: len(h[0].get("iso3166-1:alpha2") or []))
    props, url = hits[0]
    return {"id": props.get("id"), "name": props.get("name"), "url": url,
            "parent": props.get("parent")}


# ------------------------------------------------- archivo remoto por rangos


class HttpRangeFile(io.RawIOBase):
    """Archivo de solo lectura sobre HTTP con Range, para abrir un ZIP sin bajarlo entero.

    zipfile necesita poder posicionarse: con esto leemos el directorio central al final
    del archivo y luego solo los miembros que interesan (vias y transporte), que en un
    extracto grande son una fraccion del total.
    """

    def __init__(self, url, session=None, chunk=8 << 20):
        self.session = session or requests.Session()
        head = self.session.head(url, headers={"User-Agent": UA},
                                 allow_redirects=True, timeout=120)
        head.raise_for_status()
        self.url = head.url
        self.size = int(head.headers.get("Content-Length") or 0)
        self.ranges_ok = head.headers.get("Accept-Ranges", "").lower() == "bytes"
        if not self.size or not self.ranges_ok:
            raise OSError("el servidor no soporta descargas por rango")
        self.chunk = chunk
        self.pos = 0
        self._buf = b""
        self._buf_start = -1
        self.bytes_fetched = 0

    # --- interfaz de archivo
    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        else:
            self.pos = self.size + offset
        self.pos = max(0, min(self.pos, self.size))
        return self.pos

    def _fetch(self, start, length):
        end = min(start + length, self.size) - 1
        if end < start:
            return b""
        r = self.session.get(self.url, headers={"User-Agent": UA,
                                                "Range": "bytes=%d-%d" % (start, end)},
                             timeout=300)
        r.raise_for_status()
        self.bytes_fetched += len(r.content)
        return r.content

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""
        if self._buf_start <= self.pos and self.pos + n <= self._buf_start + len(self._buf):
            off = self.pos - self._buf_start
            self.pos += n
            return self._buf[off:off + n]
        want = max(n, self.chunk)
        self._buf = self._fetch(self.pos, want)
        self._buf_start = self.pos
        off = 0
        data = self._buf[off:off + n]
        self.pos += len(data)
        return data

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)


def open_extract(url, cache_dir=None, session=None):
    """Devuelve (zipfile.ZipFile, descripcion_de_como_se_abrio)."""
    session = session or requests.Session()
    try:
        remote = HttpRangeFile(url, session=session)
        zf = zipfile.ZipFile(remote)
        return zf, "por rangos HTTP (%.0f MB en el servidor)" % (remote.size / 1e6)
    except Exception as exc:
        log("      sin soporte de rangos (%s); descargando el zip completo"
            % type(exc).__name__)
    dest = os.path.join(cache_dir or ".", os.path.basename(url))
    if not os.path.exists(dest):
        with session.get(url, stream=True, headers={"User-Agent": UA},
                         timeout=(30, 300)) as r:
            r.raise_for_status()
            done, mark = 0, 0
            with open(dest + ".part", "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if done - mark >= 50 << 20:
                        mark = done
                        log("      %.0f MB" % (done / 1e6))
        os.replace(dest + ".part", dest)
    return zipfile.ZipFile(dest), "zip descargado a %s" % dest


# ------------------------------------------------------------ lectura SHP/DBF


def _dbf_records(data):
    """Parsea un DBF (dBase III) completo en memoria -> lista de dicts."""
    n_records, header_len, record_len = struct.unpack("<IHH", data[4:12])
    fields, off = [], 32
    while data[off] != 0x0D:
        name = data[off:off + 11].split(b"\x00")[0].decode("latin-1")
        ftype = chr(data[off + 11])
        flen = data[off + 16]
        fields.append((name, ftype, flen))
        off += 32
    out = []
    pos = header_len
    for _ in range(n_records):
        row = data[pos:pos + record_len]
        pos += record_len
        if not row or row[:1] == b"*":       # registro borrado
            out.append(None)
            continue
        rec, cur = {}, 1
        for name, ftype, flen in fields:
            raw = row[cur:cur + flen]
            cur += flen
            val = raw.decode("utf-8", "replace").strip()
            if ftype == "N" and val:
                try:
                    val = float(val) if "." in val else int(val)
                except ValueError:
                    pass
            rec[name] = val if val != "" else None
        out.append(rec)
    return out


def _shapes(stream, r6):
    """Itera las geometrias de un .shp -> (tipo_geojson, coordenadas) o None."""
    header = stream.read(100)
    if len(header) < 100:
        return
    while True:
        rec_header = stream.read(8)
        if len(rec_header) < 8:
            return
        _, content_words = struct.unpack(">ii", rec_header)
        content = stream.read(content_words * 2)
        if len(content) < 4:
            return
        shape_type = struct.unpack("<i", content[:4])[0]
        if shape_type == 0:                       # null
            yield None
        elif shape_type in (1, 11, 21):           # point
            x, y = struct.unpack("<dd", content[4:20])
            yield ("Point", [r6(x), r6(y)])
        elif shape_type in (3, 5, 13, 15, 23, 25):  # polyline / polygon
            n_parts, n_points = struct.unpack("<ii", content[36:44])
            p = 44
            parts = list(struct.unpack("<%di" % n_parts, content[p:p + 4 * n_parts]))
            p += 4 * n_parts
            pts = struct.unpack("<%dd" % (2 * n_points), content[p:p + 16 * n_points])
            rings, parts_end = [], parts[1:] + [n_points]
            for start, end in zip(parts, parts_end):
                ring = [[r6(pts[2 * i]), r6(pts[2 * i + 1])] for i in range(start, end)]
                if len(ring) >= 2:
                    rings.append(ring)
            if not rings:
                yield None
            elif shape_type in (3, 13, 23):
                yield ("LineString", rings[0]) if len(rings) == 1 else ("MultiLineString", rings)
            else:
                yield ("Polygon", rings)
        else:
            yield None


def read_layer(zf, layer, r6=lambda v: round(float(v), 6)):
    """Recorre una capa del extracto -> (geometria, atributos) registro a registro."""
    names = {os.path.basename(n): n for n in zf.namelist()}
    shp, dbf = names.get(layer + ".shp"), names.get(layer + ".dbf")
    if not shp or not dbf:
        raise LookupError("el extracto no trae la capa %s" % layer)
    with zf.open(dbf) as f:
        attrs = _dbf_records(f.read())
    with zf.open(shp) as f:
        stream = io.BufferedReader(f, buffer_size=1 << 22)
        for i, geom in enumerate(_shapes(stream, r6)):
            if geom is None or i >= len(attrs) or attrs[i] is None:
                continue
            yield geom, attrs[i]


# ----------------------------------------------------------------- capas


def _centroid(geom_type, coords):
    if geom_type == "Point":
        return coords
    pts = coords if geom_type == "LineString" else [p for ring in coords for p in ring]
    if not pts:
        return None
    return [round(sum(p[0] for p in pts) / len(pts), 6),
            round(sum(p[1] for p in pts) / len(pts), 6)]


def roads(zf, classes, links):
    """Vias del extracto filtradas por fclass -> (features, conteo por clase)."""
    wanted = set(classes) | set(links)
    feats, by_class = {}, {}
    for (gtype, coords), a in read_layer(zf, ROADS_LAYER):
        fclass = a.get("fclass")
        if fclass not in wanted or gtype not in ("LineString", "MultiLineString"):
            continue
        osm_id = a.get("osm_id")
        try:
            osm_id = int(osm_id)
        except (TypeError, ValueError):
            continue
        if osm_id in feats:
            continue
        props = {"osm_id": osm_id, "osm_type": "way", "highway": fclass,
                 "source_layer": "geofabrik:" + ROADS_LAYER}
        for key, field in (("name", "name"), ("ref", "ref"), ("oneway", "oneway"),
                           ("maxspeed", "maxspeed"), ("layer", "layer")):
            if a.get(field) not in (None, "", 0):
                props[key] = a[field]
        for key in ("bridge", "tunnel"):
            if a.get(key) in ("T", "t", True, 1):
                props[key] = "yes"
        feats[osm_id] = {"type": "Feature", "id": osm_id,
                         "geometry": {"type": gtype, "coordinates": coords},
                         "properties": props}
        by_class[fclass] = by_class.get(fclass, 0) + 1
    return feats, by_class


def bus_stops(zf):
    """Paradas y terminales del extracto -> (features, conteo por tipo)."""
    feats, by_kind = {}, {}

    def add(gtype, coords, a, layer, osm_type, source):
        fclass = a.get("fclass")
        if fclass not in ("bus_stop", "bus_station"):
            return
        osm_id = a.get("osm_id")
        try:
            osm_id = int(osm_id)
        except (TypeError, ValueError):
            return
        key = (osm_type, osm_id)
        if key in feats:
            return
        point = _centroid(gtype, coords)
        if point is None:
            return
        props = {"osm_id": osm_id, "osm_type": osm_type, "stop_kind": fclass,
                 "geometry_source": source, "source_layer": "geofabrik:" + layer}
        if a.get("name"):
            props["name"] = a["name"]
        feats[key] = {"type": "Feature", "id": "%s/%s" % (osm_type, osm_id),
                      "geometry": {"type": "Point", "coordinates": point},
                      "properties": props}
        by_kind[fclass] = by_kind.get(fclass, 0) + 1

    for (gtype, coords), a in read_layer(zf, TRANSPORT_POINTS):
        add(gtype, coords, a, TRANSPORT_POINTS, "node", "node")
    try:
        for (gtype, coords), a in read_layer(zf, TRANSPORT_AREAS):
            add(gtype, coords, a, TRANSPORT_AREAS, "way", "way_centroid")
    except LookupError:
        pass   # no todos los extractos traen la capa de areas
    return feats, by_kind


if __name__ == "__main__":   # inspeccion rapida: python geofabrik.py UY
    import sys
    iso2 = sys.argv[1] if len(sys.argv) > 1 else "UY"
    region = find_region(iso2)
    print(json.dumps(region, ensure_ascii=False, indent=2))
    zf, how = open_extract(region["url"])
    print("abierto", how)
    for n in sorted(set(os.path.basename(x) for x in zf.namelist())):
        if n.endswith(".shp"):
            print("  ", n)
