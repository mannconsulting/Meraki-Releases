#!/usr/bin/env python3
"""
Build the firmware changelog cache from Cisco Community notification emails.

The community site blocks programmatic access, so fetch_meraki_changelogs.py can no longer
reach it. A subscription to the "Meraki Firmware Upgrades Feed" board delivers the same content
by email: one email per announcement ("posted a new article") and one per comment ("posted a
new comment"), and the comment email carries the changelog HTML with its headings intact.

Input: JSON envelopes under --inbox (default ./inbox), written by the Gmail Apps Script
(apps_script/Code.gs). Each file holds one envelope or a list of them:
    {"id": gmail message id, "date": ISO-8601, "subject": ..., "html": raw email HTML}

Output under --out (default ./firmware), same layout fetch_meraki_changelogs.py produced:
    <FAMILY>/<VERSION>.json, index.json, posts.json
plus:
    email_state.json   processed message ids and the announcements seen (idempotency + joins)
    versions.txt       one FAMILY/VERSION per line, a cheap read for "is this cached?"
    status.json        recent events and announcements still waiting for a changelog

Parsing is delegated to fetch_meraki_changelogs.py so both sources yield identical records.
Re-running over the same envelopes changes nothing.  Stdlib only.

    python3 ingest_emails.py [--inbox DIR] [--out DIR] [--keep]
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys
import time

import fetch_meraki_changelogs as F

BODY_RE = re.compile(r'<p class="email-notification-body">(.*?)<a class="button"', re.S | re.I)
LINK_RE = re.compile(r"<h3>\s*<a href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S | re.I)
WHO_RE = re.compile(r"([^\s<>()][^<>()\r\n]*?)\s*\([^()<>]*\)\s+posted a new (?:article|comment)", re.I)
URL_RE = re.compile(r"/t5/[^/]+/([^/]+)/b([ac])-p/(\d+)")
CHANGELOG_WORDS = ("changelog", "known issues", "bug fixes", "fixed issues",
                   "executive summary", "release highlights")
RECENT_CAP = 60
PENDING_DAYS = 45


def ts(s):
    """Epoch seconds from either timestamp style in the cache (API offsets or email Z)."""
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def extract_body(email_html):
    m = BODY_RE.search(email_html)
    if not m:
        return None
    b = m.group(1)
    b = re.sub(r"</?(html|body)\b[^>]*>|<head\b[^>]*>\s*</head>", "", b, flags=re.I)
    b = re.sub(r"<p>\s*$", "", b.rstrip(), flags=re.I)
    return re.sub(r">\s+<", "><", b).strip()


def parse_email(env):
    """Envelope -> dict(kind, slug, msg_id, url, subject, author, body, date) or None."""
    h = env.get("html") or ""
    link = LINK_RE.search(h)
    if not link:
        return None
    url = link.group(1)
    u = URL_RE.search(url)
    body = extract_body(h)
    if not u or body is None:
        return None
    subject = " ".join(F.html_to_lines(link.group(2)))
    who = WHO_RE.search(h)
    return {"kind": "article" if u.group(2) == "a" else "comment", "slug": u.group(1),
            "msg_id": u.group(3), "url": url, "subject": re.sub(r"^Re:\s*", "", subject),
            "author": who.group(1).strip() if who else None, "body": body, "date": env["date"]}


def changelog_score(lines):
    text = " ".join(lines).lower()
    return sum(1 for w in CHANGELOG_WORDS if w in text)


def index_entry(rec, fname):
    return {"family": rec["family"], "version": rec["version"], "file": fname,
            "releaseTypes": sorted({a["releaseType"] for a in rec["announcements"]}),
            "first_announced": rec["announcements"][0]["announced"] if rec["announcements"] else None,
            "last_announced": rec["announcements"][-1]["announced"] if rec["announcements"] else None,
            "sections": list(rec["sections"].keys())}


def save_rec(out, rec, index):
    fname = os.path.join(F.safe(rec["family"]), F.safe(rec["version"]) + ".json")
    fpath = os.path.join(out, fname)
    os.makedirs(os.path.dirname(fpath), exist_ok=True)
    rec["announcements"].sort(key=lambda a: ts(a["announced"]))
    rec["fetched"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(fpath, "w") as f:
        json.dump(rec, f, indent=2, ensure_ascii=False)
    index[f"{rec['family']}/{rec['version']}"] = index_entry(rec, fname)


def load_rec(out, family, version):
    return F.load(os.path.join(out, F.safe(family), F.safe(version) + ".json"), None)


def add_announcement(rec, ann):
    if any(a["post_id"] == ann["post_id"] for a in rec["announcements"]):
        return False
    rec["announcements"].append({k: ann[k] for k in
                                 ("post_id", "releaseType", "announced", "post_subject", "post_url")})
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", default="inbox")
    ap.add_argument("--out", default="firmware")
    ap.add_argument("--keep", action="store_true", help="leave processed inbox files in place")
    args = ap.parse_args()
    out = args.out
    os.makedirs(out, exist_ok=True)

    index = F.load(os.path.join(out, "index.json"), {})
    posts = F.load(os.path.join(out, "posts.json"), {})
    state = F.load(os.path.join(out, "email_state.json"), {"processed": {}, "announcements": []})
    status = F.load(os.path.join(out, "status.json"), {"recent": []})
    anns = state["announcements"]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    files = sorted(glob.glob(os.path.join(args.inbox, "*.json")))
    envs = []
    for fp in files:
        data = json.load(open(fp))
        envs.extend(data if isinstance(data, list) else [data])
    envs.sort(key=lambda e: ts(e.get("date", "")))

    events, skipped, unparsed = [], 0, []

    def event(kind, key, rtype, e, **extra):
        events.append({"event": kind, "key": key, "releaseType": rtype, "url": e["url"],
                       "email_date": e["date"], "ingested": now, **extra})

    for env in envs:
        if env["id"] in state["processed"]:
            skipped += 1
            continue
        e = parse_email(env)
        if not e:
            unparsed.append({"id": env["id"], "subject": env.get("subject")})
            state["processed"][env["id"]] = env.get("date")
            continue

        if e["kind"] == "article":
            line = " ".join(F.html_to_lines(e["body"]))
            m = re.search(r"Firmware\s+(.+?)\s+was released", line)
            family, version = F.detect_family_version(m.group(1) if m else line)
            family = family or F.subject_family(e["subject"])
            rtype = F.subject_release_type(e["subject"])
            ann = {"post_id": e["msg_id"], "slug": e["slug"], "releaseType": rtype,
                   "announced": e["date"], "post_subject": e["subject"], "post_url": e["url"],
                   "family": family, "version": version}
            if not any(a["post_id"] == ann["post_id"] for a in anns):
                anns.append(ann)
            rec = load_rec(out, family, version) if version else None
            key = f"{family}/{version}" if version else None
            if rec:
                if add_announcement(rec, ann):
                    save_rec(out, rec, index)
                    event("reannounced", key, rtype, e)
                posts[e["msg_id"]] = {"status": "captured", "key": key, "subject": e["subject"],
                                      "post_time": e["date"], "releaseType": rtype}
            elif posts.get(e["msg_id"], {}).get("status") != "captured":
                posts[e["msg_id"]] = {"status": "pending", "subject": e["subject"],
                                      "post_time": e["date"], "url": e["url"], "releaseType": rtype,
                                      "family_guess": family, "version_guess": version,
                                      "checked": now[:10]}
                event("announced", key, rtype, e)

        else:
            lines = F.html_to_lines(e["body"])
            parents = [a for a in anns if a["slug"] == e["slug"] and ts(a["announced"]) <= ts(e["date"])]
            parent = max(parents, key=lambda a: ts(a["announced"])) if parents else None
            comment = {"id": e["msg_id"], "author": e["author"], "posted": e["date"],
                       "text": "\n".join(lines), "html": e["body"]}

            if changelog_score(lines) < 2:  # discussion, not a changelog
                rec = load_rec(out, parent["family"], parent["version"]) if parent and parent["version"] else None
                if rec and not any(c["id"] == comment["id"] for c in rec.setdefault("other_comments", [])):
                    rec["other_comments"].append(comment)
                    save_rec(out, rec, index)
                state["processed"][env["id"]] = env.get("date")
                continue

            title, family, version, sections = F.parse_changelog(e["body"])
            if parent:
                family = family or parent["family"]
                version = version or parent["version"]
            family = family or F.subject_family(e["subject"])
            version = version or f"post-{parent['post_id'] if parent else e['msg_id']}"
            key = f"{family}/{version}"
            fresh = {"title": title, "changelog_comment_id": e["msg_id"],
                     "changelog_author": e["author"], "changelog_posted": e["date"],
                     "sections": sections, "text": "\n".join(lines), "html": e["body"]}

            rec = load_rec(out, family, version)
            kind = None
            if rec is None:
                rec = {"family": family, "version": version, "announcements": [],
                       "other_comments": [], "source": "email", **fresh}
                kind = "captured"
            elif rec.get("changelog_comment_id") != e["msg_id"] and ts(e["date"]) > ts(rec["changelog_posted"]):
                rec.update(fresh)
                kind = "updated"
            matched = [a for a in anns if a["family"] == family and a["version"] == version]
            if parent and parent not in matched:
                matched.append(parent)
            changed = [add_announcement(rec, a) for a in matched]
            if kind or any(changed):
                save_rec(out, rec, index)
            for a in matched:
                posts[a["post_id"]] = {"status": "captured", "key": key, "subject": a["post_subject"],
                                       "post_time": a["announced"], "releaseType": a["releaseType"]}
            if kind:
                rtypes = sorted({a["releaseType"] for a in rec["announcements"]})
                event(kind, key, ", ".join(rtypes) or "unknown", e,
                      sections=list(sections.keys()), no_announcement=not matched)

        state["processed"][env["id"]] = env.get("date")

    # an announcement whose changelog landed in the same run is not worth its own event
    got = {e["key"] for e in events if e["event"] in ("captured", "updated")}
    events = [e for e in events if not (e["event"] == "announced" and e["key"] in got)]

    # keep state bounded; anything older is already reflected in the version files
    state["announcements"] = sorted(anns, key=lambda a: ts(a["announced"]))[-400:]
    if len(state["processed"]) > 3000:
        keep = sorted(state["processed"].items(), key=lambda kv: ts(kv[1] or ""))[-3000:]
        state["processed"] = dict(keep)

    cutoff = time.time() - PENDING_DAYS * 86400
    pending = [{"post_id": pid, **{k: p.get(k) for k in
                ("subject", "url", "post_time", "releaseType", "family_guess", "version_guess")}}
               for pid, p in posts.items()
               if p.get("status") == "pending" and ts(p.get("post_time", "")) >= cutoff]
    pending.sort(key=lambda p: ts(p["post_time"]))
    dates = [e.get("date") for e in envs if e.get("date")]
    status = {"updated": now,
              "last_email_date": max(dates, key=ts) if dates else status.get("last_email_date"),
              "versions_indexed": len(index), "pending": pending,
              "recent": (status.get("recent", []) + events)[-RECENT_CAP:]}

    def dump(name, obj):
        with open(os.path.join(out, name), "w") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)

    dump("index.json", dict(sorted(index.items())))
    dump("posts.json", posts)
    dump("email_state.json", state)
    dump("status.json", status)
    with open(os.path.join(out, "versions.txt"), "w") as f:
        f.write("\n".join(sorted(index)) + "\n")

    if not args.keep:
        for fp in files:
            os.remove(fp)

    print(json.dumps({"envelopes": len(envs), "already_processed": skipped,
                      "unparsed": unparsed, "events": events,
                      "pending": len(pending), "versions_indexed": len(index)}, indent=2))
    if unparsed:
        print(f"WARNING: {len(unparsed)} email(s) did not match the notification template",
              file=sys.stderr)


if __name__ == "__main__":
    main()
