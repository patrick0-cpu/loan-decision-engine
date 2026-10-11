"""The whole application workflow, steps 1 to 9, used by the dashboard and the evaluation.

    1  completeness check        code
    2  Document Reader           LLM (vision, JSON schema, thinking off)
    3  extraction validation     code (computes annual income)
    4  Financial Calculator      code
    5a rule checks               code
    5b pattern review            LLM (JSON schema, thinking on)
    5c evidence check            code
    6  Risk Scorer               ML (EBM)
    7  Decision Coordinator      LLM (tool-calling loop, at most 8 calls)
    8  output validation         code
    9  Review Gatekeeper         code
    10 loan officer review       human, on the review page (never here)

Each step commits, so the application and its audit trail survive a later failure. If the LLM
is unavailable, the application is still saved with calculations, rule checks and the risk
score, and marked LLM_UNAVAILABLE for additional review.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

import llm
from calculations import FORMULAS, check_rules, run_calculations
from coordinator import coordinate
from db import Applicant, Application, Calculation, Flag, Recommendation, RiskScore, log
from document_reader import read_document
from documents import DOC_TYPES, DocumentError, save_document
from pattern_review import run_pattern_review
from validation import gatekeeper, validate_recommendation

REQUIRED_FIELDS = ["full_name", "person_income", "person_emp_exp", "person_education",
                   "person_home_ownership", "loan_amnt", "loan_intent", "loan_int_rate",
                   "credit_score", "cb_person_cred_hist_length", "previous_loan_defaults_on_file"]
OPTIONAL_FIELDS = ["loan_percent_income", "term_months"]
STEPS = ["Completeness check", "Document Reader", "Extraction validation", "Calculations",
         "Rule checks", "Pattern review", "Risk score", "Decision Coordinator",
         "Output validation", "Gatekeeper"]
# Applicant profile columns and the form fields they come from.
PROFILE = {"education": "person_education", "annual_income": "person_income",
           "employment_years": "person_emp_exp", "home_ownership": "person_home_ownership",
           "credit_score": "credit_score", "credit_history_years": "cb_person_cred_hist_length"}

Progress = Callable[[str, str, str], None]  # (step, state: running/done/skipped/failed, detail)


@dataclass
class WorkflowResult:
    application_id: int
    status: str
    stopped_incomplete: list[str] = field(default_factory=list)
    llm_used: bool = False
    marked: bool = False
    review_reasons: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)


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


def run_application(form: dict[str, Any], files: dict[str, tuple[str, bytes]], *,
                    engine, scorer, applicant_id: Optional[int] = None,
                    model: Optional[str] = None, use_llm: bool = True,
                    progress: Optional[Progress] = None) -> WorkflowResult:
    """Run steps 1 to 9 for one application. files maps doc_type -> (filename, bytes)."""
    import time

    def report(step, state, detail=""):
        if progress:
            progress(step, state, detail)

    timings: dict[str, float] = {}
    clock = time.monotonic()

    def lap(step):
        nonlocal clock
        now = time.monotonic()
        timings[step] = round(now - clock, 2)
        clock = now

    with Session(engine) as s:
        # 1. Completeness check
        report(STEPS[0], "running")
        missing = missing_items(form, files)
        if any(m in REQUIRED_FIELDS for m in missing):
            raise ValueError("Required form fields are missing: " + ", ".join(missing))
        app = _create(s, form, applicant_id)
        app_id = app.id
        docs, rejected = [], []
        for doc_type, item in files.items():
            if not item:
                continue
            try:
                docs.append(save_document(s, app_id, doc_type, item[0], item[1]))
            except DocumentError as exc:
                rejected.append(f"document: {doc_type} ({exc})")
        missing += rejected
        if missing:
            app.status, app.current_step = "stopped_incomplete", "completeness_check"
            log(s, "completeness_check", "code", "stopped: application incomplete", app_id,
                missing=missing, status="stopped_incomplete")
            s.commit()
            report(STEPS[0], "failed", "Missing: " + ", ".join(missing))
            return WorkflowResult(app_id, "stopped_incomplete", stopped_incomplete=missing)
        s.flush()
        log(s, "completeness_check", "code", "application complete", app_id,
            documents=[{"doc_type": d.doc_type, "sha256": d.sha256, "pages": d.page_count}
                       for d in docs])
        app.current_step = "completeness_check"
        s.commit()
        lap("completeness")
        report(STEPS[0], "done", f"{len(docs)} documents stored")

        llm_error: Optional[str] = None
        if use_llm and not llm.available():
            llm_error = "LLM unavailable: the school server did not answer"
        elif not use_llm:
            llm_error = "LLM unavailable: LLM steps were turned off for this run"
        if llm_error:
            log(s, "llm", "code", "LLM steps skipped", app_id, reason=llm_error)

        # 2-3. Document Reader and extraction validation
        documented_income, extraction_review = None, False
        if llm_error is None:
            report(STEPS[1], "running")
            app.current_step = "document_reader"
            for doc in docs:
                ex = read_document(s, doc, model=model)
                extraction_review |= ex.needs_review
                if doc.doc_type == "pay_stub":
                    documented_income = ex.annual_income
                if ex.error:
                    llm_error = llm_error or ex.error
            s.commit()
            lap("document_reader")
            report(STEPS[1], "done", f"{len(docs)} documents read")
            report(STEPS[2], "done", "needs review" if extraction_review else
                   f"all fields valid; documented income {documented_income:,.2f}"
                   if documented_income else "no documented income")
        else:
            extraction_review = True
            report(STEPS[1], "skipped", llm_error)
            report(STEPS[2], "skipped", "no extraction to validate")

        # 4. Calculations
        report(STEPS[3], "running")
        app.current_step = "calculations"
        calcs = run_calculations(form)
        for c in calcs:
            s.add(Calculation(application_id=app_id, metric=c.metric, value=c.value,
                              inputs=c.inputs, code_used=FORMULAS[c.metric],
                              assumptions=c.assumptions))
        log(s, "calculations", "code", "ran financial calculations", app_id,
            results={c.metric: c.value for c in calcs})
        s.commit()
        report(STEPS[3], "done", ", ".join(f"{c.metric} {c.value}" for c in calcs))

        # 5a. Rule checks (documented income comes from the pay stub extraction)
        report(STEPS[4], "running")
        app.current_step = "rule_check"
        rule_flags = check_rules(form, calcs, documented_income=documented_income)
        for f in rule_flags:
            s.add(Flag(application_id=app_id, code=f.code, source="rule", severity=f.severity,
                       evidence=f.evidence))
        log(s, "rule_check", "code", "checked rules", app_id, documented_income=documented_income,
            flags=[{"code": f.code, "severity": f.severity} for f in rule_flags])
        s.commit()
        lap("calculations_and_rules")
        report(STEPS[4], "done", ", ".join(f.code for f in rule_flags) or "no flags")

        # 5b-5c. Pattern review and evidence check
        if llm_error is None:
            report(STEPS[5], "running")
            app.current_step = "pattern_review"
            try:
                s.refresh(app)
                kept = run_pattern_review(s, app, form, model=model)
                report(STEPS[5], "done", ", ".join(k["code"] for k in kept) or "no findings")
            except llm.LLMError as exc:
                llm_error = str(exc)
                report(STEPS[5], "failed", llm_error)
            s.commit()
            lap("pattern_review")
        else:
            report(STEPS[5], "skipped", llm_error)

        # 6. Risk score
        report(STEPS[6], "running")
        app.current_step = "risk_scoring"
        model_input = dict(form)
        if model_input.get("loan_percent_income") is None:
            model_input["loan_percent_income"] = round(form["loan_amnt"] / form["person_income"], 2)
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
        lap("risk_score")
        report(STEPS[6], "done", f"probability {result['default_probability']}")

        s.refresh(app)
        flags = [{"code": f.code, "severity": f.severity, "source": f.source} for f in app.flags]

        # 7-8. Decision Coordinator and output validation
        answer, failures, limit_reached, explanation = None, None, False, None
        if llm_error is None:
            report(STEPS[7], "running")
            app.current_step = "coordinator"
            out = coordinate(s, app, scorer=scorer, model=model)
            limit_reached = out.limit_reached
            if out.error:
                llm_error = out.error
                report(STEPS[7], "failed", out.error)
                report(STEPS[8], "skipped", "no recommendation to validate")
            else:
                answer = out.answer
                log(s, "coordinator", "model", "recommendation written", app_id,
                    answer=answer if answer is not None else out.raw_answer[:2000],
                    tool_calls=out.tool_calls_used, limit_reached=limit_reached)
                report(STEPS[7], "done", f"{(answer or {}).get('recommendation', 'unparseable')} "
                       f"after {out.tool_calls_used} tool calls")
                failures = validate_recommendation(
                    answer, tool_results=out.tool_results, flags=flags,
                    default_probability=result["default_probability"],
                    contribution_terms=result["contributions"])
                log(s, "output_validation", "code",
                    "validation passed" if not failures else "validation failed", app_id,
                    failures=failures)
                report(STEPS[8], "done" if not failures else "failed",
                       "passed" if not failures else "; ".join(failures))
            s.commit()
            lap("coordinator_and_validation")
        else:
            report(STEPS[7], "skipped", llm_error)
            report(STEPS[8], "skipped", "no recommendation to validate")

        # 9. Gatekeeper and the recommendation row (never a decision)
        app.current_step = "gatekeeper"
        usable = isinstance(answer, dict)
        recommendation = answer.get("recommendation") if usable else None
        if recommendation not in ("approve", "deny", "refer"):
            recommendation = "refer"
        confidence = answer.get("confidence") if usable else None
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) \
                or not 0 <= confidence <= 1:
            confidence = None
        if usable and isinstance(answer.get("reason_codes"), list):
            reason_codes = [str(c) for c in answer["reason_codes"]]
            explanation = str(answer.get("explanation") or "") or None
        else:  # no usable LLM answer: the phase 1 reason codes
            reason_codes = ([f"MODEL_DRIVER:{name}" for name, _ in result["top_risk_drivers"]]
                            + [f["code"] for f in flags])
        reasons = gatekeeper(flags=flags, validation_failures=failures,
                             extraction_needs_review=extraction_review,
                             confidence=confidence if answer is not None else None,
                             tool_limit_reached=limit_reached, llm_error=llm_error)
        s.add(Recommendation(application_id=app_id, recommendation=recommendation,
                             reason_codes=reason_codes, confidence=confidence,
                             passed_validation=None if failures is None else not failures,
                             marked_for_additional_review=bool(reasons), review_reasons=reasons,
                             explanation=explanation))
        log(s, "gatekeeper", "code", "marked for additional review" if reasons
            else "not marked", app_id, recommendation=recommendation, review_reasons=reasons)
        app.status, app.current_step = "in_review", "officer_review"
        log(s, "status", "code", "status set to in_review", app_id, status="in_review")
        s.commit()
        lap("gatekeeper")
        report(STEPS[9], "done", "marked: " + ", ".join(reasons) if reasons
               else "not marked; goes to the officer")
        return WorkflowResult(app_id, "in_review", llm_used=llm_error is None, marked=bool(reasons),
                              review_reasons=reasons, timings=timings)
