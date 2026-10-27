import frappe
from frappe import _
from frappe.model.document import Document

from draft_notifications.draft_notifications.draft_notification import validate_custom_method


class DraftNotificationRule(Document):
	def validate(self):
		if self.recipient_type == "Custom Method":
			validate_custom_method(self.custom_method)

		from draft_notifications.draft_notifications.draft_notification import channel_uses_dingtalk, channel_uses_group

		if channel_uses_dingtalk(self.notification_channel):
			if not self.dingtalk_config:
				frappe.throw(_("DingTalk Robot Config is required for DingTalk notification channels."))
			kind = frappe.db.get_value("DingTalk Robot Config", self.dingtalk_config, "robot_type") or "Private Chat"
			if (kind == "Group Webhook") != channel_uses_group(self.notification_channel):
				frappe.throw(_("The DingTalk channel and robot type must match."))
