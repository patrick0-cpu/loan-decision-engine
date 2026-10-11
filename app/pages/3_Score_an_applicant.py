import streamlit as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shared import engine, historical, no_model_warning, scorer, show_application

st.set_page_config(page_title="Score an applicant", page_icon="📝", layout="wide")

from calculations import FORMULAS, check_rules, run_calculations  # noqa: E402
from db import (Applicant, Application, Calculation, Flag, Recommendation, RiskScore,  # noqa: E402
                log)

st.title("Score an applicant")
st.caption("Gender and age are not collected here: the model never uses them. The result is a "
           "recommendation for a loan officer, never a decision.")

model = scorer()
if model is None:
    no_model_warning()
    st.stop()


def options(column: str) -> list[str]:
    return sorted(historical(clean=True)[column].dropna().unique())


# Applicant profile columns and the form fields they come from.
PROFILE = {"education": "person_education", "annual_income": "person_income",
           "employment_years": "person_emp_exp", "home_ownership": "person_home_ownership",
           "credit_score": "credit_score", "credit_history_years": "cb_person_cred_hist_length"}


def score_application(form: dict, applicant_id: int | None = None) -> int:
    """Run the pipeline in order and save every step. Returns the new application id.
    With applicant_id, the new application is linked to that existing applicant."""
    with Session(engine()) as s:
        # 1. Applicant and application
        profile = {col: form[field] for col, field in PROFILE.items()}
        profile["previous_defaults"] = form["previous_loan_defaults_on_file"] == "Yes"
        changes = {}
        if applicant_id is None:
            applicant = Applicant(full_name=form["full_name"], age=None, gender=None, **profile)
        else:
            applicant = s.get(Applicant, applicant_id)
            # The applicant row holds the latest profile; each application keeps the values it
            # was scored with in its calculations and audit entries.
            for col, value in profile.items():
                if getattr(applicant, col) != value:
                    changes[col] = [getattr(applicant, col), value]
                    setattr(applicant, col, value)
        application = Application(
            applicant=applicant, loan_amount=form["loan_amnt"], loan_intent=form["loan_intent"],
            interest_rate=form["loan_int_rate"], term_months=form["term_months"],
            status="submitted", current_step="intake")
        s.add(application)
        s.flush()
        app_id = application.id
        log(s, "intake", "code",
            "created applicant and application" if applicant_id is None
            else "created application for existing applicant", app_id,
            applicant_id=applicant.id, returning_applicant=applicant_id is not None,
            profile_changes=changes, loan_amount=form["loan_amnt"],
            stated_income=form["person_income"],
            loan_percent_income_provided=form["loan_percent_income"] is not None)

        # 2. Calculations
        application.current_step = "calculations"
        calcs = run_calculations(form)
        for c in calcs:
            s.add(Calculation(application_id=app_id, metric=c.metric, value=c.value,
                              inputs=c.inputs, code_used=FORMULAS[c.metric],
                              assumptions=c.assumptions))
        log(s, "calculations", "code", "ran financial calculations", app_id,
            results={c.metric: c.value for c in calcs})

        # 3. Rule checks
        application.current_step = "rule_check"
        flags = check_rules(form, calcs, documented_income=form["documented_income"])
        for f in flags:
            s.add(Flag(application_id=app_id, code=f.code, source="rule", severity=f.severity,
                       evidence=f.evidence))
        log(s, "rule_check", "code", "checked rules", app_id,
            flags=[{"code": f.code, "severity": f.severity} for f in flags])

        # 4. Risk score. If loan_percent_income was left blank, the model gets the calculated ratio.
        application.current_step = "risk_scoring"
        model_input = dict(form)
        if model_input["loan_percent_income"] is None:
            model_input["loan_percent_income"] = round(form["loan_amnt"] / form["person_income"], 2)
        result = model.score(model_input)
        s.add(RiskScore(application_id=app_id, model_version=result["model_version"],
                        default_probability=result["default_probability"],
                        contributions=result["contributions"]))
        log(s, "risk_scoring", "model", "scored default risk", app_id,
            model_version=result["model_version"],
            default_probability=result["default_probability"],
            top_risk_drivers=result["top_risk_drivers"],
            loan_percent_income_used=model_input["loan_percent_income"])

        # 5. Recommendation: always refer to an officer
        application.current_step = "recommendation"
        critical = [f.code for f in flags if f.severity == "critical"]
        reason_codes = ([f"MODEL_DRIVER:{name}" for name, _ in result["top_risk_drivers"]]
                        + [f.code for f in flags])
        s.add(Recommendation(application_id=app_id, recommendation="refer",
                             reason_codes=reason_codes, confidence=None, passed_validation=None,
                             marked_for_additional_review=bool(critical),
                             review_reasons=critical))
        log(s, "recommendation", "code", "recommended refer to officer", app_id,
            reason_codes=reason_codes, marked_for_additional_review=bool(critical))

        # 6. Hand over to the officer
        application.status = "in_review"
        application.current_step = "officer_review"
        log(s, "status", "code", "status set to in_review", app_id, status="in_review")

        s.commit()
        return app_id


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
        documented_income = st.number_input(
            "Documented income (optional)", min_value=0.0, value=None, step=1000.0, format="%.0f",
            help="Income shown on supporting documents. Compared with stated income.")
    submitted = st.form_submit_button("Score applicant", type="primary")

if submitted:
    form = dict(full_name=(full_name or "").strip(), person_income=person_income,
                person_emp_exp=person_emp_exp, person_education=person_education,
                person_home_ownership=person_home_ownership, loan_amnt=loan_amnt,
                loan_intent=loan_intent, loan_int_rate=loan_int_rate,
                loan_percent_income=loan_percent_income, term_months=term_months,
                credit_score=credit_score, cb_person_cred_hist_length=cb_person_cred_hist_length,
                previous_loan_defaults_on_file=previous_default,
                documented_income=documented_income)
    optional = {"loan_percent_income", "term_months", "documented_income"}
    missing = [k for k, v in form.items() if k not in optional and v in (None, "")]
    if missing:
        st.error("Please fill in: " + ", ".join(k.replace("_", " ") for k in missing))
    else:
        try:
            st.session_state["scored_app_id"] = score_application(
                form, applicant_id=existing[0] if existing else None)
        except Exception as exc:  # nothing is saved: the transaction rolls back
            st.error(f"Scoring failed and nothing was saved: {exc}")

app_id = st.session_state.get("scored_app_id")
if app_id is not None:
    st.divider()
    with Session(engine()) as s:
        app = s.get(Application, app_id)
        if app is not None:
            show_application(app)
