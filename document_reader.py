"""Document Reader (workflow step 2) and extraction validation (step 3).

Step 2: the LLM reads the page images, with thinking off and a JSON schema per document type, and
copies out only what is printed. It never calculates.
Step 3: plain code checks the reply: schema, types, plausible dates, page numbers, required
fields. It then computes annual income from gross pay and pay period. Anything that fails sets
needs_review on the field. The model's self-reported confidence is stored but never trusted on
its own.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

import llm
from db import Document, ExtractedField, log
from documents import page_images

PERIODS_PER_YEAR = {"weekly": 52, "biweekly": 26, "semimonthly": 24, "monthly": 12}
MAX_ATTEMPTS = 2  # one retry when the reply has a problem the model can fix
# Step 3 problems a second try can fix (format or shape). A null "not found" value, an
# implausible date or a negative amount is about the document itself, so it goes to review.
FIXABLE = ("invalid JSON", "missing from the reply", "is not a number", "not a non-empty string",
           "is not one of", "is not a YYYY-MM-DD date", "page ")

# Field name -> (JSON type, required). Every field comes back as {value, page, confidence}.
FIELDS = {
    "pay_stub": {
        "employer": ("string", True),
        "pay_period": ("period", True),
        "gross_pay": ("money", True),
        "pay_date": ("date", True),
    },
    "bank_statement": {
        "bank_name": ("string", True),
        "statement_period_start": ("date", True),
        "statement_period_end": ("date", True),
        "opening_balance": ("signed_money", True),
        "closing_balance": ("signed_money", True),
        "total_deposits": ("money", True),
    },
}

PROMPTS = {
    "pay_stub": (
        "You are reading a pay stub. Extract these fields exactly as printed:\n"
        "- employer: the employer's name\n"
        "- pay_period: how often the employee is paid, one of weekly, biweekly (every two weeks), "
        "semimonthly (twice a month), monthly\n"
        "- gross_pay: gross pay for THIS pay period (before deductions), not net pay and not "
        "year-to-date\n"
        "- pay_date: the pay date, written as YYYY-MM-DD\n"),
    "bank_statement": (
        "You are reading a bank account statement. Extract these fields exactly as printed:\n"
        "- bank_name: the bank's name\n"
        "- statement_period_start and statement_period_end: the statement period dates, "
        "written as YYYY-MM-DD\n"
        "- opening_balance and closing_balance: the balances for the statement period\n"
        "- total_deposits: the total of deposits (credits) for the period, as printed\n"),
}
RULES_TEXT = (
    "\nRules:\n"
    "- Extract only what is printed. Never calculate, add up, convert, or estimate a value.\n"
    "- If a field is not printed, use null for its value and page.\n"
    "- Write amounts as plain numbers without currency symbols or thousands separators "
    "(e.g. 1218.18). Negative balances get a minus sign.\n"
    "- page is the 1-based number of the image the value appears on, in the order given.\n"
    "- confidence is your confidence from 0 to 1 that the value is copied correctly.\n")


@dataclass
class Extraction:
    document_id: int
    doc_type: str
    values: dict[str, Any] = field(default_factory=dict)       # validated values (or None)
    pages: dict[str, Optional[int]] = field(default_factory=dict)
    problems: dict[str, list[str]] = field(default_factory=dict)  # field -> reasons
    valid_json: bool = False
    annual_income: Optional[float] = None
    error: Optional[str] = None

    @property
    def needs_review(self) -> bool:
        return bool(self.error) or any(self.problems.values())


def schema_for(doc_type: str) -> dict:
    kinds = {"string": "string", "period": "string", "money": "number", "signed_money": "number",
             "date": "string"}
    props = {}
    for name, (kind, _) in FIELDS[doc_type].items():
        value = {"type": [kinds[kind], "null"]}
        if kind == "period":
            value["enum"] = list(PERIODS_PER_YEAR) + [None]
        props[name] = {"type": "object", "properties": {
            "value": value, "page": {"type": ["integer", "null"]},
            "confidence": {"type": ["number", "null"]}},
            "required": ["value", "page", "confidence"]}
    return {"type": "object", "properties": props, "required": list(props)}


def _to_number(raw) -> Optional[float]:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and re.fullmatch(r"\s*-?\$?\s*[\d,]+(\.\d+)?\s*", raw):
        return float(raw.replace("$", "").replace(",", "").replace(" ", ""))
    return None


def _to_date(raw) -> Optional[date]:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def validate(doc_type: str, reply: str, page_count: int, today: Optional[date] = None) -> Extraction:
    """Step 3 checks on the model's raw reply. Pure function: no database access."""
    today = today or datetime.now(timezone.utc).date()
    ex = Extraction(document_id=0, doc_type=doc_type)
    try:
        data = json.loads(reply)
        if not isinstance(data, dict):
            raise ValueError("reply is not a JSON object")
        ex.valid_json = True
    except ValueError as exc:
        ex.error = f"invalid JSON: {exc}"
        data = {}

    for name, (kind, required) in FIELDS[doc_type].items():
        problems: list[str] = []
        item = data.get(name)
        if ex.valid_json and not (isinstance(item, dict) and "value" in item):
            problems.append("missing from the reply or not in the schema's shape")
            item = {}
        item = item or {}
        raw, page = item.get("value"), item.get("page")
        value: Any = None
        if raw is None:
            if required:
                problems.append("required field not found (null)")
        elif kind == "string":
            value = raw.strip() if isinstance(raw, str) and raw.strip() else None
            if value is None:
                problems.append("not a non-empty string")
        elif kind == "period":
            value = raw.strip().lower() if isinstance(raw, str) else None
            if value not in PERIODS_PER_YEAR:
                problems.append(f"pay period {raw!r} is not one of {', '.join(PERIODS_PER_YEAR)}")
                value = None
        elif kind in ("money", "signed_money"):
            value = _to_number(raw)
            if value is None:
                problems.append(f"{raw!r} is not a number")
            elif kind == "money" and value <= 0:
                problems.append(f"{value} should be greater than 0")
            elif abs(value) > 10_000_000:
                problems.append(f"{value} is implausibly large")
        elif kind == "date":
            parsed = _to_date(raw)
            if parsed is None:
                problems.append(f"{raw!r} is not a YYYY-MM-DD date")
            elif not (today - timedelta(days=550) <= parsed <= today + timedelta(days=31)):
                problems.append(f"date {parsed} is not within the last 18 months")
            else:
                value = parsed.isoformat()
        if raw is not None and not (isinstance(page, int) and 1 <= page <= page_count):
            problems.append(f"page {page!r} is not between 1 and {page_count}")
        ex.values[name], ex.pages[name] = value, page if isinstance(page, int) else None
        ex.problems[name] = problems

    if doc_type == "bank_statement":
        start, end = ex.values.get("statement_period_start"), ex.values.get("statement_period_end")
        if start and end:
            days = (date.fromisoformat(end) - date.fromisoformat(start)).days
            if not 0 < days <= 62:
                ex.problems["statement_period_end"].append(
                    f"statement period of {days} days is not between 1 and 62")
    if doc_type == "pay_stub":
        gross, period = ex.values.get("gross_pay"), ex.values.get("pay_period")
        if gross is not None and period is not None:
            ex.annual_income = round(gross * PERIODS_PER_YEAR[period], 2)  # computed by code
    return ex


def fixable_problems(ex: Extraction) -> list[str]:
    """Problems worth one retry, as text the model can act on."""
    found = [f"invalid JSON ({ex.error})"] if ex.error else []
    for name, problems in ex.problems.items():
        found += [f"{name}: {p}" for p in problems if any(m in p for m in FIXABLE)]
    return found


def read_document(session: Session, doc: Document, model: Optional[str] = None) -> Extraction:
    """Run steps 2 and 3 for one document and save its extracted_fields rows (caller commits).
    A reply with fixable problems gets one retry that quotes the problems back to the model."""
    model = model or llm.active_model()
    images = page_images(doc)
    prompt = (PROMPTS[doc.doc_type] + RULES_TEXT
              + f"\nThe document has {len(images)} page image(s). Reply with JSON only.")
    messages = [{"role": "user", "content": prompt}]
    try:
        retry_reason = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            reply = llm.chat(messages, purpose=f"document reader: {doc.doc_type}",
                             step="document_reader", model=model, images=images,
                             schema=schema_for(doc.doc_type), think=False, session=session,
                             application_id=doc.application_id, attempt=attempt,
                             retry_reason=retry_reason)
            ex = validate(doc.doc_type, reply.content, len(images))
            fixable = fixable_problems(ex)
            if not fixable or attempt == MAX_ATTEMPTS:
                break
            retry_reason = "; ".join(fixable)
            messages = messages + [
                {"role": "assistant", "content": reply.content},
                {"role": "user", "content": "Your reply had these problems: " + retry_reason
                 + ". Reply again with the corrected JSON for the same document. Use null only "
                   "for values that are not printed."}]
        raw = json.loads(reply.content) if ex.valid_json else {}
    except llm.LLMError as exc:
        ex = Extraction(document_id=doc.id, doc_type=doc.doc_type, error=str(exc))
        for name in FIELDS[doc.doc_type]:
            ex.values[name], ex.pages[name] = None, None
            ex.problems[name] = ["the document could not be read: LLM error"]
        raw = {}
    ex.document_id = doc.id

    for name in FIELDS[doc.doc_type]:
        conf = (raw.get(name) or {}).get("confidence") if isinstance(raw.get(name), dict) else None
        conf = float(conf) if isinstance(conf, (int, float)) and 0 <= conf <= 1 else None
        session.add(ExtractedField(
            document_id=doc.id, field_name=name,
            field_value=None if ex.values.get(name) is None else str(ex.values[name]),
            confidence=conf, source_page=ex.pages.get(name), extraction_model=model,
            needs_review=bool(ex.problems.get(name))))
    if doc.doc_type == "pay_stub":
        # Computed by code from two extracted fields, so it is reviewable like they are.
        inputs_bad = bool(ex.problems.get("gross_pay") or ex.problems.get("pay_period"))
        session.add(ExtractedField(
            document_id=doc.id, field_name="annual_income",
            field_value=None if ex.annual_income is None else str(ex.annual_income),
            confidence=None, source_page=ex.pages.get("gross_pay"), extraction_model="code",
            needs_review=ex.annual_income is None or inputs_bad))
    log(session, "extraction_validation", "code", f"validated {doc.doc_type} extraction",
        doc.application_id, document_id=doc.id, valid_json=ex.valid_json,
        problems={k: v for k, v in ex.problems.items() if v}, annual_income=ex.annual_income,
        error=ex.error)
    return ex
