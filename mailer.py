"""Send plain-text emails (optionally with attachments) via SMTP using only the stdlib."""

import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path


def split_addresses(value: str) -> list[str]:
    return [a.strip() for a in (value or "").replace(";", ",").split(",") if a.strip()]


class Mailer:
    def __init__(self, host: str, port: int, security: str, user: str, password: str,
                 sender: str, timeout: int = 60):
        self.host, self.port, self.security = host, port, security
        self.user, self.password, self.sender = user, password, sender
        self.timeout = timeout

    @classmethod
    def from_settings(cls, s) -> "Mailer":
        return cls(s.smtp_host, s.smtp_port, s.smtp_security, s.smtp_user, s.smtp_password,
                   s.smtp_from)

    def build(self, to: str, subject: str, body: str, attachments=()) -> EmailMessage:
        recipients = split_addresses(to)
        if not recipients:
            raise ValueError(f"No recipient address for {subject!r}")
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        domain = parseaddr(self.sender)[1].rpartition("@")[2] or None
        msg["Message-ID"] = make_msgid(domain=domain)
        msg.set_content(body.rstrip("\n") + "\n\n")  # always end with one blank line
        for path in attachments:
            path = Path(path)
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype,
                               filename=path.name)
        return msg

    def send(self, to: str, subject: str, body: str, attachments=()) -> None:
        if not self.host or not self.sender:
            raise ValueError("SMTP_HOST and SMTP_FROM/SMTP_USER must be set")
        msg = self.build(to, subject, body, attachments)
        context = ssl.create_default_context()
        if self.security == "ssl":
            smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
        else:
            smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        with smtp:
            if self.security == "starttls":
                smtp.starttls(context=context)
            if self.user:
                smtp.login(self.user, self.password)
            smtp.send_message(msg)
