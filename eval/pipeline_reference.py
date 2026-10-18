#!/usr/bin/env python3
"""Implémentation de référence (hors n8n) de la logique de décision, pour l'évaluation.

  python3 pipeline_reference.py --mode complet   # double extraction + vote + contrôles + 3 voies + empreinte
  python3 pipeline_reference.py --mode naif      # un seul extracteur, dédoublonnage par hash du PDF, aucun contrôle

Le mode naïf représente l'automatisation "démo" typique, pour montrer ce que la métrique
"erreurs silencieuses" détecte. Les sorties vont dans sorties/.
"""
import argparse, json
from decimal import Decimal
from pathlib import Path
from commun import (CRITICAL_FIELDS, money, norm_field, norm_iban, norm_invoice_number,
                    iban_valid, siret_valid, fingerprint)

HERE = Path(__file__).parent
PRICE_TOLERANCE = Decimal("0.01")      # 1 % d'écart de prix unitaire toléré vs bon de commande
ACOMPTE_MAX_RATIO = Decimal("0.40")    # un acompte ne dépasse pas 40 % du HT commandé
AUTO_THRESHOLD = Decimal("0.90")
CENT = Decimal("0.01")

def load(name): return json.loads((HERE / "donnees" / name).read_text(encoding="utf-8"))

def jsonable(v):
    if isinstance(v, Decimal): return float(v)
    if isinstance(v, list): return [jsonable(x) for x in v]
    if isinstance(v, tuple): return [jsonable(x) for x in v]
    return v

def vote(a, b):
    """Vote champ par champ : la valeur n'est retenue que si les deux extracteurs concordent."""
    out, disagreements = {}, []
    for k in CRITICAL_FIELDS:
        va, vb = norm_field(k, a.get(k)), norm_field(k, b.get(k))
        if va == vb:
            out[k] = a.get(k) if k not in ("invoice_date", "iban") else (va)
        else:
            out[k] = None; disagreements.append(k)
    out["supplier_name"] = a.get("supplier_name")
    return out, disagreements

class Ledger:
    """État côté base : empreintes réservées, quantités facturées, acomptes, factures écrites."""
    def __init__(self):
        self.seen_sources, self.seen_files, self.fingerprints = set(), set(), {}
        self.invoiced_qty = {}          # (po, sku) -> qté nette facturée
        self.acomptes_ht = {}           # po -> HT d'acomptes écrits
        self.posted = {}                # (siren, numéro normalisé) -> champs

def checks(f, refs, ledger):
    """Contrôles déterministes + rapprochement 3 voies. Retourne (raisons bloquantes, pénalités)."""
    reasons, penalties = [], []
    lines = norm_field("lines", f["lines"])
    ht, tva, ttc = money(f["total_ht"]), money(f["total_tva"]), money(f["total_ttc"])
    if sum((money(q * p) for _, q, p, _ in lines), Decimal(0)) != ht:
        reasons.append("somme des lignes ≠ total HT")
    by_rate = {}
    for _, q, p, r in lines: by_rate[r] = by_rate.get(r, Decimal(0)) + money(q * p)
    tva_calc = sum((money(b * r / 100) for r, b in by_rate.items()), Decimal(0))
    if abs(tva_calc - tva) > CENT * len(by_rate): reasons.append("TVA recalculée ≠ TVA imprimée")
    if ht + tva != ttc: reasons.append("HT + TVA ≠ TTC")
    if not siret_valid(f["supplier_siret"]): reasons.append("SIRET invalide (Luhn)")
    if not iban_valid(f["iban"]): reasons.append("IBAN invalide (mod 97)")
    siren = (f["supplier_siret"] or "")[:9]
    sup = refs["suppliers"].get(siren)
    if sup is None: reasons.append("fournisseur inconnu")
    elif iban_valid(f["iban"]) and norm_iban(f["iban"]) not in sup["known_ibans"]:
        reasons.append("IBAN différent de l'IBAN connu du fournisseur")
    doc = f["doc_type"]
    if (doc == "avoir") != (ttc < 0): reasons.append("signe du montant incohérent avec le type de document")

    po = refs["purchase_orders"].get(f["po_number"])
    if po is None:
        reasons.append("bon de commande introuvable"); return reasons, penalties
    if po["supplier_siren"] != siren: reasons.append("bon de commande d'un autre fournisseur")
    po_lines = {l["sku"]: l for l in po["lines"]}
    po_ht = sum((money(Decimal(str(l["qty"])) * Decimal(str(l["unit_price_ht"]))) for l in po["lines"]), Decimal(0))
    received = refs["goods_receipts"].get(f["po_number"], {})

    if doc == "acompte":
        penalties.append(("acompte", Decimal("0.03")))
        if ht > po_ht * ACOMPTE_MAX_RATIO: reasons.append("acompte supérieur au plafond")
        return reasons, penalties
    if doc == "avoir":
        penalties.append(("avoir", Decimal("0.03")))
        orig = ledger.posted.get((siren, norm_invoice_number(f["related_invoice_number"])))
        if orig is None: reasons.append("avoir sans facture d'origine écrite")
        elif -ttc > money(orig["total_ttc"]): reasons.append("avoir supérieur à la facture d'origine")
    if doc == "facture_solde":
        penalties.append(("facture de solde", Decimal("0.03")))
        deducted = -sum((money(q * p) for s, q, p, _ in lines if s == "ACOMPTE-DEDUIT"), Decimal(0))
        if deducted != ledger.acomptes_ht.get(f["po_number"], Decimal(0)):
            reasons.append("acompte déduit ≠ acomptes déjà écrits")

    for sku, q, p, r in lines:
        if sku == "ACOMPTE-DEDUIT": continue
        pl = po_lines.get(sku)
        if pl is None: reasons.append(f"{sku} absent du bon de commande"); continue
        po_price = Decimal(str(pl["unit_price_ht"]))
        gap = abs(p - po_price) / po_price
        if gap > PRICE_TOLERANCE: reasons.append(f"{sku} : prix hors tolérance ({gap:.2%})")
        elif gap > 0: penalties.append((f"{sku} : écart de prix toléré {gap:.2%}", Decimal("0.05")))
        if r != Decimal(str(pl["vat_rate"])): reasons.append(f"{sku} : taux de TVA ≠ bon de commande")
        if doc != "avoir":
            already = ledger.invoiced_qty.get((f["po_number"], sku), Decimal(0))
            if already + q > Decimal(str(received.get(sku, 0))):
                reasons.append(f"{sku} : quantité facturée > quantité reçue")
    return reasons, penalties

def post(f, ledger):
    siren = f["supplier_siret"][:9]
    ledger.posted[(siren, norm_invoice_number(f["invoice_number"]))] = f
    for sku, q, p, _ in norm_field("lines", f["lines"]):
        if f["doc_type"] == "acompte":
            ledger.acomptes_ht[f["po_number"]] = ledger.acomptes_ht.get(f["po_number"], Decimal(0)) + money(q * p)
        elif sku != "ACOMPTE-DEDUIT":
            key = (f["po_number"], sku)
            ledger.invoiced_qty[key] = ledger.invoiced_qty.get(key, Decimal(0)) + q

def run(mode):
    refs, A, B = load("references.json"), load("extraction_a.json"), load("extraction_b.json")
    ledger, results = Ledger(), []
    for a, b in zip(A["invoices"], B["invoices"]):
        res = {"id": a["id"], "decision": None, "confidence": None, "reasons": [], "fingerprint": None, "fields": None}
        src = (a["source_message_id"], a["file_sha256"])
        if mode == "naif":
            if a["file_sha256"] in ledger.seen_files:
                res.update(decision="doublon", reasons=["même hash de fichier"]); results.append(res); continue
            ledger.seen_files.add(a["file_sha256"])
            res.update(decision="auto", confidence=1.0, fields=a["fields"]); results.append(res); continue

        if src in ledger.seen_sources:                         # relance webhook : UNIQUE(source, sha)
            res.update(decision="doublon", reasons=["relance : même message et même fichier"]); results.append(res); continue
        ledger.seen_sources.add(src)

        fields, disagreements = vote(a["fields"], b["fields"])
        res["fields"] = {k: jsonable(v) for k, v in fields.items()}
        if disagreements:
            res.update(decision="revue", confidence=0.0,
                       reasons=[f"désaccord des extracteurs sur : {', '.join(disagreements)}"])
            results.append(res); continue

        fp, canon = fingerprint(fields)                         # lecture seule : déjà vue ?
        res["fingerprint"] = fp
        if fp in ledger.fingerprints:
            res.update(decision="doublon", reasons=[f"empreinte déjà réservée par {ledger.fingerprints[fp]} ({canon})"])
            results.append(res); continue

        reasons, penalties = checks(fields, refs, ledger)
        conf = Decimal(1) - sum((p for _, p in penalties), Decimal(0)) if not reasons else Decimal(0)
        res["confidence"] = float(conf)
        res["reasons"] = reasons + [f"pénalité : {n}" for n, _ in penalties]
        if reasons or conf < AUTO_THRESHOLD:
            res["decision"] = "revue"; results.append(res); continue

        # Réservation AVANT tout effet de bord (en prod : INSERT ... ON CONFLICT DO NOTHING, cf. fn_reserve)
        ledger.fingerprints[fp] = a["id"]
        post(fields, ledger)
        res["decision"] = "auto"; results.append(res)
    return results

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["complet", "naif"], default="complet")
    args = ap.parse_args()
    out = HERE / "sorties" / f"sortie_{args.mode}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"_avertissement": "Sortie générée sur le jeu SYNTHÉTIQUE d'exemple.",
                               "mode": args.mode, "invoices": run(args.mode)},
                              ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"écrit : {out}")
