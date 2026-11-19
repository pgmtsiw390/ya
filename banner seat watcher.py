#!/usr/bin/env python3
"""
KU Banner seat watcher (Khalifa University) — with live dashboard data
---------------------------------------------------------------------
Checks seat counts for the CRNs below every few minutes and sends a phone
push notification (via the ntfy app) when someone drops and a seat frees up.
No login needed: it reads the same public "Browse Classes" data you see.

On GitHub Actions it also publishes the latest numbers to a "data" branch,
which the dashboard page (index.html) reads to show live status.

Run on a computer:
  pip install requests
  python banner_seat_watcher.py --test     # test notification
  python banner_seat_watcher.py            # start watching
"""

import os
import re
import sys
import json
import time
import random
import subprocess
from datetime import datetime, timezone

import requests

# ============================ CONFIG ============================

BANNER_BASE = "https://studentregistration.ku.ac.ae/StudentRegistrationSsb"
TERM = "202620"
TERM_NAME = "Spring 2027"

# CRNs to watch. Add more like: ["20009", "20010"]
CRNS = ["20009"]         # AERO 225-02 Mechanics of Solids

NTFY_TOPIC = "s-ku-seats-CHANGE-ME"   # same topic you subscribe to in ntfy
NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or NTFY_TOPIC   # GitHub secret overrides

CHECK_EVERY_SECONDS = 180             # 3 min; please don't go much lower

# Alert every time seats go UP (someone dropped), not only when going 0 -> open
ALERT_ON_ANY_INCREASE = True

# ================================================================

DATA_DIR = os.environ.get("DATA_DIR")        # set by the GitHub workflow
HISTORY_HOURS = 72                            # chart history kept on the dashboard
MAX_EVENTS = 200

UA = {"User-Agent": "Mozilla/5.0 (personal seat watcher)"}
REGISTER_URL = f"{BANNER_BASE}/ssb/registration"


# ---------------------------- alerts ----------------------------

def notify(title, message):
    if "CHANGE-ME" in NTFY_TOPIC:
        print("[!] NTFY_TOPIC isn't set (GitHub secret missing or empty)")
    try:
        r = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high",
                     "Tags": "rotating_light", "Click": REGISTER_URL},
            timeout=15,
        )
        if r.ok:
            print(f"Notification accepted by ntfy (topic ends in "
                  f"...{NTFY_TOPIC[-3:]}, {len(NTFY_TOPIC)} characters)")
        else:
            print(f"[!] ntfy refused the notification: {r.status_code} {r.text[:200]}")
    except requests.RequestException as e:
        print(f"[!] Notification failed: {e}")


# ---------------------------- Banner ----------------------------

def page_text(s, endpoint, crn):
    r = s.get(f"{BANNER_BASE}/ssb/searchResults/{endpoint}",
              params={"term": TERM, "courseReferenceNumber": crn}, timeout=30)
    r.raise_for_status()
    text = re.sub(r"<[^>]+>", " ", r.text)          # strip HTML tags
    return re.sub(r"\s+", " ", text)


def field(text, label):
    m = re.search(re.escape(label) + r"\s*:?\s*(-?\d+)", text)
    return int(m.group(1)) if m else None


def course_info(s, crn):
    """Course name plus the other CRNs it's cross-listed with."""
    info = {"name": f"CRN {crn}", "short": f"CRN {crn}", "title": "", "linked": []}
    try:
        t = page_text(s, "getClassDetails", crn)
        subj = re.search(r"Subject:\s*(.+?)\s+Course Number", t).group(1)
        num = re.search(r"Course Number:\s*(\S+)", t).group(1)
        sec = re.search(r"Section Number:\s*(\S+)", t).group(1)
        title = re.search(r"Title:\s*(.+?)\s+Credit Hours", t).group(1)
        info.update(name=f"{subj} {num}-{sec} {title} (CRN {crn})",
                    short=f"{subj} {num}-{sec}", title=title)
    except Exception:
        pass
    try:
        x = page_text(s, "getXlstSections", crn)
        part = x.split("Sections:", 1)[1] if "Sections:" in x else ""
        for c, subj, num in re.findall(r"\b(\d{5})\s+([A-Za-z&,\- ]+?)\s+(\d{3}[A-Z]?)\b", part):
            if c != crn:
                info["linked"].append({"crn": c, "course": f"{subj.strip()} {num}"})
    except Exception:
        pass
    return info


def seats(s, crn):
    """Real open seats = the smaller of the section's own seats and the
    shared cross-list seats (e.g. AERO 225 / CIVE 225 / MEEN 325 share one cap)."""
    t = page_text(s, "getEnrollmentInfo", crn)
    avail = field(t, "Enrollment Seats Available")
    if avail is None:
        raise ValueError(f"Couldn't read seats for CRN {crn} (check CRN/term)")
    actual, cap = field(t, "Enrollment Actual"), field(t, "Enrollment Maximum")

    x = page_text(s, "getXlstSections", crn)
    x_avail = field(x, "Cross List Seats Available")
    if x_avail is not None:
        avail = min(avail, x_avail)
        actual, cap = field(x, "Cross List Actual"), field(x, "Cross List Maximum")
    return avail, actual, cap


# ------------------------ dashboard data ------------------------

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def status_path():
    return os.path.join(DATA_DIR, "status.json") if DATA_DIR else "seat_state.json"


def load_status():
    try:
        with open(status_path()) as f:
            data = json.load(f)
        if "courses" in data:
            return data
    except (OSError, ValueError):
        pass
    return {"courses": {}, "events": [], "checksTotal": 0}


def save_status(st):
    try:
        with open(status_path(), "w") as f:
            json.dump(st, f, separators=(",", ":"))
    except OSError as e:
        print(f"[!] Couldn't save status: {e}")


def git(*args):
    return subprocess.run(["git", "-C", DATA_DIR, *args],
                          capture_output=True, text=True)


def publish():
    """Push status.json to the 'data' branch, keeping it to one small commit."""
    if not DATA_DIR:
        return
    git("add", "-A")
    has_commit = git("rev-parse", "--verify", "HEAD").returncode == 0
    commit = ["commit", "-q", "-m", "seat data"]
    if has_commit:
        commit.insert(1, "--amend")
    git(*commit)
    r = git("push", "-f", "-q", "origin", "HEAD:data")
    if r.returncode != 0:
        print(f"[!] Couldn't publish dashboard data: {r.stderr.strip()[:200]}")


# ----------------------------- main -----------------------------

def main():
    if "--test" in sys.argv:
        notify("Seat watcher test", "If you see this, notifications work ✅")
        return

    # Optional: stop after N minutes (used by the GitHub Actions version).
    stop_at = None
    if "--minutes" in sys.argv:
        stop_at = time.time() + 60 * int(sys.argv[sys.argv.index("--minutes") + 1])

    s = requests.Session()
    s.headers.update(UA)
    st = load_status()
    st.update(term=TERM, termName=TERM_NAME, interval=CHECK_EVERY_SECONDS,
              runStarted=now_iso(), runEnds=None, registerUrl=REGISTER_URL)
    if stop_at:
        st["runEnds"] = datetime.fromtimestamp(stop_at, timezone.utc).isoformat(timespec="seconds")

    # Forget CRNs you've removed from the list.
    st["courses"] = {c: v for c, v in st["courses"].items() if c in CRNS}
    for crn in CRNS:
        c = st["courses"].setdefault(crn, {"history": []})
        c.update(course_info(s, crn))

    print(f"Watching {len(CRNS)} CRN(s) every ~{CHECK_EVERY_SECONDS}s. Ctrl+C to stop.")
    while stop_at is None or time.time() < stop_at:
        for crn in CRNS:
            c = st["courses"][crn]
            try:
                avail, actual, cap = seats(s, crn)
            except (requests.RequestException, ValueError) as e:
                print(f"[!] {e}")
                st["lastError"] = {"t": now_iso(), "msg": str(e)[:200]}
                continue
            print(time.strftime("[%H:%M:%S] ") +
                  f"{c['name']}: {max(avail, 0)} open ({actual}/{cap} enrolled)")

            prev = c.get("avail")
            if prev is None:
                if avail > 0:
                    notify(f"Seats open now: CRN {crn}",
                           f"{c['name']}\n{avail} seat(s) available right now.")
            elif avail > max(prev, 0) and (ALERT_ON_ANY_INCREASE or prev <= 0):
                notify(f"Someone dropped! CRN {crn}",
                       f"{c['name']}\nSeats: {max(prev, 0)} → {avail}. Register now!")

            ts = int(time.time())
            if prev is not None and (avail != prev or actual != c.get("actual")):
                st["events"].append({"t": ts, "crn": crn, "short": c["short"],
                                     "from": max(prev, 0), "to": max(avail, 0),
                                     "enrolledFrom": c.get("actual"), "enrolledTo": actual})
                st["events"] = st["events"][-MAX_EVENTS:]
                c["lastChange"] = now_iso()

            c.update(avail=avail, actual=actual, cap=cap, checked=now_iso())
            c["history"].append([ts, max(avail, 0), actual, cap])
            cutoff = ts - HISTORY_HOURS * 3600
            c["history"] = [h for h in c["history"] if h[0] >= cutoff]
            time.sleep(2)

        st["updated"] = now_iso()
        st["checksTotal"] = st.get("checksTotal", 0) + 1
        save_status(st)
        publish()

        if stop_at is not None and time.time() + CHECK_EVERY_SECONDS > stop_at:
            break
        time.sleep(CHECK_EVERY_SECONDS + random.randint(0, 30))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
