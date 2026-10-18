-- Tests du schéma (données 100 % fictives). Exécution :
--   psql -v ON_ERROR_STOP=1 -f schema.sql -f tests.sql
SET search_path TO ap, public;
SET ap.actor = 'tests';

DO $$
DECLARE
  id1 bigint; id2 bigint; id3 bigint; r invoice_state; n int; o outbox%ROWTYPE;
BEGIN
  -- 1. Contrôles déterministes
  ASSERT fn_iban_valid('FR48 3000 1000 0100 0000 0001 101'), 'IBAN valide refusé';
  ASSERT NOT fn_iban_valid('FR4830001000010000000001102'),   'IBAN modifié accepté';
  ASSERT fn_siret_valid('11111111100014'),  'SIRET Luhn valide refusé';
  ASSERT NOT fn_siret_valid('11111111100015'), 'SIRET Luhn invalide accepté';
  ASSERT fn_norm_invoice_number('FA-2026/0042') = fn_norm_invoice_number('fa 2026 42'), 'normalisation numéro';

  -- 2. Relance de webhook : même message + même fichier => rejet par UNIQUE
  INSERT INTO purchase_orders VALUES ('PO-1001', '111111111', 'EUR', now());
  INSERT INTO invoices (source_message_id, file_sha256) VALUES ('msg-1', repeat('a', 64)) RETURNING id INTO id1;
  BEGIN
    INSERT INTO invoices (source_message_id, file_sha256) VALUES ('msg-1', repeat('a', 64));
    RAISE EXCEPTION 'relance webhook non bloquée';
  EXCEPTION WHEN unique_violation THEN NULL;
  END;

  -- 3. Transition interdite (recu -> confirme)
  BEGIN
    UPDATE invoices SET state = 'reserve', fingerprint = repeat('f', 64) WHERE id = id1;
    RAISE EXCEPTION 'transition interdite acceptée';
  EXCEPTION WHEN check_violation THEN NULL;
  END;

  -- 4. Chemin nominal jusqu'à la réservation
  UPDATE invoices SET state = 'extrait', doc_type = 'facture', supplier_siret = '11111111100014',
         invoice_number = 'FA-2026/0042', invoice_date = '2026-09-30', po_number = 'PO-1001',
         total_ht = 1000.00, total_tva = 200.00, total_ttc = 1200.00,
         iban = 'FR4830001000010000000001101' WHERE id = id1;
  UPDATE invoices SET state = 'valide' WHERE id = id1;
  r := fn_reserve(id1, '{"journal":"HA","lines":[]}');
  ASSERT r = 'reserve', 'réservation nominale';

  -- 5. PDF ré-exporté : autre hash binaire, même contenu métier => doublon
  INSERT INTO invoices (source_message_id, file_sha256) VALUES ('msg-2', repeat('b', 64)) RETURNING id INTO id2;
  UPDATE invoices SET state = 'extrait', doc_type = 'facture', supplier_siret = '11111111100014',
         invoice_number = 'fa 2026 42', invoice_date = '2026-09-30',
         total_ht = 1000.00, total_tva = 200.00, total_ttc = 1200.00 WHERE id = id2;
  UPDATE invoices SET state = 'valide' WHERE id = id2;
  r := fn_reserve(id2, '{}');
  ASSERT r = 'doublon', 'PDF ré-exporté non détecté';
  SELECT count(*) INTO n FROM outbox;
  ASSERT n = 1, 'un doublon a créé un message outbox';

  -- 6. Avoir : même numéro, type différent, montant négatif => pas un doublon
  INSERT INTO invoices (source_message_id, file_sha256) VALUES ('msg-3', repeat('c', 64)) RETURNING id INTO id3;
  BEGIN
    UPDATE invoices SET state = 'extrait', doc_type = 'avoir', total_ttc = 120.00 WHERE id = id3;
    RAISE EXCEPTION 'avoir positif accepté';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
  UPDATE invoices SET state = 'extrait', doc_type = 'avoir', supplier_siret = '11111111100014',
         invoice_number = 'FA-2026/0042', related_invoice_number = 'FA-2026/0042',
         invoice_date = '2026-10-05', total_ht = -100.00, total_tva = -20.00, total_ttc = -120.00 WHERE id = id3;
  UPDATE invoices SET state = 'valide' WHERE id = id3;
  ASSERT fn_reserve(id3, '{}') = 'reserve', 'avoir confondu avec un doublon';

  -- 7. Worker : prise outbox -> ecrit ; un second worker ne reprend rien
  SELECT count(*) INTO n FROM fn_claim_outbox(10);
  ASSERT n = 2, 'claim outbox';
  SELECT count(*) INTO n FROM fn_claim_outbox(10);
  ASSERT n = 0, 'double prise outbox';
  ASSERT (SELECT state FROM invoices WHERE id = id1) = 'ecrit', 'état ecrit';

  -- 8. Timeout API compta : incertain, puis réconciliation qui RETROUVE l'écriture
  PERFORM fn_mark_uncertain(id1, 'timeout 30s');
  ASSERT fn_reconcile(id1, true, 'ECR-000123') = 'confirme', 'réconciliation found';
  ASSERT (SELECT status FROM outbox WHERE invoice_id = id1) = 'confirmed', 'outbox confirmée';

  -- 9. Crash du worker après envoi (bail expiré) : réconciliation, écriture ABSENTE => renvoi sûr
  UPDATE outbox SET locked_until = now() - interval '1 minute' WHERE invoice_id = id3;
  SELECT count(*) INTO n FROM v_reconcile_candidates WHERE invoice_id = id3;
  ASSERT n = 1, 'worker mort non détecté';
  ASSERT fn_reconcile(id3, false) = 'reserve', 'réconciliation absent';
  ASSERT (SELECT status FROM outbox WHERE invoice_id = id3) = 'pending', 'outbox remise en file';

  -- 10. Confirmé sans référence externe : impossible
  BEGIN
    UPDATE invoices SET state = 'confirme' WHERE id = id2;
    RAISE EXCEPTION 'confirmé sans ref accepté';
  EXCEPTION WHEN check_violation THEN NULL;
  END;

  -- 11. Audit append-only
  BEGIN
    DELETE FROM audit;
    RAISE EXCEPTION 'audit supprimable';
  EXCEPTION WHEN raise_exception THEN
    IF SQLERRM NOT LIKE '%append-only%' THEN RAISE; END IF;
  END;

  RAISE NOTICE 'OK : tous les tests du schéma passent';
END $$;

SELECT invoice_id, from_state, to_state FROM state_transitions ORDER BY id;

-- Fonctions appelées par n8n : extraction, contrôles, revue humaine
DO $$
DECLARE id5 bigint; j jsonb; st invoice_state;
BEGIN
  INSERT INTO purchase_order_lines VALUES ('PO-1001', 1, 'PANNEAU-CTP', 40, 32.50, 20);
  INSERT INTO goods_receipts VALUES ('GR-1', 'PO-1001', 'PANNEAU-CTP', 40, now());
  INSERT INTO supplier_bank_accounts VALUES ('111111111', 'FR4830001000010000000001101', now());
  INSERT INTO invoices (source_message_id, file_sha256) VALUES ('msg-5', repeat('d', 64)) RETURNING id INTO id5;
  st := fn_save_extraction(id5, '{}', '{}',
        '{"doc_type":"facture","supplier_siret":"11111111100014","invoice_number":"FA-2026/0099",
          "invoice_date":"2026-10-10","po_number":"PO-1001","total_ht":130,"total_tva":26,"total_ttc":156,
          "iban":"FR4830001000010000000001101",
          "lines":[{"sku":"PANNEAU-CTP","qty":4,"unit_price_ht":32.5,"vat_rate":20}]}', ARRAY['total_ttc']);
  ASSERT st = 'en_revue', 'désaccord => en_revue';
  -- le réviseur corrige le TTC : retour à extrait, contrôles à refaire
  ASSERT fn_apply_review(id5, 'didier', 'valider', '{"total_ttc":156}') = 'extrait', 'revue -> extrait';
  j := fn_load_for_checks(id5);
  ASSERT (j->'received'->>'PANNEAU-CTP')::numeric = 40, 'réceptions chargées';
  ASSERT j->'known_ibans' ? 'FR4830001000010000000001101', 'IBAN connus chargés';
  ASSERT (j->>'acomptes_ht')::numeric = 0, 'acomptes';
  ASSERT fn_save_checks(id5, '{"summary":"ok"}', 0.97, true) = 'valide', 'contrôles OK => valide';
  RAISE NOTICE 'OK : fonctions n8n (extraction, revue, contrôles)';
  RAISE NOTICE 'load_for_checks = %', j;
END $$;
