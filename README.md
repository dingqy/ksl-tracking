# KSL Live World 2026 (Tokyo) — Tixplus Trade resale watcher

Monitors <https://trade.tixplus.jp/artists/tour/14584> once a minute and alerts you
the moment tickets matching your requirement (2 seats) are listed for trade.

---

## Read this first — how this resale actually works

This is **not** a first-come-first-served resale. Confirmed from the page's own data:

| | |
|---|---|
| Trade type | `抽選` — **lottery** |
| Apply window | **2026-08-26 10:00 → 2026-09-18 11:59 JST** |
| Draw | **every day at 12:00 JST**, repeatedly, until the window closes |
| Roll-over | *"一度申込をしたチケットは、当選しない限り、トレード最終日まで抽選対象となります"* — once you apply, your entry **automatically re-enters every subsequent daily draw** until you win or trading ends. You do not re-apply daily. |
| Result | Winners get an email + a notice on the マイトレード page. Losers are told only after the final draw. |
| Show | 2026-09-26 (Sat), 豊洲PIT 東京, open 16:30 / start 17:30 |
| Payment | Same-day payment deadline after winning |

**What that means for you:** clicking 30 seconds faster does *not* improve your odds
within a given day's draw. What matters is that **your application is in before the
next 12:00 draw**. So the job of this monitor is not to race anyone — it is to tell
you within a minute that something applicable has been listed, so you can get an
entry in before noon.

### Current inventory (checked 2026-08-28)

```
S席  ¥28,600   在庫 1枚   申込可能枚数: 1枚 only
```

Only **one** S-seat is listed, and it is offered as a **single**, so right now you
**cannot** apply for 2 seats together. The other seat types for this show — SS席,
A席 (standing), 車椅子席 — have nothing in the trade pool yet. That is exactly the
condition the watcher exists to catch: it fires when a 2-consecutive-seat (`連番`)
lot appears, or when a new seat type enters the pool.

### One thing to do now, by hand

Applying is a personal, account-bound action (Plus member ID, identity check,
same-day payment) — this script deliberately **does not** log in, apply, or pay on
your behalf. It only watches and alerts. Since entries roll over automatically, the
strongest move is: **apply as soon as anything acceptable appears** and let it ride
every daily draw until 9/18.

---

## What the watcher does

- Polls the tour's Inertia JSON endpoint (~6 KB per check, not the 94 KB HTML page)
- Tracks **every seat type** on the tour, not just the one currently listed
- Alerts on:
  - **MATCH** — a lot of ≥ 2 consecutive seats is available → *apply now*
  - **new listing** below your target (so you can decide on singles)
  - **stock increase** / lot-size change (e.g. `1枚` → `1枚/2枚`)
  - **new seat type** entering the trade pool (SS席 / A席 / 車椅子席)
  - stock disappearing, or trade status changing (トレード中 → トレード終了).
    Note tixplus does **not** report a sold-out seat type as `total: 0` — it
    drops the entry from `member_precedes` altogether, so a delisting is
    inferred from the row vanishing. This also matters for the *reverse*
    case: without it, stale state would silence a listing that reappears
    with identical numbers.
  - **72 h / 24 h / 3 h before the apply deadline**
  - **10 consecutive failed polls** — so a silently broken monitor gets noticed
- **One alarm per listing.** It rings when a match appears, stays quiet for as
  long as that listing is up (even if the stock grows), and re-arms only once
  the listing disappears -- so a ticket that comes back rings again.
  `KSL_REPEAT_MIN=30` opts back into periodic re-nagging.
- **`--ack` once you have applied.** The listing you applied for stays in the
  pool until the draw, and the pool churns, so even one-alarm-per-episode keeps
  finding "new" episodes to ring about. `--ack` records the lot size you applied
  for and goes quiet on it for good -- unless a strictly *larger* lot appears,
  which is the only thing left worth acting on. `--unack` restores alerting.
- Every alert carries the countdown to the next 12:00 draw and to the 9/18 deadline
- Survives the site changing its Inertia asset version (409 → refetches, recovers)
- Python **standard library only** — no `pip install` on the NAS

### Alert tiers (Bark)

Same call shape as `aa_tracker.push_bark` — `GET https://api.day.app/<key>` with
`title` / `body` / `group` / `sound` / `level` / `url`, grouped under `KSL-Trade`.

| Tier | Bark delivery | Used for |
|---|---|---|
| `urgent` | `sound=alarm` + `level=timeSensitive` | **a lot matching your 2 seats**, and the final deadline warning |
| `normal` | silent + `level=timeSensitive` (banner, wakes screen) | a new listing below target, status changes, 72h/24h warnings |
| `passive` | Notification Center only, no banner or sound | cold-start baseline, rate-limit and health notices |

So the only thing that will actually wake you up is the one thing worth waking up
for. Tapping the notification opens the tour page directly.

---

## Not getting the IP banned

`robots.txt` is `Disallow:` (nothing is disallowed) and each sweep costs ~6 KB —
about 8 MB/day — because the watcher uses the Inertia partial-reload endpoint
rather than re-downloading the 94 KB HTML page. On top of that:

| Measure | Why |
|---|---|
| Sweep gap drawn from `[50, 75]`s, not a fixed 60 | A request landing on the same wall-clock second for three weeks is trivially fingerprintable |
| 5 % of sweeps idle an extra 75–150 s | Real people wander off; perfectly uniform traffic does not |
| `--once` waits a random 0–45 s first | cron fires at exactly `:00`, which is the same fingerprint by another route |
| Persistent cookie jar (`state.cookies`) | tixplus issues a `babel_session`; arriving cookie-less every minute is a scraper tell |
| Real browser UA + `Referer` on the partial fetch | Matches what the site's own Vue app sends |
| 403 / 429 / 503 → 10 min pause, doubling per strike, capped at 2 h | Back off instead of digging in; one passive Bark tells you it happened |
| Multiple tours staggered 1.5–5 s apart | No burst of simultaneous requests |

The effective rate stays at roughly one request per minute from one IP — well
inside what a person refreshing a page they care about would generate.

---

## Setup

### 1. Configure

```bash
cp config.env config.env.local   # optional
nano config.env
```

Set `KSL_WANT=2`, `KSL_MODE=serial`, and set your **Bark key**:

```
BARK_KEY=<your key from the Bark iOS app>
BARK_GROUP=KSL-Trade
BARK_SOUND=alarm
```

Telegram, Discord, ntfy, a generic JSON webhook, and SMTP email are also built in
(all commented out in `config.env`) — enabling several just sends to all of them.

### 2. Verify

```bash
python3 ksl_watch.py --print
```

```bash
python3 ksl_watch.py --test-notify
```

The first prints live inventory; the second must land on your phone. **Do not skip
the second one** — a watcher whose alerts go nowhere is worse than no watcher.

### 3. Deploy on the NAS — pick one

<details open>
<summary><b>A. Docker (Synology Container Manager / QNAP Container Station)</b> — recommended</summary>

Copy `ksl_watch.py`, `Dockerfile`, `.dockerignore`, `docker-compose.yml` and
`config.env` to the NAS, then:

```bash
mkdir -p /volume1/docker/ksl-watch/data && cd /volume1/docker/ksl-watch
docker compose up -d --build
```

```bash
docker compose logs -f
```

- **Base image** `python:3.12-alpine` — the script is stdlib-only, so the image
  needs no wheels and stays ~50 MB.
- **State** lives in the bind mount `./data/` (`state.json`, `state.cookies`,
  `ksl.log`), so restarts and rebuilds do not cold-start the watcher or replay
  alerts you have already seen.
- **`restart: unless-stopped`** brings it back with the NAS.
- **Log rotation** is capped at 3 × 10 MB so a three-week run cannot fill the
  volume.
- **No `VOLUME` in the Dockerfile** — on purpose. A declared `VOLUME` makes a
  bare `docker run` mint a fresh anonymous volume on every recreate, which would
  silently reset state and re-alert. The mount is explicit instead.

Check config resolution before the first start — this prints every value Compose
will actually inject, so you can confirm `BARK_KEY` and the pacing landed:

```bash
docker compose config
```

**Healthcheck.** The image ships one:

```bash
docker inspect --format '{{.State.Health.Status}}' ksl-watch
```

It compares now against `next_due` — the time the daemon itself recorded for its
next sweep — rather than using a fixed staleness threshold. That distinction
matters: a deliberate rate-limit backoff can legitimately last hours, and a naive
healthcheck would call that a stall and restart the container, resetting the very
backoff protecting you from a ban.

Note that Docker's `restart` policy does *not* act on `unhealthy` — it only
restarts on exit. The healthcheck surfaces a stall in `docker ps` and in the
Synology / QNAP container UI; a genuinely dead process is caught by the restart
policy, and network failures are caught by the watcher's own
`KSL_FAIL_ALERT_AFTER` Bark push. Add `willfarrell/autoheal` if you want
unhealthy containers bounced automatically.

On Synology you can instead point *Container Manager → Project → Create* at this
`docker-compose.yml`.
</details>

<details>
<summary><b>B. Synology DSM Task Scheduler</b> — no Docker needed</summary>

DSM has no per-minute UI option, so run the daemon as a boot-triggered task:

*Control Panel → Task Scheduler → Create → Triggered Task → User-defined script*
- Event: **Boot-up**, User: `root`
- Command:
  ```
  /volume1/scripts/ksl-watch/run.sh --daemon
  ```

Then start it once manually (*Run*) so you do not have to reboot.
</details>

<details>
<summary><b>C. cron (QNAP, OMV, Asustor, any Linux NAS)</b></summary>

```bash
crontab -e
```
```
* * * * * /volume1/scripts/ksl-watch/run.sh --once >> /volume1/scripts/ksl-watch/cron.log 2>&1
```

`--once` is stateless-safe: all history lives in `state.json`, so cron works fine.
On QNAP also append the line to `/etc/config/crontab` and run `crontab /etc/config/crontab`
so it survives a firmware reboot.
</details>

<details>
<summary><b>D. systemd (TrueNAS SCALE, OMV, Debian-based NAS)</b></summary>

```bash
sudo cp -r . /opt/ksl-watch && sudo cp ksl-watch.service /etc/systemd/system/
sudo systemctl enable --now ksl-watch && sudo journalctl -u ksl-watch -f
```
</details>

---

## Tuning

| Setting | Effect |
|---|---|
| `KSL_MODE=serial` | fire only on a lot of ≥ `KSL_WANT` **consecutive** seats (default — this is what "2 tickets together" means) |
| `KSL_MODE=total` | fire when ≥ 2 tickets exist for a seat type, even as separate singles |
| `KSL_MODE=any` | fire on *anything* listed — maximum paranoia, more noise |
| `KSL_NOTIFY_LOW=0` | silence the "listing appeared but it's only 1 seat" alerts |
| `KSL_REPEAT_MIN=30` | minutes before re-nagging about a still-open match; `0` = alert once only |
| `KSL_TOURS=14584,14585` | watch several tours at once |
| `KSL_INTERVAL_MIN` / `MAX` | the random sweep-gap band, in seconds |
| `KSL_STAGGER_MAX=45` | max random pre-sweep delay in `--once`/cron mode |
| `KSL_BAN_PAUSE_SEC=600` | first back-off after a 403/429/503 |
| `KSL_HEALTH_GRACE_SEC=120` | how late a sweep may be before `--healthcheck` fails |

Since a single ticket is what is on offer today, consider running with
`KSL_MODE=any` for a few days: you will hear about *every* movement in this pool
while it is this thin.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `data-page payload not found` | The site changed its markup — re-check `parse_inventory()` |
| No alerts ever | Run `--test-notify`; check `KSL_LOG` for `no notification channel configured` |
| Alerts repeat constantly | Check that `KSL_STATE` points somewhere **writable and persistent** |
| Wrong draw countdown | Set `TZ=Asia/Tokyo`; the script pins JST internally but logs use it too |
| Nothing after a reboot | Docker `restart: unless-stopped`, or `systemctl enable`, or the DSM boot trigger |

Inspect what the watcher believes at any time:

```bash
python3 ksl_watch.py --print
```
