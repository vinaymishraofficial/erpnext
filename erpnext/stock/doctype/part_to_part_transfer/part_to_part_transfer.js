// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Part to Part Transfer", {
	setup(frm) {
		// Server-side validate_items() enforces Serial No/Batch No matching between
		// Source and Target - this query only narrows to stock items, so it doesn't
		// need to duplicate that OR logic (or_filters isn't reliably supported by
		// every link-query backend the way it is for frappe.get_list).
		const transferable_item_query = () => ({
			filters: { is_stock_item: 1, disabled: 0 },
		});
		frm.set_query("source_item", transferable_item_query);
		frm.set_query("target_item", transferable_item_query);

		frm.set_query("warehouse", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
		frm.set_query("target_warehouse", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));

		frm.set_query("serial_no", "serial_nos", () => ({
			filters: { item_code: frm.doc.source_item, warehouse: frm.doc.warehouse, status: "Active" },
		}));
		frm.set_query("batch_no", "batches", () => ({
			filters: { item: frm.doc.source_item },
		}));
	},

	refresh(frm) {
		if (frm.doc.stock_entry) {
			frm.add_custom_button(__("Stock Entry"), () =>
				frappe.set_route("Form", "Stock Entry", frm.doc.stock_entry)
			);
		}
	},

	source_item(frm) {
		frm.clear_table("serial_nos");
		frm.clear_table("batches");
		frm.refresh_field("serial_nos");
		frm.refresh_field("batches");
	},

	warehouse(frm) {
		frm.clear_table("serial_nos");
		frm.clear_table("batches");
		frm.refresh_field("serial_nos");
		frm.refresh_field("batches");
	},
});
