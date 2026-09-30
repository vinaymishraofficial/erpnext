# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.serial_batch_bundle import get_batches_from_bundle, get_serial_nos_from_bundle
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

	def test_multi_warehouse_transfer(self):
		"""Target Warehouse lets the converted item land somewhere other than Source Warehouse."""
		target_warehouse = make_warehouse("_Test PPT Target WH")
		transfer = frappe.get_doc(
			{
				"doctype": "Part to Part Transfer",
				"company": COMPANY,
				"warehouse": WAREHOUSE,
				"target_warehouse": target_warehouse,
				"source_item": self.source,
				"target_item": self.target,
				"serial_nos": [{"serial_no": self.serial_nos[0]}],
			}
		)
		transfer.insert()
		transfer.submit()

		details = frappe.db.get_value(
			"Serial No", self.serial_nos[0], ["item_code", "warehouse"], as_dict=True
		)
		self.assertEqual(details.item_code, self.target)
		self.assertEqual(details.warehouse, target_warehouse)
		self.assertEqual(get_balance(self.source), (2, 200))
		self.assertEqual(get_balance(self.target, target_warehouse), (1, 100))

		transfer.cancel()
		details = frappe.db.get_value(
			"Serial No", self.serial_nos[0], ["item_code", "warehouse"], as_dict=True
		)
		self.assertEqual(details.item_code, self.source)
		self.assertEqual(details.warehouse, WAREHOUSE)
		self.assertEqual(get_balance(self.source), (3, 300))
		self.assertEqual(get_balance(self.target, target_warehouse), (0, 0))

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


class TestPartToPartTransferBatch(ERPNextTestSuite):
	def setUp(self):
		self.source = make_batch_item("_Test PPT Batch Source Item", "PPT-BSRC-.#####")
		self.target = make_batch_item("_Test PPT Batch Target Item", "PPT-BTGT-.#####")
		receipt = make_stock_entry(item_code=self.source, target=WAREHOUSE, qty=100, basic_rate=10)
		self.batch_no = next(iter(get_batches_from_bundle(receipt.items[0].serial_and_batch_bundle)))

	def test_partial_batch_qty_converts_and_mints_a_new_batch(self):
		"""Only part of a batch moves; the old batch's own item and remaining qty are untouched."""
		transfer = frappe.get_doc(
			{
				"doctype": "Part to Part Transfer",
				"company": COMPANY,
				"warehouse": WAREHOUSE,
				"source_item": self.source,
				"target_item": self.target,
				"batches": [{"batch_no": self.batch_no, "qty": 30}],
			}
		)
		transfer.insert()
		transfer.submit()

		self.assertEqual(transfer.qty, 30)
		self.assertEqual(frappe.db.get_value("Batch", self.batch_no, "item"), self.source)
		self.assertEqual(get_balance(self.source), (70, 700))
		self.assertEqual(get_balance(self.target), (30, 300))

		new_batches = frappe.get_all("Batch", filters={"item": self.target}, pluck="name")
		self.assertTrue(new_batches)
		self.assertNotIn(self.batch_no, new_batches)

		transfer.cancel()
		self.assertEqual(get_balance(self.source), (100, 1000))
		self.assertEqual(get_balance(self.target), (0, 0))

	def test_target_item_must_allow_new_batch_creation(self):
		no_auto_batch_target = make_batch_item(
			"_Test PPT Batch Target No Auto", "PPT-BTGT2-.#####", create_new_batch=0
		)
		transfer = frappe.get_doc(
			{
				"doctype": "Part to Part Transfer",
				"company": COMPANY,
				"warehouse": WAREHOUSE,
				"source_item": self.source,
				"target_item": no_auto_batch_target,
				"batches": [{"batch_no": self.batch_no, "qty": 10}],
			}
		)
		self.assertRaises(frappe.ValidationError, transfer.insert)

	def test_batch_qty_exceeding_available_is_rejected(self):
		transfer = frappe.get_doc(
			{
				"doctype": "Part to Part Transfer",
				"company": COMPANY,
				"warehouse": WAREHOUSE,
				"source_item": self.source,
				"target_item": self.target,
				"batches": [{"batch_no": self.batch_no, "qty": 1000}],
			}
		)
		self.assertRaises(frappe.ValidationError, transfer.insert)


class TestPartToPartTransferGLEntries(ERPNextTestSuite):
	def test_gl_entries_for_perpetual_inventory(self):
		"""Repack's own GL logic carries value from the source account to the target account."""
		company = frappe.db.get_value("Warehouse", "Stores - TCP1", "company")
		self.assertTrue(frappe.db.get_value("Company", company, "enable_perpetual_inventory"))

		source_account = create_stock_account("_Test PPT GL Source Account", company)
		target_account = create_stock_account("_Test PPT GL Target Account", company)
		source_warehouse = make_warehouse("_Test PPT GL Source WH", company, account=source_account)
		target_warehouse = make_warehouse("_Test PPT GL Target WH", company, account=target_account)

		source_item = make_serial_item("_Test PPT GL Source Item", "PPT-GLSRC-.#####")
		target_item = make_serial_item("_Test PPT GL Target Item", "PPT-GLTGT-.#####")

		receipt = make_stock_entry(
			item_code=source_item, target=source_warehouse, qty=1, basic_rate=500, company=company
		)
		serial_no = get_serial_nos_from_bundle(receipt.items[0].serial_and_batch_bundle)[0]

		transfer = frappe.get_doc(
			{
				"doctype": "Part to Part Transfer",
				"company": company,
				"warehouse": source_warehouse,
				"target_warehouse": target_warehouse,
				"source_item": source_item,
				"target_item": target_item,
				"serial_nos": [{"serial_no": serial_no}],
			}
		)
		transfer.insert()
		transfer.submit()

		gl_entries = frappe.get_all(
			"GL Entry",
			filters={"voucher_type": "Stock Entry", "voucher_no": transfer.stock_entry, "is_cancelled": 0},
			fields=["account", "debit", "credit"],
		)
		by_account = {g.account: g for g in gl_entries}
		self.assertEqual(by_account[source_account].credit, 500)
		self.assertEqual(by_account[source_account].debit, 0)
		self.assertEqual(by_account[target_account].debit, 500)
		self.assertEqual(by_account[target_account].credit, 0)

		transfer.cancel()
		self.assertFalse(
			frappe.get_all(
				"GL Entry",
				filters={
					"voucher_type": "Stock Entry",
					"voucher_no": transfer.stock_entry,
					"is_cancelled": 0,
				},
			)
		)


def create_stock_account(account_name, company):
	name = f"{account_name} - {frappe.get_cached_value('Company', company, 'abbr')}"
	if frappe.db.exists("Account", name):
		return name
	parent = frappe.db.get_value(
		"Account", {"company": company, "account_type": "Stock", "is_group": 1}, "name"
	) or frappe.db.get_value("Account", {"company": company, "root_type": "Asset", "is_group": 1}, "name")
	return (
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": account_name,
				"company": company,
				"parent_account": parent,
				"account_type": "Stock",
			}
		)
		.insert()
		.name
	)


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


def get_balance(item_code, warehouse=WAREHOUSE):
	sle = get_previous_sle(
		{
			"item_code": item_code,
			"warehouse": warehouse,
			"posting_date": frappe.utils.add_days(frappe.utils.nowdate(), 1),
			"posting_time": "00:00:00",
		}
	)
	return (sle.get("qty_after_transaction", 0), sle.get("stock_value", 0)) if sle else (0, 0)


def make_warehouse(warehouse_name, company=COMPANY, account=None):
	name = f"{warehouse_name} - {frappe.get_cached_value('Company', company, 'abbr')}"
	if frappe.db.exists("Warehouse", name):
		return name
	return (
		frappe.get_doc(
			{"doctype": "Warehouse", "warehouse_name": warehouse_name, "company": company, "account": account}
		)
		.insert()
		.name
	)


def make_batch_item(item_code, series, create_new_batch=1, stock_uom="Nos"):
	return make_item(
		item_code,
		{
			"is_stock_item": 1,
			"has_batch_no": 1,
			"create_new_batch": create_new_batch,
			"batch_number_series": series,
			"stock_uom": stock_uom,
			"valuation_method": "FIFO",
		},
	).name
