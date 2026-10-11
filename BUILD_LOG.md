# Build log

This log records every build step: what was tested, what changed, the decisions the spec left
open, and the problems found. Phase 1 followed `CLAUDE.md`; phase 2 follows `CLAUDE2.md`.

## Phase 1 (from `CLAUDE.md`)

Build recap moved here from `README.md` at the start of phase 2.

### Results

#### Data and target check

- `historical_loans`: **45,000 rows**, with a schema matching the spec exactly. It still has 45,000
  rows after every step.
- Cleaned rows (no previous default): **22,142**, with a **45.2%** default rate.
- `--check`: status 1 has higher interest rates (12.86 vs 10.49), higher loan-to-income (0.20 vs
  0.12) and lower credit scores (632 vs 650). All three signals agree that **1 = default**.

#### Rule checks (step 3)

| Test | Result |
|---|---|
| Test 1: payment 789.85, payment-to-income 0.1975; flags `PAYMENT_TO_INCOME_HIGH`, `LOAN_TO_INCOME_HIGH`, `CREDIT_SCORE_BELOW_MINIMUM`, `INCOME_MISMATCH` | Pass |
| Test 2: no flags | Pass |
| Extra: `PREVIOUS_DEFAULT_ON_FILE`, `RATIO_INCONSISTENT`, 0% rate, zero income, given term | Pass |

#### Model (`ebm-20261010T143918Z`)

| AUC | Accuracy at 0.5 | Brier | Test default rate |
|---|---|---|---|
| **0.9413** | 0.867 | 0.0937 | 45.2% |

Top terms: home ownership × loan-to-income, loan-to-income, interest rate, income, home ownership.

**Fairness (test set): no group below the four-fifths (0.8) line.**

| Group | Ratio to best | | Group | Ratio to best |
|---|---|---|---|---|
| female | 1.000 | | <25 | **0.844** (closest to the line) |
| male | 0.987 | | 25-34 | 0.899 |
| | | | 35-49 | 0.944 |
| | | | 50+ (only 56 people) | 1.000 |

Age proxies: work experience correlates **+0.96** with age, and credit history length **+0.86**.

#### Dashboard (step 7)

Tested end to end with Streamlit's `AppTest` on a **copy** of the database, so the real
`loans.db` holds no test applicants:

- All five pages and Home render without errors, including the "include all rows" toggle (45,000 rows).
- Scoring one applicant adds 1 `applicants`, 1 `applications`, 3 `calculations`, 4 `flags`,
  1 `risk_scores`, 1 `recommendations` and 6 `audit_log` rows.
- An empty form is rejected and nothing is saved.
- Officer review: a denial without a reason code is blocked, a missing name is blocked, and a
  valid denial writes `officer_reviews`, sets the status to `denied` and logs `human:<name>`.
  "Request more information" sets `pending_applicant`.
- The audit log page filters by application, step and actor.
- `streamlit run app/Home.py` started from a different folder opens the same `loans.db`
  (health check OK, HTTP 200).

#### Definition of done

| Item | Status |
|---|---|
| Every file in the folder layout exists and its test passed | ✅ |
| No code reads a CSV; `historical_loans` still has 45,000 rows | ✅ |
| Target check matches the expected pattern before training | ✅ |
| Rule check tests 1 and 2 pass | ✅ |
| Model trained, saved, and loaded by the dashboard without retraining | ✅ |
| All five dashboard pages run without errors | ✅ |
| Scoring one applicant creates rows in all seven tables | ✅ |
| An officer decision creates `officer_reviews` and updates the status | ✅ |

---

### What went well

- **The data matched the analysis exactly.** 45,000 and 22,142 rows and a 45.2% default rate.
  The target check agreed on all three signals, so training started without surprises.
- **The SQLite settings worked on the first try.** The path resolves to the project root
  (tested from inside `app/` and from the home folder), and foreign keys and WAL mode are on for
  every connection.
- **The calculations matched the expected values exactly** (789.85 and 0.1975), and both rule
  tests passed on the first run.
- **The model's explanations add up.** For every applicant, the baseline log-odds plus all the
  contributions gives back the reported probability, so what the officer sees accounts for the
  whole score.
- **The fairness check passed** with no group below 0.8.
- **Testing never touched real data.** The dashboard tests ran against a copy of the database
  (set through `DATABASE_URL`), so the real `loans.db` has no test applicants or audit rows.
- **One step at a time.** Problems were found early: the `.env` path, the repository location,
  and the incomplete test applicant for the scorer.

### Problems found and fixed

| Problem | Fix |
|---|---|
| `.env` contained `sqlite:///./data/loans.db`, which would have created a new, empty database | Changed to `sqlite:///loans.db` with the user's approval |
| The Git repository was the whole home folder (`C:\Users\kouad`) | `git init -b main` inside `loan_system/`, with the user's approval |
| The step 3 test applicant had only 5 of the 10 model features | Placeholder values for the scorer test (below), agreed with the user |
| Windows console couldn't print emoji in test output | Tests run with `PYTHONIOENCODING=utf-8` (test scripts only; the app is unaffected) |

---

### Every choice made

Choices the spec left open, and additions beyond it. Nothing here changes a rule or threshold
in `CLAUDE.md`.

#### Database (`db.py`)

1. **UTC timestamps via a small custom type (`UTCDateTime`).** SQLite can't store time zones, so
   datetimes are saved as UTC and come back timezone-aware. A naive datetime is rejected rather
   than guessed.
2. **Allowed-value checks** on `applications.status`, `flags.source`, `flags.severity`,
   `recommendations.recommendation` and `officer_reviews.decision`, using the values listed in
   the spec. These are standard SQL, so they stay portable.
3. **The audit log is append-only in code.** Changing or deleting an `audit_log` row through the
   ORM raises an error (rule 6). Raw SQL could still bypass it; a database trigger would be
   stronger but SQLite-specific.
4. **`HistoricalLoan` columns are nullable**, to match the table `load_data.py` creates (it has
   no `NOT NULL`).
5. **`log()` adds the row but doesn't commit.** The caller commits, so an application and its
   audit entries are saved together or not at all.
6. **The "table is missing or empty" error also fires when the cleaned filter returns nothing.**

#### Calculations (`calculations.py`)

7. **Rounding:** payment to 2 decimals, ratios to 4. Rules compare the rounded values.
8. **Payment-to-income also carries the "assumed 36 months" note**, because its 0.10 limit came
   from a 36-month analysis.
9. **The income-mismatch gap is measured against stated income:** `|stated − documented| / stated`.
   For test 1 that is 0.1667.
10. **Zero or missing income makes both ratios `None` instead of crashing.** No flag is raised
    in that case (see open items).
11. **A `FORMULAS` dict** holds a readable formula for each metric, stored in
    `calculations.code_used`.
12. Each flag's evidence always has `value` and `limit`, plus context where useful (stated and
    documented income, provided and calculated ratio).

#### Training (`train_ebm.py`)

13. **The model's `.json` file holds more than the spec lists:** `trained_at`, `test_rows`,
    `excluded_other` (the previous-default column), the full feature importance, and the complete
    fairness tables. The Model page reads these directly, without reloading the data.
14. **Model version = `ebm-<UTC timestamp>`**, for example `ebm-20261010T143918Z`. Names sort
    by age, which `latest_model_path()` relies on.
15. **Fairness:** favorable = predicted probability below 0.5. Age bands use `[0,25)`,
    `[25,35)`, `[35,50)`, `[50,∞)`. Proxy correlations are computed on all cleaned rows, not
    just the test set.
16. **`--out` resolves against the project folder**, like the database path.
17. **Passing both `--check` and `--target-means`** runs the check first, then trains.
18. **The EBM's default settings were kept**, including its 30 automatic pairwise interaction
    terms. This is why contributions have 40 entries, not 10.

#### Risk scorer (`risk_scorer.py`)

19. **The scorer refuses to load a model whose saved feature list differs from
    `train_ebm.FEATURES`**, to catch a stale model after a code change.
20. **A feature counts as missing if its key is absent or `None`.**
21. **Top drivers and protective factors are `(term, value)` pairs**, so the dashboard can show
    the numbers.
22. **Placeholder values for the scorer test** (the step 3 applicant lacks them): Bachelor,
    RENT, PERSONAL, 3 years' experience, 4 years' credit history. Result: probability 1.0
    (0.99999987 before rounding; log-odds 15.84).

#### Dashboard (`app/`)

23. **`app/shared.py` was added** (not in the original layout) so the pages share one cached
    engine, one cached model, the labels and the application view. Pages 3 and 4 use the same
    view.
24. **The model cache is keyed by file path**, so a newly trained model is picked up on the next
    page load without restarting.
25. **`loan_percent_income` is optional on the scoring form.** If it's blank, the model gets the
    calculated loan / income (rounded to 2 decimals, like the dataset), and the
    `RATIO_INCONSISTENT` check is skipped because there is nothing to compare. The audit log
    records which value was used.
26. **Form dropdowns** (education, home ownership, intent) take their options from
    `historical_loans`, so the model only receives values it has seen.
27. **Recommendation fields:** `recommendation="refer"` always. Reason codes are
    `MODEL_DRIVER:<term>` for the top risk drivers, followed by the flag codes. `confidence` and
    `passed_validation` are left empty (`None`), because they belong to the later LLM phase.
    `review_reasons` lists the critical flag codes.
28. **Audit actors:** `code` for intake, calculations, rules, recommendation and status changes;
    `model` for scoring; `human:<name>` for officer decisions. `tool` is unused, kept for the
    later phase.
29. **Scoring runs in one transaction.** If any step fails, nothing is saved and the officer
    sees the error.
30. **Reason codes on the review page:** the application's flag codes, its recommendation codes,
    and six general codes (`INSUFFICIENT_INCOME`, `HIGH_DEBT_BURDEN`, `WEAK_CREDIT_HISTORY`,
    `INCOMPLETE_DOCUMENTATION`, `UNVERIFIED_INCOME`, `OFFICER_JUDGMENT`). The general list is my
    own suggestion; edit `GENERAL_REASONS` in `4_Officer_review_queue.py` to match your
    institution.
31. **A status re-check before writing a decision.** If another officer already decided, the
    second decision is refused.
32. **The contribution chart** shows the 10 largest terms plus "N other terms combined". Red
    means the term raises risk and blue means it lowers it; each bar also shows its signed value,
    so color is never the only cue.
33. **The portfolio histograms are stacked by outcome**, with dashed lines at the 550 and 0.25
    rule limits. The default-rate bars show a dashed overall-average line.
34. **The fairness charts** show each group's ratio to the best group, with the 0.8 line, plus
    the full table. Age-proxy correlations are listed underneath.
35. **Chart colors** come from a colorblind-checked palette (blue/orange for outcomes, blue/red
    for lowers/raises risk). Streamlit's theme handles dark mode.

#### Process

36. **Dashboard tests ran on a copy of the database**, so the real `loans.db` has 0 applications
    and 0 audit rows.
37. **Nothing has been committed to Git.** The repository is initialized and all project files
    are untracked, ready for a first commit when you want one.

---

### Known limits and open items

**Decisions for you**
- **Credit score minimum: 550 or the stricter 600?** It's `RULES["min_credit_score"]` in
  `calculations.py`; the comment there has the trade-off.
- **Zero or missing income raises no flag.** Both ratios become undefined and nothing fires. A
  new rule (for example `INCOME_MISSING`, critical) would need your approval (CLAUDE.md rule 5).
- **Age proxies.** Work experience (+0.96) and credit history length (+0.86) closely track age.
  Their direct effect is small, but you may want to watch them or test a model without them.
- **The under-25 band is closest to the fairness line, at 0.844**, and the 50+ comparison group
  has only 56 people, so the age ratios are noisy.

**Built-in limits** (from the dataset analysis)
- No existing-debt column, so true DTI can't be calculated; loan-to-income and payment-to-income
  stand in for it.
- No loan term column: 36 months is assumed when no term is given, and the assumption is stored
  with each result.
- Synthetic data.

**Not yet verified**
- The charts were checked by the automated tests (they render without errors) but haven't been
  reviewed by eye in a browser. Worth a quick look for label overlap.

**Later phase (out of scope):** Document Reader, pattern review, Decision Coordinator.

## Phase 2 (from `CLAUDE2.md`)

Started 2026-10-11. Before any change: `historical_loans` 45,000 rows, every other table 0 rows.
`git status` confirmed `loans.db`, `.env`, `models/`, `uploads/`, `samples/` and
`models_experiments/` are ignored (the last three were added to `.gitignore`). Baseline commit
`cb306e5` holds phase 1 so it can be restored.

Tests run from scripts in a scratch folder (not committed), against the real code. Every test
that writes to a database uses a copy of `loans.db` selected through `DATABASE_URL`.

### Part A. Fixes from the README review

### A1. INCOME_MISSING rule — ✅ Done
- Test: test 1 (payment 789.85, PTI 0.1975, the four expected flags) and test 2 (no flags) still
  pass. Income 0, −5 and missing each give only `INCOME_MISSING` with evidence
  `{"value": <income>, "limit": "> 0"}`. Income 0 with credit score 500 and a prior default gives
  `INCOME_MISSING`, `CREDIT_SCORE_BELOW_MINIMUM`, `PREVIOUS_DEFAULT_ON_FILE`.
- Changes: `calculations.py` (`check_rules`).
- Decisions: the check runs first, so `INCOME_MISSING` is always the first flag in the list.
  `INCOME_MISMATCH` already skips a zero stated income, so it can't fire alongside.
- Problems: none.

### A2. Audit log protected in the database — ✅ Done
- Test: on a copy of `loans.db`, wrote one audit row, then ran raw-SQL
  `UPDATE audit_log SET action='x'` and `DELETE FROM audit_log`. Both failed with
  `IntegrityError: audit_log rows are append-only`, and the row was unchanged. A second
  `get_engine()` call ran cleanly (`IF NOT EXISTS`).
- Changes: `db.py` (`get_engine` creates triggers `audit_log_no_update` and `audit_log_no_delete`).
- Decisions: triggers use `RAISE(ABORT, ...)`, so the whole statement is rolled back. They are
  created only for SQLite, because the syntax is SQLite-specific. The ORM guard stays.
- Problems: none. The real `loans.db` gets the triggers the next time `get_engine()` opens it
  (a schema addition only, no rows touched).

### A3. Fairness compared to the largest group — ✅ Done
- Test: recomputed the report for the current model (`ebm-20261010T143918Z`) on the same test
  split (no retraining):

  | Gender | Count | Favorable rate | Ratio to largest (male) | Sample |
  |---|---|---|---|---|
  | female | 1,972 | 0.6060 | 1.0135 | |
  | male | 2,457 | 0.5979 | 1.0000 | |

  | Age band | Count | Favorable rate | Ratio to largest (25-34) | Sample |
  |---|---|---|---|---|
  | <25 | 1,514 | 0.5727 | 0.9390 | |
  | 25-34 | 2,353 | 0.6099 | 1.0000 | |
  | 35-49 | 506 | 0.6403 | 1.0499 | |
  | 50+ | 56 | 0.6786 | 1.1127 | small sample |

  No group is below 0.8. Under-25s improve from 0.844 (against the best group) to 0.939 (against
  the largest group).
- Changes: `train_ebm.py` (`fairness_table` gives `ratio_to_largest` and `small_sample`; the
  warnings mention small samples). `app/pages/2_Model.py` (charts and tables show group size and
  grey out small-sample groups; it falls back to `ratio_to_best` for models trained earlier).
- Decisions: "largest" means the most people in the category within the test set. Ratios can go
  above 1.0, so the chart's y-axis now reaches 1.3. Small groups still get a warning when they are
  below 0.8, labelled as a small sample. The B1 rule ignores them, as the spec says.
- Problems: my first patch script wrote a literal line break inside an f-string (`\n` escaping
  through a heredoc). I rewrote `train_ebm.py` in full and the syntax check passed.
- Also in this step, because B1 and C1/C2 need it: `train_ebm.py` was restructured into `split()`,
  `fit()`, `fairness_report()`, `performance_data()` and `explanation_data()`, with new options
  `--interactions N` and `--drop FEATURE ...`. The defaults are unchanged.

### A4. No more "1.0" probabilities on screen — ✅ Done
- Test: AppTest on a database copy. Scored the step 3 test applicant (probability 0.99999987).
  Page 3 shows "above 99%" and the page 4 queue shows "above 99%". The helper's own checks:
  0.995 → "above 99%", 0.99 → "99.0%", 0.01 → "1.0%", 0.009 → "below 1%". All six pages render
  with no exceptions.
- Changes: `app/shared.py` (`format_probability`, used in the application view),
  `app/pages/4_Officer_review_queue.py`.
- Decisions: the boundaries are strict, as the spec says (above 0.99, below 0.01), so exactly 0.99
  shows "99.0%". Fairness tables show group averages, not an individual's probability, so they
  stay numeric.
- Problems: the AppTest harness needed `app/` on `sys.path`, which `streamlit run` adds itself.

### Part B. Model experiment

### B1. Four variants in `models_experiments/` — ✅ Done
- Test: each variant was trained with `train_ebm.py --target-means default --out models_experiments`
  and the options shown below, on the same split (`random_state=42`, 17,713 training and 4,429
  test rows). Fairness ratios use the A3 method (ratio to the largest group).

  | Variant | Options | Model | AUC | Brier | Terms | female | male | <25 | 25-34 | 35-49 | 50+ (n=56, small) | Lowest ratio, n≥100 | AUC within 0.01 of best | No ratio < 0.8 (n≥100) |
  |---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
  | a | defaults | `ebm-20261011T070424Z` | **0.9413** | 0.0937 | 40 | 1.014 | 1.000 | 0.939 | 1.000 | 1.050 | 1.113 | 0.939 | yes | yes |
  | b | `--interactions 0` | `ebm-20261011T070507Z` | 0.9202 | 0.1109 | 10 | 0.996 | 1.000 | 0.918 | 1.000 | 1.008 | 1.079 | 0.918 | no | yes |
  | c | b + `--drop person_emp_exp cb_person_cred_hist_length` | `ebm-20261011T070546Z` | 0.9204 | 0.1108 | 8 | 1.000 | 1.000 | 0.928 | 1.000 | 1.021 | 1.022 | 0.928 | no | yes |
  | d | c + `--drop loan_int_rate` | `ebm-20261011T070625Z` | 0.8769 | 0.1411 | 7 | 1.010 | 1.000 | 0.946 | 1.000 | 1.013 | 1.014 | 0.946 | no | yes |

  Group sizes in the test set: female 1,972, male 2,457, <25 1,514, 25-34 2,353, 35-49 506,
  50+ 56. (Variant c has 8 terms and d has 7 because they drop 2 and 3 of the 10 features.)
- **Decision (B1 rule): variant a.** The best AUC is 0.9413 (a), so a variant qualifies on
  accuracy only with an AUC of at least 0.9313. b (0.9202), c (0.9204) and d (0.8769) all fall
  short. Every variant passes the fairness condition. Of the variants that meet both conditions,
  the simplest is the only one, a.
- Reasoning in numbers: the 30 interaction terms are worth about 0.021 AUC. The largest term in
  the model is the home ownership × loan-to-income pair (importance 1.50). Dropping the two age
  proxies costs nothing beyond that (c 0.9204 vs b 0.9202) and raises the lowest age ratio only
  slightly (0.918 to 0.928). Dropping the interest rate costs another 0.043 AUC. If you prefer a
  model without the age proxies anyway, variant c is the one to pick: it loses 0.021 AUC compared
  with a.
- Changes: none beyond A3 (the options already existed). Outputs in `models_experiments/`.
- Problems: none. Variant a reproduces the phase 1 model exactly (same AUC and fairness), which
  confirms the split and seed are deterministic.

### B2. Train the chosen variant as the main model — ✅ Done
- Test: `python train_ebm.py --target-means default` saved `models/ebm-20261011T070748Z`
  (AUC 0.9413, accuracy 0.867, Brier 0.0937, 40 terms, no fairness warnings). `RiskScorer`
  loaded it through `latest_model_path()` (feature-list check passed) and scored the step 3 test
  applicant: probability 1.0 stored (displayed "above 99%"), baseline 0.7833 log-odds, top
  drivers home ownership × loan-to-income (+5.50), loan-to-income (+3.07), interest rate (+2.26).
  Baseline plus all contributions = 15.8353 log-odds, which matches the probability.
- Changes: none to `FEATURES` or the form, because variant a keeps all ten features.
- Decisions: the phase 1 model file stays in `models/` (not deleted). The dashboard loads the
  newest, which is the new one, and the new `.json` holds the A3 fairness format and the C1/C2
  graph data. Training into `models/` with `--drop` is refused, so the dashboard can never load a
  model whose features differ from `train_ebm.FEATURES`.
- Problems: none.

### Part C. Model performance and EBM graphs

### C1. Performance data saved at training time — ✅ Done
- Test: read back from the model's `.json`. ROC: 100 points (downsampled from the full curve),
  AUC 0.9413. Calibration: 10 bins, with every predicted average within 0.084 of the actual
  default rate (largest gap in the 0.7-0.8 bin: 0.751 predicted, 0.835 actual, 145 loans).
  Score distribution: 20 bins, summing to 2,429 repaid and 2,000 defaulted (= the 4,429 test
  rows). Confusion at 0.5: TN 2,252, FP 177, FN 412, TP 1,588 (79.4% of defaults caught, 7.3% of
  repaid loans flagged, precision 90.0%).
- Changes: `train_ebm.py` (`performance_data`).
- Decisions: calibration uses 10 equal-width bins of predicted probability (0-0.1 … 0.9-1.0).
  ROC thresholds are capped at 1.0 (sklearn's first threshold is infinity, which JSON can't hold).
- Problems: none.

### C2. EBM explanation data saved at training time — ✅ Done
- Test: the `.json` holds 10 single-feature terms (7 numeric with ≤100 points, 3 categorical)
  and 30 pair grids (≤30×30). Each numeric term has contribution, upper and lower bounds, and a
  density histogram. File size: about 300 KB.
- Changes: `train_ebm.py` (`explanation_data`, using `ebm.explain_global().data(i)`;
  `.visualize()` is never called).
- Decisions: the main model includes pairs (variant a), so pair grids are saved. Numeric shapes
  keep evenly spaced bin indices: EBM bins are quantile-based, so this keeps detail where the
  data is dense. **Numeric density comes from the training rows** (40 bins up to the 99th
  percentile, plus a count of the rows above it). The EBM's own density uses about 28 bins over
  the full range, and for income (up to 2.4 million) the visible part collapsed into a single
  bar. The 99th percentile (`view_max`) also sets the chart's initial zoom.
- Problems: the density issue above was found by looking at a rendered PNG. Fixing it required
  saving new data, so the main model was retrained with the same command:
  `models/ebm-20261011T071422Z`, identical metrics (AUC 0.9413, accuracy 0.867, Brier 0.0937) and
  fairness. It is now the newest model and the one the dashboard loads. The two older model
  files stay in `models/`.

### C3. Model page layout — ✅ Done
- Test: AppTest renders page 2 with no exceptions. Sections appear in order: Performance,
  Calibration, Score distribution, How the model sees each feature, Interactions, Global feature
  importance, Fairness report. All 10 feature shape functions and the first 5 pair heatmaps were
  selected and rendered. The waterfall, income and home-ownership shapes, and the top pair
  heatmap were exported to PNG and checked by eye.
- Changes: `app/pages/2_Model.py`, `app/shared.py` (`shape_chart`, `pair_heatmap`).
- Decisions: interactions use one selector with one heatmap, ordered by importance, rather than
  30 heatmaps stacked on the page. Numeric shapes are step lines (the EBM is piecewise constant)
  with the bounds as a shaded band. Categorical shapes are bars with error bars, red when they
  raise risk and blue when they lower it, each with a signed text label. The heatmap uses the
  palette's blue↔red diverging pair with a grey midpoint, centred on 0. Older models without
  graph data show a "retrain to see these graphs" note.
- Problems: Streamlit's AppTest applies `format_func` twice when a test sets a selectbox, so the
  page now uses label strings as the options. Rendered PNGs showed the waterfall labels colliding
  with the term names (fixed by padding the x-axis range) and "(bin start)" on a category axis
  (now added to numeric axes only).

### C4. Applicant explanation waterfall — ✅ Done
- Test: on a database copy, scored the high-risk and low-risk test applicants through page 3.
  Waterfall total = baseline + every stored contribution, exactly: high risk +15.8353 log-odds
  (stored probability 1.0), low risk −2.1391 log-odds → sigmoid 0.105354 (stored 0.1054). Pages
  3 and 4 render the waterfall with no exceptions.
- Changes: `app/shared.py` (`waterfall_rows`, `waterfall_chart`, `baseline_log_odds`; the
  application view shows the waterfall, with the old bar chart in an expander),
  `risk_scorer.py` (`RiskScorer.baseline_log_odds` attribute).
- Decisions: `risk_scores` stores contributions but not the baseline, so the view reads the
  baseline from the model file named by the stored `model_version` (no schema change). If that
  file is gone, the view says so and shows only the bar chart. Steps: the 10 largest terms by
  size, then "N other terms combined". The final bar is labelled with the log-odds and the A4
  probability format.
- Problems: none.

### Part D. Returning applicants

### D1. New or existing applicant on page 3 — ✅ Done
- Test: AppTest on a database copy. Scored "Test Applicant Low" as a new applicant, then chose
  *Existing applicant* and searched "applicant low": one match, labelled
  `#1: Test Applicant Low (created 2026-10-11, 1 earlier application(s))`. The form prefilled
  income 60,000 and credit score 700. Changed the income to 65,000 and entered a new loan
  (9,000, HOMEIMPROVEMENT, 10.5%), then submitted. Result: 2 applications, both
  `applicant_id = 1`, 1 applicant row. The audit log records
  `created application for existing applicant` with
  `profile_changes: {"annual_income": [60000.0, 65000.0]}`. Page 4 shows each application's
  other application with its status and officer decision (`none yet`).
- Changes: `app/pages/3_Score_an_applicant.py` (applicant choice, search, prefilled form,
  `score_application(form, applicant_id)`). `app/shared.py` (`other_applications` table in the
  application view; the stated income now comes from the application's own calculation inputs).
  `app/pages/4_Officer_review_queue.py` (the selector uses label strings).
- Decisions: the search is a case-insensitive name match showing up to 50 applicants. The
  applicant's profile fields (income, education, home ownership, experience, credit score,
  history, prior default) are prefilled from the latest profile. Loan fields (amount, intent,
  rate, term) start blank because they belong to the new loan. A returning applicant's row is
  updated to the values entered (so it holds the latest profile), and every change is written to
  the audit log as old → new. Each application keeps the values it was scored with in its
  calculations and audit entries, so the view reads stated income from there instead of the
  applicant row. Page 4 lists all of the applicant's other applications, marked *earlier* or
  *later*, since a later application can also matter to the officer.
- Problems: the AppTest `format_func` issue (see C3) also affected the page 4 selector. Fixed the
  same way.

### Part E. The LLM layer

### E0. Connection settings and test — ✅ Done
- Test: `GET /api/tags` → 200 in 0.2 s, 17 models including `qwen3.5:latest` and `qwen3.5:35b`.
  `POST /api/chat` to `LLM_MODEL` (`qwen3.5:35b`, thinking off, temperature 0, `num_ctx` 32768)
  → 200 in 10.3 s, reply `"connection ok"`. Auth is `Authorization: Bearer <key>` on Ollama's
  native API, as the spec assumed. The key was never printed (only its length was checked).
- Changes: `.env` (added `LLM_MODEL_FINAL=qwen3.5:35b`, `LLM_NUM_CTX=32768`,
  `LLM_TIMEOUT_SECONDS=120`; existing values untouched), `.env.example` (names only, plus
  `LLM_MODEL_ACTIVE` for E10).
- Decisions: your `LLM_BASE_URL` ends in `/api/generate`. The client strips any trailing
  `/api/...` path and calls `/api/tags` and `/api/chat` on the server root, so the value works
  as it is. Setting it to `https://gpu-01.insight.gsu.edu:11443` would be cleaner. **Your
  `LLM_MODEL` is `qwen3.5:35b`, the same as `LLM_MODEL_FINAL`.** I left it unchanged. See E10
  for how the comparison handles this.
- Problems: none.

### E1. `llm.py`: one small client — ✅ Done
- Test (database copy): plain reply ("Hello", 1.3 s); JSON-schema reply parsed as
  `{"city": "Paris", "country": "France"}`; a tool call with thinking on returned
  `get_application({"application_id": 42})`; an image call read the number from a small PNG.
  A 0.001 s timeout retried once and then raised `LLMError` with no key in the message. Five
  `audit_log` rows were written with actor `model` (model, purpose, duration, tool calls,
  image count, retry flag, error). Neither the key nor any base64 image data appears in them.
- Changes: new `llm.py` (`chat`, `available`, `list_models`, `active_model`, `settings`,
  `LLMError`). `requirements.txt` gains `requests`, `pypdfium2` and `fpdf2` (fpdf2 is for E3).
- Decisions: `chat()` takes messages, images (attached to the last user message), a JSON schema
  (Ollama `format`), tools and `think`. Every call sends `temperature: 0` and `num_ctx` from
  `.env`. The audit row goes into the caller's session when one is given (so it commits with the
  application's other rows), otherwise it is committed on its own. Failed calls are audited too.
  The model defaults to `LLM_MODEL_ACTIVE`, falling back to `LLM_MODEL`.
- Problems: the first version recorded `retried: false` after a retry, because the second
  attempt overwrote the flag. Fixed and retested.

### E2. Document storage — ✅ Done
- Test (database copy, uploads in a scratch folder): the tables were upgraded from the old
  columns to the new ones. Stored a 2-page PDF and a PNG with filename, MIME type (detected from
  the file signature), size, SHA-256 and page count. Rejected a text file ("Only PDF, PNG and JPG
  files are accepted"), an 11.5 MB file and a broken PDF. The PDF rendered to 2 PNG page images.
  `hash_matches()` was true, then false after the file was modified on disk. On a second copy
  with a row in `documents`, `get_engine()` refused to recreate the tables and the row was kept.
- Real `loans.db`: confirmed `documents` 0 rows and `extracted_fields` 0 rows, then opened it.
  Both tables now have the new columns. `historical_loans` still has 45,000 rows, and
  `applications` and `audit_log` are still empty.
- Changes: `db.py` (new columns on `Document` and `ExtractedField`; `_upgrade_document_tables()`
  drops and recreates only these two tables, only when both are empty, and raises an error
  otherwise). New `documents.py` (`save_document`, `page_images`, `hash_matches`, `read_bytes`).
- Decisions: the file type is checked from its first bytes, not the file name. Files are saved as
  `uploads/<application_id>/<doc_type>-<first 12 hex of sha256>.<ext>`, and the path is stored
  relative to the project. PDF pages are rendered at 144 dpi (scale 2). `UPLOADS_DIR` can
  redirect storage (used by tests so the real `uploads/` stays empty). Document types:
  `pay_stub` and `bank_statement`.
- Problems: none.

### E3. Sample documents for testing — ✅ Done
- Test: `python make_samples.py` wrote 40 PDFs (20 pay stubs, 20 two-page bank statements) and
  `samples/answer_key.json`. A text extraction of every page found the watermark
  "SAMPLE - NOT A REAL DOCUMENT" on all of them (0 pages missing it). It is printed three times
  per page: a red header, a grey diagonal across the page, and the footer. Planted mismatches:
  S01, S04, S08, S09, S17, with pay stub income at 1.23×, 1.31×, … of stated income (factors
  between 1.2 and 1.4 are in the key). Rendered pages were checked by eye. `git status` doesn't
  list `samples/`.
- Changes: new `make_samples.py`.
- Decisions: the 20 applicants are a seeded random sample (seed 42) of the cleaned rows, and the
  key holds each one's form values so E10 can run the full workflow. Pay periods are randomly
  weekly, biweekly, semimonthly or monthly, with gross pay = income × factor ÷ periods per year.
  Pay stubs also print net pay and deductions, so the reader has to pick gross. The bank
  statement puts balances on page 1 and transactions plus total deposits on page 2, which tests
  the page numbers. Deposits always match stated income (two payroll deposits of income ÷ 24),
  so in a planted case the pay stub disagrees with both the form and the bank statement. All
  names are fictional and tagged "(sample Sxx)".
- Problems: none.

### E4. Document Reader — ✅ Done
- Test (database copy, `qwen3.5:35b`, all 20 samples, 40 documents):

  | Run | Field accuracy | Page accuracy | Valid JSON | Annual income correct | s / document |
  |---|---|---|---|---|---|
  | 1: full pages | 145/200 = 72.5% | 150/200 = 75.0% | 40/40 | 20/20 | 3.6 |
  | 2: pages cropped to content | **200/200 = 100%** | **200/200 = 100%** | **40/40** | **20/20** | 3.4 |

- Changes: new `document_reader.py` (prompts, one JSON schema per document type, `validate()`
  for step 3, `read_document()` saves `extracted_fields`). `documents.py` (`crop_to_content`).
- Decisions: each field comes back as `{value, page, confidence}`. Thinking is off. The prompt
  says to extract only what is printed, use null when a value is missing, and never calculate.
  The step 3 checks in code: valid JSON, the expected shape, types, pay period in the allowed
  list, amounts positive and plausible, dates in ISO format within the last 18 months (up to 31
  days ahead), page numbers within the document, and a statement period of 1 to 62 days.
  Annual income = gross pay × periods per year (52/26/24/12), computed by code and saved as its
  own `annual_income` field (`extraction_model = "code"`). Any failed check or null required
  field sets `needs_review` on that field. The model's confidence is stored but never used to
  clear a field. `annual_income` becomes `documented_income` for `check_rules()`, so
  `INCOME_MISMATCH` now uses the pay stub.
- Problems: in run 1, 18 of 20 bank statements came back with null or misread balances (e.g.
  2,792.44 read as 279244), while the single-page pay stubs were perfect. An experiment on 3
  statements showed the cause. The server appears to downscale large images (each A4 page cost
  about 1,100 prompt tokens), so on mostly blank pages the digits became unreadable. Cropping
  each page to its printed content (ignoring the light watermark) fixed all 3, and run 2 got
  every field right. Caveat: the samples are clean generated PDFs, so real scans will do worse.
  That is why `needs_review` and officer verification exist.

### E5. Pattern review — ✅ Done
- Test: unit checks of the evidence filter. Kept a valid finding and one whose numbers appear in
  the data. Discarded findings with no evidence, an unknown document (`w2`), an unknown field
  (`net_pay`), or a number not in the data ("23% higher"). Then ran pattern review on the
  extracted samples (database copy). The run was stopped after 16 of 20, at your request
  ("one is enough").

  | Result (16 applications, `qwen3.5:35b`, thinking on) | |
  |---|---|
  | Findings kept / discarded by the evidence check | 3 / 0 |
  | Planted mismatches with a finding | 2 of 4 reached (S04, S09; not S01, S08) |
  | Clean applications with a finding | 1 of 12 (S16, false positive) |
  | Seconds per application | 10.8 to 62.2 (about 38 on average) |

  All saved flags have `source = "pattern"` and `severity = "warning"`, with evidence holding the
  model's description plus each cited field and its stored value, looked up by code.
- Changes: new `pattern_review.py`.
- Decisions: findings cite `{document, field}` pairs, where the document is `pay_stub`,
  `bank_statement` or `form`. Codes are normalized to UPPER_SNAKE_CASE. Following rule 10, a
  finding whose description states a number not found in the cited data is discarded (the
  prompt says to name fields and leave the arithmetic to code). Pattern review is a second
  opinion: the planted income mismatches are caught deterministically by the `INCOME_MISMATCH`
  rule, which uses the pay stub income from E4.
- Problems: S16's finding said the closing balance didn't equal the opening balance plus
  deposits, but withdrawals exist and are not extracted. The prompt now says this is normal.
  Not re-run (single-sample testing from here on).

### E6. Decision Coordinator — ✅ Done
- Test (one sample, S04 with a planted mismatch, database copy): the full workflow ran in 65 s
  (document reader 7 s, pattern review 41 s, Coordinator plus validation 15 s). The Coordinator
  called the five tools once each (5 of 8 allowed) and answered **deny**, confidence 0.95,
  reason codes `INCOME_MISMATCH, PAYMENT_TO_INCOME_HIGH, LOAN_TO_INCOME_HIGH,
  DEPOSITS_BELOW_PAY_STUB_INCOME, MODEL_DRIVER:person_home_ownership & loan_percent_income`. Its
  explanation cites the stated income ($36,575), documented income ($47,803.56), mismatch 0.307
  against the 15% limit, probability 0.9999, the top two contributions, and both ratio flags,
  every number taken from a tool result. Guard tests: an unknown tool, another application's id,
  and an extra argument each returned an error message instead of running. The application
  stayed `in_review` (tools never change status).
- Changes: new `coordinator.py` (5 tools, `run_tool` guard, `coordinate` loop), new
  `workflow.py` (steps 1-9). `db.py`: `recommendations.explanation` column, added with
  `ALTER TABLE ADD COLUMN`, which keeps existing rows (the real table has 0 rows).
- Decisions: each tool call writes an audit row with actor `model`, matching the sequence diagram
  and F3. Tools read stored results. If calculations, rules or the score are missing, the tool
  computes them with the same function and saves them. The intake audit row now stores the full
  form, so tools rebuild exactly what was scored. When the model stops calling tools, one more
  call (no tools, JSON schema) gets the final answer. At the 8-call limit the loop stops, that
  final call is still made so the officer sees a recommendation, and the application is marked
  `TOOL_CALL_LIMIT_REACHED`. The Coordinator's text goes into the new `explanation` column (the
  table had no field for it).
- Problems: none.

### E7. Output validation — ✅ Done
- Test: offline checks. A valid refer passed. An unknown reason code, an explanation stating 55%
  (not in any tool result), approve with a critical flag or p ≥ 0.5, deny with no flags and
  p = 0.1, and a malformed answer each failed with the matching reason. The number check accepts
  rounding and percent forms (0.1975 written as 19.75% or 0.20, −0.9012 as "lowers by 0.90",
  "36 months" from an assumption string) and rejects 91% and 800. In the S04 run, validation
  passed.
- Changes: new `validation.py` (`unsupported_numbers`, `validate_recommendation`,
  `GENERAL_REASONS`, `RECOMMENDATION_SCHEMA`).
- Decisions: the allowed numbers are every number in this run's tool results, including numbers
  inside strings such as dates and assumptions, sign-insensitive, matched at the precision
  written. "above 99%" and "below 1%" are allowed (A4 display). Counts written as numerals are
  not allowed unless a tool returned them; the prompt asks the model not to invent numbers. The
  general reason list moved here and gained `LOW_MODEL_RISK` and `NO_RULE_FLAGS`, so an approve
  can cite something.
- Problems: none.

### E8. Review Gatekeeper — ✅ Done
- Test: offline, every trigger together gave `CRITICAL_FLAG:…, VALIDATION_FAILED: …,
  EXTRACTION_NEEDS_REVIEW, LOW_CONFIDENCE:0.50, TOOL_CALL_LIMIT_REACHED`; a clean case gave no
  reasons. S04 was marked `CRITICAL_FLAG:INCOME_MISMATCH`. LLM turned off: the application was
  still saved (`in_review`, `refer`, 3 calculations, 1 score, 2 documents) and marked
  `LLM_UNAVAILABLE, EXTRACTION_NEEDS_REVIEW`. A missing bank statement stopped the application at
  step 1 as `stopped_incomplete` with `document: bank_statement`.
- Changes: `validation.py` (`gatekeeper`), `workflow.py`.
- Decisions: every application goes to `in_review`. Validation failures are copied in full into
  `review_reasons`. If the LLM is unreachable or fails mid-run, the recommendation is `refer` with
  the phase 1 reason codes. A required form field missing raises an error and saves nothing (the
  form already blocks it). Missing or rejected documents save the application as
  `stopped_incomplete` with the missing items, as the workflow diagram shows.
- Problems: none.

### E9. Dashboard integration — ✅ Done
- Test (AppTest, database copy holding the S04 workflow run from E6): page 3 shows both
  uploaders and the line "LLM steps use qwen3.5:35b on the school server". The result view shows
  Documents and extracted fields (2 view/download buttons, hash check), Calculations, Flags
  (rule + pattern), Model risk score, and Recommendation ("🔴 DENY · confidence 0.95 ·
  validation: ✅ passed"). On page 4 the officer sees the Coordinator's explanation and the review
  reasons, then confirmed `gross_pay`. The field was saved with `verified_by_human = True` and an
  audit row `human:Officer Test · confirmed extracted field`. All six pages render with no
  exceptions. The live progress display was exercised by the E6 workflow run (same callback); a
  file upload through AppTest isn't possible (AppTest has no file-upload support).
- Changes: `app/pages/3_Score_an_applicant.py` (uploaders, `workflow.run_application`, progress
  per step in an `st.status` box; the manual "documented income" field is gone, since documents
  replace it). `app/shared.py` (`show_documents`, `show_recommendation`, pattern flags with their
  cited evidence, `llm_status`, a stopped-incomplete notice). `app/pages/4_Officer_review_queue.py`
  (verify-field form; reason codes come from `validation.GENERAL_REASONS`).
- Decisions: "view link" is a View / download button that serves the stored file only if its
  SHA-256 still matches. A changed or missing file shows 🛑 and no button. Confirming or
  correcting a field requires the officer's name and records old → new in the audit log.
  Corrections don't re-run the rules or the Coordinator; the page says so. If the server is
  down, page 3 says so and runs the workflow with the LLM steps skipped.
- Problems: the verification message read an attribute after its session closed
  (`DetachedInstanceError`); fixed. Because the audit log is append-only, the failed attempt's
  row stayed on the test copy, which is correct behaviour.

### E10. Evaluation — ⚠️ Done with issues (one sample instead of twenty)
- Test: the full workflow (steps 1-9) on a database copy, once per model, on **one sample (S09,
  a planted mismatch)**. You asked mid-build to test one document instead of all of them. Your
  `.env` sets `LLM_MODEL` and `LLM_MODEL_FINAL` both to `qwen3.5:35b`, so comparing them would
  compare a model with itself. The comparison therefore used the spec's development model,
  `qwen3.5:latest`, against `qwen3.5:35b`.

  | Model | Extraction field accuracy | Valid JSON | Planted mismatches caught | False mismatch flags | Validation pass | Seconds per application |
  |---|---|---|---|---|---|---|
  | `qwen3.5:latest` | 10/10 (100%) | 2/2 | 1/1 | 0/0 (no clean sample run) | 1/1 (100%) | 101.3 |
  | `qwen3.5:35b` | 10/10 (100%) | 2/2 | 1/1 | 0/0 (no clean sample run) | 0/1 (0%) | 60.8 |

  Both recommended deny (confidence 0.95) and were marked `CRITICAL_FLAG:INCOME_MISMATCH`.
  `qwen3.5:35b` failed validation by citing the reason code `FLAG:INCOME_MISMATCH`, which is not
  a real code. The check caught it, the recommendation stayed visible, and the application was
  marked `VALIDATION_FAILED`. `qwen3.5:latest`'s pattern review also produced
  `FORM_INCOME_MISMATCH_PAY_STUB`.
  For context, the E4 run with `qwen3.5:35b` scored 200/200 on extraction across all 20 samples.
- **Decision (E10 rule): `LLM_MODEL_ACTIVE=qwen3.5:latest`.** The smaller model is within 5
  points on extraction (100% vs 100%), catches as many planted mismatches (1 vs 1), and its
  validation pass rate is not lower (100% vs 0%; "within 5 points" was read as "not more than 5
  points worse"). Set in `.env` and named in `.env.example`.
- Caveats: one sample per model can't separate the models. The validation result for each is a
  single coin flip, and the false-flag column is untested. `qwen3.5:latest` was slower here,
  probably because the server had to load it first (the 35b model was already warm from earlier
  runs). Rerun on all 20 with
  `eval_e10.py "qwen3.5:latest,qwen3.5:35b" S01,…,S20` style before relying on this choice. To
  switch, change `LLM_MODEL_ACTIVE`.
- Changes: `.env` (`LLM_MODEL_ACTIVE`). The evaluation script lives in the scratch test folder,
  like the other tests.
- Problems: none in the code.

### Part F. Dashboard additions

### F1. Recommendations in the officer review queue — ✅ Done
- Test (AppTest, database copy with three applications in review: S04 from the LLM workflow, S02
  run without the LLM, and one added the phase 1 way with no score):

  | Applicant | Recommendation | Default probability | Critical flags | Validation | Confidence | Marked for review |
  |---|---|---|---|---|---|---|
  | Morgan Tanaka (sample S04) | 🔴 deny | above 99% | 1 | ✅ passed | 0.95 | 🚩 CRITICAL_FLAG:INCOME_MISMATCH |
  | Jordan Novak (sample S02) | 🟡 refer | 33.9% | 0 | not run | | 🚩 LLM_UNAVAILABLE; EXTRACTION_NEEDS_REVIEW |
  | Phase One Applicant | 🟡 refer | | 0 | not run | | |

  Filtering by "🔴 deny" left only S04, and "Not marked" left only the phase 1 application.
  Marked cases come first, then the oldest.
- Changes: `app/pages/4_Officer_review_queue.py`.
- Decisions: each recommendation is a coloured dot plus its word (🟢 approve, 🔴 deny,
  🟡 refer), so colour is never the only cue. Applications without an LLM recommendation show
  "not run" for validation and a blank confidence. The filters also limit the application
  selector below the table.
- Problems: the test reused a widget handle after the page re-rendered (an AppTest artefact);
  the test now starts a fresh page run instead.

### F2. Borrower insights (page 1, new tab) — ✅ Done
- Test (AppTest): the tab renders with the cleaned rows (22,142) and with all rows (45,000), with
  each of the five ordered-group variables selected. Each state draws 14 charts with no
  exceptions. The sunburst, the credit-score line and the rule-limit chart were exported to PNG
  and checked by eye. Rule limits on the cleaned rows:

  | Rule limit | Share crossing | Default rate crossing | Everyone else |
  |---|---|---|---|
  | Payment-to-income ≥ 0.10 (36-month term) | 18.9% (4,183) | 88.4% | 35.1% |
  | Loan-to-income ≥ 0.25 | 19.4% (4,296) | 88.2% | 34.8% |
  | Credit score < 550 | 4.2% (929) | 77.0% | 43.8% |

  With all rows, a fourth bar appears: previous default on file, 50.8% of borrowers, 0.0% default
  rate. That is the data artifact, visible.
- Changes: `app/pages/1_Portfolio_overview.py` (Overview and Borrower insights tabs).
- Decisions: the sunburst's outer ring uses the page's existing outcome colours (blue = repaid,
  orange = defaulted). The intent ring is neutral grey with text labels, so no colour means two
  things on the page. Median loan amount and median loan-to-income are two separate bar charts
  (different scales, no dual axis). Ordered groups: credit score bands of 50 points (the 550 rule
  limit is a band edge), income deciles, loan-to-income bands of 0.05 up to 0.30, the fairness
  report's age bands, and employment bands. Groups under 100 loans are grey, labelled "small
  sample", and left off the line. The protected-attribute note appears above the age line chart
  and above the gender bar chart. Payment-to-income in the rule chart uses the calculator's
  formula with a 36-month term.
- Problems: the small-sample label was clipped at the chart edge, and the share crossing was
  only in the table. Both fixed.

### F3. Assistant chat panel — ✅ Done
- Test (AppTest on a database copy, model `qwen3.5:latest`, the E10 choice). All six pages
  render with the panel. Its caption reads "Model qwen3.5:latest. Answers questions about the
  data and applications. It can't make or change decisions."

  | Question | Where | Tool calls | Answer (summary) | Number check |
  |---|---|---|---|---|
  | "What is the default rate by home ownership?" | page 1, no context | `portfolio_stats` | A table: MORTGAGE 7,827 / 27.39%, OWN 1,156 / 19.20%, RENT 13,089 / 58.03%, OTHER 70 / 55.71%, with "OTHER" noted as a small sample | passed |
  | "Why is this one marked for review?" | page 4, application #1 (S04) | `get_recommendation` | The critical `INCOME_MISMATCH` (stated $36,575 vs documented $47,803.56, value 0.307, 15% limit), probability 0.9999, both ratio flags; "the final decision rests with you on the review page" | passed |
  | "Please approve this loan." | page 4, application #1 | `get_recommendation` | "I cannot approve or deny this application — that's your responsibility on the review page", then explained why the case is marked | passed |

  Nine `audit_log` rows were written (LLM calls and tool calls, actor `model`, purpose
  `assistant`). None contains the question or answer text. Application #1 is still `in_review`.
- Changes: new `assistant.py` (6 read-only tools, `ask()`, number check). `app/shared.py`
  (`page_layout()`, `chat_panel()`, bubble CSS). Every page calls `page_layout()`. Pages 3 and 4
  store the application on screen as the chat context.
- Decisions: the panel is drawn first in the right column (¼ of the width), so it still appears
  when a page stops early. The page content goes into the left column with a single
  `main.__enter__()` call, so existing page code didn't need re-indenting. On narrow screens
  Streamlit stacks the columns, putting the panel below the content. Bubbles: the user's are
  right-aligned in a translucent blue, the assistant's left-aligned in translucent grey, readable
  in light and dark themes. The CSS matches Streamlit 1.65's `stChatMessageAvatarUser` test id
  (confirmed in the installed frontend bundle). `portfolio_stats` uses a fixed list of 10
  variables computed by code, never model-written SQL; gender and age results carry the
  "never used to decide" note. With an application in context, the tools ignore any id the model
  passes. History is kept in `st.session_state` (last 10 turns sent). The number check is
  `validation.unsupported_numbers`, and a failed answer is shown with a warning label. If the
  server is down, the panel shows "Assistant unavailable" and the page works as usual.
- Problems: none blocking. Two wording slips in answer 2, though every number was correct: it
  said "exceeding the 15% tolerance limit by 0.307" (0.307 is the gap itself, not the excess)
  and called the Coordinator's recommendation "the model". The number check confirms numbers
  come from tools; it cannot catch a wrong description of them. Not verified by eye in a
  browser: the bubble alignment and the narrow-screen stacking.

### Part G. Documentation

### G1. `README.md` for GitHub — ✅ Done
- Test: checked by reading it against the code. All 12 sections are present in the order the spec
  lists, with both Mermaid diagrams in ` ```mermaid ` blocks, the tool tables (Coordinator and
  assistant), the tool design rules, the 11 tables, the data analysis table, the model variants
  and metrics, the evaluation tables, the rules with their sources, setup, and limitations. Every
  figure was copied from this log. The Mermaid diagrams were not rendered here (GitHub renders
  them on push).
- Changes: `README.md` rewritten as public documentation.
- Decisions: both diagrams were updated to match the code. The flowchart gained the two dotted
  "LLM unavailable" paths (step 1 → step 4 and step 6 → step 9). The sequence diagram shows the
  extra final-answer call (no tools, JSON schema) after the model stops calling tools. Screenshots
  are a placeholder table of file names under `docs/`.
- Problems: none.

### G2. Final build log — ✅ Done (this section)

## Phase 2 summary

| Step | Status | Note |
|---|---|---|
| A1 INCOME_MISSING rule | ✅ Done | |
| A2 Audit log triggers | ✅ Done | |
| A3 Fairness vs largest group | ✅ Done | |
| A4 Probability display | ✅ Done | |
| B1 Four model variants | ✅ Done | Rule chose variant a |
| B2 Chosen variant as main model | ✅ Done | Retrained once more in C2 (same metrics) |
| C1 Performance data | ✅ Done | |
| C2 EBM explanation data | ✅ Done | Density from training rows |
| C3 Model page layout | ✅ Done | |
| C4 Applicant waterfall | ✅ Done | |
| D1 Returning applicants | ✅ Done | |
| E0 Connection test | ✅ Done | |
| E1 `llm.py` | ✅ Done | |
| E2 Document storage | ✅ Done | Real tables upgraded (both had 0 rows) |
| E3 Sample documents | ✅ Done | |
| E4 Document Reader | ✅ Done | 100% after cropping fix |
| E5 Pattern review | ✅ Done | Stopped at 16 of 20 samples at your request |
| E6 Decision Coordinator | ✅ Done | Tested on one sample |
| E7 Output validation | ✅ Done | |
| E8 Review Gatekeeper | ✅ Done | |
| E9 Dashboard integration | ✅ Done | |
| E10 Evaluation | ⚠️ Done with issues | One sample per model; `qwen3.5:latest` vs `qwen3.5:35b` |
| F1 Recommendations in the queue | ✅ Done | |
| F2 Borrower insights | ✅ Done | |
| F3 Assistant chat panel | ✅ Done | Bubble layout not checked in a browser |
| G1 README | ✅ Done | |
| G2 Final build log | ✅ Done | |

**Decisions made by the written rules**

- **B1 → variant a** (default EBM, 40 terms). Best AUC 0.9413; b 0.9202, c 0.9204 and d 0.8769
  are all more than 0.01 below it. All four variants pass the fairness condition (lowest ratios
  0.939 / 0.918 / 0.928 / 0.946). Variant c (no age proxies) is the alternative if you prefer it:
  it costs 0.021 AUC.
- **E10 → `LLM_MODEL_ACTIVE=qwen3.5:latest`**. On sample S09, extraction was 10/10 for both
  models, both caught the planted mismatch, and validation passed 1/1 for `qwen3.5:latest`
  against 0/1 for `qwen3.5:35b` (an invented reason code). Based on a single sample.

**State at the end:** the real `loans.db` has `historical_loans` with 45,000 rows and 0 rows in
every other table (all tests used copies). `uploads/` is empty. The dashboard loads
`models/ebm-20261011T071422Z`.

## Waiting for the user

1. **Confirm the LLM model choice with a larger evaluation.** E10 used one sample per model, so
   rerun the evaluation on all 20 samples before relying on `qwen3.5:latest`. Your `.env` also
   has `LLM_MODEL=qwen3.5:35b`, the same as `LLM_MODEL_FINAL`; set it to the development model
   you intend.
2. **Credit score minimum: 550 or the stricter 600?** (Still open from phase 1.)
   `RULES["min_credit_score"]` in `calculations.py`.
3. **Age proxies:** keep variant a, or switch the main model to variant c (no work experience or
   credit history length, AUC 0.9204)? Changing it requires editing `train_ebm.FEATURES` and
   retraining.
4. **`LLM_BASE_URL`** ends in `/api/generate`. It works (the client strips the path), but
   `https://gpu-01.insight.gsu.edu:11443` would be cleaner.
5. **Screenshots:** add the six images listed in the README under `docs/`.
6. **A look in the browser:** the chat bubble alignment, the narrow-screen layout, and label
   overlap on the charts were checked by automated tests and PNG exports, not by eye in the
   running dashboard.
7. **Field corrections don't re-run the rules or the Coordinator.** If an officer corrects the
   pay stub's gross pay, `INCOME_MISMATCH` is not recomputed (the page says so). Re-running the
   workflow after a correction could be a future step.
8. **Housekeeping (optional):** `models/` holds two older model files
   (`ebm-20261010T143918Z`, `ebm-20261011T070748Z`) that can be deleted. `kaleido` was
   pip-installed into `.venv` only to export charts for visual checks; it isn't in
   `requirements.txt` and can be uninstalled.

## Phase 2b (from `CLAUDE3.md`)

Started 2026-10-11. Your decisions: model variant c, credit score minimum stays 550, Milestone 1
measurable outcome left blank. `CLAUDE3.md` was added to `.gitignore`, like `CLAUDE.md` and
`CLAUDE2.md`. Before any change, the real `loans.db` had 45,000 `historical_loans` rows and no
other rows. Tests use database copies, as before.

### 1. Switch the main model to variant c — ✅ Done
- Test: `python train_ebm.py --target-means default` (the plain command) trained
  `models/ebm-20261011T230951Z`: 8 features, 0 interactions, 8 terms, **AUC 0.9204, Brier 0.1108**
  (matches the B1 row for variant c), accuracy at 0.5 84.3%. The scorer loads it (feature check
  passes). The step 3 test applicant scores 0.9996, with top drivers loan-to-income +3.04,
  interest rate +2.28 and credit score +1.11, and baseline + contributions gives back the
  probability. Fairness: female 1.000, male 1.000, <25 0.928, 25-34 1.000, 35-49 1.021,
  50+ 1.022 (small sample); no warnings. Model page: 8 shape functions, all render, no
  Interactions section. Page 3 no longer has the two inputs. Older stored scores: the waterfall
  and the Coordinator's `score_risk` read the baseline from the old model's file (0.7833 for
  `ebm-20261011T071422Z`), and that model is refused for new scoring. `explain_feature` works for
  the 8 features and returns an error for `person_emp_exp`.
- Changes: `train_ebm.py` (`FEATURES` = 8, `DEFAULT_INTERACTIONS = 0` used by `fit`, `train` and
  the `--interactions` default). `risk_scorer.py` (`check_features=False` loads an older model
  only to read its baseline; scoring uses the model's own feature list). `app/shared.py`,
  `coordinator.py` (baselines from old model files), `workflow.py` (the two features are no longer
  required). `app/pages/3_Score_an_applicant.py` (inputs removed, caption explains why).
  `pattern_review.py` and `coordinator.stored_form` no longer pass them.
- Decisions: `applicants.employment_years` and `credit_history_years` are nullable, so **the two
  inputs were removed from the form** rather than kept as optional fields. New applicants store
  NULL there, and a returning applicant's earlier values are left as they are. All earlier model
  files stay in `models/`.
- README sections changed: Features (Model page bullet), The model (features, variant table with
  c chosen and why, metrics, calibration, fairness, age-proxy note), Setup (training command
  description), Limitations (age proxies).
- Problems: the first patch failed on its first check (a shell-quoting problem) after the
  training command had already been chained, so one extra variant-a model was trained. I deleted
  that file (`ebm-20261011T230834Z`), since nothing referenced it, and re-ran with the patch
  written as a file.

### 2. Interrupted runs — ✅ Done
- Behaviour before the change (database copy, a real process kill inside the risk score step,
  LLM steps skipped because the server was unreachable, see below): the application was left
  with `status = submitted` and `current_step = rule_check`. That was the last step saved, not
  where it stopped. 2 documents were on disk in `uploads/1/`, plus 3 calculations, 2 flags and
  5 audit rows (intake, completeness, LLM skipped, calculations, rule check), with no score and
  no recommendation. Nothing listed it: the queue shows only `in_review`, so the run was
  invisible and could not be recovered.
- Test after the change: two runs (S04, S02) were killed inside the risk score step, and both
  now show `current_step = risk_scoring`. Both appeared on page 4 under "Interrupted runs (2)"
  (`INTERRUPTED_AFTER_MINUTES=0` for the test). **Resume** on #1 by "Officer Resume" ran only
  the risk score and the gatekeeper, and the run ended `in_review` with 3 calculations, 2 flags,
  1 score and 1 recommendation. **Mark as stopped** on #2 by "Officer Stop" set
  `stopped_incomplete`, `current_step = interrupted`, and an audit row with
  `reason: INTERRUPTED`. In the audit log, no step appears twice in either application. Afterwards
  the Interrupted section is empty.
- Changes: `workflow.py` rewritten around `continue_application()`, which checks each step
  against the current run and runs only the missing ones, plus `start_application`, `resume`,
  `mark_stopped`, `interrupted_runs` and `interrupted_after`. `current_step` is committed as each
  step starts, and the Document Reader commits per document. `db.py`: run helpers on
  `Application` (`run_start`, `run_audit`, `current_calculations`, `current_flags`,
  `current_risk_score`, `current_recommendation`) and `Document.latest_fields()`.
  `coordinator.py` tools read the current run. `app/pages/4_Officer_review_queue.py`: Interrupted
  runs section. `app/shared.py`: the view reads the current run.
- Decisions: "hasn't changed for 15 minutes" is measured from the application's newest audit row
  (every step writes one), so no schema change was needed. The threshold can be changed with
  `INTERRUPTED_AFTER_MINUTES`. A step's results and audit rows are written in one transaction, so
  a step cut off mid-way leaves nothing and runs again in full; "no step ran twice" therefore
  holds for completed steps, which is what the audit log shows. LLM availability is checked once
  per run and recorded (`LLM steps skipped`), so a resumed run stays consistent with how it
  started.
- README sections changed: Features (Interrupted runs), Workflow (recovery table), new section
  "Run state and recovery".
- **Problem, blocking: the school server is unreachable.** From this point the LLM host
  `gpu-01.insight.gsu.edu` does not resolve (`NameResolutionError`; Comcast DNS answers
  "Non-existent domain"), while the internet and GitHub work. The machine is off the campus
  network or VPN. This is CLAUDE2.md stop condition 1. Following the working mode, every step
  that doesn't need the server continues; the ones that do are listed as they come up.

### 3. One retry on invalid model output — ✅ Done
- Test (mocked server responses at the HTTP layer, so the real `llm.chat` runs and writes audit
  rows; database copy):

  | Case | Calls | Result | Audit |
  |---|---|---|---|
  | Document Reader, fails once then passes | 2 | clean, annual income 47,803.56 | attempt 1; attempt 2 with `retry_reason` listing the three problems (pay period "every month", gross pay "about four thousand", date "30/09/2026") |
  | Document Reader, fails twice | 2 | `needs_review`, no income | attempts 1 and 2 |
  | Coordinator final answer, fails once then passes | 1 loop + 2 final | validation passed | retry reason `REASON_CODE: … FLAG:INCOME_MISMATCH` |
  | Coordinator final answer, fails twice | 1 loop + 2 final | `REASON_CODE` failure kept, so the case is marked `VALIDATION_FAILED` | |

- Changes: `document_reader.py` (`MAX_ATTEMPTS = 2`, `FIXABLE`, `fixable_problems`; the retry
  quotes the problems back to the model). `coordinator.py` (`coordinate(..., validator)` retries
  the final answer once on a fixable failure; `MAX_FINAL_ATTEMPTS = 2`). `validation.py`
  (`fixable_failures`: `SCHEMA` and `REASON_CODE`). `llm.py` (`attempt` and `retry_reason`
  recorded in every audit row; the connection-retry loop variable was renamed so it no longer
  shadows them). `workflow.py` saves `final_attempts` and `retry_reason`.
- Decisions: fixable problems are format and shape (invalid JSON, a missing field, a wrong type,
  a pay period outside the list, a non-ISO date, a page out of range). A null "not found" value,
  an implausible date or an amount ≤ 0 describes the document itself, so it goes straight to
  `needs_review` without a retry. For the Coordinator, only schema and reason-code failures are
  retried; number and consistency failures go to the officer as they are. Connection retries stay
  inside `llm.chat`, 1 per call.
- README sections changed: Workflow table (steps 2, 7, 8), tool design rules (retry limits).
- Problems: none.

### 4. Reason codes as an enum — ⚠️ Done with issues (server enforcement not tested)
- Implemented: `coordinator.answer_schema(app)` builds the final-answer schema per application,
  with `reason_codes.items.enum` = this application's current flag codes + `MODEL_DRIVER:<term>`
  for each term in its current score + `GENERAL_REASONS`. Shown in the Step 3 test: for the test
  application the enum held `PAYMENT_TO_INCOME_HIGH, LOAN_TO_INCOME_HIGH`, the 8 model-driver
  codes and the 8 general reasons. The step 8 check stays as a second safeguard.
- **Not tested: whether the server enforces the enum.** That needs the school server, which is
  unreachable (stop condition 1). The Step 3 retry covers a server that ignores the enum.
  Pending: one call with this schema, asking for an off-list code, then check the reply.
- Changes: `coordinator.py` (`allowed_reason_codes`, `answer_schema`).
- README sections changed: Workflow table (steps 7 and 8), tool design rules ("Reason codes are
  an enum").

### 5. Re-run checks after a correction — ✅ Done
- Test (AppTest on page 4, database copy holding the S04 workflow run from E6, which has a
  planted mismatch: stated income 36,575 vs pay stub 47,803.56). "Officer Fix" corrected
  `gross_pay` from 3983.63 to 3047.92 (= 36,575 ÷ 12) and the **Re-run checks** button appeared.
  The re-run added a new `annual_income` row (36,575.04; the old 47,803.56 row stays), new
  calculations, flags, a score and a recommendation. Current flags went from
  `PAYMENT_TO_INCOME_HIGH, LOAN_TO_INCOME_HIGH, INCOME_MISMATCH, DEPOSITS_BELOW_PAY_STUB_INCOME`
  to `PAYMENT_TO_INCOME_HIGH, LOAN_TO_INCOME_HIGH`. **The old `INCOME_MISMATCH` row (id 3) is
  still in `flags`.** The view says "Results recalculated after a correction by Officer Fix … flags
  removed: DEPOSITS_BELOW_PAY_STUB_INCOME, INCOME_MISMATCH; recommendation deny → refer", and the
  audit log has `re-run checks started` (human:Officer Fix) and `re-run checks finished` with the
  same changes.
- Changes: `workflow.rerun_checks()` writes the run boundary (with the previous flags and
  recommendation), sets the status to `submitted`, and calls `continue_application()`, which on a
  re-run skips the Document Reader and recomputes annual income (`_revalidate`). The view, the
  tools and the queue read only the current run (Step 2 helpers). `app/pages/4_Officer_review_queue.py`
  shows the Re-run checks form once a field has been verified.
- Decisions: a re-run is a new "run" marked by an audit row; every table keeps its old rows, and
  "current" means created after the latest boundary. While a re-run is in progress the status is
  `submitted`, so an interrupted re-run shows under Interrupted runs and can be resumed. Fields
  the officer verified count as reviewed for the gatekeeper. Re-run requires the status
  `in_review`.
- Caveat: with the server unreachable, the re-run skipped pattern review and the Coordinator, so
  the new recommendation is `refer` with `LLM_UNAVAILABLE`. The `DEPOSITS_BELOW_PAY_STUB_INCOME`
  pattern flag disappeared because pattern review didn't run, not because the correction cleared
  it. With the server, both LLM steps run again on the corrected values.
- README sections changed: Features (officer review queue, Re-run checks), Workflow table
  (step 10).

### 6. Evaluation script in the repository — ⚠️ Done with issues (two LLM cases not run)
- Test: `python eval/run_cases.py` (database copy and temporary uploads, both removed afterwards)
  wrote `eval/results/2026-10-11/cases.md` and `cases.json`:

  | Case | Requirement | Result |
  |---|---|---|
  | Clean sample (S06), then a scripted officer approval | 1. Representative successful case | **not run**: LLM server unreachable |
  | Planted mismatch (S01) | 2. Different input condition / branch | **not run**: LLM server unreachable |
  | Text file renamed to `.pdf` | 3. Invalid or failing case | pass: `stopped_incomplete`, "Only PDF, PNG and JPG files are accepted." |
  | Missing bank statement | Extra: failing case | pass: `stopped_incomplete`, missing `document: bank_statement` |
  | LLM turned off | Extra: fallback path | pass: `in_review`, `refer`, probability 0.6342, `LLM_UNAVAILABLE, EXTRACTION_NEEDS_REVIEW` |
  | Interrupted then resumed | Extra: recovery path | pass: stopped at `risk_scoring`, resumed to `in_review`, no step run twice |

  Each case records input, expected, actual, evidence (application id, status, flags,
  recommendation, validation, review reasons, audit rows), seconds per step, and a draft
  interpretation with a next action. The Milestone 1 outcome was left blank, so the report
  includes the default measures: seconds per application (0.02 without the LLM), extraction
  accuracy and planted mismatches caught (both "not run").
  The real `loans.db` still has 0 applications after the run, and `uploads/` is empty.
- Changes: new `eval/run_cases.py` (cases, `--compare MODEL_A,MODEL_B --samples all|ids`, the E10
  rule in `decide()`, Markdown/JSON writers) and `eval/README.md`. `make_samples.py` now fixes
  the PDF creation date: two runs gave 41 of 41 files byte-identical, where before fpdf's "now"
  timestamp changed every PDF. `samples/` was regenerated, and the answer key's SHA-256 is
  unchanged (`b12c6c48…`), so the values are identical.
- Decisions: case 1 uses the clean sample with no rule flags and the lowest default probability
  (S06), so approve or refer is a fair expectation. The interruption in case 6 is an exception
  raised inside the risk score step; Step 2 recorded a real process kill. Results contain only
  fictional sample data, so `eval/results/` is committed. When the server is unreachable, the LLM
  cases are written as "not run" instead of failing the whole script.
- README sections changed: new "Evaluation" section (test case table with the three required
  cases first, Document Reader accuracy, the provisional model comparison, and the "Test data"
  subsection read from `make_samples.py`). It replaces the old "LLM evaluation" section.
- Note on the real `loans.db`: it holds exactly one `audit_log` row (id 1, an `assistant` LLM call
  from 2026-10-11 08:04:58 UTC with no application). It was written while the live dashboard was
  running for you before this phase, when a question was asked in the chat panel, so it is real
  use and not a test row (and it can't be deleted: the log is append-only).
- Pending (needs the server): cases 1 and 2.

### 7. Compare the two models on all 20 samples — ❌ Blocked (stop condition 1)
- Not run: the school server was still unreachable (rechecked, `llm.available()` false). The
  command is ready: `python eval/run_cases.py --compare qwen3.5:latest,qwen3.5:35b --samples all`.
  It reports per model: extraction field accuracy, valid JSON rate, planted mismatches caught
  (out of 5), false mismatch flags (out of 15), pattern-review findings on planted and clean
  samples, validation pass rate, retries used and average seconds, and it applies the E10 rule.
- `LLM_MODEL_ACTIVE` stays `qwen3.5:latest` (the one-sample E10 choice) until this runs.
- README: the "Model comparison" subsection is labelled provisional and still shows the
  one-sample table.

### 8. Diagrams — ✅ Done
- Test: every ```mermaid block in `README.md` was parsed and rendered with Mermaid 10.9.1 in
  headless Chrome (`mermaid.parse` and `mermaid.render`): workflow OK, sequence OK, components
  OK. The checker was confirmed on a deliberately broken diagram, which it reported as a parse
  error. The workflow and component diagrams were also screenshotted and read by eye: labels are
  legible and the layout is tall but clear. Every function named in the branch table exists in
  the code.
- Changes (README only): the workflow diagram now shows the trigger, each step's actor (code,
  LLM, ML, human) and function, the exact condition on every branch, the validation-failure path,
  the retries and their limits (document reader 1, connection 1, final answer 1, 8 tool calls),
  interrupted runs with Resume and Mark as stopped, Re-run checks, completion, and the hand-off to
  the officer. The sequence diagram adds the invalid-output retry (an `alt` block) and the enum
  schema. The new component diagram covers the dashboard pages, workflow modules, LLM client,
  school server, model files, SQLite tables, file storage and `eval/`, with simulated parts
  (Kaggle data, generated samples) dashed orange, planned parts (credit bureau, real scans) dotted
  grey, and a legend. Under the workflow diagram: "How the workflow is controlled" and a 17-row
  branch-conditions table.
- Decisions: no Node.js was installed (stop condition 5), so the parse check uses the installed
  Chrome with Mermaid loaded from jsDelivr instead of `@mermaid-js/mermaid-cli`. The diagram
  sources live in one scratch file that both the README patch and the checker read, so the check
  covered exactly what shipped; the final check ran on the README itself.
- README sections changed: Workflow (diagram, control paragraph, branch table), How the LLM uses
  tools (sequence diagram), Architecture (component diagram and legend note).
- Problems: none.

### 9. Report material in the README — ✅ Done (one record provisional)
- Test: read back against the code and this log. Added "Step contracts" (12 rows: steps 1 to 10
  with 5b and 5c, naming the actual fields, tables and audit steps), "Design decision records" (6
  records), "Next steps", "AI-use disclosure (draft)" and the placeholder "Decisions to revisit
  from Milestone 1". The decision records' numbers come from B1 and Step 1 (variant c vs a),
  E4 (cropping: 145/200 vs 200/200 on the same 40 documents), E10 (model choice), the dataset
  analysis (22,858 prior defaulters all repaid; 22,142 cleaned rows at 45.2%) and E6/E7 (number
  check). Each measured comparison names the rule it was judged by.
- Changes: `README.md` only.
- Decisions: the model-choice record uses the **one-sample** E10 numbers, marked as such, because
  Step 7 could not run; it should be replaced with the 20-sample results. I added a sixth record
  (fixed workflow versus an agent choosing steps), since the spec asks for at least five and
  Step 8's control paragraph makes the same decision. The AI-use disclosure is labelled a draft,
  with a prompt for the team's own checks.
- README sections changed: new sections Step contracts, Design decision records, Next steps,
  AI-use disclosure (draft), Decisions to revisit from Milestone 1.
- Problems: my first draft said the S04 explanation had "8 figures"; recounting the E6 entry
  found 11, which is now corrected.
