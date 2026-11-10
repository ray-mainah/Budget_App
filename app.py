"""
Local budget dashboard. Run with:   streamlit run app.py

Tabs: Plan, Track, Transactions (tag income/debt/none), Debts, Bills, Contacts, Ask.
All the logic lives in budget.py (budgeting), ledger.py (tags, debts, bills, contacts) and
pdf_import.py (PDF statements), so the dashboard and the command line always agree.
"""

import hashlib
import io
import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

import budget as bd
import ledger as lg
import pdf_import
import theme

theme.apply_theme("Budget Kingdom", "Plan it. Track it. Own it.", "💰", kicker="Black & Gold")

config = bd.load_config()
cur = config.get("currency", "KES")
SAMPLE = Path(__file__).parent / "sample_transactions.csv"
SAMPLE_FILES = [("sample_transactions.csv", "Bank"), ("sample_mpesa.csv", "M-Pesa")]
ICON = {"ok": "✅", "watch": "⚠️", "OVER": "🔴", "done": "✅", "pending": "⏳"}
BAR_STATE = {"watch": "watch", "OVER": "over"}
BILL_ICON = {"Paid": "✅", "Partial": "🟡", "Overdue": "🔴", "Due soon": "⏳", "Upcoming": "🗓️"}
BILL_BAR = {"Partial": "watch", "Overdue": "over"}


def money(x):
    return f"{cur} {x:,.0f}"


def to_csv(frame):
    return frame.to_csv(index=False).encode("utf-8-sig")  # utf-8-sig so Excel opens it cleanly


def table(rows, cols, numeric=()):
    """DataFrame with fixed columns and proper dtypes (so editors behave even when empty)."""
    df = pd.DataFrame(rows, columns=cols)
    for c in numeric:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def flash(message):
    """Show a message after the next rerun (saving usually reruns the page)."""
    st.session_state["flash"] = message


# ---------- Sidebar: AI, statements, approval rules ----------
with st.sidebar:
    st.header("AI features")
    key = st.text_input("Anthropic API key", type="password",
                        help="Optional. Kept in memory for this session only.")
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
    if os.getenv("ANTHROPIC_API_KEY"):
        st.success("AI is on")
    else:
        st.info("AI is off. Everything except AI categorization, plan tweaks and advice still works.")
    st.caption(
        "Categorization sends only masked transaction descriptions. Advice and questions "
        "send your category totals. Nothing else leaves your computer."
    )

    st.header("Your statements")
    st.caption("Add one statement per account (bank, M-Pesa, card...). They are combined, and transfers "
               "between your own accounts are spotted and ignored.")
    uploaded = st.file_uploader("Statements (CSV or PDF)", type=["csv", "pdf"], accept_multiple_files=True)
    use_sample = st.checkbox("Use the included sample data", disabled=not SAMPLE.exists())

    known_files = lg.load_file_map()
    specs = []
    for n, f in enumerate(uploaded or []):
        pdf_f = f.name.lower().endswith(".pdf")
        with st.expander(f"⚙️ {f.name}", expanded=len(uploaded) <= 3):
            acct = st.text_input(
                "Which account is this?", value=known_files.get(f.name, lg.default_account_name(f.name)),
                key=f"acct_{n}_{f.name}",
                help="Files with the same account name are merged, e.g. October and November of one account.")
            pw = st.text_input(
                "PDF password (if protected)", type="password", key=f"pw_{n}_{f.name}",
                help="M-Pesa statements are protected; the password is sent to you by SMS.") if pdf_f else ""
            flip = False if pdf_f else st.checkbox("Spending shows as positive numbers", key=f"flip_{n}_{f.name}")
        specs.append({"file": f.name, "account": acct.strip() or lg.default_account_name(f.name),
                      "raw": f.getvalue(), "pdf": pdf_f, "password": pw, "flip": flip})
    if not specs and use_sample:
        for fname, acct in SAMPLE_FILES:
            path = SAMPLE.parent / fname
            if path.exists():
                specs.append({"file": fname, "account": acct, "raw": path.read_bytes(), "pdf": False,
                              "password": "", "flip": False, "sample": True})

    st.header("Approval rules")
    settings = lg.load_settings()
    limit_in = st.number_input(
        f"Hold amounts above ({cur})", min_value=100.0, step=1000.0, value=float(settings["approval_threshold"]),
        help="Anything above this, money in or out, waits for you to allocate it before it counts anywhere.")
    review_in = st.checkbox(
        "Review ALL new incoming money", value=bool(settings["review_all_incoming"]),
        help="Off: only large amounts and possible debt payments wait for you.")
    window_in = st.number_input(
        "Match transfers within (days)", min_value=0, max_value=10, step=1,
        value=int(settings["transfer_window_days"]),
        help="How far apart in time the two sides of a transfer between your accounts may be.")
    new_settings = {"approval_threshold": float(limit_in), "review_all_incoming": review_in,
                    "transfer_window_days": int(window_in)}
    if any(new_settings[k] != settings[k] for k in new_settings):
        lg.save_settings(new_settings)


@st.cache_data(show_spinner="Reading your statement...")
def load_data(raw: bytes, pdf: bool, pdf_password: str, flip: bool, config_json: str):
    cfg = json.loads(config_json)
    notes = []
    if pdf:
        df, notes = pdf_import.load_pdf(io.BytesIO(raw), pdf_password or None)
    else:
        df = bd.load_transactions(io.BytesIO(raw), invert=flip)
    df = bd.categorize(df, cfg)
    return df, notes


# Read every statement -> combine -> tag (contacts, transfers, approvals) -> sync the debt ledger
config_json = json.dumps(config, sort_keys=True)
frames, load_notes = [], []
for sp in specs:
    try:
        df_f, notes_f = load_data(sp["raw"], sp["pdf"], sp["password"], sp["flip"], config_json)
    except (SystemExit, pdf_import.PdfError) as e:
        st.sidebar.error(f"{sp['file']}: {e or 'could not be read'}")
        continue
    if df_f.empty:
        st.sidebar.error(f"{sp['file']}: no transactions found.")
        continue
    load_notes += [f"{sp['file']}: {n}" for n in notes_f]
    frames.append(df_f.assign(account=sp["account"]))
lg.remember_files({sp["file"]: sp["account"] for sp in specs if not sp.get("sample")})

tx_all = tx = None
shown, dupes = [], 0
if frames:
    combined, dupes = lg.combine(frames)
    combined["month"] = combined["date"].dt.strftime("%Y-%m")
    tx_all = lg.process(combined)
    names = sorted(tx_all["account"].unique())
    shown = names
    if len(names) > 1:
        shown = st.sidebar.multiselect(
            "Accounts to analyze", names, default=names,
            key="acct_filter_" + hashlib.md5("|".join(names).encode()).hexdigest()[:8],
            help="Pick one account to analyze it alone, or several to combine them. "
                 "Transfers between your accounts are ignored either way.")
    tx = tx_all[tx_all["account"].isin(shown)]
    if tx.empty:
        st.sidebar.warning("Select at least one account.")
        tx = None
if dupes:
    st.sidebar.info(f"Skipped {dupes} duplicate row(s) that appeared in overlapping statements.")

month = month_df = counted = as_of = None
if tx is not None:
    month = st.sidebar.selectbox("Month", sorted(tx["month"].unique(), reverse=True))
    month_df = tx[tx["month"] == month]
    counted = lg.counted_view(month_df)       # only what counts as income / spending
    as_of = lg.as_of_for_month(tx, month)
for note in load_notes:
    st.sidebar.warning(note)

if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))

if tx is not None:
    held_all = lg.pending_view(tx)
    if not held_all.empty:
        n_debt = int((held_all["suggest"] != "").sum())
        parts = []
        if n_debt:
            parts.append(f"**{n_debt}** possible debt payment(s) on the 🤝 Debts tab")
        if len(held_all) - n_debt:
            parts.append(f"**{len(held_all) - n_debt}** amount(s) to allocate on the 🏷️ Transactions tab")
        st.warning(f"⏳ {len(held_all)} item(s) are waiting for you and don't count yet: " + " and ".join(parts) + ".")
if tx_all is not None:
    n_prop = len(lg.transfer_pairs(tx_all, "prop_id"))
    if n_prop:
        st.info(f"🔁 {n_prop} possible transfer(s) between your accounts need a quick look on the 🏦 Accounts tab. "
                "Until you decide, they count as normal income and spending.")

tab_plan, tab_track, tab_tx, tab_debts, tab_bills, tab_accounts, tab_contacts, tab_ask = st.tabs(
    ["📝 Plan", "📊 Track", "🏷️ Transactions", "🤝 Debts", "🧾 Bills", "🏦 Accounts", "👤 Contacts", "💬 Ask"])


# ---------- Plan tab ----------
with tab_plan:
    st.subheader("Build a budget from your income")
    fixed_now, _ = bd.resolve_fixed(config)

    with st.form("plan_form"):
        c1, c2 = st.columns(2)
        income = c1.number_input(f"Monthly take-home income ({cur})", min_value=0.0,
                                 value=float(config.get("income", 0)), step=1000.0)
        savings_pct = c2.slider("Savings target (% of income)", 0, 50, 20)
        st.markdown("**Fixed monthly costs**")
        cols = st.columns(max(len(fixed_now), 1))
        overrides = {name: col.number_input(name, min_value=0.0, value=float(val), step=500.0)
                     for col, (name, val) in zip(cols, fixed_now.items())}
        notes = st.text_input("Goals or situation (optional)",
                              placeholder="e.g. saving for a trip, I eat out too much")
        history = st.file_uploader("Past transactions CSV (optional)", type="csv",
                                   help="If given, your real spending shapes the proportions.")
        submitted = st.form_submit_button("Build plan")

    if submitted:
        if income <= 0:
            st.error("Enter your monthly income first.")
        else:
            fixed_map, _ = bd.resolve_fixed(config, overrides)
            flexible = bd.flexible_categories(config, fixed_map)
            weights = None
            if history is not None:
                weights = bd.history_weights(io.BytesIO(history.getvalue()), config, flexible)
            if notes:
                if os.getenv("ANTHROPIC_API_KEY"):
                    base_w = weights or {c: bd.DEFAULT_WEIGHTS.get(c, 0.05) for c in flexible}
                    weights = bd.ai_adjust_weights(base_w, income, notes)
                else:
                    st.warning("Add an API key in the sidebar to let AI use your notes.")
            try:
                st.session_state["plan"] = (income, bd.compute_plan(
                    config, income, f"{savings_pct}%", overrides, weights))
            except ValueError as e:
                st.session_state.pop("plan", None)
                st.error(str(e))

    if "plan" in st.session_state:
        plan_income, plan = st.session_state["plan"]
        plan_df = pd.DataFrame({"Category": list(plan["budgets"]),
                                "Amount": list(plan["budgets"].values())})
        plan_df["% of income"] = (plan_df["Amount"] / plan_income * 100).round(1)
        plan_df["Type"] = plan_df["Category"].map(
            lambda c: "Fixed" if c in plan["fixed"]
            else ("Savings" if c == plan["savings_category"] else "Flexible"))
        left, right = st.columns([3, 2])
        left.dataframe(plan_df, hide_index=True)
        right.markdown("<div class='gold-card'>" + theme.donut_html(
            list(plan["budgets"].items()), "Monthly income", money(plan_income), size=220) + "</div>",
            unsafe_allow_html=True)
        for w in bd.plan_warnings(plan, plan_income):
            st.warning(w)
        if st.button("💾 Save this plan"):
            bd.save_plan(config, plan_income, plan)
            flash("Plan saved. The Track tab now compares your spending against it.")
            st.rerun()


# ---------- Track tab ----------
def render_track():
    if tx is None:
        st.info("Upload a statement (CSV or PDF) in the sidebar, or tick the sample data box.")
        return
    s = bd.summarize(counted, config, month, as_of)
    st.session_state["report"] = bd.build_report(counted, config, month, as_of)

    m = st.columns(4)
    m[0].metric("Income" + (" (planned)" if s["income_planned"] else ""), money(s["income"]))
    m[1].metric("Spent", money(s["spent_total"]))
    m[2].metric("Saved", money(s["saved"]))
    m[3].metric("Unallocated", money(s["unallocated"]))
    st.caption(f"Data through day {s['day']} of {s['days_in_month']} · Accounts: {', '.join(shown)}"
               + (" · transfers between them ignored" if len(shown) > 1 else ""))
    skipped = lg.exclusion_summary(month_df)
    if skipped:
        st.caption("Not counted as income or spending: "
                   + " · ".join(f"{k} {money(v)}" for k, v in skipped.items())
                   + ". Approve or allocate held items in the Debts or Transactions tab.")
    if s["flex_per_day"] is not None:
        st.info(f"Flexible budget left: **{money(s['flex_left'])}**, about "
                f"**{money(s['flex_per_day'])}/day** for the next {s['days_left']} days.")

    st.subheader("Where your money went")
    include_sav = st.checkbox("Include savings & transfers", value=True)
    bk = bd.spending_breakdown(counted, config, include_savings=include_sav)
    if bk.empty:
        st.info("No spending recorded this month.")
    else:
        items = list(zip(bk["Category"], bk["Amount"]))
        d_col, r_col = st.columns([2, 3])
        d_col.markdown("<div class='gold-card'>" + theme.donut_html(
            items, "Total out", money(bk["Amount"].sum())) + "</div>", unsafe_allow_html=True)
        r_col.markdown("<div class='gold-card'>" + theme.share_rows_html(items, money) + "</div>",
                       unsafe_allow_html=True)

    st.markdown("**Download your data**")
    e1, e2, e3 = st.columns(3)
    e1.download_button("⬇ This month (CSV)", to_csv(bd.export_frame(month_df)),
                       f"transactions_{month}.csv", "text/csv")
    e2.download_button("⬇ All months (CSV)", to_csv(bd.export_frame(tx)),
                       "transactions_all_months.csv", "text/csv")
    e3.download_button("⬇ Category summary (CSV)", to_csv(bk),
                       f"category_summary_{month}.csv", "text/csv")

    st.subheader("Budget by category")
    for _, r in s["table"].iterrows():
        if pd.isna(r["Budget"]):
            st.write(f"**{r['Category']}**: {money(r['Spent'])} (no budget set)")
            continue
        pace = f" · on pace for {money(r['Projected'])}" if r["Status"] in ("watch", "OVER") else ""
        st.write(f"{ICON.get(r['Status'], '')} **{r['Category']}**: "
                 f"{money(r['Spent'])} of {money(r['Budget'])}{pace}")
        st.markdown(theme.progress_html(r["Spent"] / r["Budget"], BAR_STATE.get(r["Status"], "ok")),
                    unsafe_allow_html=True)

    budgeted = s["table"].dropna(subset=["Budget"]).set_index("Category")
    st.subheader("Spent vs projected vs budget")
    st.bar_chart(budgeted[["Spent", "Projected", "Budget"]], color=["#FFD60A", "#FF9F1C", "#8E8E8E"])

    other = counted[(counted["category"] == "Other") & (counted["amount"] < 0)]
    if not other.empty:
        st.warning(f"{len(other)} transaction(s) are uncategorized ('Other'). Add AI (sidebar key), "
                   "add keywords in budgets.json, or register the account in the Contacts tab.")

    st.subheader("Largest expenses")
    top = s["top"][["date", "amount", "category", "description"]].copy()
    top["date"] = top["date"].dt.strftime("%d %b")
    st.dataframe(top, hide_index=True)

    if st.button("✨ Get AI advice"):
        with st.spinner("Thinking..."):
            try:
                st.markdown(bd.get_advice(st.session_state["report"], config))
            except Exception as e:
                st.error(f"AI request failed: {e}")


with tab_track:
    render_track()


# ---------- Transactions tab: income / debt / none ----------
CHOOSE = "— choose —"


def render_allocation():
    """Large amounts (and, if switched on, all new incoming money) wait here for your decision."""
    held = tx[(tx["nature"] == "Pending") & (tx["suggest"] == "")]
    if held.empty:
        return
    limit = lg.load_settings()["approval_threshold"]
    st.subheader(f"⏳ Needs your allocation ({len(held)})")
    st.caption(f"Amounts above {money(limit)} are held here. They don't count towards income, spending, debts or "
               "bills until you allocate them. Pick what each one is, then confirm.")
    grid = held.set_index("tx_id")[["date", "description", "amount", "reason", "counterparty"]].copy()
    grid["date"] = grid["date"].dt.strftime("%d %b %Y")
    grid["allocate"] = CHOOSE
    sig = hashlib.md5(",".join(grid.index).encode()).hexdigest()[:8]  # fresh editor state when the list changes
    edited = st.data_editor(
        grid, key=f"alloc_{sig}",
        column_config={
            "date": st.column_config.TextColumn("Date"),
            "description": st.column_config.TextColumn("Description", width="large"),
            "amount": st.column_config.NumberColumn("Amount", format="%.2f"),
            "reason": st.column_config.TextColumn("Why it's held"),
            "counterparty": st.column_config.TextColumn("Counterparty", help="Who this was with (used to group debts)."),
            "allocate": st.column_config.SelectboxColumn(
                "Allocate as", options=[CHOOSE] + lg.NATURES, required=True,
                help="Money in: Income, Debt or None. Money out: Debt or None. Own account = your own money."),
        },
        disabled=["date", "description", "amount", "reason"],
    )
    if st.button("✅ Confirm allocations"):
        picked = edited[edited["allocate"] != CHOOSE]
        if picked.empty:
            st.warning("Choose an allocation for at least one row first.")
        else:
            saved, skipped = lg.allocate(
                [(i, r["allocate"], r["counterparty"]) for i, r in picked.iterrows()], tx.set_index("tx_id"))
            flash(f"Allocated {saved} item(s)."
                  + (f" {skipped} skipped: money you sent can't be Income." if skipped else ""))
            st.rerun()
    st.divider()


def render_transactions():
    if tx is None:
        st.info("Load a statement first (sidebar).")
        return
    render_allocation()
    st.subheader("Tag your transactions")
    st.caption("**Money received:** Income, Debt (you borrowed, or someone paid you back) or None. "
               "**Money sent:** Debt (you lent, or you repaid) or None. "
               "**Own account** = moving your own money, never counted. "
               "Money you send to someone you owe is matched to that debt automatically; money coming in that looks like "
               "a debt payment, and large amounts, are held for your approval.")
    show = st.radio("Show", ["All", "Money in", "Money out", "Debt", "Own account"], horizontal=True)
    view = month_df[month_df["nature"] != "Pending"]  # held items are decided above / in the Debts tab
    if show == "Money in":
        view = view[view["amount"] > 0]
    elif show == "Money out":
        view = view[view["amount"] < 0]
    elif show == "Debt":
        view = view[view["nature"] == "Debt"]
    elif show == "Own account":
        view = view[view["nature"] == "Own account"]
    if view.empty:
        st.info("Nothing to show here.")
        return

    grid = view.set_index("tx_id")[["date", "description", "amount", "nature", "counterparty", "category", "reason"]].copy()
    grid["date"] = grid["date"].dt.strftime("%d %b %Y")
    sig = hashlib.md5(",".join(grid.index).encode()).hexdigest()[:8]
    edited = st.data_editor(
        grid, key=f"tags_{month}_{show}_{sig}",
        column_config={
            "date": st.column_config.TextColumn("Date"),
            "description": st.column_config.TextColumn("Description", width="large"),
            "amount": st.column_config.NumberColumn("Amount", format="%.0f"),
            "nature": st.column_config.SelectboxColumn(
                "Nature", options=lg.NATURES, required=True,
                help="Money in: Income, Debt or None. Money out: Debt or None."),
            "counterparty": st.column_config.TextColumn(
                "Counterparty", help="Who this was with. Debts are grouped by this name."),
            "category": st.column_config.TextColumn("Category"),
            "reason": st.column_config.TextColumn("Why"),
        },
        disabled=["date", "description", "amount", "category", "reason"],
    )
    if st.button("💾 Save tags"):
        changed, corrected = lg.save_tag_edits(edited, month_df.set_index("tx_id"))
        msg = f"Saved {changed} change(s)." if changed else "No changes to save."
        if corrected:
            msg += f" {corrected} money-out row(s) set to Income were changed to None (money you sent can't be income)."
        flash(msg)
        st.rerun()


with tab_tx:
    render_transactions()


# ---------- Debts tab ----------
def render_approvals():
    """Incoming money that looks like a debt payment waits here. Nothing reaches the debts without your OK."""
    queue = tx[(tx["nature"] == "Pending") & (tx["suggest"] != "")] if tx is not None else None
    st.markdown(f"#### 📥 Awaiting your approval ({0 if queue is None else len(queue)})")
    if queue is None or queue.empty:
        st.caption("Money that looks like a debt payment waits here until you approve it. "
                   "Nothing is added to your debts without your OK.")
        return
    summary = lg.debt_summary().set_index("Counterparty")
    current = tx.set_index("tx_id")
    for tid, r in queue.sort_values("date").set_index("tx_id").iterrows():
        owed = ""
        if r["suggest"] in summary.index:
            d = summary.loc[[r["suggest"]]].iloc[0]
            if d["Status"] == "Owed to me":
                owed = f" · they currently owe you {money(d['Outstanding'])}"
            elif d["Status"] == "I owe":
                owed = f" · you currently owe them {money(d['Outstanding'])}"
        st.markdown(theme.note_card_html(
            f"{money(r['amount'])} received from {r['suggest']}",
            f"{r['date']:%d %b %Y} · {str(r['description'])[:60]} · {r['reason']}{owed}", "waiting"),
            unsafe_allow_html=True)
        c1, c2, c3 = st.columns([2, 2, 2])
        if c1.button("✅ Approve as debt", key=f"ap_{tid}"):
            lg.allocate([(tid, "Debt", r["suggest"])], current)
            flash(f"Approved: {money(r['amount'])} from {r['suggest']} is now in your debts.")
            st.rerun()
        other = c2.selectbox("If it isn't a debt, record it as", ["Income", "None", "Own account"], key=f"as_{tid}")
        if c3.button("✕ Not a debt", key=f"no_{tid}"):
            lg.allocate([(tid, other, r["counterparty"])], current)
            flash(f"Recorded {money(r['amount'])} from {r['suggest']} as {other}.")
            st.rerun()
    st.divider()


def render_debts():
    st.subheader("Debts")
    st.caption("This page updates itself. Every loan or repayment tagged Debt moves the balance, "
               "and a debt drops off the open lists the moment it reaches zero.")
    render_approvals()
    summary = lg.debt_summary()
    open_ = summary[summary["Status"] != "Settled"]
    owe, owed = open_[open_["Status"] == "I owe"], open_[open_["Status"] == "Owed to me"]

    m = st.columns(4)
    m[0].metric("I owe", money(owe["Outstanding"].sum()))
    m[1].metric("Owed to me", money(owed["Outstanding"].sum()))
    m[2].metric("Net position", money(owed["Outstanding"].sum() - owe["Outstanding"].sum()))
    m[3].metric("Open debts", len(open_))

    left, right = st.columns(2)
    left.markdown("#### 💸 I owe")
    if owe.empty:
        left.info("You don't owe anyone right now. 🎉")
    for _, r in owe.iterrows():
        left.markdown(theme.debt_card_html(
            r["Counterparty"], money(r["Outstanding"]), r["Progress"],
            f"Borrowed {money(r['Original'])} · repaid {money(r['Repaid'])} · last activity {r['Last activity']}"),
            unsafe_allow_html=True)
    right.markdown("#### 💰 Owed to me")
    if owed.empty:
        right.info("Nobody owes you right now.")
    for _, r in owed.iterrows():
        right.markdown(theme.debt_card_html(
            r["Counterparty"], money(r["Outstanding"]), r["Progress"],
            f"Lent {money(r['Original'])} · received back {money(r['Repaid'])} · last activity {r['Last activity']}"),
            unsafe_allow_html=True)

    settled = summary[summary["Status"] == "Settled"]
    with st.expander(f"✅ Settled debts ({len(settled)})"):
        if settled.empty:
            st.caption("Nothing settled yet. Paid-off debts will appear here automatically.")
        else:
            st.dataframe(settled[["Counterparty", "Original", "Last activity"]].rename(
                columns={"Original": "Amount cleared", "Last activity": "Settled on"}), hide_index=True)

    with st.expander("➕ Add a debt or payment by hand (cash, or outside your statements)"):
        with st.form("manual_debt"):
            c1, c2 = st.columns(2)
            who = c1.text_input("Who?", help="Use the same name as in the lists above to add to that person's debt.")
            action = c2.selectbox("What happened?", list(lg.DEBT_ACTIONS))
            c3, c4 = st.columns(2)
            amount = c3.number_input(f"Amount ({cur})", min_value=0.0, step=100.0)
            when = c4.date_input("Date")
            note = st.text_input("Note (optional)")
            if st.form_submit_button("Add entry"):
                if who.strip() and amount > 0:
                    lg.add_manual_debt(who, action, amount, when, note)
                    flash(f"Added: {action.lower()}, {money(amount)} ({who.strip()}).")
                    st.rerun()
                else:
                    st.error("Enter a name and an amount.")

    with st.expander("📒 Full ledger (every entry behind these balances)"):
        entries = lg.debt_entries()
        if entries.empty:
            st.caption("No entries yet. Tag a transaction as Debt, or add one by hand above.")
        else:
            st.dataframe(entries.drop(columns="id"), hide_index=True)
            manual = entries[entries["source"] == "manual"]
            if not manual.empty:
                labels = {f"{r['date']} · {r['counterparty']} · {r['amount']:+,.0f}": r["id"] for _, r in manual.iterrows()}
                pick = st.selectbox("Remove a manual entry", list(labels))
                if st.button("🗑 Remove entry"):
                    lg.delete_debt_entry(labels[pick])
                    flash("Entry removed.")
                    st.rerun()
    st.caption("To change a debt that came from a statement, re-tag that transaction in the Transactions tab.")


with tab_debts:
    render_debts()


# ---------- Bills tab ----------
def render_bills():
    st.subheader("Bills")
    st.caption("List your regular bills. When a matching payment appears in your statement the bill flips "
               "to Paid on its own. 'Paid to' can be a paybill/till/account/phone number or a name.")
    grid = table(lg.load_bills(), ["name", "match", "amount", "due_day"], numeric=("amount", "due_day"))
    edited = st.data_editor(
        grid, num_rows="dynamic", hide_index=True, key="bills_editor",
        column_config={
            "name": st.column_config.TextColumn("Bill"),
            "match": st.column_config.TextColumn("Paid to (number or keyword)"),
            "amount": st.column_config.NumberColumn("Expected amount", min_value=0, step=100),
            "due_day": st.column_config.NumberColumn("Due day of month", min_value=1, max_value=31, step=1),
        })
    if st.button("💾 Save bills"):
        flash(f"Saved {lg.save_bills(edited.to_dict('records'))} bill(s).")
        st.rerun()

    bills = lg.load_bills()
    if not bills:
        st.info("Add your first bill above, for example: Rent, landlord's number, 25000, due day 1.")
        return
    if tx is None:
        st.info("Load a statement (sidebar) to see which bills are paid.")
        return

    status = lg.bill_status(bills, month_df, month, as_of)
    st.subheader(f"Status for {month}")
    m = st.columns(4)
    m[0].metric("Paid", f"{(status['Status'] == 'Paid').sum()} of {len(status)}")
    m[1].metric("Total expected", money(status["Expected"].sum()))
    m[2].metric("Paid so far", money(status["Paid"].sum()))
    m[3].metric("Still to pay", money(status["Remaining"].sum()))
    order = {"Overdue": 0, "Due soon": 1, "Partial": 2, "Upcoming": 3, "Paid": 4}
    cols = st.columns(2)
    for i, (_, r) in enumerate(status.sort_values("Status", key=lambda s: s.map(order)).iterrows()):
        frac = r["Paid"] / r["Expected"] if r["Expected"] else (1.0 if r["Paid"] else 0.0)
        paid_on = f" on {r['Paid on']}" if r["Paid on"] else ""
        cols[i % 2].markdown(theme.debt_card_html(
            r["Bill"], f"{BILL_ICON.get(r['Status'], '')} {r['Status']}", frac,
            f"Due {r['Due']} · expected {money(r['Expected'])} · paid {money(r['Paid'])}{paid_on}",
            BILL_BAR.get(r["Status"], "ok")), unsafe_allow_html=True)


with tab_bills:
    render_bills()


# ---------- Accounts tab ----------
def render_accounts():
    st.subheader("Accounts")
    st.caption("Name your accounts and, if you like, add their phone or account numbers. A payment in one "
               "account that names a number registered here is recognised as going to another of YOUR "
               "accounts. Use numbers only: a word would mark every transaction containing it as your own money.")
    registry = lg.load_accounts()
    loaded = sorted(tx_all["account"].unique()) if tx_all is not None else []
    names = list(dict.fromkeys(list(registry) + loaded))
    rows = [{"name": n, "type": registry.get(n, {}).get("type", "Other"),
             "numbers": ", ".join(registry.get(n, {}).get("identifiers", []))} for n in names]
    edited = st.data_editor(
        table(rows, ["name", "type", "numbers"]), num_rows="dynamic", hide_index=True,
        key="accounts_" + hashlib.md5("|".join(names).encode()).hexdigest()[:8],
        column_config={
            "name": st.column_config.TextColumn("Account name"),
            "type": st.column_config.SelectboxColumn("Type", options=lg.ACCOUNT_TYPES),
            "numbers": st.column_config.TextColumn(
                "Phone / account numbers", help="Separate several with commas, e.g. 0722123456, 1234567890"),
        })
    if st.button("💾 Save accounts"):
        flash(f"Saved {lg.save_accounts(edited.to_dict('records'))} account(s). Transfers have been re-checked.")
        st.rerun()
    if tx_all is None:
        st.info("Add statements in the sidebar to see each account analyzed and your transfers matched.")
        return

    st.markdown(f"#### Per-account summary · {month or 'all months'}")
    summary = lg.account_summary(tx_all, month)
    st.dataframe(summary, hide_index=True)
    plot = summary[summary["Account"] != "Combined"].set_index("Account")[["Money in", "Money out"]]
    if len(plot):
        st.bar_chart(plot, color=["#FFD60A", "#8E8E8E"])
    st.caption("Money in and out count only real income and spending. 'Moved between accounts' is shown for "
               "your information and never counted.")

    window = lg.load_settings()["transfer_window_days"]
    linked = lg.transfer_pairs(tx_all, "link_id")
    st.markdown(f"#### 🔁 Transfers between your accounts ({len(linked)}), ignored everywhere")
    if linked.empty:
        st.caption("None found yet. A transfer is recognised when the same amount leaves one account and arrives "
                   f"in another within {window} day(s), and the wording or a registered number backs it up.")
    else:
        grid = linked.set_index("id")[["date", "from", "to", "amount", "why"]].copy()
        grid["date"] = grid["date"].dt.strftime("%d %b %Y")
        grid["not a transfer"] = False
        edited = st.data_editor(
            grid, key="linked_" + hashlib.md5(",".join(grid.index).encode()).hexdigest()[:8],
            disabled=["date", "from", "to", "amount", "why"],
            column_config={
                "amount": st.column_config.NumberColumn("Amount", format="%.0f"),
                "why": st.column_config.TextColumn("Matched because"),
                "not a transfer": st.column_config.CheckboxColumn("Not a transfer", help="Tick if this is wrong."),
            })
        if st.button("💾 Un-link ticked transfers"):
            ticked = list(edited[edited["not a transfer"]].index)
            for pid in ticked:
                lg.decide_transfer(pid, "reject")
            flash(f"Un-linked {len(ticked)} transfer(s); those payments now count normally." if ticked
                  else "Nothing ticked.")
            st.rerun()

    props = lg.transfer_pairs(tx_all, "prop_id")
    st.markdown(f"#### ❓ Possible transfers to confirm ({len(props)})")
    if props.empty:
        st.caption("Nothing to confirm.")
    else:
        st.caption(f"Same amount within {window} day(s), but nothing else links them, so they still count as normal "
                   "income and spending until you decide.")
        undecided, yes, no = "— undecided —", "Yes, it's a transfer", "No, it isn't"
        grid = props.set_index("id")[["date", "from", "to", "amount"]].copy()
        grid["date"] = grid["date"].dt.strftime("%d %b %Y")
        grid["decision"] = undecided
        edited = st.data_editor(
            grid, key="props_" + hashlib.md5(",".join(grid.index).encode()).hexdigest()[:8],
            disabled=["date", "from", "to", "amount"],
            column_config={
                "amount": st.column_config.NumberColumn("Amount", format="%.0f"),
                "decision": st.column_config.SelectboxColumn("Is it a transfer?", options=[undecided, yes, no], required=True),
            })
        if st.button("💾 Save decisions"):
            done = 0
            for pid, r in edited.iterrows():
                if r["decision"] in (yes, no):
                    lg.decide_transfer(pid, "confirm" if r["decision"] == yes else "reject")
                    done += 1
            flash(f"Saved {done} decision(s)." if done else "Choose Yes or No for at least one row.")
            st.rerun()


with tab_accounts:
    render_accounts()


# ---------- Contacts tab ----------
def render_contacts():
    st.subheader("Contacts & accounts")
    st.caption("Add as many phone numbers, account numbers, paybills, tills or names as you like and say what "
               "they are. Matching is automatic, even for numbers written differently (0722…, +254722…) or "
               "masked on statements (0722***456). The numbers of your OWN accounts can also go on the 🏦 Accounts tab.")
    with st.expander("What do the types do?"):
        st.markdown(
            "- **Own account**: your own numbers and accounts. Transfers to or from them are *not counted* "
            "as spending or income.\n"
            "- **Expense**: a shop, landlord or paybill. Payments are filed under the category you pick.\n"
            "- **Debt party**: someone you lend to or borrow from. Their transactions default to Debt.\n"
            "- **Income source**: an employer or client. Money in from them is Income.")
    cats = list(config["budgets"]) + ["Other"]
    grid = table(lg.load_contacts(), ["identifier", "label", "type", "category"])
    edited = st.data_editor(
        grid, num_rows="dynamic", hide_index=True, key="contacts_editor",
        column_config={
            "identifier": st.column_config.TextColumn("Phone / account / paybill / name"),
            "label": st.column_config.TextColumn("Label"),
            "type": st.column_config.SelectboxColumn("What is it?", options=lg.CONTACT_TYPES),
            "category": st.column_config.SelectboxColumn(
                "Expense category", options=cats, help="Only used for the Expense type."),
        })
    if st.button("💾 Save contacts"):
        flash(f"Saved {lg.save_contacts(edited.to_dict('records'))} contact(s). Tags and totals have been refreshed.")
        st.rerun()


with tab_contacts:
    render_contacts()


# ---------- Ask tab ----------
with tab_ask:
    report = st.session_state.get("report")
    if not report:
        st.info("Load your transactions and open the Track tab first, then ask questions here.")
    else:
        question = st.text_input("Ask about your spending",
                                 placeholder="Can I afford a 15,000 trip this month?")
        if st.button("Ask") and question:
            with st.spinner("Thinking..."):
                try:
                    st.markdown(bd.ask_about_report(report, question, config))
                except Exception as e:
                    st.error(f"AI request failed: {e}")
