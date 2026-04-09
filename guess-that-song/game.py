"""Game state models for Guess That Song."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class GameConfig:
    """Settings for a single game instance."""

    query: str = "popular hits"
    num_rounds: int = 5
    clip_seconds: int = 3
    speakers: list[str] = field(default_factory=list)
    volume: int | None = None


@dataclass
class SongInfo:
    """Pre-fetched song metadata for a round."""

    track_id: str
    title: str
    artist: str
    uri: str
    duration_seconds: float
    album_art_url: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "track_id": self.track_id,
            "title": self.title,
            "artist": self.artist,
            "uri": self.uri,
            "duration_seconds": self.duration_seconds,
            "album_art_url": self.album_art_url,
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> SongInfo:
        return cls(
            track_id=str(d["track_id"]),
            title=str(d["title"]),
            artist=str(d["artist"]),
            uri=str(d["uri"]),
            duration_seconds=float(d.get("duration_seconds", 0)),  # type: ignore[arg-type]
            album_art_url=str(d.get("album_art_url", "")),
        )


@dataclass
class PlayerGuess:
    """A single player's guess for a round."""

    player_id: str
    player_name: str
    guess_text: str
    timestamp: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, object]:
        return {
            "player_id": self.player_id,
            "player_name": self.player_name,
            "guess_text": self.guess_text,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> PlayerGuess:
        return cls(
            player_id=str(d["player_id"]),
            player_name=str(d["player_name"]),
            guess_text=str(d["guess_text"]),
            timestamp=float(d.get("timestamp", 0)),  # type: ignore[arg-type]
        )


@dataclass
class GuessResult:
    """Scored result for a single guess."""

    player_id: str
    player_name: str
    guess_text: str
    got_title: bool = False
    got_artist: bool = False
    is_fastest: bool = False

    @property
    def points(self) -> int:
        pts = 0
        if self.got_title:
            pts += 1
        if self.got_artist:
            pts += 1
        if self.is_fastest:
            pts += 1
        return pts


@dataclass
class RoundResult:
    """Results for a completed round."""

    round_number: int
    song: SongInfo
    results: list[GuessResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "round_number": self.round_number,
            "song": self.song.to_dict(),
        }


@dataclass
class GameState:
    """Full mutable state for an active game."""

    game_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    host_id: str = ""
    host_name: str = ""
    config: GameConfig = field(default_factory=GameConfig)
    status: str = "lobby"  # lobby | playing | between_rounds | ended
    players: dict[str, str] = field(default_factory=dict)  # user_id -> display_name
    scores: dict[str, int] = field(default_factory=dict)  # user_id -> total score
    songs: list[SongInfo] = field(default_factory=list)
    current_round: int = 0
    guesses: dict[str, PlayerGuess] = field(default_factory=dict)  # user_id -> guess
    round_results: list[RoundResult] = field(default_factory=list)

    @property
    def current_song(self) -> SongInfo | None:
        """The song for the current round (1-indexed)."""
        idx = self.current_round - 1
        if 0 <= idx < len(self.songs):
            return self.songs[idx]
        return None

    @property
    def rounds_remaining(self) -> int:
        return self.config.num_rounds - self.current_round

    def add_player(self, user_id: str, display_name: str) -> None:
        self.players[user_id] = display_name
        self.scores.setdefault(user_id, 0)

    def remove_player(self, user_id: str) -> None:
        self.players.pop(user_id, None)
        self.scores.pop(user_id, None)
        self.guesses.pop(user_id, None)

    def all_guessed(self) -> bool:
        """True if every active player has submitted a guess."""
        return len(self.guesses) >= len(self.players)

    def to_dict(self) -> dict[str, object]:
        """Serialize full game state for storage."""
        return {
            "game_id": self.game_id,
            "host_id": self.host_id,
            "host_name": self.host_name,
            "config": {
                "query": self.config.query,
                "num_rounds": self.config.num_rounds,
                "clip_seconds": self.config.clip_seconds,
                "speakers": self.config.speakers,
                "volume": self.config.volume,
            },
            "status": self.status,
            "players": dict(self.players),
            "scores": dict(self.scores),
            "songs": [s.to_dict() for s in self.songs],
            "current_round": self.current_round,
            "guesses": {k: v.to_dict() for k, v in self.guesses.items()},
            "round_results": [r.to_dict() for r in self.round_results],
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> GameState:
        """Deserialize game state from storage."""
        config_d = d.get("config", {})
        assert isinstance(config_d, dict)
        config = GameConfig(
            query=str(config_d.get("query", "popular hits")),
            num_rounds=int(config_d.get("num_rounds", 5)),  # type: ignore[arg-type]
            clip_seconds=int(config_d.get("clip_seconds", 3)),  # type: ignore[arg-type]
            speakers=list(config_d.get("speakers", [])),  # type: ignore[arg-type]
            volume=int(config_d["volume"]) if config_d.get("volume") is not None else None,  # type: ignore[arg-type]
        )
        songs_raw = d.get("songs", [])
        assert isinstance(songs_raw, list)
        guesses_raw = d.get("guesses", {})
        assert isinstance(guesses_raw, dict)
        results_raw = d.get("round_results", [])
        assert isinstance(results_raw, list)
        players = d.get("players", {})
        assert isinstance(players, dict)
        scores = d.get("scores", {})
        assert isinstance(scores, dict)
        return cls(
            game_id=str(d.get("game_id", "")),
            host_id=str(d.get("host_id", "")),
            host_name=str(d.get("host_name", "")),
            config=config,
            status=str(d.get("status", "lobby")),
            players={str(k): str(v) for k, v in players.items()},
            scores={str(k): int(v) for k, v in scores.items()},  # type: ignore[arg-type]
            songs=[SongInfo.from_dict(s) for s in songs_raw],  # type: ignore[arg-type]
            current_round=int(d.get("current_round", 0)),  # type: ignore[arg-type]
            guesses={str(k): PlayerGuess.from_dict(v) for k, v in guesses_raw.items()},  # type: ignore[arg-type]
            round_results=[
                RoundResult(
                    round_number=int(r.get("round_number", 0)),  # type: ignore[union-attr]
                    song=SongInfo.from_dict(r["song"]),  # type: ignore[arg-type, index]
                )
                for r in results_raw
            ],
        )

    def to_ai_summary(self) -> dict[str, object]:
        """AI-visible summary — excludes future song answers."""
        waiting_on = [
            name for uid, name in self.players.items()
            if uid not in self.guesses
        ] if self.status == "playing" else []

        past_songs = [
            {"round": r.round_number, "title": r.song.title, "artist": r.song.artist}
            for r in self.round_results
        ]

        summary: dict[str, object] = {
            "game_id": self.game_id,
            "status": self.status,
            "host": self.host_name,
            "theme": self.config.query,
            "current_round": self.current_round,
            "total_rounds": self.config.num_rounds,
            "rounds_remaining": self.rounds_remaining,
            "players": dict(self.players),
            "scores": dict(self.scores),
        }
        if waiting_on:
            summary["waiting_on_guesses_from"] = waiting_on
        if past_songs:
            summary["past_rounds"] = past_songs
        return summary

    def format_scores(self) -> str:
        """Format the current scoreboard as text."""
        if not self.scores:
            return "No scores yet."
        sorted_scores = sorted(self.scores.items(), key=lambda x: x[1], reverse=True)
        lines = ["**Standings:**"]
        for i, (uid, score) in enumerate(sorted_scores, 1):
            name = self.players.get(uid, uid)
            lines.append(f"{i}. {name}: {score} pt{'s' if score != 1 else ''}")
        return "\n".join(lines)

    def format_final_scores(self) -> str:
        """Format end-of-game results."""
        sorted_scores = sorted(self.scores.items(), key=lambda x: x[1], reverse=True)
        lines = ["**Guess That Song — Game Over!**\n"]
        for i, (uid, score) in enumerate(sorted_scores, 1):
            name = self.players.get(uid, uid)
            lines.append(f"{i}. {name}: {score} pt{'s' if score != 1 else ''}")

        if sorted_scores:
            top = sorted_scores[0][1]
            winners = [self.players.get(uid, uid) for uid, s in sorted_scores if s == top]
            if len(winners) > 1:
                lines.append(f"\nIt's a tie! {' and '.join(winners)} share the glory!")
            else:
                lines.append(f"\n**{winners[0]} wins!**")
        lines.append("\nThanks for playing!")
        return "\n".join(lines)
