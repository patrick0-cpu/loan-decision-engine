"""Train the Explainable Boosting Machine that estimates probability of default.

Usage:
  python train_ebm.py --check                    inspect what loan_status means (run this first)
  python train_ebm.py --target-means default     train, treating loan_status = 1 as default
"""
import argparse
import json
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sqlalchemy import text

from db import get_engine, load_historical

PROTECTED = ["person_gender", "person_age"]
FEATURES = ["person_education", "person_income", "person_emp_exp", "person_home_ownership",
            "loan_amnt", "loan_intent", "loan_int_rate", "loan_percent_income",
            "cb_person_cred_hist_length", "credit_score"]
# previous_loan_defaults_on_file is deliberately excluded: see "Dataset analysis results".
TARGET = "loan_status"
POSSIBLE_AGE_PROXIES = ["person_emp_exp", "cb_person_cred_hist_length"]

HIGH_RISK_THRESHOLD = 0.5
FOUR_FIFTHS = 0.8
AGE_BINS = [-np.inf, 25, 35, 50, np.inf]
AGE_LABELS = ["<25", "25-34", "35-49", "50+"]


def load_clean_data() -> pd.DataFrame:
    engine = get_engine()
    df = load_historical(engine, clean=True)
    with engine.connect() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM historical_loans")).scalar()
    print(f"Using {len(df):,} of {total:,} rows (rows with a previous default on file are "
          f"excluded; see 'Dataset analysis results' in CLAUDE.md).\n")
    return df


def check_target(df: pd.DataFrame) -> None:
    print("1. Share of rows by loan_status")
    for status, share in df[TARGET].value_counts(normalize=True).sort_index().items():
        print(f"   {status}: {share:.1%}")

    means = df.groupby(TARGET)[["loan_int_rate", "loan_percent_income", "credit_score"]].mean()
    print("\n2. Averages by loan_status")
    print(textwrap.indent(means.round(4).to_string(), "   "))

    if not {0, 1} <= set(means.index):
        print("\nWARNING: loan_status does not contain both 0 and 1; cannot interpret it.")
        return

    signals = {
        "higher interest rates": means.loc[1, "loan_int_rate"] > means.loc[0, "loan_int_rate"],
        "larger loans relative to income":
            means.loc[1, "loan_percent_income"] > means.loc[0, "loan_percent_income"],
        "lower credit scores": means.loc[1, "credit_score"] < means.loc[0, "credit_score"],
    }
    print("\n3. Signals")
    for name, points_to_default in signals.items():
        verdict = "suggests 1 = default" if points_to_default else "suggests 1 = repaid"
        print(f"   Status 1 {'has' if points_to_default else 'does NOT have'} {name}: {verdict}")

    print("\n4. Conclusion")
    if all(signals.values()):
        print("   All signals agree: loan_status = 1 is the bad outcome (default).")
        print("   Train with: python train_ebm.py --target-means default")
    elif not any(signals.values()):
        print("   All signals agree: loan_status = 1 is the good outcome (repaid).")
        print("   Train with: python train_ebm.py --target-means repaid")
    else:
        print("   WARNING: the signals disagree. Do not train until the target meaning is confirmed.")


def fairness_table(groups: pd.Series, proba: np.ndarray) -> pd.DataFrame:
    frame = pd.DataFrame({"group": groups.astype(str).values, "proba": proba})
    frame["high_risk"] = frame["proba"] >= HIGH_RISK_THRESHOLD
    table = frame.groupby("group").agg(count=("proba", "size"),
                                       avg_default_probability=("proba", "mean"),
                                       share_high_risk=("high_risk", "mean"))
    table["favorable_rate"] = 1 - table["share_high_risk"]
    table["ratio_to_best"] = table["favorable_rate"] / table["favorable_rate"].max()
    return table.round(4)


def print_fairness(title: str, table: pd.DataFrame) -> list[str]:
    print(f"\n   By {title}")
    print(textwrap.indent(table.to_string(), "   "))
    warnings = [f"{title} group '{g}' has a favorable-rate ratio of {r:.2f} (below {FOUR_FIFTHS})"
                for g, r in table["ratio_to_best"].items() if r < FOUR_FIFTHS]
    for w in warnings:
        print(f"   WARNING: {w}")
    return warnings


def to_records(table: pd.DataFrame) -> list[dict]:
    return [{"group": g, **{k: float(v) for k, v in row.items()}} for g, row in table.iterrows()]


def train(df: pd.DataFrame, target_means: str, out_dir: Path) -> None:
    from interpret.glassbox import ExplainableBoostingClassifier
    from sklearn.metrics import accuracy_score, brier_score_loss, roc_auc_score
    from sklearn.model_selection import train_test_split

    before = len(df)
    df = df.dropna(subset=FEATURES + [TARGET]).copy()
    print(f"Dropped {before - len(df):,} rows with missing feature or target values; "
          f"{len(df):,} remain.")

    # Always model 1 = defaulted.
    y = df[TARGET].astype(int) if target_means == "default" else 1 - df[TARGET].astype(int)
    train_df, test_df, y_train, y_test = train_test_split(df, y, test_size=0.2, stratify=y,
                                                          random_state=42)
    print(f"Training on {len(train_df):,} rows, testing on {len(test_df):,} rows.\n")

    ebm = ExplainableBoostingClassifier(random_state=42)
    ebm.fit(train_df[FEATURES], y_train)

    proba = ebm.predict_proba(test_df[FEATURES])[:, 1]
    metrics = {
        "auc": round(float(roc_auc_score(y_test, proba)), 4),
        "accuracy_at_0_5": round(float(accuracy_score(y_test, proba >= 0.5)), 4),
        "brier_score": round(float(brier_score_loss(y_test, proba)), 4),
        "test_default_rate": round(float(y_test.mean()), 4),
    }
    print("Test-set metrics")
    for name, value in metrics.items():
        print(f"   {name}: {value}")

    importance = sorted(zip(ebm.term_names_, ebm.term_importances()), key=lambda t: -t[1])
    print("\nTop 10 features by importance (mean absolute contribution, log-odds)")
    for name, value in importance[:10]:
        print(f"   {name:40} {value:.4f}")

    print(f"\nFairness report (test set; high risk = probability >= {HIGH_RISK_THRESHOLD}; "
          f"favorable = not high risk)")
    gender_table = fairness_table(test_df["person_gender"], proba)
    age_bands = pd.cut(test_df["person_age"], bins=AGE_BINS, labels=AGE_LABELS, right=False)
    age_table = fairness_table(age_bands, proba).reindex(
        [a for a in AGE_LABELS if a in set(age_bands.astype(str))])
    fairness_warnings = print_fairness("gender", gender_table)
    fairness_warnings += print_fairness("age band", age_table)
    if not fairness_warnings:
        print(f"\n   No group falls below the {FOUR_FIFTHS} ratio.")

    proxy_corr = {p: round(float(df[p].corr(df["person_age"])), 4) for p in POSSIBLE_AGE_PROXIES}
    print("\n   Correlation of possible age proxies with person_age (cleaned rows)")
    for name, corr in proxy_corr.items():
        print(f"   {name:30} {corr:+.4f}")

    trained_at = datetime.now(timezone.utc)
    version = f"ebm-{trained_at:%Y%m%dT%H%M%SZ}"
    out_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(ebm, out_dir / f"{version}.pkl")
    metadata = {
        "model_version": version,
        "trained_at": trained_at.isoformat(),
        "predicts": "probability of default",
        "target_means": target_means,
        "features": FEATURES,
        "excluded_protected": PROTECTED,
        "excluded_other": ["previous_loan_defaults_on_file"],
        "metrics": metrics,
        "trained_rows": len(train_df),
        "test_rows": len(test_df),
        "feature_importance": {name: round(float(v), 4) for name, v in importance},
        "fairness": {
            "high_risk_threshold": HIGH_RISK_THRESHOLD,
            "four_fifths_line": FOUR_FIFTHS,
            "by_gender": to_records(gender_table),
            "by_age_band": to_records(age_table),
            "warnings": fairness_warnings,
            "age_proxy_correlations": proxy_corr,
        },
        "note": "Trained on a synthetic Kaggle dataset (udaymalviya/bank-loan-data). "
                "Results are for a prototype, not real-world accuracy claims.",
    }
    (out_dir / f"{version}.json").write_text(json.dumps(metadata, indent=2))
    print(f"\nSaved {out_dir / (version + '.pkl')} and its .json metadata.")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="show what loan_status appears to mean")
    p.add_argument("--target-means", choices=["default", "repaid"],
                   help="what loan_status = 1 means; required to train")
    p.add_argument("--out", default="models", help="output folder (default: models)")
    args = p.parse_args()

    if not args.check and not args.target_means:
        sys.exit("Run `python train_ebm.py --check` first to confirm what loan_status means, "
                 "then train with `--target-means default` or `--target-means repaid`.")

    df = load_clean_data()
    if args.check:
        check_target(df)
    if args.target_means:
        out = Path(args.out)
        if not out.is_absolute():
            out = Path(__file__).resolve().parent / out
        train(df, args.target_means, out)


if __name__ == "__main__":
    main()
