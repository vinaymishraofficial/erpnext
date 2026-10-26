// Copyright (c) 2017, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("BOM Update Tool", {
	setup: function (frm) {
		frm.set_query("current_bom", function () {
			return {
				query: "erpnext.controllers.queries.bom",
				filters: { name: "!" + frm.doc.new_bom },
			};
		});

		frm.set_query("new_bom", function () {
			return {
				query: "erpnext.controllers.queries.bom",
				filters: { name: "!" + frm.doc.current_bom },
			};
		});
	},

	refresh: function (frm) {
		frm.disable_save();
		frm.events.disable_button(frm, "replace");

		frm.add_custom_button(__("View BOM Update Log"), () => {
			frappe.set_route("List", "BOM Update Log");
		});
	},

	disable_button: (frm, field, disable = true) => {
		frm.get_field(field).input.disabled = disable;
	},

	current_bom: (frm) => {
		if (frm.doc.current_bom && frm.doc.new_bom) {
			frm.events.disable_button(frm, "replace", false);
		}
	},

	new_bom: (frm) => {
		if (frm.doc.current_bom && frm.doc.new_bom) {
			frm.events.disable_button(frm, "replace", false);
		}
	},

	replace: (frm) => {
		if (frm.doc.current_bom && frm.doc.new_bom) {
			frappe.call({
				method: "erpnext.manufacturing.doctype.bom_update_tool.bom_update_tool.enqueue_replace_bom",
				freeze: true,
				args: {
					boms: {
						current_bom: frm.doc.current_bom,
						new_bom: frm.doc.new_bom,
					},
				},
				callback: (result) => {
					if (result && result.message && !result.exc) {
						frm.events.confirm_job_start(frm, result.message);
					}
				},
			});
		}
	},

	update_latest_price_in_all_boms: (frm) => {
		frappe.call({
			method: "erpnext.manufacturing.doctype.bom_update_tool.bom_update_tool.enqueue_update_cost",
			freeze: true,
			callback: (result) => {
				if (result && result.message && !result.exc) {
					frm.events.confirm_job_start(frm, result.message);
				}
			},
		});
	},

	link_sub_assembly_boms: (frm) => {
		frappe.call({
			method: "erpnext.manufacturing.doctype.bom_update_tool.bom_update_tool.get_unlinked_sub_assemblies",
			freeze: true,
			callback: ({ message }) => {
				if (!message) return;
				if (!message.rows.length) {
					frappe.msgprint({
						title: __("Link Sub-assembly BOMs"),
						message:
							frm.events.zero_cost_note(message) ||
							__("Every sub-assembly in active BOMs is already linked to its BOM."),
					});
					return;
				}
				frm.events.confirm_sub_assembly_links(frm, message);
			},
		});
	},

	// Rows whose sub-assembly BOM costs 0 wait: linking would turn their rate into 0.
	zero_cost_note: (preview) => {
		if (!preview.zero_cost.length) return "";
		const esc = frappe.utils.escape_html;
		const boms = [...new Set(preview.zero_cost.map((row) => row.sub_bom))]
			.map((name) => `<a href="${frappe.utils.get_form_link("BOM", name)}">${esc(name)}</a>`)
			.join(", ");
		return `<p class="text-warning">${__(
			"{0} rows wait: their sub-assembly BOM costs 0, so linking would make their rate 0. Give the components of these BOMs a rate, then link again:",
			[preview.zero_cost.length]
		)} ${boms}</p>`;
	},

	confirm_sub_assembly_links: (frm, preview) => {
		const esc = frappe.utils.escape_html;
		const bom_link = (name) => `<a href="${frappe.utils.get_form_link("BOM", name)}">${esc(name)}</a>`;
		const rows = preview.rows
			.map(
				(row) => `<tr>
					<td>${bom_link(row.parent)}</td>
					<td>${cint(row.idx)}</td>
					<td>${esc(row.item_code)}<div class="text-muted small">${esc(row.item_name || "")}</div></td>
					<td>${bom_link(row.sub_bom)}</td>
				</tr>`
			)
			.join("");
		const loops = preview.loops.length
			? `<p class="text-warning">${__("{0} rows skipped: linking them would put a BOM inside itself.", [
					preview.loops.length,
			  ])}</p>`
			: "";

		const dialog = new frappe.ui.Dialog({
			title: __("Link Sub-assembly BOMs"),
			size: "extra-large",
			fields: [{ fieldtype: "HTML", fieldname: "preview" }],
			primary_action_label: __("Link {0} rows", [preview.rows.length]),
			primary_action: () => {
				dialog.hide();
				frappe.call({
					method: "erpnext.manufacturing.doctype.bom_update_tool.bom_update_tool.enqueue_link_sub_assembly_boms",
					type: "POST",
					freeze: true,
					// The job reports progress under the same title, so it fills this dialog.
					callback: (r) =>
						!r.exc &&
						frappe.show_progress(__("Linking Sub-assembly BOMs"), 0, 100, __("Starting…"), true),
				});
			},
		});
		dialog.fields_dict.preview.$wrapper.html(`
			<p>${__(
				"{0} rows in {1} active BOMs use an item that now has its own default BOM. Their BOM No will be set, and the exploded items and cost of these BOMs and every BOM above them will be updated.",
				[preview.rows.length, preview.boms]
			)}</p>
			<p class="text-muted">${__(
				"New Work Orders with Use Multi-Level BOM will take the sub-assembly's raw materials. Existing Work Orders do not change."
			)}</p>
			${loops}
			${frm.events.zero_cost_note(preview)}
			<div style="max-height: 50vh; overflow: auto;">
				<table class="table table-bordered table-sm">
					<thead><tr>
						<th>${__("BOM")}</th><th>${__("Row")}</th><th>${__("Item")}</th><th>${__("Sub-assembly BOM")}</th>
					</tr></thead>
					<tbody>${rows}</tbody>
				</table>
			</div>`);
		dialog.show();
	},

	confirm_job_start: (frm, log_data) => {
		let log_link = frappe.utils.get_form_link("BOM Update Log", log_data.name, true);
		frappe.msgprint({
			message: __("BOM update is queued and may take a few minutes. Check {0} for progress.", [
				log_link,
			]),
			title: __("BOM Update Initiated"),
			indicator: "blue",
		});
	},
});
