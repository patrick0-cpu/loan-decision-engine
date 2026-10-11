"""The dashboard's chat assistant: answers an officer's questions with read-only tools.

It never makes or changes a decision. Every number in an answer must come from a tool result
(the same check as output validation); an answer that fails is shown with a warning, not hidden.
At most 6 tool calls per question, thinking on. Each tool call writes an audit_log row (actor
"model", purpose "assistant"); the questions and answers themselves are not stored.
"""
import json
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

import llm
from calculations import RULES
from coordinator import tool_get_application, tool_score_risk
from db import Application, load_historical, log
from validation import unsupported_numbers

MAX_TOOL_CALLS = 6

FLAG_MEANINGS = {
    "INCOME_MISSING": "critical: income is zero, negative or missing, so no ratio can be computed",
    "PAYMENT_TO_INCOME_HIGH": "warning: monthly payment / monthly income at or above the limit",
    "LOAN_TO_INCOME_HIGH": "warning: loan amount / annual income at or above the limit",
    "CREDIT_SCORE_BELOW_MINIMUM": "critical: credit score below the minimum",
    "PREVIOUS_DEFAULT_ON_FILE": "critical: the applicant has a previous loan default on file",
    "RATIO_INCONSISTENT": "warning: the loan-to-income given on the form differs from the "
                          "calculated one by more than the rounding tolerance",
    "INCOME_MISMATCH": "critical: income on the pay stub differs from stated income by more "
                       "than the limit",
    "pattern flags": "warning: an inconsistency found by the LLM pattern review, kept only if "
                     "the document fields it cites exist",
}

# portfolio_stats variables: a fixed list, each computed by code from historical_loans.
VARIABLES = ["loan_intent", "home_ownership", "education", "credit_score_band", "income_band",
             "loan_to_income_band", "employment_band", "age_band", "gender",
             "previous_default_on_file"]


def _variable(df: pd.DataFrame, name: str) -> pd.Series:
    lti = df["loan_amnt"] / df["person_income"]
    if name == "loan_intent":
        return df["loan_intent"]
    if name == "home_ownership":
        return df["person_home_ownership"]
    if name == "education":
        return df["person_education"]
    if name == "gender":
        return df["person_gender"]
    if name == "previous_default_on_file":
        return df["previous_loan_defaults_on_file"]
    if name == "credit_score_band":
        return pd.cut(df["credit_score"], [0, 500, 550, 600, 650, 700, 750, 900], right=False,
                      labels=["<500", "500-549", "550-599", "600-649", "650-699", "700-749",
                              "750+"]).astype(str)
    if name == "income_band":
        bands = pd.qcut(df["person_income"], 5, duplicates="drop")
        return bands.map(lambda iv: f"{iv.left:,.0f}-{iv.right:,.0f}").astype(str)
    if name == "loan_to_income_band":
        return pd.cut(lti, [0, .10, .20, .25, .30, np.inf], right=False,
                      labels=["<0.10", "0.10-0.20", "0.20-0.25", "0.25-0.30", "0.30+"]).astype(str)
    if name == "employment_band":
        return pd.cut(df["person_emp_exp"], [0, 1, 3, 5, 10, 20, np.inf], right=False,
                      labels=["<1", "1-2", "3-4", "5-9", "10-19", "20+"]).astype(str)
    if name == "age_band":
        return pd.cut(df["person_age"], [0, 25, 35, 50, np.inf], right=False,
                      labels=["<25", "25-34", "35-49", "50+"]).astype(str)
    raise KeyError(name)


def portfolio_stats(engine, variable: str, by: Optional[str] = None,
                    include_prior_defaulters: bool = False) -> dict:
    if variable not in VARIABLES or (by is not None and by not in VARIABLES):
        return {"error": f"variable and by must be one of: {', '.join(VARIABLES)}"}
    df = load_historical(engine, clean=not include_prior_defaulters)
    keys = [_variable(df, variable).rename(variable)]
    if by:
        keys.append(_variable(df, by).rename(by))
    g = df.groupby(keys, observed=True)["loan_status"].agg(count="size", default_rate="mean")
    rows = [{**dict(zip([variable] + ([by] if by else []), idx if isinstance(idx, tuple) else (idx,))),
             "count": int(r["count"]), "default_rate": round(float(r["default_rate"]), 4),
             **({"small_sample": True} if r["count"] < 100 else {})}
            for idx, r in g.iterrows()]
    out = {"rows": len(df), "overall_default_rate": round(float(df["loan_status"].mean()), 4),
           "groups": rows,
           "data": "historical_loans, " + ("all rows" if include_prior_defaulters else
                                           "cleaned rows (no previous default on file)")}
    if {"gender", "age_band"} & {variable, by}:
        out["note"] = ("Gender and age are shown to understand the data and check fairness. The "
                       "risk model does not use them, and they are never used to decide an "
                       "application.")
    return out


def explain_feature(scorer, feature: str) -> dict:
    shapes = (scorer.metadata or {}).get("shape_functions") or {}
    if feature not in shapes or shapes[feature]["type"] == "pair":
        return {"error": f"feature must be one of: {', '.join(scorer.features)}"}
    sh = shapes[feature]
    importance = scorer.metadata.get("feature_importance", {}).get(feature)
    if sh["type"] == "categorical":
        points = [{"value": x, "contribution": c} for x, c in zip(sh["x"], sh["contribution"])]
    else:
        idx = np.unique(np.linspace(0, len(sh["x"]) - 1, 12).round().astype(int))
        points = [{"from": sh["x"][i], "contribution": sh["contribution"][i]} for i in idx]
    return {"feature": feature, "type": sh["type"], "importance": importance,
            "shape": points, "model_version": scorer.model_version,
            "how_to_read": "Contribution to the log-odds of default at each feature value; "
                           "positive raises risk, negative lowers it."}


def get_rules() -> dict:
    return {"rules": RULES, "flags": FLAG_MEANINGS,
            "source": "Limits come from the dataset analysis (cleaned rows, a band is high risk "
                      "when its default rate is at least 1.5 times the average)."}


def get_recommendation(session: Session, app: Application) -> dict:
    rec = max(app.recommendations, key=lambda r: r.id) if app.recommendations else None
    if rec is None:
        return {"application_id": app.id, "recommendation": None}
    return {"application_id": app.id, "status": app.status,
            "recommendation": rec.recommendation, "reason_codes": rec.reason_codes,
            "confidence": rec.confidence, "passed_validation": rec.passed_validation,
            "marked_for_additional_review": rec.marked_for_additional_review,
            "review_reasons": rec.review_reasons, "explanation": rec.explanation,
            "note": "A recommendation only. The loan officer makes the final decision."}


def _app_param(context: bool) -> dict:
    if context:
        return {"type": "object", "properties": {}, "required": []}
    return {"type": "object", "properties": {"application_id": {"type": "integer"}},
            "required": ["application_id"]}


def tool_definitions(context_app_id: Optional[int]) -> list[dict]:
    ctx = context_app_id is not None
    app_note = (f" (the application on screen, #{context_app_id}; no argument needed)" if ctx
                else " for the given application_id")
    defs = [
        ("get_application", "Form data, loan request, earlier applications, documents and "
                            "extracted fields" + app_note, _app_param(ctx)),
        ("get_recommendation", "The stored recommendation, its validation result and review "
                               "reasons" + app_note, _app_param(ctx)),
        ("score_risk", "The stored risk score: probability, contributions, top drivers"
                       + app_note, _app_param(ctx)),
        ("portfolio_stats", "Count and default rate in historical_loans for one variable, "
                            "optionally split by a second one",
         {"type": "object", "properties": {
             "variable": {"type": "string", "enum": VARIABLES},
             "by": {"type": "string", "enum": VARIABLES},
             "include_prior_defaulters": {"type": "boolean"}}, "required": ["variable"]}),
        ("explain_feature", "How the risk model sees one feature (its shape function)",
         {"type": "object", "properties": {"feature": {"type": "string"}},
          "required": ["feature"]}),
        ("get_rules", "The rule thresholds and what each flag means",
         {"type": "object", "properties": {}, "required": []}),
    ]
    return [{"type": "function", "function": {"name": n, "description": d, "parameters": p}}
            for n, d, p in defs]


def system_prompt(context_app_id: Optional[int], features: list[str]) -> str:
    where = (f"The officer is looking at application #{context_app_id}; questions like \"this "
             "one\" refer to it." if context_app_id is not None else
             "No application is selected; ask for an application id if a question needs one.")
    return f"""You are the assistant in a loan decision support dashboard. You answer a loan
officer's questions about the portfolio data, the risk model and applications, in plain
language, using the tools. {where}

Rules:
- Every number you state must come from a tool result. Do not calculate new numbers.
- You cannot make, change or recommend a final decision. If asked to approve, deny or change an
  application, decline politely, say the loan officer decides on the review page, and offer to
  explain the stored recommendation instead.
- You never change data; all tools are read-only.
- Do not use gender or age to explain an individual application. If asked, say the model does not
  use them and point to the fairness section of the Model page.
- Keep answers short: a few sentences or a small list.
- Model features you can explain: {", ".join(features)}."""


@dataclass
class Answer:
    text: str
    tool_calls: list[dict] = field(default_factory=list)
    unsupported_numbers: list[str] = field(default_factory=list)
    limit_reached: bool = False


def ask(question: str, history: list[dict], *, engine, scorer,
        context_app_id: Optional[int] = None, model: Optional[str] = None) -> Answer:
    """Answer one question. history holds earlier {role, content} turns of this browser session."""
    tools = tool_definitions(context_app_id)
    names = {t["function"]["name"] for t in tools}
    messages = ([{"role": "system", "content": system_prompt(context_app_id, scorer.features)}]
                + history[-10:] + [{"role": "user", "content": question}])
    results: list[Any] = []
    calls: list[dict] = []
    limit = False
    with Session(engine) as s:
        while True:
            reply = llm.chat(messages, purpose="assistant", step="assistant", model=model,
                             tools=None if limit else tools, think=True, session=s,
                             application_id=context_app_id, tool_calls_so_far=len(calls))
            messages.append({"role": "assistant", "content": reply.content,
                             **({"tool_calls": reply.tool_calls} if reply.tool_calls else {})})
            if not reply.tool_calls or limit:
                break
            for call in reply.tool_calls:
                if len(calls) >= MAX_TOOL_CALLS:
                    limit = True
                    messages.append({"role": "user", "content": "Tool-call limit reached; "
                                     "answer with what you have."})
                    break
                name, args, result = run_tool(s, call, names, context_app_id, engine, scorer)
                calls.append({"tool": name, "arguments": args, "ok": "error" not in result})
                results.append(result)
                messages.append({"role": "tool", "tool_name": name,
                                 "content": json.dumps(result, default=str)})
        s.commit()
    text = reply.content.strip() or "(no answer)"
    return Answer(text=text, tool_calls=calls,
                  unsupported_numbers=unsupported_numbers(text, results), limit_reached=limit)


def run_tool(session: Session, call: dict, names: set[str], context_app_id: Optional[int],
             engine, scorer) -> tuple[str, dict, dict]:
    fn = call.get("function") or {}
    name, args = fn.get("name"), fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    result: dict
    if name not in names:
        result = {"error": f"Unknown tool {name!r}."}
    elif name in ("get_application", "get_recommendation", "score_risk"):
        app_id = context_app_id if context_app_id is not None else args.get("application_id")
        app = session.get(Application, app_id) if isinstance(app_id, int) else None
        if app is None:
            result = {"error": f"No application with id {app_id!r}."}
        elif name == "get_application":
            result = tool_get_application(session, app)
        elif name == "get_recommendation":
            result = get_recommendation(session, app)
        else:
            result = (tool_score_risk(session, app, scorer) if app.risk_scores
                      else {"error": "This application has no stored risk score."})
    elif name == "portfolio_stats":
        result = portfolio_stats(engine, args.get("variable"), args.get("by"),
                                 bool(args.get("include_prior_defaulters")))
    elif name == "explain_feature":
        result = explain_feature(scorer, args.get("feature"))
    else:
        result = get_rules()
    log(session, "assistant", "model", f"tool call: {name}",
        context_app_id if context_app_id is not None else (
            args.get("application_id") if isinstance(args.get("application_id"), int)
            and session.get(Application, args.get("application_id")) else None),
        purpose="assistant", tool=name, arguments=args, ok="error" not in result)
    return str(name), args, result
