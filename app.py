# IMPORTANT: Monkey patch MUST be first
import eventlet
eventlet.monkey_patch()

import os
import sys
import math
import time
import random
import hashlib
import threading
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_from_directory, g
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.security import generate_password_hash, check_password_hash
import jwt

# ─── App Setup ───────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'pepeti-aviator-secret-2024-ksh')
app.config['DATABASE'] = os.environ.get('DATABASE_URL', 'pepeti.db')

# Initialize SocketIO with proper settings for Render
socketio = SocketIO(app, 
                   cors_allowed_origins="*", 
                   async_mode='eventlet',
                   logger=False, 
                   engineio_logger=False,
                   ping_timeout=60,
                   ping_interval=25)

# ... rest of your existing code remains the same


# ─── Database ─────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(app.config['DATABASE'])
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            phone TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            balance REAL DEFAULT 0.0,
            avatar INTEGER DEFAULT 1,
            is_admin INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            amount REAL NOT NULL,
            method TEXT,
            reference TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS game_rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_seed TEXT NOT NULL,
            crash_point REAL NOT NULL,
            started_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            bet_amount REAL NOT NULL,
            cashout_at REAL,
            win_amount REAL DEFAULT 0,
            status TEXT DEFAULT 'playing',
            slot TEXT DEFAULT 'f',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(round_id) REFERENCES game_rounds(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            avatar INTEGER DEFAULT 1,
            message TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    # Create default admin
    try:
        c.execute("INSERT INTO users (username, phone, password_hash, balance, is_admin) VALUES (?, ?, ?, ?, ?)",
                  ('admin', '0700000000', generate_password_hash('admin123'), 999999.0, 1))
    except:
        pass
    conn.commit()
    conn.close()

# ─── JWT Auth ─────────────────────────────────────────────────────────────────
def create_token(user_id):
    payload = {'user_id': user_id, 'exp': datetime.utcnow() + timedelta(days=30)}
    return jwt.encode(payload, app.config['SECRET_KEY'], algorithm='HS256')

def decode_token(token):
    try:
        return jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
    except:
        return None

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            token = request.args.get('token', '')
        data = decode_token(token)
        if not data:
            return jsonify({'status': False, 'message': 'Unauthorized'}), 401
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (data['user_id'],)).fetchone()
        db.close()
        if not user or not user['is_active']:
            return jsonify({'status': False, 'message': 'User not found'}), 401
        return f(dict(user), *args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        data = decode_token(token)
        if not data:
            return jsonify({'status': False, 'message': 'Unauthorized'}), 401
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (data['user_id'],)).fetchone()
        db.close()
        if not user or not user['is_admin']:
            return jsonify({'status': False, 'message': 'Admin only'}), 403
        return f(dict(user), *args, **kwargs)
    return decorated

# ─── Game Engine ──────────────────────────────────────────────────────────────
class AviatorGame:
    def __init__(self):
        self.state = 'BET'          # BET | PLAYING | GAMEEND
        self.crash_point = 1.0
        self.current_multiplier = 1.0
        self.start_time = 0
        self.round_id = None
        self.server_seed = ''
        self.betted_users = []      # [{name, betAmount, cashOut, cashouted, target, img, slot, user_id, bet_id}]
        self.history = []           # last 20 crash points
        self.bet_start = 0
        self.lock = threading.Lock()
        self.running = False

    def generate_crash_point(self):
        seed = os.urandom(32).hex()
        self.server_seed = seed
        h = hashlib.sha256(seed.encode()).hexdigest()
        val = int(h[:8], 16) % 10000
        if val == 0:
            return 1.0
        crash = max(1.0, round(9900 / (val), 2))
        return min(crash, 1000.0)

    def start_bet_phase(self):
        with self.lock:
            self.state = 'BET'
            self.crash_point = self.generate_crash_point()
            self.betted_users = []
            self.bet_start = time.time()
        socketio.emit('gameState', {
            'GameState': 'BET',
            'currentNum': '1.00',
            'currentSecondNum': 0,
            'time': int((time.time() - self.bet_start) * 1000)
        })
        socketio.emit('history', self.history[-20:])
        socketio.emit('bettedUserInfo', [])

    def start_playing(self):
        db = get_db()
        rid = db.execute('INSERT INTO game_rounds (server_seed, crash_point) VALUES (?,?)',
                         (self.server_seed, self.crash_point)).lastrowid
        db.commit()
        db.close()
        with self.lock:
            self.state = 'PLAYING'
            self.round_id = rid
            self.start_time = time.time()
            self.current_multiplier = 1.0
        socketio.emit('gameState', {
            'GameState': 'PLAYING',
            'currentNum': '1.00',
            'currentSecondNum': 0,
            'time': int((time.time() - self.start_time) * 1000)
        })

    def calc_multiplier(self, elapsed):
        t = elapsed
        m = 1 + 0.06*t + (0.06*t)**2 - (0.04*t)**3 + (0.04*t)**4
        return max(1.0, round(m, 2))

    def end_game(self):
        with self.lock:
            self.state = 'GAMEEND'
            final = self.crash_point
        # Mark all uncashed bets as lost
        db = get_db()
        if self.round_id:
            db.execute("UPDATE bets SET status='lost' WHERE round_id=? AND status='playing'", (self.round_id,))
            db.commit()
        db.close()

        cp_str = f"{final:.2f}"
        self.history.insert(0, float(cp_str))
        if len(self.history) > 30:
            self.history = self.history[:30]

        socketio.emit('gameState', {
            'GameState': 'GAMEEND',
            'currentNum': cp_str,
            'currentSecondNum': 0,
            'time': 0
        })
        socketio.emit('history', self.history[:20])

    def place_bet(self, user_id, username, avatar, bet_amount, slot):
        with self.lock:
            if self.state != 'BET':
                return False, 'Betting is closed'
            # check already bet in this slot
            for u in self.betted_users:
                if u['user_id'] == user_id and u['slot'] == slot:
                    return False, 'Already bet in this slot'
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
        if not user or user['balance'] < bet_amount:
            db.close()
            return False, 'Insufficient balance'
        db.execute('UPDATE users SET balance=balance-? WHERE id=?', (bet_amount, user_id))
        bid = db.execute('INSERT INTO bets (round_id, user_id, username, bet_amount, slot) VALUES (?,?,?,?,?)',
                         (0, user_id, username, bet_amount, slot)).lastrowid
        db.commit()
        new_bal = db.execute('SELECT balance FROM users WHERE id=?', (user_id,)).fetchone()['balance']
        db.close()

        entry = {
            'user_id': user_id, 'username': username,
            'name': username, 'betAmount': bet_amount,
            'cashOut': 0.0, 'cashouted': False,
            'target': 0.0, 'img': f'/static/avatars/av-{avatar}.png',
            'slot': slot, 'bet_id': bid
        }
        with self.lock:
            self.betted_users.append(entry)
        socketio.emit('bettedUserInfo', self._public_bets())
        return True, {'balance': new_bal, 'bet_id': bid}

    def cashout(self, user_id, slot, at_multiplier):
        with self.lock:
            if self.state != 'PLAYING':
                return False, 'Game not playing'
            if at_multiplier > self.crash_point:
                return False, 'Too late!'
            entry = None
            for u in self.betted_users:
                if u['user_id'] == user_id and u['slot'] == slot and not u['cashouted']:
                    entry = u
                    break
            if not entry:
                return False, 'No active bet'
            win = round(entry['betAmount'] * at_multiplier, 2)
            entry['cashouted'] = True
            entry['cashOut'] = at_multiplier
            entry['target'] = at_multiplier

        db = get_db()
        db.execute('UPDATE users SET balance=balance+? WHERE id=?', (win, user_id))
        db.execute("UPDATE bets SET cashout_at=?, win_amount=?, status='won' WHERE id=?",
                   (at_multiplier, win, entry['bet_id']))
        db.commit()
        new_bal = db.execute('SELECT balance FROM users WHERE id=?', (user_id,)).fetchone()['balance']
        db.close()
        socketio.emit('bettedUserInfo', self._public_bets())
        return True, {'win': win, 'balance': new_bal}

    def _public_bets(self):
        result = []
        for u in self.betted_users:
            result.append({
                'name': u['username'],
                'betAmount': u['betAmount'],
                'cashOut': u['cashOut'],
                'cashouted': u['cashouted'],
                'target': u['target'],
                'img': u['img']
            })
        return result


game = AviatorGame()

def game_loop():
    game.running = True
    while game.running:
        try:
            # BET phase: 5 seconds
            game.start_bet_phase()
            time.sleep(5)

            # PLAYING phase
            game.start_playing()
            crash = game.crash_point
            elapsed = 0.0
            interval = 0.05

            while True:
                time.sleep(interval)
                elapsed = time.time() - game.start_time
                m = game.calc_multiplier(elapsed)
                with game.lock:
                    game.current_multiplier = m
                socketio.emit('gameState', {
                    'GameState': 'PLAYING',
                    'currentNum': f'{m:.2f}',
                    'currentSecondNum': 0,
                    'time': int(elapsed * 1000)
                })
                if m >= crash:
                    break

            # GAMEEND
            game.end_game()
            time.sleep(3)
        except Exception as e:
            print(f"Game loop error: {e}")
            time.sleep(2)

# ─── Socket Events ────────────────────────────────────────────────────────────
connected_users = {}  # sid -> user info

@socketio.on('connect')
def on_connect():
    pass

@socketio.on('enterRoom')
def on_enter(data):
    token = data.get('token')
    if token:
        info = decode_token(token)
        if info:
            db = get_db()
            user = db.execute('SELECT * FROM users WHERE id=?', (info['user_id'],)).fetchone()
            db.close()
            if user:
                connected_users[request.sid] = dict(user)
                emit('myInfo', {
                    'balance': user['balance'],
                    'userName': user['username'],
                    'userType': bool(user['is_admin']),
                    'currency': 'KSH',
                    'avatar': user['avatar']
                })
    emit('history', game.history[:20])
    emit('bettedUserInfo', game._public_bets())
    emit('gameState', {
        'GameState': game.state,
        'currentNum': f'{game.current_multiplier:.2f}',
        'currentSecondNum': 0,
        'time': int((time.time() - game.start_time) * 1000) if game.state == 'PLAYING' else 0
    })
    emit('getBetLimits', {'min': 10, 'max': 50000})

@socketio.on('playBet')
def on_bet(data):
    sid = request.sid
    user = connected_users.get(sid)
    if not user:
        emit('error', {'message': 'Not authenticated', 'index': data.get('type','f')})
        return
    amount = float(data.get('betAmount', 0))
    slot = data.get('type', 'f')
    if amount < 10:
        emit('error', {'message': 'Minimum bet is KSH 10', 'index': slot})
        return
    ok, result = game.place_bet(user['id'], user['username'], user['avatar'], amount, slot)
    if ok:
        db = get_db()
        new_bal = db.execute('SELECT balance FROM users WHERE id=?', (user['id'],)).fetchone()['balance']
        db.close()
        connected_users[sid]['balance'] = new_bal
        emit('myInfo', {'balance': new_bal, 'userName': user['username'], 'userType': bool(user['is_admin']), 'currency': 'KSH'})
        emit('myBetState', {'f': {'betted': True, 'cashouted': False, 'betAmount': amount, 'cashAmount': 0},
                            's': {'betted': True, 'cashouted': False, 'betAmount': 0, 'cashAmount': 0}})
    else:
        emit('error', {'message': result, 'index': slot})

@socketio.on('cashOut')
def on_cashout(data):
    sid = request.sid
    user = connected_users.get(sid)
    if not user:
        return
    slot = data.get('type', 'f')
    at = game.current_multiplier
    ok, result = game.cashout(user['id'], slot, at)
    if ok:
        connected_users[sid]['balance'] = result['balance']
        emit('myInfo', {'balance': result['balance'], 'userName': user['username'], 'userType': bool(user['is_admin']), 'currency': 'KSH'})
        emit('success', f"Cashed out at {at:.2f}x! Won KSH {result['win']:.2f}")
    else:
        emit('error', {'message': result, 'index': slot})

@socketio.on('sendMsg')
def on_message(data):
    sid = request.sid
    user = connected_users.get(sid)
    if not user:
        return
    msg = str(data.get('msg', '')).strip()[:200]
    if not msg:
        return
    db = get_db()
    db.execute('INSERT INTO chat_messages (user_id, username, avatar, message) VALUES (?,?,?,?)',
               (user['id'], user['username'], user['avatar'], msg))
    db.commit()
    db.close()
    socketio.emit('newMsg', {
        'name': user['username'],
        'msg': msg,
        'img': f"/static/avatars/av-{user['avatar']}.png"
    })

@socketio.on('getMessages')
def on_get_messages():
    db = get_db()
    msgs = db.execute('SELECT * FROM chat_messages ORDER BY id DESC LIMIT 50').fetchall()
    db.close()
    result = [{'name': m['username'], 'msg': m['message'], 'img': f"/static/avatars/av-{m['avatar']}.png"} for m in reversed(msgs)]
    emit('messages', result)

@socketio.on('disconnect')
def on_disconnect():
    connected_users.pop(request.sid, None)

# ─── Auth Routes ──────────────────────────────────────────────────────────────
@app.route('/api/register', methods=['POST'])
def register():
    d = request.json or {}
    username = d.get('username', '').strip()
    phone = d.get('phone', '').strip()
    password = d.get('password', '').strip()
    if not username or not phone or not password:
        return jsonify({'status': False, 'message': 'All fields required'})
    if len(password) < 6:
        return jsonify({'status': False, 'message': 'Password min 6 chars'})
    db = get_db()
    try:
        db.execute('INSERT INTO users (username, phone, password_hash) VALUES (?,?,?)',
                   (username, phone, generate_password_hash(password)))
        db.commit()
        user = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        db.close()
        token = create_token(user['id'])
        return jsonify({'status': True, 'token': token, 'username': username, 'balance': 0.0})
    except Exception as e:
        db.close()
        return jsonify({'status': False, 'message': 'Username or phone already exists'})

@app.route('/api/login', methods=['POST'])
def login():
    d = request.json or {}
    phone = d.get('phone', '').strip()
    password = d.get('password', '').strip()
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE phone=?', (phone,)).fetchone()
    db.close()
    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({'status': False, 'message': 'Invalid phone or password'})
    if not user['is_active']:
        return jsonify({'status': False, 'message': 'Account suspended'})
    token = create_token(user['id'])
    return jsonify({'status': True, 'token': token, 'username': user['username'],
                    'balance': user['balance'], 'is_admin': bool(user['is_admin'])})

@app.route('/api/me', methods=['GET'])
@token_required
def me(current_user):
    return jsonify({'status': True, 'balance': current_user['balance'],
                    'username': current_user['username'], 'is_admin': bool(current_user['is_admin']),
                    'avatar': current_user['avatar']})

# ─── Deposit/Withdraw ─────────────────────────────────────────────────────────
@app.route('/api/deposit', methods=['POST'])
@token_required
def deposit(current_user):
    d = request.json or {}
    amount = float(d.get('amount', 0))
    method = d.get('method', 'mpesa')  # mpesa | airtel
    phone = d.get('phone', '').strip()
    reference = d.get('reference', '').strip()
    if amount < 10:
        return jsonify({'status': False, 'message': 'Minimum deposit KSH 10'})
    if not phone or not reference:
        return jsonify({'status': False, 'message': 'Phone and transaction reference required'})
    db = get_db()
    db.execute('INSERT INTO transactions (user_id, type, amount, method, reference, status) VALUES (?,?,?,?,?,?)',
               (current_user['id'], 'deposit', amount, method, reference, 'pending'))
    db.commit()
    db.close()
    return jsonify({'status': True, 'message': f'Deposit of KSH {amount:.2f} submitted for review. Reference: {reference}'})

@app.route('/api/withdraw', methods=['POST'])
@token_required
def withdraw(current_user):
    d = request.json or {}
    amount = float(d.get('amount', 0))
    method = d.get('method', 'mpesa')
    phone = d.get('phone', '').strip()
    if amount < 50:
        return jsonify({'status': False, 'message': 'Minimum withdrawal KSH 50'})
    db = get_db()
    user = db.execute('SELECT balance FROM users WHERE id=?', (current_user['id'],)).fetchone()
    if user['balance'] < amount:
        db.close()
        return jsonify({'status': False, 'message': 'Insufficient balance'})
    db.execute('UPDATE users SET balance=balance-? WHERE id=?', (amount, current_user['id']))
    db.execute('INSERT INTO transactions (user_id, type, amount, method, reference, status) VALUES (?,?,?,?,?,?)',
               (current_user['id'], 'withdraw', amount, method, phone, 'pending'))
    db.commit()
    new_bal = db.execute('SELECT balance FROM users WHERE id=?', (current_user['id'],)).fetchone()['balance']
    db.close()
    return jsonify({'status': True, 'message': f'Withdrawal of KSH {amount:.2f} submitted', 'balance': new_bal})

@app.route('/api/my-info', methods=['POST'])
@token_required
def my_info(current_user):
    db = get_db()
    bets = db.execute('''SELECT b.*, r.crash_point FROM bets b
                         LEFT JOIN game_rounds r ON b.round_id=r.id
                         WHERE b.user_id=? ORDER BY b.id DESC LIMIT 30''',
                      (current_user['id'],)).fetchall()
    db.close()
    return jsonify({'status': True, 'data': [dict(b) for b in bets]})

# ─── Admin Routes ─────────────────────────────────────────────────────────────
@app.route('/admin')
def admin_page():
    return render_template('admin.html')

@app.route('/api/admin/stats', methods=['GET'])
@admin_required
def admin_stats(current_user):
    db = get_db()
    total_users = db.execute('SELECT COUNT(*) FROM users WHERE is_admin=0').fetchone()[0]
    total_deposits = db.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE type='deposit' AND status='approved'").fetchone()[0]
    total_withdrawals = db.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE type='withdraw' AND status='approved'").fetchone()[0]
    pending_deposits = db.execute("SELECT COUNT(*) FROM transactions WHERE type='deposit' AND status='pending'").fetchone()[0]
    pending_withdrawals = db.execute("SELECT COUNT(*) FROM transactions WHERE type='withdraw' AND status='pending'").fetchone()[0]
    total_rounds = db.execute('SELECT COUNT(*) FROM game_rounds').fetchone()[0]
    online_users = len(connected_users)
    db.close()
    return jsonify({
        'total_users': total_users, 'total_deposits': total_deposits,
        'total_withdrawals': total_withdrawals, 'pending_deposits': pending_deposits,
        'pending_withdrawals': pending_withdrawals, 'total_rounds': total_rounds,
        'online_users': online_users, 'game_state': game.state,
        'current_multiplier': game.current_multiplier
    })

@app.route('/api/admin/users', methods=['GET'])
@admin_required
def admin_users(current_user):
    db = get_db()
    users = db.execute('SELECT id, username, phone, balance, avatar, is_active, created_at FROM users WHERE is_admin=0 ORDER BY id DESC').fetchall()
    db.close()
    return jsonify({'status': True, 'data': [dict(u) for u in users]})

@app.route('/api/admin/user/<int:uid>/balance', methods=['POST'])
@admin_required
def admin_adjust_balance(current_user, uid):
    d = request.json or {}
    amount = float(d.get('amount', 0))
    db = get_db()
    db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, uid))
    db.commit()
    new_bal = db.execute('SELECT balance FROM users WHERE id=?', (uid,)).fetchone()['balance']
    db.close()
    return jsonify({'status': True, 'balance': new_bal})

@app.route('/api/admin/user/<int:uid>/toggle', methods=['POST'])
@admin_required
def admin_toggle_user(current_user, uid):
    db = get_db()
    db.execute('UPDATE users SET is_active = CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?', (uid,))
    db.commit()
    user = db.execute('SELECT is_active FROM users WHERE id=?', (uid,)).fetchone()
    db.close()
    return jsonify({'status': True, 'is_active': bool(user['is_active'])})

@app.route('/api/admin/transactions', methods=['GET'])
@admin_required
def admin_transactions(current_user):
    ttype = request.args.get('type', 'all')
    status = request.args.get('status', 'all')
    db = get_db()
    query = '''SELECT t.*, u.username, u.phone FROM transactions t 
               JOIN users u ON t.user_id=u.id WHERE 1=1'''
    params = []
    if ttype != 'all':
        query += ' AND t.type=?'; params.append(ttype)
    if status != 'all':
        query += ' AND t.status=?'; params.append(status)
    query += ' ORDER BY t.id DESC LIMIT 100'
    txns = db.execute(query, params).fetchall()
    db.close()
    return jsonify({'status': True, 'data': [dict(t) for t in txns]})

@app.route('/api/admin/transaction/<int:tid>/approve', methods=['POST'])
@admin_required
def admin_approve_txn(current_user, tid):
    db = get_db()
    txn = db.execute('SELECT * FROM transactions WHERE id=?', (tid,)).fetchone()
    if not txn:
        db.close()
        return jsonify({'status': False, 'message': 'Not found'})
    if txn['status'] != 'pending':
        db.close()
        return jsonify({'status': False, 'message': 'Already processed'})
    if txn['type'] == 'deposit':
        db.execute('UPDATE users SET balance=balance+? WHERE id=?', (txn['amount'], txn['user_id']))
    db.execute("UPDATE transactions SET status='approved' WHERE id=?", (tid,))
    db.commit()
    db.close()
    # Notify user via socket
    for sid, u in connected_users.items():
        if u['id'] == txn['user_id']:
            db2 = get_db()
            bal = db2.execute('SELECT balance FROM users WHERE id=?', (txn['user_id'],)).fetchone()['balance']
            db2.close()
            socketio.emit('recharge', {}, to=sid)
            socketio.emit('myInfo', {'balance': bal, 'userName': u['username'], 'userType': False, 'currency': 'KSH'}, to=sid)
    return jsonify({'status': True, 'message': 'Approved'})

@app.route('/api/admin/transaction/<int:tid>/reject', methods=['POST'])
@admin_required
def admin_reject_txn(current_user, tid):
    db = get_db()
    txn = db.execute('SELECT * FROM transactions WHERE id=?', (tid,)).fetchone()
    if not txn:
        db.close()
        return jsonify({'status': False, 'message': 'Not found'})
    # Refund withdrawal
    if txn['type'] == 'withdraw' and txn['status'] == 'pending':
        db.execute('UPDATE users SET balance=balance+? WHERE id=?', (txn['amount'], txn['user_id']))
    db.execute("UPDATE transactions SET status='rejected' WHERE id=?", (tid,))
    db.commit()
    db.close()
    return jsonify({'status': True, 'message': 'Rejected'})

@app.route('/api/admin/game-history', methods=['GET'])
@admin_required
def admin_game_history(current_user):
    db = get_db()
    rounds = db.execute('SELECT * FROM game_rounds ORDER BY id DESC LIMIT 50').fetchall()
    db.close()
    return jsonify({'status': True, 'data': [dict(r) for r in rounds]})

# ─── Static / Pages ───────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/static/avatars/<filename>')
def serve_avatar(filename):
    return send_from_directory('static/avatars', filename)

# ─── Main ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    t = threading.Thread(target=game_loop, daemon=True)
    t.start()
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
