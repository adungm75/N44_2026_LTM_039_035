"""Read the CSV tables in ``report/tables`` robustly.

The tables are written by the pipeline as plain CSV; a CSV viewer may save them back with
columns padded by spaces ("aligned"). Cells are stripped and numeric columns converted, so
both forms read the same.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd


def read_table(path: str | Path) -> pd.DataFrame:
    with open(path, newline="") as f:
        rows = [[c.strip() for c in row] for row in csv.reader(f, skipinitialspace=True)]
    df = pd.DataFrame(rows[1:], columns=rows[0]).replace("", None)
    for col in df.columns:
        conv = pd.to_numeric(df[col], errors="coerce")
        if conv.notna().sum() == df[col].notna().sum():
            df[col] = conv
    return df
