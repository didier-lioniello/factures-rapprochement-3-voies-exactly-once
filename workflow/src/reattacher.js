// Mode : Run Once for Each Item
// Le nœud Postgres ne transmet pas le binaire : on réattache le PDF depuis le nœud de hash.
return { json: { invoice_id: $json.invoice_id },
         binary: $('Hash SHA-256 du PDF').item.binary };
