# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.manufacturing.doctype.rework.rework import get_fg_current_composition
from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.serial_batch_bundle import get_serial_nos_from_bundle
from erpnext.tests.utils import ERPNextTestSuite

COMPANY = "_Test Company"
WAREHOUSE = "_Test Warehouse - _TC"


class TestRework(ERPNextTestSuite):
	def setUp(self):
		self.fg_item = make_serial_item("_Test Rework FG", "RWK-FG-.#####")
		self.comp_old = make_serial_item("_Test Rework Comp Old", "RWK-OLD-.#####")
		self.comp_new = make_serial_item("_Test Rework Comp New", "RWK-NEW-.#####")

		receipt = make_stock_entry(item_code=self.comp_old, target=WAREHOUSE, qty=1, basic_rate=100)
		self.comp_old_serial = get_serial_nos_from_bundle(receipt.items[0].serial_and_batch_bundle)[0]

		manu = frappe.new_doc("Stock Entry")
		manu.stock_entry_type = "Manufacture"
		manu.purpose = "Manufacture"
		manu.company = COMPANY
		manu.append(
			"items",
			{
				"item_code": self.comp_old,
				"qty": 1,
				"s_warehouse": WAREHOUSE,
				"serial_no": self.comp_old_serial,
				"use_serial_batch_fields": 1,
			},
		)
		manu.append(
			"items",
			{
				"item_code": self.fg_item,
				"qty": 1,
				"t_warehouse": WAREHOUSE,
				"is_finished_item": 1,
				"basic_rate": 100,
				"use_serial_batch_fields": 1,
			},
		)
		manu.insert()
		manu.submit()
		manu.reload()
		fg_row = next(d for d in manu.items if d.is_finished_item)
		self.fg_serial = get_serial_nos_from_bundle(fg_row.serial_and_batch_bundle)[0]

		new_receipt = make_stock_entry(item_code=self.comp_new, target=WAREHOUSE, qty=1, basic_rate=120)
		self.comp_new_serial = get_serial_nos_from_bundle(new_receipt.items[0].serial_and_batch_bundle)[0]

	def test_baseline_is_reliable_for_single_unit_manufacture(self):
		composition = get_fg_current_composition(self.fg_serial)
		self.assertTrue(composition["baseline_reliable"])
		item_codes = {c["item_code"] for c in composition["current_components"]}
		self.assertIn(self.comp_old, item_codes)

	def test_inhouse_swap_updates_composition_and_leaves_fg_untouched(self):
		make_inhouse_rework(
			self.fg_serial, self.comp_old, self.comp_old_serial, self.comp_new, self.comp_new_serial
		)

		fg = frappe.db.get_value(
			"Serial No", self.fg_serial, ["item_code", "warehouse", "status"], as_dict=True
		)
		self.assertEqual(fg.item_code, self.fg_item)
		self.assertEqual(fg.warehouse, WAREHOUSE)
		self.assertEqual(fg.status, "Active")

		old = frappe.db.get_value("Serial No", self.comp_old_serial, "status")
		new = frappe.db.get_value("Serial No", self.comp_new_serial, ["status", "warehouse"], as_dict=True)
		self.assertEqual(old, "Active")
		self.assertNotEqual(new.status, "Active")

		composition = get_fg_current_composition(self.fg_serial)
		item_codes = {c["item_code"] for c in composition["current_components"]}
		self.assertIn(self.comp_new, item_codes)
		self.assertNotIn(self.comp_old, item_codes)

	def test_cancel_reverses_inhouse_swap(self):
		rework = make_inhouse_rework(
			self.fg_serial, self.comp_old, self.comp_old_serial, self.comp_new, self.comp_new_serial
		)
		rework.cancel()

		new = frappe.db.get_value("Serial No", self.comp_new_serial, ["status", "warehouse"], as_dict=True)
		self.assertEqual(new.status, "Active")
		self.assertEqual(new.warehouse, WAREHOUSE)

		fg = frappe.db.get_value("Serial No", self.fg_serial, "status")
		self.assertEqual(fg, "Active")

	def test_removed_component_must_not_be_active_free_stock(self):
		rework = frappe.get_doc(
			{
				"doctype": "Rework",
				"rework_house": "In-House",
				"company": COMPANY,
				"fg_serial_no": self.fg_serial,
				"return_warehouse": WAREHOUSE,
				"components_removed": [
					{"item_code": self.comp_new, "serial_no": self.comp_new_serial, "qty": 1}
				],
			}
		)
		# comp_new_serial is Active/free stock - not something that could be "inside" this FG.
		self.assertRaises(frappe.ValidationError, rework.insert)

	def test_bought_out_round_trip_leaves_item_untouched(self):
		receipt = make_stock_entry(item_code=self.fg_item, target=WAREHOUSE, qty=1, basic_rate=500)
		fg_serial = get_serial_nos_from_bundle(receipt.items[0].serial_and_batch_bundle)[0]

		supplier = frappe.db.get_value(
			"Supplier", {"supplier_name": "_Test Supplier"}, "name"
		) or frappe.db.get_value("Supplier", {}, "name")
		supplier_warehouse = "_Test Warehouse 1 - _TC"
		if not frappe.db.exists("Warehouse", supplier_warehouse):
			frappe.get_doc(
				{"doctype": "Warehouse", "warehouse_name": "_Test Warehouse 1", "company": COMPANY}
			).insert()

		rework = frappe.get_doc(
			{
				"doctype": "Rework",
				"rework_house": "Bought Out",
				"company": COMPANY,
				"fg_serial_no": fg_serial,
				"supplier": supplier,
				"supplier_warehouse": supplier_warehouse,
			}
		)
		rework.insert()
		rework.submit()

		sent = frappe.db.get_value("Serial No", fg_serial, ["item_code", "warehouse"], as_dict=True)
		self.assertEqual(sent.item_code, self.fg_item)
		self.assertEqual(sent.warehouse, supplier_warehouse)

		rework.reload()
		rework.mark_received()
		rework.reload()

		received = frappe.db.get_value("Serial No", fg_serial, ["item_code", "warehouse"], as_dict=True)
		self.assertEqual(received.item_code, self.fg_item)
		self.assertEqual(received.warehouse, WAREHOUSE)

		rework.cancel()
		after_cancel = frappe.db.get_value("Serial No", fg_serial, ["item_code", "warehouse"], as_dict=True)
		self.assertEqual(after_cancel.item_code, self.fg_item)
		self.assertEqual(after_cancel.warehouse, WAREHOUSE)


def make_inhouse_rework(fg_serial, comp_old, comp_old_serial, comp_new, comp_new_serial, submit=True):
	rework = frappe.get_doc(
		{
			"doctype": "Rework",
			"rework_house": "In-House",
			"company": COMPANY,
			"fg_serial_no": fg_serial,
			"return_warehouse": WAREHOUSE,
			"issue_warehouse": WAREHOUSE,
			"components_removed": [{"item_code": comp_old, "serial_no": comp_old_serial, "qty": 1}],
			"components_added": [{"item_code": comp_new, "serial_no": comp_new_serial, "qty": 1}],
		}
	)
	if submit:
		rework.insert()
		rework.submit()
	return rework


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
