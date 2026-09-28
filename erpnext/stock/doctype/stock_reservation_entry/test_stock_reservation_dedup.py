# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: GNU General Public License v3. See license.txt

"""Regression test for StockReservation.make_stock_reservation_entries()
creating a duplicate Stock Reservation Entry when called more than once for
the same row.

Bug (confirmed live on a production Frappe Cloud site): calling this method
a second time for a Work Order that had already been reserved created a
second, independent Stock Reservation Entry for the exact same
voucher_detail_no, with no check for whether an active one already existed.
Five Work Orders accumulated 674 duplicate reservations between them this
way (as many as 3 for a single required-items row) purely from the method
running more than once -- each call blindly re-reserved material that was
already reserved.

That inflated, multiply-counted reserved total then made an unrelated
voucher's own, perfectly valid stock consumption fail at submit with
"Reserved Batch Conflict", for material that was never actually
double-booked -- just double-counted.
"""

from __future__ import annotations

import importlib
import unittest
from types import SimpleNamespace
from unittest import mock

import frappe
import erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry as sre_module


class TestStockReservationDedup(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# A site that installs a custom app patching this exact method (e.g. via
		# setattr on the class, at app boot) leaves the *class attribute*
		# permanently overridden for the rest of the process -- re-importing this
		# module doesn't help, since Python caches it in sys.modules. Reload the
		# module here so StockReservation.make_stock_reservation_entries is the
		# genuine, unpatched method this test is actually meant to exercise.
		importlib.reload(sre_module)
		cls.StockReservation = sre_module.StockReservation

	def test_has_active_reservation_checks_docstatus_and_status(self):
		with mock.patch.object(frappe.db, "exists", return_value=True) as mock_exists:
			result = self.StockReservation._has_active_reservation(
				SimpleNamespace(), "Work Order", "WO-1", "row-1"
			)

		self.assertTrue(result)
		mock_exists.assert_called_once_with(
			"Stock Reservation Entry",
			{
				"voucher_type": "Work Order",
				"voucher_no": "WO-1",
				"voucher_detail_no": "row-1",
				"docstatus": 1,
				"status": ("in", ("Reserved", "Partially Reserved")),
			},
		)

	def test_skips_row_with_existing_active_reservation(self):
		"""Calling make_stock_reservation_entries twice for the same Work
		Order created a second Stock Reservation Entry for the same row
		every time. A row whose voucher_detail_no already has an active
		reservation must be skipped entirely, not re-reserved."""
		row = frappe._dict(name="row-1", item_code="ITEM-1", required_qty=10)
		fake_self = SimpleNamespace(
			items=[row],
			doc=frappe._dict(doctype="Work Order", name="WO-1"),
			table_name="required_items",
			qty_field="required_qty",
			warehouse_field="source_warehouse",
			warehouse=None,
			_has_active_reservation=lambda *a, **k: True,
		)

		with mock.patch.object(frappe, "new_doc") as mock_new_doc:
			created = self.StockReservation.make_stock_reservation_entries(fake_self)

		mock_new_doc.assert_not_called()
		self.assertFalse(created)

	def test_creates_row_without_existing_reservation(self):
		"""A row with no existing active reservation must still be reserved
		normally -- the guard must not block genuinely new reservations."""
		row = frappe._dict(name="row-1", item_code="ITEM-1", required_qty=10, source_warehouse="WH-1")
		sre_mock = mock.MagicMock()
		sre_mock.reservation_based_on = None

		fake_self = SimpleNamespace(
			items=[row],
			doc=frappe._dict(
				doctype="Work Order",
				name="WO-1",
				skip_transfer=0,
				from_wip_warehouse=0,
				company="Test Co",
				project=None,
			),
			table_name="required_items",
			qty_field="required_qty",
			warehouse_field="source_warehouse",
			warehouse=None,
			_has_active_reservation=lambda *a, **k: False,
			get_available_qty_to_reserve=lambda item_code, warehouse: 10,
			throw_stock_not_exists_error=lambda *a, **k: None,
		)

		with (
			mock.patch.object(frappe, "new_doc", return_value=sre_mock),
			mock.patch.object(
				frappe,
				"get_cached_value",
				return_value=frappe._dict(has_serial_no=0, has_batch_no=0, stock_uom="Nos"),
			),
		):
			created = self.StockReservation.make_stock_reservation_entries(fake_self)

		self.assertTrue(created)
		sre_mock.save.assert_called_once()
		sre_mock.submit.assert_called_once()
		self.assertEqual(sre_mock.voucher_detail_no, "row-1")
		self.assertEqual(sre_mock.voucher_no, "WO-1")
		self.assertEqual(sre_mock.warehouse, "WH-1")

	def test_second_call_for_same_row_does_not_duplicate(self):
		"""End-to-end shape of the original bug: call the method twice in a
		row for the same Work Order item. The first call must create a
		reservation; the second call, seeing that reservation is now active,
		must not create a second one."""
		row = frappe._dict(name="row-1", item_code="ITEM-1", required_qty=10, source_warehouse="WH-1")
		sre_mock = mock.MagicMock()
		sre_mock.reservation_based_on = None

		fake_self = SimpleNamespace(
			items=[row],
			doc=frappe._dict(
				doctype="Work Order",
				name="WO-1",
				skip_transfer=0,
				from_wip_warehouse=0,
				company="Test Co",
				project=None,
			),
			table_name="required_items",
			qty_field="required_qty",
			warehouse_field="source_warehouse",
			warehouse=None,
			get_available_qty_to_reserve=lambda item_code, warehouse: 10,
			throw_stock_not_exists_error=lambda *a, **k: None,
		)
		fake_self._has_active_reservation = lambda *a, **k: self.StockReservation._has_active_reservation(
			fake_self, *a, **k
		)

		with (
			mock.patch.object(frappe, "new_doc", return_value=sre_mock),
			mock.patch.object(
				frappe,
				"get_cached_value",
				return_value=frappe._dict(has_serial_no=0, has_batch_no=0, stock_uom="Nos"),
			),
			mock.patch.object(frappe.db, "exists") as mock_exists,
		):
			mock_exists.return_value = False
			first_call_created = self.StockReservation.make_stock_reservation_entries(fake_self)

			mock_exists.return_value = True  # the reservation from the first call is now active
			second_call_created = self.StockReservation.make_stock_reservation_entries(fake_self)

		self.assertTrue(first_call_created)
		self.assertFalse(second_call_created)
		sre_mock.save.assert_called_once()
		sre_mock.submit.assert_called_once()


if __name__ == "__main__":
	unittest.main()
