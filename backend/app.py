from __future__ import annotations

import gzip
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "crib-score.db"
BACKUP_DIR = DATA_DIR / "backups"
SEED_PATH = BASE_DIR / "crib_data.json"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def default_state() -> dict[str, Any]:
    return {
        "danName": "Dan",
        "genName": "Gen",
        "gameMode": "board",
    }


def default_stats() -> dict[str, Any]:
    return {
        "dan": {
            "wins": 0,
            "losses": 0,
            "skunksGiven": 0,
            "skunksReceived": 0,
            "streak": 0,
            "bestStreak": 0,
            "everBest": 0,
            "nibs": 0,
        },
        "gen": {
            "wins": 0,
            "losses": 0,
            "skunksGiven": 0,
            "skunksReceived": 0,
            "streak": 0,
            "bestStreak": 0,
            "everBest": 0,
            "nibs": 0,
        },
        "games": [],
    }


class SnapshotPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    stats: dict[str, Any] | None = None
    state: dict[str, Any] | None = None


class SyncPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    games: list[dict[str, Any]] = Field(default_factory=list)
    state: dict[str, Any] | None = None
    queuedAt: str | None = None
    source: str | None = Field(default="client")


app = FastAPI(title="Crib Score API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "PUT", "POST", "DELETE"],
    allow_headers=["*"],
)


def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {r["name"] for r in rows}


def migrate_legacy_schema(conn: sqlite3.Connection) -> None:
    cols = table_columns(conn, "games")
    if not cols:
        return

    if "created_at" not in cols:
        conn.execute("ALTER TABLE games ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE games ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")

    ts = now_iso()
    conn.execute(
        "UPDATE games SET created_at = ? WHERE created_at IS NULL OR created_at = ''",
        (ts,),
    )
    conn.execute(
        "UPDATE games SET updated_at = COALESCE(NULLIF(updated_at, ''), created_at, ?) "
        "WHERE updated_at IS NULL OR updated_at = ''",
        (ts,),
    )


def init_db() -> None:
    with closing(get_connection()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_game_id TEXT NOT NULL UNIQUE,
                winner TEXT NOT NULL,
                loser TEXT NOT NULL,
                win_score INTEGER NOT NULL,
                lose_score INTEGER NOT NULL,
                skunk INTEGER NOT NULL,
                initial_dealer TEXT NOT NULL,
                played_at TEXT NOT NULL,
                high_score_dan INTEGER,
                high_score_gen INTEGER,
                hs_player TEXT,
                nibs_dan INTEGER NOT NULL DEFAULT 0,
                nibs_gen INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS state_store (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

        migrate_legacy_schema(conn)

        row = conn.execute("SELECT id FROM state_store WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO state_store(id, state_json, updated_at) VALUES (1, ?, ?)",
                (json.dumps(default_state(), ensure_ascii=True), now_iso()),
            )
        conn.commit()


def set_meta(key: str, value: str) -> None:
    with closing(get_connection()) as conn:
        conn.execute(
            """
            INSERT INTO meta(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, now_iso()),
        )
        conn.commit()


def get_meta(key: str) -> str | None:
    with closing(get_connection()) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def read_state() -> dict[str, Any]:
    with closing(get_connection()) as conn:
        row = conn.execute("SELECT state_json FROM state_store WHERE id = 1").fetchone()

    if row is None:
        return default_state()

    try:
        raw = json.loads(row["state_json"])
    except json.JSONDecodeError:
        return default_state()

    return {
        "danName": raw.get("danName", "Dan"),
        "genName": raw.get("genName", "Gen"),
        "gameMode": raw.get("gameMode", "board"),
    }


def write_state(state_payload: dict[str, Any] | None) -> str:
    base = read_state()
    incoming = state_payload if isinstance(state_payload, dict) else {}
    merged = {
        "danName": incoming.get("danName", base.get("danName", "Dan")),
        "genName": incoming.get("genName", base.get("genName", "Gen")),
        "gameMode": incoming.get("gameMode", base.get("gameMode", "board")),
    }

    updated_at = now_iso()
    with closing(get_connection()) as conn:
        conn.execute(
            """
            INSERT INTO state_store(id, state_json, updated_at) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state_json = excluded.state_json,
                updated_at = excluded.updated_at
            """,
            (json.dumps(merged, ensure_ascii=True), updated_at),
        )
        conn.commit()
    return updated_at


def normalize_game(g: dict[str, Any], fallback_idx: int) -> dict[str, Any]:
    date_value = g.get("date") or now_iso()
    client_game_id = g.get("clientGameId") or f"legacy-{fallback_idx}-{date_value}"

    winner = g.get("winner", "dan")
    loser = g.get("loser", "gen")
    if winner not in ("dan", "gen"):
        winner = "dan"
    if loser not in ("dan", "gen"):
        loser = "gen" if winner == "dan" else "dan"

    win_score = int(g.get("winScore", 0) or 0)
    lose_score = int(g.get("loseScore", 0) or 0)

    return {
        "clientGameId": str(client_game_id),
        "winner": winner,
        "loser": loser,
        "winScore": win_score,
        "loseScore": lose_score,
        "skunk": bool(g.get("skunk", lose_score <= 75)),
        "initialDealer": g.get("initialDealer", "dan") if g.get("initialDealer") in ("dan", "gen") else "dan",
        "date": str(date_value),
        "highScoreDan": g.get("highScoreDan") if g.get("highScoreDan") is not None else None,
        "highScoreGen": g.get("highScoreGen") if g.get("highScoreGen") is not None else None,
        "hsPlayer": g.get("hsPlayer") if g.get("hsPlayer") in ("dan", "gen") else None,
        "nibsDan": int(g.get("nibsDan", 0) or 0),
        "nibsGen": int(g.get("nibsGen", 0) or 0),
    }


def upsert_games(conn: sqlite3.Connection, games: list[dict[str, Any]]) -> int:
    if not games:
        return 0

    sql = """
    INSERT INTO games (
        client_game_id, winner, loser, win_score, lose_score, skunk,
        initial_dealer, played_at, high_score_dan, high_score_gen, hs_player,
        nibs_dan, nibs_gen, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(client_game_id) DO UPDATE SET
        winner = excluded.winner,
        loser = excluded.loser,
        win_score = excluded.win_score,
        lose_score = excluded.lose_score,
        skunk = excluded.skunk,
        initial_dealer = excluded.initial_dealer,
        played_at = excluded.played_at,
        high_score_dan = excluded.high_score_dan,
        high_score_gen = excluded.high_score_gen,
        hs_player = excluded.hs_player,
        nibs_dan = excluded.nibs_dan,
        nibs_gen = excluded.nibs_gen,
        updated_at = excluded.updated_at
    """

    count = 0
    for idx, raw in enumerate(games):
        g = normalize_game(raw, idx)
        ts = now_iso()
        conn.execute(
            sql,
            (
                g["clientGameId"],
                g["winner"],
                g["loser"],
                g["winScore"],
                g["loseScore"],
                1 if g["skunk"] else 0,
                g["initialDealer"],
                g["date"],
                g["highScoreDan"],
                g["highScoreGen"],
                g["hsPlayer"],
                g["nibsDan"],
                g["nibsGen"],
                ts,
                ts,
            ),
        )
        count += 1

    return count


def list_games() -> list[dict[str, Any]]:
    with closing(get_connection()) as conn:
        rows = conn.execute(
            """
            SELECT client_game_id, winner, loser, win_score, lose_score, skunk,
                   initial_dealer, played_at, high_score_dan, high_score_gen,
                   hs_player, nibs_dan, nibs_gen
            FROM games
            ORDER BY played_at ASC, id ASC
            """
        ).fetchall()

    return [
        {
            "clientGameId": r["client_game_id"],
            "winner": r["winner"],
            "loser": r["loser"],
            "winScore": r["win_score"],
            "loseScore": r["lose_score"],
            "skunk": bool(r["skunk"]),
            "initialDealer": r["initial_dealer"],
            "date": r["played_at"],
            "highScoreDan": r["high_score_dan"],
            "highScoreGen": r["high_score_gen"],
            "hsPlayer": r["hs_player"],
            "nibsDan": r["nibs_dan"],
            "nibsGen": r["nibs_gen"],
        }
        for r in rows
    ]


def compute_stats_from_games(games: list[dict[str, Any]]) -> dict[str, Any]:
    stats = default_stats()

    for g in games:
        winner = g.get("winner")
        loser = g.get("loser")
        if winner not in ("dan", "gen") or loser not in ("dan", "gen") or winner == loser:
            continue

        stats[winner]["wins"] += 1
        stats[loser]["losses"] += 1

        if bool(g.get("skunk")):
            stats[winner]["skunksGiven"] += 1
            stats[loser]["skunksReceived"] += 1

        stats[winner]["streak"] += 1
        stats[loser]["streak"] = 0
        if stats[winner]["streak"] > stats[winner]["bestStreak"]:
            stats[winner]["bestStreak"] = stats[winner]["streak"]

        stats["dan"]["nibs"] += int(g.get("nibsDan", 0) or 0)
        stats["gen"]["nibs"] += int(g.get("nibsGen", 0) or 0)

        hs_dan = int(g.get("highScoreDan", 0) or 0)
        hs_gen = int(g.get("highScoreGen", 0) or 0)
        hs_player = g.get("hsPlayer")
        if hs_dan > 0 and hs_gen > 0:
            if hs_player == "dan" and hs_dan > stats["dan"]["everBest"]:
                stats["dan"]["everBest"] = hs_dan
            elif hs_player == "gen" and hs_gen > stats["gen"]["everBest"]:
                stats["gen"]["everBest"] = hs_gen
            elif hs_dan >= hs_gen and hs_dan > stats["dan"]["everBest"]:
                stats["dan"]["everBest"] = hs_dan
            elif hs_gen > stats["gen"]["everBest"]:
                stats["gen"]["everBest"] = hs_gen
        else:
            if hs_dan > stats["dan"]["everBest"]:
                stats["dan"]["everBest"] = hs_dan
            if hs_gen > stats["gen"]["everBest"]:
                stats["gen"]["everBest"] = hs_gen

    stats["games"] = games
    return stats


def build_snapshot() -> dict[str, Any]:
    games = list_games()
    state = read_state()
    stats = compute_stats_from_games(games)
    return {
        "stats": stats,
        "state": state,
        "updatedAt": now_iso(),
    }


def replace_all_games(games: list[dict[str, Any]]) -> int:
    with closing(get_connection()) as conn:
        conn.execute("DELETE FROM games")
        inserted = upsert_games(conn, games)
        conn.commit()
    return inserted


def bootstrap_from_seed_if_needed() -> None:
    if not SEED_PATH.exists():
        return
    if get_meta("seed_import_v2_done") == "1":
        return

    existing_games = list_games()
    if existing_games:
        set_meta("seed_import_v2_done", "1")
        return

    try:
        payload = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return

    stats_payload = payload.get("stats") if isinstance(payload, dict) else None
    if not isinstance(stats_payload, dict):
        return
    games = stats_payload.get("games")
    if not isinstance(games, list):
        return

    with closing(get_connection()) as conn:
        upsert_games(conn, games)
        conn.commit()

    incoming_state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    write_state(incoming_state)
    set_meta("seed_import_v2_done", "1")


def copy_sqlite_database(source_path: Path, target_path: Path) -> None:
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    target = sqlite3.connect(target_path)
    try:
        with target:
            source.backup(target)
    finally:
        source.close()
        target.close()


def backup_metadata(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.name,
        "sizeBytes": stat.st_size,
        "modifiedAt": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
    }


def update_latest_backup_link(archive_path: Path) -> None:
    latest_link = BACKUP_DIR / "latest.sqlite.gz"
    latest_link.unlink(missing_ok=True)
    latest_link.symlink_to(archive_path.name)


def create_backup_archive(prefix: str = "crib-score", *, update_latest: bool = False) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    snapshot_path = BACKUP_DIR / f"{prefix}-{stamp}.sqlite"
    archive_path = snapshot_path.with_suffix(snapshot_path.suffix + ".gz")

    copy_sqlite_database(DB_PATH, snapshot_path)
    try:
        with snapshot_path.open("rb") as src, gzip.open(archive_path, "wb") as dst:
            dst.write(src.read())
    finally:
        snapshot_path.unlink(missing_ok=True)

    if update_latest:
        update_latest_backup_link(archive_path)

    return archive_path


def get_latest_backup_path() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    latest_link = BACKUP_DIR / "latest.sqlite.gz"
    if latest_link.exists():
        resolved = latest_link.resolve(strict=True)
        if resolved.exists():
            return resolved

    candidates = sorted(BACKUP_DIR.glob("crib-score-*.sqlite.gz"))
    if candidates:
        return candidates[-1]

    raise FileNotFoundError("No SQLite backup found")


def restore_latest_backup() -> dict[str, Any]:
    backup_path = get_latest_backup_path()
    pre_restore_archive = create_backup_archive(prefix="pre-restore")

    with TemporaryDirectory() as tmp_dir:
        restored_db_path = Path(tmp_dir) / "restored.sqlite"
        with gzip.open(backup_path, "rb") as src, restored_db_path.open("wb") as dst:
            dst.write(src.read())

        source = sqlite3.connect(restored_db_path)
        target = get_connection()
        try:
            with target:
                source.backup(target)
        finally:
            source.close()
            target.close()

    snap = build_snapshot()
    return {
        "ok": True,
        "stats": snap["stats"],
        "state": snap["state"],
        "updatedAt": snap["updatedAt"],
        "restoredFrom": backup_path.name,
        "preRestoreBackup": pre_restore_archive.name,
    }


@app.on_event("startup")
def startup() -> None:
    init_db()
    bootstrap_from_seed_if_needed()


@app.get("/api/health")
def health() -> dict[str, Any]:
    try:
        with closing(get_connection()) as conn:
            conn.execute("SELECT 1").fetchone()
            # Validate sync-path schema so the UI does not oscillate online/offline
            # when /api/health is OK but /api/sync would fail.
            conn.execute("SELECT updated_at FROM games LIMIT 1").fetchone()
        return {"status": "ok", "db": "ok"}
    except sqlite3.Error as exc:
        return {"status": "degraded", "db": f"error: {exc}"}


@app.get("/api/snapshot")
def get_snapshot() -> JSONResponse:
    snap = build_snapshot()
    return JSONResponse({"ok": True, **snap})


@app.get("/api/stats")
def get_stats() -> JSONResponse:
    snap = build_snapshot()
    return JSONResponse({"ok": True, "stats": snap["stats"], "updatedAt": snap["updatedAt"]})


@app.put("/api/snapshot")
def put_snapshot(payload: SnapshotPayload) -> dict[str, Any]:
    incoming_games: list[dict[str, Any]] = []
    if isinstance(payload.stats, dict) and isinstance(payload.stats.get("games"), list):
        incoming_games = payload.stats.get("games") or []

    replace_all_games(incoming_games)
    write_state(payload.state if isinstance(payload.state, dict) else None)

    snap = build_snapshot()
    return {"ok": True, "updatedAt": snap["updatedAt"], "gameCount": len(snap["stats"].get("games", []))}


@app.post("/api/sync")
def post_sync(payload: SyncPayload) -> dict[str, Any]:
    with closing(get_connection()) as conn:
        accepted = upsert_games(conn, payload.games or [])
        conn.commit()

    if isinstance(payload.state, dict):
        write_state(payload.state)

    snap = build_snapshot()
    return {
        "ok": True,
        "acceptedGames": accepted,
        "updatedAt": snap["updatedAt"],
        "source": payload.source or "client",
        "snapshot": {
            "stats": snap["stats"],
            "state": snap["state"],
        },
    }


@app.get("/api/games")
def get_games() -> JSONResponse:
    games = list_games()
    return JSONResponse({"ok": True, "games": games})


@app.get("/api/backups/latest")
def get_latest_backup() -> JSONResponse:
    try:
        path = get_latest_backup_path()
        return JSONResponse({"exists": True, **backup_metadata(path)})
    except FileNotFoundError:
        return JSONResponse({"exists": False})


@app.post("/api/backups/create")
def post_create_backup() -> JSONResponse:
    try:
        archive_path = create_backup_archive(prefix="crib-score", update_latest=True)
        return JSONResponse({"ok": True, **backup_metadata(archive_path)})
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"Backup creation failed: {exc}") from exc


@app.post("/api/restore-latest")
def post_restore_latest() -> JSONResponse:
    try:
        return JSONResponse(restore_latest_backup())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Restore failed: {exc}") from exc
