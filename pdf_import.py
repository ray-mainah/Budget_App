"""
Read bank / M-Pesa statement PDFs into a transactions table (date, description, amount).

How it works
  1. Pull the text out of every page (pdfplumber; handles password-protected PDFs).
  2. Find lines that start with a date and end with money columns (e.g. "3,000.00").
  3. Work out money-in vs money-out from the running balance when the statement has one
     (most do), because that is far more reliable than guessing from the wording.

Statement layouts vary a lot. If a PDF reads wrongly, send me a few of its lines and I'll
tailor the parser. Scanned (image-only) PDFs have no text and can't be read without OCR.
"""

import re

import pandas as pd


class PdfError(Exception):
    """A readable problem message for the user (wrong password, scanned PDF, ...)."""


_DATE = (
    r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?"
    r"|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}(?:\s+\d{2}:\d{2}(?::\d{2})?)?"
    r"|\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{2,4}(?:\s+\d{2}:\d{2}(?::\d{2})?)?"
)
_DATE_RE = re.compile(f"({_DATE})")
_AMOUNT_RE = re.compile(r"^\(?-?\d{1,3}(?:,\d{3})*\.\d{1,2}\)?(?:CR|DR|Cr|Dr)?$|^\(?-?\d+\.\d{1,2}\)?(?:CR|DR|Cr|Dr)?$")
_SKIP_RE = re.compile(
    r"page \d+|statement|summary|total|brought forward|opening|closing|receipt no|completion time",
    re.I,
)
_IN_WORDS = ("received", "deposit", "salary", "credit", "refund", "reversal", "interest", "from -")
_STATUS_OK = {"COMPLETED", "SUCCESS", "SUCCESSFUL"}
_STATUS_BAD = {"FAILED", "CANCELLED", "CANCELED", "DECLINED"}


def _extract_lines(file, password):
    try:
        import pdfplumber
    except ImportError as e:
        raise PdfError("PDF support needs one more package. Run: pip install pdfplumber") from e
    try:
        with pdfplumber.open(file, password=password or None) as pdf:
            pages = [(p.extract_text() or "") for p in pdf.pages]
    except Exception as e:  # pdfminer raises PDFPasswordIncorrect with an empty message
        if "assword" in f"{type(e).__name__} {e!r} {e}":  # pdfplumber wraps pdfminer's PDFPasswordIncorrect
            raise PdfError(
                "This PDF is password-protected (or the password is wrong). "
                "Enter the password in the sidebar. M-Pesa sends it to you by SMS."
            ) from e
        raise PdfError(f"Couldn't read that PDF: {e}") from e
    lines = [ln.strip() for text in pages for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise PdfError("No text found in that PDF. It may be a scan (an image), which needs OCR.")
    return lines


def _to_float(token):
    """Magnitude only. Whether money went in or out is decided from the running balance."""
    return float(re.sub(r"[^\d.]", "", token))


def _pop_amounts(tokens):
    nums = []
    while tokens and len(nums) < 3 and _AMOUNT_RE.match(tokens[-1]):
        nums.insert(0, _to_float(tokens.pop()))
    return nums


def _parse_lines(lines):
    rows, pending = [], None  # pending = a dated line whose amounts wrapped onto the next line
    for ln in lines:
        m = _DATE_RE.search(ln)
        if m and m.start() <= 20:
            pending = None
            rest = ln[m.end():].split()
            nums = _pop_amounts(rest)
            status = ""
            if rest and rest[-1].upper() in _STATUS_OK | _STATUS_BAD:
                status = rest.pop().upper()
            if status in _STATUS_BAD:
                continue
            row = {"date": m.group(1), "desc": " ".join(rest), "nums": nums}
            if nums:
                rows.append(row)
            else:
                pending = row
        elif not _SKIP_RE.search(ln):
            tokens = ln.split()
            nums = _pop_amounts(tokens)
            if pending is not None and nums:
                pending["desc"] += " " + " ".join(tokens)
                pending["nums"] = nums
                rows.append(pending)
                pending = None
            elif tokens and not nums:
                target = pending if pending is not None else (rows[-1] if rows else None)
                if target is not None and len(target["desc"]) < 160:
                    target["desc"] += " " + ln  # wrapped description line
    return rows


def parse_one_date(value):
    """Parse a single date string: ISO first, then day-first. Per-value, so mixed styles survive."""
    for kwargs in ({"format": "ISO8601"}, {"dayfirst": True}):
        try:
            return pd.to_datetime(value, errors="raise", **kwargs)
        except (ValueError, TypeError):
            continue
    return pd.NaT


def _parse_dates(strings):
    return [parse_one_date(s) for s in strings]


def _guess_sign(desc):
    low = desc.lower()
    return 1.0 if any(w in low for w in _IN_WORDS) else -1.0


def _resolve(rows):
    """Turn raw rows into signed amounts, using running balances to tell in from out."""
    dates = [r["when"] for r in rows]
    newest_first = len(rows) > 1 and dates[0] > dates[-1]
    seq = rows[::-1] if newest_first else rows

    out, prev_bal, unsure = [], None, 0
    for r in seq:
        nums = r["nums"]
        if len(nums) >= 3:                       # [paid in, withdrawn, balance] (M-Pesa style)
            a, b, bal = nums[-3:]
            amount = None
            if prev_bal is not None:
                delta = bal - prev_bal
                if abs(delta - (a - b)) < 0.02:
                    amount = a - b
                elif abs(delta - (b - a)) < 0.02:  # [debit, credit, balance] layout
                    amount = b - a
            if amount is None:
                amount = a - b
                unsure += 1 if prev_bal is not None else 0
            prev_bal = bal
        elif len(nums) == 2:                     # [amount, balance]
            amt, bal = nums
            if prev_bal is not None and abs(bal - prev_bal) > 0.005:
                amount = abs(amt) if bal > prev_bal else -abs(amt)
            else:
                amount = abs(amt) * _guess_sign(r["desc"])
                unsure += 1
            prev_bal = bal
        else:                                    # [amount] only: wording is all we have
            amount = abs(nums[0]) * _guess_sign(r["desc"])
            unsure += 1
        out.append({"date": r["when"], "description": r["desc"].strip(), "amount": amount})
    return out, unsure


def load_pdf(file, password=None):
    """Return (DataFrame[date, description, amount], notes). `file` is a path or file-like."""
    rows = _parse_lines(_extract_lines(file, password))
    if not rows:
        raise PdfError(
            "I couldn't find any transaction rows in that PDF. Rows need a date and amounts "
            "like 1,500.00. Send me a few lines from it and I'll adapt the reader."
        )
    when = _parse_dates([r["date"] for r in rows])
    rows = [dict(r, when=w) for r, w in zip(rows, when) if pd.notna(w)]
    if not rows:
        raise PdfError("Found rows but couldn't understand their dates.")

    resolved, unsure = _resolve(rows)
    df = pd.DataFrame(resolved)
    notes = []
    if unsure:
        notes.append(
            f"{unsure} row(s) had no running balance to confirm money in vs out, so I guessed from "
            "the wording. Please check them in the Transactions tab."
        )
    return df, notes
