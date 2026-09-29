"""SQLite database for storing Litter Robot state history."""
import aiosqlite
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any


class LitterRobotDatabase:
    """Manages SQLite storage for Litter Robot state history."""
    
    def __init__(self, db_path: str = "litter_robot.db"):
        self.db_path = Path(db_path)
        self._connection: Optional[aiosqlite.Connection] = None
    
    async def connect(self) -> None:
        """Initialize database connection and tables."""
        self._connection = await aiosqlite.connect(self.db_path)
        await self._create_tables()
    
    async def disconnect(self) -> None:
        """Close database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None
    
    async def _create_tables(self) -> None:
        """Create database tables if they don't exist."""
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS robot_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                robot_id TEXT NOT NULL,
                robot_name TEXT,
                status TEXT NOT NULL,
                status_description TEXT,
                waste_drawer_level INTEGER,
                power_status TEXT,
                nightlight_status INTEGER,
                panel_locked INTEGER,
                sleep_mode INTEGER,
                cycle_count INTEGER,
                total_cycles INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                raw_data TEXT
            )
        """)
        
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                robot_id TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                acknowledged INTEGER DEFAULT 0
            )
        """)
        
        # Cat visits table - stores individual cat weight measurements
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS cat_visits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                robot_id TEXT NOT NULL,
                cat_name TEXT NOT NULL,
                weight_lbs REAL NOT NULL,
                weight_kg REAL NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_robot_states_robot_id 
            ON robot_states(robot_id)
        """)
        
        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_robot_states_timestamp 
            ON robot_states(timestamp)
        """)
        
        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_alerts_robot_id 
            ON alerts(robot_id)
        """)
        
        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_cat_visits_robot_id 
            ON cat_visits(robot_id)
        """)
        
        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_cat_visits_timestamp 
            ON cat_visits(timestamp)
        """)
        
        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_cat_visits_cat_name 
            ON cat_visits(cat_name)
        """)

        # Drawer events table - tracks full/emptied events
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS drawer_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                robot_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                waste_level INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_drawer_events_robot_id
            ON drawer_events(robot_id)
        """)

        await self._connection.commit()
    
    def _serialize_state(self, state: Dict[str, Any]) -> str:
        """Serialize state dict to JSON, handling datetime objects."""
        def json_serial(obj):
            if hasattr(obj, 'isoformat'):
                return obj.isoformat()
            raise TypeError(f"Type {type(obj)} not serializable")
        return json.dumps(state, default=json_serial)
    
    async def store_state(self, robot_id: str, state: Dict[str, Any]) -> None:
        """Store a robot state snapshot."""
        await self._connection.execute(
            """
            INSERT INTO robot_states (
                robot_id, robot_name, status, status_description,
                waste_drawer_level, power_status, nightlight_status,
                panel_locked, sleep_mode, cycle_count, total_cycles, raw_data
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                robot_id,
                state.get("name"),
                state.get("status"),
                state.get("status_description"),
                state.get("waste_drawer_level"),
                state.get("power_status"),
                state.get("nightlight_status"),
                state.get("panel_locked"),
                state.get("sleep_mode"),
                state.get("cycle_count"),
                state.get("total_cycles"),
                self._serialize_state(state)
            )
        )
        await self._connection.commit()
    
    async def store_alert(self, robot_id: str, alert_type: str, 
                         message: str, status: str) -> None:
        """Store an alert."""
        await self._connection.execute(
            """
            INSERT INTO alerts (robot_id, alert_type, message, status)
            VALUES (?, ?, ?, ?)
            """,
            (robot_id, alert_type, message, status)
        )
        await self._connection.commit()
    
    async def get_latest_state(self, robot_id: str) -> Optional[Dict[str, Any]]:
        """Get the most recent state for a robot."""
        async with self._connection.execute(
            """
            SELECT * FROM robot_states 
            WHERE robot_id = ? 
            ORDER BY timestamp DESC 
            LIMIT 1
            """,
            (robot_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(zip([col[0] for col in cursor.description], row))
            return None
    
    async def get_state_history(self, robot_id: str, 
                               hours: int = 24) -> List[Dict[str, Any]]:
        """Get state history for a robot."""
        async with self._connection.execute(
            """
            SELECT * FROM robot_states 
            WHERE robot_id = ? 
            AND timestamp > datetime('now', '-' || ? || ' hours')
            ORDER BY timestamp DESC
            """,
            (robot_id, hours)
        ) as cursor:
            rows = await cursor.fetchall()
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in rows]
    
    async def get_alerts(self, robot_id: Optional[str] = None,
                        unacknowledged_only: bool = False,
                        limit: int = 100) -> List[Dict[str, Any]]:
        """Get alerts."""
        query = "SELECT * FROM alerts WHERE 1=1"
        params = []
        
        if robot_id:
            query += " AND robot_id = ?"
            params.append(robot_id)
        
        if unacknowledged_only:
            query += " AND acknowledged = 0"
        
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        
        async with self._connection.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in rows]
    
    async def acknowledge_alert(self, alert_id: int) -> None:
        """Mark an alert as acknowledged."""
        await self._connection.execute(
            "UPDATE alerts SET acknowledged = 1 WHERE id = ?",
            (alert_id,)
        )
        await self._connection.commit()
    
    async def store_cat_visit(self, robot_id: str, cat_name: str,
                             weight_lbs: float, weight_kg: float,
                             timestamp_override: str = None) -> None:
        """Store a cat visit with weight measurement."""
        if timestamp_override:
            await self._connection.execute(
                """
                INSERT INTO cat_visits (robot_id, cat_name, weight_lbs, weight_kg, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (robot_id, cat_name, weight_lbs, weight_kg, timestamp_override)
            )
        else:
            await self._connection.execute(
                """
                INSERT INTO cat_visits (robot_id, cat_name, weight_lbs, weight_kg)
                VALUES (?, ?, ?, ?)
                """,
                (robot_id, cat_name, weight_lbs, weight_kg)
            )
        await self._connection.commit()
    
    async def get_cat_visits(self, robot_id: str, cat_name: Optional[str] = None,
                            days: int = 30) -> List[Dict[str, Any]]:
        """Get cat visits for a robot, optionally filtered by cat name."""
        query = """
            SELECT * FROM cat_visits 
            WHERE robot_id = ? 
            AND timestamp > datetime('now', '-' || ? || ' days')
        """
        params = [robot_id, days]
        
        if cat_name:
            query += " AND cat_name = ?"
            params.append(cat_name)
        
        query += " ORDER BY timestamp ASC"
        
        async with self._connection.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in rows]
    
    async def get_distinct_robot_ids(self) -> List[str]:
        """Get list of distinct robot IDs that have data in the database."""
        robot_ids = set()
        
        # Check robot_states table
        async with self._connection.execute(
            "SELECT DISTINCT robot_id FROM robot_states ORDER BY robot_id"
        ) as cursor:
            rows = await cursor.fetchall()
            robot_ids.update([row[0] for row in rows])
        
        # Check cat_visits table
        async with self._connection.execute(
            "SELECT DISTINCT robot_id FROM cat_visits ORDER BY robot_id"
        ) as cursor:
            rows = await cursor.fetchall()
            robot_ids.update([row[0] for row in rows])
        
        return sorted(list(robot_ids))
    
    async def get_cat_visit_stats(self, robot_id: str, days: int = 30) -> Dict[str, Any]:
        """Get statistics about cat visits (days counted in America/Los_Angeles)."""
        # PDT is UTC-7; adjust the cutoff so 'last 14 days' means 14 PDT days
        async with self._connection.execute(
            """
            SELECT cat_name, COUNT(*) as visit_count, 
                   AVG(weight_lbs) as avg_weight_lbs,
                   MIN(weight_lbs) as min_weight_lbs,
                   MAX(weight_lbs) as max_weight_lbs
            FROM cat_visits 
            WHERE robot_id = ? 
            AND timestamp > datetime('now', '-7 hours', '-' || ? || ' days')
            GROUP BY cat_name
            """,
            (robot_id, days)
        ) as cursor:
            rows = await cursor.fetchall()
            stats = {}
            for row in rows:
                stats[row[0]] = {
                    "visit_count": row[1],
                    "avg_weight_lbs": round(row[2], 2) if row[2] else None,
                    "min_weight_lbs": round(row[3], 2) if row[3] else None,
                    "max_weight_lbs": round(row[4], 2) if row[4] else None,
                }
            return stats

    async def get_last_visit_per_cat(self, robot_id: str) -> Dict[str, Any]:
        """Get the most recent visit for each cat."""
        async with self._connection.execute(
            """
            SELECT cat_name, weight_lbs, weight_kg, timestamp
            FROM cat_visits
            WHERE robot_id = ?
            AND timestamp = (
                SELECT MAX(timestamp) FROM cat_visits cv2
                WHERE cv2.robot_id = cat_visits.robot_id
                AND cv2.cat_name = cat_visits.cat_name
            )
            ORDER BY timestamp DESC
            """,
            (robot_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            result = {}
            for row in rows:
                result[row[0]] = {
                    "weight_lbs": row[1],
                    "weight_kg": row[2],
                    "timestamp": row[3],
                }
            return result

    async def store_drawer_event(self, robot_id: str, event_type: str, waste_level: int = None) -> None:
        """Store a drawer event (full or emptied)."""
        await self._connection.execute(
            "INSERT INTO drawer_events (robot_id, event_type, waste_level) VALUES (?, ?, ?)",
            (robot_id, event_type, waste_level)
        )
        await self._connection.commit()

    async def get_last_drawer_event(self, robot_id: str, event_type: str = None) -> Optional[Dict[str, Any]]:
        """Get the most recent drawer event."""
        if event_type:
            query = "SELECT event_type, waste_level, timestamp FROM drawer_events WHERE robot_id = ? AND event_type = ? ORDER BY timestamp DESC LIMIT 1"
            params = (robot_id, event_type)
        else:
            query = "SELECT event_type, waste_level, timestamp FROM drawer_events WHERE robot_id = ? ORDER BY timestamp DESC LIMIT 1"
            params = (robot_id,)
        async with self._connection.execute(query, params) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"event_type": row[0], "waste_level": row[1], "timestamp": row[2]}
            return None

    async def get_drawer_emptied_history(self, robot_id: str, limit: int = 20) -> list:
        """Get history of drawer emptied events."""
        async with self._connection.execute(
            "SELECT timestamp FROM drawer_events WHERE robot_id = ? AND event_type = 'emptied' ORDER BY timestamp DESC LIMIT ?",
            (robot_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def get_drawer_full_history(self, robot_id: str, limit: int = 20) -> list:
        """Get history of drawer full events."""
        async with self._connection.execute(
            "SELECT timestamp FROM drawer_events WHERE robot_id = ? AND event_type = 'full' ORDER BY timestamp DESC LIMIT ?",
            (robot_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]
