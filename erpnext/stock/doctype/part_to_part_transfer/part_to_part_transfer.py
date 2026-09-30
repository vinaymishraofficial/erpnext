# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Move serial numbers from one item code to another while keeping the serial numbers.

Works like SAP's material-to-material transfer posting (movement type 309): a Repack
Stock Entry takes the serials out under the source item and brings the same serials
back in under the target item, carrying their value across.
"""

import frappe
from frappe import _, bold
from frappe.model.document import Document
from frappe.utils import nowdate, nowtime


class ParttoPartTransfer(Document):
	def validate(self):
		self.set_posting_datetime()
		self.validate_items()
		self.validate_warehouse()
		self.validate_serial_nos()
		self.qty = len(self.serial_nos)

	def on_submit(self):
		self.make_stock_entry()
		set_item_on_serial_nos(self.get_serial_nos(), self.target_item)

	def before_cancel(self):
		self.validate_serial_nos_not_moved()

	def on_cancel(self):
		stock_entry = frappe.get_doc("Stock Entry", self.stock_entry)
		if stock_entry.docstatus == 1:
			stock_entry.cancel()

		set_item_on_serial_nos(self.get_serial_nos(), self.source_item)

	def get_serial_nos(self):
		return [row.serial_no for row in self.serial_nos]

	def set_posting_datetime(self):
		if not self.set_posting_time:
			self.posting_date = nowdate()
			self.posting_time = nowtime()

	def validate_items(self):
		if self.source_item == self.target_item:
			frappe.throw(_("Source Item and Target Item cannot be the same"))

		source = get_item_details(self.source_item)
		target = get_item_details(self.target_item)
		for item in (source, target):
			if item.disabled or not item.is_stock_item or not item.has_serial_no:
				frappe.throw(
					_("Item {0} must be an enabled stock item with Serial No").format(bold(item.name))
				)

			# Batch-wise serials would also need the batch moved; not supported yet.
			if item.has_batch_no:
				frappe.throw(_("Item {0} has Batch No, which is not supported").format(bold(item.name)))

		if source.stock_uom != target.stock_uom:
			frappe.throw(
				_("Source Item UOM {0} and Target Item UOM {1} must be the same").format(
					bold(source.stock_uom), bold(target.stock_uom)
				)
			)

	def validate_warehouse(self):
		company, is_group = frappe.db.get_value("Warehouse", self.warehouse, ["company", "is_group"])
		if company != self.company:
			frappe.throw(
				_("Warehouse {0} does not belong to Company {1}").format(
					bold(self.warehouse), bold(self.company)
				)
			)

		if is_group:
			frappe.throw(_("Warehouse {0} is a group warehouse").format(bold(self.warehouse)))

	def validate_serial_nos(self):
		serial_nos = self.get_serial_nos()
		duplicates = {serial_no for serial_no in serial_nos if serial_nos.count(serial_no) > 1}
		if duplicates:
			frappe.throw(_("Serial No {0} is entered more than once").format(bold(", ".join(duplicates))))

		details = {
			row.name: row
			for row in frappe.get_all(
				"Serial No",
				filters={"name": ("in", serial_nos)},
				fields=["name", "item_code", "warehouse", "status"],
			)
		}
		for row in self.serial_nos:
			self.validate_serial_no(row, details.get(row.serial_no))

	def validate_serial_no(self, row, serial_no):
		if not serial_no:
			frappe.throw(_("Row #{0}: Serial No {1} does not exist").format(row.idx, bold(row.serial_no)))

		if serial_no.item_code != self.source_item:
			frappe.throw(
				_("Row #{0}: Serial No {1} belongs to Item {2}, not {3}").format(
					row.idx, bold(row.serial_no), bold(serial_no.item_code), bold(self.source_item)
				)
			)

		if serial_no.status != "Active" or serial_no.warehouse != self.warehouse:
			frappe.throw(
				_("Row #{0}: Serial No {1} is not available in Warehouse {2}").format(
					row.idx, bold(row.serial_no), bold(self.warehouse)
				)
			)

	def make_stock_entry(self):
		serial_nos = "\n".join(self.get_serial_nos())
		stock_entry = frappe.new_doc("Stock Entry")
		stock_entry.update(
			{
				"stock_entry_type": "Repack",
				"purpose": "Repack",
				"company": self.company,
				"set_posting_time": 1,
				"posting_date": self.posting_date,
				"posting_time": self.posting_time,
				"remarks": _("Part to Part Transfer {0}").format(self.name),
			}
		)
		stock_entry.append(
			"items", self.get_stock_entry_row(self.source_item, serial_nos, s_warehouse=self.warehouse)
		)
		stock_entry.append(
			"items",
			self.get_stock_entry_row(
				self.target_item, serial_nos, t_warehouse=self.warehouse, is_finished_item=1
			),
		)

		stock_entry.flags.ignore_permissions = True
		stock_entry.insert()
		stock_entry.submit()
		self.db_set("stock_entry", stock_entry.name)

	def get_stock_entry_row(self, item_code, serial_nos, **kwargs):
		stock_uom = frappe.get_cached_value("Item", item_code, "stock_uom")
		return {
			"item_code": item_code,
			"qty": self.qty,
			"uom": stock_uom,
			"stock_uom": stock_uom,
			"conversion_factor": 1,
			"use_serial_batch_fields": 1,
			"serial_no": serial_nos,
			**kwargs,
		}

	def validate_serial_nos_not_moved(self):
		"""Cancelling is only safe while the serials still sit where this transfer put them."""
		for serial_no in self.get_serial_nos():
			last_voucher = get_last_voucher_of_serial_no(serial_no)
			if last_voucher != self.stock_entry:
				frappe.throw(
					_("Serial No {0} has a later transaction {1}. Cancel that first.").format(
						bold(serial_no), bold(last_voucher)
					)
				)


def get_serial_nos_moving_to_item(item_code, voucher_no, serial_nos):
	"""Serial Nos a submitted Part to Part Transfer is moving to `item_code`.

	Used by Serial and Batch Bundle to let these serials be received under an item
	they do not belong to yet.
	"""
	transfer = frappe.qb.DocType("Part to Part Transfer")
	row = frappe.qb.DocType("Part to Part Transfer Serial")
	serial = frappe.qb.DocType("Serial No")

	query = (
		frappe.qb.from_(row)
		.join(transfer)
		.on(transfer.name == row.parent)
		.join(serial)
		.on(serial.name == row.serial_no)
		.select(row.serial_no)
		.where(
			(transfer.docstatus == 1)
			& (transfer.target_item == item_code)
			& (serial.item_code == transfer.source_item)
			& (row.serial_no.isin(serial_nos))
			# stock_entry is still empty while the transfer creates its own Stock Entry
			& (transfer.stock_entry.isnull() | (transfer.stock_entry == voucher_no))
		)
	)

	return set(query.run(pluck=True))


def set_item_on_serial_nos(serial_nos, item_code):
	item = frappe.get_cached_value(
		"Item", item_code, ["item_name", "item_group", "brand", "description"], as_dict=True
	)
	serial = frappe.qb.DocType("Serial No")
	(
		frappe.qb.update(serial)
		.set(serial.item_code, item_code)
		.set(serial.item_name, item.item_name)
		.set(serial.item_group, item.item_group)
		.set(serial.brand, item.brand)
		.set(serial.description, item.description)
		.where(serial.name.isin(serial_nos))
		.run()
	)


def get_last_voucher_of_serial_no(serial_no):
	bundle = frappe.qb.DocType("Serial and Batch Bundle")
	entry = frappe.qb.DocType("Serial and Batch Entry")
	result = (
		frappe.qb.from_(entry)
		.join(bundle)
		.on(bundle.name == entry.parent)
		.select(bundle.voucher_no)
		.where((entry.serial_no == serial_no) & (bundle.docstatus == 1) & (bundle.is_cancelled == 0))
		.orderby(bundle.posting_datetime, order=frappe.qb.desc)
		.orderby(bundle.creation, order=frappe.qb.desc)
		.limit(1)
		.run(pluck=True)
	)

	return result[0] if result else None


def get_item_details(item_code):
	return frappe.get_cached_value(
		"Item",
		item_code,
		["name", "disabled", "is_stock_item", "has_serial_no", "has_batch_no", "stock_uom"],
		as_dict=True,
	)
