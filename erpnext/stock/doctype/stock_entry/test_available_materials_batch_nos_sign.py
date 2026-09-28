# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: GNU General Public License v3. See license.txt

"""Regression test for get_available_materials() mishandling the sign of
row.batch_nos values.

Bug: row.batch_nos can be populated from more than one source, and those
sources are not consistently signed -- some give the raw signed stock
ledger qty (negative for an outward-matched row, positive for inward),
others always give a positive magnitude. get_available_materials() added
row.batch_nos straight into batch_details on the "material transferred in"
branch, and on the "material consumed" branch it *also* added it (instead
of subtracting), so a consumed batch's qty was added back into what's
still considered available for the Work Order -- overstating it.
"""

from __future__ import annotations

import importlib
import unittest
from types import SimpleNamespace
from unittest import mock

import frappe
import erpnext.stock.doctype.stock_entry.stock_entry as stock_entry_module


class TestAvailableMaterialsBatchNosSign(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# See test_stock_reservation_dedup.py for why this reload is needed: a
		# site with a custom app that overrides this exact function at app
		# boot leaves the module attribute permanently overridden for the
		# rest of the process, so re-importing the module alone isn't enough.
		importlib.reload(stock_entry_module)
		# staticmethod() avoids the plain function being turned into a bound
		# method (with `self` injected as its first arg) when accessed as
		# self.get_available_materials(...) below.
		cls.get_available_materials = staticmethod(stock_entry_module.get_available_materials)

	def _row(self, **kwargs):
		defaults = dict(
			item_code="ITEM-1",
			warehouse="WIP-1",
			s_warehouse=None,
			purpose="Material Transfer for Manufacture",
			qty=0,
			batch_no=None,
			batch_nos=None,
			serial_no=None,
			serial_nos=None,
			original_item=None,
		)
		defaults.update(kwargs)
		return frappe._dict(defaults)

	def test_transfer_in_normalizes_negative_batch_nos_qty(self):
		"""A transfer-in row whose batch_nos source gave a negative
		(outward-signed) qty must still add a positive amount to what's
		available -- not subtract."""
		rows = [self._row(qty=20, batch_nos={"BATCH-A": -20})]

		with mock.patch.object(stock_entry_module, "get_stock_entry_data", return_value=rows):
			available = self.get_available_materials("WO-1")

		item_data = available[("ITEM-1", "WIP-1")]
		self.assertEqual(item_data.batch_details["BATCH-A"], 20)
		self.assertEqual(item_data.qty, 20)

	def test_consumption_subtracts_regardless_of_batch_nos_sign(self):
		"""A consumption row (purpose='Manufacture') must reduce
		batch_details for that batch, whether its batch_nos source gave a
		positive or a negative qty -- never add it back."""
		rows = [
			self._row(qty=20, batch_nos={"BATCH-A": -20}),
			self._row(
				purpose="Manufacture",
				s_warehouse="WIP-1",
				warehouse=None,
				qty=6,
				batch_nos={"BATCH-A": 6},
			),
		]

		with mock.patch.object(stock_entry_module, "get_stock_entry_data", return_value=rows):
			available = self.get_available_materials("WO-1")

		item_data = available[("ITEM-1", "WIP-1")]
		self.assertEqual(item_data.batch_details["BATCH-A"], 14)
		self.assertEqual(item_data.qty, 14)


if __name__ == "__main__":
	unittest.main()
