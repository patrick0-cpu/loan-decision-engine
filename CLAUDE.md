# Loan Decision Expert System

## Project summary

A loan decision support system. It scores applicants with an Explainable Boosting Machine (EBM),
runs financial calculations and rule checks in plain code, stores everything in a serverless
SQLite database, and shows results in a Streamlit dashboard. A human loan officer always makes the final decision.
The system never approves or denies a loan on its own.

This phase covers the dataset, the database, the calculations, the EBM, and the dashboard.
The LLM parts (Document Reader, pattern review, Decision Coordinator) come later and are out of scope.

The project folder starts with this file, `.env`, `load_data.py`, and `loans.db`, which the user
has already filled by running `python load_data.py`. Claude Code creates everything else by
following the build order below.

## Rules for Claude Code

1. **Never use `person_gender` or `person_age` as model features.** They are protected
   characteristics. Use them only in the fairness report.
2. **`loan_status = 1` means default.** This was confirmed by the user's dataset analysis (see
   "Dataset analysis results"). Still run the target check in build step 6 and show the output.
3. **Never add automatic approval or denial.** The dashboard may show a recommendation, but only
   a loan officer's action on the review page records a final decision.
4. **Keep calculations in plain code.** Do not move arithmetic into an LLM.
5. **Thresholds in `RULES` come from the user's dataset analysis** (see the comments in step 3).
   Never change them without the user's approval.
6. **Write every officer decision to `officer_reviews` and every action to `audit_log`.**
   Never update or delete audit log rows.
7. **The database is SQLite, read from `DATABASE_URL` in `.env`.** Never hardcode the URL, never
   commit `.env`, and do not add a database server, Docker, or PostgreSQL (see "Database").
8. **The database is the only source of data.** Read the dataset from the `historical_loans`
   table, never from a CSV. Do not download the dataset or modify `load_data.py`. If the table is
   missing or empty, stop and ask the user to run `python load_data.py`. Never commit `loans.db`
   or the dataset to Git.
9. Build one step at a time. After each step, run the step's test and show the result before
   moving on.

## Dataset

Kaggle dataset `udaymalviya/bank-loan-data`: 45,000 rows, 14 columns. Synthetic.

**How the data gets in:** the user runs `python load_data.py` once. It downloads the dataset
with kagglehub (or takes `--csv path` for a local file) and loads all 45,000 rows into the
`historical_loans` table of `loans.db`. Running it again replaces that table only. All code
reads the data from `historical_loans`. Neither the CSV nor `loans.db` is stored in Git: each
person who clones the project runs `load_data.py` themselves.

`historical_loans` keeps all rows, including the ones excluded from training (see below).
Filtering happens in the code that reads the table.

| Group | Columns |
|---|---|
| Demographics | `person_age`, `person_gender`, `person_education`, `person_income`, `person_emp_exp`, `person_home_ownership` |
| Loan | `loan_amnt`, `loan_intent`, `loan_int_rate`, `loan_percent_income` |
| Credit | `cb_person_cred_hist_length`, `credit_score`, `previous_loan_defaults_on_file` |
| Target | `loan_status` (1 = default) |

### Dataset analysis results

The user analyzed the dataset before the build. Findings:

- **Data artifact:** all 22,858 applicants with `previous_loan_defaults_on_file = "Yes"` have
  `loan_status = 0`. This comes from how the synthetic data was generated, not from real borrower
  behavior. If kept, the model would learn that a previous default lowers risk.
- **Decision:** use only the 22,142 rows where `previous_loan_defaults_on_file = "No"` for training
  and analysis, and remove that column from the model's features. Prior defaulters are handled by
  the `PREVIOUS_DEFAULT_ON_FILE` rule, which sends them to the loan officer with a critical flag.
- **Target meaning:** on the cleaned rows, default rates rise with lower credit scores, higher
  loan-to-income, and higher interest rates, which confirms 1 = default.
- **Cleaned default rate:** 45.2%.

| Variable | Default rate below the limit | Default rate past the limit | Limit |
|---|---|---|---|
| Credit score | 56.9% (550 to 599) | 74.0% (500 to 549) | below 550 |
| Loan-to-income | 50.1% (0.20 to 0.25) | 87.1% (0.25 to 0.30) | 0.25 or above |
| Payment-to-income (36-month term) | 53.1% (0.075 to 0.10) | 86.8% (0.10 to 0.125) | 0.10 or above |

Method: a band is high risk when its default rate is at least 1.5 times the cleaned average
(67.7%). The limit is where the high-risk bands start. Interest rate also showed a jump at 14%,
but it is not used as a rule, because a lender sets the rate after judging risk.

Known limits:
- No existing-debt column, so true DTI cannot be calculated. Loan-to-income and payment-to-income
  are used as substitutes.
- No loan term column. Monthly payment assumes 36 months, and that assumption is stored with each result.
- Synthetic data. Results are for a prototype, not real-world accuracy claims.

## Database

The project uses **SQLite**, a serverless database: there is no database server to install, start,
or log into. The whole database is a single file, `loans.db`, in the project root, and Python's
built-in `sqlite3` driver reads and writes it directly. SQLAlchemy is the layer the code uses on top.

`.env` contains exactly:

```
DATABASE_URL=sqlite:///loans.db
```

`loans.db` is created by the user's `load_data.py`, which also fills `historical_loans`.
`get_engine()` then adds the remaining tables to the same file.

SQLite needs four settings that a database server would handle on its own. Apply all of them in
`get_engine()`:

| Setting | Why |
|---|---|
| Resolve `loans.db` relative to the project root, not the current folder | `sqlite:///loans.db` is a relative path. Running `streamlit run app/Home.py` from another folder would otherwise create a second, empty database. |
| `PRAGMA foreign_keys=ON` on every connection (SQLAlchemy `connect` event) | SQLite ignores foreign keys unless this is turned on per connection. |
| `PRAGMA journal_mode=WAL` | Lets the dashboard read while a write is in progress. |
| `connect_args={"check_same_thread": False}` | Streamlit serves requests from several threads. |

Keep the code portable anyway: generic SQLAlchemy types only (`JSON`, not a PostgreSQL type),
so moving to a database server later would only require changing `DATABASE_URL`.

## Final folder layout

```
loan_system/
├── CLAUDE.md
├── .env                 (created by the user: DATABASE_URL=sqlite:///loans.db)
├── loans.db             (SQLite database file, created automatically)
├── .gitignore
├── requirements.txt
├── db.py
├── calculations.py
├── train_ebm.py
├── risk_scorer.py
├── load_data.py         (provided by the user, loads the dataset into loans.db; do not modify)
├── models/              (created by training)
└── app/
    ├── Home.py
    └── pages/
```

---

## Build order

### Step 1. Environment

Create a virtual environment and `requirements.txt` with:

```
interpret-core
sqlalchemy>=2.0
pandas
scikit-learn
joblib
streamlit
plotly
python-dotenv
kagglehub
```

`kagglehub` is used only by `load_data.py`.

No database driver is needed: SQLite support is built into Python.

Use `interpret-core`, not `interpret`. The full package pulls in dashboard dependencies that often
fail to install. Create `.gitignore` containing `.env`, `.venv/`, `__pycache__/`, `models/`, `*.db`, `*.db-wal`,
`*.db-shm`, `*.csv`. The last three keep SQLite's WAL side files and any downloaded dataset out of Git.

**Test:** `python -c "from interpret.glassbox import ExplainableBoostingClassifier; import sqlalchemy, streamlit"`

### Step 2. `db.py`: database models

SQLAlchemy 2.0 style (`DeclarativeBase`, `Mapped`, `mapped_column`), on SQLite. Use generic types
only (`JSON` for JSON fields). All timestamps are timezone-aware UTC.

Tables:

| Table | Columns | Purpose |
|---|---|---|
| `historical_loans` | Already created by `load_data.py`, see below | Training data |
| `applicants` | id, full_name, age, gender, education, annual_income, employment_years, home_ownership, credit_score, credit_history_years, previous_defaults (bool), created_at | Age and gender stored for fairness checks only |
| `applications` | id, applicant_id (FK), loan_amount, loan_intent, interest_rate, term_months, status, current_step, created_at | Status values: `submitted`, `stopped_incomplete`, `in_review`, `approved`, `denied`, `pending_applicant` |
| `documents` | id, application_id (FK), doc_type, storage_path, uploaded_at | File location only, not the file |
| `extracted_fields` | id, document_id (FK), field_name, field_value, confidence, source_page, verified_by_human | Document Reader output (later phase) |
| `calculations` | id, application_id (FK), metric, value, inputs (JSON), code_used, assumptions (JSON list), created_at | Step 4 results |
| `flags` | id, application_id (FK), code, source (`rule` or `pattern`), severity (`info`, `warning`, `critical`), evidence (JSON), created_at | Step 5 results |
| `risk_scores` | id, application_id (FK), model_version, default_probability, contributions (JSON), created_at | Step 6 results |
| `recommendations` | id, application_id (FK), recommendation (`approve`, `deny`, `refer`), reason_codes (JSON), confidence, passed_validation, marked_for_additional_review, review_reasons (JSON), created_at | Steps 7 to 9 |
| `officer_reviews` | id, application_id (FK), officer_name, decision (`approved`, `denied`, `request_info`), reason_codes (JSON), notes, decided_at | The only place a final decision is recorded |
| `audit_log` | id, application_id (FK, nullable), step, actor (`code`, `model`, `tool`, `human:<name>`), action, details (JSON), created_at | Append-only |

Add relationships from `applications` to each child table.

`historical_loans` already exists when the build starts. The `HistoricalLoan` model must match
the table `load_data.py` creates exactly, so `create_all()` leaves it untouched:

| Column | Type |
|---|---|
| `id` | INTEGER PRIMARY KEY |
| `person_age`, `person_income`, `loan_amnt`, `loan_int_rate`, `loan_percent_income`, `cb_person_cred_hist_length` | REAL (`float`) |
| `person_emp_exp`, `credit_score`, `loan_status` | INTEGER (`int`) |
| `person_gender`, `person_education`, `person_home_ownership`, `loan_intent`, `previous_loan_defaults_on_file` | TEXT (`str`) |

Helper functions:
- `get_engine()`: loads `.env` with python-dotenv, reads `DATABASE_URL` (falls back to
  `sqlite:///loans.db` if missing), resolves the file path against the project root, applies the
  four SQLite settings from "Database", creates all tables, and returns the engine.
- `load_historical(engine, clean=True)`: reads `historical_loans` into a pandas DataFrame. With
  `clean=True`, keeps only rows where `previous_loan_defaults_on_file` is "No" (case-insensitive).
  If the table is missing or empty, raise an error telling the user to run `python load_data.py`.
- `log(session, step, actor, action, application_id=None, **details)`: adds an `audit_log` row.

**Test:** run `get_engine()` and list the tables. Confirm `load_historical(engine, clean=False)`
returns 45,000 rows and `load_historical(engine)` returns 22,142, and that `historical_loans`
still has those 45,000 rows afterward.
Then confirm `loans.db` exists in the project root, and that `PRAGMA foreign_keys` returns 1 on a
new connection. Run the same check from inside the `app/` folder and confirm it opens the same file.

### Step 3. `calculations.py`: Financial Calculator and rule checks

Plain Python, no LLM. Each calculation returns a `CalcResult` dataclass with
`metric`, `value`, `inputs` (dict), and `assumptions` (list of strings).

Constants:

```python
DEFAULT_TERM_MONTHS = 36

RULES = {  # Derived from the dataset analysis (cleaned rows, 1.5x risk multiple)
    "max_payment_to_income": 0.10,  # default rate jumps from 53% to 87% (36-month term assumed)
    "max_loan_to_income": 0.25,     # default rate jumps from 50% to 87%
    "min_credit_score": 550,        # 74% default below 550; a stricter choice would be 600 (57% at 550-599)
    "max_income_mismatch": 0.15,    # judgment call, not testable with this dataset
    "loan_percent_income_tolerance": 0.02,  # rounding allowance
}
```

Functions:
- `loan_to_income(loan_amount, annual_income)`: loan / income, rounded to 4 decimals. `None` if income is 0 or less.
- `monthly_payment(principal, annual_rate_pct, term_months=None)`: standard amortization,
  `P * r / (1 - (1 + r) ** -n)` with `r = rate / 100 / 12`. If the rate is 0, use `P / n`.
  If no term is given, use 36 and add the assumption "term not provided, assumed 36 months".
- `payment_to_income(payment, annual_income)`: payment / (income / 12). Always add the
  assumption "excludes existing debts, so it understates true DTI".
- `run_calculations(app)`: runs all three and returns the list. `app` uses dataset column names.
- `check_rules(app, calcs, documented_income=None)`: returns a list of `RuleFlag(code, severity, evidence)`:

| Flag code | Condition | Severity |
|---|---|---|
| `PAYMENT_TO_INCOME_HIGH` | payment-to-income at or above `max_payment_to_income` | warning |
| `LOAN_TO_INCOME_HIGH` | loan-to-income at or above `max_loan_to_income` | warning |
| `CREDIT_SCORE_BELOW_MINIMUM` | credit score below `min_credit_score` | critical |
| `PREVIOUS_DEFAULT_ON_FILE` | `previous_loan_defaults_on_file` is "Yes" (case-insensitive) | critical |
| `RATIO_INCONSISTENT` | provided `loan_percent_income` differs from calculated loan-to-income by more than the tolerance | warning |
| `INCOME_MISMATCH` | documented income given and gap from stated income above `max_income_mismatch` | critical |

Evidence holds the actual value and the limit it was compared to.

**Test 1:** an applicant with income 48,000, loan 22,000, rate 17.5, credit score 530, no prior
default, `loan_percent_income` 0.46, and documented income 40,000 should produce a monthly payment
of about 789.85, payment-to-income of about 0.1975, and flags `PAYMENT_TO_INCOME_HIGH`,
`LOAN_TO_INCOME_HIGH`, `CREDIT_SCORE_BELOW_MINIMUM`, and `INCOME_MISMATCH`.

**Test 2:** an applicant with income 60,000, loan 6,000, rate 9.0, credit score 700, no prior
default, and `loan_percent_income` 0.10 should produce no flags.

### Step 4. `train_ebm.py`: training script

Constants:

```python
PROTECTED = ["person_gender", "person_age"]
FEATURES = ["person_education", "person_income", "person_emp_exp", "person_home_ownership",
            "loan_amnt", "loan_intent", "loan_int_rate", "loan_percent_income",
            "cb_person_cred_hist_length", "credit_score"]
# previous_loan_defaults_on_file is deliberately excluded: see "Dataset analysis results".
TARGET = "loan_status"
POSSIBLE_AGE_PROXIES = ["person_emp_exp", "cb_person_cred_hist_length"]
```

Data comes from the database: `load_historical(get_engine())` from `db.py`. There is no `--csv` option.

Command line (argparse): `--check`, `--target-means` (`default` or `repaid`),
`--out` (default `models`). If neither `--check` nor `--target-means` is given, exit with a message
telling the user to run `--check` first.

`--check` prints:
1. The share of rows for each `loan_status` value.
2. Average `loan_int_rate`, `loan_percent_income`, and `credit_score` for each status.
3. Plain-language signals: higher rates, larger loans relative to income, or lower credit scores
   in status 1 suggest 1 is the bad outcome (default).
4. A warning if the signals disagree.

Both `--check` and training use only the cleaned rows (`clean=True`). Print how many rows were
kept out of the table's total.

Training (`--target-means`):
- Always convert the target so 1 = defaulted. If `repaid`, use `1 - loan_status`.
  For this dataset the user has confirmed `default`.
- Drop rows with missing values in features or target, and report how many.
- 80/20 stratified split, `random_state=42`.
- `ExplainableBoostingClassifier(random_state=42)`, trained on `FEATURES` only.
- Print AUC, accuracy at 0.5, Brier score, and the test default rate.
- Print the top 10 features by `ebm.term_importances()` with `ebm.term_names_`.
- Fairness report on the test set: group by gender and by age band (<25, 25-34, 35-49, 50+).
  For each group, show count, average predicted default probability, share flagged high risk
  (probability 0.5 or above), and the favorable rate divided by the best group's favorable rate.
  Warn for any group below 0.8 (the "four-fifths" rule of thumb). Also print the correlation of
  `POSSIBLE_AGE_PROXIES` with age.
- Save `models/ebm-<UTC timestamp>.pkl` with joblib, plus a matching `.json` containing
  model_version, "predicts": "probability of default", target_means, features,
  excluded_protected, metrics, trained_rows, and a note that it was trained on synthetic data.

**Test:** run `--check` only and confirm about 22,142 rows are kept.

### Step 5. `risk_scorer.py`: Risk Scorer tool

Class `RiskScorer(model_path)` loads the `.pkl` and its `.json` metadata.
Add a helper `latest_model_path(folder="models")` returning the newest `.pkl`.

`score(applicant)`:
- Raise a clear error listing any missing features.
- Build a one-row DataFrame of `FEATURES` (import the list from `train_ebm.py`).
- Return a dict: `model_version`, `default_probability` (rounded to 4 decimals),
  `baseline_log_odds` from `ebm.intercept_`, `contributions` (feature name to value, using
  `ebm.eval_terms(X)[0]` paired with `ebm.term_names_`), `top_risk_drivers` (up to 3 largest
  positive contributions), and `top_protective_factors` (up to 3 largest negative contributions).

Positive contributions raise default risk. Negative ones lower it.

**Test:** after training, score the step 3 test applicant and print the result.

### Step 6. Confirm the target and train

```bash
python train_ebm.py --check
```

Show the output to the user. Expected on the cleaned rows: a default rate of about 45.2%, and
status 1 having higher interest rates, higher `loan_percent_income`, and lower credit scores.
If the output matches, continue. If it doesn't, stop and tell the user.

```bash
python train_ebm.py --target-means default
```

Report the AUC and any fairness warnings to the user. Training happens once. Retrain only when
the data changes.

### Step 7. Streamlit dashboard

Run with `streamlit run app/Home.py`, using Streamlit's multipage layout (`app/pages/`).
Load the newest model and the database engine with `@st.cache_resource`.
`app/Home.py` shows a short project description and a warning if no model is trained yet.

**Page 1, Portfolio overview** (from `historical_loans`)
- Show the cleaned rows (no previous default) by default, with a toggle to include all rows and
  a short note explaining the data artifact
- Row count, default rate, average loan amount, average credit score
- Outcome rate by loan intent, home ownership, and education
- Distribution of credit scores and loan-to-income

**Page 2, Model** (from the model's `.json` and the model)
- Model version, training date, metrics
- Global feature importance chart
- Fairness results by gender and age band, with the 0.8 line shown

**Page 3, Score an applicant**
A form with applicant name, the 10 model features, previous default on file (Yes/No, used by
the rule check only), optional loan term, and optional documented income. Gender and age are not
collected here. On submit, in this order:
1. Create `applicants` and `applications` rows.
2. Run `run_calculations()` and save each result to `calculations`.
3. Run `check_rules()` and save each flag to `flags` with `source="rule"`.
4. Run `RiskScorer.score()` and save to `risk_scores`.
5. Save a `recommendations` row with `recommendation="refer"`, reason codes from the top risk
   drivers and flag codes, and `marked_for_additional_review=True` if any flag is critical.
6. Set the application status to `in_review`.
7. Write an `audit_log` entry for each step.

Display the calculations (with assumptions under each number), flags colored by severity, the
default probability, a contribution bar chart (positive raises risk, negative lowers it), and a
note that this is a recommendation awaiting officer review.

**Page 4, Officer review queue**
- Applications with status `in_review`, marked cases first
- Selecting one shows calculations, flags, score, contributions, and the recommendation
- The officer enters their name and chooses Approve, Deny, or Request more information
- Deny requires at least one reason code; the name is always required
- On submit, write `officer_reviews`, set the status (`approved`, `denied`, or
  `pending_applicant`), and write an `audit_log` entry

**Page 5, Audit log**
- Read-only table of `audit_log`, filterable by application, step, and actor

## Definition of done

- Every file in the folder layout exists and its test passed
- No code reads a CSV; everything reads `historical_loans`, which still has 45,000 rows
- Target check output matches the expected pattern before training
- Rule check tests 1 and 2 both pass
- Model trained, saved, and loaded by the dashboard without retraining
- All five dashboard pages run without errors
- Scoring one applicant creates rows in `applicants`, `applications`, `calculations`, `flags`,
  `risk_scores`, `recommendations`, and `audit_log`
- An officer decision creates an `officer_reviews` row and updates the application status

## Open items for the user

- Whether to keep the credit score minimum at 550 or use the stricter 600
- Sample documents for the Document Reader (later phase)
