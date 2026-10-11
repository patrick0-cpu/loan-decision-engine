import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from shared import REFERENCE_LINE, SERIES_1, SERIES_2, historical, page_layout, style_bars

st.set_page_config(page_title="Portfolio overview", page_icon="📊", layout="wide")
page_layout()

from calculations import DEFAULT_TERM_MONTHS, RULES  # noqa: E402

SMALL = 100          # groups smaller than this are greyed out and labelled "small sample"
INTENT_RING = "#bdbbb5"  # neutral: intents are identified by their text labels
SMALL_BAR = "#d3d1cb"
PROTECTED_NOTE = ("Gender and age are shown to understand the data and to check fairness. The "
                  "risk model does not use them, and they must never be used to decide an "
                  "application.")

st.title("Portfolio overview")

include_all = st.toggle("Include applicants with a previous default on file", value=False)
st.caption("By default this page shows the 22,142 cleaned rows. In this synthetic dataset, every "
           "applicant with a previous default on file has `loan_status = 0` (repaid), which is an "
           "artifact of how the data was generated, not real borrower behavior. Those rows are "
           "excluded from training and analysis; prior defaulters are handled by the "
           "`PREVIOUS_DEFAULT_ON_FILE` rule instead.")

df = historical(clean=not include_all).copy()
df["Outcome"] = df["loan_status"].map({0: "Repaid (0)", 1: "Defaulted (1)"})
overall = df["loan_status"].mean()

overview, insights = st.tabs(["Overview", "Borrower insights"])

# ---------------------------------------------------------------------------------- Overview
with overview:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rows", f"{len(df):,}")
    c2.metric("Default rate", f"{overall:.1%}")
    c3.metric("Average loan amount", f"{df['loan_amnt'].mean():,.0f}")
    c4.metric("Average credit score", f"{df['credit_score'].mean():.0f}")

    def rate_chart(column: str) -> go.Figure:
        g = (df.groupby(column)["loan_status"].agg(rate="mean", count="size")
             .sort_values("rate").reset_index())
        small = g["count"] < SMALL
        fig = go.Figure(go.Bar(
            x=g["rate"], y=[f"{v} (small sample)" if s else str(v) for v, s in zip(g[column], small)],
            orientation="h", marker_color=[SMALL_BAR if s else SERIES_1 for s in small],
            text=[f"{r:.1%} · n={n:,}" for r, n in zip(g["rate"], g["count"])],
            textposition="outside", cliponaxis=False, customdata=g["count"],
            hovertemplate="%{y}<br>Default rate %{x:.1%}<br>%{customdata:,} loans<extra></extra>"))
        fig.add_vline(x=overall, line_dash="dash", line_width=1, line_color=REFERENCE_LINE,
                      annotation_text=f"overall {overall:.1%}", annotation_position="top")
        fig.update_xaxes(title="Default rate", tickformat=".0%", rangemode="tozero")
        fig.update_layout(margin=dict(l=10, r=90, t=30, b=10))
        return style_bars(fig, height=70 + 45 * len(g))

    st.subheader("Default rate by group")
    tabs = st.tabs(["Loan intent", "Home ownership", "Education"])
    for tab, column in zip(tabs, ["loan_intent", "person_home_ownership", "person_education"]):
        with tab:
            st.plotly_chart(rate_chart(column), width="stretch", key=f"rate-{column}")

    def histogram(column: str, title: str, limit: float, limit_text: str, nbins: int) -> go.Figure:
        fig = px.histogram(df, x=column, color="Outcome", nbins=nbins, barmode="stack",
                           category_orders={"Outcome": ["Repaid (0)", "Defaulted (1)"]},
                           color_discrete_map={"Repaid (0)": SERIES_1, "Defaulted (1)": SERIES_2})
        fig.update_traces(marker_line_width=1, marker_line_color="rgba(255,255,255,0.6)")
        fig.add_vline(x=limit, line_dash="dash", line_width=1.5, line_color=REFERENCE_LINE,
                      annotation_text=limit_text, annotation_position="top right")
        fig.update_layout(height=360, bargap=0.02, margin=dict(l=10, r=10, t=40, b=10),
                          legend=dict(orientation="h", y=1.12, x=0, title=None))
        fig.update_xaxes(title=title)
        fig.update_yaxes(title="Loans")
        return fig

    st.subheader("Distributions")
    st.caption("Stacked by outcome. Dashed lines mark the rule limits from the dataset analysis.")
    left, right = st.columns(2)
    with left:
        st.plotly_chart(histogram("credit_score", "Credit score", RULES["min_credit_score"],
                                  f"minimum {RULES['min_credit_score']}", 50),
                        width="stretch", key="hist-credit")
    with right:
        st.plotly_chart(histogram("loan_percent_income", "Loan-to-income",
                                  RULES["max_loan_to_income"],
                                  f"limit {RULES['max_loan_to_income']}", 35),
                        width="stretch", key="hist-lti")

# ---------------------------------------------------------------------------------- Insights
with insights:
    st.caption(f"{len(df):,} loans ({'all rows' if include_all else 'cleaned rows'}; follows the "
               "toggle above). The dataset has no dates, so the line charts show ordered groups, "
               f"not time. Groups with fewer than {SMALL} loans are greyed out and labelled "
               "*small sample*.")

    # 1. Who borrows and who defaults
    st.subheader("Who borrows and who defaults")
    by_intent = df.groupby("loan_intent")["loan_status"].agg(count="size", defaults="sum")
    ids, labels, parents, values, colors, hover = [], [], [], [], [], []
    for intent, row in by_intent.sort_values("count", ascending=False).iterrows():
        share = row["count"] / len(df)
        ids.append(intent); labels.append(f"{intent}<br>{share:.1%}"); parents.append("")
        values.append(int(row["count"])); colors.append(INTENT_RING)
        hover.append(f"{intent}<br>{share:.1%} of borrowers<br>{int(row['count']):,} loans")
        for outcome, n, color in [("Repaid", row["count"] - row["defaults"], SERIES_1),
                                  ("Defaulted", row["defaults"], SERIES_2)]:
            within = n / row["count"]
            ids.append(f"{intent}/{outcome}"); labels.append(f"{outcome}<br>{within:.0%}")
            parents.append(intent); values.append(int(n)); colors.append(color)
            hover.append(f"{intent} · {outcome}<br>{within:.1%} of this intent<br>{int(n):,} loans")
    fig = go.Figure(go.Sunburst(ids=ids, labels=labels, parents=parents, values=values,
                                branchvalues="total", marker=dict(colors=colors,
                                                                  line=dict(color="white", width=2)),
                                hovertext=hover, hoverinfo="text", insidetextorientation="radial"))
    fig.update_layout(height=520, margin=dict(l=10, r=10, t=10, b=10))
    left, right = st.columns([3, 2])
    left.plotly_chart(fig, width="stretch", key="sunburst")
    right.markdown("**How to read it.** The inner ring is each loan intent's share of all "
                   "borrowers. The outer ring splits each intent into "
                   f"<span style='color:{SERIES_1}'>■</span> **repaid** and "
                   f"<span style='color:{SERIES_2}'>■</span> **defaulted**, as a share of that "
                   "intent. Click an intent to zoom in.", unsafe_allow_html=True)
    right.dataframe(by_intent.assign(default_rate=by_intent["defaults"] / by_intent["count"])
                    .sort_values("count", ascending=False)
                    .rename(columns={"count": "Loans", "defaults": "Defaults",
                                     "default_rate": "Default rate"}),
                    width="stretch", column_config={
                        "Default rate": st.column_config.NumberColumn(format="percent")})

    # 2. Default rate across ordered groups
    st.subheader("Default rate across ordered groups")
    lti = df["loan_amnt"] / df["person_income"]
    ordered = {
        "Credit score band": pd.cut(df["credit_score"], [0, 500, 550, 600, 650, 700, 750, 900],
                                    right=False, labels=["<500", "500-549", "550-599", "600-649",
                                                         "650-699", "700-749", "750+"]),
        "Income band (deciles)": pd.qcut(df["person_income"], 10, duplicates="drop"),
        "Loan-to-income band": pd.cut(lti, [0, .05, .10, .15, .20, .25, .30, .40, np.inf],
                                      right=False, labels=["<0.05", "0.05-0.10", "0.10-0.15",
                                                           "0.15-0.20", "0.20-0.25", "0.25-0.30",
                                                           "0.30-0.40", "0.40+"]),
        "Age band": pd.cut(df["person_age"], [0, 25, 35, 50, np.inf], right=False,
                           labels=["<25", "25-34", "35-49", "50+"]),
        "Years of employment": pd.cut(df["person_emp_exp"], [0, 1, 3, 5, 10, 20, np.inf],
                                      right=False, labels=["<1", "1-2", "3-4", "5-9", "10-19",
                                                           "20+"]),
    }
    variable = st.selectbox("Variable", list(ordered), key="ordered-variable")
    if variable == "Age band":
        st.info(PROTECTED_NOTE, icon="⚖️")
    bands = ordered[variable]
    if variable.startswith("Income"):
        bands = bands.cat.rename_categories([f"{iv.left:,.0f}-{iv.right:,.0f}"
                                             for iv in bands.cat.categories])
    g = df.groupby(bands, observed=True)["loan_status"].agg(rate="mean", count="size").reset_index()
    g.columns = ["band", "rate", "count"]
    g["band"] = g["band"].astype(str)
    big = g["count"] >= SMALL
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=g["band"], y=g["rate"].where(big), mode="lines+markers+text", name="Default rate",
        line=dict(color=SERIES_2, width=2), marker=dict(size=9),
        text=[f"{r:.0%}" if b else "" for r, b in zip(g["rate"], big)], textposition="top center",
        customdata=g["count"],
        hovertemplate="%{x}<br>Default rate %{y:.1%}<br>%{customdata:,} loans<extra></extra>"))
    if (~big).any():
        fig.add_trace(go.Scatter(
            x=g["band"][~big], y=g["rate"][~big], mode="markers+text", name="Small sample",
            marker=dict(size=9, color=SMALL_BAR, line=dict(color=REFERENCE_LINE, width=1)),
            text=[f"small sample<br>n={n}" for n in g["count"][~big]], textposition="top center",
            customdata=g["count"][~big], cliponaxis=False,
            hovertemplate="%{x} (small sample)<br>Default rate %{y:.1%}<br>%{customdata:,} loans"
                          "<extra></extra>"))
    fig.add_hline(y=overall, line_dash="dash", line_width=1, line_color=REFERENCE_LINE,
                  annotation_text=f"overall {overall:.1%}", annotation_position="top left")
    fig.update_layout(height=400, margin=dict(l=10, r=40, t=30, b=10), showlegend=False)
    fig.update_xaxes(title=variable, type="category")
    fig.update_yaxes(title="Default rate", tickformat=".0%", range=[0, 1.08])
    st.plotly_chart(fig, width="stretch", key="ordered-line")
    st.caption("Group sizes: " + " · ".join(f"{b}: {n:,}" for b, n in zip(g["band"], g["count"])))

    # 3. Default rate by category
    st.subheader("Default rate by category")
    categories = {"Education": "person_education", "Home ownership": "person_home_ownership",
                  "Loan intent": "loan_intent", "Gender": "person_gender"}
    cols = st.columns(2)
    for i, (title, column) in enumerate(categories.items()):
        with cols[i % 2]:
            st.markdown(f"**{title}**")
            if column == "person_gender":
                st.info(PROTECTED_NOTE, icon="⚖️")
            st.plotly_chart(rate_chart(column), width="stretch", key=f"insight-{column}")

    # 4. Loan size by purpose (two measures on different scales: two charts, not two axes)
    st.subheader("Loan size by purpose")
    purpose = (df.assign(lti=lti).groupby("loan_intent")
               .agg(median_amount=("loan_amnt", "median"), median_lti=("lti", "median"),
                    count=("loan_amnt", "size")).sort_values("median_amount").reset_index())
    left, right = st.columns(2)
    for col, measure, title, fmt in [(left, "median_amount", "Median loan amount", ",.0f"),
                                     (right, "median_lti", "Median loan-to-income", ".2f")]:
        fig = go.Figure(go.Bar(
            x=purpose[measure], y=purpose["loan_intent"], orientation="h", marker_color=SERIES_1,
            text=[f"{v:{fmt}} · n={n:,}" for v, n in zip(purpose[measure], purpose["count"])],
            textposition="outside", cliponaxis=False, customdata=purpose["count"],
            hovertemplate="%{y}<br>" + title + " %{x:" + fmt + "}<br>%{customdata:,} loans"
                          "<extra></extra>"))
        fig.update_xaxes(title=title)
        fig.update_layout(margin=dict(l=10, r=100, t=30, b=10))
        col.plotly_chart(style_bars(fig, height=330), width="stretch", key=f"purpose-{measure}")

    # 5. Rule limits in the data
    st.subheader("Rule limits in the data")
    r = df["loan_int_rate"] / 100 / 12
    n = DEFAULT_TERM_MONTHS
    payment = np.where(r == 0, df["loan_amnt"] / n, df["loan_amnt"] * r / (1 - (1 + r) ** -n))
    pti = payment / (df["person_income"] / 12)
    limits = {
        f"Payment-to-income ≥ {RULES['max_payment_to_income']} (36-month term)":
            pti.round(4) >= RULES["max_payment_to_income"],
        f"Loan-to-income ≥ {RULES['max_loan_to_income']}": lti.round(4) >= RULES["max_loan_to_income"],
        f"Credit score < {RULES['min_credit_score']}": df["credit_score"] < RULES["min_credit_score"],
    }
    if include_all:
        limits["Previous default on file"] = (df["previous_loan_defaults_on_file"].str.strip()
                                              .str.lower() == "yes")
    rows = []
    for name, crossed in limits.items():
        rows.append({"limit": name, "share": crossed.mean(), "n_crossed": int(crossed.sum()),
                     "rate_crossed": df.loc[crossed, "loan_status"].mean() if crossed.any() else None,
                     "rate_other": df.loc[~crossed, "loan_status"].mean()})
    lim = pd.DataFrame(rows)
    axis_labels = [f"{r['limit']}<br>{r['share']:.1%} of borrowers (n={r['n_crossed']:,})"
                   for r in rows]
    fig = go.Figure()
    for label, key, color in [("Crossing the limit", "rate_crossed", SERIES_2),
                              ("Everyone else", "rate_other", SERIES_1)]:
        fig.add_trace(go.Bar(
            y=axis_labels, x=lim[key], name=label, orientation="h", marker_color=color,
            text=[f"{v:.1%}" if pd.notna(v) else "" for v in lim[key]], textposition="outside",
            cliponaxis=False,
            hovertemplate="%{y}<br>" + label + ": default rate %{x:.1%}<extra></extra>"))
    fig.update_layout(barmode="group", height=110 + 90 * len(lim), barcornerradius=4,
                      bargap=0.3, margin=dict(l=10, r=60, t=30, b=10),
                      legend=dict(orientation="h", y=1.15, x=0))
    fig.update_xaxes(title="Default rate", tickformat=".0%", range=[0, 1.1])
    fig.update_yaxes(autorange="reversed")
    st.plotly_chart(fig, width="stretch", key="rule-limits")
    st.dataframe(lim.rename(columns={"limit": "Rule limit", "share": "Share crossing",
                                     "n_crossed": "Loans crossing",
                                     "rate_crossed": "Default rate (crossing)",
                                     "rate_other": "Default rate (everyone else)"}),
                 hide_index=True, width="stretch", column_config={
                     c: st.column_config.NumberColumn(format="percent")
                     for c in ["Share crossing", "Default rate (crossing)",
                               "Default rate (everyone else)"]})
    st.caption("Payment-to-income uses the same formula as the Financial Calculator, with the "
               "36-month term the limit was derived from. Thresholds are the `RULES` values from "
               "the dataset analysis.")
