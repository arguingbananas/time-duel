# Three Minute Duel — Application Specification

**A local, two-phone chess-clock dueling game with a jigsaw-puzzle turn mechanic.**

---

## 1. Overview

Three Minute Duel is a two-player game played on two separate iPhones (or any phones/browsers), connected to a single small web server run on a local machine (e.g. an Ubuntu box) over the LAN. Each player has their own countdown clock, starting at a fixed 3 minutes. Players alternate turns; on your turn, you must correctly solve a small visual puzzle to pass the clock to your opponent. The loser is whoever's clock reaches zero first.

The application is intentionally self-contained and dependency-free: it's a single Python file using only the standard library, requiring no installation step (`pip install`) and no internet access to run. Puzzle graphics are generated as inline SVG in the browser rather than fetched as image assets.

### 1.1 Design goals

- **Zero setup friction.** No accounts, no manual role selection, no time configuration.
- **No external dependencies.** Runs anywhere Python 3 is available; nothing to install.
- **LAN-only, self-hosted.** No cloud service, no data leaves the local network.
- **Deterministic, server-authoritative timing.** The clock and win condition are judged by the server's wall clock, not by client taps — closing the "stall and tap right before losing" loophole.

---

## 2. Game Rules

| Rule | Behavior |
|---|---|
| Starting time | Fixed at **2 minutes** per player (`GAME_MINUTES = 2`), not configurable through the UI. |
| Player assignment | Automatic. The first phone to load the page becomes **Player 1**; the second becomes **Player 2**. No manual role picker. |
| Turn order | Player 1 always moves first once the game is started. |
| Passing your turn | You must answer a visual puzzle correctly (see §3). A correct answer immediately passes the active turn to your opponent. |
| Wrong answer penalty | A wrong answer costs the active player **2 seconds** (`WRONG_PENALTY_MS = 2000`) off their own remaining time. The turn does **not** pass — the same player is shown a new puzzle and must try again. |
| Losing condition | A player's remaining time reaching zero (whether from normal countdown or a penalty) ends the game immediately. That player loses; the other wins. |
| Time enforcement | Judged server-side against the server's own clock at every request (see §5.3) — a tap that arrives after time has already expired has no effect. There is no way to "sneak" a late answer through. |
| Rematch | "Play again" resets the whole game: both role claims are released and a new player-assignment race begins. |

---

## 3. Turn-Passing Puzzle

On your turn, instead of a plain tap, you are shown a **geometric shape split into four congruent (or near-congruent) parts, with one part missing**, drawn as an outlined "gap" in the shape. Below it are **five candidate piece tiles**; you tap the one whose shape and orientation matches the gap.

### 3.1 Shape types

Eight shape types are in rotation, each chosen because its four pieces are visually distinguishable by orientation/position alone (not just by color), which keeps the puzzle legible without making it trivial:

| Shape | Division method | Piece shape |
|---|---|---|
| `square` | Corner-to-corner diagonals | 4 triangles (top / right / bottom / left) |
| `rectangle` | Corner-to-corner diagonals | 4 asymmetric triangles (wider top/bottom, narrower left/right) |
| `diamond` | Vertex-to-vertex diagonals (rotated square) | 4 triangles, same construction as square but rotated 45° |
| `triangle` | Connect the three edge midpoints | 3 "corner" triangles (upright) + 1 "center" triangle (inverted) — the **center piece is always the one that's missing**, since it's the only piece visually distinct from the other three |
| `hexagon` | Two diameters through center (flat-top orientation) | 4 kite-shaped quadrilaterals |
| `octagon` | Two diameters through center (flat-top orientation) | 4 pentagon-shaped quadrants |
| `circle` | Two diameters through center | 4 quarter-circle pie wedges, each bulging toward a different corner |
| `cross` (plus sign) | Two center lines through the arms | 4 L-shaped/hexagonal arm pieces |

A new shape, color, and missing-piece position are randomly chosen for **every** puzzle (i.e. after every correct or incorrect answer).

### 3.2 Answer choices

Each puzzle presents **five** tiles: one correct piece and four decoys.

- **Decoy sourcing:** the app first fills decoy slots with the *other* pieces of the *same* shape (up to 3 available, since each shape has 4 total pieces). Since 5 choices require 4 decoys, at least one decoy is necessarily borrowed from a **different shape type** at a random piece index. A cross-shape decoy is obviously mismatched in silhouette, which is fine — it's simply an extra wrong option.
- **Color:** every tile in a given puzzle (the gap, the correct piece, and all decoys) shares the same single randomly chosen color, so the puzzle tests shape/orientation recognition, not color matching.
- **Answer validation:** the correct choice's index among the five is tracked server-side only (`correctIndex`) and is **never sent to the client** — the client only receives shape/piece descriptors for what to render, and submits back which slot (0–4) it picked.

### 3.3 Rendering

Shapes are drawn as inline SVG using hardcoded path data per shape type (defined once in the client-side JavaScript, in a `SHAPE_DATA` table). No image files, no network image fetches. The "gap" piece is rendered with a translucent fill and dashed stroke so its exact outline is visible for comparison against the candidate tiles.

### 3.4 Rotation mechanic

After each puzzle is generated, the shape rotates 90° every 2 seconds (`ROTATION_INTERVAL_MS = 2000`). The gap position shifts with each rotation, so the correct answer changes dynamically — the client must track which piece index currently aligns with the gap, not just the initial `missingIndex`.

- The triangle (`"triangle"`) is the only **non-rotating** shape (`NON_ROTATING_SHAPES = {"triangle"}`), because its four pieces do not map onto each other under 90° turns (the inverted centre piece stays put).
- The server calculates the current gap position server-side via `get_current_missing_index()`, which adds the elapsed rotation count to the original `missingIndex` modulo 4.
- Answer validation always uses the **current** missing index (after rotation), not the static `missingIndex` stored in the puzzle object.
- The client renders the shape with a CSS `rotate()` transform based on the server timestamp (`serverNow`) and the puzzle's `rotationStartedAt`, so the visual rotation stays in sync with the server's calculation.

---

## 4. Architecture

### 4.1 Summary

A single Python file (`duel_clock_server.py`) that:

1. Runs an HTTP server (`http.server.ThreadingHTTPServer`) on port 8765 by default, bound to `0.0.0.0` (all network interfaces).
2. Serves one embedded HTML/CSS/JS page at `/` — no separate static files.
3. Exposes a small JSON API for game-state mutation (`/join`, `/start`, `/answer`, `/new`) and polling (`/state`).
4. Holds all game state in a single in-memory Python dict (`STATE`), guarded by a `threading.Lock`, supporting exactly **one game at a time**.

### 4.2 Why polling instead of WebSockets

The client polls `GET /state` every 250ms and locally interpolates the countdown between polls using `requestAnimationFrame`, rather than opening a persistent WebSocket connection. This was a deliberate simplicity trade-off: it requires zero extra dependencies (no `websockets` package), works with nothing but `http.server` from the standard library, and is more than responsive enough for a turn-based game over a LAN.

### 4.3 Clock accuracy technique

To avoid needing synchronized clocks between the server and each phone:

- The server includes its own current timestamp (`serverNow`) in every response.
- The client computes `localOffsetMs` (elapsed real time since the last poll landed) and adds it to `serverNow` before computing remaining time, rather than trusting the phone's own clock in absolute terms.
- This gives smooth, accurate-looking per-frame countdown updates on the client while the server remains the sole source of truth for win/loss determination.

---

## 5. Server-Side Data Model

### 5.1 Global state shape (`STATE` dict)

| Field | Type | Description |
|---|---|---|
| `status` | enum string | One of `claiming`, `ready`, `active`, `gameover`. |
| `totalMs` | int | Starting time per player, in milliseconds (`3 * 60 * 1000`). |
| `p1RemainingMs` / `p2RemainingMs` | int | Remaining time for each player, in ms. |
| `activePlayer` | `"p1"` \| `"p2"` \| `null` | Whose clock is currently running. |
| `turnStartedAt` | int (epoch ms) \| `null` | Server timestamp when the current turn began. |
| `p1Taken` / `p2Taken` | bool | Whether each role has been claimed by a phone. |
| `winner` | `"p1"` \| `"p2"` \| `null` | Set once the game ends. |
| `puzzle` | object \| `null` | The current puzzle (see §5.2). Regenerated on every turn pass and on every wrong answer. |

### 5.2 Puzzle object shape

Internal (server-only) fields are marked; everything else is included in the public `/state` response.

| Field | Type | Visibility | Description |
|---|---|---|---|
| `id` | int | public | Monotonically increasing puzzle identifier. Used to reject stale answer submissions. |
| `shapeType` | string | public | One of the 8 shape type names. |
| `color` | string (hex) | public | Shared fill color for this puzzle's gap and choice tiles. |
| `missingIndex` | int (0–3) | public | The original missing piece index (before rotation). |
| `choices` | array of `{shapeType, pieceIndex}` | public | The 5 candidate tiles, in display order. |
| `correctIndex` | int (0–4) | **server-only** | Which slot in `choices` is correct. Stripped out before the state is sent to clients. |
| `rotates` | bool | public | `false` for shapes in `NON_ROTATING_SHAPES` (currently only `"triangle"`); `true` otherwise. |
| `rotationIndex` | int | public | The rotation count when this puzzle was created (always `0` for newly generated puzzles). |
| `rotationStartedAt` | int (epoch ms) | public | Server timestamp when the first rotation cycle began. |
| `rotationIntervalMs` | int | public | Milliseconds between each 90° rotation (always `4000`). |

### 5.3 Server-authoritative time enforcement

On **every** state-touching request (`GET /state`, and all `POST` endpoints), the server first calls `finalize_if_expired()`, which:

1. Checks whether the active player's clock, computed from `turnStartedAt` and the server's own current time, has reached zero.
2. If so, immediately transitions `status` to `gameover` and sets `winner`, **before** processing whatever the incoming request was asking to do.

This means a wrong-timed tap arriving after time has technically expired is evaluated against an already-finalized `gameover` state and has no effect — there's no way to "beat the clock" with a late request.

---

## 6. HTTP API

All endpoints return JSON. `POST` endpoints accept a JSON body (may be empty `{}`). The server also returns `serverNow` (current server epoch ms) in every response for client-side clock interpolation.

| Method & Path | Body | Purpose | Response highlights |
|---|---|---|---|
| `GET /` | — | Serves the single-page HTML/CSS/JS app. | HTML |
| `GET /state` | — | Polled every 250ms by each client. Returns the current game state (with `puzzle.correctIndex` stripped). | Full state snapshot |
| `POST /join` | — | Auto-assigns the caller as `p1` (if free) or `p2` (if `p1` taken). Returns `409 {"error": "full"}` if both roles are already taken. | State snapshot + `yourRole` |
| `POST /start` | — | Transitions `ready` → `active`; sets `activePlayer = "p1"`, starts the clock, generates the first puzzle. Rejected (`409`) unless `status == "ready"`. | State snapshot |
| `POST /answer` | `{ "role": "p1"\|"p2", "choice": 0-4, "puzzleId": int }` | Submits an answer for the caller's puzzle. Validates `role == activePlayer`; rejects stale answers (mismatched `puzzleId`); validates against the **current** missing index (accounting for rotation); deducts elapsed time (and, if wrong, the 2000ms penalty); passes or retains the turn accordingly; generates a new puzzle. Rejected (`409`) if it isn't that role's turn. | State snapshot |
| `POST /new` | — | Resets the entire game to a fresh `claiming` state (both roles released, clocks reset). Used for "Play again." | State snapshot |

### 6.1 Example `/state` response (mid-game)

```json
{
  "status": "active",
  "totalMs": 120000,
  "p1RemainingMs": 92300,
  "p2RemainingMs": 108900,
  "activePlayer": "p1",
  "turnStartedAt": 1732000000000,
  "p1Taken": true,
  "p2Taken": true,
  "winner": null,
  "puzzle": {
    "id": 1,
    "shapeType": "hexagon",
    "color": "#3c8ee0",
    "missingIndex": 2,
    "choices": [
      {"shapeType": "hexagon", "pieceIndex": 0},
      {"shapeType": "square", "pieceIndex": 3},
      {"shapeType": "hexagon", "pieceIndex": 2},
      {"shapeType": "hexagon", "pieceIndex": 1},
      {"shapeType": "cross", "pieceIndex": 1}
    ],
    "rotates": true,
    "rotationIndex": 0,
    "rotationStartedAt": 1732000000000,
    "rotationIntervalMs": 2000
  },
  "serverNow": 1732000012345
}
```

(Note `correctIndex` is absent — in this example it would be slot `2`, matching `missingIndex: 2`.)

---

## 7. Client-Side Behavior

### 7.1 Screens (state machine)

| Screen | Shown when | Contents |
|---|---|---|
| **Claiming** | `status == "claiming"` | Auto-attempts `/join` on load; shows "You're Player N, waiting…" once claimed, or an error if the game is already full. |
| **Ready** | `status == "ready"` | "Ready to duel" with a single **Start** button (either player can tap it). |
| **Game** | `status == "active"` or `"gameover"` | The clock display, opponent's time, and (on your turn) the puzzle UI. On game-over, a full-screen result overlay with **Play again**. |

### 7.2 Role persistence

The assigned role (`p1`/`p2`) is stored in `sessionStorage` per browser tab, so a page reload during the same game doesn't lose your seat. If the server's game state no longer shows your role as taken (e.g. after a reset), the client clears its stored role and re-attempts `/join` automatically.

### 7.3 Visual/UX details

- The active player's screen background is a bright accent color (orange for P1, blue for P2); the waiting player's screen is a darker shade of the same color.
- A wrong answer triggers a brief full-screen brightness dip plus a "−2 seconds" toast, then a new puzzle appears immediately.
- Clock digits use tabular figures (`font-variant-numeric: tabular-nums`) to avoid layout jitter as digits change.
- Layout respects iOS safe areas (`env(safe-area-inset-*)`) for notches/home indicators.

---

## 8. Running the Application

```bash
python3 duel_clock_server.py [port]
```

- No `pip install` required — pure standard library (`http.server`, `json`, `threading`, `socket`, `time`, `random`).
- Default port: `8765`.
- On startup, the script prints both a `localhost` URL (for the host machine) and a LAN URL (detected via a UDP socket trick against `8.8.8.8:80`, which doesn't actually send traffic — it's just used to determine the outbound-facing local IP).
- Both phones must be on the **same Wi-Fi network/subnet** as the host machine.
- If phones can't connect, the most common cause is the host's firewall blocking the port (e.g. `sudo ufw allow 8765` on Ubuntu).

---

## 9. Known Limitations

- **Single game at a time.** The server holds one global `STATE` object; a third device loading the page while a game is in progress is rejected with a "game already in progress" message rather than starting a second concurrent game.
- **No persistence.** All state is in-memory; restarting the server clears everything.
- **No authentication.** Anyone on the LAN who can reach the port can join, view, or (via direct API calls) manipulate the game — acceptable for a casual local family game, not suitable for exposing beyond a trusted local network.
- **No reconnect-to-spectate.** A third phone loading the page mid-game cannot spectate; it can only wait for the current game to end (via the "full" error) and then either sit out or trigger a new one indirectly by both original players hitting "Play again."
- **Polling latency.** Turn-state changes on the *opponent's* screen may lag up to ~250ms behind the actual server-side event (your own actions update instantly via the direct POST response).

---

## 10. File Manifest

| File | Purpose |
|---|---|
| `duel_clock_server.py` | The entire application — server + embedded client. Single file, no other dependencies. |
