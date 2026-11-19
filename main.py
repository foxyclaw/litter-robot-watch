#!/usr/bin/env python3
"""
Watchdog Services — multi-function monitoring daemon.
No LLM required at runtime.

Usage:
    python main.py daemon      # run continuous monitoring
    python main.py snapshot    # read-only smoke test: one Litter-Robot snapshot
                               # -> DB + JSONL, detection in dry-run (no Telegram)
"""
import asyncio
import importlib
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Make sure our directory is on sys.path so checks can import database, notifier, etc.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from notifier import TelegramNotifier
from scheduler import CheckScheduler

# Registry: config name → dotted class path
CHECK_REGISTRY = {
    "litter_robot": "checks.litter_robot.LitterRobotCheck",
    "stock_price": "checks.stock_price.StockPriceCheck",
}


def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def setup_logging(config: dict) -> None:
    log_cfg = config.get("logging", {})
    level = getattr(logging, log_cfg.get("log_level", "INFO").upper(), logging.INFO)
    log_file = log_cfg.get("log_file")
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=level,
        handlers=handlers,
        format="%(asctime)s %(name)-20s %(levelname)-8s %(message)s",
    )


def build_global_notifier(config: dict) -> TelegramNotifier | None:
    tg = config.get("telegram", {})
    if tg.get("bot_token") and tg.get("chat_id"):
        outbox = Path(__file__).parent / "data" / "telegram_outbox.json"
        notifier = TelegramNotifier(tg["bot_token"], str(tg["chat_id"]), outbox_path=str(outbox))
        # Optional: Telegram user IDs whose direct messages the bot also answers.
        notifier.authorized_user_ids = [str(u) for u in tg.get("authorized_user_ids") or []]
        notifier.ensure_retry_task()  # deliver anything left over from a previous run
        return notifier
    return None


def load_check_class(dotted_path: str):
    module_path, class_name = dotted_path.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


async def daemon(config: dict) -> None:
    global_notifier = build_global_notifier(config)
    scheduler = CheckScheduler()
    bot_tasks = []

    for check_cfg in config.get("checks", []):
        if not check_cfg.get("enabled", True):
            continue
        name = check_cfg["name"]
        if name not in CHECK_REGISTRY:
            logging.warning(f"Unknown check '{name}' — skipping")
            continue
        cls = load_check_class(CHECK_REGISTRY[name])
        check = cls(config=check_cfg.get("config", {}), notifier=global_notifier)
        scheduler.add_check(check, check_cfg.get("interval_seconds", 60))

        # If check exposes a bot handler, wire it up
        if hasattr(check, "make_bot_handler"):
            bot = await check.make_bot_handler()
            if bot:
                bot_tasks.append(bot.run_forever())
                logging.info(f"Bot handler registered for check: {name}")

    await asyncio.gather(scheduler.run_forever(), *bot_tasks)


async def snapshot(config: dict) -> int:
    """Read-only smoke test. Talks to Whisker (read only), never to Telegram."""
    from checks.litter_robot import LitterRobotCheck
    from detection import LOGGED_FIELDS, format_changes

    check_cfg = next(
        (c for c in config.get("checks", []) if c.get("name") == "litter_robot"), None
    )
    if not check_cfg:
        print("No litter_robot check in config.yaml")
        return 1
    check = LitterRobotCheck(config=check_cfg.get("config", {}), notifier=None, dry_run=True)
    check.telegram = None  # belt and braces: nothing can be sent
    try:
        results = await check.snapshot_once()
    except Exception as e:
        print(f"SMOKE TEST FAILED: {type(e).__name__}: {e}")
        return 1
    for r in results:
        f = r["fields"]
        print(f"\n=== {r['name']} — snapshot OK ({r['raw_keys']} raw keys) ===")
        print(f"  stored in DB   : {r['db']} (table raw_snapshots)")
        print(f"  appended JSONL : {r['jsonl']}")
        print(f"  activity feed  : {r['new_activity']} new entries stored, {r['activity_total']} total kept locally")
        for key in LOGGED_FIELDS:
            print(f"  {key:<18}: {f.get(key)}")
        if r["previous_ts"]:
            prev = datetime.fromtimestamp(r["previous_ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            print(f"  changes since previous snapshot ({prev}): {format_changes(r['changes']) or 'none'}")
        else:
            print("  changes: n/a (first snapshot)")
        if r["alerts"]:
            print("  detection (dry-run) WOULD ALERT:")
            for a in r["alerts"]:
                print(f"    [{a.key}/{a.kind}] {a.plain}")
        else:
            print("  detection (dry-run): no alerts")
    print("\nSMOKE TEST OK — nothing was sent to Telegram, no robot state was changed.")
    return 0


async def main() -> None:
    config_path = Path(__file__).parent / "config.yaml"
    config = load_config(config_path)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "snapshot":
        # one-off command: log to the terminal only, not into the daemon's log file
        logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                            format="%(asctime)s %(name)-20s %(levelname)-8s %(message)s")
    else:
        setup_logging(config)

    if cmd == "daemon":
        logging.info("Watchdog Services starting…")
        await daemon(config)
    elif cmd == "snapshot":
        sys.exit(await snapshot(config))
    else:
        print("Usage: python main.py daemon | snapshot")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
