"""Plain-code checks on LLM output: the number check (rule 10), output validation (workflow
step 8) and the Review Gatekeeper (step 9). Nothing here calls the LLM."""
import re
from typing import Any, Iterable, Optional

RECOMMENDATIONS = ("approve", "deny", "refer")
# General reason codes an officer or the Coordinator may cite besides flag codes and model drivers.
GENERAL_REASONS = ["INSUFFICIENT_INCOME", "HIGH_DEBT_BURDEN", "WEAK_CREDIT_HISTORY",
                   "INCOMPLETE_DOCUMENTATION", "UNVERIFIED_INCOME", "OFFICER_JUDGMENT",
                   "LOW_MODEL_RISK", "NO_RULE_FLAGS"]
MIN_CONFIDENCE = 0.7

RECOMMENDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendation": {"type": "string", "enum": list(RECOMMENDATIONS)},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
        "explanation": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["recommendation", "reason_codes", "explanation", "confidence"],
}

# A number as written in text: optional sign, thousands separators, decimals, optional percent.
NUMBER = re.compile(r"(?<![\w.])([-−]?)\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(\s?%)?")


def numbers_in(text: str) -> list[dict]:
    found = []
    for m in NUMBER.finditer(text or ""):
        whole, frac = m.group(2).replace(",", ""), m.group(3) or ""
        found.append({"text": m.group(0).strip(), "value": float(whole + frac),
                      "decimals": len(frac) - 1 if frac else 0, "percent": bool(m.group(4))})
    return found


def source_numbers(sources: Any) -> set[float]:
    """Every number in tool results: numeric values, plus numbers inside strings (dates,
    assumptions such as "36 months", ids). Signs are dropped: text may say "lowers by 0.9"."""
    out: set[float] = set()

    def walk(x):
        if isinstance(x, bool) or x is None:
            return
        if isinstance(x, (int, float)):
            out.add(abs(float(x)))
        elif isinstance(x, str):
            for n in numbers_in(x):
                out.add(n["value"])
        elif isinstance(x, dict):
            for k, v in x.items():
                walk(k)
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)

    walk(sources)
    return out


def _matches(n: dict, allowed: Iterable[float]) -> bool:
    tol = 0.5 * 10 ** -n["decimals"] + 1e-9
    value = n["value"]
    for v in allowed:
        if abs(value - v) <= tol:            # same number, rounded as written
            return True
        if abs(value - v * 100) <= tol:      # a share written as a percent (0.1975 -> 19.75%)
            return True
    return False


def unsupported_numbers(text: str, sources: Any) -> list[str]:
    """Numbers in text that match no tool result, allowing normal rounding. The display bounds
    "above 99%" and "below 1%" (A4) are allowed because they describe a stored probability."""
    allowed = source_numbers(sources) | {99.0, 1.0} if re.search(r"(above|below) (99|1)\s?%",
                                                                text or "") else source_numbers(sources)
    return [n["text"] for n in numbers_in(text) if not _matches(n, allowed)]


FIXABLE_KINDS = ("SCHEMA", "REASON_CODE")  # a second try can fix format and codes


def fixable_failures(failures: Optional[list[str]]) -> list[str]:
    """Step 8 failures worth one retry of the final answer (wrong type, missing field, invalid
    reason code). Number and consistency failures go straight to the officer as marked."""
    return [f for f in failures or [] if f.split(":")[0] in FIXABLE_KINDS]


def validate_recommendation(answer: Any, *, tool_results: Any, flags: list[dict],
                            default_probability: Optional[float],
                            contribution_terms: Iterable[str]) -> list[str]:
    """Step 8. Returns the failure reasons; an empty list means the recommendation passed."""
    if not isinstance(answer, dict):
        return ["SCHEMA: the answer is not a JSON object"]
    failures = []
    for name, kind in (("recommendation", str), ("reason_codes", list), ("explanation", str),
                       ("confidence", (int, float))):
        if not isinstance(answer.get(name), kind) or isinstance(answer.get(name), bool):
            failures.append(f"SCHEMA: {name} is missing or has the wrong type")
    if failures:
        return failures
    rec, codes, confidence = answer["recommendation"], answer["reason_codes"], answer["confidence"]
    if rec not in RECOMMENDATIONS:
        failures.append(f"SCHEMA: recommendation {rec!r} is not approve, deny or refer")
    if not 0 <= confidence <= 1:
        failures.append(f"SCHEMA: confidence {confidence} is not between 0 and 1")

    allowed_codes = ({f["code"] for f in flags} | set(GENERAL_REASONS)
                     | {f"MODEL_DRIVER:{t}" for t in contribution_terms})
    bad_codes = [c for c in codes if not isinstance(c, str) or c not in allowed_codes]
    if bad_codes:
        failures.append(f"REASON_CODE: not a flag code, MODEL_DRIVER:<term> or general reason: "
                        f"{', '.join(map(str, bad_codes))}")

    bad_numbers = unsupported_numbers(answer["explanation"], tool_results)
    if bad_numbers:
        failures.append(f"NUMBER: explanation states numbers no tool returned: "
                        f"{', '.join(bad_numbers)}")

    critical = [f["code"] for f in flags if f.get("severity") == "critical"]
    p = default_probability
    if rec == "approve" and critical:
        failures.append(f"CONSISTENCY: approve while critical flags exist ({', '.join(critical)})")
    if rec == "approve" and p is not None and p >= 0.5:
        failures.append(f"CONSISTENCY: approve while the default probability is {p} (0.5 or above)")
    if rec == "deny" and not flags and p is not None and p < 0.2:
        failures.append(f"CONSISTENCY: deny with no flags and a default probability of {p} "
                        "(below 0.2)")
    return failures


def gatekeeper(*, flags: list[dict], validation_failures: Optional[list[str]],
               extraction_needs_review: bool, confidence: Optional[float],
               tool_limit_reached: bool, llm_error: Optional[str]) -> list[str]:
    """Step 9. Every application goes to the officer; this decides whether it is also marked for
    additional review, and why. Returns the reasons (empty = not marked)."""
    reasons = [f"CRITICAL_FLAG:{f['code']}" for f in flags if f.get("severity") == "critical"]
    if llm_error:
        reasons.append("LLM_UNAVAILABLE" if "unavailable" in llm_error.lower() else "LLM_ERROR")
    if validation_failures:
        reasons += [f"VALIDATION_FAILED: {v}" for v in validation_failures]
    if extraction_needs_review:
        reasons.append("EXTRACTION_NEEDS_REVIEW")
    if confidence is not None and confidence < MIN_CONFIDENCE:
        reasons.append(f"LOW_CONFIDENCE:{confidence:.2f}")
    if tool_limit_reached:
        reasons.append("TOOL_CALL_LIMIT_REACHED")
    return list(dict.fromkeys(reasons))
