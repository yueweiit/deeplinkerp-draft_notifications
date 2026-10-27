import frappe
from frappe import _
from frappe.model.document import Document


class DingTalkRobotConfig(Document):
	def validate(self):
		if (self.get("robot_type") or "Private Chat") == "Group Webhook":
			from draft_notifications.dingtalk_group import validate_webhook_url

			validate_webhook_url(self.get_password("group_webhook"))
		else:
			if not all(self.get_password(field) if field == "app_secret" else self.get(field) for field in ("app_key", "app_secret", "robot_code")):
				frappe.throw(_("Private chat robots require AppKey, AppSecret and RobotCode."))
