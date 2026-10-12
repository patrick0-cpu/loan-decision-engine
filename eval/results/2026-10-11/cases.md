# Evaluation cases (2026-10-11)

Model: `qwen3.5:latest` · LLM server reachable: True · risk model `ebm-20261011T230951Z` · database copy, never the real `loans.db`.

Interpretations are drafts for the team to review.

| Case | Requirement | Expected | Actual | Result | Seconds |
|---|---|---|---|---|---|
| Clean sample, then officer decision | 1. Representative successful case | Completes steps 1 to 9; approve or refer; validation passes; pauses at in_review; a scripted officer approval is written to officer_reviews and the status becomes approved | recommendation approve, validation passed, paused at in_review, then approved by Eval Officer (status approved) | pass | 94.22 |
| Planted mismatch | 2. Different input condition / branch | INCOME_MISMATCH critical flag; marked for additional review | flags INCOME_MISMATCH (critical), ANNUAL_INCOME_MISMATCH_FORM_STUB (warning); recommendation refer; marked: CRITICAL_FLAG:INCOME_MISMATCH | pass | 154.55 |
| Invalid file (text renamed to .pdf) | 3. Invalid or failing case | Rejected by the file-signature check; stops at step 1 as stopped_incomplete | status stopped_incomplete; reason: document: bank_statement (Only PDF, PNG and JPG files are accepted.) | pass | 0.01 |
| Missing bank statement | Extra: failing case | Stops at step 1 as stopped_incomplete | status stopped_incomplete; missing: document: bank_statement | pass | 0.0 |
| LLM turned off | Extra: fallback path | Saved with calculations, rules and score; refer; LLM_UNAVAILABLE | status in_review; recommendation refer; probability 0.6342; marked: LLM_UNAVAILABLE, EXTRACTION_NEEDS_REVIEW | pass | 0.03 |
| Interrupted then resumed | Extra: recovery path | Completes with no step run twice | stopped at risk_scoring, resumed to in_review; steps run twice: none | pass | 71.55 |

## Measured outcomes

| Measure | Value |
|---|---|
| Seconds per completed application | Clean sample, then officer decision: 94.22, Planted mismatch: 154.55, LLM turned off: 0.03, Interrupted then resumed: 71.55 |
| Extraction field accuracy | 10/10, 10/10 |
| Planted mismatches caught | 1/1 |

## Evidence and interpretation

### Clean sample, then officer decision

- Input: S06 form + documents
- Evidence: application 1, status approved, flags [], recommendation approve, validation True, review reasons [], 24 audit rows
- Seconds per step: {'completeness': 0.05, 'document_reader': 8.79, 'calculations': 0.01, 'rule_check': 0.0, 'pattern_review': 66.98, 'risk_score': 0.01, 'coordinator_and_validation': 18.35, 'gatekeeper': 0.01}
- Interpretation and next action: Draft: the full path from intake to an officer's decision works on a clean sample. It does not show accuracy on real documents (the samples are clean generated PDFs) or how the model behaves on borderline applicants. Next: add borderline samples.

### Planted mismatch

- Input: S01 form + documents (planted mismatch)
- Evidence: application 2, status in_review, flags ['INCOME_MISMATCH (critical)', 'ANNUAL_INCOME_MISMATCH_FORM_STUB (warning)'], recommendation refer, validation True, review reasons ['CRITICAL_FLAG:INCOME_MISMATCH'], 25 audit rows
- Seconds per step: {'completeness': 0.0, 'document_reader': 8.82, 'calculations': 0.0, 'rule_check': 0.0, 'pattern_review': 120.1, 'risk_score': 0.01, 'coordinator_and_validation': 25.6, 'gatekeeper': 0.0}
- Interpretation and next action: Draft: the pay stub income read by the LLM, multiplied out by code, triggers the critical INCOME_MISMATCH rule, and the gatekeeper marks the case. The planted gap is 123% of stated income, far above the 15% limit, so this does not test borderline gaps. Next: samples at 10-20%.

### Invalid file (text renamed to .pdf)

- Input: S02 form + documents (invalid file (text renamed to .pdf))
- Evidence: application 3, status stopped_incomplete, flags [], recommendation None, validation None, review reasons [], 2 audit rows
- Seconds per step: {'completeness': 0.0}
- Interpretation and next action: Draft: the file-signature check rejects a text file renamed to .pdf before anything is read or scored. It does not test a real but corrupted or password-protected PDF beyond pypdfium2's own error. Next: add those files.

### Missing bank statement

- Input: S02 form + documents (missing bank statement)
- Evidence: application 4, status stopped_incomplete, flags [], recommendation None, validation None, review reasons [], 2 audit rows
- Seconds per step: {'completeness': 0.0}
- Interpretation and next action: Draft: a missing document stops the run at step 1 with the missing item named, and nothing is scored. Next: none.

### LLM turned off

- Input: S02 form + documents (llm turned off)
- Evidence: application 5, status in_review, flags [], recommendation refer, validation None, review reasons ['LLM_UNAVAILABLE', 'EXTRACTION_NEEDS_REVIEW'], 8 audit rows
- Seconds per step: {'completeness': 0.0, 'calculations': 0.01, 'rule_check': 0.0, 'risk_score': 0.01, 'gatekeeper': 0.0}
- Interpretation and next action: Draft: without the LLM the application is still calculated, rule-checked, scored and handed to the officer, marked LLM_UNAVAILABLE. Documents are stored but not read, so INCOME_MISMATCH cannot fire. Next: none.

### Interrupted then resumed

- Input: S02 form + documents (interrupted then resumed)
- Evidence: application 6, status in_review, flags [], recommendation deny, validation True, review reasons [], 25 audit rows
- Seconds per step: {'first_attempt': 45.73, 'risk_score': 0.15, 'coordinator_and_validation': 25.66, 'gatekeeper': 0.0}
- Interpretation and next action: Draft: a run cut off inside a step resumes from that step without redoing finished ones. The interruption here is an exception raised inside the step; the build log also records a real process kill. Next: none.
