"""Publica una version v2 de los CSV de priority sin las columnas del mapeo por embotellador.

Se le pasa UNA ruta, que puede ser un archivo o una carpeta:

    # un archivo
    uv run python scripts/quitar_columnas_mapeo.py \
        prospecting-data/bronze/providers/dataplor/sl/chile/andina/202608/2_priority_andina_chile_20260818.csv

    # toda una carpeta (los dos embotelladores de Chile, en este caso)
    uv run python scripts/quitar_columnas_mapeo.py bronze/providers/dataplor/sl/chile/ --dry-run

Baja cada archivo, le quita `metodo_mapeo`, `confianza_mapeo` y `flag_escala_mapeo`, y sube
el resultado al lado del original con el sufijo `_v2`.

Esas tres columnas solo existen en las corridas que cruzaron Datamatch x Dataplor (las
aporta el split por embotellador). Un archivo de fuente unica no las tiene: el script lo
reporta y lo saltea, en vez de escribir una copia identica.
"""

import argparse
import logging
import os
import posixpath
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import azure_utils as az

log = logging.getLogger("quitar_columnas")

COLUMNAS = ["metodo_mapeo", "confianza_mapeo", "flag_escala_mapeo"]
CONTENEDOR = "prospecting-data"


def partir_ruta(ruta, contenedor_default):
    """Acepta 'contenedor/bronze/...' o 'bronze/...' -> (contenedor, ruta)."""
    ruta = ruta.strip().strip("'\"")
    primero, _, resto = ruta.partition("/")
    if resto and not primero.startswith("bronze"):
        return primero, resto
    return contenedor_default, ruta


def buscar(contenedor, ruta, sufijo):
    """Un archivo, o todos los priority de una carpeta. Ignora los que ya son v2."""
    if ruta.endswith(".csv"):
        return [ruta]

    prefijo = ruta.rstrip("/") + "/"
    encontrados = [
        blob for blob in az.list_blob_files(contenedor, prefijo)
        if blob.endswith(".csv")
        and "priority" in posixpath.basename(blob)
        and not blob.endswith(f"_{sufijo}.csv")
    ]
    log.info("%s archivos de priority bajo '%s'\n", len(encontrados), prefijo)
    return sorted(encontrados)


def ruta_v2(blob, sufijo):
    """.../2_priority_x.csv -> .../2_priority_x_v2.csv"""
    raiz, extension = posixpath.splitext(blob)
    return f"{raiz}_{sufijo}{extension}"


def procesar(contenedor, blob, columnas, sufijo, dry_run=False, sobrescribir=False):
    """Devuelve la ruta del v2, o None si no habia nada que hacer."""
    destino = ruta_v2(blob, sufijo)
    nombre = posixpath.basename(blob)

    if not az.blob_exists(contenedor, blob):
        log.error("%s: no existe en '%s'", nombre, contenedor)
        return None

    if az.blob_exists(contenedor, destino) and not sobrescribir:
        log.warning("%s: ya existe el v2, se saltea (--sobrescribir para rehacerlo)", nombre)
        return None

    log.info("%s: leyendo...", nombre)
    df = az.read_blob_df_csv(blob, contenedor)

    presentes = [c for c in columnas if c in df.columns]
    if not presentes:
        log.warning("  no tiene ninguna de las columnas a quitar: no se escribe v2")
        return None

    antes = df.shape[1]
    df = df.drop(columns=presentes)
    log.info("  %s filas | %s -> %s columnas | quitadas: %s",
             f"{len(df):,}", antes, df.shape[1], ", ".join(presentes))

    if dry_run:
        log.info("  [dry-run] OK, se escribiria -> %s", destino)
        return destino

    az.write_blob_df_csv(destino, contenedor, df)
    log.info("  guardado -> %s/%s", contenedor, destino)
    return destino


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ruta", help="archivo .csv o carpeta; admite 'contenedor/ruta' o solo 'bronze/...'")
    parser.add_argument("--contenedor", default=CONTENEDOR,
                        help=f"contenedor por defecto (default: {CONTENEDOR})")
    parser.add_argument("--columnas", nargs="+", default=COLUMNAS,
                        help="columnas a quitar (default: las tres del mapeo)")
    parser.add_argument("--sufijo", default="v2", help="sufijo del archivo nuevo (default: v2)")
    parser.add_argument("--dry-run", action="store_true",
                        help="lee y reporta, pero no escribe nada en Azure")
    parser.add_argument("--sobrescribir", action="store_true",
                        help="rehacer el v2 aunque ya exista")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # El SDK de Azure loguea cada request HTTP en INFO
    for ruidoso in ("azure", "urllib3"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)

    contenedor, ruta = partir_ruta(args.ruta, args.contenedor)
    blobs = buscar(contenedor, ruta, args.sufijo)
    if not blobs:
        log.error("no encontre archivos de priority en '%s/%s'", contenedor, ruta)
        return 1

    escritos = []
    for blob in blobs:
        try:
            destino = procesar(contenedor, blob, args.columnas, args.sufijo,
                               dry_run=args.dry_run, sobrescribir=args.sobrescribir)
        except Exception as e:
            log.error("%s: fallo -> %s", posixpath.basename(blob), e)
            continue
        if destino:
            escritos.append(destino)

    log.info("\n%s de %s archivos %s", len(escritos), len(blobs),
             "se escribirian" if args.dry_run else "escritos")
    for destino in escritos:
        log.info("   %s/%s", contenedor, destino)

    return 0 if escritos else 1


if __name__ == "__main__":
    sys.exit(main())
