/**
 * SmartStock All-Stocks — Monthly Sheet Auto-Rollover
 *
 * Runs 1st of each month at 06:00 user TZ.
 * 1. Creates `smartstock-allstocks-YYYY-MM` (next month) in user's Drive (user's 15GB quota).
 * 2. Shares to SA email as Editor.
 * 3. Triggers GH Actions `allstocks-bootstrap.yml` with new sheet_id → seeds tabs + commits index.
 * 4. Emails user on success or failure.
 *
 * One-time setup: see tools/apps_script/SETUP.md
 *
 * Script Properties required (Project Settings → Script properties):
 *   SA_EMAIL     = <service account email from existing sheet share dialog>
 *   GITHUB_PAT   = <classic PAT with repo + workflow scopes>
 *   GITHUB_REPO  = johnny548-maker/smartstock-ai
 *   USER_EMAIL   = johnny548@gmail.com  (for notification)
 */

const REPO_DEFAULT   = 'johnny548-maker/smartstock-ai';
const WORKFLOW_FILE  = 'allstocks-bootstrap.yml';
const TITLE_PREFIX   = 'smartstock-allstocks-';
const GH_API_VERSION = '2022-11-28';

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
// Core: create next month's Sheet + dispatch bootstrap workflow
// ---------------------------------------------------------------------------

function createNextMonthSheet() {
  const saEmail   = getProp_('SA_EMAIL');
  const pat       = getProp_('GITHUB_PAT');
  const repo      = getProp_('GITHUB_REPO', REPO_DEFAULT);
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
  const sheetId = sheet.getId();

  // ------------------------------------------------------------------
  // 2. Share to SA as Editor (addEditor is idempotent — safe if already shared)
  // ------------------------------------------------------------------
  try {
    sheet.addEditor(saEmail);
    Logger.log(`Shared to SA: ${saEmail}`);
  } catch (e) {
    // Non-fatal: log and continue; SA may already be an editor
    Logger.log(`SA share warning (may already exist): ${e}`);
  }

  // ------------------------------------------------------------------
  // 3. Trigger GH Actions bootstrap workflow
  // ------------------------------------------------------------------
  const ghUrl = `https://api.github.com/repos/${repo}/actions/workflows/${WORKFLOW_FILE}/dispatches`;
  const dispatchPayload = {
    ref: 'main',
    inputs: {
      existing_id: sheetId,
      month:       month,
    },
  };

  const response = UrlFetchApp.fetch(ghUrl, {
    method:            'post',
    contentType:       'application/json',
    headers: {
      'Authorization':        `Bearer ${pat}`,
      'Accept':               'application/vnd.github+json',
      'X-GitHub-Api-Version': GH_API_VERSION,
    },
    payload:            JSON.stringify(dispatchPayload),
    muteHttpExceptions: true,
  });

  const httpCode = response.getResponseCode();
  const httpBody = response.getContentText();
  Logger.log(`GH dispatch → HTTP ${httpCode}: ${httpBody || '(empty — expected for 204 success)'}`);

  // ------------------------------------------------------------------
  // 4. Notify user
  // ------------------------------------------------------------------
  const sheetUrl  = `https://docs.google.com/spreadsheets/d/${sheetId}`;
  const actionsUrl = `https://github.com/${repo}/actions`;

  if (httpCode >= 200 && httpCode < 300) {
    notify_(
      userEmail,
      `✅ SmartStock monthly Sheet auto-created — ${month}`,
      [
        `Month:   ${month}`,
        `Sheet:   ${title}`,
        `URL:     ${sheetUrl}`,
        `Actions: ${actionsUrl}`,
        '',
        'The bootstrap workflow was dispatched.',
        'It will add 13 tabs + commit the index file in ~30 s.',
      ].join('\n')
    );
    Logger.log('SUCCESS — sheet created, SA shared, bootstrap dispatched, email sent.');
  } else {
    const manualCmd =
      `gh workflow run ${WORKFLOW_FILE} --repo ${repo}` +
      ` -f existing_id=${sheetId} -f month=${month}`;
    notify_(
      userEmail,
      `🔴 SmartStock monthly rollover FAILED — ${month}`,
      [
        `Month:    ${month}`,
        `Sheet:    ${title} (created OK, ID: ${sheetId})`,
        `URL:      ${sheetUrl}`,
        '',
        `GH dispatch failed: HTTP ${httpCode}`,
        `Body: ${httpBody}`,
        '',
        'MANUAL FIX:',
        manualCmd,
      ].join('\n')
    );
    throw new Error(`GH dispatch failed: HTTP ${httpCode} — ${httpBody}`);
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
