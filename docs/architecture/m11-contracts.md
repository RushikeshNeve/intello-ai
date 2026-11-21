
## M11.07 Streaming hardening
- New additive run event `stage` `{"stage":"verifying"}` (migration 0012 widens `ck_chat_run_events_type`); searching/reading/writing are derived client-side from `started`/`retrieval`/`delta`. Old clients ignore it.
- Cancel: DB `chat_cancel_run` ends the run `cancelled`; the worker stops on its next fenced write or, while the model is silent, via a 1 s `run_still_running` probe. `POST /cancel` then stores the streamed deltas as an assistant message (`verification: {status:"unverified", stopped:"cancelled"}`, no citations, `[n]` markers stripped).
- Model timeout after text: run ends `completed` with the partial answer, `citations: []`, `verification.status = "unverified"`, `stopped: "timeout"` (never `grounded`). Timeout with no text stays `failed timeout`.
- `VerificationOut` gains status `unverified` and optional `stopped`. `GET /runs/{r}` returns the message for `cancelled` runs too.
- Resume: unchanged; replay is by `seq > Last-Event-ID`, client dedupes by seq (tested).
