frappe.ui.form.on("DingTalk Robot Config", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		const is_group = frm.doc.robot_type === "Group Webhook";
		frm.add_custom_button(__(is_group ? "Validate Webhook" : "Test Connection"), () => {
			if (!ensure_saved_robot_config(frm)) return;
			frappe.call({
				method: "draft_notifications.dingtalk_robot.test_dingtalk_config",
				args: { config_name: frm.doc.name },
				freeze: true,
				freeze_message: __(is_group ? "Validating Webhook..." : "Testing connection..."),
				callback(response) {
					const result = response.message || {};
					frappe.show_alert({
						message: result.message || __("No response"),
						indicator: result.success ? "green" : "red",
					});
				},
			});
		});

		if (is_group) {
			frm.add_custom_button(__("Send Test Group Message"), () => {
				if (!ensure_saved_robot_config(frm)) return;
				frappe.prompt([
					{ fieldname: "user", fieldtype: "Link", options: "User", label: __("ERP User"), reqd: 1 },
					{ fieldname: "message", fieldtype: "Small Text", label: __("Message") },
				], (values) => {
					frappe.call({
						method: "draft_notifications.dingtalk_group.send_test_group",
						args: { config_name: frm.doc.name, ...values },
						freeze: true,
						callback() { frappe.show_alert({ message: __("Sent."), indicator: "green" }); },
					});
				}, __("Send Test Group Message"), __("Send"));
			});
			return;
		}

		frm.add_custom_button(__("Send Test Private Chat"), () => {
			if (!ensure_saved_robot_config(frm)) return;
			frappe.prompt(
				[
					{
						fieldname: "user_id",
						fieldtype: "Data",
						label: __("DingTalk UserId"),
						reqd: 1,
						default: frm.doc.recipients?.[0]?.user_id || "",
					},
					{
						fieldname: "message",
						fieldtype: "Small Text",
						label: __("Message"),
					},
				],
				(values) => {
					frappe.call({
						method: "draft_notifications.dingtalk_robot.send_test_private_chat",
						args: {
							config_name: frm.doc.name,
							user_id: values.user_id,
							message: values.message,
						},
						freeze: true,
						freeze_message: __("Sending test private chat..."),
						callback(response) {
							frappe.show_alert({
								message: __("Sent."),
								indicator: "green",
							});
						},
					});
				},
				__("Send Test Private Chat"),
				__("Send")
			);
		});
	},
});

function ensure_saved_robot_config(frm) {
	if (frm.is_dirty()) {
		frappe.msgprint(__("Save the robot configuration before testing."));
		return false;
	}
	return true;
}
