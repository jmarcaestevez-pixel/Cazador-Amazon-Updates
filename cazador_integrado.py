"""Cazador Amazon: executable Windows with GitHub-hosted updates and SHA-256."""
import base64
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlparse
import webbrowser
from datetime import datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_VERSION = '1.8.0'
MANIFEST_URL = 'https://raw.githubusercontent.com/jmarcaestevez-pixel/Cazador-Amazon-Updates/main/manifest.json'
SOURCES = [
    'https://www.amazon.es/gp/goldbox',
    'https://www.amazon.es/gp/movers-and-shakers',
    'https://www.amazon.es/gp/new-releases',
    'https://www.amazon.es/s?rh=p_8%3A70-99',
]
PORT = 8765
DATA_DIR = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'CazadorAmazon'
DB = DATA_DIR / 'precios.sqlite'

class CardParser(HTMLParser):
    """Collect fields only within the same product card, never adjacent products."""
    VOID = {'img','input','br','hr','meta','link','source','wbr','area'}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.products = {}
        self.stack = []
        self.capture = None
        self.parts = []
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        asin = (a.get('data-asin') or '').strip()
        current = self.stack[-1] if self.stack else None
        if re.fullmatch(r'[A-Z0-9]{10}', asin):
            current = asin
            self.products.setdefault(asin, {'name':None,'price':None,'image':None})
        if tag not in self.VOID:
            self.stack.append(current)
        if not current:
            return
        item = self.products[current]
        cls = a.get('class') or ''
        if tag == 'img':
            src = a.get('data-src') or a.get('src')
            if not item['image'] and src and src.startswith('https://') and ('images' in src or 'media-amazon' in src):
                item['image'] = src
            if not item['name']:
                alt = (a.get('alt') or '').strip()
                if len(alt)>12 and 'amazon' not in alt.lower()[:8]: item['name']=alt[:220]
        if tag in ('h2','h3') and not item['name']:
            self.capture = (current,'name',tag,len(self.stack))
            self.parts=[]
        elif tag == 'span' and ('a-offscreen' in cls or 'priceToPay' in cls) and item['price'] is None:
            self.capture = (current,'price',tag,len(self.stack))
            self.parts=[]
    def handle_data(self, data):
        if self.capture: self.parts.append(data)
    def handle_endtag(self, tag):
        if self.capture and tag == self.capture[2] and len(self.stack)==self.capture[3]:
            asin,field,_,_ = self.capture
            value = ' '.join(''.join(self.parts).split())
            if field=='name' and len(value)>5: self.products[asin]['name']=value[:220]
            if field=='price' and ('€' in value or 'EUR' in value):
                price=parse_price(value)
                if price is not None: self.products[asin]['price']=price
            self.capture=None
        if tag not in self.VOID and self.stack:
            self.stack.pop()

def parse_products(document):
    """Support product cards and JSON-LD Product offers; never invent a price."""
    parser=CardParser()
    parser.feed(document)
    products=parser.products
    for match in re.finditer(r"<script\b[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",document,re.I|re.S):
        try: payload=json.loads(match.group(1))
        except (ValueError,TypeError): continue
        def walk(node):
            if isinstance(node,list):
                for part in node: walk(part)
            elif isinstance(node,dict):
                typ=node.get('@type',[])
                if isinstance(typ,str): typ=[typ]
                if 'Product' in typ:
                    url=str(node.get('url') or '')
                    asin=re.search(r'(?:/dp/|/gp/product/)([A-Z0-9]{10})',url)
                    if asin:
                        asin=asin.group(1)
                        item=products.setdefault(asin,{'name':None,'price':None,'image':None})
                        name=node.get('name')
                        if isinstance(name,str) and name.strip(): item['name']=name.strip()[:220]
                        image=node.get('image')
                        if isinstance(image,list): image=image[0] if image else None
                        if isinstance(image,str) and image.startswith('https://'): item['image']=image
                        offers=node.get('offers')
                        if isinstance(offers,list): offers=offers[0] if offers else None
                        if isinstance(offers,dict) and str(offers.get('priceCurrency','EUR'))=='EUR':
                            raw=offers.get('price')
                            if isinstance(raw,(str,int,float)):
                                price=parse_price(str(raw))
                                if price is not None: item['price']=price
                for child in node.values():
                    if isinstance(child,(list,dict)): walk(child)
        walk(payload)
    return products

def blocked_page(document):
    low=document.lower()
    return any(x in low for x in ('enter the characters you see below','introduce los caracteres que ves','robot check','captcha-validation'))

def parse_price(text):
    m = re.search(r'(\d[\d.,]*)', text)
    if not m: return None
    number = m.group(1)
    if ',' in number: number = number.replace('.', '').replace(',', '.')
    try:
        price = float(number)
        return price if 0 < price < 100000 else None
    except ValueError: return None

def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DB.exists():
        # Import existing 1.3 database without deleting or changing the original.
        for desktop in (Path.home()/'Desktop', Path.home()/'OneDrive'/'Desktop', Path.home()/'Escritorio', Path.home()/'OneDrive'/'Escritorio'):
            if desktop.exists():
                for folder in desktop.iterdir():
                    candidate = folder/'precios.sqlite'
                    if folder.is_dir() and 'cazador' in folder.name.lower() and candidate.is_file():
                        shutil.copy2(candidate, DB)
                        break
                if DB.exists(): break
    with sqlite3.connect(DB) as c:
        c.execute('CREATE TABLE IF NOT EXISTS items (asin text primary key,title text,price real,previous real,first_seen text,last_seen text,source text)')
        c.execute('CREATE TABLE IF NOT EXISTS runs (time text,status text,count integer)')
        columns = {row[1] for row in c.execute('PRAGMA table_info(items)')}
        if 'image' not in columns: c.execute('ALTER TABLE items ADD COLUMN image TEXT')
        c.execute('CREATE TABLE IF NOT EXISTS observations (asin TEXT, at TEXT, price REAL, PRIMARY KEY(asin,at))')
        c.execute('CREATE INDEX IF NOT EXISTS idx_observations_asin_at ON observations(asin,at)')

def fetch(url, limit=4_000_000):
    req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36','Accept-Language':'es-ES,es;q=0.9'})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read(limit+1)
        if len(data)>limit: raise ValueError('Respuesta demasiado grande')
        return data

def scan():
    found = {}; errors = []
    for url in SOURCES:
        try:
            document=fetch(url).decode('utf-8','replace')
            if blocked_page(document):
                errors.append('Amazon solicita CAPTCHA'); continue
            for asin, item in parse_products(document).items():
                if asin not in found or (item['price'] is not None and found[asin][0]['price'] is None):
                    found[asin] = (item,url)
        except Exception as e: errors.append(type(e).__name__)
    now = datetime.now().isoformat(timespec='seconds')
    with sqlite3.connect(DB) as c:
        for asin,(item,url) in found.items():
            existing = c.execute('SELECT price FROM items WHERE asin=?',(asin,)).fetchone()
            title,price,image = item['name'],item['price'],item['image']
            if existing:
                previous = existing[0] if price is not None and existing[0] != price else None
                c.execute('UPDATE items SET title=COALESCE(?,title),price=COALESCE(?,price),previous=COALESCE(?,previous),image=COALESCE(?,image),last_seen=?,source=? WHERE asin=?',(title,price,previous,image,now,url,asin))
            else:
                c.execute('INSERT INTO items (asin,title,price,previous,first_seen,last_seen,source,image) VALUES (?,?,?,?,?,?,?,?)',(asin,title or 'Producto '+asin,price,None,now,now,url,image))
            if price is not None: c.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?)',(asin,now,price))
        status = f'{len(found)} detectados, {sum(v[0]["price"] is not None for v in found.values())} con precio; errores: {", ".join(errors) or "ninguno"}'
        c.execute('INSERT INTO runs VALUES (?,?,?)',(now,status,len(found)))
    print(now,status,flush=True)

def scan_loop():
    while True:
        try: scan()
        except Exception as e: print('Error:',e,flush=True)
        time.sleep(900)

def offers_and_tracking():
    """Only observations from earlier days establish historical discounts."""
    with sqlite3.connect(DB) as c:
        c.row_factory = sqlite3.Row
        latest = c.execute('SELECT time,status FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()
        rows = c.execute('SELECT asin,title,price,image,last_seen FROM items WHERE price IS NOT NULL AND price > 0 ORDER BY last_seen DESC LIMIT 500').fetchall()
        offers, tracking = [], []
        for row in rows:
            d = dict(row)
            historical = c.execute('SELECT MAX(price) FROM observations WHERE asin=? AND substr(at,1,10)<substr(?,1,10)',(d['asin'],d['last_seen'])).fetchone()[0]
            d['historical_price'] = historical
            d['drop'] = round(100*(1-d['price']/historical),1) if historical and historical>d['price'] else None
            if d['drop'] is not None and d['drop'] >= 70 and d['title'] and not d['title'].startswith('Producto '):
                offers.append(d)
            else:
                tracking.append(d)
        offers.sort(key=lambda x:x['drop'],reverse=True)
        return (dict(latest) if latest else None), offers, tracking

class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/','/index.html'):
            self.send_error(404); return
        latest, offers, tracking = offers_and_tracking()
        def card(item, deal=False):
            asin = item['asin']
            title = html.escape(item['title'] or ('Amazon '+asin))
            image = item['image']
            pic = f'<img src="{html.escape(image,quote=True)}" loading="lazy" alt="">' if image and image.startswith('https://') else '<span class="noimg">Sin imagen</span>'
            badge = f'<strong class="deal">-{item["drop"]:.1f}% frente al precio observado anterior</strong>' if deal else '<small>En seguimiento · sin rebaja ≥70% demostrada</small>'
            old = f'<p>Precio observado anterior: {item["historical_price"]:.2f} €</p>' if deal else ''
            return f'<article>{pic}<section><h3>{title}</h3><p class="price">{item["price"]:.2f} €</p>{badge}{old}<p><a target="_blank" rel="noopener noreferrer" href="https://www.amazon.es/dp/{asin}">Ver en Amazon</a></p></section></article>'
        status = html.escape(str(latest['status'] if latest else 'Todavía no se ha completado una búsqueda'))
        when = html.escape(str(latest['time'] if latest else '—'))
        page = f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta http-equiv="refresh" content="90"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Cazador Amazon</title><style>body{{background:#101b2b;color:#f1f5ff;font:16px Arial;max-width:1050px;margin:30px auto;padding:0 18px}}h1{{color:#7de6b5}}article{{display:flex;gap:20px;background:#20324a;border-radius:12px;margin:12px 0;padding:18px}}article img,.noimg{{width:110px;height:110px;object-fit:contain;background:white;color:#445;display:grid;place-items:center;border-radius:6px;flex-shrink:0}}a{{color:#80e7b9}}small{{color:#afc3d5}}.note{{padding:14px;background:#243952;border-radius:12px}}.price{{font-size:24px;font-weight:bold}}.deal{{color:#7de6b5}}h2{{margin-top:28px}}.empty{{padding:24px;background:#20324a;border-radius:12px}}</style></head><body><h1>Cazador Amazon {APP_VERSION}</h1><div class="note">Última búsqueda: {when}<p>{status}</p><p>Las rebajas se calculan frente a observaciones del propio Cazador en días anteriores, no frente al historial completo de Amazon.</p></div><h2>Ofertas ≥70% comprobadas: {len(offers)}</h2>{''.join(card(x,True) for x in offers) if offers else '<div class="empty">0 ofertas verificadas por ahora. No se inventan precios ni descuentos.</div>'}<h2>Productos con precio en seguimiento: {len(tracking)}</h2>{''.join(card(x) for x in tracking[:80]) if tracking else '<div class="empty">Aún no hay productos con precio real. Si Amazon bloquea las consultas, aparecerá indicado arriba.</div>'}</body></html>'''
        data = page.encode('utf-8')
        self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def log_message(self,*args): pass

def newer(remote, local):
    try:
        return tuple(map(int,remote.split('.'))) > tuple(map(int,local.split('.')))
    except (ValueError,TypeError):
        return False

def check_updates():
    """Check public HTTPS release channel; replace this EXE only after hash validation.

    The trusted publisher is the fixed GitHub repository. The hash protects
    downloaded bytes, not against compromise of the publishing account.
    """
    if not getattr(sys, 'frozen', False):
        return
    try:
        manifest = json.loads(fetch(MANIFEST_URL,100_000).decode('utf-8'))
        version = str(manifest['version'])
        if not newer(version, APP_VERSION):
            return
        url = str(manifest['url'])
        parsed = urlparse(url)
        allowed = {'raw.githubusercontent.com','github.com','objects.githubusercontent.com',
                   'release-assets.githubusercontent.com'}
        if parsed.scheme != 'https' or parsed.hostname not in allowed:
            raise ValueError('Servidor de actualización no autorizado')
        digest = str(manifest['sha256']).lower()
        if not re.fullmatch('[a-f0-9]{64}',digest):
            raise ValueError('SHA-256 inválido')
        current = Path(sys.executable).resolve()
        next_exe = current.with_name(current.stem+'.nuevo.exe')
        # Do not follow arbitrary redirects to non-GitHub hosts.
        req = urllib.request.Request(url,headers={'User-Agent':'CazadorAmazon/1.6'})
        with urllib.request.urlopen(req,timeout=30) as response:
            if urlparse(response.url).hostname not in allowed:
                raise ValueError('Redirección no autorizada')
            h = hashlib.sha256();total = 0
            with open(next_exe,'wb') as out:
                while True:
                    chunk = response.read(1024*1024)
                    if not chunk: break
                    total += len(chunk)
                    if total > 100*1024*1024: raise ValueError('Archivo demasiado grande')
                    h.update(chunk);out.write(chunk)
        if h.hexdigest() != digest:
            next_exe.unlink(missing_ok=True)
            raise ValueError('SHA-256 no coincide')
        # The helper is generated and run automatically, no manual .cmd is needed.
        helper = Path(tempfile.gettempdir()) / ('cazador-reemplazar-'+str(os.getpid())+'.cmd')
        old = str(current).replace('%','%%')
        new = str(next_exe).replace('%','%%')
        helper.write_text('@echo off\n'
            'setlocal\n'
            'timeout /t 3 /nobreak >nul\n'
            'move /Y "'+new+'" "'+old+'" >nul\n'
            'if exist "'+new+'" exit /b 1\n'
            'start "" "'+old+'"\n'
            'del "%~f0"\n',encoding='utf-8')
        subprocess.Popen(['cmd.exe','/c',str(helper)],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        print('Actualización verificada; reiniciando a',version,flush=True)
        sys.exit(0)
    except Exception as exc:
        print('Actualización omitida:',exc,flush=True)

def main():
    init_db()
    check_updates()
    threading.Thread(target=scan_loop,daemon=True).start()
    threading.Timer(1.5,lambda:webbrowser.open(f'http://127.0.0.1:{PORT}')).start()
    print(f'Cazador Amazon {APP_VERSION} en http://127.0.0.1:{PORT}',flush=True)
    try:
        ThreadingHTTPServer(('127.0.0.1',PORT),Page).serve_forever()
    except OSError as exc:
        print('No se puede abrir el panel. ¿Está abierta una versión anterior?',exc,flush=True)
if __name__=='__main__': main()
