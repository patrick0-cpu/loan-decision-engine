"""Helpers shared by the dashboard pages: cached resources, labels, charts, and the application view."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from db import Application, get_engine, load_historical  # noqa: E402
from risk_scorer import RiskScorer, latest_model_path  # noqa: E402

# Chart colors (validated reference palette). Risk direction uses the blue <-> red diverging pair.
SERIES_1 = "#2a78d6"   # blue
SERIES_2 = "#eb6834"   # orange
RISK_UP = "#e34948"    # red: raises default risk
RISK_DOWN = "#2a78d6"  # blue: lowers default risk
REFERENCE_LINE = "#898781"

FEATURE_LABELS = {
    "person_education": "Education",
    "person_income": "Income",
    "person_emp_exp": "Employment experience",
    "person_home_ownership": "Home ownership",
    "loan_amnt": "Loan amount",
    "loan_intent": "Loan intent",
    "loan_int_rate": "Interest rate",
    "loan_percent_income": "Loan-to-income",
    "cb_person_cred_hist_length": "Credit history length",
    "credit_score": "Credit score",
}
CALC_LABELS = {
    "loan_to_income": "Loan-to-income",
    "monthly_payment": "Monthly payment",
    "payment_to_income": "Payment-to-income",
}
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def term_label(term: str) -> str:
    """'person_income & loan_amnt' -> 'Income × Loan amount'."""
    return " × ".join(FEATURE_LABELS.get(part.strip(), part.strip()) for part in term.split("&"))


@st.cache_resource
def engine():
    return get_engine()


@st.cache_data(show_spinner="Loading historical loans...")
def historical(clean: bool) -> pd.DataFrame:
    return load_historical(engine(), clean=clean)


@st.cache_resource(show_spinner="Loading the risk model...")
def _load_scorer(path: str) -> RiskScorer:
    return RiskScorer(path)


def scorer() -> RiskScorer | None:
    """The newest trained model, or None if none exists. A newly trained model is picked up
    automatically because the cache is keyed by the model path."""
    try:
        path = latest_model_path()
    except FileNotFoundError:
        return None
    return _load_scorer(str(path))


def no_model_warning():
    st.warning("No trained model yet. Run `python train_ebm.py --check`, then "
               "`python train_ebm.py --target-means default`, and reload this page.")


def latest(rows):
    return max(rows, key=lambda r: r.id) if rows else None


def style_bars(fig: go.Figure, height: int) -> go.Figure:
    fig.update_layout(height=height, barcornerradius=4, bargap=0.35, showlegend=False,
                      margin=dict(l=10, r=30, t=30, b=10))
    fig.update_xaxes(showgrid=True, zeroline=False)
    fig.update_yaxes(showgrid=False)
    return fig


def contribution_chart(contributions: dict[str, float], top: int = 10) -> go.Figure:
    """Horizontal bars of the largest contributions; the remaining terms are combined into one."""
    items = sorted(contributions.items(), key=lambda kv: abs(kv[1]), reverse=True)
    rows = [(term_label(k), v) for k, v in items[:top]]
    rest = items[top:]
    if rest:
        rows.append((f"{len(rest)} other terms combined", sum(v for _, v in rest)))
    rows.reverse()  # largest at the top

    fig = go.Figure(go.Bar(
        x=[v for _, v in rows], y=[name for name, _ in rows], orientation="h",
        marker_color=[RISK_UP if v > 0 else RISK_DOWN for _, v in rows],
        text=[f"{v:+.2f}" for _, v in rows], textposition="outside", cliponaxis=False,
        hovertemplate="%{y}<br>%{x:+.4f} log-odds<extra></extra>"))
    fig.add_vline(x=0, line_width=1, line_color=REFERENCE_LINE)
    fig.update_xaxes(title="Contribution to the log-odds of default")
    return style_bars(fig, height=40 * len(rows) + 90)


def show_flag(flag):
    evidence = dict(flag.evidence)
    value, limit = evidence.pop("value", None), evidence.pop("limit", None)
    extra = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in evidence.items())
    text = f"**{flag.severity.upper()}: {flag.code}**. Value {value}, limit {limit}"
    text += f" ({extra})" if extra else ""
    if flag.severity == "critical":
        st.error(text, icon="🛑")
    elif flag.severity == "warning":
        st.warning(text, icon="⚠️")
    else:
        st.info(text, icon="ℹ️")


def format_calc(metric: str, value) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.2f}" if metric == "monthly_payment" else f"{value:.4f} ({value:.1%})"


def show_application(app: Application):
    """Render calculations, flags, score, contributions, and the recommendation for one application.
    Call inside an open Session so relationships can load."""
    applicant = app.applicant
    rec = latest(app.recommendations)
    score = latest(app.risk_scores)

    st.subheader(f"Application #{app.id}: {applicant.full_name}")
    term = f"{app.term_months} months" if app.term_months else "term not given"
    st.caption(f"Status **{app.status}** · submitted {app.created_at:%Y-%m-%d %H:%M} UTC · "
               f"loan {app.loan_amount:,.0f} for {app.loan_intent} at {app.interest_rate}% "
               f"({term}) · stated income {applicant.annual_income:,.0f}")
    if rec and rec.marked_for_additional_review:
        st.error("Marked for additional review: " + ", ".join(rec.review_reasons), icon="🚩")

    st.markdown("##### Calculations")
    calcs = sorted(app.calculations, key=lambda c: c.id)
    for col, calc in zip(st.columns(max(len(calcs), 1)), calcs):
        with col:
            st.metric(CALC_LABELS.get(calc.metric, calc.metric), format_calc(calc.metric, calc.value))
            for assumption in calc.assumptions:
                st.caption(f"Assumption: {assumption}")
            with st.expander("Inputs and formula"):
                st.json(calc.inputs)
                st.code(calc.code_used or "", language="text")

    st.markdown("##### Rule flags")
    flags = sorted(app.flags, key=lambda f: SEVERITY_ORDER.get(f.severity, 9))
    if not flags:
        st.success("No rule flags.", icon="✅")
    for flag in flags:
        show_flag(flag)

    st.markdown("##### Model risk score")
    if score is None:
        st.info("No risk score recorded.")
    else:
        c1, c2 = st.columns([1, 3])
        c1.metric("Probability of default", f"{score.default_probability:.1%}")
        c1.caption(f"Model {score.model_version}. Trained on synthetic data; a prototype score, "
                   "not a real-world estimate.")
        with c2:
            st.caption("Largest contributions. **Red bars (positive) raise** default risk; "
                       "**blue bars (negative) lower** it. Pairs (A × B) are interaction terms.")
            st.plotly_chart(contribution_chart(score.contributions), width="stretch",
                            key=f"contrib-{app.id}")

    st.markdown("##### Recommendation")
    if rec is None:
        st.info("No recommendation recorded.")
    else:
        st.write(f"**{rec.recommendation.upper()}**: refer to a loan officer. "
                 f"Reason codes: {', '.join(rec.reason_codes) or 'none'}")
    st.info("This is a recommendation awaiting loan officer review. The system does not approve "
            "or deny loans; only an officer's decision on the review page is final.", icon="👤")
