#!/usr/bin/env python3
"""Compare une sortie de pipeline à la vérité terrain et calcule les métriques.

  python3 metriques.py sorties/sortie_complet.json
  python3 metriques.py sorties/sortie_naif.json --json resultats.json --max-erreurs-silencieuses 0

Métriques :
  - exactitude par champ : valeur retenue == vérité (après normalisation) ; on distingue
    "faux" (valeur fausse affirmée, dangereux) et "non résolu" (null, envoyé en revue) ;
  - erreurs silencieuses : décision "auto" alors qu'un champ critique est faux, que la facture
    est un doublon ou qu'elle devait aller en revue ; ou faux doublon (facture perdue) ;
  - taux d'envoi en revue, et part des revues "évitables" (facture correcte envoyée en revue).

Le jeu fourni est SYNTHÉTIQUE : les chiffres obtenus illustrent la méthode, ce ne sont
PAS des taux de précision réels.
"""
import argparse, json, sys
from pathlib import Path
from commun import CRITICAL_FIELDS, norm_field

HERE = Path(__file__).parent

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sortie")
    ap.add_argument("--verite", default=str(HERE / "donnees" / "verite_terrain.json"))
    ap.add_argument("--json", help="écrire les métriques dans ce fichier")
    ap.add_argument("--max-erreurs-silencieuses", type=int, default=None,
                    help="code de sortie 1 si dépassé (utile en CI)")
    args = ap.parse_args()

    truth = {t["id"]: t for t in json.loads(Path(args.verite).read_text(encoding="utf-8"))["invoices"]}
    out = json.loads(Path(args.sortie).read_text(encoding="utf-8"))
    preds = {p["id"]: p for p in out["invoices"]}
    missing = sorted(set(truth) - set(preds))
    if missing: sys.exit(f"factures absentes de la sortie : {missing}")

    field_stats = {f: {"exact": 0, "faux": 0, "non_resolu": 0} for f in CRITICAL_FIELDS}
    silent, decisions = [], {"auto": 0, "revue": 0, "doublon": 0}
    avoidable_reviews, wrong_fields_by_invoice = [], {}

    for fid, t in truth.items():
        p = preds[fid]
        decisions[p["decision"]] = decisions.get(p["decision"], 0) + 1
        wrong = []
        if p.get("fields") is not None:
            for f in CRITICAL_FIELDS:
                tv, pv = norm_field(f, t["fields"].get(f)), norm_field(f, p["fields"].get(f))
                if pv is None and tv is not None: field_stats[f]["non_resolu"] += 1
                elif pv == tv: field_stats[f]["exact"] += 1
                else: field_stats[f]["faux"] += 1; wrong.append(f)
        wrong_fields_by_invoice[fid] = wrong
        exp, dec = t["expected_decision"], p["decision"]
        if dec == "auto":
            if exp == "doublon": silent.append((fid, "double écriture d'un doublon"))
            elif exp == "revue": silent.append((fid, "anomalie non détectée, écrite automatiquement"))
            elif wrong: silent.append((fid, f"écrite avec des valeurs fausses : {', '.join(wrong)}"))
        elif dec == "doublon" and exp != "doublon":
            silent.append((fid, "faux doublon : facture perdue"))
        elif dec == "revue" and exp == "auto":
            avoidable_reviews.append(fid)

    n = len(truth)
    evaluated = sum(1 for p in preds.values() if p.get("fields") is not None)
    print(f"Sortie : {args.sortie}  (mode : {out.get('mode', '?')})")
    print("ATTENTION : jeu SYNTHÉTIQUE d'exemple, ces chiffres ne sont pas des taux réels.\n")
    print(f"Exactitude par champ ({evaluated} factures extraites sur {n}) :")
    print(f"  {'champ':<24}{'exact':>7}{'faux':>7}{'non résolu':>12}")
    for f, s in field_stats.items():
        print(f"  {f:<24}{s['exact']:>7}{s['faux']:>7}{s['non_resolu']:>12}")
    print(f"\nDécisions : {decisions}")
    rate = decisions.get("revue", 0) / n
    print(f"Taux d'envoi en revue : {rate:.1%} ({decisions.get('revue', 0)}/{n})")
    print(f"Revues évitables (facture correcte envoyée en revue) : {len(avoidable_reviews)} {avoidable_reviews}")
    print(f"\nERREURS SILENCIEUSES : {len(silent)}")
    for fid, why in silent:
        print(f"  - {fid} : {why}  [{truth[fid]['scenario']}]")

    result = {"sortie": args.sortie, "mode": out.get("mode"), "synthetique": True, "factures": n,
              "exactitude_par_champ": field_stats, "decisions": decisions, "taux_revue": round(rate, 4),
              "revues_evitables": avoidable_reviews,
              "erreurs_silencieuses": [{"id": i, "raison": r} for i, r in silent]}
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.max_erreurs_silencieuses is not None and len(silent) > args.max_erreurs_silencieuses:
        sys.exit(1)

if __name__ == "__main__":
    main()
