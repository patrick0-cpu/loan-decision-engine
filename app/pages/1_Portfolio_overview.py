import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from shared import REFERENCE_LINE, SERIES_1, SERIES_2, historical, style_bars

st.set_page_config(page_title="Portfolio overview", page_icon="📊", layout="wide")

from calculations import RULES  # noqa: E402

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

c1, c2, c3, c4 = st.columns(4)
c1.metric("Rows", f"{len(df):,}")
c2.metric("Default rate", f"{overall:.1%}")
c3.metric("Average loan amount", f"{df['loan_amnt'].mean():,.0f}")
c4.metric("Average credit score", f"{df['credit_score'].mean():.0f}")


def rate_chart(column: str) -> go.Figure:
    g = (df.groupby(column)["loan_status"].agg(rate="mean", count="size")
         .sort_values("rate").reset_index())
    fig = go.Figure(go.Bar(
        x=g["rate"], y=g[column], orientation="h", marker_color=SERIES_1,
        text=[f"{r:.1%}" for r in g["rate"]], textposition="outside", cliponaxis=False,
        customdata=g["count"],
        hovertemplate="%{y}<br>Default rate %{x:.1%}<br>%{customdata:,} loans<extra></extra>"))
    fig.add_vline(x=overall, line_dash="dash", line_width=1, line_color=REFERENCE_LINE,
                  annotation_text=f"overall {overall:.1%}", annotation_position="top")
    fig.update_xaxes(title="Default rate", tickformat=".0%", rangemode="tozero")
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
    st.plotly_chart(histogram("loan_percent_income", "Loan-to-income", RULES["max_loan_to_income"],
                              f"limit {RULES['max_loan_to_income']}", 35),
                    width="stretch", key="hist-lti")
