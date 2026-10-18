// Mode : Run Once for Each Item
// Contrôles déterministes + rapprochement 3 voies (facture / BC / réception) + score de confiance.
// Entrée : le JSON renvoyé par ap.fn_load_for_checks(invoice_id).
const ctx = $json.ctx, f = ctx.fields;
const PRICE_TOL = 0.01, ACOMPTE_MAX = 0.40, AUTO_THRESHOLD = 0.90;
const c = x => Math.round(Number(x) * 100);                    // centimes entiers, jamais de float
const reasons = [], penalties = [];

function ibanValid(s) {
  s = (s || '').replace(/\s/g, '').toUpperCase();
  if (!/^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$/.test(s)) return false;
  const r = (s.slice(4) + s.slice(0, 4)).replace(/[A-Z]/g, ch => String(ch.charCodeAt(0) - 55));
  let rem = 0; for (const d of r) rem = (rem * 10 + Number(d)) % 97;
  return rem === 1;
}
function siretValid(s) {
  if (!/^\d{14}$/.test(s || '')) return false;
  if (s.startsWith('356000000')) return [...s].reduce((t, d) => t + +d, 0) % 5 === 0; // La Poste
  let t = 0;
  [...s].reverse().forEach((ch, i) => { let d = +ch; if (i % 2) { d *= 2; if (d > 9) d -= 9; } t += d; });
  return t % 10 === 0;
}

const lines = f.lines || [];
const lineHt = l => Math.round(Number(l.qty) * c(l.unit_price_ht));
if (lines.reduce((t, l) => t + lineHt(l), 0) !== c(f.total_ht)) reasons.push('somme des lignes ≠ total HT');
const byRate = {}; lines.forEach(l => byRate[l.vat_rate] = (byRate[l.vat_rate] || 0) + lineHt(l));
const tvaCalc = Object.entries(byRate).reduce((t, [r, b]) => t + Math.round(b * Number(r) / 100), 0);
if (Math.abs(tvaCalc - c(f.total_tva)) > Object.keys(byRate).length) reasons.push('TVA recalculée ≠ TVA imprimée');
if (c(f.total_ht) + c(f.total_tva) !== c(f.total_ttc)) reasons.push('HT + TVA ≠ TTC');
if (!siretValid(f.supplier_siret)) reasons.push('SIRET invalide (Luhn)');
if (!ibanValid(f.iban)) reasons.push('IBAN invalide (mod 97)');
else if (!ctx.known_ibans.includes(f.iban)) reasons.push("IBAN différent de l'IBAN connu du fournisseur");
if ((f.doc_type === 'avoir') !== (c(f.total_ttc) < 0)) reasons.push('signe incohérent avec le type de document');

const po = ctx.po;
if (!po) reasons.push('bon de commande introuvable');
else {
  if (po.supplier_siren !== (f.supplier_siret || '').slice(0, 9)) reasons.push("BC d'un autre fournisseur");
  const poLines = Object.fromEntries(po.lines.map(l => [l.sku, l]));
  const poHt = po.lines.reduce((t, l) => t + Math.round(Number(l.qty) * c(l.unit_price_ht)), 0);
  if (f.doc_type === 'acompte') {
    penalties.push(['acompte', 0.03]);
    if (c(f.total_ht) > poHt * ACOMPTE_MAX) reasons.push('acompte supérieur au plafond');
  } else {
    if (f.doc_type === 'avoir') {
      penalties.push(['avoir', 0.03]);
      if (ctx.original_ttc == null) reasons.push("avoir sans facture d'origine engagée");
      else if (-c(f.total_ttc) > c(ctx.original_ttc)) reasons.push("avoir supérieur à la facture d'origine");
    }
    if (f.doc_type === 'facture_solde') {
      penalties.push(['facture de solde', 0.03]);
      const deducted = -lines.filter(l => l.sku === 'ACOMPTE-DEDUIT').reduce((t, l) => t + lineHt(l), 0);
      if (deducted !== c(ctx.acomptes_ht)) reasons.push('acompte déduit ≠ acomptes déjà engagés');
    }
    for (const l of lines) {
      if (l.sku === 'ACOMPTE-DEDUIT') continue;
      const pl = poLines[l.sku];
      if (!pl) { reasons.push(`${l.sku} absent du BC`); continue; }
      const gap = Math.abs(c(l.unit_price_ht) - c(pl.unit_price_ht)) / c(pl.unit_price_ht);
      if (gap > PRICE_TOL) reasons.push(`${l.sku} : prix hors tolérance (${(gap * 100).toFixed(2)} %)`);
      else if (gap > 0) penalties.push([`${l.sku} : écart de prix toléré`, 0.05]);
      if (Number(l.vat_rate) !== Number(pl.vat_rate)) reasons.push(`${l.sku} : taux de TVA ≠ BC`);
      if (f.doc_type !== 'avoir') {
        const already = Number(ctx.already_invoiced[l.sku] || 0), rec = Number(ctx.received[l.sku] || 0);
        if (already + Number(l.qty) > rec) reasons.push(`${l.sku} : quantité facturée > quantité reçue`);
      }
    }
  }
}
const confidence = reasons.length ? 0 : Math.max(0, 1 - penalties.reduce((t, [, p]) => t + p, 0));
const pass = reasons.length === 0 && confidence >= AUTO_THRESHOLD;
return { json: { invoice_id: ctx.invoice_id, pass, confidence,
  checks: { reasons, penalties, summary: reasons.join(' ; ') || 'contrôles OK' },
  // écriture comptable préparée maintenant, envoyée plus tard par le worker outbox
  entry: { journal: 'HA', date: f.invoice_date, piece: f.invoice_number, supplier_siret: f.supplier_siret,
           doc_type: f.doc_type, total_ht: f.total_ht, total_tva: f.total_tva, total_ttc: f.total_ttc } } };
