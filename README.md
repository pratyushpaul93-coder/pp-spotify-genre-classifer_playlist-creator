# 🎵 Spotify Genre Classifier & Playlist Creator

An MCP (Model Context Protocol) server that automatically classifies your entire Spotify liked songs library into genre-based playlists — using a tiered AI pipeline with SQLite caching so you never pay for the same classification twice.

---

## ✨ What It Does

- Scans all your Spotify liked songs
- Classifies each track into a genre using a 4-tier fallback system
- Creates private Spotify playlists per genre (e.g. 🏠 Tech House Sessions, 💫 Indie Electronic Feels, 🎸 South Asian Indie)
- Caches every classification in SQLite — future runs are instant
- Stages rare genres until you have enough tracks to warrant a new playlist
- Syncs newly liked songs into existing playlists automatically

**Result on a 423-song library:** 422/423 songs classified (99.8%), 27 playlists created across genres spanning electronic, South Asian indie, Arabic fusion, hip-hop, Latin, Turkish, Persian, Brazilian, and more.

---

## 🧠 Classification Architecture

```
Tier 1 — Hardcoded cache       ~300 well-known artists, instant, free
Tier 2 — SQLite cache          All previously classified artists, instant, free
Tier 3 — Spotify Artist API    Genre tags from Spotify's own data
Tier 4 — Last.fm + Claude AI   Last.fm tags → Claude Haiku for final genre decision
```

Results from Tiers 3 & 4 are automatically written to SQLite, so each artist is only ever looked up once.

### Staging System
If Claude suggests a brand-new genre with no existing playlist, the track is **staged**. Once 5+ tracks accumulate in a staged genre, a new playlist is auto-created. You can also manually promote staged genres at any time.

---

## 🛠️ Setup

### Prerequisites
- Python 3.11+
- A [Spotify Developer App](https://developer.spotify.com/dashboard) (free)
- A [Last.fm API key](https://www.last.fm/api/account/create) (free)
- An [Anthropic API key](https://console.anthropic.com/) (Claude Haiku, very cheap)
- [Claude Desktop](https://claude.ai/download) with MCP support

### Installation

```bash
git clone https://github.com/YOUR_USERNAME/pp-spotify-genre-classifier-playlist-creator.git
cd pp-spotify-genre-classifier-playlist-creator

python3 -m venv venv
source venv/bin/activate
pip install spotipy mcp anthropic requests python-dotenv
```

### Environment Variables

Create a `.env` file in the project root:

```env
SPOTIFY_CLIENT_ID=your_spotify_client_id
SPOTIFY_CLIENT_SECRET=your_spotify_client_secret
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8080/callback
ANTHROPIC_API_KEY=your_anthropic_api_key
LASTFM_API_KEY=your_lastfm_api_key
```

### Claude Desktop Config

Add this to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "spotify": {
      "command": "/path/to/your/venv/bin/python3",
      "args": ["/path/to/your/server.py"],
      "env": {
        "SPOTIFY_CLIENT_ID": "your_id",
        "SPOTIFY_CLIENT_SECRET": "your_secret",
        "SPOTIFY_REDIRECT_URI": "http://127.0.0.1:8080/callback",
        "ANTHROPIC_API_KEY": "your_key",
        "LASTFM_API_KEY": "your_key"
      }
    }
  }
}
```

> ⚠️ Use the full path to your venv's `python3`, not the system `python`.  
> ⚠️ Use `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` (not `SPOTIPY_` prefix).

---

## 🎮 MCP Tools

Once configured, use these tools directly in Claude Desktop:

| Tool | Description |
|------|-------------|
| `preview_genre_playlists` | Dry-run — shows what playlists would be created, no Spotify changes |
| `create_genre_playlists` | Creates all genre playlists on Spotify |
| `sync_new_songs` | Classifies recently liked songs and adds to existing playlists |
| `get_classification_stats` | Shows SQLite cache stats and registered playlists |
| `check_staging_area` | Shows staged tracks and genres ready for new playlists |
| `promote_staged_genre` | Manually creates a playlist for a staged genre right now |
| `get_liked_songs` | Fetches raw liked songs from your library |

---

## 📁 Project Structure

```
spotify-mcp-server/
├── server.py                    # MCP server — tool definitions & handlers
├── spotify_genre_classifier.py  # Tiered classification engine + genre normalization
├── database.py                  # SQLite persistence layer
├── .env                         # Your API keys (not committed)
├── spotify_classifier.db        # Auto-created SQLite database
└── README.md
```

---

## 🔧 Key Implementation Notes

### Spotify API (February 2026 Breaking Changes)
Spotify removed `POST /users/{user_id}/playlists`. This project uses the replacement:
```python
sp._post("me/playlists", payload={"name": ..., "public": False, "description": ...})
```

### Genre Normalization
The classifier includes a normalization layer that merges fragmented genre variants into canonical keys (e.g. `trap_rap`, `american_trap`, `underground_hiphop` → `us_hiphop`). This prevents creating 15 near-identical playlists.

### SQLite WAL Mode
The database uses WAL (Write-Ahead Logging) for safe concurrent access. If you ever need to reset, delete all three files: `spotify_classifier.db`, `spotify_classifier.db-shm`, `spotify_classifier.db-wal`.

---

## 📊 Example Output

```
Total tracks: 423
Classified:   422 (99.8%)
Staged:       12 (rare one-off genres)

🎆 Festival Energy        — 56 tracks
🏠 Tech House Sessions    — 50 tracks
💫 Indie Electronic Feels — 45 tracks
💃 Latin Heat             — 26 tracks
🎸 South Asian Indie      — 24 tracks
🌊 Progressive Journey    — 23 tracks
🎵 Arabic Fusion          — 19 tracks
... and 20 more playlists
```

---

## 📄 License

MIT
