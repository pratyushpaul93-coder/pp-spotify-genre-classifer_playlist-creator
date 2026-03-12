"""
server.py - Spotify MCP Server (v2 — with SQLite, tiered classification, dynamic playlists)

Tools exposed to Claude:
  get_liked_songs            Fetch liked songs from Spotify
  preview_genre_playlists    Dry-run: show what playlists would be created
  create_genre_playlists     Actually create playlists on Spotify
  sync_new_songs             Classify & sync newly liked songs into existing playlists
  get_classification_stats   Show DB stats: cache hits, staging counts, etc.
  check_staging_area         Show staged tracks + genres that are ready for new playlists
  promote_staged_genre       Manually promote a staged genre → create its playlist now
"""

import asyncio
import json
import os
from collections import defaultdict
from typing import Any

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv
from mcp.server import Server
from mcp.types import Tool, TextContent

import database as db
from spotify_genre_classifier import SpotifyGenreClassifier, PLAYLIST_NAMES

load_dotenv()

server = Server("spotify")

# ──────────────────────────────────────────────────────────────────────────────
# Spotify client
# ──────────────────────────────────────────────────────────────────────────────

def get_spotify_client() -> spotipy.Spotify:
    return spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id=os.getenv("SPOTIFY_CLIENT_ID"),
            client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
            redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8080/callback"),
            scope=(
                "user-library-read "
                "playlist-modify-public "
                "playlist-modify-private "
                "playlist-read-private"
            ),
        )
    )


# ──────────────────────────────────────────────────────────────────────────────
# Tool definitions
# ──────────────────────────────────────────────────────────────────────────────


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_liked_songs",
            description="Fetch liked songs from your Spotify library.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max songs to fetch (default 50, max 50 per request).",
                        "default": 50,
                    }
                },
            },
        ),
        Tool(
            name="preview_genre_playlists",
            description="Dry-run classification of all liked songs. Shows what playlists would be created without touching Spotify.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="create_genre_playlists",
            description="Classify all liked songs and create genre playlists on Spotify.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="sync_new_songs",
            description="Classify recently liked songs and add them to existing genre playlists.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of recent liked songs to sync (default 20).",
                        "default": 20,
                    }
                },
            },
        ),
        Tool(
            name="get_classification_stats",
            description="Show SQLite DB stats: cache hits by source, staging counts, ready-to-promote genres.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="check_staging_area",
            description="List staged tracks and highlight any genres that have reached the threshold for a new playlist.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="promote_staged_genre",
            description="Manually promote a staged genre: create its Spotify playlist now, even if below threshold.",
            inputSchema={
                "type": "object",
                "properties": {
                    "genre_key": {
                        "type": "string",
                        "description": "The internal genre key to promote (e.g. 'french_pop').",
                    }
                },
                "required": ["genre_key"],
            },
        ),
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Tool handlers
# ──────────────────────────────────────────────────────────────────────────────


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:

    # └ get_liked_songs ┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒
    if name == "get_liked_songs":
        limit = arguments.get("limit", 50)
        sp = get_spotify_client()
        tracks = _fetch_liked_songs(sp, limit)
        return _text({"total_tracks": len(tracks), "tracks": tracks})

    # └ preview_genre_playlists ┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒
    if name == "preview_genre_playlists":
        sp = get_spotify_client()
        classifier = SpotifyGenreClassifier(spotify_client=sp)
        tracks = _fetch_liked_songs(sp, limit=500)
        categorised, staged_preview, report = _classify_tracks(classifier, tracks, dry_run=True)
        return _text(_build_preview_report(categorised, staged_preview, report, dry_run=True))

    # └ create_genre_playlists ┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒
    if name == "create_genre_playlists":
        sp = get_spotify_client()
        classifier = SpotifyGenreClassifier(spotify_client=sp)
        tracks = _fetch_liked_songs(sp, limit=500)
        categorised, staged_preview, report = _classify_tracks(classifier, tracks, dry_run=False)

        user_id = sp.current_user()["id"]
        playlist_results = _create_playlists_on_spotify(sp, categorised, user_id, classifier)

        # Auto-promote any staged genres that crossed the threshold
        auto_promoted = _auto_promote_staged_genres(sp, user_id)

        return _text({
            "playlists_created": playlist_results,
            "auto_promoted_new_playlists": auto_promoted,
            "staging_summary": db.staging_count_by_genre(),
        })

    # └ sync_new_songs ┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒
    if name == "sync_new_songs":
        limit = arguments.get("limit", 20)
        sp = get_spotify_client()
        classifier = SpotifyGenreClassifier(spotify_client=sp)
        tracks = _fetch_liked_songs(sp, limit=limit)

        synced = []
        staged = []

        for track in tracks:
            result = classifier.classify_track(
                track_id=track["id"],
                track_name=track["name"],
                artist_name=track["artist"],
            )
            genre = result["primary_genre"]

            if result["staged"]:
                staged.append({"track": track["name"], "suggested_genre": genre})
                continue

            # Add to existing playlist if it's registered
            playlist_info = db.registry_get(genre)
            if playlist_info and playlist_info.get("spotify_playlist_id"):
                sp.playlist_add_items(
                    playlist_info["spotify_playlist_id"], [f"spotify:track:{track['id']}"]
                )
                db.registry_increment(genre)
                synced.append({"track": track["name"], "playlist": result["playlist_name"]})

        # Auto-promote threshold crossings
        user_id = sp.current_user()["id"]
        auto_promoted = _auto_promote_staged_genres(sp, user_id)

        return _text({
            "synced_tracks": len(synced),
            "staged_tracks": len(staged),
            "details": synced,
            "staged_details": staged,
            "auto_promoted_new_playlists": auto_promoted,
        })

    # └ get_classification_stats ┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒
    if name == "get_classification_stats":
        stats = db.get_stats()
        registry = db.registry_get_all()
        stats["registered_playlists"] = registry
        return _text(stats)

    # └ check_staging_area ┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒
    if name == "check_staging_area":
        all_staged = db.staging_get_all()
        counts = db.staging_count_by_genre()
        ready = db.genres_ready_for_playlist()
        return _text({
            "total_staged": len(all_staged),
            "threshold": db.STAGING_THRESHOLD,
            "by_genre": counts,
            "ready_for_new_playlist": ready,
            "all_staged_tracks": all_staged,
        })

    # └ promote_staged_genre ┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒┒
    if name == "promote_staged_genre":
        genre_key = arguments["genre_key"]
        sp = get_spotify_client()
        user_id = sp.current_user()["id"]
        result = _promote_genre(sp, genre_key, user_id)
        return _text(result)

    raise ValueError(f"Unknown tool: {name}")


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────


def _fetch_liked_songs(sp: spotipy.Spotify, limit: int) -> list[dict]:
    tracks = []
    offset = 0
    while True:
        results = sp.current_user_saved_tracks(limit=min(limit - len(tracks), 50), offset=offset)
        if not results["items"]:
            break
        for item in results["items"]:
            t = item["track"]
            tracks.append({
                "name": t["name"],
                "artist": ", ".join(a["name"] for a in t["artists"]),
                "album": t["album"]["name"],
                "id": t["id"],
                "uri": t["uri"],
                "artist_id": t["artists"][0]["id"] if t["artists"] else None,
            })
        offset += len(results["items"])
        if len(tracks) >= limit or offset >= results["total"]:
            break
    return tracks


def _classify_tracks(
    classifier: SpotifyGenreClassifier,
    tracks: list[dict],
    dry_run: bool,
) -> tuple[dict, list, dict]:
    """
    Classify all tracks.
    Returns:
        categorised   – {genre_key: [track, ...]}
        staged       – [tracks that went to staging]
        report        – summary stats dict
    """
    categorised: dict[str, list] = defaultdict(list)
    staged = []
    method_counts: dict[str, int] = defaultdict(int)
    confidence_counts: dict[str, int] = defaultdict(int)

    for track in tracks:
        result = classifier.classify_track(
            track_id=track["id"],
            track_name=track["name"],
            artist_name=track["artist"],
            artist_id=track.get("artist_id"),
        )
        method_counts[result["method"]] += 1
        confidence_counts[result["confidence"]] += 1

        if result["staged"] and not dry_run:
            staged.append({**$track, "suggested_genre": result["primary_genre"],
                           "suggested_playlist": result["playlist_name"]})
        else:
            categorised[result["primary_genre"]].append({
                **track,
                "playlist_name": result["playlist_name"],
                "confidence": result["confidence"],
            })

    report = {
        "total_tracks": len(tracks),
        "classified": sum(len(v) for v in categorised.values()),
        "staged": len(staged),
        "by_confidence": dict(confidence_counts),
        "by_method": dict(method_counts),
    }
    return dict(categorised), staged, report


def _build_preview_report(
    categorised: dict, staged: list, report: dict, dry_run: bool
) -> dict:
    playlists = []
    for genre, tracks in sorted(categorised.items(), key=lambda x: -len(x[1])):
        playlists.append({
            "playlist": tracks[0]["playlist_name"] if tracks else genre,
            "genre_key": genre,
            "track_count": len(tracks),
            "sample": [f"{t['name']} — {t['artist']}" for t in tracks[:3]],
        })
    return {
        "mode": "DRY RUN —!nothing created on Spotify" if dry_run else "LIVE",
        "summary": report,
        "playlists": playlists,
        "staging_preview": [
            f"{t['name']} ‒ {t.get('suggested_genre', 'unknown')}" for t in staged[:10]
        ],
        "staging_counts": db.staging_count_by_genre(),
        "tip": "Use 'create_genre_playlists' to create these on Spotify.",
    }


def _create_playlists_on_spotify(
    sp: spotipy.Spotify,
    categorised: dict,
    user_id: str,
    classifier: SpotifyGenreClassifier,
) -> list[dict]:
    results = []
    for genre, tracks in categorised.items():
        if genre == "uncategorized" or not tracks:
            continue

        playlist_name = tracks[0]["playlist_name"]
        track_uris = [f"spotify:track:{t['id']}" for t in tracks]

        # Create or reuse playlist
        existing = db.registry_get(genre)
        if existing and existing.get("spotify_playlist_id"):
            playlist_id = existing["spotify_playlist_id"]
        else:
            pl = sp._post("me/playlists", payload={
                "name": playlist_name, "public": False,
                "description": "Auto-generated by Spotify Genre Classifier"
            })
            playlist_id = pl["id"]
            db.registry_upsert(genre, playlist_name, playlist_id, 0)

        # Add in chunks of 100 (Spotify limit)
        for i in range(0, len(track_uris), 100):
            sp.playlist_add_items(playlist_id, track_uris[i:i + 100])

        db.registry_upsert(genre, playlist_name, playlist_id, len(tracks))
        results.append({
            "playlist": playlist_name,
            "track_count": len(tracks),
            "spotify_id": playlist_id,
        })
    return results


def _promote_genre(sp: spotipy.Spotify, genre_key: str, user_id: str) -> dict:
    staged_tracks = db.staging_get_tracks_for_genre(genre_key)
    if not staged_tracks:
        return {"error": f"No staged tracks found for genre '{genre_key}'"}

    playlist_name = staged_tracks[0]["suggested_playlist_name"]
    track_uris = [f"spotify:track:{t['track_id']}" for t in staged_tracks]

    pl = sp._post("me/playlists", payload={
        "name": playlist_name, "public": False,
        "description": f"Auto-created by Spotify Genre Classifier ({len(staged_tracks)} tracks)"
    })
    playlist_id = pl["id"]

    for i in range(0, len(track_uris), 100):
        sp.playlist_add_items(playlist_id, track_uris[i:i + 100])

    db.registry_upsert(genre_key, playlist_name, playlist_id, len(staged_tracks))
    db.staging_remove_genre(genre_key)

    # Add to PLAYLIST_NAMES so future classifications can use this genre
    PLAYLIST_NAMES[genre_key] = playlist_name

    return {
        "created": playlist_name,
        "tracks_added": len(staged_tracks),
        "spotify_playlist_id": playlist_id,
    }


def _auto_promote_staged_genres(sp: spotipy.Spotify, user_id: str) -> list[dict]:
    """Automatically promote any staged genres that have crossed STAGING_THRESHOLD."""
    promoted = []
    for genre_info in db.genres_ready_for_playlist():
        result = _promote_genre(sp, genre_info["genre"], user_id)
        promoted.append(result)
    return promoted


def _text(data: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from mcp.server.stdio import stdio_server
    import asyncio

    async def main():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(main())
