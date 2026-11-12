#!/usr/bin/env python3
"""Collect evidence of cable-modem / upload problems (ARRIS SB8200, CBN web UI).

Commands:
  poll                 log in to the modem, record status/channels/event log, run ping + speed test
  ping                 ping test only; starts a full poll if it sees loss or high latency
  note TEXT [--at T]   add a manual timeline note (e.g. "Tech visit")
  report [--since D] [--until D] [-o FILE] [--pdf]   write an HTML (and PDF) report
  check                log in and print a short status summary (no DB writes)
"""
import argparse
import base64
import configparser
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.ini"

DEFAULTS = {
    "modem": {"host": "192.168.100.1", "username": "admin", "password": "", "interface": ""},
    "tests": {
        "ping_targets": "google.com",
        "ping_count": "10",
        "download_bytes": "100000000",
        "upload_bytes": "25000000",
        "speedtest_timeout": "30",
        "upload_outage_mbps": "1.0",
        # `ping` command: start a full poll when a ping test loses packets or averages above this,
        # unless a full poll already ran within trigger_cooldown_min.
        "ping_trigger_ms": "100",
        # Report: a ping test losing at least this % of packets counts toward an outage.
        "ping_outage_loss_pct": "50",
        "trigger_cooldown_min": "10",
    },
    # Reference ranges used to flag readings in the report's outage snapshots (commonly published
    # guidance for DOCSIS 3.0/3.1 cable modems; adjust if your ISP gives you different numbers).
    "ranges": {
        "ds_power_min": "-15", "ds_power_max": "15",   # dBmV, downstream receive power
        "ds_snr_min": "33",                            # dB, SC-QAM 256-QAM SNR
        "us_power_min": "35", "us_power_max": "51",    # dBmV, upstream transmit power
        "power_change_db": "2",                        # report power changes at least this big
        "snr_drop_db": "4",                            # report SNR/MER drops at least this big (SB8200 reports SNR in ~3 dB steps)
    },
    "storage": {"db": "data/modem.db", "reports": "reports"},
}

# getter.xml function codes used by the SB8200 (firmware AC01.01.008_122722_8200.03.07.733) web UI
FUN_GLOBAL, FUN_US_OFDMA, FUN_DS_OFDM, FUN_DS_QAM, FUN_US_QAM, FUN_EVENTLOG, FUN_DS_CODEWORDS = 1, 6, 9, 10, 11, 13, 19
FUN_LOGIN, FUN_LOGOUT = 15, 16


def load_config(path):
    cfg = configparser.ConfigParser()
    cfg.read_dict(DEFAULTS)
    if not cfg.read(path):
        sys.exit(f"config file not found: {path}")
    if os.environ.get("MODEM_PASSWORD"):
        cfg["modem"]["password"] = os.environ["MODEM_PASSWORD"]
    return cfg


def now_local():
    return dt.datetime.now().astimezone().replace(microsecond=0)


# ---------------------------------------------------------------------------
# Modem client
# ---------------------------------------------------------------------------

class Modem:
    def __init__(self, host, username, password, interface=""):
        import requests
        import urllib3
        from requests.adapters import HTTPAdapter

        urllib3.disable_warnings()

        class BoundAdapter(HTTPAdapter):
            def init_poolmanager(self, *a, **k):
                if interface:
                    k["socket_options"] = [(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode())]
                super().init_poolmanager(*a, **k)

        self.base = f"https://{host}"
        self.host = host
        self.username, self.password = username, password
        self.s = requests.Session()
        self.s.verify = False  # modem uses a self-signed certificate
        # The modem's web server drops requests that don't look like the browser UI's XHRs.
        self.s.headers.update({
            "Connection": "close",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.base + "/common_page/login.html",
            "Origin": self.base,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/130.0",
        })
        adapter = BoundAdapter()
        self.s.mount("https://", adapter)
        self.s.mount("http://", adapter)
        self.hw_model = None

    def _token(self):
        return self.s.cookies.get("sessionToken", domain=self.host) or self.s.cookies.get("sessionToken")

    def _encrypt(self, text):
        """Port of CBN_Encrypt() from js/encrypt_cryptoJS.js."""
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        tok = self._token().encode()
        key, iv = hashlib.sha256(tok).digest(), hashlib.md5(tok).digest()
        padder = padding.PKCS7(128).padder()
        data = padder.update(text.encode()) + padder.finalize()
        enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        ct = enc.update(data) + enc.finalize()
        return base64.b64encode(f"HS:{self.hw_model}:{ct.hex()}".encode()).decode()

    def get(self, fun):
        r = self.s.post(self.base + "/xml/getter.xml", data={"token": self._token(), "fun": fun}, timeout=30)
        r.raise_for_status()
        return r.text

    def login(self):
        self.s.get(self.base + "/", timeout=30)  # sets sessionToken cookie
        g = self.get(FUN_GLOBAL)
        self.hw_model = ET.fromstring(g).findtext("HwModel")
        r = self.s.post(self.base + "/xml/setter.xml", timeout=30, data={
            "token": self._token(), "fun": FUN_LOGIN,
            "Username": self._encrypt(self.username), "Password": self._encrypt(self.password),
        })
        if "successful" not in r.text:
            raise RuntimeError(f"modem login failed: {r.text[:200]!r}")
        self.s.cookies.set("SID", r.text.split("SID=")[1].strip(), domain=self.host, path="/")

    def logout(self):
        try:
            self.s.post(self.base + "/xml/setter.xml", data={"token": self._token(), "fun": FUN_LOGOUT}, timeout=15)
        except Exception:
            pass

    def fetch_all(self):
        """Returns {fun: xml_text} for every page we record."""
        self.login()
        try:
            return {f: self.get(f) for f in (FUN_GLOBAL, FUN_DS_QAM, FUN_DS_CODEWORDS, FUN_DS_OFDM,
                                              FUN_US_QAM, FUN_US_OFDMA, FUN_EVENTLOG)}
        finally:
            self.logout()


# ---------------------------------------------------------------------------
# Parsers (written against tests/fixtures/*.xml)
# ---------------------------------------------------------------------------

def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def parse_uptime(s):
    """'0day(s)3h:44m:40s' -> seconds"""
    m = re.match(r"\s*(\d+)\s*day\(s\)\s*(\d+)h:(\d+)m:(\d+)s", s or "")
    if not m:
        return None
    d, h, mi, se = map(int, m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + se


def parse_global(xml):
    root = ET.fromstring(xml)
    t = {c.tag: (c.text or "").strip() for c in root}
    modem_time = None
    if t.get("CurrentTime"):
        try:
            modem_time = dt.datetime.strptime(" ".join(t["CurrentTime"].split()), "%a %b %d %H:%M:%S %Y")
        except ValueError:
            pass
    return {
        "modem_time_raw": t.get("CurrentTime"),
        "modem_time": modem_time,
        "uptime_raw": t.get("cm_system_uptime"),
        "uptime_s": parse_uptime(t.get("cm_system_uptime")),
        "cm_status": t.get("cm_status"),
        "cm_connected": t.get("cm_connected"),
        "network_access": t.get("cm_network_access"),
        "config_file": t.get("configuration_file"),
        "docsis_mode": t.get("cm_docsis_mode"),
        "sw_version": t.get("SwVersion"),
        "hw_model": t.get("HwModel"),
        "hw_version": t.get("cm_hardware_version"),
        "mac": t.get("cm_mac_addr"),
        "serial": t.get("cm_serial_number"),
    }


def parse_downstream(qam_xml, codeword_xml, ofdm_xml):
    cw = {}
    for sig in ET.fromstring(codeword_xml).iter("signal"):
        cw[_int(sig.findtext("dsid"))] = sig
    rows = []
    for d in ET.fromstring(qam_xml).iter("downstream"):
        chid = _int(d.findtext("chid"))
        c = cw.get(chid)
        rows.append({
            "kind": "SC-QAM", "chid": chid, "locked": d.findtext("IsLocked") == "1",
            "modulation": d.findtext("mod"), "freq_hz": _int(d.findtext("freq")),
            "power_dbmv": _num(d.findtext("pow")), "snr_db": _num(d.findtext("snr")),
            "unerrored": _int(c.findtext("unerrored")) if c is not None else None,
            "corrected": _int(c.findtext("correctable")) if c is not None else None,
            "uncorrectable": _int(c.findtext("uncorrectable")) if c is not None else None,
        })
    for d in ET.fromstring(ofdm_xml).iter("downstream"):
        rows.append({
            "kind": "OFDM", "chid": _int(d.findtext("dsid")), "locked": d.findtext("ofdmIsLocked") == "1",
            "modulation": d.findtext("ofdmModulation"), "freq_hz": _int(d.findtext("plcFrequency")),
            "power_dbmv": _num(d.findtext("PLCPower")), "snr_db": _num(d.findtext("DataScAvgMer")),
            "unerrored": None,
            "corrected": _int(d.findtext("ofdmCorrected")),
            "uncorrectable": _int(d.findtext("ofdmUncorrectable")),
        })
    return rows


def parse_upstream(qam_xml, ofdma_xml):
    rows = []
    for u in ET.fromstring(qam_xml).iter("upstream"):
        rows.append({
            "kind": u.findtext("channeltype") or "SC-QAM", "chid": _int(u.findtext("usid")),
            "locked": u.findtext("usLocked") == "1", "modulation": u.findtext("mod"),
            "freq_hz": _int(u.findtext("freq")), "width_hz": _int(u.findtext("bandwidth")),
            "power_dbmv": _num(u.findtext("power")),
        })
    for u in ET.fromstring(ofdma_xml).iter("upstream"):
        if u.findtext("ofdmaLocked") != "1":
            continue
        rows.append({
            "kind": "OFDMA", "chid": _int(u.findtext("usid")), "locked": True,
            "modulation": u.findtext("ofdmaModulation"),
            "freq_hz": int((_num(u.findtext("centerFreq")) or 0) * 1e6),
            "width_hz": int((_num(u.findtext("width")) or 0) * 1e6),
            "power_dbmv": _num(u.findtext("repPower1_6")),
        })
    return rows


def parse_eventlog(xml):
    rows = []
    for e in ET.fromstring(xml).iter("eventlog"):
        raw_time = (e.findtext("time") or "").strip()
        text = (e.findtext("text") or "").strip()
        try:
            t = dt.datetime.strptime(raw_time, "%d/%m/%Y %H:%M:%S")  # modem uses DD/MM/YYYY
        except ValueError:
            t = None
        rows.append({
            "time_raw": raw_time,
            "time": t if (t and t.year > 1970) else None,  # 31/12/1969 = before clock sync
            "priority": (e.findtext("prior") or "").strip(),
            "text": text,
        })
    return rows


def short_event_text(text):
    """Drop the ';CM-MAC=...;CMTS-MAC=...;CM-QOS=...;CM-VER=...;' suffix."""
    return re.split(r";\s*CM-MAC=", text)[0].strip().rstrip(";")


# ---------------------------------------------------------------------------
# Network tests
# ---------------------------------------------------------------------------

def run_ping(target, count, interface=""):
    cmd = ["ping", "-n", "-c", str(count), "-i", "0.5", "-W", "2"]
    if interface:
        cmd += ["-I", interface]
    cmd.append(target)
    res = {"target": target, "sent": count, "received": 0, "loss_pct": 100.0,
           "rtt_min": None, "rtt_avg": None, "rtt_max": None, "error": None}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=count * 3 + 15)
        out = p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        res["error"] = "ping timed out"
        return res
    m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received.*?([\d.]+)% packet loss", out)
    if m:
        res["sent"], res["received"], res["loss_pct"] = int(m[1]), int(m[2]), float(m[3])
    m = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/", out)
    if m:
        res["rtt_min"], res["rtt_avg"], res["rtt_max"] = map(float, m.groups())
    if p.returncode not in (0, 1) or not m and res["received"] == 0:
        res["error"] = out.strip().splitlines()[-1][:200] if out.strip() else f"exit {p.returncode}"
    return res


def _curl(args, timeout, interface="", stdin=None):
    cmd = ["curl", "-s", "-o", "/dev/null", "-m", str(timeout), "-w", "%{json}"]
    if interface:
        cmd += ["--interface", interface]
    p = subprocess.run(cmd + args, input=stdin, capture_output=True, timeout=timeout + 15)
    try:
        info = json.loads(p.stdout.decode() or "{}")
    except json.JSONDecodeError:
        info = {}
    return p.returncode, info


def run_speedtest(cfg):
    """Download/upload test against speed.cloudflare.com using curl (time-capped)."""
    t = cfg["tests"]
    iface = cfg["modem"].get("interface", "")
    timeout = t.getint("speedtest_timeout")
    res = {"server": "speed.cloudflare.com", "down_mbps": None, "up_mbps": None,
           "latency_ms": None, "down_bytes": None, "up_bytes": None, "error": None}
    errors = []

    rc, info = _curl(["https://speed.cloudflare.com/__down?bytes=0"], 15, iface)
    if rc == 0 and info.get("time_starttransfer"):
        res["latency_ms"] = round((info["time_starttransfer"] - info.get("time_appconnect", 0)) * 1000, 1)

    # Cloudflare caps one download at ~25 MB, so fetch chunks until download_bytes or the time cap is reached.
    chunk = min(t.getint("download_bytes"), 25_000_000)
    got, spent = 0, 0.0
    while got < t.getint("download_bytes") and spent < timeout:
        rc, info = _curl([f"https://speed.cloudflare.com/__down?bytes={chunk}"],
                         max(5, int(timeout - spent)), iface)
        got += info.get("size_download") or 0
        spent += info.get("time_total") or 0
        if rc not in (0, 28) or info.get("http_code") not in (200, None):
            # 28 = hit the time cap; partial result is still a valid measurement
            errors.append(f"download: curl exit {rc} http {info.get('http_code')} {info.get('errormsg') or ''}".strip())
            break
        if rc == 28:
            break
    res["down_bytes"] = got
    res["down_mbps"] = round(got * 8 / spent / 1e6, 2) if spent and got else 0.0
    if not got and not errors:
        errors.append("download: no data received")

    payload = os.urandom(t.getint("upload_bytes"))
    rc, info = _curl(["-X", "POST", "-H", "Content-Type: application/octet-stream",
                      "--data-binary", "@-", "https://speed.cloudflare.com/__up"], timeout, iface, stdin=payload)
    up_bytes = info.get("size_upload") or 0
    elapsed = info.get("time_total") or 0
    res["up_bytes"] = up_bytes
    if info.get("speed_upload"):
        res["up_mbps"] = round(info["speed_upload"] * 8 / 1e6, 2)
    elif elapsed:
        res["up_mbps"] = round(up_bytes * 8 / elapsed / 1e6, 2)
    else:
        res["up_mbps"] = 0.0
    if rc not in (0, 28) or info.get("http_code") not in (200, None):
        errors.append(f"upload: curl exit {rc} http {info.get('http_code')} {info.get('errormsg') or ''}".strip())
    elif rc == 28 and up_bytes < t.getint("upload_bytes"):
        errors.append(f"upload: {up_bytes} of {t.getint('upload_bytes')} bytes sent before {timeout}s limit")

    res["error"] = "; ".join(errors) or None
    return res


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, ok INTEGER NOT NULL, error TEXT,
    modem_time_raw TEXT, modem_time TEXT, clock_offset_s INTEGER,
    uptime_raw TEXT, uptime_s INTEGER, cm_status TEXT, cm_connected TEXT, network_access TEXT,
    config_file TEXT, docsis_mode TEXT, sw_version TEXT, hw_model TEXT, hw_version TEXT, mac TEXT, serial TEXT
);
CREATE TABLE IF NOT EXISTS raw_xml (poll_id INTEGER, fun INTEGER, xml TEXT, PRIMARY KEY (poll_id, fun));
CREATE TABLE IF NOT EXISTS downstream (
    poll_id INTEGER, kind TEXT, chid INTEGER, locked INTEGER, modulation TEXT, freq_hz INTEGER,
    power_dbmv REAL, snr_db REAL, unerrored INTEGER, corrected INTEGER, uncorrectable INTEGER
);
CREATE TABLE IF NOT EXISTS upstream (
    poll_id INTEGER, kind TEXT, chid INTEGER, locked INTEGER, modulation TEXT, freq_hz INTEGER,
    width_hz INTEGER, power_dbmv REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, time_raw TEXT, time TEXT, priority TEXT, text TEXT,
    first_seen_poll INTEGER, first_seen_ts TEXT, UNIQUE (time_raw, text)
);
CREATE TABLE IF NOT EXISTS pings (
    id INTEGER PRIMARY KEY, ts TEXT, target TEXT, sent INTEGER, received INTEGER, loss_pct REAL,
    rtt_min REAL, rtt_avg REAL, rtt_max REAL, error TEXT
);
CREATE TABLE IF NOT EXISTS speedtests (
    id INTEGER PRIMARY KEY, ts TEXT, server TEXT, down_mbps REAL, up_mbps REAL, latency_ms REAL,
    down_bytes INTEGER, up_bytes INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, ts TEXT, text TEXT, added TEXT);
CREATE INDEX IF NOT EXISTS ds_poll ON downstream(poll_id);
CREATE INDEX IF NOT EXISTS us_poll ON upstream(poll_id);
"""


def open_db(cfg):
    path = Path(cfg["storage"]["db"])
    if not path.is_absolute():
        path = HERE / path
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    for table, col in (("polls", "trigger"), ("speedtests", "trigger"), ("pings", "source")):
        if col not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
    return db


@contextlib.contextmanager
def run_lock(cfg, wait_s=240):
    """One test run at a time: a ping during a speed test would measure the speed test, not the line."""
    path = Path(cfg["storage"]["db"])
    path = (path if path.is_absolute() else HERE / path).with_suffix(".lock")
    with open(path, "w") as f:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError("another run is still in progress")
                time.sleep(1)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def record_pings(cfg, db, source):
    """Ping every target; returns the result dicts."""
    out = []
    for target in cfg["tests"]["ping_targets"].split(","):
        target = target.strip()
        if not target:
            continue
        p = run_ping(target, cfg["tests"].getint("ping_count"), cfg["modem"].get("interface", ""))
        db.execute("INSERT INTO pings (ts, target, sent, received, loss_pct, rtt_min, rtt_avg, rtt_max, error, source)"
                   " VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (_iso(now_local()), target, p["sent"], p["received"], p["loss_pct"],
                    p["rtt_min"], p["rtt_avg"], p["rtt_max"], p["error"], source))
        print(f"  ping {target}: {p['received']}/{p['sent']} received, avg {p['rtt_avg']} ms"
              + (f" ({p['error']})" if p["error"] else ""))
        out.append(p)
    db.commit()
    return out


def _iso(t):
    return t.isoformat(sep=" ") if t else None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_poll(cfg, args):
    with run_lock(cfg):
        _poll(cfg, args.no_tests, args.trigger)


def _poll(cfg, no_tests, trigger):
    db = open_db(cfg)
    m = cfg["modem"]
    ts = now_local()
    poll_id = None
    try:
        modem = Modem(m["host"], m["username"], m["password"], m.get("interface", ""))
        pages = modem.fetch_all()
        g = parse_global(pages[FUN_GLOBAL])
        offset = None
        if g["modem_time"]:
            offset = int((ts.replace(tzinfo=None) - g["modem_time"]).total_seconds())
        cur = db.execute(
            "INSERT INTO polls (ts, ok, trigger, modem_time_raw, modem_time, clock_offset_s, uptime_raw, uptime_s,"
            " cm_status, cm_connected, network_access, config_file, docsis_mode, sw_version, hw_model, hw_version,"
            " mac, serial) VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_iso(ts), trigger, g["modem_time_raw"], _iso(g["modem_time"]), offset, g["uptime_raw"], g["uptime_s"],
             g["cm_status"], g["cm_connected"], g["network_access"], g["config_file"], g["docsis_mode"],
             g["sw_version"], g["hw_model"], g["hw_version"], g["mac"], g["serial"]))
        poll_id = cur.lastrowid
        db.executemany("INSERT INTO raw_xml VALUES (?,?,?)", [(poll_id, f, x) for f, x in pages.items()])
        for r in parse_downstream(pages[FUN_DS_QAM], pages[FUN_DS_CODEWORDS], pages[FUN_DS_OFDM]):
            db.execute("INSERT INTO downstream VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (poll_id, r["kind"], r["chid"], r["locked"], r["modulation"], r["freq_hz"],
                        r["power_dbmv"], r["snr_db"], r["unerrored"], r["corrected"], r["uncorrectable"]))
        for r in parse_upstream(pages[FUN_US_QAM], pages[FUN_US_OFDMA]):
            db.execute("INSERT INTO upstream VALUES (?,?,?,?,?,?,?,?)",
                       (poll_id, r["kind"], r["chid"], r["locked"], r["modulation"], r["freq_hz"],
                        r["width_hz"], r["power_dbmv"]))
        new_events = 0
        for e in parse_eventlog(pages[FUN_EVENTLOG]):
            cur = db.execute("INSERT OR IGNORE INTO events (time_raw, time, priority, text, first_seen_poll, first_seen_ts)"
                             " VALUES (?,?,?,?,?,?)",
                             (e["time_raw"], _iso(e["time"]), e["priority"], e["text"], poll_id, _iso(ts)))
            new_events += cur.rowcount
        db.commit()
        print(f"{ts} modem: {g['cm_status']}, uptime {g['uptime_raw']}, {new_events} new log entries")
    except Exception as exc:
        db.execute("INSERT INTO polls (ts, ok, error, trigger) VALUES (?,0,?,?)",
                   (_iso(ts), f"{type(exc).__name__}: {exc}"[:500], trigger))
        db.commit()
        print(f"{ts} modem poll failed: {exc}", file=sys.stderr)

    if not no_tests:
        record_pings(cfg, db, "full run")
        s = run_speedtest(cfg)
        db.execute("INSERT INTO speedtests (ts, server, down_mbps, up_mbps, latency_ms, down_bytes, up_bytes, error,"
                   " trigger) VALUES (?,?,?,?,?,?,?,?,?)",
                   (_iso(now_local()), s["server"], s["down_mbps"], s["up_mbps"], s["latency_ms"],
                    s["down_bytes"], s["up_bytes"], s["error"], trigger))
        db.commit()
        print(f"  speed: {s['down_mbps']} down / {s['up_mbps']} up Mbps, latency {s['latency_ms']} ms"
              + (f" ({s['error']})" if s["error"] else ""))


def cmd_ping(cfg, args):
    """Light check (no modem login). Starts a full poll when the line looks unhealthy."""
    t = cfg["tests"]
    db = open_db(cfg)
    with run_lock(cfg):
        print(f"{now_local()} ping test")
        results = record_pings(cfg, db, "5-min ping")
    bad = [p for p in results if p["received"] < p["sent"] or p["error"]
           or (p["rtt_avg"] or 0) > t.getfloat("ping_trigger_ms")]
    if not bad:
        return
    last = db.execute("SELECT MAX(ts) FROM polls").fetchone()[0]
    since = (now_local() - dt.datetime.fromisoformat(last)).total_seconds() / 60 if last else None
    if since is not None and since < t.getfloat("trigger_cooldown_min"):
        print(f"  ping looks unhealthy, but a full run already happened {since:.0f} min ago")
        return
    p = bad[0]
    why = (f"ping {p['target']}: {p['received']}/{p['sent']} received"
           + (f", avg {p['rtt_avg']:.0f} ms" if p["rtt_avg"] is not None else ""))
    print(f"  starting full run ({why})")
    with run_lock(cfg):
        _poll(cfg, False, why)


def cmd_note(cfg, args):
    db = open_db(cfg)
    at = dt.datetime.fromisoformat(args.at) if args.at else now_local()
    if at.tzinfo is None:
        at = at.astimezone()
    db.execute("INSERT INTO notes (ts, text, added) VALUES (?,?,?)", (_iso(at.replace(microsecond=0)), args.text, _iso(now_local())))
    db.commit()
    print(f"note added at {at}: {args.text}")


def cmd_check(cfg, args):
    m = cfg["modem"]
    modem = Modem(m["host"], m["username"], m["password"], m.get("interface", ""))
    pages = modem.fetch_all()
    g = parse_global(pages[FUN_GLOBAL])
    print(f"{g['hw_model']} hw {g['hw_version']} fw {g['sw_version']}")
    print(f"status {g['cm_status']} / {g['cm_connected']}, uptime {g['uptime_raw']}, modem clock {g['modem_time_raw']}")
    ds = parse_downstream(pages[FUN_DS_QAM], pages[FUN_DS_CODEWORDS], pages[FUN_DS_OFDM])
    us = parse_upstream(pages[FUN_US_QAM], pages[FUN_US_OFDMA])
    print(f"{len(ds)} downstream, {len(us)} upstream channels; upstream power "
          + ", ".join(f"ch{u['chid']} {u['power_dbmv']}" for u in us))
    ev = parse_eventlog(pages[FUN_EVENTLOG])
    t3 = sum("T3 time-out" in e["text"] for e in ev)
    print(f"event log: {len(ev)} entries, {t3} T3 time-outs")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=os.environ.get("MODEM_CHECK_CONFIG", DEFAULT_CONFIG))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("poll")
    p.add_argument("--no-tests", action="store_true", help="only read the modem; skip ping and speed test")
    p.add_argument("--trigger", default="manual", help="why this run happened (recorded with the results)")
    sub.add_parser("ping")
    p = sub.add_parser("note")
    p.add_argument("text")
    p.add_argument("--at", help="when it happened, e.g. '2026-10-08 14:37' (default: now)")
    p = sub.add_parser("report")
    p.add_argument("--since", help="start date/time (default: first record)")
    p.add_argument("--until", help="end date/time (default: now)")
    p.add_argument("-o", "--output", help="output .html path")
    p.add_argument("--pdf", action="store_true", help="also write a PDF next to the HTML (needs Chrome/Chromium)")
    sub.add_parser("check")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.cmd == "report":
        from report import cmd_report
        cmd_report(cfg, args)
    else:
        {"poll": cmd_poll, "ping": cmd_ping, "note": cmd_note, "check": cmd_check}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
