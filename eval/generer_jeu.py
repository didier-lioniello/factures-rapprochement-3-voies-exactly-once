#!/usr/bin/env python3
"""Génère le jeu d'évaluation SYNTHÉTIQUE (exemple de portfolio).

Toutes les entreprises, SIRET, IBAN et montants sont FICTIFS.
Ce jeu sert à démontrer la méthode de mesure, pas à annoncer une précision réelle.
Sorties (dans donnees/) :
  references.json      fournisseurs, bons de commande, bons de réception
  verite_terrain.json  ce qui est réellement imprimé sur chaque facture + décision attendue
  extraction_a.json    sortie simulée de l'extracteur A (OCR + règles)
  extraction_b.json    sortie simulée de l'extracteur B (LLM vision)
"""
import copy, hashlib, json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

OUT = Path(__file__).parent / "donnees"
D = Decimal
def r2(x): return D(x).quantize(D("0.01"), rounding=ROUND_HALF_UP)

SUPPLIERS = {
    "S1": {"name": "Atelier Fictif Martin SAS", "siret": "11111111100014", "iban": "FR4830001000010000000001101"},
    "S2": {"name": "Fournitures Exemple SARL",  "siret": "22222222200010", "iban": "FR8330001000020000000002202"},
    "S3": {"name": "Logistique Démo SA",        "siret": "33333333300016", "iban": "FR2130001000030000000003303"},
    "S4": {"name": "Pièces Factices SAS",       "siret": "44444444400012", "iban": "FR5630001000040000000004404"},
}

PURCHASE_ORDERS = {
    "PO-1001": ("S1", [("PANNEAU-CTP", 40, "32.50", "20"), ("VIS-M8", 500, "0.45", "20")]),
    "PO-1002": ("S1", [("CHARNIERE", 60, "3.80", "20")]),
    "PO-1003": ("S1", [("GANTS", 100, "1.20", "20")]),
    "PO-2001": ("S2", [("PAPIER-A4", 50, "4.20", "20"), ("CAFE-1KG", 10, "14.90", "5.5")]),
    "PO-2002": ("S2", [("CLASSEUR", 100, "2.10", "20")]),
    "PO-2003": ("S2", [("CARTOUCHE-TONER", 12, "45.00", "20")]),
    "PO-3001": ("S3", [("TRANSPORT", 1, "850.00", "20")]),
    "PO-3002": ("S3", [("MACHINE-X", 1, "5000.00", "20")]),
    "PO-4001": ("S4", [("COURROIE", 25, "18.40", "20")]),
    "PO-4002": ("S4", [("ROULEMENT", 20, "61.50", "20")]),
    "PO-4003": ("S4", [("FILTRE-HUILE", 30, "7.80", "20")]),
}

# Réceptions (sku -> quantité reçue). PO-2003 : 8 cartouches reçues sur 12.
RECEIPTS = {
    "PO-1001": {"PANNEAU-CTP": 40, "VIS-M8": 500}, "PO-1002": {"CHARNIERE": 60},
    "PO-1003": {"GANTS": 100}, "PO-2001": {"PAPIER-A4": 50, "CAFE-1KG": 10},
    "PO-2002": {"CLASSEUR": 100}, "PO-2003": {"CARTOUCHE-TONER": 8},
    "PO-3001": {"TRANSPORT": 1}, "PO-3002": {"MACHINE-X": 1},
    "PO-4001": {"COURROIE": 25}, "PO-4002": {"ROULEMENT": 20}, "PO-4003": {"FILTRE-HUILE": 30},
}

def invoice(fid, sup, number, date, po, lines, doc_type="facture", related=None,
            iban=None, siret=None, ttc_override=None, expected="auto", scenario=""):
    s = SUPPLIERS[sup]
    ls = [{"sku": k, "qty": q, "unit_price_ht": float(D(p)), "vat_rate": float(D(v))} for k, q, p, v in lines]
    ht = sum(r2(D(str(l["qty"])) * D(str(l["unit_price_ht"]))) for l in ls)
    by_rate = {}
    for l in ls:
        by_rate.setdefault(l["vat_rate"], D(0))
        by_rate[l["vat_rate"]] += r2(D(str(l["qty"])) * D(str(l["unit_price_ht"])))
    tva = sum(r2(base * D(str(rate)) / 100) for rate, base in by_rate.items())
    ttc = ht + tva if ttc_override is None else D(ttc_override)
    return {
        "id": fid, "scenario": scenario, "expected_decision": expected,
        "source_message_id": f"msg-{fid}", "file_sha256": hashlib.sha256(fid.encode()).hexdigest(),
        "fields": {
            "doc_type": doc_type, "supplier_name": s["name"], "supplier_siret": siret or s["siret"],
            "invoice_number": number, "invoice_date": date, "po_number": po,
            "related_invoice_number": related, "currency": "EUR", "lines": ls,
            "total_ht": float(ht), "total_tva": float(tva), "total_ttc": float(ttc),
            "iban": iban or s["iban"],
        },
    }

T = []
T.append(invoice("F01", "S1", "FA-2026/0042", "2026-09-30", "PO-1001",
                 [("PANNEAU-CTP", 40, "32.50", "20"), ("VIS-M8", 500, "0.45", "20")],
                 scenario="Nominal : facture simple, tout concorde."))
T.append(invoice("F02", "S2", "2026-1187", "2026-10-01", "PO-2001",
                 [("PAPIER-A4", 50, "4.20", "20"), ("CAFE-1KG", 10, "14.90", "5.5")],
                 scenario="Nominal : deux taux de TVA sur la même facture."))
f03 = copy.deepcopy(T[0]); f03.update(id="F03", source_message_id="msg-F03",
     file_sha256=hashlib.sha256(b"F03-reexport").hexdigest(), expected_decision="doublon",
     scenario="Piège : F01 ré-exporté en PDF par le fournisseur (autre hash binaire, numéro écrit 'FA 2026 42').")
f03["fields"]["invoice_number"] = "FA 2026 42"
T.append(f03)
f04 = copy.deepcopy(T[1]); f04.update(id="F04", expected_decision="doublon",
     scenario="Piège : relance du webhook, même message et même fichier que F02.")
T.append(f04)
T.append(invoice("F05", "S3", "LD-77812", "2026-10-02", "PO-3001", [("TRANSPORT", 1, "850.00", "20")],
                 ttc_override="1120.00", expected="revue",
                 scenario="Piège : erreur de TVA du fournisseur, HT + TVA ≠ TTC (1 020 attendu, 1 120 imprimé)."))
T.append(invoice("F06", "S4", "PF-2026-0310", "2026-10-03", "PO-4001", [("COURROIE", 25, "18.40", "20")],
                 iban="FR5630001000040000000004405", expected="revue",
                 scenario="Piège : IBAN imprimé avec une clé invalide (mod 97 ≠ 1)."))
T.append(invoice("F07", "S2", "2026-1203", "2026-10-04", "PO-2002", [("CLASSEUR", 100, "2.10", "20")],
                 iban="FR2830004000990000000009909", expected="revue",
                 scenario="Piège : IBAN valide mais différent de l'IBAN connu du fournisseur (schéma de fraude classique)."))
T.append(invoice("F08", "S1", "AV-2026/0007", "2026-10-05", "PO-1001", [("PANNEAU-CTP", -4, "32.50", "20")],
                 doc_type="avoir", related="FA-2026/0042",
                 scenario="Avoir : 4 panneaux abîmés, montants négatifs, lié à F01."))
T.append(invoice("F09", "S3", "LD-77901", "2026-10-06", "PO-3002", [("ACOMPTE", 1, "1500.00", "20")],
                 doc_type="acompte", scenario="Acompte de 30 % avant livraison : pas de bon de réception attendu."))
T.append(invoice("F10", "S3", "LD-78044", "2026-10-12", "PO-3002",
                 [("MACHINE-X", 1, "5000.00", "20"), ("ACOMPTE-DEDUIT", 1, "-1500.00", "20")],
                 doc_type="facture_solde", related="LD-77901",
                 scenario="Facture de solde : déduit l'acompte F09 (ligne négative qui casse un contrôle naïf)."))
T.append(invoice("F11", "S4", "PF-2026-0322", "2026-10-07", "PO-4002", [("ROULEMENT", 20, "61.50", "20")],
                 scenario="Facture correcte, mais les deux extracteurs divergent sur le TTC (A lit 1 478,00)."))
T.append(invoice("F12", "S1", "FA-2026/0051", "2026-10-08", "PO-1002", [("CHARNIERE", 60, "3.80", "20")],
                 scenario="Facture correcte, mais les DEUX extracteurs lisent 3,30 au lieu de 3,80 (même confusion OCR) : JSON valide, montant faux."))
T.append(invoice("F13", "S2", "2026-1219", "2026-10-09", "PO-2003", [("CARTOUCHE-TONER", 12, "45.00", "20")],
                 expected="revue", scenario="Piège : 12 cartouches facturées, 8 reçues (rapprochement 3 voies en échec)."))
T.append(invoice("F14", "S4", "PF-2026-0330", "2026-10-10", "PO-4003", [("FILTRE-HUILE", 30, "7.83", "20")],
                 scenario="Écart de prix de 0,38 % avec le bon de commande : dans la tolérance de 1 %."))
T.append(invoice("F15", "S1", "FA-2026/0060", "2026-10-11", "PO-1003", [("GANTS", 100, "1.20", "20")],
                 siret="11111111100015", expected="revue",
                 scenario="Piège : SIRET imprimé avec une faute de frappe (clé de Luhn invalide)."))

# --- Extracteurs simulés : variations de format + erreurs injectées --------
def fr_date(iso): y, m, d = iso.split("-"); return f"{d}/{m}/{y}"
A, B = [], []
for t in T:
    a = {"id": t["id"], "source_message_id": t["source_message_id"], "file_sha256": t["file_sha256"],
         "fields": copy.deepcopy(t["fields"])}
    b = copy.deepcopy(a)
    b["fields"]["invoice_date"] = fr_date(b["fields"]["invoice_date"])            # format différent
    b["fields"]["iban"] = " ".join(b["fields"]["iban"][i:i+4] for i in range(0, len(b["fields"]["iban"]), 4))
    b["fields"]["supplier_name"] = b["fields"]["supplier_name"].upper()
    if t["id"] == "F11":
        a["fields"]["total_ttc"] = 1478.00
    if t["id"] == "F12":
        a["fields"]["lines"][0]["unit_price_ht"] = 3.30
        b["fields"]["lines"][0]["unit_price_ht"] = 3.30
    A.append(a); B.append(b)

refs = {
    "_avertissement": "Données entièrement fictives, générées pour l'exemple.",
    "suppliers": {s["siret"][:9]: {"name": s["name"], "known_ibans": [s["iban"]]} for s in SUPPLIERS.values()},
    "purchase_orders": {po: {"supplier_siren": SUPPLIERS[sup]["siret"][:9],
                             "lines": [{"sku": k, "qty": q, "unit_price_ht": float(D(p)), "vat_rate": float(D(v))}
                                       for k, q, p, v in lines]}
                        for po, (sup, lines) in PURCHASE_ORDERS.items()},
    "goods_receipts": RECEIPTS,
}
OUT.mkdir(exist_ok=True)
header = "JEU SYNTHÉTIQUE D'EXEMPLE - aucune donnée réelle, aucun taux de précision réel ne peut en être tiré."
for name, data in [("references.json", refs),
                   ("verite_terrain.json", {"_avertissement": header, "invoices": T}),
                   ("extraction_a.json", {"_avertissement": header, "extracteur": "A (OCR + règles, simulé)", "invoices": A}),
                   ("extraction_b.json", {"_avertissement": header, "extracteur": "B (LLM vision, simulé)", "invoices": B})]:
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"{len(T)} factures synthétiques écrites dans {OUT}")
