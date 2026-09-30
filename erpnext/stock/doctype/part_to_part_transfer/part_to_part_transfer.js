// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Part to Part Transfer", {
	setup(frm) {
		const serial_item_query = () => ({
			filters: { has_serial_no: 1, has_batch_no: 0, is_stock_item: 1, disabled: 0 },
		});
		frm.set_query("source_item", serial_item_query);
		frm.set_query("target_item", serial_item_query);

		frm.set_query("warehouse", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));

		frm.set_query("serial_no", "serial_nos", () => ({
			filters: { item_code: frm.doc.source_item, warehouse: frm.doc.warehouse, status: "Active" },
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
		frm.refresh_field("serial_nos");
	},

	warehouse(frm) {
		frm.clear_table("serial_nos");
		frm.refresh_field("serial_nos");
	},
});
