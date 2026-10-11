"""Helpers shared by the dashboard pages: cached resources, labels, charts, and the application view."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import math  # noqa: E402

import plotly.graph_objects as go  # noqa: E402
from plotly.subplots import make_subplots  # noqa: E402
import streamlit as st  # noqa: E402

from db import Application, get_engine, load_historical  # noqa: E402
from risk_scorer import RiskScorer, latest_model_path  # noqa: E402

# Chart colors (validated reference palette). Risk direction uses the blue <-> red diverging pair.
SERIES_1 = "#2a78d6"   # blue
SERIES_2 = "#eb6834"   # orange
RISK_UP = "#e34948"    # red: raises default risk
RISK_DOWN = "#2a78d6"  # blue: lowers default risk
REFERENCE_LINE = "#898781"
NEUTRAL_MID = "#f0efec"  # grey midpoint of the blue <-> red diverging scale
TOTAL_BAR = "#52514e"   # baseline and final score in the waterfall

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


def baseline_log_odds(model_version: str) -> float | None:
    """The intercept of the model that produced a stored score (risk_scores keeps only the
    contributions). None if that model file is no longer in models/."""
    path = latest_model_path().parent / f"{model_version}.pkl"
    if not path.exists():
        return None
    return _load_scorer(str(path)).baseline_log_odds


@st.cache_data(ttl=60, show_spinner=False)
def llm_status() -> tuple[bool, str]:
    """(server reachable, active model name), rechecked at most once a minute."""
    import llm

    return llm.available(), llm.active_model()


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


def waterfall_rows(baseline: float, contributions: dict[str, float],
                   top: int = 10) -> tuple[list[tuple[str, float]], float]:
    """Steps from the baseline log-odds to the applicant's score: the `top` largest contributions
    (by size), then the rest combined. Returns (steps, total); total = baseline + every step."""
    items = sorted(contributions.items(), key=lambda kv: abs(kv[1]), reverse=True)
    steps = [(term_label(k), v) for k, v in items[:top]]
    rest = items[top:]
    if rest:
        steps.append((f"{len(rest)} other terms combined", sum(v for _, v in rest)))
    return steps, baseline + sum(v for _, v in steps)


def waterfall_chart(baseline: float, contributions: dict[str, float], top: int = 10) -> go.Figure:
    """Baseline -> each contribution -> final log-odds, labelled with the probability."""
    steps, total = waterfall_rows(baseline, contributions, top)
    probability = 1 / (1 + math.exp(-total))
    labels = ["Baseline (average applicant)"] + [n for n, _ in steps] + ["Applicant's score"]
    values = [baseline] + [v for _, v in steps] + [total]
    text = ([f"{baseline:+.2f}"] + [f"{v:+.2f}" for _, v in steps]
            + [f"{total:+.2f} = {format_probability(probability)}"])
    fig = go.Figure(go.Waterfall(
        orientation="h", y=labels, x=values, text=text, textposition="outside",
        measure=["absolute"] + ["relative"] * len(steps) + ["total"],
        increasing=dict(marker=dict(color=RISK_UP)), decreasing=dict(marker=dict(color=RISK_DOWN)),
        totals=dict(marker=dict(color=TOTAL_BAR)), cliponaxis=False,
        connector=dict(line=dict(color=REFERENCE_LINE, width=1, dash="dot")),
        hovertemplate="%{y}<br>%{x:+.4f} log-odds<extra></extra>"))
    running, ends = baseline, [0.0, baseline]
    for _, v in steps:
        running += v
        ends.append(running)
    lo, hi = min(ends), max(ends)
    pad = (hi - lo) * 0.22 or 1
    fig.add_vline(x=0, line_width=1, line_color=REFERENCE_LINE)
    fig.update_yaxes(autorange="reversed", showgrid=False)
    fig.update_xaxes(title="Log-odds of default (0 = 50% probability)", showgrid=True,
                     zeroline=False, range=[lo - pad, hi + pad])
    fig.update_layout(height=44 * len(labels) + 90, showlegend=False,
                      margin=dict(l=10, r=110, t=20, b=10))
    return fig


def shape_chart(shape: dict, label: str) -> go.Figure:
    """Contribution vs feature value with its uncertainty band, and the data density below."""
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.78, 0.22],
                        vertical_spacing=0.04)
    dens_x, dens_n = shape["density"]["x"], shape["density"]["count"]
    if shape["type"] == "numeric":
        x = shape["x"] + [shape["x_max"]]  # step function: each value holds until the next edge

        def ext(values):
            return values + [values[-1]]

        fig.add_trace(go.Scatter(x=x, y=ext(shape["upper"]), mode="lines", line_shape="hv",
                                 line=dict(width=0), hoverinfo="skip"), 1, 1)
        fig.add_trace(go.Scatter(x=x, y=ext(shape["lower"]), mode="lines", line_shape="hv",
                                 line=dict(width=0), fill="tonexty",
                                 fillcolor="rgba(42,120,214,0.18)", hoverinfo="skip"), 1, 1)
        fig.add_trace(go.Scatter(x=x, y=ext(shape["contribution"]), mode="lines",
                                 line_shape="hv", line=dict(color=SERIES_1, width=2),
                                 hovertemplate=label + " from %{x:,.4g}<br>"
                                               "%{y:+.3f} log-odds<extra></extra>"), 1, 1)
        if len(dens_x) == len(dens_n) + 1:  # density names are bin edges
            mids = [(a + b) / 2 for a, b in zip(dens_x[:-1], dens_x[1:])]
            widths = [(b - a) * 0.95 for a, b in zip(dens_x[:-1], dens_x[1:])]
        else:
            mids, widths = dens_x, None
        fig.add_trace(go.Bar(x=mids, y=dens_n, width=widths, marker_color=REFERENCE_LINE,
                             hovertemplate="around %{x:,.4g}<br>%{y:,} loans<extra></extra>"),
                      2, 1)
        # Long right tails (income, loan amount) squash the chart, so start zoomed on the bulk
        # of the data (up to the 99th percentile); the toolbar's autoscale shows the full range.
        hi = shape.get("view_max") or shape["x_max"]
        fig.update_xaxes(range=[shape["x"][0], hi])
        fig.update_xaxes(title=label, row=2, col=1)
    else:
        c = shape["contribution"]
        fig.add_trace(go.Bar(
            x=shape["x"], y=c, marker_color=[RISK_UP if v > 0 else RISK_DOWN for v in c],
            error_y=dict(type="data", symmetric=False, color=REFERENCE_LINE,
                         array=[u - v for u, v in zip(shape["upper"], c)],
                         arrayminus=[v - lo for v, lo in zip(c, shape["lower"])]),
            text=[f"{v:+.2f}" for v in c], textposition="outside", cliponaxis=False,
            hovertemplate="%{x}<br>%{y:+.3f} log-odds<extra></extra>"), 1, 1)
        fig.add_trace(go.Bar(x=dens_x, y=dens_n, marker_color=REFERENCE_LINE,
                             hovertemplate="%{x}<br>%{y:,} loans<extra></extra>"), 2, 1)
        fig.update_xaxes(title=label, row=2, col=1)
    fig.add_hline(y=0, line_width=1, line_color=REFERENCE_LINE, row=1, col=1)
    fig.update_yaxes(title="Contribution (log-odds)", row=1, col=1)
    fig.update_yaxes(title="Loans", row=2, col=1)
    fig.update_layout(height=470, showlegend=False, barcornerradius=3, bargap=0.25,
                      margin=dict(l=10, r=10, t=20, b=10))
    return fig


def _axis_labels(values) -> list[str]:
    return [v if isinstance(v, str) else f"{v:,.4g}" for v in values]


def pair_heatmap(shape: dict) -> go.Figure:
    """Grid of an interaction term's contribution (log-odds), diverging around zero."""
    a, b = shape["features"]
    grid = shape["contribution"]  # [x index][y index]
    z = [[grid[i][j] for i in range(len(grid))] for j in range(len(grid[0]))]
    limit = max(abs(v) for row in grid for v in row) or 1
    fig = go.Figure(go.Heatmap(
        x=_axis_labels(shape["x"]), y=_axis_labels(shape["y"]), z=z, zmid=0, zmin=-limit,
        zmax=limit, colorscale=[[0, RISK_DOWN], [0.5, NEUTRAL_MID], [1, RISK_UP]],
        xgap=1, ygap=1, colorbar=dict(title="log-odds"),
        hovertemplate=term_label(a) + " %{x}<br>" + term_label(b) + " %{y}<br>"
                      "%{z:+.3f} log-odds<extra></extra>"))
    def title(feature, values):
        numeric = any(not isinstance(v, str) for v in values)
        return term_label(feature) + (" (bin start)" if numeric else "")

    fig.update_xaxes(title=title(a, shape["x"]), type="category")
    fig.update_yaxes(title=title(b, shape["y"]), type="category")
    fig.update_layout(height=480, margin=dict(l=10, r=10, t=20, b=10))
    return fig


def show_flag(flag):
    evidence = dict(flag.evidence)
    if flag.source == "pattern":
        cited = "; ".join(f"{c['document']} · {c['field']} = {c['value']}"
                          for c in evidence.get("cited", []))
        text = (f"**{flag.severity.upper()} (pattern review): {flag.code}**. "
                f"{evidence.get('description', '')}  \nEvidence checked by code: {cited}")
    else:
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


def format_probability(p) -> str:
    """Display only: a fitted model is never certain, so extremes show as bounds. Stored values
    are unchanged."""
    if p is None:
        return ""
    if p > 0.99:
        return "above 99%"
    if p < 0.01:
        return "below 1%"
    return f"{p:.1%}"


def format_calc(metric: str, value) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.2f}" if metric == "monthly_payment" else f"{value:.4f} ({value:.1%})"


def other_applications(app: Application) -> pd.DataFrame:
    """The applicant's other applications (earlier ones first) with their outcome."""
    rows = []
    for other in sorted(app.applicant.applications, key=lambda a: a.id):
        if other.id == app.id:
            continue
        review = latest(other.officer_reviews)
        score = latest(other.risk_scores)
        rows.append({
            "Application": other.id,
            "When": "earlier" if other.id < app.id else "later",
            "Submitted (UTC)": other.created_at.strftime("%Y-%m-%d %H:%M"),
            "Loan amount": f"{other.loan_amount:,.0f}",
            "Intent": other.loan_intent,
            "Default probability": format_probability(score.default_probability) if score else "",
            "Status": other.status,
            "Officer decision": (f"{review.decision} by {review.officer_name} "
                                 f"({review.decided_at:%Y-%m-%d})") if review else "none yet",
        })
    return pd.DataFrame(rows)


DOC_LABELS = {"pay_stub": "Pay stub", "bank_statement": "Bank statement"}
REC_ICONS = {"approve": "🟢", "deny": "🔴", "refer": "🟡"}


def show_documents(app: Application):
    """Documents with a view/download button, hash check, and extracted fields."""
    from documents import file_path, hash_matches

    docs = sorted(app.documents, key=lambda d: d.id)
    st.markdown("##### Documents and extracted fields")
    if not docs:
        st.caption("No documents on file for this application.")
        return
    for doc in docs:
        label = DOC_LABELS.get(doc.doc_type, doc.doc_type)
        intact = hash_matches(doc)
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            c1.markdown(f"**{label}**: `{doc.original_filename}` · {doc.page_count} page(s) · "
                        f"{(doc.size_bytes or 0) / 1024:,.0f} KB · uploaded "
                        f"{doc.uploaded_at:%Y-%m-%d %H:%M} UTC")
            c1.caption(("✅ File unchanged since upload" if intact else
                        "🛑 File missing or changed since upload") + f" (SHA-256 {doc.sha256[:16]}…)")
            if intact:
                c2.download_button("View / download", file_path(doc).read_bytes(),
                                   file_name=doc.original_filename or f"{doc.doc_type}",
                                   mime=doc.mime_type, key=f"doc-{doc.id}")
            fields = sorted(doc.extracted_fields, key=lambda f: f.id)
            if not fields:
                st.caption("Not read: the Document Reader did not run for this document.")
                continue
            st.dataframe(pd.DataFrame([{
                "Field": f.field_name, "Value": f.field_value if f.field_value is not None else "(not found)",
                "Page": f.source_page, "Needs review": "⚠️ yes" if f.needs_review else "",
                "Verified by officer": "✅" if f.verified_by_human else "",
                "Source": "computed by code" if f.extraction_model == "code" else f.extraction_model,
            } for f in fields]), hide_index=True, width="stretch")


def show_recommendation(rec):
    st.markdown("##### Recommendation")
    if rec is None:
        st.info("No recommendation recorded.")
        return
    icon = REC_ICONS.get(rec.recommendation, "")
    confidence = "" if rec.confidence is None else f" · confidence {rec.confidence:.2f}"
    if rec.passed_validation is None:
        validation = "not validated (no LLM recommendation)"
    else:
        validation = "✅ passed" if rec.passed_validation else "⚠️ failed"
    st.markdown(f"{icon} **{rec.recommendation.upper()}**{confidence} · validation: {validation}")
    if rec.explanation:
        st.markdown(f"> {rec.explanation}")
        st.caption("Written by the Decision Coordinator (LLM) from tool results only. Every number "
                   "in it was checked against those results by code.")
    st.caption(f"Reason codes: {', '.join(rec.reason_codes) or 'none'}")
    if rec.review_reasons:
        st.markdown("**Review reasons:** " + "; ".join(rec.review_reasons))


def show_application(app: Application):
    """Render calculations, flags, score, contributions, and the recommendation for one application.
    Call inside an open Session so relationships can load."""
    applicant = app.applicant
    rec = latest(app.recommendations)
    score = latest(app.risk_scores)

    st.subheader(f"Application #{app.id}: {applicant.full_name} (applicant #{applicant.id})")
    term = f"{app.term_months} months" if app.term_months else "term not given"
    # Income this application was scored with (the applicant row holds the latest profile).
    lti = next((c for c in app.calculations if c.metric == "loan_to_income"), None)
    income = lti.inputs.get("annual_income") if lti else applicant.annual_income
    st.caption(f"Status **{app.status}** · submitted {app.created_at:%Y-%m-%d %H:%M} UTC · "
               f"loan {app.loan_amount:,.0f} for {app.loan_intent} at {app.interest_rate}% "
               f"({term}) · stated income {income:,.0f}")
    if rec and rec.marked_for_additional_review:
        st.error("Marked for additional review: " + ", ".join(rec.review_reasons), icon="🚩")

    others = other_applications(app)
    if not others.empty:
        st.markdown(f"##### Other applications by this applicant ({len(others)})")
        st.dataframe(others, hide_index=True, width="stretch")

    if app.status == "stopped_incomplete":
        st.warning("Stopped at the completeness check: the application is incomplete, so nothing "
                   "was scored. Ask the applicant for the missing items and submit again.",
                   icon="⏹️")

    show_documents(app)

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

    st.markdown("##### Flags (rule checks and pattern review)")
    flags = sorted(app.flags, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.source != "rule"))
    if not flags:
        st.success("No flags.", icon="✅")
    for flag in flags:
        show_flag(flag)

    st.markdown("##### Model risk score")
    if score is None:
        st.info("No risk score recorded.")
    else:
        c1, c2 = st.columns([1, 3])
        c1.metric("Probability of default", format_probability(score.default_probability))
        c1.caption(f"Model {score.model_version}. Trained on synthetic data; a prototype score, "
                   "not a real-world estimate.")
        with c2:
            baseline = baseline_log_odds(score.model_version)
            if baseline is None:
                st.caption(f"Model file {score.model_version} is no longer in models/, so the "
                           "baseline is unknown; showing contributions only.")
            else:
                st.caption("How the score adds up: start at the baseline (an average applicant), "
                           "add each term's contribution, end at this applicant's score. "
                           "**Red steps raise** default risk; **blue steps lower** it. Pairs "
                           "(A × B) are interaction terms.")
                st.plotly_chart(waterfall_chart(baseline, score.contributions), width="stretch",
                                key=f"waterfall-{app.id}")
            with st.expander("Contribution bar chart", expanded=baseline is None):
                st.plotly_chart(contribution_chart(score.contributions), width="stretch",
                                key=f"contrib-{app.id}")

    show_recommendation(rec)
    st.info("This is a recommendation awaiting loan officer review. The system does not approve "
            "or deny loans; only an officer's decision on the review page is final.", icon="👤")
