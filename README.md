<p align="center"><img src="hub/static/hub/logo.svg" alt="Print Hub" width="360"></p>

# Print Hub

Smista i lavori di stampa dalle applicazioni (CopyaGo, altri gestionali, script, n8n…) agli agenti CLIPrint installati sui PC con le stampanti.

```
[CopyaGo] ──HTTPS──▶ [Print Hub] ◀──WebSocket in uscita── [CLIPrint sul PC]
```

- I client mandano un file pronto (PDF, PNG, JPEG) con il nome di un **profilo**: `POST /api/v1/jobs`.
- Gli agenti si collegano **loro** all'hub, quindi non hanno porte aperte, e ricevono i lavori appena arrivano.
- Contro le stampe doppie: `Idempotency-Key` obbligatoria, scadenza dei lavori, stati che non tornano mai indietro.

## API in breve

Autenticazione: `Authorization: Bearer phk_…` (API key del client).

| Metodo | Percorso | Cosa fa |
|---|---|---|
| `GET` | `/api/v1/profiles` | Profili utilizzabili, con opzioni e stato online dell'agente |
| `POST` | `/api/v1/jobs` | Nuovo lavoro: multipart con `file` e `meta` (JSON), header `Idempotency-Key` |
| `GET` | `/api/v1/jobs/<id>` | Stato di un lavoro |
| `GET` | `/api/v1/jobs?client_ref=…` | Lavori di un documento (es. per chiedere «ristampare?») |
| `POST` | `/api/v1/jobs/<id>/cancel` | Annulla |

Esempio:

```bash
curl -H "Authorization: Bearer $KEY" -H "Idempotency-Key: fattura-2026-0412-1" \
     -F file=@fattura.pdf \
     -F 'meta={"profile":"fattura","title":"Fattura 2026/0412","client_ref":"fatture.Fattura:4521"}' \
     https://printhub.example.com/api/v1/jobs
```

Gli errori hanno sempre la forma `{"error": {"code": "...", "message": "..."}}`, con i codici del protocollo (§7).

Agenti: WebSocket su `/agent/v1` con `Authorization: Bearer pha_…`; il file di un lavoro si scarica da `/agent/v1/jobs/<id>/file`.

## Credenziali

Si creano da riga di comando. Il valore in chiaro viene mostrato **una sola volta**; nel database resta solo l'hash.

```bash
python manage.py create_tenant copisteria "Copisteria Turri"
python manage.py create_agent copisteria pc-banco                  # token per CLIPrint
python manage.py create_api_key copisteria CopyaGo                 # key per un'applicazione
python manage.py create_api_key copisteria telefono --profile etichetta   # limitata a un profilo
```

La revoca si fa dall'admin (`/admin/`, azione «Revoca»): un agente revocato viene disconnesso subito.

## Sviluppo

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv -r requirements-dev.txt
export DJANGO_DEBUG=1
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/uvicorn printhub.asgi:application --reload
```

Senza `DATABASE_URL` usa SQLite; senza `CHANNEL_LAYER_URL` i canali restano in memoria.

Test, formattazione e lint:

```bash
.venv/bin/python manage.py test --noinput hub
.venv/bin/python -m black .
.venv/bin/ruff check --fix .
```

## Produzione

`docker-compose.yml` avvia l'hub (uvicorn, HTTP e WebSocket sulla stessa porta) e Postgres. Le variabili sono in `.env`: parti da [.env.example](.env.example).

- **Un solo processo** con canali in memoria: basta per molti agenti e qualche migliaio di lavori al giorno.
- **Per scalare** su più processi (`WEB_CONCURRENCY > 1`) serve un channel layer condiviso: [Valkey](https://valkey.io), il fork di Redis con licenza BSD, impostando `CHANNEL_LAYER_URL=redis://…` (va bene anche Redis). Senza, l'hub si rifiuta di partire: un lavoro creato in un processo non avviserebbe l'agente collegato a un altro. Le istruzioni sono in testa a `docker-compose.yml`.
- **Rete di sicurezza**: ogni connessione controlla nel database ogni 5 secondi se ci sono lavori non consegnati. Se una notifica va persa, il lavoro arriva comunque, con qualche secondo di ritardo.
- **Manutenzione** (`manage.py housekeeping`, ogni ora): segna scaduti i lavori non partiti e cancella i file più vecchi di `PRINTHUB_FILE_RETENTION_DAYS` (default 7). Fatture e foto contengono dati personali: dei lavori vecchi restano solo i metadati. Va lanciata dal cron dell'host (su TrueNAS: System → Advanced → Cron Jobs, utente `root`, ogni ora):

  ```bash
  docker compose -f /percorso/printhub/docker-compose.yml exec -T printhub python manage.py housekeeping
  ```

  `-T` perché cron non ha un terminale. Se un'ora salta (hub spento) non si perde niente: il giro successivo recupera tutto quello che è scaduto nel frattempo.
- **File dei lavori** in `/app/media`, montato sull'host: devono sopravvivere quando il container viene ricreato a ogni deploy, altrimenti i lavori in coda in quel momento perdono il file e l'agente non riesce a scaricarlo. La cartella dev'essere scrivibile dall'utente del container: `chown 1000:1000 /mnt/nvme/docker/printhub/media`.
- Dietro cloudflared, HTTPS e WebSocket (`wss://`) passano senza configurazioni particolari.
- **Solo LAN su `http://`**: `PRINTHUB_SECURE_COOKIES=False`, altrimenti il browser non rimanda il cookie di sessione e il login all'admin fallisce. Client e agenti non usano cookie: per loro non cambia niente. Gli agenti in questo caso vogliono `allow_insecure = true` in `agent.toml`.
- **Versione**: il builder passa `--build-arg APP_VERSION=$(git describe --tags --always)`; si vede nell'header dell'admin e in `/healthz`.

### Presenza degli agenti

Un agente è online se non revocato, ha una connessione registrata e ha dato
riscontro negli ultimi `PRINTHUB_AGENT_OFFLINE_AFTER` secondi (default: tre
intervalli di heartbeat). Al limite esatto è ancora online. Il margine tollera
ritardi temporanei; dopo un arresto improvviso la presenza scade anche se il
canale è rimasto registrato, e si ripristina al successivo contatto.

### Lavori potenzialmente bloccati

In admin i lavori consegnati o nello spooler senza riscontri da oltre
`PRINTHUB_JOB_STALLED_AFTER` secondi (default 300) sono segnalati come
potenzialmente bloccati. Il limite esatto non è ancora bloccato; in assenza
di riscontri si usa la data di creazione. È un avviso diagnostico: non modifica
lo stato e non ristampa. Controllare la stampante prima di ristampare.

### Storico dei lavori

Il dettaglio admin mostra eventi registrati dall'hub a partire dall'attivazione
della funzione. Gli orari sono quelli dell'hub; un tentativo di offerta non
prova la consegna né la stampa. Housekeeping elimina gli eventi oltre
`PRINTHUB_EVENT_RETENTION_DAYS` giorni (default 90), senza eliminare i lavori.

### Salute e manutenzione

`/healthz` indica che il processo HTTP risponde; `/readyz` controlla database,
storage e channel layer e restituisce 503 se una dipendenza non risponde. Il
probe è isolato in un processo terminato dopo `PRINTHUB_READINESS_TIMEOUT`
secondi (default 3): configurare intervalli dei probe adeguati al costo di
avvio. Il risultato è conservato in memoria per cinque secondi per processo;
una verifica già in corso limita le richieste contemporanee. Usare un supervisore per la liveness e monitorare separatamente la
readiness. Eseguire `manage.py housekeeping` via cron almeno ogni ora; lo stato
admin segnala manutenzione oltre `PRINTHUB_HOUSEKEEPING_STALE_AFTER` secondi
(default tre ore), senza compromettere la readiness.

## Licenza

Print Hub è software libero, copyleft: chiunque può usarlo, modificarlo e
ridistribuirlo, purché le versioni derivate restino sotto la stessa licenza.

Lo distribuiamo sotto GNU General Public License, versione 3 **o successiva**
(GPL-3.0-or-later); il testo completo è nel file [LICENSE](LICENSE).

Il programma è distribuito nella speranza che sia utile, ma **senza alcuna
garanzia**, nemmeno quella implicita di commerciabilità o di idoneità a uno
scopo particolare: vedi il testo della licenza per i dettagli.
