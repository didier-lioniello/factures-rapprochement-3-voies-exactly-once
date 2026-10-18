"""Fonctions partagées : normalisation, contrôles déterministes, empreinte.
Mêmes règles que sql/schema.sql (fn_norm_invoice_number, fn_fingerprint, fn_iban_valid, fn_siret_valid)."""
import hashlib, re
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

CRITICAL_FIELDS = ["doc_type", "supplier_siret", "invoice_number", "invoice_date", "po_number",
                   "related_invoice_number", "total_ht", "total_tva", "total_ttc", "iban", "lines"]

def money(x):
    return None if x is None else Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def norm_invoice_number(n):
    if n is None: return None
    s = re.sub(r"(^|[^0-9])0+([0-9])", r"\1\2", n.upper())
    return re.sub(r"[^A-Z0-9]", "", s)

def norm_date(s):
    if s is None: return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try: return datetime.strptime(s, fmt).date().isoformat()
        except ValueError: pass
    return None

def norm_iban(s):
    return None if s is None else re.sub(r"\s", "", s).upper()

def norm_lines(lines):
    if lines is None: return None
    return [(l["sku"], Decimal(str(l["qty"])), money(l["unit_price_ht"]), Decimal(str(l["vat_rate"])))
            for l in lines]

def norm_field(name, value):
    if value is None: return None
    if name in ("total_ht", "total_tva", "total_ttc"): return money(value)
    if name in ("invoice_number", "related_invoice_number"): return norm_invoice_number(value)
    if name == "invoice_date": return norm_date(value)
    if name == "iban": return norm_iban(value)
    if name == "lines": return norm_lines(value)
    return value

def iban_valid(iban):
    s = norm_iban(iban or "")
    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}", s): return False
    s = s[4:] + s[:4]
    return int("".join(str(int(c, 36)) for c in s)) % 97 == 1

def siret_valid(siret):
    if not siret or not re.fullmatch(r"[0-9]{14}", siret): return False
    if siret.startswith("356000000"):                       # La Poste
        return sum(int(c) for c in siret) % 5 == 0
    total = 0
    for i, c in enumerate(reversed(siret)):
        d = int(c)
        if i % 2: d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0

def fingerprint(f):
    canon = "|".join([f["supplier_siret"][:9], f["doc_type"], norm_invoice_number(f["invoice_number"]),
                      str(int(money(f["total_ttc"]) * 100)), norm_date(f["invoice_date"])])
    return hashlib.sha256(canon.encode()).hexdigest(), canon
