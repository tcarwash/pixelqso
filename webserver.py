"""Optional companion mobile web interface and local control API."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import json

from ui_theme import COLORS, RADIUS


PAGE = r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="__WINDOW__"><title>Pixel QSO</title><style>
*{box-sizing:border-box}body{margin:0;padding:calc(16px + env(safe-area-inset-top)) calc(16px + env(safe-area-inset-right)) calc(16px + env(safe-area-inset-bottom)) calc(16px + env(safe-area-inset-left));background:__WINDOW__;color:__TEXT__;font:16px system-ui;max-width:720px;margin:auto}h1{font-size:22px}button,select,input{background:__SURFACE_RAISED__;color:__TEXT_BRIGHT__;border:1px solid __BORDER_BUTTON__;border-radius:__BUTTON_RADIUS__px;padding:12px;font-size:16px}button.primary{background:__ACCENT__;color:__TEXT_PRIMARY__;font-weight:bold;width:100%}.row{display:flex;gap:8px;margin:10px 0}.row>*{flex:1;min-width:0}.row label{display:flex;flex-direction:column;gap:6px}.row input{width:100%;min-width:0}.panel{background:__SURFACE__;border:1px solid __BORDER_CONTROL__;border-radius:__PANEL_RADIUS__px;padding:14px;margin:12px 0}canvas{width:100%;max-width:480px;aspect-ratio:1;image-rendering:pixelated;touch-action:none;background:#000;display:block;margin:12px auto}#draw:focus-visible{outline:3px solid __FOCUS_RING__;outline-offset:2px}.colors{display:flex;flex-wrap:wrap;gap:8px}.sw{width:38px;height:38px;border:2px solid __BORDER_CONTROL__;border-radius:__CONTROL_RADIUS__px}.sw.on{border-color:__TEXT_BRIGHT__;box-shadow:inset 0 0 0 2px __TEXT_PRIMARY__}button:focus-visible,select:focus-visible,input:focus-visible{outline:3px solid __FOCUS_RING__;outline-offset:2px}article{border-bottom:1px solid __BORDER_CONTROL__;padding:10px 0}article canvas{max-width:220px}.muted{color:__TEXT_MUTED__;font-size:14px}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}</style></head><body><h1>Pixel QSO</h1><div id="state" class="muted" role="status" aria-live="polite" aria-atomic="true">Connecting…</div><section class="panel"><h2>Transmit</h2><div class="row"><label>Transmit stage<select id="stage" aria-label="Transmit stage"><option value="" selected disabled>Loading…</option><option value="card" disabled>Card (station mode)</option><option value="cq">CQ</option><option value="exchange">Exchange</option><option value="report73">RR73 with report</option><option value="final73">Final 73</option></select></label><button id="stop" aria-label="Stop transmission">Stop transmission</button></div><button class="primary" id="send">Transmit selected stage</button><p class="muted">Uses the desktop station's selected mode, card, and radio settings.</p></section><section class="panel"><h2>Quick draw</h2><div class="row"><label>Call sign <input id="call" maxlength="12" value="N0CALL"></label><label>Grid <input id="grid" maxlength="8" value="AA00"></label></div><div class="colors" id="colors" role="group" aria-label="Choose a drawing color"></div><canvas id="draw" width="32" height="32" tabindex="0" aria-label="Quick draw canvas" aria-describedby="draw-help draw-position"></canvas><p id="draw-help" class="muted">Draw by touch or mouse. For keyboard drawing, focus the canvas, move with the arrow keys, and press Space or Enter to paint with the selected color.</p><span id="draw-position" class="sr-only" aria-live="polite">Cursor at row 1, column 1; selected color 2.</span><div class="row"><button id="clear">Clear</button><button class="primary" id="use">Use quick draw</button></div><p class="muted">Use quick draw loads this image as the next selected-stage transmission.</p></section><section class="panel"><h2>Cards</h2><div id="cards">Loading…</div></section><script>
const pal=[[0,0,0],[15,15,15],[0,0,15],[0,15,15],[0,15,0],[15,15,0],[15,0,0],[15,0,15]], palNames=["Black","White","Blue","Cyan","Green","Yellow","Red","Magenta"], cv=document.querySelector('#draw'),ctx=cv.getContext('2d');let pixels=Array(1024).fill(0),color=1,cursorX=0,cursorY=0;function paint(){ctx.imageSmoothingEnabled=false;for(let i=0;i<1024;i++){let c=pal[pixels[i]];ctx.fillStyle=`rgb(${c.map(x=>x*17).join(',')})`;ctx.fillRect(i%32,Math.floor(i/32),1,1)}if(document.activeElement===cv){ctx.strokeStyle="#fff";ctx.lineWidth=.18;ctx.strokeRect(cursorX+.1,cursorY+.1,.8,.8)}}paint();document.querySelector('#colors').innerHTML=pal.map((c,i)=>`<button class="sw ${i===color?'on':''}" style="background:rgb(${c.map(x=>x*17).join(',')})" data-i="${i}" aria-label="${palNames[i]}, palette color ${i+1}" aria-pressed="${i===color}"></button>`).join('');function announceCursor(){document.querySelector("#draw-position").textContent="Cursor at row "+(cursorY+1)+", column "+(cursorX+1)+"; selected "+palNames[color]+", color "+(color+1)+"."}document.querySelectorAll(".sw").forEach(b=>b.onclick=()=>{color=+b.dataset.i;document.querySelectorAll(".sw").forEach(x=>{let selected=x===b;x.classList.toggle("on",selected);x.setAttribute("aria-pressed",String(selected))});announceCursor()});cv.onkeydown=e=>{let move={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]}[e.key];if(move){e.preventDefault();cursorX=Math.max(0,Math.min(31,cursorX+move[0]));cursorY=Math.max(0,Math.min(31,cursorY+move[1]));announceCursor();paint()}else if(e.key===" "||e.key==="Enter"){e.preventDefault();pixels[cursorY*32+cursorX]=color;announceCursor();paint()}};cv.onfocus=()=>{announceCursor();paint()};cv.onblur=paint;let down=false;function draw(e){if(!down)return;let r=cv.getBoundingClientRect(),x=Math.max(0,Math.min(31,Math.floor((e.clientX-r.left)*32/r.width))),y=Math.max(0,Math.min(31,Math.floor((e.clientY-r.top)*32/r.height)));cursorX=x;cursorY=y;pixels[y*32+x]=color;paint()}cv.onpointerdown=e=>{down=true;cv.setPointerCapture(e.pointerId);draw(e)};cv.onpointermove=draw;cv.onpointerup=()=>down=false;document.querySelector('#clear').onclick=()=>{pixels.fill(0);paint()};async function api(path,body){let r=await fetch('/api/'+path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});let d=await r.json();if(!r.ok)throw Error(d.error||r.statusText);return d}function stageLabel(s){return ({card:"Card",cq:"CQ",exchange:"Exchange",report73:"RR73 with report",final73:"Final 73"})[s]||"Exchange"}async function refresh(){try{let s=await api('status');document.querySelector('#state').textContent=`${s.callsign} · ${s.server} · ${s.transmitting?'Transmitting':'Ready'}`;let stageSelect=document.querySelector('#stage');stageSelect.value=s.stage;stageSelect.setAttribute('aria-label','Transmit stage: '+stageLabel(s.stage));let c=await api('cards');document.querySelector('#cards').innerHTML=c.cards.map((x,n)=>`<article><b>${esc(x.callsign)} ${esc(x.grid||'')}</b> · ${esc(x.source)}<div class="muted">${x.width}×${x.height} · ${esc(x.message_type||'card')}</div><canvas id="card${n}" width="${x.width}" height="${x.height}"></canvas></article>`).join('')||'No cards yet';c.cards.forEach((x,n)=>{let el=document.querySelector('#card'+n),g=el.getContext('2d');x.pixels.forEach((p,i)=>{let q=x.palette[p];g.fillStyle=`rgb(${q.map(v=>v*17).join(',')})`;g.fillRect(i%x.width,Math.floor(i/x.width),1,1)})})}catch(e){document.querySelector('#state').textContent=e.message}}function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}document.querySelector('#send').onclick=async()=>{try{await api('transmit',{});alert('Transmit requested')}catch(e){alert(e.message)}};document.querySelector('#stop').onclick=async()=>{try{await api('stop',{});refresh()}catch(e){alert(e.message)}};document.querySelector('#stage').onchange=async e=>{try{await api('stage',{stage:e.target.value})}catch(error){alert(error.message);refresh()}};document.querySelector('#use').onclick=async()=>{try{await api('quickdraw',{callsign:document.querySelector('#call').value,grid:document.querySelector('#grid').value,width:32,height:32,palette:pal,pixels});alert('Quick draw loaded. Transmit selected stage when ready.')}catch(e){alert(e.message)}};refresh();setInterval(refresh,5000);
</script></body></html>'''

for _token, _color in {
    "__WINDOW__": COLORS["window"],
    "__TEXT__": COLORS["text"],
    "__TEXT_BRIGHT__": COLORS["text_bright"],
    "__TEXT_PRIMARY__": COLORS["text_primary"],
    "__TEXT_MUTED__": COLORS["text_muted"],
    "__SURFACE__": COLORS["surface"],
    "__SURFACE_RAISED__": COLORS["surface_raised"],
    "__BORDER_CONTROL__": COLORS["border_control"],
    "__BORDER_BUTTON__": COLORS["border_button"],
    "__ACCENT__": COLORS["accent"],
    "__FOCUS_RING__": COLORS["focus_ring"],
}.items():
    PAGE = PAGE.replace(_token, _color)
PAGE = PAGE.replace("__BUTTON_RADIUS__", str(RADIUS["button"]))
PAGE = PAGE.replace("__PANEL_RADIUS__", str(RADIUS["panel"]))
PAGE = PAGE.replace("__CONTROL_RADIUS__", str(RADIUS["control"]))


class CompanionServer:
    def __init__(self, dispatch, port=8765):
        self.dispatch = dispatch
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def reply(self, status, data, content_type="application/json"):
                body = data.encode() if isinstance(data, str) else data if isinstance(data, bytes) else json.dumps(data).encode()
                self.send_response(status); self.send_header("Content-Type", content_type+"; charset=utf-8")
                self.send_header("Content-Length", str(len(body))); self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type"); self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.end_headers(); self.wfile.write(body)
            def do_OPTIONS(self): self.reply(204, b"")
            def do_GET(self):
                if self.path == "/" or self.path.startswith("/app"):
                    self.reply(200, PAGE, "text/html")
                elif self.path.startswith("/api/"): self.dispatch_api("GET", self.path[5:], {})
                else: self.reply(404, {"error":"Not found"})
            def do_POST(self):
                if not self.path.startswith("/api/"): return self.reply(404,{"error":"Not found"})
                try: data=json.loads(self.rfile.read(int(self.headers.get("Content-Length",0)) or 0) or b"{}")
                except (ValueError, json.JSONDecodeError): return self.reply(400,{"error":"Invalid JSON"})
                self.dispatch_api("POST",self.path[5:],data)
            def dispatch_api(self,method,path,data):
                try: self.reply(200,owner.dispatch(method,path,data))
                except TimeoutError as e: self.reply(504,{"error":str(e)})
                except (ValueError,KeyError) as e: self.reply(400,{"error":str(e)})
                except Exception as e: self.reply(409,{"error":str(e)})
        self.server = ThreadingHTTPServer(("0.0.0.0", int(port)), Handler)
        self.thread = Thread(target=self.server.serve_forever, name="PixelQSO web server", daemon=True)
        self.thread.start()
    @property
    def port(self): return self.server.server_address[1]
    def close(self): self.server.shutdown(); self.server.server_close()
