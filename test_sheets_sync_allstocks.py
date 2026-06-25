"""Tests for sheets_sync_allstocks.py — bootstrap mode + 7 P0 tab schemas.

All tests run offline (no gspread network calls). The _FakeWorksheet /
_FakeSpreadsheet / _FakeClient pattern mirrors test_sheets_sync.py so the two
test modules share the same idiom.

CONTRACT: these cover BOOTSTRAP ONLY.  Fetcher tests live in Sprint 2 P2.
"""
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


if __name__ == "__main__":
    unittest.main()
