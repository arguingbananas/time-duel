# Time Duel

A local, two-phone chess-clock dueling game with a jigsaw-puzzle turn mechanic.¹ Two players open the same page on their own phones, each gets a 2-minute countdown, and turns alternate by correctly solving a "which piece fills the gap?" shape puzzle. Wrong answers cost 2 seconds. The first clock to hit zero loses.

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

## Security

Time Duel assumes a trusted home network: there's no HTTPS and no password,
and anyone on the same Wi-Fi can open the page, watch, and join a free seat.
Within that, the server:

- gives each player's phone a random token when it joins, and checks it on
  every answer, start and reset, so one phone can't play as the other;
- accepts only small JSON requests;
- refuses requests addressed to anything but an IP address, `localhost` or
  the computer's own name, so other websites can't reach the game through a
  player's browser;
- sends standard security headers.

Don't expose it beyond a trusted local network. Details in
[time-duel-spec.md](time-duel-spec.md), section 6.2.

## Tests

```bash
python3 -m unittest test_duel_clock_server
```

## License

Released under the [MIT License](LICENSE).

---

¹ The entire project — concept, specification, and code — was an original human idea written by an artificial intelligence.
