import io
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

BASE = Path(__file__).resolve().parent
DB = Path(os.environ.get('OFFICE_DB', str(BASE / 'office.sqlite3')))
TOKEN = os.environ.get('OFFICE_TOKEN', '')
LOCK = threading.RLock()
app = FastAPI(title='Офис агентов')


def connect():
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def init():
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS records (bucket TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(bucket,id))')


init()


def put(bucket, rid, body):
    with LOCK, connect() as db:
        db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?)', (bucket, rid, json.dumps(body, ensure_ascii=False)))


def all_records(bucket):
    with connect() as db:
        return [{'id': row['id'], **json.loads(row['body'])} for row in db.execute('SELECT id,body FROM records WHERE bucket=?', (bucket,))]


def get(bucket, rid):
    return next((x for x in all_records(bucket) if x['id'] == rid), None)


def now():
    return datetime.now(timezone.utc).isoformat()


def event(stage, status, detail, run_id):
    stamp = now()
    put('agents', stage, {'status': status, 'task': detail, 'updated_at': stamp, 'run_id': run_id})
    put('log', f'{run_id}-{stage}-{stamp}', {'agent': stage, 'ts': stamp, 'text': detail, 'run_id': run_id})


@app.middleware('http')
async def auth(request: Request, call_next):
    if request.url.path.startswith('/api/') or request.url.path.startswith('/_blob/'):
        if TOKEN:
            if request.headers.get('authorization') != 'Bearer ' + TOKEN:
                return JSONResponse({'error': 'Неверный ключ'}, status_code=401)
        elif request.client and request.client.host not in ('127.0.0.1', '::1', 'testclient'):
            return JSONResponse({'error': 'Задайте OFFICE_TOKEN для сетевого доступа'}, status_code=403)
    return await call_next(request)


@app.get('/', response_class=HTMLResponse)
def home():
    return (BASE / 'index.html').read_text(encoding='utf-8')


@app.get('/manifest.webmanifest')
def manifest():
    return JSONResponse({'name': 'Офис агентов', 'short_name': 'Агенты', 'start_url': '/', 'display': 'standalone', 'background_color': '#efedea', 'theme_color': '#efedea', 'icons': [{'src': '/icon.svg', 'sizes': 'any', 'type': 'image/svg+xml', 'purpose': 'any maskable'}]}, media_type='application/manifest+json')


@app.get('/icon.svg')
def icon():
    return Response('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192"><rect width="192" height="192" rx="38" fill="#4d4a45"/><text x="96" y="126" text-anchor="middle" fill="white" font-size="106">⌂</text></svg>', media_type='image/svg+xml')


@app.get('/sw.js')
def service_worker():
    return Response((BASE / 'sw.js').read_text(encoding='utf-8'), media_type='application/javascript', headers={'Service-Worker-Allowed': '/'})


@app.get('/api/{bucket}')
def list_bucket(bucket: str):
    if bucket not in ('objects', 'reports', 'sources', 'agents', 'log', 'watch', 'requests', 'meta'):
        raise HTTPException(404)
    return all_records(bucket)


@app.put('/api/{bucket}/{rid}')
async def save_bucket(bucket: str, rid: str, request: Request):
    if bucket not in ('objects', 'sources', 'watch', 'requests', 'reports', 'deleted'):
        raise HTTPException(403)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400)
    old = get(bucket, rid) or {}
    old.update(body)
    put(bucket, rid, old)
    return {'ok': True}


@app.delete('/api/{bucket}/{rid}')
def remove_bucket(bucket: str, rid: str):
    if bucket not in ('objects', 'sources', 'watch', 'requests', 'reports', 'deleted'):
        raise HTTPException(403)
    with connect() as db:
        db.execute('DELETE FROM records WHERE bucket=? AND id=?', (bucket, rid))
    return {'ok': True}


def price_from_text(value):
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or '').lower().replace('\u00a0', ' ')
    found = re.search(r'(\d[\d\s.,]*)\s*(млн|тыс|₽|руб)?', text)
    if not found:
        return None
    numeric = found.group(1).replace(' ', '').replace(',', '.')
    try:
        val = float(numeric)
        return int(val * (1_000_000 if found.group(2) == 'млн' else 1_000 if found.group(2) == 'тыс' else 1))
    except ValueError:
        return None


def ingest(entry, origin):
    link = entry.get('link', '').strip()
    if not link.startswith(('https://', 'http://')):
        return False
    title = re.sub('<[^>]*>', ' ', entry.get('title', ''))[:250]
    summary = re.sub('<[^>]*>', ' ', entry.get('summary', ''))[:1000]
    text = title + ' ' + summary
    if not re.search(r'комнат|гостинк|дол[яиюеи]|квартир', text, re.I):
        return False
    import hashlib
    rid = hashlib.sha256(link.encode()).hexdigest()[:24]
    existing = get('objects', rid)
    if existing:
        return False
    price = price_from_text(text)
    put('objects', rid, {'address': title, 'type': 'Доля' if re.search('дол[яиюеи]', title, re.I) else 'Квартира' if 'квартир' in title.lower() else 'Комната', 'price': price, 'url': link, 'source': origin, 'status': 'new', 'rating': 'C', 'risk_note': 'Юридические риски не проверены', 'margin': None, 'created_at': now(), 'listings': [{'url': link, 'source': origin, 'price': price}]})
    return True


def run_search():
    start = now()
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    event('scout', 'working', 'Начата проверка подключённых RSS/Atom-источников', run_id)
    found, errors = 0, []
    try:
        with httpx.Client(timeout=18, follow_redirects=False, trust_env=False, headers={'User-Agent': 'OfficeAgents/1.0 (+personal RSS reader)'}) as client:
            for source in all_records('sources'):
                if source.get('status') == 'off':
                    continue
                event('scout', 'working', 'Проверяю: ' + str(source.get('name', 'источник')), run_id)
                url = source.get('url', '')
                parsed = urlparse(url)
                if parsed.scheme != 'https' or not parsed.hostname or parsed.hostname in ('localhost', '127.0.0.1'):
                    errors.append(f'Некорректный URL: {source.get("name", "источник")}')
                    continue
                try:
                    response = client.get(url)
                    response.raise_for_status()
                    feed = feedparser.parse(response.content)
                    if feed.bozo and not feed.entries:
                        raise ValueError('Это не RSS/Atom')
                    count = sum(ingest(entry, source.get('name', parsed.hostname)) for entry in feed.entries[:200])
                    found += count
                    event('scout', 'working', f'{source.get("name", "Источник")}: новых объявлений {count}', run_id)
                    put('sources', source['id'], {**source, 'found': source.get('found', 0) + count, 'last_checked': now()})
                except Exception as error:
                    errors.append(f'{source.get("name", url)}: {str(error)[:140]}')
        event('scout', 'done', f'Проверка источников окончена: новых {found}, ошибок {len(errors)}', run_id)
        event('reporter', 'working', 'Сохраняю результаты и PDF-отчёт', run_id)
        rid = run_id
        put('reports', rid, {'title': 'Отчёт о поиске', 'date': now(), 'kind': 'agent', 'objects': found, 'errors': errors, 'asset_id': rid})
        put('meta', 'run', {'last': now(), 'found': found, 'errors': errors, 'source': 'Сервер Офиса агентов', 'run_id': run_id})
        event('reporter', 'done', 'Отчёт доступен для скачивания', run_id)
        return {'new': found, 'errors': errors}
    except Exception as error:
        event('scout', 'error', f'Сбой поиска: {str(error)[:180]}', run_id)
        raise
    finally:
        pass


@app.post('/api/run')
def manual_run():
    return run_search()


def pdf_report(rid):
    report = get('reports', rid)
    if not report:
        raise HTTPException(404)
    buff = io.BytesIO()
    font_path = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
    if Path(font_path).exists() and 'DejaVu' not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont('DejaVu', font_path))
    font = 'DejaVu' if Path(font_path).exists() else 'Helvetica'
    doc = SimpleDocTemplate(buff, pagesize=A4, rightMargin=32, leftMargin=32, topMargin=36, bottomMargin=36)
    style = ParagraphStyle('ru', fontName=font, fontSize=9, leading=14, spaceAfter=7)
    title = ParagraphStyle('ruTitle', parent=style, fontSize=17, leading=23, spaceAfter=15)
    from xml.sax.saxutils import escape
    elements = [Paragraph('Офис агентов - отчёт по недвижимости', title), Paragraph('Дата: ' + escape(report.get('date', '')), style), Paragraph('Новых объектов в этом запуске: ' + str(report.get('objects', 0)), style)]
    elements.append(Paragraph('Данные предварительные. Неизвестные значения не считаются подтверждёнными.', style))
    for obj in all_records('objects')[:120]:
        address = escape(str(obj.get('address') or 'Адрес не установлен'))
        price = obj.get('price')
        elements.append(Paragraph('<b>' + address + '</b>', style))
        elements.append(Paragraph('Цена: ' + (f'{price:,} ₽'.replace(',', ' ') if isinstance(price, (int, float)) else 'не установлена') + ' | Маржа: ' + (str(obj.get('margin')) + ' ₽' if obj.get('margin') is not None else 'не рассчитана'), style))
        elements.append(Paragraph('Источник: ' + escape(str(obj.get('url', 'не указан'))), style))
        elements.append(Spacer(1, 5))
    if report.get('errors'):
        elements.append(Paragraph('Ошибки источников: ' + escape('; '.join(report['errors'])), style))
    doc.build(elements)
    return buff.getvalue()


@app.get('/_blob/{rid}')
def report_blob(rid: str):
    return Response(pdf_report(rid), media_type='application/pdf', headers={'Content-Disposition': f'inline; filename="report-{rid}.pdf"'})


scheduler = BackgroundScheduler(timezone='Asia/Krasnoyarsk')
scheduler.add_job(run_search, 'cron', day_of_week='mon,thu', hour=11, minute=0, id='search', max_instances=1, coalesce=True)


@app.on_event('startup')
def start_scheduler():
    if not scheduler.running:
        scheduler.start()


@app.on_event('shutdown')
def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
