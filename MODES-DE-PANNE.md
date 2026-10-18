# Modes de panne

Une démo montre le chemin heureux. Ce fichier montre le reste : chaque façon dont le flux peut se tromper,
comment on le **détecte** et comment on le **rattrape**. La règle commune : une panne peut ralentir une
facture, elle ne doit jamais produire une écriture fausse ou en double **sans que personne ne le sache**.

Les références « F01 » à « F15 » renvoient au jeu d'évaluation synthétique d'[`eval/`](eval/).
Les fonctions `ap.*` sont dans [`sql/schema.sql`](sql/schema.sql).

## Exécution et infrastructure

| Panne | Détection | Rattrapage |
|---|---|---|
| **Crash de n8n juste après l'écriture comptable**, avant d'avoir enregistré la réponse | La facture reste `ecrit`, le message outbox reste `sending` avec un bail expiré. `ap.v_reconcile_candidates` la remonte. | La réconciliation cherche l'écriture par empreinte (`external_reference`). Trouvée : `confirme`, **sans renvoi**. Absente : retour à `reserve` et renvoi sûr. Testé dans `sql/tests.sql` (cas 9). |
| **Timeout de l'API compta** (l'écriture est peut-être passée) | Sortie d'erreur du nœud HTTP Request (`continueErrorOutput`). | `ap.fn_mark_uncertain()` : état `incertain`, **pas de nouvel essai immédiat**. La réconciliation tranche avec une preuve. Testé (cas 8). |
| **Erreur 5xx ou réponse illisible** de l'API compta | Idem : sortie d'erreur. | Idem : `incertain`, puis réconciliation. |
| **L'écriture réussit mais la relecture échoue** | Sortie d'erreur du nœud « Relire l'écriture ». | `incertain`. La réconciliation retrouvera l'écriture et confirmera. |
| **Deux exécutions du worker en même temps** | Aucune, et c'est voulu : impossible par construction. | `ap.fn_claim_outbox()` prend les messages avec `FOR UPDATE SKIP LOCKED` et pose un bail. Testé (cas 7 : la seconde prise ne renvoie rien). |
| **Relance automatique de n8n** sur un nœud | L'appel arrive avec la même `Idempotency-Key` (= empreinte). | L'API compta déduplique si elle gère la clé. Sinon, la réconciliation retrouve l'écriture déjà passée. |
| **API compta sans clé d'idempotence ni recherche par référence** | Connue à la conception. | Limite assumée : les cas `incertain` passent en revue manuelle. Documenté dans « Ce qu'on ne promet pas ». |
| **Réconciliation qui échoue en boucle** (l'écriture n'apparaît jamais) | `attempts` augmente à chaque passage, avec un délai croissant. | Après 5 tentatives, l'outbox passe en `dead` et la facture apparaît dans `ap.v_stuck`, d'où une alerte Slack. |
| **Facture bloquée dans un état intermédiaire** pour une raison imprévue | `ap.v_stuck` : tout état non terminal depuis plus de 30 minutes. | Alerte Slack à chaque passage de la réconciliation. Rien ne reste « en l'air » en silence. |
| **Postgres indisponible** | Le nœud Postgres échoue, puis l'Error Trigger. | Rien n'est réservé, donc rien n'est écrit. Le message Gmail ou le webhook sera rejoué et absorbé par `UNIQUE(source_message_id, file_sha256)`. |
| **Erreur non prévue dans un nœud** | Error Trigger. | Alerte Slack avec le nœud fautif. L'état en base dit exactement où reprendre. |

## Doublons

| Panne | Détection | Rattrapage |
|---|---|---|
| **Relance du webhook** : même message, même fichier (F04) | `INSERT ... ON CONFLICT (source_message_id, file_sha256) DO NOTHING` ne retourne aucune ligne. | Branche « Relance ignorée ». Aucun traitement, aucun coût d'extraction. Testé (cas 2). |
| **PDF ré-exporté** par le fournisseur : autre hash binaire, même facture (F03) | Le hash ne voit rien. L'**empreinte normalisée** (SIREN, type, numéro normalisé, TTC en centimes, date) est identique. | `ap.fn_reserve()` trouve l'empreinte déjà prise : état `doublon`, aucun message outbox. Testé (cas 5). |
| **Même facture renvoyée par un autre canal** (email + portail) | Même empreinte. | Même rattrapage : `doublon`. |
| **Numéro écrit différemment** (`FA-2026/0042`, `fa 2026 42`) | `fn_norm_invoice_number()` : majuscules, séparateurs et zéros de tête retirés. | Même empreinte, donc doublon détecté. |
| **Faux doublon** : deux factures réellement différentes qui ont la même empreinte | Très improbable (même fournisseur, même numéro normalisé, même montant, même date). | La facture bloquée est visible (`doublon`, avec l'id de la facture « propriétaire » dans `audit`). Un humain peut la requalifier. Elle n'est jamais perdue en silence. |

## Extraction

| Panne | Détection | Rattrapage |
|---|---|---|
| **Montant faux mais JSON valide**, un seul extracteur se trompe (F11) | Le vote champ par champ voit le désaccord sur `total_ttc`. | Revue humaine avec le champ en cause. Le réviseur corrige, puis les contrôles repassent. |
| **Les deux extracteurs se trompent pareil** (F12 : 3,30 lu au lieu de 3,80) | Le vote ne voit rien. Les contrôles déterministes, si : somme des lignes ≠ HT, TVA recalculée ≠ TVA, prix hors tolérance vs BC. | Revue humaine. C'est pour ce cas que les contrôles arithmétiques existent en plus du vote. |
| **Champ manquant** (null chez un extracteur) | Désaccord au vote. | Revue. |
| **Date au format différent** (`30/09/2026` / `2026-09-30`) | Normalisée avant le vote, ce n'est pas un désaccord. | Rien à faire. |
| **Facture multi-pages ou scan de mauvaise qualité** | Désaccords au vote, ou contrôles en échec. | Revue. Le taux de revue sur ces documents se mesure avec `eval/metriques.py`. |
| **Hallucination d'une ligne qui n'existe pas** | Somme des lignes ≠ HT, ou référence absente du BC. | Revue. |
| **Mauvais bon de commande lu** | BC d'un autre fournisseur, ou références absentes du BC. | Revue. |

## Données métier

| Panne | Détection | Rattrapage |
|---|---|---|
| **Erreur de TVA du fournisseur** : HT + TVA ≠ TTC (F05) | Contrôle `HT + TVA ≠ TTC`. | Revue. Le réviseur ne peut pas forcer le contrôle : il rejette et demande une facture rectifiée. |
| **IBAN avec une clé invalide** (F06) | Clé ISO 13616 : mod 97 ≠ 1. | Revue. |
| **IBAN valide mais modifié** par rapport à l'IBAN connu (F07 ; fraude au changement de RIB) | IBAN absent de `supplier_bank_accounts` pour ce fournisseur. | Revue **systématique**. Tout changement d'IBAN est aussi tracé dans `audit` (`iban_changed`). Un nouvel IBAN ne s'ajoute qu'après vérification hors bande. |
| **SIRET mal imprimé** (F15) | Clé de Luhn invalide. | Revue. |
| **Quantité facturée > quantité reçue** (F13) | Rapprochement 3 voies : déjà facturé + facturé ≤ reçu. | Revue. Correction à 8 par le réviseur, ou attente de la réception. |
| **Écart de prix avec le BC** | Écart > 1 % : bloquant. Écart ≤ 1 % (F14) : pénalité de confiance. | Revue au-delà de la tolérance ; écriture auto en deçà. |
| **Avoir** : montants négatifs (F08) | `ck_sign` dans Postgres, et contrôle de signe dans le Code. Facture d'origine requise et déjà engagée, avoir ≤ origine. | Écriture auto si tout concorde, revue sinon. Un avoir avec le même numéro que sa facture n'est pas un doublon (type différent dans l'empreinte). Testé (cas 6). |
| **Avoir positif** (erreur d'extraction du signe) | Contrainte `ck_sign` : la base refuse la ligne. | Revue. Testé (cas 6). |
| **Acompte** sans bon de réception (F09) | Type `acompte` : pas de contrôle de réception, plafond de 40 % du HT commandé. | Écriture auto sous le plafond, revue au-dessus. |
| **Facture de solde** qui déduit un acompte (F10) | Ligne `ACOMPTE-DEDUIT` négative : un contrôle naïf « lignes positives » échouerait. Ici elle doit égaler les acomptes engagés sur le BC. | Écriture auto si égalité, revue sinon. |
| **Avoir sans facture d'origine connue** | `original_ttc` nul dans `fn_load_for_checks`. | Revue. |

## Humain et gouvernance

| Panne | Détection | Rattrapage |
|---|---|---|
| **Le réviseur valide une facture incohérente** | La validation renvoie la facture à `extrait` et les contrôles repassent. | Impossible de forcer un contrôle arithmétique. Correction ou rejet. |
| **Décision humaine non tracée** | Impossible par construction. | `fn_apply_review()` écrit dans `audit` (en ajout seul) ; l'acteur est enregistré dans `state_transitions`. |
| **Tentative d'effacer une trace** | Trigger `trg_append_only`. | `UPDATE` et `DELETE` refusés sur `audit` et `state_transitions`. Testé (cas 11). |
| **Transition d'état incohérente** dans le workflow (bug, mauvaise branche) | Trigger `trg_invoice_state` et table `allowed_transitions`. | La base refuse. Exemple : `recu → reserve` impossible. Testé (cas 3). |
| **« Confirmé » sans preuve** | Contrainte `ck_confirmed_has_ref`. | Refusé par la base. Testé (cas 10). |
| **Dérive du modèle d'extraction** (nouvelle version, nouveau prompt) | Rejouer `eval/metriques.py` sur le jeu figé avant tout changement. | `--max-erreurs-silencieuses 0` en CI : si le changement fait apparaître une seule erreur silencieuse, il ne passe pas. |
