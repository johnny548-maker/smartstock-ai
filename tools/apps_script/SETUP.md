# SmartStock Monthly Rollover — Apps Script Setup

Google Apps Script that eliminates the monthly manual task of creating the next
`smartstock-allstocks-YYYY-MM` Sheet and dispatching the bootstrap workflow.

---

## 1. Why this exists

The CI service account's Drive quota is `0 bytes` (Google Workspace policy on
free-tier SA accounts), so the GitHub Actions bootstrap workflow **cannot**
create a brand-new Spreadsheet on its own — it can only seed tabs into a sheet
that already exists and is shared with it.

The historical workaround was a manual 1st-of-month task (create Sheet, share
with SA, dispatch workflow). This Apps Script replaces that task: it runs in
the **user's** account (which has the standard 15 GB Drive quota), creates the
Sheet, shares it to the SA, and triggers the existing bootstrap workflow via
the GitHub API.

See `.decisions/2026-06-25-allstocks-monthly-rollover-manual.md` for the
historical decision record (if present).

---

## 2. One-time setup (~5 min)

### Step a — Create a new Apps Script project
Open <https://script.google.com> → **New project**.

### Step b — Paste the script
Inside the editor:
- Delete the default `Code.gs` contents.
- Paste the entire contents of `tools/apps_script/monthly_rollover.gs`.

### Step c — Save and name the project
Press `Ctrl+S` (`Cmd+S` on macOS). Rename the project to
**SmartStock Monthly Rollover**.

### Step d — Get the service account email
1. Open the current month's Sheet
   (`1VqRmlyD2LcXye1flAE9kFeLs7oyrW9KyReAjTvB-iK8`).
2. Click **Share** (top-right).
3. The SA email appears in the access list — it looks like:
   `xxx@PROJECTNAME-XXXX.iam.gserviceaccount.com`.
4. Copy the exact address (case-sensitive).

### Step e — Create a GitHub PAT (classic)
1. Open <https://github.com/settings/tokens>.
2. **Generate new token (classic)**.
3. Note: `SmartStock Apps Script monthly rollover`.
4. Expiration: `1 year` (max recommended).
5. Scopes: tick **`repo`** + **`workflow`** (both required).
6. Generate. Copy the token (`ghp_...`) — you can never view it again.

### Step f — Set 4 Script properties
In the Apps Script editor, click the **gear icon** (Project Settings) on the
left sidebar, scroll down to **Script properties**, click **Add script
property** four times, and fill in:

| Property      | Value                                |
| ------------- | ------------------------------------ |
| `SA_EMAIL`    | (paste SA email from Step d)         |
| `GITHUB_PAT`  | (paste PAT from Step e)              |
| `GITHUB_REPO` | `johnny548-maker/smartstock-ai`      |
| `USER_EMAIL`  | `johnny548@gmail.com`                |

Click **Save script properties**.

### Step g — Install the monthly trigger
Back in the editor view:
1. Function dropdown (top toolbar) → select **`installMonthlyTrigger`**.
2. Click **Run**.
3. The first run prompts for **Authorization**. Click through:
   - "Review permissions" → choose your Google account
   - "Google hasn't verified this app" → **Advanced** → **Go to project (unsafe)**
   - Approve all requested scopes (Drive, Mail, External URL fetch).
4. Wait for the run to complete. Check the **Execution log** at the bottom
   for `Trigger installed: createNextMonthSheet will run on the 1st...`.

### Step h — Sanity-check immediately
1. Function dropdown → select **`testRunNow`** → **Run**.
2. Watch the Execution log; expect lines like:
   - `Created new sheet: smartstock-allstocks-2026-07 (...)`
   - `Shared to SA: ...`
   - `GH dispatch → HTTP 204: (empty — expected for 204 success)`
3. Verify each item in the checklist below.

---

## 3. Verification checklist

After Step h (`testRunNow`):

- [ ] Drive: new `smartstock-allstocks-<next-month>` shows up in your Drive
      (top-level "My Drive").
- [ ] Sheet permissions: open it → Share → SA email present as **Editor**.
- [ ] GitHub Actions:
      <https://github.com/johnny548-maker/smartstock-ai/actions> shows a new
      `allstocks-bootstrap` run (started by `workflow_dispatch`).
- [ ] Email: an email titled
      `✅ SmartStock monthly Sheet auto-created — YYYY-MM` arrives at
      `johnny548@gmail.com`.
- [ ] ~30 s later: the bootstrap workflow commits
      `_allstocks_sheets_index.json` to `main` with the new month's entry.

If all five pass, the monthly cron is live — zero further maintenance until
the GitHub PAT expires (≤ 1 year).

---

## 4. Maintenance

- **PAT expiry (≤ 1 year):** generate a new classic PAT
  (same `repo` + `workflow` scopes) and update the `GITHUB_PAT` Script
  property. No code change needed.
- **Disable temporarily:** Apps Script left sidebar → **Triggers** (clock
  icon) → delete the `createNextMonthSheet` trigger.
- **Re-enable:** run `installMonthlyTrigger()` again from the editor.
- **Change schedule:** edit `installMonthlyTrigger()` (e.g. `.atHour(8)` for
  08:00) → re-run it.

---

## 5. Troubleshooting

| Symptom                                                | Likely cause                                                       | Fix                                                                                        |
| ------------------------------------------------------ | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------ |
| `HTTP 401` in log                                      | PAT expired, missing scopes, or typo                               | Regenerate PAT with `repo` + `workflow`; update `GITHUB_PAT` property.                     |
| `HTTP 404` in log                                      | `allstocks-bootstrap.yml` missing on `main` or wrong `GITHUB_REPO` | Verify the workflow file exists on `main`; verify `GITHUB_REPO` value.                     |
| `HTTP 422` in log                                      | Bad `ref` or workflow `inputs` mismatch                            | Confirm workflow declares `existing_id` and `month` as inputs and `ref: main` exists.      |
| No email arriving                                      | MailApp daily quota hit (rare — we use ~1/mo)                      | Check Apps Script **Executions** tab; if quota exceeded wait 24 h.                         |
| Sheet created but SA can't see it                      | `SA_EMAIL` typo (case-sensitive)                                   | Verify exact SA email; re-share manually from the Sheet if needed.                         |
| "Authorization required" loop                          | OAuth scopes changed or revoked                                    | Editor → **Run** → re-approve the permissions dialog.                                      |
| `testRunNow` reuses last month                         | Ran very late on the last day; clock-edge case                     | Harmless — it'll create the right one on the 1st. Or rerun on the new day.                 |
| GH Actions run never appears                           | Cron fired but dispatch was rejected silently                      | Check Apps Script **Executions** log for the HTTP code; follow the row above.              |

---

## 6. Cost & quota footprint

All well under free-tier limits:

| Resource                  | Limit               | Our usage             | % of quota |
| ------------------------- | ------------------- | --------------------- | ---------- |
| MailApp `sendEmail`       | 100/day (Gmail)     | ~1/month              | <1 %       |
| UrlFetchApp `fetch`       | 20 000/day          | ~1/month              | <1 %       |
| Drive storage             | 15 GB (Gmail)       | ~1 MB/sheet × 12/yr   | 0.08 %     |
| Apps Script execution     | 6 min/run           | <30 s typical         | <10 %      |
| Trigger total             | 20/script           | 1                     | 5 %        |

Cost: $0/yr.
