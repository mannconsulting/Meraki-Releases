#!/usr/bin/env python3
"""
Pull Meraki firmware release notes from the Cisco Community "Meraki Firmware Upgrades Feed"
via the Khoros REST API (no browser, no auth) and store them as JSON.  v2

Layout under --out (default ./firmware):
  index.json              keyed "FAMILY/VERSION": releaseTypes, announcements, section names, file
  posts.json              every feed post seen (idempotency + pending tracking)
  <FAMILY>/<VERSION>.json full record: title, sections (by heading), text, html, all announcements

Family and version come from the changelog title ("Security appliances software versions MX 19.2.7
changelog"), not the post subject, which is unreliable on older posts. Sections are split on the
HTML headings the changelog actually uses, so names vary (Executive summary, Bug fixes - general
fixes, Known issues, Known issues status, What's new, ...). Match on them case-insensitively.

Stdlib only.  python3 fetch_meraki_changelogs.py [--out DIR] [--all] [--max-posts N] [--sleep S]
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://community.cisco.com"
BOARD_ID = "networking-firmwareupgrades"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
PAGE = 50
FAMILIES = "MX|MR|MS|CS|MV|MT|MG|CW|SM|Z"

# fallback only, when the changelog title has no family token
SUBJECT_FAMILY = [
    (r"\bCS\b|Catalyst", "CS"), (r"\bMR\b|Wireless(?! WAN)|Access Point", "MR"),
    (r"\bMS\b|\bSwitch", "MS"), (r"\bMX\b|appliance", "MX"), (r"\bMV\b|Camera", "MV"),
    (r"\bMT\b|Sensor", "MT"), (r"\bMG\b|Wireless WAN|Cellular", "MG"), (r"\bSM\b", "SM"),
]
RELEASE_TYPES = ["Generally Available", "Recommended Release", "candidate", "beta", "legacy", "stable"]


# ---------------- API ----------------
def api(query, sleep):
    url = BASE + "/api/2.0/search?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            break
        except Exception as e:
            if attempt == 2:
                raise
            print(f"   retry after error: {e}", file=sys.stderr)
            time.sleep(3)
    if data.get("status") != "success":
        raise RuntimeError(f"API error for {query!r}: {json.dumps(data)[:300]}")
    time.sleep(sleep)
    return data["data"].get("items", [])


def list_posts(max_posts, sleep):
    out, offset = [], 0
    while len(out) < max_posts:
        items = api(f"SELECT id, subject, post_time, view_href FROM messages "
                    f"WHERE board.id = '{BOARD_ID}' AND depth = 0 "
                    f"ORDER BY post_time DESC LIMIT {PAGE} OFFSET {offset}", sleep)
        if not items:
            break
        out.extend(items)
        offset += PAGE
        if len(items) < PAGE:
            break
    return out[:max_posts]


def comments_for(post_id, sleep):
    return api(f"SELECT id, author.login, post_time, body FROM messages "
               f"WHERE parent.id = '{post_id}' ORDER BY post_time ASC", sleep)


# ---------------- parsing ----------------
def _row_to_line(m):
    cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", m.group(0), flags=re.I | re.S)
    texts = []
    for c in cells:
        c = re.sub(r"</\s*(li|p)\s*>", "; ", c, flags=re.I)
        c = html.unescape(re.sub(r"<[^>]+>", "", c))
        c = re.sub(r"[\s\u00a0]+", " ", c)
        texts.append(re.sub(r"(\s*;\s*)+", "; ", c).strip(" ;"))
    return "\n" + " | ".join(t for t in texts if t) + "\n"


def html_to_lines(h):
    h = re.sub(r"<tr[^>]*>.*?</tr>", _row_to_line, h, flags=re.I | re.S)
    h = re.sub(r"<\s*br\s*/?>", "\n", h, flags=re.I)
    h = re.sub(r"</\s*(p|div|li|h\d|tr|ul|ol)\s*>", "\n", h, flags=re.I)
    h = html.unescape(re.sub(r"<[^>]+>", "", h))
    return [re.sub(r"[ \t\u00a0]+", " ", l).strip() for l in h.splitlines() if l.strip()]


def parse_changelog(body):
    """Returns (title, family, version, sections{heading: [lines]})."""
    parts = re.split(r"(<h[1-6][^>]*>.*?</h[1-6]>)", body, flags=re.I | re.S)
    sections, cur = {}, "preamble"
    for p in parts:
        m = re.match(r"<h([1-6])[^>]*>(.*?)</h\1>", p, re.I | re.S)
        if m:
            name = " ".join(html_to_lines(m.group(2))).rstrip(":").strip()
            if not name or re.search(r"changelog", name, re.I):
                continue  # blank spacer heading, or the title itself
            cur = name
        else:
            lines = [l for l in html_to_lines(p) if not re.search(r"\bchangelog\b", l, re.I)]
            if lines:
                sections.setdefault(cur, []).extend(lines)
    all_lines = html_to_lines(body)
    title = next((l for l in all_lines[:8]
                  if re.search(r"change\s*log|release notes", l, re.I)), None)
    family, version = detect_family_version(title or "")
    if not version:
        # title line didn't carry it; scan the first few lines of the comment instead
        for l in all_lines[:8]:
            family, version = detect_family_version(l)
            if version:
                break
    if not version:
        # no title at all (bare fixed/known-issues tables); the issue links name the release
        m = re.search(rf"/({FAMILIES})_(\d+(?:\.\d+)+)_Release_Notes", body)
        if m:
            family, version = m.group(1), m.group(2)
            title = f"{family} {version} (version taken from documentation.meraki.com release-notes links)"
    if not sections.get("preamble"):
        sections.pop("preamble", None)
    return title, family, version, sections


VERSION_RE = r"(\d+(?:\.\d+){1,4})"
FAMILY_WORDS = [  # product words that appear in titles without the family code
    (r"IOS[\s-]*XE|Catalyst", "CS"), (r"security appliance", "MX"), (r"access point", "MR"),
    (r"\bswitch", "MS"), (r"camera", "MV"), (r"sensor", "MT"), (r"cellular gateway", "MG"),
]


def detect_family_version(line):
    """(family, version) from one line of text, or (None, None)."""
    m = re.search(rf"\b({FAMILIES})[\s-]*{VERSION_RE}\b", line)
    if m:
        return m.group(1), m.group(2)
    m = re.search(rf"IOS[\s-]*XE[\s-]*{VERSION_RE}\b", line, re.I)
    if m:
        return "CS", m.group(1)
    m = re.search(rf"\b{VERSION_RE}\b", line)
    if m and re.search(r"change\s*log|release notes|software version|firmware|fixes for", line, re.I):
        fam = next((f for pat, f in FAMILY_WORDS if re.search(pat, line, re.I)), None)
        return fam, m.group(1)
    return None, None


def pick_changelog(comments):
    """Best changelog comment: one with a parseable 'FAMILY x.y.z changelog' title wins,
    then the richest-looking one, newest breaking ties."""
    best, best_score = None, -1
    for c in comments:
        lines = html_to_lines(c.get("body", ""))
        text = " ".join(lines).lower()
        score = sum(1 for s in ("changelog", "known issues", "bug fixes", "fixed issues",
                                "executive summary", "release highlights") if s in text)
        if score < 2:
            continue
        if any(detect_family_version(l)[1] for l in lines[:8]):
            score += 10
        if score > best_score or (score == best_score and c["post_time"] > best["post_time"]):
            best, best_score = c, score
    return best


def other_comments(comments, chosen_id):
    return [{"id": c["id"], "author": c.get("author", {}).get("login"), "posted": c["post_time"],
             "text": "\n".join(html_to_lines(c.get("body", ""))), "html": c.get("body", "")}
            for c in comments if c["id"] != chosen_id]


def subject_family(subject):
    for pat, fam in SUBJECT_FAMILY:
        if re.search(pat, subject, re.I):
            return fam
    return "UNKNOWN"


def subject_release_type(subject):
    for rt in RELEASE_TYPES:
        if re.search(re.escape(rt), subject, re.I):
            return rt
    return "unknown"


def safe(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)


# ---------------- main ----------------
def load(path, default):
    return json.load(open(path)) if os.path.exists(path) else default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="firmware")
    ap.add_argument("--all", action="store_true", help="walk the whole board, not just recent posts")
    ap.add_argument("--max-posts", type=int)
    ap.add_argument("--sleep", type=float, default=0.25)
    ap.add_argument("--reparse", action="store_true",
                    help="no network: re-run the parser over the html already stored under --out")
    args = ap.parse_args()
    if args.reparse:
        return reparse(args.out)
    max_posts = args.max_posts or (5000 if args.all else 60)

    os.makedirs(args.out, exist_ok=True)
    index_path, posts_path = os.path.join(args.out, "index.json"), os.path.join(args.out, "posts.json")
    index, posts_seen = load(index_path, {}), load(posts_path, {})

    posts = list_posts(max_posts, args.sleep)
    print(f"{len(posts)} posts listed")
    new = filled = pending = 0

    for p in posts:
        pid = p["id"]
        seen = posts_seen.get(pid)
        if seen and seen.get("status") == "captured":
            continue
        rtype = subject_release_type(p["subject"])
        comments = comments_for(pid, args.sleep)
        cl = pick_changelog(comments)
        if not cl:
            posts_seen[pid] = {"status": "pending", "subject": p["subject"], "post_time": p["post_time"],
                               "url": p.get("view_href"), "releaseType": rtype,
                               "family_guess": subject_family(p["subject"]),
                               "comments_seen": len(comments), "checked": time.strftime("%Y-%m-%d")}
            pending += 1
            continue

        title, family, version, sections = parse_changelog(cl["body"])
        if not family:
            family = subject_family(p["subject"])
        if not version:
            version = f"post-{pid}"
        key = f"{family}/{version}"
        announcement = {"post_id": pid, "releaseType": rtype, "announced": p["post_time"],
                        "post_subject": p["subject"], "post_url": p.get("view_href")}

        fname = os.path.join(safe(family), safe(version) + ".json")
        fpath = os.path.join(args.out, fname)
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        rec = load(fpath, None)
        if rec is None:
            rec = {"family": family, "version": version, "title": title, "announcements": [],
                   "changelog_comment_id": cl["id"], "changelog_author": cl.get("author", {}).get("login"),
                   "changelog_posted": cl["post_time"], "sections": sections,
                   "text": "\n".join(html_to_lines(cl["body"])), "html": cl["body"],
                   "other_comments": []}
        for oc in other_comments(comments, cl["id"]):
            if not any(x["id"] == oc["id"] for x in rec.setdefault("other_comments", [])):
                rec["other_comments"].append(oc)
        if not any(a["post_id"] == pid for a in rec["announcements"]):
            rec["announcements"].append(announcement)
        # same version re-announced later (candidate -> stable): keep the newer changelog comment
        if cl["post_time"] > rec["changelog_posted"]:
            rec.update({"title": title, "changelog_comment_id": cl["id"],
                        "changelog_author": cl.get("author", {}).get("login"),
                        "changelog_posted": cl["post_time"], "sections": sections,
                        "text": "\n".join(html_to_lines(cl["body"])), "html": cl["body"]})
        rec["announcements"].sort(key=lambda a: a["announced"])
        rec["fetched"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(fpath, "w") as f:
            json.dump(rec, f, indent=2, ensure_ascii=False)

        index[key] = {"family": family, "version": version, "file": fname,
                      "releaseTypes": sorted({a["releaseType"] for a in rec["announcements"]}),
                      "first_announced": rec["announcements"][0]["announced"],
                      "last_announced": rec["announcements"][-1]["announced"],
                      "sections": list(rec["sections"].keys())}
        if seen:
            filled += 1
        else:
            new += 1
        posts_seen[pid] = {"status": "captured", "key": key, "subject": p["subject"],
                           "post_time": p["post_time"], "releaseType": rtype}
        print(f"  + {key} ({rtype})")

    json.dump(dict(sorted(index.items())), open(index_path, "w"), indent=2)
    json.dump(posts_seen, open(posts_path, "w"), indent=2)
    unknown = [k for k in index if k.startswith("UNKNOWN/")]
    print(f"\nnew: {new}  filled: {filled}  pending (no changelog yet): {pending}  "
          f"versions indexed: {len(index)}")
    if unknown:
        print(f"UNKNOWN family: {unknown}", file=sys.stderr)


def reparse(out):
    """Rebuild every version file and index.json from stored html, fixing family/version/sections."""
    import glob
    index, posts_seen = {}, load(os.path.join(out, "posts.json"), {})
    files = glob.glob(os.path.join(out, "*", "*.json"))
    for fpath in files:
        rec = load(fpath, None)
        if not rec or "html" not in rec:
            continue
        if "announcements" not in rec:  # v1 file layout
            rec["announcements"] = [{"post_id": rec.get("post_id"), "releaseType": rec.get("releaseType"),
                                     "announced": rec.get("announced"), "post_subject": rec.get("post_subject"),
                                     "post_url": rec.get("post_url")}]
        title, family, version, sections = parse_changelog(rec["html"])
        subj = rec["announcements"][0]["post_subject"] if rec.get("announcements") else ""
        family = family or subject_family(subj)
        version = version or rec["version"]
        rec.update({"family": family, "version": version, "title": title, "sections": sections,
                    "text": "\n".join(html_to_lines(rec["html"]))})
        key = f"{family}/{version}"
        new_path = os.path.join(out, safe(family), safe(version) + ".json")
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        if os.path.abspath(new_path) != os.path.abspath(fpath):
            existing = load(new_path, None)
            if existing:  # merge announcements into the file that already owns this version
                for a in rec["announcements"]:
                    if not any(x["post_id"] == a["post_id"] for x in existing["announcements"]):
                        existing["announcements"].append(a)
                if rec["changelog_posted"] > existing["changelog_posted"]:
                    existing.update({k: rec[k] for k in ("title", "changelog_comment_id",
                                     "changelog_author", "changelog_posted", "sections", "text", "html")})
                rec = existing
            os.remove(fpath)
            print(f"  moved {os.path.relpath(fpath, out)} -> {os.path.relpath(new_path, out)}")
        rec["announcements"].sort(key=lambda a: a["announced"])
        json.dump(rec, open(new_path, "w"), indent=2, ensure_ascii=False)
        index[key] = {"family": family, "version": version,
                      "file": os.path.join(safe(family), safe(version) + ".json"),
                      "releaseTypes": sorted({a["releaseType"] for a in rec["announcements"]}),
                      "first_announced": rec["announcements"][0]["announced"],
                      "last_announced": rec["announcements"][-1]["announced"],
                      "sections": list(sections.keys())}
        for a in rec["announcements"]:
            if a["post_id"] in posts_seen:
                posts_seen[a["post_id"]]["key"] = key
    for d in glob.glob(os.path.join(out, "*")):
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
    json.dump(dict(sorted(index.items())), open(os.path.join(out, "index.json"), "w"), indent=2)
    json.dump(posts_seen, open(os.path.join(out, "posts.json"), "w"), indent=2)
    bad = [k for k in index if "/post-" in k or k.startswith("UNKNOWN/")]
    print(f"reparsed {len(files)} files, {len(index)} versions" + (f"; still unresolved: {bad}" if bad else ""))


if __name__ == "__main__":
    main()
