"""统一状态库：术语库 / 角色卡 / 场景摘要 / 项目元信息。

为什么和译文缓存共用一个 SQLite 文件：

1. 用户备份、迁移、清除只需要关心一个文件；
2. 避免"术语库改了但缓存不知道"这类跨库不一致——现在它们在同一份文件里，
   清除缓存不会误伤术语积累，反之亦然；
3. 现有 ``TranslationCache`` 用的是 ``translations`` 表，这里用别的表名，
   两者互不影响，属于纯增量接入。

内存模式（path=":memory:" 或打不开）下所有状态仅存活于本次运行，不会抛异常——
词条 LONG 目标是"少啰嗦地降级"，不是让整条管线挂掉。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .common import data_root

_BASE_DIR = data_root()

#: 与 app.py 的 ``CACHE_DIR / "translations.sqlite"`` 是同一个文件。
#: 这样维护者只需要记住一个路径；表不同所以互不干扰。
DEFAULT_DB = _BASE_DIR / "cache" / "translations.sqlite"

#: 术语的四个状态。流转见 core/termbase.py 顶部注释。
TERM_STATES = ("candidate", "adopted", "disputed", "rejected")

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS terms ("
    " source TEXT PRIMARY KEY,"
    " target TEXT NOT NULL,"
    " state TEXT NOT NULL,"
    " note TEXT NOT NULL DEFAULT '',"
    " first_seen TEXT NOT NULL DEFAULT '',"
    " updated_at INTEGER NOT NULL"
    ")",
    "CREATE TABLE IF NOT EXISTS term_variants ("
    " variant TEXT PRIMARY KEY,"
    " source TEXT NOT NULL"
    ")",
    "CREATE TABLE IF NOT EXISTS characters ("
    " name TEXT PRIMARY KEY,"
    " sheet TEXT NOT NULL,"
    " updated_at INTEGER NOT NULL"
    ")",
    "CREATE TABLE IF NOT EXISTS scenes ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " scene_id TEXT NOT NULL UNIQUE,"
    " summary TEXT NOT NULL,"
    " order_hint INTEGER NOT NULL DEFAULT 0,"
    " created_at INTEGER NOT NULL"
    ")",
    "CREATE TABLE IF NOT EXISTS tm ("
    " source TEXT PRIMARY KEY,"
    " target TEXT NOT NULL,"
    " updated_at INTEGER NOT NULL"
    ")",
)

_TM_ORIGINS: tuple[str, ...] = ("auto", "human", "tmx")

_TM_DEFAULT_ORIGIN = "auto"

#: tm 表的新增列（``_migrate_tm_columns`` 用 ``PRAGMA table_info`` 探测后逐列
#: ADD COLUMN）。三列解决三件以前做不到的事：
#: * ``origin``——这条译文是谁给的（引擎 auto / 人工 human / 外部 TMX tmx）。
#:   没有它就无法回答「这条记忆能不能信」；
#: * ``confirmed``——人工确认过没有。未确认的译文不许通过模糊匹配被复印到
#:   别的句子上（见 core/tm.py 的取舍说明）；
#: * ``src_lang``——源语言。记忆库不按语种分表（一张表里混着英文与其他语言），
#:   记下真实源语言，导出 TMX 时才能逐个 tu 标 srclang，而不是一律标 en 骗人。
#: 列的顺序即迁移顺序；每项 (列名, 类型与默认值)。默认值不能省——
#: 给已有行 ALTER ADD COLUMN 时必须非空。
_TM_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("origin", f"TEXT NOT NULL DEFAULT '{_TM_DEFAULT_ORIGIN}'"),
    ("confirmed", "INTEGER NOT NULL DEFAULT 0"),
    ("src_lang", "TEXT NOT NULL DEFAULT ''"),
)

_SCHEMA_VERSION = 1
_META_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS state_meta ("
    " key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


def _migrate_tm_columns(conn: "sqlite3.Connection") -> list[str]:
    """给 tm 表补列，返回本次补上的列名（幂等：已存在就不动）。

    为什么必须幂等：这个迁移在**每次** ``StateStore()`` 构造时都跑一遍
    （不是只跑一次的一次性脚本），用户的库早就可能是新结构了。
    SQLite 的 ``ALTER TABLE ADD COLUMN`` 遇到同名列直接报
    "duplicate column name"，所以用 ``PRAGMA table_info`` 先探测再补。

    ``PRAGMA table_info`` 返回 6 元组，第 2 项（下标 1）是列名——按位置取
    而不是按名称解包，是因为不同 SQLite 版本返回的行 factory 不一致，
    统一按下标读最稳。
    """
    added: list[str] = []
    existing = {row[1] for row in conn.execute("PRAGMA table_info(tm)")}
    for name, ddl in _TM_EXTRA_COLUMNS:
        if name in existing:
            continue
        conn.execute(f"ALTER TABLE tm ADD COLUMN {name} {ddl}")
        added.append(name)
    return added


class StateStore:
    """统一状态库。所有写操作都带 lock，允许多线程调用。"""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.path = str(path or DEFAULT_DB)
        self.log = log or (lambda message: None)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        try:
            if self.path != ":memory:":
                parent = os.path.dirname(self.path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            for statement in _META_SCHEMA + _SCHEMA:
                self._conn.execute(statement)
            # 结构迁移放在建表之后：老库是在没有这三列的时代建的，
            # CREATE TABLE IF NOT EXISTS 对已存在的表什么都不做，
            # 只有独立的 ADD COLUMN 迁移能把它们补上。
            try:
                _migrate_tm_columns(self._conn)
            except Exception as exc:  # 迁移失败不该让状态库整体不可用
                self.log(f"翻译记忆表结构升级失败（{type(exc).__name__}），按旧结构继续")
            self._conn.execute(
                "INSERT OR REPLACE INTO state_meta(key, value) VALUES (?, ?)",
                ("schema_version", str(_SCHEMA_VERSION)),
            )
            self._conn.commit()
        except Exception as exc:  # 状态库不是主流程，失败就退化成内存态
            self._conn = None
            self.log(f"状态库不可用（{type(exc).__name__}），本次不保存术语/角色/场景状态")

    @property
    def available(self) -> bool:
        return self._conn is not None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # ---------------------------------------------------------------- terms

    def upsert_term(
        self,
        source: str,
        target: str,
        state: str = "adopted",
        note: str = "",
        first_seen: str = "",
    ) -> None:
        """写入一条术语。已存在则更新译名/状态，但保留 first_seen。"""
        if not source:
            return
        # 「候选」允许还没有译名——预处理抽出来的专有名词就只有源词，
        # 译名要等模型第一次给出来再补。其余状态没有译名就没有意义。
        if state != "candidate" and not target:
            return
        if state not in TERM_STATES:
            state = "adopted"
        with self._lock:
            if self._conn is None:
                return
            existing = self.get_term(source)
            keep_seen = existing.get("first_seen", "") if existing else ""
            self._conn.execute(
                "INSERT INTO terms(source, target, state, note, first_seen, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source) DO UPDATE SET "
                "target=excluded.target, state=excluded.state, note=excluded.note,"
                " first_seen=excluded.first_seen, updated_at=excluded.updated_at",
                (
                    source,
                    target,
                    state,
                    note,
                    first_seen or keep_seen,
                    int(time.time()),
                ),
            )
            self._conn.commit()

    def get_term(self, source: str) -> dict[str, Any] | None:
        with self._lock:
            if self._conn is None or not source:
                return None
            row = self._conn.execute(
                "SELECT source, target, state, note, first_seen FROM terms WHERE source=?",
                (source,),
            ).fetchone()
        if not row:
            return None
        return {
            "source": row[0],
            "target": row[1],
            "state": row[2],
            "note": row[3],
            "first_seen": row[4],
        }

    def all_terms(self) -> list[dict[str, Any]]:
        return self._query_terms("SELECT source, target, state, note, first_seen FROM terms")

    def terms_by_state(self, state: str) -> list[dict[str, Any]]:
        return self._query_terms(
            "SELECT source, target, state, note, first_seen FROM terms WHERE state=?",
            (state,),
        )

    def _query_terms(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            if self._conn is None:
                return []
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "source": row[0],
                "target": row[1],
                "state": row[2],
                "note": row[3],
                "first_seen": row[4],
            }
            for row in rows
        ]

    def update_state(self, source: str, state: str, note: str | None = None) -> None:
        with self._lock:
            if self._conn is None or state not in TERM_STATES:
                return
            if note is None:
                self._conn.execute(
                    "UPDATE terms SET state=?, updated_at=? WHERE source=?",
                    (state, int(time.time()), source),
                )
            else:
                self._conn.execute(
                    "UPDATE terms SET state=?, note=?, updated_at=? WHERE source=?",
                    (state, note, int(time.time()), source),
                )
            self._conn.commit()

    def update_target(self, source: str, target: str) -> None:
        """全局重命名：把某个源词的译名整体换掉。"""
        with self._lock:
            if self._conn is None or not target:
                return
            self._conn.execute(
                "UPDATE terms SET target=?, updated_at=? WHERE source=?",
                (target, int(time.time()), source),
            )
            self._conn.commit()

    def delete_term(self, source: str) -> None:
        with self._lock:
            if self._conn is None:
                return
            self._conn.execute("DELETE FROM terms WHERE source=?", (source,))
            self._conn.execute("DELETE FROM term_variants WHERE source=?", (source,))
            self._conn.commit()

    # -------------------------------------------------------- term variants

    def add_variant(self, source: str, variant: str) -> None:
        if not source or not variant or variant == source:
            return
        with self._lock:
            if self._conn is None:
                return
            self._conn.execute(
                "INSERT OR REPLACE INTO term_variants(variant, source) VALUES (?, ?)",
                (variant, source),
            )
            self._conn.commit()

    def variants_of(self, source: str) -> list[str]:
        with self._lock:
            if self._conn is None:
                return []
            rows = self._conn.execute(
                "SELECT variant FROM term_variants WHERE source=?", (source,)
            ).fetchall()
        return [row[0] for row in rows]

    def canonical_of(self, surface: str) -> str:
        """把变体还原成标准源词，本身就是标准词则原样返回。"""
        with self._lock:
            if self._conn is None:
                return surface
            row = self._conn.execute(
                "SELECT source FROM term_variants WHERE variant=?", (surface,)
            ).fetchone()
        return row[0] if row else surface

    # ----------------------------------------------------------- characters

    def upsert_character(self, name: str, sheet: dict[str, Any]) -> None:
        with self._lock:
            if self._conn is None or not name:
                return
            self._conn.execute(
                "INSERT INTO characters(name, sheet, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET sheet=excluded.sheet,"
                " updated_at=excluded.updated_at",
                (name, json.dumps(sheet, ensure_ascii=False), int(time.time())),
            )
            self._conn.commit()

    def get_character(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            if self._conn is None or not name:
                return None
            row = self._conn.execute(
                "SELECT sheet FROM characters WHERE name=?", (name,)
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None

    def all_characters(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            if self._conn is None:
                return {}
            rows = self._conn.execute("SELECT name, sheet FROM characters").fetchall()
        sheets: dict[str, dict[str, Any]] = {}
        for name, raw in rows:
            try:
                sheets[name] = json.loads(raw)
            except Exception:
                continue
        return sheets

    # --------------------------------------------------------------- scenes

    def add_scene(self, scene_id: str, summary: str, order_hint: int = 0) -> None:
        with self._lock:
            if self._conn is None or not scene_id:
                return
            self._conn.execute(
                "INSERT INTO scenes(scene_id, summary, order_hint, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(scene_id) DO UPDATE SET "
                "summary=excluded.summary, created_at=excluded.created_at",
                (scene_id, summary, order_hint, int(time.time())),
            )
            self._conn.commit()

    def recent_scenes(self, limit: int = 5) -> list[str]:
        with self._lock:
            if self._conn is None:
                return []
            rows = self._conn.execute(
                "SELECT summary FROM scenes ORDER BY order_hint DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        seen: list[str] = []
        for row in rows:
            summary = row[0]
            if summary and summary not in seen:
                seen.append(summary)
        return seen

    def clear_scenes(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            self._conn.execute("DELETE FROM scenes")
            self._conn.commit()

    # ------------------------------------------------------------------ tm

    def upsert_tm(
        self,
        source: str,
        target: str,
        origin: str = _TM_DEFAULT_ORIGIN,
        confirmed: int = 0,
        src_lang: str = "",
    ) -> None:
        """写入一条翻译记忆。已存在则更新译文（以最新一条为准）。

        ``origin`` / ``confirmed`` / ``src_lang`` 是 v1.5.6 给 TM 加的三条
        元信息（``_TM_EXTRA_COLUMNS`` 有逐列理由）：分别回答「译文从哪来」
        「人工确认过没有」「原文是什么语言」。默认 ``origin="auto"``、
        ``confirmed=0``——引擎自动生成且没人复核过的译文就是这个值，
        调用方要声明别的来源必须显式传，不给糊涂账留方便。

        已有调用方传两参数的行为一字不变（新增的都是带默认值的尾参）。
        """
        if not source or not target:
            return
        if origin not in _TM_ORIGINS:
            origin = _TM_DEFAULT_ORIGIN
        with self._lock:
            if self._conn is None:
                return
            self._conn.execute(
                "INSERT INTO tm(source, target, updated_at, origin, confirmed, src_lang)"
                " VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source) DO UPDATE SET target=excluded.target,"
                " updated_at=excluded.updated_at, origin=excluded.origin,"
                " confirmed=excluded.confirmed, src_lang=excluded.src_lang",
                (
                    source,
                    target,
                    int(time.time()),
                    origin,
                    1 if confirmed else 0,
                    src_lang or "",
                ),
            )
            self._conn.commit()

    def get_tm(self, source: str) -> str | None:
        with self._lock:
            if self._conn is None or not source:
                return None
            row = self._conn.execute(
                "SELECT target FROM tm WHERE source=?", (source,)
            ).fetchone()
        return row[0] if row else None

    def all_tm(self, limit: int = 20000) -> list[tuple[str, str]]:
        with self._lock:
            if self._conn is None:
                return []
            rows = self._conn.execute(
                "SELECT source, target FROM tm ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def all_tm_full(self, limit: int = 20000) -> list[dict[str, object]]:
        """带元信息读出全部记忆：``all_tm`` 只有 (source, target) 两列。

        给出的是字典列表 ``{source, target, origin, confirmed, src_lang}``，
        ``confirmed`` 已转成 bool。TMX 导出要按 srclang 逐个标语言、
        100% 直通与「近似复用待确认」的分级要看 origin/confirmed——
        只有两列的 ``all_tm`` 撑不起这两件事，所以单开一个方法而不是给
        ``all_tm`` 加参数（加参数会让它从"轻量热路径"变成"什么都读"）。
        """
        with self._lock:
            if self._conn is None:
                return []
            rows = self._conn.execute(
                "SELECT source, target, origin, confirmed, src_lang FROM tm "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "source": row[0] or "",
                "target": row[1] or "",
                "origin": row[2] or _TM_DEFAULT_ORIGIN,
                "confirmed": bool(row[3]),
                "src_lang": row[4] or "",
            }
            for row in rows
        ]

    def count_tm(self) -> int:
        with self._lock:
            if self._conn is None:
                return 0
            row = self._conn.execute("SELECT COUNT(*) FROM tm").fetchone()
        return int(row[0]) if row else 0

    def clear_translations(self) -> None:
        """只清译文缓存表，不动术语库/角色卡/翻译记忆。

        「清除缓存」是用户的日常操作，而术语积累可能是几个项目攒下来的——
        早年的实现直接删整个 SQLite 文件，等于让用户拿术语库换缓存空间。
        同库多表之后，这条界线必须由代码保证，而不是靠用户记得别点。
        """
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute("DELETE FROM translations")
                self._conn.commit()
                self._conn.execute("VACUUM")
            except Exception:
                pass

    # ----------------------------------------------------------------- misc

    def vacuum(self) -> None:
        """收缩数据库。长期反复翻译会留下空洞，清缓存时顺手压一把。"""
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.commit()
                self._conn.execute("VACUUM")
            except Exception:
                pass

    def clear_tables(self, *tables: str) -> None:
        allowed = {"terms", "term_variants", "characters", "scenes", "tm"}
        with self._lock:
            if self._conn is None:
                return
            for table in tables:
                if table in allowed:
                    self._conn.execute(f"DELETE FROM {table}")
            self._conn.commit()
