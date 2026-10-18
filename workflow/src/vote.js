// Mode : Run Once for Each Item
// Vote champ par champ entre l'extracteur A (OCR) et l'extracteur B (LLM vision).
// Une valeur n'est retenue que si les deux concordent après normalisation ; sinon null + revue.
const CRITICAL = ['doc_type','supplier_siret','invoice_number','invoice_date','po_number',
                  'related_invoice_number','total_ht','total_tva','total_ttc','iban','lines'];
const money = x => x == null ? null : Math.round(Number(String(x).replace(/\s/g,'').replace(',', '.')) * 100);
const normNum = n => n == null ? null : n.toUpperCase().replace(/(^|[^0-9])0+([0-9])/g, '$1$2').replace(/[^A-Z0-9]/g, '');
const normDate = s => { if (!s) return null; const m = s.match(/^(\d{2})[\/.](\d{2})[\/.](\d{4})$/);
                        return m ? `${m[3]}-${m[2]}-${m[1]}` : s; };
const normIban = s => s == null ? null : s.replace(/\s/g, '').toUpperCase();
const normLines = ls => ls == null ? null : JSON.stringify(ls.map(l =>
  [l.sku, Number(l.qty), money(l.unit_price_ht), Number(l.vat_rate)]));
const norm = (k, v) => {
  if (v == null) return null;
  if (k.startsWith('total_')) return money(v);
  if (k.endsWith('invoice_number')) return normNum(v);
  if (k === 'invoice_date') return normDate(v);
  if (k === 'iban') return normIban(v);
  if (k === 'lines') return normLines(v);
  return v;
};
const a = $json.extraction_a || {}, b = $json.extraction_b || {};
const voted = { supplier_name: a.supplier_name ?? b.supplier_name }, disagreements = [];
for (const k of CRITICAL) {
  const va = norm(k, a[k]), vb = norm(k, b[k]);
  if (va === vb) voted[k] = (k === 'invoice_date') ? va : (k === 'iban' ? va : a[k]);
  else { voted[k] = null; disagreements.push(k); }
}
return { json: { invoice_id: $json.invoice_id, extraction_a: a, extraction_b: b, voted, disagreements } };
