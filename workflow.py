"""The whole application workflow, steps 1 to 9, used by the dashboard and the evaluation.

    1  completeness check        code
    2  Document Reader           LLM (vision, JSON schema, thinking off; 1 retry on invalid output)
    3  extraction validation     code (computes annual income)
    4  Financial Calculator      code
    5a rule checks               code
    5b pattern review            LLM (JSON schema, thinking on)
    5c evidence check            code
    6  Risk Scorer               ML (EBM)
    7  Decision Coordinator      LLM (tool-calling loop, at most 8 calls; 1 retry on invalid answer)
    8  output validation         code
    9  Review Gatekeeper         code
    10 loan officer review       human, on the review page (never here)

Run state lives in the database, not in memory: every step commits its results (and an audit
row) as soon as it finishes and updates applications.current_step. continue_application() looks
at what the current run has already saved and runs only the missing steps, so the same code
serves a fresh run, a Resume after an interruption, and a Re-run after an officer's correction.
If the LLM is unavailable, the application is still saved with calculations, rule checks and the
risk score, and marked LLM_UNAVAILABLE for additional review.
"""
import os
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import llm
from calculations import FORMULAS, CalcResult, check_rules, run_calculations
from coordinator import coordinate, documented_income, stored_form
from db import (Applicant, Application, AuditLog, Calculation, ExtractedField, Flag,
                Recommendation, RiskScore, log, utcnow)
from document_reader import PERIODS_PER_YEAR, read_document
from documents import DOC_TYPES, DocumentError, save_document
from pattern_review import run_pattern_review
from validation import gatekeeper, validate_recommendation

REQUIRED_FIELDS = ["full_name", "person_income", "person_education", "person_home_ownership",
                   "loan_amnt", "loan_intent", "loan_int_rate", "credit_score",
                   "previous_loan_defaults_on_file"]
OPTIONAL_FIELDS = ["loan_percent_income", "term_months"]
STEPS = ["Completeness check", "Document Reader", "Extraction validation", "Calculations",
         "Rule checks", "Pattern review", "Risk score", "Decision Coordinator",
         "Output validation", "Gatekeeper"]
# Applicant profile columns and the form fields they come from.
PROFILE = {"education": "person_education", "annual_income": "person_income",
           "employment_years": "person_emp_exp", "home_ownership": "person_home_ownership",
           "credit_score": "credit_score", "credit_history_years": "cb_person_cred_hist_length"}
FINAL_STATUSES = ("approved", "denied", "pending_applicant", "stopped_incomplete")

Progress = Callable[[str, str, str], None]  # (step, state: running/done/skipped/failed, detail)


def interrupted_after() -> timedelta:
    """A run counts as interrupted after this long without progress (INTERRUPTED_AFTER_MINUTES,
    default 15)."""
    return timedelta(minutes=float(os.getenv("INTERRUPTED_AFTER_MINUTES") or 15))


@dataclass
class WorkflowResult:
    application_id: int
    status: str
    stopped_incomplete: list[str] = field(default_factory=list)
    llm_used: bool = False
    marked: bool = False
    review_reasons: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    steps_run: list[str] = field(default_factory=list)


def missing_items(form: dict, files: dict) -> list[str]:
    missing = [f for f in REQUIRED_FIELDS if form.get(f) in (None, "")]
    missing += [f"document: {t}" for t in DOC_TYPES if not files.get(t)]
    return missing


def _create(session: Session, form: dict, applicant_id: Optional[int]) -> Application:
    profile = {col: form.get(f) for col, f in PROFILE.items()}
    profile["previous_defaults"] = str(form.get("previous_loan_defaults_on_file")).lower() == "yes"
    changes = {}
    if applicant_id is None:
        applicant = Applicant(full_name=form["full_name"], age=None, gender=None, **profile)
    else:
        applicant = session.get(Applicant, applicant_id)
        # The applicant row holds the latest profile; each application keeps the values it was
        # scored with in its intake audit entry and calculations.
        for col, value in profile.items():
            if value is not None and getattr(applicant, col) != value:
                changes[col] = [getattr(applicant, col), value]
                setattr(applicant, col, value)
    app = Application(applicant=applicant, loan_amount=form.get("loan_amnt"),
                      loan_intent=form.get("loan_intent"), interest_rate=form.get("loan_int_rate"),
                      term_months=form.get("term_months"), status="submitted",
                      current_step="intake")
    session.add(app)
    session.flush()
    log(session, "intake", "code",
        "created applicant and application" if applicant_id is None
        else "created application for existing applicant", app.id,
        applicant_id=applicant.id, returning_applicant=applicant_id is not None,
        profile_changes=changes,
        form={k: form.get(k) for k in REQUIRED_FIELDS + OPTIONAL_FIELDS})
    return app


def start_application(form: dict[str, Any], files: dict[str, tuple[str, bytes]], *, engine,
                      applicant_id: Optional[int] = None,
                      progress: Optional[Progress] = None) -> tuple[int, list[str]]:
    """Step 1: create the application and store its documents. Returns (id, missing items)."""
    report = progress or (lambda *a: None)
    report(STEPS[0], "running", "")
    missing = missing_items(form, files)
    if any(m in REQUIRED_FIELDS for m in missing):
        raise ValueError("Required form fields are missing: " + ", ".join(missing))
    with Session(engine) as s:
        app = _create(s, form, applicant_id)
        docs, rejected = [], []
        for doc_type, item in files.items():
            if not item:
                continue
            try:
                docs.append(save_document(s, app.id, doc_type, item[0], item[1]))
            except DocumentError as exc:
                rejected.append(f"document: {doc_type} ({exc})")
        missing += rejected
        if missing:
            app.status, app.current_step = "stopped_incomplete", "completeness_check"
            log(s, "completeness_check", "code", "stopped: application incomplete", app.id,
                missing=missing, status="stopped_incomplete")
            s.commit()
            report(STEPS[0], "failed", "Missing: " + ", ".join(missing))
            return app.id, missing
        s.flush()
        log(s, "completeness_check", "code", "application complete", app.id,
            documents=[{"doc_type": d.doc_type, "sha256": d.sha256, "pages": d.page_count}
                       for d in docs])
        app.current_step = "completeness_check"
        s.commit()
        report(STEPS[0], "done", f"{len(docs)} documents stored")
        return app.id, []


def run_application(form: dict[str, Any], files: dict[str, tuple[str, bytes]], *,
                    engine, scorer, applicant_id: Optional[int] = None,
                    model: Optional[str] = None, use_llm: bool = True,
                    progress: Optional[Progress] = None) -> WorkflowResult:
    """Run steps 1 to 9 for one application. files maps doc_type -> (filename, bytes)."""
    started = time.monotonic()
    app_id, missing = start_application(form, files, engine=engine, applicant_id=applicant_id,
                                        progress=progress)
    step1 = round(time.monotonic() - started, 2)
    if missing:
        return WorkflowResult(app_id, "stopped_incomplete", stopped_incomplete=missing,
                              timings={"completeness": step1}, steps_run=["completeness"])
    result = continue_application(app_id, engine=engine, scorer=scorer, model=model,
                                  use_llm=use_llm, progress=progress)
    result.timings = {"completeness": step1, **result.timings}
    result.steps_run.insert(0, "completeness")
    return result


# ----------------------------------------------------------------------- resumable steps

def _ran(app: Application, step: str, action: Optional[str] = None) -> bool:
    return any(a.step == step and (action is None or a.action == action) for a in app.run_audit())


def _last(app: Application, step: str, action: str) -> Optional[AuditLog]:
    rows = [a for a in app.run_audit() if a.step == step and a.action == action]
    return rows[-1] if rows else None


def llm_problem(app: Application) -> Optional[str]:
    """The first LLM failure recorded in the current run, if any."""
    for a in app.run_audit():
        if a.step == "llm" and a.action == "LLM steps skipped":
            return a.details.get("reason")
        if a.step == "extraction_validation" and (a.details or {}).get("error"):
            return a.details["error"]
        if (a.step, a.action) in (("pattern_review", "pattern review failed"),
                                  ("coordinator", "coordinator failed")):
            return a.details.get("error")
    return None


def _revalidate(s: Session, app: Application) -> None:
    """Step 3 of a re-run: recompute annual income from the pay stub's current (possibly
    corrected) fields. A new row is added; the earlier one stays."""
    for doc in app.documents:
        if doc.doc_type != "pay_stub":
            continue
        latest = doc.latest_fields()
        gross, period = latest.get("gross_pay"), latest.get("pay_period")
        income = None
        try:
            if gross and period and gross.field_value and period.field_value in PERIODS_PER_YEAR:
                income = round(float(gross.field_value) * PERIODS_PER_YEAR[period.field_value], 2)
        except ValueError:
            income = None
        reviewed = all(f is not None and (f.verified_by_human or not f.needs_review)
                       for f in (gross, period))
        s.add(ExtractedField(document_id=doc.id, field_name="annual_income",
                             field_value=None if income is None else str(income),
                             source_page=gross.source_page if gross else None,
                             extraction_model="code", needs_review=income is None or not reviewed))
        log(s, "extraction_validation", "code", "re-validated after correction", app.id,
            document_id=doc.id, gross_pay=gross.field_value if gross else None,
            pay_period=period.field_value if period else None, annual_income=income)


def _extraction_needs_review(app: Application) -> bool:
    for doc in app.documents:
        fields = doc.latest_fields()
        if not fields:
            return True  # never read
        if any(f.needs_review and not f.verified_by_human for f in fields.values()):
            return True
    return False


def continue_application(app_id: int, *, engine, scorer, model: Optional[str] = None,
                         use_llm: bool = True,
                         progress: Optional[Progress] = None) -> WorkflowResult:
    """Run every step of the current run whose result is missing (steps 2 to 9)."""
    report = progress or (lambda *a: None)
    timings: dict[str, float] = {}
    ran: list[str] = []
    clock = time.monotonic()

    def begin(step):  # saved before the step runs, so an interruption shows where it stopped
        app.current_step = step
        s.commit()

    def finish(step):
        nonlocal clock
        now = time.monotonic()
        timings[step] = round(now - clock, 2)
        ran.append(step)
        clock = now

    with Session(engine) as s:
        app = s.get(Application, app_id)
        rerun = app.run_start() is not None

        # LLM availability is decided once per run and recorded, so a resumed run is consistent.
        if llm_problem(app) is None:
            reason = None
            if not use_llm:
                reason = "LLM unavailable: LLM steps were turned off for this run"
            elif not llm.available():
                reason = "LLM unavailable: the school server did not answer"
            if reason:
                log(s, "llm", "code", "LLM steps skipped", app_id, reason=reason)
                s.commit()

        # 2-3. Document Reader and extraction validation
        if rerun:
            report(STEPS[1], "skipped", "re-run after a correction: documents are not read again")
            if not _ran(app, "extraction_validation", "re-validated after correction"):
                begin("extraction_validation")
                _revalidate(s, app)
                s.commit()
                finish("extraction_validation")
            report(STEPS[2], "done", "annual income recomputed from the current fields")
        elif llm_problem(app) is None:
            report(STEPS[1], "running", "")
            read_now = 0
            for doc in sorted(app.documents, key=lambda d: d.id):
                if doc.extracted_fields:
                    continue  # read in an earlier attempt of this run
                begin("document_reader")
                read_document(s, doc, model=model)
                s.commit()  # one document at a time, so a resume keeps finished ones
                read_now += 1
            if read_now:
                finish("document_reader")
            report(STEPS[1], "done", f"{read_now} read now, {len(app.documents) - read_now} earlier")
        else:
            report(STEPS[1], "skipped", llm_problem(app))
        s.refresh(app)
        income = documented_income(app)
        needs_review = _extraction_needs_review(app)
        if not rerun:
            report(STEPS[2], "failed" if needs_review else "done",
                   "needs review" if needs_review else f"documented income {income:,.2f}"
                   if income else "no documented income")

        # 4. Calculations
        form = stored_form(s, app)
        if not app.current_calculations:
            begin("calculations")
            report(STEPS[3], "running", "")
            for c in run_calculations(form):
                s.add(Calculation(application_id=app_id, metric=c.metric, value=c.value,
                                  inputs=c.inputs, code_used=FORMULAS[c.metric],
                                  assumptions=c.assumptions))
            log(s, "calculations", "code", "ran financial calculations", app_id)
            s.commit()
            finish("calculations")
        calcs = [CalcResult(c.metric, c.value, c.inputs, c.assumptions)
                 for c in app.current_calculations]
        report(STEPS[3], "done", ", ".join(f"{c.metric} {c.value}" for c in calcs))

        # 5a. Rule checks (documented income comes from the pay stub extraction)
        if not _ran(app, "rule_check"):
            begin("rule_check")
            report(STEPS[4], "running", "")
            for f in check_rules(form, calcs, documented_income=income):
                s.add(Flag(application_id=app_id, code=f.code, source="rule", severity=f.severity,
                           evidence=f.evidence))
            log(s, "rule_check", "code", "checked rules", app_id, documented_income=income)
            s.commit()
            finish("rule_check")
        report(STEPS[4], "done", ", ".join(f.code for f in app.current_flags
                                           if f.source == "rule") or "no flags")

        # 5b-5c. Pattern review and evidence check
        pattern_done = _ran(app, "evidence_check") or _ran(app, "pattern_review",
                                                           "pattern review failed")
        if llm_problem(app) is None and not pattern_done:
            begin("pattern_review")
            report(STEPS[5], "running", "")
            try:
                kept = run_pattern_review(s, app, form, model=model)
                report(STEPS[5], "done", ", ".join(k["code"] for k in kept) or "no findings")
            except llm.LLMError as exc:
                log(s, "pattern_review", "code", "pattern review failed", app_id, error=str(exc))
                report(STEPS[5], "failed", str(exc))
            s.commit()
            finish("pattern_review")
        elif pattern_done:
            report(STEPS[5], "done", "done earlier in this run")
        else:
            report(STEPS[5], "skipped", llm_problem(app))

        # 6. Risk score
        if app.current_risk_score is None:
            begin("risk_scoring")
            report(STEPS[6], "running", "")
            model_input = dict(form)
            if model_input.get("loan_percent_income") is None:
                model_input["loan_percent_income"] = round(form["loan_amnt"]
                                                           / form["person_income"], 2)
            result = scorer.score(model_input)
            s.add(RiskScore(application_id=app_id, model_version=result["model_version"],
                            default_probability=result["default_probability"],
                            contributions=result["contributions"]))
            log(s, "risk_scoring", "model", "scored default risk", app_id,
                model_version=result["model_version"],
                default_probability=result["default_probability"],
                top_risk_drivers=result["top_risk_drivers"],
                loan_percent_income_used=model_input["loan_percent_income"])
            s.commit()
            finish("risk_score")
        score = app.current_risk_score
        report(STEPS[6], "done", f"probability {score.default_probability}")
        flags = [{"code": f.code, "severity": f.severity, "source": f.source}
                 for f in app.current_flags]

        # 7-8. Decision Coordinator and output validation (saved together, as one step)
        def validator(answer, tool_results):
            return validate_recommendation(answer, tool_results=tool_results, flags=flags,
                                           default_probability=score.default_probability,
                                           contribution_terms=score.contributions)

        coordinator_done = _ran(app, "output_validation") or _ran(app, "coordinator",
                                                                  "coordinator failed")
        if llm_problem(app) is None and not coordinator_done:
            begin("coordinator")
            report(STEPS[7], "running", "")
            out = coordinate(s, app, scorer=scorer, model=model, validator=validator)
            if out.error:
                log(s, "coordinator", "code", "coordinator failed", app_id, error=out.error)
                report(STEPS[7], "failed", out.error)
                report(STEPS[8], "skipped", "no recommendation to validate")
            else:
                log(s, "coordinator", "model", "recommendation written", app_id,
                    answer=out.answer if out.answer is not None else out.raw_answer[:2000],
                    tool_calls=out.tool_calls_used, limit_reached=out.limit_reached,
                    final_attempts=out.final_attempts, retry_reason=out.retry_reason)
                log(s, "output_validation", "code",
                    "validation passed" if not out.failures else "validation failed", app_id,
                    failures=out.failures, attempts=out.final_attempts)
                answer = out.answer if isinstance(out.answer, dict) else {}
                report(STEPS[7], "done", f"{answer.get('recommendation', 'unparseable')} after "
                       f"{out.tool_calls_used} tool calls")
                report(STEPS[8], "failed" if out.failures else "done",
                       "; ".join(out.failures) if out.failures else "passed")
            s.commit()
            finish("coordinator_and_validation")
        elif llm_problem(app) is not None:
            report(STEPS[7], "skipped", llm_problem(app))
            report(STEPS[8], "skipped", "no recommendation to validate")

        # 9. Gatekeeper and the recommendation row (never a decision)
        if app.current_recommendation is None:
            begin("gatekeeper")
            written = _last(app, "coordinator", "recommendation written")
            checked = (_last(app, "output_validation", "validation passed")
                       or _last(app, "output_validation", "validation failed"))
            answer = (written.details or {}).get("answer") if written else None
            failures = (checked.details or {}).get("failures") if checked else None
            limit = bool(written and written.details.get("limit_reached"))
            usable = isinstance(answer, dict)
            recommendation = answer.get("recommendation") if usable else None
            if recommendation not in ("approve", "deny", "refer"):
                recommendation = "refer"
            confidence = answer.get("confidence") if usable else None
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) \
                    or not 0 <= confidence <= 1:
                confidence = None
            explanation = None
            if usable and isinstance(answer.get("reason_codes"), list):
                reason_codes = [str(c) for c in answer["reason_codes"]]
                explanation = str(answer.get("explanation") or "") or None
            else:  # no usable LLM answer: the phase 1 reason codes
                ranked = sorted(score.contributions.items(), key=lambda kv: kv[1], reverse=True)
                reason_codes = ([f"MODEL_DRIVER:{t}" for t, v in ranked if v > 0][:3]
                                + [f["code"] for f in flags])
            reasons = gatekeeper(flags=flags, validation_failures=failures,
                                 extraction_needs_review=needs_review,
                                 confidence=confidence if usable else None,
                                 tool_limit_reached=limit, llm_error=llm_problem(app))
            s.add(Recommendation(application_id=app_id, recommendation=recommendation,
                                 reason_codes=reason_codes, confidence=confidence,
                                 passed_validation=None if failures is None else not failures,
                                 marked_for_additional_review=bool(reasons),
                                 review_reasons=reasons, explanation=explanation))
            log(s, "gatekeeper", "code", "marked for additional review" if reasons
                else "not marked", app_id, recommendation=recommendation, review_reasons=reasons)
            app.status, app.current_step = "in_review", "officer_review"
            log(s, "status", "code", "status set to in_review", app_id, status="in_review")
            if rerun:
                _log_rerun_changes(s, app, recommendation)
            s.commit()
            finish("gatekeeper")
        elif app.status == "submitted":  # everything was saved; only the hand-over was missing
            app.status, app.current_step = "in_review", "officer_review"
            log(s, "status", "code", "status set to in_review", app_id, status="in_review")
            s.commit()
        rec = app.current_recommendation
        report(STEPS[9], "done", "marked: " + ", ".join(rec.review_reasons)
               if rec.review_reasons else "not marked; goes to the officer")
        return WorkflowResult(app_id, app.status, llm_used=llm_problem(app) is None,
                              marked=rec.marked_for_additional_review,
                              review_reasons=list(rec.review_reasons), timings=timings,
                              steps_run=ran)


# ----------------------------------------------------------------------- recovery and re-runs

def interrupted_runs(session: Session, now=None) -> list[tuple[Application, Any]]:
    """Applications whose run stopped partway: status neither final nor in_review, and no audit
    activity for interrupted_after(). Returns (application, time of last activity)."""
    now = now or utcnow()
    out = []
    for app in session.scalars(select(Application).where(
            Application.status.not_in(FINAL_STATUSES + ("in_review",)))).all():
        last = max((a.created_at for a in app.audit_logs), default=app.created_at)
        if now - last >= interrupted_after():
            out.append((app, last))
    return sorted(out, key=lambda pair: pair[1])


def resume(app_id: int, officer: str, *, engine, scorer, model: Optional[str] = None,
           use_llm: bool = True, progress: Optional[Progress] = None) -> WorkflowResult:
    """Continue an interrupted run once, from the first step whose result is missing."""
    with Session(engine) as s:
        app = s.get(Application, app_id)
        log(s, "recovery", f"human:{officer}", "resume requested", app_id,
            current_step=app.current_step, status=app.status)
        s.commit()
    return continue_application(app_id, engine=engine, scorer=scorer, model=model,
                                use_llm=use_llm, progress=progress)


def mark_stopped(app_id: int, officer: str, *, engine) -> None:
    with Session(engine) as s:
        app = s.get(Application, app_id)
        log(s, "recovery", f"human:{officer}", "marked as stopped", app_id, reason="INTERRUPTED",
            status_from=app.status, current_step=app.current_step)
        app.status, app.current_step = "stopped_incomplete", "interrupted"
        s.commit()


def rerun_checks(app_id: int, officer: str, *, engine, scorer, model: Optional[str] = None,
                 use_llm: bool = True, progress: Optional[Progress] = None) -> WorkflowResult:
    """Re-run steps 3 to 9 with the officer's corrected fields (no Document Reader). New results
    are added as new rows; the earlier rows stay for the record."""
    with Session(engine) as s:
        app = s.get(Application, app_id)
        if app.status != "in_review":
            raise ValueError(f"Application #{app_id} is {app.status}; only applications in review "
                             "can be re-checked.")
        rec = app.current_recommendation
        log(s, Application.RERUN_STEP, f"human:{officer}", Application.RERUN_STARTED, app_id,
            officer=officer, previous_flags=[f.code for f in app.current_flags],
            previous_recommendation=rec.recommendation if rec else None,
            previous_review_reasons=list(rec.review_reasons) if rec else [])
        app.status, app.current_step = "submitted", "rerun"
        s.commit()
    return continue_application(app_id, engine=engine, scorer=scorer, model=model,
                                use_llm=use_llm, progress=progress)


def _log_rerun_changes(s: Session, app: Application, recommendation: str) -> None:
    start = app.run_start()
    before = set(start.details.get("previous_flags", []))
    after = {f.code for f in app.current_flags}
    log(s, Application.RERUN_STEP, "code", "re-run checks finished", app.id,
        officer=start.details.get("officer"), flags_removed=sorted(before - after),
        flags_added=sorted(after - before),
        recommendation_from=start.details.get("previous_recommendation"),
        recommendation_to=recommendation)
