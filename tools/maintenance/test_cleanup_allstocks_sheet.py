# -*- coding: utf-8 -*-
"""Offline tests for tools/maintenance/cleanup_allstocks_sheet.py (fake gspread)."""
import json
import os
import tempfile
import unittest

import cleanup_allstocks_sheet as cl
import sheets_sync_allstocks as sa


class _FakeWs:
    """Worksheet double with grid dimensions + the ops cleanup uses."""

    def __init__(self, title, rows=10, cols=26, index=0):
        self.title = title
        self.row_count = rows
        self.col_count = cols
        self.index = index
        self._rows = []
        self.resize_calls = []
        self.append_calls = []

    def update(self, values, range_name=None):
        if self._rows:
            self._rows[0] = values[0]
        else:
            self._rows.append(values[0])

    def append_rows(self, rows, value_input_option=None):
        self.append_calls.append(len(rows))
        self._rows.extend(rows)
        self.row_count = max(self.row_count, len(self._rows))

    def resize(self, rows=None, cols=None):
        self.resize_calls.append({"rows": rows, "cols": cols})
        if rows is not None:
            self.row_count = rows
        if cols is not None:
            self.col_count = cols


class _FakeSh:
    def __init__(self, tabs):
        self._tabs = list(tabs)
        self.deleted = []
        self.added = []

    def worksheets(self):
        return list(self._tabs)

    def worksheet(self, title):
        for ws in self._tabs:
            if ws.title == title:
                return ws
        raise Exception(f"worksheet {title!r} not found")

    def del_worksheet(self, ws):
        self.deleted.append(ws.title)
        self._tabs.remove(ws)

    def add_worksheet(self, title, rows=1000, cols=26, index=None):
        self.added.append({"title": title, "rows": rows, "cols": cols,
                           "index": index})
        ws = _FakeWs(title, rows=rows, cols=cols,
                     index=index if index is not None else len(self._tabs))
        if index is not None and 0 <= index <= len(self._tabs):
            self._tabs.insert(index, ws)
        else:
            self._tabs.append(ws)
        return ws


class _FakeClient:
    def __init__(self, sh, expect_key=None):
        self._sh = sh
        self.expect_key = expect_key
        self.opened = []

    def open_by_key(self, key):
        self.opened.append(key)
        if self.expect_key is not None and key != self.expect_key:
            raise Exception(f"unexpected key {key}")
        return self._sh


def _sheet_with_all_tabs(sec_ftd_rows=100000, default_cols=26):
    tabs = []
    for i, tab in enumerate(sa._TAB_ORDER):
        rows = sec_ftd_rows if tab == "sec_ftd_semimonthly" else 10000
        tabs.append(_FakeWs(tab, rows=rows, cols=default_cols, index=i))
    return _FakeSh(tabs)


class TestEstimateCells(unittest.TestCase):
    def test_sums_grid_rows_times_cols(self):
        sh = _FakeSh([_FakeWs("a", rows=10, cols=4), _FakeWs("b", rows=5, cols=2)])
        per_tab, total = cl.estimate_cells(sh)
        self.assertEqual(per_tab["a"], (10, 4, 40))
        self.assertEqual(per_tab["b"], (5, 2, 10))
        self.assertEqual(total, 50)


class TestRebuildSecFtd(unittest.TestCase):
    _ROWS = [["20260615", "AAPL", "037833100", "12345"],
             ["20260630", "MSFT", "594918104", "5678"]]

    def test_apply_deletes_recreates_at_schema_width_and_writes(self):
        sh = _sheet_with_all_tabs()
        n = cl.rebuild_sec_ftd(sh, self._ROWS, apply=True)
        self.assertEqual(n, 2)
        self.assertIn("sec_ftd_semimonthly", sh.deleted)
        self.assertEqual(len(sh.added), 1)
        self.assertEqual(sh.added[0]["cols"],
                         len(sa.TAB_HEADERS["sec_ftd_semimonthly"]),
                         "rebuilt tab must be schema-width (4 cols)")
        ws = sh.worksheet("sec_ftd_semimonthly")
        self.assertEqual(ws._rows[0], sa.TAB_HEADERS["sec_ftd_semimonthly"])
        self.assertEqual(ws._rows[1:], self._ROWS)

    def test_apply_chunks_large_append(self):
        sh = _sheet_with_all_tabs()
        big = [["20260615", f"SYM{i}", "c", i] for i in range(45000)]
        cl.rebuild_sec_ftd(sh, big, apply=True)
        ws = sh.worksheet("sec_ftd_semimonthly")
        self.assertEqual(sum(ws.append_calls), 45000)
        self.assertEqual(len(ws.append_calls), 3,
                         "45k rows @ 20k chunk = 3 append calls")

    def test_dry_run_mutates_nothing(self):
        sh = _sheet_with_all_tabs()
        n = cl.rebuild_sec_ftd(sh, self._ROWS, apply=False)
        self.assertEqual(n, 2)
        self.assertEqual(sh.deleted, [])
        self.assertEqual(sh.added, [])

    def test_empty_rows_refused(self):
        sh = _sheet_with_all_tabs()
        with self.assertRaises(RuntimeError):
            cl.rebuild_sec_ftd(sh, [], apply=True)


class TestResizeTabs(unittest.TestCase):
    def test_apply_resizes_26col_tabs_to_schema_width(self):
        sh = _sheet_with_all_tabs(default_cols=26)
        changed = cl.resize_tabs_to_schema(sh, apply=True)
        self.assertIn("bwibbu_daily", changed)
        ws = sh.worksheet("bwibbu_daily")
        self.assertEqual(ws.col_count, len(sa.TAB_HEADERS["bwibbu_daily"]))
        self.assertEqual(ws.resize_calls[-1]["cols"],
                         len(sa.TAB_HEADERS["bwibbu_daily"]))

    def test_skip_list_honoured(self):
        sh = _sheet_with_all_tabs(default_cols=26)
        changed = cl.resize_tabs_to_schema(sh, apply=True,
                                           skip=("sec_ftd_semimonthly",))
        self.assertNotIn("sec_ftd_semimonthly", changed)
        ws = sh.worksheet("sec_ftd_semimonthly")
        self.assertEqual(ws.resize_calls, [])

    def test_non_schema_tab_untouched(self):
        sh = _FakeSh([_FakeWs("my_random_tab", rows=5, cols=26)])
        changed = cl.resize_tabs_to_schema(sh, apply=True)
        self.assertEqual(changed, {})

    def test_already_schema_width_noop(self):
        sh = _FakeSh([_FakeWs("bwibbu_daily", rows=10,
                              cols=len(sa.TAB_HEADERS["bwibbu_daily"]))])
        changed = cl.resize_tabs_to_schema(sh, apply=True)
        self.assertEqual(changed, {})

    def test_dry_run_reports_but_does_not_resize(self):
        sh = _sheet_with_all_tabs(default_cols=26)
        changed = cl.resize_tabs_to_schema(sh, apply=False)
        self.assertIn("bwibbu_daily", changed)
        self.assertEqual(sh.worksheet("bwibbu_daily").resize_calls, [])


class TestRunEndToEnd(unittest.TestCase):
    def test_full_apply_flow_with_git_truth(self):
        sh = _sheet_with_all_tabs()
        with tempfile.TemporaryDirectory() as td:
            idx = os.path.join(td, "idx.json")
            with open(idx, "w", encoding="utf-8") as f:
                json.dump({"2026-07": "SHEET_JULY"}, f)
            adir = os.path.join(td, "_allstocks")
            rows = [["20260615", "AAPL", "037833100", "12345"]]
            sa._write_csv(os.path.join(adir, "sec_ftd_semimonthly",
                                       "2026-07.csv.gz"),
                          sa.TAB_HEADERS["sec_ftd_semimonthly"], rows)
            client = _FakeClient(sh, expect_key="SHEET_JULY")
            before, after = cl.run("2026-07", index_path=idx,
                                   allstocks_dir=adir, apply=True,
                                   client=client)
        self.assertEqual(client.opened, ["SHEET_JULY"])
        self.assertLess(after, before,
                        "cell estimate must shrink after rebuild+resize")
        ws = sh.worksheet("sec_ftd_semimonthly")
        self.assertEqual(len(ws._rows) - 1, 1, "git truth rows written")

    def test_month_not_in_index_raises(self):
        with tempfile.TemporaryDirectory() as td:
            idx = os.path.join(td, "idx.json")
            with open(idx, "w", encoding="utf-8") as f:
                json.dump({}, f)
            with self.assertRaises(RuntimeError):
                cl.run("2026-07", index_path=idx, client=_FakeClient(_FakeSh([])))


if __name__ == "__main__":
    unittest.main()
