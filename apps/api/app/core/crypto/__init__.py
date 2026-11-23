"""Envelope encryption and key management (M14.08, docs/architecture/m14-contracts.md ## M14.08).

* A master key (KEK) lives outside the database behind :class:`~.providers.KeyProvider`
  (``LocalFileKeyProvider`` by default; cloud KMS adapters are documented stubs).
* Each organization gets data keys (DEK) in ``tenant_keys`` (migration 0026): random 32 bytes,
  stored ONLY wrapped by the KEK. One ``active`` DEK per org seals new secrets; ``retired`` DEKs
  keep opening older rows until they are re-encrypted.
* :class:`~.envelope.TenantCipher` seals a secret with the org's active DEK (key id
  ``dek:<uuid>``) and still opens rows sealed by the previous single connector key (dual read).
* Deleting an org's DEK rows (``crypto-shred``) leaves its ``dek:`` ciphertexts unrecoverable.

Key material is never logged, returned, audited or put in an exception message; ``repr`` of every
object holding a key shows only ids.
"""

from app.core.crypto.errors import CryptoError, CryptoNotConfigured, DecryptionFailed, Sealed

__all__ = ["CryptoError", "CryptoNotConfigured", "DecryptionFailed", "Sealed"]
