# Paper run logging (draft)

Supabase `paper_runs` / `paper_events` append-only logger. **Never places orders.**
Rejects non-paper mode and refuses the live Alpaca host.

See `momentum/paper_run_logging.py` and `momentum/paper_run_hooks.py`.

```sh
cd worker && python -m unittest tests.test_paper_run_logging -v
```

If Supabase env is missing, logging warns and the observer keeps running.

## Live paper feed (signal / entry / exit / stop / skip)

Each observe cycle that runs the crypto paper loop also writes trade events to
`paper_events` under the current `paper_runs` row (one row per worker session/day):

| kind   | when |
|--------|------|
| signal | the rule fired (buy or sell), including fired-but-refused orders |
| entry  | a paper buy was submitted to the paper host |
| exit   | a paper sell for a non-stop reason |
| stop   | a paper sell because the 1% stop was hit |
| skip   | a pair held for a new reason, an order was refused, or the whole cycle was skipped (kill switch, paper lock) |

Payload fields: `symbol`, `asset_class`, `rule`, `side`, `price`, `qty`,
`stop_price`, `entry_price`, `pnl`, `client_order_id`, `reason`, `pattern`,
`bid`/`ask`/`mid`/`quote_at`, and the quote window (`window`, `window_start`,
`window_end`, up to the last 8 mids). `mode` is always `paper` and
`placed_by_logger` is always false.

Volume control: unchanged skips and unchanged blocker sets are not re-written
every cycle (they were most of the table's growth). Heartbeats still write
once per cycle.

Best effort: trade events are written after the paper order step, any write
failure is logged as a warning (exception type only) and stops the remaining
writes for that cycle. Nothing here can block, delay or fail an order.

The site reads these rows with the signed-in owner's session (existing
`paper_runs_select` / `paper_events_select` RLS, `owner_id = auth.uid()`).
No schema change, no new grants.
