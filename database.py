"""
database.py - SQLite persistence layer for Spotify Genre Classifier

Handles two main concerns:
  1. classification_cache  - Remember how each artist was classified so we never
                             call Spotify / Last.fm / Claude twice for the same artist.
  2. staging_tracks        - Hold tracks whose genre doesn't map to an existing playlist.
                             When a new genre accumulates enough tracks (STAGING_THRESHOLD)
                             the MCP server should auto-create a new playlist.
  3. playlist_registry     - Record every playlist that has been created, plus which
                             genre key it corresponds to.
"""

import sqlite3
import json
import os
from datetime import datetime

# How many staged tracks of the same genre triggers a new playlist
STAGING_THRESHOLD = 5

# Where the database file lives (same directory as this script)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spotify_classifier.db")


def get_connection() -> sqlite3.Connection:
    """Return a connection with row_factory set so rows behave like dicts."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # safer concurrent access
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables if they don't already exist. Safe to call on every startup."""
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS classification_cache (
                artist_name     TEXT    PRIMARY KEY,        -- lowercase, stripped
                genre           TEXT    NOT NULL,           -- our internal genre key
                source          TEXT    NOT NULL,           -- 'hardcoded'|'spotify'|'lastfm_claude'|'claude'
                spotify_tags    TEXT,                       -- JSON array of raw Spotify genre tags
                lastfm_tags     TEXT,                       -- JSON array of raw Last.fm tags
                claude_reasoning TEXT,                      -- short explanation from Claude
                created_at      TEXT    NOT NULL,
                updated_at      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS staging_tracks (
                track_id        TEXT    PRIMARY KEY,        -- Spotify track ID
                track_name      TEXT    NOT NULL,
                artist_name     TEXT    NOT NULL,
                suggested_genre TEXT    NOT NULL,           -- what Claude / Last.fm suggested
                suggested_playlist_name TEXT NOT NULL,      -- human-readable name suggestion
                confidence      REAL    NOT NULL DEFAULT 0, -- 0-1 score
                classification_source TEXT NOT NULL,
                staged_at       TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS playlist_registry (
                genre_key           TEXT    PRIMARY KEY,    -- our internal key, e.g. 'french_pop'
                playlist_name       TEXT    NOT NULL,       -- display name, e.g. '🇫🇷 French Pop'
                spotify_playlist_id TEXT,                   -- set once playlist is created on Spotify
                track_count         INTEGER NOT NULL DEFAULT 0,
                created_at          TEXT    NOT NULL,
                updated_at          TEXT    NOT NULL
            );
        """)


# ─────────────────────────────────────────────────────────────────────────────
# Classification Cache
# ─────────────────────────────────────────────────────────────────────────────

def cache_get(artist_name: str) -> dict | None:
    """
    Look up a cached classification for an artist.
    Returns a dict with keys: genre, source, spotify_tags, lastfm_tags, claude_reasoning
    or None if not cached.
    """
    key = _normalise(artist_name)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM classification_cache WHERE artist_name = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    return {
        "genre": row["genre"],
        "source": row["source"],
        "spotify_tags": json.loads(row["spotify_tags"]) if row["spotify_tags"] else [],
        "lastfm_tags": json.loads(row["lastfm_tags"]) if row["lastfm_tags"] else [],
        "claude_reasoning": row["claude_reasoning"],
    }


def cache_set(
    artist_name: str,
    genre: str,
    source: str,
    spotify_tags: list = None,
    lastfm_tags: list = None,
    claude_reasoning: str = None,
):
    """Upsert a classification result into the cache."""
    key = _normalise(artist_name)
    now = _now()
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO classification_cache
                (artist_name, genre, source, spotify_tags, lastfm_tags, claude_reasoning, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artist_name) DO UPDATE SET
                genre             = excluded.genre,
                source            = excluded.source,
                spotify_tags      = excluded.spotify_tags,
                lastfm_tags       = excluded.lastfm_tags,
                claude_reasoning  = excluded.claude_reasoning,
                updated_at        = excluded.updated_at
        """, (
            key, genre, source,
            json.dumps(spotify_tags or []),
            json.dumps(lastfm_tags or []),
            claude_reasoning,
            now, now,
        ))


# ─────────────────────────────────────────────────────────────────────────────
# Staging Area
# ─────────────────────────────────────────────────────────────────────────────

def staging_add(
    track_id: str,
    track_name: str,
    artist_name: str,
    suggested_genre: str,
    suggested_playlist_name: str,
    confidence: float,
    source: str,
):
    """Add a track to the staging area."""
    with get_connection() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO staging_tracks
                (track_id, track_name, artist_name, suggested_genre,
                 suggested_playlist_name, confidence, classification_source, staged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            track_id, track_name, artist_name,
            suggested_genre, suggested_playlist_name,
            confidence, source, _now(),
        ))


def staging_count_by_genre() -> dict[str, int]:
    """Return {genre_key: count} for all staged genres."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT suggested_genre, COUNT(*) as cnt
            FROM staging_tracks
            GROUP BY suggested_genre
        """).fetchall()
    return {row["suggested_genre"]: row["cnt"] for row in rows}


def staging_get_tracks_for_genre(genre_key: str) -> list[dict]:
    """Return all staged tracks for a given genre key."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM staging_tracks WHERE suggested_genre = ?", (genre_key,)
        ).fetchall()
    return [dict(row) for row in rows]


def staging_remove_genre(genre_key: str):
    """Remove all staged tracks for a genre (called after playlist creation)."""
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM staging_tracks WHERE suggested_genre = ?", (genre_key,)
        )


def staging_get_all() -> list[dict]:
    """Return every staged track."""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM staging_tracks ORDER BY staged_at").fetchall()
    return [dict(row) for row in rows]


def genres_ready_for_playlist() -> list[dict]:
    """
    Return a list of genres that have reached STAGING_THRESHOLD and are ready
    to have a real Spotify playlist created.
    Each entry: {genre, playlist_name, track_count, tracks: [...]}
    """
    counts = staging_count_by_genre()
    ready = []
    for genre, count in counts.items():
        if count >= STAGING_THRESHOLD:
            tracks = staging_get_tracks_for_genre(genre)
            playlist_name = tracks[0]["suggested_playlist_name"] if tracks else genre
            ready.append({
                "genre": genre,
                "playlist_name": playlist_name,
                "track_count": count,
                "tracks": tracks,
            })
    return ready


# ─────────────────────────────────────────────────────────────────────────────
# Playlist Registry
# ─────────────────────────────────────────────────────────────────────────────

def registry_get_all() -> list[dict]:
    """Return all known playlists."""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM playlist_registry ORDER BY created_at").fetchall()
    return [dict(row) for row in rows]


def registry_get(genre_key: str) -> dict | None:
    """Look up a playlist by genre key."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM playlist_registry WHERE genre_key = ?", (genre_key,)
        ).fetchone()
    return dict(row) if row else None


def registry_upsert(genre_key: str, playlist_name: str, spotify_playlist_id: str = None, track_count: int = 0):
    """Register or update a playlist."""
    now = _now()
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO playlist_registry
                (genre_key, playlist_name, spotify_playlist_id, track_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(genre_key) DO UPDATE SET
                playlist_name       = excluded.playlist_name,
                spotify_playlist_id = COALESCE(excluded.spotify_playlist_id, playlist_registry.spotify_playlist_id),
                track_count         = excluded.track_count,
                updated_at          = excluded.updated_at
        """, (genre_key, playlist_name, spotify_playlist_id, track_count, now, now))


def registry_increment(genre_key: str, delta: int = 1):
    """Increment the track count for a playlist."""
    with get_connection() as conn:
        conn.execute("""
            UPDATE playlist_registry
            SET track_count = track_count + ?, updated_at = ?
            WHERE genre_key = ?
        """, (delta, _now(), genre_key))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _normalise(name: str) -> str:
    """Lowercase and strip whitespace for consistent cache keys."""
    return name.lower().strip()


def _now() -> str:
    return datetime.utcnow().isoformat()


def get_stats() -> dict:
    """Return a summary dict useful for the get_classification_stats MCP tool."""
    with get_connection() as conn:
        cache_total = conn.execute("SELECT COUNT(*) FROM classification_cache").fetchone()[0]
        cache_by_source = {
            row["source"]: row["cnt"]
            for row in conn.execute(
                "SELECT source, COUNT(*) as cnt FROM classification_cache GROUP BY source"
            ).fetchall()
        }
        staging_total = conn.execute("SELECT COUNT(*) FROM staging_tracks").fetchone()[0]
        staging_by_genre = {
            row["suggested_genre"]: row["cnt"]
            for row in conn.execute(
                "SELECT suggested_genre, COUNT(*) as cnt FROM staging_tracks GROUP BY suggested_genre"
            ).fetchall()
        }
        playlist_count = conn.execute("SELECT COUNT(*) FROM playlist_registry").fetchone()[0]

    return {
        "cache": {
            "total_artists_cached": cache_total,
            "by_source": cache_by_source,
        },
        "staging": {
            "total_staged_tracks": staging_total,
            "threshold_for_new_playlist": STAGING_THRESHOLD,
            "by_genre": staging_by_genre,
            "genres_ready": [g["genre"] for g in genres_ready_for_playlist()],
        },
        "playlists": {
            "total_registered": playlist_count,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Initialise on import
# ─────────────────────────────────────────────────────────────────────────────
init_db()
