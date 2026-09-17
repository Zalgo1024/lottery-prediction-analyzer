"""
基于 SQLite 的异步任务持久化存储
替换原 web/utils.py 中的内存字典 _task_store
"""
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional


DB_PATH = Path(__file__).parent.parent / "tasks.db"

# 运行中任务心跳超过该秒数未续期 → 视为孤儿（进程崩溃/被杀）
DEFAULT_STALE_SECONDS = 300


class TaskStore:
    """异步任务 SQLite 持久化存储"""

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path or DB_PATH
        self._local = threading.local()
        # 进程内串行化认领（跨进程由 BEGIN IMMEDIATE 保证原子性）
        self._claim_lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self._db_path))
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def close(self):
        """Close this thread's SQLite connection, if one is open."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _init_db(self):
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id          TEXT PRIMARY KEY,
                type        TEXT NOT NULL,
                params      TEXT DEFAULT '{}',
                status      TEXT DEFAULT 'pending',
                progress    INTEGER DEFAULT 0,
                message     TEXT DEFAULT '',
                result      TEXT,
                error       TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at)
        """)
        # worker 支持字段（存量库平滑迁移：缺列则补）
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
        if "worker_id" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN worker_id TEXT DEFAULT ''")
        if "heartbeat_at" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN heartbeat_at TEXT")
        conn.commit()

    # ---------- CRUD ----------

    def create(self, task_type: str, params: dict) -> str:
        import uuid
        task_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO tasks (id, type, params, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, task_type, json.dumps(params), now, now),
        )
        conn.commit()
        return task_id

    def update(self, task_id: str, **kwargs):
        kwargs["updated_at"] = datetime.now().isoformat()
        if "result" in kwargs and kwargs["result"] is not None:
            kwargs["result"] = json.dumps(kwargs["result"], ensure_ascii=False)
        if "params" in kwargs and kwargs["params"] is not None:
            kwargs["params"] = json.dumps(kwargs["params"], ensure_ascii=False)
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [task_id]
        self._get_conn().execute(
            f"UPDATE tasks SET {sets} WHERE id=?", vals
        )
        self._get_conn().commit()

    def get(self, task_id: str) -> Optional[dict]:
        row = self._get_conn().execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        for key in ("result", "params"):
            if d.get(key):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    def cleanup(self, days: int = 7):
        """清理 N 天前已完成或失败的任务"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        self._get_conn().execute(
            "DELETE FROM tasks WHERE status IN ('done','error') AND created_at < ?",
            (cutoff,),
        )
        self._get_conn().commit()

    # ---------- worker：原子认领 / 心跳 / 孤儿回收 ----------

    def claim_next(self, types: Iterable[str], worker_id: str) -> Optional[dict]:
        """原子认领：把指定类型中最旧的 pending 任务置为 running 并绑定 worker。
        跨进程安全：BEGIN IMMEDIATE 串行化「查询+更新」；进程内再由 _claim_lock 串行。
        无可认领任务返回 None。"""
        types = [t for t in (types or [])]
        if not types:
            return None
        now = datetime.now().isoformat(timespec="seconds")
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in types)
        with self._claim_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    f"SELECT * FROM tasks WHERE status='pending' AND type IN ({placeholders}) "
                    "ORDER BY created_at LIMIT 1",
                    types,
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                cur = conn.execute(
                    "UPDATE tasks SET status='running', worker_id=?, heartbeat_at=?, "
                    "message='已被 worker 认领', updated_at=? "
                    "WHERE id=? AND status='pending'",
                    (worker_id, now, now, row["id"]),
                )
                if cur.rowcount == 0:
                    # 极端竞态：pending 已被抢走
                    conn.execute("COMMIT")
                    return None
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
        return self.get(row["id"])

    def heartbeat(self, task_id: str):
        """续期任务心跳（worker 执行循环中定期调用，证明任务仍被活进程持有）"""
        now = datetime.now().isoformat(timespec="seconds")
        self._get_conn().execute(
            "UPDATE tasks SET heartbeat_at=?, updated_at=? WHERE id=? AND status='running'",
            (now, now, task_id),
        )
        self._get_conn().commit()

    def has_pending(self, types: Iterable[str], params_match: Optional[dict] = None) -> bool:
        """是否存在指定类型（可附加 params 精确子串匹配）的 pending 任务，用于入队去重。
        params_match 形如 {"lottery": "双色球"}，要求 params JSON 中同时包含这些键值。"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT params FROM tasks WHERE status='pending' AND type IN "
            f"({','.join('?' for _ in types)})",
            list(types),
        ).fetchall()
        if not params_match:
            return bool(rows)
        for r in rows:
            try:
                p = json.loads(r["params"] or "{}")
                if all(p.get(k) == v for k, v in params_match.items()):
                    return True
            except (json.JSONDecodeError, TypeError):
                continue
        return False

    def reclaim_orphans(self, types: Optional[Iterable[str]] = None,
                        stale_seconds: int = DEFAULT_STALE_SECONDS,
                        reason: str = "worker 心跳超时，孤儿任务被回收") -> int:
        """把 running 但心跳过期（或从未续期）的任务标记为 failed。
        只回收指定类型（不传=全部），避免误伤旧版线程池正在跑的流水线任务
        （旧任务没有 worker 心跳，但其类型不在 worker 认领范围内）。"""
        cutoff = (datetime.now() - timedelta(seconds=stale_seconds)).isoformat(
            timespec="seconds")
        now = datetime.now().isoformat(timespec="seconds")
        conn = self._get_conn()
        if types:
            types = list(types)
            placeholders = ",".join("?" for _ in types)
            where = f"type IN ({placeholders})"
            args: List = list(types)
        else:
            where = "1=1"
            args = []
        cur = conn.execute(
            f"UPDATE tasks SET status='failed', error=?, updated_at=? "
            f"WHERE status='running' AND {where} "
            "AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
            [reason, now] + args + [cutoff],
        )
        conn.commit()
        return cur.rowcount


# 全局单例
task_store = TaskStore()
