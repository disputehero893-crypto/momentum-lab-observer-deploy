# Paper resume after a latched kill switch

Paper only. The ordinary halt endpoints (`POST /api/halt`, `POST /api/kill-switch`)
still refuse `enabled=false` with `remote_resume_is_not_supported`. This is the one
sanctioned way to clear the latch.

## How it is authenticated

A `resume_entries` row in `public.paper_commands`. The browser insert policy
(`paper_commands_insert`) still allows queued `halt_new_entries` rows only, so a
signed-in dashboard user cannot queue a resume. Only a server-side writer (service
role / database owner) can insert one. The worker reads it with its own owner-scoped
server key, filtered to its `OWNER_USER_ID`.

Schema change (applied once; widens the kind check only, policies unchanged):

```sql
alter table public.paper_commands drop constraint paper_commands_kind_check;
alter table public.paper_commands add constraint paper_commands_kind_check
  check (kind = any (array['halt_new_entries'::text, 'resume_entries'::text]));
```

Queue a resume:

```sql
insert into public.paper_commands (id, owner_id, kind, nonce, status)
values (gen_random_uuid(), '<owner uuid>', 'resume_entries', gen_random_uuid(), 'queued');
```

## When the worker resumes

During a `serve-observe` cycle, after the paper account, positions and open orders
were read successfully in that same cycle, the worker clears the latch only if all hold:

- mode is `paper`, and the trading hosts are `paper-api.alpaca.markets` (never live);
- `PAPER_REVIEWED_CONFIG_HASH` matches `Config.review_hash()` and `paper_blockers()` is empty;
- the broker paper check passed this cycle;
- no halt command arrived in the same cycle (a halt always wins);
- the request is at most 15 minutes old and not from the future;
- the latch is readable and is not an emergency flatten request.

Otherwise the row is marked `failed` with one refusal code in `error`, and the latch
stays. Any channel or validation error leaves the latch unchanged.

## What is reported (no secrets)

- `worker_state.payload.kill_switch`: `enabled`, `flatten_requested`, bounded `reason`.
- `worker_state.payload.paper_config`: `hash_status` (`match`/`mismatch`/`unset`/`unavailable`),
  `computed_hash` (digest of non-secret settings, for the owner to approve), `blockers`.
- `/health`: `kill_switch` and `paper_config_hash_status`.
- Logs: one startup line with the latch reason and hash status, and one line per resume request.
