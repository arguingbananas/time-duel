# Time Duel

A local, two-phone chess-clock dueling game with a jigsaw-puzzle turn mechanic. Two players open the same page on their own phones, each gets a 2-minute countdown, and turns alternate by correctly solving a "which piece fills the gap?" shape puzzle. Wrong answers cost 2 seconds. The first clock to hit zero loses.

Single Python file, embedded HTML/JS client, zero dependencies — nothing to `pip install`, no internet needed, all graphics are inline SVG.

## Running

```bash
python3 duel_clock_server.py [port]   # default port: 8765
```

The server prints a `localhost` URL and a LAN URL. Open the LAN URL on both phones (same Wi-Fi as the host). If phones can't connect, the host firewall is the usual culprit (e.g. `sudo ufw allow 8765`).

## How it works

- First phone to load becomes Player 1, second becomes Player 2 — no setup screens.
- Server-authoritative timing: win/loss is judged by the server's clock, so late taps can't "beat the clock".
- On your turn: a shape with a missing piece rotates 90° every 2 seconds; pick the tile that fits the gap *as it currently points*.
- While you wait, your phone shows a live spectator view of your opponent's puzzle, their misses, and a time lead bar.

Full details in [time-duel-spec.md](time-duel-spec.md).

## Tests

```bash
python3 -m pytest test_duel_clock_server.py
```

## License

Released under the [MIT License](LICENSE).
