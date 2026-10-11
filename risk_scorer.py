"""Risk Scorer tool: loads a trained EBM and explains one applicant's default probability.

Positive contributions raise default risk; negative contributions lower it.
"""
import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from train_ebm import FEATURES

PROJECT_ROOT = Path(__file__).resolve().parent


def latest_model_path(folder: str | Path = "models") -> Path:
    """Return the newest .pkl in the folder (versions are UTC timestamps, so names sort by age)."""
    folder = Path(folder)
    if not folder.is_absolute():
        folder = PROJECT_ROOT / folder
    models = sorted(folder.glob("ebm-*.pkl"))
    if not models:
        raise FileNotFoundError(f"No trained model in {folder}. "
                                "Run `python train_ebm.py --target-means default` first.")
    return models[-1]


class RiskScorer:
    def __init__(self, model_path: str | Path):
        self.model_path = Path(model_path)
        self.ebm = joblib.load(self.model_path)
        self.metadata = json.loads(self.model_path.with_suffix(".json").read_text())
        self.model_version = self.metadata["model_version"]
        self.features = self.metadata.get("features", FEATURES)
        if self.features != FEATURES:
            raise ValueError(f"Model {self.model_version} was trained on {self.features}, "
                             f"but train_ebm.FEATURES is {FEATURES}.")

    def score(self, applicant: dict[str, Any]) -> dict[str, Any]:
        missing = [f for f in FEATURES if applicant.get(f) is None]
        if missing:
            raise ValueError(f"Applicant is missing required features: {', '.join(missing)}")

        X = pd.DataFrame([{f: applicant[f] for f in FEATURES}], columns=FEATURES)
        probability = float(self.ebm.predict_proba(X)[0, 1])
        contributions = {name: round(float(v), 4)
                         for name, v in zip(self.ebm.term_names_, self.ebm.eval_terms(X)[0])}

        ranked = sorted(contributions.items(), key=lambda kv: kv[1], reverse=True)
        return {
            "model_version": self.model_version,
            "default_probability": round(probability, 4),
            "baseline_log_odds": round(float(np.ravel(self.ebm.intercept_)[0]), 4),
            "contributions": contributions,
            "top_risk_drivers": [(k, v) for k, v in ranked if v > 0][:3],
            "top_protective_factors": [(k, v) for k, v in reversed(ranked) if v < 0][:3],
        }
