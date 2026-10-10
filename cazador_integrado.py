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

APP_VERSION = '1.6.0'
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
    def __init__(self):
        super().__init__()
        self.products = {}
        self.active = None
        self.depth = 0
        self.capture = None
        self.buffer = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        asin = attrs.get('data-asin', '')
        if re.fullmatch('[A-Z0-9]{10}', asin):
            self.active = asin
            self.products.setdefault(asin, {'name':None,'price':None,'image':None})
            self.depth = 1
        elif self.active and tag not in {'img','input','br','hr','meta','link','source','wbr','area'}:
            self.depth += 1
        if not self.active: return
        item = self.products[self.active]
        cls = attrs.get('class', '')
        if tag == 'img' and not item['image']:
            image = attrs.get('data-src') or attrs.get('src')
            if image and image.startswith('https://'):
                item['image'] = image
        if tag == 'h2' and not item['name']:
            self.capture = ('name', tag)
            self.buffer = []
        elif tag == 'span' and 'a-offscreen' in cls and item['price'] is None:
            self.capture = ('price', tag)
            self.buffer = []
    def handle_data(self, data):
        if self.capture: self.buffer.append(data)
    def handle_endtag(self, tag):
        if self.capture and tag == self.capture[1] and self.active:
            field = self.capture[0]
            value = ' '.join(''.join(self.buffer).split())
            if field == 'name' and value: self.products[self.active]['name'] = value[:220]
            if field == 'price': self.products[self.active]['price'] = parse_price(value)
            self.capture = None
        if self.active and tag not in {'img','input','br','hr','meta','link','source','wbr','area'}:
            self.depth -= 1
            if self.depth <= 0: self.active = None

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
            parser = CardParser()
            parser.feed(fetch(url).decode('utf-8','replace'))
            for asin, item in parser.products.items():
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

class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        with sqlite3.connect(DB) as c:
            rows = c.execute('SELECT asin,title,price,image,last_seen FROM items ORDER BY last_seen DESC LIMIT 300').fetchall()
            latest = c.execute('SELECT time,status FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()
        cards=[]
        for asin,title,price,image,seen in rows:
            pic = f'<img src="{html.escape(image,quote=True)}" loading="lazy">' if image and image.startswith('https://') else '📦'
            cards.append(f'<article>{pic}<section><h3>{html.escape(title or asin)}</h3><p>{f"{price:.2f} €" if price is not None else "Precio no disponible"}</p><small>{html.escape(seen)}</small><p><a target="_blank" rel="noopener noreferrer" href="https://www.amazon.es/dp/{asin}">Abrir Amazon</a></p></section></article>')
        status = html.escape(str(latest))
        page = f'''<!doctype html><html lang="es"><meta charset="utf-8"><meta http-equiv="refresh" content="60"><title>Cazador Amazon</title><style>body{{background:#101b2b;color:#f1f5ff;font:16px Arial;max-width:1050px;margin:35px auto;padding:0 18px}}h1{{color:#7de6b5}}article{{display:flex;gap:20px;background:#20324a;border-radius:12px;margin:12px 0;padding:18px}}article img{{width:110px;height:110px;object-fit:contain;background:white}}a{{color:#80e7b9}}small{{color:#afc3d5}}.note{{padding:14px;background:#243952;border-radius:12px}}</style><h1>Cazador Amazon {APP_VERSION}</h1><div class="note">Última revisión: {status}<p>Los productos detectados NO son descuentos verificados. No se inventan precios ni se anuncia un 70 % sin historial fiable.</p></div><h2>Productos: {len(rows)}</h2>{''.join(cards)}</html>'''
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
    ThreadingHTTPServer(('127.0.0.1',PORT),Page).serve_forever()
if __name__=='__main__': main()