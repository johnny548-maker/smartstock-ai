# SmartStock Monthly Rollover — Apps Script Setup

Google Apps Script that eliminates the monthly manual task of creating the next
`smartstock-allstocks-YYYY-MM` Sheet. It runs in the **user's** account (15 GB
Drive quota), creates the Sheet, and shares it to the CI service account (SA).

**No GitHub token required.** Seeding the 13 tabs and committing the index file is
handled by the scheduled `allstocks-auto-register.yml` GitHub Actions workflow,
which runs on the 2nd of each month, discovers any sheet shared with the SA, and
registers it automatically.

---

## 1. Why this exists

The CI service account's Drive quota is `0 bytes` (Google Workspace policy on
free-tier SA accounts), so the GitHub Actions workflow **cannot** create a
brand-new Spreadsheet on its own — it can only seed tabs into a sheet that
already exists and is shared with it.

This Apps Script does the one thing the SA cannot: create the Sheet (in the
user's 15 GB Drive) and share it to the SA. From there, the scheduled
`allstocks-auto-register` workflow takes over — it lists every sheet the SA can
access, finds the new month, seeds its tabs, and commits the index. Because that
registration runs on a schedule (no `workflow_dispatch`), the Apps Script needs
**no GitHub PAT** — there is nothing to expire or rotate.

See `.decisions/2026-06-25-allstocks-monthly-rollover-manual.md` for the
historical decision record (if present).

---

## 2. One-time setup (~3 min)

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

### Step e — Set 2 Script properties
In the Apps Script editor, click the **gear icon** (Project Settings) on the
left sidebar, scroll down to **Script properties**, click **Add script
property**, and fill in:

| Property     | Value                                | Required |
| ------------ | ------------------------------------ | -------- |
| `SA_EMAIL`   | (paste SA email from Step d)         | Yes      |
| `USER_EMAIL` | `johnny548@gmail.com`                | Optional (defaults to active user's email) |

Click **Save script properties**.

### Step f — Install the monthly trigger
Back in the editor view:
1. Function dropdown (top toolbar) → select **`installMonthlyTrigger`**.
2. Click **Run**.
3. The first run prompts for **Authorization**. Click through:
   - "Review permissions" → choose your Google account
   - "Google hasn't verified this app" → **Advanced** → **Go to project (unsafe)**
   - Approve all requested scopes (Drive, Mail).
4. Wait for the run to complete. Check the **Execution log** at the bottom
   for `Trigger installed: createNextMonthSheet will run on the 1st...`.

### Step g — Sanity-check immediately
1. Function dropdown → select **`testRunNow`** → **Run**.
2. Watch the Execution log; expect lines like:
   - `Created new sheet: smartstock-allstocks-2026-07 (...)`
   - `Shared to SA: ...`
   - `SUCCESS — sheet created, SA shared, user notified.`
3. Verify each item in the checklist below.

---

## 3. Verification checklist

After Step g (`testRunNow`):

- [ ] Drive: new `smartstock-allstocks-<next-month>` shows up in your Drive
      (top-level "My Drive").
- [ ] Sheet permissions: open it → Share → SA email present as **Editor**.
- [ ] Email: an email titled
      `✅ SmartStock monthly Sheet auto-created — YYYY-MM` arrives at
      `johnny548@gmail.com`.
- [ ] Within ~1 day, the scheduled `allstocks-auto-register` workflow (or a
      manual `gh workflow run allstocks-auto-register.yml --repo johnny548-maker/smartstock-ai`)
      seeds the 13 tabs and commits `_allstocks_sheets_index.json` to `main`
      with the new month's entry.

If all pass, the monthly cron is live — **zero further maintenance** (no token to
expire).

---

## 4. Maintenance

- **No token to rotate.** Registration is keyless (scheduled workflow + SA
  secret already configured in the repo).
- **Disable temporarily:** Apps Script left sidebar → **Triggers** (clock
  icon) → delete the `createNextMonthSheet` trigger.
- **Re-enable:** run `installMonthlyTrigger()` again from the editor.
- **Change schedule:** edit `installMonthlyTrigger()` (e.g. `.atHour(8)` for
  08:00) → re-run it.
- **Force registration now** (don't wait for the 2nd):
  `gh workflow run allstocks-auto-register.yml --repo johnny548-maker/smartstock-ai`.

---

## 5. Troubleshooting

| Symptom                                                | Likely cause                                                       | Fix                                                                                        |
| ------------------------------------------------------ | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------ |
| No email arriving                                      | MailApp daily quota hit (rare — we use ~1/mo)                      | Check Apps Script **Executions** tab; if quota exceeded wait 24 h.                         |
| Sheet created but SA can't see it                      | `SA_EMAIL` typo (case-sensitive)                                   | Verify exact SA email; re-share manually from the Sheet if needed.                         |
| `🔴 ... rollover FAILED` email arrives                  | `addEditor(SA_EMAIL)` threw (e.g. bad SA email)                    | Fix `SA_EMAIL`; re-run `testRunNow`, or run the manual `gh workflow run` in the email.     |
| "Authorization required" loop                          | OAuth scopes changed or revoked                                    | Editor → **Run** → re-approve the permissions dialog.                                      |
| `testRunNow` reuses last month                         | Ran very late on the last day; clock-edge case                     | Harmless — it'll create the right one on the 1st. Or rerun on the new day.                 |
| Tabs never appear / index not committed                | auto-register workflow hasn't run yet, or `GOOGLE_SA_JSON` secret missing | Check Actions; run `gh workflow run allstocks-auto-register.yml --repo johnny548-maker/smartstock-ai`; verify the repo secret. |

---

## 6. Cost & quota footprint

All well under free-tier limits:

| Resource                  | Limit               | Our usage             | % of quota |
| ------------------------- | ------------------- | --------------------- | ---------- |
| MailApp `sendEmail`       | 100/day (Gmail)     | ~1/month              | <1 %       |
| Drive storage             | 15 GB (Gmail)       | ~1 MB/sheet × 12/yr   | 0.08 %     |
| Apps Script execution     | 6 min/run           | <10 s typical         | <10 %      |
| Trigger total             | 20/script           | 1                     | 5 %        |

Cost: $0/yr.
