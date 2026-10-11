"""Decision Coordinator (workflow step 7): a bounded tool-calling loop with thinking on.

It runs after code has finished steps 1 to 6 and saved their results. Its five tools take only
an application_id, read the stored results (computing and saving a missing one with the same
function), and never write decisions or change the application's status. So every number the
model sees comes from code. The orchestrating code checks each tool call before running it.
"""
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import llm
from calculations import FORMULAS, check_rules as rule_check, run_calculations as calc
from db import Application, AuditLog, Calculation, Flag, RiskScore, log
from validation import GENERAL_REASONS, RECOMMENDATION_SCHEMA

MAX_TOOL_CALLS = 8

SYSTEM_PROMPT = f"""You are the Decision Coordinator in a loan decision support system. You write a
recommendation for a human loan officer, who always makes the final decision. You never approve
or deny a loan yourself.

Use the tools to read the application's stored results: get_application, run_calculations,
check_rules, get_pattern_findings and score_risk. Each takes only the application_id. You may
make at most {MAX_TOOL_CALLS} tool calls in total.

Rules:
- Base every statement on tool results. Never state a number that no tool returned; copy numbers
  exactly as returned (you may round them). Do not calculate new numbers.
- Never override the risk score. Explain using the score's top contributions and the flags.
- Recommend "approve" only if there are no critical flags and the default probability is below
  0.5. Recommend "deny" only with supporting flags or a high default probability. Otherwise, or if
  something is missing or uncertain, recommend "refer".
- reason_codes may only contain: flag codes returned by check_rules or get_pattern_findings,
  MODEL_DRIVER:<term> for a term in the score's contributions, or one of
  {", ".join(GENERAL_REASONS)}.
- The explanation is 2 to 5 plain sentences for the officer. Gender and age are not used by the
  model and must not be mentioned.
- confidence is your confidence from 0 to 1 in the recommendation."""

TOOLS = [{"type": "function", "function": {
    "name": name, "description": description,
    "parameters": {"type": "object",
                   "properties": {"application_id": {"type": "integer",
                                                      "description": "The application's id"}},
                   "required": ["application_id"]}}}
    for name, description in [
        ("get_application", "Form data, the loan request, the applicant's earlier applications, "
                            "and the documents with extracted fields, page numbers and "
                            "needs_review markers."),
        ("run_calculations", "Loan-to-income, monthly payment and payment-to-income, with their "
                             "inputs and assumptions."),
        ("check_rules", "Rule flags with severity, the actual value and the limit it was "
                        "compared to. Uses documented income from the pay stub."),
        ("get_pattern_findings", "Verified pattern-review findings with the document fields "
                                 "they cite."),
        ("score_risk", "The model's default probability, baseline log-odds, every term's "
                       "contribution, top risk drivers and protective factors, model version."),
    ]]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


# ----------------------------------------------------------------------- stored inputs

def stored_form(session: Session, app: Application) -> dict[str, Any]:
    """The form values the application was scored with (saved in its intake audit row), with
    the applicant and application columns as a fallback."""
    entry = session.scalars(select(AuditLog).where(AuditLog.application_id == app.id,
                                                   AuditLog.step == "intake")
                            .order_by(AuditLog.id)).first()
    form = dict((entry.details or {}).get("form") or {}) if entry else {}
    a = app.applicant
    fallback = {
        "full_name": a.full_name, "person_income": a.annual_income,
        "person_emp_exp": a.employment_years, "person_education": a.education,
        "person_home_ownership": a.home_ownership, "credit_score": a.credit_score,
        "cb_person_cred_hist_length": a.credit_history_years,
        "previous_loan_defaults_on_file": "Yes" if a.previous_defaults else "No",
        "loan_amnt": app.loan_amount, "loan_intent": app.loan_intent,
        "loan_int_rate": app.interest_rate, "term_months": app.term_months,
        "loan_percent_income": None,
    }
    return {**fallback, **{k: v for k, v in form.items() if k in fallback}}


def documented_income(app: Application) -> Optional[float]:
    """Annual income computed by code from the pay stub, if extraction produced one."""
    for doc in app.documents:
        if doc.doc_type == "pay_stub":
            for f in doc.extracted_fields:
                if f.field_name == "annual_income" and f.field_value not in (None, ""):
                    return float(f.field_value)
    return None


def _flag_dict(f: Flag) -> dict:
    out = {"code": f.code, "severity": f.severity, "source": f.source}
    out.update({k: v for k, v in (f.evidence or {}).items()})
    return out


# ----------------------------------------------------------------------- the five tools

def tool_get_application(session: Session, app: Application, scorer=None) -> dict:
    form = stored_form(session, app)
    earlier = []
    for other in sorted(app.applicant.applications, key=lambda a: a.id):
        if other.id == app.id:
            continue
        review = max(other.officer_reviews, key=lambda r: r.id) if other.officer_reviews else None
        earlier.append({"application_id": other.id, "submitted": other.created_at.date().isoformat(),
                        "loan_amount": other.loan_amount, "status": other.status,
                        "officer_decision": review.decision if review else None})
    docs = []
    for d in sorted(app.documents, key=lambda d: d.id):
        docs.append({"document_id": d.id, "doc_type": d.doc_type, "pages": d.page_count,
                     "fields": [{"name": f.field_name, "value": f.field_value,
                                 "page": f.source_page, "needs_review": f.needs_review,
                                 "verified_by_human": f.verified_by_human}
                                for f in sorted(d.extracted_fields, key=lambda f: f.id)]})
    return {"application_id": app.id, "applicant_id": app.applicant_id,
            "applicant_name": app.applicant.full_name,
            "form": {k: v for k, v in form.items() if k != "full_name"},
            "earlier_applications": earlier, "documents": docs,
            "note": "Gender and age are not collected for scoring and are not used."}


def tool_run_calculations(session: Session, app: Application, scorer=None) -> dict:
    rows = sorted(app.calculations, key=lambda c: c.id)
    if not rows:  # missing: compute with the same function, save, then return what was saved
        for c in calc(stored_form(session, app)):
            session.add(Calculation(application_id=app.id, metric=c.metric, value=c.value,
                                    inputs=c.inputs, code_used=FORMULAS[c.metric],
                                    assumptions=c.assumptions))
        session.flush()
        session.refresh(app)
        rows = sorted(app.calculations, key=lambda c: c.id)
        log(session, "calculations", "code", "computed missing calculations for a tool call",
            app.id)
    return {"results": [{"metric": c.metric, "value": c.value, "inputs": c.inputs,
                         "assumptions": c.assumptions} for c in rows]}


def tool_check_rules(session: Session, app: Application, scorer=None) -> dict:
    flags = [f for f in app.flags if f.source == "rule"]
    ran = session.scalars(select(AuditLog).where(AuditLog.application_id == app.id,
                                                 AuditLog.step == "rule_check")).first()
    if ran is None:  # rules never ran for this application: run and save them now
        tool_run_calculations(session, app)
        form = stored_form(session, app)
        from calculations import CalcResult
        calcs = [CalcResult(c.metric, c.value, c.inputs, c.assumptions) for c in app.calculations]
        for f in rule_check(form, calcs, documented_income=documented_income(app)):
            session.add(Flag(application_id=app.id, code=f.code, source="rule",
                             severity=f.severity, evidence=f.evidence))
        log(session, "rule_check", "code", "checked rules for a tool call", app.id)
        session.flush()
        session.refresh(app)
        flags = [f for f in app.flags if f.source == "rule"]
    return {"documented_income": documented_income(app),
            "flags": [_flag_dict(f) for f in sorted(flags, key=lambda f: f.id)]}


def tool_get_pattern_findings(session: Session, app: Application, scorer=None) -> dict:
    flags = sorted((f for f in app.flags if f.source == "pattern"), key=lambda f: f.id)
    return {"findings": [_flag_dict(f) for f in flags],
            "note": "Only findings whose cited evidence was verified by code are listed."}


def tool_score_risk(session: Session, app: Application, scorer=None) -> dict:
    score = max(app.risk_scores, key=lambda r: r.id) if app.risk_scores else None
    if score is None:
        if scorer is None:
            return {"error": "no stored risk score and no model available"}
        form = stored_form(session, app)
        if form.get("loan_percent_income") is None:
            form["loan_percent_income"] = round(form["loan_amnt"] / form["person_income"], 2)
        result = scorer.score(form)
        score = RiskScore(application_id=app.id, model_version=result["model_version"],
                          default_probability=result["default_probability"],
                          contributions=result["contributions"])
        session.add(score)
        log(session, "risk_scoring", "model", "scored default risk for a tool call", app.id,
            model_version=result["model_version"])
        session.flush()
    ranked = sorted(score.contributions.items(), key=lambda kv: kv[1], reverse=True)
    baseline = None
    if scorer is not None and scorer.model_version == score.model_version:
        baseline = scorer.baseline_log_odds
    return {
        "model_version": score.model_version,
        "default_probability": score.default_probability,
        "baseline_log_odds": baseline,
        "top_risk_drivers": [{"term": k, "contribution": v} for k, v in ranked if v > 0][:3],
        "top_protective_factors": [{"term": k, "contribution": v}
                                   for k, v in reversed(ranked) if v < 0][:3],
        "contributions": score.contributions,
        "note": "Positive contributions raise default risk; negative ones lower it. "
                "Gender and age are not model inputs.",
    }


TOOL_FUNCTIONS = {
    "get_application": tool_get_application, "run_calculations": tool_run_calculations,
    "check_rules": tool_check_rules, "get_pattern_findings": tool_get_pattern_findings,
    "score_risk": tool_score_risk,
}


# ----------------------------------------------------------------------- the loop

@dataclass
class CoordinatorResult:
    answer: Optional[dict] = None
    raw_answer: str = ""
    tool_results: list[dict] = field(default_factory=list)
    tool_calls_used: int = 0
    limit_reached: bool = False
    error: Optional[str] = None


def run_tool(session: Session, app: Application, call: dict, scorer, purpose: str,
             allowed: set[str]) -> tuple[str, dict]:
    """Check one tool call, run it if valid, and write its audit row. Returns (name, result)."""
    fn = call.get("function") or {}
    name, args = fn.get("name"), fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {"_unparsed": args}
    if name not in allowed:
        result = {"error": f"Unknown tool {name!r}. Available: {', '.join(sorted(allowed))}."}
    elif set(args) != {"application_id"} or args.get("application_id") != app.id:
        result = {"error": f"Tools take only application_id, which must be {app.id}."}
    else:
        result = TOOL_FUNCTIONS[name](session, app, scorer)
    log(session, purpose, "model", f"tool call: {name}", app.id, purpose=purpose, tool=name,
        arguments=args, ok="error" not in result)
    return str(name), result


def coordinate(session: Session, app: Application, scorer=None,
               model: Optional[str] = None) -> CoordinatorResult:
    out = CoordinatorResult()
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Recommend for application {app.id}."}]
    try:
        while True:
            reply = llm.chat(messages, purpose="decision coordinator", step="coordinator",
                             model=model, tools=TOOLS, think=True, session=session,
                             application_id=app.id, tool_calls_so_far=out.tool_calls_used)
            messages.append({"role": "assistant", "content": reply.content,
                             **({"tool_calls": reply.tool_calls} if reply.tool_calls else {})})
            if not reply.tool_calls:
                break
            for call in reply.tool_calls:
                if out.tool_calls_used >= MAX_TOOL_CALLS:
                    out.limit_reached = True
                    break
                out.tool_calls_used += 1
                name, result = run_tool(session, app, call, scorer, "coordinator", TOOL_NAMES)
                out.tool_results.append({"tool": name, "result": result})
                messages.append({"role": "tool", "tool_name": name,
                                 "content": json.dumps(result, default=str)})
            if out.limit_reached:
                log(session, "coordinator", "code", "tool-call limit reached", app.id,
                    limit=MAX_TOOL_CALLS)
                break

        messages.append({"role": "user", "content": (
            "Give your final answer now as a JSON object with recommendation, reason_codes, "
            "explanation and confidence. Use only numbers from the tool results.")})
        final = llm.chat(messages, purpose="decision coordinator: final answer",
                         step="coordinator", model=model, schema=RECOMMENDATION_SCHEMA,
                         think=True, session=session, application_id=app.id,
                         tool_calls_so_far=out.tool_calls_used)
        out.raw_answer = final.content
        try:
            out.answer = json.loads(final.content)
        except ValueError:
            out.answer = None
    except llm.LLMError as exc:
        out.error = str(exc)
    return out
