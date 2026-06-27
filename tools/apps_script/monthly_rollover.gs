/**
 * SmartStock All-Stocks — Monthly Sheet Auto-Rollover (keyless / no GitHub token)
 *
 * Runs 1st of each month at 06:00 user TZ.
 * 1. Creates `smartstock-allstocks-YYYY-MM` (next month) in user's Drive (user's 15GB quota).
 * 2. Shares to SA email as Editor.
 * 3. Emails user on success or failure.
 *
 * Registration (seeding the 13 tabs + committing the index) is handled entirely by the
 * scheduled GitHub Actions workflow `allstocks-auto-register.yml`, which runs on the 2nd of
 * each month, discovers any user-created sheet shared with the SA, and registers it. No GitHub
 * token is needed here — the previous UrlFetchApp workflow_dispatch (and its PAT) is removed.
 *
 * One-time setup: see tools/apps_script/SETUP.md
 *
 * Script Properties required (Project Settings → Script properties):
 *   SA_EMAIL    = <service account email from existing sheet share dialog>
 *   USER_EMAIL  = johnny548@gmail.com  (optional — falls back to the active user's email)
 */

const TITLE_PREFIX = 'smartstock-allstocks-';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getProp_(name, fallback) {
  const v = PropertiesService.getScriptProperties().getProperty(name);
  if (!v && fallback === undefined) {
    throw new Error(
      `Script property '${name}' is required. Set it via Project Settings → Script properties.`
    );
  }
  return v || fallback;
}

/**
 * Returns "YYYY-MM" for next month relative to `now`.
 * @param {Date} now
 * @returns {string}
 */
function nextMonthYYYYMM_(now) {
  const next = new Date(now.getFullYear(), now.getMonth() + 1, 1);
  const y    = next.getFullYear();
  const m    = String(next.getMonth() + 1).padStart(2, '0');
  return `${y}-${m}`;
}

/**
 * Sends a notification email; swallows errors so main flow isn't blocked.
 * @param {string} email
 * @param {string} subject
 * @param {string} body
 */
function notify_(email, subject, body) {
  try {
    MailApp.sendEmail(email, subject, body);
  } catch (e) {
    Logger.log(`Email failed (non-fatal): ${e}`);
  }
}

// ---------------------------------------------------------------------------
// Core: create next month's Sheet + share to SA (registration is CI's job)
// ---------------------------------------------------------------------------

function createNextMonthSheet() {
  const saEmail   = getProp_('SA_EMAIL');
  const userEmail = getProp_('USER_EMAIL', Session.getActiveUser().getEmail());

  const month = nextMonthYYYYMM_(new Date());
  const title = TITLE_PREFIX + month;

  // ------------------------------------------------------------------
  // 1. Create Sheet (idempotent: reuse if already exists, don't duplicate)
  // ------------------------------------------------------------------
  let sheet;
  const existing = DriveApp.getFilesByName(title);
  if (existing.hasNext()) {
    const f = existing.next();
    sheet = SpreadsheetApp.openById(f.getId());
    Logger.log(`Reusing existing sheet: ${title} (${sheet.getId()})`);
  } else {
    sheet = SpreadsheetApp.create(title);
    Logger.log(`Created new sheet: ${title} (${sheet.getId()})`);
  }
  const sheetId  = sheet.getId();
  const sheetUrl = `https://docs.google.com/spreadsheets/d/${sheetId}`;

  try {
    // ----------------------------------------------------------------
    // 2. Share to SA as Editor (addEditor is idempotent — safe if already shared).
    //    This is what lets the scheduled allstocks-auto-register workflow open the
    //    sheet headlessly and seed its tabs.
    // ----------------------------------------------------------------
    sheet.addEditor(saEmail);
    Logger.log(`Shared to SA: ${saEmail}`);

    // ----------------------------------------------------------------
    // 3. Notify user — success.
    // ----------------------------------------------------------------
    notify_(
      userEmail,
      `✅ SmartStock monthly Sheet auto-created — ${month}`,
      [
        `Month:  ${month}`,
        `Sheet:  ${title}`,
        `URL:    ${sheetUrl}`,
        `Shared: ${saEmail} (Editor)`,
        '',
        'A scheduled GitHub Actions workflow (allstocks-auto-register, runs',
        'monthly on the 2nd) will detect this sheet and seed its 13 tabs +',
        'register it automatically — no token needed.',
      ].join('\n')
    );
    Logger.log('SUCCESS — sheet created, SA shared, user notified.');
  } catch (e) {
    // Any failure (e.g. the SA share) → notify the user with a manual fallback and rethrow
    // so the run is marked failed in the Apps Script Executions log.
    const manualCmd =
      'gh workflow run allstocks-bootstrap.yml' +
      ' --repo johnny548-maker/smartstock-ai' +
      ` -f existing_id=${sheetId} -f month=${month}`;
    notify_(
      userEmail,
      `🔴 SmartStock monthly rollover FAILED — ${month}`,
      [
        `Month:  ${month}`,
        `Sheet:  ${title} (created OK, ID: ${sheetId})`,
        `URL:    ${sheetUrl}`,
        '',
        `Error: ${e}`,
        '',
        'MANUAL FIX (seed tabs now instead of waiting for the 2nd):',
        manualCmd,
      ].join('\n')
    );
    throw e;
  }
}

// ---------------------------------------------------------------------------
// One-time setup: install the monthly time trigger
// ---------------------------------------------------------------------------

/**
 * Run ONCE from the Apps Script UI after pasting the script and setting Script Properties.
 * Installs: createNextMonthSheet fires on the 1st of every month at 06:00 user TZ.
 * Idempotent — removes any existing triggers for this function before installing.
 */
function installMonthlyTrigger() {
  // Remove existing triggers for this handler to avoid duplicates
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'createNextMonthSheet') {
      ScriptApp.deleteTrigger(t);
    }
  });

  ScriptApp.newTrigger('createNextMonthSheet')
    .timeBased()
    .onMonthDay(1)
    .atHour(6)
    .create();

  Logger.log(
    'Trigger installed: createNextMonthSheet will run on the 1st of every month at 06:00 user TZ.'
  );
}

// ---------------------------------------------------------------------------
// Manual test — run createNextMonthSheet immediately (does NOT wait for cron)
// ---------------------------------------------------------------------------

/**
 * Sanity-check: runs createNextMonthSheet right now.
 * Use this after initial setup to verify everything is wired correctly.
 */
function testRunNow() {
  createNextMonthSheet();
}
