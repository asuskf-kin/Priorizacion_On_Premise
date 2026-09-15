"""CLI del pipeline de priorizacion On Premise.

    python -m pipeline                      # config.yaml, pasos 1 y 2
    python -m pipeline --step 1             # solo Priority
    python -m pipeline --step 2             # solo geo (lee el output del paso 1 de Azure)
    python -m pipeline --config otro.yaml   # otra corrida (otro pais / embotellador)
"""

import argparse
import logging
import sys
import time

from pipeline.config import load_config


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="pipeline", description=__doc__)
    parser.add_argument("--config", default="config.yaml", help="archivo de configuracion (default: config.yaml)")
    parser.add_argument("--step", choices=["1", "2", "all"], default="all", help="paso a ejecutar (default: all)")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # El SDK de Azure loguea cada request HTTP en INFO: solo nos interesan sus warnings
    for ruidoso in ("azure", "azure.core.pipeline.policies.http_logging_policy", "urllib3"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)

    log = logging.getLogger("pipeline")

    # Se importan aca para que --help no exija las dependencias pesadas
    from pipeline import step1_priority, step2_geo

    config = load_config(args.config)
    log.info("Corrida '%s' (config: %s, paso: %s)", config.get("run", {}).get("name", "-"), args.config, args.step)
    start = time.time()

    priority = None
    if args.step in ("1", "all"):
        priority = step1_priority.run(config)
    if args.step in ("2", "all"):
        step2_geo.run(config, priority=priority)

    log.info("Listo en %.1f min", (time.time() - start) / 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
