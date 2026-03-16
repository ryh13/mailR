import imaplib
import email
import json
import os
import queue
import threading
from email.header import decode_header
from email.utils import parsedate_to_datetime
from flask import Flask, Response, request, stream_with_context
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

BATCH_SIZE = 10


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
    return body[:3000]


def get_date(msg):
    try:
        dt = parsedate_to_datetime(msg.get("Date", ""))
        return dt.strftime("%b %d, %Y")
    except Exception:
        return ""


def classify_one(client, uid, msg, result_queue):
    try:
        subject = decode_str(msg.get("Subject", "(no subject)"))
        sender  = decode_str(msg.get("From", "unknown"))
        body    = get_body(msg)
        date    = get_date(msg)

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
            "uid":     uid.decode() if isinstance(uid, bytes) else str(uid),
            "subject": subject[:80],
            "sender":  sender[:60],
            "color":   result.get("color", "red"),
            "reason":  result.get("reason", ""),
            "date":    date,
        })
    except Exception as e:
        result_queue.put({
            "uid":     uid.decode() if isinstance(uid, bytes) else str(uid),
            "subject": "(error processing email)",
            "sender":  "", "color": "red",
            "reason":  str(e), "date": "",
        })


def apply_label(mail, uid, color):
    folder = f"Finance-Classifier/{color.capitalize()}"
    try:
        mail.create(folder)
    except Exception:
        pass
    try:
        uid_b = uid.encode() if isinstance(uid, str) else uid
        mail.uid("COPY", uid_b, folder)
    except Exception:
        pass


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
  .run-btn{background:var(--accent);color:#080b10;border:none;border-radius:8px;font-family:var(--sans);font-weight:600;font-size:14px;padding:12px;cursor:pointer;width:100%;margin-top:auto;transition:opacity .15s,transform .1s;}
  .run-btn:hover{opacity:.85;} .run-btn:active{transform:scale(.98);}
  .run-btn:disabled{opacity:.3;cursor:not-allowed;transform:none;}
  .status{font-size:11px;font-family:var(--mono);color:var(--muted);text-align:center;min-height:16px;}
  .status.err{color:var(--red);} .status.ok{color:var(--accent);}
  .prog-wrap{display:none;}
  .prog-label{font-size:10px;font-family:var(--mono);color:var(--muted);margin-bottom:4px;}
  .prog-track{height:2px;background:var(--border);border-radius:1px;overflow:hidden;}
  .prog-fill{height:100%;background:var(--accent);width:0%;transition:width .3s ease;}
  .sec-note{font-size:10px;color:var(--muted);line-height:1.5;padding:10px;background:var(--card);border-radius:6px;border:1px solid var(--border);}

  /* Dashboard */
  .dashboard{display:flex;flex-direction:column;overflow:hidden;}
  .summary{display:grid;grid-template-columns:repeat(3,1fr);border-bottom:1px solid var(--border);}
  .card{padding:20px 24px;border-right:1px solid var(--border);display:flex;flex-direction:column;gap:5px;cursor:pointer;transition:background .15s;user-select:none;}
  .card:last-child{border-right:none;}
  .card:hover{background:var(--card);}
  .card.active{background:var(--card);}
  .card .num{font-size:34px;font-family:var(--mono);font-weight:500;line-height:1;}
  .card .lbl{font-size:10px;color:var(--muted);letter-spacing:.08em;text-transform:uppercase;}
  .card .bar{height:3px;border-radius:2px;margin-top:8px;width:0%;transition:width .4s ease;}
  .card.g .num{color:var(--green);} .card.g .bar{background:var(--green);}
  .card.y .num{color:var(--yellow);} .card.y .bar{background:var(--yellow);}
  .card.r .num{color:var(--red);} .card.r .bar{background:var(--red);}
  .card .filter-indicator{font-size:9px;font-family:var(--mono);margin-top:2px;opacity:0;transition:opacity .2s;}
  .card.active .filter-indicator{opacity:1;}
  .card.g .filter-indicator{color:var(--green);}
  .card.y .filter-indicator{color:var(--yellow);}
  .card.r .filter-indicator{color:var(--red);}

  /* List */
  .list-wrap{flex:1;overflow:hidden;display:flex;flex-direction:column;}
  .list-head{display:grid;grid-template-columns:12px 1fr 170px 140px 1fr;gap:12px;padding:10px 20px;border-bottom:1px solid var(--border);font-size:10px;font-family:var(--mono);color:var(--muted);letter-spacing:.1em;text-transform:uppercase;background:var(--surface);flex-shrink:0;}
  .list-head span{cursor:pointer;display:flex;align-items:center;gap:4px;transition:color .15s;}
  .list-head span:hover{color:var(--text);}
  .list-head span.sorted{color:var(--accent);}
  .sort-arrow{font-size:9px;}
  .list{flex:1;overflow-y:auto;}
  .list::-webkit-scrollbar{width:4px;}
  .list::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px;}
  .row{display:grid;grid-template-columns:12px 1fr 170px 140px 1fr;gap:12px;padding:12px 20px;border-bottom:1px solid var(--border);align-items:center;animation:fadeIn .25s ease;transition:background .15s;}
  .row:hover{background:var(--card);}
  @keyframes fadeIn{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:translateY(0)}}
  .dot{width:9px;height:9px;border-radius:50%;flex-shrink:0;}
  .dot.green{background:var(--green);box-shadow:0 0 5px var(--green);}
  .dot.yellow{background:var(--yellow);box-shadow:0 0 5px var(--yellow);}
  .dot.red{background:var(--red);box-shadow:0 0 5px var(--red);}
  .sub{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .snd{font-size:11px;color:var(--muted);font-family:var(--mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .dt{font-size:11px;color:var(--muted);font-family:var(--mono);white-space:nowrap;}
  .rsn{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .empty{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;gap:10px;color:var(--muted);font-family:var(--mono);font-size:13px;}
  .empty .icon{font-size:30px;opacity:.25;}
  .spinner{width:22px;height:22px;border:2px solid var(--border2);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;display:none;margin:0 auto;}
  @keyframes spin{to{transform:rotate(360deg)}}
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
    <div class="field"><label>Email address</label><input type="email" id="email" placeholder="you@example.com"></div>
    <div class="field"><label>Password</label><input type="password" id="password" placeholder="••••••••"></div>
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
    <div class="sec-note">🔒 Credentials are never stored — only used for this session.</div>
    <div class="prog-wrap" id="prog-wrap">
      <div class="prog-label" id="prog-label">Starting...</div>
      <div class="prog-track"><div class="prog-fill" id="prog-fill"></div></div>
    </div>
    <div class="status" id="status"></div>
    <button class="run-btn" id="run-btn" onclick="run()">Run Classifier</button>
  </aside>

  <div class="dashboard">
    <div class="summary">
      <div class="card g" id="card-green" onclick="filterBy('green')">
        <div class="num" id="cnt-green">—</div>
        <div class="lbl">Financial Content</div>
        <div class="bar" id="bar-green"></div>
        <div class="filter-indicator">● showing only green</div>
      </div>
      <div class="card y" id="card-yellow" onclick="filterBy('yellow')">
        <div class="num" id="cnt-yellow">—</div>
        <div class="lbl">Mixed + Upsell</div>
        <div class="bar" id="bar-yellow"></div>
        <div class="filter-indicator">● showing only yellow</div>
      </div>
      <div class="card r" id="card-red" onclick="filterBy('red')">
        <div class="num" id="cnt-red">—</div>
        <div class="lbl">Promotional</div>
        <div class="bar" id="bar-red"></div>
        <div class="filter-indicator">● showing only red</div>
      </div>
    </div>

    <div class="list-wrap">
      <div class="list-head" id="list-head" style="display:none;">
        <span></span>
        <span onclick="sortBy('subject')" id="h-subject">Subject <span class="sort-arrow" id="a-subject"></span></span>
        <span onclick="sortBy('sender')"  id="h-sender">From   <span class="sort-arrow" id="a-sender"></span></span>
        <span onclick="sortBy('date')"    id="h-date" class="sorted">Date <span class="sort-arrow" id="a-date">↓</span></span>
        <span onclick="sortBy('reason')"  id="h-reason">Reason <span class="sort-arrow" id="a-reason"></span></span>
      </div>
      <div class="list" id="list">
        <div class="empty"><div class="icon">📥</div><div>Fill in your credentials and click Run</div></div>
      </div>
    </div>
  </div>
</main>

<script>
  let allRows = [];
  let activeFilter = null;  // null = show all
  let sortCol = "date";
  let sortAsc = false;       // newest first by default
  let total = 0, done = 0, evtSource = null;
  const counts = {green:0, yellow:0, red:0};

  function onProviderChange() {
    document.getElementById("imap-field").style.display =
      document.getElementById("provider").value === "other" ? "flex" : "none";
  }

  function setStatus(msg, type="") {
    const el = document.getElementById("status");
    el.textContent = msg; el.className = "status "+type;
  }

  // ── Filter by color card click ──────────────────────────────
  function filterBy(color) {
    if (activeFilter === color) {
      activeFilter = null;
      ["green","yellow","red"].forEach(c => document.getElementById(`card-${c}`).classList.remove("active"));
    } else {
      activeFilter = color;
      ["green","yellow","red"].forEach(c => {
        document.getElementById(`card-${c}`).classList.toggle("active", c === color);
      });
    }
    renderTable();
  }

  // ── Sorting ──────────────────────────────────────────────────
  function sortBy(col) {
    if (sortCol === col) { sortAsc = !sortAsc; }
    else { sortCol = col; sortAsc = col !== "date"; }
    ["subject","sender","date","reason"].forEach(c => {
      document.getElementById(`h-${c}`).classList.toggle("sorted", c === col);
      document.getElementById(`a-${c}`).textContent = c === col ? (sortAsc ? "↑" : "↓") : "";
    });
    renderTable();
  }

  function getSortVal(r, col) {
    if (col === "date") return r.dateTs || 0;
    return (r[col] || "").toLowerCase();
  }

  // ── Render visible rows ──────────────────────────────────────
  function renderTable() {
    const list = document.getElementById("list");
    let rows = activeFilter ? allRows.filter(r => r.color === activeFilter) : allRows;

    rows = [...rows].sort((a, b) => {
      const av = getSortVal(a, sortCol), bv = getSortVal(b, sortCol);
      return sortAsc ? (av > bv ? 1 : av < bv ? -1 : 0) : (av < bv ? 1 : av > bv ? -1 : 0);
    });

    if (rows.length === 0) {
      list.innerHTML = `<div class="empty"><div class="icon">📭</div><div>No emails in this category.</div></div>`;
      return;
    }

    list.innerHTML = rows.map(r => `
      <div class="row">
        <div class="dot ${r.color}"></div>
        <div class="sub">${esc(r.subject)}</div>
        <div class="snd">${esc(r.sender)}</div>
        <div class="dt">${esc(r.date)}</div>
        <div class="rsn">${esc(r.reason)}</div>
      </div>`).join("");
  }

  function updateBars() {
    ["green","yellow","red"].forEach(c => {
      document.getElementById(`cnt-${c}`).textContent = counts[c];
      document.getElementById(`bar-${c}`).style.width = total ? (counts[c]/total*100)+"%" : "0%";
    });
  }

  // ── Main run ─────────────────────────────────────────────────
  async function run() {
    const emailVal    = document.getElementById("email").value.trim();
    const password    = document.getElementById("password").value.trim();
    const provider    = document.getElementById("provider").value;
    const custom_imap = document.getElementById("custom_imap").value.trim();
    const api_key     = document.getElementById("api_key").value.trim();
    const btn         = document.getElementById("run-btn");

    if (!emailVal.includes("@"))       { setStatus("Enter a valid email.", "err"); return; }
    if (!password)                     { setStatus("Enter your password.", "err"); return; }
    if (!api_key.startsWith("sk-ant-")){ setStatus("API key must start with sk-ant-", "err"); return; }
    if (provider === "other" && !custom_imap) { setStatus("Enter your IMAP server.", "err"); return; }

    if (evtSource) evtSource.close();
    btn.disabled = true;
    allRows = []; counts.green = counts.yellow = counts.red = 0;
    total = 0; done = 0; activeFilter = null;
    ["green","yellow","red"].forEach(c => {
      document.getElementById(`card-${c}`).classList.remove("active");
      document.getElementById(`cnt-${c}`).textContent = "—";
      document.getElementById(`bar-${c}`).style.width = "0%";
    });
    setStatus("Connecting...");
    document.getElementById("prog-wrap").style.display = "block";
    document.getElementById("prog-fill").style.width = "0%";
    document.getElementById("prog-label").textContent = "Connecting to inbox...";
    document.getElementById("list-head").style.display = "none";
    document.getElementById("list").innerHTML = `<div class="empty"><div class="spinner" style="display:block"></div><div>Connecting...</div></div>`;

    const p = new URLSearchParams({email:emailVal, password, provider, custom_imap, api_key});
    evtSource = new EventSource(`/stream?${p}`);

    evtSource.addEventListener("start", e => {
      const d = JSON.parse(e.data);
      total = d.total;
      if (total === 0) {
        setStatus("No unread emails found.", "ok");
        document.getElementById("list").innerHTML = `<div class="empty"><div class="icon">📭</div><div>No unread emails.</div></div>`;
        ["green","yellow","red"].forEach(c => document.getElementById(`cnt-${c}`).textContent = "0");
        document.getElementById("prog-wrap").style.display = "none";
        evtSource.close(); btn.disabled = false;
      } else {
        setStatus(`Found ${total} unread emails. Classifying...`);
        document.getElementById("prog-label").textContent = `Classifying 0 / ${total}...`;
        document.getElementById("list-head").style.display = "grid";
      }
    });

    evtSource.addEventListener("result", e => {
      const r = JSON.parse(e.data);
      // Parse date to timestamp for sorting
      try { r.dateTs = new Date(r.dateRaw || r.date).getTime() || 0; } catch { r.dateTs = 0; }
      allRows.push(r);
      counts[r.color] = (counts[r.color]||0) + 1;
      done++;
      document.getElementById("prog-fill").style.width = total ? (done/total*100)+"%" : "0%";
      document.getElementById("prog-label").textContent = `Classifying ${done} / ${total}...`;
      updateBars();
      renderTable();
    });

    evtSource.addEventListener("done", () => {
      evtSource.close(); btn.disabled = false;
      document.getElementById("prog-wrap").style.display = "none";
      setStatus(`Done — ${done} email(s) classified.`, "ok");
    });

    evtSource.addEventListener("error_msg", e => {
      const d = JSON.parse(e.data);
      evtSource.close(); btn.disabled = false;
      document.getElementById("prog-wrap").style.display = "none";
      setStatus(d.message, "err");
      document.getElementById("list").innerHTML = `<div class="empty"><div class="icon">⚠️</div><div>${esc(d.message)}</div></div>`;
    });

    evtSource.onerror = () => {};
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
    server      = custom_imap if provider == "other" else IMAP_SERVERS.get(provider)

    def generate():
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

        status, data_raw = mail.uid("SEARCH", None, "UNSEEN")
        uids = data_raw[0].split() if status == "OK" and data_raw[0] else []

        # Reverse so newest (highest UID) is processed first
        uids = list(reversed(uids))
        total = len(uids)

        yield f"event: start\ndata: {json.dumps({'total': total})}\n\n"

        if not uids:
            mail.logout()
            yield f"event: done\ndata: {{}}\n\n"
            return

        client = anthropic.Anthropic(api_key=api_key)

        for i in range(0, total, BATCH_SIZE):
            batch_uids = uids[i:i + BATCH_SIZE]
            messages = {}
            for uid in batch_uids:
                try:
                    s, msg_data = mail.uid("FETCH", uid, "(RFC822)")
                    if s == "OK":
                        messages[uid] = email.message_from_bytes(msg_data[0][1])
                except Exception:
                    pass

            result_queue = queue.Queue()
            threads = []
            for uid, msg in messages.items():
                t = threading.Thread(target=classify_one, args=(client, uid, msg, result_queue))
                t.daemon = True
                t.start()
                threads.append(t)

            received = 0
            while received < len(messages):
                try:
                    result = result_queue.get(timeout=25)
                    received += 1
                    try:
                        apply_label(mail, result["uid"], result["color"])
                    except Exception:
                        pass
                    # Include raw date for JS timestamp parsing
                    yield f"event: result\ndata: {json.dumps(result)}\n\n"
                except queue.Empty:
                    break

        mail.logout()
        yield f"event: done\ndata: {{}}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
