import sqlite3
import json
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta

class Database:
    def __init__(self, data_path: Path):
        self.db_path = data_path / "roco_cache.db"
        self._init_db()

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS elves (
                    t_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    avatar TEXT,
                    data_json TEXT,
                    screenshot_path TEXT,
                    updated_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS skills (
                    skill_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    data_json TEXT,
                    screenshot_path TEXT,
                    updated_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS teams (
                    team_id TEXT PRIMARY KEY,
                    name TEXT,
                    description TEXT,
                    data_json TEXT,
                    screenshot_path TEXT,
                    updated_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS egg_groups (
                    elf_name TEXT PRIMARY KEY,
                    egg_group TEXT,
                    breedable_json TEXT,
                    updated_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS breeding_plans (
                    id TEXT PRIMARY KEY,  -- parent_target_gender
                    plan_json TEXT,
                    updated_at INTEGER
                );
            """)

    def _now_ts(self) -> int:
        return int(datetime.now().timestamp())

    def get_elf(self, t_id: str, ttl_hours: int) -> Optional[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT data_json, screenshot_path, updated_at FROM elves WHERE t_id = ?",
                (t_id,)
            ).fetchone()
        if not row:
            return None
        updated_at = row[2]
        if self._now_ts() - updated_at > ttl_hours * 3600:
            return None  # 过期
        return {
            "data": json.loads(row[0]) if row[0] else None,
            "screenshot_path": row[1]
        }

    def save_elf(self, t_id: str, name: str, avatar: str, data_json: Dict, screenshot_path: Optional[str] = None):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO elves (t_id, name, avatar, data_json, screenshot_path, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (t_id, name, avatar, json.dumps(data_json, ensure_ascii=False),
                 screenshot_path, self._now_ts())
            )

    def get_skill(self, skill_id: str, ttl_hours: int) -> Optional[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT data_json, screenshot_path, updated_at FROM skills WHERE skill_id = ?",
                (skill_id,)
            ).fetchone()
        if not row:
            return None
        if self._now_ts() - row[2] > ttl_hours * 3600:
            return None
        return {"data": json.loads(row[0]), "screenshot_path": row[1]}

    def save_skill(self, skill_id: str, name: str, data_json: Dict, screenshot_path: Optional[str] = None):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO skills (skill_id, name, data_json, screenshot_path, updated_at) VALUES (?, ?, ?, ?, ?)",
                (skill_id, name, json.dumps(data_json, ensure_ascii=False), screenshot_path, self._now_ts())
            )

    def get_team(self, team_id: str, ttl_hours: int) -> Optional[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT data_json, screenshot_path, updated_at FROM teams WHERE team_id = ?",
                (team_id,)
            ).fetchone()
        if not row:
            return None
        if self._now_ts() - row[2] > ttl_hours * 3600:
            return None
        return {"data": json.loads(row[0]), "screenshot_path": row[1]}

    def save_team(self, team_id: str, name: str, description: str, data_json: Dict, screenshot_path: Optional[str] = None):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO teams (team_id, name, description, data_json, screenshot_path, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (team_id, name, description, json.dumps(data_json, ensure_ascii=False), screenshot_path, self._now_ts())
            )

    def get_egg_group(self, elf_name: str, ttl_hours: int) -> Optional[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT egg_group, breedable_json, updated_at FROM egg_groups WHERE elf_name = ?",
                (elf_name,)
            ).fetchone()
        if not row:
            return None
        if self._now_ts() - row[2] > ttl_hours * 3600:
            return None
        return {"egg_group": row[0], "breedable": json.loads(row[1])}

    def save_egg_group(self, elf_name: str, egg_group: str, breedable_list: List[Dict]):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO egg_groups (elf_name, egg_group, breedable_json, updated_at) VALUES (?, ?, ?, ?)",
                (elf_name, egg_group, json.dumps(breedable_list, ensure_ascii=False), self._now_ts())
            )

    def get_breeding_plan(self, plan_id: str, ttl_hours: int) -> Optional[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT plan_json, updated_at FROM breeding_plans WHERE id = ?",
                (plan_id,)
            ).fetchone()
        if not row:
            return None
        if self._now_ts() - row[1] > ttl_hours * 3600:
            return None
        return json.loads(row[0])

    def save_breeding_plan(self, plan_id: str, plan_json: Dict):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO breeding_plans (id, plan_json, updated_at) VALUES (?, ?, ?)",
                (plan_id, json.dumps(plan_json, ensure_ascii=False), self._now_ts())
            )