#!/usr/bin/env python3
"""Assemble workflow/n8n-workflow.json à partir des fichiers JS de src/ (identifiants factices)."""
import json, uuid
from pathlib import Path

SRC = Path(__file__).parent
NS = uuid.UUID("6f1c2a52-0000-4000-8000-000000000000")
def nid(name): return str(uuid.uuid5(NS, name))
def js(name): return (SRC / name).read_text(encoding="utf-8")

PG = {"postgres": {"id": "PG_CRED_FACTICE_01", "name": "Postgres AP (factice)"}}
HDR = {"httpHeaderAuth": {"id": "HTTP_CRED_FACTICE_01", "name": "API compta (factice)"}}
OCR = {"httpHeaderAuth": {"id": "HTTP_CRED_FACTICE_02", "name": "Service OCR (factice)"}}
LLM = {"httpHeaderAuth": {"id": "HTTP_CRED_FACTICE_03", "name": "API LLM vision (factice)"}}
SLACK = {"slackApi": {"id": "SLACK_CRED_FACTICE_01", "name": "Slack (factice)"}}
GMAIL = {"gmailOAuth2": {"id": "GMAIL_CRED_FACTICE_01", "name": "Gmail factures (factice)"}}

nodes, connections = [], {}

def node(name, type_, version, pos, params, creds=None, **extra):
    n = {"parameters": params, "id": nid(name), "name": name, "type": type_,
         "typeVersion": version, "position": pos}
    if creds: n["credentials"] = creds
    n.update(extra)
    nodes.append(n)
    return name

def connect(src, dst, out=0, inp=0):
    outs = connections.setdefault(src, {"main": []})["main"]
    while len(outs) <= out: outs.append([])
    outs[out].append({"node": dst, "type": "main", "index": inp})

def pg(name, pos, query, replacement=None):
    params = {"operation": "executeQuery", "query": query, "options": {}}
    if replacement: params["options"]["queryReplacement"] = replacement
    return node(name, "n8n-nodes-base.postgres", 2.5, pos, params, PG)

def if_node(name, pos, left, op_type, operation, right=None):
    cond = {"id": nid(name + "-c"), "leftValue": left, "operator": {"type": op_type, "operation": operation}}
    if right is None: cond["operator"]["singleValue"] = True; cond["rightValue"] = ""
    else: cond["rightValue"] = right
    return node(name, "n8n-nodes-base.if", 2, pos, {
        "conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
                       "conditions": [cond], "combinator": "and"}, "options": {}})

def switch(name, pos, left, keys):
    rules = [{"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                             "conditions": [{"id": nid(f"{name}-{k}"), "leftValue": left, "rightValue": k,
                                             "operator": {"type": "string", "operation": "equals"}}],
                             "combinator": "and"},
              "renameOutput": True, "outputKey": k} for k in keys]
    return node(name, "n8n-nodes-base.switch", 3, pos, {"rules": {"values": rules}, "options": {}})

def code(name, pos, src):
    return node(name, "n8n-nodes-base.code", 2, pos, {"mode": "runOnceForEachItem", "jsCode": js(src)})

def slack(name, pos, text):
    return node(name, "n8n-nodes-base.slack", 2.2, pos, {
        "select": "channel", "channelId": {"__rl": True, "value": "C0FACTICE00", "mode": "id"},
        "text": text, "otherOptions": {}}, SLACK)

def http(name, pos, method, url, creds, body=None, headers=None, binary=False, timeout=30000, on_error=None):
    p = {"method": method, "url": url, "authentication": "genericCredentialType",
         "genericAuthType": "httpHeaderAuth", "options": {"timeout": timeout}}
    if headers:
        p["sendHeaders"] = True
        p["headerParameters"] = {"parameters": [{"name": k, "value": v} for k, v in headers.items()]}
    if binary:
        p.update(sendBody=True, contentType="binaryData", inputDataFieldName="pdf")
    elif body is not None:
        p.update(sendBody=True, specifyBody="json", jsonBody=body)
    extra = {"onError": on_error} if on_error else {}
    if method == "GET": extra.setdefault("retryOnFail", True); extra.setdefault("maxTries", 3)
    return node(name, "n8n-nodes-base.httpRequest", 4.2, pos, p, creds, **extra)

# ---------------------------------------------------------------- 1. Ingestion
gm = node("Gmail : factures entrantes", "n8n-nodes-base.gmailTrigger", 1.2, [0, 200], {
    "pollTimes": {"item": [{"mode": "everyMinute"}]}, "simple": False,
    "filters": {"q": "has:attachment filename:pdf label:factures-fournisseurs"},
    "options": {"downloadAttachments": True}}, GMAIL)
wh = node("Webhook : portail fournisseur", "n8n-nodes-base.webhook", 2, [0, 400], {
    "httpMethod": "POST", "path": "factures-entrantes-factice", "responseMode": "onReceived",
    "options": {"binaryPropertyName": "data"}}, webhookId=nid("webhook"))
nm = code("Normaliser l'entrée", [240, 300], "normaliser.js")
connect(gm, nm); connect(wh, nm)
h = node("Hash SHA-256 du PDF", "n8n-nodes-base.crypto", 1, [460, 300], {
    "action": "hash", "type": "SHA256", "binaryData": True, "binaryPropertyName": "pdf",
    "dataPropertyName": "file_sha256"})
connect(nm, h)
rec = pg("Enregistrer la réception (recu)", [680, 300],
         "INSERT INTO ap.invoices (source_message_id, file_sha256)\nVALUES ($1, $2)\n"
         "ON CONFLICT (source_message_id, file_sha256) DO NOTHING\nRETURNING id AS invoice_id;",
         "={{ [$json.source_message_id, $json.file_sha256] }}")
connect(h, rec)
new = if_node("Nouvelle réception ?", [900, 300], "={{ $json.invoice_id }}", "number", "exists")
connect(rec, new)
dup = node("Relance ignorée (idempotent)", "n8n-nodes-base.noOp", 1, [1120, 440], {})
connect(new, dup, out=1)
pdf = code("Réattacher le PDF", [1120, 200], "reattacher.js")
connect(new, pdf, out=0)

# ------------------------------------------------- 2. Double extraction + vote
ea = http("Extracteur A : OCR factures", [1340, 100], "POST",
          "https://ocr.example.invalid/v1/invoices:analyze", OCR, binary=True, timeout=120000)
eb = http("Extracteur B : LLM vision (JSON strict)", [1340, 320], "POST",
          "https://llm.example.invalid/v1/extract", LLM, binary=True, timeout=120000,
          headers={"X-Schema": "invoice-fr-v3", "X-Temperature": "0"})
connect(pdf, ea); connect(pdf, eb)
wa = node("Étiqueter A", "n8n-nodes-base.set", 3.4, [1560, 100], {"mode": "manual", "assignments": {"assignments": [
    {"id": nid("wa1"), "name": "invoice_id", "value": "={{ $('Réattacher le PDF').item.json.invoice_id }}", "type": "number"},
    {"id": nid("wa2"), "name": "extraction_a", "value": "={{ $json }}", "type": "object"}]},
    "includeOtherFields": False, "options": {}})
wb = node("Étiqueter B", "n8n-nodes-base.set", 3.4, [1560, 320], {"mode": "manual", "assignments": {"assignments": [
    {"id": nid("wb2"), "name": "extraction_b", "value": "={{ $json }}", "type": "object"}]},
    "includeOtherFields": False, "options": {}})
connect(ea, wa); connect(eb, wb)
mg = node("Réunir A et B", "n8n-nodes-base.merge", 3, [1780, 200],
          {"mode": "combine", "combineBy": "combineByPosition", "options": {}})
connect(wa, mg, inp=0); connect(wb, mg, inp=1)
vt = code("Vote champ par champ", [2000, 200], "vote.js")
connect(mg, vt)
sx = pg("Enregistrer extraction + vote", [2220, 200],
        "SELECT ap.fn_save_extraction($1, $2::jsonb, $3::jsonb, $4::jsonb, $5::text[]) AS state, $1::bigint AS invoice_id;",
        "={{ [$json.invoice_id, JSON.stringify($json.extraction_a), JSON.stringify($json.extraction_b), "
        "JSON.stringify($json.voted), '{' + $json.disagreements.join(',') + '}'] }}")
connect(vt, sx)
agree = if_node("Extracteurs d'accord ?", [2440, 200], "={{ $json.state }}", "string", "equals", "extrait")
connect(sx, agree)

# ---------------------------------------- 3. Contrôles + rapprochement 3 voies
ld = pg("Charger BC, réceptions, IBAN connus", [2660, 100],
        "SELECT ap.fn_load_for_checks($1) AS ctx;", "={{ [$json.invoice_id] }}")
connect(agree, ld, out=0)
ck = code("Contrôles déterministes + 3 voies", [2880, 100], "controles.js")
connect(ld, ck)
sc = pg("Enregistrer contrôles (valide / en_revue)", [3100, 100],
        "SELECT ap.fn_save_checks($1, $2::jsonb, $3, $4) AS state, $1::bigint AS invoice_id, $5::jsonb AS entry;",
        "={{ [$json.invoice_id, JSON.stringify($json.checks), $json.confidence, $json.pass, JSON.stringify($json.entry)] }}")
connect(ck, sc)
route = switch("Routage par état", [3320, 100], "={{ $json.state }}", ["valide", "en_revue"])
connect(sc, route)

# ----------------------------- 4. Réservation de l'empreinte + outbox (AVANT écriture)
rv = pg("Réserver empreinte + créer outbox", [3540, 0],
        "SELECT ap.fn_reserve($1, $2::jsonb) AS result, $1::bigint AS invoice_id;",
        "={{ [$json.invoice_id, JSON.stringify($json.entry)] }}")
connect(route, rv, out=0)
rr = switch("Réservé ou doublon ?", [3760, 0], "={{ $json.result }}", ["reserve", "doublon"])
connect(rv, rr)
ok = node("En file pour le worker outbox", "n8n-nodes-base.noOp", 1, [3980, -100], {})
connect(rr, ok, out=0)
sd = slack("Slack : doublon bloqué", [3980, 100],
           "=Doublon bloqué avant écriture : facture {{ $json.invoice_id }} (empreinte déjà réservée).")
connect(rr, sd, out=1)

# ---------------------------------------------------- 5. Revue humaine
sr = slack("Slack : facture à revoir", [3540, 300],
           "=Facture {{ $json.invoice_id }} à revoir. Formulaire : https://n8n.example.invalid/form/revue-facture")
connect(route, sr, out=1); connect(agree, sr, out=1)
fm = node("Formulaire : décision du réviseur", "n8n-nodes-base.formTrigger", 2.2, [2220, 560], {
    "formTitle": "Revue d'une facture fournisseur",
    "formDescription": "Valider (avec corrections éventuelles) ou rejeter. Une validation repasse par les contrôles : elle ne les force jamais.",
    "formFields": {"values": [
        {"fieldLabel": "invoice_id", "fieldType": "number", "requiredField": True},
        {"fieldLabel": "reviewer", "requiredField": True},
        {"fieldLabel": "decision", "fieldType": "dropdown", "requiredField": True,
         "fieldOptions": {"values": [{"option": "valider"}, {"option": "rejeter"}]}},
        {"fieldLabel": "corrections_json", "fieldType": "textarea", "placeholder": "{\"total_ttc\": 1476.00}"},
        {"fieldLabel": "commentaire", "fieldType": "textarea"}]},
    "options": {}}, webhookId=nid("form"))
ar = pg("Appliquer la décision (audit)", [2440, 560],
        "SELECT ap.fn_apply_review($1, $2, $3, $4::jsonb, $5) AS state, $1::bigint AS invoice_id;",
        "={{ [$json.invoice_id, $json.reviewer, $json.decision, $json.corrections_json || '{}', $json.commentaire || null] }}")
connect(fm, ar)
back = if_node("Validée → repasser les contrôles ?", [2660, 560], "={{ $json.state }}", "string", "equals", "extrait")
connect(ar, back); connect(back, ld, out=0)

# ---------------------------------------------------- 6. Worker outbox
tw = node("Toutes les minutes : worker outbox", "n8n-nodes-base.scheduleTrigger", 1.2, [0, 900],
          {"rule": {"interval": [{"field": "minutes", "minutesInterval": 1}]}})
cl = pg("Prendre un lot (SKIP LOCKED) → ecrit", [240, 900], "SELECT * FROM ap.fn_claim_outbox(10, interval '2 minutes');")
connect(tw, cl)
api = http("API compta : créer l'écriture", [460, 900], "POST", "https://compta.example.invalid/api/v1/entries", HDR,
           body="={{ JSON.stringify({ ...$json.payload, external_reference: $json.fingerprint }) }}",
           headers={"Idempotency-Key": "={{ $json.fingerprint }}"}, timeout=30000, on_error="continueErrorOutput")
connect(cl, api)
rd = http("Relire l'écriture (preuve)", [680, 820], "GET",
          "=https://compta.example.invalid/api/v1/entries/{{ $json.id }}", HDR, on_error="continueErrorOutput")
connect(api, rd, out=0)
cf = pg("Confirmer (confirme)", [900, 760], "SELECT ap.fn_confirm($1, $2);",
        "={{ [$('Prendre un lot (SKIP LOCKED) → ecrit').item.json.invoice_id, $json.id] }}")
connect(rd, cf, out=0)
un = pg("Marquer incertain (pas de renvoi)", [900, 1000], "SELECT ap.fn_mark_uncertain($1, $2);",
        "={{ [$('Prendre un lot (SKIP LOCKED) → ecrit').item.json.invoice_id, $json.error?.message || 'erreur API'] }}")
connect(api, un, out=1); connect(rd, un, out=1)

# ---------------------------------------------------- 7. Réconciliation
tr = node("Toutes les 15 min : réconciliation", "n8n-nodes-base.scheduleTrigger", 1.2, [0, 1300],
          {"rule": {"interval": [{"field": "minutes", "minutesInterval": 15}]}})
cr = pg("Candidats à réconcilier", [240, 1300], "SELECT * FROM ap.v_reconcile_candidates;")
connect(tr, cr)
sq = http("Chercher dans la compta par empreinte", [460, 1300], "GET",
          "=https://compta.example.invalid/api/v1/entries?external_reference={{ $json.fingerprint }}", HDR)
connect(cr, sq)
vd = code("Verdict : trouvée / absente", [680, 1300], "reconcilier.js")
connect(sq, vd)
rc = pg("Appliquer le verdict", [900, 1300], "SELECT ap.fn_reconcile($1, $2, $3) AS state;",
        "={{ [$json.invoice_id, $json.found, $json.external_ref] }}")
connect(vd, rc)
st = pg("Supervision : états bloqués", [1120, 1300], "SELECT * FROM ap.v_stuck;")
connect(rc, st)
hasst = if_node("Factures bloquées ?", [1340, 1300], "={{ $json.id }}", "number", "exists")
connect(st, hasst)
ss = slack("Slack : états bloqués", [1560, 1300],
           "=Facture {{ $json.id }} bloquée à l'état {{ $json.state }} depuis {{ $json.age }}.")
connect(hasst, ss, out=0)

# ---------------------------------------------------- 8. Erreurs
et = node("Error Trigger", "n8n-nodes-base.errorTrigger", 1, [0, 1600], {})
se = slack("Slack : erreur d'exécution", [240, 1600],
           "=Échec du workflow factures : {{ $json.execution.error.message }} (nœud {{ $json.execution.lastNodeExecuted }})")
connect(et, se)

# ---------------------------------------------------- Notes
def note(name, pos, text, w=420, h=160):
    node(name, "n8n-nodes-base.stickyNote", 1, pos, {"content": text, "width": w, "height": h})
note("Note : idempotence", [640, 20], "## Idempotence\nn8n ne garantit pas « exactement une fois ». "
     "UNIQUE(source_message_id, file_sha256) absorbe les relances ; l'empreinte métier UNIQUE est réservée "
     "dans Postgres AVANT tout appel à la compta.")
note("Note : outbox", [440, 640], "## Outbox\nAucun nœud d'ingestion n'appelle la compta. Seul le worker le fait, "
     "avec Idempotency-Key = empreinte. Timeout => incertain, jamais de renvoi aveugle.")
note("Note : aucun paiement", [1340, 560], "## Aucun paiement\nCe workflow passe des écritures comptables. "
     "Il ne déclenche jamais de virement.", h=120)

wf = {
    "name": "Factures fournisseurs → compta : rapprochement 3 voies, écriture exactement une fois (exemple)",
    "nodes": nodes, "connections": connections, "active": False,
    "settings": {"executionOrder": "v1", "saveManualExecutions": True, "callerPolicy": "workflowsFromSameOwner",
                 "errorWorkflow": ""},
    "pinData": {}, "meta": {"templateCredsSetupCompleted": False, "instanceId": "exemple-portfolio-factice"},
    "tags": [{"name": "exemple"}, {"name": "comptabilité"}],
}
out = SRC.parent / "n8n-workflow.json"
out.write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"{len(nodes)} nœuds, {sum(len(c) for v in connections.values() for c in v['main'])} connexions -> {out}")
