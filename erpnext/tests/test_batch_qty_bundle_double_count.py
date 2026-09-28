# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: GNU General Public License v3. See license.txt

"""Regression test: batch qty summed twice when a Stock Ledger Entry has
both `batch_no` and a linked Serial and Batch Bundle set.

A Stock Ledger Entry row can carry the same batch's qty in two places at
once: its own `batch_no` column, and (separately) a linked Serial and Batch
Bundle whose child Serial and Batch Entry rows also name that batch. Several
places in ERPNext compute a batch's available/opening qty by summing
`batch_no` rows directly AND separately summing the bundle's Serial and
Batch Entry rows for the same batch, then adding both totals together. When
a row has both fields set, that row's qty gets counted once on each side,
so the combined total comes out roughly double the batch's real qty.

This showed up in practice as: a Work Order manufacture entry's batch
auto-picker read an inflated "available" qty (e.g. 29 instead of the real
19), let a consumption be entered for more than was truly free, and submit
then failed with a negative stock error -- or, in the Stock Ledger report,
an opening balance of 64 was shown for a batch whose true qty was 32.

The fix, applied the same way in each of the five affected functions below,
is to skip rows that have a `serial_and_batch_bundle` set when summing
`batch_no` directly, since that row's qty is already being counted through
the bundle side. This test does not need a live database: it patches
`frappe.db.sql` to capture the SQL text each function actually sends, and
checks that the exclusion clause is present in it.
"""

from __future__ import annotations

import importlib
import unittest
from unittest import mock

import frappe

import erpnext.controllers.queries as queries_module
import erpnext.stock.deprecated_serial_batch as deprecated_serial_batch_module
import erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle as sabb_module
import erpnext.stock.report.batch_wise_balance_history.batch_wise_balance_history as bwbh_module
import erpnext.stock.report.stock_ledger.stock_ledger as stock_ledger_module


def _capture_sql():
	"""Returns (mock, calls) -- calls collects every SQL string frappe.db.sql was
	called with, so a test can inspect a query's WHERE clause without touching the
	DB. Framework internals (permission/doctype lookups) also go through
	frappe.db.sql while these functions run, so a test must pick out its own query
	from `calls` rather than assume it is the first one sent."""
	calls: list[str] = []

	def fake_sql(query, *args, **kwargs):
		calls.append(str(query))
		return []

	return mock.patch.object(frappe.local.db, "sql", side_effect=fake_sql), calls


def _find_sle_query(calls: list[str]) -> str:
	"""Picks out the one query, among everything frappe.db.sql was called with,
	that actually reads the Stock Ledger Entry table -- ignoring unrelated
	framework-internal lookups (permissions, doctype metadata, etc.) that also
	go through frappe.db.sql while the function under test runs."""
	for call in calls:
		if "tabStock Ledger Entry" in call and "batch_no" in call:
			return call
	raise AssertionError(f"no Stock Ledger Entry batch_no query found among: {calls}")


class TestBatchQtyBundleDoubleCount(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# alliedcustom (and any other installed app) can monkey-patch these exact
		# functions at app-boot time by reassigning the module attribute directly.
		# That reassignment survives for the rest of this process, so importing the
		# module isn't enough -- it must be reloaded here to get the genuine,
		# unpatched core implementation this test is actually meant to verify.
		importlib.reload(sabb_module)
		importlib.reload(queries_module)
		importlib.reload(stock_ledger_module)
		importlib.reload(bwbh_module)
		importlib.reload(deprecated_serial_batch_module)

	def test_get_stock_ledgers_batches_excludes_bundle_linked_rows(self):
		patch, calls = _capture_sql()
		with patch:
			sabb_module.get_stock_ledgers_batches(frappe._dict(based_on="FIFO"))
		self.assertIn("serial_and_batch_bundle", _find_sle_query(calls))

	def test_get_batches_from_stock_ledger_entries_excludes_bundle_linked_rows(self):
		patch, calls = _capture_sql()
		with patch:
			queries_module.get_batches_from_stock_ledger_entries(
				searchfields=[], txt=None, filters=frappe._dict(item_code="ITEM-1")
			)
		self.assertIn("serial_and_batch_bundle", _find_sle_query(calls))

	def test_get_opening_balance_from_batch_excludes_bundle_linked_rows_on_direct_sum(self):
		patch, calls = _capture_sql()
		with patch:
			stock_ledger_module.get_opening_balance_from_batch(
				frappe._dict(batch_no="BATCH-1", from_date="2026-01-01", company="Test Company"),
				columns=[],
				sl_entries=[],
			)
		# The direct SLE.batch_no sum must exclude bundle-linked rows so it doesn't
		# overlap with the separate bundle-side query run further down.
		self.assertIn("serial_and_batch_bundle", _find_sle_query(calls))

	def test_get_stock_ledger_entries_for_batch_no_excludes_bundle_linked_rows(self):
		patch, calls = _capture_sql()
		with patch:
			bwbh_module.get_stock_ledger_entries_for_batch_no(
				frappe._dict(from_date="2026-01-01", to_date="2026-01-31")
			)
		self.assertIn("serial_and_batch_bundle", _find_sle_query(calls))

	def test_get_sle_for_batches_excludes_bundle_linked_rows(self):
		# get_sle_for_batches is a method on BatchNoValuation, inherited unchanged
		# from DeprecatedBatchNoValuation -- this is the exact function the
		# manufacture-submit valuation path calls.
		valuation = deprecated_serial_batch_module.DeprecatedBatchNoValuation.__new__(
			deprecated_serial_batch_module.DeprecatedBatchNoValuation
		)
		valuation.batchwise_valuation_batches = ["BATCH-1"]
		valuation.sle = frappe._dict(
			item_code="ITEM-1", warehouse="WH-1", posting_datetime=None, creation=None, name=None
		)

		patch, calls = _capture_sql()
		with patch:
			valuation.get_sle_for_batches()
		self.assertIn("serial_and_batch_bundle", _find_sle_query(calls))


if __name__ == "__main__":
	unittest.main()
