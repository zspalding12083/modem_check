# modem_check

Monitors an **ARRIS SURFboard SB8200** cable modem and your internet connection, and builds a
factual report you can hand to your ISP: outages with start/end windows, modem conditions during
each outage, channel levels and errors, speed and ping results, and the modem's event log.

It was written to document an intermittent upload outage on a DOCSIS 3.1 connection (T3 time-outs,
upstream channels losing lock), where the ISP needed evidence collected while the problem was happening.

- Logs in to the modem's web UI (the newer CBN "HS:" firmware with AES-encrypted login) and records
  status, uptime, the modem clock, every downstream/upstream channel (lock, power, SNR, codeword
  counters) and the event log.
- Runs a ping test every 5 minutes and a full run (modem + ping + speed test) every 30 minutes. A
  failed ping test starts a full run right away, so outages get modem readings taken during them.
- Stores everything in SQLite.
- Serves a dashboard on your LAN and produces HTML/PDF reports with an outage summary, per-outage
  snapshots, charts, and daily speed/ping summaries. Wording sticks to what was measured.

Tested with an SB8200 (hardware 3.0, firmware AC01.01.008_122722_8200.03.07.733) on Ubuntu 22.04
(Orange Pi 5) and Ubuntu 26.04, Python 3.10+. Other firmware versions may use different pages.

## Setup

```bash
git clone https://github.com/zspalding12083/modem_check.git ~/modem_check
cd ~/modem_check
pip3 install --user requests cryptography matplotlib
cp config.example.ini config.ini && chmod 600 config.ini   # then edit: modem password, interface
python3 modem_check.py check                                 # test the login; prints the modem's certificate fingerprint
python3 -m unittest discover -s tests                        # parser tests against saved sample pages
```

`curl` and `ping` must be installed. PDFs need Chrome/Chromium; if a snap-packaged Chromium does not
start (some ARM boards), `pip3 install --user playwright && python3 -m playwright install chromium`
provides a headless build that is found automatically.

Run it on schedule with systemd user units (they expect the project at `~/modem_check`):

```bash
mkdir -p ~/.config/systemd/user
systemctl --user link ~/modem_check/systemd/*.service ~/modem_check/systemd/*.timer
systemctl --user daemon-reload
systemctl --user enable --now modem-check.timer modem-ping.timer modem-dashboard.service
loginctl enable-linger "$USER"     # keep running when you are logged out
```

## Commands

```bash
python3 modem_check.py check                 # read-only status, no database writes
python3 modem_check.py poll                  # full run: modem + ping + speed test
python3 modem_check.py poll --no-tests       # modem only
python3 modem_check.py ping                  # ping test; starts a full run if the line looks unhealthy
python3 modem_check.py note "Tech visit: replaced drop" --at "2026-10-06 10:00"
python3 modem_check.py report --pdf          # everything so far -> reports/*.html + .pdf
python3 modem_check.py report --since 2026-10-09 --until 2026-10-10 --pdf
python3 dashboard.py --port 8520                  # dashboard on 127.0.0.1 (also installed as a service)
```

## Configuration (`config.ini`)

| Section / key | Default | Meaning |
|---|---|---|
| `[modem] host`, `username`, `password` | 192.168.100.1, admin | Modem web UI login (`MODEM_PASSWORD` env var overrides) |
| `[modem] cert_sha256` | (none) | Pin the modem's self-signed certificate (fingerprint from `check`); recommended, see Security |
| `[modem] interface` | (none) | Bind every test to this NIC (e.g. `eth0`), so tests use the cable connection even when a VPN is up |
| `[tests] ping_targets`, `ping_count` | google.com, 10 | Ping test |
| `[tests] download_bytes`, `upload_bytes`, `speedtest_timeout` | 100 MB, 25 MB, 30 s | Speed test size and time cap |
| `[tests] upload_outage_mbps`, `ping_outage_loss_pct` | 1, 50 | What counts as an outage in the report |
| `[tests] ping_trigger_ms`, `trigger_cooldown_min` | 100, 10 | When a ping test starts an extra full run |
| `[ranges] …` | DS power −15…+15 dBmV, SNR ≥ 33 dB, US power 35…51 dBmV | Reference ranges flagged in outage snapshots |

## How the measurements work

All tests run one after another (a shared lock keeps a ping from measuring a speed test in progress).

| Test | What it does |
|---|---|
| Ping | `ping -c 10 -i 0.5 -W 2` to each target; records loss and min/avg/max RTT |
| Download | `curl` GETs of 25 MB blocks from `speed.cloudflare.com/__down` until 100 MB or 30 s; Mbps = bytes × 8 ÷ time |
| Upload | One `curl` POST of 25 MB of random data to `speed.cloudflare.com/__up`, capped at 30 s; failures record curl's error (timed out, connection reset, …) |
| Modem | `xml/getter.xml` functions 1, 6, 9, 10, 11, 13, 19 after an encrypted login via `xml/setter.xml` (fun 15); logs out after each run |

Notes:
- Single-connection tests can read lower than multi-connection tests (e.g. Ookla) on fast lines.
- A full run uses up to 100 MB down + 25 MB up (about 6 GB / 1.2 GB per day at 48 runs).
- Each modem login adds a line to the modem's small event log; that's why the 5-minute check is a
  ping test only. The report leaves the tool's own login entries out.
- Codeword counters are cumulative and reset on modem restart or re-initialization; the report works
  with increases between readings and skips intervals where a channel had no signal (the modem keeps
  counting failed decodes on a channel it can't receive).
- The modem's clock may differ from local time; each poll records the difference and the report shows
  event times both as logged and converted.

## Dashboard

`dashboard.py` serves the report at `http://127.0.0.1:8520/`, rebuilt from the database on every page load,
with range buttons (24 h / 7 days / 30 days / all / custom) and a "Download PDF" link. Chart values show on
hover or tap.

It has **no login**, and the report includes your modem's MAC, serial number and connection history, so
the service listens on localhost only. To view it from other devices on your LAN (e.g. a phone), opt in:

```bash
systemctl --user edit modem-dashboard.service
# add:
#   [Service]
#   ExecStart=
#   ExecStart=/usr/bin/python3 %h/modem_check/dashboard.py --port 8520 --bind 0.0.0.0
systemctl --user restart modem-dashboard.service
```

Only do this on a network you trust, and never forward the port to the internet.

## Security

- **Modem password.** The SB8200 serves HTTPS with a self-signed certificate, and its login "encryption"
  is keyed from a token the modem sends, so it only protects against casual viewing. Anything that can
  impersonate the modem on your network could capture the password. Run the tool on a trusted wired LAN,
  and set `cert_sha256` (printed by `python3 modem_check.py check`): connections to anything presenting a
  different certificate are then refused before the password is sent. If the modem's certificate changes
  (e.g. after a firmware update), `check` reports the mismatch and shows the new fingerprint.
- **Secrets.** `config.ini` is git-ignored; keep it `chmod 600`. The password can come from the
  `MODEM_PASSWORD` environment variable instead.
- **Reports and database** (`reports/`, `data/`) contain your modem's identifiers and connection
  history; they are git-ignored. Review a report before sharing it.

## License

MIT, see [LICENSE](LICENSE).
