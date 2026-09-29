#!/usr/bin/env python3
"""Command line interface for Litter Robot Monitor."""
import asyncio
import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

import yaml

from database import LitterRobotDatabase
from monitor import LitterRobotMonitor, STATUS_CODES
from telegram_bot import TelegramBot


def setup_logging(log_file: Optional[str], log_level: str) -> None:
    """Configure logging."""
    level = getattr(logging, log_level.upper(), logging.INFO)
    
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers
    )


async def cmd_status(config: dict, args: argparse.Namespace) -> int:
    """Show current status of all robots."""
    db = LitterRobotDatabase(config.get("monitor", {}).get("database", "litter_robot.db"))
    await db.connect()
    
    monitor = LitterRobotMonitor(config, db)
    
    if not await monitor.connect(
        config["whisker"]["email"],
        config["whisker"]["password"]
    ):
        print("Failed to connect to Whisker API")
        return 1
    
    try:
        statuses = await monitor.get_current_status()
        
        if not statuses:
            print("No robots found")
            return 0
        
        print("\n📊 Litter Robot Status")
        print("=" * 50)
        
        for robot_id, state in statuses.items():
            name = state.get("name", "Unknown")
            status = state.get("status", "???")
            desc = state.get("status_description", STATUS_CODES.get(status, ("Unknown", ""))[0])
            waste = state.get("waste_drawer_level")
            
            # Status emoji
            status_type = state.get("status_type", "unknown")
            emoji = {
                "normal": "✅",
                "activity": "🔄",
                "warning": "⚠️",
                "error": "❌",
                "inactive": "⏸️",
            }.get(status_type, "❓")
            
            print(f"\n{name} ({robot_id[:8]}...)")
            print(f"  Status: {emoji} {desc}")
            if waste is not None:
                bar = "█" * (waste // 10) + "░" * (10 - waste // 10)
                print(f"  Waste:  [{bar}] {waste}%")
            print(f"  Power:  {state.get('power_status', 'Unknown')}")
            print(f"  Light:  {'On' if state.get('nightlight_status') else 'Off'}")
            print(f"  Lock:   {'On' if state.get('panel_locked') else 'Off'}")
            
    finally:
        await monitor.disconnect()
        await db.disconnect()
    
    return 0


async def cmd_history(config: dict, args: argparse.Namespace) -> int:
    """Show state history for a robot."""
    db = LitterRobotDatabase(config.get("monitor", {}).get("database", "litter_robot.db"))
    await db.connect()
    
    # If no robot_id specified, list available robots
    if not args.robot_id:
        monitor = LitterRobotMonitor(config, db)
        if await monitor.connect(config["whisker"]["email"], config["whisker"]["password"]):
            print("\nAvailable robots:")
            for rid, robot in monitor.robots.items():
                print(f"  {rid} - {robot.name}")
            await monitor.disconnect()
        await db.disconnect()
        return 0
    
    history = await db.get_state_history(args.robot_id, hours=args.hours)
    
    if not history:
        print(f"No history found for robot {args.robot_id}")
        return 0
    
    print(f"\n📈 State History (last {args.hours} hours)")
    print("=" * 60)
    
    for row in history[:args.limit]:
        ts = row.get("timestamp", "Unknown")
        status = row.get("status", "???")
        desc = row.get("status_description", STATUS_CODES.get(status, ("Unknown", ""))[0])
        waste = row.get("waste_drawer_level", "N/A")
        
        print(f"{ts} | {status:4} | Waste: {waste:3}% | {desc}")
    
    await db.disconnect()
    return 0


async def cmd_alerts(config: dict, args: argparse.Namespace) -> int:
    """Show and manage alerts."""
    db = LitterRobotDatabase(config.get("monitor", {}).get("database", "litter_robot.db"))
    await db.connect()
    
    if args.acknowledge:
        await db.acknowledge_alert(args.acknowledge)
        print(f"Alert {args.acknowledge} acknowledged")
    
    alerts = await db.get_alerts(
        robot_id=args.robot_id,
        unacknowledged_only=args.unacknowledged,
        limit=args.limit
    )
    
    if not alerts:
        print("No alerts found")
        await db.disconnect()
        return 0
    
    print("\n🚨 Alerts")
    print("=" * 70)
    
    for alert in alerts:
        status = "✓" if alert.get("acknowledged") else " "
        print(f"[{status}] {alert['id']:3} | {alert['timestamp']} | {alert['alert_type']:12} | {alert['message']}")
    
    await db.disconnect()
    return 0


async def cmd_daemon(config: dict, args: argparse.Namespace) -> int:
    """Run the monitoring daemon."""
    setup_logging(
        config.get("monitor", {}).get("log_file"),
        config.get("monitor", {}).get("log_level", "INFO")
    )
    logger = logging.getLogger(__name__)
    
    db = LitterRobotDatabase(config.get("monitor", {}).get("database", "litter_robot.db"))
    await db.connect()
    
    monitor = LitterRobotMonitor(config, db)
    
    # Add console alert handler
    def on_alert(robot_id: str, alert_type: str, message: str):
        print(f"\n🚨 ALERT: {message}\n")
    monitor.add_alert_handler(on_alert)
    
    if not await monitor.connect(
        config["whisker"]["email"],
        config["whisker"]["password"]
    ):
        logger.error("Failed to connect to Whisker API")
        return 1
    
    # Start Telegram bot if configured
    tg_config = config.get("notifications", {}).get("telegram", {})
    telegram_bot = None
    if tg_config.get("enabled") and tg_config.get("bot_token"):
        telegram_bot = TelegramBot(
            tg_config["bot_token"],
            tg_config["chat_id"],
            monitor,
            db
        )
        logger.info("Telegram command bot enabled")
    
    logger.info("Daemon started. Press Ctrl+C to stop.")
    
    try:
        # Run monitor and bot concurrently
        tasks = [monitor.run()]
        if telegram_bot:
            tasks.append(telegram_bot.start())
        
        await asyncio.gather(*tasks)
        
    except asyncio.CancelledError:
        pass
    finally:
        monitor.stop()
        if telegram_bot:
            telegram_bot.stop()
        await monitor.disconnect()
        await db.disconnect()
    
    return 0


async def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Litter Robot Monitor - Standalone monitoring service",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s status                    # Show current robot status
  %(prog)s history -r <robot_id>     # Show history for a robot
  %(prog)s alerts                    # Show all alerts
  %(prog)s daemon                    # Run monitoring daemon
        """
    )
    
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Configuration file path (default: config.yaml)"
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    
    # status command
    subparsers.add_parser("status", help="Show current robot status")
    
    # history command
    history_parser = subparsers.add_parser("history", help="Show state history")
    history_parser.add_argument("-r", "--robot-id", help="Robot ID")
    history_parser.add_argument("-H", "--hours", type=int, default=24, help="Hours to look back")
    history_parser.add_argument("-l", "--limit", type=int, default=50, help="Max entries")
    
    # alerts command
    alerts_parser = subparsers.add_parser("alerts", help="Show and manage alerts")
    alerts_parser.add_argument("-r", "--robot-id", help="Filter by robot ID")
    alerts_parser.add_argument("-u", "--unacknowledged", action="store_true", help="Only unacknowledged")
    alerts_parser.add_argument("-l", "--limit", type=int, default=50, help="Max entries")
    alerts_parser.add_argument("-a", "--acknowledge", type=int, help="Acknowledge alert by ID")
    
    # daemon command
    daemon_parser = subparsers.add_parser("daemon", help="Run monitoring daemon")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    # Load config
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config file not found: {config_path}")
        print("Copy config.yaml.example to config.yaml and edit it")
        return 1
    
    with open(config_path) as f:
        config = yaml.safe_load(f)
    
    # Route to command handler
    handlers = {
        "status": cmd_status,
        "history": cmd_history,
        "alerts": cmd_alerts,
        "daemon": cmd_daemon,
    }
    
    handler = handlers.get(args.command)
    if not handler:
        parser.print_help()
        return 1
    
    try:
        return await handler(config, args)
    except KeyboardInterrupt:
        print("\nInterrupted")
        return 130
    except Exception as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
