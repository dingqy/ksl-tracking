# Simulate inventory changes against the real parser + alert logic.
#   python3 test_alerts.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")
import ksl_watch as K


def payload(entries):
    """entries: list of (key, seat_type, price, total, serial_list)

    Mirrors real tixplus behaviour: a seat type with no stock is ABSENT from
    member_precedes entirely -- the site does not report it as total 0. The
    watcher has to infer the delisting from the row's disappearance.
    """
    mps = {}
    for dk, seat, price, total, serial in entries:
        if not total:
            continue                      # delisted -> not in the payload
        mps[dk] = {
            "detailKey": dk,
            "trade_type_label": "抽選",
            "trade_info": {"total": total, "serial": {str(s): True for s in serial}},
            "ticket_price": {"trade_price": price},
            "concert": {"seat_type": seat, "title": "豊洲PIT", "place": "東京",
                        "concert_date": "2026-09-26", "op_time": "16:30:00",
                        "st_time": "17:30:00"},
        }
    return {"props": {"concertList": {"14584_1": {
        "concert": {"title": "豊洲PIT", "place": "東京",
                    "concert_date": "2026-09-26", "seat_type": "*車椅子席"},
        "concert_info": {"week": "土"},
        "trade_status_label": "トレード中",
        "trade_total": sum(e[3] for e in entries),
        "isClose": False,
        "trade_schedule": {"application_end_at": "2026-09-18 11:59:59",
                           "application_start_at": "2026-08-26 10:00:00"},
        "member_precedes": mps}}}}


S, A = "1601725_6_1_1", "1601726_6_1_1"

SCENES = [
    ("baseline: 1x S席 single",            [(S, "S席", 28600, 1, [1])],       "passive"),
    ("no change (expect silence)",         [(S, "S席", 28600, 1, [1])],       None),
    ("S席 delisted (row disappears)",      [],                                "normal"),
    ("S席 returns, IDENTICAL 1x single",   [(S, "S席", 28600, 1, [1])],       "normal"),
    ("*** 2-consecutive S席 lot ***",      [(S, "S席", 28600, 2, [1, 2])],    "urgent"),
    ("no change (expect silence)",         [(S, "S席", 28600, 2, [1, 2])],    None),
    ("A席 standing enters trade too",      [(S, "S席", 28600, 2, [1, 2]),
                                            (A, "A席", 15400, 3, [1, 2, 3])], "urgent"),
    ("everything delisted",                [],                                "normal"),
    # regression: a match inside its repeat window must not silence a NEW
    # below-target listing on a different seat type
    ("S席 2-lot back (urgent)",            [(S, "S席", 28600, 2, [1, 2])],    "urgent"),
    ("no change -> S席 suppressed",        [(S, "S席", 28600, 2, [1, 2])],    None),
    ("A席 newly listed while S席 muted",   [(S, "S席", 28600, 2, [1, 2]),
                                            (A, "A席", 15400, 1, [1])],       "normal"),
    # one alarm per episode: ring once, stay quiet while it is up, re-arm only
    # after the listing goes away
    ("reset: everything gone",             [],                                "normal"),
    ("2-lot appears -> ring once",         [(S, "S席", 28600, 2, [1, 2])],    "urgent"),
    ("still up -> silence",                [(S, "S席", 28600, 2, [1, 2])],    None),
    ("still up, stock 2->3 -> silence",    [(S, "S席", 28600, 3, [1, 2, 3])], None),
    ("still up much later -> silence",     [(S, "S席", 28600, 3, [1, 2, 3])], None),
    ("listing disappears -> re-arms",      [],                                "normal"),
    ("comes back -> rings AGAIN",          [(S, "S席", 28600, 2, [1, 2])],    "urgent"),
]

state, cur, fired = {}, [None], []
K.fetch_tour = lambda tid, st, cfg: payload(cur[0])
_real_notify = K.notify


def spy(title, body, url, cfg, level="normal"):
    fired.append(level)
    _real_notify(title, body, url, cfg, level)


K.notify = spy


class A_:
    tours = "14584"; want = 2; mode = "serial"; interval = 60
    state = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim_state.json")
    no_stagger = True
    dry_run = True


cfg = K.Config(A_())
cfg.repeat_min = 30

failures = 0
for name, entries, expect in SCENES:
    print("\n" + "=" * 74)
    print("SCENARIO:", name, "  (expect: %s)" % (expect or "silence"))
    print("=" * 74)
    cur[0], fired[:] = entries, []
    state = K.check(cfg, state, announce=True)
    got = fired[0] if fired else None
    if got != expect:
        print("  !! FAIL expected %r got %r" % (expect, got))
        failures += 1

print("\n" + "=" * 74)
print("FAILURES: %d / %d" % (failures, len(SCENES)))
try:
    os.remove(A_.state)
except OSError:
    pass
sys.exit(1 if failures else 0)
