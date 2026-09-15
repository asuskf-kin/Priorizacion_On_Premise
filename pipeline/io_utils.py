"""Lectura/escritura de blobs para el pipeline."""

import logging

import azure_utils as az

log = logging.getLogger(__name__)

_READERS = {
    ".csv": az.read_blob_df_csv,
    ".xlsx": az.read_excel_blob,
    ".parquet": az.read_parquet_blob,
    ".json": az.read_blob_df_json,
    ".geojson": az.read_blob_df_geojson,
}


def load_data(file_path, container_name):
    """Carga csv, xlsx, parquet, json o geojson desde Blob Storage."""
    for extension, reader in _READERS.items():
        if file_path.endswith(extension):
            log.info("Leyendo %s de '%s'", file_path, container_name)
            df = reader(file_path, container_name)
            if df is None or len(df) == 0:
                raise ValueError(f"'{file_path}' en '{container_name}' vino vacio")
            log.info("  -> %s filas x %s columnas", f"{len(df):,}", df.shape[1])
            return df

    raise ValueError(
        f"Extension no soportada para '{file_path}'. "
        f"Se admiten: {', '.join(_READERS)}"
    )


def load_source(source):
    """load_data a partir de un bloque del config ({file_path, container_name})."""
    return load_data(source["file_path"], source["container_name"])


def save_csv(df, output):
    from pipeline.config import blob_uri

    path = blob_uri(output)
    log.info("Guardando %s filas en '%s/%s'", f"{len(df):,}", output["container_name"], path)
    az.write_blob_df_csv(path, output["container_name"], df)
    return path


def read_blob_bytes(source):
    """Descarga un blob crudo (por ej. el .tif de poblacion) como bytes."""
    container, path = source["container_name"], source["file_path"]
    log.info("Descargando %s de '%s'", path, container)
    client = az.get_blob_service_client_sas().get_blob_client(container=container, blob=path)
    return client.download_blob().readall()
