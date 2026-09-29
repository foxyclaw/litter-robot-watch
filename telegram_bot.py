"""Telegram bot command handler for Litter Robot Monitor."""
import asyncio
import logging
import io
import os
from typing import Dict, Any, Optional, Callable
import aiohttp

from monitor import LitterRobotMonitor, STATUS_CODES
from database import LitterRobotDatabase

# Plotting imports
try:
    import matplotlib
    matplotlib.use('Agg')  # Non-interactive backend
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from datetime import datetime
    PLOTTING_AVAILABLE = True
except ImportError:
    PLOTTING_AVAILABLE = False


class TelegramBot:
    """Handles Telegram bot commands for querying Litter Robot status."""
    
    def __init__(self, bot_token: str, chat_id: str, monitor: LitterRobotMonitor, db: LitterRobotDatabase):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.monitor = monitor
        self.db = db
        self.logger = logging.getLogger(__name__)
        self._running = False
        self._offset = 0
        self._processed_updates: set[int] = set()  # Track processed update IDs to prevent duplicates
        self._processed_commands: dict[str, float] = {}  # Track recently processed commands (user+cmd -> timestamp)
        
    async def start(self) -> None:
        """Start polling for Telegram commands."""
        self._running = True
        self.logger.info("Telegram bot started - listening for commands")
        # Silent startup - no message sent
        
        while self._running:
            try:
                await self._poll_updates()
            except Exception as e:
                self.logger.error(f"Error polling Telegram: {e}")
            await asyncio.sleep(1)
    
    async def run_forever(self) -> None:
        """Alias for start() — fits the watchdog scheduler interface."""
        await self.start()

    def stop(self) -> None:
        """Stop the bot."""
        self._running = False
        self.logger.info("Telegram bot stopped")
    
    async def _poll_updates(self) -> None:
        """Poll for new messages."""
        async with aiohttp.ClientSession() as session:
            url = f"{self.base_url}/getUpdates"
            params = {
                "offset": self._offset,
                "limit": 10,
                "timeout": 30
            }
            
            try:
                async with session.get(url, params=params, timeout=35) as response:
                    if response.status != 200:
                        return
                    
                    data = await response.json()
                    if not data.get("ok"):
                        return
                    
                    for update in data.get("result", []):
                        self._offset = update.get("update_id", 0) + 1
                        await self._handle_update(update)
            except asyncio.TimeoutError:
                pass
    
    async def _handle_update(self, update: Dict[str, Any]) -> None:
        """Handle an incoming update."""
        update_id = update.get("update_id", 0)
        
        # Skip duplicate updates - log at INFO level to help debug
        if update_id in self._processed_updates:
            self.logger.info(f"Skipping duplicate update {update_id}")
            return
        
        # Track this update (keep last 100 to limit memory)
        self._processed_updates.add(update_id)
        if len(self._processed_updates) > 100:
            self._processed_updates = set(sorted(self._processed_updates)[-50:])
        
        self.logger.info(f"Processing update {update_id}")
        
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        
        chat = message.get("chat", {})
        chat_id = str(chat.get("id"))
        
        # Only respond to authorized chat
        if chat_id != self.chat_id:
            return
        
        text = message.get("text", "")
        if not text:
            return
        
        # Parse command
        cmd_parts = text.strip().split()
        cmd = cmd_parts[0].lower().split('@')[0]  # Remove bot username
        
        # Command deduplication: same command from same chat within 5 seconds is a duplicate
        import time
        cmd_key = f"{chat_id}:{cmd}"
        now = time.time()
        if cmd_key in self._processed_commands:
            last_time = self._processed_commands[cmd_key]
            if now - last_time < 5:  # 5 second cooldown for same command
                self.logger.info(f"Skipping duplicate command {cmd} from {chat_id} (within 5s)")
                return
        self._processed_commands[cmd_key] = now
        # Cleanup old entries (keep last 50)
        if len(self._processed_commands) > 50:
            oldest = sorted(self._processed_commands.items(), key=lambda x: x[1])[:25]
            for key, _ in oldest:
                del self._processed_commands[key]
        
        handlers = {
            "/start": self.cmd_start,
            "/help": self.cmd_help,
            "/status": self.cmd_status,
            "/level": self.cmd_level,
            "/cats": self.cmd_cats,
            "/shit": self.cmd_shit,
            "/history": self.cmd_history,
            "/issues": self.cmd_issues,
            "/full": self.cmd_full_report,
        }
        
        handler = handlers.get(cmd)
        if handler:
            try:
                await handler()
            except Exception as e:
                self.logger.error(f"Command {cmd} failed: {e}", exc_info=True)
                await self.send_message(f"❌ Command failed: <code>{e}</code>")
    
    async def send_message(self, message: str) -> bool:
        """Send a message to the chat."""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/sendMessage"
                payload = {
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_notification": False
                }
                
                async with session.post(url, json=payload) as response:
                    return response.status == 200
                        
        except Exception as e:
            self.logger.error(f"Failed to send message: {e}")
            return False
    
    async def send_photo(self, photo_bytes: bytes, caption: str = "") -> bool:
        """Send a photo to the chat."""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/sendPhoto"
                
                data = aiohttp.FormData()
                data.add_field('chat_id', self.chat_id)
                data.add_field('photo', photo_bytes, filename='chart.png', content_type='image/png')
                if caption:
                    data.add_field('caption', caption)
                    data.add_field('parse_mode', 'HTML')
                
                async with session.post(url, data=data) as response:
                    if response.status != 200:
                        text = await response.text()
                        self.logger.error(f"Failed to send photo: {response.status} - {text}")
                    return response.status == 200
                        
        except Exception as e:
            self.logger.error(f"Failed to send photo: {e}")
            return False
    
    async def cmd_start(self) -> None:
        """Handle /start command."""
        await self.send_message(
            "🐱 <b>Welcome to COPTiP Monitor!</b>\n\n"
            "I monitor your Litter Robot and alert you when:\n"
            "• 🗑️ Waste drawer is full\n"
            "• ⚠️ There are errors or issues\n"
            "• 🐈 Cat activity detected\n\n"
            "Type /help to see all commands."
        )
    
    async def cmd_help(self) -> None:
        """Handle /help command."""
        await self.send_message(
            "📋 <b>Available Commands:</b>\n\n"
            "<code>/status</code> - Full status with any issues highlighted\n"
            "<code>/level</code> - Just the waste drawer level\n"
            "<code>/cats</code> - Cat weights and last visits\n"
            "<code>/shit</code> - Drawer emptying history & frequency\n"
            "<code>/history</code> - Recent activity history\n"
            "<code>/full</code> - Complete detailed report\n"
            "<code>/help</code> - Show this help message"
        )
    
    async def cmd_status(self) -> None:
        """Handle /status command - shows full status + any issues."""
        if not self.monitor.robots:
            await self.send_message("❌ No robots connected")
            return
        
        for robot_id, robot in self.monitor.robots.items():
            state = self.monitor._get_state_dict(robot)
            
            status_emoji = {
                "normal": "✅",
                "activity": "🔄",
                "warning": "⚠️",
                "error": "❌",
                "inactive": "⏸️",
            }.get(state.get("status_type"), "❓")
            
            waste = state.get("waste_drawer_level")
            waste_str = f"{waste}%" if waste is not None else "Unknown"
            
            litter = state.get('litter_level')
            litter_str = f"{litter:.0f}%" if litter is not None else "Unknown"
            
            # Build message parts
            message_parts = []
            
            # Check for issues FIRST (highlight at top)
            issues = []
            status = state.get("status", "")
            
            # Problem status codes that block operation
            problem_statuses = {
                "BR": "🚨 <b>Bonnet Removed</b> - Reattach the bonnet",
                "CSF": "⚠️ <b>Cat Sensor Fault</b> - Check sensor",
                "CSI": "⚠️ <b>Cat Sensor Interrupted</b> - Cycle was interrupted",
                "DFS": "🗑️ <b>Drawer Full</b> - Empty the waste drawer",
                "DRAWER_FULL": "🗑️ <b>Drawer Full</b> - Empty the waste drawer",
                "OFF": "⏸️ <b>Unit is Off</b> - Turn on to resume operation",
                "P": "⏸️ <b>Unit is Paused</b> - Unpause to resume",
            }
            
            if status in problem_statuses:
                issues.append(problem_statuses[status])
            
            # Check waste level threshold
            if waste and waste >= 95:
                issues.append(f"🗑️ <b>Critical Waste Level</b> - Drawer at {waste}%! Empty now.")
            elif waste and waste >= 80:
                issues.append(f"⚡ <b>Drawer Getting Full</b> - At {waste}%, plan to empty soon.")
            
            # Add issues section if any found
            if issues:
                message_parts.append("⚠️ <b>Issues Detected:</b>")
                for issue in issues:
                    message_parts.append(f"  {issue}")
                message_parts.append("")
            
            # Full status section
            message_parts.append(f"📊 <b>{state.get('name', 'COPTiP')}</b>")
            message_parts.append(f"Status: {status_emoji} {state.get('status_description')}")
            message_parts.append(f"🗑️ Waste Level: <code>{waste_str}</code>")
            message_parts.append(f"🌿 Litter Level: <code>{litter_str}</code> {state.get('litter_level_state', '')}")
            message_parts.append(f"⚡ Power: {state.get('power_status', 'Unknown')}")
            message_parts.append(f"💡 Night Light: {'On' if state.get('nightlight_status') else 'Off'}")
            message_parts.append(f"🔒 Panel Lock: {'Locked' if state.get('panel_locked') else 'Unlocked'}")
            
            # Add cat weight if available
            cat_weight_lbs = state.get('cat_weight_lbs')
            cat_weight_kg = state.get('cat_weight_kg')
            if cat_weight_lbs:
                threshold = 14.3
                cat_name = "PIXEL" if cat_weight_lbs > threshold else "CHICKA"
                message_parts.append(f"🐈 Last Cat ({cat_name}): <code>{cat_weight_lbs:.1f} lbs</code> ({cat_weight_kg:.2f} kg)")
            
            # Add scoops saved
            scoops = state.get('scoops_saved') or state.get('cycle_count')
            if scoops:
                message_parts.append(f"♻️ Scoops Saved: <code>{scoops:,}</code>")
            
            await self.send_message("\n".join(message_parts))
    
    async def cmd_level(self) -> None:
        """Handle /level command - just waste drawer level."""
        if not self.monitor.robots:
            await self.send_message("❌ No robots connected")
            return
        
        for robot_id, robot in self.monitor.robots.items():
            state = self.monitor._get_state_dict(robot)
            waste = state.get("waste_drawer_level")
            robot_name = state.get('name', 'COPTiP')
            
            if waste is not None:
                bar_len = int(waste / 10)
                bar = "█" * bar_len + "░" * (10 - bar_len)
                message = (
                    f"🗑️ <b>{robot_name} - Waste Drawer</b>\n\n"
                    f"[{bar}] <code>{waste}%</code>\n"
                )
                if waste >= 95:
                    message += "\n⚠️ <b>CRITICAL:</b> Empty the drawer now!"
                elif waste >= 80:
                    message += "\n⚡ Getting full, plan to empty soon."
                else:
                    message += "\n✅ Looking good."
                
                # Add litter level too
                litter = state.get('litter_level')
                if litter is not None:
                    message += f"\n🌿 Litter Level: <code>{litter:.0f}%</code>"
            else:
                message = f"🗑️ <b>{robot_name}</b>\nWaste drawer level: Unknown"
            
            await self.send_message(message)
    
    def _generate_weight_plot(self, visits: list, days: int = 60) -> Optional[bytes]:
        """Generate a weight plot for the cats with individual visits on x-axis."""
        if not PLOTTING_AVAILABLE or not visits:
            return None
        
        # Separate visits by cat
        pixel_visits = [v for v in visits if v.get('cat_name') == 'PIXEL']
        chicka_visits = [v for v in visits if v.get('cat_name') == 'CHICKA']
        
        if not pixel_visits and not chicka_visits:
            return None
        
        try:
            fig, ax = plt.subplots(figsize=(12, 6))
            
            threshold = 14.3

            # Add threshold line first so it appears behind data
            ax.axhline(y=threshold, color='gray', linestyle='--', alpha=0.5,
                      linewidth=1.5, label=f'ID threshold ({threshold} lbs)')

            def _plot_cat(cat_visits, color, marker, label):
                """Plot all visits for a cat."""
                if not cat_visits:
                    return
                dates = [datetime.fromisoformat(v['timestamp']) for v in cat_visits]
                weights = [v['weight_lbs'] for v in cat_visits]

                ax.plot(dates, weights, f'{marker}-', color=color,
                       label=label, linewidth=2, markersize=6)

            _plot_cat(pixel_visits,  '#FF6B35', 'o', 'PIXEL')
            _plot_cat(chicka_visits, '#4ECDC4', 's', 'CHICKA')
            
            # Formatting
            ax.set_xlabel('Date', fontsize=12)
            ax.set_ylabel('Weight (lbs)', fontsize=12)
            ax.set_title(f'Cat Weight History (Last {days} Days)', fontsize=14, fontweight='bold')
            ax.legend(loc='best', fontsize=11)
            ax.grid(True, alpha=0.3)
            
            # Clean x-axis: dates only, weekly ticks for long ranges
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %d'))
            if days >= 30:
                ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
            elif days >= 14:
                ax.xaxis.set_major_locator(mdates.DayLocator(interval=2))
            else:
                ax.xaxis.set_major_locator(mdates.DayLocator(interval=1))
            plt.xticks(rotation=30, ha='right', fontsize=10)
            
            # Y-axis: focus on relevant range
            ax.set_ylim([13.0, 16.0])
            
            plt.tight_layout()
            
            # Save to bytes
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            buf.seek(0)
            plt.close(fig)
            
            return buf.getvalue()
            
        except Exception as e:
            self.logger.error(f"Failed to generate plot: {e}")
            return None
    
    async def cmd_cats(self) -> None:
        """Handle /cats command - cat weights and activity with plot."""
        # Get robot IDs from database (works even when API is down)
        robot_ids = await self.db.get_distinct_robot_ids()
        
        if not robot_ids and not self.monitor.robots:
            await self.send_message("❌ No robot data available")
            return
        
        # Use robots from monitor if connected, otherwise use DB robot IDs
        if self.monitor.robots:
            robots_to_show = [(rid, robot) for rid, robot in self.monitor.robots.items()]
        else:
            # Create placeholder entries for DB-only robots
            robots_to_show = [(rid, None) for rid in robot_ids]
        
        for robot_id, robot in robots_to_show:
            # Get state from monitor if connected, otherwise get from DB
            if robot and self.monitor:
                state = self.monitor._get_state_dict(robot)
                robot_name = state.get('name', 'COPTiP')
                is_online = True
            else:
                # Get from DB - fetch latest state
                db_state = await self.db.get_latest_state(robot_id)
                if db_state:
                    robot_name = db_state.get('robot_name', 'COPTiP')
                    state = db_state
                else:
                    robot_name = 'COPTiP'
                    state = {}
                is_online = False
            
            # Get cat visit data from database (last 14 days)
            visits = await self.db.get_cat_visits(robot_id, days=60)
            stats = await self.db.get_cat_visit_stats(robot_id, days=14)
            
            # Build message
            message_parts = [f"🐱 <b>Cat Activity - {robot_name}</b>"]
            if not is_online:
                message_parts.append("<i>(API offline - showing cached data)</i>")
            message_parts.append("")
            
            # Stats summary
            if stats:
                message_parts.append("<b>Last 14 Days:</b>")
                for cat_name in ['PIXEL', 'CHICKA']:
                    if cat_name in stats:
                        s = stats[cat_name]
                        message_parts.append(
                            f"  • <b>{cat_name}:</b> {s['visit_count']} visits, "
                            f"avg {s['avg_weight_lbs']:.1f} lbs"
                        )
            
            # Last seen per cat (in PDT)
            last_seen_info = await self.db.get_last_visit_per_cat(robot_id)
            if last_seen_info:
                message_parts.append("")
                from datetime import timezone, timedelta
                pdt = timezone(timedelta(hours=-7))
                for cat_name in ['PIXEL', 'CHICKA']:
                    if cat_name in last_seen_info:
                        info = last_seen_info[cat_name]
                        ts_utc = info['timestamp']
                        try:
                            from datetime import datetime as dt
                            ts = dt.strptime(ts_utc[:19], '%Y-%m-%d %H:%M:%S')
                            ts = ts.replace(tzinfo=timezone.utc).astimezone(pdt)
                            time_str = ts.strftime('%b %d, %I:%M %p')
                        except Exception:
                            time_str = ts_utc
                        message_parts.append(
                            f"🐈 <b>{cat_name}:</b> {info['weight_lbs']:.1f} lbs — {time_str} PDT"
                        )
            
            # Generate and send plot if we have visit data
            has_plot = False
            if visits and PLOTTING_AVAILABLE:
                plot_bytes = self._generate_weight_plot(visits, days=60)
                if plot_bytes:
                    # Send the plot first
                    caption = f"🐱 Weight history for {robot_name}"
                    await self.send_photo(plot_bytes, caption=caption)
                    has_plot = True
            
            if not has_plot:
                if not visits:
                    message_parts.append("\n<i>(No weight history yet - will track visits going forward)</i>")
                elif not PLOTTING_AVAILABLE:
                    message_parts.append("\n<i>(Plotting not available - install matplotlib)</i>")
                else:
                    message_parts.append("\n<i>(Could not generate weight plot)</i>")
            
            # Send the text message
            await self.send_message("\n".join(message_parts))

    async def cmd_shit(self) -> None:
        """Handle /shit command - drawer full history & frequency."""
        from datetime import datetime as dt, timezone, timedelta
        pdt = timezone(timedelta(hours=-7))

        robot_ids = await self.db.get_distinct_robot_ids()
        if not robot_ids:
            await self.send_message("❌ No data available")
            return

        robot_id = robot_ids[0]
        full_dates = await self.db.get_drawer_full_history(robot_id, limit=20)

        if not full_dates:
            await self.send_message("🗑️ No drawer full events recorded yet.\n\n<i>Tracking starts now — data will appear after the next full event.</i>")
            return

        lines = ["💩 <b>Drawer Full History</b>", ""]
        lines.append("<pre>")
        lines.append(f"{'Date':<14}{'Days':>5}")
        lines.append(f"{'-'*14}{'-'*5}")

        prev_ts = None
        # Reverse to show oldest first
        for ts_str in reversed(full_dates):
            try:
                ts = dt.strptime(ts_str[:19], '%Y-%m-%d %H:%M:%S')
                ts = ts.replace(tzinfo=timezone.utc).astimezone(pdt)
            except Exception:
                continue

            if prev_ts:
                days_diff = (ts - prev_ts).days
                days_str = str(days_diff)
            else:
                days_str = "—"

            lines.append(f"{ts.strftime('%b %d %H:%M'):<14}{days_str:>5}")
            prev_ts = ts

        lines.append("</pre>")

        # Summary
        if len(full_dates) >= 2:
            all_ts = []
            for ts_str in full_dates:
                try:
                    ts = dt.strptime(ts_str[:19], '%Y-%m-%d %H:%M:%S')
                    all_ts.append(ts)
                except Exception:
                    pass
            if len(all_ts) >= 2:
                diffs = [(all_ts[i-1] - all_ts[i]).days for i in range(1, len(all_ts))]
                avg_days = sum(diffs) / len(diffs)
                lines.append(f"\n📊 <b>Avg:</b> {avg_days:.1f} days between fills")

        await self.send_message("\n".join(lines))

    async def cmd_history(self) -> None:
        """Handle /history command."""
        if not self.monitor.robots:
            await self.send_message("❌ No robots connected")
            return
        
        for robot_id in self.monitor.robots:
            history = await self.db.get_state_history(robot_id, hours=24)
            
            if not history:
                await self.send_message("📊 No history available for last 24 hours")
                return
            
            message_parts = ["📈 <b>Last 24 Hours Activity</b>\n"]
            
            # Count status changes
            status_counts = {}
            for row in history:
                status = row.get("status", "Unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
            
            message_parts.append("\n<b>Status Activity:</b>")
            for status, count in sorted(status_counts.items(), key=lambda x: -x[1])[:5]:
                desc = STATUS_CODES.get(status, (status,))[0]
                message_parts.append(f"  • {status}: {count} times ({desc})")
            
            # Recent waste levels
            waste_readings = [r for r in history if r.get("waste_drawer_level") is not None][:5]
            if waste_readings:
                message_parts.append("\n<b>Recent Waste Levels:</b>")
                for row in waste_readings[:3]:
                    ts = row.get("timestamp", "")[11:16]  # Just HH:MM
                    waste = row.get("waste_drawer_level")
                    message_parts.append(f"  • {ts}: {waste}%")
            
            await self.send_message("\n".join(message_parts))
    
    async def cmd_issues(self) -> None:
        """Handle /issues command - now redirects to /status."""
        await self.cmd_status()
    
    async def cmd_full_report(self) -> None:
        """Handle /full command - complete detailed report."""
        if not self.monitor.robots:
            await self.send_message("❌ No robots connected")
            return
        
        for robot_id, robot in self.monitor.robots.items():
            state = self.monitor._get_state_dict(robot)
            
            message_parts = [
                f"📋 <b>Complete Report: {state['name']}</b>",
                "",
                f"<b>Status:</b> {state.get('status')} - {state.get('status_description')}",
                f"🗑️ Waste Level: {state.get('waste_drawer_level', 'Unknown')}%",
                f"🌿 Litter Level: {state.get('litter_level', 'Unknown'):.0f}% ({state.get('litter_level_state', 'Unknown')})",
                f"⚡ Power: {state.get('power_status', 'Unknown')}",
                f"💡 Night Light: {'On' if state.get('nightlight_status') else 'Off'}",
                f"🔒 Panel Lock: {'Locked' if state.get('panel_locked') else 'Unlocked'}",
                f"😴 Sleep Mode: {'Active' if state.get('sleep_mode') else 'Off'}",
            ]
            
            # Add cycle/scoops info
            scoops = state.get('scoops_saved')
            cycles = state.get('cycle_count')
            if scoops:
                message_parts.append(f"♻️ Scoops Saved: {scoops:,}")
            elif cycles:
                message_parts.append(f"🔄 Total Cycles: {cycles:,}")
            
            # Add cat info if available
            cat_weight_lbs = state.get('cat_weight_lbs')
            cat_weight_kg = state.get('cat_weight_kg')
            if cat_weight_lbs:
                message_parts.append(f"🐈 Last Cat Weight: {cat_weight_lbs:.1f} lbs ({cat_weight_kg:.2f} kg)")
            
            # Add firmware
            fw = state.get('firmware')
            if fw:
                message_parts.append(f"🔧 Firmware: {fw}")
            
            # Add serial
            serial = state.get('serial')
            if serial:
                message_parts.append(f"📟 Serial: {serial}")
            
            last_seen = state.get('last_seen')
            if last_seen:
                message_parts.append(f"📅 Last Seen: {last_seen}")
            
            # Add any warnings
            status = state.get("status", "")
            if status in ["BR", "CSF", "CSI", "DFS", "DRAWER_FULL", "OFF", "P"]:
                message_parts.append("")
                message_parts.append(f"⚠️ <b>WARNING:</b> {state.get('status_description')}")
            
            await self.send_message("\n".join(message_parts))
