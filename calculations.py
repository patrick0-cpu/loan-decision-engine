"""Financial Calculator and rule checks. Plain Python only: no arithmetic is delegated to an LLM.

`app` dicts use the dataset column names (person_income, loan_amnt, loan_int_rate, ...),
plus an optional `term_months`.
"""
from dataclasses import dataclass, field
from typing import Any, Optional

DEFAULT_TERM_MONTHS = 36

RULES = {  # Derived from the dataset analysis (cleaned rows, 1.5x risk multiple)
    "max_payment_to_income": 0.10,  # default rate jumps from 53% to 87% (36-month term assumed)
    "max_loan_to_income": 0.25,     # default rate jumps from 50% to 87%
    "min_credit_score": 550,        # 74% default below 550; a stricter choice would be 600 (57% at 550-599)
    "max_income_mismatch": 0.15,    # judgment call, not testable with this dataset
    "loan_percent_income_tolerance": 0.02,  # rounding allowance
}

# Human-readable formulas, stored with each result in the calculations table.
FORMULAS = {
    "loan_to_income": "round(loan_amount / annual_income, 4)",
    "monthly_payment": "round(P * r / (1 - (1 + r) ** -n), 2), r = rate / 100 / 12; P / n if rate is 0",
    "payment_to_income": "round(monthly_payment / (annual_income / 12), 4)",
}


@dataclass
class CalcResult:
    metric: str
    value: Optional[float]
    inputs: dict[str, Any]
    assumptions: list[str] = field(default_factory=list)


@dataclass
class RuleFlag:
    code: str
    severity: str  # info, warning, critical
    evidence: dict[str, Any]


def loan_to_income(loan_amount: float, annual_income: float) -> CalcResult:
    inputs = {"loan_amount": loan_amount, "annual_income": annual_income}
    if annual_income is None or annual_income <= 0:
        return CalcResult("loan_to_income", None, inputs, ["income is 0 or less, ratio undefined"])
    return CalcResult("loan_to_income", round(loan_amount / annual_income, 4), inputs)


def monthly_payment(principal: float, annual_rate_pct: float,
                    term_months: Optional[int] = None) -> CalcResult:
    assumptions = []
    if not term_months:
        term_months = DEFAULT_TERM_MONTHS
        assumptions.append(f"term not provided, assumed {DEFAULT_TERM_MONTHS} months")
    inputs = {"principal": principal, "annual_rate_pct": annual_rate_pct,
              "term_months": term_months}

    r = annual_rate_pct / 100 / 12
    n = term_months
    payment = principal / n if r == 0 else principal * r / (1 - (1 + r) ** -n)
    return CalcResult("monthly_payment", round(payment, 2), inputs, assumptions)


def payment_to_income(payment: float, annual_income: float) -> CalcResult:
    inputs = {"monthly_payment": payment, "annual_income": annual_income}
    assumptions = ["excludes existing debts, so it understates true DTI"]
    if payment is None or annual_income is None or annual_income <= 0:
        assumptions.append("income is 0 or less, ratio undefined")
        return CalcResult("payment_to_income", None, inputs, assumptions)
    return CalcResult("payment_to_income", round(payment / (annual_income / 12), 4), inputs,
                      assumptions)


def run_calculations(app: dict[str, Any]) -> list[CalcResult]:
    income = app["person_income"]
    lti = loan_to_income(app["loan_amnt"], income)
    pmt = monthly_payment(app["loan_amnt"], app["loan_int_rate"], app.get("term_months"))
    pti = payment_to_income(pmt.value, income)
    # The payment-to-income limit was derived assuming a 36-month term, so carry that forward.
    pti.assumptions = pmt.assumptions + pti.assumptions
    return [lti, pmt, pti]


def check_rules(app: dict[str, Any], calcs: list[CalcResult],
                documented_income: Optional[float] = None) -> list[RuleFlag]:
    values = {c.metric: c.value for c in calcs}
    flags: list[RuleFlag] = []

    # Checked first: without income both ratios are None, so the ratio rules below cannot fire.
    income = app.get("person_income")
    if income is None or income <= 0:
        flags.append(RuleFlag("INCOME_MISSING", "critical", {"value": income, "limit": "> 0"}))

    pti = values.get("payment_to_income")
    if pti is not None and pti >= RULES["max_payment_to_income"]:
        flags.append(RuleFlag("PAYMENT_TO_INCOME_HIGH", "warning",
                              {"value": pti, "limit": RULES["max_payment_to_income"]}))

    lti = values.get("loan_to_income")
    if lti is not None and lti >= RULES["max_loan_to_income"]:
        flags.append(RuleFlag("LOAN_TO_INCOME_HIGH", "warning",
                              {"value": lti, "limit": RULES["max_loan_to_income"]}))

    score = app.get("credit_score")
    if score is not None and score < RULES["min_credit_score"]:
        flags.append(RuleFlag("CREDIT_SCORE_BELOW_MINIMUM", "critical",
                              {"value": score, "limit": RULES["min_credit_score"]}))

    prior = app.get("previous_loan_defaults_on_file")
    if prior is not None and str(prior).strip().lower() == "yes":
        flags.append(RuleFlag("PREVIOUS_DEFAULT_ON_FILE", "critical",
                              {"value": prior, "limit": "No"}))

    provided = app.get("loan_percent_income")
    if provided is not None and lti is not None:
        gap = round(abs(provided - lti), 4)
        if gap > RULES["loan_percent_income_tolerance"]:
            flags.append(RuleFlag("RATIO_INCONSISTENT", "warning",
                                  {"value": gap, "provided": provided, "calculated": lti,
                                   "limit": RULES["loan_percent_income_tolerance"]}))

    stated = app.get("person_income")
    if documented_income is not None and stated:
        gap = round(abs(stated - documented_income) / stated, 4)
        if gap > RULES["max_income_mismatch"]:
            flags.append(RuleFlag("INCOME_MISMATCH", "critical",
                                  {"value": gap, "stated_income": stated,
                                   "documented_income": documented_income,
                                   "limit": RULES["max_income_mismatch"]}))
    return flags
