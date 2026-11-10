"""
AI-assisted budget planner and tracker.

PLAN a budget from your income:
    python budget.py plan --income 95000
    python budget.py plan --income 95000 --savings 25% --fixed "Rent=28000,Utilities=4500"
    python budget.py plan --income 95000 --history oct.csv --notes "saving for a trip, eat out too much"

TRACK spending against the plan:
    python budget.py track transactions.csv
    python budget.py track transactions.csv --month 2026-10 --advice

Design: code does all the math (allocation, totals, pacing). Claude is only used to
(a) categorize unfamiliar transactions, (b) adjust the *proportions* of your plan
based on your notes, and (c) give written advice. Set ANTHROPIC_API_KEY to enable it;
everything else works without it.
"""

import argparse
import calendar
import json
import os
import re
import shutil
import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "budgets.json"
CACHE_PATH = BASE / "category_cache.json"
MODEL = os.getenv("BUDGET_MODEL", "claude-sonnet-5-5")

# Starting proportions for flexible categories when you have no history.
DEFAULT_WEIGHTS = {
    "Groceries": 0.28,
    "Eating out": 0.10,
    "Transport": 0.16,
    "Airtime & Data": 0.06,
    "Entertainment": 0.08,
    "Health": 0.08,
    "Shopping": 0.14,
}


DEFAULT_CONFIG = {
    "currency": "KES",
    "income": 95000,
    "fixed_categories": ["Rent", "Utilities"],
    "savings_category": "Savings & Transfers",
    "budgets": {
        "Rent": 25000, "Utilities": 4000, "Groceries": 12000, "Eating out": 5000,
        "Transport": 6000, "Airtime & Data": 2500, "Entertainment": 3000,
        "Health": 3000, "Shopping": 5000, "Savings & Transfers": 8000,
    },
    "rules": {
        "Rent": ["rent"],
        "Shopping": ["naivas", "quickmart", "carrefour", "chandarana", "greengrocer", "supermarket"],
        "Eating out": ["java", "kfc", "artcaffe", "cafe", "restaurant", "pizza", "glovo", "uber eats"],
        "Transport": ["uber", "bolt", "little cab", "matatu", "fuel", "petrol", "shell", "rubis", "total energies"],
        "Utilities": ["kplc", "kenya power", "nairobi water", "zuku", "safaricom home", "stima"],
        "Airtime & Data": ["airtime", "bundle", "data"],
        "Entertainment": ["netflix", "spotify", "showmax", "cinema", "imax"],
        "Health": ["pharmacy", "hospital", "clinic", "chemist", "sha ", "nhif"],
        "Savings & Transfers": ["mshwari", "m-shwari", "kcb mpesa", "sacco", "money market"],
    },
}


def load_config():
    """Load budgets.json, creating it with sensible defaults if it doesn't exist yet."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False))
        print(f"Created {CONFIG_PATH} with default budgets. Edit it to suit you.")
    return json.loads(CONFIG_PATH.read_text())


def save_config(config):
    if CONFIG_PATH.exists():
        shutil.copy(CONFIG_PATH, CONFIG_PATH.with_suffix(".json.bak"))
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False))


# ---------- Loading transactions ----------

def find_col(df, candidates):
    for cand in candidates:
        for col in df.columns:
            if cand in col:
                return col
    return None


def to_number(series):
    return pd.to_numeric(
        series.astype(str).str.replace(",", "", regex=False).str.strip(),
        errors="coerce",
    ).fillna(0.0)


def load_transactions(path, invert=False, password=None):
    """Return DataFrame: date, description, amount. Negative = money out. Reads CSV or PDF."""
    if str(path).lower().endswith(".pdf"):
        import pdf_import
        try:
            df, notes = pdf_import.load_pdf(path, password)
        except pdf_import.PdfError as e:
            sys.exit(str(e))
        for note in notes:
            print(f"Note: {note}")
        return df
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]

    date_col = find_col(df, ["completion time", "date", "time"])
    desc_col = find_col(df, ["details", "description", "narrative", "particulars"])
    if not date_col or not desc_col:
        sys.exit(f"Couldn't find date/description columns. Found: {list(df.columns)}")

    paid_in = find_col(df, ["paid in", "credit", "money in"])
    withdrawn = find_col(df, ["withdrawn", "debit", "money out"])
    amount_col = find_col(df, ["amount"])

    if paid_in and withdrawn:  # M-Pesa style
        amount = to_number(df[paid_in]) - to_number(df[withdrawn]).abs()
    elif amount_col:
        amount = to_number(df[amount_col])
        if invert:
            amount = -amount
    else:
        sys.exit(f"Couldn't find an amount column. Found: {list(df.columns)}")

    raw_dates = df[date_col].astype(str).str.strip()
    dates = pd.to_datetime(raw_dates, format="ISO8601", errors="coerce")
    if dates.isna().any():  # per-value fallback: pandas would otherwise guess one format for all
        from pdf_import import parse_one_date
        dates = dates.where(dates.notna(), raw_dates.map(parse_one_date))

    out = pd.DataFrame(
        {
            "date": dates,
            "description": df[desc_col].astype(str).str.strip(),
            "amount": amount,
        }
    ).dropna(subset=["date"])
    return out


# ---------- Categorizing ----------

def mask(text):
    """Strip phone numbers and long digit strings before anything is sent to an API."""
    text = re.sub(r"(?<!\d)(?:\+?254|0)7\d{8}(?!\d)", "<phone>", text)
    text = re.sub(r"\d{6,}", "#", text)
    return text


def rule_category(desc, rules):
    low = desc.lower()
    for category, keywords in rules.items():
        if any(k.lower() in low for k in keywords):
            return category
    return None


def get_client():
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        print("  (anthropic package not installed - AI features skipped)")
        return None
    return anthropic.Anthropic()


def extract_json(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(match.group(0)) if match else {}


def llm_categorize(descriptions, categories):
    client = get_client()
    if client is None:
        print("  (AI categorization unavailable - unmatched items go to 'Other')")
        return {}
    results = {}
    for i in range(0, len(descriptions), 40):
        batch = descriptions[i : i + 40]
        prompt = (
            "Categorize each bank/M-Pesa transaction description into exactly one of "
            f"these categories: {', '.join(categories)}.\n"
            "Reply with ONLY a JSON object mapping each description (exact text) to its "
            "category. Use 'Other' if unsure.\n\n" + json.dumps(batch, ensure_ascii=False)
        )
        try:
            resp = client.messages.create(
                model=MODEL, max_tokens=2000, messages=[{"role": "user", "content": prompt}]
            )
            for desc, cat in extract_json(resp.content[0].text).items():
                results[desc] = cat if cat in categories else "Other"
        except Exception as e:
            print(f"  (AI categorization failed for a batch: {e})")
    return results


def categorize(df, config):
    rules = config["rules"]
    categories = list(config["budgets"].keys()) + ["Other"]
    cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}

    cats, unknown = [], set()
    for desc, amount in zip(df["description"], df["amount"]):
        if amount > 0:
            cats.append("Income")
            continue
        masked = mask(desc)
        cat = rule_category(desc, rules) or cache.get(masked)
        if cat is None:
            unknown.add(masked)
        cats.append(cat)

    if unknown:
        print(f"Asking AI to categorize {len(unknown)} new description(s)...")
        cache.update(llm_categorize(sorted(unknown), categories))
        CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False))

    df = df.copy()
    df["category"] = [c or cache.get(mask(d)) or "Other" for d, c in zip(df["description"], cats)]
    return df


# ======================================================================
# PLAN MODE
# ======================================================================

def parse_savings(value, income):
    value = value.strip()
    if value.endswith("%"):
        return income * float(value[:-1]) / 100
    return float(value.replace(",", ""))


def match_category(name, categories):
    for c in categories:
        if c.lower() == name.lower():
            return c
    return name


def history_weights(csv_path, config, flexible):
    """Average monthly spend per flexible category from past transactions."""
    df = categorize(load_transactions(csv_path), config)
    spend = df[df["amount"] < 0]
    months = max(df["date"].dt.to_period("M").nunique(), 1)
    avg = spend.groupby("category")["amount"].apply(lambda s: s.abs().sum()) / months
    weights = {c: float(avg.get(c, 0.0)) for c in flexible}
    positives = sorted(v for v in weights.values() if v > 0)
    if not positives:
        return None
    floor = 0.25 * positives[len(positives) // 2]  # categories with no history get a small share
    return {c: (v if v > 0 else floor) for c, v in weights.items()}


def ai_adjust_weights(weights, income, notes):
    client = get_client()
    if client is None:
        print("  (notes ignored: set ANTHROPIC_API_KEY to let AI adjust the plan)")
        return weights
    prompt = (
        "I'm building a monthly budget. Here are baseline relative weights for my flexible "
        f"spending categories: {json.dumps(weights)}.\n"
        f"Monthly income: {income:,.0f}. My notes about my situation: \"{notes}\"\n"
        "Adjust the weights to reflect my notes (e.g. raise categories I need more for, "
        "trim ones I want to cut). Keep every category present. Reply with ONLY a JSON "
        "object mapping each category to a positive number."
    )
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=500, messages=[{"role": "user", "content": prompt}]
        )
        new = extract_json(resp.content[0].text)
        adjusted = {c: float(new.get(c, w)) for c, w in weights.items()}
        if all(v > 0 for v in adjusted.values()):
            return adjusted
    except Exception as e:
        print(f"  (AI adjustment failed, using baseline: {e})")
    return weights


def allocate(pool, weights):
    """Split `pool` by weights, rounded to the nearest 100, summing exactly to pool."""
    total = sum(weights.values())
    amounts = {c: int(round(pool * w / total / 100.0)) * 100 for c, w in weights.items()}
    diff = int(pool) - sum(amounts.values())
    biggest = max(amounts, key=amounts.get)
    amounts[biggest] += diff
    return amounts


def resolve_fixed(config, overrides=None):
    """Fixed-cost categories and amounts: config defaults, then any overrides."""
    budgets = config["budgets"]
    fixed_cats = list(config.get("fixed_categories", ["Rent", "Utilities"]))
    fixed = {c: float(budgets.get(c, 0)) for c in fixed_cats}
    for name, amount in (overrides or {}).items():
        name = match_category(name, list(budgets) + fixed_cats)
        fixed[name] = float(amount)
        if name not in fixed_cats:
            fixed_cats.append(name)
    return fixed, fixed_cats


def flexible_categories(config, fixed):
    sav = config.get("savings_category", "Savings & Transfers")
    return [c for c in config["budgets"] if c not in fixed and c != sav]


def compute_plan(config, income, savings="20%", fixed_overrides=None, weights=None):
    """Pure calculation (no printing or saving). Raises ValueError if the numbers don't fit."""
    sav_cat = config.get("savings_category", "Savings & Transfers")
    fixed, fixed_cats = resolve_fixed(config, fixed_overrides)
    savings_amt = round(parse_savings(str(savings), income))
    pool = int(round(income - savings_amt - sum(fixed.values())))
    if pool <= 0:
        raise ValueError(
            f"Not enough room: income {income:,.0f} - savings {savings_amt:,.0f} - fixed costs "
            f"{sum(fixed.values()):,.0f} leaves {pool:,.0f}. Lower the savings target or fixed costs."
        )
    flexible = flexible_categories(config, fixed)
    w = {c: (weights or {}).get(c, DEFAULT_WEIGHTS.get(c, 0.05)) for c in flexible}
    budgets = {c: round(v) for c, v in fixed.items()}
    budgets.update(allocate(pool, w))
    budgets[sav_cat] = savings_amt
    return {
        "budgets": budgets, "fixed": fixed, "fixed_categories": fixed_cats,
        "savings": savings_amt, "savings_category": sav_cat, "pool": pool,
    }


def plan_warnings(plan, income):
    notes = []
    rent = plan["fixed"].get("Rent")
    if rent and rent / income > 0.35:
        notes.append(f"Rent is {rent / income * 100:.0f}% of income, above the common 30-35% guideline.")
    if plan["savings"] / income < 0.10:
        notes.append("Savings are under 10% of income. Even a small emergency fund helps.")
    return notes


def save_plan(config, income, plan):
    config["income"] = income
    config["budgets"] = plan["budgets"]
    config["fixed_categories"] = plan["fixed_categories"]
    config["savings_category"] = plan["savings_category"]
    save_config(config)


def cmd_plan(args):
    config = load_config()
    cur = config.get("currency", "KES")
    income = float(args.income)

    overrides = {}
    for item in filter(None, (args.fixed or "").split(",")):
        if "=" not in item:
            sys.exit(f"Bad --fixed entry '{item}'. Use Name=Amount, e.g. Rent=28000")
        name, amount = item.split("=", 1)
        overrides[name.strip()] = float(amount.replace(",", ""))

    fixed, _ = resolve_fixed(config, overrides)
    flexible = flexible_categories(config, fixed)
    weights = None
    if args.history:
        weights = history_weights(args.history, config, flexible)
        if weights:
            print("Using your past spending to shape the proportions.")
    if args.notes:
        base = weights or {c: DEFAULT_WEIGHTS.get(c, 0.05) for c in flexible}
        weights = ai_adjust_weights(base, income, args.notes)

    try:
        plan = compute_plan(config, income, args.savings, overrides, weights)
    except ValueError as e:
        sys.exit(str(e))

    print(f"\nPlan for monthly income of {income:,.0f} {cur}")
    print("=" * 46)
    print(f"{'Category':<22}{'Amount':>10}{'% income':>11}")
    print("-" * 46)
    for c, v in plan["budgets"].items():
        tag = " (fixed)" if c in plan["fixed"] else ""
        print(f"{c + tag:<22}{v:>10,.0f}{v / income * 100:>10.1f}%")
    total = sum(plan["budgets"].values())
    print("-" * 46)
    print(f"{'Total':<22}{total:>10,.0f}{total / income * 100:>10.1f}%")
    for note in plan_warnings(plan, income):
        print(f"\nNote: {note}")

    if args.dry_run:
        print("\n(dry run - nothing saved)")
        return
    save_plan(config, income, plan)
    print(f"\nSaved to {CONFIG_PATH.name} (previous version kept as .bak). Now track with:")
    print("  python budget.py track your_transactions.csv")


# ======================================================================
# TRACK MODE
# ======================================================================

def summarize(df, config, month, as_of=None):
    """All tracking numbers as data (used by both the CLI report and the dashboard)."""
    budgets = config["budgets"]
    fixed = set(config.get("fixed_categories", []))
    sav = config.get("savings_category", "Savings & Transfers")

    year, mon = map(int, month.split("-"))
    days_in_month = calendar.monthrange(year, mon)[1]
    day = int((as_of if as_of is not None else df["date"].max()).day)
    days_left = days_in_month - day

    spend = df[df["amount"] < 0].copy()
    spend["amount"] = spend["amount"].abs()
    spent = spend.groupby("category")["amount"].sum()

    actual_income = float(df.loc[df["amount"] > 0, "amount"].sum())
    income_planned = actual_income <= 0
    income = float(config.get("income", 0)) if income_planned else actual_income

    rows, flex_left = [], 0.0
    for cat in list(budgets) + [c for c in spent.index if c not in budgets]:
        s = float(spent.get(cat, 0.0))
        b = budgets.get(cat)
        if s == 0 and b is None:
            continue
        if not b:
            rows.append({"Category": cat, "Spent": s, "Budget": None, "Left": None,
                         "Projected": None, "Status": ""})
            continue
        if cat == sav:
            proj, status = s, ("done" if s >= b else "pending")
        else:
            lumpy = cat in fixed or days_left == 0
            proj = s if lumpy else s / max(day, 1) * days_in_month
            status = "OVER" if s > b else ("watch" if proj > b else "ok")
            if cat not in fixed:
                flex_left += max(b - s, 0)
        rows.append({"Category": cat, "Spent": s, "Budget": float(b), "Left": b - s,
                     "Projected": proj, "Status": status})

    spent_total = float(sum(v for c, v in spent.items() if c != sav))
    saved = float(spent.get(sav, 0.0))
    return {
        "month": month, "day": day, "days_in_month": days_in_month, "days_left": days_left,
        "table": pd.DataFrame(rows), "income": income, "income_planned": income_planned,
        "spent_total": spent_total, "saved": saved,
        "unallocated": income - spent_total - saved,
        "flex_left": flex_left,
        "flex_per_day": flex_left / days_left if days_left > 0 else None,
        "top": spend[spend["category"] != sav].nlargest(5, "amount"),
    }


def spending_breakdown(df, config, include_savings=True):
    """Where the money went: amount, share of total and budget use per category."""
    sav = config.get("savings_category", "Savings & Transfers")
    out = df[df["amount"] < 0].copy()
    if not include_savings:
        out = out[out["category"] != sav]
    out["spent"] = out["amount"].abs()
    g = (out.groupby("category")["spent"].agg(["sum", "count"]).reset_index()
            .rename(columns={"category": "Category", "sum": "Amount", "count": "Transactions"}))
    total = g["Amount"].sum()
    g["Share %"] = (g["Amount"] / total * 100).round(1) if total else 0.0
    g["Budget"] = g["Category"].map(config["budgets"])
    g["Budget used %"] = (g["Amount"] / g["Budget"] * 100).round(1)
    g["Amount"] = g["Amount"].round(2)
    return g.sort_values("Amount", ascending=False).reset_index(drop=True)


def export_frame(df):
    """Transactions ready for CSV, with each counted expense's share of its month's spending."""
    cols = [c for c in ["date", "account", "description", "amount", "category", "nature", "counterparty"] if c in df.columns]
    out = df[cols].sort_values("date").copy()
    is_spend = out["amount"] < 0
    if "nature" in out:
        is_spend &= out["nature"] == "None"
    spend = out["amount"].abs().where(is_spend, 0)
    month_total = spend.groupby(out["date"].dt.strftime("%Y-%m")).transform("sum")
    out["pct_of_month_spend"] = (spend / month_total * 100).round(2).where(is_spend)
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    return out


def build_report(df, config, month, as_of=None):
    cur = config.get("currency", "KES")
    s = summarize(df, config, month, as_of)
    lines = [
        f"Budget report for {month}  (data through day {s['day']} of {s['days_in_month']})",
        "=" * 74,
        f"{'Category':<20}{'Spent':>10}{'Budget':>10}{'Left':>10}{'Projected':>11}  Status",
        "-" * 74,
    ]
    for _, r in s["table"].iterrows():
        if pd.isna(r["Budget"]):
            lines.append(f"{r['Category']:<20}{r['Spent']:>10,.0f}{'-':>10}{'-':>10}{'-':>11}")
        else:
            lines.append(f"{r['Category']:<20}{r['Spent']:>10,.0f}{r['Budget']:>10,.0f}"
                         f"{r['Left']:>10,.0f}{r['Projected']:>11,.0f}  {r['Status']}")
    lines.append("-" * 74)
    income_label = "Income (planned)" if s["income_planned"] else "Income"
    lines.append(f"{income_label:<20}{s['income']:>10,.0f} {cur}")
    lines.append(f"{'Spent':<20}{s['spent_total']:>10,.0f} {cur}")
    lines.append(f"{'Saved/transferred':<20}{s['saved']:>10,.0f} {cur}")
    lines.append(f"{'Unallocated':<20}{s['unallocated']:>10,.0f} {cur}")
    if s["flex_per_day"] is not None:
        lines.append(
            f"\nFlexible budget left: {s['flex_left']:,.0f} {cur} "
            f"(about {s['flex_per_day']:,.0f} {cur}/day for the next {s['days_left']} days)"
        )
    lines.append("\nLargest expenses:")
    for _, r in s["top"].iterrows():
        lines.append(f"  {r['date']:%d %b}  {r['amount']:>9,.0f}  {r['category']:<16} {r['description'][:28]}")
    return "\n".join(lines)


def ask_about_report(report_text, question, config):
    client = get_client()
    if client is None:
        return "Add an API key to ask questions."
    prompt = (
        f"Here is my monthly budget report (currency: {config.get('currency', 'KES')}):\n\n"
        f"{report_text}\n\nMy question: {question}\n\n"
        "Answer using only the numbers in the report. Be concise and practical. "
        "If the report doesn't contain enough to answer, say what's missing."
    )
    resp = client.messages.create(
        model=MODEL, max_tokens=600, messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text


def get_advice(report_text, config):
    client = get_client()
    if client is None:
        return "Set ANTHROPIC_API_KEY to get AI advice."
    prompt = (
        f"Here is my monthly budget report (currency: {config.get('currency', 'KES')}). "
        "'Projected' is my estimated month-end spend at the current pace.\n\n"
        f"{report_text}\n\n"
        "Give me 3-5 practical, specific observations or suggestions: which categories are "
        "at risk of going over, anything unusual, and one concrete change for the rest of "
        "this month. Be concise and don't invent numbers that aren't in the report."
    )
    resp = client.messages.create(
        model=MODEL, max_tokens=800, messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text


def cmd_track(args):
    import ledger

    config = load_config()
    frames = []
    for path in args.csv:  # each statement file is treated as its own account (named after the file)
        f = load_transactions(path, invert=args.invert, password=args.password)
        f["account"] = Path(path).stem
        frames.append(f)
    df, dupes = ledger.combine(frames)
    if df.empty:
        sys.exit("No transactions found.")
    if dupes:
        print(f"Note: skipped {dupes} duplicate row(s) from overlapping statements.")
    df["month"] = df["date"].dt.strftime("%Y-%m")
    month = args.month or df["month"].max()
    if month not in set(df["month"]):
        sys.exit(f"No transactions for {month}.")

    df = ledger.process(categorize(df, config))  # tags, transfers between accounts, contacts, debts
    month_df = df[df["month"] == month]
    report = build_report(ledger.counted_view(month_df), config, month, ledger.as_of_for_month(df, month))
    print("\n" + report)

    if df["account"].nunique() > 1:
        print("\nBy account:")
        print(ledger.account_summary(df, month).to_string(index=False, float_format=lambda v: f"{v:,.0f}"))
        pairs = ledger.transfer_pairs(df, "link_id")
        if not pairs.empty:
            print(f"\n{len(pairs)} transfer(s) between your accounts were ignored (see the dashboard's Accounts tab).")
        if not ledger.transfer_pairs(df, "prop_id").empty:
            print("Some possible transfers need your confirmation in the dashboard's Accounts tab.")

    skipped = ledger.exclusion_summary(month_df)
    if skipped:
        print("\nNot counted as income/spending: " + ", ".join(f"{k} {v:,.0f}" for k, v in skipped.items()))
    held = month_df[month_df["nature"] == "Pending"]
    if not held.empty:
        print(f"\nHeld for your approval ({len(held)}, not counted yet). Open the dashboard to decide:")
        for _, r in held.head(8).iterrows():
            print(f"  {r['date']:%d %b}  {r['amount']:>11,.0f}  {r['description'][:38]}")
    open_debts = ledger.debt_summary()
    open_debts = open_debts[open_debts["Status"] != "Settled"]
    if not open_debts.empty:
        print("\nOpen debts:")
        for _, r in open_debts.iterrows():
            print(f"  {r['Status']:<11}{r['Counterparty'][:28]:<30}{r['Outstanding']:>10,.0f}")
    if args.export:
        export_frame(month_df).to_csv(args.export, index=False, encoding="utf-8-sig")
        print(f"\nSaved categorized transactions to {args.export}")
    if args.advice:
        print("\nAI advice\n" + "=" * 74)
        print(get_advice(report, config))


# ---------- CLI ----------

def main():
    parser = argparse.ArgumentParser(description="AI-assisted budget planner and tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="Build a budget from your income")
    p.add_argument("--income", required=True, type=float, help="Monthly take-home income")
    p.add_argument("--savings", default="20%", help="Savings target: '20%%' or an amount (default 20%%)")
    p.add_argument("--fixed", help='Fixed costs, e.g. "Rent=28000,Utilities=4500"')
    p.add_argument("--history", help="CSV of past transactions to base proportions on")
    p.add_argument("--notes", help="Free-text goals/situation for AI to factor in")
    p.add_argument("--dry-run", action="store_true", help="Show the plan without saving it")
    p.set_defaults(func=cmd_plan)

    t = sub.add_parser("track", help="Compare spending to your plan")
    t.add_argument("csv", nargs="+", help="One or more statements (CSV or PDF). Each file is treated as one account.")
    t.add_argument("--password", help="Password for protected PDFs (applies to every PDF given)")
    t.add_argument("--month", help="Month, e.g. 2026-10 (default: latest in data)")
    t.add_argument("--advice", action="store_true", help="Get AI suggestions")
    t.add_argument("--export", help="Save the categorized transactions to this CSV path")
    t.add_argument("--invert", action="store_true", help="Use if spending is positive in your CSV")
    t.set_defaults(func=cmd_track)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
