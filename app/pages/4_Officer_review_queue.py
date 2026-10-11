import pandas as pd
import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared import engine, format_probability, latest, show_application

st.set_page_config(page_title="Officer review queue", page_icon="👤", layout="wide")

from db import Application, ExtractedField, OfficerReview, log  # noqa: E402
from validation import GENERAL_REASONS  # noqa: E402

# Officer choice -> (officer_reviews.decision, new application status)
DECISIONS = {
    "Approve": ("approved", "approved"),
    "Deny": ("denied", "denied"),
    "Request more information": ("request_info", "pending_applicant"),
}

st.title("Officer review queue")

if message := st.session_state.pop("review_message", None):
    st.success(message, icon="✅")


def is_marked(app: Application) -> bool:
    rec = latest(app.recommendations)
    return bool(rec and rec.marked_for_additional_review)


with Session(engine()) as s:
    queue = s.scalars(select(Application).where(Application.status == "in_review")).all()
    queue = sorted(queue, key=lambda a: (not is_marked(a), a.created_at))
    if not queue:
        st.info("No applications are waiting for review. Score an applicant to add one.")
        st.stop()

    rows = []
    for a in queue:
        score = latest(a.risk_scores)
        rows.append({
            "Application": a.id, "Applicant": a.applicant.full_name,
            "Marked": "🚩 yes" if is_marked(a) else "",
            "Loan amount": a.loan_amount,
            "Default probability": format_probability(score.default_probability) if score else "",
            "Flags": len(a.flags), "Submitted (UTC)": a.created_at.strftime("%Y-%m-%d %H:%M")})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
        "Loan amount": st.column_config.NumberColumn(format="%,.0f")})
    st.caption("Marked cases (at least one critical flag) are listed first.")

    labels = {f"#{a.id}: {a.applicant.full_name}" + ("  🚩 marked" if is_marked(a) else ""): a.id
              for a in queue}
    selected_id = labels[st.selectbox("Application to review", list(labels))]
    app = s.get(Application, selected_id)
    st.divider()
    show_application(app)
    rec = latest(app.recommendations)
    reason_options = list(dict.fromkeys(
        [f.code for f in app.flags] + (rec.reason_codes if rec else []) + GENERAL_REASONS))
    field_options = {
        f"{d.doc_type} · {f.field_name} = {f.field_value if f.field_value is not None else '(not found)'}"
        f" (page {f.source_page or '?'})" + ("  ⚠️ needs review" if f.needs_review else "")
        + ("  ✅ verified" if f.verified_by_human else ""): (f.id, f.field_value)
        for d in sorted(app.documents, key=lambda d: d.id)
        for f in sorted(d.extracted_fields, key=lambda f: f.id) if f.extraction_model != "code"}

if field_options:
    st.divider()
    st.subheader("Verify extracted fields")
    st.caption("Check a value against the document (use *View / download* above), then confirm it "
               "or enter the correct value. This marks the field as verified by a human and is "
               "written to the audit log. Rule checks and the recommendation are not re-run.")
    with st.form(f"verify-{selected_id}"):
        verifier = st.text_input("Your name *", key=f"verifier-{selected_id}")
        picked = st.selectbox("Field", list(field_options))
        corrected = st.text_input("Correct value (leave blank to confirm the value as extracted)")
        verify = st.form_submit_button("Confirm or correct field")
    if verify:
        if not verifier.strip():
            st.error("Enter your name to verify a field.")
        else:
            field_id, old_value = field_options[picked]
            new_value = corrected.strip() or old_value
            with Session(engine()) as s:
                f = s.get(ExtractedField, field_id)
                f.field_value, f.verified_by_human = new_value, True
                log(s, "field_verification", f"human:{verifier.strip()}",
                    "corrected extracted field" if new_value != old_value
                    else "confirmed extracted field", selected_id, field_id=field_id,
                    document_id=f.document_id, field_name=f.field_name, old_value=old_value,
                    new_value=new_value)
                field_name = f.field_name
                s.commit()
            st.session_state["review_message"] = (
                f"Field {field_name} " + ("corrected" if new_value != old_value else "confirmed")
                + f" by {verifier.strip()}.")
            st.rerun()

st.divider()
st.subheader("Officer decision")
with st.form(f"decision-{selected_id}", clear_on_submit=False):
    officer_name = st.text_input("Your name *")
    choice = st.radio("Decision *", list(DECISIONS), horizontal=True, index=None)
    reason_codes = st.multiselect("Reason codes (at least one required to deny)", reason_options)
    notes = st.text_area("Notes")
    submitted = st.form_submit_button("Record decision", type="primary")

if submitted:
    officer_name = officer_name.strip()
    errors = []
    if not officer_name:
        errors.append("Enter your name.")
    if choice is None:
        errors.append("Choose a decision.")
    if choice == "Deny" and not reason_codes:
        errors.append("A denial needs at least one reason code.")
    for e in errors:
        st.error(e)

    if not errors:
        decision, new_status = DECISIONS[choice]
        with Session(engine()) as s:
            app = s.get(Application, selected_id)
            if app.status != "in_review":
                st.error(f"Application #{app.id} is no longer in review (status: {app.status}).")
                st.stop()
            rec = latest(app.recommendations)
            s.add(OfficerReview(application_id=app.id, officer_name=officer_name,
                                decision=decision, reason_codes=reason_codes,
                                notes=notes.strip() or None))
            old_status = app.status
            app.status = new_status
            app.current_step = "officer_decision"
            log(s, "officer_review", f"human:{officer_name}", f"decision: {decision}", app.id,
                decision=decision, reason_codes=reason_codes, notes=notes.strip() or None,
                status_from=old_status, status_to=new_status,
                system_recommendation=rec.recommendation if rec else None)
            s.commit()
        st.session_state["review_message"] = (
            f"Recorded {decision} for application #{selected_id} by {officer_name}. "
            f"Status is now {new_status}.")
        st.rerun()
