import imaplib
import email
import json
import os
from email.header import decode_header
from flask import Flask, render_template, request, jsonify
import anthropic

app = Flask(__name__)

IMAP_SERVERS = {
    "gmail":       "imap.gmail.com",
    "outlook":     "outlook.office365.com",
    "yahoo":       "imap.mail.yahoo.com",
    "centurylink": "mail.centurylink.net",
    "gulftel":     "mail.centurylink.net",
}

CLASSIFY_PROMPT = """You are an assistant that classifies finance newsletter emails.

Return ONLY a valid JSON object, no extra text:
{{
  "color": "green",
  "reason": "one sentence explanation"
}}

Color rules:
- green:  ONLY valuable financial content (market data, earnings, economic analysis, news)
- yellow: valuable financial content BUT ALSO subscription upsells or paid promotions mixed in
- red:    primarily or entirely promotional — upgrades, webinar sales, subscription offers

EMAIL SUBJECT: {subject}
EMAIL BODY:
{body}"""


def decode_str(value):
    parts = decode_header(value or "")
    result = []
    for part, encoding in parts:
        if isinstance(part, bytes):
            result.append(part.decode(encoding or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return " ".join(result)


def get_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                charset = part.get_content_charset() or "utf-8"
                body = part.get_payload(decode=True).decode(charset, errors="replace")
                break
    else:
        charset = msg.get_content_charset() or "utf-8"
        body = msg.get_payload(decode=True).decode(charset, errors="replace")
    return body[:4000]


def classify_email(client, subject, body):
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": CLASSIFY_PROMPT.format(subject=subject, body=body)}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def run_classifier():
    data        = request.get_json()
    email_addr  = data.get("email", "").strip()
    password    = data.get("password", "").strip()
    provider    = data.get("provider", "").strip().lower()
    custom_imap = data.get("custom_imap", "").strip()
    api_key     = data.get("api_key", "").strip()

    # Resolve IMAP server
    if provider == "other":
        server = custom_imap
    else:
        server = IMAP_SERVERS.get(provider)

    if not server:
        return jsonify({"success": False, "error": "Unknown provider. Please enter your IMAP server manually."})

    try:
        mail = imaplib.IMAP4_SSL(server, 993)
        mail.login(email_addr, password)
        mail.select("INBOX")

        status, data_raw = mail.uid("SEARCH", None, "UNSEEN")
        if status != "OK" or not data_raw[0]:
            mail.logout()
            return jsonify({"success": True, "results": [], "message": "No unread emails found."})

        uids   = data_raw[0].split()[:20]  # cap at 20 per run
        client = anthropic.Anthropic(api_key=api_key)
        results = []

        for uid in uids:
            try:
                status, msg_data = mail.uid("FETCH", uid, "(RFC822)")
                if status != "OK":
                    continue
                msg     = email.message_from_bytes(msg_data[0][1])
                subject = decode_str(msg.get("Subject", "(no subject)"))
                sender  = decode_str(msg.get("From", "unknown"))
                body    = get_body(msg)
                result  = classify_email(client, subject, body)
                color   = result.get("color", "red")
                reason  = result.get("reason", "")

                # Apply IMAP label/folder
                folder = f"Finance-Classifier/{color.capitalize()}"
                mail.create(folder)
                mail.uid("COPY", uid, folder)

                results.append({
                    "subject": subject[:80],
                    "sender":  sender[:60],
                    "color":   color,
                    "reason":  reason,
                })
            except Exception as e:
                results.append({
                    "subject": "(error processing email)",
                    "sender":  "",
                    "color":   "red",
                    "reason":  str(e),
                })

        mail.logout()
        return jsonify({"success": True, "results": results})

    except imaplib.IMAP4.error:
        return jsonify({"success": False, "error": "Login failed. Check your email and password."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
