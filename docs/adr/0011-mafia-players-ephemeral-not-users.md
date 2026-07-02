# Mafia Players are ephemeral per-game identities, not Users

Mafia is an in-person party game: 4–10+ physically present people, most of them visitors who will
never have a Gilbert account. Gilbert's guest mechanism cannot tell visitors apart — every
unauthenticated LAN visitor shares the single `guest` `UserContext` — so account-less players would
collapse into one identity. We decided the mafia plugin manages its **own** player identity: the
Host (a real account holder) creates a game, and anyone joins it with a short join code plus a
typed name, receiving a per-game player token held in their browser. A Player exists only for the
duration of one game and is never a row in any auth backend.

## Considered options

- **Require accounts for every player** — rejected: a party host will not pre-create nine accounts
  for visiting friends; the feature would go unused.
- **Extend core auth with distinct guest identities** — rejected: a platform-wide identity change
  with security implications far beyond one game plugin, solving a problem only the game has.

## Consequences

- The `/mafia` route and the game's join/act/vote WS RPCs must be reachable at `everyone` level
  (guest connections), so the plugin — not the platform's RBAC — authenticates Players by game-scoped
  token. Secrecy therefore never rides on `UserContext` for Players.
- Host-only powers (start, skip, end day, remove, abort) still key off the real authenticated
  `user_id`, and only a User can create a game.
- Per-player secret delivery cannot use per-user event-bus filtering (all guests are one user);
  the plugin pushes to specific WebSocket connections tracked against player tokens.
- Player identity dies with the game (and with a server restart); leaderboards or cross-game stats
  would need a deliberate future decision, not a quiet extension of these tokens.
