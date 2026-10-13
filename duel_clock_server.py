#!/usr/bin/env python3
"""
Three Minute Duel -- a tiny local web server for a two-player chess-clock
game. Fixed 3-minute clock for each player. Whoever's phone loads the page
first automatically becomes Player 1, the second becomes Player 2 -- no
setup screen. To pass your turn you're shown a geometric shape split into
four parts with one part missing, and you pick the piece that completes it
from five choices. Get it wrong and you eat a 2-second penalty and try
again.

Run this on your Ubuntu box:

    python3 duel_clock_server.py

Then open the "On your phones" address it prints on both iPhones,
as long as they're on the same Wi-Fi network as this machine.
No dependencies beyond the Python standard library -- nothing to pip
install, and no images fetched over the network (the shapes are drawn
as SVG paths in the browser).
"""
import itertools
import json
import random
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
WRONG_PENALTY_MS = 2000
GAME_MINUTES = 3
NUM_CHOICES = 5
ROTATION_INTERVAL_MS = 4000
# TODO: Consider adjusting rotation speed as time runs low

# Shapes whose pieces don't map onto each other under a 90-degree turn (the
# triangle's inverted centre piece stays put), so they never rotate.
NON_ROTATING_SHAPES = {"triangle"}
_puzzle_ids = itertools.count(1)

# Shapes whose four pieces are visually distinct by orientation/position,
# so the puzzle can't be solved by color alone. See SHAPE_DATA in the
# client-side JS for the actual piece geometry. Each shape has 4 pieces
# (indices 0-3), so with 5 choices on offer, at least one decoy has to be
# borrowed from a different shape type -- it'll obviously not belong,
# which is fine, it's just an extra wrong option.
SHAPE_TYPES = ["square", "rectangle", "diamond", "triangle",
               "hexagon", "octagon", "circle", "cross"]
COLORS = ["#e0793c", "#3c8ee0", "#4caf6b", "#c14ccb", "#e0c93c", "#e0577a"]


_mock_time = None


def now_ms():
    if _mock_time is not None:
        return _mock_time
    return int(time.time() * 1000)


def make_puzzle():
    shape_type = random.choice(SHAPE_TYPES)
    color = random.choice(COLORS)
    if shape_type == "triangle":
        # the triangle only has one visually distinct "odd" piece: the
        # inverted center piece. Always make that the missing one.
        missing_index = 3
        same_shape_pool = [0, 1, 2]
    else:
        missing_index = random.randint(0, 3)
        same_shape_pool = [i for i in range(4) if i != missing_index]

    seen = {(shape_type, missing_index)}
    decoys = []

    # prefer decoys from the same shape first (most relevant distractors)
    random.shuffle(same_shape_pool)
    for idx in same_shape_pool:
        decoys.append({"shapeType": shape_type, "pieceIndex": idx})
        seen.add((shape_type, idx))

    # fill any remaining slots with pieces borrowed from other shapes
    attempts = 0
    while len(decoys) < NUM_CHOICES - 1 and attempts < 200:
        attempts += 1
        other_shape = random.choice(SHAPE_TYPES)
        piece_idx = random.randint(0, 3)
        key = (other_shape, piece_idx)
        if key in seen:
            continue
        seen.add(key)
        decoys.append({"shapeType": other_shape, "pieceIndex": piece_idx})

    options = [{"shapeType": shape_type, "pieceIndex": missing_index}] + decoys
    order = list(range(NUM_CHOICES))
    random.shuffle(order)
    choices = [options[i] for i in order]
    correct_index = order.index(0)

    return {
        "shapeType": shape_type,
        "color": color,
        "missingIndex": missing_index,
        "choices": choices,
        "id": next(_puzzle_ids),
        "correctIndex": correct_index,
        "rotationIndex": 0,
        "rotationStartedAt": now_ms(),
    }


def get_current_missing_index(puzzle):
    """Calculate which piece is currently the gap based on rotation."""
    if not puzzle:
        return None
    if puzzle["shapeType"] in NON_ROTATING_SHAPES:
        return puzzle["missingIndex"]
    elapsed = now_ms() - puzzle["rotationStartedAt"]
    rotation_count = elapsed // ROTATION_INTERVAL_MS
    current_rotation = (puzzle["rotationIndex"] + rotation_count) % 4
    return (puzzle["missingIndex"] + current_rotation) % 4


def default_state():
    total = GAME_MINUTES * 60 * 1000
    return {
        "status": "claiming",   # claiming | ready | active | gameover
        "totalMs": total,
        "p1RemainingMs": total,
        "p2RemainingMs": total,
        "activePlayer": None,
        "turnStartedAt": None,
        "p1Taken": False,
        "p2Taken": False,
        "winner": None,
        "puzzle": None,
    }


STATE = default_state()


def end_game(state, loser):
    """Mark the game over because `loser` ran out of time. Mutates state."""
    state["status"] = "gameover"
    state["winner"] = "p2" if loser == "p1" else "p1"
    state["turnStartedAt"] = None
    state["activePlayer"] = None
    state["puzzle"] = None


def finalize_if_expired(state):
    """If the active player's clock has run out, end the game. Mutates state."""
    if state["status"] != "active" or not state["activePlayer"]:
        return state
    elapsed = now_ms() - state["turnStartedAt"]
    key = "p1RemainingMs" if state["activePlayer"] == "p1" else "p2RemainingMs"
    if state[key] - elapsed <= 0:
        state[key] = 0
        end_game(state, state["activePlayer"])
    return state


def snapshot():
    finalize_if_expired(STATE)
    s = dict(STATE)
    if s.get("puzzle"):
        puzzle = s["puzzle"]
        s["puzzle"] = {
            "id": puzzle["id"],
            "shapeType": puzzle["shapeType"],
            "color": puzzle["color"],
            "missingIndex": puzzle["missingIndex"],
            "choices": puzzle["choices"],
            "rotates": puzzle["shapeType"] not in NON_ROTATING_SHAPES,
            "rotationIndex": puzzle["rotationIndex"],
            "rotationStartedAt": puzzle["rotationStartedAt"],
            "rotationIntervalMs": ROTATION_INTERVAL_MS,
            # No answer field: the client works out which slot is correct
            # from the rotation, and the server re-checks every answer.
        }
    s["serverNow"] = now_ms()
    return s


def do_join():
    """Auto-assign a role: first caller becomes p1, second becomes p2."""
    with LOCK:
        finalize_if_expired(STATE)
        if not STATE["p1Taken"]:
            STATE["p1Taken"] = True
            role = "p1"
        elif not STATE["p2Taken"]:
            STATE["p2Taken"] = True
            role = "p2"
        else:
            return {"error": "full"}, 409

        if STATE["p1Taken"] and STATE["p2Taken"]:
            STATE["status"] = "ready"

        s = snapshot()
        s["yourRole"] = role
        return s, 200


def do_start():
    with LOCK:
        finalize_if_expired(STATE)
        if STATE["status"] != "ready":
            return {"error": "not_ready"}, 409
        STATE["status"] = "active"
        STATE["activePlayer"] = "p1"
        STATE["turnStartedAt"] = now_ms()
        STATE["puzzle"] = make_puzzle()
        return snapshot(), 200


def do_answer(role, choice, puzzle_id=None):
    with LOCK:
        finalize_if_expired(STATE)
        if STATE["status"] != "active":
            return snapshot(), 200
        if STATE["activePlayer"] != role:
            return {"error": "not_your_turn"}, 409

        # A tap meant for a puzzle that has since been replaced (double-tap
        # after a wrong answer) must not be graded against the new one.
        puzzle = STATE.get("puzzle") or {}
        if puzzle_id is not None and puzzle_id != puzzle.get("id"):
            return {"error": "stale_puzzle"}, 409

        elapsed = now_ms() - STATE["turnStartedAt"]
        key = "p1RemainingMs" if role == "p1" else "p2RemainingMs"
        STATE[key] = max(0, STATE[key] - elapsed)

        try:
            choice_int = int(choice)
            choices = puzzle.get("choices", [])
            if 0 <= choice_int < len(choices):
                chosen = choices[choice_int]
                current_missing = get_current_missing_index(puzzle)
                # shape type must match too: a decoy borrowed from another
                # shape can share the right piece index.
                correct = (chosen.get("shapeType") == puzzle["shapeType"]
                           and chosen.get("pieceIndex") == current_missing)
            else:
                correct = False
        except (TypeError, ValueError):
            correct = False

        if not correct:
            STATE[key] = max(0, STATE[key] - WRONG_PENALTY_MS)

        if STATE[key] <= 0:
            end_game(STATE, role)
            return snapshot(), 200

        if correct:
            STATE["activePlayer"] = "p2" if role == "p1" else "p1"
        # wrong answer: same player keeps the turn and gets a new puzzle
        STATE["turnStartedAt"] = now_ms()
        STATE["puzzle"] = make_puzzle()
        return snapshot(), 200


def do_new():
    global STATE
    with LOCK:
        finalize_if_expired(STATE)
        # Both phones show "Play again"; only the first tap may reset, or the
        # second tap would wipe the seat the first player just re-claimed.
        if STATE["status"] == "gameover":
            STATE = default_state()
        return snapshot(), 200


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<title>Three Minute Duel</title>
<style>
  :root {
    --paper: #f3ede3;
    --ink: #16211f;
    --muted: #6b6257;
    --line: #d8d0c2;
    --p1: #e0793c;
    --p1-dark: #7a3d1c;
    --p2: #3c8ee0;
    --p2-dark: #1c4a7a;
    --lose: #7a2222;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; user-select: none; }
  html, body {
    margin: 0; padding: 0; height: 100%; overflow: hidden;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
    background: var(--paper); color: var(--ink);
    touch-action: manipulation;
  }
  #app { position: fixed; inset: 0; display: flex; flex-direction: column; }
  .screen { flex: 1; display: none; flex-direction: column; align-items: center; justify-content: center; padding: 28px 24px; text-align: center; }
  .screen.show { display: flex; }
  .eyebrow { font-size: 15px; color: var(--muted); margin-bottom: 6px; }
  h1 { font-family: Georgia, "Times New Roman", serif; font-size: 34px; font-weight: 400; margin: 0 0 10px; letter-spacing: -0.01em; }
  p.sub { font-size: 16px; color: var(--muted); max-width: 320px; line-height: 1.5; margin: 0 0 30px; }
  .bigbtn { background: var(--ink); color: var(--paper); border: none; padding: 16px 36px; border-radius: 30px; font-size: 17px; }
  .bigbtn:active { opacity: 0.85; }
  .dots { display: flex; gap: 7px; margin-top: 10px; }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--line); animation: pulse 1.2s infinite ease-in-out; }
  .dot:nth-child(2) { animation-delay: 0.15s; }
  .dot:nth-child(3) { animation-delay: 0.3s; }
  @keyframes pulse { 0%, 80%, 100% { opacity: 0.3; } 40% { opacity: 1; } }
  .errmsg { color: var(--lose); font-size: 14px; margin-top: 14px; min-height: 18px; }
  #game { padding: 0; }
  #gameInner { flex: 1; width: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; position: relative; transition: background 0.15s ease, color 0.15s ease, filter 0.1s ease; }
  #gameInner.penalty { filter: brightness(0.55); }
  #oppRow { position: absolute; top: env(safe-area-inset-top, 18px); left: 0; right: 0; display: flex; flex-direction: column; align-items: center; opacity: 0.65; font-size: 14px; }
  #oppTime { font-family: Georgia, serif; font-size: 22px; }
  #myClock { font-family: Georgia, serif; font-size: 14vw; line-height: 1; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; margin-bottom: 4px; }
  #myStatus { font-size: 14px; opacity: 0.75; margin-top: 2px; margin-bottom: 8px; }
  #waitingWrap { display: flex; flex-direction: column; align-items: center; }
  #puzzleArea { display: none; flex-direction: column; align-items: center; margin-top: 6px; width: 100%; padding: 0 18px; }
  #puzzleArea.show { display: flex; }
  #puzzleShape { width: 32vw; max-width: 175px; margin-bottom: 20px; }
  #puzzleShape svg { width: 100%; height: auto; display: block; overflow: visible; transition: transform 0.5s ease-in-out; }
  #puzzleChoices { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; max-width: 340px; }
  .choiceTile { width: 54px; height: 54px; border-radius: 14px; border: none; background: rgba(0,0,0,0.18); padding: 7px; display: flex; align-items: center; justify-content: center; flex: 0 0 auto; }
  .choiceTile:active { background: rgba(0,0,0,0.32); }
  .choiceTile svg { width: 100%; height: 100%; overflow: visible; }
  #penaltyToast { position: absolute; top: 40%; left: 50%; transform: translate(-50%, -50%) scale(0.9); font-family: Georgia, serif; font-size: 34px; color: #ffb4a8; opacity: 0; pointer-events: none; transition: opacity 0.15s ease, transform 0.15s ease; }
  #penaltyToast.show { opacity: 1; transform: translate(-50%, -50%) scale(1); }
  #resultOverlay { position: fixed; inset: 0; background: rgba(20,10,10,0.94); color: var(--paper); display: none; flex-direction: column; align-items: center; justify-content: center; text-align: center; z-index: 20; padding: 24px; }
  #resultOverlay.show { display: flex; }
  #resultOverlay .eyebrow2 { font-size: 14px; opacity: 0.6; margin-bottom: 10px; }
  #resultOverlay h2 { font-family: Georgia, serif; font-size: 38px; font-weight: 400; margin: 0 0 28px; }
  #resultOverlay button { background: var(--paper); color: var(--ink); border: none; padding: 14px 40px; border-radius: 28px; font-size: 16px; }
</style>
</head>
<body>
<div id="app">

  <div class="screen show" id="claiming">
    <div class="eyebrow">Three Minute Duel</div>
    <h1>Last to answer loses.</h1>
    <p class="sub" id="claimingSub">Loading&hellip;</p>
    <div class="dots"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>
    <div class="errmsg" id="claimErr"></div>
  </div>

  <div class="screen" id="readyScreen">
    <div class="eyebrow" id="readyEyebrow">Both players in</div>
    <h1>Ready to duel</h1>
    <p class="sub" id="readySub">Player 1 moves first.</p>
    <button class="bigbtn" id="startBtn">Start</button>
  </div>

  <div class="screen" id="game">
    <div id="gameInner">
      <div id="oppRow"><div id="oppLabel">Opponent</div><div id="oppTime">3:00</div></div>
      <div id="waitingWrap">
        <div id="myClock">3:00</div>
        <div id="myStatus">waiting&hellip;</div>
        <div id="puzzleArea">
          <div id="puzzleShape"></div>
          <div id="puzzleChoices"></div>
        </div>
      </div>
      <div id="penaltyToast">&minus;2 seconds</div>
    </div>
  </div>

  <div id="resultOverlay">
    <div class="eyebrow2">Time's up</div>
    <h2 id="resultText">You win</h2>
    <button id="playAgainBtn">Play again</button>
  </div>

</div>

<script>
(function() {
  function fmt(ms) {
    var s = Math.max(0, Math.ceil(ms / 1000));
    var m = Math.floor(s / 60);
    var sec = s % 60;
    return m + ':' + (sec < 10 ? '0' : '') + sec;
  }

  // Each shape's outer viewBox and the four SVG paths that exactly tile it.
  // Piece index alone determines a piece's exact position/orientation
  // within its own shape.
  var SHAPE_DATA = {
    square: {
      viewBox: '0 0 100 100',
      pieces: [
        'M0,0 L100,0 L50,50 Z',
        'M100,0 L100,100 L50,50 Z',
        'M100,100 L0,100 L50,50 Z',
        'M0,100 L0,0 L50,50 Z'
      ]
    },
    rectangle: {
      viewBox: '0 0 140 100',
      pieces: [
        'M0,0 L140,0 L70,50 Z',
        'M140,0 L140,100 L70,50 Z',
        'M140,100 L0,100 L70,50 Z',
        'M0,100 L0,0 L70,50 Z'
      ]
    },
    diamond: {
      viewBox: '0 0 100 100',
      pieces: [
        'M50,50 L50,5 L95,50 Z',
        'M50,50 L95,50 L50,95 Z',
        'M50,50 L50,95 L5,50 Z',
        'M50,50 L5,50 L50,5 Z'
      ]
    },
    triangle: {
      viewBox: '0 0 100 100',
      pieces: [
        'M50,6 L72,48 L28,48 Z',
        'M6,90 L28,48 L50,90 Z',
        'M94,90 L50,90 L72,48 Z',
        'M28,48 L72,48 L50,90 Z'
      ]
    },
    hexagon: {
      viewBox: '0 0 100 100',
      pieces: [
        'M50,50 L95,50 L72.5,11.03 L50,11.03 Z',
        'M50,50 L50,88.97 L72.5,88.97 L95,50 Z',
        'M50,50 L5,50 L27.5,88.97 L50,88.97 Z',
        'M50,50 L50,11.03 L27.5,11.03 L5,50 Z'
      ]
    },
    octagon: {
      viewBox: '0 0 100 100',
      pieces: [
        'M50,50 L50,8.42 L67.22,8.42 L91.58,32.78 L91.58,50 Z',
        'M50,50 L91.58,50 L91.58,67.22 L67.22,91.58 L50,91.58 Z',
        'M50,50 L50,91.58 L32.78,91.58 L8.42,67.22 L8.42,50 Z',
        'M50,50 L8.42,50 L8.42,32.78 L32.78,8.42 L50,8.42 Z'
      ]
    },
    circle: {
      viewBox: '0 0 100 100',
      pieces: [
        'M50,50 L50,5 A45,45 0 0 1 95,50 Z',
        'M50,50 L95,50 A45,45 0 0 1 50,95 Z',
        'M50,50 L50,95 A45,45 0 0 1 5,50 Z',
        'M50,50 L5,50 A45,45 0 0 1 50,5 Z'
      ]
    },
    cross: {
      viewBox: '0 0 100 100',
      pieces: [
        'M50,10 L65,10 L65,35 L90,35 L90,50 L50,50 Z',
        'M90,50 L90,65 L65,65 L65,90 L50,90 L50,50 Z',
        'M50,90 L35,90 L35,65 L10,65 L10,50 L50,50 Z',
        'M10,50 L10,35 L35,35 L35,10 L50,10 L50,50 Z'
      ]
    }
  };

  function shapeWithGapSvg(shapeType, currentMissingIndex, color) {
    var data = SHAPE_DATA[shapeType];
    var svg = '<svg viewBox="' + data.viewBox + '">';
    data.pieces.forEach(function(d, i) {
      if (i === currentMissingIndex) {
        svg += '<path d="' + d + '" fill="rgba(255,255,255,0.12)" stroke="rgba(255,255,255,0.65)" stroke-width="2" stroke-dasharray="4,3"/>';
      } else {
        svg += '<path d="' + d + '" fill="' + color + '"/>';
      }
    });
    svg += '</svg>';
    return svg;
  }

  function pieceTileSvg(shapeType, pieceIndex, color) {
    var data = SHAPE_DATA[shapeType];
    return '<svg viewBox="' + data.viewBox + '"><path d="' + data.pieces[pieceIndex] + '" fill="' + color + '"/></svg>';
  }

  var els = {};
  ['claiming','readyScreen','game'].forEach(function(id){ els[id] = document.getElementById(id); });
  function showScreen(id) {
    Object.keys(els).forEach(function(k){ els[k].classList.toggle('show', k === id); });
  }

  var myRole = sessionStorage.getItem('duelclock_role') || null;
  var joinInFlight = false;
  var lastState = null;
  var rafHandle = null;
  var frameStart = null;
  var lastPuzzleKey = null;

  function post(path, body) {
    return fetch(path, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body || {}) })
      .then(function(r){ return r.json().then(function(j){ return { ok: r.ok, data: j }; }); });
  }
  function getState() { return fetch('/state').then(function(r){ return r.json(); }); }

  function attemptJoin() {
    if (joinInFlight || myRole) return;
    joinInFlight = true;
    post('/join', {}).then(function(res) {
      joinInFlight = false;
      if (!res.ok) {
        document.getElementById('claimErr').textContent = res.data.error === 'full'
          ? 'Two players are already in this game. Wait for it to finish, or restart the server.'
          : "Couldn't connect right now.";
        return;
      }
      document.getElementById('claimErr').textContent = '';
      myRole = res.data.yourRole;
      sessionStorage.setItem('duelclock_role', myRole);
      onState(res.data);
    }).catch(function(){ joinInFlight = false; });
  }

  document.getElementById('startBtn').addEventListener('click', function(){
    post('/start', {}).then(function(res){ if (res.ok) onState(res.data); });
  });

  document.getElementById('playAgainBtn').addEventListener('click', function(){
    document.getElementById('resultOverlay').classList.remove('show');
    myRole = null;
    sessionStorage.removeItem('duelclock_role');
    post('/new', {}).then(function(res){ onState(res.data); });
  });

  function onState(state) {
    lastState = state;
    frameStart = Date.now();  // when this state arrived; clocks extrapolate from here
    if (state.status !== 'gameover') {
      document.getElementById('resultOverlay').classList.remove('show');
    }
    if (state.status === 'claiming') {
      if (myRole && ((myRole === 'p1' && !state.p1Taken) || (myRole === 'p2' && !state.p2Taken))) {
        myRole = null; sessionStorage.removeItem('duelclock_role');
      }
      showScreen('claiming');
      var sub = document.getElementById('claimingSub');
      if (myRole) {
        sub.textContent = "You're Player " + (myRole === 'p1' ? '1' : '2') + ". Waiting for the other phone to load this page\u2026";
      } else {
        sub.textContent = 'Loading\u2026';
        attemptJoin();
      }
      stopTicking();
    } else if (state.status === 'ready') {
      if (!myRole) { showScreen('claiming'); attemptJoin(); return; }
      showScreen('readyScreen');
      var readySub = document.getElementById('readySub');
      readySub.textContent = myRole === 'p1' ? "Both players are in. Tap Start when you're ready to move first." : "Both players are in. Your opponent will tap Start.";
      stopTicking();
    } else if (state.status === 'active') {
      if (!myRole) { showScreen('claiming'); return; }
      showScreen('game');
      renderPuzzle(state);
      startTicking();
    } else if (state.status === 'gameover') {
      if (!myRole) { showScreen('claiming'); return; }
      showScreen('game');
      stopTicking();
      puzzleArea.classList.remove('show');
      renderOnce(state);
      var won = state.winner === myRole;
      document.getElementById('resultText').textContent = won ? 'You win' : 'You lose';
      document.getElementById('resultOverlay').classList.add('show');
    }
  }

  var gameInner = document.getElementById('gameInner');
  var myClockEl = document.getElementById('myClock');
  var oppTimeEl = document.getElementById('oppTime');
  var myStatusEl = document.getElementById('myStatus');
  var oppLabelEl = document.getElementById('oppLabel');
  var puzzleArea = document.getElementById('puzzleArea');

  function computeRemaining(state, localOffsetMs) {
    var p1r = state.p1RemainingMs, p2r = state.p2RemainingMs;
    if (state.activePlayer && state.turnStartedAt) {
      var elapsed = (state.serverNow + localOffsetMs) - state.turnStartedAt;
      if (state.activePlayer === 'p1') p1r -= elapsed; else p2r -= elapsed;
    }
    return { p1r: p1r, p2r: p2r };
  }

  function renderOnce(state) {
    var r = computeRemaining(state, 0);
    var myR = myRole === 'p1' ? r.p1r : r.p2r;
    var oppR = myRole === 'p1' ? r.p2r : r.p1r;
    myClockEl.textContent = fmt(myR);
    oppTimeEl.textContent = fmt(oppR);
  }

  function applyColors(state, myTurn) {
    var mine = myRole === 'p1' ? 'var(--p1)' : 'var(--p2)';
    var mineDark = myRole === 'p1' ? 'var(--p1-dark)' : 'var(--p2-dark)';
    if (myTurn) { gameInner.style.background = mine; gameInner.style.color = '#16130d'; }
    else { gameInner.style.background = mineDark; gameInner.style.color = 'rgba(243,237,227,0.9)'; }
  }

  var lastRotationAngle = 0;

  // Best guess at the server's clock right now: the timestamp that arrived
  // with the last state, plus however long ago it arrived.
  function estServerNow() {
    return lastState.serverNow + (Date.now() - frameStart);
  }

  // Cumulative (never wrapped to 360) so the CSS transition always keeps
  // turning forward instead of spinning back 270 degrees at the wrap.
  function getRotationAngle(puzzle) {
    if (!puzzle || !puzzle.rotates || !puzzle.rotationStartedAt) return 0;
    var interval = puzzle.rotationIntervalMs || 4000;
    var steps = Math.max(0, Math.floor((estServerNow() - puzzle.rotationStartedAt) / interval));
    return ((puzzle.rotationIndex || 0) + steps) * 90;
  }

  function updateRotation(puzzle) {
    var angle = getRotationAngle(puzzle);
    if (angle === lastRotationAngle) return;
    lastRotationAngle = angle;
    var svg = document.querySelector('#puzzleShape svg');
    if (svg) svg.style.transform = 'rotate(' + angle + 'deg)';
  }

  function renderPuzzle(state) {
    var myTurn = state.activePlayer === myRole;
    if (!myTurn || !state.puzzle) {
      puzzleArea.classList.remove('show');
      lastPuzzleKey = null;
      lastRotationAngle = 0;
      return;
    }
    var p = state.puzzle;
    var key = p.id + '|' + p.shapeType + '|' + p.color + '|' + p.missingIndex + '|' +
      p.choices.map(function(c){ return c.shapeType + c.pieceIndex; }).join(',');

    // Same puzzle as last poll: leave the DOM alone so taps aren't lost to a
    // rebuild and the rotation transition isn't restarted.
    if (key === lastPuzzleKey) {
      puzzleArea.classList.add('show');
      updateRotation(p);
      return;
    }
    lastPuzzleKey = key;
    lastRotationAngle = 0;

    document.getElementById('puzzleShape').innerHTML = shapeWithGapSvg(p.shapeType, p.missingIndex, p.color);

    var choicesEl = document.getElementById('puzzleChoices');
    choicesEl.innerHTML = '';
    p.choices.forEach(function(c, slot) {
      var b = document.createElement('button');
      b.className = 'choiceTile';
      b.innerHTML = pieceTileSvg(c.shapeType, c.pieceIndex, p.color);
      b.addEventListener('click', function(ev){ ev.stopPropagation(); submitAnswer(slot, p.id); });
      choicesEl.appendChild(b);
    });

    puzzleArea.classList.add('show');
    updateRotation(p);
  }

  function submitAnswer(choiceSlot, puzzleId) {
    if (!lastState || lastState.status !== 'active' || lastState.activePlayer !== myRole) return;
    if (lastState.puzzle && lastState.puzzle.id !== puzzleId) return;  // stale tile
    var wasRole = myRole;
    post('/answer', { role: myRole, choice: choiceSlot, puzzleId: puzzleId }).then(function(res) {
      if (!res.ok) return;
      var stillMine = res.data.status === 'active' && res.data.activePlayer === wasRole;
      if (stillMine) flashPenalty();
      onState(res.data);
    });
  }

  function flashPenalty() {
    var toast = document.getElementById('penaltyToast');
    gameInner.classList.add('penalty');
    toast.classList.add('show');
    setTimeout(function(){ gameInner.classList.remove('penalty'); toast.classList.remove('show'); }, 550);
  }

  function startTicking() {
    if (rafHandle) return;  // already running; onState calls this every poll
    function frame() {
      if (!lastState || lastState.status !== 'active') { rafHandle = null; return; }
      var elapsedSincePoll = Date.now() - frameStart;
      var r = computeRemaining(lastState, elapsedSincePoll);
      var myR = myRole === 'p1' ? r.p1r : r.p2r;
      var oppR = myRole === 'p1' ? r.p2r : r.p1r;
      var myTurn = lastState.activePlayer === myRole;

      myClockEl.textContent = fmt(Math.max(0, myR));
      oppTimeEl.textContent = fmt(Math.max(0, oppR));
      oppLabelEl.textContent = !myTurn ? "Opponent \u2014 their turn" : "Opponent";
      myStatusEl.textContent = myTurn ? 'pick the missing piece' : 'waiting on opponent';
      applyColors(lastState, myTurn);

      if (myTurn && lastState.puzzle) updateRotation(lastState.puzzle);

      rafHandle = requestAnimationFrame(frame);
    }
    rafHandle = requestAnimationFrame(frame);
  }
  function stopTicking() {
    if (rafHandle) cancelAnimationFrame(rafHandle);
    rafHandle = null;
  }

  function poll() {
    getState().then(onState).catch(function(){}).then(function(){
      setTimeout(poll, 250);
    });
  }
  poll();
})();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        body = INDEX_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send_html()
        elif path == "/state":
            self._send_json(snapshot())
        else:
            self.send_error(404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        length = max(0, min(length, 10_000))  # bodies here are tiny
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        if self.path == "/join":
            result, code = do_join()
        elif self.path == "/start":
            result, code = do_start()
        elif self.path == "/answer":
            result, code = do_answer(payload.get("role"), payload.get("choice"),
                                     payload.get("puzzleId"))
        elif self.path == "/new":
            result, code = do_new()
        else:
            self.send_error(404)
            return
        self._send_json(result, code)

    def log_message(self, format, *args):
        pass  # keep the console quiet


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    ip = lan_ip()
    print("Three Minute Duel is running.")
    print("  On this machine:  http://localhost:%d" % port)
    print("  On your phones:   http://%s:%d" % (ip, port))
    print("  (Both phones need to be on the same Wi-Fi network as this machine.)")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
