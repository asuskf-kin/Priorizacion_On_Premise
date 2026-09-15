"""Carga y validacion del archivo de configuracion de la corrida."""

import logging
import posixpath

import yaml

log = logging.getLogger(__name__)


def load_config(path):
    with open(path, encoding="utf-8") as fh:
        config = yaml.safe_load(fh)

    for step in ("step1_priority", "step2_geo"):
        if step not in config:
            raise ValueError(f"Falta la seccion '{step}' en {path}")
        _validate_blocks(config[step], step, path)

    return config


def _validate_blocks(step, nombre, path):
    """Cada bloque de input/output necesita file_path y container_name."""
    geodata = step.get("geodata")
    if geodata and not (geodata.get("prefix") and geodata.get("container_name")):
        raise ValueError(f"A '{nombre}.geodata' le falta prefix o container_name en {path}")

    bloques = [(f"{nombre}.inputs.{k}", v) for k, v in (step.get("inputs") or {}).items() if v]
    bloques.append((f"{nombre}.output", step.get("output")))

    for etiqueta, bloque in bloques:
        if not isinstance(bloque, dict):
            raise ValueError(f"'{etiqueta}' esta mal definido en {path}")
        faltantes = [k for k in ("file_path", "container_name") if not bloque.get(k)]
        if etiqueta.endswith(".output"):
            faltantes += [k for k in ("file_name",) if not bloque.get(k)]
        if faltantes:
            raise ValueError(f"A '{etiqueta}' le falta {', '.join(faltantes)} en {path}")


def blob_uri(output):
    """Ruta final del blob: carpeta + nombre + .csv, sin dobles barras."""
    return posixpath.join(output["file_path"].rstrip("/"), f"{output['file_name']}.csv")


def resolve_step2_input(config):
    """Input del paso 2: el declarado en el config o, si no, el output del paso 1."""
    declared = config["step2_geo"]["inputs"].get("priority")
    if declared:
        return declared

    output = config["step1_priority"]["output"]
    log.info("step2.inputs.priority vacio -> uso el output del paso 1")
    return {"file_path": blob_uri(output), "container_name": output["container_name"]}
