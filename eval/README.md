# Évaluation : jeu SYNTHÉTIQUE d'exemple

> **Attention.** Les 15 factures de ce dossier sont **inventées** : fournisseurs, SIRET, IBAN, montants.
> Elles sont construites pour contenir des pièges connus. Les chiffres produits ici **démontrent la méthode
> de mesure**. Ce ne sont **pas** des taux de précision réels, et ils ne doivent pas être présentés comme tels.
> Pour annoncer un taux, il faut remplacer ce jeu par 30 à 50 vraies factures anonymisées, figées, avec
> leur vérité terrain établie à la main.

## Contenu

| Fichier | Rôle |
|---|---|
| `generer_jeu.py` | Génère les données de `donnees/` de façon déterministe |
| `donnees/references.json` | Fournisseurs (IBAN connus), bons de commande, bons de réception |
| `donnees/verite_terrain.json` | Ce qui est réellement imprimé sur chaque facture + décision attendue |
| `donnees/extraction_a.json`, `extraction_b.json` | Sorties **simulées** des deux extracteurs, avec erreurs injectées |
| `commun.py` | Normalisation, IBAN mod 97, SIRET Luhn, empreinte (mêmes règles que le SQL) |
| `pipeline_reference.py` | Logique de décision de référence, en mode `complet` ou `naif` |
| `metriques.py` | Compare une sortie à la vérité terrain et calcule les métriques |
| `sorties/` | Sorties et métriques produites par les commandes ci-dessous |

## Les 15 cas

| Id | Cas | Décision attendue |
|---|---|---|
| F01 | Nominal | auto |
| F02 | Deux taux de TVA (20 % et 5,5 %) | auto |
| F03 | **PDF ré-exporté** de F01 (autre hash, numéro écrit autrement) | doublon |
| F04 | **Relance de webhook** de F02 | doublon |
| F05 | **Erreur de TVA** : HT + TVA ≠ TTC | revue |
| F06 | **IBAN invalide** (clé mod 97) | revue |
| F07 | **IBAN valide mais changé** (fraude au RIB) | revue |
| F08 | **Avoir** lié à F01 | auto |
| F09 | **Acompte** de 30 % | auto |
| F10 | **Facture de solde** qui déduit l'acompte F09 | auto |
| F11 | Correcte, mais l'extracteur A lit un TTC faux | auto (revue acceptable) |
| F12 | Correcte, mais **les deux** extracteurs lisent un prix faux | auto (revue acceptable) |
| F13 | **Quantité facturée > reçue** | revue |
| F14 | Écart de prix de 0,38 %, dans la tolérance | auto |
| F15 | **SIRET invalide** (Luhn) | revue |

## Métriques

- **Exactitude par champ** : `exact` (valeur juste), `faux` (valeur fausse affirmée : dangereux),
  `non résolu` (null après désaccord : envoyé en revue, pas dangereux).
- **Erreurs silencieuses**, la métrique qui compte : facture écrite automatiquement alors qu'elle est un
  doublon, qu'elle devait aller en revue ou qu'un champ critique est faux ; ou facture classée à tort en
  doublon (perdue).
- **Taux d'envoi en revue** et **revues évitables** (facture correcte envoyée en revue) : le coût de la prudence.

## Lancer

```bash
python3 generer_jeu.py
python3 pipeline_reference.py --mode complet
python3 pipeline_reference.py --mode naif
python3 metriques.py sorties/sortie_complet.json --json sorties/metriques_complet.json --max-erreurs-silencieuses 0
python3 metriques.py sorties/sortie_naif.json --json sorties/metriques_naif.json
```

## Ce que montre le jeu synthétique

| | Mode naïf (un extracteur, dédoublonnage par hash, pas de contrôles) | Mode complet |
|---|---|---|
| Erreurs silencieuses | 8 sur 15 | 0 sur 15 |
| Envoyées en revue | 0 | 7 (dont 2 évitables : F11, F12) |

Ce tableau ne mesure pas une performance. Le jeu a été **construit** pour contenir ces pièges, donc le mode
complet les attrape par conception. Il montre deux choses : la métrique « erreurs silencieuses » distingue
une démo d'un système fiable, et le prix de la fiabilité se lit dans le taux de revue. Sur de vraies
factures, les deux chiffres seraient différents, et c'est ce qu'il faudrait mesurer.
