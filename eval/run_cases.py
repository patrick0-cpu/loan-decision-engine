"""Run the evaluation cases on a copy of the database and write the results.

    python eval/run_cases.py                                  # the six test cases
    python eval/run_cases.py --compare qwen3.5:latest,qwen3.5:35b --samples all

Always works on a fresh copy of loans.db (made with SQLite's backup API, the real file is only
read) and a temporary uploads folder, so the real database never gets test rows. Results go to
eval/results/<YYYY-MM-DD>/ as Markdown tables and a JSON file. Needs samples/ (run
`python make_samples.py` first) and a trained model.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
STEP_LABELS = ["completeness", "extraction_validation", "document_reader", "calculations",
               "rule_check", "pattern_review", "risk_score", "coordinator_and_validation",
               "gatekeeper"]


def prepare_copy(workdir: Path) -> None:
    """Copy the real database and point the app at the copy, before any project import."""
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    real = os.getenv("DATABASE_URL", "sqlite:///loans.db").replace("sqlite:///", "")
    real_path = Path(real) if Path(real).is_absolute() else ROOT / real
    copy = workdir / "eval.db"
    src = sqlite3.connect(f"file:{real_path}?mode=ro", uri=True)
    dst = sqlite3.connect(copy)
    src.backup(dst)
    dst.close()
    src.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{copy.as_posix()}"
    os.environ["UPLOADS_DIR"] = str(workdir / "uploads")
    sys.path.insert(0, str(ROOT))


def load_key() -> list[dict]:
    path = SAMPLES / "answer_key.json"
    if not path.exists():
        sys.exit("samples/answer_key.json is missing. Run `python make_samples.py` first.")
    return json.loads(path.read_text())


def files_for(k: dict, drop: str | None = None, fake_pdf: str | None = None) -> dict:
    out = {}
    for t in ("pay_stub", "bank_statement"):
        if t == drop:
            continue
        data = (SAMPLES / k[t]["file"]).read_bytes()
        if t == fake_pdf:
            data = b"This is a plain text file renamed to look like a PDF.\n"
        out[t] = (k[t]["file"], data)
    return out


def evidence(engine, app_id: int) -> dict:
    from sqlalchemy.orm import Session

    from db import Application
    with Session(engine) as s:
        app = s.get(Application, app_id)
        rec = app.current_recommendation
        score = app.current_risk_score
        steps = {}
        for a in app.audit_logs:
            steps[a.step] = steps.get(a.step, 0) + 1
        return {
            "application_id": app_id, "status": app.status, "current_step": app.current_step,
            "flags": [f"{f.code} ({f.severity})" for f in app.current_flags],
            "default_probability": score.default_probability if score else None,
            "recommendation": rec.recommendation if rec else None,
            "confidence": rec.confidence if rec else None,
            "passed_validation": rec.passed_validation if rec else None,
            "review_reasons": list(rec.review_reasons) if rec else [],
            "officer_decision": [(r.decision, r.officer_name) for r in app.officer_reviews],
            "audit_rows": len(app.audit_logs), "audit_steps": steps,
        }


def field_accuracy(engine, app_id: int, k: dict) -> tuple[int, int]:
    from sqlalchemy.orm import Session

    from db import Application
    from document_reader import FIELDS
    right = total = 0
    with Session(engine) as s:
        for doc in s.get(Application, app_id).documents:
            got = {n: f.field_value for n, f in doc.latest_fields().items()}
            for name in FIELDS[doc.doc_type]:
                want = k[doc.doc_type]["fields"][name]
                value = got.get(name)
                total += 1
                if value is None:
                    continue
                if isinstance(want, (int, float)):
                    try:
                        right += abs(float(value) - want) < 0.005
                    except ValueError:
                        pass
                else:
                    right += str(value).strip().lower() == str(want).strip().lower()
    return right, total


def officer_approve(engine, app_id: int, name: str) -> None:
    """The scripted officer decision for case 1 (what page 4 writes)."""
    from sqlalchemy.orm import Session

    from db import Application, OfficerReview, log
    with Session(engine) as s:
        app = s.get(Application, app_id)
        rec = app.current_recommendation
        s.add(OfficerReview(application_id=app_id, officer_name=name, decision="approved",
                            reason_codes=[], notes="Scripted decision in eval/run_cases.py"))
        app.status, app.current_step = "approved", "officer_decision"
        log(s, "officer_review", f"human:{name}", "decision: approved", app_id,
            decision="approved", status_from="in_review", status_to="approved",
            system_recommendation=rec.recommendation if rec else None)
        s.commit()


class Interrupt(Exception):
    pass


def pick_clean_sample(key: list[dict], scorer) -> dict:
    """The clean sample (no planted mismatch, no rule flags) with the lowest default probability:
    the representative case where approve or refer is expected."""
    from calculations import check_rules, run_calculations
    best = None
    for k in key:
        if k["planted_mismatch"]:
            continue
        form = dict(k["form"])
        flags = check_rules(form, run_calculations(form),
                            documented_income=k["pay_stub"]["annual_income"])
        if flags:
            continue
        p = scorer.score(form)["default_probability"]
        if best is None or p < best[0]:
            best = (p, k)
    return best[1] if best else next(k for k in key if not k["planted_mismatch"])


def run_cases(model: str | None) -> list[dict]:
    import llm
    from db import get_engine
    from risk_scorer import RiskScorer, latest_model_path
    from workflow import continue_application, run_application, start_application

    engine, scorer = get_engine(), RiskScorer(latest_model_path())
    key = load_key()
    llm_ok = llm.available()
    clean = pick_clean_sample(key, scorer)
    planted = next(k for k in key if k["planted_mismatch"])
    other = next(k for k in key if not k["planted_mismatch"] and k is not clean)
    cases = []

    def form_of(k, name=None):
        return dict(k["form"], full_name=name or k["full_name"])

    def record(case, requirement, k, expected, fn, check, needs_llm):
        entry = {"case": case, "requirement": requirement, "sample": k["sample_id"],
                 "input": f"{k['sample_id']} form + documents" + (
                     f" ({case.lower()})" if case != "Clean sample, then officer decision" else ""),
                 "expected": expected}
        if needs_llm and not llm_ok:
            entry.update(actual="not run: the LLM server is unavailable", passed=None,
                         evidence={}, seconds={}, interpretation=(
                             "Draft: blocked. Rerun when the school server is reachable."))
            cases.append(entry)
            print(f"[skipped] {case}: LLM server unavailable", flush=True)
            return
        t0 = time.monotonic()
        app_id, seconds, extra = fn()
        ev = evidence(engine, app_id)
        ok, actual, interpretation = check(ev, extra)
        entry.update(actual=actual, passed=ok, evidence=ev, seconds=seconds,
                     total_seconds=round(time.monotonic() - t0, 2), interpretation=interpretation,
                     **extra.get("metrics", {}))
        cases.append(entry)
        print(f"[{'pass' if ok else 'FAIL'}] {case}: {actual}", flush=True)

    # 1. Representative successful case
    def case1():
        r = run_application(form_of(clean), files_for(clean), engine=engine, scorer=scorer,
                            model=model)
        right, total = field_accuracy(engine, r.application_id, clean)
        officer_approve(engine, r.application_id, "Eval Officer")
        return r.application_id, r.timings, {"metrics": {"field_accuracy": f"{right}/{total}"},
                                             "reached_review": r.status == "in_review"}

    def check1(ev, extra):
        ok = (extra["reached_review"] and ev["recommendation"] in ("approve", "refer")
              and ev["passed_validation"] is True and ev["status"] == "approved"
              and ev["officer_decision"] == [("approved", "Eval Officer")])
        actual = (f"recommendation {ev['recommendation']}, validation "
                  f"{'passed' if ev['passed_validation'] else ev['passed_validation']}, paused at "
                  f"in_review, then approved by Eval Officer (status {ev['status']})")
        return ok, actual, (
            "Draft: the full path from intake to an officer's decision works on a clean sample. "
            "It does not show accuracy on real documents (the samples are clean generated PDFs) "
            "or how the model behaves on borderline applicants. Next: add borderline samples.")

    record("Clean sample, then officer decision", "1. Representative successful case", clean,
           "Completes steps 1 to 9; approve or refer; validation passes; pauses at in_review; a "
           "scripted officer approval is written to officer_reviews and the status becomes "
           "approved", case1, check1, needs_llm=True)

    # 2. Different input condition / branch
    def case2():
        r = run_application(form_of(planted), files_for(planted), engine=engine, scorer=scorer,
                            model=model)
        right, total = field_accuracy(engine, r.application_id, planted)
        return r.application_id, r.timings, {"metrics": {"field_accuracy": f"{right}/{total}"}}

    def check2(ev, extra):
        ok = "INCOME_MISMATCH (critical)" in ev["flags"] and any(
            r.startswith("CRITICAL_FLAG:INCOME_MISMATCH") for r in ev["review_reasons"])
        return ok, (f"flags {', '.join(ev['flags'])}; recommendation {ev['recommendation']}; "
                    f"marked: {', '.join(ev['review_reasons'])}"), (
            "Draft: the pay stub income read by the LLM, multiplied out by code, triggers the "
            "critical INCOME_MISMATCH rule, and the gatekeeper marks the case. The planted gap "
            f"is {planted['pay_stub_income_factor']:.0%} of stated income, far above the 15% "
            "limit, so this does not test borderline gaps. Next: samples at 10-20%.")

    record("Planted mismatch", "2. Different input condition / branch", planted,
           "INCOME_MISMATCH critical flag; marked for additional review", case2, check2,
           needs_llm=True)

    # 3. Invalid or failing case
    def case3():
        t0 = time.monotonic()
        app_id, missing = start_application(form_of(other, "Eval invalid file"),
                                            files_for(other, fake_pdf="bank_statement"),
                                            engine=engine)
        return app_id, {"completeness": round(time.monotonic() - t0, 2)}, {"missing": missing}

    def check3(ev, extra):
        ok = ev["status"] == "stopped_incomplete" and any("Only PDF" in m for m in extra["missing"])
        return ok, f"status {ev['status']}; reason: {'; '.join(extra['missing'])}", (
            "Draft: the file-signature check rejects a text file renamed to .pdf before anything "
            "is read or scored. It does not test a real but corrupted or password-protected PDF "
            "beyond pypdfium2's own error. Next: add those files.")

    record("Invalid file (text renamed to .pdf)", "3. Invalid or failing case", other,
           "Rejected by the file-signature check; stops at step 1 as stopped_incomplete",
           case3, check3, needs_llm=False)

    # Extra: missing bank statement
    def case4():
        t0 = time.monotonic()
        app_id, missing = start_application(form_of(other, "Eval missing statement"),
                                            files_for(other, drop="bank_statement"),
                                            engine=engine)
        return app_id, {"completeness": round(time.monotonic() - t0, 2)}, {"missing": missing}

    def check4(ev, extra):
        ok = ev["status"] == "stopped_incomplete" and "document: bank_statement" in extra["missing"]
        return ok, f"status {ev['status']}; missing: {', '.join(extra['missing'])}", (
            "Draft: a missing document stops the run at step 1 with the missing item named, and "
            "nothing is scored. Next: none.")

    record("Missing bank statement", "Extra: failing case", other,
           "Stops at step 1 as stopped_incomplete", case4, check4, needs_llm=False)

    # Extra: LLM turned off
    def case5():
        r = run_application(form_of(other, "Eval LLM off"), files_for(other), engine=engine,
                            scorer=scorer, use_llm=False)
        return r.application_id, r.timings, {}

    def check5(ev, extra):
        ok = (ev["recommendation"] == "refer" and "LLM_UNAVAILABLE" in ev["review_reasons"]
              and ev["default_probability"] is not None and ev["audit_steps"].get("calculations")
              and ev["audit_steps"].get("rule_check"))
        return ok, (f"status {ev['status']}; recommendation {ev['recommendation']}; probability "
                    f"{ev['default_probability']}; marked: {', '.join(ev['review_reasons'])}"), (
            "Draft: without the LLM the application is still calculated, rule-checked, scored and "
            "handed to the officer, marked LLM_UNAVAILABLE. Documents are stored but not read, so "
            "INCOME_MISMATCH cannot fire. Next: none.")

    record("LLM turned off", "Extra: fallback path", other,
           "Saved with calculations, rules and score; refer; LLM_UNAVAILABLE", case5, check5,
           needs_llm=False)

    # Extra: interrupted then resumed
    def case6():
        timings = {}

        def stop_at_score(step, state, detail):
            if step == "Risk score" and state == "running":
                raise Interrupt("simulated interruption inside the risk score step")
        t0 = time.monotonic()
        try:
            run_application(form_of(other, "Eval interrupted"), files_for(other), engine=engine,
                            scorer=scorer, model=model, use_llm=llm_ok, progress=stop_at_score)
        except Interrupt:
            pass
        timings["first_attempt"] = round(time.monotonic() - t0, 2)
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from db import Application
        with Session(engine) as s:
            app_id = s.scalars(select(Application.id).order_by(Application.id.desc())).first()
            before = s.get(Application, app_id).current_step
        r = continue_application(app_id, engine=engine, scorer=scorer, model=model, use_llm=llm_ok)
        timings.update(r.timings)
        return app_id, timings, {"stopped_at": before}

    def check6(ev, extra):
        twice = {k: v for k, v in ev["audit_steps"].items()
                 if v > 1 and k not in ("document_reader", "extraction_validation", "coordinator",
                                        "assistant")}
        ok = ev["status"] == "in_review" and not twice
        return ok, (f"stopped at {extra['stopped_at']}, resumed to {ev['status']}; steps run twice: "
                    f"{twice or 'none'}"), (
            "Draft: a run cut off inside a step resumes from that step without redoing finished "
            "ones. The interruption here is an exception raised inside the step; the build log "
            "also records a real process kill. Next: none.")

    record("Interrupted then resumed", "Extra: recovery path", other,
           "Completes with no step run twice", case6, check6, needs_llm=False)
    return cases


def run_compare(models: list[str], sample_ids: list[str]) -> dict:
    import llm
    from db import get_engine
    from risk_scorer import RiskScorer, latest_model_path
    from sqlalchemy.orm import Session
    from workflow import run_application

    if not llm.available():
        return {"error": "the LLM server is unavailable", "models": models}
    engine, scorer = get_engine(), RiskScorer(latest_model_path())
    key = {k["sample_id"]: k for k in load_key()}
    report = {}
    for model in models:
        m = dict(right=0, fields=0, json_ok=0, calls=0, planted=0, caught=0, clean=0, false=0,
                 pattern_true=0, pattern_false=0, validated=0, passed=0, retries=0, seconds=0.0,
                 rows=[])
        for sid in sample_ids:
            k = key[sid]
            t0 = time.monotonic()
            r = run_application(dict(k["form"], full_name=k["full_name"]), files_for(k),
                                engine=engine, scorer=scorer, model=model)
            m["seconds"] += time.monotonic() - t0
            right, total = field_accuracy(engine, r.application_id, k)
            m["right"] += right
            m["fields"] += total
            from db import Application
            with Session(engine) as s:
                app = s.get(Application, r.application_id)
                codes = [f.code for f in app.current_flags]
                mismatch = "INCOME_MISMATCH" in codes
                pattern = [f.code for f in app.current_flags if f.source == "pattern"]
                for a in app.audit_logs:
                    if a.step == "extraction_validation":
                        m["calls"] += 1
                        m["json_ok"] += bool(a.details.get("valid_json"))
                    if a.actor == "model" and (a.details or {}).get("attempt", 1) > 1:
                        m["retries"] += 1
                rec = app.current_recommendation
            if k["planted_mismatch"]:
                m["planted"] += 1
                m["caught"] += mismatch
                m["pattern_true"] += bool(pattern)
            else:
                m["clean"] += 1
                m["false"] += mismatch
                m["pattern_false"] += bool(pattern)
            if rec and rec.passed_validation is not None:
                m["validated"] += 1
                m["passed"] += rec.passed_validation
            m["rows"].append({"sample": sid, "planted": k["planted_mismatch"],
                              "field_accuracy": f"{right}/{total}", "flags": codes,
                              "pattern_findings": pattern,
                              "recommendation": rec.recommendation if rec else None,
                              "passed_validation": rec.passed_validation if rec else None,
                              "seconds": round(time.monotonic() - t0, 1)})
            print(f"{model} {sid}: {right}/{total} fields, flags {codes}, "
                  f"{rec.recommendation if rec else None}", flush=True)
        n = len(sample_ids)
        report[model] = {
            "extraction_field_accuracy": round(m["right"] / m["fields"], 4) if m["fields"] else None,
            "fields": f"{m['right']}/{m['fields']}",
            "valid_json": f"{m['json_ok']}/{m['calls']}",
            "planted_caught": f"{m['caught']}/{m['planted']}",
            "false_mismatch_flags": f"{m['false']}/{m['clean']}",
            "pattern_findings_planted": f"{m['pattern_true']}/{m['planted']}",
            "pattern_findings_clean": f"{m['pattern_false']}/{m['clean']}",
            "validation_pass": f"{m['passed']}/{m['validated']}",
            "validation_pass_rate": round(m["passed"] / m["validated"], 4) if m["validated"] else None,
            "retries": m["retries"], "avg_seconds": round(m["seconds"] / n, 1), "rows": m["rows"],
        }
    report["decision"] = decide(models, report)
    return report


def decide(models: list[str], report: dict) -> dict:
    """CLAUDE2.md E10 rule: use the smaller model (the first) if it is within 5 points of the
    larger on extraction accuracy, catches at least as many planted mismatches, and its
    validation pass rate is within 5 points (not more than 5 points lower)."""
    small, large = report[models[0]], report[models[1]]

    def caught(r):
        return int(r["planted_caught"].split("/")[0])
    ok = ((small["extraction_field_accuracy"] or 0) >= (large["extraction_field_accuracy"] or 0) - 0.05
          and caught(small) >= caught(large)
          and (small["validation_pass_rate"] or 0) >= (large["validation_pass_rate"] or 0) - 0.05)
    return {"chosen": models[0] if ok else models[1], "smaller_meets_rule": ok}


def markdown_cases(cases: list[dict], meta: dict) -> str:
    lines = [f"# Evaluation cases ({meta['date']})", "",
             f"Model: `{meta['model']}` · LLM server reachable: {meta['llm_available']} · "
             f"risk model `{meta['risk_model']}` · database copy, never the real `loans.db`.", "",
             "Interpretations are drafts for the team to review.", "",
             "| Case | Requirement | Expected | Actual | Result | Seconds |",
             "|---|---|---|---|---|---|"]
    for c in cases:
        result = {True: "pass", False: "FAIL", None: "not run"}[c["passed"]]
        lines.append(f"| {c['case']} | {c['requirement']} | {c['expected']} | {c['actual']} | "
                     f"{result} | {c.get('total_seconds', '')} |")
    # No Milestone 1 measurable outcome was set (CLAUDE3.md), so report the default measures.
    ran = [c for c in cases if c["passed"] is not None and c.get("evidence", {}).get(
        "status") in ("in_review", "approved")]
    acc = [c["field_accuracy"] for c in cases if c.get("field_accuracy")]
    planted = [c for c in cases if c["case"] == "Planted mismatch"]
    lines += ["", "## Measured outcomes", "",
              "| Measure | Value |", "|---|---|",
              "| Seconds per completed application | " + (", ".join(
                  f"{c['case']}: {c['total_seconds']}" for c in ran) or "none completed") + " |",
              "| Extraction field accuracy | " + (", ".join(acc) or "not measured (LLM cases not "
                                                    "run)") + " |",
              "| Planted mismatches caught | " + (
                  f"{sum(bool(c['passed']) for c in planted if c['passed'] is not None)}/"
                  f"{sum(c['passed'] is not None for c in planted)}"
                  if any(c["passed"] is not None for c in planted) else "not run") + " |"]
    lines += ["", "## Evidence and interpretation", ""]
    for c in cases:
        ev = c.get("evidence") or {}
        lines += [f"### {c['case']}", "",
                  f"- Input: {c['input']}",
                  f"- Evidence: application {ev.get('application_id')}, status {ev.get('status')}, "
                  f"flags {ev.get('flags')}, recommendation {ev.get('recommendation')}, "
                  f"validation {ev.get('passed_validation')}, review reasons "
                  f"{ev.get('review_reasons')}, {ev.get('audit_rows')} audit rows",
                  f"- Seconds per step: {c.get('seconds')}",
                  f"- Interpretation and next action: {c['interpretation']}", ""]
    return "\n".join(lines)


def markdown_compare(report: dict, models: list[str], samples: list[str]) -> str:
    if "error" in report:
        return f"# Model comparison\n\nNot run: {report['error']}.\n"
    rows = ["extraction_field_accuracy", "fields", "valid_json", "planted_caught",
            "false_mismatch_flags", "pattern_findings_planted", "pattern_findings_clean",
            "validation_pass", "retries", "avg_seconds"]
    lines = [f"# Model comparison on {len(samples)} samples", "",
             "| Measure | " + " | ".join(f"`{m}`" for m in models) + " |",
             "|---|" + "---|" * len(models)]
    for r in rows:
        lines.append(f"| {r.replace('_', ' ')} | " + " | ".join(str(report[m][r]) for m in models)
                     + " |")
    d = report["decision"]
    lines += ["", f"Decision (E10 rule): **{d['chosen']}** (smaller model meets the rule: "
                  f"{d['smaller_meets_rule']})."]
    return "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", help="LLM model for the cases (default: LLM_MODEL_ACTIVE)")
    p.add_argument("--compare", help="two models, comma-separated: smaller first")
    p.add_argument("--samples", default="all", help="'all' or comma-separated ids, e.g. S01,S04")
    p.add_argument("--out", default=str(ROOT / "eval" / "results"))
    args = p.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="loan_eval_"))
    try:
        prepare_copy(workdir)
        import llm
        from risk_scorer import latest_model_path
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out = Path(args.out) / date
        out.mkdir(parents=True, exist_ok=True)
        meta = {"date": date, "model": args.model or llm.active_model(),
                "llm_available": llm.available(), "risk_model": latest_model_path().stem}
        if args.compare:
            models = args.compare.split(",")
            key = load_key()
            samples = [k["sample_id"] for k in key] if args.samples == "all" \
                else args.samples.split(",")
            report = run_compare(models, samples)
            (out / "compare.json").write_text(json.dumps({**meta, "samples": samples,
                                                          "report": report}, indent=1, default=str))
            (out / "compare.md").write_text(markdown_compare(report, models, samples),
                                            encoding="utf-8")
            print(markdown_compare(report, models, samples))
        else:
            cases = run_cases(args.model)
            (out / "cases.json").write_text(json.dumps({**meta, "cases": cases}, indent=1,
                                                       default=str))
            (out / "cases.md").write_text(markdown_cases(cases, meta), encoding="utf-8")
            print(f"\nWrote {out / 'cases.md'} and cases.json")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
