# Guess That Song Plugin

## Summary
Multiplayer music guessing game plugin for Gilbert. Players guess songs from short audio clips played on speakers. AI acts as game master, each player interacts through their own chat session. Uses UI Blocks for interactive forms (setup, guess input, action buttons).

## Details

### Architecture
- **Plugin**: `plugins/guess-that-song/` with `plugin.yaml`, `plugin.py`, `__init__.py`
- **Service**: `GuessGameService` in `service.py` — implements `Service` + `ToolProvider` protocol
- **State**: `game.py` — `GameState`, `GameConfig`, `SongInfo`, `PlayerGuess`, `RoundResult`, `GuessResult`
- **Scoring**: `scoring.py` — exact-match shortcut + AI fallback via `ai_call="guess_song_validate"`

### Service Info
- Name: `guess_game`
- Capabilities: `guess_game`, `ai_tools`
- Requires: `music`, `speaker_control`
- Optional: `event_bus`, `text_to_speech`, `ai_chat`
- AI calls: `guess_song_validate` (for guess checking via cheap model)

### AI Tools
| Tool | Purpose |
|------|---------|
| `guess_song_setup` | Show setup form (UI Block) |
| `guess_song_create` | Create game from setup values |
| `guess_song_join` | Join existing game |
| `guess_song_start` | Start game / next round |
| `guess_song_submit_guess` | Submit guess (hidden until reveal) |
| `guess_song_action` | Host actions: reveal, replay, end |
| `guess_song_status` | Get scores / list games |

### Game Flow
1. Setup form → create game (fetches songs via MusicService.search)
2. Lobby (players join) → host starts
3. Each round: play clip via MusicService.play_track() with random position, schedule stop after clip_seconds
4. Players submit guesses (private per chat session)
5. All guessed → auto-reveal with scores + TTS announcement
6. Repeat → final scores

### UI Blocks Usage
- Setup: form with query, rounds, clip_seconds, volume, speakers
- Lobby: Start Game button
- Playing: text input for guess
- Between rounds: Next Round, Replay, Scores, End Game buttons

### Scoring
- 1pt for title, +1pt for artist, +1pt for fastest correct
- Exact match shortcut for substring matches
- AI validation fallback via `ai_svc.chat(prompt, ai_call="guess_song_validate")`

### Key Dependencies
- `MusicService` — search, play_track (with position_seconds)
- `SpeakerService` — stop (after clip), announce (TTS results)
- `AIService` — guess validation (optional, falls back to exact match)

### Testing
- `tests/unit/test_guess_game.py` — 33 tests covering lifecycle, scoring, UI blocks, edge cases
- Uses virtual package import hack for relative imports in tests

## Related
- `src/gilbert/interfaces/ui.py` — ToolOutput, UIBlock, UIElement
- `src/gilbert/core/services/music.py` — MusicService (search, play_track)
- `src/gilbert/core/services/speaker.py` — SpeakerService (stop, announce)
- `src/gilbert/core/services/radio_dj.py` — similar entertainment service pattern
