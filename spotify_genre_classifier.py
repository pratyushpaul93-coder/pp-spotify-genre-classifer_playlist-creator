"""
spotify_genre_classifier.py - Tiered genre classification engine

Classification order (stops at first confident result):
  Tier 1  Hardcoded artist cache     – instant, no API calls
  Tier 2  SQLite classification cache – instant, persists across restarts
  Tier 3  Spotify Artist Genre API   – free, fast, covers most mainstream artists
  Tier 4  Last.fm tags + Claude API  – catches niche / underground artists
  Tier 5  Stage for new playlist     – if Claude suggests a brand-new genre

Results from Tiers 3-4 are written back to the SQLite cache so subsequent
lookups for the same artist skip straight to Tier 2.
"""

import os
import json
import time
import requests
import anthropic
from dotenv import load_dotenv

import database as db

load_dotenv()

LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ─────────────────────────────────────────────────────────────────────────────
# Static artist → genre mappings (Tier 1)
# These never change unless you edit this file, but now act as a fast pre-seed
# rather than the only source of truth.
# ─────────────────────────────────────────────────────────────────────────────
HARDCODED_ARTISTS: dict[str, str] = {
    # Progressive / Melodic House & Techno
    **{a: "progressive_journey" for a in [
        "lane 8", "adriatique", "hernan cattaneo", "miss monique", "solomun",
        "tale of us", "afterlife", "innellea", "jon gurd", "yotto", "joris voorn",
        "fabricio pecanha", "monolink", "art department", "stephan bodzin",
        "kollektiv turmstrasse", "extrawelt", "way out west", "shingo nakamura",
        "husa & zeyada", "jody wisternoff", "andrew bayer", "dusky",
        "deep forest", "edu imbernon", "silicone soul", "pional",
        "mesmeric", "nicolas jaar", "maceo plex", "recondite",
        "nils hoffmann", "kiasmos", "the revenge", "djrum",
    ]},

    # Tech House / Bass House
    **{a: "tech_house" for a in [
        "john summit", "fisher", "chris lake", "dom dolla", "vintage culture",
        "claptone", "camelphat", "eli brown", "patrick topping", "green velvet",
        "mr. scruff", "detlef", "lee foss", "dirtybird", "claude vonstraten",
        "gorgon city", "max chapman", "skream", "eats everything",
        "christian smith", "richy ahmed",
    ]},

    # Festival / Big Room EDM
    **{a: "festival_energy" for a in [
        "martin garrix", "kshmr", "lost stories", "dimitri vegas & like mike",
        "dimitri vegas", "like mike", "hardwell", "tiësto", "tiesto",
        "w&w", "armin van buuren", "afrojack", "nervo", "don diablo",
        "alesso", "thomas jack", "mesto", "lucas & steve", "dbstf",
        "nicky romero", "sander van doorn", "dannic", "swanky tunes",
        "kaveh", "r3hab", "sick individuals",
    ]},

    # Indie Electronic
    **{a: "indie_electronic" for a in [
        "rüfüs du sol", "rufus du sol", "the blaze", "fred again..", "fred again",
        "bon entendeur", "odesza", "kasbo", "jai wolf", "shallou",
        "polo & pan", "emmit fenn", "nimino", "ford.", "tourist",
        "caribou", "bicep", "caribou", "bonobo", "washed out",
        "tycho", "m83", "james blake", "jon hopkins",
        "khruangbin", "alt-j", "little dragon", "sylvan esso",
    ]},

    # Desi Hip-Hop
    **{a: "desi_hiphop" for a in [
        "seedhe maut", "divine", "raftaar", "naezy", "brodha v",
        "dino james", "ikka", "bohemia", "big deal", "prabh deep",
        "raga", "deep jandu", "karan aujla", "ap dhillon",
        "shubh", "hanumankind", "yashraj", "sez on the beat",
        "encore abj", "rebel7", "mellow d", "slow cheeta",
    ]},

    # South Asian Indie
    **{a: "south_asian_indie" for a in [
        "prateek kuhad", "rianjali", "the yellow diary", "when chai met toast",
        "taimour baig", "maanu", "abdullah siddiqui", "bayaan", "gentle robot",
        "arooj aftab", "quratulain balouch", "ali sethi", "shamoon ismail",
        "talha anjum", "hasan raheem", "asim azhar", "zara larsson",
        "arjun kanungo", "ankur tewari",
    ]},

    # Desi Fusion
    **{a: "desi_fusion" for a in [
        "nucleya", "ritviz", "jai dhir", "rusha & blizza", "lost stories",
        "zaeden", "akull", "jasleen royal", "Vishal-Shekhar".lower(),
        "a.r. rahman", "shankar-ehsaan-loy",
    ]},

    # Latin
    **{a: "latin_heat" for a in [
        "bad bunny", "j balvin", "ozuna", "maluma", "daddy yankee",
        "rauw alejandro", "anuel aa", "karol g", "milo j", "maye",
        "tainy", "jhay cortez", "myke towers", "sech",
        "feid", "mora", "jhayco",
    ]},

    # Arabic Fusion
    **{a: "arabic_fusion" for a in [
        "elyanna", "dystinct", "amr diab", "saad lamjarred",
        "mahmoud el esseily", "ali termos", "nassif zeytoun",
        "cairokee", "mashrou' leila", "tamer hosny",
    ]},

    # Turkish Beats
    **{a: "turkish_beats" for a in [
        "sura iskenderli", "teya dora", "murda", "yüzyüzeyken konuşuruz",
        "manga", "duman", "mor ve ötesi", "athena",
    ]},

    # Persian Vibes
    **{a: "persian_vibes" for a in [
        "asadi", "mohsen ebrahimzadeh", "erfan", "hamed nikpay",
        "darno", "nasiri", "xye", "sham.m.an",
        "shadmehr aghili", "googoosh", "dariush",
    ]},

    # Brazilian Bass
    **{a: "brazilian_bass" for a in [
        "alok", "liu", "kvsh", "sevenn", "mandragora",
        "vintage culture", "sabo", "illusionize",
        "dubdogz", "lukas graham", "mochakk",
    ]},

    # Afro House
    **{a: "afro_house" for a in [
        "black coffee", "shimza", "da capo", "culoe de song",
        "enoo napa", "themba", "lars behrenroth", "ian pooley",
        "osunlade", "louie vega", "atjazz", "naarly",
    ]},
}


# How Spotify genre tag substrings map to our internal genre keys
SPOTIFY_GENRE_MAP: list[tuple[str, str]] = [
    ("progressive house", "progressive_journey"),
    ("melodic house", "progressive_journey"),
    ("organic house", "progressive_journey"),
    ("melodic techno", "progressive_journey"),
    ("tech house", "tech_house"),
    ("bass house", "tech_house"),
    ("big room", "festival_energy"),
    ("edm", "festival_energy"),
    ("electro house", "festival_energy"),
    ("future house", "festival_energy"),
    ("desi", "desi_hiphop"),
    ("desi pop", "south_asian_indie"),
    ("indian hip hop", "desi_hiphop"),
    ("punjabi", "desi_hiphop"),
    ("pakistani indie", "south_asian_indie"),
    ("bollywood", "south_asian_indie"),
    ("indie pop", "indie_electronic"),
    ("chillwave", "indie_electronic"),
    ("indietronica", "indie_electronic"),
    ("latin", "latin_heat"),
    ("reggaeton", "latin_heat"),
    ("arabic", "arabic_fusion"),
    ("khaleeji", "arabic_fusion"),
    ("turkish", "turkish_beats"),
    ("persian", "persian_vibes"),
    ("iran", "persian_vibes"),
    ("brazilian", "brazilian_bass"),
    ("funk carioca", "brazilian_bass"),
    ("afro house", "afro_house"),
    ("afrobeats", "afro_house"),
]

# ─────────────────────────────────────────────────────────────────────────────
# Genre key normalisation
# Maps any variant Claude might invent → the single canonical key we use.
# Applied immediately after Claude returns a genre key, before anything is
# written to cache or counted toward playlist thresholds.
# ─────────────────────────────────────────────────────────────────────────────
GENRE_NORMALISATION: dict[str, str] = {
    # Hip-hop variants → one canonical key per region
    "us_hip_hop":           "us_hiphop",
    "american_hiphop":      "us_hiphop",
    "mainstream_hiphop":    "us_hiphop",
    "underground_hiphop":   "us_hiphop",
    "west_coast_hiphop":    "us_hiphop",
    "east_coast_hiphop":    "us_hiphop",
    "trap_rap":             "us_hiphop",
    "american_trap":        "us_hiphop",
    "alternative_hiphop":   "us_hiphop",
    "experimental_hiphop":  "us_hiphop",
    # Dutch hip-hop
    "dutch_hip_hop":        "dutch_hiphop",
    # R&B variants
    "rnb_pop":              "contemporary_rnb",
    "rnb_hip_hop":          "contemporary_rnb",
    "soul_hiphop":          "contemporary_rnb",
    "r&b_hip_hop":          "contemporary_rnb",
    "r&b_pop":              "contemporary_rnb",
    # Afrobeats → afro_house
    "afrobeats":            "afro_house",
    # Film/TV/cinematic → one bucket
    "cinematic_scores":     "film_soundtrack",
    "soundtrack_tv":        "film_soundtrack",
    "italian_soundtrack":   "film_soundtrack",
    # Italian music catch-all
    "italian_classics":     "italian_lounge",
    "italian_house":        "italian_lounge",
    # Electronic sub-genres that fit existing playlists
    "minimal_techno":       "progressive_journey",
    "dnb_jump_up":          "festival_energy",
    "drum_and_bass":        "festival_energy",
    "hardstyle_energy":     "festival_energy",
    "hardcore_electronic":  "festival_energy",
    "eastern_european_edm": "festival_energy",
    # Pop catch-alls
    "pop_anthems":          "indie_pop",
    "pop_rock_anthems":     "indie_pop",
    "indie_covers":         "indie_pop",
    # World/lounge one-offs
    "lounge_jazz":          "world_lounge",
    "folk_maritime":        "world_lounge",
    "slovenian_acoustic":   "world_lounge",
    "portuguese_indie":     "world_lounge",
    "european_indie":       "world_lounge",
    "nordic_pop":           "world_lounge",
    "contemporary_classical": "world_lounge",
    "southeast_asian_beats": "world_lounge",
    "russian_rap":          "world_lounge",
    # Hebrew/Israeli
    "israeli_rock":         "hebrew_pop",
    "jewish_folk":          "hebrew_pop",
    # Misc
    "german_rnb":           "german_rap",
    "german_comedy":        "german_rap",
    "uk_rap":               "us_hiphop",
    "caribbean_vibes":      "latin_heat",
    "sertanejo":            "brazilian_bass",
}


def normalise_genre(genre_key: str) -> str:
    """Return the canonical genre key, collapsing known variants."""
    return GENRE_NORMALISATION.get(genre_key, genre_key)


# Human-readable display names for each genre key
PLAYLIST_NAMES: dict[str, str] = {
    # Core electronic
    "progressive_journey":  "🌊 Progressive Journey",
    "tech_house":           "🏠 Tech House Sessions",
    "festival_energy":      "🎆 Festival Energy",
    "indie_electronic":     "💫 Indie Electronic Feels",
    # South Asian
    "desi_hiphop":          "🎤 Desi Hip-Hop",
    "south_asian_indie":    "🎸 South Asian Indie",
    "desi_fusion":          "🔥 Desi Fusion",
    # Global / regional
    "latin_heat":           "💃 Latin Heat",
    "arabic_fusion":        "🎵 Arabic Fusion",
    "turkish_beats":        "🇹🇷 Turkish Beats",
    "persian_vibes":        "🇮🇷 Persian Vibes",
    "brazilian_bass":       "🇧🇷 Brazilian Bass",
    "afro_house":           "🌍 Afro House Vibes",
    # New genres discovered by Claude
    "contemporary_rnb":     "🎤 Contemporary R&B",
    "french_pop":           "🇫🇷 French Pop",
    "french_hiphop":        "🇫🇷 French Hip-Hop",
    "german_rap":           "🇩🇪 German Rap",
    "dutch_hiphop":         "🇳🇱 Dutch Hip-Hop",
    "us_hiphop":            "🎤 US Hip-Hop",
    "k_pop":                "🇰🇷 K-Pop",
    "japanese_rock":        "🇯🇵 Japanese Rock",
    "hebrew_pop":           "🇮🇱 Hebrew Pop",
    "eastern_european_pop": "🌍 Eastern European Pop",
    "indie_pop":            "🎵 Indie Pop",
    "world_lounge":         "🌍 World Lounge",
    "film_soundtrack":      "🎬 Film Soundtracks",
    "italian_lounge":       "🇮🇹 Italian Lounge",
    "classic_rock":         "🎸 Classic Rock",
    "uncategorized":        "❓ To Categorize",
}


class SpotifyGenreClassifier:
    """
    Tiered classifier. Pass a Spotipy client at construction time to enable
    Tier 3 (Spotify Artist Genre API). Last.fm + Claude run without extra setup
    as long as LASTFM_API_KEY and ANTHROPIC_API_KEY are in .env.
    """

    def __init__(self, spotify_client=None):
        self.sp = spotify_client
        self._anthropic = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

    # ─────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────

    def classify_track(
        self,
        track_id: str,
        track_name: str,
        artist_name: str,
        artist_id: str = None,
    ) -> dict:
        """
        Classify a single track.

        Returns:
            {
                'primary_genre': str,           # internal genre key
                'playlist_name': str,           # human-readable display name
                'confidence': 'high'|'medium'|'low',
                'method': str,                  # which tier classified it
                'is_new_genre': bool,           # True if this genre has no existing playlist
                'staged': bool,                 # True if added to staging area
            }
        """
        # Work with a normalised, comma-split list of artist names
        artists = [a.strip().lower() for a in artist_name.split(",")]
        primary_artist = artists[0]

        # ── Tier 1: hardcoded dict ──────────────────────────────────────
        for artist in artists:
            if artist in HARDCODED_ARTISTS:
                genre = HARDCODED_ARTISTS[artist]
                return self._result(genre, "high", "hardcoded", False, False)

        # ── Tier 2: SQLite cache ────────────────────────────────────────
        cached = db.cache_get(primary_artist)
        if cached:
            genre = cached["genre"]
            is_new = self._is_new_genre(genre)
            return self._result(genre, "high", f"cache:{cached['source']}", is_new, False)

        # ── Tier 3: Spotify Artist Genre API ───────────────────────────
        if self.sp:
            result = self._classify_via_spotify(primary_artist, artist_id)
            if result:
                db.cache_set(primary_artist, result["genre"], "spotify",
                             spotify_tags=result.get("raw_tags"))
                is_new = self._is_new_genre(result["genre"])
                return self._result(result["genre"], result["confidence"], "spotify", is_new, False)

        # ── Tier 4: Last.fm tags → Claude API ──────────────────────────
        lastfm_tags = self._fetch_lastfm_tags(primary_artist)
        claude_result = self._classify_via_claude(
            track_name, artist_name, lastfm_tags
        )

        if claude_result:
            genre = claude_result["genre"]
            is_new = claude_result.get("is_new_genre", False)
            db.cache_set(
                primary_artist, genre, "lastfm_claude",
                lastfm_tags=lastfm_tags,
                claude_reasoning=claude_result.get("reasoning"),
            )

            if is_new:
                # Stage the track — don't create a playlist yet
                db.staging_add(
                    track_id=track_id,
                    track_name=track_name,
                    artist_name=artist_name,
                    suggested_genre=genre,
                    suggested_playlist_name=claude_result.get("playlist_name", genre),
                    confidence=claude_result.get("confidence_score", 0.6),
                    source="lastfm_claude",
                )
                return self._result(genre, "medium", "claude_new_genre", True, True,
                                    playlist_name=claude_result.get("playlist_name"))

            return self._result(genre, "medium", "lastfm_claude", False, False)

        # ── Tier 5: Give up — uncategorized ────────────────────────────
        return self._result("uncategorized", "low", "none", False, False)

    def get_playlist_name(self, genre_key: str) -> str:
        """Return the display name for a genre key."""
        return PLAYLIST_NAMES.get(genre_key, f"❓ {genre_key.replace('_', ' ').title()}")

    def known_genres(self) -> list[str]:
        """Return all genre keys that have a named playlist."""
        return list(PLAYLIST_NAMES.keys())

    # ─────────────────────────────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────────────────────────────

    def _classify_via_spotify(self, artist_name: str, artist_id: str = None) -> dict | None:
        """Look up an artist on Spotify and map their genre tags to our keys."""
        try:
            if artist_id:
                info = self.sp.artist(artist_id)
            else:
                results = self.sp.search(q=f"artist:{artist_name}", type="artist", limit=1)
                items = results.get("artists", {}).get("items", [])
                if not items:
                    return None
                info = items[0]

            raw_tags = info.get("genres", [])
            if not raw_tags:
                return None

            # Walk through priority-ordered map
            for tag in raw_tags:
                tag_lower = tag.lower()
                for fragment, genre_key in SPOTIFY_GENRE_MAP:
                    if fragment in tag_lower:
                        return {
                            "genre": normalise_genre(genre_key),
                            "confidence": "high",
                            "raw_tags": raw_tags,
                        }

            # No direct match — still return raw tags so Claude has context
            return {"genre": None, "confidence": "low", "raw_tags": raw_tags}

        except Exception:
            return None

    def _fetch_lastfm_tags(self, artist_name: str) -> list[str]:
        """Fetch top tags for an artist from Last.fm. Returns [] on any error."""
        if not LASTFM_API_KEY:
            return []
        try:
            resp = requests.get(
                "https://ws.audioscrobbler.com/2.0/",
                params={
                    "method": "artist.getTopTags",
                    "artist": artist_name,
                    "api_key": LASTFM_API_KEY,
                    "format": "json",
                    "limit": 10,
                },
                timeout=5,
            )
            data = resp.json()
            tags = data.get("toptags", {}).get("tag", [])
            return [t["name"] for t in tags if int(t.get("count", 0)) >= 20]
        except Exception:
            return []

    def _classify_via_claude(
        self,
        track_name: str,
        artist_name: str,
        lastfm_tags: list[str],
    ) -> dict | None:
        """
        Ask Claude to classify the track.
        Returns a dict:  {genre, playlist_name, is_new_genre, confidence_score, reasoning}
        or None on failure.
        """
        if not self._anthropic:
            return None

        existing_playlists = json.dumps(
            {k: v for k, v in PLAYLIST_NAMES.items() if k != "uncategorized"},
            indent=2,
        )
        lastfm_str = ", ".join(lastfm_tags) if lastfm_tags else "none available"

        prompt = f"""You are a music genre classifier for a personal Spotify library.

Track: "{track_name}"
Artist: "{artist_name}"
Last.fm community tags: {lastfm_str}

Existing playlist categories (JSON key → display name):
{existing_playlists}

Task:
1. Decide which playlist this track belongs to, OR decide it needs a brand-new playlist.
2. If it fits an existing playlist, use that key exactly.
3. If it needs a new playlist, invent a short snake_case key (e.g. "french_pop") and a
   display name with an emoji (e.g. "🇫🇷 French Pop").
4. Be decisive — prefer an existing category over a new one unless the genre is clearly
   distinct (e.g. French pop, K-pop, Afrobeats, Jazz, Classical).

Respond ONLY with valid JSON — no markdown fences, no extra text:
{{
  "genre": "<genre_key>",
  "playlist_name": "<display name>",
  "is_new_genre": <true|false>,
  "confidence_score": <0.0-1.0>,
  "reasoning": "<one sentence>"
}}"""

        try:
            message = self._anthropic.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            result = json.loads(raw)
            # Normalise the genre key to eliminate fragmentation variants
            result["genre"] = normalise_genre(result["genre"])
            # Update is_new_genre after normalisation
            result["is_new_genre"] = result["genre"] not in PLAYLIST_NAMES
            return result
        except Exception:
            return None

    def _is_new_genre(self, genre_key: str) -> bool:
        """True if this genre key has no registered playlist yet."""
        return genre_key not in PLAYLIST_NAMES and db.registry_get(genre_key) is None

    @staticmethod
    def _result(
        genre: str,
        confidence: str,
        method: str,
        is_new_genre: bool,
        staged: bool,
        playlist_name: str = None,
    ) -> dict:
        name = playlist_name or PLAYLIST_NAMES.get(
            genre, f"❓ {genre.replace('_', ' ').title()}"
        )
        return {
            "primary_genre": genre,
            "playlist_name": name,
            "confidence": confidence,
            "method": method,
            "is_new_genre": is_new_genre,
            "staged": staged,
        }
