// Exécute les nœuds Code (vote.js, controles.js) hors n8n sur le jeu synthétique d'eval/
// et compare leurs décisions à l'implémentation de référence Python (eval/sorties/sortie_complet.json).
//   node workflow/src/test_code_nodes.js
const fs = require('fs'), path = require('path'), crypto = require('crypto');
const EVAL = path.join(__dirname, '..', '..', 'eval');
const load = f => JSON.parse(fs.readFileSync(path.join(EVAL, f), 'utf8'));
const runNode = (file, $json) => new Function('$json', fs.readFileSync(path.join(__dirname, file), 'utf8'))($json).json;
const refs = load('donnees/references.json'), A = load('donnees/extraction_a.json').invoices,
      B = load('donnees/extraction_b.json').invoices, expected = load('sorties/sortie_complet.json').invoices;
const normNum = n => n.toUpperCase().replace(/(^|[^0-9])0+([0-9])/g, '$1$2').replace(/[^A-Z0-9]/g, '');
const seen = new Set(), fps = new Set(), posted = [];
let failures = 0;
A.forEach((a, i) => {
  let decision;
  const src = a.source_message_id + a.file_sha256;
  if (seen.has(src)) decision = 'doublon';
  else {
    seen.add(src);
    const v = runNode('vote.js', { invoice_id: i, extraction_a: a.fields, extraction_b: B[i].fields });
    if (v.disagreements.length) decision = 'revue';
    else {
      const f = v.voted, siren = f.supplier_siret.slice(0, 9);
      const fp = crypto.createHash('sha256').update([siren, f.doc_type, normNum(f.invoice_number),
        Math.round(f.total_ttc * 100), f.invoice_date].join('|')).digest('hex');
      if (fps.has(fp)) decision = 'doublon';
      else {
        const same = posted.filter(p => p.po_number === f.po_number);
        const already = {}; same.filter(p => p.doc_type !== 'acompte').forEach(p => p.lines.forEach(l => {
          if (l.sku !== 'ACOMPTE-DEDUIT') already[l.sku] = (already[l.sku] || 0) + l.qty; }));
        const orig = posted.find(p => p.supplier_siret.slice(0, 9) === siren && p.doc_type !== 'avoir'
          && f.related_invoice_number && normNum(p.invoice_number) === normNum(f.related_invoice_number));
        const ctx = { invoice_id: i, fields: f, po: refs.purchase_orders[f.po_number] || null,
          received: refs.goods_receipts[f.po_number] || {}, already_invoiced: already,
          acomptes_ht: same.filter(p => p.doc_type === 'acompte').reduce((t, p) => t + p.total_ht, 0),
          original_ttc: orig ? orig.total_ttc : null, known_ibans: refs.suppliers[siren]?.known_ibans || [] };
        const r = runNode('controles.js', { ctx });
        decision = r.pass ? 'auto' : 'revue';
        if (r.pass) { fps.add(fp); posted.push(f); }
      }
    }
  }
  const exp = expected[i].decision, ok = exp === decision;
  if (!ok) failures++;
  console.log(`${a.id}  nœuds n8n : ${decision.padEnd(8)} référence Python : ${exp.padEnd(8)} ${ok ? 'OK' : 'ÉCART'}`);
});
console.log(failures ? `${failures} écart(s)` : 'OK : les nœuds Code et la référence Python décident pareil sur les 15 factures');
process.exit(failures ? 1 : 0);
