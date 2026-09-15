"""Paso 1 - Priority: cruce Datamatch x Dataplor + score de existencia.

Equivalente automatizado de 1_Priority_OnPremise.ipynb.
"""

import json
import logging

import pandas as pd
from sklearn.preprocessing import MinMaxScaler

from pipeline.io_utils import load_source, save_csv

log = logging.getLogger(__name__)

# Columnas de identificacion del POI (las que trae la base Datamatch)
BASE_COLUMNS = [
    "dataplor_id",
    "name",
    "business_category",
    "city",
    "state",
    "country",
    "latitude",
    "longitude",
    "dataplor_status",
    "channel_cat",
]

# Columnas que se traen de Dataplor al cruzar por dataplor_id
DATAPLOR_COLUMNS = [
    "dataplor_id",
    "historical_popularity_scores",
    "historical_sentiment_scores",
    "price_level",
    "number_of_reviews",
    "average_stars",
    "website",
    "place_attributes",
    "validity_score",
    "open_closed_status_confidence_score",
    "monday_hours",
    "tuesday_hours",
    "wednesday_hours",
    "thursday_hours",
    "friday_hours",
    "saturday_hours",
    "sunday_hours",
]


def calculate_impressions_n_months(df, column_name, months=6, weights=None):
    """Cuenta (o pondera) los meses recientes con popularidad > 0."""
    parsed = df[column_name].apply(lambda x: json.loads(x) if isinstance(x, str) else x)

    def weighted_count(value):
        if not isinstance(value, dict) or not value:
            return 0
        sorted_dates = sorted(value.keys(), reverse=True)[:months]
        if weights:
            return sum(w for w, d in zip(weights, sorted_dates) if value[d] > 0)
        return sum(1 for d in sorted_dates if value[d] > 0)

    df[f"{column_name}_in_last_{months}_months"] = parsed.apply(weighted_count)
    return df


def create_engagement_category(df, historical_column, months=6, weights=None,
                               max_reviews=100, bins=None, labels=None):
    """weighted_combined_score + probability_existence_cat (low/moderate/high)."""
    weights = weights or [0.30, 0.35, 0.40]
    bins = bins or [-0.01, 0.4, 0.8, 1.01]
    labels = labels or ["low", "moderate", "high"]

    cols = [
        f"{historical_column}_in_last_{months}_months",
        "number_of_reviews",
        "open_closed_status_confidence_score",
    ]

    # Se trabaja sobre una copia para no pisar los valores crudos de df
    work = df[cols].fillna(0).astype(float)
    work["number_of_reviews"] = work["number_of_reviews"].clip(upper=max_reviews)

    # Normalizar (todo el dataset es un solo canal: On Premise)
    avg = work[cols[:-1]].mean().replace(0, 1)
    work[cols[:-1]] = work[cols[:-1]] / avg

    df_norm = pd.DataFrame(
        MinMaxScaler().fit_transform(work[cols]), columns=cols, index=df.index
    )

    df["weighted_combined_score"] = sum(
        df_norm[col] * w for col, w in zip(cols, weights)
    ) / sum(weights)

    df["probability_existence_cat"] = pd.cut(
        df["weighted_combined_score"], bins=bins, labels=labels
    )
    return df


def _same_source(a, b):
    keys = ("file_path", "container_name")
    return a and b and all(a.get(k) == b.get(k) for k in keys)


def build_priority_base(inputs):
    """Base del paso 1: cruce Datamatch x Dataplor, o una sola fuente si alcanza.

    Si `datamatch` esta vacio o apunta al mismo archivo que `dataplor`, se lee una
    sola vez y se seleccionan las columnas equivalentes al cruce, en vez de hacer
    un self-join que duplicaria columnas (number_of_reviews_x / _y).
    """
    dataplor_src = inputs["dataplor"]
    datamatch_src = inputs.get("datamatch") or dataplor_src

    if _same_source(datamatch_src, dataplor_src):
        log.info("Fuente unica: Dataplor se usa tambien como base (sin cruce)")
        dataplor = load_source(dataplor_src)
        _require_columns(dataplor, DATAPLOR_COLUMNS)
        columnas = list(dict.fromkeys(BASE_COLUMNS + DATAPLOR_COLUMNS))
        faltantes = [c for c in columnas if c not in dataplor.columns]
        if faltantes:
            log.warning("Columnas ausentes en la base, se omiten: %s", faltantes)
        priority = dataplor[[c for c in columnas if c in dataplor.columns]].copy()
    else:
        datamatch = load_source(datamatch_src)
        dataplor = load_source(dataplor_src)
        _require_columns(dataplor, DATAPLOR_COLUMNS)
        # Se quitan de Datamatch las columnas que aporta Dataplor para no generar _x / _y
        repetidas = [c for c in DATAPLOR_COLUMNS if c != "dataplor_id" and c in datamatch.columns]
        if repetidas:
            log.info("Columnas repetidas en Datamatch, mandan las de Dataplor: %s", repetidas)
        priority = datamatch.drop(columns=repetidas).merge(
            dataplor[DATAPLOR_COLUMNS], on="dataplor_id", how="left"
        )
        sin_match = priority["historical_popularity_scores"].isna().sum()
        log.info("Cruce listo: %s sin datos de Dataplor", f"{sin_match:,}")

    priority.drop(columns=["regla", "metodo"], inplace=True, errors="ignore")
    priority = priority[
        ["dataplor_id", "name"]
        + [c for c in priority.columns if c not in ("dataplor_id", "name")]
    ].reset_index(drop=True)
    log.info("Base lista: %s filas x %s columnas", f"{len(priority):,}", priority.shape[1])
    return priority


def _require_columns(df, columnas):
    faltantes = [c for c in columnas if c not in df.columns]
    if faltantes:
        raise ValueError(f"A la base Dataplor le faltan columnas: {faltantes}")


def run(config):
    step = config["step1_priority"]
    params = step["params"]

    log.info("=== PASO 1 - Priority ===")
    priority = build_priority_base(step["inputs"])

    popularity_column = params["popularity_column"]
    months = params["months"]

    priority = calculate_impressions_n_months(
        priority, popularity_column, months=months, weights=params["month_weights"]
    )
    priority = create_engagement_category(
        priority,
        popularity_column,
        months=months,
        weights=params["score_weights"],
        max_reviews=params["max_reviews"],
        bins=params["category_bins"],
        labels=params["category_labels"],
    )

    log_distribution(priority, "probability_existence_cat")
    save_csv(priority, step["output"])
    return priority


def log_distribution(df, column):
    conteo = df[column].value_counts(dropna=False)
    for valor, cantidad in conteo.items():
        log.info("  %-10s %8s  (%.2f%%)", valor, f"{cantidad:,}", cantidad / len(df) * 100)
