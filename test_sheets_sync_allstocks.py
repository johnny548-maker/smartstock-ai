"""Tests for sheets_sync_allstocks.py — bootstrap mode + 7 P0 tab schemas.

All tests run offline (no gspread network calls). The _FakeWorksheet /
_FakeSpreadsheet / _FakeClient pattern mirrors test_sheets_sync.py so the two
test modules share the same idiom.

CONTRACT: these cover BOOTSTRAP ONLY.  Fetcher tests live in Sprint 2 P2.
"""
import contextlib
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
        result, client = _run_bootstrap()
        sh = client._drive[0]
        self.assertEqual(len(sh._worksheets), 7,
                         f"expected 7 tabs, got {len(sh._worksheets)}: "
                         f"{list(sh._worksheets.keys())}")

    def test_tab_names_match_spec(self):
        result, client = _run_bootstrap()
        sh = client._drive[0]
        expected = {
            "bwibbu_daily", "mi_margn_daily", "t86_daily",
            "tpex_3insti_daily", "tpex_margin_daily", "tpex_per_daily",
            "tdcc_weekly",
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
        self.assertEqual(len(tabs), 7)

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
            self.assertEqual(len(sh._worksheets), 7,
                             "tabs must NOT be duplicated on second bootstrap call")
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
        # 7 tabs must be ensured
        self.assertEqual(len(sh._worksheets), 7)


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
        # First call — creates 7 tabs
        with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
            sa.bootstrap(month="2026-06", user_email="x@x.com",
                         existing_id="abc123", index_path=os.devnull)
        # Second call — must not duplicate tabs
        with mock.patch("sheets_sync_allstocks.get_client", return_value=client):
            sa.bootstrap(month="2026-06", user_email="x@x.com",
                         existing_id="abc123", index_path=os.devnull)
        self.assertEqual(len(sh._worksheets), 7,
                         "tabs must NOT be duplicated on second bootstrap call with existing_id")
        self.assertEqual(len(client.create_calls), 0,
                         "create() must never be called when existing_id is supplied")


# ═══════════════════════════════════════════════════════════════════════════════
# Sprint 2 P2 Tests — sync_allstocks() 7 P0 fetchers
# ═══════════════════════════════════════════════════════════════════════════════

# Extended _FakeWorksheet that supports delete_rows for upsert testing
class _FakeWorksheetFull(_FakeWorksheet):
    """_FakeWorksheet extended with delete_rows for idempotent-upsert testing."""

    def delete_rows(self, rn):
        """Delete 1-based row number rn (row 1 = header)."""
        if 1 <= rn <= len(self._rows):
            self._rows.pop(rn - 1)


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
              tdcc_fn=None):
    """Run sync_allstocks with fake client + injected fetchers; return counts dict."""
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
                 mock.patch("sheets_sync_allstocks._fetch_tdcc", return_value=_TDCC_ROWS):
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
                    "tpex_3insti_daily", "tpex_margin_daily", "tpex_per_daily"):
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


if __name__ == "__main__":
    unittest.main()
