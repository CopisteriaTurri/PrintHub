# Print Hub

Progetto Django 5.2 (Python 3.13) con Channels: smista i lavori di stampa dai client agli agenti CLIPrint. Protocollo: `CLIPrint/docs/PROTOCOL.md` (repo CopisteriaTurri/CLIPrint), da tenere allineato a ogni modifica del comportamento.

## Comandi

```bash
.venv/bin/python manage.py test --noinput hub     # suite completa, ~1 s
.venv/bin/python -m black .
.venv/bin/ruff check --fix .
.venv/bin/coverage run manage.py test --noinput hub && .venv/bin/coverage report
```

`DJANGO_DEBUG=1` per lanciare il server in locale (senza serve `SECRET_KEY`).

## Struttura

- `hub/services.py` — **tutta la logica**: creazione lavori, idempotenza, stati, profili degli agenti. Non dipende dal trasporto.
- `hub/views.py` — API HTTP dei client e download dei file per gli agenti: solo adattatori verso `services`.
- `hub/consumers.py` — WebSocket degli agenti: solo adattatore verso `services` (via `database_sync_to_async`).
- `hub/notify.py` — messaggi dal codice sincrono alle connessioni WebSocket tramite channel layer.
- `hub/tokens.py` — API key (`phk_`), token agente (`pha_`), ID lavori (`job_`). Si salva solo l'hash SHA-256.

## Convenzioni

- Commenti, messaggi d'errore e nomi dei test in italiano; codici d'errore e campi dell'API in inglese, come nel protocollo.
- Ogni query parte dal tenant della credenziale: mai cercare un lavoro o un profilo solo per ID.
- Gli stati dei lavori non tornano mai indietro e da uno stato finale non si esce (`_AGENT_TRANSITIONS`).
- Le notifiche agli agenti partono in `transaction.on_commit`, mai prima che il dato sia salvato.
- Test WebSocket con `TransactionTestCase` (il consumer usa il DB da altri thread). Nei test con `captureOnCommitCallbacks`, il `mock.patch` va **fuori**: le callback partono all'uscita del context manager.
