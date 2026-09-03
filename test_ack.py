# --ack: I already applied. Stay quiet, unless a BIGGER lot shows up.
import os, sys
sys.path.insert(0, r"C:\tools\ksl-script")
sys.stdout.reconfigure(encoding="utf-8")
import ksl_watch as K
S = "1601726_6_1_1"


def payload(entries):
    mps = {}
    for dk, seat, price, total, serial in entries:
        if not total:
            continue
        mps[dk] = {"detailKey": dk, "trade_type_label": "抽選",
                   "trade_info": {"total": total,
                                  "serial": {str(s): True for s in serial}},
                   "ticket_price": {"trade_price": price},
                   "concert": {"seat_type": seat, "title": "豊洲PIT",
                               "place": "東京", "concert_date": "2026-09-26",
                               "op_time": "16:30:00", "st_time": "17:30:00"}}
    return {"props": {"concertList": {"14584_1": {
        "concert": {"title": "豊洲PIT", "place": "東京",
                    "concert_date": "2026-09-26", "seat_type": "A席"},
        "concert_info": {"week": "土"}, "trade_status_label": "トレード中",
        "trade_total": sum(e[3] for e in entries), "isClose": False,
        "trade_schedule": {"application_end_at": "2026-09-18 11:59:59"},
        "member_precedes": mps}}}}


base = os.path.dirname(os.path.abspath(__file__))
for f in ("ack_state.json", "ack_state.ack.json"):
    try: os.remove(os.path.join(base, f))
    except OSError: pass


class A_:
    tours = "14584"; want = 2; mode = "serial"; interval = 60
    state = os.path.join(base, "ack_state.json")
    no_stagger = True; dry_run = True


cfg = K.Config(A_())
fired = []
_real = K.notify
K.notify = lambda t, b, u, c, level="normal": (fired.append(level),
                                               print("   -> ALERT [%s] %s" % (level, t)))[0]
cur = [None]
K.fetch_tour = lambda tid, st, c: payload(cur[0])
state = {}
fails = 0


def step(name, entries, expect):
    global state, fails
    cur[0], fired[:] = entries, []
    print("\n" + name)
    state = K.check(cfg, state, announce=True)
    K.save_state(cfg.state_file, state)
    got = fired[0] if fired else None
    if got != expect:
        print("   !! FAIL expected %r got %r" % (expect, got)); fails += 1
    elif not fired:
        print("   -> (silence)")


step("1. A席 2連番 appears -> ring", [(S, "A席", 11000, 2, [1, 2])], "urgent")
print("\n2. you apply, then run --ack")
K.cmd_ack(cfg)
step("3. still listed -> silence", [(S, "A席", 11000, 2, [1, 2])], None)
step("4. delisted (would normally re-arm)", [], "normal")
step("5. SAME 2-lot comes back -> STILL silent (acked)",
     [(S, "A席", 11000, 2, [1, 2])], None)
step("6. churns again", [], "normal")
step("7. back again -> still silent", [(S, "A席", 11000, 2, [1, 2])], None)
step("8. a BIGGER 3-seat lot appears -> rings",
     [(S, "A席", 11000, 3, [1, 2, 3])], "urgent")
print("\n9. --unack")
K.cmd_unack(cfg)
# --unack clears the acknowledgement, not the per-episode latch: this exact
# listing has been up continuously since step 8 and already rang once.
step("10. same listing still up -> silence (already rang in step 8)",
     [(S, "A席", 11000, 2, [1, 2])], None)
step("11. it churns", [], "normal")
step("12. new episode -> rings, alerting truly restored",
     [(S, "A席", 11000, 2, [1, 2])], "urgent")
print("\n%s" % ("=" * 60))
print("FAILURES: %d" % fails)
sys.exit(1 if fails else 0)
