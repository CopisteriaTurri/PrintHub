# Architettura di Print Hub

Come si incastrano i pezzi. Il **cosa** (messaggi, campi, codici d'errore) sta in
`CLIPrint/docs/PROTOCOL.md`; qui c'è il **come** e il **perché**, cioè quello che
dal codice si capisce solo leggendo più file insieme. I `§` rimandano a PROTOCOL.md.

## 1. Il quadro

```
 client (CopyaGo, n8n…)                                   agente CLIPrint (PC con stampanti)
        │  HTTP + API key phk_                                    ▲  WebSocket + token pha_
        ▼                                                         │
  hub/views.py ──► hub/services.py ◄── database_sync_to_async ── hub/consumers.py
                        │      ▲                                  ▲
                        │      └──────── DB (stato vero) ─────────┤ (controllo periodico)
                        ▼                                         │
               transaction.on_commit ──► hub/notify.py ──► channel layer ──► gruppo agent-<id>
```

- **Un processo ASGI** (uvicorn) serve HTTP e WebSocket: `printhub/asgi.py`
  manda `agent/v1` al consumer e tutto il resto a Django.
- **`services` è l'unico posto con le regole.** View, consumer, admin e comandi
  di gestione sono adattatori: leggono l'input, chiamano `services`, traducono
  il risultato (`HubError` → JSON con codice §7, oppure messaggio `error` sulla
  WebSocket).
- **Il database è la fonte di verità; il channel layer serve solo a dare la
  sveglia.** Una notifica persa ritarda un lavoro di qualche secondo, non lo
  perde (§4 sotto).

## 2. Credenziali e tenant

```
Tenant ─┬─ ApiKey (phk_, client)    ── allowed_profiles: vuoto = tutti
        ├─ Agent  (pha_, CLIPrint)  ── Profile (dichiarati dall'agente)
        └─ Job ── JobEvent
```

- Si salva solo lo SHA-256 del token (`tokens.py`): con 32 byte casuali basta e
  permette di cercare direttamente per hash. Il prefisso dice che credenziale è.
- Ogni query parte dal tenant della credenziale (`api_key.tenant`, `agent`).
  Un ID di lavoro di un altro tenant si comporta come inesistente; il download
  del file risponde 404 sia per "non esiste" sia per "non è tuo".
- Unica eccezione: la dashboard `operations.py`, riservata ai superuser, vede
  tutti i tenant, ma cerca comunque i lavori per `(tenant_id, pk)` e annulla o
  ristampa passando dalla API key originale del lavoro.
- Revoca: per le API key basta `revoked_at` (ogni richiesta HTTP si
  riautentica). Per gli agenti serve anche chiudere la WebSocket aperta, vedi §5.

## 3. Ciclo di vita di un lavoro

### Creazione (`services.create_job`, §3.2)

1. **Prima l'idempotenza.** Stessa `Idempotency-Key` sulla stessa API key:
   stesso hash di file + meta → si restituisce il lavoro esistente (HTTP 200
   invece di 201), anche se nel frattempo il profilo è sparito o l'agente è
   offline. Hash diverso → 409 `idempotency_conflict`. Due richieste identiche
   in parallelo: il vincolo unico fa perdere una delle due, che cancella il suo
   file e restituisce il lavoro dell'altra.
2. Validazione: profilo ammesso per la key ed esistente nel tenant, tipo del
   file dai primi byte (non dall'estensione), opzioni contro quelle dichiarate
   dal profilo, con i default aggiunti (`resolve_options`).
3. Il lavoro **congela** al momento della creazione l'agente, la stampante e il
   nome del profilo: lo storico resta leggibile anche se il profilo sparisce.
   Il file si salva col nome `<job_id>.<ext>`, senza dati personali.
4. Salvataggio + evento `created` in una transazione; la notifica all'agente
   parte in `on_commit`.

### Consegna

Un lavoro arriva all'agente per **tre strade**, tutte via `pending_offers`, che
segna `offered_at` e registra un evento `offer_attempt`:

| Strada | Quando | Cosa ripropone |
|---|---|---|
| `hub_job_new` | notifica di `create_job` (solo se l'`hello` è già fatto) | quel lavoro |
| risposta all'`hello` | a ogni connessione | tutti i `queued` e `dispatched` |
| `resync_loop` → `stale_offers` | ogni `PRINTHUB_RESYNC_INTERVAL` s | i `queued` mai proposti o proposti da oltre `PRINTHUB_REOFFER_AFTER` s |

Riproporre un lavoro è sempre sicuro: l'agente scarta i doppioni col suo
registro (§6). Per questo l'hub può permettersi di essere ridondante invece che
preciso.

L'agente scarica il file da `GET /agent/v1/jobs/<id>/file` (`can_download`:
solo l'agente assegnato, solo in `queued`/`dispatched`, solo se il file non è
già stato cancellato), poi risponde `job.ack` → `dispatched`.

### Stati (§4)

```
 queued ──► dispatched ──► spooled ──► completed | failed | canceled   (finali)
   │
   └──► expired   (finale, solo dall'hub)
```

- `_AGENT_TRANSITIONS` dice cosa può riportare l'agente: sempre in avanti, mai
  fuori da uno stato finale, saltare passaggi è ammesso (un agente può mandare
  direttamente `completed`). Il resto viene **ignorato e scritto nel log**, non
  rifiutato: un messaggio duplicato o in ritardo non deve far danni.
- Lo stesso stato ripetuto aggiorna `agent_seen_at`: è il "sono ancora vivo"
  del lavoro, su cui si basa il rilevamento dei lavori bloccati
  (`Job.stalled_filter`, `dispatched`/`spooled` senza riscontri da
  `PRINTHUB_JOB_STALLED_AFTER` s).
- Ogni transizione passa da `select_for_update` e lascia un `JobEvent`: lo
  storico di un lavoro si ricostruisce dagli eventi, non dai log.
- `job.reject` con `busy` lascia il lavoro in coda (solo un evento); con
  qualsiasi altro codice lo fa fallire.

### Annullamento (`cancel_job`, §3.4)

- `queued` o `dispatched`: l'hub lo segna subito `canceled`.
- `dispatched` o `spooled`: l'agente riceve `job.cancel`. Per `spooled` lo
  stato resta com'è finché l'agente non riporta l'esito: solo lui sa se è
  riuscito a toglierlo dalla coda del sistema operativo.
- Stato finale: 409 `not_cancelable`.

### Scadenza

Solo i lavori `queued` scadono (un lavoro già consegnato è in mano all'agente).
`expire_overdue` gira **in modo pigro** su ogni lettura (`get_job`,
`jobs_by_client_ref`, `pending_offers`) e in più col comando `housekeeping`:
così un client non vede mai `queued` un lavoro già scaduto, anche se il cron
non è passato.

## 4. Notifiche e channel layer

- `notify.py` manda al gruppo `agent-<id>` messaggi `hub.*`, che Channels
  consegna ai metodi `hub_*` del consumer: `hub.job_new`, `hub.job_cancel`,
  `hub.close`.
- Partono **sempre in `transaction.on_commit`**: altrimenti il consumer, che
  legge il DB da un'altra connessione, potrebbe cercare un lavoro non ancora
  salvato e non trovarlo.
- Con un solo processo basta il layer in memoria. Con `WEB_CONCURRENCY > 1` la
  richiesta HTTP e la WebSocket dell'agente possono stare in processi diversi:
  serve `CHANNEL_LAYER_URL` (Valkey o Redis), e `settings.py` si rifiuta di
  partire senza.
- Qualunque notifica può andare persa (riavvio, layer giù): il controllo
  periodico nel consumer è la rete di sicurezza sia per i lavori
  (`stale_offers`) sia per la revoca (`agent_revoked`).

## 5. La connessione dell'agente (`consumers.py`, §5)

1. **`connect`**: token nell'header `Authorization` (un browser non può
   impostarlo su una WebSocket, quindi niente controllo dell'Origin). Token
   non valido → chiusura prima di `accept`, cioè handshake fallito con 403.
   Poi `group_add` e `agent_connected`: **vale l'ultima connessione**, la
   precedente riceve `hub.close` con `replaced` (4409).
2. **`hello`**: prima dell'`hello` sono ammessi solo `hello` e `ping`, il resto
   chiude con 4400. `services.hello` controlla la versione del protocollo e che
   questa sia ancora la connessione registrata (`connection_replaced`), poi:
   - `sync_profiles`: allinea i profili. Il nome è unico nel tenant: **il primo
     agente che lo dichiara lo tiene**, gli altri ricevono `profile_conflict`.
     Un profilo malformato viene rifiutato da solo, senza bloccare gli altri; i
     profili non più dichiarati vengono cancellati e il nome torna libero.
   - `reconcile_in_flight`: applica gli stati dei lavori che l'agente aveva in
     corso prima di disconnettersi.
   - `welcome` + tutte le offerte in sospeso, poi parte `resync_loop`.
3. **Messaggi**: ogni handler è una riga verso `services`. I tipi sconosciuti
   si ignorano (§9): un agente più nuovo può mandare messaggi che questo hub non
   conosce. Un errore nel `resync_loop` (es. DB irraggiungibile) viene scritto
   nel log e si riprova al giro dopo, senza chiudere la connessione.
4. **`disconnect`**: azzera `channel_name` **solo se è ancora quella
   registrata**. Una connessione vecchia che si chiude dopo la nuova non deve
   far risultare l'agente offline.

`Agent.online` = connessione registrata + non revocato + `last_seen_at` entro
`PRINTHUB_AGENT_OFFLINE_AFTER` (ping, pong, `agent.status` lo aggiornano).

### Revoca di un agente

Tutte le strade (azione admin «Revoca», campo `revoked_at` modificato nella
scheda, eventuale codice futuro) passano da `services.revoke_agent`: salva
`revoked_at` e in `on_commit` manda `hub.close` → chiusura 4401. In più:

- `resync_loop` rilegge la revoca dal DB a ogni giro, nel caso la notifica vada
  persa (l'oggetto `self.agent` è quello caricato alla connessione e non la vede).
- `update_job`, `pending_offers`, `reject_job` e `agent_seen` ignorano un
  agente revocato: tra la revoca e la chiusura effettiva non cambia niente.
- `close_once`: notifica e controllo periodico possono arrivare in qualunque
  ordine, la chiusura parte una volta sola.

## 6. Operatività e manutenzione

- **`manage.py housekeeping`** (da cron): scade i lavori, cancella i file oltre
  `PRINTHUB_FILE_RETENTION_DAYS` (i metadati restano, `file_purged_at`) e gli
  eventi oltre `PRINTHUB_EVENT_RETENTION_DAYS`. Scrive l'esito in
  `MaintenanceStatus` (riga unica): la dashboard segnala un giro fallito o
  troppo vecchio. `purge_files` non tocca `updated_at`, che vale come "ultima
  transizione di stato".
- **`/healthz`** risponde sempre se il processo è vivo; **`/readyz`** lancia
  `check_readiness` in un **sottoprocesso con timeout** (DB, scrittura su
  storage, giro sul channel layer), così una dipendenza bloccata non blocca il
  server. Il risultato resta in cache 5 secondi.
- **Dashboard operativa** (`/admin/operations/`): lavori filtrabili, presenza
  degli agenti, annullamento, ristampa. La ristampa passa da `create_job` con la
  API key originale, `source="admin"` e come `Idempotency-Key` il nonce di un
  token firmato mostrato nella pagina di conferma: un doppio clic non crea due
  ristampe.
- Credenziali e tenant si creano solo da riga di comando (`create_tenant`,
  `create_api_key`, `create_agent`): il valore in chiaro si vede una volta sola.
  L'admin non permette di aggiungerli.

## 7. Webhook in uscita (estensione v2)

Opzionali: senza `WebhookDestination` non parte niente. Il formato del corpo e
della firma è nel README; qui c'è il percorso.

```
 services.record_event ──► enqueue_final ──► WebhookDelivery (outbox, stessa transazione)
                                                    │
 deliver_webhooks (worker) ── claim: lease 60 s + token ┘
        │
        └─► processo figlio `send_webhook <id> <token>` (timeout 20 s) ──► POST HTTPS firmato
                 │ codice di uscita: 0 / EXIT_TEMPORARY / EXIT_PERMANENT
                 ▼
        esito scritto solo se il token è ancora quello del lease ──► WebhookAttempt
```

- **L'outbox nasce con la transizione.** `enqueue_final` sta in `services`, non
  in `webhooks`: chi produce l'evento non dipende dal trasporto. Si accoda solo
  per `state_changed` verso uno stato finale, leggendo lo stato da
  `details["to"]` (la transizione registrata, non lo stato corrente). Un
  rollback non lascia consegne; il vincolo unico evento/destinazione e
  `ignore_conflicts` rendono innocuo un evento ripetuto.
- **Destinazioni per tenant**: tutte quelle abilitate del tenant del lavoro,
  senza API key o con la key del lavoro. In admin (solo superuser) tenant,
  API key e URL non si cambiano dopo la creazione: le consegne in coda puntano
  alla destinazione, cambiarla le manderebbe a un altro ricevente. Per un
  ricevente nuovo si crea una destinazione nuova.
- **Il worker non si blocca mai su un ricevente.** Ogni invio gira in un
  processo figlio con timeout; il lease scade dopo 60 s, così un worker morto
  libera la consegna. L'esito si scrive filtrando per `lease_token`: un figlio
  in ritardo dopo che un altro worker ha ripreso la consegna non sovrascrive
  niente. Una riconsegna dopo un crash è possibile: il ricevente deduplica per
  `X-PrintHub-Event`.
- **Uscita verso Internet, mai in LAN.** `validate_destination` vuole HTTPS
  sulla 443, host in `PRINTHUB_WEBHOOK_ALLOWED_HOSTS`, niente credenziali
  nell'URL. Al momento dell'invio il DNS viene risolto e basta un indirizzo non
  globale per rifiutare tutto (difesa dal DNS rebinding);
  `PinnedHTTPSConnection` si collega solo agli indirizzi validati, con il
  certificato verificato sull'hostname. Li prova alternando IPv6 e IPv4
  (`attempt_order`, la partenza ruota a ogni retry) entro `CONNECT_BUDGET`
  (10 s su 20 del figlio): un IPv6 rotto che va in timeout non esaurisce il
  tempo prima di arrivare a un IPv4. I redirect non si
  seguono (un 3xx è un errore permanente).
- **Permanente o temporaneo.** Permanenti: configurazione non valida,
  certificato non valido, risposta 3xx/4xx diversa da 429, destinazione
  disabilitata. Temporanei: segreto mancante nell'ambiente, rete, timeout,
  altri errori TLS, 429, 5xx, figlio che non parte o va in crash. I temporanei
  si ritentano con backoff esponenziale (massimo 1 h) fino a
  `PRINTHUB_WEBHOOK_MAX_ATTEMPTS`.
- `housekeeping` cancella le consegne concluse (riuscite o fallite) oltre
  `PRINTHUB_WEBHOOK_RETENTION_DAYS`; quelle ancora pendenti restano.

## 8. Dove mettere le mani

- **Nuovo messaggio dall'agente**: funzione in `services`, handler di una riga
  nel dizionario di `receive_json`, test in `test_agent_ws.py` (logica in
  `test_services.py`), sezione in PROTOCOL.md.
- **Nuovo messaggio verso l'agente**: funzione in `notify`, metodo `hub_*` nel
  consumer, chiamata in `on_commit` da `services`.
- **Nuovo stato o transizione**: `Job.State`, `_AGENT_TRANSITIONS`, eventuali
  `FINAL_STATES`/`PENDING_STATES`/`STALLED_STATES`, PROTOCOL.md §4.
- **Nuova regola di business**: in `services`, mai in view/consumer/admin.
- **Nuovo evento verso i webhook**: condizione e payload in
  `services.enqueue_final`; la classificazione degli errori di invio in
  `webhooks.send`. Aggiornare il README (formato) e i test in `test_webhooks.py`.
- Questo file va aggiornato quando cambia un flusso o un invariante descritto
  qui, non per ogni modifica.
