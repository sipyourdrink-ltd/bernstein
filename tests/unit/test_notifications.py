import pytest
from unittest.mock import MagicMock, patch
from bernstein.core.notifications import NotificationManager, NotificationTarget, NotificationPayload
from bernstein.core.models import SmtpConfig

def test_email_notification_sent():
    smtp_config = SmtpConfig(
        host="smtp.example.com",
        port=587,
        username="user",
        password="pass",
        from_address="bernstein@example.com",
        to_addresses=["admin@example.com"]
    )
    
    target = NotificationTarget(type="email", url="", events=["task.completed"])
    manager = NotificationManager(targets=[target], smtp_config=smtp_config)
    
    payload = NotificationPayload(
        event="task.completed",
        title="Task Done",
        body="The task is finished.",
        metadata={"cost": "$0.05"}
    )
    
    with patch("smtplib.SMTP") as mock_smtp:
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_server
        
        manager.notify("task.completed", payload)
        
        # Verify SMTP was called
        mock_smtp.assert_called_with("smtp.example.com", 587)
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_with("user", "pass")
        mock_server.send_message.assert_called_once()
        
        msg = mock_server.send_message.call_args[0][0]
        assert msg["Subject"] == "Task Done"
        assert "The task is finished." in msg.get_payload()[0].get_payload()
