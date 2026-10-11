"""Train the Explainable Boosting Machine that estimates probability of default.

Usage:
  python train_ebm.py --check                    inspect what loan_status means (run this first)
  python train_ebm.py --target-means default     train, treating loan_status = 1 as default

The main model is variant c (CLAUDE3.md): eight features and no interaction terms.

Experiments (never into models/, which the dashboard loads):
  python train_ebm.py --target-means default --out models_experiments --interactions 10 \\
      --drop loan_int_rate
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
FEATURES = ["person_education", "person_income", "person_home_ownership", "loan_amnt",
            "loan_intent", "loan_int_rate", "loan_percent_income", "credit_score"]
# previous_loan_defaults_on_file is deliberately excluded: see "Dataset analysis results".
# person_emp_exp and cb_person_cred_hist_length were dropped in phase 2b (variant c): they track
# age closely (correlations +0.96 and +0.86) and added nothing once interactions were removed.
DEFAULT_INTERACTIONS = 0  # variant c: one term per feature
TARGET = "loan_status"
POSSIBLE_AGE_PROXIES = ["person_emp_exp", "cb_person_cred_hist_length"]

PROJECT_ROOT = Path(__file__).resolve().parent
MAIN_MODEL_DIR = PROJECT_ROOT / "models"
MAX_POINTS = 100  # curves and shape functions saved for the dashboard are downsampled to this

HIGH_RISK_THRESHOLD = 0.5
FOUR_FIFTHS = 0.8
SMALL_SAMPLE = 100  # groups smaller than this are marked "small sample"
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
    """Favorable rate (not high risk) per group, compared with the largest group in the category."""
    frame = pd.DataFrame({"group": groups.astype(str).values, "proba": proba})
    frame["high_risk"] = frame["proba"] >= HIGH_RISK_THRESHOLD
    table = frame.groupby("group").agg(count=("proba", "size"),
                                       avg_default_probability=("proba", "mean"),
                                       share_high_risk=("high_risk", "mean"))
    table["favorable_rate"] = 1 - table["share_high_risk"]
    reference = table["count"].idxmax()
    table["ratio_to_largest"] = table["favorable_rate"] / table.loc[reference, "favorable_rate"]
    table["small_sample"] = table["count"] < SMALL_SAMPLE
    return table.round(4)


def print_fairness(title: str, table: pd.DataFrame) -> list[str]:
    reference = table["count"].idxmax()
    print(f"\n   By {title} (reference: largest group '{reference}'; "
          f"'small sample' = fewer than {SMALL_SAMPLE} people)")
    shown = table.assign(small_sample=table["small_sample"].map({True: "small sample", False: ""}))
    print(textwrap.indent(shown.to_string(), "   "))
    warnings = []
    for g, row in table.iterrows():
        if row["ratio_to_largest"] < FOUR_FIFTHS:
            note = f", small sample of {int(row['count'])}" if row["small_sample"] else ""
            warnings.append(f"{title} group '{g}' has a favorable-rate ratio of "
                            f"{row['ratio_to_largest']:.2f} (below {FOUR_FIFTHS}{note})")
    for w in warnings:
        print(f"   WARNING: {w}")
    return warnings


def to_records(table: pd.DataFrame) -> list[dict]:
    return [{"group": g, **{k: (bool(v) if k == "small_sample" else float(v))
                            for k, v in row.items()}} for g, row in table.iterrows()]


def split(df: pd.DataFrame, target_means: str):
    """Drop incomplete rows and make the 80/20 stratified split. Always returns y with 1 = default."""
    from sklearn.model_selection import train_test_split

    before = len(df)
    df = df.dropna(subset=FEATURES + [TARGET]).copy()
    print(f"Dropped {before - len(df):,} rows with missing feature or target values; "
          f"{len(df):,} remain.")
    y = df[TARGET].astype(int) if target_means == "default" else 1 - df[TARGET].astype(int)
    train_df, test_df, y_train, y_test = train_test_split(df, y, test_size=0.2, stratify=y,
                                                          random_state=42)
    return df, train_df, test_df, y_train, y_test


def fairness_report(df: pd.DataFrame, test_df: pd.DataFrame, proba: np.ndarray) -> dict:
    print(f"\nFairness report (test set; high risk = probability >= {HIGH_RISK_THRESHOLD}; "
          f"favorable = not high risk; ratio = favorable rate / largest group's)")
    gender_table = fairness_table(test_df["person_gender"], proba)
    age_bands = pd.cut(test_df["person_age"], bins=AGE_BINS, labels=AGE_LABELS, right=False)
    age_table = fairness_table(age_bands, proba).reindex(
        [a for a in AGE_LABELS if a in set(age_bands.astype(str))])
    warnings = print_fairness("gender", gender_table)
    warnings += print_fairness("age band", age_table)
    if not warnings:
        print(f"\n   No group falls below the {FOUR_FIFTHS} ratio.")

    proxy_corr = {p: round(float(df[p].corr(df["person_age"])), 4) for p in POSSIBLE_AGE_PROXIES}
    print("\n   Correlation of possible age proxies with person_age (cleaned rows)")
    for name, corr in proxy_corr.items():
        print(f"   {name:30} {corr:+.4f}")
    return {
        "high_risk_threshold": HIGH_RISK_THRESHOLD,
        "four_fifths_line": FOUR_FIFTHS,
        "ratio_reference": "largest group",
        "small_sample_below": SMALL_SAMPLE,
        "by_gender": to_records(gender_table),
        "by_age_band": to_records(age_table),
        "warnings": warnings,
        "age_proxy_correlations": proxy_corr,
    }


def _pick(n: int, limit: int = MAX_POINTS) -> np.ndarray:
    """Evenly spaced indices into a sequence of length n, at most `limit`, keeping both ends."""
    return np.unique(np.linspace(0, n - 1, min(n, limit)).round().astype(int))


def _floats(values, digits: int = 4) -> list[float]:
    return [round(float(v), digits) for v in values]


def performance_data(y_test: pd.Series, proba: np.ndarray) -> dict:
    """Test-set data for the Model page graphs: ROC, calibration, score distribution, confusion."""
    from sklearn.metrics import confusion_matrix, roc_curve

    y = np.asarray(y_test)
    fpr, tpr, thresholds = roc_curve(y, proba)
    keep = _pick(len(fpr))
    roc = {"fpr": _floats(fpr[keep]), "tpr": _floats(tpr[keep]),
           "threshold": _floats(np.minimum(thresholds[keep], 1.0))}

    edges = np.linspace(0, 1, 11)
    bins = np.clip(np.digitize(proba, edges[1:-1]), 0, 9)
    calibration = []
    for b in range(10):
        mask = bins == b
        calibration.append({
            "bin": f"{edges[b]:.1f}-{edges[b + 1]:.1f}",
            "count": int(mask.sum()),
            "mean_predicted": round(float(proba[mask].mean()), 4) if mask.any() else None,
            "actual_default_rate": round(float(y[mask].mean()), 4) if mask.any() else None,
        })

    hist_edges = np.linspace(0, 1, 21)
    distribution = {
        "bin_edges": _floats(hist_edges, 2),
        "repaid": np.histogram(proba[y == 0], bins=hist_edges)[0].tolist(),
        "defaulted": np.histogram(proba[y == 1], bins=hist_edges)[0].tolist(),
    }

    tn, fp, fn, tp = (int(v) for v in
                      confusion_matrix(y, proba >= HIGH_RISK_THRESHOLD, labels=[0, 1]).ravel())
    confusion = {
        "threshold": HIGH_RISK_THRESHOLD,
        "counts": {"tn": tn, "fp": fp, "fn": fn, "tp": tp},
        "rates": {
            "true_positive_rate": round(tp / (tp + fn), 4),   # defaults caught
            "false_positive_rate": round(fp / (fp + tn), 4),  # repaid loans flagged high risk
            "true_negative_rate": round(tn / (tn + fp), 4),
            "false_negative_rate": round(fn / (fn + tp), 4),  # defaults missed
            "precision": round(tp / (tp + fp), 4) if tp + fp else None,
        },
    }
    return {"roc": roc, "calibration": calibration, "score_distribution": distribution,
            "confusion_matrix": confusion}


def explanation_data(ebm, train_df: pd.DataFrame) -> dict:
    """Shape function per single-feature term and a grid per pair term, from explain_global().

    Long numeric shapes are downsampled to about MAX_POINTS bins. EBM bins are quantile-based, so
    evenly spaced bin indices keep detail where the data is dense. Numeric density comes from the
    training rows (40 bins up to the 99th percentile), because the EBM's own density bins span
    the full range and hide the bulk of skewed features such as income.
    """
    explanation = ebm.explain_global()
    terms = {}
    for i, term in enumerate(ebm.term_names_):
        d = explanation.data(i)
        if d["type"] == "univariate":
            scores = np.asarray(d["scores"])
            names = list(d["names"])
            categorical = len(names) == len(scores)  # numeric names are bin edges (one extra)
            if categorical:
                keep = np.arange(len(scores))
                x = [str(n) for n in names]
                density = {"x": [str(n) for n in d["density"]["names"]],
                           "count": [int(c) for c in d["density"]["scores"]]}
            else:
                keep = _pick(len(scores))
                x = _floats(np.asarray(names, dtype=float)[keep])  # left edge of each kept bin
                values = train_df[term].astype(float)
                view_max = float(values.quantile(0.99))
                counts, edges = np.histogram(values[values <= view_max], bins=40)
                density = {"x": _floats(edges), "count": counts.tolist(),
                           "above_view": int((values > view_max).sum())}
            terms[term] = {
                "type": "categorical" if categorical else "numeric",
                "x": x,
                "x_max": None if categorical else round(float(names[-1]), 4),
                "view_max": None if categorical else round(view_max, 4),
                "contribution": _floats(scores[keep]),
                "upper": _floats(np.asarray(d["upper_bounds"])[keep]),
                "lower": _floats(np.asarray(d["lower_bounds"])[keep]),
                "density": density,
            }
        else:
            grid = np.asarray(d["scores"])
            rows, cols = _pick(grid.shape[0], 30), _pick(grid.shape[1], 30)

            def axis(names, keep, size):
                names = list(names)
                if len(names) == size:  # categories
                    return [str(names[k]) for k in keep]
                return _floats(np.asarray(names, dtype=float)[keep])

            terms[term] = {
                "type": "pair",
                "features": [part.strip() for part in term.split("&")],
                "x": axis(d["left_names"], rows, grid.shape[0]),
                "y": axis(d["right_names"], cols, grid.shape[1]),
                "contribution": [_floats(grid[r, cols]) for r in rows],  # [x index][y index]
            }
    return terms


def fit(train_df: pd.DataFrame, y_train: pd.Series, features: list[str],
        interactions=DEFAULT_INTERACTIONS):
    from interpret.glassbox import ExplainableBoostingClassifier

    kwargs = {} if interactions is None else {"interactions": interactions}
    ebm = ExplainableBoostingClassifier(random_state=42, **kwargs)
    ebm.fit(train_df[features], y_train)
    return ebm


def train(df: pd.DataFrame, target_means: str, out_dir: Path, interactions=DEFAULT_INTERACTIONS,
          drop: tuple[str, ...] = ()) -> dict:
    from sklearn.metrics import accuracy_score, brier_score_loss, roc_auc_score

    features = [f for f in FEATURES if f not in drop]
    if out_dir.resolve() == MAIN_MODEL_DIR and features != FEATURES:
        sys.exit("Dropping features is for experiments only (--out models_experiments). To change "
                 "the main model's features, edit train_ebm.FEATURES so the scorer matches.")

    df, train_df, test_df, y_train, y_test = split(df, target_means)
    print(f"Training on {len(train_df):,} rows, testing on {len(test_df):,} rows.")
    print(f"Features ({len(features)}): {', '.join(features)}")
    print(f"Interactions: {'EBM default' if interactions is None else interactions}\n")

    ebm = fit(train_df, y_train, features, interactions)
    proba = ebm.predict_proba(test_df[features])[:, 1]
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
    print(f"\nTop 10 of {len(importance)} terms by importance "
          "(mean absolute contribution, log-odds)")
    for name, value in importance[:10]:
        print(f"   {name:40} {value:.4f}")

    fairness = fairness_report(df, test_df, proba)

    trained_at = datetime.now(timezone.utc)
    version = f"ebm-{trained_at:%Y%m%dT%H%M%SZ}"
    out_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(ebm, out_dir / f"{version}.pkl")
    metadata = {
        "model_version": version,
        "trained_at": trained_at.isoformat(),
        "predicts": "probability of default",
        "target_means": target_means,
        "features": features,
        "excluded_protected": PROTECTED,
        "excluded_other": ["previous_loan_defaults_on_file"],
        "dropped_features": list(drop),
        "interactions": "EBM default" if interactions is None else interactions,
        "n_terms": len(ebm.term_names_),
        "metrics": metrics,
        "trained_rows": len(train_df),
        "test_rows": len(test_df),
        "feature_importance": {name: round(float(v), 4) for name, v in importance},
        "fairness": fairness,
        "performance": performance_data(y_test, proba),
        "shape_functions": explanation_data(ebm, train_df),
        "note": "Trained on a synthetic Kaggle dataset (udaymalviya/bank-loan-data). "
                "Results are for a prototype, not real-world accuracy claims.",
    }
    (out_dir / f"{version}.json").write_text(json.dumps(metadata, indent=1))
    print(f"\nSaved {out_dir / (version + '.pkl')} and its .json metadata.")
    return metadata


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="show what loan_status appears to mean")
    p.add_argument("--target-means", choices=["default", "repaid"],
                   help="what loan_status = 1 means; required to train")
    p.add_argument("--out", default="models", help="output folder (default: models)")
    p.add_argument("--interactions", type=int, default=DEFAULT_INTERACTIONS,
                   help=f"number of pairwise interaction terms (default: {DEFAULT_INTERACTIONS}, "
                        "the main model's setting)")
    p.add_argument("--drop", nargs="+", default=[], choices=FEATURES, metavar="FEATURE",
                   help="features to leave out (experiments only)")
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
            out = PROJECT_ROOT / out
        train(df, args.target_means, out, args.interactions, tuple(args.drop))


if __name__ == "__main__":
    main()
