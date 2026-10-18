-- =====================================================================
-- Factures fournisseurs -> comptabilité
-- Rapprochement 3 voies + écriture "exactement une fois" (effective)
-- Schéma PostgreSQL (testé sur PostgreSQL 17 avec sql/tests.sql)
--
-- Principe : n8n n'offre AUCUNE garantie "exactly once" native.
-- La garantie est fournie ici, côté base :
--   1. une empreinte normalisée UNIQUE réservée AVANT tout effet de bord ;
--   2. une machine à états dont les transitions sont contrôlées par trigger ;
--   3. une outbox (une seule écriture comptable possible par facture) ;
--   4. une boucle de réconciliation qui relit la compta pour fermer
--      les états incertains (écrit mais non confirmé).
-- =====================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS ap;          -- ap = accounts payable
SET search_path TO ap, public;

CREATE EXTENSION IF NOT EXISTS pgcrypto; -- digest() pour l'empreinte

-- ---------------------------------------------------------------------
-- Types
-- ---------------------------------------------------------------------
CREATE TYPE invoice_state AS ENUM (
  'recu',        -- PDF reçu, stocké, hash binaire calculé
  'extrait',     -- double extraction faite + vote champ par champ
  'valide',      -- contrôles déterministes + rapprochement 3 voies OK
  'en_revue',    -- confiance insuffisante : file de revue humaine
  'reserve',     -- empreinte réservée (UNIQUE) + message outbox créé
  'ecrit',       -- appel API compta parti, accusé non encore relu
  'incertain',   -- timeout / crash après envoi : la réconciliation tranche
  'confirme',    -- écriture relue dans la compta (référence externe)
  'doublon',     -- empreinte déjà réservée par une autre facture
  'rejete'       -- refus définitif (humain ou contrôle bloquant)
);

CREATE TYPE document_type AS ENUM ('facture', 'avoir', 'acompte', 'facture_solde');

CREATE TYPE outbox_status AS ENUM ('pending', 'sending', 'sent_unconfirmed', 'confirmed', 'dead');

-- ---------------------------------------------------------------------
-- Référentiel : bons de commande et bons de réception
-- ---------------------------------------------------------------------
CREATE TABLE purchase_orders (
  po_number        text PRIMARY KEY,
  supplier_siren   char(9)      NOT NULL CHECK (supplier_siren ~ '^[0-9]{9}$'),
  currency         char(3)      NOT NULL DEFAULT 'EUR',
  created_at       timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE purchase_order_lines (
  po_number        text         NOT NULL REFERENCES purchase_orders(po_number),
  line_no          int          NOT NULL,
  sku              text         NOT NULL,
  qty_ordered      numeric(14,3) NOT NULL CHECK (qty_ordered > 0),
  unit_price_ht    numeric(14,4) NOT NULL CHECK (unit_price_ht >= 0),
  vat_rate         numeric(5,2)  NOT NULL,
  PRIMARY KEY (po_number, line_no)
);

CREATE TABLE goods_receipts (
  receipt_id       text         PRIMARY KEY,
  po_number        text         NOT NULL REFERENCES purchase_orders(po_number),
  sku              text         NOT NULL,
  qty_received     numeric(14,3) NOT NULL CHECK (qty_received >= 0),
  received_at      timestamptz  NOT NULL
);

-- ---------------------------------------------------------------------
-- Factures (une ligne par document reçu, doublons compris)
-- ---------------------------------------------------------------------
CREATE TABLE invoices (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_message_id  text        NOT NULL,          -- id email / webhook
  file_sha256        char(64)    NOT NULL,          -- hash binaire du PDF
  state              invoice_state NOT NULL DEFAULT 'recu',
  doc_type           document_type,
  supplier_siret     char(14)    CHECK (supplier_siret ~ '^[0-9]{14}$'),
  supplier_name      text,
  invoice_number     text,
  invoice_number_norm text,                         -- "FA-2026/0042" -> "FA20260042"
  invoice_date       date,
  po_number          text        REFERENCES purchase_orders(po_number),
  related_invoice_number text,                      -- avoir -> facture d'origine ; solde -> acompte(s)
  currency           char(3)     NOT NULL DEFAULT 'EUR',
  total_ht           numeric(14,2),
  total_tva          numeric(14,2),
  total_ttc          numeric(14,2),
  iban               text,
  extraction_a       jsonb,                         -- sortie extracteur A (OCR + règles / modèle 1)
  extraction_b       jsonb,                         -- sortie extracteur B (LLM vision / modèle 2)
  voted              jsonb,                         -- résultat du vote champ par champ
  checks             jsonb,                         -- résultat des contrôles déterministes
  match_result       jsonb,                         -- résultat du rapprochement 3 voies
  confidence         numeric(4,3) CHECK (confidence BETWEEN 0 AND 1),
  review_reason      text,
  fingerprint        char(64),                      -- empreinte normalisée (voir fn_fingerprint)
  external_entry_ref text,                          -- référence de l'écriture dans la compta
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  -- un même message (relance de webhook) ne crée jamais deux lignes
  CONSTRAINT uq_invoice_source UNIQUE (source_message_id, file_sha256),
  -- un avoir est négatif, une facture est positive
  CONSTRAINT ck_sign CHECK (
    total_ttc IS NULL OR doc_type IS NULL
    OR (doc_type = 'avoir' AND total_ttc < 0)
    OR (doc_type <> 'avoir' AND total_ttc > 0)
  ),
  -- impossible d'être confirmé sans référence externe
  CONSTRAINT ck_confirmed_has_ref CHECK (state <> 'confirme' OR external_entry_ref IS NOT NULL),
  -- impossible d'être réservé ou plus loin sans empreinte
  CONSTRAINT ck_reserved_has_fp CHECK (
    state NOT IN ('reserve','ecrit','incertain','confirme') OR fingerprint IS NOT NULL
  )
);

CREATE INDEX ix_invoices_state ON invoices (state);
CREATE INDEX ix_invoices_file_sha ON invoices (file_sha256);

-- ---------------------------------------------------------------------
-- Clés d'idempotence : LE verrou métier
-- La réservation se fait par INSERT ... ON CONFLICT DO NOTHING RETURNING
-- AVANT tout appel à l'API compta. Si rien n'est retourné => doublon.
-- ---------------------------------------------------------------------
CREATE TABLE idempotency_keys (
  fingerprint   char(64)    PRIMARY KEY,           -- UNIQUE par construction
  invoice_id    bigint      NOT NULL UNIQUE REFERENCES invoices(id),
  canonical     text        NOT NULL,              -- chaîne normalisée avant hash (audit)
  reserved_at   timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Outbox : intention d'écriture comptable, créée dans la MÊME transaction
-- que la réservation. Un seul message par facture et par empreinte.
-- ---------------------------------------------------------------------
CREATE TABLE outbox (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  invoice_id       bigint      NOT NULL UNIQUE REFERENCES invoices(id),
  fingerprint      char(64)    NOT NULL UNIQUE REFERENCES idempotency_keys(fingerprint),
  payload          jsonb       NOT NULL,           -- écriture à passer (journal, comptes, montants)
  status           outbox_status NOT NULL DEFAULT 'pending',
  attempts         int         NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  locked_until     timestamptz,                    -- bail du worker (évite deux envois concurrents)
  next_attempt_at  timestamptz NOT NULL DEFAULT now(),
  last_error       text,
  external_ref     text,                           -- id renvoyé / retrouvé dans la compta
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_confirmed_ref CHECK (status <> 'confirmed' OR external_ref IS NOT NULL)
);

CREATE INDEX ix_outbox_ready ON outbox (status, next_attempt_at);

-- ---------------------------------------------------------------------
-- Journal des transitions (rempli automatiquement par trigger)
-- ---------------------------------------------------------------------
CREATE TABLE state_transitions (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  invoice_id  bigint        NOT NULL REFERENCES invoices(id),
  from_state  invoice_state,
  to_state    invoice_state NOT NULL,
  actor       text          NOT NULL DEFAULT coalesce(current_setting('ap.actor', true), 'system'),
  at          timestamptz   NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Audit append-only (aucun UPDATE / DELETE autorisé)
-- ---------------------------------------------------------------------
CREATE TABLE audit (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  invoice_id  bigint      REFERENCES invoices(id),
  event       text        NOT NULL,   -- ex. 'vote_disagreement', 'iban_changed', 'reconciled'
  detail      jsonb       NOT NULL DEFAULT '{}'::jsonb,
  actor       text        NOT NULL DEFAULT coalesce(current_setting('ap.actor', true), 'system'),
  at          timestamptz NOT NULL DEFAULT now()
);

-- IBAN connus par fournisseur : un IBAN nouveau ou modifié force la revue
CREATE TABLE supplier_bank_accounts (
  supplier_siren char(9)  NOT NULL,
  iban           text     NOT NULL,
  verified_at    timestamptz,             -- vérifié hors bande (appel au fournisseur)
  PRIMARY KEY (supplier_siren, iban)
);

-- =====================================================================
-- Fonctions
-- =====================================================================

-- Normalisation du numéro de facture : majuscules, sans séparateurs,
-- sans zéros de tête dans les blocs numériques ("FA-2026/0042" = "fa 2026 42")
CREATE FUNCTION fn_norm_invoice_number(n text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT regexp_replace(
           regexp_replace(upper(n), '(^|[^0-9])0+([0-9])', '\1\2', 'g'),
           '[^A-Z0-9]', '', 'g')
$$;

-- Empreinte normalisée : SIREN | type | numéro normalisé | montant en centimes | date
-- Le hash binaire du PDF n'est PAS suffisant (PDF ré-exporté = autre hash).
CREATE FUNCTION fn_fingerprint_canonical(
  p_siret text, p_doc_type document_type, p_number text, p_ttc numeric, p_date date
) RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT concat_ws('|',
           left(p_siret, 9),
           p_doc_type::text,
           fn_norm_invoice_number(p_number),
           (round(p_ttc * 100))::bigint::text,
           to_char(p_date, 'YYYY-MM-DD'))
$$;

CREATE FUNCTION fn_fingerprint(
  p_siret text, p_doc_type document_type, p_number text, p_ttc numeric, p_date date
) RETURNS char(64) LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT encode(digest(fn_fingerprint_canonical(p_siret, p_doc_type, p_number, p_ttc, p_date), 'sha256'), 'hex')
$$;

-- Clé IBAN (ISO 13616, mod 97 = 1)
CREATE FUNCTION fn_iban_valid(p_iban text) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE
  s text := upper(regexp_replace(p_iban, '\s', '', 'g'));
  r text := '';
  c char;
  rem int := 0;
  i int;
BEGIN
  IF s !~ '^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$' THEN RETURN false; END IF;
  s := substr(s, 5) || substr(s, 1, 4);
  FOR i IN 1..length(s) LOOP
    c := substr(s, i, 1);
    IF c ~ '[A-Z]' THEN r := r || (ascii(c) - 55)::text; ELSE r := r || c; END IF;
  END LOOP;
  FOR i IN 1..length(r) LOOP
    rem := (rem * 10 + substr(r, i, 1)::int) % 97;
  END LOOP;
  RETURN rem = 1;
END $$;

-- SIRET : 14 chiffres + clé de Luhn (exception connue : La Poste, SIREN 356000000)
CREATE FUNCTION fn_siret_valid(p_siret text) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE
  total int := 0; d int; i int;
BEGIN
  IF p_siret !~ '^[0-9]{14}$' THEN RETURN false; END IF;
  IF left(p_siret, 9) = '356000000' THEN
    -- La Poste : somme des chiffres multiple de 5
    SELECT sum(substr(p_siret, g, 1)::int) INTO total FROM generate_series(1, 14) g;
    RETURN total % 5 = 0;
  END IF;
  FOR i IN 1..14 LOOP
    d := substr(p_siret, 15 - i, 1)::int;      -- de droite à gauche
    IF i % 2 = 0 THEN d := d * 2; IF d > 9 THEN d := d - 9; END IF; END IF;
    total := total + d;
  END LOOP;
  RETURN total % 10 = 0;
END $$;

-- ---------------------------------------------------------------------
-- Machine à états : seules ces transitions sont autorisées
-- ---------------------------------------------------------------------
CREATE TABLE allowed_transitions (
  from_state invoice_state NOT NULL,
  to_state   invoice_state NOT NULL,
  PRIMARY KEY (from_state, to_state)
);

INSERT INTO allowed_transitions VALUES
  ('recu',      'extrait'),
  ('recu',      'doublon'),     -- même PDF binaire déjà vu
  ('extrait',   'valide'),
  ('extrait',   'en_revue'),
  ('extrait',   'rejete'),
  ('en_revue',  'valide'),      -- corrigé/validé par un humain PUIS contrôles repassés
  ('en_revue',  'extrait'),     -- corrections humaines appliquées, contrôles à refaire
  ('en_revue',  'rejete'),
  ('valide',    'reserve'),
  ('valide',    'doublon'),     -- empreinte déjà réservée
  ('reserve',   'ecrit'),
  ('ecrit',     'confirme'),
  ('ecrit',     'incertain'),
  ('incertain', 'confirme'),    -- la réconciliation a retrouvé l'écriture
  ('incertain', 'reserve');     -- la réconciliation prouve l'absence : on peut renvoyer

CREATE FUNCTION trg_invoice_state() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.state <> 'recu' THEN
      RAISE EXCEPTION 'une facture doit naître à l''état recu (reçu: %)', NEW.state;
    END IF;
    INSERT INTO state_transitions (invoice_id, from_state, to_state, actor)
    VALUES (NEW.id, NULL, NEW.state, coalesce(current_setting('ap.actor', true), 'system'));
    RETURN NEW;
  END IF;

  IF NEW.state IS DISTINCT FROM OLD.state THEN
    IF NOT EXISTS (SELECT 1 FROM allowed_transitions
                   WHERE from_state = OLD.state AND to_state = NEW.state) THEN
      RAISE EXCEPTION 'transition interdite % -> % (facture %)', OLD.state, NEW.state, OLD.id
        USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO state_transitions (invoice_id, from_state, to_state, actor)
    VALUES (NEW.id, OLD.state, NEW.state, coalesce(current_setting('ap.actor', true), 'system'));
  END IF;

  -- un IBAN ne se modifie jamais en silence
  IF NEW.iban IS DISTINCT FROM OLD.iban AND OLD.iban IS NOT NULL THEN
    INSERT INTO audit (invoice_id, event, detail)
    VALUES (NEW.id, 'iban_changed', jsonb_build_object('old', OLD.iban, 'new', NEW.iban));
  END IF;

  NEW.updated_at := now();
  RETURN NEW;
END $$;

-- AFTER INSERT pour disposer de NEW.id ; BEFORE UPDATE pour pouvoir refuser
CREATE TRIGGER invoices_state_ins AFTER INSERT ON invoices
  FOR EACH ROW EXECUTE FUNCTION trg_invoice_state();
CREATE TRIGGER invoices_state_upd BEFORE UPDATE ON invoices
  FOR EACH ROW EXECUTE FUNCTION trg_invoice_state();

-- Audit et transitions : append-only
CREATE FUNCTION trg_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% est en ajout seul (append-only)', TG_TABLE_NAME;
END $$;

CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit
  FOR EACH ROW EXECUTE FUNCTION trg_append_only();
CREATE TRIGGER transitions_append_only BEFORE UPDATE OR DELETE ON state_transitions
  FOR EACH ROW EXECUTE FUNCTION trg_append_only();

-- ---------------------------------------------------------------------
-- Réservation atomique : empreinte + outbox dans UNE transaction,
-- AVANT tout appel externe. Retourne 'reserve' ou 'doublon'.
-- ---------------------------------------------------------------------
CREATE FUNCTION fn_reserve(p_invoice_id bigint, p_payload jsonb) RETURNS invoice_state
LANGUAGE plpgsql AS $$
DECLARE
  inv invoices%ROWTYPE;
  v_fp char(64);
  v_canon text;
  v_owner bigint;
BEGIN
  SELECT * INTO inv FROM invoices WHERE id = p_invoice_id FOR UPDATE;
  IF inv.state <> 'valide' THEN
    RAISE EXCEPTION 'fn_reserve: facture % à l''état %, attendu valide', p_invoice_id, inv.state;
  END IF;

  v_canon := fn_fingerprint_canonical(inv.supplier_siret, inv.doc_type, inv.invoice_number,
                                      inv.total_ttc, inv.invoice_date);
  v_fp    := fn_fingerprint(inv.supplier_siret, inv.doc_type, inv.invoice_number,
                            inv.total_ttc, inv.invoice_date);

  INSERT INTO idempotency_keys (fingerprint, invoice_id, canonical)
  VALUES (v_fp, p_invoice_id, v_canon)
  ON CONFLICT (fingerprint) DO NOTHING
  RETURNING invoice_id INTO v_owner;

  IF v_owner IS NULL THEN
    SELECT invoice_id INTO v_owner FROM idempotency_keys WHERE fingerprint = v_fp;
    UPDATE invoices SET state = 'doublon', fingerprint = v_fp,
           review_reason = 'empreinte déjà réservée par la facture ' || v_owner
     WHERE id = p_invoice_id;
    INSERT INTO audit (invoice_id, event, detail)
    VALUES (p_invoice_id, 'duplicate_blocked', jsonb_build_object('owner_invoice_id', v_owner, 'canonical', v_canon));
    RETURN 'doublon';
  END IF;

  UPDATE invoices SET state = 'reserve', fingerprint = v_fp WHERE id = p_invoice_id;
  INSERT INTO outbox (invoice_id, fingerprint, payload) VALUES (p_invoice_id, v_fp, p_payload);
  RETURN 'reserve';
END $$;

-- ---------------------------------------------------------------------
-- Worker outbox : prise d'un lot avec bail (FOR UPDATE SKIP LOCKED).
-- Deux exécutions n8n concurrentes ne prennent jamais le même message.
-- On ne reprend JAMAIS automatiquement un message 'sending' dont le bail
-- a expiré : l'appel est peut-être parti. C'est la réconciliation qui tranche.
-- La facture passe à 'ecrit' (= écriture en vol) dans la même transaction.
-- ---------------------------------------------------------------------
CREATE FUNCTION fn_claim_outbox(p_limit int DEFAULT 10, p_lease interval DEFAULT '2 minutes')
RETURNS SETOF outbox LANGUAGE plpgsql AS $$
DECLARE r outbox%ROWTYPE;
BEGIN
  FOR r IN
    UPDATE outbox o
       SET status = 'sending', attempts = o.attempts + 1,
           locked_until = now() + p_lease, updated_at = now()
     WHERE o.id IN (
       SELECT id FROM outbox
        WHERE status = 'pending' AND next_attempt_at <= now()
        ORDER BY id
        LIMIT p_limit
        FOR UPDATE SKIP LOCKED)
    RETURNING o.*
  LOOP
    UPDATE invoices SET state = 'ecrit' WHERE id = r.invoice_id;
    RETURN NEXT r;
  END LOOP;
END $$;

-- Accusé lu dans la compta (lecture de contrôle après l'appel, ou réconciliation)
CREATE FUNCTION fn_confirm(p_invoice_id bigint, p_external_ref text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  UPDATE outbox SET status = 'confirmed', external_ref = p_external_ref,
         locked_until = NULL, updated_at = now()
   WHERE invoice_id = p_invoice_id;
  UPDATE invoices SET state = 'confirme', external_entry_ref = p_external_ref
   WHERE id = p_invoice_id;
  INSERT INTO audit (invoice_id, event, detail)
  VALUES (p_invoice_id, 'confirmed', jsonb_build_object('external_ref', p_external_ref));
END $$;

-- Timeout / 5xx / réponse illisible : on NE renvoie PAS, on marque incertain
CREATE FUNCTION fn_mark_uncertain(p_invoice_id bigint, p_error text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  UPDATE outbox SET status = 'sent_unconfirmed', last_error = p_error, updated_at = now()
   WHERE invoice_id = p_invoice_id;
  UPDATE invoices SET state = 'incertain' WHERE id = p_invoice_id;
END $$;

-- Réconciliation : candidats = incertains + écritures en vol dont le bail a expiré
-- (worker n8n tué après l'appel, avant d'avoir enregistré la réponse).
CREATE VIEW v_reconcile_candidates AS
SELECT i.id AS invoice_id, i.fingerprint, i.state, o.status, o.attempts, o.locked_until
  FROM invoices i JOIN outbox o ON o.invoice_id = i.id
 WHERE i.state = 'incertain'
    OR (i.state = 'ecrit' AND o.status = 'sending' AND o.locked_until < now());

-- Verdict de la réconciliation après relecture de la compta par empreinte.
--   found = true  -> confirme (l'écriture existe déjà : on ne renvoie surtout pas)
--   found = false -> retour à 'reserve' + outbox 'pending' (renvoi sûr : preuve d'absence)
CREATE FUNCTION fn_reconcile(p_invoice_id bigint, p_found boolean, p_external_ref text DEFAULT NULL)
RETURNS invoice_state LANGUAGE plpgsql AS $$
DECLARE st invoice_state;
BEGIN
  SELECT state INTO st FROM invoices WHERE id = p_invoice_id FOR UPDATE;
  IF st = 'ecrit' THEN
    PERFORM fn_mark_uncertain(p_invoice_id, 'bail expiré, worker présumé mort');
  END IF;
  IF p_found THEN
    PERFORM fn_confirm(p_invoice_id, p_external_ref);
    INSERT INTO audit (invoice_id, event, detail)
    VALUES (p_invoice_id, 'reconciled_found', jsonb_build_object('external_ref', p_external_ref));
    RETURN 'confirme';
  END IF;
  UPDATE invoices SET state = 'reserve' WHERE id = p_invoice_id;
  UPDATE outbox SET status = CASE WHEN attempts >= 5 THEN 'dead'::outbox_status ELSE 'pending'::outbox_status END,
         locked_until = NULL, next_attempt_at = now() + (attempts * interval '5 minutes'), updated_at = now()
   WHERE invoice_id = p_invoice_id;
  INSERT INTO audit (invoice_id, event, detail)
  VALUES (p_invoice_id, 'reconciled_absent', '{}'::jsonb);
  RETURN 'reserve';
END $$;

-- ---------------------------------------------------------------------
-- Fonctions appelées par les nœuds Postgres du workflow n8n
-- (requêtes courtes côté n8n, logique testable côté base)
-- ---------------------------------------------------------------------

-- Enregistre les deux extractions et le vote ; recu -> extrait (-> en_revue si désaccord)
CREATE FUNCTION fn_save_extraction(p_invoice_id bigint, p_a jsonb, p_b jsonb, p_voted jsonb,
                                   p_disagreements text[]) RETURNS invoice_state
LANGUAGE plpgsql AS $$
DECLARE st invoice_state;
BEGIN
  UPDATE invoices SET
    state = 'extrait', extraction_a = p_a, extraction_b = p_b, voted = p_voted,
    doc_type = (p_voted->>'doc_type')::document_type,
    supplier_siret = p_voted->>'supplier_siret', supplier_name = p_voted->>'supplier_name',
    invoice_number = p_voted->>'invoice_number',
    invoice_number_norm = fn_norm_invoice_number(p_voted->>'invoice_number'),
    invoice_date = (p_voted->>'invoice_date')::date, po_number = p_voted->>'po_number',
    related_invoice_number = p_voted->>'related_invoice_number',
    total_ht = (p_voted->>'total_ht')::numeric, total_tva = (p_voted->>'total_tva')::numeric,
    total_ttc = (p_voted->>'total_ttc')::numeric, iban = p_voted->>'iban'
  WHERE id = p_invoice_id;
  IF coalesce(array_length(p_disagreements, 1), 0) > 0 THEN
    UPDATE invoices SET state = 'en_revue',
           review_reason = 'désaccord des extracteurs : ' || array_to_string(p_disagreements, ', ')
     WHERE id = p_invoice_id;
    INSERT INTO audit (invoice_id, event, detail)
    VALUES (p_invoice_id, 'vote_disagreement', jsonb_build_object('fields', p_disagreements));
  END IF;
  SELECT state INTO st FROM invoices WHERE id = p_invoice_id;
  RETURN st;
END $$;

-- Tout ce dont les contrôles et le rapprochement 3 voies ont besoin, en un seul JSON
CREATE FUNCTION fn_load_for_checks(p_invoice_id bigint) RETURNS jsonb
LANGUAGE sql STABLE AS $$
  WITH inv AS (SELECT * FROM invoices WHERE id = p_invoice_id),
  posted AS (   -- factures déjà engagées (réservées ou au-delà) sur le même BC
    SELECT i.* FROM invoices i, inv
     WHERE i.po_number = inv.po_number AND i.id <> inv.id
       AND i.state IN ('reserve', 'ecrit', 'incertain', 'confirme'))
  SELECT jsonb_build_object(
    'invoice_id', inv.id,
    'state', inv.state,
    'fields', inv.voted,
    'po', (SELECT jsonb_build_object('supplier_siren', po.supplier_siren,
             'lines', (SELECT jsonb_agg(jsonb_build_object('sku', l.sku, 'qty', l.qty_ordered,
                         'unit_price_ht', l.unit_price_ht, 'vat_rate', l.vat_rate) ORDER BY l.line_no)
                         FROM purchase_order_lines l WHERE l.po_number = po.po_number))
             FROM purchase_orders po WHERE po.po_number = inv.po_number),
    'received', (SELECT coalesce(jsonb_object_agg(sku, q), '{}') FROM
                  (SELECT sku, sum(qty_received) q FROM goods_receipts
                    WHERE po_number = inv.po_number GROUP BY sku) r),
    'already_invoiced', (SELECT coalesce(jsonb_object_agg(sku, q), '{}') FROM
                  (SELECT l->>'sku' sku, sum((l->>'qty')::numeric) q
                     FROM posted p, jsonb_array_elements(p.voted->'lines') l
                    WHERE p.doc_type IN ('facture', 'facture_solde', 'avoir')
                      AND l->>'sku' <> 'ACOMPTE-DEDUIT'
                    GROUP BY 1) x),
    'acomptes_ht', (SELECT coalesce(sum(total_ht), 0) FROM posted WHERE doc_type = 'acompte'),
    'original_ttc', (SELECT o.total_ttc FROM invoices o
                      WHERE left(o.supplier_siret, 9) = left(inv.supplier_siret, 9)
                        AND o.invoice_number_norm = fn_norm_invoice_number(inv.related_invoice_number)
                        AND o.doc_type <> 'avoir'
                        AND o.state IN ('reserve', 'ecrit', 'incertain', 'confirme')
                      LIMIT 1),
    'known_ibans', (SELECT coalesce(jsonb_agg(iban), '[]') FROM supplier_bank_accounts
                     WHERE supplier_siren = left(inv.supplier_siret, 9))
  ) FROM inv
$$;

-- Résultat des contrôles : -> valide si tout passe, sinon -> en_revue
CREATE FUNCTION fn_save_checks(p_invoice_id bigint, p_checks jsonb, p_confidence numeric,
                               p_pass boolean) RETURNS invoice_state
LANGUAGE plpgsql AS $$
DECLARE st invoice_state;
BEGIN
  UPDATE invoices SET checks = p_checks, confidence = p_confidence,
         state = CASE WHEN p_pass THEN 'valide'::invoice_state ELSE 'en_revue'::invoice_state END,
         review_reason = CASE WHEN p_pass THEN NULL ELSE p_checks->>'summary' END
   WHERE id = p_invoice_id
  RETURNING state INTO st;
  RETURN st;
END $$;

-- Décision du réviseur humain. Il valide (avec corrections éventuelles) ou rejette.
-- Une validation NE force PAS les contrôles : la facture corrigée repasse par
-- fn_load_for_checks + contrôles déterministes avant de pouvoir être réservée.
CREATE FUNCTION fn_apply_review(p_invoice_id bigint, p_reviewer text, p_decision text,
                                p_corrections jsonb DEFAULT '{}', p_comment text DEFAULT NULL)
RETURNS invoice_state LANGUAGE plpgsql AS $$
DECLARE st invoice_state; v jsonb;
BEGIN
  PERFORM set_config('ap.actor', 'revue:' || p_reviewer, true);
  SELECT state, voted INTO st, v FROM invoices WHERE id = p_invoice_id FOR UPDATE;
  IF st <> 'en_revue' THEN
    RAISE EXCEPTION 'facture % à l''état %, pas en revue', p_invoice_id, st;
  END IF;
  INSERT INTO audit (invoice_id, event, detail, actor)
  VALUES (p_invoice_id, 'human_review',
          jsonb_build_object('decision', p_decision, 'corrections', p_corrections, 'comment', p_comment),
          'revue:' || p_reviewer);
  IF p_decision = 'rejeter' THEN
    UPDATE invoices SET state = 'rejete', review_reason = p_comment WHERE id = p_invoice_id;
    RETURN 'rejete';
  ELSIF p_decision <> 'valider' THEN
    RAISE EXCEPTION 'décision inconnue : %', p_decision;
  END IF;
  -- les corrections s'appliquent au vote ; les colonnes sont recalculées
  PERFORM fn_save_extraction(p_invoice_id,
            (SELECT extraction_a FROM invoices WHERE id = p_invoice_id),
            (SELECT extraction_b FROM invoices WHERE id = p_invoice_id),
            coalesce(v, '{}') || p_corrections, ARRAY[]::text[]);
  RETURN 'extrait';    -- en_revue -> extrait : les contrôles doivent repasser
END $$;

-- Vue de supervision : tout ce qui n'est pas dans un état terminal depuis trop longtemps
CREATE VIEW v_stuck AS
SELECT i.id, i.state, i.updated_at, now() - i.updated_at AS age, o.status AS outbox_status, o.attempts
  FROM invoices i
  LEFT JOIN outbox o ON o.invoice_id = i.id
 WHERE i.state NOT IN ('confirme', 'doublon', 'rejete')
   AND i.updated_at < now() - interval '30 minutes';

COMMIT;
