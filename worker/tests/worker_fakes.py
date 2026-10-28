"""Entirely invented provider; never opens a network connection."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from momentum.alpaca import PaperClient, BrokerError
from momentum.config import Config
from momentum.demo import synthetic_snapshot


def armed_config(directory):
    metadata = Path(directory)/"symbols.json"
    metadata.write_text("{}")
    cfg = Config(mode="paper", paper_enabled=True, api_key="FAKE-KEY", api_secret="FAKE-SECRET",
                 watchlist=("DEMO-A", "DEMO-B", "DEMO-C"), metadata_path=str(metadata),
                 db_path=str(Path(directory)/"journal.sqlite3"), export_dir=str(Path(directory)/"exports"))
    return replace(cfg, paper_reviewed_config_hash=cfg.review_hash())


class FakeVenue:
    def __init__(self, config):
        self.now = datetime.fromisoformat("2026-10-16T13:35:05+00:00")
        self.orders = {}
        self.calls = []
        self.positions = {}
        self.cash = Decimal("10000")
        self.realized = Decimal(0)
        self.fees = Decimal(0)
        self.bid = Decimal("9.81")
        self.ambiguous_post = False
        self.post_commits = True
        self.cancel_failure = False
        self.cancel_is_pending = True
        self.client = PaperClient(config, transport=self.request, clock=lambda: self.now)

    def advance(self, seconds=1):
        self.now += timedelta(seconds=seconds)

    def snapshot(self, symbol="DEMO-A", bid="9.81", ask="9.82"):
        snapshot = synthetic_snapshot(symbol, self.now.isoformat())
        snapshot["feed"] = "iex"
        snapshot["provenance"] = "injected_fake_provider"
        snapshot["quote"].update(feed="iex", bid=bid, ask=ask)
        snapshot["last_trade"].update(feed="iex", price=ask)
        snapshot["rvol"]["feed"] = "iex"
        for row in [*snapshot["bars"], *snapshot["rvol"]["history"]]:
            row["feed"] = "iex"
        self.bid = Decimal(bid)
        return snapshot

    def ledger(self):
        return {"complete": True, "session_date": "2026-10-16", "as_of": self.now.isoformat(),
                "realized_pnl": str(self.realized), "fees": str(self.fees), "source": "injected_fake_execution_ledger"}

    def request(self, method, url, headers, payload):
        parsed = urlsplit(url)
        self.calls.append({"method": method, "url": url, "payload": deepcopy(payload)})
        path = parsed.path
        if method == "GET" and path == "/v2/account":
            value = sum(self.bid*p["qty"] for p in self.positions.values())
            return {"status": "ACTIVE", "trading_blocked": False, "account_blocked": False,
                    "equity": str(self.cash+value), "buying_power": str(self.cash)}
        if method == "GET" and path == "/v2/positions":
            return [{"symbol": symbol, "side": "long", "qty": str(p["qty"]), "avg_entry_price": str(p["entry"]),
                     "unrealized_pl": str((self.bid-p["entry"])*p["qty"])} for symbol, p in self.positions.items() if p["qty"]]
        if method == "GET" and path == "/v2/orders":
            return deepcopy([o for o in self.orders.values() if o["status"] not in {"filled", "canceled", "expired", "rejected"}])
        if method == "GET" and path == "/v2/orders:by_client_order_id":
            client_id = parse_qs(parsed.query)["client_order_id"][0]
            if client_id not in self.orders:
                raise BrokerError("Fake order not found", 404)
            return deepcopy(self.orders[client_id])
        if method == "POST" and path == "/v2/orders":
            order = {**payload, "id": "fake-"+str(len(self.orders)+1), "filled_qty": "0",
                     "filled_avg_price": None, "status": "new", "submitted_at": self.now.isoformat(),
                     "updated_at": self.now.isoformat(), "filled_at": None}
            if self.post_commits:
                self.orders[payload["client_order_id"]] = order
            if self.ambiguous_post:
                raise BrokerError("Injected fake response timeout")
            return deepcopy(order)
        if method == "DELETE" and path.startswith("/v2/orders/"):
            if self.cancel_failure:
                raise BrokerError("Injected fake cancellation timeout")
            order = next(o for o in self.orders.values() if o["id"] == path.rsplit("/", 1)[1])
            order["status"] = "pending_cancel" if self.cancel_is_pending else "canceled"
            order["updated_at"] = self.now.isoformat()
            return None
        raise AssertionError("Unexpected fake request: "+method+" "+url)

    def fill(self, client_id, quantity, price):
        order = self.orders[client_id]
        old = int(order["filled_qty"])
        quantity = int(quantity)
        price = Decimal(str(price))
        if old+quantity > int(order["qty"]):
            raise AssertionError("Fake overfill")
        old_cost = Decimal(order["filled_avg_price"] or "0")*old
        order["filled_qty"] = str(old+quantity)
        order["filled_avg_price"] = str((old_cost+price*quantity)/(old+quantity))
        order["status"] = "filled" if old+quantity == int(order["qty"]) else "partially_filled"
        order["updated_at"] = self.now.isoformat()
        if order["status"] == "filled":
            order["filled_at"] = self.now.isoformat()
        symbol = order["symbol"]
        fee = Decimal("0.003")*quantity
        self.fees += fee
        if order["side"] == "buy":
            previous = self.positions.get(symbol, {"qty": 0, "entry": Decimal(0)})
            self.positions[symbol] = {"qty": previous["qty"]+quantity,
                                      "entry": (previous["qty"]*previous["entry"]+price*quantity)/(previous["qty"]+quantity)}
            self.cash -= quantity*price+fee
        else:
            previous = self.positions[symbol]
            self.realized += (price-previous["entry"])*quantity
            previous["qty"] -= quantity
            self.cash += quantity*price-fee
            if not previous["qty"]:
                del self.positions[symbol]

