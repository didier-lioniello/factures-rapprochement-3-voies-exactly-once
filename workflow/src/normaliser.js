// Mode : Run Once for Each Item
// Unifie Gmail et le webhook : un item = un PDF, avec un identifiant de message stable.
const j = $json;
const binKey = Object.keys($binary || {}).find(k => ($binary[k].mimeType || '').includes('pdf'));
if (!binKey) throw new Error('Aucun PDF dans le message');
const sourceId = j.id                                   // Gmail : id du message
  || j.headers?.['idempotency-key']                     // portail fournisseur
  || j.body?.message_id;
if (!sourceId) throw new Error('Pas d\'identifiant de message : refus (pas d\'idempotence possible)');
return { json: { source_message_id: String(sourceId), binary_key: binKey },
         binary: { pdf: $binary[binKey] } };
