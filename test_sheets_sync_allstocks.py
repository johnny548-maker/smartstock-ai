"""Tests for sheets_sync_allstocks.py — bootstrap mode + 7 P0 tab schemas.

All tests run offline (no gspread network calls). The _FakeWorksheet /
_FakeSpreadsheet / _FakeClient pattern mirrors test_sheets_sync.py so the two
test modules share the same idiom.

CONTRACT: these cover BOOTSTRAP ONLY.  Fetcher tests live in Sprint 2 P2.
"""
import contextlib
import csv
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import sheets_sync_allstocks as sa


# ── Fake gspread layer ────────────────────────────────────────────────────────

class _FakeWorksheet:
    """Minimal worksheet double that records the header written to row 1."""

    def __init__(self, title, headers=None):
        self.title = title
        self._rows = []
        if headers:
            self._rows.append(headers[:])

    def row_values(self, n):
        if n == 1 and self._rows:
            return self._rows[0]
        return []

    def update(self, values, range_name=None):
        if range_name == "A1" or range_name is None:
            if self._rows:
                self._rows[0] = values[0]
            else:
                self._rows.insert(0, values[0])

    def col_values(self, col):
        return [r[col - 1] if col - 1 < len(r) else "" for r in self._rows]

    def append_rows(self, rows, value_input_option=None):
        self._rows.extend(rows)


class _FakeSpreadsheet:
    """Minimal spreadsheet double. Supports worksheet(title) / add_worksheet / share."""

    def __init__(self, title="", sheet_id="FAKE_ID_001"):
        self.title = title
        self.id = sheet_id
        self._worksheets = {}
        self._shared = []

    def worksheet(self, title):
        if title not in self._worksheets:
            raise Exception(f"worksheet '{title}' not found")
        return self._worksheets[title]

    def add_worksheet(self, title, rows=1000, cols=26):
        ws = _FakeWorksheet(title)
        self._worksheets[title] = ws
        return ws

    def share(self, email, perm_type="user", role="writer", notify=False):
        self._shared.append({"email": email, "perm_type": perm_type, "role": role})

    def worksheets(self):
        return list(self._worksheets.values())


class _FakeClient:
    """Minimal gspread client double used to control openall() / create() behaviour."""

    def __init__(self, existing=None):
        # existing: list of _FakeSpreadsheet already 'in the drive'
        self._drive = list(existing or [])
        self.create_calls = []
        self.open_by_key_calls = []

    def openall(self):
        return list(self._drive)

    def create(self, title):
        sh = _FakeSpreadsheet(title=title)
        self._drive.append(sh)
        self.create_calls.append(title)
        return sh

    def open_by_key(self, key):
        self.open_by_key_calls.append(key)
        for sh in self._drive:
            if sh.id == key:
                return sh
        raise Exception(f"Spreadsheet not found for key: {key}")


# ── Helper ────────────────────────────────────────────────────────────────────

def _run_bootstrap(month="2026-06", user_email="test@example.com",
                   existing_sheets=None, index_path=None):
    """Run bootstrap() with a fake client; returns (result_dict, fake_client, fake_sh)."""
    client = _FakeClient(existing=existing_sheets)
    with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
        result = sa.bootstrap(month=month, user_email=user_email,
                              index_path=index_path or os.devnull)
    return result, client


# ── Tests: creation / idempotency ─────────────────────────────────────────────

class TestBootstrapCreatesSpreadsheet(unittest.TestCase):
    def test_creates_when_absent(self):
        """Fake client has no existing sheets → bootstrap creates one, returns an ID."""
        result, client = _run_bootstrap()
        self.assertEqual(len(client.create_calls), 1)
        self.assertIn("2026-06", client.create_calls[0])
        self.assertIsNotNone(result["id"])
        self.assertNotEqual(result["id"], "")

    def test_returns_id_on_create(self):
        result, _ = _run_bootstrap()
        self.assertEqual(result["title"], "smartstock-allstocks-2026-06")
        # URL contains the sheet ID (not the month string); verify both are present.
        self.assertIn(result["id"], result["url"])
        self.assertIn("docs.google.com/spreadsheets", result["url"])


class TestBootstrapSkipsCreateWhenExists(unittest.TestCase):
    def test_skips_create_when_title_matches(self):
        """Fake client already has a matching sheet → bootstrap returns existing ID, no create."""
        existing = _FakeSpreadsheet(title="smartstock-allstocks-2026-06",
                                    sheet_id="EXISTING_999")
        result, client = _run_bootstrap(existing_sheets=[existing])
        self.assertEqual(len(client.create_calls), 0,
                         "must NOT call create() when sheet already exists")
        self.assertEqual(result["id"], "EXISTING_999")

    def test_returns_existing_title_and_url(self):
        existing = _FakeSpreadsheet(title="smartstock-allstocks-2026-06",
                                    sheet_id="EXISTING_999")
        result, _ = _run_bootstrap(existing_sheets=[existing])
        self.assertEqual(result["title"], "smartstock-allstocks-2026-06")
        self.assertIn("EXISTING_999", result["url"])


# ── Tests: 7 tabs ─────────────────────────────────────────────────────────────

class TestBootstrapEnsuresAllSevenTabs(unittest.TestCase):
    def test_exactly_7_worksheets_after_bootstrap(self):
        """Sprint 3 #22: tab count grew from 7 (P0) to 13 (P0+6 P1)."""
        result, client = _run_bootstrap()
        sh = client._drive[0]
        self.assertEqual(len(sh._worksheets), 13,
                         f"expected 13 tabs (7 P0 + 6 P1), got {len(sh._worksheets)}: "
                         f"{list(sh._worksheets.keys())}")

    def test_tab_names_match_spec(self):
        """Sprint 3 #22: tab set includes 7 P0 + 6 P1 sources."""
        result, client = _run_bootstrap()
        sh = client._drive[0]
        expected = {
            "bwibbu_daily", "mi_margn_daily", "t86_daily",
            "tpex_3insti_daily", "tpex_margin_daily", "tpex_per_daily",
            "tdcc_weekly",
            "stock_day_all_daily", "t187ap03_monthly", "notice_punish_daily",
            "sec_frames_quarterly", "sec_ftd_semimonthly", "cnyes_news_daily",
        }
        self.assertEqual(set(sh._worksheets.keys()), expected)

    def test_each_tab_has_header_row(self):
        result, client = _run_bootstrap()
        sh = client._drive[0]
        for title, ws in sh._worksheets.items():
            header = ws.row_values(1)
            self.assertGreater(len(header), 0,
                               f"tab {title!r} must have a non-empty header row")
            self.assertEqual(header[0], header[0].strip(),
                             f"tab {title!r} header[0] must not have whitespace")


# ── Tests: tab headers match spec exactly ─────────────────────────────────────

class TestTabHeadersMatchSpec(unittest.TestCase):
    """Each of the 7 tabs must have headers matching sa.TAB_HEADERS exactly."""

    def _get_ws(self, tab_name):
        result, client = _run_bootstrap()
        sh = client._drive[0]
        return sh._worksheets[tab_name]

    def test_bwibbu_daily_headers(self):
        ws = self._get_ws("bwibbu_daily")
        self.assertEqual(ws.row_values(1), sa.TAB_HEADERS["bwibbu_daily"])

    def test_mi_margn_daily_headers(self):
        ws = self._get_ws("mi_margn_daily")
        self.assertEqual(ws.row_values(1), sa.TAB_HEADERS["mi_margn_daily"])

    def test_t86_daily_headers(self):
        ws = self._get_ws("t86_daily")
        self.assertEqual(ws.row_values(1), sa.TAB_HEADERS["t86_daily"])

    def test_tpex_3insti_daily_headers(self):
        ws = self._get_ws("tpex_3insti_daily")
        self.assertEqual(ws.row_values(1), sa.TAB_HEADERS["tpex_3insti_daily"])

    def test_tpex_margin_daily_headers(self):
        ws = self._get_ws("tpex_margin_daily")
        self.assertEqual(ws.row_values(1), sa.TAB_HEADERS["tpex_margin_daily"])

    def test_tpex_per_daily_headers(self):
        ws = self._get_ws("tpex_per_daily")
        self.assertEqual(ws.row_values(1), sa.TAB_HEADERS["tpex_per_daily"])

    def test_tdcc_weekly_headers(self):
        ws = self._get_ws("tdcc_weekly")
        self.assertEqual(ws.row_values(1), sa.TAB_HEADERS["tdcc_weekly"])


# ── Tests: sharing ────────────────────────────────────────────────────────────

class TestBootstrapSharesWithUserEmail(unittest.TestCase):
    def test_share_called_with_writer_role(self):
        result, client = _run_bootstrap(user_email="alice@example.com")
        sh = client._drive[0]
        roles = [s["role"] for s in sh._shared]
        self.assertIn("writer", roles, "must share with role='writer'")

    def test_share_called_with_correct_email(self):
        result, client = _run_bootstrap(user_email="alice@example.com")
        sh = client._drive[0]
        emails = [s["email"] for s in sh._shared]
        self.assertIn("alice@example.com", emails)

    def test_result_user_shared_field(self):
        result, _ = _run_bootstrap(user_email="bob@example.com")
        self.assertEqual(result["shared"], "bob@example.com")


# ── Tests: index file ─────────────────────────────────────────────────────────

class TestBootstrapWritesIndexFile(unittest.TestCase):
    def test_index_file_created(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            f.write("{}")
            tmp = f.name
        try:
            _run_bootstrap(month="2026-06", index_path=tmp)
            with open(tmp, encoding="utf-8") as f:
                data = json.load(f)
            self.assertIn("2026-06", data)
        finally:
            os.unlink(tmp)

    def test_index_file_contains_sheet_id(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            f.write("{}")
            tmp = f.name
        try:
            result, _ = _run_bootstrap(month="2026-07", index_path=tmp)
            with open(tmp, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["2026-07"], result["id"])
        finally:
            os.unlink(tmp)

    def test_index_file_preserves_existing_entries(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({"2026-05": "OLD_ID_ABC"}, f)
            tmp = f.name
        try:
            _run_bootstrap(month="2026-06", index_path=tmp)
            with open(tmp, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["2026-05"], "OLD_ID_ABC",
                             "existing month entries must not be overwritten")
            self.assertIn("2026-06", data)
        finally:
            os.unlink(tmp)


# ── Tests: graceful SA missing ────────────────────────────────────────────────

class TestBootstrapGracefulWhenSAMissing(unittest.TestCase):
    def test_exits_0_when_google_sa_json_missing(self):
        env = {k: v for k, v in os.environ.items() if k != "GOOGLE_SA_JSON"}
        with mock.patch.dict(os.environ, env, clear=True):
            rc = sa.main(["--bootstrap"])
        self.assertEqual(rc, 0)

    def test_prints_skip_message_when_sa_missing(self):
        env = {k: v for k, v in os.environ.items() if k != "GOOGLE_SA_JSON"}
        with mock.patch.dict(os.environ, env, clear=True):
            captured = io.StringIO()
            with mock.patch("sys.stdout", captured):
                sa.main(["--bootstrap"])
        out = captured.getvalue()
        self.assertIn("SKIP", out)
        self.assertIn("GOOGLE_SA_JSON", out)

    def test_exits_0_when_google_sa_json_blank(self):
        with mock.patch.dict(os.environ, {"GOOGLE_SA_JSON": "   "}):
            rc = sa.main(["--bootstrap"])
        self.assertEqual(rc, 0)


# ── Tests: stdout export line ─────────────────────────────────────────────────

class TestBootstrapPrintsSecretExportLine(unittest.TestCase):
    def _capture_main_bootstrap(self, month="2026-06",
                                 user_email="test@example.com"):
        client = _FakeClient()
        captured = io.StringIO()
        # main() short-circuits on missing GOOGLE_SA_JSON before calling get_client(),
        # so we must fake the env var AND mock get_client() to return our fake client.
        fake_env = {"GOOGLE_SA_JSON": '{"type":"service_account"}'}
        with mock.patch.dict(os.environ, fake_env):
            with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
                with mock.patch("sys.stdout", captured):
                    rc = sa.main(["--bootstrap", "--month", month,
                                   "--user-email", user_email,
                                   "--index-path", os.devnull])
        return rc, captured.getvalue(), client

    def test_prints_sheets_id_line(self):
        rc, out, _ = self._capture_main_bootstrap()
        self.assertEqual(rc, 0)
        self.assertIn("SHEETS_ID_ALLSTOCKS_TW=", out)

    def test_prints_title_line(self):
        _, out, _ = self._capture_main_bootstrap(month="2026-06")
        self.assertIn("TITLE=smartstock-allstocks-2026-06", out)

    def test_prints_url_line(self):
        _, out, _ = self._capture_main_bootstrap()
        self.assertIn("URL=https://docs.google.com/spreadsheets/d/", out)

    def test_prints_tabs_line_with_all_7(self):
        _, out, _ = self._capture_main_bootstrap()
        self.assertIn("TABS=", out)
        tabs_line = next(l for l in out.splitlines() if l.startswith("TABS="))
        tabs = tabs_line.replace("TABS=", "").split(",")
        self.assertEqual(len(tabs), 13)   # Sprint 3 #22: 7 P0 + 6 P1

    def test_prints_user_shared_line(self):
        _, out, _ = self._capture_main_bootstrap(user_email="alice@example.com")
        self.assertIn("USER_SHARED=alice@example.com", out)


# ── Tests: idempotency ────────────────────────────────────────────────────────

class TestBootstrapIdempotent(unittest.TestCase):
    def test_second_call_does_not_duplicate_tabs(self):
        """Call bootstrap() twice; second call must not double-add tabs."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            f.write("{}")
            tmp = f.name
        try:
            client = _FakeClient()
            with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
                sa.bootstrap(month="2026-06", user_email="x@x.com", index_path=tmp)
            # second call — same client still holds the created sheet in _drive
            with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
                sa.bootstrap(month="2026-06", user_email="x@x.com", index_path=tmp)
            sh = client._drive[0]
            self.assertEqual(len(sh._worksheets), 13,
                             "tabs must NOT be duplicated on second bootstrap call "
                             "(Sprint 3 #22: 13 = 7 P0 + 6 P1)")
        finally:
            os.unlink(tmp)

    def test_second_call_does_not_create_second_spreadsheet(self):
        client = _FakeClient()
        with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
            sa.bootstrap(month="2026-06", user_email="x@x.com",
                         index_path=os.devnull)
        with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
            sa.bootstrap(month="2026-06", user_email="x@x.com",
                         index_path=os.devnull)
        self.assertEqual(len(client.create_calls), 1,
                         "create() must only be called once (idempotent)")


# ── Tests: pure helpers ───────────────────────────────────────────────────────

class TestComputeTitle(unittest.TestCase):
    def test_title_format(self):
        self.assertEqual(sa._compute_title("2026-06"), "smartstock-allstocks-2026-06")
        self.assertEqual(sa._compute_title("2025-12"), "smartstock-allstocks-2025-12")


class TestFindExisting(unittest.TestCase):
    def test_returns_none_when_not_found(self):
        sh_other = _FakeSpreadsheet(title="smartstock-allstocks-2025-05")
        client = _FakeClient(existing=[sh_other])
        result = sa._find_existing(client, "smartstock-allstocks-2026-06")
        self.assertIsNone(result)

    def test_returns_spreadsheet_when_found(self):
        sh = _FakeSpreadsheet(title="smartstock-allstocks-2026-06",
                              sheet_id="XYZ123")
        client = _FakeClient(existing=[sh])
        result = sa._find_existing(client, "smartstock-allstocks-2026-06")
        self.assertIsNotNone(result)
        self.assertEqual(result.id, "XYZ123")


# ── Helper for --existing-id path ─────────────────────────────────────────────

def _run_bootstrap_existing_id(existing_id, month="2026-06",
                                user_email="test@example.com",
                                pre_existing_sheets=None,
                                index_path=None):
    """Run bootstrap() passing existing_id; pre_existing_sheets must include the
    sheet with that ID so open_by_key() resolves it in the fake client."""
    client = _FakeClient(existing=pre_existing_sheets or [])
    with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
        result = sa.bootstrap(month=month, user_email=user_email,
                              existing_id=existing_id,
                              index_path=index_path or os.devnull)
    return result, client


# ── Tests: --existing-id path ─────────────────────────────────────────────────

class TestBootstrapWithExistingIdSkipsCreate(unittest.TestCase):
    """bootstrap(existing_id=...) must NOT call create() and must open by key."""

    def test_bootstrap_with_existing_id_skips_create_and_uses_given_sheet(self):
        """Pass existing_id → no create call; sheet opened by key; 7 tabs ensured."""
        sh = _FakeSpreadsheet(title="smartstock-allstocks-2026-06",
                              sheet_id="abc123")
        result, client = _run_bootstrap_existing_id(
            existing_id="abc123", pre_existing_sheets=[sh])
        self.assertEqual(len(client.create_calls), 0,
                         "create() must NOT be called when existing_id is supplied")
        self.assertIn("abc123", client.open_by_key_calls,
                      "open_by_key('abc123') must be called")
        self.assertEqual(result["id"], "abc123")
        # 13 tabs must be ensured (Sprint 3 #22: 7 P0 + 6 P1)
        self.assertEqual(len(sh._worksheets), 13)


class TestBootstrapWithExistingIdWritesIndex(unittest.TestCase):
    """bootstrap(existing_id=...) must write the exact given ID to the index file."""

    def test_bootstrap_with_existing_id_writes_index_with_given_id(self):
        sh = _FakeSpreadsheet(title="smartstock-allstocks-2026-06",
                              sheet_id="abc123")
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            f.write("{}")
            tmp = f.name
        try:
            _run_bootstrap_existing_id(
                existing_id="abc123", month="2026-06",
                pre_existing_sheets=[sh], index_path=tmp)
            with open(tmp, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data.get("2026-06"), "abc123",
                             "_allstocks_sheets_index.json must contain the given ID")
        finally:
            os.unlink(tmp)


class TestBootstrapWithExistingIdStillShares(unittest.TestCase):
    """bootstrap(existing_id=...) must still share with user_email (idempotent)."""

    def test_bootstrap_with_existing_id_still_shares_to_user(self):
        sh = _FakeSpreadsheet(title="smartstock-allstocks-2026-06",
                              sheet_id="abc123")
        result, client = _run_bootstrap_existing_id(
            existing_id="abc123", user_email="alice@example.com",
            pre_existing_sheets=[sh])
        emails = [s["email"] for s in sh._shared]
        self.assertIn("alice@example.com", emails,
                      "share() must still be called when existing_id is supplied")


class TestCliExistingIdArgRoundTrips(unittest.TestCase):
    """main(['--bootstrap', '--existing-id', 'abc123', '--month', '2026-06'])
    must dispatch bootstrap with existing_id='abc123'."""

    def test_cli_existing_id_arg_round_trips(self):
        sh = _FakeSpreadsheet(title="smartstock-allstocks-2026-06",
                              sheet_id="abc123")
        client = _FakeClient(existing=[sh])
        captured = io.StringIO()
        fake_env = {"GOOGLE_SA_JSON": '{"type":"service_account"}'}
        with mock.patch.dict(os.environ, fake_env):
            with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
                with mock.patch("sys.stdout", captured):
                    rc = sa.main([
                        "--bootstrap",
                        "--existing-id", "abc123",
                        "--month", "2026-06",
                        "--user-email", "test@example.com",
                        "--index-path", os.devnull,
                    ])
        self.assertEqual(rc, 0)
        out = captured.getvalue()
        self.assertIn("SHEETS_ID_ALLSTOCKS_TW=abc123", out)
        self.assertEqual(len(client.create_calls), 0,
                         "CLI --existing-id must not trigger create()")
        self.assertIn("abc123", client.open_by_key_calls)


class TestBootstrapExistingIdAndUserEmailCoexist(unittest.TestCase):
    """Both --existing-id and --user-email flags work together correctly."""

    def test_bootstrap_existing_id_and_user_email_can_coexist(self):
        sh = _FakeSpreadsheet(title="smartstock-allstocks-2026-06",
                              sheet_id="abc123")
        client = _FakeClient(existing=[sh])
        captured = io.StringIO()
        fake_env = {"GOOGLE_SA_JSON": '{"type":"service_account"}'}
        with mock.patch.dict(os.environ, fake_env):
            with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
                with mock.patch("sys.stdout", captured):
                    rc = sa.main([
                        "--bootstrap",
                        "--existing-id", "abc123",
                        "--month", "2026-06",
                        "--user-email", "bob@example.com",
                        "--index-path", os.devnull,
                    ])
        self.assertEqual(rc, 0)
        out = captured.getvalue()
        self.assertIn("USER_SHARED=bob@example.com", out)
        self.assertIn("SHEETS_ID_ALLSTOCKS_TW=abc123", out)


class TestBootstrapExistingIdIdempotent(unittest.TestCase):
    """Second call with same existing_id must not create extra tabs."""

    def test_bootstrap_existing_id_idempotent_when_tabs_already_present(self):
        sh = _FakeSpreadsheet(title="smartstock-allstocks-2026-06",
                              sheet_id="abc123")
        client = _FakeClient(existing=[sh])
        # First call — creates 13 tabs (Sprint 3 #22: 7 P0 + 6 P1)
        with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
            sa.bootstrap(month="2026-06", user_email="x@x.com",
                         existing_id="abc123", index_path=os.devnull)
        # Second call — must not duplicate tabs
        with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
            sa.bootstrap(month="2026-06", user_email="x@x.com",
                         existing_id="abc123", index_path=os.devnull)
        self.assertEqual(len(sh._worksheets), 13,
                         "tabs must NOT be duplicated on second bootstrap call with existing_id")
        self.assertEqual(len(client.create_calls), 0,
                         "create() must never be called when existing_id is supplied")


# ═══════════════════════════════════════════════════════════════════════════════
# Sprint 2 P2 Tests — sync_allstocks() 7 P0 fetchers
# ═══════════════════════════════════════════════════════════════════════════════

# Extended _FakeWorksheet that supports delete_rows for upsert testing
class _FakeWorksheetFull(_FakeWorksheet):
    """_FakeWorksheet extended with ranged delete_rows for idempotent-upsert testing.

    delete_rows accepts BOTH delete_rows(rn) (single 1-based row) AND
    delete_rows(start, end) (inclusive 1-based range), mirroring gspread. Tracks
    delete_calls / append_calls (incremented per successful call) so tests can assert
    the ranged-batch contract (N contiguous dups -> 1 call, not N)."""

    def __init__(self, title, headers=None):
        super().__init__(title, headers=headers)
        self.delete_calls = 0
        self.append_calls = 0

    def delete_rows(self, start, end=None):
        """Delete inclusive 1-based row range [start, end] (row 1 = header).
        end omitted -> single row. Pops high->low so 1-based indices stay valid."""
        self.delete_calls += 1
        end = start if end is None else end
        for rn in range(end, start - 1, -1):
            if 1 <= rn <= len(self._rows):
                self._rows.pop(rn - 1)

    def append_rows(self, rows, value_input_option=None):
        self.append_calls += 1
        super().append_rows(rows, value_input_option=value_input_option)


class _FakeSpreadsheetFull(_FakeSpreadsheet):
    """_FakeSpreadsheet that creates _FakeWorksheetFull instances."""

    def add_worksheet(self, title, rows=1000, cols=26):
        ws = _FakeWorksheetFull(title)
        self._worksheets[title] = ws
        return ws


class _FakeClientFull(_FakeClient):
    """_FakeClient that opens a specific sheet_id directly (for sync_allstocks)."""

    def __init__(self, sheet, existing=None):
        super().__init__(existing=existing or [sheet])
        self._sheet = sheet

    def open_by_key(self, key):
        self.open_by_key_calls.append(key)
        if key == self._sheet.id:
            return self._sheet
        raise Exception(f"Spreadsheet not found: {key}")


# ── Fixtures — minimal raw fetcher outputs matching each source's real shape ───

_BWIBBU_RAW = [
    {"Code": "2330", "Name": "台積電", "PEratio": "22.5", "DividendYield": "1.8",
     "PBratio": "6.0", "Date": "1150624"},
    {"Code": "2317", "Name": "鴻海", "PEratio": "", "DividendYield": "3.2",
     "PBratio": "1.5", "Date": "1150624"},
]

_MI_MARGN_RAW = [
    {"股票代號": "2330", "股票名稱": "台積電",
     "融資買進": "100", "融資賣出": "80", "融資現金償還": "5",
     "融資今日餘額": "1000", "融資限額": "50000",
     "融券賣出": "50", "融券買進": "30", "融券現券償還": "10",
     "融券今日餘額": "200", "融資前日餘額": "980", "融券前日餘額": "210"},
    {"股票代號": "2317", "股票名稱": "鴻海",
     "融資買進": "200", "融資賣出": "150", "融資現金償還": "10",
     "融資今日餘額": "2000", "融資限額": "80000",
     "融券賣出": "60", "融券買進": "40", "融券現券償還": "5",
     "融券今日餘額": "300", "融資前日餘額": "1950", "融券前日餘額": "330"},
]

_T86_RAW = [
    # positional list[list]: index 0=code,1=name,4=foreign_net,10=trust_net,11=dealer_net,18=total
    ["2330", "台積電", "", "", "", "5000000", "", "", "", "", "", "1000000", "500000",
     "", "", "", "", "", "6500000"],
    ["2317", "鴻海", "", "", "", "-2000000", "", "", "", "", "", "200000", "-100000",
     "", "", "", "", "", "-1900000"],
]

_TPEX_3INSTI_RAW = [
    {"SecuritiesCompanyCode": "6488", "SecuritiesCompanyName": "環球晶",
     "Date": "1150624",
     "ForeignInvestorsIncludeMainlandAreaInvestors-Difference": "100000",
     "SecuritiesInvestmentTrustCompanies-Difference": "50000",
     "Dealers-Difference": "10000",
     "TotalDifference": "160000"},
]

_TPEX_MARGIN_RAW = [
    {"Code": "6488", "MarginPurchaseTodayBalance": "5000",
     "MarginPurchasePreviousDayBalance": "4800",
     "ShortSaleTodayBalance": "200", "ShortSalePreviousDayBalance": "220"},
]

_TPEX_PE_RAW = [
    {"Code": "6488", "PEratio": "18.5", "DividendYield": "2.1", "PBratio": "3.2"},
]

_TDCC_ROWS = [
    {"code": "2330", "date": "20260620", "tier": 12, "holders": 500, "shares": 10000, "pct": 45.5},
    {"code": "2330", "date": "20260620", "tier": 17, "holders": 1000, "shares": 20000, "pct": 100.0},
    {"code": "2317", "date": "20260620", "tier": 12, "holders": 300, "shares": 8000, "pct": 38.2},
    {"code": "2317", "date": "20260620", "tier": 17, "holders": 800, "shares": 15000, "pct": 100.0},
]


def _make_sync_sheet():
    """Create a fake spreadsheet with all 7 P0 tabs pre-seeded (headers in row 1)."""
    sh = _FakeSpreadsheetFull(title="smartstock-allstocks-2026-06",
                              sheet_id="1VqRmlyD2LcXye1flAE9kFeLs7oyrW9KyReAjTvB-iK8")
    for tab_name in sa._TAB_ORDER:
        ws = _FakeWorksheetFull(tab_name, headers=sa.TAB_HEADERS[tab_name])
        sh._worksheets[tab_name] = ws
    return sh


def _make_index_file(tmp_dir, month="2026-06", sheet_id="1VqRmlyD2LcXye1flAE9kFeLs7oyrW9KyReAjTvB-iK8"):
    """Write a minimal _allstocks_sheets_index.json and return its path."""
    path = os.path.join(tmp_dir, "_allstocks_sheets_index.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({month: sheet_id}, f)
    return path


def _run_sync(sheet, index_path, date_str="2026-06-24",
              bwibbu_fn=None, mi_margn_fn=None, t86_fn=None,
              tpex_3insti_fn=None, tpex_margin_fn=None, tpex_pe_fn=None,
              tdcc_fn=None,
              stock_day_all_fn=None, t187ap03_fn=None, notice_fn=None,
              punish_fn=None, sec_frames_fn=None, sec_ftd_fn=None, cnyes_fn=None):
    """Run sync_allstocks with fake client + injected fetchers; return counts dict.

    Sprint 3 #22: extended with 6 P1 fetcher overrides. Each defaults to an empty
    return (no network) so legacy callers stay valid — existing P0 tests still
    pass without modification; the 6 new sources just sync zero rows."""
    client = _FakeClientFull(sheet=sheet)
    patches = [
        mock.patch("sheets_sync_allstocks.get_client", return_value=client),
        mock.patch("sheets_sync_allstocks._fetch_bwibbu", side_effect=bwibbu_fn or (lambda: _BWIBBU_RAW)),
        mock.patch("sheets_sync_allstocks._fetch_mi_margn", side_effect=mi_margn_fn or (lambda: _MI_MARGN_RAW)),
        mock.patch("sheets_sync_allstocks._fetch_t86", side_effect=t86_fn or (lambda d: _T86_RAW)),
        mock.patch("sheets_sync_allstocks._fetch_tpex_3insti", side_effect=tpex_3insti_fn or (lambda: _TPEX_3INSTI_RAW)),
        mock.patch("sheets_sync_allstocks._fetch_tpex_margin", side_effect=tpex_margin_fn or (lambda: _TPEX_MARGIN_RAW)),
        mock.patch("sheets_sync_allstocks._fetch_tpex_pe", side_effect=tpex_pe_fn or (lambda: _TPEX_PE_RAW)),
        mock.patch("sheets_sync_allstocks._fetch_tdcc", side_effect=tdcc_fn or (lambda: _TDCC_ROWS)),
        # Sprint 3 #22 — P1 fetchers default to empty list/dict (no network)
        mock.patch("sheets_sync_allstocks._fetch_stock_day_all", side_effect=stock_day_all_fn or (lambda: [])),
        mock.patch("sheets_sync_allstocks._fetch_t187ap03", side_effect=t187ap03_fn or (lambda: [])),
        mock.patch("sheets_sync_allstocks._fetch_notice", side_effect=notice_fn or (lambda: {})),
        mock.patch("sheets_sync_allstocks._fetch_punish", side_effect=punish_fn or (lambda: {})),
        mock.patch("sheets_sync_allstocks._fetch_sec_frames", side_effect=sec_frames_fn or (lambda: [])),
        mock.patch("sheets_sync_allstocks._fetch_sec_ftd", side_effect=sec_ftd_fn or (lambda: [])),
        mock.patch("sheets_sync_allstocks._fetch_cnyes", side_effect=cnyes_fn or (lambda: [])),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return sa.sync_allstocks(date_str=date_str, index_path=index_path)


# ── Tests: sync_allstocks() orchestration ─────────────────────────────────────

class TestSyncAllstocksReadsIndexAndOpensSheet(unittest.TestCase):
    def test_reads_index_and_opens_correct_sheet(self):
        sh = _make_sync_sheet()
        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            client = _FakeClientFull(sheet=sh)
            with mock.patch("sheets_sync_allstocks.get_client", return_value=client), \
                 mock.patch("sheets_sync_allstocks._fetch_bwibbu", return_value=_BWIBBU_RAW), \
                 mock.patch("sheets_sync_allstocks._fetch_mi_margn", return_value=_MI_MARGN_RAW), \
                 mock.patch("sheets_sync_allstocks._fetch_t86", return_value=_T86_RAW), \
                 mock.patch("sheets_sync_allstocks._fetch_tpex_3insti", return_value=_TPEX_3INSTI_RAW), \
                 mock.patch("sheets_sync_allstocks._fetch_tpex_margin", return_value=_TPEX_MARGIN_RAW), \
                 mock.patch("sheets_sync_allstocks._fetch_tpex_pe", return_value=_TPEX_PE_RAW), \
                 mock.patch("sheets_sync_allstocks._fetch_tdcc", return_value=_TDCC_ROWS), \
                 mock.patch("sheets_sync_allstocks._fetch_stock_day_all", return_value=[]), \
                 mock.patch("sheets_sync_allstocks._fetch_t187ap03", return_value=[]), \
                 mock.patch("sheets_sync_allstocks._fetch_notice", return_value={}), \
                 mock.patch("sheets_sync_allstocks._fetch_punish", return_value={}), \
                 mock.patch("sheets_sync_allstocks._fetch_sec_frames", return_value=[]), \
                 mock.patch("sheets_sync_allstocks._fetch_sec_ftd", return_value=[]), \
                 mock.patch("sheets_sync_allstocks._fetch_cnyes", return_value=[]):
                sa.sync_allstocks(date_str="2026-06-24", index_path=idx)
            self.assertIn("1VqRmlyD2LcXye1flAE9kFeLs7oyrW9KyReAjTvB-iK8",
                          client.open_by_key_calls,
                          "sync_allstocks must open the sheet from index")


class TestSyncAllstocksWritesBwibbuRowPerStock(unittest.TestCase):
    def test_n_stocks_returns_n_bwibbu_rows(self):
        """2 raw BWIBBU stocks → exactly 2 data rows appended to bwibbu_daily."""
        sh = _make_sync_sheet()
        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            _run_sync(sh, idx)
        ws = sh._worksheets["bwibbu_daily"]
        # row 0 = header; rows 1+ = data
        data_rows = ws._rows[1:]
        self.assertEqual(len(data_rows), 2,
                         f"expected 2 bwibbu rows, got {len(data_rows)}: {data_rows}")


class TestSyncAllstocksBwibbuRowShape(unittest.TestCase):
    def test_bwibbu_row_matches_tab_headers_exactly(self):
        """Each bwibbu row must have exactly len(TAB_HEADERS['bwibbu_daily']) columns."""
        sh = _make_sync_sheet()
        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            _run_sync(sh, idx)
        ws = sh._worksheets["bwibbu_daily"]
        expected_cols = len(sa.TAB_HEADERS["bwibbu_daily"])
        for row in ws._rows[1:]:
            self.assertEqual(len(row), expected_cols,
                             f"bwibbu row width mismatch: {row}")

    def test_bwibbu_row_first_col_is_date(self):
        sh = _make_sync_sheet()
        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            _run_sync(sh, idx, date_str="2026-06-24")
        ws = sh._worksheets["bwibbu_daily"]
        for row in ws._rows[1:]:
            self.assertEqual(row[0], "2026-06-24")


class TestSyncAllstocksIdempotentPerSource(unittest.TestCase):
    def test_call_twice_same_date_no_duplicates(self):
        """Calling sync twice with the same date must NOT duplicate rows."""
        sh = _make_sync_sheet()
        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            _run_sync(sh, idx, date_str="2026-06-24")
            _run_sync(sh, idx, date_str="2026-06-24")
        ws = sh._worksheets["bwibbu_daily"]
        data_rows = ws._rows[1:]
        self.assertEqual(len(data_rows), 2,
                         f"after 2 runs same date, expected 2 rows not {len(data_rows)}")


class TestSyncAllstocksContinuesWhenOneSourceFails(unittest.TestCase):
    def test_bwibbu_fails_others_still_sync(self):
        """If bwibbu fetcher raises, other 6 sources must still sync."""
        sh = _make_sync_sheet()

        def boom():
            raise RuntimeError("network timeout")

        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            counts = _run_sync(sh, idx, bwibbu_fn=boom)

        # bwibbu tab must have no data rows (only header)
        ws_bwibbu = sh._worksheets["bwibbu_daily"]
        self.assertEqual(len(ws_bwibbu._rows), 1, "bwibbu must have header-only on failure")

        # at least 4 of the other 6 must have data rows
        other_tabs = ["mi_margn_daily", "t86_daily", "tpex_3insti_daily",
                      "tpex_margin_daily", "tpex_per_daily"]
        for tab in other_tabs:
            ws = sh._worksheets[tab]
            self.assertGreater(len(ws._rows), 1,
                               f"tab {tab} should have data rows even when bwibbu fails")

        # bwibbu count must be 0 or negative (skipped)
        self.assertLessEqual(counts.get("bwibbu_daily", 0), 0,
                             "bwibbu count must indicate skip on failure")

    def test_skip_logged_for_failed_source(self):
        """A SKIP log line must appear when a source fails."""
        sh = _make_sync_sheet()

        def boom():
            raise RuntimeError("connection refused")

        import io as _io
        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            captured = _io.StringIO()
            with mock.patch("sys.stdout", captured):
                _run_sync(sh, idx, bwibbu_fn=boom)
        self.assertIn("SKIP", captured.getvalue())


class TestSyncAllstocksSkipsTdccWhenNoNewRelease(unittest.TestCase):
    def test_empty_tdcc_leaves_tab_unchanged(self):
        """When TDCC fetcher returns [], tdcc_weekly tab must stay header-only."""
        sh = _make_sync_sheet()
        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            _run_sync(sh, idx, tdcc_fn=lambda: [])
        ws = sh._worksheets["tdcc_weekly"]
        # only the header row
        self.assertEqual(len(ws._rows), 1,
                         "tdcc_weekly must stay header-only when fetcher returns []")


class TestSyncAllstocksGracefulWhenSAMissing(unittest.TestCase):
    def test_exits_0_when_google_sa_json_missing(self):
        env = {k: v for k, v in os.environ.items() if k != "GOOGLE_SA_JSON"}
        with mock.patch.dict(os.environ, env, clear=True):
            rc = sa.main(["--sync", "--date", "2026-06-24"])
        self.assertEqual(rc, 0)

    def test_prints_skip_when_sa_missing(self):
        import io as _io
        env = {k: v for k, v in os.environ.items() if k != "GOOGLE_SA_JSON"}
        captured = _io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("sys.stdout", captured):
            sa.main(["--sync", "--date", "2026-06-24"])
        self.assertIn("SKIP", captured.getvalue())


class TestSyncAllstocksGracefulWhenIndexMissing(unittest.TestCase):
    def test_returns_none_when_index_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = os.path.join(tmp, "no_such_index.json")
            result = sa.sync_allstocks(date_str="2026-06-24", index_path=missing_path)
        self.assertIsNone(result, "sync_allstocks must return None when index file missing")

    def test_logs_skip_when_index_missing(self):
        import io as _io
        captured = _io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = os.path.join(tmp, "no_such_index.json")
            with mock.patch("sys.stdout", captured):
                sa.sync_allstocks(date_str="2026-06-24", index_path=missing_path)
        self.assertIn("SKIP", captured.getvalue())


class TestSyncAllstocksGracefulWhenMonthNotInIndex(unittest.TestCase):
    def test_returns_none_when_month_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            idx_path = os.path.join(tmp, "idx.json")
            with open(idx_path, "w") as f:
                json.dump({"2025-01": "OLD_SHEET"}, f)
            result = sa.sync_allstocks(date_str="2026-06-24", index_path=idx_path)
        self.assertIsNone(result)


# ── Tests: per-source row shape ───────────────────────────────────────────────

class TestBuildBwibbuRows(unittest.TestCase):
    def test_row_matches_headers(self):
        rows = sa._build_bwibbu_rows("2026-06-24", _BWIBBU_RAW)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(len(row), len(sa.TAB_HEADERS["bwibbu_daily"]))

    def test_missing_per_becomes_none(self):
        rows = sa._build_bwibbu_rows("2026-06-24", _BWIBBU_RAW)
        # second stock has empty PEratio → should be None
        self.assertIsNone(rows[1][3], "empty PEratio must map to None")

    def test_first_col_is_date(self):
        rows = sa._build_bwibbu_rows("2026-06-24", _BWIBBU_RAW)
        for row in rows:
            self.assertEqual(row[0], "2026-06-24")


class TestBuildMiMargnRows(unittest.TestCase):
    def test_row_matches_headers(self):
        rows = sa._build_mi_margn_rows("2026-06-24", _MI_MARGN_RAW)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(len(row), len(sa.TAB_HEADERS["mi_margn_daily"]))

    def test_empty_input_returns_empty(self):
        rows = sa._build_mi_margn_rows("2026-06-24", [])
        self.assertEqual(rows, [])


class TestBuildT86Rows(unittest.TestCase):
    def test_row_matches_headers(self):
        rows = sa._build_t86_rows("2026-06-24", _T86_RAW)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(len(row), len(sa.TAB_HEADERS["t86_daily"]))

    def test_empty_input_returns_empty(self):
        self.assertEqual(sa._build_t86_rows("2026-06-24", []), [])


class TestBuildTpex3instiRows(unittest.TestCase):
    def test_row_matches_headers(self):
        rows = sa._build_tpex_3insti_rows("2026-06-24", _TPEX_3INSTI_RAW)
        self.assertEqual(len(rows), 1)
        for row in rows:
            self.assertEqual(len(row), len(sa.TAB_HEADERS["tpex_3insti_daily"]))


class TestBuildTpexMarginRows(unittest.TestCase):
    def test_row_matches_headers(self):
        rows = sa._build_tpex_margin_rows("2026-06-24", _TPEX_MARGIN_RAW)
        self.assertEqual(len(rows), 1)
        for row in rows:
            self.assertEqual(len(row), len(sa.TAB_HEADERS["tpex_margin_daily"]))


class TestBuildTpexPerRows(unittest.TestCase):
    def test_row_matches_headers(self):
        rows = sa._build_tpex_per_rows("2026-06-24", _TPEX_PE_RAW)
        self.assertEqual(len(rows), 1)
        for row in rows:
            self.assertEqual(len(row), len(sa.TAB_HEADERS["tpex_per_daily"]))


class TestBuildTdccRows(unittest.TestCase):
    def test_row_matches_headers(self):
        rows = sa._build_tdcc_rows(_TDCC_ROWS)
        # 2 stocks (2330, 2317), each has rows including tier 17 total row
        # We expect one row per code (based on aggregation)
        for row in rows:
            self.assertEqual(len(row), len(sa.TAB_HEADERS["tdcc_weekly"]),
                             f"tdcc row width mismatch: {row}")

    def test_empty_input_returns_empty(self):
        self.assertEqual(sa._build_tdcc_rows([]), [])


# ── Tests: CLI --sync arg ─────────────────────────────────────────────────────

class TestCliSyncArg(unittest.TestCase):
    def test_cli_sync_date_dispatches_sync_allstocks(self):
        """--sync --date YYYY-MM-DD must call sync_allstocks(date_str=...)."""
        with mock.patch("sheets_sync_allstocks.sync_allstocks", return_value={}) as mock_sync, \
             mock.patch.dict(os.environ, {"GOOGLE_SA_JSON": '{"type":"service_account"}'}):
            rc = sa.main(["--sync", "--date", "2026-06-24"])
        mock_sync.assert_called_once()
        call_kwargs = mock_sync.call_args
        # date_str must be passed either as positional or keyword
        date_passed = (call_kwargs.args[0] if call_kwargs.args
                       else call_kwargs.kwargs.get("date_str"))
        self.assertEqual(date_passed, "2026-06-24")

    def test_cli_sync_without_date_uses_today(self):
        """--sync without --date must use today's UTC date."""
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with mock.patch("sheets_sync_allstocks.sync_allstocks", return_value={}) as mock_sync, \
             mock.patch.dict(os.environ, {"GOOGLE_SA_JSON": '{"type":"service_account"}'}):
            sa.main(["--sync"])
        mock_sync.assert_called_once()
        call_kwargs = mock_sync.call_args
        date_passed = (call_kwargs.args[0] if call_kwargs.args
                       else call_kwargs.kwargs.get("date_str"))
        self.assertEqual(date_passed, today)

    def test_cli_sync_exits_0(self):
        with mock.patch("sheets_sync_allstocks.sync_allstocks", return_value={}), \
             mock.patch.dict(os.environ, {"GOOGLE_SA_JSON": '{"type":"service_account"}'}):
            rc = sa.main(["--sync", "--date", "2026-06-24"])
        self.assertEqual(rc, 0)


# ── Tests: sync_allstocks returns counts dict ─────────────────────────────────

class TestSyncAllstocksReturnsCounts(unittest.TestCase):
    def test_returns_dict_with_all_7_keys(self):
        sh = _make_sync_sheet()
        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            counts = _run_sync(sh, idx)
        for tab in sa._TAB_ORDER:
            self.assertIn(tab, counts, f"counts dict must include key {tab!r}")

    def test_bwibbu_count_equals_row_count(self):
        sh = _make_sync_sheet()
        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            counts = _run_sync(sh, idx)
        self.assertEqual(counts["bwibbu_daily"], 2,
                         "bwibbu_daily count must equal number of raw stocks fetched")


# ═══════════════════════════════════════════════════════════════════════════════
# Sprint 1 TDCC local-cron: --source filter tests
# ═══════════════════════════════════════════════════════════════════════════════

def _run_sync_with_source(sheet, index_path, date_str="2026-06-24", source_filter=None,
                           bwibbu_fn=None, mi_margn_fn=None, t86_fn=None,
                           tpex_3insti_fn=None, tpex_margin_fn=None, tpex_pe_fn=None,
                           tdcc_fn=None):
    """Like _run_sync but passes source_filter= to sync_allstocks."""
    client = _FakeClientFull(sheet=sheet)
    patches = [
        mock.patch("sheets_sync_allstocks.get_client", return_value=client),
        mock.patch("sheets_sync_allstocks._fetch_bwibbu",
                   side_effect=bwibbu_fn or (lambda: _BWIBBU_RAW)),
        mock.patch("sheets_sync_allstocks._fetch_mi_margn",
                   side_effect=mi_margn_fn or (lambda: _MI_MARGN_RAW)),
        mock.patch("sheets_sync_allstocks._fetch_t86",
                   side_effect=t86_fn or (lambda d: _T86_RAW)),
        mock.patch("sheets_sync_allstocks._fetch_tpex_3insti",
                   side_effect=tpex_3insti_fn or (lambda: _TPEX_3INSTI_RAW)),
        mock.patch("sheets_sync_allstocks._fetch_tpex_margin",
                   side_effect=tpex_margin_fn or (lambda: _TPEX_MARGIN_RAW)),
        mock.patch("sheets_sync_allstocks._fetch_tpex_pe",
                   side_effect=tpex_pe_fn or (lambda: _TPEX_PE_RAW)),
        mock.patch("sheets_sync_allstocks._fetch_tdcc",
                   side_effect=tdcc_fn or (lambda: _TDCC_ROWS)),
        # Sprint 3 #22 — P1 fetchers default empty (no network)
        mock.patch("sheets_sync_allstocks._fetch_stock_day_all", side_effect=lambda: []),
        mock.patch("sheets_sync_allstocks._fetch_t187ap03", side_effect=lambda: []),
        mock.patch("sheets_sync_allstocks._fetch_notice", side_effect=lambda: {}),
        mock.patch("sheets_sync_allstocks._fetch_punish", side_effect=lambda: {}),
        mock.patch("sheets_sync_allstocks._fetch_sec_frames", side_effect=lambda: []),
        mock.patch("sheets_sync_allstocks._fetch_sec_ftd", side_effect=lambda: []),
        mock.patch("sheets_sync_allstocks._fetch_cnyes", side_effect=lambda: []),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return sa.sync_allstocks(date_str=date_str, index_path=index_path,
                                  source_filter=source_filter)


class TestSyncAllstocksSourceFilterOnlyRunsNamedSource(unittest.TestCase):
    """sync_allstocks(source_filter='tdcc_weekly') must call only the TDCC
    fetcher; the other 6 fetchers must NOT be called at all."""

    def test_sync_allstocks_with_source_filter_only_runs_named_source(self):
        sh = _make_sync_sheet()
        calls = {}

        def track(name, data):
            def fetcher(*args, **kwargs):
                calls[name] = calls.get(name, 0) + 1
                return data
            return fetcher

        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            _run_sync_with_source(
                sh, idx,
                source_filter="tdcc_weekly",
                bwibbu_fn=track("bwibbu", _BWIBBU_RAW),
                mi_margn_fn=track("mi_margn", _MI_MARGN_RAW),
                t86_fn=track("t86", _T86_RAW),
                tpex_3insti_fn=track("tpex_3insti", _TPEX_3INSTI_RAW),
                tpex_margin_fn=track("tpex_margin", _TPEX_MARGIN_RAW),
                tpex_pe_fn=track("tpex_pe", _TPEX_PE_RAW),
                tdcc_fn=track("tdcc", _TDCC_ROWS),
            )

        self.assertIn("tdcc", calls, "TDCC fetcher must be called when source_filter='tdcc_weekly'")
        for name in ("bwibbu", "mi_margn", "t86", "tpex_3insti", "tpex_margin", "tpex_pe"):
            self.assertNotIn(name, calls,
                             f"fetcher {name!r} must NOT be called when source_filter='tdcc_weekly'")

    def test_source_filter_only_writes_to_named_tab(self):
        """When source_filter='tdcc_weekly', only tdcc_weekly tab should have data rows;
        all other tabs must remain header-only."""
        sh = _make_sync_sheet()
        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            _run_sync_with_source(sh, idx, source_filter="tdcc_weekly")

        ws_tdcc = sh._worksheets["tdcc_weekly"]
        self.assertGreater(len(ws_tdcc._rows), 1,
                           "tdcc_weekly must have data rows when it is the only source")
        for tab in ("bwibbu_daily", "mi_margn_daily", "t86_daily",
                    "tpex_3insti_daily", "tpex_margin_daily", "tpex_per_daily",
                    # Sprint 3 #22 — P1 tabs also expected header-only when filtered out
                    "stock_day_all_daily", "t187ap03_monthly", "notice_punish_daily",
                    "sec_frames_quarterly", "sec_ftd_semimonthly", "cnyes_news_daily"):
            ws = sh._worksheets[tab]
            self.assertEqual(len(ws._rows), 1,
                             f"tab {tab!r} must stay header-only when filtered out")


class TestSyncAllstocksSourceFilterInvalidRaisesOrSkips(unittest.TestCase):
    """Unknown source name → graceful SKIP, not a crash (exit 0 compliant)."""

    def test_sync_allstocks_source_filter_invalid_raises_or_skips(self):
        """Unknown source_filter must not raise an unhandled exception.

        Acceptable outcomes: returns None / empty dict / dict of all-zero counts;
        must NOT raise an exception to the caller."""
        sh = _make_sync_sheet()
        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            try:
                result = _run_sync_with_source(sh, idx, source_filter="no_such_source_xyz")
                # If it returns, it must be None or a dict (not crash)
                self.assertTrue(
                    result is None or isinstance(result, dict),
                    f"unexpected return type: {type(result)}"
                )
            except (ValueError, KeyError) as exc:
                # Raising ValueError/KeyError is also acceptable
                pass

    def test_invalid_source_filter_logs_skip(self):
        """An invalid source_filter must emit a SKIP log line, not silently proceed."""
        import io as _io
        sh = _make_sync_sheet()
        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            captured = _io.StringIO()
            with mock.patch("sys.stdout", captured):
                try:
                    _run_sync_with_source(sh, idx, source_filter="no_such_source_xyz")
                except (ValueError, KeyError):
                    pass
        out = captured.getvalue()
        self.assertIn("SKIP", out,
                      "invalid source_filter must produce SKIP log (got: %r)" % out[:200])


class TestCliSourceArg(unittest.TestCase):
    """main(['--sync', '--source', 'tdcc_weekly']) must pass source_filter='tdcc_weekly'
    through to sync_allstocks, so only TDCC is synced."""

    def test_cli_source_arg_passes_filter_to_sync_allstocks(self):
        """--source tdcc_weekly must forward source_filter kwarg to sync_allstocks."""
        with mock.patch("sheets_sync_allstocks.sync_allstocks", return_value={}) as mock_sync, \
             mock.patch.dict(os.environ, {"GOOGLE_SA_JSON": '{"type":"service_account"}'}):
            rc = sa.main(["--sync", "--date", "2026-06-24", "--source", "tdcc_weekly"])
        self.assertEqual(rc, 0)
        mock_sync.assert_called_once()
        call_kwargs = mock_sync.call_args
        source_passed = call_kwargs.kwargs.get("source_filter")
        self.assertEqual(source_passed, "tdcc_weekly",
                         "main() must pass source_filter='tdcc_weekly' to sync_allstocks")

    def test_cli_source_arg_tdcc_only_integration(self):
        """End-to-end: main --sync --source tdcc_weekly should call TDCC fetcher only."""
        sh = _make_sync_sheet()
        calls = {}

        def track(name, data):
            def fetcher(*args, **kwargs):
                calls[name] = calls.get(name, 0) + 1
                return data
            return fetcher

        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            client = _FakeClientFull(sheet=sh)
            patches = [
                mock.patch("sheets_sync_allstocks.get_client", return_value=client),
                mock.patch("sheets_sync_allstocks._fetch_bwibbu",
                           side_effect=track("bwibbu", _BWIBBU_RAW)),
                mock.patch("sheets_sync_allstocks._fetch_mi_margn",
                           side_effect=track("mi_margn", _MI_MARGN_RAW)),
                mock.patch("sheets_sync_allstocks._fetch_t86",
                           side_effect=track("t86", _T86_RAW)),
                mock.patch("sheets_sync_allstocks._fetch_tpex_3insti",
                           side_effect=track("tpex_3insti", _TPEX_3INSTI_RAW)),
                mock.patch("sheets_sync_allstocks._fetch_tpex_margin",
                           side_effect=track("tpex_margin", _TPEX_MARGIN_RAW)),
                mock.patch("sheets_sync_allstocks._fetch_tpex_pe",
                           side_effect=track("tpex_pe", _TPEX_PE_RAW)),
                mock.patch("sheets_sync_allstocks._fetch_tdcc",
                           side_effect=track("tdcc", _TDCC_ROWS)),
                # Sprint 3 #22 — P1 fetchers default empty (no network)
                mock.patch("sheets_sync_allstocks._fetch_stock_day_all", side_effect=lambda: []),
                mock.patch("sheets_sync_allstocks._fetch_t187ap03", side_effect=lambda: []),
                mock.patch("sheets_sync_allstocks._fetch_notice", side_effect=lambda: {}),
                mock.patch("sheets_sync_allstocks._fetch_punish", side_effect=lambda: {}),
                mock.patch("sheets_sync_allstocks._fetch_sec_frames", side_effect=lambda: []),
                mock.patch("sheets_sync_allstocks._fetch_sec_ftd", side_effect=lambda: []),
                mock.patch("sheets_sync_allstocks._fetch_cnyes", side_effect=lambda: []),
                mock.patch.dict(os.environ, {"GOOGLE_SA_JSON": '{"type":"service_account"}'}),
            ]
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                rc = sa.main(["--sync", "--date", "2026-06-24",
                              "--source", "tdcc_weekly",
                              "--index-path", idx])
        self.assertEqual(rc, 0)
        self.assertIn("tdcc", calls, "TDCC fetcher must be called")
        for name in ("bwibbu", "mi_margn", "t86", "tpex_3insti", "tpex_margin", "tpex_pe"):
            self.assertNotIn(name, calls,
                             f"fetcher {name!r} must NOT be called with --source tdcc_weekly")


# ═══════════════════════════════════════════════════════════════════════════════
# Sprint 3 #22 — P1 source wiring tests (6 new sources)
# ═══════════════════════════════════════════════════════════════════════════════

# Minimal fixtures matching each P1 fetcher's real return shape

_STOCK_DAY_ALL_RAW = [
    {"Code": "2330", "Name": "台積電",
     "OpeningPrice": "1080", "HighestPrice": "1090", "LowestPrice": "1075",
     "ClosingPrice": "1085", "TradeVolume": "25000000", "TradeValue": "27125000000"},
    {"Code": "2317", "Name": "鴻海",
     "OpeningPrice": "210", "HighestPrice": "212", "LowestPrice": "209",
     "ClosingPrice": "211", "TradeVolume": "18000000", "TradeValue": "3798000000"},
]

_T187AP03_RAW = [
    {"公司代號": "2330", "公司簡稱": "台積電",
     "已發行普通股數及TDR原股發行股數": "25930380458", "出表日期": "1150624"},
    {"公司代號": "2317", "公司簡稱": "鴻海",
     "已發行普通股數及TDR原股發行股數": "13860000000", "出表日期": "1150624"},
]

_NOTICE_MAP = {
    "2454": {"reason": "週/月成交量異常", "count": 3, "date": "2026-06-24", "name": "聯發科"},
}

_PUNISH_MAP = {
    "6488": {"reason": "處置條件達成", "date": "2026-06-24",
             "level": 1, "period": "10 個營業日", "name": "環球晶"},
}

_SEC_FRAMES_RAW = [
    {"cik": "0000320193", "concept": "Revenues", "period": "CY2026Q1",
     "val": 123456789000.0, "end": "2026-03-31", "accn": "0000320193-26-000001",
     "entity": "APPLE INC"},
    {"cik": "0000320193", "concept": "NetIncomeLoss", "period": "CY2026Q1",
     "val": 35000000000.0, "end": "2026-03-31", "accn": "0000320193-26-000001",
     "entity": "APPLE INC"},
]

_SEC_FTD_RAW = [
    {"settlement_date": "20260615", "cusip": "037833100", "symbol": "AAPL",
     "quantity": 12345, "description": "APPLE INC", "price": "215.50"},
    {"settlement_date": "20260615", "cusip": "594918104", "symbol": "MSFT",
     "quantity": 5678, "description": "MICROSOFT CORP", "price": "455.10"},
]

_CNYES_RAW = [
    {"newsId": 5123456, "title": "台積電 6 月營收續創新高",
     "publishAt": 1782739200,   # arbitrary epoch s in 2026
     "stock": ["2330"]},
    {"newsId": 5123457, "title": "聯發科手機晶片市佔超前競爭",
     "publishAt": 1782739800,
     "stock": ["2454"], "market": [{"code": "2454", "name": "聯發科", "symbol": "TWSE:2454"}]},
]


# ── P1 row-builder tests ──────────────────────────────────────────────────────

class TestBuildStockDayAllRows(unittest.TestCase):
    def test_row_matches_headers(self):
        rows = sa._build_stock_day_all_rows("2026-06-24", _STOCK_DAY_ALL_RAW)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(len(row), len(sa.TAB_HEADERS["stock_day_all_daily"]))

    def test_first_col_is_date(self):
        rows = sa._build_stock_day_all_rows("2026-06-24", _STOCK_DAY_ALL_RAW)
        self.assertEqual(rows[0][0], "2026-06-24")

    def test_empty_input_returns_empty(self):
        self.assertEqual(sa._build_stock_day_all_rows("2026-06-24", []), [])

    def test_skips_rows_without_code(self):
        rows = sa._build_stock_day_all_rows("2026-06-24",
                                             [{"Code": "", "Name": "blank"}])
        self.assertEqual(rows, [])


class TestBuildT187ap03Rows(unittest.TestCase):
    def test_row_matches_headers(self):
        rows = sa._build_t187ap03_rows("2026-06", _T187AP03_RAW)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(len(row), len(sa.TAB_HEADERS["t187ap03_monthly"]))

    def test_first_col_is_yyyymm(self):
        rows = sa._build_t187ap03_rows("2026-06", _T187AP03_RAW)
        self.assertEqual(rows[0][0], "2026-06")

    def test_empty_input_returns_empty(self):
        self.assertEqual(sa._build_t187ap03_rows("2026-06", []), [])


class TestBuildNoticePunishRows(unittest.TestCase):
    def test_combined_notice_and_punish_yields_both(self):
        rows = sa._build_notice_punish_rows("2026-06-24", _NOTICE_MAP, _PUNISH_MAP)
        types = [r[3] for r in rows]
        self.assertIn("notice", types)
        self.assertIn("punish", types)

    def test_row_matches_headers(self):
        rows = sa._build_notice_punish_rows("2026-06-24", _NOTICE_MAP, _PUNISH_MAP)
        for row in rows:
            self.assertEqual(len(row), len(sa.TAB_HEADERS["notice_punish_daily"]))

    def test_empty_input_returns_empty(self):
        self.assertEqual(sa._build_notice_punish_rows("2026-06-24", {}, {}), [])

    def test_punish_carries_period(self):
        rows = sa._build_notice_punish_rows("2026-06-24", {}, _PUNISH_MAP)
        # period is column index 5 per header
        self.assertEqual(rows[0][5], "10 個營業日")


class TestBuildSecFramesRows(unittest.TestCase):
    def test_row_matches_headers(self):
        rows = sa._build_sec_frames_rows(_SEC_FRAMES_RAW)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(len(row), len(sa.TAB_HEADERS["sec_frames_quarterly"]))

    def test_concept_preserved(self):
        rows = sa._build_sec_frames_rows(_SEC_FRAMES_RAW)
        concepts = [r[1] for r in rows]   # concept column
        self.assertIn("Revenues", concepts)
        self.assertIn("NetIncomeLoss", concepts)

    def test_empty_input_returns_empty(self):
        self.assertEqual(sa._build_sec_frames_rows([]), [])

    def test_skips_rows_without_cik(self):
        rows = sa._build_sec_frames_rows([{"cik": "", "concept": "Revenues", "val": 1.0}])
        self.assertEqual(rows, [])


class TestBuildSecFtdRows(unittest.TestCase):
    def test_row_matches_headers(self):
        rows = sa._build_sec_ftd_rows(_SEC_FTD_RAW)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(len(row), len(sa.TAB_HEADERS["sec_ftd_semimonthly"]))

    def test_symbol_uppercased_in_real_data(self):
        rows = sa._build_sec_ftd_rows(_SEC_FTD_RAW)
        symbols = [r[1] for r in rows]
        self.assertIn("AAPL", symbols)

    def test_empty_input_returns_empty(self):
        self.assertEqual(sa._build_sec_ftd_rows([]), [])


class TestBuildCnyesRows(unittest.TestCase):
    def test_row_matches_headers(self):
        rows = sa._build_cnyes_rows("2026-06-24", _CNYES_RAW)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(len(row), len(sa.TAB_HEADERS["cnyes_news_daily"]))

    def test_stock_codes_joined_comma(self):
        rows = sa._build_cnyes_rows("2026-06-24", _CNYES_RAW)
        # First item has stock=["2330"] → "2330"
        self.assertEqual(rows[0][1], "2330")

    def test_market_fallback_when_no_stock(self):
        no_stock_raw = [{"newsId": 999, "title": "market fallback test",
                         "publishAt": 1782739200,
                         "market": [{"code": "1234", "name": "test"}]}]
        rows = sa._build_cnyes_rows("2026-06-24", no_stock_raw)
        self.assertEqual(rows[0][1], "1234")

    def test_empty_input_returns_empty(self):
        self.assertEqual(sa._build_cnyes_rows("2026-06-24", []), [])

    def test_skips_items_without_title(self):
        rows = sa._build_cnyes_rows("2026-06-24",
                                     [{"newsId": 1, "title": "", "stock": ["2330"]}])
        self.assertEqual(rows, [])


# ── P1 integration tests (failure isolation) ──────────────────────────────────

class TestSyncAllstocksP1FailureIsolation(unittest.TestCase):
    """One P1 fetcher raising must NOT block the other 5 P1 sources or the 7 P0s."""

    def test_stock_day_all_raises_others_still_sync(self):
        sh = _make_sync_sheet()
        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            counts = _run_sync(sh, idx,
                               stock_day_all_fn=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        # stock_day_all → SKIP (-1); other P1 sources → 0; P0 sources → real counts
        self.assertEqual(counts["stock_day_all_daily"], -1,
                         "failed source must report -1")
        self.assertGreaterEqual(counts["bwibbu_daily"], 1,
                                "P0 sources must still sync when one P1 source fails")
        self.assertEqual(counts["cnyes_news_daily"], 0,
                         "other P1 sources unaffected")


class TestSyncAllstocksP1AllSourcesSynced(unittest.TestCase):
    """End-to-end: all 13 sources (7 P0 + 6 P1) must appear in counts dict."""

    def test_all_13_keys_present_in_counts(self):
        sh = _make_sync_sheet()
        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            counts = _run_sync(sh, idx,
                               stock_day_all_fn=lambda: _STOCK_DAY_ALL_RAW,
                               t187ap03_fn=lambda: _T187AP03_RAW,
                               notice_fn=lambda: _NOTICE_MAP,
                               punish_fn=lambda: _PUNISH_MAP,
                               sec_frames_fn=lambda: _SEC_FRAMES_RAW,
                               sec_ftd_fn=lambda: _SEC_FTD_RAW,
                               cnyes_fn=lambda: _CNYES_RAW)
        for tab in sa._TAB_ORDER:
            self.assertIn(tab, counts, f"counts must include {tab!r}")
        # P1 counts should be > 0 when fixtures are supplied
        self.assertEqual(counts["stock_day_all_daily"], 2)
        self.assertEqual(counts["t187ap03_monthly"], 2)
        self.assertEqual(counts["notice_punish_daily"], 2)   # 1 notice + 1 punish
        self.assertEqual(counts["sec_frames_quarterly"], 2)
        self.assertEqual(counts["sec_ftd_semimonthly"], 2)
        self.assertEqual(counts["cnyes_news_daily"], 2)


class TestSyncAllstocksP1WritesToCorrectTab(unittest.TestCase):
    """Each P1 source must write its rows to its named tab (not another tab)."""

    def test_p1_sources_write_to_their_own_tabs(self):
        sh = _make_sync_sheet()
        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            _run_sync(sh, idx,
                      stock_day_all_fn=lambda: _STOCK_DAY_ALL_RAW,
                      t187ap03_fn=lambda: _T187AP03_RAW,
                      notice_fn=lambda: _NOTICE_MAP,
                      punish_fn=lambda: _PUNISH_MAP,
                      sec_frames_fn=lambda: _SEC_FRAMES_RAW,
                      sec_ftd_fn=lambda: _SEC_FTD_RAW,
                      cnyes_fn=lambda: _CNYES_RAW)
        # row[0] is the header, row[1:] are data rows
        self.assertEqual(len(sh._worksheets["stock_day_all_daily"]._rows), 1 + 2)
        self.assertEqual(len(sh._worksheets["t187ap03_monthly"]._rows), 1 + 2)
        self.assertEqual(len(sh._worksheets["notice_punish_daily"]._rows), 1 + 2)
        self.assertEqual(len(sh._worksheets["sec_frames_quarterly"]._rows), 1 + 2)
        self.assertEqual(len(sh._worksheets["sec_ftd_semimonthly"]._rows), 1 + 2)
        self.assertEqual(len(sh._worksheets["cnyes_news_daily"]._rows), 1 + 2)


# ═══════════════════════════════════════════════════════════════════════════════
# Git-file archive — keyless CSV replacement for the Sheets month-shard
# ═══════════════════════════════════════════════════════════════════════════════

_T187_RAW = [
    {"公司代號": "2330", "公司簡稱": "台積電",
     "已發行普通股數及TDR原股發行股數": "25930380458", "出表日期": "1150601"},
]


def _read_csv(path):
    """Read a gzip-compressed CSV back as (header, rows) for assertions."""
    import gzip
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        r = list(csv.reader(f))
    return (r[0], r[1:]) if r else ([], [])


def _run_archive(out_dir, date_str="2026-06-24", source_filter=None,
                 bwibbu_fn=None, mi_margn_fn=None, t86_fn=None,
                 tpex_3insti_fn=None, tpex_margin_fn=None, tpex_pe_fn=None, tdcc_fn=None,
                 stock_day_all_fn=None, t187ap03_fn=None, notice_fn=None,
                 punish_fn=None, sec_frames_fn=None, sec_ftd_fn=None, cnyes_fn=None):
    """Run archive_allstocks_to_files with injected fetchers (no client/network)."""
    patches = [
        mock.patch("sheets_sync_allstocks._fetch_bwibbu", side_effect=bwibbu_fn or (lambda: _BWIBBU_RAW)),
        mock.patch("sheets_sync_allstocks._fetch_mi_margn", side_effect=mi_margn_fn or (lambda: _MI_MARGN_RAW)),
        mock.patch("sheets_sync_allstocks._fetch_t86", side_effect=t86_fn or (lambda d: _T86_RAW)),
        mock.patch("sheets_sync_allstocks._fetch_tpex_3insti", side_effect=tpex_3insti_fn or (lambda: _TPEX_3INSTI_RAW)),
        mock.patch("sheets_sync_allstocks._fetch_tpex_margin", side_effect=tpex_margin_fn or (lambda: _TPEX_MARGIN_RAW)),
        mock.patch("sheets_sync_allstocks._fetch_tpex_pe", side_effect=tpex_pe_fn or (lambda: _TPEX_PE_RAW)),
        mock.patch("sheets_sync_allstocks._fetch_tdcc", side_effect=tdcc_fn or (lambda: _TDCC_ROWS)),
        mock.patch("sheets_sync_allstocks._fetch_stock_day_all", side_effect=stock_day_all_fn or (lambda: [])),
        mock.patch("sheets_sync_allstocks._fetch_t187ap03", side_effect=t187ap03_fn or (lambda: [])),
        mock.patch("sheets_sync_allstocks._fetch_notice", side_effect=notice_fn or (lambda: {})),
        mock.patch("sheets_sync_allstocks._fetch_punish", side_effect=punish_fn or (lambda: {})),
        mock.patch("sheets_sync_allstocks._fetch_sec_frames", side_effect=sec_frames_fn or (lambda: [])),
        mock.patch("sheets_sync_allstocks._fetch_sec_ftd", side_effect=sec_ftd_fn or (lambda: [])),
        mock.patch("sheets_sync_allstocks._fetch_cnyes", side_effect=cnyes_fn or (lambda: [])),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return sa.archive_allstocks_to_files(date_str=date_str, out_dir=out_dir,
                                             source_filter=source_filter)


class TestWriteCsv(unittest.TestCase):
    def test_writes_header_and_rows_none_to_empty(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "sub", "x.csv.gz")
            sa._write_csv(path, ["a", "b", "c"], [[1, None, "z"], [2, 3, None]])
            header, rows = _read_csv(path)
            self.assertEqual(header, ["a", "b", "c"])
            self.assertEqual(rows, [["1", "", "z"], ["2", "3", ""]])

    def test_atomic_overwrite_no_tmp_left(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "x.csv.gz")
            sa._write_csv(path, ["a"], [["1"]])
            sa._write_csv(path, ["a"], [["2"]])  # overwrite
            _, rows = _read_csv(path)
            self.assertEqual(rows, [["2"]])
            self.assertFalse(os.path.exists(path + ".tmp"), "no .tmp left behind")

    def test_gzip_output_is_deterministic(self):
        """Same input → byte-identical gzip (mtime=0), so unchanged periodic files don't
        churn git history."""
        with tempfile.TemporaryDirectory() as td:
            p1 = os.path.join(td, "a.csv.gz")
            p2 = os.path.join(td, "b.csv.gz")
            sa._write_csv(p1, ["x"], [["1"], ["2"]])
            sa._write_csv(p2, ["x"], [["1"], ["2"]])
            with open(p1, "rb") as f1, open(p2, "rb") as f2:
                self.assertEqual(f1.read(), f2.read(), "identical content → identical bytes")


class TestArchiveAllstocksToFiles(unittest.TestCase):
    def test_writes_per_source_csv_with_counts(self):
        with tempfile.TemporaryDirectory() as td:
            counts = _run_archive(td, date_str="2026-06-24")
        # daily sources keyed by date
        self.assertEqual(counts["bwibbu_daily"], 2)
        self.assertEqual(counts["mi_margn_daily"], 2)
        self.assertEqual(counts["t86_daily"], 2)
        self.assertEqual(counts["tpex_3insti_daily"], 1)
        self.assertEqual(counts["tpex_per_daily"], 1)
        # tdcc keyed by weekly asof (from _TDCC_ROWS date 20260620)
        self.assertEqual(counts["tdcc_weekly"], 2)

    def test_files_land_at_expected_paths(self):
        with tempfile.TemporaryDirectory() as td:
            _run_archive(td, date_str="2026-06-24")
            self.assertTrue(os.path.exists(os.path.join(td, "bwibbu_daily", "2026-06-24.csv.gz")))
            self.assertTrue(os.path.exists(os.path.join(td, "t86_daily", "2026-06-24.csv.gz")))
            # tdcc filename = weekly asof, NOT the run date
            self.assertTrue(os.path.exists(os.path.join(td, "tdcc_weekly", "2026-06-20.csv.gz")))

    def test_csv_header_matches_schema_and_values(self):
        with tempfile.TemporaryDirectory() as td:
            _run_archive(td, date_str="2026-06-24")
            header, rows = _read_csv(os.path.join(td, "bwibbu_daily", "2026-06-24.csv.gz"))
            self.assertEqual(header, sa.TAB_HEADERS["bwibbu_daily"])
            d = dict(zip(header, rows[0]))
            self.assertEqual(d["date"], "2026-06-24")
            self.assertEqual(d["code"], "2330")
            self.assertEqual(d["name"], "台積電")
            self.assertEqual(d["per"], "22.5")

    def test_monthly_key_for_t187ap03(self):
        with tempfile.TemporaryDirectory() as td:
            counts = _run_archive(td, date_str="2026-06-24", t187ap03_fn=lambda: _T187_RAW)
            self.assertEqual(counts["t187ap03_monthly"], 1)
            # filename = yyyymm, not the daily date
            self.assertTrue(os.path.exists(os.path.join(td, "t187ap03_monthly", "2026-06.csv.gz")))

    def test_empty_source_writes_no_file_count_zero(self):
        with tempfile.TemporaryDirectory() as td:
            counts = _run_archive(td, date_str="2026-06-24", bwibbu_fn=lambda: [])
            self.assertEqual(counts["bwibbu_daily"], 0)
            self.assertFalse(os.path.exists(os.path.join(td, "bwibbu_daily", "2026-06-24.csv.gz")))

    def test_source_filter_writes_only_that_source(self):
        with tempfile.TemporaryDirectory() as td:
            counts = _run_archive(td, date_str="2026-06-24", source_filter="bwibbu_daily")
            self.assertEqual(counts.get("bwibbu_daily"), 2)
            self.assertNotIn("t86_daily", counts)
            self.assertTrue(os.path.exists(os.path.join(td, "bwibbu_daily", "2026-06-24.csv.gz")))
            self.assertFalse(os.path.exists(os.path.join(td, "t86_daily", "2026-06-24.csv.gz")))

    def test_unknown_source_filter_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(_run_archive(td, source_filter="nope"), {})

    def test_fetch_failure_is_isolated_skip(self):
        def boom():
            raise RuntimeError("network down")
        with tempfile.TemporaryDirectory() as td:
            counts = _run_archive(td, date_str="2026-06-24", bwibbu_fn=boom)
            self.assertEqual(counts["bwibbu_daily"], -1)          # failed source SKIP
            self.assertEqual(counts["mi_margn_daily"], 2)          # others unaffected
            self.assertTrue(os.path.exists(os.path.join(td, "mi_margn_daily", "2026-06-24.csv.gz")))

    def test_archive_needs_no_google_credentials(self):
        """Archive mode must not call get_client (keyless). Patch get_client to raise."""
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("sheets_sync_allstocks.get_client",
                            side_effect=AssertionError("get_client must NOT be called in archive mode")):
                counts = _run_archive(td, date_str="2026-06-24")
        self.assertEqual(counts["bwibbu_daily"], 2)


class TestArchiveCLI(unittest.TestCase):
    def test_archive_files_cli_no_sa_still_runs(self):
        """--archive-files runs even when GOOGLE_SA_JSON is unset (routed before the SA guard)."""
        old = os.environ.pop("GOOGLE_SA_JSON", None)
        try:
            with tempfile.TemporaryDirectory() as td:
                with mock.patch("sheets_sync_allstocks.archive_allstocks_to_files",
                                return_value={"bwibbu_daily": 2}) as m:
                    rc = sa.main(["--archive-files", "--date", "2026-06-24", "--out-dir", td])
            self.assertEqual(rc, 0)
            m.assert_called_once()
            self.assertEqual(m.call_args.kwargs.get("date_str"), "2026-06-24")
        finally:
            if old is not None:
                os.environ["GOOGLE_SA_JSON"] = old


# ═══════════════════════════════════════════════════════════════════════════════
# auto_register() — keyless SA-side discovery (drops the Apps Script GitHub PAT)
# ═══════════════════════════════════════════════════════════════════════════════


class TestAutoRegisterRegistersOnlyNewMonths(unittest.TestCase):
    """openall() exposes 2 matching sheets; only the one missing from the index is
    registered (idempotent). The index file must gain the new month's id."""

    def test_registers_only_unindexed_matching_sheet(self):
        jan = _FakeSpreadsheet(title="smartstock-allstocks-2027-01", sheet_id="ID_JAN")
        feb = _FakeSpreadsheet(title="smartstock-allstocks-2027-02", sheet_id="ID_FEB")
        client = _FakeClient(existing=[jan, feb])
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({"2027-01": "ID_JAN"}, f)
            tmp = f.name
        try:
            with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
                registered = sa.auto_register(index_path=tmp,
                                              user_email="test@example.com")
            self.assertEqual(registered, ["2027-02"],
                             "only the un-indexed month must be registered")
            # bootstrap must reuse the existing sheet (existing_id path), never create
            self.assertEqual(len(client.create_calls), 0,
                             "auto_register must never create() a sheet")
            self.assertIn("ID_FEB", client.open_by_key_calls,
                          "the new month's sheet must be opened by key")
            with open(tmp, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data.get("2027-01"), "ID_JAN",
                             "pre-existing index entry must be preserved")
            self.assertEqual(data.get("2027-02"), "ID_FEB",
                             "index file must gain the newly-registered month's id")
        finally:
            os.unlink(tmp)


class TestAutoRegisterIgnoresNonMatchingTitles(unittest.TestCase):
    """Sheets whose titles don't match smartstock-allstocks-YYYY-MM (incl. an
    invalid month like 2026-13) must all be ignored → returns []."""

    def test_non_matching_titles_yield_empty(self):
        sheets = [
            _FakeSpreadsheet(title="Untitled spreadsheet", sheet_id="U1"),
            _FakeSpreadsheet(title="smartstock-picks", sheet_id="P1"),
            _FakeSpreadsheet(title="smartstock-allstocks-2026-13", sheet_id="BAD"),
        ]
        client = _FakeClient(existing=sheets)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({}, f)
            tmp = f.name
        try:
            with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
                registered = sa.auto_register(index_path=tmp)
            self.assertEqual(registered, [],
                             "no non-matching / invalid-month sheet may be registered")
            self.assertEqual(len(client.create_calls), 0)
            self.assertEqual(client.open_by_key_calls, [],
                             "no sheet should be opened when nothing matches")
        finally:
            os.unlink(tmp)


class TestAutoRegisterGracefulWhenSAMissing(unittest.TestCase):
    """get_client() → None (GOOGLE_SA_JSON unset) must return None, never raise."""

    def test_returns_none_when_sa_missing(self):
        with mock.patch("sheets_sync_allstocks.get_client", return_value=None):
            result = sa.auto_register(index_path=os.devnull)
        self.assertIsNone(result, "auto_register must return None when SA is missing")


class TestCliAutoRegister(unittest.TestCase):
    """main(['--auto-register']) with SA set + get_client mocked → 0 + REGISTERED= line."""

    def test_cli_auto_register_returns_0_and_prints_registered(self):
        feb = _FakeSpreadsheet(title="smartstock-allstocks-2027-02", sheet_id="ID_FEB")
        client = _FakeClient(existing=[feb])
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({}, f)
            tmp = f.name
        captured = io.StringIO()
        fake_env = {"GOOGLE_SA_JSON": '{"type":"service_account"}'}
        try:
            with mock.patch.dict(os.environ, fake_env):
                with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
                    with mock.patch("sys.stdout", captured):
                        rc = sa.main(["--auto-register",
                                       "--index-path", tmp,
                                       "--user-email", "test@example.com"])
            self.assertEqual(rc, 0)
            out = captured.getvalue()
            self.assertIn("REGISTERED=", out)
            self.assertIn("2027-02", out,
                          "the discovered month must appear on the REGISTERED line")
        finally:
            os.unlink(tmp)


# ═══════════════════════════════════════════════════════════════════════════════
# sync_allstocks_from_files() — mirror the complete git-file gz-CSV archive → Sheet
# (gets the TWSE/TDCC sources a CI-IP live --sync cannot fetch; they arrive in the
#  archive via the local TW-IP archiver task)
# ═══════════════════════════════════════════════════════════════════════════════


class TestReadCsv(unittest.TestCase):
    def test_read_csv_roundtrips_write_csv_dropping_header(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "x.csv.gz")
            sa._write_csv(p, ["a", "b"], [["1", "2"], ["3", "4"]])
            self.assertEqual(sa._read_csv(p), [["1", "2"], ["3", "4"]])

    def test_read_csv_missing_file_returns_empty(self):
        self.assertEqual(sa._read_csv(os.path.join("no", "such", "f.csv.gz")), [])


class TestLatestKeyFile(unittest.TestCase):
    def test_returns_lexicographically_greatest_gz(self):
        with tempfile.TemporaryDirectory() as td:
            for k in ("20260606", "20260620", "20260613"):
                sa._write_csv(os.path.join(td, f"{k}.csv.gz"), ["a"], [["1"]])
            self.assertTrue(sa._latest_key_file(td).endswith("20260620.csv.gz"))

    def test_returns_none_when_dir_absent(self):
        self.assertIsNone(sa._latest_key_file(os.path.join("no", "such", "dir")))


def _seed_archive(adir, tab, key, rows):
    """Write a real fixture gz-CSV via the production writer (round-trips _read_csv)."""
    sa._write_csv(os.path.join(adir, tab, f"{key}.csv.gz"), sa.TAB_HEADERS[tab], rows)


class TestSyncAllstocksFromFiles(unittest.TestCase):
    def test_daily_tab_file_upserted_by_date(self):
        sh = _make_sync_sheet()
        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            adir = os.path.join(tmp, "_allstocks")
            rows = [["2026-06-24", "2330", "台積電", "22.5", "1.8", "6.0", "1150624"],
                    ["2026-06-24", "2317", "鴻海", "", "3.2", "1.5", "1150624"]]
            _seed_archive(adir, "bwibbu_daily", "2026-06-24", rows)
            client = _FakeClientFull(sheet=sh)
            with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
                counts = sa.sync_allstocks_from_files(
                    date_str="2026-06-24", index_path=idx, allstocks_dir=adir)
        self.assertEqual(counts["bwibbu_daily"], 2)
        ws = sh._worksheets["bwibbu_daily"]
        self.assertEqual(len(ws._rows), 3, "header + 2 data rows")
        self.assertEqual(ws._rows[1][0], "2026-06-24")
        self.assertEqual(ws._rows[2][1], "2317")

    def test_tdcc_newest_file_upserted_by_asof(self):
        sh = _make_sync_sheet()
        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            adir = os.path.join(tmp, "_allstocks")
            old = [["20260613", "2330", "40.0", "400", "", "", "台積電"]]
            new = [["20260620", "2330", "45.5", "500", "", "", "台積電"],
                   ["20260620", "2317", "38.2", "300", "", "", "鴻海"]]
            _seed_archive(adir, "tdcc_weekly", "20260613", old)
            _seed_archive(adir, "tdcc_weekly", "20260620", new)
            client = _FakeClientFull(sheet=sh)
            with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
                counts = sa.sync_allstocks_from_files(
                    date_str="2026-06-24", index_path=idx, allstocks_dir=adir)
        self.assertEqual(counts["tdcc_weekly"], 2,
                         "newest weekly file (20260620) must be the one mirrored")
        ws = sh._worksheets["tdcc_weekly"]
        self.assertEqual(len(ws._rows), 3, "header + 2 newest-file rows")
        self.assertEqual(ws._rows[1][0], "20260620",
                         "upsert key must be the asof_date from the rows")

    def test_missing_file_counts_zero_no_crash(self):
        sh = _make_sync_sheet()
        with tempfile.TemporaryDirectory() as tmp:
            idx = _make_index_file(tmp)
            adir = os.path.join(tmp, "_allstocks")  # empty: no source files
            client = _FakeClientFull(sheet=sh)
            with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
                counts = sa.sync_allstocks_from_files(
                    date_str="2026-06-24", index_path=idx, allstocks_dir=adir)
        for tab in sa._TAB_ORDER:
            self.assertEqual(counts.get(tab), 0, f"{tab} must be 0 with no source file")
            self.assertEqual(len(sh._worksheets[tab]._rows), 1,
                             f"{tab} must stay header-only with no source file")

    def test_returns_none_when_sa_missing(self):
        with mock.patch("sheets_sync_allstocks.get_client", return_value=None):
            result = sa.sync_allstocks_from_files(
                date_str="2026-06-24", index_path=os.devnull)
        self.assertIsNone(result)

    def test_returns_none_when_month_absent(self):
        sh = _make_sync_sheet()
        with tempfile.TemporaryDirectory() as tmp:
            idx_path = os.path.join(tmp, "idx.json")
            with open(idx_path, "w", encoding="utf-8") as f:
                json.dump({"2025-01": "OLD_SHEET"}, f)
            client = _FakeClientFull(sheet=sh)
            with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
                result = sa.sync_allstocks_from_files(
                    date_str="2026-06-24", index_path=idx_path)
        self.assertIsNone(result)


class TestCliSyncFromFiles(unittest.TestCase):
    def test_cli_sync_from_files_returns_0_and_forwards_args(self):
        with mock.patch("sheets_sync_allstocks.sync_allstocks_from_files",
                        return_value={}) as m, \
             mock.patch.dict(os.environ, {"GOOGLE_SA_JSON": '{"type":"service_account"}'}):
            rc = sa.main(["--sync-from-files", "--date", "2026-06-24",
                          "--source", "tdcc_weekly"])
        self.assertEqual(rc, 0)
        m.assert_called_once()
        self.assertEqual(m.call_args.kwargs.get("date_str"), "2026-06-24")
        self.assertEqual(m.call_args.kwargs.get("source_filter"), "tdcc_weekly")


# ═══════════════════════════════════════════════════════════════════════════════
# _upsert_allstocks — ranged contiguous-run delete + 429 backoff
# (was per-row delete -> bursts past Sheets 60-writes/min on big re-mirrors)
# ═══════════════════════════════════════════════════════════════════════════════

class _FakeWorksheet429(_FakeWorksheetFull):
    """Raises a 429-like APIError on the first `fail_k` calls to each named op, then
    succeeds — proves _write_with_retry backs off and retries (sleep is patched no-op).
    The 429 is raised BEFORE the success counter increments, so delete_calls/append_calls
    count only the eventually-successful call."""

    def __init__(self, title, headers=None, fail_k=0, fail_on=("delete_rows",)):
        super().__init__(title, headers=headers)
        self._fail_on = set(fail_on)
        self._fails_left = {name: fail_k for name in self._fail_on}

    def _maybe_429(self, name):
        if self._fails_left.get(name, 0) > 0:
            self._fails_left[name] -= 1
            raise Exception(
                "APIError: [429]: Quota exceeded for quota metric 'Write requests'")

    def col_values(self, col):
        self._maybe_429("col_values")
        return super().col_values(col)

    def delete_rows(self, start, end=None):
        self._maybe_429("delete_rows")
        return super().delete_rows(start, end=end)

    def append_rows(self, rows, value_input_option=None):
        self._maybe_429("append_rows")
        return super().append_rows(rows, value_input_option=value_input_option)


def _ws_with_dates(date_strs):
    """Build a _FakeWorksheetFull with header + one data row per entry in date_strs
    (col 0 = date, col 1 = a unique code)."""
    ws = _FakeWorksheetFull("t", headers=["date", "code"])
    for i, d in enumerate(date_strs):
        ws._rows.append([d, f"c{i}"])
    return ws


class TestUpsertAllstocksRangedDelete(unittest.TestCase):
    DATE = "2026-06-24"

    def test_contiguous_helper_groups_runs(self):
        self.assertEqual(sa._contiguous_runs([4, 5, 6, 9, 10]), [(4, 6), (9, 10)])
        self.assertEqual(sa._contiguous_runs([]), [])
        self.assertEqual(sa._contiguous_runs([3]), [(3, 3)])

    def test_six_contiguous_dups_one_ranged_delete(self):
        """6 same-date rows must collapse to ONE delete_rows(start,end), not 6 calls."""
        ws = _ws_with_dates([self.DATE] * 6)  # rows 2..7, all the same date
        self.assertEqual(ws.delete_calls, 0)
        sa._upsert_allstocks(ws, 0, self.DATE, [[self.DATE, "new"]])
        self.assertEqual(ws.delete_calls, 1,
                         "6 contiguous dups => exactly 1 ranged delete, not 6")
        self.assertEqual(len(ws._rows), 2, "header + 1 appended row")
        self.assertEqual(ws._rows[1], [self.DATE, "new"])

    def test_two_separate_runs_two_ranged_deletes(self):
        """Two non-adjacent dup runs => two ranged delete calls."""
        ws = _ws_with_dates([self.DATE, "2026-06-23", self.DATE])  # dups at rows 2 and 4
        sa._upsert_allstocks(ws, 0, self.DATE, [])  # no append
        self.assertEqual(ws.delete_calls, 2,
                         "two separated dup runs => two ranged deletes")
        self.assertEqual(len(ws._rows), 2, "header + the surviving non-dup row")
        self.assertEqual(ws._rows[1][0], "2026-06-23")

    def test_transient_429_on_delete_is_retried(self):
        """A transient 429 on delete is retried (sleep patched) then succeeds."""
        ws = _FakeWorksheet429("t", headers=["date", "code"],
                               fail_k=1, fail_on=("delete_rows",))
        ws._rows.append([self.DATE, "old"])
        with mock.patch.object(sa.time, "sleep", return_value=None) as msleep:
            sa._upsert_allstocks(ws, 0, self.DATE, [[self.DATE, "new"]])
        self.assertGreaterEqual(msleep.call_count, 1, "429 must trigger a backoff sleep")
        self.assertEqual(ws.delete_calls, 1, "delete must succeed after the retry")
        self.assertEqual(len(ws._rows), 2)
        self.assertEqual(ws._rows[1], [self.DATE, "new"])

    def test_non_429_error_propagates_immediately(self):
        """A non-429 error is re-raised immediately with no retry/backoff."""
        ws = _FakeWorksheetFull("t", headers=["date", "code"])
        ws._rows.append([self.DATE, "old"])

        def boom(*a, **k):
            raise RuntimeError("boom")

        with mock.patch.object(ws, "delete_rows", side_effect=boom), \
             mock.patch.object(sa.time, "sleep", return_value=None) as msleep:
            with self.assertRaises(RuntimeError):
                sa._upsert_allstocks(ws, 0, self.DATE, [[self.DATE, "new"]])
        self.assertEqual(msleep.call_count, 0, "non-429 must not back off")

    def test_reupsert_same_date_idempotent(self):
        """Re-upserting the same date yields an identical final row set."""
        ws = _FakeWorksheetFull("t", headers=["date", "code"])
        rows = [[self.DATE, "2330"], [self.DATE, "2317"]]
        sa._upsert_allstocks(ws, 0, self.DATE, rows)
        first = [r[:] for r in ws._rows]
        sa._upsert_allstocks(ws, 0, self.DATE, rows)  # re-run
        self.assertEqual(ws._rows, first,
                         "re-upsert same date must yield identical rows")
        self.assertEqual(len(ws._rows), 3, "header + 2 data rows")


if __name__ == "__main__":
    unittest.main()
