"""SQLite 持久化层。

三张单例表：
- pending        : 待确认的预览（安全表 payload，不含隐藏格真实计数）
- publication    : 已发布的安全表（确认后写入，重启后可查；不可替换）
- revision_check : 修订核查状态（最高修订号 + 最近核查结论 + 是否已撤下）

发布动作在单个事务内完成（写 publication + 清 pending），
失败不会产生部分公开结果。

修订核查的“比较”在发布基线上进行（发布后基线不可变），
而“记录结论”在 BEGIN IMMEDIATE 事务内再次核对最高修订号：
较旧修订即使晚完成也不能覆盖较新结论；同号不同文件拒绝；
已撤下的状态是单向的，后续核查不会自动恢复公开。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publication (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT NOT NULL,
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revision_check (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    max_revision INTEGER NOT NULL,
    max_revision_digest TEXT NOT NULL,
    last_outcome TEXT NOT NULL,
    last_reason TEXT,
    withdrawn INTEGER NOT NULL DEFAULT 0,
    withdrawn_at TEXT,
    withdrawn_revision INTEGER,
    withdrawn_reason TEXT,
    checked_at TEXT NOT NULL
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._migrate()

    _REVISION_COLUMNS = (
        "max_revision", "max_revision_digest", "last_outcome", "last_reason",
        "withdrawn", "withdrawn_at", "withdrawn_revision", "withdrawn_reason",
        "checked_at",
    )

    def _migrate(self) -> None:
        """兼容旧库：补齐 revision_check 缺失的列。

        早于修订核查功能的库没有该表，_SCHEMA 会直接建出最新结构；
        这里只处理结构不完整的库：空表直接重建，非空表补齐缺失列。
        """
        cols = {r["name"] for r in self._conn.execute(
            "PRAGMA table_info(revision_check)"
        )}
        missing = set(self._REVISION_COLUMNS) - cols
        if not missing:
            return
        has_rows = self._conn.execute(
            "SELECT 1 FROM revision_check LIMIT 1"
        ).fetchone() is not None
        if not has_rows:
            self._conn.execute("DROP TABLE revision_check")
            self._conn.executescript(_SCHEMA)
            return
        for col in missing:
            if col in ("withdrawn",):
                self._conn.execute(
                    "ALTER TABLE revision_check ADD COLUMN withdrawn "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            elif col == "checked_at":
                self._conn.execute(
                    "ALTER TABLE revision_check ADD COLUMN checked_at TEXT "
                    "NOT NULL DEFAULT ''"
                )
            elif col in ("max_revision", "max_revision_digest"):
                self._conn.execute(
                    "ALTER TABLE revision_check ADD COLUMN "
                    f"{col} NOT NULL DEFAULT ''"
                )
            else:
                self._conn.execute(
                    f"ALTER TABLE revision_check ADD COLUMN {col} TEXT"
                )

    def close(self) -> None:
        self._conn.close()

    # ---------------------------------------------------------- pending

    def get_pending(self) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, created_at FROM pending WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        payload["created_at"] = row["created_at"]
        return payload

    def set_pending(self, payload: dict) -> None:
        """保存/替换待确认预览（发布前允许重新上传）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO pending (id, payload, created_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, "
                "created_at = excluded.created_at",
                (json.dumps(payload, ensure_ascii=False), _utcnow()),
            )

    def delete_pending(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM pending WHERE id = 1")

    # ---------------------------------------------------------- publication

    def get_publication(self) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, published_at FROM publication WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        payload["published_at"] = row["published_at"]
        return payload

    def publish(self, payload: dict) -> bool:
        """原子发布：写入 publication 并清空 pending。

        已存在发布结果时拒绝（返回 False），保证确认后不接受替换文件。
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                exists = cur.execute(
                    "SELECT 1 FROM publication WHERE id = 1"
                ).fetchone()
                if exists:
                    cur.execute("ROLLBACK")
                    return False
                cur.execute(
                    "INSERT INTO publication (id, payload, published_at) "
                    "VALUES (1, ?, ?)",
                    (json.dumps(payload, ensure_ascii=False), _utcnow()),
                )
                cur.execute("DELETE FROM pending")
                cur.execute("COMMIT")
                return True
            except Exception:
                cur.execute("ROLLBACK")
                raise

    # ---------------------------------------------------------- revision_check

    def get_revision_check(self) -> Optional[dict]:
        """返回修订核查状态；从未核查过则为 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT max_revision, max_revision_digest, last_outcome, last_reason, "
                "withdrawn, withdrawn_at, withdrawn_revision, withdrawn_reason, "
                "checked_at FROM revision_check WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        return self._row_state(row)

    def record_revision_check(
        self, revision: int, digest: str, outcome: str, reason: str | None
    ) -> tuple[str, dict]:
        """在单个事务内记录一次修订核查结论。

        outcome 为 "unchanged" 或 "withdrawn"；reason 为面向数据员的
        变化说明（不含任何计数）。

        返回 (status, state)：
        - "recorded"         : 已记录（state 为记录后的最新状态）；
        - "same_revision"    : 该修订号已有不同文件的结论，拒绝；
        - "stale"            : 已有更高的最高修订号，拒绝覆盖；
        - "no_publication"   : 没有已发布表可核查。
        拒绝时 state 也是事务内看到的最新状态，调用方据此说明原因。
        事务内重新读取状态做判定，避免并发下“较旧修订晚完成覆盖较新结论”。
        撤下是单向状态：一旦撤下，后续任何核查都不会把它改回公开；
        核对不变时仍保留首次撤下的修订号、原因与时间。
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                if cur.execute(
                    "SELECT 1 FROM publication WHERE id = 1"
                ).fetchone() is None:
                    cur.execute("ROLLBACK")
                    return "no_publication", {}

                row = cur.execute(
                    "SELECT max_revision, max_revision_digest, last_outcome, "
                    "last_reason, withdrawn, withdrawn_at, withdrawn_revision, "
                    "withdrawn_reason, checked_at "
                    "FROM revision_check WHERE id = 1"
                ).fetchone()

                if row is not None:
                    if revision < row["max_revision"]:
                        cur.execute("ROLLBACK")
                        return "stale", self._row_state(row)
                    if revision == row["max_revision"]:
                        if digest != row["max_revision_digest"]:
                            cur.execute("ROLLBACK")
                            return "same_revision", self._row_state(row)
                        # 同号同文件：结论已存在，重放是幂等的
                        cur.execute("COMMIT")
                        return "recorded", self._row_state(row)

                now = _utcnow()
                already_withdrawn = row is not None and bool(row["withdrawn"])
                if already_withdrawn:
                    # 撤下是单向的：后续核查（无论结论）都不恢复公开，
                    # 并保留首次撤下的修订号、原因与时间。
                    effective_withdrawn = True
                    withdrawn_at = row["withdrawn_at"]
                    withdrawn_revision = row["withdrawn_revision"]
                    withdrawn_reason = row["withdrawn_reason"]
                elif outcome == "withdrawn":
                    effective_withdrawn = True
                    withdrawn_at = now
                    withdrawn_revision = revision
                    withdrawn_reason = reason
                else:
                    effective_withdrawn = False
                    withdrawn_at = None
                    withdrawn_revision = None
                    withdrawn_reason = None

                state = {
                    "max_revision": revision,
                    "max_revision_digest": digest,
                    "last_outcome": outcome,
                    "last_reason": reason,
                    "withdrawn": effective_withdrawn,
                    "withdrawn_at": withdrawn_at,
                    "withdrawn_revision": withdrawn_revision,
                    "withdrawn_reason": withdrawn_reason,
                    "checked_at": now,
                }
                cur.execute(
                    "INSERT INTO revision_check (id, max_revision, max_revision_digest, "
                    "last_outcome, last_reason, withdrawn, withdrawn_at, "
                    "withdrawn_revision, withdrawn_reason, checked_at) "
                    "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "max_revision = excluded.max_revision, "
                    "max_revision_digest = excluded.max_revision_digest, "
                    "last_outcome = excluded.last_outcome, "
                    "last_reason = excluded.last_reason, "
                    "withdrawn = excluded.withdrawn, "
                    "withdrawn_at = excluded.withdrawn_at, "
                    "withdrawn_revision = excluded.withdrawn_revision, "
                    "withdrawn_reason = excluded.withdrawn_reason, "
                    "checked_at = excluded.checked_at",
                    (revision, digest, outcome, reason,
                     1 if effective_withdrawn else 0, withdrawn_at,
                     withdrawn_revision, withdrawn_reason, now),
                )
                cur.execute("COMMIT")
                return "recorded", state
            except Exception:
                cur.execute("ROLLBACK")
                raise

    @staticmethod
    def _row_state(row) -> dict:
        return {
            "max_revision": row["max_revision"],
            "max_revision_digest": row["max_revision_digest"],
            "last_outcome": row["last_outcome"],
            "last_reason": row["last_reason"],
            "withdrawn": bool(row["withdrawn"]),
            "withdrawn_at": row["withdrawn_at"],
            "withdrawn_revision": row["withdrawn_revision"],
            "withdrawn_reason": row["withdrawn_reason"],
            "checked_at": row["checked_at"],
        }
