import imaplib
import email
import json
import os
import queue
import threading
from email.header import decode_header
from flask import Flask, Response, request, jsonify, stream_with_context
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


def classify_one(client, uid, msg, result_queue):
    """Classify a single email and push the result to the queue."""
    try:
        subject = decode_str(msg.get("Subject", "(no subject)"))
        sender  = decode_str(msg.get("From", "unknown"))
        body    = get_body(msg)

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
        result = json.loads(raw.strip())

        result_queue.put({
            "uid":     uid.decode(),
            "subject": subject[:80],
            "sender":  sender[:60],
            "color":   result.get("color", "red"),
            "reason":  result.get("reason", ""),
            "error":   False,
        })
    except Exception as e:
        result_queue.put({
            "uid":     uid.decode() if isinstance(uid, bytes) else str(uid),
            "subject": "(error)",
            "sender":  "",
            "color":   "red",
            "reason":  str(e),
            "error":   True,
        })


def apply_label(mail, uid, color):
    """Copy email to its classifier folder."""
    folder = f"Finance-Classifier/{color.capitalize()}"
    try:
        mail.create(folder)
    except Exception:
        pass
    mail.uid("COPY", uid if isinstance(uid, bytes) else uid.encode(), folder)


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Finance Email Classifier</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#080b10;--surface:#0e1318;--card:#131920;--border:#1e2830;
    --border2:#253040;--text:#c8d8e8;--muted:#4a6070;--accent:#00c8a0;
    --green:#00c878;--yellow:#f0b040;--red:#e84848;
    --mono:'IBM Plex Mono',monospace;--sans:'IBM Plex Sans',sans-serif;
  }
  *{box-sizing:border-box;margin:0;padding:0;}
  body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;display:flex;flex-direction:column;}
  header{display:flex;align-items:center;gap:14px;padding:18px 32px;border-bottom:1px solid var(--border);background:var(--surface);}
  .logo{width:34px;height:34px;background:var(--accent);border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:17px;flex-shrink:0;}
  header h1{font-size:15px;font-weight:600;letter-spacing:.03em;}
  header p{font-size:11px;color:var(--muted);font-family:var(--mono);}
  main{display:grid;grid-template-columns:300px 1fr;flex:1;min-height:0;}
  aside{background:var(--surface);border-right:1px solid var(--border);padding:24px 20px;display:flex;flex-direction:column;gap:14px;overflow-y:auto;}
  .section-label{font-size:10px;font-family:var(--mono);color:var(--muted);letter-spacing:.12em;text-transform:uppercase;padding-bottom:4px;border-bottom:1px solid var(--border);}
  .field{display:flex;flex-direction:column;gap:4px;}
  .field label{font-size:11px;color:var(--muted);}
  .field input,.field select{background:var(--card);border:1px solid var(--border2);border-radius:6px;color:var(--text);font-family:var(--mono);font-size:12px;padding:8px 10px;outline:none;transition:border-color .15s;width:100%;appearance:none;}
  .field input:focus,.field select:focus{border-color:var(--accent);}
  .field input::placeholder{color:var(--muted);}
  .hint{font-size:10px;color:var(--muted);line-height:1.5;}
  #imap-field{display:none;}
  .run-btn{background:var(--accent);color:#080b10;border:none;border-radius:8px;font-family:var(--sans);font-weight:600;font-size:14px;padding:12px;cursor:pointer;width:100%;margin-top:auto;transition:opacity .15s,transform .1s;letter-spacing:.02em;}
  .run-btn:hover{opacity:.85;}
  .run-btn:active{transform:scale(.98);}
  .run-btn:disabled{opacity:.3;cursor:not-allowed;transform:none;}
  .status{font-size:11px;font-family:var(--mono);color:var(--muted);text-align:center;min-height:16px;}
  .status.err{color:var(--red);}
  .status.ok{color:var(--accent);}
  .progress-wrap{padding:0 0 4px;display:none;}
  .progress-track{height:2px;background:var(--border);border-radius:1px;overflow:hidden;}
  .progress-fill{height:100%;background:var(--accent);border-radius:1px;width:0%;transition:width .3s ease;}
  .progress-label{font-size:10px;font-family:var(--mono);color:var(--muted);margin-bottom:4px;}
  .dashboard{display:flex;flex-direction:column;overflow:hidden;}
  .summary{display:grid;grid-template-columns:repeat(3,1fr);border-bottom:1px solid var(--border);}
  .card{padding:20px 24px;border-right:1px solid var(--border);display:flex;flex-direction:column;gap:5px;}
  .card:last-child{border-right:none;}
  .card .num{font-size:34px;font-family:var(--mono);font-weight:500;line-height:1;}
  .card .lbl{font-size:10px;color:var(--muted);letter-spacing:.08em;text-transform:uppercase;}
  .card .bar{height:3px;border-radius:2px;margin-top:8px;width:0%;transition:width .4s ease;}
  .card.g .num{color:var(--green);} .card.g .bar{background:var(--green);}
  .card.y .num{color:var(--yellow);} .card.y .bar{background:var(--yellow);}
  .card.r .num{color:var(--red);} .card.r .bar{background:var(--red);}
  .list{flex:1;overflow-y:auto;}
  .list::-webkit-scrollbar{width:4px;}
  .list::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px;}
  .list-head{display:grid;grid-template-columns:12px 1fr 180px 1fr;gap:14px;padding:10px 20px;border-bottom:1px solid var(--border);font-size:10px;font-family:var(--mono);color:var(--muted);letter-spacing:.1em;text-transform:uppercase;position:sticky;top:0;background:var(--surface);z-index:1;}
  .row{display:grid;grid-template-columns:12px 1fr 180px 1fr;gap:14px;padding:12px 20px;border-bottom:1px solid var(--border);align-items:center;animation:fadeIn .25s ease;transition:background .15s;}
  .row:hover{background:var(--card);}
  @keyframes fadeIn{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:translateY(0)}}
  .dot{width:9px;height:9px;border-radius:50%;flex-shrink:0;}
  .dot.green{background:var(--green);box-shadow:0 0 5px var(--green);}
  .dot.yellow{background:var(--yellow);box-shadow:0 0 5px var(--yellow);}
  .dot.red{background:var(--red);box-shadow:0 0 5px var(--red);}
  .sub{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .snd{font-size:11px;color:var(--muted);font-family:var(--mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .rsn{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .empty{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;gap:10px;color:var(--muted);font-family:var(--mono);font-size:13px;}
  .empty .icon{font-size:30px;opacity:.25;}
  .spinner{width:22px;height:22px;border:2px solid var(--border2);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;display:none;margin:0 auto;}
  @keyframes spin{to{transform:rotate(360deg)}}
  .sec-note{font-size:10px;color:var(--muted);line-height:1.5;padding:10px;background:var(--card);border-radius:6px;border:1px solid var(--border);}
</style>
</head>
<body>
<header>
  <div class="logo">📧</div>
  <div>
    <h1>Finance Email Classifier</h1>
    <p>powered by claude ai · works with any email provider</p>
  </div>
</header>
<main>
  <aside>
    <div class="section-label">Email Settings</div>
    <div class="field">
      <label>Email address</label>
      <input type="email" id="email" placeholder="you@example.com">
    </div>
    <div class="field">
      <label>Password</label>
      <input type="password" id="password" placeholder="••••••••">
    </div>
    <div class="field">
      <label>Provider</label>
      <select id="provider" onchange="onProviderChange()">
        <option value="gmail">Gmail</option>
        <option value="outlook">Outlook / Hotmail</option>
        <option value="yahoo">Yahoo Mail</option>
        <option value="centurylink">CenturyLink / Gulftel</option>
        <option value="other">Other (enter IMAP server)</option>
      </select>
    </div>
    <div class="field" id="imap-field">
      <label>IMAP Server</label>
      <input type="text" id="custom_imap" placeholder="mail.yourprovider.com">
      <span class="hint">Ask your email provider for this address.</span>
    </div>
    <div class="section-label" style="margin-top:4px;">AI Settings</div>
    <div class="field">
      <label>Anthropic API Key</label>
      <input type="password" id="api_key" placeholder="sk-ant-...">
      <span class="hint">Get a free key at console.anthropic.com</span>
    </div>
    <div class="sec-note">
      🔒 Your credentials are never stored. They are only used for this session and sent directly to your email provider and Anthropic.
    </div>
    <div class="progress-wrap" id="progress-wrap">
      <div class="progress-label" id="progress-label">Classifying...</div>
      <div class="progress-track"><div class="progress-fill" id="progress-fill"></div></div>
    </div>
    <div class="status" id="status"></div>
    <button class="run-btn" id="run-btn" onclick="run()">Run Classifier</button>
  </aside>
  <div class="dashboard">
    <div class="summary">
      <div class="card g">
        <div class="num" id="cnt-green">—</div>
        <div class="lbl">Financial Content</div>
        <div class="bar" id="bar-green"></div>
      </div>
      <div class="card y">
        <div class="num" id="cnt-yellow">—</div>
        <div class="lbl">Mixed + Upsell</div>
        <div class="bar" id="bar-yellow"></div>
      </div>
      <div class="card r">
        <div class="num" id="cnt-red">—</div>
        <div class="lbl">Promotional</div>
        <div class="bar" id="bar-red"></div>
      </div>
    </div>
    <div class="list" id="list">
      <div class="empty">
        <div class="icon">📥</div>
        <div>Fill in your credentials and click Run</div>
      </div>
    </div>
  </div>
</main>
<script>
  const counts = {green:0, yellow:0, red:0};
  let total = 0;
  let listStarted = false;

  function onProviderChange() {
    const p = document.getElementById("provider").value;
    document.getElementById("imap-field").style.display = p === "other" ? "flex" : "none";
  }

  function setStatus(msg, type="") {
    const el = document.getElementById("status");
    el.textContent = msg;
    el.className = "status " + type;
  }

  function updateBars() {
    ["green","yellow","red"].forEach(c => {
      document.getElementById(`cnt-${c}`).textContent = counts[c];
      document.getElementById(`bar-${c}`).style.width = total ? (counts[c]/total*100)+"%" : "0%";
    });
  }

  function appendRow(r) {
    const list = document.getElementById("list");
    if (!listStarted) {
      list.innerHTML = `<div class="list-head"><span></span><span>Subject</span><span>From</span><span>Reason</span></div>`;
      listStarted = true;
    }
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `
      <div class="dot ${r.color}"></div>
      <div class="sub">${esc(r.subject)}</div>
      <div class="snd">${esc(r.sender)}</div>
      <div class="rsn">${esc(r.reason)}</div>`;
    list.appendChild(row);
    list.scrollTop = list.scrollHeight;
  }

  async function run() {
    const emailVal   = document.getElementById("email").value.trim();
    const password   = document.getElementById("password").value.trim();
    const provider   = document.getElementById("provider").value;
    const custom_imap = document.getElementById("custom_imap").value.trim();
    const api_key    = document.getElementById("api_key").value.trim();
    const btn        = document.getElementById("run-btn");

    if (!emailVal || !emailVal.includes("@")) { setStatus("Enter a valid email.", "err"); return; }
    if (!password)  { setStatus("Enter your password.", "err"); return; }
    if (!api_key.startsWith("sk-ant-")) { setStatus("API key must start with sk-ant-", "err"); return; }
    if (provider === "other" && !custom_imap) { setStatus("Enter your IMAP server.", "err"); return; }

    btn.disabled = true;
    counts.green = counts.yellow = counts.red = 0;
    total = 0;
    listStarted = false;
    setStatus("Connecting...");
    document.getElementById("progress-wrap").style.display = "block";
    document.getElementById("progress-fill").style.width = "0%";
    document.getElementById("list").innerHTML = `<div class="empty"><div class="spinner" style="display:block"></div><div>Connecting to inbox...</div></div>`;
    ["green","yellow","red"].forEach(c => {
      document.getElementById(`cnt-${c}`).textContent = "—";
      document.getElementById(`bar-${c}`).style.width = "0%";
    });

    // SSE stream
    const params = new URLSearchParams({email: emailVal, password, provider, custom_imap, api_key});
    const evtSource = new EventSource(`/stream?${params}`);

    evtSource.addEventListener("start", e => {
      const d = JSON.parse(e.data);
      total = d.total;
      setStatus(`Classifying ${total} email(s)...`);
      if (total === 0) {
        document.getElementById("list").innerHTML = `<div class="empty"><div class="icon">📭</div><div>No unread emails found.</div></div>`;
        ["green","yellow","red"].forEach(c => document.getElementById(`cnt-${c}`).textContent = "0");
        evtSource.close();
        btn.disabled = false;
        document.getElementById("progress-wrap").style.display = "none";
      }
    });

    evtSource.addEventListener("result", e => {
      const r = JSON.parse(e.data);
      counts[r.color] = (counts[r.color] || 0) + 1;
      const done = counts.green + counts.yellow + counts.red;
      document.getElementById("progress-fill").style.width = total ? (done/total*100)+"%" : "0%";
      document.getElementById("progress-label").textContent = `Classifying ${done} / ${total}...`;
      updateBars();
      appendRow(r);
    });

    evtSource.addEventListener("done", e => {
      evtSource.close();
      btn.disabled = false;
      document.getElementById("progress-wrap").style.display = "none";
      setStatus(`Done — ${total} email(s) classified.`, "ok");
    });

    evtSource.addEventListener("error_msg", e => {
      const d = JSON.parse(e.data);
      evtSource.close();
      btn.disabled = false;
      document.getElementById("progress-wrap").style.display = "none";
      setStatus(d.message, "err");
      document.getElementById("list").innerHTML = `<div class="empty"><div class="icon">⚠️</div><div>${esc(d.message)}</div></div>`;
    });

    evtSource.onerror = () => {
      evtSource.close();
      btn.disabled = false;
      document.getElementById("progress-wrap").style.display = "none";
      setStatus("Connection lost. Please try again.", "err");
    };
  }

  function esc(s){ return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
</script>
</body>
</html>"""


@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")


@app.route("/stream")
def stream():
    email_addr  = request.args.get("email", "").strip()
    password    = request.args.get("password", "").strip()
    provider    = request.args.get("provider", "").strip().lower()
    custom_imap = request.args.get("custom_imap", "").strip()
    api_key     = request.args.get("api_key", "").strip()

    server = custom_imap if provider == "other" else IMAP_SERVERS.get(provider)

    def generate():
        # Connect to IMAP
        try:
            mail = imaplib.IMAP4_SSL(server, 993)
            mail.login(email_addr, password)
            mail.select("INBOX")
        except imaplib.IMAP4.error:
            yield f"event: error_msg\ndata: {json.dumps({'message': 'Login failed. Check your email and password.'})}\n\n"
            return
        except Exception as e:
            yield f"event: error_msg\ndata: {json.dumps({'message': str(e)})}\n\n"
            return

        # Fetch all unread UIDs
        status, data_raw = mail.uid("SEARCH", None, "UNSEEN")
        uids = data_raw[0].split() if status == "OK" and data_raw[0] else []
        yield f"event: start\ndata: {json.dumps({'total': len(uids)})}\n\n"

        if not uids:
            mail.logout()
            yield f"event: done\ndata: {{}}\n\n"
            return

        # Fetch all email messages first
        messages = {}
        for uid in uids:
            try:
                s, msg_data = mail.uid("FETCH", uid, "(RFC822)")
                if s == "OK":
                    messages[uid] = email.message_from_bytes(msg_data[0][1])
            except Exception:
                pass

        # Classify all in parallel using threads
        result_queue = queue.Queue()
        client = anthropic.Anthropic(api_key=api_key)

        threads = []
        for uid, msg in messages.items():
            t = threading.Thread(target=classify_one, args=(client, uid, msg, result_queue))
            t.daemon = True
            t.start()
            threads.append(t)

        # Stream results as they come in
        received = 0
        while received < len(messages):
            try:
                result = result_queue.get(timeout=30)
                received += 1

                # Apply label back to inbox
                try:
                    uid_bytes = result["uid"].encode()
                    apply_label(mail, uid_bytes, result["color"])
                except Exception:
                    pass

                yield f"event: result\ndata: {json.dumps(result)}\n\n"
            except queue.Empty:
                break

        mail.logout()
        yield f"event: done\ndata: {{}}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
