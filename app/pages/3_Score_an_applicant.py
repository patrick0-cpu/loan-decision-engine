import streamlit as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shared import (CONTEXT_KEY, engine, historical, llm_status, no_model_warning, page_layout,
                    scorer, show_application)

st.set_page_config(page_title="Score an applicant", page_icon="📝", layout="wide")
page_layout()

from db import Applicant, Application  # noqa: E402
from documents import MAX_BYTES  # noqa: E402
from workflow import PROFILE, STEPS, run_application  # noqa: E402

st.title("Score an applicant")
st.caption("Gender and age are not collected here: the model never uses them. The result is a "
           "recommendation for a loan officer, never a decision.")

model = scorer()
if model is None:
    no_model_warning()
    st.stop()


def options(column: str) -> list[str]:
    return sorted(historical(clean=True)[column].dropna().unique())


kind = st.radio("Applicant", ["New applicant", "Existing applicant"], horizontal=True,
                key="applicant-kind")
existing = None  # (id, name, profile dict) of the chosen returning applicant
if kind == "Existing applicant":
    query = st.text_input("Search by name", key="applicant-search").strip()
    with Session(engine()) as s:
        stmt = select(Applicant).order_by(Applicant.full_name, Applicant.id)
        if query:
            stmt = stmt.where(func.lower(Applicant.full_name).contains(query.lower()))
        matches = s.scalars(stmt.limit(50)).all()
        choices = {f"#{a.id}: {a.full_name} (created {a.created_at:%Y-%m-%d}, "
                   f"{len(a.applications)} earlier application(s))":
                   (a.id, a.full_name, {col: getattr(a, col) for col in PROFILE}
                    | {"previous_defaults": a.previous_defaults}) for a in matches}
    if not choices:
        st.info("No applicant matches that name. Choose *New applicant* to add one.")
        st.stop()
    existing = choices[st.selectbox("Applicant", list(choices), key="applicant-pick")]
    st.caption("The form starts from this applicant's latest profile. Update anything that has "
               "changed; the new application is linked to the same applicant.")


def default(column):
    return existing[2].get(column) if existing else None


def index_of(values, column):
    return values.index(default(column)) if default(column) in values else None


form_key = f"applicant-{existing[0]}" if existing else "applicant-new"
with st.form(form_key):
    if existing:
        full_name = existing[1]
        st.markdown(f"**Applicant #{existing[0]}: {existing[1]}**")
    else:
        full_name = st.text_input("Applicant name *")
    a, b, c = st.columns(3)
    with a:
        st.markdown("**Applicant**")
        person_income = st.number_input("Annual income *", min_value=1.0,
                                        value=default("annual_income"), step=1000.0,
                                        format="%.0f")
        person_emp_exp = st.number_input("Employment experience (years) *", min_value=0,
                                         max_value=80, value=default("employment_years"), step=1)
        educations, homes = options("person_education"), options("person_home_ownership")
        person_education = st.selectbox("Education *", educations,
                                        index=index_of(educations, "education"))
        person_home_ownership = st.selectbox("Home ownership *", homes,
                                             index=index_of(homes, "home_ownership"))
    with b:
        st.markdown("**Loan**")
        loan_amnt = st.number_input("Loan amount *", min_value=1.0, value=None, step=500.0,
                                    format="%.0f")
        loan_intent = st.selectbox("Loan intent *", options("loan_intent"), index=None)
        loan_int_rate = st.number_input("Interest rate (% per year) *", min_value=0.0,
                                        max_value=100.0, value=None, step=0.25, format="%.2f")
        loan_percent_income = st.number_input(
            "Loan as share of income (optional)", min_value=0.0, max_value=10.0, value=None,
            step=0.01, format="%.2f",
            help="As reported on the application, for example 0.25. Leave blank to use the "
                 "calculated loan / income; the consistency check is then skipped.")
        term_months = st.number_input("Loan term in months (optional)", min_value=1,
                                      max_value=480, value=None, step=12,
                                      help="Leave blank to assume 36 months.")
    with c:
        st.markdown("**Credit**")
        credit_score = st.number_input("Credit score *", min_value=300, max_value=850,
                                       value=default("credit_score"), step=1)
        cb_person_cred_hist_length = st.number_input("Credit history length (years) *",
                                                     min_value=0.0, max_value=80.0,
                                                     value=default("credit_history_years"),
                                                     step=1.0, format="%.0f")
        previous_default = st.radio("Previous loan default on file *", ["No", "Yes"],
                                    horizontal=True, index=1 if default("previous_defaults") else 0,
                                    help="Used by the rule check only, not by the model.")
    st.markdown("**Documents** (PDF, PNG or JPG, up to "
                f"{MAX_BYTES // (1024 * 1024)} MB each). Both are required; without them the "
                "application stops at the completeness check.")
    d1, d2 = st.columns(2)
    pay_stub = d1.file_uploader("Pay stub *", type=["pdf", "png", "jpg", "jpeg"])
    bank_statement = d2.file_uploader("Bank statement *", type=["pdf", "png", "jpg", "jpeg"])
    submitted = st.form_submit_button("Run the workflow", type="primary")

ok, model_name = llm_status()
if ok:
    st.caption(f"LLM steps use **{model_name}** on the school server.")
else:
    st.warning("The LLM server is unavailable. Applications are still saved with calculations, "
               "rule checks and the risk score, and marked LLM_UNAVAILABLE for review.", icon="⚠️")

if submitted:
    form = dict(full_name=(full_name or "").strip(), person_income=person_income,
                person_emp_exp=person_emp_exp, person_education=person_education,
                person_home_ownership=person_home_ownership, loan_amnt=loan_amnt,
                loan_intent=loan_intent, loan_int_rate=loan_int_rate,
                loan_percent_income=loan_percent_income, term_months=term_months,
                credit_score=credit_score, cb_person_cred_hist_length=cb_person_cred_hist_length,
                previous_loan_defaults_on_file=previous_default)
    optional = {"loan_percent_income", "term_months"}
    missing = [k for k, v in form.items() if k not in optional and v in (None, "")]
    if missing:
        st.error("Please fill in: " + ", ".join(k.replace("_", " ") for k in missing))
    else:
        files = {t: (f.name, f.getvalue()) for t, f in
                 (("pay_stub", pay_stub), ("bank_statement", bank_statement)) if f is not None}
        icons = {"running": "⏳", "done": "✅", "skipped": "⏭️", "failed": "⚠️"}
        with st.status("Running the workflow…", expanded=True) as status:
            lines = {step: st.empty() for step in STEPS}
            for step in STEPS:
                lines[step].markdown(f"▫️ {step}")

            def progress(step, state, detail):
                text = f"{icons.get(state, '')} **{step}**" + (f": {detail}" if detail else "")
                lines[step].markdown(text)

            try:
                result = run_application(form, files, engine=engine(), scorer=model,
                                         applicant_id=existing[0] if existing else None,
                                         model=model_name if ok else None, use_llm=ok,
                                         progress=progress)
                st.session_state["scored_app_id"] = result.application_id
                label = ("Stopped: application incomplete" if result.status == "stopped_incomplete"
                         else "Done: the application is waiting for officer review")
                status.update(label=label, state="error" if result.stopped_incomplete
                              else "complete", expanded=bool(result.stopped_incomplete))
            except Exception as exc:  # steps already finished stay saved with their audit rows
                status.update(label="The workflow failed", state="error")
                st.error(f"The workflow failed: {exc}")

app_id = st.session_state.get("scored_app_id")
st.session_state[CONTEXT_KEY] = app_id  # the assistant answers about the application on screen
if app_id is not None:
    st.divider()
    with Session(engine()) as s:
        app = s.get(Application, app_id)
        if app is not None:
            show_application(app)
