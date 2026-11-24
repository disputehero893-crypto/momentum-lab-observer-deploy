"""CLI. All default commands are synthetic or local; preflight is explicitly read-only."""
import argparse
import json
from pathlib import Path
import sys
import signal
import threading
import os
from contextlib import contextmanager

from .config import Config, ConfigError
from .demo import run_demo
from .journal import Journal
from .lease import WorkerLease
from .server import Application, make_server


@contextmanager
def shutdown_signals(stop_event):
    """A supervisor stop requests cooperative shutdown before releasing ownership."""
    old = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            old[sig] = signal.signal(sig, lambda *_: stop_event.set())
    try:
        yield
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Momentum Lab: synthetic replay and paper-only components")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Run the deterministic invented market fixture; no network calls")
    demo.add_argument("--run-id", default="demo-v1")
    sub.add_parser("readiness", help="Print sanitized configuration and actual release gates")
    sub.add_parser("serve", help="Serve authenticated demo and journal API; does not start paper trading")
    observe = sub.add_parser("observe", help="Run GET-only paper observations; never submits or cancels orders")
    observe.add_argument("--once", action="store_true", help="Observe one bounded cycle, then stop")
    sub.add_parser("serve-observe", help="Run GET-only observations with the authenticated HTTP API")
    autopilot = sub.add_parser("serve-autopilot", help="Run account-bound paper executor; starts paused until explicit activation")
    autopilot.add_argument("--account-id", default=os.environ.get("AUTOPILOT_ACCOUNT_ID", ""),
                           help="Verified paper broker account UUID, never an API key")
    customer = sub.add_parser("serve-customer-paper", help="Opt-in owner-bound paper worker with authenticated customer controls")
    customer.add_argument("--account-id", default=os.environ.get("AUTOPILOT_ACCOUNT_ID", ""))
    sub.add_parser("paper-preflight", help="Explicitly read paper account/calendar/data; never submits orders")
    publish = sub.add_parser("publish-demo", help="Insert an existing synthetic export in configured Supabase")
    publish.add_argument("--run-id", default="demo-v1")
    args = parser.parse_args(argv)
    try:
        config = Config.from_env()
        if args.command == "demo":
            with WorkerLease(config.db_path):
                result = run_demo(config, run_id=args.run_id)
            print(json.dumps({"run_id": result["run_id"], "mode": result["mode"],
                              "provenance": result["provenance"], "metrics": result["metrics"],
                              "export": str(Path(config.export_dir)/f"{args.run_id}.json")}, indent=2))
        elif args.command == "readiness":
            print(json.dumps(Application(config).readiness(), indent=2))
        elif args.command == "serve":
            with WorkerLease(config.db_path):
                server = make_server(config)
                print(f"Momentum Lab listening on {config.host}:{server.server_port}; paper execution is not running", flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    server.server_close()
        elif args.command in {"serve-autopilot", "serve-customer-paper"}:
            from .autopilot import PaperAutopilot
            if not config.control_token:
                raise ConfigError("Autopilot requires server-side control authentication")
            gateway = None
            if args.command == "serve-customer-paper":
                from .customer_runtime import build_customer_paper_runtime
                from .customer_cloud import strict_json
                try:
                    raw = os.environ.get("CUSTOMER_REVIEWED_STRATEGIES_JSON", "{}").encode()
                    if len(raw) > 16384:
                        raise ValueError
                    registry = strict_json(raw)
                    if not isinstance(registry, dict):
                        raise ValueError
                except (ValueError, TypeError, UnicodeError, RecursionError):
                    raise ConfigError("Invalid server-reviewed customer strategy registry") from None
                config, runner, gateway = build_customer_paper_runtime(config,
                    account_id=args.account_id,
                    publishable_key=os.environ.get("SUPABASE_PUBLISHABLE_KEY", ""),
                    expected_settings_version=os.environ.get("CUSTOMER_SETTINGS_VERSION", ""),
                    reviewed_strategy_ids=registry)
            else:
                runner = PaperAutopilot(config, account_id=args.account_id)
            with runner as worker, shutdown_signals(worker.stop_event):
                server = make_server(config, Application(config, autopilot=worker, customer_gateway=gateway))
                thread = threading.Thread(target=server.serve_forever, name="autopilot-api", daemon=True)
                thread.start()
                print("TactiRise paper autopilot service started; authorization and reconciliation gates apply", flush=True)
                try:
                    worker.run()
                finally:
                    worker.stop_event.set()
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)
        elif args.command in {"observe", "serve-observe"}:
            from .observation import ObservationWorker
            with ObservationWorker(config) as worker, shutdown_signals(worker.stop_event):
                server = None
                thread = None
                if args.command == "serve-observe":
                    server = make_server(config, Application(config, observation_worker=worker))
                    thread = threading.Thread(target=server.serve_forever, name="observation-api", daemon=True)
                    thread.start()
                print("Paper observation started; broker transport is GET-only; execution disabled", flush=True)
                try:
                    count = worker.run(max_cycles=1 if getattr(args, "once", False) else None)
                    last = worker.state()["latest_cycle"] or {}
                    print(json.dumps({"observation_cycles": count, "execution_enabled": False,
                                      "orders_submitted": 0, "last_error_code": last.get("error_code")}))
                    if args.command == "observe" and args.once and last.get("error_code"):
                        return 2
                finally:
                    worker.stop_event.set()
                    if server is not None:
                        server.shutdown()
                        server.server_close()
                        thread.join(timeout=2)
        elif args.command == "paper-preflight":
            from .observation import ReadOnlyPaperClient
            from .data import SnapshotBuilder
            with WorkerLease(config.db_path):
                client = ReadOnlyPaperClient(config)
                account = client.account()
                positions = client.positions()
                orders = client.open_orders()
                snapshots = SnapshotBuilder(client, config).build_watchlist() if config.watchlist else []
                result = {"mode": "paper", "read_only": True, "account_status": account.get("status"),
                          "positions_count": len(positions), "open_orders_count": len(orders),
                          "symbols_received": [row["symbol"] for row in snapshots],
                          "account_endpoint_reachable": True, "execution_armed": False,
                          "orders_submitted": 0, "reconciliation_complete": False,
                          "note": "Endpoint reads alone do not establish strategy ownership or launch readiness."}
                print(json.dumps(result, indent=2))
        elif args.command == "publish-demo":
            from .publisher import Publisher
            row = Journal(config.db_path).event_by_key(f"demo:{args.run_id}:complete")
            if not row:
                raise ValueError("Run is absent from this journal; run the demo first")
            Publisher(config).publish_demo(row["payload"])
            print(json.dumps({"published": True, "mode": "demo", "run_id": args.run_id}))
    except (ConfigError, ValueError, RuntimeError, OSError) as exc:
        print(f"Worker stopped: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
