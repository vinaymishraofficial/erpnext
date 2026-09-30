# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Move serial numbers, or a batch's quantity, from one item code to another.

Works like SAP's material-to-material transfer posting (movement type 309): a Repack
Stock Entry takes the source item out and brings the target item back in, carrying
value across. For serials, the exact same serial numbers come back in under the
target item (set_item_on_serial_nos()) - a serial is one physical unit, so "the same
serial" and "the same unit" are the same thing. A Batch has no such 1:1 identity - its
quantity is shared and partial, so repointing an existing Batch's item the way we
repoint a Serial No's item_code would silently misattribute that batch's other
quantity and other warehouses/transactions. Batches are instead moved the same way any
ordinary Repack already handles them: the old batch goes out under the source item as
normal, and a brand-new Batch is minted under the target item for just the converted
quantity - which is why the target item must have "Automatically Create New Batch"
enabled (validated below), and why, unlike the serial path, there is no item-identity
flip to make or undo on submit/cancel.
"""

import frappe
from frappe import _, bold
from frappe.model.document import Document
from frappe.utils import flt, get_link_to_form, nowdate, nowtime

from erpnext.stock.doctype.batch.batch import get_batch_qty


class ParttoPartTransfer(Document):
	def insert(self, *args, **kwargs):
		if self.amended_from:
			# `stock_entry` is no_copy, but Frappe's amend flow copies no_copy
			# fields too (unlike Duplicate), so the fresh draft would otherwise
			# still point at the Stock Entry the cancelled original already
			# cancelled - and Document.insert() rejects any link to a cancelled
			# document before before_insert() ever runs, so this has to be
			# cleared here rather than in a hook. on_submit() sets it again once
			# this amendment is itself submitted.
			self.stock_entry = None

		return super().insert(*args, **kwargs)

	def validate(self):
		self.set_posting_datetime()
		self.validate_items()
		self.validate_warehouse()
		if self.is_batch_transfer():
			self.validate_batches()
			self.qty = flt(sum(row.qty for row in self.batches))
		else:
			self.validate_serial_nos()
			self.qty = len(self.serial_nos)

	def is_batch_transfer(self):
		return bool(frappe.get_cached_value("Item", self.source_item, "has_batch_no"))

	def get_target_warehouse(self):
		return self.target_warehouse or self.warehouse

	def on_submit(self):
		try:
			self.make_stock_entry()
			if not self.is_batch_transfer():
				set_item_on_serial_nos(self.get_serial_nos(), self.target_item)
		except Exception:
			# Nothing in on_submit persisted a usable Stock Entry (or it did and the
			# serial flip after it failed); either way the parent must not be left
			# looking submitted with no Repack behind it. Frappe already wrote
			# docstatus=1 to the DB before calling on_submit, so an exception here
			# would otherwise leave a permanently stuck, uncancellable document.
			self.db_set("docstatus", 0, update_modified=False)
			raise

	def before_cancel(self):
		if not self.is_batch_transfer():
			self.validate_serial_nos_not_moved()

	def on_cancel(self):
		try:
			if not self.is_batch_transfer():
				# Re-checked here (not just in before_cancel) because callers that
				# cancel with flags.ignore_validate=True skip before_cancel entirely -
				# on_cancel still runs, and it is the only remaining place that can
				# stop a serial that has already moved on again from having its
				# item_code silently reverted out from under that later transaction.
				# Batches have no equivalent identity flip to protect: the target
				# item's batch is a brand-new document, so cancelling the Stock
				# Entry alone fully reverses a batch transfer.
				self.validate_serial_nos_not_moved()

			stock_entry = frappe.get_doc("Stock Entry", self.stock_entry)
			if stock_entry.docstatus == 1:
				stock_entry.cancel()

			if not self.is_batch_transfer():
				set_item_on_serial_nos(self.get_serial_nos(), self.source_item)
		except Exception:
			# Frappe already wrote docstatus=2 to the DB before calling on_cancel.
			# If the safety check (or the Stock Entry's own cancel) fails partway
			# through, nothing was actually reverted - the linked Stock Entry is
			# still submitted and the serial is still on the target item - so the
			# parent must not be left looking cancelled either.
			self.db_set("docstatus", 1, update_modified=False)
			raise

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
			if item.disabled or not item.is_stock_item:
				frappe.throw(_("Item {0} must be an enabled stock item").format(bold(item.name)))
			if not item.has_serial_no and not item.has_batch_no:
				frappe.throw(_("Item {0} must have Serial No or Batch No").format(bold(item.name)))

		if bool(source.has_serial_no) != bool(target.has_serial_no) or bool(source.has_batch_no) != bool(
			target.has_batch_no
		):
			frappe.throw(
				_("Source Item {0} and Target Item {1} must both use Serial No or both use Batch No").format(
					bold(source.name), bold(target.name)
				)
			)

		if source.has_batch_no and not target.create_new_batch:
			frappe.throw(
				_("Target Item {0} must have {1} enabled to receive a converted batch quantity").format(
					bold(target.name), bold(_("Automatically Create New Batch"))
				)
			)

		if source.stock_uom != target.stock_uom:
			frappe.throw(
				_("Source Item UOM {0} and Target Item UOM {1} must be the same").format(
					bold(source.stock_uom), bold(target.stock_uom)
				)
			)

	def validate_warehouse(self):
		self.validate_one_warehouse(self.warehouse, "Warehouse")
		if self.target_warehouse:
			self.validate_one_warehouse(self.target_warehouse, "Target Warehouse")

	def validate_one_warehouse(self, warehouse, label):
		company, is_group = frappe.db.get_value("Warehouse", warehouse, ["company", "is_group"])
		if company != self.company:
			frappe.throw(
				_("{0} {1} does not belong to Company {2}").format(
					_(label), bold(warehouse), bold(self.company)
				)
			)

		if is_group:
			frappe.throw(_("{0} {1} is a group warehouse").format(_(label), bold(warehouse)))

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

		self.validate_serial_not_reserved(row)

	def validate_serial_not_reserved(self, row):
		"""A serial can be Active/in-stock and still be off-limits - reserved for a Work Order
		via a Stock Reservation Entry. Checked explicitly so this surfaces as a clear validation
		error on this row, rather than as a confusing failure deep inside the Repack Stock
		Entry's own submit (or worse, a converted item silently pulled out from under a Work
		Order that still thinks it has this exact serial reserved).
		"""
		from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
			get_serial_no_reservation,
		)

		reservation = get_serial_no_reservation(self.source_item, row.serial_no, self.warehouse)
		if reservation:
			frappe.throw(
				_(
					"Row #{0}: Serial No {1} is reserved for {2} {3} via {4}. Use an unreserved "
					"serial number or cancel the reservation."
				).format(
					row.idx,
					bold(row.serial_no),
					reservation.voucher_type,
					bold(reservation.voucher_no),
					get_link_to_form("Stock Reservation Entry", reservation.name),
				)
			)

	def validate_batches(self):
		batch_nos = [row.batch_no for row in self.batches]
		duplicates = {batch_no for batch_no in batch_nos if batch_nos.count(batch_no) > 1}
		if duplicates:
			frappe.throw(_("Batch No {0} is entered more than once").format(bold(", ".join(duplicates))))

		details = {
			row.name: row
			for row in frappe.get_all(
				"Batch", filters={"name": ("in", batch_nos)}, fields=["name", "item", "disabled"]
			)
		}
		for row in self.batches:
			self.validate_batch(row, details.get(row.batch_no))

	def validate_batch(self, row, batch):
		if not batch:
			frappe.throw(_("Row #{0}: Batch No {1} does not exist").format(row.idx, bold(row.batch_no)))

		if batch.item != self.source_item:
			frappe.throw(
				_("Row #{0}: Batch No {1} belongs to Item {2}, not {3}").format(
					row.idx, bold(row.batch_no), bold(batch.item), bold(self.source_item)
				)
			)

		if batch.disabled:
			frappe.throw(_("Row #{0}: Batch No {1} is disabled").format(row.idx, bold(row.batch_no)))

		if not row.qty or row.qty <= 0:
			frappe.throw(_("Row #{0}: Qty must be greater than 0").format(row.idx))

		available_qty = flt(get_batch_qty(batch_no=row.batch_no, warehouse=self.warehouse))
		if row.qty > available_qty:
			frappe.throw(
				_("Row #{0}: Only {1} of Batch No {2} is available in Warehouse {3}").format(
					row.idx, available_qty, bold(row.batch_no), bold(self.warehouse)
				)
			)

	def make_stock_entry(self):
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
		target_warehouse = self.get_target_warehouse()

		if self.is_batch_transfer():
			for row in self.batches:
				stock_entry.append(
					"items",
					self.get_stock_entry_row(
						self.source_item, row.qty, s_warehouse=self.warehouse, batch_no=row.batch_no
					),
				)
			stock_entry.append(
				"items",
				self.get_stock_entry_row(
					self.target_item, self.qty, t_warehouse=target_warehouse, is_finished_item=1
				),
			)
		else:
			serial_nos = "\n".join(self.get_serial_nos())
			stock_entry.append(
				"items",
				self.get_stock_entry_row(
					self.source_item, self.qty, s_warehouse=self.warehouse, serial_no=serial_nos
				),
			)
			stock_entry.append(
				"items",
				self.get_stock_entry_row(
					self.target_item,
					self.qty,
					t_warehouse=target_warehouse,
					is_finished_item=1,
					serial_no=serial_nos,
				),
			)

		stock_entry.flags.ignore_permissions = True
		stock_entry.insert()
		try:
			stock_entry.submit()
		except Exception:
			# Don't leave an orphaned Repack behind if it fails to submit (e.g. a
			# backdated posting datetime clashing with a later transaction). This
			# must never itself raise: Frappe writes docstatus=1 to the DB before
			# running on_submit, so a failed submit() can leave the Stock Entry
			# looking "submitted" in the DB even though its in-memory object
			# already reflects that - a plain delete() would then refuse ("Submitted
			# Record cannot be deleted") and replace the real error with a
			# confusing one of our own making.
			self.cleanup_failed_stock_entry(stock_entry)
			raise
		self.db_set("stock_entry", stock_entry.name)

	def cleanup_failed_stock_entry(self, stock_entry):
		try:
			stock_entry.reload()
			if stock_entry.docstatus == 1:
				stock_entry.flags.ignore_permissions = True
				stock_entry.cancel()
			if stock_entry.docstatus == 0:
				stock_entry.delete(ignore_permissions=True)
		except Exception:
			frappe.log_error(
				title="Part to Part Transfer: could not clean up a Repack Stock Entry "
				"left behind by a failed submit"
			)

	def get_stock_entry_row(self, item_code, qty, **kwargs):
		stock_uom = frappe.get_cached_value("Item", item_code, "stock_uom")
		return {
			"item_code": item_code,
			"qty": qty,
			"uom": stock_uom,
			"stock_uom": stock_uom,
			"conversion_factor": 1,
			"use_serial_batch_fields": 1,
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
		[
			"name",
			"disabled",
			"is_stock_item",
			"has_serial_no",
			"has_batch_no",
			"create_new_batch",
			"stock_uom",
		],
		as_dict=True,
	)
