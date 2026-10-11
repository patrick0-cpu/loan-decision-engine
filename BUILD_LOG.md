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
