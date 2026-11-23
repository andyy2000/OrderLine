"""Runs on a computer at the restaurant: polls for new orders and prints kitchen tickets.

    python -m scripts.print_agent --server https://your-server --restaurant tops-china \
        --token ADMIN_TOKEN --printer 192.168.1.50

With --printer it prints to a network ESC/POS thermal printer (pip install python-escpos).
Without it, tickets are printed to the terminal, which is handy for testing.
Polling (instead of the server pushing to the printer) means no ports need to be
opened on the restaurant's router.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request


def fetch(url: str, token: str, method: str = "GET") -> object:
    req = urllib.request.Request(url, method=method, headers={"X-Admin-Token": token} if token else {})
    if method == "POST":
        req.data = b"{}"
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def make_printer(host: str | None):
    if not host:
        return lambda text: print(text, flush=True)
    from escpos.printer import Network  # python-escpos

    printer = Network(host)

    def print_ticket(text: str) -> None:
        # Order number, pickup time and name in big type so they're readable across the kitchen.
        head, _, body = text.partition("\n" + "-" * 42 + "\n")
        printer.set(align="left", bold=True, double_height=True, double_width=False)
        printer.text(head + "\n")
        printer.set(align="left", bold=False, double_height=False, double_width=False)
        printer.text("-" * 42 + "\n" + body)
        printer.text("\n\n")
        printer.cut()

    return print_ticket


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--server", default="http://localhost:8000")
    p.add_argument("--restaurant", default="tops-china")
    p.add_argument("--token", default="")
    p.add_argument("--printer", help="printer IP address; omit to print to the terminal")
    p.add_argument("--every", type=float, default=2.0, help="seconds between checks")
    p.add_argument("--once", action="store_true", help="check once and exit")
    args = p.parse_args()

    print_ticket = make_printer(args.printer)
    query = urllib.parse.urlencode({"restaurant": args.restaurant})
    while True:
        try:
            for job in fetch(f"{args.server}/api/print-queue?{query}", args.token):
                print_ticket(job["ticket"])
                fetch(f"{args.server}/api/orders/{job['id']}/printed?{query}", args.token, method="POST")
        except Exception as e:  # keep running through network blips
            print(f"[print agent] {e}", flush=True)
        if args.once:
            break
        time.sleep(args.every)


if __name__ == "__main__":
    main()
