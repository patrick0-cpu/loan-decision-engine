"""
Load the Kaggle loan dataset into the project's SQLite database (loans.db).

Run once from the project folder:
  python load_data.py                         download from Kaggle, then load
  python load_data.py --csv path/to/file.csv  load a CSV you already have
  python load_data.py --version 1             pin a dataset version (recommended)

Running it again replaces the historical_loans table. Other tables are not touched.
Requires: pip install pandas kagglehub
"""
import argparse
import sqlite3
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).resolve().parent / "loans.db"
DATASET = "udaymalviya/bank-loan-data"
TABLE = "historical_loans"

# column -> SQLite type. db.py's HistoricalLoan model must match this.
COLUMNS = {
    "person_age": "REAL",
    "person_gender": "TEXT",
    "person_education": "TEXT",
    "person_income": "REAL",
    "person_emp_exp": "INTEGER",
    "person_home_ownership": "TEXT",
    "loan_amnt": "REAL",
    "loan_intent": "TEXT",
    "loan_int_rate": "REAL",
    "loan_percent_income": "REAL",
    "cb_person_cred_hist_length": "REAL",
    "credit_score": "INTEGER",
    "previous_loan_defaults_on_file": "TEXT",
    "loan_status": "INTEGER",
}


def get_csv_path(csv: str | None, version: int | None) -> Path:
    if csv:
        return Path(csv)
    import kagglehub  # only needed when downloading
    handle = f"{DATASET}/versions/{version}" if version else DATASET
    folder = Path(kagglehub.dataset_download(handle))
    return folder / "loan_data.csv"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", help="path to loan_data.csv (skips the download)")
    p.add_argument("--version", type=int, help="Kaggle dataset version to download")
    args = p.parse_args()

    csv_path = get_csv_path(args.csv, args.version)
    df = pd.read_csv(csv_path)

    missing = set(COLUMNS) - set(df.columns)
    if missing:
        raise SystemExit(f"The CSV is missing columns: {sorted(missing)}")
    df = df[list(COLUMNS)]

    columns_sql = ",\n  ".join(f"{name} {kind}" for name, kind in COLUMNS.items())
    with sqlite3.connect(DB_PATH) as con:
        con.execute(f"DROP TABLE IF EXISTS {TABLE}")
        con.execute(f"CREATE TABLE {TABLE} (\n  id INTEGER PRIMARY KEY,\n  {columns_sql}\n)")
        df.to_sql(TABLE, con, if_exists="append", index=False)
        total = con.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        clean = con.execute(
            f"SELECT COUNT(*) FROM {TABLE} "
            "WHERE LOWER(TRIM(previous_loan_defaults_on_file)) = 'no'").fetchone()[0]

    print(f"Source:   {csv_path}")
    print(f"Database: {DB_PATH}")
    print(f"Loaded {total:,} rows into {TABLE} ({clean:,} without a previous default).")


if __name__ == "__main__":
    main()
