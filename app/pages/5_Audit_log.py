import json

import pandas as pd
import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared import engine

st.set_page_config(page_title="Audit log", page_icon="📜", layout="wide")

from db import AuditLog  # noqa: E402

st.title("Audit log")
st.caption("Read-only. Every action by the code, the model, and loan officers, newest first. "
           "Rows are never updated or deleted.")

with Session(engine()) as s:
    entries = s.scalars(select(AuditLog).order_by(AuditLog.id.desc())).all()
    df = pd.DataFrame([{
        "id": e.id,
        "time (UTC)": e.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        "application": e.application_id,
        "step": e.step,
        "actor": e.actor,
        "action": e.action,
        "details": json.dumps(e.details, default=str),
    } for e in entries])

if df.empty:
    st.info("The audit log is empty.")
    st.stop()

NO_APP = "(none)"
app_values = [NO_APP if pd.isna(v) else int(v) for v in df["application"]]
f1, f2, f3 = st.columns(3)
apps = f1.multiselect("Application", sorted(set(app_values), key=lambda v: (v == NO_APP, v)))
steps = f2.multiselect("Step", sorted(df["step"].unique()))
actors = f3.multiselect("Actor", sorted(df["actor"].unique()))

mask = pd.Series(True, index=df.index)
if apps:
    mask &= pd.Series(app_values, index=df.index).isin(apps)
if steps:
    mask &= df["step"].isin(steps)
if actors:
    mask &= df["actor"].isin(actors)

st.caption(f"Showing {mask.sum():,} of {len(df):,} entries.")
st.dataframe(df[mask], hide_index=True, width="stretch",
             column_config={"application": st.column_config.NumberColumn(format="%d")})
