# Paper risk guardrails (draft)

**Branch:** `feature/paper-risk-guardrails` (draft PR #8). Not merged, not deployed, not wired into the live observer.

## Scope

`worker/momentum/paper_risk.py` is an isolated, pure fail-closed screen for **paper entry intents only**.

- Feed age is evaluated against the free **IEX** quote/trade clock (`feed=iex`). Any other feed label (including SIP) is rejected.
- No broker HTTP, no order submission, no `PAPER_TRADING_ENABLED` flip.
- Never use `check_paper_intent` to block protective exits.

## Defaults

| Limit | Default |
| --- | --- |
| max order notional | $100 |
| max total exposure | $1000 |
| max daily loss | $50 |
| max open positions | 3 |
| max IEX feed age | 2000 ms |
| feed | `iex` |

## Tests

```bash
cd worker && python -m unittest tests.test_paper_risk -v
```

Offline only. Integration with `RiskManager` / paper session still needs a separate reviewed PR and Derek's yes before arming.
