"""One supported launcher for the local paper bot and explicit research tools."""

import argparse
import os
from pathlib import Path
import sys


def build_parser():
    parser = argparse.ArgumentParser(prog="crypto-bot", description=(
        "Start the local paper-trading dashboard. Opening the server does not start trading; "
        "use Start in the dashboard to begin or resume the paper account."),
        epilog="Advanced tools: crypto-bot analyze --help | crypto-bot research --help | crypto-bot paper --help")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: local computer only).")
    parser.add_argument("--port", type=int, default=8000, help="Dashboard port (default: 8000).")
    parser.add_argument("--data-dir", type=Path, help="Use a separate persistent directory for this bot's account, history, and analysis reports.")
    return parser


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"analyze", "research", "paper"}:
        if args[0] == "analyze":
            from trading_bot.analysis.cli import main as command
        elif args[0] == "research":
            from trading_bot.research.cli import main as command
        else:
            from trading_bot.paper.cli import main as command
        return command(args[1:])
    parser = build_parser()
    options = parser.parse_args(args)
    if not 1 <= options.port <= 65535:
        parser.error("--port must be between 1 and 65535.")
    if options.data_dir is not None:
        root = options.data_dir.expanduser().resolve()
        os.environ.update(TRADING_DATA_DIR=str(root), TRADING_HISTORY_ROOT=str(root / "history"),
            TRADING_PAPER_JOURNAL=str(root / "paper" / "core_paper.db"),
            TRADING_ANALYSIS_DB=str(root / "analysis" / "reports.db"))
    # A single process owns the background worker. Deliberately no reload or
    # multi-worker switch: the journal lease also rejects competing writers.
    import uvicorn
    uvicorn.run("trading_bot.web.app:app", host=options.host, port=options.port, workers=1)
    return 0

