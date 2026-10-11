import pandas as pd
import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared import (CONTEXT_KEY, engine, format_probability, llm_status, page_layout, scorer,
                    show_application)

st.set_page_config(page_title="Officer review queue", page_icon="👤", layout="wide")
page_layout()

from db import Application, ExtractedField, OfficerReview, log  # noqa: E402
from validation import GENERAL_REASONS  # noqa: E402
from workflow import (STEPS, interrupted_after, interrupted_runs, mark_stopped,  # noqa: E402
                      rerun_checks, resume)

# Officer choice -> (officer_reviews.decision, new application status)
DECISIONS = {
    "Approve": ("approved", "approved"),
    "Deny": ("denied", "denied"),
    "Request more information": ("request_info", "pending_applicant"),
}
REC_LABELS = {"approve": "🟢 approve", "deny": "🔴 deny", "refer": "🟡 refer"}

st.title("Officer review queue")

if message := st.session_state.pop("review_message", None):
    st.success(message, icon="✅")


def is_marked(app: Application) -> bool:
    rec = app.current_recommendation
    return bool(rec and rec.marked_for_additional_review)


def run_with_progress(label: str, fn):
    """Run a workflow call with a per-step progress box; returns the result or None."""
    with st.status(label, expanded=True) as box:
        lines = {step: st.empty() for step in STEPS}
        icons = {"running": "⏳", "done": "✅", "skipped": "⏭️", "failed": "⚠️"}

        def progress(step, state, detail):
            lines[step].markdown(f"{icons.get(state, '')} **{step}**" + (f": {detail}" if detail else ""))

        try:
            result = fn(progress)
            box.update(label=f"{label}: done", state="complete")
            return result
        except Exception as exc:  # finished steps stay saved; the run shows as interrupted
            box.update(label=f"{label}: stopped", state="error")
            st.error(f"The run stopped: {exc}")
            return None


# ------------------------------------------------------------------ interrupted runs
ok, model_name = llm_status()
with Session(engine()) as s:
    stuck = [(a.id, a.applicant.full_name, a.current_step, last)
             for a, last in interrupted_runs(s)]
if stuck:
    st.subheader(f"Interrupted runs ({len(stuck)})")
    minutes = interrupted_after().total_seconds() / 60
    st.caption(f"These runs stopped partway: not in review, not finished, and no progress for "
               f"{minutes:.0f} minutes. **Resume** continues from the first step whose result is "
               "missing (finished steps are not redone). **Mark as stopped** closes the run as "
               "incomplete (reason INTERRUPTED). Both are written to the audit log.")
    st.dataframe(pd.DataFrame([{"Application": i, "Applicant": n, "Stopped at step": step,
                                "Last activity (UTC)": f"{t:%Y-%m-%d %H:%M}"}
                               for i, n, step, t in stuck]), hide_index=True, width="stretch")
    with st.form("interrupted"):
        options = {f"#{i}: {n} (stopped at {step})": i for i, n, step, _ in stuck}
        picked = options[st.selectbox("Interrupted run", list(options))]
        who = st.text_input("Your name *", key="recovery-name")
        c1, c2 = st.columns(2)
        do_resume = c1.form_submit_button("Resume", type="primary")
        do_stop = c2.form_submit_button("Mark as stopped")
    if (do_resume or do_stop) and not who.strip():
        st.error("Enter your name to resume or stop a run.")
    elif do_stop:
        mark_stopped(picked, who.strip(), engine=engine())
        st.session_state["review_message"] = f"Application #{picked} marked as stopped by {who.strip()}."
        st.rerun()
    elif do_resume:
        result = run_with_progress(f"Resuming application #{picked}", lambda progress: resume(
            picked, who.strip(), engine=engine(), scorer=scorer(),
            model=model_name if ok else None, use_llm=ok, progress=progress))
        if result is not None:
            st.session_state["review_message"] = (f"Application #{picked} resumed by {who.strip()}; "
                                                  f"status {result.status}.")
            st.rerun()
    st.divider()

# ------------------------------------------------------------------ the queue
with Session(engine()) as s:
    queue = s.scalars(select(Application).where(Application.status == "in_review")).all()
    queue = sorted(queue, key=lambda a: (not is_marked(a), a.created_at))  # marked, then oldest
    if not queue:
        st.info("No applications are waiting for review. Score an applicant to add one.")
        st.stop()

    rows = []
    for a in queue:
        score, rec = a.current_risk_score, a.current_recommendation
        recommendation = rec.recommendation if rec else "refer"
        if rec is None or rec.passed_validation is None:
            validation = "not run"
        else:
            validation = "✅ passed" if rec.passed_validation else "⚠️ failed"
        rows.append({
            "Application": a.id, "Applicant": a.applicant.full_name,
            "Loan amount": a.loan_amount,
            "Recommendation": REC_LABELS.get(recommendation, recommendation),
            "Default probability": format_probability(score.default_probability) if score else "",
            "Critical flags": sum(f.severity == "critical" for f in a.current_flags),
            "Validation": validation,
            "Confidence": "" if rec is None or rec.confidence is None else f"{rec.confidence:.2f}",
            "Marked for review": ("🚩 " + "; ".join(rec.review_reasons)) if is_marked(a) else "",
            "Submitted (UTC)": a.created_at.strftime("%Y-%m-%d %H:%M"),
            "_rec": recommendation, "_marked": is_marked(a)})
    table = pd.DataFrame(rows)

    f1, f2 = st.columns(2)
    by_label = {label: rec for rec, label in REC_LABELS.items()}
    rec_filter = [by_label[x] for x in f1.multiselect("Recommendation", list(by_label))]
    marked_filter = f2.radio("Marked for additional review", ["All", "Marked", "Not marked"],
                             horizontal=True)
    shown = table
    if rec_filter:
        shown = shown[shown["_rec"].isin(rec_filter)]
    if marked_filter != "All":
        shown = shown[shown["_marked"] == (marked_filter == "Marked")]
    st.dataframe(shown.drop(columns=["_rec", "_marked"]), hide_index=True, width="stretch",
                 column_config={"Loan amount": st.column_config.NumberColumn(format="%,.0f")})
    st.caption(f"Showing {len(shown)} of {len(table)} applications waiting for review. Marked "
               "cases come first, then the oldest. Recommendations are suggestions; only the "
               "officer's decision below is final. Applications scored before the LLM layer show "
               "\"refer\" with no confidence.")
    if shown.empty:
        st.info("No application matches these filters.")
        st.stop()
    queue = [a for a in queue if a.id in set(shown["Application"])]

    labels = {f"#{a.id}: {a.applicant.full_name}" + ("  🚩 marked" if is_marked(a) else ""): a.id
              for a in queue}
    selected_id = labels[st.selectbox("Application to review", list(labels))]
    if st.session_state.get(CONTEXT_KEY) != selected_id:
        st.session_state[CONTEXT_KEY] = selected_id  # the assistant answers about this one
        st.rerun()  # the panel already drew this run; redraw it with the new context
    app = s.get(Application, selected_id)
    st.divider()
    show_application(app)
    rec = app.current_recommendation
    reason_options = list(dict.fromkeys(
        [f.code for f in app.current_flags] + (rec.reason_codes if rec else []) + GENERAL_REASONS))
    field_options = {}
    any_verified = False
    for d in sorted(app.documents, key=lambda d: d.id):
        for f in d.latest_fields().values():
            if f.extraction_model == "code":
                continue  # computed by code from other fields; re-run checks recomputes it
            any_verified |= f.verified_by_human
            value = f.field_value if f.field_value is not None else "(not found)"
            label = (f"{d.doc_type} · {f.field_name} = {value} (page {f.source_page or '?'})"
                     + ("  ⚠️ needs review" if f.needs_review else "")
                     + ("  ✅ verified" if f.verified_by_human else ""))
            field_options[label] = (f.id, f.field_value)

if field_options:
    st.divider()
    st.subheader("Verify extracted fields")
    st.caption("Check a value against the document (use *View / download* above), then confirm it "
               "or enter the correct value. This marks the field as verified by a human and is "
               "written to the audit log. Then use **Re-run checks** to recalculate with it.")
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
                + f" by {verifier.strip()}. Use Re-run checks to recalculate with it.")
            st.rerun()

    if any_verified:
        with st.form(f"rerun-{selected_id}"):
            st.markdown("**Re-run checks** with the verified values: steps 3 to 9 again (extraction "
                        "validation, calculations, rules, pattern review, score, Coordinator, "
                        "validation, gatekeeper), without reading the documents again. New results "
                        "are added; earlier ones stay in the record.")
            rerunner = st.text_input("Your name *", key=f"rerunner-{selected_id}")
            do_rerun = st.form_submit_button("Re-run checks")
        if do_rerun:
            if not rerunner.strip():
                st.error("Enter your name to re-run the checks.")
            else:
                result = run_with_progress(
                    f"Re-running checks for application #{selected_id}",
                    lambda progress: rerun_checks(selected_id, rerunner.strip(), engine=engine(),
                                                  scorer=scorer(), model=model_name if ok else None,
                                                  use_llm=ok, progress=progress))
                if result is not None:
                    st.session_state["review_message"] = (
                        f"Checks re-run for application #{selected_id} by {rerunner.strip()}.")
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
            rec = app.current_recommendation
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
