import os
import re
import sqlite3
import uuid
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('GARAGE_SECRET_KEY', 'garage-control-local-dev-key')
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, 'garage.db')

PART_CATEGORIES = [
    'Suspension',
    'Exhaust',
    'Maintenance',
    'Engine',
    'Electrical',
    'Interior',
    'Brakes',
    'Wheels & Tires',
    'Other'
]

PART_STATUSES = ['Wishlist', 'Purchased', 'Installed']


def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def has_column(conn, table_name, column_name):
    columns = conn.execute(f'PRAGMA table_info({table_name})').fetchall()
    return any(row['name'] == column_name for row in columns)


def has_table(conn, table_name):
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table_name,)).fetchone()
    return tables is not None


def password_is_valid(password):
    if len(password) < 8:
        return False
    if not re.search(r'[A-Z]', password):
        return False
    if not re.search(r'\d', password):
        return False
    if not re.search(r'[^A-Za-z0-9]', password):
        return False
    return True


def send_resend_email(to_email, subject, html_content, text_content=None):
    api_key = os.environ.get('RESEND_API_KEY')
    sender_email = os.environ.get('RESEND_SENDER_EMAIL', 'no-reply@garage.local')
    if not api_key:
        return False

    payload = {
        'from': sender_email,
        'to': [to_email],
        'subject': subject,
        'html': html_content,
        'text': text_content or html_content,
    }

    url = 'https://api.resend.com/emails'
    data = json.dumps(payload).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
    }

    try:
        req = urllib.request.Request(url, data=data, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 201, 202)
    except Exception:
        return False


def init_db():
    conn = get_db_connection()

    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS cars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER,
            make TEXT NOT NULL,
            model TEXT NOT NULL,
            year INTEGER NOT NULL,
            trim TEXT NOT NULL,
            budget REAL NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_id) REFERENCES users(id)
        )
    ''')

    # Migration for older SQLite files that were created before the owner_id column existed.
    if not has_column(conn, 'cars', 'owner_id'):
        conn.execute('ALTER TABLE cars ADD COLUMN owner_id INTEGER')

    # If the old DB had cars and no users row yet, create a default owner account and point old cars at it.
    owner_count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    if owner_count == 0:
        default_password = generate_password_hash('garageowner')
        default_user = conn.execute('''
            INSERT INTO users (username, email, password_hash)
            VALUES (?, ?, ?)
        ''', ('garage_owner', 'garage_owner@local.local', default_password))
        default_user_id = default_user.lastrowid
        conn.execute('UPDATE cars SET owner_id = ? WHERE owner_id IS NULL OR owner_id = 0', (default_user_id,))

    # If there are any cars still missing owner_id, assign the first user as owner.
    first_user_id = conn.execute('SELECT id FROM users ORDER BY id ASC LIMIT 1').fetchone()
    if first_user_id and conn.execute('SELECT COUNT(*) FROM cars WHERE owner_id IS NULL OR owner_id = 0').fetchone()[0] > 0:
        conn.execute('UPDATE cars SET owner_id = ? WHERE owner_id IS NULL OR owner_id = 0', (first_user_id['id'],))

    conn.execute('''
        CREATE TABLE IF NOT EXISTS parts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            cost REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'Wishlist',
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (car_id) REFERENCES cars(id)
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS car_collaborators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'Collaborator',
            invited_by INTEGER NOT NULL,
            invited_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            accepted_at TIMESTAMP,
            UNIQUE(car_id, user_id),
            FOREIGN KEY (car_id) REFERENCES cars(id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (invited_by) REFERENCES users(id)
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS car_participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            share_pct REAL NOT NULL DEFAULT 50.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(car_id, user_id),
            FOREIGN KEY (car_id) REFERENCES cars(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS car_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_id INTEGER NOT NULL,
            payer_id INTEGER NOT NULL,
            payee_id INTEGER NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (car_id) REFERENCES cars(id),
            FOREIGN KEY (payer_id) REFERENCES users(id),
            FOREIGN KEY (payee_id) REFERENCES users(id)
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS password_resets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            expires_at TIMESTAMP NOT NULL,
            used_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS car_invitations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'Collaborator',
            invited_by INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            invited_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            accepted_at TIMESTAMP,
            accepted_by INTEGER,
            FOREIGN KEY (car_id) REFERENCES cars(id),
            FOREIGN KEY (invited_by) REFERENCES users(id),
            FOREIGN KEY (accepted_by) REFERENCES users(id)
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS car_edits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (car_id) REFERENCES cars(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')

    # Backfill participant rows for existing cars from their owners.
    all_cars = conn.execute('SELECT id, owner_id FROM cars').fetchall()
    for car in all_cars:
        exists = conn.execute('SELECT id FROM car_participants WHERE car_id = ? AND user_id = ?', (car['id'], car['owner_id'])).fetchone()
        if not exists:
            conn.execute('INSERT INTO car_participants (car_id, user_id, share_pct) VALUES (?, ?, ?)', (car['id'], car['owner_id'], 100.0))

    conn.commit()
    conn.close()


@app.before_request
def require_login():
    allowed = {'login', 'register', 'forgot_password', 'reset_password', 'static'}
    if request.endpoint in allowed:
        return None
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return None


@app.after_request
def security_headers(response):
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    return response


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('home'))
        return render_template('login.html', error='Invalid username or password.')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not username or not email:
            return render_template('register.html', error='Username and email are required.')

        if not password_is_valid(password):
            return render_template('register.html', error='Password must be at least 8 characters and include one uppercase letter, one number, and one symbol.')

        conn = get_db_connection()
        # Check duplicates before attempting to write.
        existing = conn.execute('''
            SELECT id FROM users WHERE username = ? OR email = ?
        ''', (username, email)).fetchone()
        if existing:
            conn.close()
            return render_template('register.html', error='That username or email is already in use.')

        try:
            cur = conn.execute('''
                INSERT INTO users (username, email, password_hash)
                VALUES (?, ?, ?)
            ''', (username, email, generate_password_hash(password)))
            conn.commit()
            user_id = cur.lastrowid
            conn.close()
            session['user_id'] = user_id
            session['username'] = username
            return redirect(url_for('home'))
        except sqlite3.IntegrityError:
            conn.close()
            return render_template('register.html', error='That username or email is already in use.')

    return render_template('register.html')


@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
        if user:
            token = uuid.uuid4().hex
            expires_at = (datetime.utcnow() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
            conn.execute('INSERT INTO password_resets (user_id, token, expires_at) VALUES (?, ?, ?)', (user['id'], token, expires_at))
            conn.commit()
            reset_link = request.host_url.rstrip('/') + url_for('reset_password', token=token)
            html = f'<p>Reset your Garage Control password:</p><p><a href="{reset_link}">{reset_link}</a></p>'
            text = f'Reset your Garage Control password: {reset_link}'
            sent = send_resend_email(email, 'Garage Control password reset', html, text)
            conn.close()
            if sent:
                return render_template('forgot_password.html', message='Password reset email sent.')
            return render_template('forgot_password.html', message='Password reset link created locally for development.', reset_link=reset_link)
        conn.close()
        return render_template('forgot_password.html', message='If an account exists with that email, a reset link can be created.')
    return render_template('forgot_password.html')


@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    conn = get_db_connection()
    record = conn.execute('SELECT * FROM password_resets WHERE token = ? AND used_at IS NULL AND expires_at > ?', (token, datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'))).fetchone()
    if not record:
        conn.close()
        return render_template('reset_password.html', error='That reset link is invalid or expired.')

    if request.method == 'POST':
        password = request.form.get('password', '')
        if not password_is_valid(password):
            conn.close()
            return render_template('reset_password.html', error='Password must be at least 8 characters and include one uppercase letter, one number, and one symbol.', token=token)
        user_id = record['user_id']
        conn.execute('UPDATE users SET password_hash = ? WHERE id = ?', (generate_password_hash(password), user_id))
        conn.execute('UPDATE password_resets SET used_at = ? WHERE id = ?', (datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), record['id']))
        conn.commit()
        conn.close()
        return redirect(url_for('login'))

    conn.close()
    return render_template('reset_password.html', token=token)


@app.route('/accept_invite/<token>', methods=['GET'])
def accept_invite(token):
    conn = get_db_connection()
    invite = conn.execute('SELECT * FROM car_invitations WHERE token = ?', (token,)).fetchone()
    if not invite:
        conn.close()
        return render_template('accept_invite.html', message='That invitation token is invalid.')

    user = conn.execute('SELECT * FROM users WHERE email = ?', (invite['email'],)).fetchone()
    if not user:
        conn.close()
        return render_template('accept_invite.html', message='That invite is waiting for an account using this email.')

    try:
        conn.execute('''
            INSERT OR IGNORE INTO car_collaborators (car_id, user_id, role, invited_by)
            VALUES (?, ?, ?, ?)
        ''', (invite['car_id'], user['id'], invite['role'], invite['invited_by']))
        conn.execute('''
            INSERT OR IGNORE INTO car_participants (car_id, user_id, share_pct)
            VALUES (?, ?, ?)
        ''', (invite['car_id'], user['id'], 50.0))
        conn.execute('''
            INSERT INTO car_edits (car_id, user_id, action, entity_type, entity_id, details)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (invite['car_id'], invite['invited_by'], 'collaborator_accepted', 'user', user['id'], f'Accepted invite from {invite["email"]}'))
        conn.execute('UPDATE car_invitations SET accepted_at = ?, accepted_by = ? WHERE id = ?', (datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), user['id'], invite['id']))
        conn.commit()
    except sqlite3.IntegrityError:
        pass

    conn.close()
    return render_template('accept_invite.html', message='Invitation accepted.')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
def home():
    user_id = session.get('user_id')
    conn = get_db_connection()
    cars = conn.execute('''
        SELECT c.*
        FROM cars c
        LEFT JOIN car_collaborators cc ON cc.car_id = c.id AND cc.user_id = ?
        WHERE c.owner_id = ? OR cc.user_id = ?
        ORDER BY c.year DESC, c.make ASC, c.model ASC
    ''', (user_id, user_id, user_id)).fetchall()
    conn.close()
    return render_template('home.html', cars=[dict(car) for car in cars], current_user={'id': user_id})


@app.route('/new_car', methods=['GET'])
def new_car():
    return render_template('new_car.html')


@app.route('/car/<int:car_id>')
def view_car(car_id):
    user_id = session.get('user_id')
    conn = get_db_connection()

    ownership = conn.execute('''
        SELECT c.*, u.username AS owner_username
        FROM cars c
        JOIN users u ON u.id = c.owner_id
        WHERE c.id = ? AND c.owner_id = ?
    ''', (car_id, user_id)).fetchone()

    if not ownership:
        collaborator = conn.execute('''
            SELECT c.*, u.username AS owner_username
            FROM cars c
            JOIN users u ON u.id = c.owner_id
            JOIN car_collaborators cc ON cc.car_id = c.id
            WHERE c.id = ? AND cc.user_id = ?
        ''', (car_id, user_id)).fetchone()
        if not collaborator:
            conn.close()
            return 'Car not found', 404
        car = collaborator
    else:
        car = ownership

    parts = conn.execute('''
        SELECT * FROM parts
        WHERE car_id = ?
        ORDER BY CASE status
            WHEN 'Purchased' THEN 1
            WHEN 'Installed' THEN 2
            ELSE 3
        END, category ASC, name ASC
    ''', (car_id,)).fetchall()

    collaborators = conn.execute('''
        SELECT cc.role, cc.invited_at, cc.accepted_at, u.username, u.email
        FROM car_collaborators cc
        JOIN users u ON u.id = cc.user_id
        WHERE cc.car_id = ?
        ORDER BY u.username ASC
    ''', (car_id,)).fetchall()

    participants = conn.execute('''
        SELECT cp.share_pct, u.id AS user_id, u.username, u.email
        FROM car_participants cp
        JOIN users u ON u.id = cp.user_id
        WHERE cp.car_id = ?
        ORDER BY u.username ASC
    ''', (car_id,)).fetchall()

    payments = conn.execute('''
        SELECT p.*, payer.username AS payer_username, payee.username AS payee_username
        FROM car_payments p
        JOIN users payer ON payer.id = p.payer_id
        JOIN users payee ON payee.id = p.payee_id
        WHERE p.car_id = ?
        ORDER BY p.created_at DESC
    ''', (car_id,)).fetchall()

    edits = conn.execute('''
        SELECT e.*, u.username
        FROM car_edits e
        JOIN users u ON u.id = e.user_id
        WHERE e.car_id = ?
        ORDER BY e.created_at DESC
        LIMIT 80
    ''', (car_id,)).fetchall()

    car_data = dict(car)
    car_parts = [dict(part) for part in parts]
    collaborator_data = [dict(row) for row in collaborators]
    participant_data = [dict(row) for row in participants]
    payment_data = [dict(row) for row in payments]
    edit_data = [dict(row) for row in edits]
    total_spent = sum(part['cost'] for part in car_parts if part['status'] in ('Purchased', 'Installed'))
    remaining_budget = car_data['budget'] - total_spent

    part_by_status = {status: [part for part in car_parts if part['status'] == status] for status in PART_STATUSES}

    # Simple balance model: compute per-user share of total spent, then adjust by payments recorded in the ledger.
    total_share = sum(row['share_pct'] for row in participant_data) or 1
    balance = {}
    for row in participant_data:
        share = row['share_pct'] / total_share
        user_cost = total_spent * share
        paid_in = sum(p['amount'] for p in payment_data if p['payee_id'] == row['user_id'])
        paid_out = sum(p['amount'] for p in payment_data if p['payer_id'] == row['user_id'])
        balance[row['username']] = user_cost - paid_out + paid_in

    all_cars = conn.execute('SELECT * FROM cars ORDER BY year DESC, make ASC, model ASC').fetchall()
    conn.close()

    return render_template(
        'index.html',
        car=car_data,
        owner_username=car_data['owner_username'],
        parts=car_parts,
        parts_by_status=part_by_status,
        collaborators=collaborator_data,
        participants=participant_data,
        payments=payment_data,
        balances=balance,
        edits=edit_data,
        total_spent=total_spent,
        remaining_budget=remaining_budget,
        cars=[dict(c) for c in all_cars],
        categories=PART_CATEGORIES,
        statuses=PART_STATUSES,
    )


@app.route('/add_car', methods=['POST'])
def add_car():
    make = request.form.get('make', '').strip()
    model = request.form.get('model', '').strip()
    year = 0
    try:
        year = int(request.form.get('year', 0))
    except ValueError:
        year = 0
    trim = request.form.get('trim', '').strip()
    budget = float(request.form.get('budget', 0) or 0)

    if not make or not model or not trim or year <= 0:
        return redirect(url_for('home'))

    user_id = session.get('user_id')
    conn = get_db_connection()
    cur = conn.execute('''
        INSERT INTO cars (owner_id, make, model, year, trim, budget)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, make, model, year, trim, budget))
    car_id = cur.lastrowid
    conn.execute('''
        INSERT INTO car_participants (car_id, user_id, share_pct)
        VALUES (?, ?, ?)
    ''', (car_id, user_id, 100.0))
    conn.execute('''
        INSERT INTO car_edits (car_id, user_id, action, entity_type, entity_id, details)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (car_id, user_id, 'car_created', 'car', car_id, f'Created {year} {make} {model} by {session.get("username")}'))
    conn.commit()
    conn.close()

    return redirect(url_for('view_car', car_id=car_id))


@app.route('/add_part/<int:car_id>', methods=['POST'])
def add_part(car_id):
    user_id = session.get('user_id')
    conn = get_db_connection()
    car = conn.execute('SELECT id FROM cars WHERE id = ?', (car_id,)).fetchone()
    if not car:
        conn.close()
        return 'Car not found', 404

    name = request.form.get('name', '').strip()
    category = request.form.get('category', 'Other')
    cost = float(request.form.get('cost', 0) or 0)
    status = request.form.get('status', 'Wishlist')
    notes = request.form.get('notes', '').strip()

    if not name:
        conn.close()
        return redirect(url_for('view_car', car_id=car_id))

    cur = conn.execute('''
        INSERT INTO parts (car_id, name, category, cost, status, notes)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (car_id, name, category, cost, status, notes))
    part_id = cur.lastrowid
    conn.execute('''
        INSERT INTO car_edits (car_id, user_id, action, entity_type, entity_id, details)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (car_id, user_id, 'part_added', 'part', part_id, f'Added {name} [{category}] {status} ${cost:.2f}'))
    conn.commit()
    conn.close()

    return redirect(url_for('view_car', car_id=car_id))


@app.route('/update_budget/<int:car_id>', methods=['POST'])
def update_budget(car_id):
    user_id = session.get('user_id')
    conn = get_db_connection()
    car = conn.execute('SELECT * FROM cars WHERE id = ?', (car_id,)).fetchone()
    if not car:
        conn.close()
        return 'Car not found', 404

    old_budget = car['budget']
    new_budget = float(request.form.get('budget', old_budget) or old_budget)
    conn.execute('UPDATE cars SET budget = ? WHERE id = ?', (new_budget, car_id))
    conn.execute('''
        INSERT INTO car_edits (car_id, user_id, action, entity_type, entity_id, details)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (car_id, user_id, 'budget_updated', 'car', car_id, f'Budget updated from ${old_budget:.2f} to ${new_budget:.2f}'))
    conn.commit()
    conn.close()
    return redirect(url_for('view_car', car_id=car_id))


@app.route('/delete_part/<int:car_id>/<int:part_id>', methods=['POST'])
def delete_part(car_id, part_id):
    user_id = session.get('user_id')
    conn = get_db_connection()
    conn.execute('DELETE FROM parts WHERE id = ? AND car_id = ?', (part_id, car_id))
    conn.execute('''
        INSERT INTO car_edits (car_id, user_id, action, entity_type, entity_id, details)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (car_id, user_id, 'part_removed', 'part', part_id, 'Part removed from build'))
    conn.commit()
    conn.close()
    return redirect(url_for('view_car', car_id=car_id))


@app.route('/invite/<int:car_id>', methods=['POST'])
def invite_collaborator(car_id):
    user_id = session.get('user_id')
    email = request.form.get('email', '').strip().lower()
    role = request.form.get('role', 'Collaborator').strip() or 'Collaborator'

    conn = get_db_connection()
    car = conn.execute('SELECT * FROM cars WHERE id = ? AND owner_id = ?', (car_id, user_id)).fetchone()
    if not car:
        conn.close()
        return 'Car not found', 404

    invited_user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
    if invited_user:
        try:
            conn.execute('''
                INSERT INTO car_collaborators (car_id, user_id, role, invited_by)
                VALUES (?, ?, ?, ?)
            ''', (car_id, invited_user['id'], role, user_id))
            conn.execute('''
                INSERT OR IGNORE INTO car_participants (car_id, user_id, share_pct)
                VALUES (?, ?, ?)
            ''', (car_id, invited_user['id'], 50.0))
            conn.execute('''
                INSERT INTO car_edits (car_id, user_id, action, entity_type, entity_id, details)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (car_id, user_id, 'collaborator_invited', 'user', invited_user['id'], f'Invited {invited_user["username"]} as {role}'))
            conn.commit()
        except sqlite3.IntegrityError:
            pass
        conn.close()
        return redirect(url_for('view_car', car_id=car_id))

    token = uuid.uuid4().hex
    conn.execute('''
        INSERT INTO car_invitations (car_id, email, role, invited_by, token)
        VALUES (?, ?, ?, ?, ?)
    ''', (car_id, email, role, user_id, token))
    conn.commit()

    invite_link = request.host_url.rstrip('/') + url_for('accept_invite', token=token)
    html = f'<p>You were invited to collaborate on a Garage Control car.</p><p><a href="{invite_link}">{invite_link}</a></p>'
    text = f'You were invited to collaborate on a Garage Control car: {invite_link}'
    sent = send_resend_email(email, 'Garage Control car invitation', html, text)

    conn.close()
    if sent:
        return redirect(url_for('view_car', car_id=car_id))
    return redirect(url_for('view_car', car_id=car_id))


@app.route('/update_split/<int:car_id>', methods=['POST'])
def update_split(car_id):
    user_id = session.get('user_id')
    conn = get_db_connection()
    car = conn.execute('SELECT * FROM cars WHERE id = ? AND owner_id = ?', (car_id, user_id)).fetchone()
    if not car:
        conn.close()
        return 'Car not found', 404

    share_rows = conn.execute('SELECT id, user_id FROM car_participants WHERE car_id = ?', (car_id,)).fetchall()
    for row in share_rows:
        share_name = f'share_{row["user_id"]}'
        share_text = request.form.get(share_name, '0')
        try:
            share_pct = float(share_text)
        except ValueError:
            share_pct = 50.0
        if share_pct < 0:
            share_pct = 0
        conn.execute('UPDATE car_participants SET share_pct = ? WHERE id = ?', (share_pct, row['id']))

    conn.execute('''
        INSERT INTO car_edits (car_id, user_id, action, entity_type, entity_id, details)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (car_id, user_id, 'split_updated', 'car', car_id, 'Updated cost share percentages'))
    conn.commit()
    conn.close()
    return redirect(url_for('view_car', car_id=car_id))


@app.route('/record_payment/<int:car_id>', methods=['POST'])
def record_payment(car_id):
    user_id = session.get('user_id')
    conn = get_db_connection()
    car = conn.execute('SELECT * FROM cars WHERE id = ? AND owner_id = ?', (car_id, user_id)).fetchone()
    if not car:
        conn.close()
        return 'Car not found', 404

    payer_id = request.form.get('payer_id')
    payee_id = request.form.get('payee_id')
    amount = float(request.form.get('amount', 0) or 0)
    note = request.form.get('note', '').strip()
    if not payer_id or not payee_id or amount <= 0:
        conn.close()
        return redirect(url_for('view_car', car_id=car_id))

    conn.execute('''
        INSERT INTO car_payments (car_id, payer_id, payee_id, amount, note)
        VALUES (?, ?, ?, ?, ?)
    ''', (car_id, payer_id, payee_id, amount, note))
    conn.execute('''
        INSERT INTO car_edits (car_id, user_id, action, entity_type, entity_id, details)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (car_id, user_id, 'payment_recorded', 'payment', None, f'Payment recorded: {amount:.2f}'))
    conn.commit()
    conn.close()
    return redirect(url_for('view_car', car_id=car_id))


init_db()


if __name__ == '__main__':
    init_db()
    app.run(debug=True)
