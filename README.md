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

`docker-compose.yml` avvia l'hub (uvicorn, HTTP e WebSocket sulla stessa porta), Postgres e un processo di manutenzione oraria. Le variabili sono in `.env`: parti da [.env.example](.env.example).

- **Un solo processo** con canali in memoria: basta per molti agenti e qualche migliaio di lavori al giorno.
- **Per scalare** su più processi (`WEB_CONCURRENCY > 1`) serve un channel layer condiviso: [Valkey](https://valkey.io), il fork di Redis con licenza BSD, impostando `CHANNEL_LAYER_URL=redis://…` (va bene anche Redis). Senza, l'hub si rifiuta di partire: un lavoro creato in un processo non avviserebbe l'agente collegato a un altro. Le istruzioni sono in testa a `docker-compose.yml`.
- **Rete di sicurezza**: ogni connessione controlla nel database ogni 5 secondi se ci sono lavori non consegnati. Se una notifica va persa, il lavoro arriva comunque, con qualche secondo di ritardo.
- **Manutenzione** (`manage.py housekeeping`, ogni ora): segna scaduti i lavori non partiti e cancella i file più vecchi di `PRINTHUB_FILE_RETENTION_DAYS` (default 7). Fatture e foto contengono dati personali: dei lavori vecchi restano solo i metadati.
- Dietro cloudflared, HTTPS e WebSocket (`wss://`) passano senza configurazioni particolari.
