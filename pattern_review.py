"""Pattern review (workflow step 5b) and its evidence check (step 5c).

One LLM call with thinking on looks across the extracted fields of all the applicant's documents
and the form data (no images) for inconsistencies. Code then checks every finding: each cited
document and field must exist, and the description may not state a number that isn't in the
cited data (rule 10). Findings that fail are discarded and logged. Valid ones become warning
flags with source "pattern". The model cannot create critical flags.
"""
import json
import re
from typing import Any, Optional

from sqlalchemy.orm import Session

import llm
from db import Application, Flag, log
from validation import unsupported_numbers

FORM_FIELDS = ["person_income", "person_emp_exp", "person_education", "person_home_ownership",
               "loan_amnt", "loan_intent", "loan_int_rate", "loan_percent_income",
               "credit_score", "cb_person_cred_hist_length", "previous_loan_defaults_on_file",
               "full_name"]

SCHEMA = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "description": {"type": "string"},
            "evidence": {"type": "array", "items": {
                "type": "object",
                "properties": {"document": {"type": "string"}, "field": {"type": "string"}},
                "required": ["document", "field"]}},
        },
        "required": ["code", "description", "evidence"]}}},
    "required": ["findings"],
}

PROMPT = """You review a loan application for inconsistencies between the applicant's documents
and the application form. You are given the fields extracted from each document and the form
data, as JSON. Look for patterns such as:
- pay dates that don't fit the stated pay period
- deposits on the bank statement that are inconsistent with the stated or documented income
- employer or account-holder names that differ between documents or from the form
- statement periods or dates that don't line up
Withdrawals are not extracted, so a closing balance that differs from the opening balance plus
deposits is normal and is not a finding.

Return a JSON object {"findings": [...]}. Each finding has:
- code: a short UPPER_SNAKE_CASE label, e.g. DEPOSITS_BELOW_PAY_STUB_INCOME
- description: one or two plain sentences for a loan officer. Refer to fields by name. Do NOT
  write any number that is not printed in the data, and do not compute differences, ratios or
  percentages; code does the arithmetic.
- evidence: the document and field each claim rests on. document is one of the document names
  given (e.g. "pay_stub", "bank_statement") or "form"; field is a field name from that document.
Only report real inconsistencies. If there are none, return {"findings": []}.
Income or pay arithmetic is checked separately by code, so do not repeat the stated-vs-documented
income comparison unless the documents also disagree with each other."""


def _code(raw: str) -> str:
    code = re.sub(r"[^A-Z0-9]+", "_", str(raw).upper()).strip("_")[:60]
    return code or "UNLABELLED"


def review_inputs(app: Application, form: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """What the model sees and what evidence may cite: {document name: {field: value}}."""
    data = {"form": {k: form.get(k) for k in FORM_FIELDS if k in form}}
    for doc in sorted(app.documents, key=lambda d: d.id):
        data[doc.doc_type] = {f.field_name: f.field_value for f in doc.extracted_fields}
    return data


def check_finding(finding: Any, data: dict[str, dict[str, Any]]) -> tuple[Optional[dict], str]:
    """Return (clean finding, "") if valid, else (None, reason)."""
    if not isinstance(finding, dict):
        return None, "not an object"
    evidence = finding.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return None, "no evidence cited"
    cited = []
    for item in evidence:
        doc, fld = (item or {}).get("document"), (item or {}).get("field")
        if doc not in data:
            return None, f"cites document {doc!r}, which this application doesn't have"
        if fld not in data[doc]:
            return None, f"cites field {fld!r}, which {doc} doesn't have"
        cited.append({"document": doc, "field": fld, "value": data[doc][fld]})
    description = str(finding.get("description") or "").strip()
    if not description:
        return None, "no description"
    bad = unsupported_numbers(description, data)
    if bad:
        return None, f"description states numbers not in the data: {', '.join(bad)}"
    return {"code": _code(finding.get("code")), "description": description,
            "evidence": cited}, ""


def run_pattern_review(session: Session, app: Application, form: dict[str, Any],
                       model: Optional[str] = None) -> list[dict]:
    """Steps 5b and 5c. Saves valid findings as warning flags and returns them. Raises LLMError
    if the model can't be reached (the caller records it)."""
    data = review_inputs(app, form)
    reply = llm.chat(
        [{"role": "system", "content": PROMPT},
         {"role": "user", "content": json.dumps(data, indent=1, default=str)}],
        purpose="pattern review", step="pattern_review", model=model, schema=SCHEMA, think=True,
        session=session, application_id=app.id)
    try:
        findings = json.loads(reply.content).get("findings", [])
        if not isinstance(findings, list):
            raise ValueError("findings is not a list")
    except (ValueError, AttributeError) as exc:
        log(session, "pattern_review", "code", "pattern review reply was not valid JSON", app.id,
            error=str(exc))
        return []

    kept, dropped = [], []
    for raw in findings:
        clean, reason = check_finding(raw, data)
        if clean is None:
            dropped.append({"finding": raw, "reason": reason})
            continue
        kept.append(clean)
        session.add(Flag(application_id=app.id, code=clean["code"], source="pattern",
                         severity="warning",
                         evidence={"description": clean["description"],
                                   "cited": clean["evidence"]}))
    log(session, "evidence_check", "code", "checked pattern findings", app.id,
        kept=[k["code"] for k in kept], discarded=dropped)
    return kept
