import frappe
from frappe import _

@frappe.whitelist()
def create_appointment(queue_name, customer_email, customer_name):
	"""
	Create an appointment in the given queue with concurrency control
	"""
	# 使用 FOR UPDATE 获取行级排他锁，防止高并发下超卖
	queue = frappe.db.sql("""
		SELECT name, capacity, booked_count, status
		FROM `tabAppointment Queue`
		WHERE name = %s FOR UPDATE
	""", (queue_name,), as_dict=True)
	
	if not queue:
		frappe.throw(_("Appointment Queue not found"), exc=frappe.DoesNotExistError)
	
	queue = queue[0]
	
	if queue.status != "Open":
		frappe.throw(_("Appointment Queue is currently closed"))
	
	if queue.booked_count >= queue.capacity:
		frappe.throw(_("Appointment Queue is fully booked! Cannot overbook."))
	
	# 创建排队记录
	entry = frappe.get_doc({
		"doctype": "Appointment Queue Entry",
		"appointment_queue": queue.name,
		"customer_email": customer_email,
		"customer_name": customer_name,
		"status": "Queued"
	})
	entry.insert(ignore_permissions=True)
	
	# 更新当前预约数
	frappe.db.set_value("Appointment Queue", queue.name, "booked_count", queue.booked_count + 1)
	
	# Frappe 框架会在请求成功结束时自动 COMMIT 释放锁
	return entry.name

@frappe.whitelist()
def get_appointment_status(entry_name):
	"""
	View appointment status and current queue position
	"""
	if not frappe.db.exists("Appointment Queue Entry", entry_name):
		frappe.throw(_("Appointment not found"), exc=frappe.DoesNotExistError)
	
	entry = frappe.get_doc("Appointment Queue Entry", entry_name)
	entry.check_permission("read")
	
	position = None
	if entry.status == "Queued":
		# 计算在自己之前创建且仍在排队中的记录数
		position = frappe.db.count("Appointment Queue Entry", filters={
			"appointment_queue": entry.appointment_queue,
			"status": "Queued",
			"creation": ("<", entry.creation)
		}) + 1
	
	return {
		"name": entry.name,
		"queue": entry.appointment_queue,
		"status": entry.status,
		"queue_position": position
	}

@frappe.whitelist()
def cancel_appointment(entry_name):
	"""
	Cancel an appointment and release the booked slot
	"""
	entry = frappe.get_doc("Appointment Queue Entry", entry_name)
	entry.check_permission("write")
	
	if entry.status in ["Cancelled", "Completed"]:
		frappe.throw(_("Cannot cancel an appointment that is already {0}").format(entry.status))
	
	# 获取行锁，防止并发取消/创建导致计数错误
	queue = frappe.db.sql("""
		SELECT name, booked_count
		FROM `tabAppointment Queue`
		WHERE name = %s FOR UPDATE
	""", (entry.appointment_queue,), as_dict=True)
	
	if not queue:
		frappe.throw(_("Appointment Queue not found"))
	
	queue = queue[0]
	
	# 更新排队记录状态
	entry.db_set("status", "Cancelled")
	
	# 释放名额
	if queue.booked_count > 0:
		frappe.db.set_value("Appointment Queue", queue.name, "booked_count", queue.booked_count - 1)
		
	return entry.status
