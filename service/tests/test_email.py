"""Tests for the login notification email sender."""

from __future__ import annotations

from unittest.mock import patch

from botocore.exceptions import ClientError

from utils.email import send_login_notification_email


class TestSendLoginNotificationEmail:
    """Tests for send_login_notification_email."""

    @patch("utils.email._ses_client")
    @patch("utils.email._STAGE", "prod")
    def test_sends_email_with_correct_parameters(self, mock_ses_client) -> None:
        """Verify SES send_email is called with the right sender, recipient, subject, and HTML body."""
        mock_ses_client.send_email.return_value = {"MessageId": "test-id"}

        send_login_notification_email(tenant_name="Acme Corp", user_name="Alice Smith", user_email="alice@acme.com")

        mock_ses_client.send_email.assert_called_once()
        call_kwargs = mock_ses_client.send_email.call_args[1]
        assert call_kwargs["Source"] == "info@dotelastic.com"
        assert call_kwargs["Destination"]["ToAddresses"] == ["ollie@dotelastic.com"]
        assert call_kwargs["Message"]["Subject"]["Data"] == "Statement Processor Login"
        html_body = call_kwargs["Message"]["Body"]["Html"]["Data"]
        assert "Acme Corp" in html_body
        assert "Alice Smith" in html_body
        assert "alice@acme.com" in html_body

    @patch("utils.email._ses_client")
    @patch("utils.email._STAGE", "prod")
    def test_does_not_raise_on_ses_failure(self, mock_ses_client) -> None:
        """Verify that SES errors are caught and logged, never raised."""
        mock_ses_client.send_email.side_effect = ClientError({"Error": {"Code": "MessageRejected", "Message": "Email rejected"}}, "SendEmail")

        # Must not raise
        send_login_notification_email(tenant_name="Acme Corp", user_name="Alice Smith", user_email="alice@acme.com")

    @patch("utils.email._ses_client")
    @patch("utils.email._STAGE", "dev")
    def test_skips_sending_in_dev(self, mock_ses_client) -> None:
        """Verify that no email is sent when STAGE is dev."""
        send_login_notification_email(tenant_name="Acme Corp", user_name="Alice Smith", user_email="alice@acme.com")

        mock_ses_client.send_email.assert_not_called()
