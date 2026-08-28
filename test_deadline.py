# Deadline-warning correctness: title must state the ACTUAL time left, and the
# level must escalate on real urgency, not on which threshold matched.
import os, sys
sys.path.insert(0, r"C:\tools\ksl-script")
sys.stdout.reconfigure(encoding="utf-8")
from datetime import datetime, timedelta
import ksl_watch as K

fired = []
K.notify = lambda t, b, u, c, level="normal": fired.append((level, t))


def run(hours_left, warned=None):
    cfg_state = {"deadline_warned": list(warned or [])}
    end = (datetime.now(K.JST) + timedelta(hours=hours_left))
    K.fetch_tour = lambda tid, st, c: {"props": {"concertList": {"14584_1": {
        "concert": {"title": "豊洲PIT", "place": "東京",
                    "concert_date": "2026-09-26", "seat_type": "S席"},
        "concert_info": {"week": "土"}, "trade_status_label": "トレード中",
        "trade_total": 0, "isClose": False,
        "trade_schedule": {"application_end_at": end.strftime("%Y-%m-%d %H:%M:%S")},
        "member_precedes": {}}}}}
    fired[:] = []
    st = K.check(cfg, cfg_state, announce=True)
    return (fired[0] if fired else None), st.get("deadline_warned")


class A_:
    tours = "14584"; want = 2; mode = "serial"; interval = 60
    state = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dl_state.json")
    no_stagger = True; dry_run = True


os.environ["KSL_DEADLINE_WARN"] = "72,24,3"
cfg = K.Config(A_())

cases = [
    ("normal operation: 71h left, nothing warned yet", 71, [],
     "normal", "~71h"),
    ("normal operation: 23h left, 72 already warned",  23, [72],
     "normal", "~23h"),
    ("final stretch: 2h left, 72+24 warned",            2, [72, 24],
     "urgent", "~2h"),
    ("LATE START: 4h left, nothing warned (was the bug)", 4, [],
     "urgent", "~4h"),
    ("already past every threshold -> silence",        71, [72, 24, 3],
     None, None),
]

fails = 0
for name, hrs, warned, exp_level, exp_txt in cases:
    got, w = run(hrs, warned)
    lvl = got[0] if got else None
    title = got[1] if got else ""
    ok = (lvl == exp_level) and (exp_txt is None or exp_txt in title)
    print("%-52s -> %-7s %s" % (name, lvl, title))
    if not ok:
        print("     !! FAIL expected level=%s containing %r" % (exp_level, exp_txt))
        fails += 1
    if name.startswith("LATE START"):
        print("     thresholds now marked: %s (all of them, so no bogus"
              " '~72h' later)" % sorted(w))

print("\nFAILURES: %d / %d" % (fails, len(cases)))
try:
    os.remove(A_.state)
except OSError:
    pass
sys.exit(1 if fails else 0)
