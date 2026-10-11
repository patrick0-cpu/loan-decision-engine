# Loan Decision Support System

A prototype that helps a loan officer review loan applications. For each applicant it runs financial
calculations and rule checks in plain code, estimates the probability of default with an
Explainable Boosting Machine (EBM), and shows everything in a Streamlit dashboard.
**A human loan officer always makes the final decision.** The system only recommends.

The full specification is in [`CLAUDE.md`](CLAUDE.md). The build recap is in
[`BUILD_LOG.md`](BUILD_LOG.md).

> Prototype trained on a synthetic Kaggle dataset (`udaymalviya/bank-loan-data`). Results are not
> real-world accuracy claims.

---

## Quick start (Windows)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# .env must contain:  DATABASE_URL=sqlite:///loans.db
python load_data.py                          # once: loads 45,000 rows into loans.db
python train_ebm.py --check                  # confirm loan_status = 1 means default
python train_ebm.py --target-means default   # train once (about 70 seconds)
streamlit run app/Home.py
```

`loans.db`, `.env`, `models/` and any CSV are git-ignored. Everyone who clones the project runs
`load_data.py` and trains the model themselves.

## Files

| File | Purpose |
|---|---|
| `db.py` | SQLAlchemy 2.0 models for 11 tables, `get_engine()`, `load_historical()`, `log()` |
| `calculations.py` | Loan-to-income, monthly payment, payment-to-income, and the six rule checks |
| `train_ebm.py` | Target check (`--check`), EBM training, fairness report, model saving |
| `risk_scorer.py` | `RiskScorer` (probability + per-term contributions), `latest_model_path()` |
| `app/Home.py` | Dashboard entry point |
| `app/shared.py` | Helpers shared by the pages: cached engine and model, labels, charts, the application view |
| `app/pages/1_Portfolio_overview.py` … `5_Audit_log.py` | The five dashboard pages |
| `load_data.py` | Provided by the user; not modified |
