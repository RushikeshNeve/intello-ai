# M14 contracts

## M14.02
Ingestion scan runs inside `DefaultPipelineRunner` between extract and chunk (`modules/ingestion/scan/`). It is not a checkpointed stage (`STAGES` is unchanged): it re-runs idempotently on resume (findings replaced per version in one transaction).
- Policy `document_scan_policies` (workspace row, else org default, else `flag`): `off` | `flag` (index, show findings) | `quarantine` (a `high` finding sets `knowledge_revisions.state='quarantined'`; no chunks, embeddings, index writes or publication; job ends `succeeded`/`quarantined`).
- Fails closed: scanner error in `quarantine` mode raises `RetryableJobError("scan_failed")`; in `flag` mode indexing proceeds.
- `document_findings` (rule, severity, page/offset/entry location, preview <= 40 chars redacted; FORCE RLS, admin read, worker write; unique per version/rule/location). Rules: hidden_instruction, invisible_unicode, hidden_text (HTML), suspicious_link, office_macro, embedded_executable, embedded_object, pdf_active_content. Not available from extractors: PDF white/tiny text, text-to-visible mismatch.
- API (workspace admin = `workspace.update`): `GET/PUT .../scan-policy`, `GET .../documents/{d}/findings`, `GET .../quarantine`, `POST .../quarantine/{d}/release` (records `document_scan_releases`, definer `ingest_release_quarantine` re-queues the job), `DELETE .../quarantine/{d}` (normal deletion; also removes findings).
- Non-admins see a quarantined document's ingestion state as `processing` (status/jobs mapping). ORM `REVISION_STATES` is unchanged (an existing test pins it); migration 0024 is the authority for the `quarantined` CHECK.
