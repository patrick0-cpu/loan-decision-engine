"""Generate fictional test documents for the Document Reader.

For 20 applicants from historical_loans, writes a pay stub and a bank statement as PDFs to
samples/, each page marked "SAMPLE - NOT A REAL DOCUMENT", plus samples/answer_key.json with the
correct values. Income on the documents matches the applicant's stated income, except for 5
applicants whose pay stub shows 20% to 40% more (planted mismatches).

Usage: python make_samples.py [--out samples] [--seed 42]
"""
import argparse
import json
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fpdf import FPDF

from db import PROJECT_ROOT, get_engine, load_historical

N_APPLICANTS = 20
N_PLANTED = 5
WATERMARK = "SAMPLE - NOT A REAL DOCUMENT"
PERIODS = {"weekly": 52, "biweekly": 26, "semimonthly": 24, "monthly": 12}
EMPLOYERS = ["Northwind Fabrication LLC", "Bluebird Logistics Inc.", "Cedar Grove Health Partners",
             "Lakeside Software Co.", "Summit Ridge Builders", "Harbor Point Foods",
             "Maple & Finch Retail", "Ironwood Engineering Group", "Silverline Transit Authority",
             "Golden Prairie Schools"]
BANKS = ["First Fictional Bank", "Riverbend Credit Union", "Example Savings & Trust",
         "Placeholder National Bank"]
FIRST = ["Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Avery", "Quinn", "Drew"]
LAST = ["Rivera", "Chen", "Okafor", "Novak", "Haddad", "Larsen", "Mensah", "Ibarra", "Kowalski",
        "Tanaka"]
STATEMENT_START, STATEMENT_END = date(2026, 9, 1), date(2026, 9, 30)


def money(x: float) -> str:
    return f"${x:,.2f}"


class SamplePDF(FPDF):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A fixed timestamp keeps the files byte-identical on every run (fpdf stamps "now").
        self.set_creation_date(datetime(2026, 10, 1, tzinfo=timezone.utc))

    def header(self):
        # Diagonal watermark on every page, drawn first so the content prints over it.
        self.set_font("Helvetica", "B", 34)
        self.set_text_color(225, 225, 225)
        with self.rotation(35, x=self.w / 2, y=self.h / 2):
            self.text(18, self.h / 2 + 10, WATERMARK)
        self.set_text_color(170, 30, 30)
        self.set_font("Helvetica", "B", 9)
        self.set_xy(10, 6)
        self.cell(0, 5, WATERMARK, align="C")
        self.set_text_color(0, 0, 0)
        self.set_xy(10, 16)

    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 5, f"{WATERMARK}  |  Page {self.page_no()} of {{nb}}", align="C")


def row(pdf: FPDF, label: str, value: str, bold: bool = False):
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(70, 7, label)
    pdf.set_font("Helvetica", "B" if bold else "", 10)
    pdf.cell(0, 7, value, new_x="LMARGIN", new_y="NEXT")


def pay_stub(path: Path, name: str, employer: str, period: str, gross: float, pay_date: date,
             start: date, end: date, rng: random.Random):
    pdf = SamplePDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, employer, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, "Earnings Statement", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    row(pdf, "Employee", name)
    row(pdf, "Employee ID", f"E{rng.randint(10000, 99999)}")
    row(pdf, "Pay frequency", period.capitalize())
    row(pdf, "Pay period", f"{start:%m/%d/%Y} - {end:%m/%d/%Y}")
    row(pdf, "Pay date", f"{pay_date:%m/%d/%Y}")
    pdf.ln(4)
    federal, state = round(gross * 0.12, 2), round(gross * 0.045, 2)
    social, medicare = round(gross * 0.062, 2), round(gross * 0.0145, 2)
    net = round(gross - federal - state - social - medicare, 2)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "Earnings", new_x="LMARGIN", new_y="NEXT")
    row(pdf, "Regular pay", money(gross))
    row(pdf, "Gross pay (this period)", money(gross), bold=True)
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "Deductions", new_x="LMARGIN", new_y="NEXT")
    row(pdf, "Federal income tax", money(federal))
    row(pdf, "State income tax", money(state))
    row(pdf, "Social Security", money(social))
    row(pdf, "Medicare", money(medicare))
    pdf.ln(2)
    row(pdf, "Net pay", money(net), bold=True)
    pdf.output(str(path))
    return net


def bank_statement(path: Path, name: str, bank: str, monthly_income: float, rng: random.Random):
    """Page 1: summary with balances. Page 2: transactions and total deposits."""
    opening = round(rng.uniform(400, 9000), 2)
    deposits = [round(monthly_income / 2, 2), round(monthly_income - round(monthly_income / 2, 2), 2)]
    total_deposits = round(sum(deposits), 2)
    spend = [("Rent / mortgage payment", round(monthly_income * rng.uniform(0.25, 0.35), 2)),
             ("Groceries", round(rng.uniform(250, 700), 2)),
             ("Utilities", round(rng.uniform(90, 260), 2)),
             ("Card payment", round(rng.uniform(100, 900), 2))]
    total_withdrawals = round(sum(v for _, v in spend), 2)
    closing = round(opening + total_deposits - total_withdrawals, 2)

    pdf = SamplePDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, bank, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, "Checking Account Statement", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    row(pdf, "Account holder", name)
    row(pdf, "Account number", f"XXXX-XXXX-{rng.randint(1000, 9999)}")
    row(pdf, "Statement period", f"{STATEMENT_START:%m/%d/%Y} - {STATEMENT_END:%m/%d/%Y}")
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "Account summary", new_x="LMARGIN", new_y="NEXT")
    row(pdf, "Opening balance", money(opening), bold=True)
    row(pdf, "Closing balance", money(closing), bold=True)
    pdf.set_font("Helvetica", "", 9)
    pdf.ln(3)
    pdf.multi_cell(0, 5, "Transaction details and totals are listed on the next page.")

    pdf.add_page()
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "Transactions", new_x="LMARGIN", new_y="NEXT")
    days = [STATEMENT_START + timedelta(days=d) for d in (2, 6, 14, 20, 27, 28)]
    lines = [(days[0], "Payroll deposit", deposits[0], True), (days[1], spend[0][0], spend[0][1], False),
             (days[2], spend[1][0], spend[1][1], False), (days[3], spend[2][0], spend[2][1], False),
             (days[4], "Payroll deposit", deposits[1], True), (days[5], spend[3][0], spend[3][1], False)]
    for day, text, amount, is_deposit in lines:
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(30, 7, f"{day:%m/%d/%Y}")
        pdf.cell(100, 7, text)
        pdf.cell(0, 7, ("+" if is_deposit else "-") + money(amount), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    row(pdf, "Total deposits", money(total_deposits), bold=True)
    row(pdf, "Total withdrawals", money(total_withdrawals), bold=True)
    pdf.output(str(path))
    return {"opening_balance": opening, "closing_balance": closing,
            "total_deposits": total_deposits, "total_withdrawals": total_withdrawals}


def period_dates(period: str, pay_date: date) -> tuple[date, date]:
    if period == "weekly":
        return pay_date - timedelta(days=11), pay_date - timedelta(days=5)
    if period == "biweekly":
        return pay_date - timedelta(days=18), pay_date - timedelta(days=5)
    if period == "semimonthly":
        return date(pay_date.year, pay_date.month, 1), date(pay_date.year, pay_date.month, 15)
    return date(pay_date.year, pay_date.month, 1), date(pay_date.year, pay_date.month, 30)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="samples")
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()
    out = Path(args.out)
    out = out if out.is_absolute() else PROJECT_ROOT / out
    out.mkdir(parents=True, exist_ok=True)

    rng = random.Random(args.seed)
    rows = load_historical(get_engine(), clean=True).sample(N_APPLICANTS, random_state=args.seed)
    planted = set(rng.sample(range(N_APPLICANTS), N_PLANTED))
    key = []
    for i, (_, r) in enumerate(rows.iterrows()):
        sid = f"S{i + 1:02d}"
        name = f"{FIRST[i % 10]} {LAST[(i * 3) % 10]} (sample {sid})"
        income = float(r["person_income"])
        factor = round(rng.uniform(1.2, 1.4), 3) if i in planted else 1.0
        period = rng.choice(list(PERIODS))
        gross = round(income * factor / PERIODS[period], 2)
        pay_date = date(2026, 9, 30) if period in ("semimonthly", "monthly") else date(2026, 9, 25)
        start, end = period_dates(period, pay_date)
        employer, bank = rng.choice(EMPLOYERS), rng.choice(BANKS)

        stub_path, bank_path = out / f"{sid}_pay_stub.pdf", out / f"{sid}_bank_statement.pdf"
        net = pay_stub(stub_path, name, employer, period, gross, pay_date, start, end, rng)
        balances = bank_statement(bank_path, name, bank, round(income / 12, 2), rng)
        key.append({
            "sample_id": sid,
            "historical_loan_id": int(r["id"]),
            "full_name": name,
            "planted_mismatch": i in planted,
            "pay_stub_income_factor": factor,
            "form": {  # application form values, from the historical row
                "person_income": income, "person_emp_exp": int(r["person_emp_exp"]),
                "person_education": r["person_education"],
                "person_home_ownership": r["person_home_ownership"],
                "loan_amnt": float(r["loan_amnt"]), "loan_intent": r["loan_intent"],
                "loan_int_rate": float(r["loan_int_rate"]),
                "loan_percent_income": float(r["loan_percent_income"]),
                "credit_score": int(r["credit_score"]),
                "cb_person_cred_hist_length": float(r["cb_person_cred_hist_length"]),
                "previous_loan_defaults_on_file": r["previous_loan_defaults_on_file"],
            },
            "pay_stub": {
                "file": stub_path.name,
                "fields": {"employer": employer, "pay_period": period, "gross_pay": gross,
                           "pay_date": pay_date.isoformat()},
                "pages": {"employer": 1, "pay_period": 1, "gross_pay": 1, "pay_date": 1},
                "net_pay": net,
                "annual_income": round(gross * PERIODS[period], 2),
            },
            "bank_statement": {
                "file": bank_path.name,
                "fields": {"bank_name": bank, "statement_period_start": STATEMENT_START.isoformat(),
                           "statement_period_end": STATEMENT_END.isoformat(),
                           "opening_balance": balances["opening_balance"],
                           "closing_balance": balances["closing_balance"],
                           "total_deposits": balances["total_deposits"]},
                "pages": {"bank_name": 1, "statement_period_start": 1, "statement_period_end": 1,
                          "opening_balance": 1, "closing_balance": 1, "total_deposits": 2},
            },
        })
    (out / "answer_key.json").write_text(json.dumps(key, indent=2))
    print(f"Wrote {2 * len(key)} PDFs and answer_key.json to {out} "
          f"({N_PLANTED} planted mismatches: {', '.join(k['sample_id'] for k in key if k['planted_mismatch'])}).")


if __name__ == "__main__":
    main()
