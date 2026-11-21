# M11 contracts

## M11.03/04

- Embedding cache (`ai/embeddings/cache.py`, migration `0013_embedding_cache`, flag `EMBEDDING_CACHE_ENABLED`, default off): key `(org_id, embedding_space_id, sha256(NFC text))` -> float32 vector; the text is never stored. Per-org keys + FORCE RLS by org on purpose: a global cache would let identical text in two tenants share a row, leaking its existence through hits/timing. Space change (model, dims, prefixes, spec version) always misses.
- Ingestion embeds through `CachingEmbedder` (only misses reach the embedder); queries use an in-process LRU `(org, space, hash)`. Eviction: `evict_org(session, max_rows, max_age_days)` per org tenant session (no scheduler wired yet).
- Retrieval cache (`retrieval/cache.py`, hook in `service.gather`, flag `RETRIEVAL_CACHE_ENABLED`, TTL `RETRIEVAL_CACHE_TTL_SECONDS`=30): caches the fused candidates per (org, user, workspace-set hash, query hash, mode, limits, embedding space, corpus version); skipped with a reranker, never stores degraded results.
- Corpus version: per-org counter (Redis, else in-process; Redis error = bypass). Bumped by `bump_corpus` / `bump_from_session` in document finalize, document delete, grant/revoke and ingestion publish. A hit still goes through `finish` -> `revalidate`, so liveness/authorization are re-checked every time.
- Migration chain: 0013 declares `down_revision = 0011_member_preapprovals` (no 0012 in this branch); relink at merge if 0012 lands first. `embedding_cache` is registered in `ORG_SCOPED_TABLES` and the RLS catalog test.
