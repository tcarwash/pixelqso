"""Optional companion mobile web interface and local control API."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import json


PAGE = r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><meta name="theme-color" content="#11161a"><title>Pixel QSO</title><style>
*{box-sizing:border-box}body{margin:0;padding:16px;background:#11161a;color:#e8eee9;font:16px system-ui;max-width:720px;margin:auto}h1{font-size:22px}button,select{background:#25333b;color:#eff7f1;border:1px solid #496158;border-radius:9px;padding:12px;font-size:16px}button.primary{background:#a0dfc0;color:#142017;font-weight:bold;width:100%}.row{display:flex;gap:8px;margin:10px 0}.row>*{flex:1}.panel{background:#192126;border:1px solid #35464e;border-radius:12px;padding:14px;margin:12px 0}canvas{width:100%;max-width:480px;aspect-ratio:1;image-rendering:pixelated;touch-action:none;background:#000;display:block;margin:12px auto}.colors{display:flex;flex-wrap:wrap;gap:8px}.sw{width:38px;height:38px;border:2px solid #52635e;border-radius:8px}.sw.on{outline:3px solid #a0dfc0}article{border-bottom:1px solid #35464e;padding:10px 0}article canvas{max-width:220px}.muted{color:#9eada3;font-size:14px}</style></head><body><h1>Pixel QSO</h1><div id="state" class="muted">Connecting…</div><section class="panel"><h2>Transmit</h2><div class="row"><button id="stage">Card</button><button id="stop">Stop TX</button></div><button class="primary" id="send">Transmit selected stage</button><p class="muted">Uses the desktop station's selected mode, card, and radio settings.</p></section><section class="panel"><h2>Quick draw</h2><div class="row"><label>Call sign <input id="call" maxlength="12" value="N0CALL"></label><label>Grid <input id="grid" maxlength="8" value="AA00"></label></div><div class="colors" id="colors"></div><canvas id="draw" width="32" height="32"></canvas><div class="row"><button id="clear">Clear</button><button class="primary" id="use">Use quick draw</button></div><p class="muted">Draw by touch or mouse. “Use quick draw” makes it the next selected-stage transmission.</p></section><section class="panel"><h2>Cards</h2><div id="cards">Loading…</div></section><script>
const pal=[[0,0,0],[15,15,15],[0,0,15],[0,15,15],[0,15,0],[15,15,0],[15,0,0],[15,0,15]], cv=document.querySelector('#draw'),ctx=cv.getContext('2d');let pixels=Array(1024).fill(0),color=1;function paint(){ctx.imageSmoothingEnabled=false;for(let i=0;i<1024;i++){let c=pal[pixels[i]];ctx.fillStyle=`rgb(${c.map(x=>x*17).join(',')})`;ctx.fillRect(i%32,Math.floor(i/32),1,1)}}paint();document.querySelector('#colors').innerHTML=pal.map((c,i)=>`<button class="sw ${i===color?'on':''}" style="background:rgb(${c.map(x=>x*17).join(',')})" data-i="${i}"></button>`).join('');document.querySelectorAll('.sw').forEach(b=>b.onclick=()=>{color=+b.dataset.i;document.querySelectorAll('.sw').forEach(x=>x.classList.toggle('on',x===b))});let down=false;function draw(e){if(!down)return;let r=cv.getBoundingClientRect(),x=Math.max(0,Math.min(31,Math.floor((e.clientX-r.left)*32/r.width))),y=Math.max(0,Math.min(31,Math.floor((e.clientY-r.top)*32/r.height)));pixels[y*32+x]=color;paint()}cv.onpointerdown=e=>{down=true;cv.setPointerCapture(e.pointerId);draw(e)};cv.onpointermove=draw;cv.onpointerup=()=>down=false;document.querySelector('#clear').onclick=()=>{pixels.fill(0);paint()};async function api(path,body){let r=await fetch('/api/'+path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});let d=await r.json();if(!r.ok)throw Error(d.error||r.statusText);return d}async function refresh(){try{let s=await api('status');document.querySelector('#state').textContent=`${s.callsign} · ${s.server} · ${s.transmitting?'Transmitting':'Ready'}`;let stageButton=document.querySelector('#stage');stageButton.dataset.stage=s.stage;stageButton.textContent=s.stage==='report73'?'73 · with SNR':s.stage==='final73'?'73 · final':s.stage;let c=await api('cards');document.querySelector('#cards').innerHTML=c.cards.map((x,n)=>`<article><b>${esc(x.callsign)} ${esc(x.grid||'')}</b> · ${esc(x.source)}<div class="muted">${x.width}×${x.height} · ${esc(x.message_type||'card')}</div><canvas id="card${n}" width="${x.width}" height="${x.height}"></canvas></article>`).join('')||'No cards yet';c.cards.forEach((x,n)=>{let el=document.querySelector('#card'+n),g=el.getContext('2d');x.pixels.forEach((p,i)=>{let q=x.palette[p];g.fillStyle=`rgb(${q.map(v=>v*17).join(',')})`;g.fillRect(i%x.width,Math.floor(i/x.width),1,1)})})}catch(e){document.querySelector('#state').textContent=e.message}}function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}document.querySelector('#send').onclick=async()=>{try{await api('transmit',{});alert('Transmit requested')}catch(e){alert(e.message)}};document.querySelector('#stop').onclick=async()=>{try{await api('stop',{});refresh()}catch(e){alert(e.message)}};document.querySelector('#stage').onclick=async()=>{let a=['cq','exchange','report73','final73'],b=document.querySelector('#stage'),i=a.indexOf(b.dataset.stage||'exchange'),stage=a[(i+1)%a.length];try{await api('stage',{stage});b.dataset.stage=stage;b.textContent=stage==='report73'?'73 · with SNR':stage==='final73'?'73 · final':stage}catch(e){alert(e.message)}};document.querySelector('#use').onclick=async()=>{try{await api('quickdraw',{callsign:document.querySelector('#call').value,grid:document.querySelector('#grid').value,width:32,height:32,palette:pal,pixels});alert('Quick draw loaded. Transmit selected stage when ready.')}catch(e){alert(e.message)}};refresh();setInterval(refresh,5000);
</script></body></html>'''


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
