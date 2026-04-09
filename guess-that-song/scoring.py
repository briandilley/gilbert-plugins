"""Guess validation and scoring for Guess That Song."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .game import PlayerGuess, SongInfo

logger = logging.getLogger(__name__)


def check_guess_exact(guess: str, title: str, artist: str) -> dict[str, bool]:
    """Fast exact/substring match. Returns {"title": bool, "artist": bool}."""
    g = guess.strip().lower()
    t = title.strip().lower()
    a = artist.strip().lower()

    if len(g) < 3:
        return {"title": False, "artist": False}

    got_title = t == g or t in g or g in t
    got_artist = False
    if got_title:
        # Check if they also got the artist
        got_artist = a in g or any(w in g for w in a.split() if len(w) > 3)

    return {"title": got_title, "artist": got_artist}


async def check_guess_ai(
    ai_svc: Any,
    guess: str,
    title: str,
    artist: str,
) -> dict[str, bool]:
    """Use AI to judge if the guess matches the song title and/or artist.

    Falls back to exact matching if AI is unavailable.
    """
    # Try exact match first
    exact = check_guess_exact(guess, title, artist)
    if exact["title"]:
        return exact

    if ai_svc is None:
        return exact

    try:
        prompt = (
            f"Does this guess match the song? Be STRICT — the guess must clearly "
            f"identify the song, not just contain a number or vague word.\n"
            f'Song: "{title}" by {artist}\n'
            f'Guess: "{guess}"\n'
            f"Reply with EXACTLY one of: 'both' (got title and artist), "
            f"'title' (got the song name), 'artist' (got only the artist), 'no'."
        )
        response, _, _ = await ai_svc.chat(prompt, ai_call="guess_song_validate")
        answer = response.strip().lower()
        if answer.startswith("both"):
            return {"title": True, "artist": True}
        elif answer.startswith("title"):
            return {"title": True, "artist": False}
        elif answer.startswith("artist"):
            return {"title": False, "artist": True}
        return {"title": False, "artist": False}
    except Exception:
        logger.exception("AI guess validation failed, falling back to exact match")
        return exact


async def score_round(
    guesses: list[PlayerGuess],
    song: SongInfo,
    ai_svc: Any = None,
) -> list[dict[str, Any]]:
    """Score all guesses for a round.

    Returns list of dicts with keys:
        player_id, player_name, guess_text, got_title, got_artist, is_fastest, points
    """
    results: list[dict[str, Any]] = []
    first_correct = True

    # Sort by timestamp to determine fastest
    sorted_guesses = sorted(guesses, key=lambda g: g.timestamp)

    for pg in sorted_guesses:
        match = await check_guess_ai(ai_svc, pg.guess_text, song.title, song.artist)

        is_fastest = False
        if match["title"] and first_correct:
            is_fastest = True
            first_correct = False

        pts = 0
        if match["title"]:
            pts += 1
        if match["artist"]:
            pts += 1
        if is_fastest:
            pts += 1

        results.append({
            "player_id": pg.player_id,
            "player_name": pg.player_name,
            "guess_text": pg.guess_text,
            "got_title": match["title"],
            "got_artist": match["artist"],
            "is_fastest": is_fastest,
            "points": pts,
        })

    return results
