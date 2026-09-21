import json, time, hmac, hashlib, threading, queue
from datetime import datetime, timedelta
import requests
import websocket

from kivy.app import App
from kivy.clock import Clock
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.label import Label
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView

REST_URL = "https://api.india.delta.exchange"
WS_URL = "wss://public-socket.india.delta.exchange"

KV = r'''
#:import dp kivy.metrics.dp
<Root>:
    orientation: 'vertical'
    spacing: dp(6)
    padding: dp(6)
    ScrollView:
        do_scroll_x: False
        bar_width: dp(8)
        GridLayout:
            id: content
            cols: 1
            spacing: dp(6)
            size_hint_y: None
            padding: dp(2)
            height: self.minimum_height
'''
Builder.load_string(KV)


def sign_request(secret, method, path, query_string="", body=""):
    ts = str(int(time.time()))
    msg = method + ts + path + query_string + body
    sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return ts, sig


def fmt_qty(v):
    try:
        f = float(v)
        return str(int(f)) if f.is_integer() else f"{f:g}"
    except Exception:
        return str(v)


class DeltaClient:
    def __init__(self, key, secret):
        self.key, self.secret = key.strip(), secret.strip()

    def request(self, method, path, params=None, payload=None):
        params, payload = params or {}, payload or {}
        body = json.dumps(payload, separators=(",", ":")) if payload else ""
        query = "?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else ""
        ts, sig = sign_request(self.secret, method.upper(), path, query, body)
        headers = {"Accept":"application/json","Content-Type":"application/json",
                   "api-key":self.key,"timestamp":ts,"signature":sig,
                   "User-Agent":"DeltaDualTrader-Android/1.0"}
        r = requests.request(method.upper(), REST_URL+path, params=params,
                             data=body or None, headers=headers, timeout=12)
        try: data = r.json()
        except Exception: data = None
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {data or r.text[:300]}")
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(str(data))
        return data

    def tickers(self, expiry=None):
        p = {"contract_types":"call_options,put_options", "underlying_asset_symbols":"BTC"}
        if expiry: p["expiry_date"] = expiry
        return self.request("GET", "/v2/tickers", params=p)

    def order(self, pid, symbol, size, side, sl=None, target=None, limit=None):
        p={"product_id":int(pid),"product_symbol":symbol,"size":int(size),
           "side":side,"order_type":"market_order"}
        if sl is not None:
            p["bracket_stop_loss_price"]=str(sl); p["bracket_stop_trigger_method"]="mark_price"
        if target is not None:
            p["bracket_take_profit_price"]=str(target); p["bracket_stop_trigger_method"]="mark_price"
            if limit is not None: p["bracket_take_profit_limit_price"]=str(limit)
        return self.request("POST","/v2/orders",payload=p)

    def positions(self):
        return self.request("GET","/v2/positions",params={"underlying_asset_symbol":"BTC"})

    def close(self,pid,symbol,size,pos_side):
        side="sell" if str(pos_side).lower()=="long" else "buy"
        p={"product_id":int(pid),"product_symbol":symbol,"size":int(abs(size)),
           "side":side,"order_type":"market_order","reduce_only":True}
        return self.request("POST","/v2/orders",payload=p)


class Root(BoxLayout):
    pass


class MobileTrader(App):
    status = StringProperty("Not connected")
    def build(self):
        self.title = "Delta Dual Trader Mobile"
        self.root = Root()
        self.c = self.root.ids.content
        self.clients=[]; self.chain=[]; self.expiries=[]; self.marks={}
        self.selected=None; self.ws=None; self.stop_ws=threading.Event(); self.q=queue.Queue()
        self.qty_mode=""; self.auto_qty="1"; self.position_busy=False
        self._build_ui()
        Clock.schedule_interval(self._drain, .2)
        Clock.schedule_interval(self._position_tick, 2.0)
        return self.root

    def title_label(self, text):
        l=Label(text=text,size_hint_y=None,height=dp(34),font_size='18sp',bold=True,halign='left',valign='middle')
        l.bind(size=lambda x,_: setattr(x,'text_size',(x.width,None)))
        return l
    def row(self, widgets, h=dp(44)):
        b=BoxLayout(size_hint_y=None,height=h,spacing=dp(5))
        for w in widgets:b.add_widget(w)
        self.c.add_widget(b); return b
    def lab(self,t,w=.35): return Label(text=t,size_hint_x=w,font_size='14sp',halign='left',valign='middle')
    def inp(self, hint='', w=.65, password=False):
        return TextInput(hint_text=hint,multiline=False,size_hint_x=w,password=password,font_size='14sp')
    def btn(self,t,w=1): return Button(text=t,size_hint_x=w,font_size='14sp')

    def _build_ui(self):
        self.c.add_widget(self.title_label('1. API ACCOUNTS'))
        self.main_key=self.inp('Main API Key'); self.main_sec=self.inp('Main API Secret',password=True)
        self.row([self.lab('Main Key',.25),self.main_key]); self.row([self.lab('Main Secret',.25),self.main_sec])
        r=self.row([self.lab('Main enabled',.45)]); self.main_en=CheckBox(active=True,size_hint_x=.12); r.add_widget(self.main_en)
        self.sub_box=BoxLayout(orientation='vertical',size_hint_y=None); self.sub_box.bind(minimum_height=self.sub_box.setter('height')); self.c.add_widget(self.sub_box)
        self.subs=[]; self.add_sub()
        self.row([self.btn('+ ADD SUB ACCOUNT',.5),self.btn('REMOVE LAST',.5)])
        # reconnect button wired separately because row returns container
        last=self.c.children[0]
        # children order is reversed; find buttons by traversal below
        for child in self.c.children:
            if isinstance(child,BoxLayout):
                for w in child.children:
                    if isinstance(w,Button) and w.text=='+ ADD SUB ACCOUNT': w.bind(on_release=lambda *_: self.add_sub())
                    if isinstance(w,Button) and w.text=='REMOVE LAST': w.bind(on_release=lambda *_: self.remove_sub())
        self.row([self.btn('CONNECT / LOAD',1)])
        self._find_button('CONNECT / LOAD').bind(on_release=lambda *_: self.connect())

        self.c.add_widget(self.title_label('2. BTC OPTION SELECTION'))
        self.expiry=Spinner(text='Loading...',values=(),size_hint_x=.6,font_size='14sp'); self.row([self.lab('Expiry',.35),self.expiry])
        self.option=Spinner(text='CALL',values=('CALL','PUT'),size_hint_x=.6,font_size='14sp'); self.row([self.lab('Option',.35),self.option])
        self.side=Spinner(text='SELL',values=('BUY','SELL'),size_hint_x=.6,font_size='14sp'); self.row([self.lab('Buy / Sell',.35),self.side])
        self.auto_btn=self.btn('AUTO',.45); self.manual_btn=self.btn('MANUAL',.45); self.auto_qty_box=self.inp('AUTO qty',.25); self.manual_qty_box=self.inp('Manual qty',.25)
        r=self.row([self.lab('Quantity mode',.3),self.auto_btn,.08*self.manual_btn if False else self.manual_btn])
        self.auto_btn.bind(on_release=lambda *_: self.set_qty_mode('auto')); self.manual_btn.bind(on_release=lambda *_: self.set_qty_mode('manual'))
        self.row([self.lab('AUTO Qty',.35),self.auto_qty_box]); self.auto_qty_box.readonly=True
        self.row([self.lab('Manual Qty',.35),self.manual_qty_box])
        self.live=CheckBox(active=False,size_hint_x=.12); self.row([self.lab('I understand LIVE orders',.88),self.live])
        self.sl=CheckBox(active=False,size_hint_x=.12); self.sl_price=self.inp('SL Mark Price',.6); self.row([self.lab('SL ENABLE',.3),self.sl,self.sl_price])
        self.target=CheckBox(active=False,size_hint_x=.12); self.t_trigger=self.inp('Target Trigger',.42); self.t_limit=self.inp('Target Limit optional',.42); self.row([self.lab('TARGET',.22),self.target,self.t_trigger]); self.row([self.lab('Target Limit',.22),self.t_limit])
        r=self.row([self.btn('REFRESH CHAIN',1)]); self._find_button('REFRESH CHAIN').bind(on_release=lambda *_: self.refresh_chain())

        self.c.add_widget(self.title_label('3. ELIGIBLE BTC OPTIONS'))
        self.table=BoxLayout(orientation='vertical',size_hint_y=None); self.table.bind(minimum_height=self.table.setter('height')); self.c.add_widget(self.table)
        self.c.add_widget(self.title_label('4. LIVE POSITIONS + UPNL'))
        r=self.row([self.btn('REFRESH ALL POSITIONS',1)]); self._find_button('REFRESH ALL POSITIONS').bind(on_release=lambda *_: self.refresh_positions())
        self.pos_box=BoxLayout(orientation='vertical',size_hint_y=None); self.pos_box.bind(minimum_height=self.pos_box.setter('height')); self.c.add_widget(self.pos_box)
        self.c.add_widget(self.title_label('5. ORDER'))
        self.selected_label=self.lab('Selected: none',1); self.selected_label.font_size='15sp'; self.selected_label.bold=True; self.c.add_widget(self.selected_label)
        r=self.row([self.btn('PLACE MARKET ORDER',.5),self.btn('CLOSE ALL POSITIONS',.5)])
        for w in r.children:
            if w.text=='PLACE MARKET ORDER': w.bind(on_release=lambda *_: self.place_orders())
            if w.text=='CLOSE ALL POSITIONS': w.bind(on_release=lambda *_: self.close_all())
        self.c.add_widget(self.title_label('6. ONE-TIME SCHEDULED ORDER'))
        self.sh=self.inp('HH',.2); self.sm=self.inp('MM',.2); self.ss=self.inp('SS',.2); self.sbtn=self.btn('SCHEDULE',.4)
        self.row([self.lab('Time 24h',.25),self.sh,self.sm,self.ss,self.sbtn]); self.sbtn.bind(on_release=lambda *_: self.schedule())
        self.status_label=Label(text='Not connected',size_hint_y=None,height=dp(44),font_size='13sp',halign='left',valign='middle'); self.status_label.bind(size=lambda x,_: setattr(x,'text_size',(x.width,None))); self.c.add_widget(self.status_label)

    def _find_button(self,text):
        for ch in self.c.children:
            if isinstance(ch,BoxLayout):
                for w in ch.children:
                    if isinstance(w,Button) and w.text==text:return w
        return None
    def add_sub(self):
        idx=len(self.subs)+1; b=BoxLayout(size_hint_y=None,height=dp(100),orientation='vertical',spacing=dp(3))
        top=BoxLayout(size_hint_y=None,height=dp(40)); en=CheckBox(active=True,size_hint_x=.12); name=self.inp(f'Sub {idx} name',.35); top.add_widget(en); top.add_widget(name); b.add_widget(top)
        key=self.inp('Sub API Key'); sec=self.inp('Sub API Secret',password=True); b.add_widget(key); b.add_widget(sec); self.sub_box.add_widget(b); self.subs.append((b,en,name,key,sec))
    def remove_sub(self):
        if len(self.subs)>1:
            b,*_=self.subs.pop(); self.sub_box.remove_widget(b)

    def set_status(self,s): self.status=s; self.status_label.text=s
    def set_qty_mode(self,m):
        self.qty_mode=m; self.auto_btn.text='AUTO'; self.manual_btn.text='MANUAL';
        if m=='auto': self.auto_btn.text='AUTO ✓'
        else:self.manual_btn.text='MANUAL ✓'

    def connect(self):
        accounts=[]
        if self.main_en.active and self.main_key.text.strip() and self.main_sec.text.strip(): accounts.append(('MAIN',DeltaClient(self.main_key.text,self.main_sec.text)))
        for b,en,name,key,sec in self.subs:
            if en.active and key.text.strip() and sec.text.strip(): accounts.append((name.text.strip() or 'SUB',DeltaClient(key.text,sec.text)))
        if not accounts: return self.popup('Connect','Enable at least one account and enter its API key/secret.')
        self.clients=accounts; self.set_status('Connecting...')
        threading.Thread(target=self._connect_worker,daemon=True).start()
    def _connect_worker(self):
        try:
            data=self.clients[0][1].tickers(); self.q.put(('chain',data)); self.q.put(('ok','Connected'))
        except Exception as e:self.q.put(('err',str(e)))

    def refresh_chain(self):
        if not self.clients:return
        expiry=self.expiry.text
        threading.Thread(target=lambda:self._chain_worker(expiry),daemon=True).start()
    def _chain_worker(self,expiry):
        try:self.q.put(('chain',self.clients[0][1].tickers(expiry if expiry!='Loading...' else None)))
        except Exception as e:self.q.put(('err',str(e)))

    def parse_chain(self,data):
        rows=data.get('result',data) if isinstance(data,dict) else data
        if isinstance(rows,dict): rows=rows.get('result',[]) or rows.get('data',[])
        rows=rows or []
        out=[]; ex=set()
        for x in rows:
            sym=x.get('symbol') or x.get('product_symbol') or ''
            if not sym.startswith(('C-BTC-','P-BTC-')):continue
            try: strike=float(x.get('strike_price') or sym.split('-')[2]); pid=int(x.get('product_id') or x.get('id')); mark=float(x.get('mark_price') or x.get('mark') or x.get('close'))
            except Exception: continue
            typ='CALL' if sym.startswith('C-') else 'PUT'; code=sym.split('-')[-1]
            if len(code)==6 and code.isdigit(): exp=f'{code[:2]}-{code[2:4]}-20{code[4:]}'
            else: exp=''
            if exp:ex.add(exp)
            out.append({'type':typ,'strike':strike,'mark':mark,'symbol':sym,'product_id':pid,'expiry':exp})
        self.chain=out; self.expiries=sorted(ex,key=lambda d:datetime.strptime(d,'%d-%m-%Y'))
        self.q.put(('parsed',None))

    def render_chain(self):
        self.table.clear_widgets(); typ=self.option.text
        if self.expiries:
            old=self.expiry.text
            if old not in self.expiries:
                today=datetime.now().date(); future=[d for d in self.expiries if datetime.strptime(d,'%d-%m-%Y').date()>=today]
                self.expiry.text=future[0] if future else self.expiries[-1]
        cand=[]
        for r in self.chain:
            if r['type']==typ and (not self.expiry.text or r['expiry']==self.expiry.text) and r['mark']>184:cand.append(r)
        cand.sort(key=lambda r:abs(r['mark']-200))
        header=BoxLayout(size_hint_y=None,height=dp(38));
        for t in ('Type','Strike','Mark Price'): header.add_widget(Label(text=t,bold=True,font_size='14sp'))
        self.table.add_widget(header)
        if not cand:
            self.table.add_widget(Label(text='No qualifying option (> $184)',size_hint_y=None,height=dp(40))); return
        r=cand[0]; self.selected=r; self.auto_qty=str(max(1,int(round(((208/(r['mark']-10))*1000)*2.5)))) if r['mark']>10 else '1'; self.auto_qty_box.text=self.auto_qty
        row=BoxLayout(size_hint_y=None,height=dp(44));
        for t in (r['type'],f"{r['strike']:,.0f}",f"${r['mark']:.2f}"): row.add_widget(Label(text=t,font_size='14sp'))
        self.table.add_widget(row); self.selected_label.text=f"Selected: {r['symbol']} | Mark ${r['mark']:.2f} | Qty {self.active_qty()}"

    def active_qty(self):
        if self.qty_mode=='auto': return int(self.auto_qty or 1)
        try:return int(self.manual_qty_box.text)
        except:return 0

    def place_orders(self):
        if not self.live.active:return self.popup('LIVE confirmation','Tick LIVE orders checkbox first.');
        if not self.selected:return self.popup('Order','Select/load an option first.')
        if not self.qty_mode:return self.popup('Quantity','Select AUTO or MANUAL.')
        qty=self.active_qty()
        if qty<=0:return self.popup('Quantity','Enter a positive quantity.')
        sl=None; target=None; limit=None
        if self.sl.active:
            try:sl=float(self.sl_price.text)
            except:return self.popup('SL','Enter valid SL Mark Price.')
        if self.target.active:
            try:target=float(self.t_trigger.text); limit=float(self.t_limit.text) if self.t_limit.text.strip() else None
            except:return self.popup('Target','Enter valid target prices.')
        side=self.side.text.lower(); r=self.selected
        threading.Thread(target=self._order_worker,args=(r,qty,side,sl,target,limit),daemon=True).start(); self.set_status('Sending orders...')
    def _order_worker(self,r,qty,side,sl,target,limit):
        out=[]
        for name,c in self.clients:
            try:c.order(r['product_id'],r['symbol'],qty,side,sl,target,limit); out.append(f'{name}: ORDER ACCEPTED')
            except Exception as e:out.append(f'{name}: FAILED {e}')
        self.q.put(('msg','\n'.join(out)))

    def refresh_positions(self):
        if self.position_busy or not self.clients:return
        self.position_busy=True; threading.Thread(target=self._pos_worker,daemon=True).start()
    def _position_tick(self,*_):
        if self.clients:self.refresh_positions()
    def _pos_worker(self):
        try:
            allp=[]
            for name,c in self.clients:
                data=c.positions(); rows=data.get('result',data) if isinstance(data,dict) else data; rows=rows or []
                if isinstance(rows,dict): rows=rows.get('positions',rows.get('result',[]))
                for p in rows:
                    size=float(p.get('size') or 0)
                    if abs(size)>0: allp.append((name,c,p))
            self.q.put(('positions',allp))
        except Exception as e:self.q.put(('err',f'Positions: {e}'))
        finally:self.position_busy=False

    def render_positions(self,items):
        self.pos_box.clear_widgets()
        hdr=BoxLayout(size_hint_y=None,height=dp(38))
        for t in ('Account','Contract','Side','Qty','Entry','Mark','UPNL','Action'):hdr.add_widget(Label(text=t,bold=True,font_size='12sp'))
        self.pos_box.add_widget(hdr)
        if not items:self.pos_box.add_widget(Label(text='No open BTC positions',size_hint_y=None,height=dp(42)));return
        for name,c,p in items:
            sym=p.get('product_symbol') or p.get('symbol') or str(p.get('product_id',''))
            size=float(p.get('size') or 0); side=p.get('side') or ('long' if size>0 else 'short'); entry=float(p.get('entry_price') or p.get('average_entry_price') or 0)
            # Current position response can expose mark_price; use it directly for display/UPNL.
            mark=p.get('mark_price') or p.get('mark') or 0
            try: mark=float(mark)
            except: mark=0
            qty=abs(size); upnl=(mark-entry)*qty if str(side).lower()=='long' else (entry-mark)*qty
            row=BoxLayout(size_hint_y=None,height=dp(52))
            vals=(name,sym,str(side).upper(),fmt_qty(qty),f'{entry:.2f}',f'{mark:.2f}',f'{upnl:.2f}')
            for v in vals:row.add_widget(Label(text=v,font_size='11sp'))
            b=Button(text='CLOSE',font_size='11sp'); b.bind(on_release=lambda *_n,n=name,cl=c,pp=p:self.close_one(n,cl,pp));row.add_widget(b);self.pos_box.add_widget(row)
    def close_one(self,name,c,p):
        try:c.close(int(p.get('product_id')),p.get('product_symbol') or p.get('symbol'),abs(int(float(p.get('size')))),p.get('side') or ('long' if float(p.get('size'))>0 else 'short'));self.popup('Close',f'{name}: close order sent')
        except Exception as e:self.popup('Close error',str(e))
    def close_all(self):
        # Refresh first; then close currently open positions.
        if not self.clients:return
        def w():
            lines=[]
            for name,c in self.clients:
                try:
                    data=c.positions(); rows=data.get('result',data) if isinstance(data,dict) else data; rows=rows or []
                    if isinstance(rows,dict):rows=rows.get('positions',rows.get('result',[]))
                    for p in rows:
                        if abs(float(p.get('size') or 0))>0:
                            c.close(int(p.get('product_id')),p.get('product_symbol') or p.get('symbol'),abs(int(float(p.get('size')))),p.get('side') or ('long' if float(p.get('size'))>0 else 'short'));lines.append(f'{name}: closed {p.get("symbol",p.get("product_symbol","position"))}')
                except Exception as e:lines.append(f'{name}: {e}')
            self.q.put(('msg','\n'.join(lines) or 'No open positions'))
        threading.Thread(target=w,daemon=True).start()

    def schedule(self):
        if not self.clients or not self.selected:return self.popup('Schedule','Connect and select an option first.')
        try:h,m,s=map(int,(self.sh.text,self.sm.text,self.ss.text)); assert 0<=h<24 and 0<=m<60 and 0<=s<60
        except:return self.popup('Schedule','Enter HH MM SS in 24-hour format.')
        target=datetime.now().replace(hour=h,minute=m,second=s,microsecond=0)
        if target<=datetime.now():target+=timedelta(days=1)
        self.set_status('Scheduled for '+target.strftime('%Y-%m-%d %H:%M:%S'))
        def w():
            while datetime.now()<target:time.sleep(.05)
            # Re-read current selected contract and mark at execution time.
            self.refresh_chain(); time.sleep(.3)
            self.q.put(('schedule_ready',None))
        threading.Thread(target=w,daemon=True).start()

    def popup(self,title,msg):
        Popup(title=title,content=Label(text=str(msg)),size_hint=(.9,.35)).open()
    def _drain(self,dt):
        while True:
            try:k,v=self.q.get_nowait()
            except queue.Empty:break
            if k=='chain':self.parse_chain(v)
            elif k=='parsed':self.render_chain()
            elif k=='ok':self.set_status(v); self.refresh_chain()
            elif k=='err':self.set_status(v); self.popup('Error',v)
            elif k=='msg':self.set_status('Completed'); self.popup('Result',v)
            elif k=='positions':self.render_positions(v)
            elif k=='schedule_ready': self.place_orders()

    def on_stop(self):
        self.stop_ws.set()
        try:
            if self.ws:self.ws.close()
        except:pass


if __name__=='__main__': MobileTrader().run()
