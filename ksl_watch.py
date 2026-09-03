#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ksl_watch.py -- Tixplus Trade (チケプラトレード) resale watcher.

Polls a tour page on https://trade.tixplus.jp and alerts when tickets matching
your requirement (e.g. a 2-consecutive-seat lot) become listed for trade.

Stdlib only -- runs on any NAS with Python 3.8+. No pip install required.

Usage:
    python3 ksl_watch.py --once            # single check (for cron)
    python3 ksl_watch.py --daemon          # built-in loop (for systemd/Docker)
    python3 ksl_watch.py --print           # show current inventory, no alerts
    python3 ksl_watch.py --test-notify     # verify notification channels
"""

import argparse
import base64
import gzip
import html
import http.cookiejar as http_cookiejar
import json
import os
import random
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

JST = timezone(timedelta(hours=9))
BASE = "https://trade.tixplus.jp"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Seat names and prices are Japanese. NAS cron jobs frequently run under
# LC_ALL=C (and Windows consoles under a legacy code page), where printing them
# would raise UnicodeEncodeError and kill the poll. Force UTF-8 on our streams.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def _env(name, default=None):
    # .strip() matters: config.env is usually edited on Windows and copied to
    # the NAS, so values arrive with a trailing \r that would corrupt URLs.
    v = os.environ.get(name)
    v = v.strip() if isinstance(v, str) else v
    return v if v not in (None, "") else default


def _env_int(name, default):
    try:
        return int(_env(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name, default=False):
    v = _env(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self, args):
        self.tour_ids = [t.strip() for t in
                         (args.tours or _env("KSL_TOURS", "14584")).split(",")
                         if t.strip()]
        self.want = args.want or _env_int("KSL_WANT", 2)
        self.mode = args.mode or _env("KSL_MODE", "serial")   # serial|total|any
        # Sweep pacing. A fixed 60.000s heartbeat is the single most obvious
        # bot signal there is, so the gap is drawn uniformly from a band that
        # still averages ~1 minute. Same idea as aa_tracker's CYCLE_PAUSE.
        base = args.interval or _env_int("KSL_INTERVAL", 60)
        self.interval_min = _env_int("KSL_INTERVAL_MIN", max(20, int(base * 0.8)))
        self.interval_max = _env_int("KSL_INTERVAL_MAX", max(self.interval_min + 5,
                                                             int(base * 1.25)))
        self.interval = base
        # Occasionally idle longer, the way a person who wandered off would.
        try:
            self.long_pause_chance = float(_env("KSL_LONG_PAUSE_CHANCE", "0.05"))
        except ValueError:
            self.long_pause_chance = 0.05
        self.long_pause_max = _env_int("KSL_LONG_PAUSE_MAX", 150)
        # If tixplus pushes back (403/429/503), stop hammering: pause, then
        # double the pause on each further strike, capped.
        self.ban_pause = _env_int("KSL_BAN_PAUSE_SEC", 600)
        self.ban_backoff_max = _env_int("KSL_BAN_BACKOFF_MAX_SEC", 7200)
        # spread cron-driven (--once) sweeps across the minute
        self.stagger_max = _env_int("KSL_STAGGER_MAX", 45)
        # how late a scheduled sweep may be before --healthcheck fails
        self.health_grace = _env_int("KSL_HEALTH_GRACE_SEC", 120)
        # delivery attempts per notification channel before giving up
        self.notify_retries = max(1, _env_int("KSL_NOTIFY_RETRIES", 3))
        # passive "still alive" push every N hours (0 = off). For an
        # unattended three-week run, silence is ambiguous: nothing listed
        # and process dead look identical from the outside.
        self.heartbeat_hours = _env_int("KSL_HEARTBEAT_HOURS", 24)
        self.state_file = args.state or _env(
            "KSL_STATE",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"))
        self.cookie_file = _env("KSL_COOKIES",
                                os.path.splitext(self.state_file)[0] + ".cookies")
        self.log_file = _env("KSL_LOG")
        self.timeout = _env_int("KSL_TIMEOUT", 20)
        # Re-nag every N minutes while a match is still listed. 0 (default)
        # = ring once per episode; the alarm re-arms only when the listing
        # goes away, so a standing listing never nags.
        self.repeat_min = _env_int("KSL_REPEAT_MIN", 0)
        # alert after this many consecutive failed polls
        self.fail_alert_after = _env_int("KSL_FAIL_ALERT_AFTER", 10)
        # hours-before-deadline warnings
        self.deadline_warn_hours = [
            int(h) for h in _env("KSL_DEADLINE_WARN", "72,24,3").split(",")
            if h.strip().isdigit()]
        # also alert on listings that appear but are below `want`
        self.notify_low = _env_bool("KSL_NOTIFY_LOW", True)
        self.dry_run = args.dry_run


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

_LOG_PATH = None
_LOG_BROKEN = False


def log(msg):
    """Log to stdout always, and to KSL_LOG when it is writable.

    stdout is the reliable channel (`docker compose logs`); the file is the
    convenience one. If the file cannot be written -- a NAS bind mount owned by
    another user is the usual reason -- say so once on stdout instead of
    failing silently, because "there is no log" with no explanation is a
    genuinely confusing way to lose an afternoon.
    """
    global _LOG_BROKEN
    line = "[%s] %s" % (datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"), msg)
    print(line, flush=True)
    if not _LOG_PATH or _LOG_BROKEN:
        return
    try:
        d = os.path.dirname(os.path.abspath(_LOG_PATH))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as e:
        _LOG_BROKEN = True          # warn once, then stop retrying every line
        print("[%s] WARNING: cannot write log file %s (%s) -- continuing with "
              "stdout only. Check the directory exists and is writable by the "
              "container (on a NAS this is usually bind-mount ownership)."
              % (datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
                 _LOG_PATH, e), flush=True)


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

class RateLimited(Exception):
    """The site pushed back (429/403/503). Caller should back off hard."""


# One opener with a persistent cookie jar for the whole process. A real browser
# keeps its session cookie (tixplus sets `babel_session`); throwing it away and
# arriving cookie-less every single minute is exactly what a scraper looks like.
_JAR = http_cookiejar.LWPCookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))


def load_cookies(path):
    _JAR.filename = path
    try:
        _JAR.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http_cookiejar.LoadError):
        pass


def save_cookies():
    if not _JAR.filename:
        return
    try:
        _JAR.save(ignore_discard=True, ignore_expires=True)
    except OSError:
        pass


def http(url, headers=None, data=None, timeout=20, method=None, use_cookies=True):
    """Return (status, headers, body_bytes). Does not raise on ordinary 4xx/5xx."""
    hdrs = {
        "User-Agent": UA,
        "Accept-Language": "ja,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    opener = _OPENER if use_cookies else urllib.request.build_opener()
    try:
        resp = opener.open(req, timeout=timeout)
        status, rh, raw = resp.getcode(), dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        status, rh, raw = e.code, dict(e.headers), e.read()
    if status in (403, 429, 503):
        raise RateLimited("HTTP %s from %s (retry-after: %s)"
                          % (status, url, rh.get("Retry-After", "n/a")))
    enc = (rh.get("Content-Encoding") or "").lower()
    try:
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        pass
    return status, rh, raw


# --------------------------------------------------------------------------
# tixplus scraping (Inertia.js app -- state lives in data-page / X-Inertia JSON)
# --------------------------------------------------------------------------

_DATA_PAGE_RE = re.compile(r'data-page="(.*?)"\s*>', re.S)


def fetch_full(tour_id, cfg):
    """Fetch the HTML page and parse the embedded Inertia payload."""
    url = "%s/artists/tour/%s" % (BASE, tour_id)
    status, _, body = http(url, timeout=cfg.timeout)
    if status != 200:
        raise RuntimeError("HTTP %s on full page %s" % (status, url))
    text = body.decode("utf-8", "replace")
    m = _DATA_PAGE_RE.search(text)
    if not m:
        raise RuntimeError("data-page payload not found (page layout changed?)")
    return json.loads(html.unescape(m.group(1)))


def fetch_partial(tour_id, version, cfg):
    """Light ~6KB Inertia partial reload. None means 'refetch the full page'."""
    url = "%s/artists/tour/%s" % (BASE, tour_id)
    status, rh, body = http(url, timeout=cfg.timeout, headers={
        "X-Inertia": "true",
        "X-Inertia-Version": version,
        "X-Inertia-Partial-Component": "web/tour/Index",
        "X-Inertia-Partial-Data": "concertList,allTradeEnd,tourId",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "text/html, application/xhtml+xml",
        "Referer": url,          # the real SPA reloads from the page itself
    })
    if status == 409:            # asset version changed -> caller refetches full page
        return None
    if status != 200:
        raise RuntimeError("HTTP %s on partial %s" % (status, url))
    if "json" not in (rh.get("Content-Type") or ""):
        return None
    return json.loads(body.decode("utf-8", "replace"))


def fetch_tour(tour_id, state, cfg):
    """Fetch tour payload, preferring the cheap partial endpoint."""
    versions = state.setdefault("versions", {})
    ver = versions.get(str(tour_id))
    if ver:
        payload = fetch_partial(tour_id, ver, cfg)
        if payload is not None:
            return payload
        log("tour %s: Inertia version changed, refetching full page" % tour_id)
    payload = fetch_full(tour_id, cfg)
    if payload.get("version"):
        versions[str(tour_id)] = payload["version"]
    return payload


def parse_inventory(payload, tour_id):
    """
    Flatten the payload into a list of seat-type rows.

    Tixplus semantics (verified against live data):
      trade_info.total  -> number of tickets currently listed for that seat type
      trade_info.serial -> selectable lot sizes, e.g. {"1": true, "2": true} means
                           single tickets AND 2-consecutive-seat lots are offered.
      A seat type with nothing listed has no trade_info key at all.
    """
    props = payload.get("props", {})
    concerts = props.get("concertList") or {}
    rows, meta = [], []
    for ckey, c in concerts.items():
        info = c.get("concert_info") or {}
        base_c = c.get("concert") or {}
        sched = c.get("trade_schedule") or {}
        meta.append({
            "key": "%s|%s" % (tour_id, ckey),
            "venue": base_c.get("title"),
            "place": base_c.get("place"),
            "date": base_c.get("concert_date"),
            "status": c.get("trade_status_label"),
            "trade_total": c.get("trade_total") or 0,
            "is_close": bool(c.get("isClose")),
            "apply_end": sched.get("application_end_at"),
            "apply_start": sched.get("application_start_at"),
        })
        for dkey, mp in (c.get("member_precedes") or {}).items():
            ti = mp.get("trade_info") or {}
            total = int(ti.get("total") or 0)
            serial = sorted(int(k) for k in (ti.get("serial") or {}).keys()
                            if str(k).isdigit())
            mc = mp.get("concert") or {}
            price = (mp.get("ticket_price") or {}).get("trade_price")
            rows.append({
                "id": "%s|%s|%s" % (tour_id, ckey, dkey),
                "tour_id": str(tour_id),
                "seat_type": mc.get("seat_type") or base_c.get("seat_type") or "?",
                "venue": mc.get("title") or base_c.get("title"),
                "place": mc.get("place") or base_c.get("place"),
                "date": mc.get("concert_date") or base_c.get("concert_date"),
                "week": info.get("week"),
                "open_time": (mc.get("op_time") or "")[:5],
                "start_time": (mc.get("st_time") or "")[:5],
                "price": price,
                "total": total,
                "serial": serial,
                "trade_type": mp.get("trade_type_label"),
                "status": c.get("trade_status_label"),
                "apply_end": sched.get("application_end_at"),
            })
    return rows, meta


# --------------------------------------------------------------------------
# matching / formatting
# --------------------------------------------------------------------------

def matches(row, cfg):
    """Does this seat type satisfy the requirement?"""
    if cfg.mode == "total":
        return row["total"] >= cfg.want
    if cfg.mode == "any":
        return row["total"] >= 1
    # default "serial": need a single lot of >= want consecutive seats
    return any(s >= cfg.want for s in row["serial"])


def fmt_row(row):
    price = "¥{:,}".format(row["price"]) if row.get("price") else "?"
    serial = "/".join("%d枚" % s for s in row["serial"]) or "-"
    return ("{seat}  {price}  在庫{total}枚  "
            "申込可能枚数:{serial}\n"
            "    {date}({week}) {venue} {place}  "
            "開場{op} 開演{st}  [{tt}]").format(
        seat=row["seat_type"], price=price, total=row["total"], serial=serial,
        date=row["date"], week=row.get("week") or "", venue=row["venue"],
        place=row["place"], op=row.get("open_time") or "?",
        st=row.get("start_time") or "?", tt=row.get("trade_type") or "?")


def parse_jst(s):
    if not s:
        return None
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, f).replace(tzinfo=JST)
        except ValueError:
            continue
    return None


def parse_jst_iso(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=JST)
    except (ValueError, TypeError):
        return None


def next_draw(now, apply_end):
    """Next daily 12:00 JST lottery draw that can still include a new entry."""
    draw = now.replace(hour=12, minute=0, second=0, microsecond=0)
    if now >= draw:
        draw += timedelta(days=1)
    if apply_end and draw > apply_end + timedelta(hours=1):
        return None
    return draw


def deadline_line(apply_end):
    now = datetime.now(JST)
    end = parse_jst(apply_end)
    out = []
    nd = next_draw(now, end)
    if nd:
        mins = int((nd - now).total_seconds() // 60)
        out.append("次の抽選 / next draw: %s  (in %dh%02dm)"
                   % (nd.strftime("%m/%d %H:%M"), mins // 60, mins % 60))
    if end:
        left = end - now
        if left.total_seconds() > 0:
            out.append("申込締切 / apply deadline: %s  (%dd %dh left)"
                       % (end.strftime("%Y-%m-%d %H:%M"), left.days, left.seconds // 3600))
        else:
            out.append("申込締切 / apply deadline: %s  (CLOSED)"
                       % end.strftime("%Y-%m-%d %H:%M"))
    return "\n".join(out)


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------

def _post_json(url, obj, timeout=15, headers=None):
    data = json.dumps(obj).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    # use_cookies=False: notification hosts must not touch the tixplus jar.
    status, _, body = http(url, headers=h, data=data, timeout=timeout,
                           method="POST", use_cookies=False)
    if status >= 300:
        raise RuntimeError("HTTP %s: %s" % (status, body[:200].decode("utf-8", "replace")))


def notify_bark(title, body, url, level):
    """Bark (iOS push), same call shape as aa_tracker.push_bark.

    level -> delivery:
      urgent  : alarm sound + timeSensitive banner   (a matching lot -- act now)
      normal  : silent but still a waking banner     (listings, status, deadline)
      passive : Notification Center only, no banner  (baseline, health warnings)
    """
    key = _env("BARK_KEY")
    if not key:
        return False
    server = _env("BARK_SERVER", "https://api.day.app").rstrip("/")
    params = {
        "title": title,
        "body": body,
        "group": _env("BARK_GROUP", "KSL-Trade"),
        "sound": {"urgent": _env("BARK_SOUND", "alarm")}.get(level, "silence"),
        # so repeated pushes about the same seat type collapse instead of stacking
        "isArchive": "1",
    }
    if level == "passive":
        params["level"] = "passive"
    else:
        params["level"] = "timeSensitive"
    if url:
        params["url"] = url
    status, _, resp = http("%s/%s?%s" % (server, key, urllib.parse.urlencode(params)),
                           timeout=15, use_cookies=False)
    if status >= 300:
        raise RuntimeError("bark HTTP %s: %s" % (status, resp[:200]))
    return True


def notify_telegram(title, body, url, level):
    token, chat = _env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    _post_json("https://api.telegram.org/bot%s/sendMessage" % token,
               {"chat_id": chat,
                "text": "%s\n\n%s\n\n%s" % (title, body, url),
                "disable_notification": level == "passive",
                "disable_web_page_preview": False})
    return True


def notify_discord(title, body, url, level):
    hook = _env("DISCORD_WEBHOOK_URL")
    if not hook:
        return False
    _post_json(hook, {"content": "**%s**\n```\n%s\n```\n%s" % (title, body[:1600], url)})
    return True


def _rfc2047(s):
    """ASCII-safe header value; base64 RFC 2047 only when the text needs it."""
    try:
        s.encode("ascii")
        return s
    except UnicodeEncodeError:
        return "=?UTF-8?B?%s?=" % base64.b64encode(s.encode("utf-8")).decode("ascii")


def notify_ntfy(title, body, url, level):
    topic = _env("NTFY_TOPIC")
    if not topic:
        return False
    server = _env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {
        # HTTP headers must be ASCII; ntfy decodes RFC 2047 for non-ASCII titles
        "Title": _rfc2047(title),
        "Priority": {"urgent": "urgent", "passive": "low"}.get(
            level, _env("NTFY_PRIORITY", "high")),
        "Tags": "tickets",
        "Click": url,
        "Markdown": "no",
        "Content-Type": "text/plain; charset=utf-8",
    }
    tok = _env("NTFY_TOKEN")
    if tok:
        headers["Authorization"] = "Bearer %s" % tok
    status, _, resp = http("%s/%s" % (server, topic), headers=headers,
                           data=("%s\n\n%s" % (body, url)).encode("utf-8"),
                           method="POST", timeout=15, use_cookies=False)
    if status >= 300:
        raise RuntimeError("ntfy HTTP %s: %s" % (status, resp[:200]))
    return True


def notify_webhook(title, body, url, level):
    hook = _env("WEBHOOK_URL")
    if not hook:
        return False
    _post_json(hook, {"title": title, "body": body, "url": url, "level": level,
                      "ts": datetime.now(JST).isoformat()})
    return True


def notify_email(title, body, url, level):
    host, to = _env("SMTP_HOST"), _env("SMTP_TO")
    if not host or not to:
        return False
    port = _env_int("SMTP_PORT", 587)
    user, pw = _env("SMTP_USER"), _env("SMTP_PASS")
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = _env("SMTP_FROM", user or "ksl-watch@localhost")
    msg["To"] = to
    msg.set_content("%s\n\n%s" % (body, url))
    if port == 465:
        srv = smtplib.SMTP_SSL(host, port, timeout=20,
                               context=ssl.create_default_context())
    else:
        srv = smtplib.SMTP(host, port, timeout=20)
        if _env_bool("SMTP_TLS", True):
            srv.starttls(context=ssl.create_default_context())
    with srv:
        if user and pw:
            srv.login(user, pw)
        srv.send_message(msg)
    return True


NOTIFIERS = [
    ("bark", notify_bark),
    ("telegram", notify_telegram),
    ("discord", notify_discord),
    ("ntfy", notify_ntfy),
    ("webhook", notify_webhook),
    ("email", notify_email),
]


def _send_once(name, fn, title, body, url, level, attempts):
    """Deliver through one channel, retrying transient failures.

    Without this a single blip -- a dropped connection, a 429 from the push
    service -- silently loses the alert, and the whole point of the watcher is
    the one notification that matters.
    """
    for i in range(attempts):
        try:
            return fn(title, body, url, level)
        except Exception as e:                      # noqa: BLE001
            if i == attempts - 1:
                raise
            log("notify %s attempt %d/%d failed (%s) -- retrying"
                % (name, i + 1, attempts, e))
            time.sleep(2 * (i + 1) + random.uniform(0, 1))


def notify(title, body, url, cfg, level="normal"):
    log("ALERT [%s]: %s" % (level, title))
    log(body)
    if cfg.dry_run:
        log("(dry-run: notifications suppressed)")
        return
    sent, failed = [], []
    for name, fn in NOTIFIERS:
        try:
            if _send_once(name, fn, title, body, url, level, cfg.notify_retries):
                sent.append(name)
        except Exception as e:                      # noqa: BLE001
            failed.append(name)
            log("notify %s FAILED after %d attempts: %s"
                % (name, cfg.notify_retries, e))
    if sent:
        log("notified via: %s" % ", ".join(sent))
    elif failed:
        # Distinct from "nothing configured" on purpose -- conflating the two
        # sends you hunting for a missing config when the key is fine and
        # delivery is what broke.
        log("ERROR: alert NOT delivered -- every configured channel failed (%s)"
            % ", ".join(failed))
    else:
        log("WARNING: no notification channel configured -- alert only in log")


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    """Persist state. Never fatal: a watcher that still alerts beats one that dies.

    If this fails under cron (`--once`), every poll looks like a cold start and
    you will get repeat alerts -- which is loud, but visible, and the log says
    exactly why. In `--daemon` the in-memory state still de-duplicates.
    """
    try:
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        # `_`-prefixed keys are per-sweep signalling (e.g. _rate_limited) and
        # must not outlive the process that set them.
        persist = {k: v for k, v in state.items() if not k.startswith("_")}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(persist, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        log("ERROR: cannot write state file %s (%s) -- alerts may repeat. "
            "Point KSL_STATE at a writable path." % (path, e))


# --------------------------------------------------------------------------
# one polling cycle
# --------------------------------------------------------------------------

def check(cfg, state, announce=True):
    cold = "seats" not in state          # first ever run / state file wiped
    seats_prev = state.setdefault("seats", {})
    conc_prev = state.setdefault("concerts", {})
    now = datetime.now(JST)
    now_iso = now.isoformat()

    all_rows, all_meta, errors = [], [], []
    ok_tours = set()
    limited = False
    for i, tid in enumerate(cfg.tour_ids):
        if i:
            # Don't fire several tours off back-to-back in the same millisecond.
            time.sleep(random.uniform(1.5, 5.0))
        try:
            payload = fetch_tour(tid, state, cfg)
            rows, meta = parse_inventory(payload, tid)
            all_rows.extend(rows)
            all_meta.extend(meta)
            ok_tours.add(str(tid))
        except RateLimited as e:
            limited = True
            errors.append("tour %s: %s" % (tid, e))
        except Exception as e:                       # noqa: BLE001
            errors.append("tour %s: %s" % (tid, e))
    if limited:
        state["_rate_limited"] = True

    if errors and not all_rows:
        state["fail_streak"] = int(state.get("fail_streak", 0)) + 1
        log("poll FAILED (streak %d): %s" % (state["fail_streak"], "; ".join(errors)))
        if (cfg.fail_alert_after and announce and
                state["fail_streak"] == cfg.fail_alert_after):
            notify("KSL watcher: %d consecutive failures" % state["fail_streak"],
                   "\n".join(errors) +
                   "\n\nThe monitor cannot read the page. Check the NAS network, "
                   "or whether the site layout changed.",
                   "%s/artists/tour/%s" % (BASE, cfg.tour_ids[0]), cfg,
                   level="passive")
        return state

    if state.get("fail_streak"):
        log("recovered after %s failed poll(s)" % state["fail_streak"])
    state["fail_streak"] = 0

    hits, news, events = [], [], []
    for row in all_rows:
        rid = row["id"]
        prev = seats_prev.get(rid)
        hit = matches(row, cfg)
        if hit:
            hits.append(row)

        if prev is None:
            if row["total"] > 0:
                events.append(("NEW", row, "new listing appeared"))
                news.append(row)
        else:
            p_total = prev.get("total", 0)
            p_serial = prev.get("serial", [])
            p_hit = prev.get("hit", False)
            if hit and not p_hit:
                events.append(("HIT", row, "now matches your requirement"))
            elif row["total"] > p_total:
                events.append(("UP", row, "stock increased %d -> %d"
                               % (p_total, row["total"])))
                if not hit:
                    news.append(row)
            elif row["serial"] != p_serial and row["total"] > 0:
                events.append(("SERIAL", row, "lot sizes changed %s -> %s"
                               % (p_serial, row["serial"])))
            elif p_total > 0 and row["total"] == 0:
                events.append(("GONE", row, "stock gone (taken or withdrawn)"))

        seats_prev[rid] = {
            "total": row["total"], "serial": row["serial"], "hit": hit,
            # venue/date kept so a delisting can still be described after the
            # row itself disappears from the payload
            "seat_type": row["seat_type"], "venue": row["venue"],
            "date": row["date"], "seen": now_iso,
            # Clearing the latch the moment it stops matching is what arms the
            # next alarm. Carried over only while it still matches.
            "last_alert": ((prev.get("last_alert") if prev else None)
                           if hit else None),
        }

    # Seat types that vanished from the payload entirely.
    #
    # tixplus does NOT report a sold-out seat type as `total: 0` -- it drops the
    # member_precedes entry altogether. So a row that simply stops appearing is
    # how "the ticket is gone" actually looks on the wire. Without this pass the
    # loop above never visits it, which means (a) no gone alert, and worse
    # (b) seats_prev keeps the stale total, so if the same listing comes BACK
    # unchanged the comparison is 1 > 1 and the reappearance is silent.
    #
    # Scoped to tours that fetched cleanly: a failed request must never be read
    # as "everything sold out".
    seen_ids = {r["id"] for r in all_rows}
    for rid, prev in list(seats_prev.items()):
        if rid in seen_ids or rid.split("|")[0] not in ok_tours:
            continue
        if prev.get("total", 0) > 0:
            # No row object to format -- it is gone from the payload -- so this
            # event carries its own prose instead of a formatted row.
            where = " ".join(x for x in (prev.get("date"), prev.get("venue"))
                             if x)
            events.append(("GONE", None, "%s%s: 在庫がなくなりました / delisted "
                           "(%d枚 -> 0, sold out or withdrawn)"
                           % (prev.get("seat_type", "?"),
                              " (%s)" % where if where else "",
                              prev.get("total", 0))))
        # last_alert=None re-arms the alarm: if this listing comes back, it rings.
        seats_prev[rid] = dict(prev, total=0, serial=[], hit=False,
                               last_alert=None, seen=now_iso)

    for m in all_meta:
        prev = conc_prev.get(m["key"]) or {}
        if prev.get("status") and prev["status"] != m["status"]:
            events.append(("STATUS", None, "%s %s: trade status %s -> %s"
                           % (m["date"], m["venue"], prev["status"], m["status"])))
        conc_prev[m["key"]] = {"status": m["status"], "trade_total": m["trade_total"]}

    url = "%s/artists/tour/%s" % (BASE, cfg.tour_ids[0])
    apply_end = next((m["apply_end"] for m in all_meta if m.get("apply_end")), None)
    listed = [r for r in all_rows if r["total"] > 0]
    stock_line = ("current inventory:\n" +
                  ("\n".join("  " + fmt_row(r) for r in listed) or "  (none listed)"))
    log("poll ok: %d seat type(s), %d listed, %d matching"
        % (len(all_rows), len(listed), len(hits)))

    if not announce:
        state["last_ok"] = now_iso
        return state

    # ---- primary alert: requirement satisfied ------------------------------
    if hits:
        # One alarm per episode. `last_alert` is the latch: it is set when we
        # ring, and cleared the moment the seat type stops matching (below, and
        # in the delisting pass). So a listing that stays up does not nag, a
        # listing that grows 2 -> 3 does not re-ring, and a listing that goes
        # away and comes back rings again -- which is the case that matters.
        # KSL_REPEAT_MIN > 0 opts back into periodic re-nagging.
        due = []
        for r in hits:
            last = parse_jst_iso(seats_prev[r["id"]].get("last_alert"))
            if last is None:
                due.append(r)
            elif cfg.repeat_min and (now - last) >= timedelta(
                    minutes=cfg.repeat_min):
                due.append(r)
        if due:
            title = ("KSL 2026 Tokyo: %d ticket%s available -- APPLY NOW"
                     % (cfg.want, "" if cfg.want == 1 else "s"))
            body = ("Matching listing found (mode=%s, want=%d)\n\n%s\n\n%s\n\n%s"
                    % (cfg.mode, cfg.want,
                       "\n".join(fmt_row(r) for r in due),
                       deadline_line(apply_end),
                       "Apply once -- the entry automatically rolls into every "
                       "daily 12:00 JST draw until you win or trading ends."))
            notify(title, body, url, cfg, level="urgent")
            for r in due:
                seats_prev[r["id"]]["last_alert"] = now_iso

    # ---- secondary alert: listed, but below the requirement ----------------
    #
    # Deliberately NOT `elif`. A matching seat type that is sitting inside its
    # repeat window produces no alert this sweep, and an `elif` would let that
    # silence swallow a genuinely new listing on a *different* seat type. The
    # matching rows are filtered out instead, so nothing is reported twice.
    # ---- startup confirmation ----------------------------------------------
    # Unconditional, including when nothing is listed. Deploying to a NAS with
    # an empty pool otherwise produces total silence, which is indistinguishable
    # from a broken deploy -- and on Synology the GUI log tab often shows
    # nothing for compose projects, so there is no easy second opinion.
    if cold:
        notify("KSL watcher started",
               "Watching for %d seat(s) together (mode=%s), sweeping every "
               "%d-%ds.\n\n%s\n\n%s"
               % (cfg.want, cfg.mode, cfg.interval_min, cfg.interval_max,
                  stock_line, deadline_line(apply_end)),
               url, cfg, level="passive")

    news_low = [r for r in news if not matches(r, cfg)]
    if news_low and cfg.notify_low and not cold:
        notify("KSL 2026 Tokyo: listing appeared (below your target)",
               "%s\n\n%s\n\n%s" % (
                   "\n".join(fmt_row(r) for r in news_low),
                   deadline_line(apply_end),
                   "Not enough for %d seats together. Decide whether to apply for "
                   "singles or keep waiting for a %d-seat lot." % (cfg.want, cfg.want)),
               url, cfg, level="passive" if cold else "normal")

    # ---- status / disappearance events -------------------------------------
    other = [e for e in events if e[0] in ("GONE", "STATUS")]
    if other and not hits:
        body = "\n".join(("%s\n    %s" % (fmt_row(e[1]), e[2])) if e[1] else e[2]
                         for e in other)
        notify("KSL 2026 Tokyo: status change", body + "\n\n" + stock_line,
               url, cfg, level="normal")

    # ---- deadline countdown -------------------------------------------------
    end = parse_jst(apply_end)
    warned = state.setdefault("deadline_warned", [])
    if end:
        left_h = (end - now).total_seconds() / 3600.0
        # Every threshold we are now inside of. A watcher started (or a state
        # file wiped) late is under several at once -- mark them all, report the
        # ACTUAL time left, and pick the level from that. Keying the title or
        # the level off the threshold instead would announce "~72h" with four
        # hours to go, and send the final-chance warning silently.
        crossed = [h for h in cfg.deadline_warn_hours
                   if 0 < left_h <= h and h not in warned]
        if crossed:
            warned.extend(crossed)
            notify("KSL 2026 Tokyo: apply deadline in ~%dh" % max(1, round(left_h)),
                   "%s\n\n%s\n\nDo not miss the final draw." % (
                       deadline_line(apply_end), stock_line),
                   url, cfg, level="urgent" if left_h <= 6 else "normal")

    # ---- periodic proof of life ---------------------------------------
    if cfg.heartbeat_hours > 0:
        last_hb = parse_jst_iso(state.get("last_heartbeat"))
        if last_hb is None or (now - last_hb) >= timedelta(
                hours=cfg.heartbeat_hours):
            if last_hb is not None:      # never on the very first sweep
                notify("KSL watcher: still running",
                       "%s\n\n%s" % (stock_line, deadline_line(apply_end)),
                       url, cfg, level="passive")
            state["last_heartbeat"] = now_iso

    state["last_ok"] = now_iso
    state["last_summary"] = [{"seat": r["seat_type"], "total": r["total"],
                              "serial": r["serial"]} for r in listed]
    return state


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def pick_pause(cfg):
    """Choose a randomised gap instead of a metronomic one.

    A request landing on the same wall-clock second every minute for three weeks
    is trivially fingerprintable. This keeps the average near your configured
    interval while making the sequence irregular, and occasionally steps away
    for longer the way a person watching a page actually would.
    """
    gap = random.uniform(cfg.interval_min, cfg.interval_max)
    if random.random() < cfg.long_pause_chance:
        extra = random.uniform(cfg.interval_max, max(cfg.interval_max + 1,
                                                     cfg.long_pause_max))
        log("taking a longer break this round: +%.0fs" % extra)
        gap += extra
    log("next sweep in %.0fs" % gap)
    return gap


def _ago(delta):
    s = int(abs(delta.total_seconds()))
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm%02ds" % (s // 60, s % 60)
    if s < 86400:
        return "%dh%02dm" % (s // 3600, (s % 3600) // 60)
    return "%dd%02dh" % (s // 86400, (s % 86400) // 3600)


def cmd_status(cfg):
    """Human answer to 'is it actually running?', read from the state file.

    Deliberately independent of the log: logs can be unreadable (unwritable
    mount, wrong compose project, a NAS GUI that hides stdout) while the
    watcher is perfectly healthy, and that combination is indistinguishable
    from being dead unless something reports state directly.
    """
    state = load_state(cfg.state_file)
    now = datetime.now(JST)
    print("state file : %s" % cfg.state_file)
    if not state:
        print("status     : NO STATE -- no sweep has completed yet")
        print("             (fresh start? wrong KSL_STATE path? check the logs)")
        return 1

    last = parse_jst_iso(state.get("last_ok"))
    due = parse_jst_iso(state.get("next_due"))
    if last:
        print("last sweep : %s  (%s ago)"
              % (last.strftime("%Y-%m-%d %H:%M:%S"), _ago(now - last)))
    if due:
        d = due - now
        print("next sweep : %s  (%s %s)"
              % (due.strftime("%Y-%m-%d %H:%M:%S"), _ago(d),
                 "from now" if d.total_seconds() > 0 else "OVERDUE"))
        late = (now - due).total_seconds()
        print("status     : %s" % ("RUNNING" if late <= cfg.health_grace
                                   else "STALLED (%s overdue)" % _ago(d)))
    else:
        print("status     : ran in --once mode (no daemon schedule recorded)")

    if state.get("fail_streak"):
        print("failures   : %s consecutive failed sweeps" % state["fail_streak"])
    summary = state.get("last_summary") or []
    if summary:
        print("inventory  :")
        for r in summary:
            print("   %s  在庫%d枚  申込可能枚数:%s"
                  % (r.get("seat"), r.get("total", 0),
                     "/".join("%d枚" % s for s in r.get("serial") or []) or "-"))
    else:
        print("inventory  : nothing listed for trade right now")
    print("watching   : tours=%s want=%d mode=%s"
          % (",".join(cfg.tour_ids), cfg.want, cfg.mode))
    return 0


def cmd_healthcheck(cfg):
    """Exit 0 if the daemon is on schedule, 1 if it has stalled.

    Compares now against `next_due`, the time the daemon itself said the next
    sweep was coming. That is deliberately not a fixed staleness threshold: a
    deliberate rate-limit backoff can legitimately be hours long, and a
    healthcheck that tripped during one would restart the container and reset
    the very backoff that was protecting us.
    """
    state = load_state(cfg.state_file)
    due = parse_jst_iso(state.get("next_due"))
    if due is None:
        # No daemon has recorded a schedule yet (fresh start, or --once mode).
        last = parse_jst_iso(state.get("last_ok"))
        if last is None:
            print("no state yet")
            return 1
        print("last_ok %s (no schedule recorded)" % last.strftime("%H:%M:%S"))
        return 0
    late = (datetime.now(JST) - due).total_seconds()
    if late > cfg.health_grace:
        print("STALLED: sweep was due %s, %.0fs overdue"
              % (due.strftime("%Y-%m-%d %H:%M:%S"), late))
        return 1
    print("ok: next sweep due %s" % due.strftime("%H:%M:%S"))
    return 0


def cmd_print(cfg, state):
    for tid in cfg.tour_ids:
        payload = fetch_tour(tid, state, cfg)
        rows, meta = parse_inventory(payload, tid)
        print("=" * 74)
        print("TOUR %s" % tid)
        for m in meta:
            print("  %s %s %s -- %s (trade_total=%s)"
                  % (m["date"], m["place"], m["venue"], m["status"], m["trade_total"]))
        print("-" * 74)
        for r in rows:
            print("  %s%s" % (fmt_row(r), "   <<< MATCH" if matches(r, cfg) else ""))
        if not any(r["total"] > 0 for r in rows):
            print("  (no tickets currently listed for trade)")
        print("-" * 74)
        print(deadline_line(next((m["apply_end"] for m in meta if m.get("apply_end")),
                                 None)))
    save_state(cfg.state_file, state)


def main():
    ap = argparse.ArgumentParser(description="Tixplus Trade resale watcher")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="single check then exit (cron)")
    g.add_argument("--daemon", action="store_true", help="loop forever")
    g.add_argument("--print", dest="show", action="store_true", help="dump inventory")
    g.add_argument("--test-notify", action="store_true", help="send a test alert")
    g.add_argument("--status", action="store_true",
                   help="print whether the watcher is alive and what it sees")
    g.add_argument("--healthcheck", action="store_true",
                   help="exit 0 if the daemon is on schedule, 1 if stalled")
    ap.add_argument("--tours", help="comma-separated tour ids (default 14584)")
    ap.add_argument("--want", type=int, help="tickets needed together (default 2)")
    ap.add_argument("--mode", choices=["serial", "total", "any"],
                    help="serial=one lot of N consecutive (default), "
                         "total=N in stock, any=anything at all")
    ap.add_argument("--interval", type=int,
                    help="seconds between polls in --daemon (default 60)")
    ap.add_argument("--state", help="path to the state file")
    ap.add_argument("--no-stagger", action="store_true",
                    help="skip the randomised pre-sweep delay in --once mode")
    ap.add_argument("--dry-run", action="store_true", help="log alerts, do not send")
    args = ap.parse_args()

    cfg = Config(args)
    global _LOG_PATH
    _LOG_PATH = cfg.log_file

    if args.test_notify:
        notify("KSL watcher test",
               "If you can read this, notifications work.\n"
               "tours=%s want=%d mode=%s interval=%ds"
               % (",".join(cfg.tour_ids), cfg.want, cfg.mode, cfg.interval),
               "%s/artists/tour/%s" % (BASE, cfg.tour_ids[0]), cfg,
               level="normal")
        return 0

    if args.status:
        return cmd_status(cfg)

    if args.healthcheck:
        return cmd_healthcheck(cfg)

    state = load_state(cfg.state_file)
    load_cookies(cfg.cookie_file)

    if args.show:
        cmd_print(cfg, state)
        return 0

    if args.daemon:
        log("daemon start: tours=%s want=%d mode=%s sweep=%d-%ds state=%s"
            % (",".join(cfg.tour_ids), cfg.want, cfg.mode,
               cfg.interval_min, cfg.interval_max, cfg.state_file))
        strikes = 0
        while True:
            try:
                state = check(cfg, state, announce=True)
                save_state(cfg.state_file, state)
                save_cookies()
            except Exception as e:                    # noqa: BLE001
                log("unexpected error: %r" % e)

            if state.pop("_rate_limited", False):
                strikes += 1
                pause = min(cfg.ban_backoff_max,
                            cfg.ban_pause * (2 ** (strikes - 1)))
                log("RATE LIMITED (strike %d) -- backing off %.0f min"
                    % (strikes, pause / 60.0))
                if strikes == 1:
                    notify("KSL watcher: rate limited by tixplus",
                           "The site returned 403/429/503. Backing off for "
                           "%.0f minutes and retrying with a longer sweep gap.\n\n"
                           "If this keeps happening, raise KSL_INTERVAL_MIN/MAX."
                           % (pause / 60.0),
                           "%s/artists/tour/%s" % (BASE, cfg.tour_ids[0]),
                           cfg, level="passive")
                pause += random.uniform(0, 30)
                # A long backoff is healthy behaviour, so publish it as the
                # schedule too -- otherwise --healthcheck would call it a stall.
                state["next_due"] = (datetime.now(JST)
                                     + timedelta(seconds=pause)).isoformat()
                save_state(cfg.state_file, state)
                time.sleep(pause)
                continue
            if strikes:
                log("rate limit cleared after %d strike(s)" % strikes)
                strikes = 0
            gap = pick_pause(cfg)
            # Publish the schedule so --healthcheck can tell "sleeping normally"
            # apart from "hung", without guessing at a staleness threshold.
            state["next_due"] = (datetime.now(JST)
                                 + timedelta(seconds=gap)).isoformat()
            save_state(cfg.state_file, state)
            time.sleep(gap)

    # default: --once
    # cron fires at exactly :00 every minute, which is a perfectly regular
    # fingerprint. Spread the actual request across the minute instead.
    if cfg.stagger_max > 0 and not args.no_stagger:
        delay = random.uniform(0, cfg.stagger_max)
        log("stagger: waiting %.0fs before the sweep" % delay)
        time.sleep(delay)
    try:
        state = check(cfg, state, announce=True)
    finally:
        save_state(cfg.state_file, state)
        save_cookies()
    return 0


if __name__ == "__main__":
    sys.exit(main())
