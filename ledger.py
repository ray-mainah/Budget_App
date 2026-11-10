"""
Contacts, transaction tagging, the debt ledger and bills.

Pure pandas + JSON (no Streamlit), so the dashboard and the command line follow exactly the
same rules. Everything you enter is saved in ./data/*.json next to these scripts.

The ideas
  * Every transaction gets a NATURE:  Income | Debt | None | Own account
      money in  -> Income, Debt or None        money out -> Debt or None
      "Own account" = moving your own money around; it is never counted.
  * CONTACTS (phone / account / paybill / name) can be tied to an expense category, marked
    as your own account, as a debt party, or as an income source.
  * DEBTS are a net balance per person. Money in tagged Debt adds to what I owe them (or
    reduces what they owe me); money out tagged Debt does the opposite. Balance back to
    zero = settled. So when a repayment shows up, the debt page updates itself.
"""

import calendar
import hashlib
import json
import re
import uuid
from datetime import date
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"
CONTACT_TYPES = ["Own account", "Expense", "Debt party", "Income source"]
NATURES = ["Income", "Debt", "None", "Own account"]
# action -> sign of the entry. Positive = I owe them more (or they owe me less).
DEBT_ACTIONS = {
    "I borrowed from them": 1,
    "They paid me back": 1,
    "I lent to them": -1,
    "I paid them back": -1,
}
TOL = 0.5  # balances smaller than this count as settled
NATURES_GRID = NATURES + ["Pending"]  # "Pending" = held until you decide (never chosen by hand)
DEFAULT_SETTINGS = {"approval_threshold": 10000.0, "review_all_incoming": False, "transfer_window_days": 2}


# ---------------------------------------------------------------- storage

def _load(name, default):
    path = DATA_DIR / f"{name}.json"
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        path.rename(path.with_suffix(".json.corrupt"))  # keep it; never silently overwrite
        return default


def _save(name, data):
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"{name}.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- settings (approval rules)

def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    settings.update(_load("settings", {}))
    return settings


def save_settings(settings):
    _save("settings", {k: settings[k] for k in DEFAULT_SETTINGS if k in settings})


# ---------------------------------------------------------------- accounts & transfer decisions

ACCOUNT_TYPES = ["Bank", "M-Pesa / mobile money", "Card", "Cash", "Other"]


def load_accounts():
    """{account name: {"type": ..., "identifiers": [phone / account numbers]}}"""
    return _load("accounts", {})


def save_accounts(rows):
    out = {}
    for r in rows:
        name = str(r.get("name") or "").strip()
        if not name or name.lower() == "nan":
            continue
        raw = str(r.get("numbers") or "")
        ids = [s.strip() for s in raw.replace(";", ",").split(",") if s.strip() and s.strip().lower() != "nan"]
        out[name] = {"type": r.get("type") if r.get("type") in ACCOUNT_TYPES else "Other", "identifiers": ids}
    _save("accounts", out)
    return len(out)


def load_file_map():
    """Which account each statement file was last assigned to (so you aren't asked twice)."""
    return _load("files", {})


def remember_files(mapping):
    current = load_file_map()
    if any(current.get(k) != v for k, v in mapping.items()):
        current.update(mapping)
        _save("files", current)


def default_account_name(filename):
    return Path(filename).stem.strip() or "Account"


def load_transfers():
    d = _load("transfers", {})
    return {"confirmed": list(d.get("confirmed", [])), "rejected": list(d.get("rejected", []))}


def transfer_key(a, b):
    return "|".join(sorted([a, b]))


def decide_transfer(key, decision):
    """decision: 'confirm' (it IS a transfer), 'reject' (it is NOT), or 'clear'."""
    d = load_transfers()
    for lst in d.values():
        while key in lst:
            lst.remove(key)
    if decision == "confirm":
        d["confirmed"].append(key)
    elif decision == "reject":
        d["rejected"].append(key)
    _save("transfers", d)


# ---------------------------------------------------------------- matching phone / account numbers

_MASK_RE = re.compile(r"(\+?\d{2,6})\*{2,}(\d{2,4})")


def _national(digits):
    if digits.startswith("254"):
        return digits[3:]
    return digits[1:] if digits.startswith("0") else digits


def matches(description, identifier):
    """Does this description mention this phone / account / paybill / name?

    Phone-style numbers match however they're written (0722123456, +254722123456,
    0722 123 456) and also the masked form statements use (0722***456).
    """
    ident = str(identifier or "").strip()
    if not ident:
        return False
    text = re.sub(r"(\+?\d{3,4})[\s\-](\d{3})[\s\-](\d{3,4})", r"\1\2\3", str(description))
    digits = re.sub(r"\D", "", ident)
    if len(digits) >= 4 and not re.search(r"[A-Za-z]", ident):
        for run in re.findall(r"\d+", text):
            if len(digits) >= 9:
                if len(run) >= 9 and run.endswith(digits[-9:]):
                    return True
            elif run == digits:
                return True
        if len(digits) >= 9:
            full = digits[-9:]
            for prefix, suffix in _MASK_RE.findall(text):
                p = _national(re.sub(r"\D", "", prefix))
                if p and full.startswith(p) and full.endswith(suffix):
                    return True
        return False
    return ident.lower() in text.lower()


def find_contact(description, contacts):
    for c in contacts:
        if matches(description, c.get("identifier")):
            return c
    return None


_PREFIX_RE = re.compile(
    r"^(customer transfer(?: fuliza)?|funds received|received|sent|pay ?bill|buy goods|"
    r"merchant payment|customer payment|business payment|transfer)\b[\s\-:]*(?:to|from|for)?[\s\-:]*",
    re.I,
)


def guess_counterparty(description):
    s = re.sub(r"\s+", " ", str(description)).strip()
    s = _PREFIX_RE.sub("", s).strip(" -:")
    return (s or str(description).strip())[:40]


def _key(name):
    return re.sub(r"\s+", " ", str(name).strip().lower())


# ---------------------------------------------------------------- contacts

def load_contacts():
    return _load("contacts", [])


def save_contacts(rows):
    clean = []
    for r in rows:
        ident = str(r.get("identifier") or "").strip()
        if not ident or ident.lower() == "nan":
            continue
        ctype = r.get("type") if r.get("type") in CONTACT_TYPES else "Expense"
        label = str(r.get("label") or "").strip()
        cat = str(r.get("category") or "").strip()
        clean.append({"identifier": ident, "label": "" if label == "nan" else label, "type": ctype,
                      "category": "" if cat == "nan" else cat})
    _save("contacts", clean)
    return len(clean)


# ---------------------------------------------------------------- tags

def load_tags():
    return _load("tags", {})


def add_ids(df):
    """A stable id per transaction, so tags survive re-uploading the same statement.

    The account name is part of the id (two accounts can have identical-looking rows).
    `legacy_id` is the old account-less id, so tags saved by earlier versions still apply.
    """
    df = df.copy()
    if "account" not in df.columns:
        df["account"] = ""
    df["account"] = df["account"].fillna("").astype(str)
    base = (df["date"].dt.strftime("%Y-%m-%d %H:%M:%S") + "|" + df["description"].str.strip().str.lower()
            + "|" + df["amount"].round(2).astype(str))
    legacy_occ = base.groupby(base).cumcount()
    with_acct = df["account"] + "|" + base
    occ = with_acct.groupby(with_acct).cumcount()
    md5 = lambda s: hashlib.md5(s.encode()).hexdigest()[:12]
    df["legacy_id"] = [md5(f"{b}|{o}") for b, o in zip(base, legacy_occ)]
    df["tx_id"] = [lg if not acct else md5(f"{acct}|{b}|{o}")
                   for lg, acct, b, o in zip(df["legacy_id"], df["account"], base, occ)]
    return df


# ---------------------------------------------------------------- transfers between your own accounts

_TRANSFER_WORDS = re.compile(
    r"\b(transfer|trf|own acc(?:ount)?|withdraw(?:al)?|deposit|top ?-?up|pesalink|mpesa|m-pesa|float)\b", re.I)


def _mentions(description, account, accounts):
    """Does this description name the OTHER account (its name, or a number registered on it)?"""
    names = [account] + list(accounts.get(account, {}).get("identifiers", []))
    return any(len(str(n).strip()) >= 3 and matches(description, n) for n in names)


def find_transfers(df, accounts, own_ids, decisions, window_days=2):
    """Pair money leaving one account with an equal amount arriving in another within a few days.

    Returns (links, proposals).
      links     {tx_id: {"key", "account": partner account, "why"}}  - treated as 'Own account'
      proposals [{"key", "out", "in"}]  - same amount and date but NO other evidence: shown for you to confirm
    Evidence = a leg names the other account / its number, a leg matches a registered own account,
    or the wording says transfer / deposit / withdraw / mpesa. One-to-one: a leg pairs at most once.
    """
    links, proposals = {}, []
    if df["account"].nunique() < 2:
        return links, proposals
    confirmed, rejected = set(decisions["confirmed"]), set(decisions["rejected"])
    money_in = {}
    for r in df[df["amount"] > 0].itertuples():
        money_in.setdefault(int(round(r.amount * 100)), []).append(r)

    cands = []
    for o in df[df["amount"] < 0].itertuples():
        for i in money_in.get(int(round(-o.amount * 100)), []):
            if i.account == o.account:
                continue
            gap = abs((i.date.normalize() - o.date.normalize()).days)
            if gap > window_days:
                continue
            if _mentions(o.description, i.account, accounts) or _mentions(i.description, o.account, accounts):
                why = "names the other account"
            elif o.tx_id in own_ids or i.tx_id in own_ids:
                why = "registered own account"
            elif _TRANSFER_WORDS.search(f"{o.description} {i.description}"):
                why = "transfer wording"
            else:
                why = ""
            cands.append((0 if why else 1, gap, o.date, o, i, why))
    cands.sort(key=lambda c: c[:3])  # evidence first, then closest in time

    used = set()
    for weak, _, _, o, i, why in cands:
        if o.tx_id in used or i.tx_id in used:
            continue
        key = transfer_key(o.tx_id, i.tx_id)
        if key in rejected:
            continue
        if not weak or key in confirmed:
            why = why or "confirmed by you"
            links[o.tx_id] = {"key": key, "account": i.account, "why": why}
            links[i.tx_id] = {"key": key, "account": o.account, "why": why}
        else:
            proposals.append({"key": key, "out": o.tx_id, "in": i.tx_id})
        used.update({o.tx_id, i.tx_id})
    return links, proposals


def transfer_pairs(df, column="link_id"):
    """One row per transfer pair (column = 'link_id' for linked, 'prop_id' for proposed)."""
    cols = ["id", "date", "from", "to", "amount", "why"]
    rows = []
    sub = df[df[column] != ""] if column in df.columns else df.iloc[0:0]
    for pid, g in sub.groupby(column):
        out, inn = g[g["amount"] < 0], g[g["amount"] > 0]
        if len(g) != 2 or out.empty or inn.empty:
            continue
        o, i = out.iloc[0], inn.iloc[0]
        rows.append({"id": pid, "date": min(o["date"], i["date"]), "from": o["account"], "to": i["account"],
                     "amount": float(i["amount"]), "why": o.get("link_why", "") or ""})
    return pd.DataFrame(rows, columns=cols).sort_values("date").reset_index(drop=True)


def combine(frames):
    """Stack statements from several accounts. Rows repeated because two statements of the SAME
    account overlap are dropped; genuinely identical rows inside one statement are kept.
    Returns (table, duplicates_removed)."""
    parts = []
    for f in frames:
        f = f.copy()
        key = (f["account"].astype(str) + "|" + f["date"].dt.strftime("%Y-%m-%d %H:%M:%S") + "|"
               + f["description"].str.strip().str.lower() + "|" + f["amount"].round(2).astype(str))
        f["_key"], f["_occ"] = key, key.groupby(key).cumcount()
        parts.append(f)
    out = pd.concat(parts, ignore_index=True)
    before = len(out)
    out = out.drop_duplicates(subset=["_key", "_occ"]).drop(columns=["_key", "_occ"])
    return out.sort_values("date", kind="stable").reset_index(drop=True), before - len(out)


def account_summary(df, month=None):
    """Per-account totals, plus a Combined row. 'Moved between accounts' is shown but never counted."""
    d = df if month is None else df[df["month"] == month]
    cols = ["Account", "Money in", "Money out", "Moved between accounts", "Debt flows", "Held", "Transactions"]
    rows = []
    for acct, g in d.groupby("account"):
        c = counted_view(g)
        rows.append({
            "Account": acct or "(unnamed)",
            "Money in": float(c.loc[c["amount"] > 0, "amount"].sum()),
            "Money out": float(c.loc[c["amount"] < 0, "amount"].abs().sum()),
            "Moved between accounts": float(g.loc[g["nature"] == "Own account", "amount"].abs().sum()),
            "Debt flows": float(g.loc[g["nature"] == "Debt", "amount"].abs().sum()),
            "Held": float(g.loc[g["nature"] == "Pending", "amount"].abs().sum()),
            "Transactions": len(g),
        })
    out = pd.DataFrame(rows, columns=cols)
    if len(out) > 1:
        total = [out[c].sum() for c in cols[1:]]
        if "link_id" in d.columns:  # a linked transfer has two legs but is ONE movement of money
            total[2] -= d.loc[d["link_id"] != "", "amount"].abs().sum() / 2
        out.loc[len(out)] = ["Combined"] + total
    return out


def _default_nature(amount, contact):
    if contact:
        ctype = contact["type"]
        if ctype == "Own account":
            return "Own account"
        if ctype == "Debt party":
            return "Debt"
        if ctype == "Income source" and amount > 0:
            return "Income"
    return "Income" if amount > 0 else "None"


def _display_category(nature, amount, category, contact):
    if nature == "Pending":
        return "Pending approval"
    if nature == "Own account":
        return "Own transfer"
    if nature == "Debt":
        return "Debt"
    if amount > 0:
        return "Income" if nature == "Income" else "Other inflow"
    if contact and contact["type"] == "Expense" and contact.get("category"):
        return contact["category"]
    return category


def process(df, settings=None):
    """Tag every transaction, ignore transfers between your own accounts, hold anything that needs
    your approval, and keep the debt ledger in sync. Returns the tagged table.

    Adds: tx_id, legacy_id, account, nature, counterparty, reason, suggest, link_id, link_why, prop_id.
    May rewrite `category` (a registered landlord number -> Rent). Safe to call repeatedly.

    Transfers between your accounts (same amount out of one, into another, within a few days, with
    some evidence in the wording) are linked and treated as 'Own account': counted nowhere.

    Held ("Pending") until YOU decide, and counted nowhere in the meantime:
      * any amount above the approval limit (default 10,000), money in or out
        (a linked transfer between your own accounts is exempt: it is excluded anyway)
      * incoming money that looks like a debt payment (from someone who owes you, or from a
        registered debt contact); `suggest` names that person
      * every new incoming payment, if you switch on "review all incoming money"
    Your own tags always win. Money YOU send to someone you owe is still matched automatically.
    """
    df = add_ids(df)
    settings = settings or load_settings()
    limit, review_all = float(settings["approval_threshold"]), bool(settings["review_all_incoming"])
    contacts, tags, debts = load_contacts(), load_tags(), load_debts()
    accounts = load_accounts()

    # Numbers registered on an account mean "my own account" when they appear in OTHER accounts' statements.
    acc_contacts = [{"identifier": str(i).strip(), "label": f"My {name}", "type": "Own account",
                     "category": "", "owner": name}
                    for name, info in accounts.items() for i in info.get("identifiers", []) if str(i).strip()]

    def contact_for(r):
        c = find_contact(r.description, contacts)
        if c:
            return c
        return next((a for a in acc_contacts if a["owner"] != r.account and matches(r.description, a["identifier"])), None)

    found = {r.tx_id: contact_for(r) for r in df.itertuples()}
    own_ids = {t for t, c in found.items() if c and c["type"] == "Own account"}
    links, proposals = find_transfers(df, accounts, own_ids, load_transfers(), int(settings["transfer_window_days"]))
    prop_of = {}
    for p in proposals:
        prop_of[p["out"]] = prop_of[p["in"]] = p["key"]

    natures, parties, cats, reasons, suggests, explicit = [], [], [], [], [], []
    for r in df.itertuples():
        c = found[r.tx_id]
        nature = _default_nature(r.amount, c)
        was_debt = nature == "Debt"
        party = (c.get("label") or c["identifier"]) if c else guess_counterparty(r.description)
        reason, suggest = ("contact rule" if c else ""), ""
        tag = tags.get(r.tx_id) or (tags.get(r.legacy_id) if r.legacy_id != r.tx_id else None)
        if tag and tag.get("auto") and r.amount > 0:
            tag = None  # older versions auto-applied incoming repayments; they now need your approval
        if tag:
            nature = tag["nature"]
            party = tag.get("counterparty") or party
            reason = "auto: debt repayment" if tag.get("auto") else ("approved by you" if tag.get("approved") else "your tag")
        elif r.tx_id in links:
            nature = "Own account"
            reason = f"transfer {'to' if r.amount < 0 else 'from'} {links[r.tx_id]['account']}"
        elif abs(r.amount) > limit:
            nature, reason = "Pending", f"Over {limit:,.0f}: allocate it"
        elif r.amount > 0 and was_debt:
            nature, reason = "Pending", "From a debt contact: approve it"
        elif r.amount > 0 and review_all and nature != "Own account":
            nature, reason = "Pending", "New incoming money: approve it"
        if nature == "Pending" and r.amount > 0 and was_debt:
            suggest = party
        if r.amount < 0 and nature == "Income":
            nature = "None"
        natures.append(nature)
        parties.append(party)
        reasons.append(reason)
        suggests.append(suggest)
        cats.append(_display_category(nature, r.amount, r.category, c))
        explicit.append(bool(tag))

    df["nature"], df["counterparty"], df["category"] = natures, parties, cats
    df["reason"], df["suggest"], df["_explicit"] = reasons, suggests, explicit
    df["link_id"] = [links.get(t, {}).get("key", "") for t in df["tx_id"]]
    df["link_why"] = [links.get(t, {}).get("why", "") for t in df["tx_id"]]
    df["prop_id"] = [prop_of.get(t, "") for t in df["tx_id"]]

    # Open debts as they will stand once this statement is applied
    nat = dict(zip(df["tx_id"], df["nature"]))
    live = [e for i, e in debts.items() if e.get("source") == "manual" or nat.get(i, "Debt") == "Debt"]
    balances = {}
    for e in sorted(live, key=lambda e: e["date"]):
        k = _key(e["counterparty"])
        balances[k] = (e["counterparty"], balances.get(k, (None, 0.0))[1] + e["amount"])

    def open_debt_for(name):
        ck = _key(name)
        return next((k for k, (_, b) in balances.items()
                     if abs(b) > TOL and ck and (k == ck or (min(len(k), len(ck)) >= 3 and (k in ck or ck in k)))), None)

    new_tags = {}
    todo = df[(~df["_explicit"]) & (~df["nature"].isin(["Own account", "Debt"]))]
    for idx, r in todo.sort_values("date").iterrows():
        hit = open_debt_for(r["counterparty"])
        if hit is None:
            continue
        shown, bal = balances[hit]
        amt = r["amount"]
        if amt > 0 and bal < -TOL:
            # They owe me and have paid (whatever the amount): ASK before touching the debt
            df.at[idx, "suggest"] = shown
            if r["nature"] != "Pending":
                df.at[idx, "nature"], df.at[idx, "category"] = "Pending", "Pending approval"
                df.at[idx, "reason"] = f"Looks like a debt payment from {shown}: approve it"
        elif amt < 0 and r["nature"] != "Pending" and bal > TOL and -amt <= bal + 1:
            # I paid someone I owe: matched automatically (and saved), as before
            balances[hit] = (shown, bal + amt)
            new_tags[r["tx_id"]] = {"nature": "Debt", "counterparty": shown, "auto": True}
            df.at[idx, "nature"], df.at[idx, "counterparty"] = "Debt", shown
            df.at[idx, "category"], df.at[idx, "reason"] = "Debt", "auto: debt repayment"
    if new_tags:
        tags.update(new_tags)
        _save("tags", tags)

    df = df.drop(columns="_explicit")
    sync_debts(df)
    return df


def pending_view(df):
    """Transactions being held for your decision."""
    return df[df["nature"] == "Pending"]


def allocate(changes, current):
    """Record your decisions. changes = [(tx_id, nature, counterparty), ...]; current is indexed by tx_id.
    Returns (saved, skipped): skipped = rows refused (e.g. money you sent can't be Income)."""
    tags, saved, skipped = load_tags(), 0, 0
    for tid, nature, party in changes:
        if tid not in current.index or nature not in NATURES:
            skipped += 1
            continue
        if current.at[tid, "amount"] < 0 and nature == "Income":
            skipped += 1
            continue
        tags[tid] = {"nature": nature, "approved": True,
                     "counterparty": str(party or "").strip() or current.at[tid, "counterparty"]}
        saved += 1
    _save("tags", tags)
    return saved, skipped


def save_tag_edits(edited, current):
    """Store your manual changes. Both frames are indexed by tx_id. Returns (changed, corrected)."""
    tags, changed, corrected = load_tags(), 0, 0
    for tid, row in edited.iterrows():
        if tid not in current.index:
            continue
        nature = row["nature"]
        party = str(row.get("counterparty") or "").strip()
        if nature == "Pending":
            continue  # held items are decided in the approval queues, not here
        if current.at[tid, "amount"] < 0 and nature == "Income":
            nature, corrected = "None", corrected + 1  # money you sent can't be income
        if nature != current.at[tid, "nature"] or party != current.at[tid, "counterparty"]:
            tags[tid] = {"nature": nature, "counterparty": party}
            changed += 1
    _save("tags", tags)
    return changed, corrected


def counted_view(df):
    """Only what counts for budgeting: income-tagged money in, 'None'-tagged money out."""
    money_in = (df["amount"] > 0) & (df["nature"] == "Income")
    money_out = (df["amount"] < 0) & (df["nature"] == "None")
    return df[money_in | money_out]


def exclusion_summary(df):
    """What was left out of the budget numbers, and why."""
    def total(mask):
        return float(df.loc[mask, "amount"].abs().sum())
    own = total(df["nature"] == "Own account")
    if "link_id" in df.columns:  # a linked transfer has two legs but is one movement of money
        own -= float(df.loc[df["link_id"] != "", "amount"].abs().sum()) / 2
    items = {
        "Debt money in": total((df["nature"] == "Debt") & (df["amount"] > 0)),
        "Debt money out": total((df["nature"] == "Debt") & (df["amount"] < 0)),
        "Own-account transfers": own,
        "Inflows tagged None": total((df["nature"] == "None") & (df["amount"] > 0)),
        "Awaiting your approval": total(df["nature"] == "Pending"),
    }
    return {k: v for k, v in items.items() if v > 0}


def as_of_for_month(df, month):
    """Last day to treat as 'so far': month-end for past months, latest transaction for the newest."""
    if month < df["month"].max():
        y, m = map(int, month.split("-"))
        return pd.Timestamp(y, m, calendar.monthrange(y, m)[1])
    return df.loc[df["month"] == month, "date"].max()


# ---------------------------------------------------------------- debts

def load_debts():
    return _load("debts", {})


def sync_debts(df):
    """Make the ledger match the Debt-tagged transactions (adds new, removes re-tagged)."""
    debts, changed = load_debts(), False
    for r in df.itertuples():
        old = r.legacy_id
        if old != r.tx_id and debts.get(old, {}).get("source") == "transaction":
            del debts[old]  # saved by an earlier version under the account-less id: migrate
            changed = True
        if r.nature == "Debt":
            entry = {"date": r.date.strftime("%Y-%m-%d"), "counterparty": r.counterparty,
                     "amount": float(r.amount), "note": r.description, "source": "transaction"}
            if debts.get(r.tx_id) != entry:
                debts[r.tx_id], changed = entry, True
        elif r.tx_id in debts and debts[r.tx_id].get("source") == "transaction":
            del debts[r.tx_id]
            changed = True
    if changed:
        _save("debts", debts)


def add_manual_debt(counterparty, action, amount, when, note=""):
    debts = load_debts()
    debts["m-" + uuid.uuid4().hex[:10]] = {
        "date": str(when), "counterparty": counterparty.strip(), "amount": DEBT_ACTIONS[action] * abs(float(amount)),
        "note": note, "source": "manual",
    }
    _save("debts", debts)


def delete_debt_entry(entry_id):
    debts = load_debts()
    if entry_id in debts and debts[entry_id].get("source") == "manual":
        del debts[entry_id]
        _save("debts", debts)


def debt_entries(debts=None):
    debts = load_debts() if debts is None else debts
    rows = [dict(id=i, **e) for i, e in debts.items()]
    cols = ["id", "date", "counterparty", "amount", "source", "note"]
    return pd.DataFrame(rows, columns=cols).sort_values("date", ascending=False).reset_index(drop=True)


def debt_summary(debts=None):
    """One row per person: who owes whom, how much is left, and how far along repayment is."""
    entries = debt_entries(debts)
    cols = ["Counterparty", "Status", "Outstanding", "Original", "Repaid", "Progress", "Last activity"]
    if entries.empty:
        return pd.DataFrame(columns=cols)
    entries["key"] = entries["counterparty"].map(_key)
    rows = []
    for _, g in entries.groupby("key"):
        pos = float(g.loc[g["amount"] > 0, "amount"].sum())
        neg = abs(float(g.loc[g["amount"] < 0, "amount"].sum()))
        bal = pos - neg
        name = g.sort_values("date").iloc[-1]["counterparty"]
        if bal > TOL:
            status, original, repaid = "I owe", pos, neg
        elif bal < -TOL:
            status, original, repaid = "Owed to me", neg, pos
        else:
            status, original, repaid = "Settled", max(pos, neg), max(pos, neg)
        rows.append({"Counterparty": name, "Status": status, "Outstanding": abs(bal) if status != "Settled" else 0.0,
                     "Original": original, "Repaid": repaid,
                     "Progress": min(repaid / original, 1.0) if original else 1.0,
                     "Last activity": g["date"].max()})
    return pd.DataFrame(rows, columns=cols).sort_values("Outstanding", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------- bills

def load_bills():
    return _load("bills", [])


def save_bills(rows):
    clean = []
    for r in rows:
        name = str(r.get("name") or "").strip()
        if not name or name.lower() == "nan":
            continue
        match = str(r.get("match") or "").strip()
        amount = pd.to_numeric(r.get("amount"), errors="coerce")
        due = pd.to_numeric(r.get("due_day"), errors="coerce")
        clean.append({
            "name": name, "match": "" if match.lower() == "nan" else match,
            "amount": 0.0 if pd.isna(amount) else float(amount),
            "due_day": 1 if pd.isna(due) else int(min(max(due, 1), 31)),
        })
    _save("bills", clean)
    return len(clean)


def bill_status(bills, month_df, month, as_of):
    """Check each bill against this month's payments. Updates itself as new data is loaded."""
    cols = ["Bill", "Expected", "Paid", "Remaining", "Due", "Status", "Paid on"]
    y, m = map(int, month.split("-"))
    out = []
    for b in bills:
        due = date(y, m, min(b["due_day"], calendar.monthrange(y, m)[1]))
        needle = b.get("match") or b["name"]
        sent = month_df[(month_df["amount"] < 0) & (~month_df["nature"].isin(["Own account", "Pending"]))]
        paid_rows = sent[sent["description"].map(lambda d: matches(d, needle)).astype(bool)]  # astype: safe when empty
        paid = abs(float(paid_rows["amount"].sum()))
        expected = float(b.get("amount") or 0)
        if (expected > 0 and paid >= expected - 1) or (expected == 0 and paid > 0):
            status = "Paid"
        elif paid > 0:
            status = "Partial"
        elif pd.Timestamp(as_of).date() > due:
            status = "Overdue"
        elif (due - pd.Timestamp(as_of).date()).days <= 5:
            status = "Due soon"
        else:
            status = "Upcoming"
        out.append({"Bill": b["name"], "Expected": expected, "Paid": paid,
                    "Remaining": max(expected - paid, 0.0), "Due": due.strftime("%d %b"),
                    "Status": status,
                    "Paid on": paid_rows["date"].max().strftime("%d %b") if not paid_rows.empty else ""})
    return pd.DataFrame(out, columns=cols)
