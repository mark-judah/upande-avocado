"""Mint Box stickers for a Harvest Batch and attach the PDF.

Lives in the codebase (not a Server Script) so we get full Python access:
proper imports, frappe.utils.pdf.get_pdf, no sandbox restrictions.
"""

import math

import frappe
from frappe.utils import now_datetime
from frappe.utils.pdf import get_pdf


SETTINGS_NAME = "Sticker Calculation Settings"
DEFAULT_AVG_KG = 18.0
DEFAULT_REJECT_PCT = 20.0


@frappe.whitelist()
def mint_batch_box_stickers(batch_id: str) -> dict:
	"""Compute box counts from Sticker Calculation Settings, mint Avocado
	Stickers (which fires the existing 'Before Submit' hook to create real
	Box records), render a multi-page PDF of QR labels, and attach it to the
	Harvest Batch.

	Returns a summary dict with `file_url`, `breakdown`, `total_stickers`.
	"""
	batch_id = (batch_id or "").strip()
	if not batch_id:
		frappe.throw(frappe._("batch_id is required"))
	if not frappe.db.exists("Harvest Batch", batch_id):
		frappe.throw(frappe._("Harvest Batch '{0}' not found").format(batch_id))

	batch = frappe.get_doc("Harvest Batch", batch_id)
	crate_count = frappe.db.count("Crate Allocation", {"batch_id_ref": batch_id})

	avg_kg, reject_pct, box_sizes = _load_settings()
	if not box_sizes:
		frappe.throw(frappe._("No box sizes configured in Sticker Calculation Settings"))

	actual_kg = float(batch.get("total_incoming_kg") or 0)
	if actual_kg > 0:
		incoming_kg = actual_kg
		source = "actual"
	else:
		incoming_kg = float(crate_count) * avg_kg
		source = "estimated"
	export_kg = incoming_kg * (100.0 - reject_pct) / 100.0

	all_entries, breakdown = _mint_per_size(batch_id, export_kg, box_sizes)
	if not all_entries:
		frappe.throw(frappe._(
			"Calculation produced 0 stickers — "
			"crates={0}, incoming_kg={1:.2f} ({2}), export_kg={3:.2f}. "
			"Either weigh the batch (set total_incoming_kg) or scan crates first."
		).format(crate_count, incoming_kg, source, export_kg))

	html = _render_html(batch_id, all_entries)
	pdf_bytes = get_pdf(html)

	file_url, file_name = _attach_pdf(batch_id, pdf_bytes)

	return {
		"ok": True,
		"file_url": file_url,
		"file_name": file_name,
		"batch_id": batch_id,
		"incoming_kg": round(incoming_kg, 2),
		"export_kg": round(export_kg, 2),
		"reject_pct": reject_pct,
		"source": source,
		"crate_count": crate_count,
		"total_stickers": len(all_entries),
		"breakdown": breakdown,
	}


def _load_settings():
	avg_kg = DEFAULT_AVG_KG
	reject_pct = DEFAULT_REJECT_PCT
	sizes = []
	if frappe.db.exists("Sticker Calculation Settings", SETTINGS_NAME):
		sd = frappe.get_doc("Sticker Calculation Settings", SETTINGS_NAME)
		avg_kg = float(sd.get("avg_crate_weight_kg") or DEFAULT_AVG_KG)
		reject_pct = float(sd.get("reject_pct") or DEFAULT_REJECT_PCT)
		for r in sd.get("box_sizes") or []:
			bw = float(r.get("box_weight_kg") or 0)
			ap = float(r.get("allocation_pct") or 0)
			if bw > 0 and ap > 0:
				sizes.append({
					"label": r.get("label") or f"{int(bw)}kg Box",
					"box_weight_kg": bw,
					"allocation_pct": ap,
				})
	return avg_kg, reject_pct, sizes


def _mint_per_size(batch_id, export_kg, box_sizes):
	entries = []
	breakdown = []
	for bs in box_sizes:
		bw = bs["box_weight_kg"]
		box_kg = export_kg * bs["allocation_pct"] / 100.0
		count = math.ceil(box_kg / bw)
		if count <= 0:
			continue

		sticker_doc = frappe.new_doc("Avocado Stickers")
		sticker_doc.sticker_type = "Box"
		sticker_doc.box_weight_kg = bw
		sticker_doc.count = count
		sticker_doc.notes = f"Auto for Harvest Batch {batch_id}"
		sticker_doc.insert(ignore_permissions=True)
		sticker_doc.submit()

		# Pull the IDs the Before-Submit hook generated. Try the in-memory doc
		# first; fall back to the DB row; last resort, query the Box records
		# the hook just created.
		ids = _extract_created_ids(sticker_doc, bw)
		for box_id in ids:
			entries.append({
				"box_id": box_id,
				"box_weight_kg": bw,
				"label": bs["label"],
			})
		breakdown.append({
			"label": bs["label"],
			"box_weight_kg": bw,
			"count": count,
			"first_id": ids[0] if ids else "",
			"last_id": ids[-1] if ids else "",
			"sticker_doc": sticker_doc.name,
		})
	return entries, breakdown


def _render_html(batch_id, entries):
	css = (
		"@page { margin: 0; size: 100mm 40mm; }"
		"body { margin: 0; padding: 0; font-family: Helvetica, sans-serif; }"
		".bx { display: flex; align-items: center; width: 100mm; height: 40mm;"
		"      border: 0.3mm dashed #999; padding: 2mm; box-sizing: border-box;"
		"      page-break-after: always; page-break-inside: avoid; }"
		".bx:last-child { page-break-after: auto; }"
		".q { width: 34mm; height: 34mm; flex: 0 0 34mm; }"
		".q img { width: 34mm; height: 34mm; object-fit: contain; }"
		".t { flex: 1; padding-left: 5mm; }"
		".id { font-size: 14pt; font-weight: 700; letter-spacing: 0.5px; color: #000; }"
		".m { font-size: 9pt; color: #555; margin-top: 2mm; }"
		".b { font-size: 8pt; color: #888; margin-top: 1mm; }"
	)
	blocks = []
	for e in entries:
		qr = (
			"https://api.qrserver.com/v1/create-qr-code/?size=240x240&margin=0&data="
			+ e["box_id"]
		)
		bw_str = f"{e['box_weight_kg']:g}"
		blocks.append(
			f'<div class="bx">'
			f'<div class="q"><img src="{qr}" /></div>'
			f'<div class="t">'
			f'<div class="id">{e["box_id"]}</div>'
			f'<div class="m">{e["label"]} &middot; {bw_str} kg</div>'
			f'<div class="b">Batch {batch_id}</div>'
			f'</div>'
			f'</div>'
		)
	return f"<html><head><style>{css}</style></head><body>{''.join(blocks)}</body></html>"


def _extract_created_ids(sticker_doc, box_weight_kg):
	"""Return the Box IDs minted by the Before-Submit hook on this Avocado
	Stickers doc. Tries three sources in order:

	1. `sticker_doc.created_ids` in memory (works if hook writes propagated).
	2. DB read of the same field (works if hook persisted the value).
	3. Query Box records minted on/after `sticker_doc.creation` for this
	   box_weight_kg, ordered by serial_no — the hook always inserts these.
	"""
	# 1) in-memory
	created_str = sticker_doc.get("created_ids") or ""
	ids = [s.strip() for s in created_str.splitlines() if s.strip()]
	if ids:
		return ids

	# 2) DB
	try:
		sticker_doc.reload()
		created_str = sticker_doc.get("created_ids") or ""
		ids = [s.strip() for s in created_str.splitlines() if s.strip()]
		if ids:
			return ids
	except Exception:
		pass

	# 3) Direct query on the Box doctype
	created_at = sticker_doc.get("creation") or now_datetime()
	rows = frappe.db.sql(
		"""SELECT box_id FROM `tabBox`
		   WHERE box_weight_kg = %s AND creation >= %s
		   ORDER BY serial_no""",
		(box_weight_kg, created_at),
		as_dict=True,
	)
	return [r["box_id"] for r in rows if r.get("box_id")]


def _attach_pdf(batch_id, pdf_bytes):
	ts = now_datetime().strftime("%Y%m%d-%H%M%S")
	file_name = f"batch_stickers_{batch_id}_{ts}.pdf"
	file_doc = frappe.get_doc({
		"doctype": "File",
		"file_name": file_name,
		"attached_to_doctype": "Harvest Batch",
		"attached_to_name": batch_id,
		"is_private": 1,
		"content": pdf_bytes,
	})
	file_doc.save(ignore_permissions=True)
	return file_doc.file_url, file_name
