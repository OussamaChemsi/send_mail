#!/usr/bin/env python3
"""Send the same email to a list of contacts using settings loaded strictly from .env / environment variables."""

from email.message import EmailMessage
from email.utils import formataddr
import os
from pathlib import Path
import re
import smtplib
import ssl
import stat
import sys
import time
from typing import Dict, List, Tuple


EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def parse_env_file(env_path: Path) -> Dict[str, str]:
    """Parse a .env file supporting quotes, multiline values, comments, and \\n escapes."""
    if not env_path.is_file():
        raise FileNotFoundError(f"Environment file not found: {env_path}")

    content = env_path.read_text(encoding="utf-8")
    env_vars: Dict[str, str] = {}
    idx = 0
    length = len(content)

    while idx < length:
        while idx < length and content[idx] in " \t\r\n":
            idx += 1
        if idx >= length:
            break

        if content[idx] == "#":
            while idx < length and content[idx] != "\n":
                idx += 1
            continue

        if content.startswith("export ", idx):
            idx += len("export ")

        eq_pos = content.find("=", idx)
        nl_pos = content.find("\n", idx)
        if eq_pos == -1 or (nl_pos != -1 and eq_pos > nl_pos):
            idx = length if nl_pos == -1 else nl_pos + 1
            continue

        key = content[idx:eq_pos].strip().lower()
        idx = eq_pos + 1

        while idx < length and content[idx] in " \t":
            idx += 1

        if idx >= length or content[idx] in "\r\n":
            env_vars[key] = ""
            continue

        quote_char = content[idx] if content[idx] in ('"', "'") else None
        if quote_char:
            idx += 1
            val_chars: List[str] = []
            while idx < length:
                ch = content[idx]
                if ch == "\\" and quote_char == '"' and idx + 1 < length:
                    nxt = content[idx + 1]
                    escape_map = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
                    val_chars.append(escape_map.get(nxt, "\\" + nxt))
                    idx += 2
                    continue
                if ch == quote_char:
                    idx += 1
                    break
                val_chars.append(ch)
                idx += 1
            env_vars[key] = "".join(val_chars)
            while idx < length and content[idx] != "\n":
                idx += 1
        else:
            val_start = idx
            while idx < length and content[idx] not in "\r\n":
                if content[idx] == "#" and idx > val_start and content[idx - 1] in " \t":
                    break
                idx += 1
            raw_val = content[val_start:idx].strip()
            env_vars[key] = raw_val.replace("\\n", "\n")
            while idx < length and content[idx] != "\n":
                idx += 1

    return env_vars


def secure_env_permissions(env_path: Path) -> None:
    """Restrict .env permissions to owner read/write only (0600) on POSIX systems."""
    if os.name != "posix" or not env_path.is_file():
        return
    try:
        current_mode = stat.S_IMODE(env_path.stat().st_mode)
        if current_mode & (stat.S_IRWXG | stat.S_IRWXO):
            env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def redact_secrets(text: str, secrets: List[str]) -> str:
    """Remove any secret values from error messages before logging."""
    sanitized = text
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    return sanitized


def load_config() -> Tuple[Dict[str, str], Path]:
    """Load configuration from .env file and merge with OS environment variables."""
    env_file = os.environ.get("ENV_FILE")
    env_path = (Path(env_file) if env_file else (Path(__file__).resolve().parent / ".env")).resolve()

    cfg: Dict[str, str] = {}
    if env_path.is_file():
        secure_env_permissions(env_path)
        cfg.update(parse_env_file(env_path))
    elif not os.environ:
        raise FileNotFoundError(f"Environment file not found: {env_path}")

    # OS environment variables override .env file entries if set
    for key, val in os.environ.items():
        cfg[key.lower()] = val

    return cfg, env_path.parent


def require_str(cfg: Dict[str, str], key: str, errors: List[str], strip: bool = True) -> str:
    """Fetch a required string setting from config without any hardcoded fallback."""
    if key not in cfg:
        errors.append(f"Missing '{key}' in .env.")
        return ""
    val = cfg[key].strip() if strip else cfg[key]
    if not val.strip():
        errors.append(f"'{key}' in .env cannot be empty.")
    return val


def require_int(cfg: Dict[str, str], key: str, errors: List[str]) -> int:
    """Fetch a required integer setting from config."""
    raw = require_str(cfg, key, errors)
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        errors.append(f"'{key}' in .env must be an integer (got '{raw}').")
        return 0


def require_float(cfg: Dict[str, str], key: str, errors: List[str]) -> float:
    """Fetch a required float setting from config."""
    raw = require_str(cfg, key, errors)
    if not raw:
        return 0.0
    try:
        val = float(raw)
        if val < 0:
            errors.append(f"'{key}' in .env must be >= 0 (got '{raw}').")
        return val
    except ValueError:
        errors.append(f"'{key}' in .env must be a number (got '{raw}').")
        return 0.0


def require_bool(cfg: Dict[str, str], key: str, errors: List[str]) -> bool:
    """Fetch a required boolean setting ('true'/'false') from config."""
    raw = require_str(cfg, key, errors).lower()
    if not raw:
        return False
    if raw in {"true", "1", "yes"}:
        return True
    if raw in {"false", "0", "no"}:
        return False
    errors.append(f"'{key}' in .env must be 'true' or 'false' (got '{raw}').")
    return False


def parse_recipients(raw_list: str, errors: List[str]) -> List[str]:
    """Split comma/semicolon/newline-separated emails, deduplicate, and validate."""
    tokens = re.split(r"[,;\n\r]+", raw_list)
    recipients: List[str] = []
    seen = set()
    invalid: List[str] = []

    for token in tokens:
        email_addr = token.strip()
        if not email_addr:
            continue
        if not EMAIL_REGEX.match(email_addr):
            invalid.append(email_addr)
            continue
        lowered = email_addr.lower()
        if lowered not in seen:
            seen.add(lowered)
            recipients.append(email_addr)

    if invalid:
        errors.append(f"Invalid email address(es) in 'mail_list': {', '.join(invalid)}")

    return recipients


def load_pdf_attachments(
    cfg: Dict[str, str],
    key: str,
    base_dir: Path,
    errors: List[str],
) -> List[Tuple[str, bytes]]:
    """Resolve, validate, and read PDF attachment paths configured in .env."""
    if key not in cfg:
        errors.append(f"Missing '{key}' in .env.")
        return []

    raw_paths = cfg[key].strip()
    if not raw_paths:
        return []

    tokens = re.split(r"[,;\n\r]+", raw_paths)
    attachments: List[Tuple[str, bytes]] = []
    seen_paths = set()

    for token in tokens:
        entry = token.strip().strip('"').strip("'")
        if not entry:
            continue

        candidate = Path(entry).expanduser()
        if not candidate.is_absolute():
            in_env_dir = (base_dir / candidate).resolve()
            candidate = in_env_dir if in_env_dir.exists() else candidate.resolve()
        else:
            candidate = candidate.resolve()

        if candidate in seen_paths:
            continue
        seen_paths.add(candidate)

        if not candidate.is_file():
            errors.append(f"PDF file not found in '{key}': {entry}")
            continue
        if candidate.suffix.lower() != ".pdf":
            errors.append(f"Attachment in '{key}' must have a .pdf extension: {entry}")
            continue

        try:
            data = candidate.read_bytes()
        except OSError as exc:
            errors.append(f"Cannot read PDF file '{entry}': {exc}")
            continue

        if not data.startswith(b"%PDF-"):
            errors.append(f"File in '{key}' is not a valid PDF document: {entry}")
            continue

        attachments.append((candidate.name, data))

    return attachments


def build_message(
    sender_email: str,
    sender_name: str,
    recipient_email: str,
    subject: str,
    body: str,
    is_html: bool,
    pdf_attachments: List[Tuple[str, bytes]],
) -> EmailMessage:
    """Create an EmailMessage for a single recipient, including any PDF attachments."""
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender_email)) if sender_name else sender_email
    msg["To"] = recipient_email
    msg["Subject"] = subject
    if is_html:
        msg.set_content(body, subtype="html")
    else:
        msg.set_content(body)

    for filename, pdf_bytes in pdf_attachments:
        msg.add_attachment(
            pdf_bytes,
            maintype="application",
            subtype="pdf",
            filename=filename,
        )

    return msg


def send_all(
    smtp_host: str,
    smtp_port: int,
    smtp_use_ssl: bool,
    smtp_timeout: float,
    sender_email: str,
    sender_name: str,
    app_key: str,
    recipients: List[str],
    subject: str,
    body: str,
    is_html: bool,
    pdf_attachments: List[Tuple[str, bytes]],
    send_delay: float,
) -> int:
    """Authenticate with the configured SMTP server and send individual emails."""
    print(f"Connecting to {smtp_host}:{smtp_port} (SSL={smtp_use_ssl}) as {sender_email}...")
    context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    sent_count = 0
    failed: List[str] = []

    smtp_cls = smtplib.SMTP_SSL if smtp_use_ssl else smtplib.SMTP
    smtp_kwargs = {"timeout": smtp_timeout}
    if smtp_use_ssl:
        smtp_kwargs["context"] = context

    with smtp_cls(smtp_host, smtp_port, **smtp_kwargs) as smtp:
        if not smtp_use_ssl:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()

        smtp.login(sender_email, app_key)
        attach_info = (
            f" with {len(pdf_attachments)} PDF(s)" if pdf_attachments else ""
        )
        print(f"Authenticated. Sending{attach_info} to {len(recipients)} contact(s)...\n")

        for i, recipient in enumerate(recipients, start=1):
            msg = build_message(
                sender_email=sender_email,
                sender_name=sender_name,
                recipient_email=recipient,
                subject=subject,
                body=body,
                is_html=is_html,
                pdf_attachments=pdf_attachments,
            )
            try:
                smtp.send_message(msg)
                sent_count += 1
                print(f"[{i}/{len(recipients)}] Sent -> {recipient}")
            except smtplib.SMTPException as exc:
                failed.append(f"{recipient} ({exc})")
                print(f"[{i}/{len(recipients)}] FAILED -> {recipient}: {exc}", file=sys.stderr)

            if i < len(recipients) and send_delay > 0:
                time.sleep(send_delay)

    print(f"\nDone! Sent: {sent_count}/{len(recipients)}")
    if failed:
        print("Failed recipients:", file=sys.stderr)
        for item in failed:
            print(f"  - {item}", file=sys.stderr)
        return 1

    return 0


def main() -> int:
    try:
        cfg, base_dir = load_config()
    except Exception as exc:
        print(f"[ERROR] Failed to load environment configuration: {exc}", file=sys.stderr)
        return 1

    errors: List[str] = []

    smtp_host = require_str(cfg, "smtp_host", errors)
    smtp_port = require_int(cfg, "smtp_port", errors)
    smtp_use_ssl = require_bool(cfg, "smtp_use_ssl", errors)
    smtp_timeout = require_float(cfg, "smtp_timeout", errors)

    sender_email = require_str(cfg, "gmail_sender", errors)
    if sender_email and not EMAIL_REGEX.match(sender_email):
        errors.append(f"'gmail_sender' is not a valid email address: {sender_email}")

    sender_name = cfg.get("sender_name", "").strip()
    raw_app_key = require_str(cfg, "gmail_app_key", errors)
    app_key = "".join(raw_app_key.split())

    raw_mail_list = require_str(cfg, "mail_list", errors)
    recipients = parse_recipients(raw_mail_list, errors) if raw_mail_list else []
    if raw_mail_list and not recipients:
        errors.append("No valid email addresses found in 'mail_list'.")

    subject = require_str(cfg, "mail_subject", errors)
    body = require_str(cfg, "mail_content", errors, strip=False)
    is_html = require_bool(cfg, "mail_is_html", errors)
    pdf_attachments = load_pdf_attachments(cfg, "pdf_attachments", base_dir, errors)

    send_delay = require_float(cfg, "send_delay", errors)
    dry_run = require_bool(cfg, "dry_run", errors)

    if dry_run:
        attach_summary = (
            ", ".join(f"{name} ({len(data) / 1024:.1f} KB)" for name, data in pdf_attachments)
            if pdf_attachments
            else "None"
        )
        print("=== SIMULATION / DRY RUN (dry_run=\"true\" in .env) ===")
        print(f"SMTP       : {smtp_host}:{smtp_port} (SSL={smtp_use_ssl}, timeout={smtp_timeout}s)")
        print(f"Sender     : {formataddr((sender_name, sender_email)) if sender_name else sender_email}")
        print(f"App Key    : {'[REDACTED]' if app_key else '[MISSING]'}")
        print(f"Recipients : {len(recipients)} -> {', '.join(recipients)}")
        print(f"Subject    : {subject}")
        print(f"Format     : {'HTML' if is_html else 'Plain Text'}")
        print(f"PDFs       : {len(pdf_attachments)} -> {attach_summary}")
        print(f"Send Delay : {send_delay}s")
        print("\n--- Email Content ---")
        print(body)
        print("---------------------\n")

        if errors:
            print("[NOTE] Fix these .env issues before running:", file=sys.stderr)
            for err in errors:
                print(f"  - {err}", file=sys.stderr)
            return 1

        for i, recipient in enumerate(recipients, start=1):
            msg = build_message(
                sender_email=sender_email,
                sender_name=sender_name,
                recipient_email=recipient,
                subject=subject,
                body=body,
                is_html=is_html,
                pdf_attachments=pdf_attachments,
            )
            size_kb = len(msg.as_bytes()) / 1024
            print(
                f"[SIMULATION {i}/{len(recipients)}] Prepared email -> {recipient} "
                f"(total MIME size: {size_kb:.1f} KB)"
            )
            if i < len(recipients) and send_delay > 0:
                time.sleep(send_delay)

        print(f"\nSimulation complete! {len(recipients)}/{len(recipients)} emails ready.")
        print("Set dry_run=\"false\" in .env with real Gmail credentials and contacts to send.")
        return 0

    if errors:
        print("[ERROR] Missing or invalid .env configuration:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    try:
        return send_all(
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_use_ssl=smtp_use_ssl,
            smtp_timeout=smtp_timeout,
            sender_email=sender_email,
            sender_name=sender_name,
            app_key=app_key,
            recipients=recipients,
            subject=subject,
            body=body,
            is_html=is_html,
            pdf_attachments=pdf_attachments,
            send_delay=send_delay,
        )
    except smtplib.SMTPAuthenticationError:
        print(
            "[ERROR] SMTP authentication failed. Check 'gmail_sender' and 'gmail_app_key' in .env.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        safe_err = redact_secrets(str(exc), [app_key, raw_app_key])
        print(f"[ERROR] SMTP error: {safe_err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
