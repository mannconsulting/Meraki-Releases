/**
 * Gmail -> GitHub relay for Meraki firmware changelogs.
 *
 * Cisco Community blocks programmatic access to the firmware feed, but the board subscription
 * emails carry the same content. This script copies those emails, verbatim, into the
 * Meraki-Releases repo as inbox/<timestamp>.json. The repo's GitHub Action then runs
 * ingest_emails.py to turn them into firmware/<FAMILY>/<VERSION>.json.
 *
 * One-time setup (script.google.com, signed in as the subscribed mailbox):
 *   1. New project, paste this file.
 *   2. Project Settings > Script Properties: add GITHUB_TOKEN = a fine-grained personal access
 *      token limited to mannconsulting/Meraki-Releases with Contents: Read and write.
 *   3. Run backfill() once from the editor and approve the Gmail + external request scopes.
 *   4. Triggers > Add Trigger: function run, time-driven, every hour.
 *
 * Nothing is modified in Gmail. State is one script property (LAST_MS), the date of the newest
 * email already relayed. ingest_emails.py ignores message ids it has seen, so a repeat is harmless.
 */
const REPO = 'mannconsulting/Meraki-Releases';
const BRANCH = 'main';
const QUERY = 'from:noreply@community-email.cisco.com "Meraki Firmware Upgrades Feed"';
const PAGE = 100;

function run() {
  const props = PropertiesService.getScriptProperties();
  const last = Number(props.getProperty('LAST_MS') || 0);
  // Gmail's after: is day-granular in practice, so search a day early and filter by exact time.
  const since = last ? ' after:' + Math.floor((last - 86400000) / 1000) : ' newer_than:14d';
  relay_(QUERY + since, last, props);
}

/** Relay everything in the mailbox, oldest first. Safe to run more than once. */
function backfill() {
  relay_(QUERY, 0, PropertiesService.getScriptProperties());
}

function relay_(query, last, props) {
  const token = props.getProperty('GITHUB_TOKEN');
  if (!token) throw new Error('Set the GITHUB_TOKEN script property first.');

  const envelopes = [];
  for (let start = 0; ; start += PAGE) {
    const threads = GmailApp.search(query, start, PAGE);
    threads.forEach(function (t) {
      t.getMessages().forEach(function (m) {
        const ms = m.getDate().getTime();
        if (ms <= last) return;
        if (m.getFrom().indexOf('community-email.cisco.com') < 0) return;
        envelopes.push({
          id: m.getId(),
          threadId: t.getId(),
          date: m.getDate().toISOString(),
          subject: m.getSubject(),
          html: m.getBody(),
          _ms: ms
        });
      });
    });
    if (threads.length < PAGE) break;
  }
  if (!envelopes.length) return;

  envelopes.sort(function (a, b) { return a._ms - b._ms; });
  const newest = envelopes[envelopes.length - 1]._ms;
  envelopes.forEach(function (e) { delete e._ms; });

  const stamp = Utilities.formatDate(new Date(), 'UTC', "yyyyMMdd'T'HHmmss'Z'");
  const res = UrlFetchApp.fetch(
    'https://api.github.com/repos/' + REPO + '/contents/inbox/' + stamp + '.json', {
      method: 'put',
      contentType: 'application/json',
      headers: {
        Authorization: 'Bearer ' + token,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'
      },
      payload: JSON.stringify({
        message: 'Relay ' + envelopes.length + ' firmware feed email(s)',
        branch: BRANCH,
        content: Utilities.base64Encode(JSON.stringify(envelopes), Utilities.Charset.UTF_8)
      }),
      muteHttpExceptions: true
    });
  if (res.getResponseCode() !== 201) {
    // Leave LAST_MS alone so the next run retries the same emails.
    throw new Error('GitHub upload failed: ' + res.getResponseCode() + ' ' + res.getContentText().slice(0, 300));
  }
  props.setProperty('LAST_MS', String(newest));
}
