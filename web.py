import os
import re
import secrets
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, abort, jsonify, flash
from core import Store, posted_age, posted_within_24_hours, parse_email
from scanner import Scanner
from curated import COMPANIES

def create_app(data_dir=None, start_scanner=False):
    app = Flask(__name__)
    app.config.update(SECRET_KEY=secrets.token_hex(32), MAX_CONTENT_LENGTH=2*1024*1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict')
    scanner = Scanner(data_dir or os.environ.get('JOBRADAR_DATA_DIR','data'))
    app.extensions['scanner'] = scanner
    from resumes import register_resumes
    register_resumes(app, scanner)

    @contextmanager
    def store():
        db = Store(scanner.db_path)
        try: yield db
        finally: db.db.close()

    @app.before_request
    def csrf():
        if 'csrf' not in session: session['csrf'] = secrets.token_hex(32)
        if request.method == 'POST':
            token = request.form.get('csrf','')
            if not secrets.compare_digest(token,session['csrf']): abort(403)

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers['Cache-Control'] = 'no-store'
        return response

    def available(rows, scope='fresh'):
        return [dict(j,age=posted_age(j['posted'])) for j in rows
                if j['status'] not in ('Applied','Dismissed') and
                (scope == 'browse' or posted_within_24_hours(j['posted']))]

    def listing(rows):
        scope = 'browse' if request.args.get('scope') == 'browse' else 'fresh'
        selected = request.args.get('view','all')
        if selected not in ('all','ready','review'): selected = 'all'
        jobs = available(rows, scope)
        if selected == 'ready': jobs = [j for j in jobs if j['eligibility']=='Ready']
        if selected == 'review': jobs = [j for j in jobs if j['eligibility']=='Review']
        def timestamp(job):
            try: return datetime.fromisoformat(job['posted'].replace('Z','+00:00')).timestamp()
            except (ValueError, TypeError, OverflowError, OSError): return float('-inf')
        jobs.sort(key=lambda j: (timestamp(j), j['key']), reverse=True)
        total = len(jobs)
        pages = max(1, (total + 49)//50)
        page = min(pages, max(1, request.args.get('page',1,type=int) or 1))
        return dict(jobs=jobs[(page-1)*50:page*50], scope=scope, view=selected,
                    total=total, page=page, pages=pages)

    @app.get('/')
    def index():
        with store() as db: rows = db.rows()
        return render_template('index.html',state=scanner.snapshot(),**listing(rows))

    @app.get('/api/status')
    def api_status():
        with store() as db: stored = db.rows()
        jobs = available(stored)
        visible = listing(stored)
        excluded = dict(applied=sum(j['status']=='Applied' for j in stored),
            dismissed=sum(j['status']=='Dismissed' for j in stored),
            outside_window=sum(j['status'] not in ('Applied','Dismissed') and not posted_within_24_hours(j['posted']) for j in stored))
        return jsonify(state=scanner.snapshot(),available=len(jobs),
                       excluded=excluded,
                       visible_jobs=[{k:j[k] for k in ('key','age')} for j in visible['jobs']],
                       total=visible['total'], page=visible['page'], pages=visible['pages'],
                       ready=sum(j['eligibility']=='Ready' for j in jobs),
                       jobs=[{k:j[k] for k in ('key','title','company','status','eligibility','age')} for j in jobs])

    @app.route('/monitor',methods=['GET','POST'])
    def curated_monitor():
        with scanner.lock: settings = dict(scanner.settings)
        if request.method == 'POST':
            selected = request.form.getlist('company')
            if any(value not in {c['id'] for c in COMPANIES} for value in selected): abort(400)
            settings['curated'] = list(dict.fromkeys(selected))
            scanner.save_settings(settings)
            flash('Watchlist saved. Enabled companies are checked hourly while the scanner runs.')
            scanner.trigger()
            return redirect(url_for('curated_monitor'))
        with store() as db:
            checks = {r[0]:dict(checked=datetime.fromtimestamp(r[1]).astimezone().isoformat(),status=r[2],matches=r[3],error=r[4])
                for r in db.db.execute('SELECT company,checked,status,matches,error FROM curated_checks')}
        return render_template('monitor.html',companies=COMPANIES,selected=settings.get('curated',[]),checks=checks)

    @app.get('/healthz')
    def health():
        with store() as db: db.db.execute('SELECT 1').fetchone()
        return jsonify(status='ok')

    @app.post('/scan')
    def scan():
        if not scanner.snapshot()['enabled']: flash('Scanner is disabled by the server configuration.')
        else:
            scanner.trigger()
            flash('Scan requested. Results update as sources complete.')
        return redirect(url_for('index'))

    @app.post('/dismiss')
    def dismiss():
        with store() as db: db.mark(request.form.get('key',''),'Dismissed')
        return redirect(url_for('index'))

    @app.get('/applications')
    def applications():
        with store() as db: rows = db.applications()
        return render_template('applications.html',applications=rows)

    @app.route('/application',methods=['GET','POST'])
    def application():
        key = request.values.get('key','')
        if request.method == 'POST':
            try:
                with store() as db:
                    db.save_application(key or None,*(request.form.get(k,'') for k in ('title','company','url','applied_on','stage','notes')))
            except ValueError as exc:
                flash(str(exc))
                return render_template('application.html',record=dict(request.form)),400
            return redirect(url_for('applications'))
        record = dict(key='',title='',company='',url='',applied_on='',stage='Applied',notes='')
        if key:
            with store() as db:
                saved = next((a for a in db.applications() if a['job_key']==key),None)
                job = next((j for j in db.rows() if j['key']==key),None)
            if saved: record.update(saved,key=key)
            elif job: record.update({k:job[k] for k in ('title','company','url')},key=key)
            else: abort(404)
        return render_template('application.html',record=record)

    @app.route('/settings',methods=['GET','POST'])
    def settings():
        with scanner.lock: current = dict(scanner.settings)
        if request.method == 'POST':
            try:
                updated = dict(current)
                updated['minutes'] = int(request.form.get('minutes','5'))
                updated['batch_size'] = int(request.form.get('batch_size','150'))
                if not 5 <= updated['minutes'] <= 1440 or not 1 <= updated['batch_size'] <= 300:
                    raise ValueError('Use 5–1440 minutes and 1–300 boards per round.')
                for key in ('boards','ashby_boards','lever_boards'):
                    updated[key] = list(dict.fromkeys(s.strip() for s in request.form.get(key,'').split(',') if s.strip()))
                    if len(updated[key])>30 or any(not re.fullmatch(r'[A-Za-z0-9_-]+',s) for s in updated[key]):
                        raise ValueError('Use up to 30 valid priority board tokens per platform.')
                updated['wwr'] = 'wwr' in request.form
                for key in ('mail_host','mail_user','mail_folder'): updated[key] = request.form.get(key,'').strip()
                updated['usajobs_email'] = request.form.get('usajobs_email','').strip()
                password = request.form.get('password','')
                if password and not all(updated[k] for k in ('mail_host','mail_user','mail_folder')):
                    raise ValueError('Enter the mail host, username and folder.')
                scanner.save_settings(updated,password,'disconnect' in request.form,request.form.get('usajobs_key',''),'disconnect_usajobs' in request.form)
                if request.form.get('usajobs_key') or updated['usajobs_email'] != current.get('usajobs_email'):
                    scanner.request_usajobs_check()
                flash('Saved. USAJOBS keys persist across restarts. Mail passwords stay in memory only; reconnect after a container restart.')
                return redirect(url_for('settings'))
            except ValueError as exc: flash(str(exc))
        return render_template('settings.html',settings=current,state=scanner.snapshot())

    @app.post('/check-usajobs')
    def check_usajobs():
        if not scanner.snapshot()['usajobs_configured']:
            flash('Save your registered USAJOBS email and API key first.')
        elif not scanner.snapshot()['enabled']:
            flash('Scanner is disabled by the server configuration.')
        else:
            scanner.request_usajobs_check()
            flash('USAJOBS check queued. Refresh Settings after the scan completes to see the result.')
        return redirect(url_for('settings'))

    @app.post('/import-email')
    def import_email():
        upload = request.files.get('email')
        if not upload: abort(400)
        try:
            jobs = parse_email(upload.read())
            with store() as db: db.ingest('LinkedIn import',jobs)
            flash(f'Saved {len(jobs)} matching links. Unknown posting dates are excluded from the 24-hour view.')
        except Exception: flash('Could not parse that email.')
        return redirect(url_for('settings'))

    if start_scanner: scanner.start()
    return app

if __name__ == '__main__':
    from waitress import serve
    app = create_app(start_scanner=os.environ.get('JOBRADAR_SCAN_ENABLED','true').lower()=='true')
    serve(app,host='0.0.0.0',port=8080,threads=6)
