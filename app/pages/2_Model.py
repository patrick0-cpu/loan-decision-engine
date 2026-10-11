from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from shared import (REFERENCE_LINE, SERIES_1, SERIES_2, no_model_warning, page_layout,
                    pair_heatmap, scorer, shape_chart, style_bars, term_label)

st.set_page_config(page_title="Model", page_icon="🧠", layout="wide")
page_layout()
st.title("Model")

model = scorer()
if model is None:
    no_model_warning()
    st.stop()

meta = model.metadata
trained = datetime.fromisoformat(meta["trained_at"])
st.markdown(f"**Version** `{meta['model_version']}` · **trained** {trained:%Y-%m-%d %H:%M} UTC · "
            f"**predicts** {meta['predicts']} · **training rows** {meta['trained_rows']:,} "
            f"(test rows {meta['test_rows']:,})")
st.caption(meta["note"])

metrics = meta["metrics"]
c1, c2, c3, c4 = st.columns(4)
c1.metric("AUC", f"{metrics['auc']:.3f}")
c2.metric("Accuracy at 0.5", f"{metrics['accuracy_at_0_5']:.1%}")
c3.metric("Brier score", f"{metrics['brier_score']:.3f}", help="Lower is better")
c4.metric("Test default rate", f"{metrics['test_default_rate']:.1%}")

st.markdown(f"**Features** ({len(meta['features'])}): "
            + ", ".join(term_label(f) for f in meta["features"]))
st.markdown("**Excluded:** " + ", ".join(f"`{f}`" for f in meta["excluded_protected"])
            + " (protected; fairness report only), "
            + ", ".join(f"`{f}`" for f in meta.get("excluded_other", []))
            + " (data artifact; handled by a rule)")

perf = meta.get("performance")
shapes = meta.get("shape_functions")
if perf is None or shapes is None:
    st.info("This model was trained before performance and shape-function data were saved. "
            "Retrain with `python train_ebm.py --target-means default` to see those graphs.")
else:
    # 1. Performance
    st.subheader("Performance (test set)")
    left, right = st.columns([3, 2])
    with left:
        roc = perf["roc"]
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines", name="Random guess",
                                 line=dict(color=REFERENCE_LINE, dash="dash", width=1),
                                 hoverinfo="skip"))
        fig.add_trace(go.Scatter(
            x=roc["fpr"], y=roc["tpr"], mode="lines", name="Model",
            line=dict(color=SERIES_1, width=2), customdata=roc["threshold"],
            hovertemplate="False positive rate %{x:.1%}<br>True positive rate %{y:.1%}"
                          "<br>Threshold %{customdata:.2f}<extra></extra>"))
        fig.update_layout(title=f"ROC curve (AUC {metrics['auc']:.3f})", height=380,
                          margin=dict(l=10, r=10, t=50, b=10), hovermode="closest",
                          legend=dict(orientation="h", y=-0.2, x=0))
        fig.update_xaxes(title="False positive rate (repaid loans flagged)", range=[0, 1],
                         tickformat=".0%")
        fig.update_yaxes(title="True positive rate (defaults caught)", range=[0, 1.02],
                         tickformat=".0%")
        st.plotly_chart(fig, width="stretch", key="roc")
    with right:
        cm = perf["confusion_matrix"]
        n, r = cm["counts"], cm["rates"]
        st.markdown(f"**Confusion matrix at {cm['threshold']}**")
        st.caption("High risk = predicted probability at or above the threshold.")
        st.dataframe(pd.DataFrame(
            {"Predicted low risk": [f"{n['tn']:,} (correct)", f"{n['fn']:,} (missed default)"],
             "Predicted high risk": [f"{n['fp']:,} (false alarm)", f"{n['tp']:,} (correct)"]},
            index=["Actually repaid", "Actually defaulted"]), width="stretch")
        st.markdown(f"- Defaults caught: **{r['true_positive_rate']:.1%}** "
                    f"(missed {r['false_negative_rate']:.1%})\n"
                    f"- Repaid loans flagged high risk: **{r['false_positive_rate']:.1%}**\n"
                    f"- Of those flagged high risk, actually defaulted: "
                    f"**{r['precision']:.1%}**")

    # 2. Calibration
    st.subheader("Calibration")
    st.caption("Do predicted probabilities match what happened? Each point is one band of "
               "predicted probability. Points on the diagonal are well calibrated; points below "
               "it mean the model is over-confident about default, points above it mean it "
               "under-estimates risk.")
    cal = pd.DataFrame([c for c in perf["calibration"] if c["count"]])
    left, right = st.columns([3, 2])
    with left:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines", name="Perfect calibration",
                                 line=dict(color=REFERENCE_LINE, dash="dash", width=1),
                                 hoverinfo="skip"))
        fig.add_trace(go.Scatter(
            x=cal["mean_predicted"], y=cal["actual_default_rate"], mode="lines+markers",
            name="Model", line=dict(color=SERIES_1, width=2), marker=dict(size=9),
            customdata=cal[["bin", "count"]],
            hovertemplate="Band %{customdata[0]}<br>Average predicted %{x:.1%}"
                          "<br>Actual default rate %{y:.1%}<br>%{customdata[1]:,} loans"
                          "<extra></extra>"))
        fig.update_layout(height=380, margin=dict(l=10, r=10, t=20, b=10),
                          legend=dict(orientation="h", y=-0.2, x=0))
        fig.update_xaxes(title="Average predicted probability of default", range=[0, 1],
                         tickformat=".0%")
        fig.update_yaxes(title="Actual default rate", range=[0, 1.02], tickformat=".0%")
        st.plotly_chart(fig, width="stretch", key="calibration")
    with right:
        st.dataframe(cal.rename(columns={
            "bin": "Predicted band", "count": "Loans", "mean_predicted": "Avg. predicted",
            "actual_default_rate": "Actual rate"}), hide_index=True, width="stretch",
            column_config={"Avg. predicted": st.column_config.NumberColumn(format="percent"),
                           "Actual rate": st.column_config.NumberColumn(format="percent")})

    # 3. Score distribution
    st.subheader("Score distribution")
    st.caption("How predicted probabilities spread out for loans that were actually repaid and "
               "actually defaulted. Good separation means few repaid loans on the right and few "
               "defaults on the left.")
    dist = perf["score_distribution"]
    edges = dist["bin_edges"]
    mids = [(a + b) / 2 for a, b in zip(edges[:-1], edges[1:])]
    labels = [f"{a:.0%}-{b:.0%}" for a, b in zip(edges[:-1], edges[1:])]
    fig = go.Figure()
    for name, key, color in [("Actually repaid", "repaid", SERIES_1),
                             ("Actually defaulted", "defaulted", SERIES_2)]:
        fig.add_trace(go.Bar(x=mids, y=dist[key], name=name, marker_color=color,
                             width=(edges[1] - edges[0]) * 0.9, customdata=labels,
                             hovertemplate=f"{name}<br>Predicted %{{customdata}}<br>"
                                           "%{y:,} loans<extra></extra>"))
    fig.update_layout(barmode="group", bargap=0.1, height=340, barcornerradius=3,
                      margin=dict(l=10, r=10, t=20, b=10),
                      legend=dict(orientation="h", y=1.1, x=0))
    fig.add_vline(x=0.5, line_dash="dash", line_width=1, line_color=REFERENCE_LINE,
                  annotation_text="high-risk threshold 0.5", annotation_position="top")
    fig.update_xaxes(title="Predicted probability of default", tickformat=".0%", range=[0, 1])
    fig.update_yaxes(title="Loans")
    st.plotly_chart(fig, width="stretch", key="distribution")

    # 4. Shape functions
    st.subheader("How the model sees each feature")
    st.caption("Each line shows how much one feature adds to the log-odds of default at each "
               "value, with everything else held aside. **Above zero raises risk, below zero "
               "lowers it.** The shaded band is the uncertainty range; the grey bars underneath "
               "show how many training loans had each value, so wide bands over thin bars mean "
               "little data.")
    single = {term_label(t): t for t in meta["features"] if t in shapes}
    label = st.selectbox("Feature", list(single), key="shape-feature")
    shape = shapes[single[label]]
    st.plotly_chart(shape_chart(shape, label), width="stretch", key=f"shape-{single[label]}")
    if shape.get("density", {}).get("above_view"):
        st.caption(f"Zoomed to the 99th percentile ({shape['view_max']:,.4g}); "
                   f"{shape['density']['above_view']:,} training loans lie above it, up to "
                   f"{shape['x_max']:,.4g}. Use the chart's autoscale button to see the full range.")

    # 5. Interactions
    pairs = [t for t, v in shapes.items() if v["type"] == "pair"]
    if pairs:
        st.subheader("Interactions")
        st.caption(f"The model has {len(pairs)} pair terms: extra risk from a combination of two "
                   "features beyond what each adds alone. Red cells raise risk, blue cells lower "
                   "it, grey is no effect. Pairs are listed from most to least important.")
        importance_of = meta["feature_importance"]
        pairs.sort(key=lambda t: -importance_of.get(t, 0))
        pair_labels = {f"{term_label(t)} (importance {importance_of.get(t, 0):.2f})": t
                       for t in pairs}
        pair = pair_labels[st.selectbox("Pair term", list(pair_labels), key="pair-term")]
        st.plotly_chart(pair_heatmap(shapes[pair]), width="stretch", key=f"pair-{pair}")

# 6. Global importance and fairness
st.subheader("Global feature importance")
ebm = model.ebm
importance = (pd.DataFrame({"term": ebm.term_names_, "importance": ebm.term_importances()})
              .sort_values("importance", ascending=False))
show_all = st.toggle(f"Show all {len(importance)} terms", value=False)
shown = importance if show_all else importance.head(15)
shown = shown.iloc[::-1]
fig = go.Figure(go.Bar(
    x=shown["importance"], y=[term_label(t) for t in shown["term"]], orientation="h",
    marker_color=SERIES_1, hovertemplate="%{y}<br>%{x:.4f}<extra></extra>"))
fig.update_xaxes(title="Mean absolute contribution (log-odds)")
st.plotly_chart(style_bars(fig, height=60 + 28 * len(shown)), width="stretch", key="importance")
st.caption("The EBM learns one shape function per feature plus pairwise interaction terms "
           "(shown as A × B).")

st.subheader("Fairness report (test set)")
fair = meta["fairness"]
# Models trained before phase 2 compared each group with the best group instead of the largest.
RATIO = "ratio_to_largest" if "ratio_to_largest" in fair["by_gender"][0] else "ratio_to_best"
REFERENCE = "largest" if RATIO == "ratio_to_largest" else "best"
SMALL = fair.get("small_sample_below", 100)
st.caption(f"High risk = predicted probability at or above {fair['high_risk_threshold']}. "
           "Favorable rate = share not flagged high risk. Each group's favorable rate is divided "
           f"by the {REFERENCE} group's in its category. Groups below the dashed "
           f"{fair['four_fifths_line']} line fail the four-fifths rule of thumb. Groups with "
           f"fewer than {SMALL} people are marked *small sample*: their ratios are noisy.")


def fairness_chart(records: list[dict]) -> go.Figure:
    small = [r["count"] < SMALL for r in records]
    fig = go.Figure(go.Bar(
        x=[r["group"] + ("<br>(small sample)" if s else "") for r, s in zip(records, small)],
        y=[r[RATIO] for r in records],
        marker_color=[REFERENCE_LINE if s else SERIES_1 for s in small],
        text=[f"{r[RATIO]:.2f}<br>n={r['count']:,.0f}" for r in records],
        textposition="outside", cliponaxis=False,
        customdata=[[r["count"], r["favorable_rate"]] for r in records],
        hovertemplate="%{x}<br>Ratio %{y:.3f}<br>Favorable rate %{customdata[1]:.1%}"
                      "<br>%{customdata[0]:,} applicants<extra></extra>"))
    fig.add_hline(y=fair["four_fifths_line"], line_dash="dash", line_color=REFERENCE_LINE,
                  annotation_text=f"four-fifths line ({fair['four_fifths_line']})",
                  annotation_position="bottom right")
    fig.update_layout(height=360, barcornerradius=4, bargap=0.4, showlegend=False,
                      margin=dict(l=10, r=10, t=30, b=10))
    fig.update_yaxes(title=f"Favorable rate ÷ {REFERENCE} group", range=[0, 1.3])
    return fig


left, right = st.columns(2)
for col, title, key in [(left, "By gender", "by_gender"), (right, "By age band", "by_age_band")]:
    with col:
        st.markdown(f"**{title}**")
        st.plotly_chart(fairness_chart(fair[key]), width="stretch", key=f"fair-{key}")
        table = pd.DataFrame(fair[key])
        table["small_sample"] = ["small sample" if c < SMALL else "" for c in table["count"]]
        table = table.rename(columns={
            "group": "Group", "count": "Count", "avg_default_probability": "Avg. probability",
            "share_high_risk": "Share high risk", "favorable_rate": "Favorable rate",
            RATIO: f"Ratio to {REFERENCE}", "small_sample": "Sample"})
        st.dataframe(table, hide_index=True, width="stretch")

if fair["warnings"]:
    for w in fair["warnings"]:
        st.warning(w, icon="⚠️")
else:
    st.success(f"No group falls below the {fair['four_fifths_line']} ratio.", icon="✅")

st.markdown("**Possible age proxies** (correlation with age on the cleaned rows)")
proxies = fair["age_proxy_correlations"]
st.dataframe(pd.DataFrame({"Feature": [term_label(k) for k in proxies],
                           "Correlation with age": list(proxies.values())}),
             hide_index=True)
st.caption("Both features are model inputs and track age closely, so the model can partly "
           "reflect age even though age itself is excluded. Small groups (for example 50+) make "
           "the ratios noisy.")
