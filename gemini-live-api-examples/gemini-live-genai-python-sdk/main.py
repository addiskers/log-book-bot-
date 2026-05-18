import asyncio
import base64
import json
import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from gemini_live import GeminiLive
from twilio_handler import TwilioMediaBridge

# Load environment variables
load_dotenv()

# Configure logging - DEBUG for our modules, INFO for everything else
logging.basicConfig(level=logging.INFO)
logging.getLogger("gemini_live").setLevel(logging.DEBUG)
logging.getLogger(__name__).setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)

# Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("MODEL", "gemini-3.1-flash-live-preview")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "+19785715824")

# ============ LOGBOOK360 TOOL HANDLERS ============

def handle_schedule_demo(**kwargs):
    """Log and confirm a demo scheduling request."""
    demo_id = f"DEMO-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    return {
        "success": True,
        "demo_id": demo_id,
        "contact_name": kwargs.get("contact_name", ""),
        "company_name": kwargs.get("company_name", ""),
        "contact_email": kwargs.get("contact_email", ""),
        "contact_phone": kwargs.get("contact_phone", ""),
        "industry": kwargs.get("industry", ""),
        "num_sites": kwargs.get("num_sites", ""),
        "primary_interest": kwargs.get("primary_interest", ""),
        "use_case_notes": kwargs.get("use_case_notes", ""),
        "status": "Demo Scheduled",
        "note": "A LogBook360 solutions consultant will reach out within 24 hours to confirm your demo.",
    }

def handle_check_feature(**kwargs):
    """Check feature availability for LogBook360."""
    query = kwargs.get("feature_query", "").lower()
    features = {
        "visitor": "Visitor Management is a core module of LogBook360, including pre-registration, QR-code check-in/check-out, digital badges, watchlist screening, NDA signing, and host notifications.",
        "access": "LogBook360 provides comprehensive access control integration with ID verification, multi-level access permissions, entry approval workflows, and real-time monitoring.",
        "compliance": "Full compliance and governance suite including automated audit trails, digital signatures, policy acknowledgements, role-based access control, and audit-ready reporting.",
        "emergency": "Emergency management module includes real-time evacuation tracking, SMS/email alert broadcasts, reunification workflows, incident management, and live occupancy visibility.",
        "analytics": "Advanced analytics with real-time dashboards, visitor trend reports, occupancy analytics, exportable compliance reports, and custom reporting capabilities.",
        "integration": "LogBook360 integrates with Google Workspace, Microsoft Outlook, ADP, Oracle PeopleSoft, Oracle HCM Cloud, Workday, and major access control systems.",
        "ai": "AL3i is LogBook360's built-in AI assistant providing voice-driven visitor interactions, multilingual support, real-time navigation guidance, and smart scheduling assistance.",
        "facial": "LogBook360 supports facial recognition integrations for enhanced identity verification and touchless check-in experiences.",
        "qr": "QR-code based workflows are built into LogBook360 for pre-registered and walk-in visitor check-in and check-out.",
        "nda": "Digital NDA acceptance and policy acknowledgement workflows are built into the visitor check-in process with full audit trail.",
        "watchlist": "Watchlist screening is a core security feature, automatically flagging visitors against internal and external watchlists during check-in.",
    }
    for key, description in features.items():
        if key in query:
            return {"available": True, "feature": key, "description": description}
    return {
        "available": True,
        "feature": "general",
        "description": f"LogBook360 is a comprehensive platform. The specific capability regarding '{kwargs.get('feature_query', '')}' can be discussed in detail during a personalized demo.",
    }


# Live transcript watchers (browser WebSockets watching phone calls)
live_watchers: set = set()

# Initialize FastAPI
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files
app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/")
async def root():
    return FileResponse("frontend/index.html")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for Gemini Live."""
    await websocket.accept()

    logger.info("WebSocket connection accepted")

    audio_input_queue = asyncio.Queue()
    video_input_queue = asyncio.Queue()
    text_input_queue = asyncio.Queue()

    async def audio_output_callback(data):
        await websocket.send_bytes(data)

    async def audio_interrupt_callback():
        # The event queue handles the JSON message, but we might want to do something else here
        pass

    gemini_client = GeminiLive(
        api_key=GEMINI_API_KEY, 
        model=MODEL, 
        input_sample_rate=16000,
        tool_mapping={
            "schedule_demo": handle_schedule_demo,
            "check_feature_availability": handle_check_feature,
        }
    )

    async def receive_from_client():
        try:
            while True:
                message = await websocket.receive()

                if message.get("bytes"):
                    await audio_input_queue.put(message["bytes"])
                elif message.get("text"):
                    text = message["text"]
                    try:
                        payload = json.loads(text)
                        if isinstance(payload, dict) and payload.get("type") == "image":
                            logger.info(f"Received image chunk from client: {len(payload['data'])} base64 chars")
                            image_data = base64.b64decode(payload["data"])
                            await video_input_queue.put(image_data)
                            continue
                    except json.JSONDecodeError:
                        pass

                    await text_input_queue.put(text)
        except WebSocketDisconnect:
            logger.info("WebSocket disconnected")
        except Exception as e:
            logger.error(f"Error receiving from client: {e}")

    receive_task = asyncio.create_task(receive_from_client())

    async def run_session():
        async for event in gemini_client.start_session(
            audio_input_queue=audio_input_queue,
            video_input_queue=video_input_queue,
            text_input_queue=text_input_queue,
            audio_output_callback=audio_output_callback,
            audio_interrupt_callback=audio_interrupt_callback,
        ):
            if event:
                # Forward events (transcriptions, etc) to client
                await websocket.send_json(event)

    try:
        await run_session()
    except Exception as e:
        import traceback
        logger.error(f"Error in Gemini session: {type(e).__name__}: {e}\n{traceback.format_exc()}")
    finally:
        receive_task.cancel()
        # Ensure websocket is closed if not already
        try:
            await websocket.close()
        except:
            pass


# ============ TWILIO VOICE ENDPOINTS ============

@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    """Twilio webhook: when someone calls your Twilio number, this answers."""
    host = request.headers.get("host", "localhost")
    protocol = "wss" if request.url.scheme == "https" or "onrender.com" in host else "ws"
    ws_url = f"{protocol}://{host}/twilio/media-stream"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}">
            <Parameter name="caller" value="{{{{From}}}}" />
        </Stream>
    </Connect>
</Response>"""

    return Response(content=twiml, media_type="application/xml")


@app.websocket("/twilio/media-stream")
async def twilio_media_stream(websocket: WebSocket):
    """WebSocket endpoint for Twilio Media Streams."""
    await websocket.accept()
    logger.info("Twilio Media Stream WebSocket accepted")

    gemini_client = GeminiLive(
        api_key=GEMINI_API_KEY,
        model=MODEL,
        input_sample_rate=16000,
        tool_mapping={
            "schedule_demo": handle_schedule_demo,
            "check_feature_availability": handle_check_feature,
        }
    )

    async def broadcast_event(event):
        """Send transcript events to all live watchers."""
        dead = set()
        for watcher in live_watchers:
            try:
                await watcher.send_json(event)
            except Exception:
                dead.add(watcher)
        live_watchers.difference_update(dead)

    bridge = TwilioMediaBridge(
        websocket=websocket,
        gemini_client=gemini_client,
        text_trigger="A caller has connected to the LogBook360 product line. Greet them professionally and begin the consultation flow.",
        on_event=broadcast_event,
    )

    try:
        await bridge.run()
    except Exception as e:
        import traceback
        logger.error(f"Twilio bridge error: {type(e).__name__}: {e}\n{traceback.format_exc()}")
    finally:
        try:
            await websocket.close()
        except:
            pass


@app.post("/call-me")
async def call_me(request: Request):
    """Make Twilio call a phone number and connect to the AI agent."""
    from twilio.rest import Client

    body = await request.json()
    to_number = body.get("phone")
    if not to_number:
        return {"error": "Missing 'phone' field. Send {\"phone\": \"+1XXXXXXXXXX\"}"}

    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return {"error": "Twilio credentials not configured"}

    # Use PUBLIC_URL env var or Render URL — Twilio can't reach localhost
    public_url = os.getenv("PUBLIC_URL", "")
    if public_url:
        webhook_url = f"{public_url}/twilio/voice"
    else:
        host = request.headers.get("host", "localhost")
        protocol = "https" if "onrender.com" in host else request.url.scheme
        webhook_url = f"{protocol}://{host}/twilio/voice"

    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        call = client.calls.create(
            to=to_number,
            from_=TWILIO_PHONE_NUMBER,
            url=webhook_url,
        )
        logger.info(f"Outbound call initiated: {call.sid} to {to_number}")
        return {"success": True, "call_sid": call.sid, "to": to_number}
    except Exception as e:
        logger.error(f"Failed to initiate call: {e}")
        return {"error": str(e)}


# ============ LIVE TRANSCRIPT DASHBOARD ============

@app.get("/live")
async def live_dashboard():
    """Live transcript dashboard — watch phone calls in real-time."""
    return HTMLResponse(LIVE_DASHBOARD_HTML)


@app.websocket("/live/ws")
async def live_ws(websocket: WebSocket):
    """WebSocket for live transcript watchers."""
    await websocket.accept()
    live_watchers.add(websocket)
    logger.info(f"Live watcher connected ({len(live_watchers)} total)")
    try:
        while True:
            await websocket.receive_text()  # keep alive
    except:
        pass
    finally:
        live_watchers.discard(websocket)
        logger.info(f"Live watcher disconnected ({len(live_watchers)} total)")


LIVE_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LogBook360 — Live Transcript</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0B1120;
  --card: rgba(19,27,46,0.80);
  --border: rgba(255,255,255,0.07);
  --primary: #0D9488;
  --secondary: #2563EB;
  --green: #10b981;
  --red: #ef4444;
  --text: #f1f5f9;
  --muted: #64748b;
  --text-secondary: #94a3b8;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Inter', system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}
body::before {
  content: '';
  position: fixed;
  inset: 0;
  background-image:
    linear-gradient(rgba(13,148,136,0.025) 1px, transparent 1px),
    linear-gradient(90deg, rgba(13,148,136,0.025) 1px, transparent 1px);
  background-size: 40px 40px;
  pointer-events: none;
}
.top-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 24px;
  background: rgba(11,17,32,0.9);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  z-index: 10;
}
.brand {
  font-weight: 700;
  font-size: 0.9rem;
  color: var(--primary);
}
.status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.75rem;
  font-weight: 600;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--muted);
}
.dot.live {
  background: var(--green);
  animation: pulse 2s infinite;
}
@keyframes pulse {
  0%,100% { opacity:1; box-shadow: 0 0 0 0 rgba(16,185,129,0.4); }
  50% { opacity:0.7; box-shadow: 0 0 0 4px rgba(16,185,129,0); }
}
.container {
  flex: 1;
  max-width: 700px;
  width: 100%;
  margin: 0 auto;
  padding: 20px;
  position: relative;
  z-index: 1;
}
.waiting {
  text-align: center;
  padding: 60px 20px;
  color: var(--muted);
}
.waiting h2 { font-size: 1.1rem; margin-bottom: 8px; color: var(--text-secondary); }
.waiting p { font-size: 0.8rem; }
#transcript {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.msg {
  padding: 10px 14px;
  border-radius: 12px;
  max-width: 85%;
  font-size: 0.875rem;
  line-height: 1.5;
  animation: fadeIn 0.2s ease-out;
}
@keyframes fadeIn {
  from { opacity:0; transform: translateY(8px); }
  to { opacity:1; transform: translateY(0); }
}
.msg .time {
  display: block;
  font-size: 0.6rem;
  opacity: 0.5;
  font-family: 'SF Mono', monospace;
  margin-top: 3px;
}
.msg.user {
  align-self: flex-end;
  background: linear-gradient(135deg, rgba(13,148,136,0.2), rgba(13,148,136,0.1));
  border: 1px solid rgba(13,148,136,0.15);
  border-bottom-right-radius: 4px;
}
.msg.gemini {
  align-self: flex-start;
  background: linear-gradient(135deg, rgba(37,99,235,0.2), rgba(37,99,235,0.1));
  border: 1px solid rgba(37,99,235,0.15);
  border-bottom-left-radius: 4px;
}
.msg.system {
  align-self: center;
  background: rgba(255,255,255,0.03);
  border: 1px solid var(--border);
  color: var(--muted);
  font-size: 0.75rem;
  max-width: 100%;
  text-align: center;
}
.tool-card {
  align-self: center;
  background: rgba(16,185,129,0.08);
  border: 1px solid rgba(16,185,129,0.2);
  border-radius: 8px;
  padding: 10px 14px;
  font-size: 0.75rem;
  color: var(--green);
  max-width: 100%;
  animation: fadeIn 0.2s ease-out;
}
.tool-card .tool-name { font-weight: 700; }
.tool-card pre {
  margin-top: 6px;
  color: var(--text-secondary);
  font-size: 0.7rem;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
</head>
<body>
<div class="top-bar">
  <span class="brand">LogBook360 — Live Transcript</span>
  <div class="status">
    <span class="dot" id="statusDot"></span>
    <span id="statusText">Waiting for call...</span>
  </div>
</div>
<div class="container">
  <div class="waiting" id="waiting">
    <h2>No active call</h2>
    <p>Start a test call or wait for an inbound inquiry.<br>The transcript will appear here in real-time.</p>
  </div>
  <div id="transcript"></div>
</div>
<script>
const transcript = document.getElementById('transcript');
const waiting = document.getElementById('waiting');
const statusDot = document.getElementById('statusDot');
const statusText = document.getElementById('statusText');
let currentUser = null;
let currentGemini = null;

const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
const ws = new WebSocket(protocol + '//' + location.host + '/live/ws');

ws.onopen = () => { statusText.textContent = 'Connected — waiting for call...'; };
ws.onclose = () => { statusText.textContent = 'Disconnected'; statusDot.className = 'dot'; };

ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);

  if (msg.type === 'call_start') {
    waiting.style.display = 'none';
    statusDot.className = 'dot live';
    statusText.textContent = 'Call in progress';
    addSystem('Call started');
    currentUser = null;
    currentGemini = null;
  }
  else if (msg.type === 'call_end') {
    statusDot.className = 'dot';
    statusText.textContent = 'Call ended';
    addSystem('Call ended');
    currentUser = null;
    currentGemini = null;
  }
  else if (msg.type === 'user') {
    if (currentUser) {
      currentUser.querySelector('.text').textContent += msg.text;
    } else {
      currentUser = addMsg('user', msg.text);
      currentGemini = null;
    }
  }
  else if (msg.type === 'gemini') {
    if (currentGemini) {
      currentGemini.querySelector('.text').textContent += msg.text;
    } else {
      currentGemini = addMsg('gemini', msg.text);
      currentUser = null;
    }
  }
  else if (msg.type === 'turn_complete') {
    currentUser = null;
    currentGemini = null;
  }
  else if (msg.type === 'tool_call') {
    addTool(msg.name, msg.result);
  }

  window.scrollTo(0, document.body.scrollHeight);
};

function addMsg(type, text) {
  const time = new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  const div = document.createElement('div');
  div.className = 'msg ' + type;
  div.innerHTML = '<span class="text"></span><span class="time">' + time + '</span>';
  div.querySelector('.text').textContent = text;
  transcript.appendChild(div);
  return div;
}

function addSystem(text) {
  const div = document.createElement('div');
  div.className = 'msg system';
  div.textContent = text;
  transcript.appendChild(div);
}

function addTool(name, result) {
  const div = document.createElement('div');
  div.className = 'tool-card';
  div.innerHTML = '<span class="tool-name">' + name + '</span><pre>' +
    JSON.stringify(result, null, 2).slice(0, 500) + '</pre>';
  transcript.appendChild(div);
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
