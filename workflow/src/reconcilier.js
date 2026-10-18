// Mode : Run Once for Each Item
// La compta a été interrogée par référence externe = empreinte. Verdict sans ambiguïté :
// trouvée => on confirme (surtout ne pas renvoyer) ; absente => renvoi sûr.
const hits = ($json.entries || $json.data || []).filter(e => e.external_reference === $('Candidats à réconcilier').item.json.fingerprint);
if (hits.length > 1) throw new Error('Plusieurs écritures pour une même empreinte : alerte humaine');
return { json: { invoice_id: $('Candidats à réconcilier').item.json.invoice_id,
                 found: hits.length === 1, external_ref: hits[0]?.id ?? null } };
