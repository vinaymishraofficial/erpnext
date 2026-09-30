# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.serial_batch_bundle import get_serial_nos_from_bundle
from erpnext.stock.stock_ledger import get_previous_sle
from erpnext.tests.utils import ERPNextTestSuite

COMPANY = "_Test Company"
WAREHOUSE = "_Test Warehouse - _TC"


class TestPartToPartTransfer(ERPNextTestSuite):
	def setUp(self):
		self.source = make_serial_item("_Test PPT Source Item", "PPT-SRC-.#####")
		self.target = make_serial_item("_Test PPT Target Item", "PPT-TGT-.#####")
		receipt = make_stock_entry(item_code=self.source, target=WAREHOUSE, qty=3, basic_rate=100)
		self.serial_nos = get_serial_nos_from_bundle(receipt.items[0].serial_and_batch_bundle)

	def test_serial_nos_move_to_target_item(self):
		transfer = make_transfer(self.source, self.target, self.serial_nos[:2])

		for serial_no in self.serial_nos[:2]:
			details = frappe.db.get_value(
				"Serial No", serial_no, ["item_code", "warehouse", "status"], as_dict=1
			)
			self.assertEqual(details.item_code, self.target)
			self.assertEqual(details.warehouse, WAREHOUSE)
			self.assertEqual(details.status, "Active")

		self.assertEqual(get_balance(self.source), (1, 100))
		# the value moves with the serials
		self.assertEqual(get_balance(self.target), (2, 200))

		stock_entry = frappe.get_doc("Stock Entry", transfer.stock_entry)
		self.assertEqual(stock_entry.purpose, "Repack")
		self.assertEqual(stock_entry.docstatus, 1)

	def test_target_item_serials_can_be_issued(self):
		make_transfer(self.source, self.target, self.serial_nos[:1])

		issue = make_stock_entry(
			item_code=self.target,
			source=WAREHOUSE,
			qty=1,
			serial_no=self.serial_nos[0],
			use_serial_batch_fields=1,
		)

		self.assertEqual(issue.docstatus, 1)
		self.assertEqual(frappe.db.get_value("Serial No", self.serial_nos[0], "status"), "Consumed")

	def test_cancel_moves_serial_nos_back(self):
		transfer = make_transfer(self.source, self.target, self.serial_nos[:2])
		transfer.cancel()

		for serial_no in self.serial_nos[:2]:
			details = frappe.db.get_value(
				"Serial No", serial_no, ["item_code", "warehouse", "status"], as_dict=1
			)
			self.assertEqual(details.item_code, self.source)
			self.assertEqual(details.warehouse, WAREHOUSE)
			self.assertEqual(details.status, "Active")

		self.assertEqual(frappe.db.get_value("Stock Entry", transfer.stock_entry, "docstatus"), 2)
		self.assertEqual(get_balance(self.source), (3, 300))
		self.assertEqual(get_balance(self.target), (0, 0))

	def test_cancel_blocked_after_serial_moved(self):
		transfer = make_transfer(self.source, self.target, self.serial_nos[:1])
		make_stock_entry(
			item_code=self.target,
			source=WAREHOUSE,
			qty=1,
			serial_no=self.serial_nos[0],
			use_serial_batch_fields=1,
		)

		self.assertRaises(frappe.ValidationError, transfer.cancel)

	def test_serial_of_other_item_is_rejected(self):
		transfer = make_transfer(self.target, self.source, self.serial_nos[:1], submit=False)
		self.assertRaises(frappe.ValidationError, transfer.insert)

	def test_same_item_is_rejected(self):
		transfer = make_transfer(self.source, self.source, self.serial_nos[:1], submit=False)
		self.assertRaises(frappe.ValidationError, transfer.insert)

	def test_different_uom_is_rejected(self):
		other_uom = make_serial_item("_Test PPT Box Item", "PPT-BOX-.#####", stock_uom="Box")
		transfer = make_transfer(self.source, other_uom, self.serial_nos[:1], submit=False)
		self.assertRaises(frappe.ValidationError, transfer.insert)

	def test_serial_cannot_be_received_under_other_item_without_transfer(self):
		issue = make_stock_entry(
			item_code=self.source,
			source=WAREHOUSE,
			qty=1,
			serial_no=self.serial_nos[0],
			use_serial_batch_fields=1,
		)
		self.assertEqual(issue.docstatus, 1)

		receipt = make_stock_entry(
			item_code=self.target,
			target=WAREHOUSE,
			qty=1,
			serial_no=self.serial_nos[0],
			use_serial_batch_fields=1,
			do_not_submit=True,
			do_not_save=True,
		)
		self.assertRaises(frappe.ValidationError, receipt.insert)


def make_transfer(source, target, serial_nos, submit=True):
	transfer = frappe.get_doc(
		{
			"doctype": "Part to Part Transfer",
			"company": COMPANY,
			"warehouse": WAREHOUSE,
			"source_item": source,
			"target_item": target,
			"serial_nos": [{"serial_no": serial_no} for serial_no in serial_nos],
		}
	)
	if submit:
		transfer.insert()
		transfer.submit()

	return transfer


def make_serial_item(item_code, series, stock_uom="Nos"):
	return make_item(
		item_code,
		{
			"is_stock_item": 1,
			"has_serial_no": 1,
			"serial_no_series": series,
			"stock_uom": stock_uom,
			"valuation_method": "FIFO",
		},
	).name


def get_balance(item_code):
	sle = get_previous_sle(
		{
			"item_code": item_code,
			"warehouse": WAREHOUSE,
			"posting_date": frappe.utils.add_days(frappe.utils.nowdate(), 1),
			"posting_time": "00:00:00",
		}
	)
	return (sle.get("qty_after_transaction", 0), sle.get("stock_value", 0)) if sle else (0, 0)
