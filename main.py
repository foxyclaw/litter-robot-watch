#!/usr/bin/env python3
"""
Watchdog Services — multi-function monitoring daemon.
No LLM required at runtime.

Usage:
    python main.py daemon    # run continuous monitoring
"""
import asyncio
import importlib
import logging
import os
import sys
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
        return TelegramNotifier(tg["bot_token"], str(tg["chat_id"]))
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


async def main() -> None:
    config_path = Path(__file__).parent / "config.yaml"
    config = load_config(config_path)
    setup_logging(config)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "daemon":
        logging.info("Watchdog Services starting…")
        await daemon(config)
    else:
        print("Usage: python main.py daemon")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
