"""Token degli agenti e API key dei client (PROTOCOL.md §2).

Sono 32 byte casuali: con questa entropia basta un hash SHA-256 (non serve un
hash lento come per le password), e si può cercare direttamente per hash.
Il valore in chiaro esiste solo al momento della creazione.
"""

import hashlib
import secrets

API_KEY_PREFIX = "phk_"
AGENT_TOKEN_PREFIX = "pha_"

# Alfabeto Crockford base32: niente I, L, O, U, che si confondono a leggerli.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_secret(prefix):
    """Restituisce (valore in chiaro, hash). Il prefisso dice a colpo d'occhio
    di che credenziale si tratta, anche in un log."""
    value = prefix + secrets.token_urlsafe(32)
    return value, hash_secret(value)


def hash_secret(value):
    return hashlib.sha256(value.encode()).hexdigest()


def new_job_id():
    """ID pubblico di un lavoro: `job_` + 26 caratteri base32 (130 bit casuali)."""
    n = secrets.randbits(130)
    chars = []
    for _ in range(26):
        n, r = divmod(n, 32)
        chars.append(_CROCKFORD[r])
    return "job_" + "".join(chars)
