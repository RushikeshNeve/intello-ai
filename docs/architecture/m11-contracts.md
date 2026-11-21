# M11 contracts

## M11.06
- `ai/structured.py`: `generate_structured(gateway, messages, schema, max_repairs=1)` -> `StructuredResult(value|None, error, repaired)`; `parse_structured(raw, schema)` for already-obtained text.
- `gateway` = anything with `complete(messages, temperature=, max_tokens=)`; passes `response_format` only if it has `supports_json_mode = True`, else the prompt must ask for JSON.
- Fences and surrounding prose stripped; outermost `{...}` validated with pydantic.
- One repair call: prior reply (truncated) plus the validator error summary; the original prompt is not re-sent verbatim beyond the message list already used.
- `StructuredError.code`: `not_json`, `schema_mismatch`, `model_unavailable`; `detail` never holds model text.
- Counters (lazy, via `core.tracing.metrics()`): `ukos_structured_{parse_failures,repaired,failed}_total`.
- Adopted in `graph/extract.py` (`GraphReply` envelope, items still ontology-validated) and `research/planner.py` (`PlanReply`); research repair only when spare model-call budget remains, repair calls are counted in usage.
