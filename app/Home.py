"""Loan Decision Support dashboard. Run with: streamlit run app/Home.py"""
import streamlit as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shared import engine, no_model_warning, page_layout, scorer

st.set_page_config(page_title="Loan Decision Support", page_icon="🏦", layout="wide")
page_layout()

from db import Application  # noqa: E402  (shared puts the project root on sys.path)

st.title("Loan Decision Support")
st.markdown("""
This system helps a loan officer review applications. For each applicant it:

- runs **financial calculations** (loan-to-income, monthly payment, payment-to-income) in plain code,
- checks **rules** whose thresholds come from the dataset analysis,
- estimates a **probability of default** with an Explainable Boosting Machine and shows what drove it.

**A loan officer always makes the final decision.** The system only recommends; nothing is approved
or denied until an officer records a decision on the review page. Gender and age are never used by
the model; they appear only in the fairness report.
""")

model = scorer()
if model is None:
    no_model_warning()
else:
    m = model.metadata
    st.success(f"Model **{m['model_version']}** loaded (test AUC {m['metrics']['auc']}).")

with Session(engine()) as s:
    in_review = s.scalar(select(func.count()).select_from(Application)
                         .where(Application.status == "in_review"))
st.metric("Applications waiting for officer review", in_review)

st.markdown("""
**Pages**
1. **Portfolio overview**: the historical loans and their outcomes
2. **Model**: metrics, feature importance, and the fairness report
3. **Score an applicant**: enter an application and get a recommendation
4. **Officer review queue**: review applications and record decisions
5. **Audit log**: every action the system and officers took
""")
st.caption("Prototype trained on a synthetic Kaggle dataset (udaymalviya/bank-loan-data). "
           "Results are not real-world accuracy claims.")
