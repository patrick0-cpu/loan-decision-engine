# Evaluation cases (2026-10-11)

Model: `qwen3.5:latest` · LLM server reachable: False · risk model `ebm-20261011T230951Z` · database copy, never the real `loans.db`.

Interpretations are drafts for the team to review.

| Case | Requirement | Expected | Actual | Result | Seconds |
|---|---|---|---|---|---|
| Clean sample, then officer decision | 1. Representative successful case | Completes steps 1 to 9; approve or refer; validation passes; pauses at in_review; a scripted officer approval is written to officer_reviews and the status becomes approved | not run: the LLM server is unavailable | not run |  |
| Planted mismatch | 2. Different input condition / branch | INCOME_MISMATCH critical flag; marked for additional review | not run: the LLM server is unavailable | not run |  |
| Invalid file (text renamed to .pdf) | 3. Invalid or failing case | Rejected by the file-signature check; stops at step 1 as stopped_incomplete | status stopped_incomplete; reason: document: bank_statement (Only PDF, PNG and JPG files are accepted.) | pass | 0.05 |
| Missing bank statement | Extra: failing case | Stops at step 1 as stopped_incomplete | status stopped_incomplete; missing: document: bank_statement | pass | 0.0 |
| LLM turned off | Extra: fallback path | Saved with calculations, rules and score; refer; LLM_UNAVAILABLE | status in_review; recommendation refer; probability 0.6342; marked: LLM_UNAVAILABLE, EXTRACTION_NEEDS_REVIEW | pass | 0.02 |
| Interrupted then resumed | Extra: recovery path | Completes with no step run twice | stopped at risk_scoring, resumed to in_review; steps run twice: none | pass | 0.02 |

## Measured outcomes

| Measure | Value |
|---|---|
| Seconds per completed application | LLM turned off: 0.02, Interrupted then resumed: 0.02 |
| Extraction field accuracy | not measured (LLM cases not run) |
| Planted mismatches caught | not run |

## Evidence and interpretation

### Clean sample, then officer decision

- Input: S06 form + documents
- Evidence: application None, status None, flags None, recommendation None, validation None, review reasons None, None audit rows
- Seconds per step: {}
- Interpretation and next action: Draft: blocked. Rerun when the school server is reachable.

### Planted mismatch

- Input: S01 form + documents (planted mismatch)
- Evidence: application None, status None, flags None, recommendation None, validation None, review reasons None, None audit rows
- Seconds per step: {}
- Interpretation and next action: Draft: blocked. Rerun when the school server is reachable.

### Invalid file (text renamed to .pdf)

- Input: S02 form + documents (invalid file (text renamed to .pdf))
- Evidence: application 1, status stopped_incomplete, flags [], recommendation None, validation None, review reasons [], 2 audit rows
- Seconds per step: {'completeness': 0.05}
- Interpretation and next action: Draft: the file-signature check rejects a text file renamed to .pdf before anything is read or scored. It does not test a real but corrupted or password-protected PDF beyond pypdfium2's own error. Next: add those files.

### Missing bank statement

- Input: S02 form + documents (missing bank statement)
- Evidence: application 2, status stopped_incomplete, flags [], recommendation None, validation None, review reasons [], 2 audit rows
- Seconds per step: {'completeness': 0.0}
- Interpretation and next action: Draft: a missing document stops the run at step 1 with the missing item named, and nothing is scored. Next: none.

### LLM turned off

- Input: S02 form + documents (llm turned off)
- Evidence: application 3, status in_review, flags [], recommendation refer, validation None, review reasons ['LLM_UNAVAILABLE', 'EXTRACTION_NEEDS_REVIEW'], 8 audit rows
- Seconds per step: {'completeness': 0.0, 'calculations': 0.01, 'rule_check': 0.0, 'risk_score': 0.0, 'gatekeeper': 0.0}
- Interpretation and next action: Draft: without the LLM the application is still calculated, rule-checked, scored and handed to the officer, marked LLM_UNAVAILABLE. Documents are stored but not read, so INCOME_MISMATCH cannot fire. Next: none.

### Interrupted then resumed

- Input: S02 form + documents (interrupted then resumed)
- Evidence: application 4, status in_review, flags [], recommendation refer, validation None, review reasons ['LLM_UNAVAILABLE', 'EXTRACTION_NEEDS_REVIEW'], 8 audit rows
- Seconds per step: {'first_attempt': 0.01, 'risk_score': 0.0, 'gatekeeper': 0.0}
- Interpretation and next action: Draft: a run cut off inside a step resumes from that step without redoing finished ones. The interruption here is an exception raised inside the step; the build log also records a real process kill. Next: none.
